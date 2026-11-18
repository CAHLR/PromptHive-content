import {
  DIFFICULTY_LEVELS,
  isQuestionDifficultyScored,
  labelForLogit,
} from "./difficultyLevels.js";
import {
  findPriorGeneratedStepQuestion,
  generatedQuestionToExistingScore,
  isStepInCurrentGenerationBatch,
} from "./sheetDifficultyUtils.js";

function isScoredExistingScore(score) {
  if (!score || score.scored === false) {
    return false;
  }
  const logit = score.difficultyLogit;
  if (logit == null || logit === undefined) {
    return false;
  }
  return !Number.isNaN(Number(logit));
}

function pushScoredItem(scored, unscoredRef, score) {
  if (isScoredExistingScore(score)) {
    const logit = Number(score.difficultyLogit);
    scored.push({
      logit,
      label: score.difficultyLabel || labelForLogit(logit),
    });
  } else {
    unscoredRef.count += 1;
  }
}

/** Previous/orange: lesson + promoted older batches (never the latest batch). */
export function collectPreviousStepDifficulty(sheetInfo) {
  const cim = sheetInfo?.columnIndexMap;
  const selected = sheetInfo?.selectedSheetData;
  const existingMap = sheetInfo?.existingDifficulty;
  const scored = [];
  const unscored = { count: 0 };

  if (!cim || !Array.isArray(selected)) {
    return { scored, unscoredCount: 0 };
  }

  for (let rowIdx = 0; rowIdx < selected.length; rowIdx++) {
    const row = selected[rowIdx];
    if (String(row?.[cim.rowType] || "").trim().toLowerCase() !== "step") {
      continue;
    }
    if (isStepInCurrentGenerationBatch(row, sheetInfo)) {
      continue;
    }

    let score = existingMap?.[rowIdx] || null;
    if (!score) {
      const priorQuestion = findPriorGeneratedStepQuestion(row, sheetInfo);
      if (priorQuestion) {
        score = generatedQuestionToExistingScore(priorQuestion, rowIdx);
      }
    }
    if (!score) {
      continue;
    }
    pushScoredItem(scored, unscored, score);
  }

  return { scored, unscoredCount: unscored.count };
}

/** Latest/green: current generation batch only. */
export function collectLatestStepDifficulty(sheetInfo) {
  const allGenerated = sheetInfo?.generatedScoredQuestionsAll;
  const scored = [];
  let unscoredCount = 0;

  if (!Array.isArray(allGenerated)) {
    return { scored, unscoredCount };
  }

  for (const question of allGenerated) {
    const rowType = String(question?.["Row Type"] || question?.rowType || "")
      .trim()
      .toLowerCase();
    if (rowType !== "step") {
      continue;
    }
    if (isQuestionDifficultyScored(question)) {
      const logit = Number(question.difficultyLogit);
      scored.push({
        logit,
        label: question.difficultyLabel || labelForLogit(logit),
      });
    } else {
      unscoredCount += 1;
    }
  }

  return { scored, unscoredCount };
}

function emptyBandCounts() {
  return DIFFICULTY_LEVELS.map((level) => ({
    level: level.level,
    label: level.label,
    count: 0,
  }));
}

function bandIndexForLogit(logit) {
  for (let i = 0; i < DIFFICULTY_LEVELS.length; i++) {
    const level = DIFFICULTY_LEVELS[i];
    if (logit >= level.minLogit && logit <= level.maxLogit) {
      return i;
    }
  }
  if (logit < DIFFICULTY_LEVELS[0].minLogit) {
    return 0;
  }
  return DIFFICULTY_LEVELS.length - 1;
}

function countIntoBands(scoredItems) {
  const bands = emptyBandCounts();
  for (const item of scoredItems) {
    const idx = bandIndexForLogit(item.logit);
    bands[idx].count += 1;
  }
  return bands;
}

function buildUnscoredFootnote(previousUnscored, latestUnscored) {
  const parts = [];
  if (previousUnscored > 0) {
    parts.push(`${previousUnscored} pre-existing items couldn't be measured`);
  }
  if (latestUnscored > 0) {
    parts.push(`${latestUnscored} Gen AI items couldn't be measured`);
  }
  if (parts.length === 0) {
    return null;
  }
  return `${parts.join("; ")} (not shown in chart).`;
}

/**
 * Build histogram data for the difficulty distribution panel.
 * Ignores post-hoc display filter — uses all scored items in source stores.
 */
export function buildDifficultyDistribution(sheetInfo) {
  const previous = collectPreviousStepDifficulty(sheetInfo);
  const latest = collectLatestStepDifficulty(sheetInfo);

  const previousScored = previous.scored;
  const latestScored = latest.scored;
  const hasPrevious = previousScored.length > 0;
  const hasLatest = latestScored.length > 0;

  let mode = "none";
  if (hasPrevious && hasLatest) {
    mode = "comparison";
  } else if (hasPrevious) {
    mode = "previous";
  } else if (hasLatest) {
    mode = "latest";
  }

  const previousBands = countIntoBands(previousScored);
  const latestBands = countIntoBands(latestScored);

  const bands = DIFFICULTY_LEVELS.map((level, index) => ({
    level: level.level,
    label: level.label,
    previousCount: previousBands[index].count,
    latestCount: latestBands[index].count,
    existingCount: previousBands[index].count,
    generatedCount: latestBands[index].count,
  }));

  const showSeparateRunsNote =
    mode === "comparison" && !sheetInfo?.lastJointDifficultyRun;

  return {
    mode,
    bands,
    totalPreviousScored: previousScored.length,
    totalLatestScored: latestScored.length,
    totalExistingScored: previousScored.length,
    totalGeneratedScored: latestScored.length,
    unscoredFootnote: buildUnscoredFootnote(
      previous.unscoredCount,
      latest.unscoredCount
    ),
    showSeparateRunsNote,
    hasScoredData: hasPrevious || hasLatest,
  };
}
