import { test } from "node:test";
import assert from "node:assert/strict";
import {
  applyCrossBatchDuplicateRefill,
  assignGeneratedProblemNames,
  buildAlreadyGeneratedPromptBlock,
  buildForbiddenStemKeySet,
  buildMultiQuotaGenerationPlan,
  buildQuestionGenerationTemplates,
  buildRefillTemplateSchedule,
  collectPriorGeneratedStepQuestions,
  filterUniqueExerciseGroups,
  groupQuestionsIntoExercises,
  normalizeExerciseStemKey,
  selectTemplateSheetRows,
  stripGeneratedRowsFromSheet,
} from "./sheetDifficultyUtils.js";

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

const lessonBase = [
  ["algebra_easy", "problem", "P1", "body", "", "", "", "", ""],
  ["algebra_easy", "step", "S1", "solve x+2=5", "3", "numeric", "", "", ""],
];

const batch1Step = {
  "Problem Name": "algebra_easy_b1_e1",
  "Row Type": "step",
  Title: "Gen step",
  "Body Text": "solve x+3=8",
  Answer: "5",
};

function makeExerciseGroup(bodyText, answer) {
  return [
    {
      "Problem Name": "llm_gen",
      "Row Type": "problem",
      Title: "Gen",
      "Body Text": "gen body",
    },
    {
      "Problem Name": "llm_gen",
      "Row Type": "step",
      Title: "Gen step",
      "Body Text": bodyText,
      Answer: answer,
    },
  ];
}

test("prior-batch context block included on 2nd generate, absent on 1st", () => {
  const empty = buildAlreadyGeneratedPromptBlock(
    collectPriorGeneratedStepQuestions({
      priorGeneratedScoredQuestionsAll: [],
      outgoingBatch: [],
    })
  );
  assert.equal(empty, "");

  const block = buildAlreadyGeneratedPromptBlock(
    collectPriorGeneratedStepQuestions({
      priorGeneratedScoredQuestionsAll: [],
      outgoingBatch: [batch1Step],
    })
  );
  assert.match(block, /ALREADY GENERATED/);
  assert.match(block, /solve x\+3=8/);
  assert.match(block, /\| Answer: 5/);
});

test("stripGeneratedRowsFromSheet removes Part A and legacy generated names", () => {
  const sheetWithBatch1 = [
    ...lessonBase,
    ["algebra_easy_b1_e1", "problem", "Gen", "gen body", "", "", "", "openai", ""],
    ["algebra_easy_b1_e1", "step", "Gen step", "gen step", "2", "numeric", "", "openai", ""],
    ["algebra_easy_gen2", "problem", "Legacy", "legacy body", "", "", "", "openai", ""],
    ["algebra_easy_gen2", "step", "Legacy step", "legacy step", "4", "numeric", "", "openai", ""],
  ];
  const stripped = stripGeneratedRowsFromSheet(
    sheetWithBatch1,
    columnIndexMap.problemName
  );
  assert.deepEqual(stripped, lessonBase);
});

test("duplicate against batch 1 triggers refill and count stays N", async () => {
  const priorSteps = [batch1Step];
  const forbiddenKeys = buildForbiddenStemKeySet({
    lessonExerciseGroups: groupQuestionsIntoExercises([
      {
        "Problem Name": "algebra_easy",
        "Row Type": "step",
        "Body Text": "solve x+2=5",
        Answer: "3",
      },
    ]),
    priorStepQuestions: priorSteps,
  });

  const duplicateGroup = makeExerciseGroup("solve x+3=8", "5");
  assert.equal(
    normalizeExerciseStemKey(duplicateGroup),
    normalizeExerciseStemKey([batch1Step])
  );

  let refillCalls = 0;
  const { keptGroups, extraAttempts } = await applyCrossBatchDuplicateRefill({
    initialGroups: [duplicateGroup],
    forbiddenKeys,
    targetCount: 1,
    maxExtraAttempts: 3,
    refillTemplateIndices: [0, 1, 2],
    requestExerciseGroup: async () => {
      refillCalls += 1;
      return makeExerciseGroup("solve 2y-1=9", "5");
    },
  });

  assert.equal(refillCalls, 1);
  assert.equal(extraAttempts, 1);
  assert.equal(keptGroups.length, 1);
  assert.equal(keptGroups[0][1]["Body Text"], "solve 2y-1=9");
});

