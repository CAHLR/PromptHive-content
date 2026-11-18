/** User-facing difficulty levels mapped to Rasch logit ranges (matches backend). */
export const DIFFICULTY_LEVELS = [
  { level: 1, label: "Very Easy", minLogit: -4.0, maxLogit: -1.0 },
  { level: 2, label: "Easy", minLogit: -1.0, maxLogit: -0.3 },
  { level: 3, label: "Medium", minLogit: -0.3, maxLogit: 0.3 },
  { level: 4, label: "Hard", minLogit: 0.3, maxLogit: 1.0 },
  { level: 5, label: "Very Hard", minLogit: 1.0, maxLogit: 4.0 },
];

export const DEFAULT_MIN_LEVEL = 1;
export const DEFAULT_MAX_LEVEL = 5;

export function logitRangeFromLevels(minLevel, maxLevel) {
  const lo = Math.max(1, Math.min(minLevel, maxLevel));
  const hi = Math.min(5, Math.max(minLevel, maxLevel));
  const minLogit = DIFFICULTY_LEVELS[lo - 1].minLogit;
  const maxLogit = DIFFICULTY_LEVELS[hi - 1].maxLogit;
  const labels = DIFFICULTY_LEVELS.slice(lo - 1, hi).map((l) => l.label);
  return { minLogit, maxLogit, labels, minLevel: lo, maxLevel: hi };
}

/** UI summary for optional post-hoc difficulty filter (default: no limit). */
export function difficultyFilterDisplaySummary(
  filterEnabled,
  minLevel,
  maxLevel
) {
  if (!filterEnabled) {
    return {
      filterEnabled: false,
      labels: ["No limit"],
      minLogit: null,
      maxLogit: null,
      minLevel: null,
      maxLevel: null,
      rangeText: "No limit",
      boundsText: "−∞ to +∞",
    };
  }
  const summary = logitRangeFromLevels(minLevel, maxLevel);
  return {
    filterEnabled: true,
    ...summary,
    rangeText: summary.labels.join(", "),
    boundsText: `${summary.minLogit.toFixed(1)} to ${summary.maxLogit.toFixed(1)}`,
  };
}

function stepPassesLogitFilter(step, minLogit, maxLogit, includeUnscored) {
  if (step?.difficultyScored === false || step?.difficultyLogit == null) {
    return includeUnscored;
  }
  const logit = Number(step.difficultyLogit);
  if (Number.isNaN(logit)) {
    return includeUnscored;
  }
  return logit >= minLogit && logit <= maxLogit;
}

/** Post-hoc filter for Feature 2 row scores (display-only; no API calls). */
export function scorePassesPostHocFilter(
  score,
  { filterEnabled, minLevel, maxLevel, includeUnscored = true }
) {
  if (!filterEnabled) {
    return true;
  }
  if (
    !score ||
    !score.scored ||
    score.difficultyLogit == null ||
    score.difficultyLogit === undefined
  ) {
    return includeUnscored;
  }
  const logit = Number(score.difficultyLogit);
  if (Number.isNaN(logit)) {
    return includeUnscored;
  }
  const { minLogit, maxLogit } = logitRangeFromLevels(minLevel, maxLevel);
  return logit >= minLogit && logit <= maxLogit;
}

/**
 * Post-hoc filter on already-scored generated questions (no API calls).
 * Keeps companion problem rows when at least one step in the exercise passes.
 */
export function filterGeneratedQuestionsPostHoc(
  questions,
  { filterEnabled, minLevel, maxLevel, includeUnscored = true }
) {
  if (!Array.isArray(questions) || questions.length === 0) {
    return [];
  }
  if (!filterEnabled) {
    return questions;
  }
  const { minLogit, maxLogit } = logitRangeFromLevels(minLevel, maxLevel);

  const groups = [];
  let current = [];
  for (const question of questions) {
    const rowType = String(question?.["Row Type"] || question?.rowType || "")
      .trim()
      .toLowerCase();
    if (rowType === "problem" && current.length > 0) {
      groups.push(current);
      current = [question];
    } else {
      current.push(question);
    }
  }
  if (current.length > 0) {
    groups.push(current);
  }

  const filtered = [];
  for (const group of groups) {
    const steps = group.filter(
      (q) =>
        String(q?.["Row Type"] || q?.rowType || "")
          .trim()
          .toLowerCase() === "step"
    );
    const keptSteps = steps.filter((step) =>
      stepPassesLogitFilter(step, minLogit, maxLogit, includeUnscored)
    );
    if (keptSteps.length === 0) {
      continue;
    }
    const keptSet = new Set(keptSteps);
    for (const row of group) {
      const rowType = String(row?.["Row Type"] || row?.rowType || "")
        .trim()
        .toLowerCase();
      if (rowType === "problem") {
        filtered.push(row);
      } else if (keptSet.has(row)) {
        filtered.push(row);
      }
    }
  }
  return filtered;
}

