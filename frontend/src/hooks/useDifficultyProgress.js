import { useCallback, useEffect, useRef, useState } from "react";

export const GENERATION_PROGRESS_CAP = 50;
/** Real API-call progress maps up to this %; hold here during Rasch/computing. */
export const SIMULATE_PROGRESS_CAP = 99;
export const COMPUTING_PROGRESS_PERCENT = 99;

/** @deprecated Use SIMULATE_PROGRESS_CAP; kept for any external imports. */
export const ESTIMATE_PROGRESS_CAP = SIMULATE_PROGRESS_CAP;

export const DIFFICULTY_PROGRESS_MESSAGES = {
  generating: "Generating questions…",
  simulating: "Simulating students…",
  computing: "Computing difficulty…",
  done: "Done",
};

function prefersReducedMotion() {
  if (typeof window === "undefined") {
    return false;
  }
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function formatElapsed(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return `${m}:${String(rem).padStart(2, "0")}`;
}

function computingMessage(elapsedSec) {
  return `${DIFFICULTY_PROGRESS_MESSAGES.computing} ${formatElapsed(elapsedSec)}`;
}

export function useDifficultyProgress() {
  const [state, setState] = useState({
    visible: false,
    fading: false,
    percent: 0,
    message: "",
    tone: "primary",
  });

  const hideTimerRef = useRef(null);
  const computingTimerRef = useRef(null);
  const computingStartedAtRef = useRef(null);
  const percentRef = useRef(0);
  const templateCountRef = useRef(1);
  const simulateBaseRef = useRef(GENERATION_PROGRESS_CAP);

  const clearComputingTimer = useCallback(() => {
    if (computingTimerRef.current) {
      clearInterval(computingTimerRef.current);
      computingTimerRef.current = null;
    }
    computingStartedAtRef.current = null;
  }, []);

  const clearTimers = useCallback(() => {
    if (hideTimerRef.current) {
      clearTimeout(hideTimerRef.current);
      hideTimerRef.current = null;
    }
    clearComputingTimer();
  }, [clearComputingTimer]);

  useEffect(() => () => clearTimers(), [clearTimers]);

  const setPercent = useCallback((next) => {
    const clamped = Math.max(0, Math.min(100, next));
    percentRef.current = clamped;
    setState((prev) => ({ ...prev, percent: clamped }));
  }, []);

  const startComputingHold = useCallback(() => {
    clearComputingTimer();
    setPercent(COMPUTING_PROGRESS_PERCENT);
    const startedAt = Date.now();
    computingStartedAtRef.current = startedAt;
    const tick = () => {
      const elapsedSec = (Date.now() - startedAt) / 1000;
      setState((prev) => ({
        ...prev,
        visible: true,
        fading: false,
        percent: COMPUTING_PROGRESS_PERCENT,
        message: computingMessage(elapsedSec),
        tone: "primary",
      }));
    };
    tick();
    computingTimerRef.current = setInterval(tick, 1000);
  }, [clearComputingTimer, setPercent]);

  const scheduleHide = useCallback((delayMs = 600) => {
    const fadeMs = prefersReducedMotion() ? 0 : 400;
    hideTimerRef.current = setTimeout(() => {
      setState((prev) => ({ ...prev, fading: true }));
      hideTimerRef.current = setTimeout(() => {
        setState({
          visible: false,
          fading: false,
          percent: 0,
          message: "",
          tone: "primary",
        });
        percentRef.current = 0;
      }, fadeMs);
    }, delayMs);
  }, []);

  const startGenerate = useCallback(
    ({ templateCount = 1 } = {}) => {
      clearTimers();
      templateCountRef.current = Math.max(1, Number(templateCount) || 1);
      simulateBaseRef.current = GENERATION_PROGRESS_CAP;
      percentRef.current = 0;
      setState({
        visible: true,
        fading: false,
        percent: 0,
        message: DIFFICULTY_PROGRESS_MESSAGES.generating,
        tone: "primary",
      });
    },
    [clearTimers]
  );

  const onTemplateComplete = useCallback(
    (done, total) => {
      const count = Math.max(1, Number(total) || templateCountRef.current || 1);
      const completed = Math.max(0, Math.min(Number(done) || 0, count));
      const pct = (completed / count) * GENERATION_PROGRESS_CAP;
      setPercent(pct);
      setState((prev) => ({
        ...prev,
        visible: true,
        fading: false,
        message: DIFFICULTY_PROGRESS_MESSAGES.generating,
        tone: "primary",
      }));
    },
    [setPercent]
  );

  const startSimulating = useCallback(() => {
    clearTimers();
    const from = Math.max(percentRef.current, GENERATION_PROGRESS_CAP);
    simulateBaseRef.current = from;
    setPercent(from);
    setState((prev) => ({
      ...prev,
      visible: true,
      fading: false,
      message: DIFFICULTY_PROGRESS_MESSAGES.simulating,
      tone: "primary",
    }));
  }, [clearTimers, setPercent]);

  /** Real simulate progress from SSE: percent tracks completed API calls only. */
  const onSimulateProgress = useCallback(
    ({ done, total, message, phase } = {}) => {
      const t = Math.max(0, Number(total) || 0);
      const d = Math.max(0, Math.min(Number(done) || 0, t || Number(done) || 0));
      if (phase === "computing") {
        startComputingHold();
        return;
      }
      clearComputingTimer();
      const base = simulateBaseRef.current;
      const span = SIMULATE_PROGRESS_CAP - base;
      const pct = t > 0 ? base + span * (d / t) : base;
      setPercent(pct);
      const label =
        message ||
        (t > 0
          ? `Simulating respondents ${d}/${t}`
          : DIFFICULTY_PROGRESS_MESSAGES.simulating);
      setState((prev) => ({
        ...prev,
        visible: true,
        fading: false,
        message: label,
        tone: "primary",
      }));
    },
    [clearComputingTimer, setPercent, startComputingHold]
  );

  const startProcessing = useCallback(() => {
    clearTimers();
    startComputingHold();
  }, [clearTimers, startComputingHold]);

  const startScore = useCallback(() => {
    clearTimers();
    simulateBaseRef.current = 0;
    percentRef.current = 0;
    setState({
      visible: true,
      fading: false,
      percent: 0,
      message: DIFFICULTY_PROGRESS_MESSAGES.simulating,
      tone: "primary",
    });
  }, [clearTimers]);

  const complete = useCallback(() => {
    clearTimers();
    percentRef.current = 100;
    setState((prev) => ({
      ...prev,
      visible: true,
      fading: false,
      percent: 100,
      message: DIFFICULTY_PROGRESS_MESSAGES.done,
      tone: "success",
    }));
    scheduleHide(prefersReducedMotion() ? 250 : 750);
  }, [clearTimers, scheduleHide]);

  const fail = useCallback(
    (errorMessage) => {
      clearTimers();
      // Keep current percent (never jump to 100 on failure).
      setState((prev) => ({
        ...prev,
        visible: true,
        fading: false,
        message: errorMessage || "Scoring failed",
        tone: "error",
      }));
      scheduleHide(prefersReducedMotion() ? 800 : 1500);
    },
    [clearTimers, scheduleHide]
  );

  const isBusy = state.visible;

  return {
    state,
    isBusy,
    startGenerate,
    onTemplateComplete,
    startScore,
    startSimulating,
    onSimulateProgress,
    startProcessing,
    complete,
    fail,
  };
}
