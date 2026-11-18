"""Automatic grading for numeric, algebra, and multiple-choice answers."""
import re
import unicodedata
from typing import Optional, Tuple

import sympy
from sympy.parsing.sympy_parser import (
    convert_xor,
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)

# One number: the fraction form is tried before the plain form so that
# ``x = -4/3`` yields ``-4/3`` rather than ``-4`` (regex alternation is ordered).
_NUMBER_PATTERN = r"[-+]?(?:\d+/\d+|\d+(?:\.\d+)?)"
_NUMERIC_KEY_RE = re.compile(r"^\s*" + _NUMBER_PATTERN + r"\s*$")

# ``^`` means power (convert_xor), not Python XOR; ``2x`` means ``2*x``.
_ALGEBRA_TRANSFORMATIONS = standard_transformations + (
    convert_xor,
    implicit_multiplication_application,
)

# Unicode minus (U+2212), en dash, em dash, fullwidth hyphen → ASCII hyphen.
_MINUS_VARIANTS = "\u2212\u2013\u2014\uff0d"
# Multiplication signs that LLM output and sheet keys use for ``*``.
_TIMES_VARIANTS = "\u00d7\u00b7\u22c5"
_SUPERSCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_SUPERSCRIPT_RUN_RE = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+")


def _ascii_minus(s: str) -> str:
    s = str(s or "")
    for ch in _MINUS_VARIANTS:
        s = s.replace(ch, "-")
    return s


def _unicode_to_ascii(s: str) -> str:
    """Map Unicode math text to ASCII: italic letters (``𝑥``→``x``, NFKC),
    minus variants → ``-``, ``×``/``·`` → ``*``, superscript digits → ``^n``."""
    s = str(s or "")
    s = _SUPERSCRIPT_RUN_RE.sub(lambda m: "^" + m.group(0).translate(_SUPERSCRIPT_DIGITS), s)
    s = unicodedata.normalize("NFKC", s)
    s = _ascii_minus(s)
    for ch in _TIMES_VARIANTS:
        s = s.replace(ch, "*")
    return s


def grade(
    answer_type: str,
    student: str,
    key: str,
    mc_choices: Optional[str] = None,
) -> Tuple[int, str, Optional[str]]:
    """
    Grade a student answer against the answer key.

    Returns:
        (is_correct, rule_used, error_msg)
    """
    try:
        student = _unicode_to_ascii(student)
        key = _unicode_to_ascii(key)
        if answer_type == "numeric":
            return _grade_numeric(student, key)
        if answer_type == "algebra":
            if is_plain_numeric_key(key):
                return _grade_numeric_or_expression(student, key)
            return _grade_algebra(student, key)
        if answer_type == "mc":
            return _grade_mc(student, key, mc_choices)
        if answer_type == "string":
            return _grade_string(student, key)
        return (0, "unknown_type", f"Unknown answer_type: {answer_type}")
    except Exception as e:
        return (0, "parse_error", str(e))


def is_plain_numeric_key(key: str) -> bool:
    """True when the answer key is a plain number (no variables)."""
    k = str(key or "").strip()
    if not k:
        return False
    if re.search(r"[a-zA-Z]", k):
        return False
    return bool(_NUMERIC_KEY_RE.match(k))


def _grade_numeric(student: str, key: str) -> Tuple[int, str, Optional[str]]:
    """Grade numeric answer with tolerance.

    "Does not exist" / "DNE" / "undefined" / "infinity" / "∞" are non-numeric
    answers: wrong unless the key says the same thing.
    """
    tol = 1e-6
    s_token = non_numeric_answer_token(student)
    k_token = non_numeric_answer_token(key)
    if s_token or k_token:
        if s_token and k_token and s_token == k_token:
            return (1, "non_numeric_match", None)
        return (0, "non_numeric_mismatch", None)
    try:
        s_num = _parse_number(normalize_numeric(student))
        k_num = _parse_number(normalize_numeric(key))
    except Exception as e:
        return (0, "numeric_parse", str(e))

    is_correct = abs(s_num - k_num) < tol
    return (int(is_correct), "numeric_tolerance", None)