export function labelForLogit(logit) {
  if (logit === null || logit === undefined || Number.isNaN(logit)) {
    return null;
  }
  for (const level of DIFFICULTY_LEVELS) {
    if (logit >= level.minLogit && logit <= level.maxLogit) {
      return level.label;
    }
  }
  if (logit < DIFFICULTY_LEVELS[0].minLogit) {
    return DIFFICULTY_LEVELS[0].label;
  }
  return DIFFICULTY_LEVELS[DIFFICULTY_LEVELS.length - 1].label;
}

export function levelMarks() {
  return DIFFICULTY_LEVELS.reduce((acc, l) => {
    acc[l.level] = l.label;
    return acc;
  }, {});
}

/** Human-readable labels for Feature 2 score `reason` codes (backend unchanged). */
export const SCORE_REASON_LABELS = {
  saturated_inferred_hard: "everyone wrong (legacy)",
  saturated_inferred_easy: "everyone correct (legacy)",
  saturated_low_confidence: "saturated · IRT estimate",
  saturated_fallback_no_rasch: "saturated · no IRT estimate",
  zero_variance: "no variance",
  insufficient_informative_items: "insufficient data",
  in_range: "measured",
  outside_logit_range: "outside range",
  no_rasch_estimate: "no estimate",
  not_calibratable: "not scorable",
  intermediate_step_key: "intermediate step key",
  answer_key_unverified: "answer key unverified",
  insufficient_solves: "key check did not finish",
  answer_in_text: "answer shown in question text",
  key_not_gradable: "answer format not supported",
  image_unavailable: "image unavailable",
  image_based: "image-based",
  companion_problem_row: "problem row",
};

/** Longer tooltip text for low-confidence reasons. */
export const SCORE_REASON_TOOLTIPS = {
  intermediate_step_key:
    "The answer key is an intermediate algebra step (e.g. 3x=15), not the final numeric answer. Difficulty was not scored.",
  answer_key_unverified:
    "The checker's solutions did not agree with the key or with each other. Check the answer by hand.",
  insufficient_solves:
    "The answer-key checker's solver replies were cut off or failed before giving a final answer, so the key was not confirmed and the item was not scored. Check the answer by hand.",
  answer_in_text:
    "The step text already contains the final answer, so the item was not scored.",
  key_not_gradable:
    "This answer key has more than one value (or another form the grader cannot score).",
  saturated_low_confidence:
    "Difficulty uncertain: all simulated respondents answered identically. Shown value is the Rasch item difficulty (b) from the flip-trick calibration; standard error reflects weak identification.",
  saturated_fallback_no_rasch:
    "All respondents answered identically and the IRT model did not return an item difficulty. Band midpoint used as last resort.",
  saturated_inferred_hard:
    "Legacy reason: use saturated_low_confidence.",
  saturated_inferred_easy:
    "Legacy reason: use saturated_low_confidence.",
  zero_variance:
    "All students answered the same. Try increasing the number of respondents.",
  insufficient_informative_items:
    "Not enough data to measure difficulty. Try increasing to at least 20 respondents and 4 questions.",
  no_rasch_estimate:
    "Could not estimate difficulty. Try increasing the number of respondents.",
  image_unavailable:
    "This question needs a figure, but the figure could not be loaded for the simulated respondent.",
  image_based:
    "This question needs a figure, but the figure could not be loaded for the simulated respondent.",
};

const INSUFFICIENT_SATURATION_HINT =
  "Not enough variation to calibrate difficulty. Try more respondents or a wider mix of question difficulty.";

export const IMAGE_BASED_UNSCORED_LABEL = "Can't score: image-based";
export const IMAGE_BASED_UNSCORED_TOOLTIP =
  "This question needs a figure, but the figure could not be loaded for the simulated respondent.";

export const ANSWER_KEY_UNVERIFIED_LABEL = "Can't score: answer key unverified";
export const INSUFFICIENT_SOLVES_LABEL = "Can't score: key check did not finish";
export const ANSWER_IN_TEXT_LABEL = "Can't score: answer shown in question text";
export const KEY_NOT_GRADABLE_LABEL = "Can't score: answer format not supported";

/** Chip shown next to the difficulty badge when the key check replaced the key. */
export const ANSWER_KEY_CORRECTED_CHIP_LABEL = "Answer key corrected";

/** True when the answer-key check replaced this row's key before scoring. */
export function isAnswerKeyCorrected(questionOrScore) {
  return (
    !!questionOrScore &&
    typeof questionOrScore === "object" &&
    questionOrScore.answerKeyCheck === "corrected"
  );
}

