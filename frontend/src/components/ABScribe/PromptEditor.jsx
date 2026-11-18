import React, { useEffect, useRef, useState } from "react";
import Select from "react-select";

import CheckIcon from "@mui/icons-material/Check";
import ClearIcon from "@mui/icons-material/Clear";
import {
  Alert,
  Badge,
  Button,
  ButtonGroup,
  Card,
  Dropdown,
  Form,
  ListGroup,
  ListGroupItem,
  OverlayTrigger,
  Tooltip,
  Modal,
} from "react-bootstrap";
import { useHorizontalScroll } from "../ABScribe/useHorizontalScroll";

import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";

import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Chip,
  LinearProgress,
  List,
  ListItem,
  ListItemText,
  Typography,
} from "@mui/material";

import {
  faCheck,
  faClone,
  faEllipsisV,
  faFloppyDisk,
  faGrip,
  faPencil,
  faPlay,
  faPlus,
  faSpinner,
  faTrashAlt,
  faXmark,
  faCaretDown,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { Reorder } from "framer-motion";
import ReactTextareaAutosize from "react-textarea-autosize";
import { API_BASE_URL } from "../../config";
import {
  apiHeaders,
  handleAccessCodeUnauthorized,
} from "../../utilities/apiHeaders";
import "../../scss/prompteditor.scss";
import {
  areSetsEqual,
  areUnorderedListsEqual,
  generateNameFromSeed,
  generateRandomString,
  getCurrentTimestamp,
  normalizeString,
  numberToLetters,
  isString,
} from "../../utilities/utils";
import HintExporter from "./HintExporter";
import {
  filterGeneratedQuestionsPostHoc,
  isQuestionDifficultyScored,
  labelForLogit,
  scoredDifficultyDisplayLabel,
  scoredDifficultyChipTooltip,
  isScoredWithoutFigure,
  unscoredChipLabel,
  unscoredChipTooltip,
} from "../../utilities/difficultyLevels";
import {
  baseSheetHasStepRows,
  buildUnscoredChipContextFromGeneratedQuestions,
  questionsToSheetRows,
  sheetRowsToOatutorQuestions,
  splitCombinedCalibrationResults,
  stripGeneratedRowsFromSheet,
  selectTemplateSheetRows,
  buildQuestionGenerationTemplates,
  applyGenerationStartState,
  assignGeneratedProblemNames,
  buildAlreadyGeneratedPromptBlock,
  buildForbiddenStemKeySet,
  buildMultiQuotaGenerationPlan,
  buildRefillTemplateSchedule,
  collectPriorGeneratedStepQuestions,
  extractProblemsFromSheetRows,
  filterUniqueExerciseGroups,
  groupQuestionsIntoExercises,
  stepQuestionsFromExerciseGroups,
  stepProblemNamesFromGeneratedQuestions,
} from "../../utilities/sheetDifficultyUtils";
import {
  calibrateGeneratedQuestions,
  scoreExistingQuestions,
} from "../../services/difficultyService";

const flattenExerciseGroups = (groups) => {
  if (!Array.isArray(groups) || groups.length === 0) {
    return [];
  }
  return groups.flat();
};

/** Cap total exercises across template batches; round-robin when trimming down. */
const capExerciseGroupsAcrossBatches = (batchGroups, maxTotal) => {
  if (!Array.isArray(batchGroups) || batchGroups.length === 0 || maxTotal <= 0) {
    return [];
  }
  const total = batchGroups.reduce(
    (sum, batch) => sum + (Array.isArray(batch) ? batch.length : 0),
    0
  );
  if (total <= maxTotal) {
    return flattenExerciseGroups(batchGroups.flat());
  }
  const pointers = batchGroups.map(() => 0);
  const selected = [];
  let count = 0;
  while (count < maxTotal) {
    let added = false;
    for (let b = 0; b < batchGroups.length && count < maxTotal; b++) {
      const batch = batchGroups[b];
      if (!Array.isArray(batch)) {
        continue;
      }
      const ptr = pointers[b];
      if (ptr < batch.length) {
        selected.push(...batch[ptr]);
        pointers[b] += 1;
        count += 1;
        added = true;
      }
    }
    if (!added) {
      break;
    }
  }
  return selected;
};

const rebuildSheetWithGeneratedQuestions = ({
  baseSheetRows,
  generatedQuestionsAll,
  filterEnabled,
  difficultyRange,
  includeUnscored,
  columnIndexMap,
  lessonContentAttributes,
}) => {
  const base = baseSheetRows || [];
  const visible = filterGeneratedQuestionsPostHoc(generatedQuestionsAll || [], {
    filterEnabled,
    minLevel: difficultyRange[0],
    maxLevel: difficultyRange[1],
    includeUnscored,
  });
  const generatedRows = questionsToSheetRows(
    visible,
    base,
    columnIndexMap,
    lessonContentAttributes
  );
  const selectedSheetData = [...base, ...generatedRows];
  const allProblemNames = Array.from(
    new Set(
      selectedSheetData
        .map((row) => row[columnIndexMap.problemName])
        .filter(Boolean)
    )
  );
  return {
    selectedSheetData,
    problemNames: allProblemNames,
    visibleCount: visible.filter(
      (q) =>
        String(q?.["Row Type"] || q?.rowType || "")
          .trim()
          .toLowerCase() === "step"
    ).length,
    totalGeneratedSteps: (generatedQuestionsAll || []).filter(
      (q) =>
        String(q?.["Row Type"] || q?.rowType || "")
          .trim()
          .toLowerCase() === "step"
    ).length,
  };
};

const useDebounce = (value, delay) => {
  const [debouncedValue, setDebouncedValue] = useState(value);

  useEffect(() => {
    const handler = setTimeout(() => {
      setDebouncedValue(value);
    }, delay);

    return () => {
      clearTimeout(handler);
    };
  }, [value, delay]);

  return debouncedValue;
};

export default function PromptEditor({
  selectedSheetData,
  columnIndexMap,
  setSheetInfo,
  lessonContentAttributes,
  setShowSettings,
  temperature,
  model,
  setModel,
  showSystemMessages,
  addMessageEntry,
  messageHistory,
  deleteMessageEntry,
  sheetInfo,
  addMessageEntryAtIndex,
  updateUserMessagesAtIndex,
  activeMessageIndex,
  setActiveMessageIndex,
  commitMessageEntryToLibrary,
  libraryTabKey,
  updateLessonsTestedOnPrompt,
  updateShetInfoAtIndex,
  blankUserMessages,
  defaultSytemMessages,
  questionSytemMessages,
  notify,
  commitMessageEntryFromScratchpad,
  sheetId,
  createMessageEntry,
  showHumanHints = false,
  showDifficultyModal,
  setShowDifficultyModal,
  difficultyProgress,
  difficultyFilterEnabled,
  setDifficultyFilterEnabled,
  difficultyRange,
  setDifficultyRange,
  includeUnscoredQuestions,
  setIncludeUnscoredQuestions,
  lastDifficultyFilterMeta,
  setLastDifficultyFilterMeta,
  userId,
}) {
  const LOCAL_STORAGE_KEYS = {
    userMessages: "promptwriter_user_messages",
    systemMessages: "promptwriter_system_messages",
    scratchpadId: "promptwriter_scratchpad_id",
    parentScratchpadId: "promptwriter_parent_scratchpad_id",
  };

  const scrollRef = useHorizontalScroll();
  const [isOpenEnded, setIsOpenEnded] = useState(false);

  useEffect(() => {
    scrollToBottom2();
  }, [addMessageEntry]);

  const [showExportPanel, setShowExportPanel] = useState(true);
  const [showModelModal, setShowModelModal] = useState(false);
  const [availableModels, setAvailableModels] = useState(() =>
    model ? [{ id: model }] : [{ id: "gpt-4o" }]
  );
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsError, setModelsError] = useState(null);

  const notifyModelFallback = (jsonData) => {
    if (!jsonData?.modelFallback || typeof notify !== "function") {
      return;
    }
    const requested = jsonData.modelRequested || model;
    const used = jsonData.model || "gpt-4o";
    notify(
      `Structured output unavailable for ${requested}; ran with ${used} instead.`
    );
  };

  const loadAvailableModels = async () => {
    setModelsLoading(true);
    setModelsError(null);
    try {
      const response = await fetch(`${API_BASE_URL}/chatGPT/models`, {
        headers: { ...apiHeaders() },
      });
      if (response.status === 401) {
        handleAccessCodeUnauthorized();
        throw new Error("Access code incorrect");
      }
      const data = await response.json();
      const ids = (data?.models || [])
        .map((entry) => (typeof entry === "string" ? entry : entry?.id))
        .filter(Boolean);
      if (ids.length === 0) {
        throw new Error(data?.error || "No models returned");
      }
      const unique = [...new Set(ids)];
      if (model && !unique.includes(model)) {
        unique.unshift(model);
      }
      setAvailableModels(unique.map((id) => ({ id })));
    } catch (err) {
      console.error("Failed to load OpenAI models:", err);
      setModelsError(err?.message || "Failed to load models");
      const fallback = model || "gpt-4o";
      setAvailableModels([{ id: fallback }]);
    } finally {
      setModelsLoading(false);
    }
  };

  useEffect(() => {
    if (showModelModal) {
      loadAvailableModels();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showModelModal]);

  const scrollToBottom2 = () => {
    activeMessageRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  const loadStateFromLocalStorage = (key, defaultValue) => {
    const savedState = localStorage.getItem(key);
    return savedState ? JSON.parse(savedState) : defaultValue;
  };
  const [userMessages, setUserMessages] = useState(() =>
    loadStateFromLocalStorage(
      LOCAL_STORAGE_KEYS.userMessages,
      blankUserMessages
    )
  );
  const handleToggleChange = () => {
    setIsOpenEnded((prev) => !prev);
  };
  const [scratchpadId, setScratchpadId] = useState(
    () => messageHistory[activeMessageIndex].scratchpadId
  );

  const [parentScratchpadId, setParentScratchpadId] = useState(
    () => messageHistory[activeMessageIndex].parentScratchpadId
  );
  const activeMessageRef = useRef(null);
  const debouncedUserMessagesValue = useDebounce(userMessages, 300);

  useEffect(() => {
    const entry = messageHistory[activeMessageIndex];
    if (entry) {
      setUserMessages(entry.userMessages);
      setSystemMessages(entry.systemMessages);
      setScratchpadId(entry.scratchpadId);
      setParentScratchpadId(entry.parentScratchpadId);
    }
  }, [activeMessageIndex]);

  const [systemMessages, setSystemMessages] = useState(() =>
    loadStateFromLocalStorage(
      LOCAL_STORAGE_KEYS.systemMessages,
      defaultSytemMessages
    )
  );
  const [showSyllabusModal, setShowSyllabusModal] = useState(false);
  const [syllabusText, setSyllabusText] = useState("");
  const [showEstimateModal, setShowEstimateModal] = useState(false);
  const [questionsToGenerate, setQuestionsToGenerate] = useState(1);
  const [nRespondents, setNRespondents] = useState(150);
  const [includeExistingInEvaluation, setIncludeExistingInEvaluation] =
    useState(false);
  const [useOriginalQuestionsAsTemplates, setUseOriginalQuestionsAsTemplates] =
    useState(true);
  const [
    useAiGeneratedQuestionsAsTemplates,
    setUseAiGeneratedQuestionsAsTemplates,
  ] = useState(true);

  const baseRowsForDifficulty = stripGeneratedRowsFromSheet(
    selectedSheetData,
    columnIndexMap?.problemName
  );
  const canIncludeExistingInEvaluation = baseSheetHasStepRows(
    baseRowsForDifficulty,
    columnIndexMap
  );

  useEffect(() => {
    if (!canIncludeExistingInEvaluation && includeExistingInEvaluation) {
      setIncludeExistingInEvaluation(false);
    }
  }, [canIncludeExistingInEvaluation, includeExistingInEvaluation]);

  // Feature 2: expose difficulty range + respondent count via sheetInfo.
  useEffect(() => {
    setSheetInfo((prevState) => {
      const current = prevState?.difficultyRange;
      const currentEnabled = prevState?.difficultyFilterEnabled;
      const currentN = prevState?.nRespondents;
      const currentIncludeUnscored = prevState?.includeUnscoredQuestions;
      const currentIncludeExisting = prevState?.includeExistingInEvaluation;
      const rangeSame =
        Array.isArray(current) &&
        current[0] === difficultyRange[0] &&
        current[1] === difficultyRange[1];
      const enabledSame = currentEnabled === difficultyFilterEnabled;
      const nSame = currentN === nRespondents;
      const includeUnscoredSame =
        currentIncludeUnscored === includeUnscoredQuestions;
      const includeExistingSame =
        currentIncludeExisting === includeExistingInEvaluation;
      if (
        rangeSame &&
        enabledSame &&
        nSame &&
        includeUnscoredSame &&
        includeExistingSame
      ) {
        return prevState;
      }
      return {
        ...prevState,
        difficultyRange,
        difficultyFilterEnabled,
        nRespondents,
        includeUnscoredQuestions,
        includeExistingInEvaluation,
      };
    });
  }, [
    difficultyRange,
    difficultyFilterEnabled,
    nRespondents,
    includeUnscoredQuestions,
    includeExistingInEvaluation,
    setSheetInfo,
  ]);
  const messagesEndRef = useRef(null);
  const prevMessagesRef = useRef();
  // generationRunId of the most recent Generate click, so a later "Save to
  // Shared Library" can link the library prompt to those Generate records.
  const lastGenerationRunIdRef = useRef(null);

  const [messageDrag, setMessageDrag] = useState(false);

  const [gptResult, setGptResult] = useState({});
  const [gptProgress, setGptProgress] = useState(0);
  const [gptProgressVisible, setGptProgressVisible] = useState(false);
  const progressBusy =
    gptProgressVisible || Boolean(difficultyProgress?.isBusy);

  useEffect(() => {
    if (gptProgress === 100) {
      const timeout = setTimeout(() => {
        setGptProgressVisible(false); // Hide progress bar after a delay
      }, 2000); // Hide after 2 seconds

      // Cleanup the timeout if the component unmounts or gptProgress changes
      return () => clearTimeout(timeout);
    }
  }, [gptProgress]);

  const [gptLoading, setGptLoading] = useState({});
  const [gptLoadingFinished, setGptLoadingFinished] = useState(true);

  const [showLLMOutput, setShowLLMOutput] = useState(false);

  useEffect(() => {
    const result = {};
    for (const key in gptLoading) {
      const selectedSheetData2 = Array.from(
        new Set(selectedSheetData.map((arr) => JSON.stringify(arr)))
      ).map((str) => JSON.parse(str));
      if (gptLoading.hasOwnProperty(key) && !gptLoading[key]) {
        // Check if the corresponding gptResult is a valid JSON string
        const resultString = gptResult[key];
        if (isJsonString(resultString)) {
          const parsedResult = JSON.parse(resultString);
          if (parsedResult.hasOwnProperty("hints")) {
            const hints = parsedResult.hints;
            // Loop through the columnIndexMap and set values in the new row
            let hintRows = [];
            for (let i = 0; i < hints.length; i++) {
              const newRow = [];
              let hint = hints[i];
              for (const colKey in columnIndexMap) {
                if (columnIndexMap.hasOwnProperty(colKey)) {
                  const index = columnIndexMap[colKey];

                  // Ensure lessonContentAttributes[colKey] is valid
                  if (lessonContentAttributes.hasOwnProperty(colKey)) {
                    const attributeKey = lessonContentAttributes[colKey];
                    newRow[index] = hint.hasOwnProperty(attributeKey)
                      ? hint[attributeKey]
                      : "";
                  } else {
                    // If colKey is not a valid key in lessonContentAttributes, set an empty string
                    newRow[index] = "";
                  }
                }
              }
              newRow[columnIndexMap.oerSrc] = "openai";
              hintRows.push(newRow);
              //console.log(newRow);
            }
            result[key] = hintRows;
          }
          let updatedSelectedSheetData = selectedSheetData2;
          if (parsedResult.hasOwnProperty("hints")) {
            updatedSelectedSheetData = insertHints(selectedSheetData2, result);
          }

          if (parsedResult.hasOwnProperty("questions")) {
            if (parsedResult.difficultyFilter) {
              setLastDifficultyFilterMeta(parsedResult.difficultyFilter);
            }
          } else if (parsedResult.hasOwnProperty("hints")) {
            const allProblemNames = Array.from(
              new Set(
                updatedSelectedSheetData
                  .map((row) => row[columnIndexMap["problemName"]])
                  .filter(Boolean)
              )
            );

            setSheetInfo((prevState) => ({
              ...prevState,
              selectedSheetData: updatedSelectedSheetData,
              problemNames: allProblemNames,
            }));
          }
        }
      }
    }
  }, [gptLoading, gptResult, selectedSheetData]); // gptResult required so async completion is not missed

  // Post-hoc difficulty filter: show/hide already-scored generated rows (no API calls).
  useEffect(() => {
    const allGenerated = sheetInfo?.generatedScoredQuestionsAll;
    const baseRows = sheetInfo?.baseSelectedSheetData;
    if (!Array.isArray(allGenerated) || allGenerated.length === 0) {
      return;
    }
    if (!Array.isArray(baseRows)) {
      return;
    }

    const rebuilt = rebuildSheetWithGeneratedQuestions({
      baseSheetRows: baseRows,
      generatedQuestionsAll: allGenerated,
      filterEnabled: difficultyFilterEnabled,
      difficultyRange,
      includeUnscored: includeUnscoredQuestions,
      columnIndexMap,
      lessonContentAttributes,
    });

    setSheetInfo((prevState) => {
      const currentData = prevState?.selectedSheetData;
      const sameData =
        Array.isArray(currentData) &&
        currentData.length === rebuilt.selectedSheetData.length &&
        currentData.every(
          (row, idx) =>
            JSON.stringify(row) ===
            JSON.stringify(rebuilt.selectedSheetData[idx])
        );
      if (sameData) {
        return prevState;
      }
      return {
        ...prevState,
        selectedSheetData: rebuilt.selectedSheetData,
        problemNames: rebuilt.problemNames,
      };
    });

    setLastDifficultyFilterMeta((prev) => ({
      ...(prev || {}),
      postHocFilter: true,
      filterEnabled: difficultyFilterEnabled,
      totalBefore: rebuilt.totalGeneratedSteps,
      totalAfter: rebuilt.visibleCount,
      calibrationStatus: prev?.calibrationStatus || "calibrated",
    }));
  }, [
    difficultyFilterEnabled,
    difficultyRange,
    includeUnscoredQuestions,
    sheetInfo?.generatedScoredQuestionsAll,
    sheetInfo?.baseSelectedSheetData,
  ]);

  useEffect(() => {
    setGptProgress(calculateProgress());
  }, [gptLoading]);

  function calculateProgress() {
    // Check if gptLoading is defined and is an object
    if (typeof gptLoading !== "object" || gptLoading === null) {
      return 0; // Return 0 progress if gptLoading is not a valid object
    }

    const totalItems = Object.keys(gptResult).length; // Get total number of properties

    if (totalItems === 0) {
      return 0; // Return 0 progress if there are no items
    }

    const trueCount = Object.values(gptLoading).filter((value) => value).length; // Count the number of true values

    // Calculate the progress as the ratio of true values to total items
    const progress = ((totalItems - trueCount) / totalItems) * 100;
    return progress;
  }

  useEffect(() => {
    localStorage.setItem(
      LOCAL_STORAGE_KEYS.userMessages,
      JSON.stringify(userMessages)
    );
  }, [userMessages]);

  useEffect(() => {
    updateUserMessagesAtIndex(activeMessageIndex, userMessages);
  }, [debouncedUserMessagesValue]);

  useEffect(() => {
    updateShetInfoAtIndex(activeMessageIndex, sheetInfo);
  }, [sheetInfo]);

  useEffect(() => {
    localStorage.setItem(
      LOCAL_STORAGE_KEYS.systemMessages,
      JSON.stringify(systemMessages)
    );
  }, [systemMessages]);

  useEffect(() => {
    if (
      prevMessagesRef.current &&
      userMessages.length > prevMessagesRef.current.length
    ) {
      scrollToBottom();
      console.log("message");
    }
    prevMessagesRef.current = userMessages;
  }, [userMessages]);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  const createMessage = (role, content) => {
    const message = {
      id: `message_${generateRandomString(5)}`,
      role: role,
      content: content,
      time_created: getCurrentTimestamp(),
      time_last_updated: getCurrentTimestamp(),
      disabled: false,
      hidden: false,
    };
    if (role == "user") {
      setUserMessages((prev) => [...prev, message]);
    } else if (role == "system") {
      setSystemMessages((prev) => [...prev, message]);
    } else {
      console.log("unreconized role, couldn't create message");
    }
  };

  const deleteMessage = (id) => {
    setUserMessages((prev) => prev.filter((message) => message.id !== id));
    setSystemMessages((prev) => prev.filter((message) => message.id !== id));
  };

  function insertHints(selectedSheetData, result) {
    selectedSheetData = selectedSheetData.filter(
      (row) =>
        normalizeString(row[1]) !== "hint" &&
        normalizeString(row[1]) !== "scaffold"
    );

    let modifiedData = [];
    let stepNumber = 1;
    let lastProblemName = "";
    for (let i = 0; i < selectedSheetData.length; i++) {
      let row = selectedSheetData[i];
      let rowType = row[columnIndexMap.rowType];
      let problemName = row[columnIndexMap.problemName];

      modifiedData.push(row);
      if (normalizeString(rowType) === "step") {
        let key = `${problemName}_step_${stepNumber}`;
        if (result.hasOwnProperty(key)) {
          result[key].forEach((hint) => {
            modifiedData.push(hint);
          });
        }
      }
      if (problemName !== lastProblemName) {
        lastProblemName = problemName;
        stepNumber = 1;
      } else {
        stepNumber = stepNumber + 1;
      }
    }

    return modifiedData;
  }

  const updateUserMessage = (id, role, content) => {
    setUserMessages((prev) =>
      prev.map((message) =>
        message.id === id
          ? {
              ...message,
              role: role,
              content: content,
              time_last_updated: getCurrentTimestamp(),
            }
          : message
      )
    );
  };

  const updateSystemMessage = (id, role, content) => {
    setSystemMessages((prev) =>
      prev.map((message) =>
        message.id === id
          ? {
              ...message,
              role: role,
              content: content,
              time_last_updated: getCurrentTimestamp(),
            }
          : message
      )
    );
  };

  const toggleSystemMessage = (id) => {
    console.log(id);
    setSystemMessages((prev) =>
      prev.map((message) =>
        message.id === id
          ? {
              ...message,
              disabled: !message.disabled,
            }
          : message
      )
    );
  };

  const setUserMessagesFromHistory = (index) => {
    const entry = messageHistory[index];
    if (entry) {
      setUserMessages(entry.userMessages);
    }
  };

  const setSystemMessagesFromHistory = (index) => {
    const entry = messageHistory[index];
    if (entry) {
      setSystemMessages(entry.systemMessages);
    }
  };

  const setSheetInfoFromHistory = (index) => {
    const entry = messageHistory[index];
    if (entry) {
      setSheetInfo(entry.sheetInfo);
    }
  };

  const getProblemListFromSheetInfo = (sheetInfo) => {
    // Check if sheetInfo and selectedSheetData exist and are arrays
    if (!sheetInfo || !Array.isArray(sheetInfo.selectedSheetData)) {
      return new Set(); // Return an empty set if conditions are not met
    }

    const problemNamesSet = new Set();

    // Iterate through each subarray in selectedSheetData
    sheetInfo.selectedSheetData.forEach((subArray) => {
      // Check if subArray is an array and has at least one element
      if (Array.isArray(subArray) && subArray.length > 0) {
        const problemName = subArray[0];
        if (typeof problemName === "string") {
          problemNamesSet.add(problemName); // Add problemName to the set to ensure uniqueness
        }
      }
    });

    // Return the set of unique problem names
    return problemNamesSet;
  };

  const setStatesFromMessageEntry = (index) => {
    // Check if messageHistory and the entry at the given index exist
    if (!Array.isArray(messageHistory) || !messageHistory[index]) {
      console.error("Invalid index or messageHistory is not an array.");
      return;
    }

    const entry = messageHistory[index];

    // Destructure with fallback values
    const {
      sheetInfo: entrySheetInfo = {},
      userMessages,
      systemMessages,
    } = entry;
    const displayedSheetInfo = sheetInfo || {};

    const displayedProblemList =
      getProblemListFromSheetInfo(displayedSheetInfo);
    const entryProblemList = getProblemListFromSheetInfo(entrySheetInfo);

    const displayedTitleIndexes =
      displayedSheetInfo.selectedSheetTitleIndexes || [];
    const entryTitleIndexes = entrySheetInfo.selectedSheetTitleIndexes || [];

    const sameProblems = areSetsEqual(entryProblemList, displayedProblemList);
    const sameLessons = areUnorderedListsEqual(
      entryTitleIndexes,
      displayedTitleIndexes
    );

    console.log("Same Problems:", sameProblems);
    if (!sameProblems) {
      console.log(entry);
      console.log(entryProblemList);
      console.log(displayedSheetInfo);
      console.log(displayedProblemList);
    }
    console.log("Same Lessons:", sameLessons);

    // Safely set user and system messages if they exist
    if (userMessages) {
      setUserMessages(userMessages);
    } else {
      console.warn("userMessages not found in entry.");
    }

    if (systemMessages) {
      setSystemMessages(systemMessages);
    } else {
      console.warn("systemMessages not found in entry.");
    }

    // Update sheetInfo only if the problems and lessons are the same
    if (sameProblems && sameLessons) {
      setSheetInfo(entrySheetInfo);
    } else {
      console.warn("Problems or lessons do not match; sheetInfo not updated.");
    }
  };

  function replaceVariables(str, variables, values) {
    if (variables.length !== values.length) {
      throw new Error("Variables and values arrays must have the same length");
    }

    let result = str;

    for (let i = 0; i < variables.length; i++) {
      const variable = variables[i].replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); // Escape special characters
      const value = values[i];

      const regex = new RegExp(variable, "g");
      result = result.replace(regex, value);
    }

    return result;
  }

  /**
   * Splits a string of image URLs into an array of individual URLs.
   *
   * @param {string} imageStr - A string containing image URLs, separated by a single space.
   * @returns {string[]} An array of individual image URLs. Returns an empty array if the input string is empty or consists solely of whitespace.
   */
  function splitImageStr(imageStr) {
    const trimmed = imageStr.trim();
    if (!trimmed) {
      return [];
    }
    return trimmed.split(" ").filter(Boolean);
  }

  /**
   * Appends a vision prompt to the list of messages being sent to a language model.
   *
   * @param {object[]} messages - The list of messages to which the vision prompt will be added.
   * @param {string[]} imageUrls - An array of image URLs to be included in the vision prompt.
   * @param {string} description - A description to be included along with the image URLs.
   */
  function appendVisionPrompt(messages, imageUrls, description) {
    if (imageUrls.length === 0) {
      return;
    }
    const content = [
      {
        type: "text",
        text: description,
      },
    ];
    for (const url of imageUrls) {
      content.push({
        type: "image_url",
        image_url: {
          url: url,
        },
      });
    }
    const visionPrompt = {
      role: "user",
      content: content,
    };
    messages.push(visionPrompt);
  }

  const executePrompt = async (messages, sheetData, type, batchSize, difficultyFilter = null, outgoingBatch = null, batchSeq = 1, logContext = null) => {
    console.log("Begin executing prompt...");
    const rowsForProblems =
      type === "oatutor_questions"
        ? selectTemplateSheetRows(sheetData, columnIndexMap.problemName, {
            useOriginalQuestionsAsTemplates,
            useAiGeneratedQuestionsAsTemplates,
          })
        : sheetData;
    const problems = extractProblemsFromSheetRows(rowsForProblems, columnIndexMap);

    let priorGeneratedStepsForPrompt = [];

    const buildStepMessages = (problem, step, generateCount, extraKeptSteps = []) => {
        const variabalizedMessage = [];
        for (let message of messages) {
            const variables = [
                "{{questionsToGenerate}}",
                "{{problemName}}",
                "{{problemTitle}}",
                "{{problemBodyText}}",
                "{{problemImages}}",
                "{{stepTitle}}",
                "{{stepBodyText}}",
                "{{stepAnswer}}",
                "{{stepAnswerType}}",
                "{{stepMcChoices}}",
                "{{stepImages}}",
            ];
            const values = [
                String(generateCount),
                problem.problemName,
                problem.problemTitle,
                problem.problemBodyText,
                problem.problemImages,
                step.stepTitle,
                step.stepBodyText,
                step.stepAnswer,
                step.stepAnswerType,
                step.stepMcChoices,
                step.stepImages,
            ];
            variabalizedMessage.push({
                role: message.role,
                content: replaceVariables(message.content, variables, values),
            });
        }
        if (isString(problem.problemImages)) {
            appendVisionPrompt(
                variabalizedMessage,
                splitImageStr(problem.problemImages),
                "The following images are included in the problem description. Please analyze them carefully, as they are essential for your problem-solving process."
            );
        }
        if (isString(step.stepImages)) {
            appendVisionPrompt(
                variabalizedMessage,
                splitImageStr(step.stepImages),
                "The following images are relevant to this step in solving the problem. Please take the time to analyze them carefully, as they play a crucial role in your problem-solving process."
            );
        }
        const alreadyGeneratedBlock = buildAlreadyGeneratedPromptBlock([
          ...priorGeneratedStepsForPrompt,
          ...(Array.isArray(extraKeptSteps) ? extraKeptSteps : []),
        ]);
        if (alreadyGeneratedBlock) {
            variabalizedMessage.push({
                role: "user",
                content: alreadyGeneratedBlock,
            });
        }
        return variabalizedMessage;
    };

    if (type === "oatutor_questions") {
        const generateCount = Math.max(
            1,
            parseInt(difficultyFilter?.questionsToGenerate, 10) || 1
        );
        const templates = buildQuestionGenerationTemplates(problems);
        const promptOnlyTemplates =
          !useOriginalQuestionsAsTemplates &&
          !useAiGeneratedQuestionsAsTemplates;
        const templateProblemNames = templates.map(
          (t) => t.problem?.problemName || "(blank)"
        );
        console.log(
          `[generate templates] useOriginal=${useOriginalQuestionsAsTemplates} useAiGenerated=${useAiGeneratedQuestionsAsTemplates} promptOnly=${promptOnlyTemplates} count=${templates.length} names=[${templateProblemNames.join(", ")}]`
        );
        const { diverseIndices, quotas, templateCount } =
            buildMultiQuotaGenerationPlan(generateCount, templates.length);

        difficultyProgress?.startGenerate?.({ templateCount });

        const resultKey = "generated_questions";
        setGptLoadingFinished(false);
        setGptLoading((prevState) => ({ ...prevState, [resultKey]: true }));
        setGptResult((prevState) => ({ ...prevState, [resultKey]: "" }));

        const batchToPromote = Array.isArray(outgoingBatch) ? outgoingBatch : [];
        let baseRows = [];
        setSheetInfo((prevState) => {
          const nextState = applyGenerationStartState({
            prevState,
            sheetData,
            outgoingBatch: batchToPromote,
            columnIndexMap,
            lessonContentAttributes,
          });
          baseRows = nextState.baseSelectedSheetData || [];
          return nextState;
        });

        const skipFilter = {
          ...(difficultyFilter || {}),
          skipCalibration: true,
        };
        priorGeneratedStepsForPrompt = collectPriorGeneratedStepQuestions({
          priorGeneratedScoredQuestionsAll:
            sheetInfo?.priorGeneratedScoredQuestionsAll,
          outgoingBatch: batchToPromote,
        });
        // Avoid-list uses the full sheet (not the template pool), so prompt-only
        // and AI-only modes still reject stems already on the lesson.
        const lessonExerciseGroups = groupQuestionsIntoExercises(
          sheetRowsToOatutorQuestions(sheetData, columnIndexMap)
        );
        const forbiddenKeys = buildForbiddenStemKeySet({
          lessonExerciseGroups,
          priorStepQuestions: priorGeneratedStepsForPrompt,
        });
        const maxExtraAttempts = generateCount;
        const batchExerciseGroups = [];
        let loopApiFailures = 0;
        let refillApiFailures = 0;
        let modelFallbackNotified = false;
        const notifyModelFallbackOnce = (jsonData) => {
          if (modelFallbackNotified || !jsonData?.modelFallback) {
            return;
          }
          modelFallbackNotified = true;
          notifyModelFallback(jsonData);
        };
        try {
            for (let i = 0; i < templateCount; i++) {
                const { problem, step } = templates[diverseIndices[i]];
                const quota = quotas[i];
                const stepMessages = buildStepMessages(problem, step, quota);
                const genFilter = {
                    ...skipFilter,
                    questionsToGenerate: quota,
                };
                const requestBody = {
                    messages: stepMessages,
                    model: model,
                    temperature: temperature,
                    type: type,
                    isOpenEnded: isOpenEnded,
                    difficultyFilter: genFilter,
                    ...(logContext || {}),
                };

                const requestOnce = async () => {
                    const response = await fetch(`${API_BASE_URL}/chatGPT/json`, {
                        method: "POST",
                        headers: {
                            "Content-Type": "application/json",
                            ...apiHeaders(),
                        },
                        body: JSON.stringify(requestBody),
                    });
                    if (response.status === 401) {
                        handleAccessCodeUnauthorized();
                        throw new Error("Access code incorrect");
                    }
                    const text = await response.text();
                    return JSON.parse(text);
                };

                let jsonData;
                try {
                    jsonData = await requestOnce();
                } catch (parseErr) {
                    console.error("Failed to parse generation JSON:", parseErr);
                    try {
                        jsonData = await requestOnce();
                    } catch (retryErr) {
                        console.error(
                            "Failed to parse generation JSON on retry:",
                            retryErr
                        );
                        loopApiFailures += 1;
                        difficultyProgress?.onTemplateComplete?.(
                            i + 1,
                            templateCount
                        );
                        continue;
                    }
                }
                notifyModelFallbackOnce(jsonData);

                const batch = (jsonData?.questions || []).map((question) => ({
                    ...question,
                    _sourceTemplateProblemName: problem.problemName,
                }));
                const exerciseGroups = groupQuestionsIntoExercises(batch);
                batchExerciseGroups.push(exerciseGroups);
                difficultyProgress?.onTemplateComplete?.(i + 1, templateCount);
            }

            const fromLoopGroups = batchExerciseGroups.flat();
            const fromLoopCount = fromLoopGroups.length;
            let keptGroups = filterUniqueExerciseGroups(
              fromLoopGroups,
              forbiddenKeys
            );
            const keptAfterDedupe = keptGroups.length;
            const droppedAsDuplicates = fromLoopCount - keptAfterDedupe;
            const refillSchedule = buildRefillTemplateSchedule({
              totalTemplates: templates.length,
              successfulTemplateIndices: diverseIndices,
              needed: generateCount,
              maxExtraAttempts,
            });
            let refillIdx = 0;
            let extraAttempts = 0;
            let refillAccepted = 0;
            const progressDenominator = templateCount + maxExtraAttempts;

            while (
              keptGroups.length < generateCount &&
              extraAttempts < maxExtraAttempts
            ) {
                const templateIndex =
                  refillSchedule[refillIdx] ??
                  refillSchedule[refillIdx % Math.max(refillSchedule.length, 1)] ??
                  diverseIndices[extraAttempts % diverseIndices.length];
                refillIdx += 1;
                const { problem, step } = templates[templateIndex];
                const quota = 1;
                const extraKeptSteps = stepQuestionsFromExerciseGroups(keptGroups);
                const stepMessages = buildStepMessages(
                  problem,
                  step,
                  quota,
                  extraKeptSteps
                );
                const genFilter = {
                    ...skipFilter,
                    questionsToGenerate: quota,
                };
                const requestBody = {
                    messages: stepMessages,
                    model: model,
                    temperature: temperature,
                    type: type,
                    isOpenEnded: isOpenEnded,
                    difficultyFilter: genFilter,
                    ...(logContext || {}),
                };

                const requestOnce = async () => {
                    const response = await fetch(`${API_BASE_URL}/chatGPT/json`, {
                        method: "POST",
                        headers: {
                            "Content-Type": "application/json",
                            ...apiHeaders(),
                        },
                        body: JSON.stringify(requestBody),
                    });
                    if (response.status === 401) {
                        handleAccessCodeUnauthorized();
                        throw new Error("Access code incorrect");
                    }
                    const text = await response.text();
                    return JSON.parse(text);
                };

                let jsonData;
                try {
                    jsonData = await requestOnce();
                } catch (parseErr) {
                    console.error("Failed to parse generation JSON:", parseErr);
                    try {
                        jsonData = await requestOnce();
                    } catch (retryErr) {
                        console.error(
                            "Failed to parse generation JSON on retry:",
                            retryErr
                        );
                        refillApiFailures += 1;
                        difficultyProgress?.onTemplateComplete?.(
                            templateCount + extraAttempts + 1,
                            progressDenominator
                        );
                        extraAttempts += 1;
                        continue;
                    }
                }
                notifyModelFallbackOnce(jsonData);

                const batch = (jsonData?.questions || []).map((question) => ({
                    ...question,
                    _sourceTemplateProblemName: problem.problemName,
                }));
                const exerciseGroups = groupQuestionsIntoExercises(batch);
                if (exerciseGroups.length > 0) {
                  const merged = filterUniqueExerciseGroups(
                    [...keptGroups, exerciseGroups[0]],
                    forbiddenKeys
                  );
                  if (merged.length > keptGroups.length) {
                    keptGroups = merged;
                    refillAccepted += 1;
                  }
                }
                extraAttempts += 1;
                difficultyProgress?.onTemplateComplete?.(
                  templateCount + extraAttempts,
                  progressDenominator
                );
            }

            const finalGroups = keptGroups.slice(0, generateCount);
            const finalCount = finalGroups.length;
            console.log(
              `[generate count] requested=${generateCount} fromLoop=${fromLoopCount} droppedDuplicates=${droppedAsDuplicates} keptAfterDedupe=${keptAfterDedupe} refillAttempts=${extraAttempts} refillAccepted=${refillAccepted} loopApiFailures=${loopApiFailures} refillApiFailures=${refillApiFailures} final=${finalCount}`
            );

            const combinedQuestions = flattenExerciseGroups(finalGroups);

            difficultyProgress?.startSimulating?.();

            const existingQuestions = includeExistingInEvaluation
              ? sheetRowsToOatutorQuestions(baseRows, columnIndexMap)
              : [];
            const existingCount = existingQuestions.length;
            const calibrationInput = [
              ...existingQuestions,
              ...combinedQuestions,
            ];

            const calibrated = await calibrateGeneratedQuestions(
                calibrationInput,
                buildCalibrationPayload(),
                {
                  onProgress: (event) => {
                    difficultyProgress?.onSimulateProgress?.(event);
                  },
                  ...(logContext || {}),
                }
            );
            if (calibrated?.difficultyFilter) {
                setLastDifficultyFilterMeta(calibrated.difficultyFilter);
            }
            const { existingDifficulty, generatedQuestions } =
              splitCombinedCalibrationResults(
                calibrationInput,
                calibrated?.difficultyFilter?.evaluations,
                existingCount,
                calibrated?.difficultyFilter?.answerKeyCheckItems
              );
            let generatedForSheet = generatedQuestions;
            if (
              generatedForSheet.length === 0 &&
              existingCount === 0 &&
              combinedQuestions.length > 0
            ) {
              const fallback = calibrated?.questions || [];
              generatedForSheet = fallback.slice(-combinedQuestions.length);
            }
            generatedForSheet = generatedForSheet.map((question, i) => {
              const source = combinedQuestions[existingCount + i];
              if (!source) {
                return question;
              }
              return {
                ...question,
                _sourceTemplateProblemName:
                  question._sourceTemplateProblemName ||
                  source._sourceTemplateProblemName,
              };
            });
            const namedQuestions = assignGeneratedProblemNames(generatedForSheet, {
              existingProblemNames: baseRows.map(
                (row) => row[columnIndexMap.problemName]
              ),
              batchSeq,
            });
            const calibratedForUi = {
                ...(calibrated || {}),
                questions: namedQuestions,
            };
            setGptResult((prevState) => ({
                ...prevState,
                [resultKey]: JSON.stringify(calibratedForUi),
            }));

            setSheetInfo((prevState) => {
              const baseRowsStored =
                prevState.baseSelectedSheetData || baseRows;
              const rebuilt = rebuildSheetWithGeneratedQuestions({
                baseSheetRows: baseRowsStored,
                generatedQuestionsAll: namedQuestions,
                filterEnabled: difficultyFilterEnabled,
                difficultyRange,
                includeUnscored: includeUnscoredQuestions,
                columnIndexMap,
                lessonContentAttributes,
              });
              const latestStepNames = Array.from(
                stepProblemNamesFromGeneratedQuestions(namedQuestions)
              );
              return {
                ...prevState,
                baseSelectedSheetData: rebuilt.selectedSheetData,
                generatedScoredQuestionsAll: namedQuestions,
                latestGenerationStepProblemNames: latestStepNames,
                selectedSheetData: rebuilt.selectedSheetData,
                problemNames: rebuilt.problemNames,
                lastJointDifficultyRun: includeExistingInEvaluation,
                ...(includeExistingInEvaluation
                  ? {
                      existingDifficulty: {
                        ...(prevState.existingDifficulty || {}),
                        ...existingDifficulty,
                      },
                      existingDifficultyStatus:
                        calibrated?.difficultyFilter?.calibrationStatus ||
                        "done",
                      existingDifficultyError: null,
                      existingRespondentAbility:
                        calibrated?.difficultyFilter?.respondentAbility || null,
                    }
                  : {}),
              };
            });
            setLastDifficultyFilterMeta((prev) => ({
              ...(calibrated?.difficultyFilter || prev || {}),
              postHocFilter: true,
              filterEnabled: difficultyFilterEnabled,
              totalBefore: namedQuestions.filter(
                (q) =>
                  String(q?.["Row Type"] || q?.rowType || "")
                    .trim()
                    .toLowerCase() === "step"
              ).length,
              totalAfter: filterGeneratedQuestionsPostHoc(
                namedQuestions,
                {
                  filterEnabled: difficultyFilterEnabled,
                  minLevel: difficultyRange[0],
                  maxLevel: difficultyRange[1],
                  includeUnscored: includeUnscoredQuestions,
                }
              ).filter(
                (q) =>
                  String(q?.["Row Type"] || q?.rowType || "")
                    .trim()
                    .toLowerCase() === "step"
              ).length,
              calibrationStatus:
                calibrated?.difficultyFilter?.calibrationStatus ||
                prev?.calibrationStatus ||
                "calibrated",
            }));
            difficultyProgress?.complete?.();
        } catch (error) {
            console.error("Option B generate+calibrate failed:", error);
            const message =
              error?.message ||
              "Question generation or calibration failed. Check the console for details.";
            difficultyProgress?.fail?.(message);
            if (typeof notify === "function") {
              notify(message);
            }
        } finally {
            setGptLoading((prevState) => ({ ...prevState, [resultKey]: false }));
            setGptLoadingFinished(true);
        }
        console.log("End executing prompt...");
        return;
    }

    const processProblem = async (problem) => {
        for (let i = 0; i < problem.problemSteps.length; i++) {
            const step = problem.problemSteps[i];
            const variabalizedMessage = buildStepMessages(problem, step, 1);
            problem.problemSteps[i].stepMessages = variabalizedMessage;
        }

        for (let i = 0; i < problem.problemSteps.length; i++) {
            let step = problem.problemSteps[i];
            await getChatResponse(
                `${problem.problemName}_step_${i + 1}`,
                step.stepMessages,
                type,
                isOpenEnded,
                difficultyFilter,
                logContext
            );
        }
    };

    if (batchSize) {
        for (let i = 0; i < problems.length; i += batchSize) {
            const batch = problems.slice(i, i + batchSize);
            await Promise.all(batch.map(processProblem));
        }
    } else {
        await Promise.all(problems.map(processProblem));
    }

    console.log("End executing prompt...");
};


const getChatResponse = async (resultKey, messages, type, isOpenEnded, difficultyFilter = null, logContext = null) => {
  setGptLoadingFinished(false);
  let result = "";
  setGptResult((prevState) => ({
      ...prevState,
      [resultKey]: "",
  }));
  setGptLoading((prevState) => ({
      ...prevState,
      [resultKey]: true,
  }));
  try {
      const requestBody = {
          messages: messages,
          model: model,
          temperature: temperature,
          type: type,
          isOpenEnded: isOpenEnded,
          ...(logContext || {}),
      };
      if (type === "oatutor_questions" && difficultyFilter) {
          requestBody.difficultyFilter = difficultyFilter;
      }
      const response = await fetch(`${API_BASE_URL}/chatGPT/json`, {
          method: "POST",
          headers: {
              "Content-Type": "application/json",
              ...apiHeaders(),
          },
          body: JSON.stringify(requestBody),
      });
      if (response.status === 401) {
          handleAccessCodeUnauthorized();
          throw new Error("Access code incorrect");
      }
      let text = await response.text(); // Get response as text
      console.log("Raw Text:", text);

      let jsonData;
      try {
          jsonData = JSON.parse(text); // Try to parse as JSON
          console.log("JSON Data:", jsonData);
      } catch (error) {
          console.error("Failed to parse JSON:", error);
      }
      notifyModelFallback(jsonData);

      setGptLoading((prevState) => ({
          ...prevState,
          [resultKey]: false,
      }));
      if (isOpenEnded) {
        const responseText = jsonData.response
        const problemName = jsonData.problemName
          // If open-ended, treat response as a single hint (plaintext)
          setGptResult((prevState) => ({
              ...prevState,
              [resultKey]: JSON.stringify({
                hints: [
                    {
                        "Body Text": responseText,
                        "HintID": "h1",
                        "Row Type": "hint",
                        "Answer": "",
                        "Answer Rendered": "",
                        "Answer Tutoring": "",
                        "Body Text Rendered": responseText,
                        "Body Text Tutoring": responseText,
                        "Dependency": "",
                        "Problem Name": problemName,
                        "Title": "",
                        "Title Rendered": "",
                        "Title Tutoring": "",
                        "answerMCR": "",
                        "answerType": "",
                        "answerType Rendered": "",
                        "answerType Tutoring": "",
                        "mcChoices": "",
                        "mcChoicesMCR": ""
                    }
                ],
            }),
          }));
      } else {
          if (type === "oatutor_questions" && jsonData?.difficultyFilter) {
              setLastDifficultyFilterMeta(jsonData.difficultyFilter);
          }
          // If structured, parse as JSON
          setGptResult((prevState) => ({
              ...prevState,
              [resultKey]: text,
          }));
      }
  } catch (error) {
      console.error("Something went wrong!", error);
  } finally {
      setGptLoadingFinished(true);
      return result;
  }

    //   await fetchEventSource("http://127.0.0.1:8080/chatGPT/json", {
    //     method: "POST",
    //     headers: {
    //       "Content-Type": "application/json",
    //     },
    //     openWhenHidden: true,
    //     body: JSON.stringify({
    //       messages: messages,
    //       model: model,
    //       temperature: temperature,
    //       type: "json_object",
    //     }),
    //     onmessage(message) {
    //       // console.log(message.data);
    //       setGptResult((prevState) => ({
    //         ...prevState,
    //         [resultKey]: prevState[resultKey]
    //           ? prevState[resultKey] + message.data
    //           : message.data,
    //       }));
    //       setGptLoading((prevState) => ({
    //         ...prevState,
    //         [resultKey]: true,
    //       }));
    //     },
    //     onclose() {
    //       setGptLoading((prevState) => ({
    //         ...prevState,
    //         [resultKey]: false,
    //       }));
    //     },
    //   });
    // } catch (error) {
    //   console.error("Something went wrong!", error);
    // } finally {
    //   setGptLoadingFinished(true);
    //   return result;
    // }
  };

  const resetLocalStorage = () => {
    localStorage.removeItem(LOCAL_STORAGE_KEYS.userMessages);
    localStorage.removeItem(LOCAL_STORAGE_KEYS.systemMessages);
    setSystemMessages(defaultSytemMessages);
    setUserMessages([]);
  };
  // Function to check if a string is a valid JSON string
  const isJsonString = (str) => {
    try {
      JSON.parse(str);
    } catch (e) {
      return false;
    }
    return true;
  };

  // Function to render values, including nested objects and arrays
  const renderValue = (value, keyPrefix) => {
    if (isJsonString(value)) {
      const parsedValue = JSON.parse(value);
      return renderObject(parsedValue, keyPrefix);
    }
    if (typeof value === "object" && value !== null) {
      return renderObject(value, keyPrefix);
    }
    return value ? <span>{value}</span> : null;
  };

  const parsedRespondentsOrDefault = () => {
    const parsedRespondents = parseInt(nRespondents, 10);
    return Number.isFinite(parsedRespondents) ? parsedRespondents : 150;
  };

  const buildCalibrationPayload = () => {
    const parsedQuestions = parseInt(questionsToGenerate, 10);
    const generateCount = Number.isFinite(parsedQuestions)
      ? parsedQuestions
      : 1;
    const payload = {
      noFilter: true,
      includeUnscored: includeUnscoredQuestions,
      questionsToGenerate: generateCount,
      nRespondents: parsedRespondentsOrDefault(),
    };
    if (includeExistingInEvaluation) {
      payload.includeExisting = true;
      payload.scoreSaturatedViaAnchors = true;
    }
    return payload;
  };

  // Estimate difficulty for all rows currently on the sheet (peer of Generate).
  async function handleEstimateDifficulty() {
    const data = sheetInfo?.selectedSheetData;
    const cim = sheetInfo?.columnIndexMap || columnIndexMap;
    if (!Array.isArray(data) || data.length === 0 || !cim) return;
    if (typeof cim.problemName !== "number" || cim.problemName < 0) return;
    if (progressBusy) return;

    setShowEstimateModal(false);
    difficultyProgress?.startScore?.();
    setSheetInfo((prev) => ({
      ...prev,
      existingDifficultyStatus: null,
      existingDifficultyError: null,
      // Keep sheetInfo.nRespondents in lockstep with the Estimate dialog value
      // so Generate calibrate never silently falls back to 150.
      nRespondents: parsedRespondentsOrDefault(),
    }));
    try {
      const questions = sheetRowsToOatutorQuestions(data, cim);
      // Hash the current scratch pad so Estimate records on the same user
      // prompt share a userPromptHash with the Generate records that preceded
      // them (if any). Omitted when the scratchpad is empty.
      const userPromptText = buildScratchpadPromptText();
      const userPromptHash = await sha256Hex(userPromptText);
      // Fresh generationRunId per Estimate click (not tied to any prior Generate).
      const estimateContext = buildLogContext({
        generationRunId: newRunId(),
        batchNumber: Number(sheetInfo?.generationBatchSeq) || null,
        userPromptHash: userPromptHash || null,
      });
      const result = await scoreExistingQuestions(questions, {
        nRespondents: parsedRespondentsOrDefault(),
        onProgress: (event) => {
          difficultyProgress?.onSimulateProgress?.(event);
        },
        ...estimateContext,
      });
      if (result?.error) {
        throw new Error(result.error);
      }
      const map = {};
      (result?.scores || []).forEach((s) => {
        if (s.index !== null && s.index !== undefined) map[s.index] = s;
      });
      setSheetInfo((prev) => ({
        ...prev,
        existingDifficulty: map,
        existingDifficultyStatus: result?.calibrationStatus || "done",
        existingDifficultyError: null,
        existingRespondentAbility: result?.respondentAbility || null,
        lastJointDifficultyRun: false,
      }));
      difficultyProgress?.complete?.();
    } catch (e) {
      console.error("Failed to estimate difficulty:", e);
      const errorMessage =
        e?.response?.data?.error || e?.message || "Scoring request failed.";
      difficultyProgress?.fail?.(errorMessage);
      setSheetInfo((prev) => ({
        ...prev,
        existingDifficultyStatus: "error",
        existingDifficultyError: errorMessage,
      }));
    }
  }

  const buildDifficultyFilterPayload = () => buildCalibrationPayload();

  // Build the shared PromptHive event-logging context for a Generate/Estimate
  // click. ``extra`` lets callers override or add fields (e.g. generationRunId,
  // batchNumber). Returns an object; the backend treats missing fields as null.
  //
  // Two user fields on every log entry:
  //   * ``userId``   = the raw ``hintviewer_user_id`` LocalStorage seed, i.e.
  //                    the same identifier the prompt library saves against
  //                    its rows, so Firestore joins to the library cleanly.
  //   * ``userName`` = ``generateNameFromSeed(userId)`` — the "urban-wallaby"
  //                    style handle the top bar already shows (no new UI).
  const buildLogContext = (extra = {}) => {
    const selectedLessonIdx = Array.isArray(sheetInfo?.selectedSheetTitleIndexes)
      ? sheetInfo.selectedSheetTitleIndexes[0]
      : undefined;
    const lessonName =
      selectedLessonIdx != null &&
      Array.isArray(sheetInfo?.sheetTitles) &&
      sheetInfo.sheetTitles[selectedLessonIdx]
        ? sheetInfo.sheetTitles[selectedLessonIdx]
        : null;
    const displayName = userId ? generateNameFromSeed(userId) : null;
    return {
      userId: userId || null,
      userName: displayName,
      spreadsheetId: sheetId || null,
      lessonName,
      // The scratchpad node these records belong to. executePrompt runs before
      // commitMessageEntryFromScratchpad, so this is the id the node is saved
      // under right after (HintViewer rotates it only after a 201).
      scratchpadId: messageHistory[activeMessageIndex]?.scratchpadId ?? null,
      useOriginalItemsAsTemplates: !!useOriginalQuestionsAsTemplates,
      useGeneratedItemsAsTemplates: !!useAiGeneratedQuestionsAsTemplates,
      clientTimestamp: Date.now(),
      ...extra,
    };
  };

  // Build the "scratch pad text" the user actually wrote — the Syllabus/Topic
  // field plus the raw content of every user-role message in the current
  // scratchpad, joined with newlines. This excludes system prompts and
  // wrapper boilerplate so the resulting hash is stable even if the system
  // prompt changes between calls.
  //
  // Historical bug: ``blankUserMessages`` initialises every scratchpad user
  // message with ``hidden: true`` and nothing in the typing path flips it
  // back to ``false``. Skipping ``msg.hidden`` here therefore erased the
  // actual prompt text. ``hidden`` is a UI-layout flag, NOT a "send to
  // backend?" flag (the message is already concatenated into the outgoing
  // request body unconditionally), so we honour ``disabled`` only.
  //
  // Vision prompts: when the user attaches an image the content becomes an
  // array of OpenAI vision parts (``{type:"text",text}`` and
  // ``{type:"image_url",image_url:{url}}``). Pick the text parts out and
  // ignore the image_url entries so the hash reflects what the user wrote.
  const buildScratchpadPromptText = () => {
    const parts = [];
    if (typeof syllabusText === "string" && syllabusText.trim()) {
      parts.push(syllabusText.trim());
    }
    if (Array.isArray(userMessages)) {
      for (const msg of userMessages) {
        if (!msg || msg.disabled) continue;
        if (msg.role && msg.role !== "user") continue;
        const content = msg.content;
        if (typeof content === "string") {
          const trimmed = content.trim();
          if (trimmed) parts.push(trimmed);
          continue;
        }
        if (Array.isArray(content)) {
          for (const part of content) {
            if (!part) continue;
            if (part.type === "text" && typeof part.text === "string") {
              const t = part.text.trim();
              if (t) parts.push(t);
            }
            // image_url parts intentionally skipped.
          }
        }
      }
    }
    return parts.join("\n");
  };

  // SHA-256 of ``text`` as a lowercase hex string, using Web Crypto when
  // available. Returns ``null`` on empty input or any error so the backend
  // treats it as missing instead of recording "undefined".
  const sha256Hex = async (text) => {
    if (!text || typeof text !== "string") return null;
    try {
      if (
        typeof crypto !== "undefined" &&
        crypto?.subtle &&
        typeof crypto.subtle.digest === "function"
      ) {
        const encoder = new TextEncoder();
        const buffer = await crypto.subtle.digest(
          "SHA-256",
          encoder.encode(text)
        );
        return Array.from(new Uint8Array(buffer))
          .map((b) => b.toString(16).padStart(2, "0"))
          .join("");
      }
    } catch (err) {
      console.warn("sha256Hex (subtle) failed, continuing without hash:", err);
    }
    return null;
  };

  // Prefer the Web Crypto UUID when available; fall back to a plain random
  // string so older browsers (and jsdom in tests) still get an identifier.
  const newRunId = () => {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
    return `run_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
  };

  const handleGenerateQuestions = async () => {
    let messages = [...questionSytemMessages];

    if (syllabusText && syllabusText.trim().length > 0) {
      messages.push({
        id: `message_${generateRandomString(5)}`,
        role: "user",
        content: `Syllabus/Topic:\n${syllabusText}`,
        time_created: getCurrentTimestamp(),
        time_last_updated: getCurrentTimestamp(),
        disabled: false,
        hidden: false,
      });
    }

    messages = messages.concat(userMessages);

    const outgoingBatch = Array.isArray(sheetInfo?.generatedScoredQuestionsAll)
      ? [...sheetInfo.generatedScoredQuestionsAll]
      : [];
    const batchSeq = (Number(sheetInfo?.generationBatchSeq) || 0) + 1;
    setSheetInfo((prevState) => ({
      ...prevState,
      generationBatchSeq: batchSeq,
    }));

    // Scratch-pad text + hash give the record a stable per-prompt key that
    // does not change when the backend rewraps the message list. The hash
    // is computed in the browser so it travels alongside the raw text.
    const userPromptText = buildScratchpadPromptText();
    const userPromptHash = await sha256Hex(userPromptText);

    // One generationRunId ties every /chatGPT/json call for this click, plus
    // the follow-up calibrate call, into a single Generate event in Firestore.
    // The same id is written onto the scratchpad node saved right after, and
    // remembered so a later Save-to-Shared-Library can reference it.
    const generationRunId = newRunId();
    lastGenerationRunIdRef.current = generationRunId;
    const logContext = buildLogContext({
      generationRunId,
      batchNumber: batchSeq,
      userPromptText: userPromptText || null,
      userPromptHash: userPromptHash || null,
    });

    // Order matters: executePrompt first (records carry the current
    // scratchpadId), then the scratchpad save under that same id.
    executePrompt(
      messages,
      selectedSheetData,
      "oatutor_questions",
      null,
      buildDifficultyFilterPayload(),
      outgoingBatch,
      batchSeq,
      logContext
    );
    commitMessageEntryFromScratchpad(activeMessageIndex, {
      userPromptText: userPromptText || null,
      userPromptHash: userPromptHash || null,
      generationRunId,
    });
  };

  const handleGenerateFromModal = () => {
    handleGenerateQuestions();
    setShowDifficultyModal(false);
  };

  const renderQuestionDifficultyChip = (question) => {
    const chipContext = buildUnscoredChipContextFromGeneratedQuestions(
      question,
      sheetInfo?.generatedScoredQuestionsAll
    );
    if (!isQuestionDifficultyScored(question)) {
      return (
        <Chip
          size="small"
          label={unscoredChipLabel(chipContext)}
          variant="outlined"
          color="default"
          className="ms-2"
          title={unscoredChipTooltip(chipContext)}
          sx={{ opacity: 0.88 }}
        />
      );
    }
    return (
      <Chip
        size="small"
        label={scoredDifficultyDisplayLabel(question)}
        variant="outlined"
        color={isScoredWithoutFigure(question) ? "warning" : "primary"}
        className="ms-2"
        title={scoredDifficultyChipTooltip(question)}
      />
    );
  };

  const renderCalibrationEvaluationItem = (evalRow) => {
    if (!evalRow || typeof evalRow !== "object") {
      return null;
    }
    const extras = [
      evalRow.reason != null ? `reason: ${evalRow.reason}` : null,
      evalRow.scored != null ? `scored: ${String(evalRow.scored)}` : null,
      evalRow.difficultyLogit != null
        ? `logit: ${evalRow.difficultyLogit}`
        : null,
      evalRow.difficultyLabel != null
        ? `label: ${evalRow.difficultyLabel}`
        : null,
      evalRow.figureAttached != null
        ? `figureAttached: ${String(evalRow.figureAttached)}`
        : null,
      evalRow.figureCount != null
        ? `figureCount: ${evalRow.figureCount}`
        : null,
      evalRow.figureError != null
        ? `figureError: ${evalRow.figureError}`
        : null,
    ]
      .filter(Boolean)
      .join(" · ");
    return (
      <div>
        <strong>{evalRow.problemName || "?"}</strong>
        {" · "}
        {evalRow.rowType || "?"}
        {evalRow.itemId ? ` · ${evalRow.itemId}` : ""}
        {evalRow.calibrationStem != null && (
          <div className="small mt-1">
            <strong>calibrationStem:</strong> {evalRow.calibrationStem}
          </div>
        )}
        {evalRow.calibrationKey != null && (
          <div className="small">
            <strong>calibrationKey:</strong> {evalRow.calibrationKey}
          </div>
        )}
        {extras ? (
          <div className="small text-muted mt-1">{extras}</div>
        ) : null}
      </div>
    );
  };

  const renderQuestionListItem = (question, keyPrefix) => {
    if (!question || typeof question !== "object") {
      return renderValue(question, keyPrefix);
    }
    const rowType = question["Row Type"] || question.rowType;
    if (rowType !== "step" && rowType !== "problem") {
      return renderValue(question, keyPrefix);
    }
    return (
      <div>
        <strong>{question["Problem Name"] || question.problemName}</strong>
        {" · "}
        {rowType}
        {rowType === "step" ? renderQuestionDifficultyChip(question) : null}
        <div className="text-muted small mt-1">
          {(question["Body Text"] || question.bodyText || "").slice(0, 120)}
        </div>
      </div>
    );
  };

  const renderObject = (obj, keyPrefix = "") => {
    if (typeof obj === "object" && obj !== null && Array.isArray(obj.questions)) {
      return (
        <List>
          {obj.questions.map((question, index) => (
            <ListItem key={`${keyPrefix}-question-${index}`}>
              {renderQuestionListItem(question, `${keyPrefix}-${index}`)}
            </ListItem>
          ))}
        </List>
      );
    }
    if (typeof obj === "object" && obj !== null) {
      if (Array.isArray(obj)) {
        return (
          <List>
            {obj.map((item, index) => (
              <ListItem key={`${keyPrefix}-${index}`}>
                {item &&
                (item.calibrationStem != null ||
                  item.calibrationKey != null ||
                  item.itemId)
                  ? renderCalibrationEvaluationItem(item)
                  : renderValue(item, `${keyPrefix}-${index}`)}
              </ListItem>
            ))}
          </List>
        );
      } else {
        return (
          <List>
            {Object.entries(obj)
              .filter(
                ([key, value]) =>
                  value !== "" && value !== null && value !== undefined
              )
              .map(([key, value], index) => (
                <ListItem key={`${keyPrefix}-${key}`}>
                  <ListItemText
                    primary={key}
                    secondary={renderValue(value, `${keyPrefix}-${key}`)}
                  />
                </ListItem>
              ))}
          </List>
        );
      }
    }
    return obj ? <span>{obj}</span> : null;
  };

  const MessageHistoryViewer = ({ messageHistory }) => (
    <>
      <ListGroup horizontal ref={scrollRef}>
        {messageHistory.map((entry, index) => (
          <>
            <ListGroup.Item
              disabled={progressBusy}
              action
              active={activeMessageIndex == index}
              className="p-0 d-flex align-items-center justify-content-around rounded-0"
              style={{ width: "fit-content" }}
            >
              <span
                className={
                  messageHistory.length > 1 ? "p-1 px-2 ps-3" : "p-1 px-4 pe-4"
                }
                onClick={() => {
                  setActiveMessageIndex(index);
                  setStatesFromMessageEntry(index);
                }}
                // onMouseEnter={() => {
                //   setActiveMessageIndex(index);
                //   setStatesFromMessageEntry(index);
                // }}
                // onMouseLeave={() => {
                //   setStatesFromMessageEntry(activeMessageIndex);
                // }}
              >
                <strong>{numberToLetters(index)}</strong>
              </span>

              {messageHistory.length > 1 ? (
                <Button
                  className="rounded-circle square-md me-1"
                  size="sm"
                  variant={index == activeMessageIndex ? "primary" : "outline"}
                  onClick={() => {
                    deleteMessageEntry(index);
                  }}
                >
                  <FontAwesomeIcon icon={faXmark} size="xs" fixedWidth />
                </Button>
              ) : (
                <></>
              )}
            </ListGroup.Item>
          </>
        ))}
      </ListGroup>
    </>
  );
  return (
    <>
      <Card className="border-top border-bottom border-0 rounded-0 prompt-writing-card">
        <Card.Header className="p-0 pt-1 ps-2">
          <strong>Prompt Scratch Pad</strong>
          <div className="border-0  d-flex ms-2 align-items-center">
            <div className="flex-grow-1 d-flex  message-history-container">
              <div className="align-self-end message-history-listgroup">
                <MessageHistoryViewer messageHistory={messageHistory} />
              </div>

              <div className="d-flex">
                <OverlayTrigger
                  placement="top"
                  overlay={<Tooltip>Create New Blank Prompt</Tooltip>}
                  delay={{ show: 600, hide: 0 }}
                >
                  <Button
                    className="ms-1 text-nowrap new-button"
                    disabled={progressBusy}
                    size="sm"
                    variant="light"
                    onClick={() => {
                      setActiveMessageIndex(messageHistory.length);
                      addMessageEntry(
                        systemMessages,
                        blankUserMessages,
                        sheetInfo
                      );
                    }}
                  >
                    <FontAwesomeIcon icon={faPlus} fixedWidth /> NEW
                  </Button>
                </OverlayTrigger>

                <OverlayTrigger
                  placement="top"
                  overlay={<Tooltip>Clone Selected Prompt</Tooltip>}
                  delay={{ show: 600, hide: 0 }}
                >
                  <Button
                    className="text-nowrap"
                    disabled={activeMessageIndex == null || progressBusy}
                    size="sm"
                    variant="light"
                    onClick={() => {
                      addMessageEntryAtIndex(
                        systemMessages,
                        userMessages,
                        sheetInfo,
                        activeMessageIndex,
                        scratchpadId
                      );
                      setActiveMessageIndex((prevState) => prevState + 1);
                    }}
                  >
                    <FontAwesomeIcon icon={faClone} fixedWidth /> CLONE
                  </Button>
                </OverlayTrigger>
                <OverlayTrigger
                  placement="top"
                  overlay={
                    <Tooltip>
                      Save Prompt to{" "}
                      {libraryTabKey == "textbook"
                        ? "Textbook-Level "
                        : "Lesson-Level "}
                      Shared Library
                    </Tooltip>
                  }
                  delay={{ show: 600, hide: 0 }}
                >
                  <Button
                    className="text-nowrap"
                    size="sm"
                    variant="light"
                    onClick={async () => {
                      // Text + hash computed at click time; the run id is the
                      // most recent Generate's (if any) so the library prompt
                      // links to those records.
                      const userPromptText = buildScratchpadPromptText();
                      const userPromptHash = await sha256Hex(userPromptText);
                      commitMessageEntryToLibrary(activeMessageIndex, {
                        userPromptText: userPromptText || null,
                        userPromptHash: userPromptHash || null,
                        generationRunId: lastGenerationRunIdRef.current || null,
                      });
                    }}
                    disabled={
                      progressBusy ||
                      activeMessageIndex == null ||
                      (sheetInfo.selectedSheetTitleIndexes.length !== 1 &&
                        libraryTabKey != "textbook")
                    }
                  >
                    <FontAwesomeIcon icon={faFloppyDisk} fixedWidth /> SAVE
                  </Button>
                </OverlayTrigger>
              </div>
            </div>

            <div className="d-flex align-items-center">
        <Dropdown>
          <Dropdown.Toggle
            className="text-nowrap"
            size="sm"
            variant="light"
            id="generate-questions-dropdown"
            disabled={progressBusy}
          >
  <FontAwesomeIcon icon={faCaretDown} fixedWidth /> GENERATE QUESTIONS
          </Dropdown.Toggle>
          <Dropdown.Menu>
            <Dropdown.Item
              onClick={() => {
                setShowDifficultyModal(true);
              }}
            >
              Generate settings…
            </Dropdown.Item>
            <Dropdown.Divider />
            <Dropdown.Item onClick={handleGenerateQuestions} disabled={progressBusy}>
             <FontAwesomeIcon icon={faPlay} fixedWidth /> Generate
            </Dropdown.Item>
            <Dropdown.Item
              onClick={() => {
                setShowSyllabusModal(true);
              }}
            >
              Add a Syllabus
            </Dropdown.Item>
          </Dropdown.Menu>
        </Dropdown>
        <Dropdown>
          <Dropdown.Toggle
            className="text-nowrap"
            size="sm"
            variant="light"
            id="estimate-difficulty-dropdown"
            disabled={progressBusy}
          >
            <FontAwesomeIcon icon={faCaretDown} fixedWidth /> ESTIMATE DIFFICULTY
          </Dropdown.Toggle>
          <Dropdown.Menu>
            <Dropdown.Item
              onClick={() => {
                setShowEstimateModal(true);
              }}
            >
              Estimate settings…
            </Dropdown.Item>
            <Dropdown.Divider />
            <Dropdown.Item
              onClick={handleEstimateDifficulty}
              disabled={
                progressBusy ||
                !Array.isArray(sheetInfo?.selectedSheetData) ||
                sheetInfo.selectedSheetData.length === 0
              }
            >
              <FontAwesomeIcon icon={faPlay} fixedWidth /> Estimate difficulty
            </Dropdown.Item>
          </Dropdown.Menu>
        </Dropdown>
              <OverlayTrigger
                placement="top"
                overlay={<Tooltip>Generate Hints</Tooltip>}
                delay={{ show: 600, hide: 0 }}
              >
                <Button
                  className="text-nowrap"
                  size="sm"
                  variant="light"
                  onClick={async () => {
                    setGptProgressVisible(true);
                    // Same text/hash as Generate so the hint node is keyed by
                    // the prompt the user wrote. This executePrompt call has
                    // no logContext (and therefore no run id), hence null.
                    const userPromptText = buildScratchpadPromptText();
                    const userPromptHash = await sha256Hex(userPromptText);
                    executePrompt(
                      Array.prototype.concat(userMessages, systemMessages),
                      selectedSheetData,
                      "oatutor_hints"
                    );
                    commitMessageEntryFromScratchpad(activeMessageIndex, {
                      userPromptText: userPromptText || null,
                      userPromptHash: userPromptHash || null,
                      generationRunId: null,
                    });
                    //updateLessonsTestedOnPrompt(activeMessageIndex);
                  }}
                >
                  <FontAwesomeIcon icon={faCaretDown} fixedWidth /> GENERATE HINTS
                </Button>
              </OverlayTrigger>
              <Form.Check 
        type="switch"
        id="open-ended-switch"
        label="Open-Ended Prompting"
        checked={isOpenEnded}
        onChange={handleToggleChange}
        className="mt-2"
      />
        <Modal show={showSyllabusModal} onHide={() => setShowSyllabusModal(false)} centered>
  <Modal.Header closeButton>
    <Modal.Title>Generate Questions from Syllabus</Modal.Title>
  </Modal.Header>
  <Modal.Body>
    <Form.Group>
      <Form.Label>Paste your syllabus or topic here:</Form.Label>
      <Form.Control
        as="textarea"
        rows={10}
        value={syllabusText}
        onChange={(e) => setSyllabusText(e.target.value)}
        placeholder="Enter syllabus or topic..."
      />
    </Form.Group>
  </Modal.Body>
  <Modal.Footer>
  </Modal.Footer>
</Modal>
        <Modal
          show={showEstimateModal}
          onHide={() => setShowEstimateModal(false)}
          centered
        >
          <Modal.Header closeButton>
            <Modal.Title>Estimate difficulty settings</Modal.Title>
          </Modal.Header>
          <Modal.Body>
            <Form.Group className="mt-1">
              <Form.Label className="text-muted">
                Simulated respondents
              </Form.Label>
              <Form.Control
                type="number"
                size="sm"
                value={nRespondents}
                onChange={(e) => setNRespondents(e.target.value)}
              />
              <Form.Text muted>
                LLM students for IRT calibration. Shared with Generate so both
                flows use the same respondent count.
              </Form.Text>
            </Form.Group>
            <Form.Text muted className="d-block mt-2">
              Note: For reliable difficulty scoring, we recommend at least 20
              respondents and at least 4 questions. Fewer may not produce a
              difficulty estimate.
            </Form.Text>
            <Form.Check
              className="mt-3"
              type="switch"
              id="include-existing-in-evaluation-switch"
              label="Include my existing questions in the evaluation"
              checked={includeExistingInEvaluation}
              disabled={!canIncludeExistingInEvaluation}
              onChange={(e) =>
                setIncludeExistingInEvaluation(e.target.checked)
              }
            />
            <Form.Text muted className="d-block">
              Also applies when generating questions, so new and existing
              questions are scored on the same scale.
            </Form.Text>
          </Modal.Body>
          <Modal.Footer>
            <Button
              variant="secondary"
              onClick={() => setShowEstimateModal(false)}
            >
              Done
            </Button>
            <Button
              variant="primary"
              onClick={handleEstimateDifficulty}
              disabled={
                progressBusy ||
                !Array.isArray(sheetInfo?.selectedSheetData) ||
                sheetInfo.selectedSheetData.length === 0
              }
            >
              <FontAwesomeIcon icon={faPlay} fixedWidth /> Estimate difficulty
            </Button>
          </Modal.Footer>
        </Modal>
        <Modal
          show={showDifficultyModal}
          onHide={() => setShowDifficultyModal(false)}
          centered
        >
          <Modal.Header closeButton>
            <Modal.Title>Generate settings</Modal.Title>
          </Modal.Header>
          <Modal.Body>
            <Form.Group className="mt-1">
              <Form.Label>Number of questions to generate</Form.Label>
              <Form.Control
                type="number"
                value={questionsToGenerate}
                onChange={(e) => setQuestionsToGenerate(e.target.value)}
              />
              <Form.Text muted>
                Total new exercises per Generate click, evenly spread across
                different lesson steps as templates (one generation call per
                template, then one difficulty calibration).
              </Form.Text>
            </Form.Group>
            <Form.Check
              className="mt-3"
              type="switch"
              id="use-original-questions-as-templates-switch"
              label="Use original questions as templates"
              checked={useOriginalQuestionsAsTemplates}
              onChange={(e) =>
                setUseOriginalQuestionsAsTemplates(e.target.checked)
              }
            />
            <Form.Text muted className="d-block">
              Questions imported from the spreadsheet, before any AI
              generation.
            </Form.Text>
            <Form.Check
              className="mt-3"
              type="switch"
              id="use-ai-generated-questions-as-templates-switch"
              label="Use previously generated AI questions as templates"
              checked={useAiGeneratedQuestionsAsTemplates}
              onChange={(e) =>
                setUseAiGeneratedQuestionsAsTemplates(e.target.checked)
              }
            />
            <Form.Text muted className="d-block">
              Questions this session already generated (names like
              …_b1_e1). If both toggles are off, generation uses only the
              prompt with no example questions.
            </Form.Text>
          </Modal.Body>
          <Modal.Footer>
            <Button
              variant="secondary"
              onClick={() => setShowDifficultyModal(false)}
            >
              Done
            </Button>
            <Button
              variant="primary"
              onClick={handleGenerateFromModal}
              disabled={progressBusy}
            >
              <FontAwesomeIcon icon={faPlay} fixedWidth /> Generate
            </Button>
          </Modal.Footer>
        </Modal>
              <Dropdown>
                <Dropdown.Toggle variant="light" id="scratchpad-dropdown ">
                  <FontAwesomeIcon icon={faEllipsisV} fixedWidth />
                </Dropdown.Toggle>

                <Dropdown.Menu>
                  <Dropdown.Item
                    onClick={() => {
                      setShowModelModal(true);
                    }}
                  >
                    Select LLM model ({model || "gpt-4o"})
                  </Dropdown.Item>
                  <Dropdown.Divider />
                  <Dropdown.Item
                    onClick={() => {
                      setShowLLMOutput((prev) => !prev);
                    }}
                  >
                    {showLLMOutput ? "Hide" : "Inspect"} LLM Output
                  </Dropdown.Item>
                  <Dropdown.Item
                    onClick={() => {
                      setShowExportPanel((prev) => !prev);
                    }}
                  >
                    Export Prompt Output
                  </Dropdown.Item>
                </Dropdown.Menu>
              </Dropdown>
              <Modal
                show={showModelModal}
                onHide={() => setShowModelModal(false)}
                centered
              >
                <Modal.Header closeButton>
                  <Modal.Title>Select LLM model</Modal.Title>
                </Modal.Header>
                <Modal.Body>
                  <Form.Group>
                    <Form.Label>Current model</Form.Label>
                    <Form.Select
                      aria-label="Select LLM model"
                      value={model || "gpt-4o"}
                      disabled={modelsLoading}
                      onChange={(e) => {
                        if (typeof setModel === "function") {
                          setModel(e.target.value);
                        }
                      }}
                    >
                      {availableModels.map((entry) => (
                        <option key={entry.id} value={entry.id}>
                          {entry.id}
                        </option>
                      ))}
                    </Form.Select>
                    <Form.Text muted className="d-block mt-2">
                      Live list from OpenAI, filtered to vision-capable chat
                      models. Default is gpt-4o.
                    </Form.Text>
                    {modelsLoading ? (
                      <Form.Text muted className="d-block mt-1">
                        Loading models…
                      </Form.Text>
                    ) : null}
                    {modelsError ? (
                      <Alert variant="warning" className="mt-2 mb-0 py-2">
                        Could not refresh model list ({modelsError}). Showing
                        the current selection only.
                      </Alert>
                    ) : null}
                  </Form.Group>
                </Modal.Body>
                <Modal.Footer>
                  <Button
                    variant="secondary"
                    onClick={() => setShowModelModal(false)}
                  >
                    Done
                  </Button>
                </Modal.Footer>
              </Modal>
            </div>
          </div>
        </Card.Header>
        <Card.Body className="prompt-writing-card-body p-0">
          {gptProgressVisible ? (
            <LinearProgress
              variant={gptProgress > 0 ? "determinate" : "indeterminate"}
              value={gptProgress}
              color={gptProgress == 100 ? "success" : "primary"}
            />
          ) : (
            <></>
          )}
          <PanelGroup autoSaveId="oatutor-prompt-panels" direction="horizontal">
            <Panel>
              <Card className="panel-card rounded-0">
                <Card.Body className="panel-card-body p-0">
                  {showSystemMessages ? (
                    <>
                      <p className="text-center divider mt-2">
                        <small>System Messages</small>
                      </p>
                      <Alert variant={"danger"} className="m-2">
                        Take caution when modifying system messages. Incorrect
                        changes could lead to unintended effects and prevent
                        hints from being properly formatted.
                      </Alert>
                      <ListGroup>
                        <Reorder.Group
                          axis="y"
                          values={systemMessages}
                          onReorder={setSystemMessages}
                          className="reorder-group"
                          layoutScroll
                        >
                          {systemMessages.map((message) => (
                            <Reorder.Item
                              key={`item_${message.id}`}
                              value={message}
                              dragListener={messageDrag}
                              className="reorder-item"
                            >
                              <ListGroupItem
                                key={`listgroupitem_${message.id}`}
                                className="rounded-3 m-2 p-0"
                              >
                                <div className="d-flex">
                                  <div className="d-flex flex-column content-panel p-0">
                                    {/* <Form.Select
                                  key={`role_${message.id}`}
                                  className="rounded-bottom-0 border border-light"
                                  size="sm"
                                  value={message.role}
                                  aria-label="Select user role"
                                  onChange={(e) => {
                                    updateSystemMessage(
                                      message.id,
                                      e.target.value,
                                      message.content
                                    );
                                  }}
                                >
                                  {Object.keys(roles).map((key) => (
                                    <option key={key} value={roles[key]}>
                                      {roles[key]}
                                    </option>
                                  ))}
                                </Form.Select> */}
                                    <ReactTextareaAutosize
                                      maxRows={10}
                                      className="p-2 m-0 rounded-2 rounded-top-0 border border-light"
                                      style={{
                                        resize: "none",
                                        width: "100%",
                                      }}
                                      disabled={message.disabled}
                                      value={message.content}
                                      placeholder="Enter message..."
                                      minRows={4}
                                      onChange={(e) =>
                                        updateSystemMessage(
                                          message.id,
                                          message.role,
                                          e.target.value
                                        )
                                      }
                                    />
                                  </div>
                                  <div className="d-flex flex-column justify-content-between side-panel">
                                    <Button
                                      disabled={message.disabled}
                                      className="p-0"
                                      size="sm"
                                      variant="light"
                                    >
                                      <div
                                        className="reorder-handle text-end"
                                        onPointerEnter={(e) => {
                                          setMessageDrag(true);
                                        }}
                                        onPointerLeave={(e) => {
                                          setMessageDrag(false);
                                        }}
                                      >
                                        <FontAwesomeIcon icon={faGrip} />
                                      </div>
                                    </Button>

                                    <ButtonGroup vertical>
                                      <Button
                                        size="sm"
                                        variant="light"
                                        onClick={() => {
                                          toggleSystemMessage(message.id);
                                        }}
                                      >
                                        {message.disabled ? (
                                          <FontAwesomeIcon icon={faPencil} />
                                        ) : (
                                          <FontAwesomeIcon icon={faCheck} />
                                        )}
                                      </Button>
                                      <Button
                                        size="sm"
                                        disabled={systemMessages.length === 1}
                                        variant="light"
                                        onClick={() => {
                                          deleteMessage(message.id);
                                        }}
                                      >
                                        <FontAwesomeIcon icon={faTrashAlt} />
                                      </Button>
                                    </ButtonGroup>
                                  </div>
                                </div>

                                <div ref={messagesEndRef} />
                              </ListGroupItem>
                            </Reorder.Item>
                          ))}
                        </Reorder.Group>
                      </ListGroup>
                      <div className="d-grid gap-2">
                        <Button
                          className="m-1"
                          size="sm"
                          variant="light"
                          onClick={() => {
                            createMessage("system", "");
                          }}
                        >
                          Add System Message <FontAwesomeIcon icon={faPlus} />
                        </Button>
                      </div>
                      <p className="text-center divider mt-2">
                        <small>User Messages</small>
                      </p>
                      <ListGroup>
                        <Reorder.Group
                          axis="y"
                          values={userMessages}
                          onReorder={setUserMessages}
                          className="reorder-group"
                          layoutScroll
                        >
                          {userMessages.map((message) => (
                            <Reorder.Item
                              key={`item_${message.id}`}
                              value={message}
                              dragListener={messageDrag}
                              className="reorder-item"
                            >
                              <ListGroupItem
                                key={`listgroupitem_${message.id}`}
                                className="rounded-3 m-2 p-0"
                              >
                                <div className="d-flex">
                                  <div className="d-flex flex-column content-panel p-0">
                                    {/* <Form.Select
                                  key={`role_${message.id}`}
                                  className="rounded-bottom-0 border border-light"
                                  size="sm"
                                  value={message.role}
                                  aria-label="Select user role"
                                  onChange={(e) => {
                                    updateUserMessage(
                                      message.id,
                                      e.target.value,
                                      message.content
                                    );
                                  }}
                                >
                                  {Object.keys(roles).map((key) => (
                                    <option key={key} value={roles[key]}>
                                      {roles[key]}
                                    </option>
                                  ))}
                                </Form.Select> */}
                                    <ReactTextareaAutosize
                                      maxRows={10}
                                      className="p-2 m-0 rounded-2 rounded-top-0 border border-light"
                                      style={{ resize: "none", width: "100%" }}
                                      value={message.content}
                                      placeholder="Enter message..."
                                      minRows={3}
                                      onChange={(e) =>
                                        updateUserMessage(
                                          message.id,
                                          message.role,
                                          e.target.value
                                        )
                                      }
                                    />
                                  </div>
                                  <div className="d-flex flex-column justify-content-between side-panel">
                                    <div
                                      className="reorder-handle text-end"
                                      onPointerEnter={(e) => {
                                        setMessageDrag(true);
                                      }}
                                      onPointerLeave={(e) => {
                                        setMessageDrag(false);
                                      }}
                                    >
                                      <FontAwesomeIcon icon={faGrip} />
                                    </div>
                                    <Button
                                      size="sm"
                                      disabled={userMessages.length === 1}
                                      variant="light"
                                      className="m-1"
                                      onClick={() => {
                                        deleteMessage(message.id);
                                      }}
                                    >
                                      <FontAwesomeIcon icon={faTrashAlt} />
                                    </Button>
                                  </div>
                                </div>

                                <div ref={messagesEndRef} />
                              </ListGroupItem>
                            </Reorder.Item>
                          ))}
                        </Reorder.Group>{" "}
                      </ListGroup>
                      <div className="d-grid gap-2">
                        <Button
                          className="m-1"
                          size="sm"
                          variant="light"
                          onClick={() => {
                            createMessage("user", "");
                          }}
                        >
                          Add User Message <FontAwesomeIcon icon={faPlus} />
                        </Button>
                      </div>
                    </>
                  ) : (
                    <>
                      {userMessages.length > 0 ? (
                        <>
                          <Form
                            style={{
                              width: "100%",
                              height: "100%",
                            }}
                          >
                            <Form.Control
                              className="rounded-0"
                              as="textarea"
                              value={userMessages[0].content}
                              onChange={(e) =>
                                updateUserMessage(
                                  userMessages[0].id,
                                  userMessages[0].role,
                                  e.target.value
                                )
                              }
                              placeholder={`For generating hints and scaffolds, enter a ${
                                libraryTabKey == "textbook"
                                  ? "textbook-level"
                                  : "lesson-level"
                              } prompt...\n\nFor generating questions, enter an example question(s) and a ${
                                libraryTabKey == "textbook"
                                  ? "textbook-level"
                                  : "lesson-level"
                              } prompt...`}
                              style={{
                                width: "100%",
                                height: "100%",
                                resize: "none",
                              }}
                            />
                          </Form>
                        </>
                      ) : (
                        <></>
                      )}
                    </>
                  )}
                </Card.Body>
              </Card>
            </Panel>
            <PanelResizeHandle />
            <Panel>
              {showLLMOutput ? (
                <>
                  <Card className="panel-card rounded-0">
                    <Card.Header>
                      <div className="d-flex align-items-center justify-content-between">
                        <div>
                          <strong className="me-2">LLM OUTPUT</strong>
                          <Badge>{model}</Badge>
                        </div>
                        <Button
                          size="sm"
                          className="p-0"
                          variant="light"
                          onClick={() => {
                            setGptResult({});
                            setShowLLMOutput(false);
                          }}
                        >
                          <FontAwesomeIcon icon={faXmark} fixedWidth />
                        </Button>
                      </div>
                    </Card.Header>
                    <Card.Body className="panel-card-body">
                      {Object.entries(gptResult).length > 0 ? (
                        <>
                          {Object.entries(gptResult).map(
                            ([key, value], index) => (
                              <Accordion key={index}>
                                <AccordionSummary
                                  expandIcon={<ExpandMoreIcon />}
                                >
                                  <Typography
                                    sx={{ width: "60%", flexShrink: 0 }}
                                  >
                                    {key}
                                  </Typography>
                                  <Typography sx={{ color: "text.secondary" }}>
                                    {isJsonString(value) ? (
                                      <Chip
                                        icon={<CheckIcon />}
                                        label="Valid JSON"
                                        variant="outlined"
                                        size="small"
                                      />
                                    ) : gptLoading[key] ? (
                                      <FontAwesomeIcon icon={faSpinner} spin />
                                    ) : (
                                      <Chip
                                        icon={<ClearIcon />}
                                        label="Invalid JSON"
                                        variant="outlined"
                                        size="small"
                                      />
                                    )}
                                  </Typography>
                                </AccordionSummary>
                                <AccordionDetails>
                                  {renderValue(value, key)}
                                </AccordionDetails>
                              </Accordion>
                            )
                          )}
                        </>
                      ) : (
                        <>
                          <h1 className="display-1 text-muted text-center mt-3">
                            <FontAwesomeIcon icon={faPlay} />
                          </h1>
                          <p className="lead text-center p-2">
                            Run the prompt to inspect its output.
                          </p>
                        </>
                      )}
                    </Card.Body>
                  </Card>
                </>
              ) : (
                <></>
              )}
            </Panel>
            <PanelResizeHandle />
            <Panel>
              {showExportPanel ? (
                <>
                  <Card className="panel-card rounded-0">
                    <Card.Header>
                      <div className="d-flex align-items-center justify-content-between">
                        <div>
                          <strong className="me-2">EXPORT WIZARD</strong>
                        </div>
                        <Button
                          size="sm"
                          className="p-0"
                          variant="light"
                          onClick={() => {
                            setShowExportPanel(false);
                          }}
                        >
                          <FontAwesomeIcon icon={faXmark} fixedWidth />
                        </Button>
                      </div>
                    </Card.Header>
                    <Card.Body className="panel-card-body">
                    <HintExporter
                      lessonContentAttributes={lessonContentAttributes}
                      columnIndexMap={columnIndexMap}
                      sheetInfo={sheetInfo}
                      showHumanHints={showHumanHints}
                    />

                    </Card.Body>
                  </Card>
                </>
              ) : (
                <></>
              )}
            </Panel>
          </PanelGroup>
        </Card.Body>
      </Card>
    </>
  );
}