def _grade_numeric_or_expression(student: str, key: str) -> Tuple[int, str, Optional[str]]:
    """Grade plain-numeric keys: accept numbers, prose, equations, and simple expressions."""
    numeric_result = _grade_numeric(student, key)
    if numeric_result[0]:
        return numeric_result

    try:
        tol = 1e-6
        k_num = _parse_number(normalize_numeric(key))
        transformations = _ALGEBRA_TRANSFORMATIONS
        student_norm = normalize_algebra(student)
        if student_norm:
            s_expr = parse_expr(student_norm, transformations=transformations)
            s_num = float(sympy.N(s_expr))
            if abs(s_num - k_num) < tol:
                return (1, "numeric_expression", None)
    except Exception:
        pass

    return numeric_result


_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


def _algebra_student_text(student: str, key: str) -> str:
    """When the key is a bare expression, grade the student's RHS if they wrote an equation."""
    s = str(student or "").strip()
    k = str(key or "").strip()
    if "=" in s and "=" not in k:
        return s.split("=")[-1].strip()
    return s


def _labelled_key_rhs(key: str) -> str | None:
    """Return the RHS of a labelled key like ``m=2`` or ``m1=-1/3``.

    Only when there is exactly one ``=``, the left side is a single identifier
    that does not appear on the right (so ``y=2x+1`` stays an equation).
    """
    k = str(key or "").strip()
    if k.count("=") != 1:
        return None
    left, right = k.split("=", 1)
    left, right = left.strip(), right.strip()
    if not left or not right or not _IDENTIFIER_RE.match(left):
        return None
    if re.search(rf"(?<![A-Za-z0-9]){re.escape(left)}(?![A-Za-z0-9])", right):
        return None
    return right