/**
 * Tooltip for the "Answer key corrected" chip. Accepts a generated question
 * (``Answer`` already replaced) or an existing-row score object carrying
 * ``answerKeyCorrected``.
 */
export function answerKeyCorrectedChipTooltip(questionOrScore) {
  const original = String(questionOrScore?.answerKeyOriginal ?? "").trim();
  const corrected = String(
    questionOrScore?.Answer ??
      questionOrScore?.answerKeyCorrected ??
      questionOrScore?.answer ??
      ""
  ).trim();
  return `The generator wrote ${original}. The checker solved it as ${corrected} and the key was replaced before scoring.`;
}

const IMAGE_TEXT_KEYWORD_PATTERNS = [
  /\bimage\b/i,
  /\bgraph\b/i,
  /\bfigure\b(?!\s+out\b)/i,
  /\bdiagram\b/i,
  /\bchart\b/i,
  /\bplot\b/i,
  /\bpicture\b/i,
  /\billustration\b/i,
];

const IMAGE_TEXT_PHRASES = [
  "shown below",
  "shown above",
  "as shown",
  "see the figure",
  "refer to the figure",
  "from the graph",
  "in the graph",
  "on the graph",
  "the graph shows",
  "the graph is shown",
  "given an image",
  "we are given an image",
  "from the figure",
  "in the figure",
  "the figure shows",
];

const IMAGE_UNAVAILABLE_REASONS = new Set([
  "image_unavailable",
  "image_based",
]);

function hasNonEmptyImageField(value) {
  const raw = String(value ?? "").trim();
  if (!raw) {
    return false;
  }
  return raw.split(/\s+/).some((token) => token.length > 0);
}

function combinedImageDetectionText(input) {
  if (!input || typeof input !== "object") {
    return "";
  }
  const stepBody = String(input.bodyText ?? input["Body Text"] ?? "").trim();
  const title = String(input.title ?? input.Title ?? "").trim();
  const problemBody = String(
    input.problemBodyText ?? input.problemBody ?? ""
  ).trim();
  return [problemBody, title, stepBody].filter(Boolean).join(" ");
}

function textReferencesImage(text) {
  const normalized = String(text || "").replace(/\s+/g, " ").trim();
  if (!normalized) {
    return false;
  }
  const lower = normalized.toLowerCase();
  if (IMAGE_TEXT_KEYWORD_PATTERNS.some((pattern) => pattern.test(lower))) {
    return true;
  }
  return IMAGE_TEXT_PHRASES.some((phrase) => lower.includes(phrase));
}

/**
 * True when the item should show the image-based unscored chip.
 * After vision support: non-empty Images alone is NOT enough (backend may
 * attach figures). Prefer backend reason image_unavailable; otherwise flag
 * text that references a figure when no Images URL is present.
 */
export function isImageBasedQuestion(input) {
  if (!input || typeof input !== "object") {
    return false;
  }
  // Text-only calibration after a figure miss still gets a scored chip
  // (marked "scored without figure"), not the unscored image-based chip.
  if (
    input.figureAttached === false ||
    (input.figureUnavailable &&
      (input.scored === true ||
        (input.difficultyLogit != null && input.difficultyLogit !== undefined)))
  ) {
    return false;
  }
  const reason = unscoredReasonFromInput(input);
  if (IMAGE_UNAVAILABLE_REASONS.has(String(reason || "").trim())) {
    return true;
  }
  const hasImages =
    hasNonEmptyImageField(input.images ?? input.Images ?? input.stepImages) ||
    hasNonEmptyImageField(input.problemImages ?? input.problemImageUrls);
  if (hasImages) {
    // Backend attempted (or will attempt) vision attach — do not preempt.
    return false;
  }
  return textReferencesImage(combinedImageDetectionText(input));
}

function couldntMeasureDirectionLabel(direction) {
  if (direction === "all_correct") {
    return "Couldn't measure: too easy (all correct)";
  }
  if (direction === "all_wrong") {
    return "Couldn't measure: too hard (all wrong)";
  }
  return null;
}

export function isQuestionDifficultyScored(question) {
  if (!question || question.difficultyScored === false) {
    return false;
  }
  const logit = question.difficultyLogit;
  if (logit == null || logit === undefined) {
    return false;
  }
  return !Number.isNaN(Number(logit));
}

function unscoredReasonFromInput(questionOrScore) {
  if (typeof questionOrScore === "string") {
    return questionOrScore;
  }
  const reason =
    questionOrScore?.difficultyStatus || questionOrScore?.reason || "";
  // The backend holds a row out with ``answer_key_unverified`` whichever way
  // the key check failed; ``answerKeyCheckReason`` says whether the solver
  // actually disagreed or simply never finished (``insufficient_solves``).
  if (
    reason === "answer_key_unverified" &&
    questionOrScore?.answerKeyCheckReason === "insufficient_solves"
  ) {
    return "insufficient_solves";
  }
  return reason;
}

