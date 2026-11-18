import React from "react";
import { Alert, Form } from "react-bootstrap";
import { Slider, Typography } from "@mui/material";
import { DIFFICULTY_LEVELS } from "../../utilities/difficultyLevels";

export default function DifficultyFilterControls({
  difficultyFilterEnabled,
  setDifficultyFilterEnabled,
  difficultyRange,
  setDifficultyRange,
  includeUnscoredQuestions,
  setIncludeUnscoredQuestions,
  difficultyRangeSummary,
  lastDifficultyFilterMeta,
  disabled = false,
}) {
  return (
    <>
      <Typography variant="body2" className="mb-3 text-muted">
        Use the filter to show or hide already-scored questions based on your
        need.
      </Typography>
      <Form.Check
        className="mb-3"
        type="switch"
        id="enable-difficulty-filter-switch"
        label="Apply difficulty filter to displayed questions"
        checked={difficultyFilterEnabled}
        disabled={disabled}
        onChange={(e) => setDifficultyFilterEnabled(e.target.checked)}
      />
      <Typography variant="subtitle2" gutterBottom>
        Difficulty range: {difficultyRangeSummary.rangeText}
      </Typography>
      <Typography variant="caption" display="block" className="mb-2">
        Logit bounds: {difficultyRangeSummary.boundsText}
      </Typography>
      <Slider
        value={difficultyRange}
        onChange={(_, value) => {
          const next = Array.isArray(value) ? value : [value, value];
          setDifficultyRange([next[0], next[1]]);
        }}
        min={1}
        max={5}
        step={1}
        disabled={disabled || !difficultyFilterEnabled}
        marks={DIFFICULTY_LEVELS.map((level) => ({
          value: level.level,
          label: level.label,
        }))}
        valueLabelDisplay="auto"
        valueLabelFormat={(value) => DIFFICULTY_LEVELS[value - 1].label}
      />
      <Form.Check
        className="mt-3"
        type="switch"
        id="include-unscored-switch"
        label="Include unscored questions (recommended)"
        checked={includeUnscoredQuestions}
        disabled={disabled}
        onChange={(e) => setIncludeUnscoredQuestions(e.target.checked)}
      />
      {lastDifficultyFilterMeta ? (
        <Alert variant="info" className="mt-3 mb-0">
          {lastDifficultyFilterMeta.postHocFilter
            ? `Showing ${lastDifficultyFilterMeta.totalAfter}/${lastDifficultyFilterMeta.totalBefore} generated steps`
            : `Last run: ${lastDifficultyFilterMeta.totalAfter}/${lastDifficultyFilterMeta.totalBefore} kept`}
          {" · status "}
          {lastDifficultyFilterMeta.calibrationStatus}
        </Alert>
      ) : null}
    </>
  );
}