def _strip_alternates(student: str) -> str:
    """Use primary answer before '(or ...)' alternates from batched LLM output."""
    s = str(student or "").strip()
    if " (or " in s.lower():
        s = re.split(r"\s*\(\s*or\s+", s, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    return s


def _take_balanced(s: str, i: int, open_ch: str, close_ch: str) -> tuple[str, int] | None:
    """If ``s[i]`` is ``open_ch``, return ``(inner, index_after_close)``."""
    if i >= len(s) or s[i] != open_ch:
        return None
    depth = 0
    for j in range(i, len(s)):
        if s[j] == open_ch:
            depth += 1
        elif s[j] == close_ch:
            depth -= 1
            if depth == 0:
                return s[i + 1 : j], j + 1
    return None


def _convert_roots(s: str) -> str:
    """``\\sqrt{a}`` → ``sqrt(a)``, ``\\sqrt[n]{a}`` → ``(a)**(1/n)``,
    ``√(a)`` / ``√a`` → ``sqrt(a)``."""
    s = str(s or "")
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        latex = re.match(r"\\+sqrt", s[i:], flags=re.IGNORECASE)
        if latex:
            j = i + latex.end()
            while j < n and s[j].isspace():
                j += 1
            index_n = None
            if j < n and s[j] == "[":
                taken_idx = _take_balanced(s, j, "[", "]")
                if taken_idx is None:
                    out.append(s[i])
                    i += 1
                    continue
                index_n = taken_idx[0].strip()
                j = taken_idx[1]
                while j < n and s[j].isspace():
                    j += 1
            if j < n and s[j] == "{":
                taken = _take_balanced(s, j, "{", "}")
                if taken is not None:
                    inner, j = taken
                    inner = _convert_roots(inner)
                    if index_n:
                        out.append(f"({inner})**(1/{index_n})")
                    else:
                        out.append(f"sqrt({inner})")
                    i = j
                    continue
            tok = re.match(r"[A-Za-z0-9]+", s[j:])
            if tok:
                out.append(f"sqrt({tok.group(0)})")
                i = j + tok.end()
                continue
            out.append(s[i])
            i += 1
            continue
        if s[i] == "√":
            j = i + 1
            while j < n and s[j].isspace():
                j += 1
            if j < n and s[j] == "(":
                taken = _take_balanced(s, j, "(", ")")
                if taken is not None:
                    inner, j = taken
                    out.append(f"sqrt({_convert_roots(inner)})")
                    i = j
                    continue
            if j < n and s[j] == "{":
                taken = _take_balanced(s, j, "{", "}")
                if taken is not None:
                    inner, j = taken
                    out.append(f"sqrt({_convert_roots(inner)})")
                    i = j
                    continue
            tok = re.match(r"[A-Za-z0-9]+", s[j:])
            if tok:
                out.append(f"sqrt({tok.group(0)})")
                i = j + tok.end()
                continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _latex_to_sympy(s: str) -> str:
    """Convert common LaTeX fragments from LLM output to sympy-friendly text."""
    s = _convert_roots(str(s or ""))
    s = re.sub(
        r"\\+frac\s*\{([^}]+)\}\s*\{([^}]+)\}",
        r"(\1)/(\2)",
        s,
        flags=re.IGNORECASE,
    )
    s = s.replace("π", "pi").replace("Π", "pi")
    return re.sub(r"\\+", "", s)


def _grade_algebra(student: str, key: str) -> Tuple[int, str, Optional[str]]:
    """Grade algebraic expression/equation using sympy equivalence."""
    student = _strip_alternates(_algebra_student_text(student, key))
    # Inverse of the student-RHS rule: a labelled key (m=2) against a bare
    # solve (2) compares against the key's right-hand side.
    if "=" not in student:
        rhs = _labelled_key_rhs(key)
        if rhs is not None:
            key = rhs
    student_norm = normalize_algebra(_latex_to_sympy(student))
    key_norm = normalize_algebra(_latex_to_sympy(key))

    try:
        transformations = _ALGEBRA_TRANSFORMATIONS
        s_expr = parse_expr(student_norm, transformations=transformations)
        k_expr = parse_expr(key_norm, transformations=transformations)
        diff = sympy.simplify(s_expr - k_expr)
        if diff == 0:
            return (1, "algebra_sympy", None)
        if _algebra_equations_equivalent(student, key, transformations):
            return (1, "algebra_equation", None)
        return (0, "algebra_sympy", None)
    except Exception as e:
        if _algebra_equations_equivalent(student, key, None):
            return (1, "algebra_equation", None)
        return (0, "algebra_parse", str(e))


def _algebra_equations_equivalent(
    student: str,
    key: str,
    transformations,
) -> bool:
    """True when two line/equation answers describe the same relation (e.g. 5x+3y=24 vs y=-5/3x+10)."""
    if transformations is None:
        transformations = _ALGEBRA_TRANSFORMATIONS
    s_raw = _latex_to_sympy(_strip_alternates(str(student or "")))
    k_raw = _latex_to_sympy(str(key or ""))
    if "=" not in s_raw or "=" not in k_raw:
        return False
    try:
        s_rel = _relation_expr(s_raw, transformations)
        k_rel = _relation_expr(k_raw, transformations)
        return sympy.simplify(s_rel - k_rel) == 0
    except Exception:
        return False


def _normalize_algebra_side(s: str) -> str:
    s = _latex_to_sympy(str(s or ""))
    return re.sub(r"\s+", "", s.strip())


def _relation_expr(text: str, transformations) -> sympy.Expr:
    left, right = text.split("=", 1)
    l = parse_expr(_normalize_algebra_side(left), transformations=transformations)
    r = parse_expr(_normalize_algebra_side(right), transformations=transformations)
    return sympy.simplify(l - r)


def _parse_relation(text: str, transformations) -> sympy.Eq | None:
    text = text.strip()
    if "=" not in text:
        return None
    left, right = text.split("=", 1)
    l = parse_expr(_normalize_algebra_side(left), transformations=transformations)
    r = parse_expr(_normalize_algebra_side(right), transformations=transformations)
    return sympy.Eq(l, r, evaluate=False)


def _grade_mc(student: str, key: str, mc_choices: Optional[str]) -> Tuple[int, str, Optional[str]]:
    """Grade multiple choice - match choice text, letter, or numeric-equivalent value."""
    for candidate in _mc_candidate_values(student):
        if _mc_values_equivalent(candidate, key):
            return (1, "mc_exact", None)

    if mc_choices:
        choices = [c.strip() for c in mc_choices.split("|")]
        choice_norms = [_normalize_excluded_mc(c) for c in choices]

        for candidate in _mc_candidate_values(student):
            cand_norm = _normalize_excluded_mc(candidate)
            if cand_norm in choice_norms:
                idx = choice_norms.index(cand_norm)
                if _mc_values_equivalent(choices[idx], key):
                    return (1, "mc_choice", None)

            cand_norm = normalize_mc(candidate)
            simple_norms = [normalize_mc(c) for c in choices]
            if cand_norm in simple_norms:
                idx = simple_norms.index(cand_norm)
                if _mc_values_equivalent(choices[idx], key):
                    return (1, "mc_choice", None)

            if len(cand_norm) == 1 and cand_norm.isalpha():
                idx = ord(cand_norm.upper()) - ord("A")
                if 0 <= idx < len(choices) and _mc_values_equivalent(choices[idx], key):
                    return (1, "mc_letter", None)

    return (0, "mc_mismatch", None)


def _mc_candidate_values(student: str) -> list[str]:
    """Extract plausible MC answer strings from raw LLM output."""
    s = str(student or "").strip()
    if not s:
        return []

    candidates: list[str] = []
    seen: set[str] = set()

    def _add(val: str) -> None:
        val = str(val or "").strip().rstrip(".,;")
        if val and val not in seen:
            seen.add(val)
            candidates.append(val)

    _add(s)
    first_line = s.splitlines()[0].strip()
    _add(first_line)

    extracted = normalize_numeric(s)
    _add(extracted)

    keyword = _extract_simple_mc_keyword(s)
    if keyword:
        _add(keyword)

    return candidates


def _extract_simple_mc_keyword(student: str) -> str | None:
    """Map prose to parallel / perpendicular / neither when unambiguous."""
    s = student.lower()
    hits = []
    for word in ("parallel", "perpendicular", "neither"):
        if re.search(rf"\b{word}\b", s):
            hits.append(word)
    if len(hits) == 1:
        return hits[0]
    return None


def _normalize_excluded_mc(s: str) -> str:
    """Normalize excluded-value MC strings for comparison."""
    s = normalize_mc(s)
    s = re.sub(r"^excluded values?:\s*", "", s)
    s = re.sub(r"\s+", " ", s)
    s = s.replace("x =", "x=").replace("x= ", "x=")
    return s.strip()


def _mc_values_equivalent(left: str, right: str) -> bool:
    if normalize_mc(left) == normalize_mc(right):
        return True
    if _normalize_excluded_mc(left) == _normalize_excluded_mc(right):
        return True
    try:
        tol = 1e-6
        l_num = _parse_number(normalize_numeric(left))
        r_num = _parse_number(normalize_numeric(right))
        return abs(l_num - r_num) < tol
    except Exception:
        return False


_LATEX_DELIMITER_RE = re.compile(r"\\+[\(\)\[\]]|\$+")
_LATEX_FRAC_RE = re.compile(
    r"(?P<sign>-?)\s*\\+[dt]?frac\s*\{\s*(?P<num>[^{}]*)\s*\}\s*\{\s*(?P<den>[^{}]*)\s*\}",
    flags=re.IGNORECASE,
)
_PLAIN_INT_RE = re.compile(r"^[-+]?\d+$")

# Non-numeric answers the numeric grader recognises as a value in their own
# right (wrong unless the key says the same thing). Order matters: the
# negative-infinity patterns must be tried before the plain ones.
_NON_NUMERIC_ANSWER_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("-infinity", re.compile(r"(?:-|\\-|negative\s+)\s*(?:∞|\\infty\b|infinity\b)", re.IGNORECASE)),
    ("infinity", re.compile(r"∞|\\infty\b|\binfinity\b|\binfinite\b", re.IGNORECASE)),
    ("dne", re.compile(r"\bdoes\s+not\s+exist\b|\bdoesn'?t\s+exist\b|\bdne\b|\bno\s+limit\b", re.IGNORECASE)),
    ("undefined", re.compile(r"\bundefined\b|\bnot\s+defined\b", re.IGNORECASE)),
)


