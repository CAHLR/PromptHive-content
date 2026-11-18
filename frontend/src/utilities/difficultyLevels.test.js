import { test } from "node:test";
import assert from "node:assert/strict";
import {
  ANSWER_IN_TEXT_LABEL,
  ANSWER_KEY_UNVERIFIED_LABEL,
  INSUFFICIENT_SOLVES_LABEL,
  KEY_NOT_GRADABLE_LABEL,
  IMAGE_BASED_UNSCORED_LABEL,
  IMAGE_BASED_UNSCORED_TOOLTIP,
  isImageBasedQuestion,
  isScoredWithoutFigure,
  scoredDifficultyDisplayLabel,
  scoredDifficultyChipTooltip,
  unscoredChipLabel,
  unscoredChipTooltip,
} from "./difficultyLevels.js";

test("Images URL alone is not image-based (vision may attach)", () => {
  assert.equal(
    isImageBasedQuestion({
      images: "https://example.com/graph.png",
    }),
    false
  );
});

test("problemImages alone is not image-based without fail reason", () => {
  assert.equal(
    isImageBasedQuestion({
      bodyText: "Find the slope.",
      problemImages: "https://example.com/problem.png",
    }),
    false
  );
});

test("image_unavailable reason triggers image-based chip", () => {
  assert.equal(
    isImageBasedQuestion({
      images: "https://example.com/graph.png",
      reason: "image_unavailable",
    }),
    true
  );
  assert.equal(
    unscoredChipLabel({ reason: "image_unavailable" }),
    IMAGE_BASED_UNSCORED_LABEL
  );
  assert.equal(
    unscoredChipTooltip({ reason: "image_unavailable" }),
    IMAGE_BASED_UNSCORED_TOOLTIP
  );
});

test("scored item with figureUnavailable keeps normal chip path", () => {
  assert.equal(
    isImageBasedQuestion({
      images: "https://example.com/graph.png",
      figureUnavailable: true,
      scored: true,
      difficultyLogit: -0.5,
    }),
    false
  );
});

test("figureAttached false marks scored-without-figure chip label", () => {
  const score = {
    difficultyLogit: 0.4,
    difficultyLabel: "Medium",
    figureAttached: false,
    figureCount: 0,
    figureError: "SSL error: certificate verify failed",
    scored: true,
  };
  assert.equal(isScoredWithoutFigure(score), true);
  assert.equal(
    scoredDifficultyDisplayLabel(score),
    "Medium (0.40) · scored without figure"
  );
  assert.match(
    scoredDifficultyChipTooltip(score),
    /Scored without figure: SSL error/
  );
});

test("figureAttached true keeps plain scored label", () => {
  const score = {
    difficultyLogit: 0.4,
    difficultyLabel: "Medium",
    figureAttached: true,
    figureCount: 1,
    scored: true,
  };
  assert.equal(isScoredWithoutFigure(score), false);
  assert.equal(scoredDifficultyDisplayLabel(score), "Medium (0.40)");
});

test("item with no figure fields keeps plain scored label", () => {
  const score = {
    difficultyLogit: -1.2,
    difficultyLabel: "Easy",
    scored: true,
  };
  assert.equal(isScoredWithoutFigure(score), false);
  assert.equal(scoredDifficultyDisplayLabel(score), "Easy (-1.20)");
});

test("detects graph phrasing in body text when no Images URL", () => {
  assert.equal(
    isImageBasedQuestion({
      bodyText: "Use the graph shown below to estimate the intercept.",
    }),
    true
  );
});

test("detects figure phrasing in combined problem and step text", () => {
  assert.equal(
    isImageBasedQuestion({
      problemBodyText: "A triangle is shown.",
      bodyText: "Refer to the figure above.",
    }),
    true
  );
});

test("does not treat figure out as image-based", () => {
  assert.equal(
    isImageBasedQuestion({
      bodyText: "Figure out the value of x when y = 3.",
    }),
    false
  );
});

test("returns false for plain text-only stems", () => {
  assert.equal(
    isImageBasedQuestion({
      bodyText: "Solve for x: 2x + 5 = 11.",
    }),
    false
  );
});

test("image-based text label takes priority over saturation direction", () => {
  const input = {
    bodyText: "From the graph, what is the y-intercept?",
    reason: "zero_variance",
    saturationDirection: "all_wrong",
  };
  assert.equal(unscoredChipLabel(input), IMAGE_BASED_UNSCORED_LABEL);
  assert.equal(unscoredChipTooltip(input), IMAGE_BASED_UNSCORED_TOOLTIP);
});

test("text-only saturation labels remain unchanged", () => {
  const input = {
    bodyText: "Solve for x.",
    reason: "zero_variance",
    saturationDirection: "all_wrong",
  };
  assert.equal(
    unscoredChipLabel(input),
    "Couldn't measure: too hard (all wrong)"
  );
});

test("key_not_gradable shows the unsupported-format chip", () => {
  assert.equal(
    unscoredChipLabel({ reason: "key_not_gradable" }),
    KEY_NOT_GRADABLE_LABEL
  );
  assert.equal(
    unscoredChipLabel({ difficultyStatus: "key_not_gradable" }),
    "Can't score: answer format not supported"
  );
});

test("insufficient_solves shows the key-check-did-not-finish chip", () => {
  assert.equal(INSUFFICIENT_SOLVES_LABEL, "Can't score: key check did not finish");
  assert.equal(
    unscoredChipLabel({ reason: "insufficient_solves" }),
    INSUFFICIENT_SOLVES_LABEL
  );
  // Backend rows: difficultyStatus is answer_key_unverified, the row's
  // answerKeyCheckReason says the solver never finished.
  const row = {
    difficultyStatus: "answer_key_unverified",
    answerKeyCheck: "unverified",
    answerKeyCheckReason: "insufficient_solves",
    answerKeySolves: [],
  };
  assert.equal(unscoredChipLabel(row), INSUFFICIENT_SOLVES_LABEL);
  assert.match(unscoredChipTooltip(row), /cut off or failed/);
  // A real disagreement keeps the existing unverified chip.
  assert.equal(
    unscoredChipLabel({
      difficultyStatus: "answer_key_unverified",
      answerKeyCheck: "unverified",
      answerKeySolves: ["7", "8", "9"],
    }),
    ANSWER_KEY_UNVERIFIED_LABEL
  );
});

test("answer_in_text shows the leaked-answer chip", () => {
  assert.equal(
    unscoredChipLabel({ reason: "answer_in_text" }),
    ANSWER_IN_TEXT_LABEL
  );
  assert.equal(
    unscoredChipLabel({ difficultyStatus: "answer_in_text" }),
    "Can't score: answer shown in question text"
  );
});
