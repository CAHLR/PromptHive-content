import { normalizeString } from "./utils";
import { scorePassesPostHocFilter } from "./difficultyLevels";
import { isGeneratedProblemName } from "./sheetDifficultyUtils";

/** Core OATutor columns exported in sheet column order (first 16 logical fields). */
export const EXPORT_COLUMN_KEYS = [
  "problemName",
  "rowType",
  "title",
  "bodyText",
  "answer",
  "answerType",
  "hintID",
  "dependency",
  "mcChoices",
  "images",
  "parent",
  "oerSrc",
  "openstaxKC",
  "kc",
  "taxonomy",
  "license",
];

function getPostHocFilterOptions(sheetInfo) {
  const range = sheetInfo?.difficultyRange;
  return {
    filterEnabled: Boolean(sheetInfo?.difficultyFilterEnabled),
    minLevel: Array.isArray(range) ? range[0] : 1,
    maxLevel: Array.isArray(range) ? range[1] : 5,
    includeUnscored: sheetInfo?.includeUnscoredQuestions !== false,
  };
}

function isGeneratedSheetRow(row, sheetInfo) {
  const cim = sheetInfo?.columnIndexMap;
  if (!cim || !row) {
    return false;
  }
  const problemName = String(row?.[cim.problemName] || "").trim();
  const oerSrc = row?.[cim.oerSrc];
  return isGeneratedProblemName(problemName) || oerSrc === "openai";
}

function findGeneratedQuestionForRow(row, sheetInfo) {
  const allGenerated = sheetInfo?.generatedScoredQuestionsAll;
  if (!Array.isArray(allGenerated) || allGenerated.length === 0) {
    return null;
  }
  const cim = sheetInfo.columnIndexMap;
  const problemName = String(row?.[cim.problemName] || "").trim();
  const rowType = normalizeString(row?.[cim.rowType]);
  if (rowType !== "step" || !problemName) {
    return null;
  }
  return (
    allGenerated.find((question) => {
      const qName = String(
        question?.["Problem Name"] || question?.problemName || ""
      ).trim();
      const qType = String(question?.["Row Type"] || question?.rowType || "")
        .trim()
        .toLowerCase();
      return qName === problemName && qType === "step";
    }) || null
  );
}

/** Mirrors SheetContent post-hoc filter, including generated rows via question scores. */
function rowPassesPostHocDifficultyFilter(row, sheetInfo) {
  const filterOptions = getPostHocFilterOptions(sheetInfo);
  if (!filterOptions.filterEnabled) {
    return true;
  }

  const rowType = normalizeString(row[sheetInfo.columnIndexMap.rowType]);
  if (rowType !== "step") {
    return true;
  }

  const rowIdx = sheetInfo.selectedSheetData.indexOf(row);
  const existingScore = sheetInfo.existingDifficulty?.[rowIdx];
  if (existingScore) {
    return scorePassesPostHocFilter(existingScore, filterOptions);
  }

  const question = findGeneratedQuestionForRow(row, sheetInfo);
  if (question) {
    const pseudoScore = {
      scored:
        question.difficultyScored !== false &&
        question.difficultyLogit != null &&
        question.difficultyLogit !== undefined,
      difficultyLogit: question.difficultyLogit,
    };
    return scorePassesPostHocFilter(pseudoScore, filterOptions);
  }

  return scorePassesPostHocFilter(existingScore, filterOptions);
}

function problemHasVisibleSteps(rowsForProblemName, sheetInfo, rowTypeIndex) {
  const filterOptions = getPostHocFilterOptions(sheetInfo);
  if (!filterOptions.filterEnabled || !sheetInfo?.existingDifficulty) {
    return true;
  }

  const steps = rowsForProblemName.filter(
    (row) => normalizeString(row[rowTypeIndex]) === "step"
  );
  if (steps.length === 0) {
    return true;
  }

  return steps.some((row) => rowPassesPostHocDifficultyFilter(row, sheetInfo));
}