function saturationDirectionFromInput(questionOrScore) {
  if (!questionOrScore || typeof questionOrScore === "string") {
    return undefined;
  }
  return questionOrScore.saturationDirection;
}

/** Short chip label for unscored generated/calibrated questions (display only). */
export function unscoredChipLabel(questionOrScore) {
  if (isImageBasedQuestion(questionOrScore)) {
    return IMAGE_BASED_UNSCORED_LABEL;
  }
  const reason = unscoredReasonFromInput(questionOrScore);
  const direction = saturationDirectionFromInput(questionOrScore);
  const directed = couldntMeasureDirectionLabel(direction);
  if (
    directed &&
    (reason === "zero_variance" || reason === "insufficient_informative_items")
  ) {
    return directed;
  }
  switch (reason) {
    case "insufficient_informative_items":
      return "Couldn't measure";
    case "zero_variance":
      return "Couldn't measure";
    case "no_rasch_estimate":
      return "Couldn't measure";
    case "intermediate_step_key":
      return "Intermediate step key";
    case "answer_key_unverified":
      return ANSWER_KEY_UNVERIFIED_LABEL;
    case "insufficient_solves":
      return INSUFFICIENT_SOLVES_LABEL;
    case "answer_in_text":
      return ANSWER_IN_TEXT_LABEL;
    case "key_not_gradable":
      return KEY_NOT_GRADABLE_LABEL;
    case "image_unavailable":
    case "image_based":
      return IMAGE_BASED_UNSCORED_LABEL;
    default:
      return "Unscored";
  }
}

/** Tooltip for unscored chips — fuller explanation for hover. */
export function unscoredChipTooltip(questionOrScore) {
  if (isImageBasedQuestion(questionOrScore)) {
    return IMAGE_BASED_UNSCORED_TOOLTIP;
  }
  const reason = unscoredReasonFromInput(questionOrScore);
  const direction = saturationDirectionFromInput(questionOrScore);
  if (reason === "insufficient_informative_items" && direction) {
    const parts = [];
    const dir = saturationDirectionLabel(direction);
    if (dir) parts.push(dir);
    parts.push(INSUFFICIENT_SATURATION_HINT);
    return parts.join(" ");
  }
  if (reason === "zero_variance" && direction) {
    return reasonTooltipForScore(reason, { saturationDirection: direction });
  }
  if (reason && SCORE_REASON_TOOLTIPS[reason]) {
    return SCORE_REASON_TOOLTIPS[reason];
  }
  if (reason) {
    return reasonLabelForScore(reason);
  }
  return "Difficulty could not be measured.";
}

export function scoredDifficultyDisplayLabel(question) {
  const logit = Number(question.difficultyLogit);
  const label =
    question?.difficultyLabel || labelForLogit(logit);
  let text = `${label} (${logit.toFixed(2)})`;
  if (question?.figureAttached === false) {
    text += " · scored without figure";
  }
  return text;
}

/** True when the item had a figure but respondents saw text only. */
export function isScoredWithoutFigure(questionOrScore) {
  return questionOrScore?.figureAttached === false;
}

export function scoredDifficultyChipTooltip(questionOrScore) {
  if (isScoredWithoutFigure(questionOrScore)) {
    const err = questionOrScore?.figureError
      ? String(questionOrScore.figureError)
      : "";
    return err
      ? `Scored without figure: ${err}`
      : "Scored without figure: the image could not be loaded for simulated respondents; difficulty was estimated from text only.";
  }
  return reasonTooltipForScore(
    questionOrScore?.reason,
    questionOrScore
  ) || "Difficulty score";
}

export function saturationDirectionLabel(direction) {
  if (direction === "all_correct") return "All respondents answered correctly.";
  if (direction === "all_wrong") return "All respondents answered incorrectly.";
  return "";
}

export function reasonLabelForScore(reason) {
  if (!reason) return "";
  return SCORE_REASON_LABELS[reason] || String(reason).replace(/_/g, " ");
}

export function reasonTooltipForScore(reason, score) {
  if (!reason && !score) return "";
  const parts = [];
  const base =
    SCORE_REASON_TOOLTIPS[reason] ||
    (reason ? reasonLabelForScore(reason) : "");
  if (base) parts.push(base);
  if (score?.saturationDirection) {
    const dir = saturationDirectionLabel(score.saturationDirection);
    if (dir) parts.push(dir);
  }
  if (
    score?.seDifficulty !== null &&
    score?.seDifficulty !== undefined &&
    !Number.isNaN(Number(score.seDifficulty))
  ) {
    parts.push(
      `Standard error (item b): ${Number(score.seDifficulty).toFixed(2)}`
    );
  }
  if (parts.length) return parts.join(" ");
  return reasonLabelForScore(reason);
}
