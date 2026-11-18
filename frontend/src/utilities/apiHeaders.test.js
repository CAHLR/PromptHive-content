import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import {
  apiHeaders,
  clearAccessCode,
  getAccessCode,
  setAccessCode,
} from "./apiHeaders.js";

const memory = new Map();

beforeEach(() => {
  memory.clear();
  globalThis.localStorage = {
    getItem(key) {
      return memory.has(key) ? memory.get(key) : null;
    },
    setItem(key, value) {
      memory.set(key, String(value));
    },
    removeItem(key) {
      memory.delete(key);
    },
  };
});

test("apiHeaders includes ngrok header and omits access code when unset", () => {
  clearAccessCode();
  assert.equal(getAccessCode(), "");
  assert.deepEqual(apiHeaders(), {
    "ngrok-skip-browser-warning": "true",
  });
});

test("apiHeaders includes X-PromptHive-Access-Code when a code is stored", () => {
  setAccessCode("unit-test-code");
  assert.equal(getAccessCode(), "unit-test-code");
  assert.deepEqual(apiHeaders(), {
    "ngrok-skip-browser-warning": "true",
    "X-PromptHive-Access-Code": "unit-test-code",
  });
});
