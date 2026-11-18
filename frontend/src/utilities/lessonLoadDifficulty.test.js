import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const hintViewerSrc = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "../components/ABScribe/HintViewer.jsx"),
  "utf8"
);

test("lesson fetch that sets selectedSheetData also clears existingDifficulty", () => {
  const marker = "selectedSheetData: allData";
  const start = hintViewerSrc.indexOf(marker);
  assert.notEqual(start, -1, "lesson-load setSheetInfo should set selectedSheetData");
  const block = hintViewerSrc.slice(start, start + 700);
  assert.match(block, /existingDifficulty:\s*\{\s*\}/);
  assert.match(block, /existingDifficultyStatus:\s*null/);
  assert.match(block, /existingDifficultyError:\s*null/);
  assert.match(block, /existingRespondentAbility:\s*null/);
});
