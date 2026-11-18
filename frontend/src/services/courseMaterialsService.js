const base = (import.meta.env.VITE_COURSE_MATERIALS_API_BASE_URL || "").replace(/\/$/, "");
const endpoint = `${base}/api/course-materials`;

async function request(path, { method = "GET", body, signal } = {}) {
  let response;
  try {
    response = await fetch(`${endpoint}${path}`, {
      method, credentials: "include", signal,
      headers: { "Content-Type": "application/json", "X-Course-Materials-Request": "1" },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new Error("Course Materials backend is disconnected. Check the server connection and try again.");
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) { const error = new Error(data?.error || `Course Materials request failed (${response.status}).`); error.status = response.status; throw error; }
  if (!data || typeof data !== "object") throw new Error("Course Materials returned an invalid response. Check the backend URL.");
  return data;
}

export const reviewAssetUrl = (id, path) => `${endpoint}/materials/${encodeURIComponent(id)}/assets/${path.split("/").map(encodeURIComponent).join("/")}`;
export const getCatalog = (signal) => request("/catalog", { signal });
export const listMaterials = (signal, includeDeleted = false) => request(`/materials${includeDeleted ? "?includeDeleted=1" : ""}`, { signal });
export const getMaterial = (id, signal) => request(`/materials/${id}`, { signal });
export const getReview = (id, signal, {version, section = 0, cursor = 0} = {}) => request(`/materials/${id}/review?${new URLSearchParams({section, cursor, ...(version ? {version} : {})})}`, { signal });
export const reviewDownloadUrl = (id, version) => `${endpoint}/materials/${encodeURIComponent(id)}/review/download?${new URLSearchParams({version})}`;
export const getReviewNotes = (id, version, cursor = 0, signal) => request(`/materials/${id}/review/notes?${new URLSearchParams({version, cursor})}`, {signal});
export const reviewAgainMaterial = (id) => request(`/materials/${id}/review-again`, {method: "POST", body: {}});
export const reconcileMaterial = (id) => request(`/materials/${id}/reconcile`, {method: "POST", body: {}});
export const retryMaterial = (id) => request(`/materials/${id}/retry`, { method: "POST", body: {} });
export const processMaterial = (id) => request(`/materials/${id}/process`, { method: "POST", body: {} });
export const completeUpload = (id, signal) => request(`/materials/${id}/complete`, { method: "POST", body: {}, signal });
export const cancelMaterial = (id) => request(`/materials/${id}/cancel`, { method: "POST", body: {} });
export const deleteMaterial = (id) => request(`/materials/${id}`, { method: "DELETE" });
export const updateAssignments = (id, lessonIds) => request(`/materials/${id}/assignments`, { method: "PUT", body: { lessonIds } });
export const publishMaterial = (id, review) => request(`/materials/${id}/publish`, { method: "POST", body: {
  reviewed: true, reviewVersion: review.version, compiledHash: review.compiledHash, approvalFingerprint: review.approvalFingerprint,
  baseManifestHash: review.baseManifestHash, baseCoursePlansHash: review.baseCoursePlansHash,
} });
export const signIn = (email, password) => request("/session", { method: "POST", body: { email, password } });
export const signOut = () => request("/session", { method: "DELETE" });

export async function uploadMaterial(file, metadata, onStage, { signal } = {}) {
  const { material, upload } = await request("/materials", {
    method: "POST", signal, body: { ...metadata, filename: file.name, fileSize: file.size, contentType: "application/pdf" },
  });
  onStage?.(material);
  try {
    let response;
    if (upload.method === "POST") {
      const form = new FormData();
      Object.entries(upload.fields).forEach(([key, value]) => form.append(key, value));
      form.append("file", file); // S3 requires the file field last.
      response = await fetch(upload.url, { method: "POST", body: form, credentials: "omit", signal });
    } else {
      response = await fetch(`${base}${upload.url}`, { method: "PUT", body: file, credentials: "include", signal,
        headers: { "Content-Type": "application/pdf", "X-Course-Materials-Request": "1" } });
    }
    if (!response.ok) {
      const body = upload.method === "PUT" ? await response.json().catch(() => null) : null;
      throw new Error(body?.error || `PDF transfer failed (${response.status}).`);
    }
    const completed = await completeUpload(material.id, signal);
    onStage?.(completed.material);
    return completed;
  } catch (error) {
    error.materialId = material.id;
    throw error;
  }
}