def _latex_to_plain_number_text(s: str) -> str:
    r"""Strip ``\( \)``, ``\[ \]`` and ``$`` delimiters and turn
    ``\frac{a}{b}`` / ``\dfrac{a}{b}`` into ``a/b`` (``(a)/(b)`` when the parts
    are not plain integers), keeping a leading minus: ``$-\frac{7}{3}$`` → ``-7/3``.
    """
    s = str(s or "")
    s = _LATEX_DELIMITER_RE.sub(" ", s)

    def _frac(m: "re.Match[str]") -> str:
        sign, num, den = m.group("sign"), m.group("num").strip(), m.group("den").strip()
        if _PLAIN_INT_RE.match(num) and _PLAIN_INT_RE.match(den):
            neg = sign == "-"
            if num.startswith("-"):
                neg = not neg
                num = num[1:]
            num = num.lstrip("+")
            if den.startswith("-"):
                neg = not neg
                den = den[1:]
            den = den.lstrip("+")
            return f"{'-' if neg else ''}{num}/{den}"
        return f"{sign}({num})/({den})"

    return _LATEX_FRAC_RE.sub(_frac, s)


def non_numeric_answer_token(s: str) -> Optional[str]:
    """Canonical token for a non-numeric answer, or None.

    Returns ``"dne"``, ``"undefined"``, ``"infinity"`` or ``"-infinity"`` when
    the text says so and contains no digits (so ``"5, not undefined"`` is still
    read as a number).
    """
    text = _latex_to_plain_number_text(_unicode_to_ascii(s)).strip()
    if not text or re.search(r"\d", text):
        return None
    for token, pattern in _NON_NUMERIC_ANSWER_PATTERNS:
        if pattern.search(text):
            return token
    return None