test("refill cap prevents unbounded loops", async () => {
  const forbiddenKeys = new Set(["solvex+3=8||5"]);
  let refillCalls = 0;
  const { keptGroups, extraAttempts } = await applyCrossBatchDuplicateRefill({
    initialGroups: [],
    forbiddenKeys,
    targetCount: 2,
    maxExtraAttempts: 2,
    refillTemplateIndices: [0, 0, 0, 0],
    requestExerciseGroup: async () => {
      refillCalls += 1;
      return makeExerciseGroup("solve x+3=8", "5");
    },
  });

  assert.equal(refillCalls, 2);
  assert.equal(extraAttempts, 2);
  assert.equal(keptGroups.length, 0);
});

test("Part A names still increment for batch 2 after refill path", () => {
  const batch1Named = assignGeneratedProblemNames(
    [
      ...makeExerciseGroup("a", "1"),
      ...makeExerciseGroup("b", "2"),
    ],
    { existingProblemNames: [], batchSeq: 1 }
  );
  const batch2Named = assignGeneratedProblemNames(
    [...makeExerciseGroup("c", "3")],
    {
      existingProblemNames: batch1Named.map((q) => q["Problem Name"]),
      batchSeq: 2,
    }
  );
  assert.match(batch2Named[0]["Problem Name"], /_b2_e1$/);
});

test("filterUniqueExerciseGroups removes cross-batch duplicates only", () => {
  const forbidden = new Set(["solvex+2=5||3"]);
  const unique = filterUniqueExerciseGroups(
    [
      makeExerciseGroup("solve x+2=5", "3"),
      makeExerciseGroup("solve 4a=12", "3"),
    ],
    forbidden
  );
  assert.equal(unique.length, 1);
  assert.equal(unique[0][1]["Body Text"], "solve 4a=12");
});

const mixedSheet = [
  ...lessonBase,
  ["algebra_easy_b1_e1", "problem", "Gen", "gen body", "", "", "", "openai", ""],
  ["algebra_easy_b1_e1", "step", "Gen step", "gen step", "2", "numeric", "", "openai", ""],
];

test("selectTemplateSheetRows covers four toggle combinations", () => {
  const both = selectTemplateSheetRows(mixedSheet, columnIndexMap.problemName, {
    useOriginalQuestionsAsTemplates: true,
    useAiGeneratedQuestionsAsTemplates: true,
  });
  assert.equal(both.length, mixedSheet.length);

  const originalOnly = selectTemplateSheetRows(
    mixedSheet,
    columnIndexMap.problemName,
    {
      useOriginalQuestionsAsTemplates: true,
      useAiGeneratedQuestionsAsTemplates: false,
    }
  );
  assert.deepEqual(originalOnly, lessonBase);

  const aiOnly = selectTemplateSheetRows(
    mixedSheet,
    columnIndexMap.problemName,
    {
      useOriginalQuestionsAsTemplates: false,
      useAiGeneratedQuestionsAsTemplates: true,
    }
  );
  assert.equal(aiOnly.length, 2);
  assert.equal(aiOnly[0][0], "algebra_easy_b1_e1");

  const neither = selectTemplateSheetRows(
    mixedSheet,
    columnIndexMap.problemName,
    {
      useOriginalQuestionsAsTemplates: false,
      useAiGeneratedQuestionsAsTemplates: false,
    }
  );
  assert.deepEqual(neither, []);
});

test("both toggles off: blank template + plan still requests N and refill works", () => {
  const templates = buildQuestionGenerationTemplates([]);
  assert.equal(templates.length, 1);
  assert.equal(templates[0].problem.problemName, "");
  assert.equal(templates[0].step.stepBodyText, "");

  const generateCount = 3;
  const { diverseIndices, quotas, templateCount } =
    buildMultiQuotaGenerationPlan(generateCount, templates.length);
  assert.equal(templateCount, 1);
  assert.deepEqual(diverseIndices, [0]);
  assert.equal(
    quotas.reduce((sum, q) => sum + q, 0),
    generateCount
  );

  const refillSchedule = buildRefillTemplateSchedule({
    totalTemplates: templates.length,
    successfulTemplateIndices: diverseIndices,
    needed: generateCount,
    maxExtraAttempts: generateCount,
  });
  assert.ok(refillSchedule.length > 0);
  assert.ok(refillSchedule.every((idx) => idx === 0));

  // Avoid-list still builds from prior AI steps even with a blank template pool.
  const avoidBlock = buildAlreadyGeneratedPromptBlock(
    collectPriorGeneratedStepQuestions({
      priorGeneratedScoredQuestionsAll: [],
      outgoingBatch: [batch1Step],
    })
  );
  assert.match(avoidBlock, /ALREADY GENERATED/);
  assert.match(avoidBlock, /solve x\+3=8/);
});