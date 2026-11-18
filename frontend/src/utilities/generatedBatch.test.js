import { test } from "node:test";
import assert from "node:assert/strict";
import {
  applyGenerationStartState,
  assignGeneratedProblemNames,
  isGeneratedProblemName,
  sanitizeTemplateProblemNameBase,
} from "./sheetDifficultyUtils.js";
import {
  buildDifficultyDistribution,
  collectLatestStepDifficulty,
  collectPreviousStepDifficulty,
} from "./difficultyDistribution.js";
import { stepProblemNamesFromGeneratedQuestions } from "./sheetDifficultyUtils.js";

const columnIndexMap = {
  problemName: 0,
  rowType: 1,
  title: 2,
  bodyText: 3,
  answer: 4,
  answerType: 5,
  mcChoices: 6,
  oerSrc: 7,
  images: 8,
};

const lessonContentAttributes = {
  problemName: "Problem Name",
  rowType: "Row Type",
  title: "Title",
  bodyText: "Body Text",
  answer: "Answer",
  answerType: "answerType",
  mcChoices: "mcChoices",
  images: "Images",
};

const lessonBase = [
  ["p1", "problem", "P1", "body", "", "", "", "", ""],
  ["p1", "step", "S1", "step body", "1", "numeric", "", "", ""],
];

function makeLlmBatch(llmName, logit) {
  return [
    {
      "Problem Name": llmName,
      "Row Type": "problem",
      Title: "Gen problem",
      "Body Text": "gen body",
      difficultyLogit: logit,
      difficultyScored: true,
      difficultyLabel: "Medium",
      _sourceTemplateProblemName: "algebra_easy",
    },
    {
      "Problem Name": llmName,
      "Row Type": "step",
      Title: "Gen step",
      "Body Text": "gen step",
      Answer: "2",
      difficultyLogit: logit,
      difficultyScored: true,
      difficultyLabel: "Medium",
      _sourceTemplateProblemName: "algebra_easy",
    },
  ];
}

test("isGeneratedProblemName matches legacy _gen and new _b_e patterns", () => {
  assert.equal(isGeneratedProblemName("algebra_easy_gen1"), true);
  assert.equal(isGeneratedProblemName("algebra_easy_b1_e1"), true);
  assert.equal(isGeneratedProblemName("algebra_easy_b2_e3_v1"), true);
  assert.equal(isGeneratedProblemName("algebra_easy"), false);
});

test("assignGeneratedProblemNames ignores colliding LLM names", () => {
  const llmBatch = [
    ...makeLlmBatch("algebra_easy_gen1", 0.1),
    ...makeLlmBatch("algebra_easy1_gen1", 0.2),
  ];
  const named = assignGeneratedProblemNames(llmBatch, {
    existingProblemNames: [],
    batchSeq: 1,
  });
  const problemNames = new Set(
    named.map((q) => q["Problem Name"]).filter(Boolean)
  );
  assert.equal(problemNames.size, 2);
  assert.ok([...problemNames].every((name) => name.includes("_b1_e")));
  assert.equal(named[0].llmProblemName, "algebra_easy_gen1");
  assert.equal(named[2].llmProblemName, "algebra_easy1_gen1");
});

test("assignGeneratedProblemNames uses distinct batch prefixes", () => {
  const batch1 = assignGeneratedProblemNames(makeLlmBatch("same_llm_name", 0), {
    existingProblemNames: [],
    batchSeq: 1,
  });
  const batch2 = assignGeneratedProblemNames(makeLlmBatch("same_llm_name", 0), {
    existingProblemNames: batch1.map((q) => q["Problem Name"]),
    batchSeq: 2,
  });
  assert.match(batch1[0]["Problem Name"], /_b1_e1$/);
  assert.match(batch2[0]["Problem Name"], /_b2_e1$/);
});