def normalize_numeric(s: str) -> str:
    """Extract numeric value from string (x=3, prose wrappers, equations, or bare number).

    LaTeX delimiters are stripped and ``\\frac{a}{b}`` becomes ``a/b`` first,
    so ``$-\\frac{7}{3}$`` reads as ``-7/3`` rather than ``3``.

    Note: when multiple numbers appear (e.g. coordinate pairs), only the last
    number is compared — ``numeric`` is unsuitable for ordered pairs.
    """
    s = _latex_to_plain_number_text(_unicode_to_ascii(s)).strip()
    s = " ".join(s.split())

    var_eq = re.findall(
        r"([a-zA-Z])\s*=\s*(" + _NUMBER_PATTERN + r")",
        s,
    )
    if var_eq:
        return var_eq[-1][1]

    if re.search(r"\b(answer is|equals|equal to|result is)\b", s, flags=re.IGNORECASE):
        nums = re.findall(_NUMBER_PATTERN, s)
        if nums:
            return nums[-1]

    if "=" in s:
        rhs = s.split("=")[-1].strip()
        nums = re.findall(_NUMBER_PATTERN, rhs)
        if nums:
            return nums[-1]

    s = re.sub(r"^[a-zA-Z]\s*=\s*", "", s)
    compact = re.sub(r"\s+", "", s)
    if re.fullmatch(_NUMBER_PATTERN, compact):
        return compact
    nums = re.findall(_NUMBER_PATTERN, s)
    if nums:
        return nums[-1]
    return compact


def normalize_algebra(s: str) -> str:
    """Normalize algebraic expression for sympy parsing."""
    s = _unicode_to_ascii(s).strip()
    s = s.replace("π", "pi").replace("Π", "pi")
    s = re.sub(r"\s+", "", s)

    if "=" in s:
        parts = s.split("=")
        if len(parts) == 2:
            left, right = parts
            s = f"({left}) - ({right})"

    return s


def algebra_key_parses(key: str) -> bool:
    """True when ``key`` parses as one sympy expression (or one ``a=b``
    relation) under the grader's own normalization and transformations.

    Tuples such as ``(5,-1)`` parse but are not one expression the grader can
    compare, so they return False.
    """
    raw = _latex_to_sympy(_unicode_to_ascii(key)).strip()
    if not raw:
        return False
    try:
        if "=" in raw:
            if raw.count("=") != 1:
                return False
            expr = _relation_expr(raw, _ALGEBRA_TRANSFORMATIONS)
        else:
            expr = parse_expr(normalize_algebra(raw), transformations=_ALGEBRA_TRANSFORMATIONS)
        return not isinstance(expr, (tuple, list, sympy.Tuple))
    except Exception:
        return False


def normalize_mc(s: str) -> str:
    """Normalize MC answer by stripping whitespace and lowercasing."""
    return str(s or "").strip().lower().rstrip(".,;")


def normalize_string(s: str) -> str:
    """Lowercase, strip LaTeX, punctuation, and extra spaces for word answers."""
    s = _unicode_to_ascii(s)
    s = s.lower()
    s = re.sub(
        r"\\+frac\s*\{([^}]+)\}\s*\{([^}]+)\}",
        r"\1/\2",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(r"\\[a-zA-Z]+", " ", s)
    s = s.replace("$", " ")
    s = re.sub(r"[^\w\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _grade_string(student: str, key: str) -> Tuple[int, str, Optional[str]]:
    """Grade a word/phrase answer: exact after normalize, or key as a whole word."""
    s = normalize_string(student)
    k = normalize_string(key)
    if not k:
        return (0, "string_empty", None)
    if s == k:
        return (1, "string_exact", None)
    if re.search(rf"\b{re.escape(k)}\b", s):
        return (1, "string_contains", None)
    return (0, "string_mismatch", None)


def _parse_number(s: str) -> float:
    """Parse a number that may be a fraction or decimal."""
    if "/" in s:
        parts = s.split("/")
        if len(parts) == 2:
            return float(parts[0]) / float(parts[1])
    return float(s)
