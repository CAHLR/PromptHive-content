import { labelForLogit } from "./difficultyLevels.js";

function normalizeRowType(value) {
  return String(value ?? "")
    .trim()
    .toLowerCase();
}

/** Legacy _genN suffix or system-assigned _b{batch}_e{index} names. */
export const isGeneratedProblemName = (name) => {
  const trimmed = String(name || "").trim();
  return /_gen\d+$/.test(trimmed) || /_b\d+_e\d+/.test(trimmed);
};

/** Sanitize a lesson template problem name for use as the readable ID prefix. */
export function sanitizeTemplateProblemNameBase(name) {
  const raw = String(name || "")
    .trim()
    .replace(/_gen\d+$/, "")
    .replace(/_b\d+_e\d+(?:_v\d+)?$/, "");
  const cleaned = raw
    .replace(/[^A-Za-z0-9_\-]+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_|_$/g, "");
  return cleaned || "generated";
}

/** Split flat LLM rows into exercises (problem row + following step row(s)). */
export function groupQuestionsIntoExercises(questions) {
  if (!Array.isArray(questions) || questions.length === 0) {
    return [];
  }
  const groups = [];
  let current = [];
  for (const question of questions) {
    const rowType = String(
      question?.["Row Type"] || question?.rowType || ""
    )
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
  return groups;
}

/**
 * Assign stable readable Problem Names: {template}_b{batch}_e{index}.
 * Preserves the raw LLM name on each row as llmProblemName (debug only).
 */
export function assignGeneratedProblemNames(
  questions,
  { existingProblemNames, batchSeq }
) {
  if (!Array.isArray(questions) || questions.length === 0) {
    return questions;
  }
  const batch = Math.max(1, Number(batchSeq) || 1);
  const used = new Set(
    [...(existingProblemNames || [])]
      .map((name) => String(name || "").trim())
      .filter(Boolean)
  );
  const groups = groupQuestionsIntoExercises(questions);
  const assigned = [];
  let exerciseIndex = 0;

  for (const group of groups) {
    exerciseIndex += 1;
    const templateBase = sanitizeTemplateProblemNameBase(
      group[0]?._sourceTemplateProblemName ||
        group[0]?.["Problem Name"] ||
        group[0]?.problemName
    );
    let candidate = `${templateBase}_b${batch}_e${exerciseIndex}`;
    let variant = 0;
    while (used.has(candidate)) {
      variant += 1;
      candidate = `${templateBase}_b${batch}_e${exerciseIndex}_v${variant}`;
    }
    used.add(candidate);

    for (const question of group) {
      const llmProblemName = String(
        question?.["Problem Name"] || question?.problemName || ""
      ).trim();
      assigned.push({
        ...question,
        llmProblemName: llmProblemName || null,
        "Problem Name": candidate,
      });
    }
  }
  return assigned;
}

/** Spread template picks across the sheet when N < total templates. */
export function selectDiverseTemplateIndices(questionCount, totalTemplates) {
  if (totalTemplates <= 0) {
    return [];
  }
  if (questionCount >= totalTemplates) {
    return Array.from({ length: totalTemplates }, (_, index) => index);
  }
  const indices = [];
  for (let i = 0; i < questionCount; i++) {
    indices.push(
      Math.min(
        totalTemplates - 1,
        Math.floor((i + 0.5) * totalTemplates / questionCount)
      )
    );
  }
  return indices;
}

/** Pre-Part-B multi-quota plan: T API calls with per-template quotas summing to N. */
export function buildMultiQuotaGenerationPlan(questionCount, totalTemplates) {
  const diverseIndices = selectDiverseTemplateIndices(
    questionCount,
    totalTemplates
  );
  const templateCount = diverseIndices.length;
  const baseQuota = Math.floor(questionCount / templateCount);
  const extraQuota = questionCount % templateCount;
  const quotas = diverseIndices.map((_, index) =>
    index < extraQuota ? baseQuota + 1 : baseQuota
  );
  return {
    diverseIndices,
    quotas,
    templateCount,
    apiCallCount: templateCount,
  };
}

/** One template index per generated exercise; round-robin when count > templates. */
export function buildGenerationTemplateSchedule(questionCount, totalTemplates) {
  if (totalTemplates <= 0 || questionCount <= 0) {
    return [];
  }
  const indices = [];
  if (questionCount <= totalTemplates) {
    for (let i = 0; i < questionCount; i++) {
      indices.push(
        Math.min(
          totalTemplates - 1,
          Math.floor((i + 0.5) * totalTemplates / questionCount)
        )
      );
    }
    return indices;
  }
  for (let i = 0; i < questionCount; i++) {
    indices.push(i % totalTemplates);
  }
  return indices;
}

export function normalizeExerciseStemKey(exerciseGroup) {
  if (!Array.isArray(exerciseGroup) || exerciseGroup.length === 0) {
    return "";
  }
  const stepRow =
    exerciseGroup.find(
      (q) => normalizeRowType(q?.["Row Type"] || q?.rowType) === "step"
    ) || exerciseGroup[exerciseGroup.length - 1];
  const body = String(stepRow?.["Body Text"] || stepRow?.bodyText || "")
    .trim()
    .toLowerCase()
    .replace(/\$/g, "")
    .replace(/\s+/g, "");
  const answer = String(stepRow?.Answer || stepRow?.answer || "")
    .trim()
    .toLowerCase()
    .replace(/\$/g, "")
    .replace(/\s+/g, "");
  return `${body}||${answer}`;
}

/**
 * Template indices for refill generation when dedupe leaves fewer than target.
 * Prefers templates not yet used for a kept unique stem; round-robin within pool.
 */
export function buildRefillTemplateSchedule({
  totalTemplates,
  successfulTemplateIndices = [],
  needed,
  maxExtraAttempts,
}) {
  if (totalTemplates <= 0 || needed <= 0 || maxExtraAttempts <= 0) {
    return [];
  }
  const used = new Set(
    (successfulTemplateIndices || []).filter(
      (idx) => Number.isInteger(idx) && idx >= 0 && idx < totalTemplates
    )
  );
  const unused = [];
  for (let i = 0; i < totalTemplates; i++) {
    if (!used.has(i)) {
      unused.push(i);
    }
  }
  const pool = unused.length > 0 ? unused : [...Array(totalTemplates).keys()];
  const attempts = Math.min(maxExtraAttempts, needed);
  const schedule = [];
  for (let i = 0; i < attempts; i++) {
    schedule.push(pool[i % pool.length]);
  }
  return schedule;
}

/** Drop exercise groups with identical step body + answer (keep first). */
export function dedupeExerciseGroupsByStem(exerciseGroups, maxKeep) {
  if (!Array.isArray(exerciseGroups)) {
    return [];
  }
  const seen = new Set();
  const kept = [];
  for (const group of exerciseGroups) {
    const key = normalizeExerciseStemKey(group);
    if (!key || key === "||") {
      kept.push(group);
    } else if (!seen.has(key)) {
      seen.add(key);
      kept.push(group);
    }
    if (maxKeep > 0 && kept.length >= maxKeep) {
      break;
    }
  }
  return kept;
}

export function collectPriorGeneratedStepQuestions({
  priorGeneratedScoredQuestionsAll = [],
  outgoingBatch = [],
}) {
  const all = [
    ...(Array.isArray(priorGeneratedScoredQuestionsAll)
      ? priorGeneratedScoredQuestionsAll
      : []),
    ...(Array.isArray(outgoingBatch) ? outgoingBatch : []),
  ];
  return all.filter(
    (q) => normalizeRowType(q?.["Row Type"] || q?.rowType) === "step"
  );
}

export function stepQuestionsFromExerciseGroups(exerciseGroups) {
  if (!Array.isArray(exerciseGroups)) {
    return [];
  }
  const steps = [];
  for (const group of exerciseGroups) {
    if (!Array.isArray(group)) {
      continue;
    }
    const step = group.find(
      (q) => normalizeRowType(q?.["Row Type"] || q?.rowType) === "step"
    );
    if (step) {
      steps.push(step);
    }
  }
  return steps;
}

export function buildAlreadyGeneratedPromptBlock(stepQuestions, { maxItems = 20 } = {}) {
  if (!Array.isArray(stepQuestions) || stepQuestions.length === 0) {
    return "";
  }
  const items = stepQuestions.slice(-maxItems);
  const lines = items.map((q, index) => {
    const stem = [q.Title || q.title, q["Body Text"] || q.bodyText]
      .filter(Boolean)
      .join(" ")
      .trim();
    const answer = String(q.Answer ?? q.answer ?? "").trim();
    return `${index + 1}. Stem: ${stem} | Answer: ${answer}`;
  });
  return [
    "ALREADY GENERATED — do NOT repeat these or produce near-copies (different numbers, different setup):",
    ...lines,
  ].join("\n");
}

export function buildForbiddenStemKeySet({
  lessonExerciseGroups = [],
  priorStepQuestions = [],
}) {
  const keys = new Set();
  for (const group of lessonExerciseGroups) {
    const key = normalizeExerciseStemKey(group);
    if (key && key !== "||") {
      keys.add(key);
    }
  }
  for (const question of priorStepQuestions) {
    const key = normalizeExerciseStemKey([question]);
    if (key && key !== "||") {
      keys.add(key);
    }
  }
  return keys;
}

export function isDuplicateExerciseGroup(
  group,
  forbiddenKeys,
  batchKeysInCurrentRun
) {
  const key = normalizeExerciseStemKey(group);
  if (!key || key === "||") {
    return false;
  }
  return forbiddenKeys.has(key) || batchKeysInCurrentRun.has(key);
}

export function filterUniqueExerciseGroups(exerciseGroups, forbiddenKeys) {
  if (!Array.isArray(exerciseGroups)) {
    return [];
  }
  const unique = [];
  const batchKeys = new Set();
  for (const group of exerciseGroups) {
    if (!isDuplicateExerciseGroup(group, forbiddenKeys, batchKeys)) {
      const key = normalizeExerciseStemKey(group);
      if (key && key !== "||") {
        batchKeys.add(key);
      }
      unique.push(group);
    }
  }
  return unique;
}

/**
 * Cross-batch duplicate refill (testable). Keeps unique groups and refills
 * until targetCount or maxExtraAttempts; does not drop below unique count.
 */
export async function applyCrossBatchDuplicateRefill({
  initialGroups,
  forbiddenKeys,
  targetCount,
  maxExtraAttempts,
  refillTemplateIndices,
  requestExerciseGroup,
}) {
  let keptGroups = filterUniqueExerciseGroups(initialGroups || [], forbiddenKeys);
  const schedule = Array.isArray(refillTemplateIndices)
    ? refillTemplateIndices
    : [];
  let extraAttempts = 0;
  let scheduleIdx = 0;

  while (keptGroups.length < targetCount && extraAttempts < maxExtraAttempts) {
    const templateIndex =
      schedule[scheduleIdx] ??
      schedule[scheduleIdx % Math.max(schedule.length, 1)] ??
      0;
    scheduleIdx += 1;
    const extraKeptSteps = stepQuestionsFromExerciseGroups(keptGroups);
    const newGroup = await requestExerciseGroup(templateIndex, extraKeptSteps);
    extraAttempts += 1;
    if (!newGroup) {
      continue;
    }
    const merged = filterUniqueExerciseGroups(
      [...keptGroups, newGroup],
      forbiddenKeys
    );
    if (merged.length > keptGroups.length) {
      keptGroups = merged;
    }
  }

  return {
    keptGroups: keptGroups.slice(0, targetCount),
    extraAttempts,
  };
}

/** Parse sheet rows into problem/step template objects (matches PromptEditor). */
export function extractProblemsFromSheetRows(sheetRows, columnIndexMap) {
  const problems = [];
  if (!Array.isArray(sheetRows) || !columnIndexMap) {
    return problems;
  }
  let rowIndex = 0;
  while (rowIndex < sheetRows.length) {
    let currentRow = sheetRows[rowIndex];
    if (normalizeRowType(currentRow?.[columnIndexMap.rowType]) === "problem") {
      const variableToValue = { problemSteps: [] };
      variableToValue.problemName = currentRow[columnIndexMap.problemName];
      variableToValue.problemTitle = currentRow[columnIndexMap.title];
      variableToValue.problemBodyText = currentRow[columnIndexMap.bodyText];
      variableToValue.problemImages = currentRow[columnIndexMap.images];
      rowIndex += 1;
      while (rowIndex < sheetRows.length) {
        currentRow = sheetRows[rowIndex];
        if (normalizeRowType(currentRow?.[columnIndexMap.rowType]) === "problem") {
          break;
        }
        if (normalizeRowType(currentRow?.[columnIndexMap.rowType]) === "step") {
          variableToValue.problemSteps.push({
            stepTitle: currentRow[columnIndexMap.title],
            stepBodyText: currentRow[columnIndexMap.bodyText],
            stepAnswer: currentRow[columnIndexMap.answer],
            stepAnswerType: currentRow[columnIndexMap.answer],
            stepMcChoices: currentRow[columnIndexMap.mcChoices],
            stepImages: currentRow[columnIndexMap.images],
          });
        }
        rowIndex += 1;
      }
      problems.push(variableToValue);
    } else {
      rowIndex += 1;
    }
  }
  return problems;
}

export function stripGeneratedRowsFromSheet(sheetRows, problemNameCol) {
  if (!Array.isArray(sheetRows)) {
    return [];
  }
  return sheetRows.filter(
    (row) => !isGeneratedProblemName(row?.[problemNameCol])
  );
}

/**
 * Filter sheet rows for the Generate template pool.
 * Original = imported spreadsheet names; AI = _b*_e* / legacy _gen names.
 * Both false → empty pool (caller should use blank prompt-only template).
 */
export function selectTemplateSheetRows(
  sheetRows,
  problemNameCol,
  {
    useOriginalQuestionsAsTemplates = true,
    useAiGeneratedQuestionsAsTemplates = true,
  } = {}
) {
  if (!Array.isArray(sheetRows)) {
    return [];
  }
  if (
    useOriginalQuestionsAsTemplates &&
    useAiGeneratedQuestionsAsTemplates
  ) {
    return sheetRows;
  }
  if (
    !useOriginalQuestionsAsTemplates &&
    !useAiGeneratedQuestionsAsTemplates
  ) {
    return [];
  }
  return sheetRows.filter((row) => {
    const generated = isGeneratedProblemName(row?.[problemNameCol]);
    if (useOriginalQuestionsAsTemplates && !generated) {
      return true;
    }
    if (useAiGeneratedQuestionsAsTemplates && generated) {
      return true;
    }
    return false;
  });
}

/** Synthetic template when both template toggles are off (prompt-only). */
export const BLANK_GENERATION_TEMPLATE = {
  problem: {
    problemName: "",
    problemTitle: "",
    problemBodyText: "",
    problemImages: "",
    problemSteps: [],
  },
  step: {
    stepTitle: "",
    stepBodyText: "",
    stepAnswer: "",
    stepAnswerType: "",
    stepMcChoices: "",
    stepImages: "",
  },
};

/**
 * Build {problem, step} template pairs from extracted problems.
 * Empty input → one blank template so Generate still runs N times / refill.
 */
export function buildQuestionGenerationTemplates(problems) {
  const templates = [];
  for (const problem of problems || []) {
    for (const step of problem.problemSteps || []) {
      templates.push({ problem, step });
    }
  }
  if (templates.length === 0) {
    return [
      {
        problem: { ...BLANK_GENERATION_TEMPLATE.problem },
        step: { ...BLANK_GENERATION_TEMPLATE.step },
      },
    ];
  }
  return templates;
}

export function questionsToSheetRows(
  questions,
  existingSheetRows,
  columnIndexMap,
  lessonContentAttributes
) {
  if (!Array.isArray(questions) || questions.length === 0) {
    return [];
  }
  const existingIdentifiers = new Set(
    (existingSheetRows || []).map(
      (row) =>
        `${row[columnIndexMap.problemName]}-${row[columnIndexMap.rowType]}`
    )
  );
  const questionRows = [];
  for (const question of questions) {
    const newRow = [];
    let identifier = "";
    for (const colKey in columnIndexMap) {
      if (!Object.prototype.hasOwnProperty.call(columnIndexMap, colKey)) {
        continue;
      }
      const index = columnIndexMap[colKey];
      if (Object.prototype.hasOwnProperty.call(lessonContentAttributes, colKey)) {
        const attributeKey = lessonContentAttributes[colKey];
        const value = Object.prototype.hasOwnProperty.call(question, attributeKey)
          ? question[attributeKey]
          : "";
        newRow[index] = value;
        if (colKey === "problemName" || colKey === "rowType") {
          identifier += value + "-";
        }
      } else {
        newRow[index] = "";
      }
    }
    identifier = identifier.slice(0, -1);
    newRow[columnIndexMap.oerSrc] = "openai";
    if (!existingIdentifiers.has(identifier)) {
      questionRows.push(newRow);
      existingIdentifiers.add(identifier);
    }
  }
  return questionRows;
}

export function stepProblemNamesFromGeneratedQuestions(questions) {
  const names = new Set();
  if (!Array.isArray(questions)) {
    return names;
  }
  for (const question of questions) {
    if (normalizeRowType(question?.["Row Type"] || question?.rowType) !== "step") {
      continue;
    }
    const name = String(
      question?.["Problem Name"] || question?.problemName || ""
    ).trim();
    if (name) {
      names.add(name);
    }
  }
  return names;
}

/** Names of step rows in the latest (green) generation batch only. */
export function getLatestGenerationStepProblemNames(sheetInfo) {
  const explicit = sheetInfo?.latestGenerationStepProblemNames;
  if (explicit instanceof Set) {
    return explicit;
  }
  if (Array.isArray(explicit)) {
    return new Set(
      explicit.map((name) => String(name || "").trim()).filter(Boolean)
    );
  }
  return stepProblemNamesFromGeneratedQuestions(
    sheetInfo?.generatedScoredQuestionsAll
  );
}

/** True when this step row belongs to the latest generated batch only. */
export function isStepInCurrentGenerationBatch(row, sheetInfo) {
  const columnIndexMap = sheetInfo?.columnIndexMap;
  if (!columnIndexMap || !row) {
    return false;
  }
  const problemName = String(row[columnIndexMap.problemName] ?? "").trim();
  if (
    normalizeRowType(row[columnIndexMap.rowType]) !== "step" ||
    !problemName
  ) {
    return false;
  }
  return getLatestGenerationStepProblemNames(sheetInfo).has(problemName);
}

export function findPriorGeneratedStepQuestion(row, sheetInfo) {
  const columnIndexMap = sheetInfo?.columnIndexMap;
  const prior = sheetInfo?.priorGeneratedScoredQuestionsAll;
  if (!columnIndexMap || !row || !Array.isArray(prior) || prior.length === 0) {
    return null;
  }
  const problemName = String(row[columnIndexMap.problemName] ?? "").trim();
  if (
    normalizeRowType(row[columnIndexMap.rowType]) !== "step" ||
    !problemName
  ) {
    return null;
  }
  for (let i = prior.length - 1; i >= 0; i -= 1) {
    const question = prior[i];
    const qName = String(
      question?.["Problem Name"] || question?.problemName || ""
    ).trim();
    const qType = normalizeRowType(question?.["Row Type"] || question?.rowType);
    if (qName === problemName && qType === "step") {
      return question;
    }
  }
  return null;
}

export function generatedQuestionToExistingScore(question, rowIdx) {
  if (!question) {
    return null;
  }
  const logit = question.difficultyLogit;
  const hasLogit =
    logit != null && logit !== undefined && !Number.isNaN(Number(logit));
  const score = {
    index: rowIdx,
    itemId: question.itemId,
    problemName: question["Problem Name"] || question.problemName,
    rowType: question["Row Type"] || question.rowType,
    difficultyLogit: logit,
    difficultyLabel:
      question.difficultyLabel ||
      (hasLogit ? labelForLogit(Number(logit)) : null),
    scored: question.difficultyScored !== false && hasLogit,
    reason: question.difficultyStatus || question.reason,
    lowConfidence: question.lowConfidence ?? false,
    difficultyUncertain: question.difficultyUncertain ?? false,
    seDifficulty: question.seDifficulty,
    saturationDirection: question.saturationDirection,
  };
  if (question.answerKeyCheck) {
    // Keep the answer-key-check outcome so the "Answer key corrected" chip
    // survives when this batch is promoted by the next Generate click.
    score.answerKeyCheck = question.answerKeyCheck;
    if (question.answerKeyOriginal !== undefined) {
      score.answerKeyOriginal = question.answerKeyOriginal;
      score.answerKeyCorrected = question.Answer ?? question.answer;
    }
  }
  return score;
}

function mergeGeneratedScoresIntoExistingMap(
  baseRows,
  generatedQuestions,
  existingDifficulty,
  columnIndexMap
) {
  const merged = { ...(existingDifficulty || {}) };
  if (!Array.isArray(baseRows) || !Array.isArray(generatedQuestions)) {
    return merged;
  }
  for (const question of generatedQuestions) {
    if (normalizeRowType(question?.["Row Type"] || question?.rowType) !== "step") {
      continue;
    }
    const problemName = String(
      question?.["Problem Name"] || question?.problemName || ""
    ).trim();
    if (!problemName) {
      continue;
    }
    const rowIdx = baseRows.findIndex((row) => {
      return (
        String(row?.[columnIndexMap.problemName] ?? "").trim() === problemName &&
        normalizeRowType(row?.[columnIndexMap.rowType]) === "step"
      );
    });
    if (rowIdx >= 0) {
      merged[rowIdx] = generatedQuestionToExistingScore(question, rowIdx);
    }
  }
  return merged;
}

/**
 * Promote a pre-snapshot outgoing batch to previous/orange bucket before a new run.
 * Returns null when outgoingBatch is empty (clean sheet — no promotion).
 */
export function promoteOutgoingGenerationBatch({
  outgoingBatch,
  prevState,
  columnIndexMap,
  lessonContentAttributes,
}) {
  const outgoing = Array.isArray(outgoingBatch) ? outgoingBatch : [];
  if (outgoing.length === 0) {
    return null;
  }

  const priorAccumulated = Array.isArray(prevState?.priorGeneratedScoredQuestionsAll)
    ? prevState.priorGeneratedScoredQuestionsAll
    : [];

  // Accumulated base already holds promoted batches — do NOT strip _gen rows.
  const accumulatedBase = Array.isArray(prevState?.baseSelectedSheetData)
    ? [...prevState.baseSelectedSheetData]
    : stripGeneratedRowsFromSheet(
        prevState?.selectedSheetData,
        columnIndexMap.problemName
      );

  const promotedRows = questionsToSheetRows(
    outgoing,
    accumulatedBase,
    columnIndexMap,
    lessonContentAttributes
  );
  const newBase = [...accumulatedBase, ...promotedRows];
  const existingDifficulty = mergeGeneratedScoresIntoExistingMap(
    newBase,
    outgoing,
    prevState?.existingDifficulty,
    columnIndexMap
  );

  return {
    baseSelectedSheetData: newBase,
    selectedSheetData: newBase,
    existingDifficulty,
    priorGeneratedScoredQuestionsAll: [...priorAccumulated, ...outgoing],
    generatedScoredQuestionsAll: [],
    latestGenerationStepProblemNames: [],
    lastJointDifficultyRun: false,
  };
}

/** Apply start-of-generation sheet state using a pre-click outgoing snapshot. */
export function applyGenerationStartState({
  prevState,
  sheetData,
  outgoingBatch,
  columnIndexMap,
  lessonContentAttributes,
}) {
  const promotion = promoteOutgoingGenerationBatch({
    outgoingBatch,
    prevState,
    columnIndexMap,
    lessonContentAttributes,
  });
  if (promotion) {
    return {
      ...prevState,
      ...promotion,
    };
  }
  const strippedBase = stripGeneratedRowsFromSheet(
    sheetData,
    columnIndexMap.problemName
  );
  return {
    ...prevState,
    baseSelectedSheetData: strippedBase,
    selectedSheetData: strippedBase,
    generatedScoredQuestionsAll: [],
    latestGenerationStepProblemNames: [],
  };
}

/** Find the parent problem row for a step row in the loaded sheet. */
export function findProblemRowForStep(stepRow, sheetRows, columnIndexMap) {
  if (!stepRow || !Array.isArray(sheetRows) || !columnIndexMap) {
    return null;
  }
  const problemName = String(stepRow[columnIndexMap.problemName] ?? "").trim();
  if (!problemName) {
    return null;
  }
  return (
    sheetRows.find((row) => {
      return (
        String(row?.[columnIndexMap.problemName] ?? "").trim() ===
          problemName &&
        normalizeRowType(row?.[columnIndexMap.rowType]) === "problem"
      );
    }) || null
  );
}

/** Merge sheet row text/images with score metadata for unscored chip display. */
export function buildUnscoredChipContext(row, sheetInfo, questionOrScore) {
  const base =
    questionOrScore && typeof questionOrScore === "object"
      ? { ...questionOrScore }
      : {};
  const columnIndexMap = sheetInfo?.columnIndexMap;
  if (!row || !columnIndexMap) {
    return base;
  }
  const problemRow = findProblemRowForStep(
    row,
    sheetInfo?.selectedSheetData,
    columnIndexMap
  );
  return {
    ...base,
    bodyText: row[columnIndexMap.bodyText] ?? base["Body Text"] ?? "",
    title: row[columnIndexMap.title] ?? base.Title ?? "",
    images: row[columnIndexMap.images] ?? "",
    problemBodyText: problemRow?.[columnIndexMap.bodyText] ?? "",
    problemImages: problemRow?.[columnIndexMap.images] ?? "",
  };
}

/** Build chip context from generated question objects (no sheet row). */
export function buildUnscoredChipContextFromGeneratedQuestions(
  stepQuestion,
  allGenerated
) {
  const base =
    stepQuestion && typeof stepQuestion === "object" ? { ...stepQuestion } : {};
  if (!stepQuestion || !Array.isArray(allGenerated)) {
    return base;
  }
  const problemName = String(
    stepQuestion["Problem Name"] || stepQuestion.problemName || ""
  ).trim();
  const problemRow = allGenerated.find((question) => {
    const qName = String(
      question?.["Problem Name"] || question?.problemName || ""
    ).trim();
    const qType = normalizeRowType(question?.["Row Type"] || question?.rowType);
    return qName === problemName && qType === "problem";
  });
  return {
    ...base,
    bodyText: stepQuestion["Body Text"] ?? base.bodyText ?? "",
    title: stepQuestion.Title ?? base.title ?? "",
    images: stepQuestion.Images ?? stepQuestion.images ?? "",
    problemBodyText: problemRow?.["Body Text"] ?? "",
    problemImages: problemRow?.Images ?? problemRow?.images ?? "",
  };
}

/** Convert sheet row arrays to OATutor question objects for calibration APIs. */
export function sheetRowsToOatutorQuestions(rows, columnIndexMap) {
  if (!Array.isArray(rows) || !columnIndexMap) {
    return [];
  }
  if (typeof columnIndexMap.problemName !== "number" || columnIndexMap.problemName < 0) {
    return [];
  }

    return rows.map((row) => {
    if (!Array.isArray(row)) {
      throw new Error("Sheet row data is malformed. Reload the lesson.");
    }
    return {
      "Problem Name": row[columnIndexMap.problemName] ?? "",
      "Row Type": row[columnIndexMap.rowType] ?? "",
      Title: row[columnIndexMap.title] ?? "",
      "Body Text": row[columnIndexMap.bodyText] ?? "",
      Answer: row[columnIndexMap.answer] ?? "",
      answerType: row[columnIndexMap.answerType] ?? "",
      mcChoices: row[columnIndexMap.mcChoices] ?? "",
      Images: row[columnIndexMap.images] ?? "",
    };
  });
}

/** Map one calibration evaluation row to Feature 2 existingDifficulty score shape. */
/**
 * Index -> answer-key-check outcome from ``difficultyFilter.answerKeyCheckItems``
 * (``{index, answerKeyCheck, answerKeyOriginal, answerKeySolves, Answer,
 * "Answer Rendered", "Answer Tutoring"}``; the corrected key and its
 * re-rendered columns are present only for corrected rows).
 * The backend checks keys before calibration, but the frontend rebuilds rows
 * from its own input list, so the outcomes have to be overlaid by index.
 */
export function answerKeyCheckItemsByIndex(items) {
  const byIndex = {};
  if (!Array.isArray(items)) {
    return byIndex;
  }
  for (const item of items) {
    const idx = Number(item?.index);
    if (!item || Number.isNaN(idx)) {
      continue;
    }
    byIndex[idx] = item;
  }
  return byIndex;
}

/** Sheet columns the key check rewrites alongside Answer when a key is corrected. */
const CORRECTED_ANSWER_COLUMNS = ["Answer", "Answer Rendered", "Answer Tutoring"];

/**
 * Row fields carried from the key check onto a generated question. For a
 * corrected row this includes the corrected Answer plus the re-rendered
 * "Answer Rendered"/"Answer Tutoring" (the sheet view prefers the rendered
 * column), so the browser's own copy of the row shows the corrected key.
 */
function answerKeyFieldsForQuestion(keyCheck) {
  if (!keyCheck || !keyCheck.answerKeyCheck) {
    return {};
  }
  const fields = { answerKeyCheck: keyCheck.answerKeyCheck };
  if (keyCheck.answerKeyOriginal !== undefined) {
    fields.answerKeyOriginal = keyCheck.answerKeyOriginal;
    for (const column of CORRECTED_ANSWER_COLUMNS) {
      if (keyCheck[column] !== undefined && keyCheck[column] !== null) {
        fields[column] = keyCheck[column]; // corrected key goes to the sheet
      }
    }
  }
  if (Array.isArray(keyCheck.answerKeySolves)) {
    fields.answerKeySolves = keyCheck.answerKeySolves;
  }
  return fields;
}

export function evaluationRowToExistingScore(evalRow, keyCheck) {
  if (!evalRow) {
    return null;
  }
  const logit = evalRow.difficultyLogit;
  const score = {
    index: evalRow.index,
    itemId: evalRow.itemId,
    problemName: evalRow.problemName,
    rowType: evalRow.rowType,
    difficultyLogit: logit,
    difficultyLabel:
      evalRow.difficultyLabel ||
      (logit != null && logit !== undefined && !Number.isNaN(Number(logit))
        ? labelForLogit(Number(logit))
        : null),
    scored: evalRow.scored ?? (logit != null && logit !== undefined),
    reason: evalRow.reason,
    lowConfidence: evalRow.lowConfidence ?? false,
    difficultyUncertain: evalRow.difficultyUncertain ?? false,
    seDifficulty: evalRow.seDifficulty,
    saturationDirection: evalRow.saturationDirection,
  };
  if (keyCheck?.answerKeyCheck) {
    // Existing sheet rows are never rewritten; carry the outcome for the chip.
    score.answerKeyCheck = keyCheck.answerKeyCheck;
    if (keyCheck.answerKeyOriginal !== undefined) {
      score.answerKeyOriginal = keyCheck.answerKeyOriginal;
      score.answerKeyCorrected = keyCheck.Answer;
    }
  }
  return score;
}

function enrichQuestionFromEvaluation(question, evalRow, keyCheck) {
  const logit = evalRow?.difficultyLogit;
  return {
    ...question,
    ...answerKeyFieldsForQuestion(keyCheck),
    difficultyLogit: logit,
    difficultyLabel:
      evalRow?.difficultyLabel ||
      (logit != null && logit !== undefined && !Number.isNaN(Number(logit))
        ? labelForLogit(Number(logit))
        : question?.difficultyLabel),
    difficultyScored: evalRow?.scored ?? (logit != null && logit !== undefined),
    difficultyStatus: evalRow?.reason,
    reason: evalRow?.reason,
    lowConfidence: evalRow?.lowConfidence,
    difficultyUncertain: evalRow?.difficultyUncertain,
    seDifficulty: evalRow?.seDifficulty,
    saturationDirection: evalRow?.saturationDirection,
  };
}

/**
 * Split a combined calibration response into existing sheet scores and
 * generated question objects (kept generated rows only).
 */
export function splitCombinedCalibrationResults(
  combinedQuestions,
  evaluations,
  existingCount,
  answerKeyCheckItems = null
) {
  const existingDifficulty = {};
  const generatedQuestions = [];
  const evalList = Array.isArray(evaluations) ? evaluations : [];
  const combined = Array.isArray(combinedQuestions) ? combinedQuestions : [];
  const boundary = Math.max(0, Number(existingCount) || 0);
  const keyChecks = answerKeyCheckItemsByIndex(answerKeyCheckItems);

  for (const evalRow of evalList) {
    const idx = evalRow?.index;
    if (idx === null || idx === undefined || Number.isNaN(Number(idx))) {
      continue;
    }
    if (idx < boundary) {
      existingDifficulty[idx] = evaluationRowToExistingScore(
        evalRow,
        keyChecks[idx]
      );
      continue;
    }
    if (!evalRow.kept) {
      continue;
    }
    const source = combined[idx];
    if (source) {
      generatedQuestions.push(
        enrichQuestionFromEvaluation(source, evalRow, keyChecks[idx])
      );
    }
  }

  return { existingDifficulty, generatedQuestions };
}

export function baseSheetHasStepRows(baseRows, columnIndexMap) {
  if (!Array.isArray(baseRows) || !columnIndexMap) {
    return false;
  }
  const rowTypeIndex = columnIndexMap.rowType;
  if (typeof rowTypeIndex !== "number" || rowTypeIndex < 0) {
    return false;
  }
  return baseRows.some((row) => {
    const rowType = String(row?.[rowTypeIndex] || "")
      .trim()
      .toLowerCase();
    return rowType === "step";
  });
}
