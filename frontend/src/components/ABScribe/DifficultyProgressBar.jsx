import React from "react";
import { LinearProgress } from "@mui/material";
import "../../scss/difficultyProgress.scss";

export default function DifficultyProgressBar({ progress }) {
  if (!progress?.state?.visible) {
    return null;
  }

  const { state } = progress;
  const barColor =
    state.tone === "error"
      ? "error"
      : state.tone === "success"
        ? "success"
        : "primary";

  return (
    <div
      className={[
        "difficulty-progress",
        state.fading ? "difficulty-progress--fading" : "",
        `difficulty-progress--${state.tone}`,
      ]
        .filter(Boolean)
        .join(" ")}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(state.percent)}
      aria-label={state.message || "Difficulty scoring progress"}
    >
      <div className="difficulty-progress__header">
        <span className="difficulty-progress__message">{state.message}</span>
        <span className="difficulty-progress__percent">
          {Math.round(state.percent)}%
        </span>
      </div>
      <LinearProgress
        variant="determinate"
        value={state.percent}
        color={barColor}
        className="difficulty-progress__bar"
      />
    </div>
  );
}
