import React, {useState, useEffect, useRef} from "react";
import {
  Row,
  Col,
  Card,
  Badge,
  Button,
  ListGroup,
  Container,
  Spinner,
  Alert,
} from "react-bootstrap";
import "katex/dist/katex.min.css";
import { default as ReactLatex } from "react-latex-next";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  faBook,
  faCog,
  faDice,
  faPerson,
  faRobot,
  faWandSparkles,
  faWarning,
} from "@fortawesome/free-solid-svg-icons";
import { convertToLatex, normalizeString } from "../../utilities/utils"; // Import the normalization utility
import {
  isQuestionDifficultyScored,
  labelForLogit,
  scorePassesPostHocFilter,
  scoredDifficultyDisplayLabel,
  scoredDifficultyChipTooltip,
  isScoredWithoutFigure,
  unscoredChipLabel,
  unscoredChipTooltip,
  isAnswerKeyCorrected,
  answerKeyCorrectedChipTooltip,
  ANSWER_KEY_CORRECTED_CHIP_LABEL,
} from "../../utilities/difficultyLevels";
import { buildUnscoredChipContext } from "../../utilities/sheetDifficultyUtils";
import { buildDifficultyDistribution } from "../../utilities/difficultyDistribution";
import { difficultyFilterDisplaySummary } from "../../utilities/difficultyLevels";
import DifficultyDistributionChart from "./DifficultyDistributionChart";
import DifficultyFilterControls from "./DifficultyFilterControls";
import DifficultyProgressBar from "./DifficultyProgressBar";

const Delimiters = [
  { left: "$$", right: "$$", display: false },
  { left: "$", right: "$", display: false },
];
function Latex({ children }) {
  return <ReactLatex delimiters={Delimiters}>{children}</ReactLatex>;
}

/** Prefer backend-rendered LaTeX columns (from annotate_sheet_data); fall back to client convertToLatex. */
function pickRenderedText(row, columnIndexMap, renderedKey, rawKey) {
  const renderedIdx = columnIndexMap[renderedKey];
  if (
    typeof renderedIdx === "number" &&
    renderedIdx >= 0 &&
    row[renderedIdx] != null &&
    String(row[renderedIdx]).trim() !== ""
  ) {
    return String(row[renderedIdx]);
  }
  const rawIdx = columnIndexMap[rawKey];
  const raw = typeof rawIdx === "number" && rawIdx >= 0 ? row[rawIdx] : "";
  return convertToLatex(raw ?? "");
}

function pickMcChoicesForDisplay(row, columnIndexMap) {
  const mcrIdx = columnIndexMap.mcChoicesRendered;
  if (
    typeof mcrIdx === "number" &&
    mcrIdx >= 0 &&
    row[mcrIdx] != null &&
    String(row[mcrIdx]).trim() !== ""
  ) {
    return String(row[mcrIdx]).split("<separator_eba4b31e>");
  }
  const mcIdx = columnIndexMap.mcChoices;
  const raw =
    typeof mcIdx === "number" && mcIdx >= 0 && row[mcIdx]
      ? String(row[mcIdx]).split("|")
      : [];
  return raw.map((choice) => convertToLatex(choice));
}

export function EditableText({ initialText, onChange }) {
  const [editableText, setEditableText] = useState(initialText);
  const [isEditing, setIsEditing] = useState(false);
  const divRef = useRef(null);

  useEffect(() => {
    setEditableText(initialText);
  }, [initialText]);

  const handleBlur = () => {
    if (divRef.current) {
      const newText = divRef.current.textContent.trim();
      if (newText !== editableText) {
        setEditableText(newText);
        onChange?.(newText);
      }
    }
    setIsEditing(false);
  };

  const handleDoubleClick = () => {
    setIsEditing(true);
  };

  return (
    <div
      ref={divRef}
      contentEditable={isEditing}
      suppressContentEditableWarning
      onBlur={handleBlur}
      onDoubleClick={handleDoubleClick}
      style={{
        backgroundColor: isEditing ? "#f9f9f9" : "transparent",
        cursor: isEditing ? "text" : "pointer",
        display: "inline-block",
        minWidth: "10px",
        minHeight: "20px", 
        }}
    >
    {isEditing ? (
      editableText || ""
    ) : editableText ? (
      <Latex>{editableText}</Latex>
    ) : (
      <div>&nbsp;</div>
    )}
    </div>
  );
}


