import { fetchEventSource } from "@microsoft/fetch-event-source";
import apiClient from "./abscribeAPI";
import { API_BASE_URL } from "../config";
import {
  apiHeaders,
  handleAccessCodeUnauthorized,
} from "../utilities/apiHeaders";

/** Long enough for multi-lesson calibrate with include-existing (15+ min runs observed). */
export const DIFFICULTY_REQUEST_TIMEOUT_MS = 1_800_000;

export const BROWSER_TIMEOUT_MESSAGE =
  "This run timed out in the browser. Scoring may still be finishing on the server. Wait a few minutes before starting another Generate.";

function assertCalibrationResponse(data, endpointLabel) {
  if (typeof data === "string") {
    throw new Error(
      `${endpointLabel} returned malformed JSON (could not parse difficulty metadata).`
    );
  }
  if (!data || typeof data !== "object") {
    throw new Error(
      `${endpointLabel} returned an unexpected empty or invalid response.`
    );
  }
  if (!Array.isArray(data.questions)) {
    throw new Error(
      `${endpointLabel} response is missing a questions array: calibration may have failed silently.`
    );
  }
  return data;
}

/** True when axios aborted due to client timeout (not a server 5xx). */
export function isBrowserTimeoutError(error) {
  if (!error) {
    return false;
  }
  if (error.isBrowserTimeout) {
    return true;
  }
  if (error.code === "ECONNABORTED" || error.name === "AbortError") {
    return true;
  }
  const msg = String(error.message || "").toLowerCase();
  return msg.includes("timeout") && !error.response;
}

function wrapDifficultyRequestError(error) {
  if (isBrowserTimeoutError(error)) {
    const wrapped = new Error(BROWSER_TIMEOUT_MESSAGE);
    wrapped.cause = error;
    wrapped.isBrowserTimeout = true;
    throw wrapped;
  }
  throw error;
}

/**
 * POST with Accept: text/event-stream. Resolves with the final ``result.payload``.
 * ``onProgress({ phase, done, total, message })`` is called for progress events.
 */
async function postDifficultySse(path, body, { onProgress } = {}) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => {
    controller.abort();
  }, DIFFICULTY_REQUEST_TIMEOUT_MS);

  let settled = false;
  let resultPayload = null;
  let streamError = null;

  try {
    await fetchEventSource(`${API_BASE_URL}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        ...apiHeaders(),
      },
      body: JSON.stringify(body),
      signal: controller.signal,
      openWhenHidden: true,
      async onopen(response) {
        if (response.status === 401) {
          handleAccessCodeUnauthorized();
          throw new Error(
            `Difficulty request failed (${response.status} ${response.statusText})`
          );
        }
        if (!response.ok) {
          throw new Error(
            `Difficulty request failed (${response.status} ${response.statusText})`
          );
        }
        const ct = (response.headers.get("content-type") || "").toLowerCase();
        if (!ct.includes("text/event-stream")) {
          throw new Error(
            "Expected text/event-stream from difficulty endpoint but got a different content type."
          );
        }
      },
      onmessage(message) {
        if (!message.data) {
          return;
        }
        let event;
        try {
          event = JSON.parse(message.data);
        } catch {
          return;
        }
        if (event.type === "progress") {
          onProgress?.(event);
          return;
        }
        if (event.type === "result") {
          resultPayload = event.payload;
          settled = true;
          controller.abort();
          return;
        }
        if (event.type === "error") {
          streamError = new Error(event.error || "Difficulty scoring failed");
          settled = true;
          controller.abort();
        }
      },
      onerror(err) {
        if (settled) {
          throw err;
        }
        streamError = err instanceof Error ? err : new Error(String(err));
        throw streamError;
      },
      onclose() {
        // no-op; resolved below
      },
    });
  } catch (error) {
    if (!settled) {
      if (controller.signal.aborted && !streamError && !resultPayload) {
        const wrapped = new Error(BROWSER_TIMEOUT_MESSAGE);
        wrapped.isBrowserTimeout = true;
        wrapped.cause = error;
        throw wrapped;
      }
      wrapDifficultyRequestError(streamError || error);
    }
  } finally {
    clearTimeout(timeoutId);
  }

  if (streamError) {
    throw streamError;
  }
  if (resultPayload == null) {
    throw new Error("Difficulty stream ended without a result payload.");
  }
  return resultPayload;
}

/**
 * Feature 2: score EXISTING sheet questions on the same difficulty scale as the
 * AI-generated ones. `questions` must be OATutor row objects ("Row Type",
 * "Title", "Body Text", "Answer", "answerType", "Problem Name", "mcChoices") in the SAME
 * order as the caller's rows, so each returned `index` maps back to its row.
 *
 * Returns { scores: [{ index, difficultyLogit, difficultyLabel, scored, reason }],
 *           calibrationStatus, mode, nRespondents }.
 */
export async function scoreExistingQuestions(questions, options = {}) {
  const body = { questions };
  if (options.nRespondents != null && options.nRespondents !== "") {
    body.nRespondents = Number(options.nRespondents);
  }
  attachLoggingContext(body, options);
  try {
    return await postDifficultySse("/chatGPT/score-existing-questions", body, {
      onProgress: options.onProgress,
    });
  } catch (error) {
    wrapDifficultyRequestError(error);
  }
}

/**
 * Feature 1 Option B: one Rasch + difficulty-filter pass on already-generated
 * OATutor rows (after T per-template generation calls with skipCalibration).
 *
 * Pass `includeExisting: true` and `scoreSaturatedViaAnchors: true` in
 * difficultyFilter when existing sheet rows are merged into the same list.
 */
export async function calibrateGeneratedQuestions(
  questions,
  difficultyFilter,
  options = {}
) {
  const body = { questions, difficultyFilter };
  attachLoggingContext(body, options);
  try {
    const payload = await postDifficultySse(
      "/chatGPT/calibrate-generated-questions",
      body,
      { onProgress: options.onProgress }
    );
    return assertCalibrationResponse(
      payload,
      "Calibration (calibrate-generated-questions)"
    );
  } catch (error) {
    wrapDifficultyRequestError(error);
  }
}

/** Escape hatch: one-shot JSON (Accept: application/json), no progress events. */
export async function calibrateGeneratedQuestionsJson(
  questions,
  difficultyFilter,
  options = {}
) {
  const body = { questions, difficultyFilter };
  attachLoggingContext(body, options);
  try {
    const response = await apiClient.post(
      "/chatGPT/calibrate-generated-questions",
      body,
      {
        timeout: DIFFICULTY_REQUEST_TIMEOUT_MS,
        headers: { Accept: "application/json" },
      }
    );
    return assertCalibrationResponse(
      response.data,
      "Calibration (calibrate-generated-questions)"
    );
  } catch (error) {
    wrapDifficultyRequestError(error);
  }
}

/**
 * Copy optional PromptHive event-logging context fields from ``options`` into
 * the outgoing request body. Fields that are undefined are skipped so the
 * backend sees ``null``/absent, not ``"undefined"``. Called by scoreExisting,
 * calibrateGenerated, and calibrateGeneratedQuestionsJson.
 */
function attachLoggingContext(body, options) {
  if (!body || !options) return;
  const fields = [
    "generationRunId",
    "userId",
    "userName",
    "spreadsheetId",
    "lessonName",
    "promptId",
    "useOriginalItemsAsTemplates",
    "useGeneratedItemsAsTemplates",
    "batchNumber",
    "clientTimestamp",
    "userPromptHash",
    "scratchpadId",
  ];
  for (const name of fields) {
    if (options[name] !== undefined) {
      body[name] = options[name];
    }
  }
}
