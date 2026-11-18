import React from "react";

const PREVIOUS_COLOR = "#fd7e14";
const LATEST_COLOR = "#198754";

function Bar({ count, maxCount, color, label }) {
  const heightPct =
    maxCount > 0 ? Math.max(4, Math.round((count / maxCount) * 100)) : 0;
  return (
    <div
      className="d-flex flex-column align-items-center flex-fill"
      style={{ minWidth: 0 }}
    >
      <div
        className="d-flex align-items-end justify-content-center w-100"
        style={{ height: 120 }}
      >
        <div
          role="img"
          aria-label={`${label}: ${count}`}
          title={`${label}: ${count}`}
          style={{
            width: "72%",
            maxWidth: 36,
            height: `${heightPct}%`,
            backgroundColor: color,
            borderRadius: "3px 3px 0 0",
            minHeight: count > 0 ? 4 : 0,
          }}
        />
      </div>
      <div className="small fw-semibold mt-1">{count}</div>
    </div>
  );
}

function GroupedBars({ previousCount, latestCount, maxCount }) {
  return (
    <div className="d-flex gap-1 w-100 justify-content-center">
      <Bar
        count={previousCount}
        maxCount={maxCount}
        color={PREVIOUS_COLOR}
        label="Pre-existing"
      />
      <Bar
        count={latestCount}
        maxCount={maxCount}
        color={LATEST_COLOR}
        label="Gen AI"
      />
    </div>
  );
}

export default function DifficultyDistributionChart({ distribution }) {
  if (!distribution || !distribution.hasScoredData) {
    return (
      <p className="text-muted small mb-0">
        No scored questions yet. Estimate difficulty or generate new ones
        to see a distribution.
      </p>
    );
  }

  const { mode, bands, unscoredFootnote, showSeparateRunsNote } = distribution;

  const maxCount = Math.max(
    1,
    ...bands.flatMap((band) =>
      mode === "comparison"
        ? [band.previousCount, band.latestCount]
        : mode === "previous"
          ? [band.previousCount]
          : [band.latestCount]
    )
  );

  const totalPrevious =
    distribution.totalPreviousScored ?? distribution.totalExistingScored ?? 0;
  const totalLatest =
    distribution.totalLatestScored ?? distribution.totalGeneratedScored ?? 0;

  return (
    <div>
      {mode === "comparison" ? (
        <div className="d-flex gap-3 mb-2 small">
          <span>
            <span
              className="d-inline-block rounded me-1"
              style={{
                width: 12,
                height: 12,
                backgroundColor: PREVIOUS_COLOR,
              }}
            />
            Pre-existing items (n={totalPrevious})
          </span>
          <span>
            <span
              className="d-inline-block rounded me-1"
              style={{
                width: 12,
                height: 12,
                backgroundColor: LATEST_COLOR,
              }}
            />
            Gen AI items (n={totalLatest})
          </span>
        </div>
      ) : null}

      <div className="d-flex gap-2 align-items-end">
        {bands.map((band) => (
          <div
            key={band.level}
            className="flex-fill text-center"
            style={{ minWidth: 0 }}
          >
            {mode === "comparison" ? (
              <GroupedBars
                previousCount={band.previousCount}
                latestCount={band.latestCount}
                maxCount={maxCount}
              />
            ) : (
              <Bar
                count={
                  mode === "previous" ? band.previousCount : band.latestCount
                }
                maxCount={maxCount}
                color={mode === "previous" ? PREVIOUS_COLOR : LATEST_COLOR}
                label={band.label}
              />
            )}
            <div
              className="small text-muted mt-1 text-truncate"
              title={band.label}
            >
              {band.label}
            </div>
          </div>
        ))}
      </div>

      {showSeparateRunsNote ? (
        <p className="text-muted small mt-2 mb-0">
          Scores may be from different calibration runs. For direct comparison,
          use &ldquo;Include my existing questions in the evaluation&rdquo;
          when generating.
        </p>
      ) : null}

      {unscoredFootnote ? (
        <p className="text-muted small mt-2 mb-0">{unscoredFootnote}</p>
      ) : null}
    </div>
  );
}
