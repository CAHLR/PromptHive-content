const ACCESS_CODE_STORAGE_KEY = "prompthive_access_code";
export const ACCESS_CODE_REQUIRED_EVENT = "prompthive:access-code-required";

export function getAccessCode() {
  try {
    return localStorage.getItem(ACCESS_CODE_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

export function setAccessCode(code) {
  try {
    localStorage.setItem(ACCESS_CODE_STORAGE_KEY, code ?? "");
  } catch {
    // ignore quota / private-mode errors
  }
}

export function clearAccessCode() {
  try {
    localStorage.removeItem(ACCESS_CODE_STORAGE_KEY);
  } catch {
    // ignore
  }
}

export function apiHeaders() {
  const headers = {
    "ngrok-skip-browser-warning": "true",
  };
  const code = getAccessCode();
  if (code) {
    headers["X-PromptHive-Access-Code"] = code;
  }
  return headers;
}

/** Clear stored code and ask the access-code dialog to show again. */
export function handleAccessCodeUnauthorized(
  message = "Access code incorrect"
) {
  clearAccessCode();
  if (typeof window !== "undefined" && typeof window.dispatchEvent === "function") {
    window.dispatchEvent(
      new CustomEvent(ACCESS_CODE_REQUIRED_EVENT, { detail: { message } })
    );
  }
}
