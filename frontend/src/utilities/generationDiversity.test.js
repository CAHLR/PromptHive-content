import { test } from "node:test";
import assert from "node:assert/strict";
import {
  assignGeneratedProblemNames,
  buildMultiQuotaGenerationPlan,
  groupQuestionsIntoExercises,
} from "./sheetDifficultyUtils.js";

test("multi-quota: N=5 with 4 templates -> 4 calls quotas [2,1,1,1]", () => {
  const plan = buildMultiQuotaGenerationPlan(5, 4);
  assert.equal(plan.apiCallCount, 4);
  assert.deepEqual(plan.diverseIndices, [0, 1, 2, 3]);
  assert.deepEqual(plan.quotas, [2, 1, 1, 1]);
  assert.equal(plan.quotas.reduce((sum, q) => sum + q, 0), 5);
});

test("multi-quota: N=3 with 5 templates -> 3 calls quota 1 each", () => {
  const plan = buildMultiQuotaGenerationPlan(3, 5);
  assert.equal(plan.apiCallCount, 3);
  assert.deepEqual(plan.quotas, [1, 1, 1]);
  assert.equal(plan.diverseIndices.length, 3);
  assert.equal(new Set(plan.diverseIndices).size, 3);
});

test("multi-quota keeps all returned exercises (no dedupe drop)", () => {
  const batch = [
    { "Row Type": "problem", "Problem Name": "llm_a" },
    { "Row Type": "step", "Problem Name": "llm_a", Answer: "1" },
    { "Row Type": "problem", "Problem Name": "llm_b" },
    { "Row Type": "step", "Problem Name": "llm_b", Answer: "2" },
  ].map((row) => ({
    ...row,
    _sourceTemplateProblemName: "algebra2",
  }));
  const groups = groupQuestionsIntoExercises(batch);
  assert.equal(groups.length, 2);
  const flat = groups.flat();
  assert.equal(flat.length, 4);
});

test("Part A naming: two exercises from same template get e1 and e2", () => {
  const questions = [
    { "Row Type": "problem", _sourceTemplateProblemName: "algebra2" },
    { "Row Type": "step", _sourceTemplateProblemName: "algebra2" },
    { "Row Type": "problem", _sourceTemplateProblemName: "algebra2" },
    { "Row Type": "step", _sourceTemplateProblemName: "algebra2" },
  ];
  const named = assignGeneratedProblemNames(questions, {
    existingProblemNames: [],
    batchSeq: 1,
  });
  const stepNames = named
    .filter((q) => q["Row Type"] === "step")
    .map((q) => q["Problem Name"]);
  assert.deepEqual(stepNames, ["algebra2_b1_e1", "algebra2_b1_e2"]);
});