test("sanitizeTemplateProblemNameBase strips old generated suffixes", () => {
  assert.equal(sanitizeTemplateProblemNameBase("algebra_easy_gen3"), "algebra_easy");
  assert.equal(sanitizeTemplateProblemNameBase("algebra_easy_b1_e2"), "algebra_easy");
});

test("clean sheet: empty outgoing clears generated store without promotion", () => {
  const prevState = {
    selectedSheetData: lessonBase,
    baseSelectedSheetData: lessonBase,
    generatedScoredQuestionsAll: makeLlmBatch("x", 0.5),
    priorGeneratedScoredQuestionsAll: [],
  };
  const next = applyGenerationStartState({
    prevState,
    sheetData: lessonBase,
    outgoingBatch: [],
    columnIndexMap,
    lessonContentAttributes,
  });
  assert.deepEqual(next.generatedScoredQuestionsAll, []);
  assert.deepEqual(next.baseSelectedSheetData, lessonBase);
});

test("promotion does not re-insert batch already in accumulated base", () => {
  const batch1Named = assignGeneratedProblemNames(makeLlmBatch("llm", 0.2), {
    existingProblemNames: [],
    batchSeq: 1,
  });
  const baseAfterGen1 = [
    ...lessonBase,
    ["algebra_easy_b1_e1", "problem", "Gen problem", "gen body", "", "", "", "openai", ""],
    ["algebra_easy_b1_e1", "step", "Gen step", "gen step", "2", "numeric", "", "openai", ""],
  ];
  const next = applyGenerationStartState({
    prevState: {
      selectedSheetData: baseAfterGen1,
      baseSelectedSheetData: baseAfterGen1,
      generatedScoredQuestionsAll: batch1Named,
      priorGeneratedScoredQuestionsAll: [],
      existingDifficulty: {},
    },
    sheetData: baseAfterGen1,
    outgoingBatch: batch1Named,
    columnIndexMap,
    lessonContentAttributes,
  });
  assert.equal(next.baseSelectedSheetData.length, baseAfterGen1.length);
  assert.deepEqual(next.generatedScoredQuestionsAll, []);
});

test("chart splits batch1 orange and batch2 green by system names", () => {
  const batch1 = assignGeneratedProblemNames(makeLlmBatch("llm1", -1), {
    existingProblemNames: [],
    batchSeq: 1,
  });
  const batch2 = assignGeneratedProblemNames(makeLlmBatch("llm2", 1.5), {
    existingProblemNames: ["algebra_easy_b1_e1"],
    batchSeq: 2,
  });
  const fullSheet = [
    ...lessonBase,
    ["algebra_easy_b1_e1", "problem", "Gen problem", "gen body", "", "", "", "openai", ""],
    ["algebra_easy_b1_e1", "step", "Gen step", "gen step", "2", "numeric", "", "openai", ""],
    ["algebra_easy_b2_e1", "problem", "Gen problem", "gen body", "", "", "", "openai", ""],
    ["algebra_easy_b2_e1", "step", "Gen step", "gen step", "2", "numeric", "", "openai", ""],
  ];
  const sheetInfo = {
    columnIndexMap,
    selectedSheetData: fullSheet,
    baseSelectedSheetData: fullSheet,
    generatedScoredQuestionsAll: batch2,
    priorGeneratedScoredQuestionsAll: batch1,
    latestGenerationStepProblemNames: Array.from(
      stepProblemNamesFromGeneratedQuestions(batch2)
    ),
    existingDifficulty: {
      1: { scored: true, difficultyLogit: 0, difficultyLabel: "Medium" },
      3: { scored: true, difficultyLogit: -1, difficultyLabel: "Easy" },
    },
  };

  const dist = buildDifficultyDistribution(sheetInfo);
  assert.equal(dist.mode, "comparison");
  assert.equal(dist.totalPreviousScored, 2);
  assert.equal(dist.totalLatestScored, 1);
  assert.equal(collectLatestStepDifficulty(sheetInfo).scored.length, 1);
  assert.equal(collectPreviousStepDifficulty(sheetInfo).scored.length, 2);
});
