from functools import partial
from typing import Literal

from pydantic import BaseModel, Field

from abscribe_backend.services.latex_converter_service import preprocess_text_to_latex

OATUTOR_HINT_EXAMPLE_JSON = {
    "hints": [
        {
            "Problem Name": "{{problemName}}",
            "Row Type": "<hint or scaffold>",
            "Title": "<title of the hint>",
            "Body Text": "<body text of the hint>",
            "Answer": "<answer if the Row Type is scaffold, blank otherwise>",
            "answerType": "<numeric or algebra or mc>",
            "HintID": "<hX where x is the unique hint number.>",
            "Dependency": "<id of hint on which this hint depends on, keep blank if no dependency>",
            "mcChoices": "<answer choices if answerType is mc, blank otherwise. Seperate answer choices with |>",
        },
    ],
}

_convert_rendered = partial(preprocess_text_to_latex, tutoring=False)
_convert_tutoring = partial(preprocess_text_to_latex, tutoring=True)
_convert_mc = partial(preprocess_text_to_latex, tutoring=True, stepMC=True)


class OatutorHint(BaseModel):
    problem_name: str
    row_type: Literal["hint", "scaffold"]
    title: str
    body_text: str
    answer: str
    answer_type: Literal["numeric", "algebra", "mc"]
    hint_id: str
    dependency: str
    mc_choices: str

    def to_json(self) -> dict:
        return {
            "Problem Name": self.problem_name,
            "Row Type": self.row_type,
            "Title": self.title,
            "Title Rendered": _convert_rendered(self.title)[0],
            "Title Tutoring": _convert_tutoring(self.title)[0],
            "Body Text": self.body_text,
            "Body Text Rendered": _convert_rendered(self.body_text)[0],
            "Body Text Tutoring": _convert_tutoring(self.body_text)[0],
            "Answer": self.answer,
            "Answer Rendered": _convert_rendered(self.answer)[0],
            "Answer Tutoring": _convert_tutoring(self.answer)[0],
            "answerMCR": (
                _convert_mc(self.answer)[0] if self.answer_type == "mc" else ""
            ),
            "answerType": self.answer_type,
            "answerType Rendered": _convert_rendered(self.answer_type)[0],
            "answerType Tutoring": _convert_tutoring(self.answer_type)[0],
            "HintID": self.hint_id,
            "Dependency": self.dependency,
            "mcChoices": self.mc_choices,
            "mcChoicesMCR": (
                "<separator_eba4b31e>".join(
                    map(
                        lambda text: _convert_mc(text)[0],
                        self.mc_choices.split("|"),
                    )
                )
                if self.answer_type == "mc"
                else ""
            ),
        }


class OatutorHints(BaseModel):
    hints: list[OatutorHint]

    def to_json(self) -> dict:
        return {"hints": list(map(OatutorHint.to_json, self.hints))}
    
class OatutorQuestion(BaseModel):
    problem_name: str
    row_type: Literal["problem", "step"]
    title: str = ""
    body_text: str = Field(
        description=(
            "The problem text or step prompt: state only what to find. "
            "Any working goes in the solution field, never in this text."
        )
    )
    # Worked solution for step rows: brief steps ending with the final value.
    # Empty for problem rows. Sits before ``answer`` so the structured output
    # has room to work before committing to the answer key.
    solution: str = ""
    answer: str
    answer_type: Literal["numeric", "algebra", "mc"]
    mc_choices: str

    def to_json(self) -> dict:
        return {
            "Problem Name": self.problem_name,
            "Row Type": self.row_type,
            "Title": self.title,
            "Title Rendered": _convert_rendered(self.title)[0],
            "Title Tutoring": _convert_tutoring(self.title)[0],
            "Body Text": self.body_text,
            "Body Text Rendered": _convert_rendered(self.body_text)[0],
            "Body Text Tutoring": _convert_tutoring(self.body_text)[0],
            # Not a sheet column; rides along with the row (row builders in the
            # frontend only copy keys present in the sheet's column map).
            "Solution": self.solution,
            "Answer": self.answer,
            "Answer Rendered": _convert_rendered(self.answer)[0],
            "Answer Tutoring": _convert_tutoring(self.answer)[0],
            "answerMCR": (
                _convert_mc(self.answer)[0] if self.answer_type == "mc" else ""
            ),
            "answerType": self.answer_type,
            "answerType Rendered": _convert_rendered(self.answer_type)[0],
            "answerType Tutoring": _convert_tutoring(self.answer_type)[0],
            "mcChoices": self.mc_choices,
            "mcChoicesMCR": (
                "<separator_eba4b31e>".join(
                    map(
                        lambda text: _convert_mc(text)[0],
                        self.mc_choices.split("|"),
                    )
                )
                if self.answer_type == "mc"
                else ""
            ),
        }
    
class OatutorQuestions(BaseModel):
    questions: list[OatutorQuestion]

    def to_json(self) -> dict:
        return {"questions": list(map(OatutorQuestion.to_json, self.questions))}