function getPostHocFilterOptions(sheetInfo) {
  const range = sheetInfo?.difficultyRange;
  return {
    filterEnabled: Boolean(sheetInfo?.difficultyFilterEnabled),
    minLevel: Array.isArray(range) ? range[0] : 1,
    maxLevel: Array.isArray(range) ? range[1] : 5,
    includeUnscored: sheetInfo?.includeUnscoredQuestions !== false,
  };
}

function rowPassesPostHocDifficultyFilter(row, sheetInfo) {
  const filterOptions = getPostHocFilterOptions(sheetInfo);
  if (!filterOptions.filterEnabled || !sheetInfo?.existingDifficulty) {
    return true;
  }

  const rowType = normalizeString(row[sheetInfo.columnIndexMap.rowType]);
  if (rowType !== "step") {
    return true;
  }

  const rowIdx = sheetInfo.selectedSheetData.indexOf(row);
  const score = sheetInfo.existingDifficulty[rowIdx];
  return scorePassesPostHocFilter(score, filterOptions);
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

import { isGeneratedProblemName } from "../../utilities/sheetDifficultyUtils";

function isGeneratedSheetRow(row, sheetInfo) {
  const cim = sheetInfo?.columnIndexMap;
  if (!cim) {
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

/** True when existingDifficulty owns this row's chip (scored or unscored). */
function feature2DifficultyBadgeOwnsRow(row, sheetInfo) {
  const existing = sheetInfo?.existingDifficulty;
  if (!existing) {
    return false;
  }
  const rowIdx = sheetInfo.selectedSheetData.indexOf(row);
  return existing[rowIdx] != null;
}

/**
 * "Answer key corrected" chip (same style as the low-confidence chips).
 * ``null`` for verified / unverified / skipped / unchecked rows.
 */
function renderAnswerKeyCorrectedChip(questionOrScore) {
  if (!isAnswerKeyCorrected(questionOrScore)) {
    return null;
  }
  return (
    <Badge
      bg="secondary"
      className="ms-2 opacity-75"
      title={answerKeyCorrectedChipTooltip(questionOrScore)}
    >
      {ANSWER_KEY_CORRECTED_CHIP_LABEL}
    </Badge>
  );
}

function renderGeneratedDifficultyBadge(row, sheetInfo) {
  if (normalizeString(row[sheetInfo.columnIndexMap.rowType]) !== "step") {
    return null;
  }
  if (!isGeneratedSheetRow(row, sheetInfo)) {
    return null;
  }
  // Avoid double chips when Feature 2 / joint already has an entry for this row.
  if (feature2DifficultyBadgeOwnsRow(row, sheetInfo)) {
    return null;
  }

  const question = findGeneratedQuestionForRow(row, sheetInfo);
  if (!question) {
    return null;
  }

  const chipContext = buildUnscoredChipContext(row, sheetInfo, question);
  const correctedChip = renderAnswerKeyCorrectedChip(question);

  if (isQuestionDifficultyScored(question)) {
    const withoutFigure = isScoredWithoutFigure(question);
    return (
      <>
        <Badge
          bg={withoutFigure ? "warning" : "success"}
          text={withoutFigure ? "dark" : undefined}
          className="ms-2"
          title={scoredDifficultyChipTooltip(question)}
        >
          {scoredDifficultyDisplayLabel(question)}
        </Badge>
        {correctedChip}
      </>
    );
  }

  return (
    <>
      <Badge
        bg="secondary"
        className="ms-2 opacity-75"
        title={unscoredChipTooltip(chipContext)}
      >
        {unscoredChipLabel(chipContext)}
      </Badge>
      {correctedChip}
    </>
  );
}

export default function SheetContent({
  sheetInfo,
  setSheetInfo,
  showHumanHints,
  difficultyProgress,
  difficultyFilterEnabled,
  setDifficultyFilterEnabled,
  difficultyRange,
  setDifficultyRange,
  includeUnscoredQuestions,
  setIncludeUnscoredQuestions,
  lastDifficultyFilterMeta,
}) {
  const [showDifficultyPanel, setShowDifficultyPanel] = useState(false);

  const difficultyDistribution = React.useMemo(
    () => buildDifficultyDistribution(sheetInfo),
    [
      sheetInfo?.existingDifficulty,
      sheetInfo?.generatedScoredQuestionsAll,
      sheetInfo?.priorGeneratedScoredQuestionsAll,
      sheetInfo?.latestGenerationStepProblemNames,
      sheetInfo?.selectedSheetData,
      sheetInfo?.baseSelectedSheetData,
      sheetInfo?.lastJointDifficultyRun,
    ]
  );

  const difficultyRangeSummary = difficultyFilterDisplaySummary(
    difficultyFilterEnabled,
    Array.isArray(difficultyRange) ? difficultyRange[0] : 1,
    Array.isArray(difficultyRange) ? difficultyRange[1] : 5
  );

  function handleTextChange(newText, columnIndex, row, isChoice = false) {
    const rowIndex = sheetInfo.selectedSheetData.findIndex(r => r === row);

    if (rowIndex !== -1) {
      const updatedRow = [...sheetInfo.selectedSheetData[rowIndex]];

      if (isChoice) {
        const mcChoices = updatedRow[sheetInfo.columnIndexMap.mcChoices].split('|');
        mcChoices[columnIndex] = newText;
        updatedRow[sheetInfo.columnIndexMap.mcChoices] = mcChoices.join('|');
      } 
      else {
        updatedRow[columnIndex] = newText;

        const updatedSheetData = sheetInfo.selectedSheetData.map((r, i) =>
          i === rowIndex ? updatedRow : r
        );

        setSheetInfo({ ...sheetInfo, selectedSheetData: updatedSheetData });
      }
    } 
  }

  return (
    <Container className="problems-container">
      {sheetInfo.loading ? (
        <div className="d-flex justify-content-center align-items-center loading-div">
          <Spinner animation="border" role="status" />
        </div>
      ) : sheetInfo.error ? (
        <Card.Body className="d-flex flex-column align-items-center justify-content-center loading-div">
          <h1 className="display-1 text-muted">
            <FontAwesomeIcon icon={faWarning} />
          </h1>
          <h4>Unable to load lesson problems.</h4>
          <div className="text-muted">Error: {sheetInfo.error.message}</div>

          <p className="lead p-2">
            <ul>
              <li>
                <strong>Check the Spreadsheet Format:</strong> Ensure that your
                sheet is using the correct OATutor format, with the standard
                column headers and rows.
              </li>
              <li>
                <strong>Click on a Different Lesson:</strong> If the format of a
                particular sheet is incorrect, try loading problems from a
                different sheet by clicking on lesson on the left-hand panel.
              </li>
              <li>
                <strong>Reload Problems:</strong> If the format looks good, try
                reloading the problems by clicking on{" "}
                <FontAwesomeIcon icon={faDice} /> button in the
                top-right-hand-corner.
              </li>
              <li>
                <strong>Reset Local Storage (Use with Caution):</strong>
                If the issue persists, you can reset the local storage by going
                to <strong>Settings</strong> <FontAwesomeIcon icon={faCog} />.
                <span className="text-danger">
                  {" "}
                  <strong>Warning:</strong> Only reset local storage as a last
                  resort, after trying the above steps. Resetting will erase
                  data stored locally on your device.
                </span>
              </li>
            </ul>
          </p>
        </Card.Body>
      ) : sheetInfo.selectedSheetData ? (
        <>
        {difficultyProgress ? (
          <DifficultyProgressBar progress={difficultyProgress} />
        ) : null}
        <div className="d-flex align-items-center justify-content-between mb-2 flex-wrap gap-2">
          <div className="d-flex align-items-center flex-wrap gap-2">
          <Button
            size="sm"
            variant={showDifficultyPanel ? "primary" : "outline-secondary"}
            title="Show or hide difficulty distribution and display filter"
            onClick={() => setShowDifficultyPanel((prev) => !prev)}
          >
            {showDifficultyPanel
              ? "Hide difficulty distribution / filter"
              : "Show difficulty distribution / filter"}
          </Button>
          </div>
          {sheetInfo.existingDifficultyStatus && (
            <small className="text-muted">
              {sheetInfo.existingDifficultyStatus === "error"
                ? sheetInfo.existingDifficultyError ||
                  "Scoring failed. Try again."
                : sheetInfo.existingRespondentAbility
                  ? `Difficulty scored on the same scale as generated questions. Cohort θ: mean ${Number(sheetInfo.existingRespondentAbility.meanTheta).toFixed(2)}, SD ${Number(sheetInfo.existingRespondentAbility.sdTheta).toFixed(2)} (n=${sheetInfo.existingRespondentAbility.nRespondents}).`
                  : "Difficulty scored on the same scale as generated questions."}
            </small>
          )}
        </div>
        {showDifficultyPanel ? (
          <Card className="mb-3 border-secondary-subtle">
            <Card.Body className="py-3">
              <Card.Title className="h6 mb-3">
                Difficulty distribution / filter
              </Card.Title>
              {!difficultyDistribution.hasScoredData ? (
                <Alert variant="light" className="border mb-3">
                  No difficulty scores yet. Generate questions or estimate
                  difficulty first.
                </Alert>
              ) : (
                <div className="mb-3">
                  <DifficultyDistributionChart
                    distribution={difficultyDistribution}
                  />
                </div>
              )}
              <DifficultyFilterControls
                difficultyFilterEnabled={difficultyFilterEnabled}
                setDifficultyFilterEnabled={setDifficultyFilterEnabled}
                difficultyRange={difficultyRange}
                setDifficultyRange={setDifficultyRange}
                includeUnscoredQuestions={includeUnscoredQuestions}
                setIncludeUnscoredQuestions={setIncludeUnscoredQuestions}
                difficultyRangeSummary={difficultyRangeSummary}
                lastDifficultyFilterMeta={lastDifficultyFilterMeta}
                disabled={!difficultyDistribution.hasScoredData}
              />
            </Card.Body>
          </Card>
        ) : null}
        {!sheetInfo.selectedSheetData?.length ? (
          <p className="text-muted mb-3">
            No problems loaded. Select a lesson in the sidebar or click the
            reload button.
          </p>
        ) : null}
        <Row xs={2} md={1} className="g-4">
          {sheetInfo.problemNames
            .filter((problemName) => {
              const problemNameIndex = sheetInfo.columnIndexMap.problemName;
              const rowTypeIndex = sheetInfo.columnIndexMap.rowType;
              const rowsForProblemName = sheetInfo.selectedSheetData.filter(
                (row) => row[problemNameIndex] === problemName
              );
              return (
                rowsForProblemName.length > 0 &&
                problemHasVisibleSteps(
                  rowsForProblemName,
                  sheetInfo,
                  rowTypeIndex
                )
              );
            })
            .map((problemName, index) => {
              const problemNameIndex = sheetInfo.columnIndexMap.problemName;
              const rowTypeIndex = sheetInfo.columnIndexMap.rowType;
              const oerSrcIndex = sheetInfo.columnIndexMap.oerSrc;

              const rowsForProblemName = sheetInfo.selectedSheetData.filter(
                (row) => row[problemNameIndex] === problemName
              );
              const problemRow = rowsForProblemName.find(
                (row) => normalizeString(row[rowTypeIndex]) === "problem" // Using normalizeString for comparison
              );

              const titleIndex = sheetInfo.columnIndexMap.title;
              const title = problemRow
                ? pickRenderedText(
                    problemRow,
                    sheetInfo.columnIndexMap,
                    "titleRendered",
                    "title"
                  )
                : "Untitled";

              return (
                <Col key={index}>
                  <Card className="problem-card">
                    <Card.Header className="problem-header d-flex align-items-center justify-content-between">
                      <strong>{
                      <EditableText 
                      initialText={title} 
                      onChange={(newText) => handleTextChange(newText, titleIndex, problemRow)}
                      />
                      }</strong>
                      <Badge bg="dark" text="light">
                        {problemName}
                      </Badge>
                    </Card.Header>
                    <Card.Body className="problem-body p-0">
                      {problemRow &&
                        problemRow[sheetInfo.columnIndexMap.bodyText] && (
                          <div className="p-3 bg-light border-bottom">
                            <EditableText 
                            initialText={pickRenderedText(
                              problemRow,
                              sheetInfo.columnIndexMap,
                              "bodyTextRendered",
                              "bodyText"
                            )}
                            onChange={(newText) => handleTextChange(newText, sheetInfo.columnIndexMap.bodyText, problemRow)}
                            />
                          </div>
                        )}

                      {problemRow &&
                        problemRow[sheetInfo.columnIndexMap.images] && (
                          <div className="p-3 border-bottom text-center">
                            {problemRow[sheetInfo.columnIndexMap.images]
                              .split(" ")
                              .map((src) => {
                                return (
                                  <img
                                    key={`problem_image_${index}`}
                                    src={src}
                                    style={{ height: "100%", width: "100%" }}
                                  />
                                );
                              })}
                          </div>
                        )}

                      <ListGroup variant="flush" className="problem-details">
                        {rowsForProblemName.map((row, rowIndex) => {
                          const isProblemType = ["problem"].includes(
                            normalizeString(row[rowTypeIndex])
                          );
                          const isOpenAIHint = row[oerSrcIndex] === "openai";
                          const isHumanHintVisible =
                            showHumanHints || isOpenAIHint;
                          const isHintOrScaffold = [
                            "hint",
                            "scaffold",
                          ].includes(normalizeString(row[rowTypeIndex]));

                          const shouldRenderRow =
                            !isProblemType &&
                            (isHumanHintVisible || !isHintOrScaffold) &&
                            rowPassesPostHocDifficultyFilter(row, sheetInfo);

                          return (
                            shouldRenderRow && (
                              <ListGroup.Item
                                key={rowIndex}
                                variant={isOpenAIHint ? "success" : ""}
                                onClick={() => {
                                  console.log("I was clicked");
                                  console.log(problemName);
                                }}
                              >
                                {/* Display for different types of rows */}

                                {displayRow(row, sheetInfo, setSheetInfo, isOpenAIHint, handleTextChange)}
                              </ListGroup.Item>
                            )
                          );
                        })}
                      </ListGroup>
                    </Card.Body>
                  </Card>
                </Col>
              );
            })}
        </Row>
        </>
      ) : (
        <Card.Body className="d-flex flex-column align-items-center justify-content-center loading-div">
          <h1 className="display-1 text-muted">
            <FontAwesomeIcon icon={faBook} />
          </h1>
          <p className="lead text-center p-2">
            Select a lesson to get started.
          </p>
        </Card.Body>
      )}
    </Container>
  );
}

// Feature 2: badge showing an existing row's calibrated difficulty (label + logit),
// on the SAME scale as generated questions. Out-of-range rows are hidden by the
// post-hoc filter before this badge is rendered.
function renderExistingDifficultyBadge(row, sheetInfo) {
  const existing = sheetInfo.existingDifficulty;
  if (!existing) return null;
  const rowIdx = sheetInfo.selectedSheetData.indexOf(row);
  const score = existing[rowIdx];
  if (!score) return null;

  // Unscored in-batch (e.g. zero_variance): show same chip as generated path.
  const correctedChip = renderAnswerKeyCorrectedChip(score);

  if (
    !score.scored ||
    score.difficultyLogit === null ||
    score.difficultyLogit === undefined
  ) {
    const chipContext = buildUnscoredChipContext(row, sheetInfo, score);
    return (
      <>
        <Badge
          bg="secondary"
          className="ms-2 opacity-75"
          title={unscoredChipTooltip(chipContext)}
        >
          {unscoredChipLabel(chipContext)}
        </Badge>
        {correctedChip}
      </>
    );
  }

  const logit = Number(score.difficultyLogit);
  const label = score.difficultyLabel || labelForLogit(logit);
  const withoutFigure = isScoredWithoutFigure(score);

  const isUncertain =
    score.difficultyUncertain ||
    score.lowConfidence ||
    score.reason === "saturated_low_confidence" ||
    score.reason === "saturated_fallback_no_rasch" ||
    (typeof score.reason === "string" && score.reason.startsWith("saturated_"));

  const badgeBg = withoutFigure ? "warning" : isUncertain ? "secondary" : "success";
  const tooltip = scoredDifficultyChipTooltip(score);

  return (
    <>
      <Badge
        bg={badgeBg}
        text={withoutFigure ? "dark" : undefined}
        className={`ms-2${isUncertain && !withoutFigure ? " opacity-75" : ""}`}
        title={tooltip}
      >
        {scoredDifficultyDisplayLabel({
          ...score,
          difficultyLabel: label,
          difficultyLogit: logit,
        })}
        {!withoutFigure && isUncertain ? " · uncertain" : ""}
      </Badge>
      {correctedChip}
    </>
  );
}

function displayRow(row, sheetInfo, setSheetInfo, isOpenAIHint, handleTextChange) {
  // Here we can handle how to display different types of rows based on the rowType
  const rowType = normalizeString(row[sheetInfo.columnIndexMap.rowType]); // Normalize once
  const title = row[sheetInfo.columnIndexMap.title];
  const bodyText = row[sheetInfo.columnIndexMap.bodyText];
  const answer = row[sheetInfo.columnIndexMap.answer];
  const answerType = row[sheetInfo.columnIndexMap.answerType];
  /**
   * @type {string[]} mcChoices
   */
  const mcChoices = row[sheetInfo.columnIndexMap.mcChoices]?.split("|");
  const hintID = row[sheetInfo.columnIndexMap.hintID]; // Normalize once
  const dependency = row[sheetInfo.columnIndexMap.dependency];

  const titleRendered = pickRenderedText(
    row,
    sheetInfo.columnIndexMap,
    "titleRendered",
    "title"
  );
  const bodyTextRendered = pickRenderedText(
    row,
    sheetInfo.columnIndexMap,
    "bodyTextRendered",
    "bodyText"
  );
  const answerRendered =
    normalizeString(answerType) === "mc"
      ? pickRenderedText(
          row,
          sheetInfo.columnIndexMap,
          "answerRenderedForMC",
          "answer"
        )
      : pickRenderedText(
          row,
          sheetInfo.columnIndexMap,
          "answerRendered",
          "answer"
        );
  const answerTypeRendered = convertToLatex(answerType);
  const mcChoicesRendered = pickMcChoicesForDisplay(row, sheetInfo.columnIndexMap);

  const titleIndex = sheetInfo.columnIndexMap.title
  const bodyTextIndex =  sheetInfo.columnIndexMap.bodyText
  const answerIndex = sheetInfo.columnIndexMap.answer

  const images = (() => {
    /**
     * @type {string}
     */
    const imageStr = row[sheetInfo.columnIndexMap.images];
    if (!imageStr) {
      return [];
    }
    console.log(`Images: ${imageStr}`);
    return imageStr.split(' ');
  })();

  switch (rowType) {
    case "step":
      return (
        <>
          {images.length > 0 ? (
            <div className="p-3 text-center">
              {images.map((src, index) => {
                return <img key={`step_image_${index}`} src={src} />;
              })}
            </div>
          ) : (
            <></>
          )}

          <div className="d-flex flex-column gap-2">
            {titleRendered.trim() ? (
              <EditableText
                initialText={titleRendered}
                onChange={(newText) => handleTextChange(newText, titleIndex, row)}
              />
            ) : null}
            {bodyTextRendered.trim() ? (
              <EditableText
                initialText={bodyTextRendered}
                onChange={(newText) =>
                  handleTextChange(newText, bodyTextIndex, row)
                }
              />
            ) : null}
            {!titleRendered.trim() && !bodyTextRendered.trim() ? (
              <EditableText
                initialText=""
                onChange={(newText) => handleTextChange(newText, bodyTextIndex, row)}
              />
            ) : null}
            <div className="d-flex justify-content-end">
              <Badge bg="primary">{rowType}</Badge>
            </div>
          </div>
          <div className="py-3 d-flex align-items-center justify-content-between">
            <div>
              Answer:
              <span className="p-2 m-1 border rounded-2">
              <EditableText
                initialText={answerRendered}
                onChange={(newText) => handleTextChange(newText, answerIndex, row)}
            />        
              </span>
            </div>
            <div className="d-flex align-items-center">
              <Badge bg="info">{answerType}</Badge>
              {renderExistingDifficultyBadge(row, sheetInfo)}
              {renderGeneratedDifficultyBadge(row, sheetInfo)}
            </div>
          </div>
          {answerType.trim().toLowerCase() === "mc" && (
            <>
              Options:
              {mcChoicesRendered.map((choice, choiceIndex) => (
                <span key={choiceIndex} className="p-1 m-1 border rounded-2">
                  <EditableText
                  initialText={choice}
                  onChange={(newText) => handleTextChange(newText, choiceIndex, row, true)}
                />                  
                </span>
              ))}
            </>
          )}
        </>
      );
    case "hint":
      return (
        <>
          <div className="d-flex align-items-center justify-content-between">
            <span className="d-flex align-items-center">
              <strong>HINT: <EditableText 
              initialText={titleRendered}
              onChange={(newText) => handleTextChange(newText, titleIndex, row)}/>
              </strong>
              <Badge bg={isOpenAIHint ? "success" : "light"} text={isOpenAIHint ? "light" : "dark"} className="ms-2">
                {isOpenAIHint ? "AI Generated" : "Human Generated"}
              </Badge>
            </span>
            <Badge bg="secondary">
              {dependency
                ? `id: ${hintID}, dependency: ${dependency}`
                : `id: ${hintID}`}
            </Badge>
          </div>
            <EditableText
              initialText={bodyTextRendered}
              onChange={(newText) => handleTextChange(newText, bodyTextIndex, row)}
            />   
        </>
      );
    case "scaffold":
      return (
        <>
          <div className="py-3 d-flex align-items-center justify-content-between">
            <span className="d-flex align-items-center">
              <strong>SCAFFOLD: {" "}
                <EditableText 
                  initialText={titleRendered}
                  onChange={(newText) => handleTextChange(newText, titleIndex, row)}/>
              </strong>
              <Badge bg={isOpenAIHint ? "success" : "light"} text={isOpenAIHint ? "light" : "dark"} className="ms-2">
                {isOpenAIHint ? "AI Generated" : "Human Generated"}
              </Badge>
            </span>

            {/* <Badge>{rowType}</Badge> */}
            <Badge bg="secondary">
              {dependency
                ? `id: ${hintID}, dependency: ${dependency}`
                : `id: ${hintID}`}{" "}
            </Badge>
          </div>
          {images.length > 0 ? (
            <div className="p-3 text-center">
              {images.map((src, index) => {
                return <img key={`scaffold_image_${index}`} src={src} />;
              })}
            </div>
          ) : (
            <></>
          )}
          <EditableText
              initialText={bodyTextRendered}
              onChange={(newText) => handleTextChange(newText, bodyTextIndex, row)}
            />   
          <div className="d-flex align-items-center justify-content-between">
            <div>
              Answer:
              <span className="p-1 m-1 border rounded-2 bg-light m-2">
              <EditableText
                initialText={answerRendered}
                onChange={(newText) => handleTextChange(newText, answerIndex, row)}
            />                 
            </span>
            </div>
            <Badge bg="info">{answerTypeRendered}</Badge>
          </div>
          <div className="pt-2">
            {answerType.trim().toLowerCase() === "mc" && (
              <>
                Options:
                {mcChoicesRendered.map((choice, choiceIndex) => (
                  <span
                    key={choiceIndex}
                    className="p-1 m-1 border rounded-2 bg-light"
                  >
                <EditableText
                  initialText={choice}
                  onChange={(newText) => handleTextChange(newText, choiceIndex, row, true)}
                />                     
            </span>
                ))}
              </>
            )}
          </div>
        </>
      );
    default:
      return (
        <>
          ???
          <div className="py-3 d-flex align-items-center justify-content-between">
            <strong>{title}</strong>
            <Badge>{rowType}</Badge>
          </div>
        </>
      );
  }
}