function rowIsVisibleInSheetContent(row, sheetInfo, showHumanHints) {
  const cim = sheetInfo?.columnIndexMap;
  if (!cim || !row) {
    return false;
  }

  const rowTypeIndex = cim.rowType;
  const oerSrcIndex = cim.oerSrc;
  const problemNameIndex = cim.problemName;
  const rowType = normalizeString(row[rowTypeIndex]);
  const isProblemType = rowType === "problem";
  const isOpenAIHint = row[oerSrcIndex] === "openai";
  const isHumanHintVisible = showHumanHints || isOpenAIHint;
  const isHintOrScaffold = ["hint", "scaffold"].includes(rowType);

  if (isProblemType) {
    return false;
  }

  const passesRowFilter =
    (isHumanHintVisible || !isHintOrScaffold) &&
    rowPassesPostHocDifficultyFilter(row, sheetInfo);

  if (!passesRowFilter) {
    return false;
  }

  const problemName = row[problemNameIndex];
  const rowsForProblem = sheetInfo.selectedSheetData.filter(
    (r) => r[problemNameIndex] === problemName
  );
  return problemHasVisibleSteps(rowsForProblem, sheetInfo, rowTypeIndex);
}

/**
 * Rows currently shown in SheetContent, plus companion problem rows needed for
 * OATutor export structure when at least one step in the exercise is visible.
 */
export function getVisibleSheetRowsForExport(sheetInfo, { showHumanHints = false } = {}) {
  if (!sheetInfo?.selectedSheetData?.length || !sheetInfo?.columnIndexMap) {
    return [];
  }

  const cim = sheetInfo.columnIndexMap;
  const rowTypeIndex = cim.rowType;
  const problemNameIndex = cim.problemName;
  const selectedSheetData = sheetInfo.selectedSheetData;

  const visibleProblemNames = new Set(
    sheetInfo.problemNames.filter((problemName) => {
      const rowsForProblemName = selectedSheetData.filter(
        (row) => row[problemNameIndex] === problemName
      );
      return problemHasVisibleSteps(rowsForProblemName, sheetInfo, rowTypeIndex);
    })
  );

  const visibleNonProblemRows = selectedSheetData.filter((row) =>
    rowIsVisibleInSheetContent(row, sheetInfo, showHumanHints)
  );

  const visibleProblemNamesFromRows = new Set(
    visibleNonProblemRows.map((row) => row[problemNameIndex]).filter(Boolean)
  );

  const exportProblemNames = new Set([
    ...visibleProblemNames,
    ...visibleProblemNamesFromRows,
  ]);

  const exportRows = [];
  const included = new Set();

  for (const row of selectedSheetData) {
    const problemName = row[problemNameIndex];
    if (!exportProblemNames.has(problemName)) {
      continue;
    }

    const rowType = normalizeString(row[rowTypeIndex]);
    if (rowType === "problem") {
      const hasVisibleStep = selectedSheetData.some(
        (candidate) =>
          candidate[problemNameIndex] === problemName &&
          rowIsVisibleInSheetContent(candidate, sheetInfo, showHumanHints)
      );
      if (!hasVisibleStep) {
        continue;
      }
    } else if (!rowIsVisibleInSheetContent(row, sheetInfo, showHumanHints)) {
      continue;
    }

    if (!included.has(row)) {
      included.add(row);
      exportRows.push(row);
    }
  }

  return exportRows;
}

/** Sort export columns by sheet index and densify sparse row arrays. */
export function buildSheetExportMatrix(
  rows,
  columnIndexMap,
  lessonContentAttributes
) {
  if (!columnIndexMap || !lessonContentAttributes) {
    return { headers: [], rows: [] };
  }

  const keys = EXPORT_COLUMN_KEYS.filter(
    (key) =>
      lessonContentAttributes.hasOwnProperty(key) &&
      typeof columnIndexMap[key] === "number" &&
      columnIndexMap[key] >= 0
  ).sort((a, b) => columnIndexMap[a] - columnIndexMap[b]);

  const headers = keys.map((key) => lessonContentAttributes[key]);
  const matrixRows = (rows || []).map((row) =>
    keys.map((key) => {
      const value = row?.[columnIndexMap[key]];
      return value == null ? "" : String(value);
    })
  );

  return { headers, rows: matrixRows };
}
