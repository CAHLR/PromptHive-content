import React, { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Form, Modal, Spinner } from "react-bootstrap";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faArrowUpFromBracket, faCheck, faCloudArrowUp, faFilePdf, faRotateRight, faShieldHalved, faXmark, faPlus, faTrashCan, faMagnifyingGlass } from "@fortawesome/free-solid-svg-icons";
import NavHeader from "../Header/NavHeader";
import { completeUpload, getCatalog, listMaterials, processMaterial, retryMaterial, uploadMaterial, signOut, cancelMaterial, deleteMaterial } from "../../services/courseMaterialsService";
import { formatFileSize, STATUS_LABELS, validateFile, validateForm } from "./validation";
import InstructorSignIn from "./InstructorSignIn";
import LessonPicker from "./LessonPicker";
import MaterialReview from "./MaterialReview";
import "../../scss/courseMaterials.scss";

const INITIAL_FORM = { title: "", author: "", documentType: "textbook", courseId: "", lessonIds: [], visibility: "private_tutor" };
const ACTIVE = new Set(["uploading", "queued", "extracting", "compiling", "validating", "publish_queued", "publishing"]);

export default function CourseMaterials() {
  const [catalog, setCatalog] = useState(null);
  const [materials, setMaterials] = useState([]);
  const [caps, setCaps] = useState(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [needsSignIn, setNeedsSignIn] = useState(false);
  const [pollError, setPollError] = useState("");
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [file, setFile] = useState(null);
  const [form, setForm] = useState(INITIAL_FORM);
  const [errors, setErrors] = useState({});
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [fileChecking, setFileChecking] = useState(false);
  const [busyId, setBusyId] = useState(null);
  const [reviewId, setReviewId] = useState(null);
  const [filter, setFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [showUpload, setShowUpload] = useState(false);
  const [confirmation, setConfirmation] = useState(null);
  const transfer = useRef(null);
  const transferId = useRef(null);
  const inputRef = useRef(null);
  const selectionRef = useRef(0);
  const mounted = useRef(true);
  const uploadLock = useRef(false);

  const refresh = useCallback(async (signal) => {
    let result;
    try { result = await listMaterials(signal); }
    catch (error) {
      if (error.status === 401 && mounted.current) { setCatalog(null); setMaterials([]); setCaps(null); setReviewId(null); setNeedsSignIn(true); setLoadError(error.message); setPollError(""); }
      throw error;
    }
    if (!signal?.aborted && mounted.current) {
      setMaterials(result.materials); setCaps(result.capabilities); setPollError("");
    }
  }, []);

  const connect = useCallback(async (signal) => {
    setLoading(true); setLoadError(""); setNeedsSignIn(false);
    try {
      const result = await getCatalog(signal);
      if (signal?.aborted) return;
      setCatalog(result); setCaps(result.capabilities);
      await refresh(signal);
    } catch (error) {
      if (error.name !== "AbortError" && mounted.current) { setLoadError(error.message); setNeedsSignIn(error.status === 401); if (error.status === 401) { setCatalog(null); setMaterials([]); setCaps(null); setReviewId(null); } }
    } finally { if (!signal?.aborted && mounted.current) setLoading(false); }
  }, [refresh]);

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    connect(controller.signal);
    return () => { mounted.current = false; controller.abort(); selectionRef.current += 1; };
  }, [connect]);

  useEffect(() => {
    if (!catalog || loadError) return undefined;
    const controller = new AbortController();
    let timer;
    let stopped = false;
    async function poll() {
      try { await refresh(controller.signal); }
      catch (error) { if (error.name !== "AbortError" && error.status !== 401 && !stopped) setPollError("Status updates are disconnected. Previously loaded statuses may be out of date. Reconnect to refresh them."); }
      if (!stopped) timer = setTimeout(poll, 5000);
    }
    timer = setTimeout(poll, 5000);
    return () => { stopped = true; clearTimeout(timer); controller.abort(); };
  }, [catalog, loadError, refresh]);

  function updateRecord(record) {
    if (!mounted.current) return;
    setMaterials((previous) => [record, ...previous.filter((item) => item.id !== record.id)]);
  }
  async function chooseFile(next) {
    const sequence = ++selectionRef.current;
    setFile(null); setNotice(""); setFileChecking(false);
    const error = validateFile(next, caps?.maxFileBytes);
    if (error) { setErrors((previous) => ({ ...previous, file: error })); return; }
    setFileChecking(true);
    try {
      const header = new TextDecoder().decode(await next.slice(0, 5).arrayBuffer());
      const tail = new TextDecoder().decode(await next.slice(-2048).arrayBuffer());
      if (sequence !== selectionRef.current) return;
      if (header !== "%PDF-" || !tail.includes("%%EOF")) throw new Error("This file is not a complete PDF. Choose a valid PDF.");
      setFile(next); setErrors((previous) => ({ ...previous, file: undefined }));
    } catch (error) { if (sequence === selectionRef.current) setErrors((previous) => ({ ...previous, file: error.message })); }
    finally { if (sequence === selectionRef.current) setFileChecking(false); }
  }
  function removeFile() {
    selectionRef.current += 1; setFile(null); setFileChecking(false);
    if (inputRef.current) inputRef.current.value = "";
    setErrors((previous) => ({ ...previous, file: undefined }));
  }
  function field(name, value) {
    setForm((previous) => ({ ...previous, [name]: value, ...(name === "courseId" ? { lessonIds: [], newCourse: value === "__new__" ? { courseName: "", lessons: [""] } : undefined } : {}) }));
    setErrors((previous) => ({ ...previous, [name]: undefined }));
  }
  async function submit(event) {
    event.preventDefault();
    if (uploadLock.current) return;
    const validation = validateForm(form, file, catalog?.courses || [], caps?.maxFileBytes);
    setErrors(validation); setNotice(""); setActionError("");
    if (Object.keys(validation).length || !caps?.uploadEnabled || fileChecking) return;
    uploadLock.current = true; setUploading(true);
    try {
      transfer.current = new AbortController(); transferId.current = null;
      const result = await uploadMaterial(file, form, (record) => { transferId.current = record.id; updateRecord(record); }, {signal: transfer.current.signal});
      if (!mounted.current) return;
      setNotice(result.material.status === "queued"
        ? "PDF uploaded. Processing is queued and will continue if you leave this page."
        : result.ingestion?.error ? `PDF uploaded and verified. Processing could not be queued: ${result.ingestion.error} Use Process to try again without uploading again.` : "PDF uploaded and verified. Processing is not ready; use Process when the worker and pipeline are available.");
      setShowUpload(false); removeFile(); setForm((previous) => previous.newCourse ? INITIAL_FORM : ({ ...previous, title: "", author: "" }));
      await refresh();
    } catch (error) {
      if (mounted.current && error.name !== "AbortError") setActionError(`${error.message}${error.materialId ? " The upload record was saved. If the transfer completed, use Verify upload; otherwise submit the file again." : ""}`);
    } finally { uploadLock.current = false; if (mounted.current) setUploading(false); }
  }
  async function action(record, kind) {
    setBusyId(record.id); setActionError(""); setNotice("");
    try {
      const result = await ({ retry: retryMaterial, process: processMaterial, complete: completeUpload }[kind])(record.id);
      updateRecord(result.material);
      setNotice(kind === "complete" ? "Upload verified. The material is not published." : "Processing queued. You can return later to review the result.");
    } catch (error) { setActionError(error.message); }
    finally { setBusyId(null); }
  }
  async function closeUpload() {
    transfer.current?.abort();
    setShowUpload(false); removeFile(); setForm(INITIAL_FORM); setErrors({});
    if (uploading && transferId.current) {
      try { const result = await cancelMaterial(transferId.current); updateRecord(result.material); setNotice("Upload cancelled."); }
      catch (error) { setActionError(error.message); }
    }
  }
  async function confirmAction() {
    const {record, kind} = confirmation;
    setBusyId(record.id); setActionError("");
    try {
      if (kind === "delete") {
        await deleteMaterial(record.id); setMaterials(previous => previous.map(item => item.id === record.id ? {...item, deletedAt: Date.now()} : item));
        setNotice(`“${record.title}” removed from your library.`);
      } else {
        const result = await cancelMaterial(record.id); updateRecord(result.material);
        setNotice(result.material.cancelRequested ? "Cancellation requested. The current cloud operation may finish before work stops." : "Operation cancelled.");
      }
      setConfirmation(null);
    } catch (error) { setActionError(error.message); }
    finally { setBusyId(null); }
  }
  const courses = catalog?.courses || [];
  const course = courses.find((item) => item.id === form.courseId);
  const visibleMaterials = materials.filter(item => !item.deletedAt);
  const selectedMaterial = visibleMaterials.find((item) => item.id === reviewId);
  const shown = visibleMaterials.filter(item => `${item.title} ${item.courseName}`.toLowerCase().includes(filter.toLowerCase()) &&
    (statusFilter === "all" || (statusFilter === "review" ? ["awaiting_review", "ready_to_publish"].includes(item.status) : statusFilter === "active" ? ACTIVE.has(item.status) : statusFilter === "failed" ? ["failed", "publication_uncertain", "cancelled"].includes(item.status) : item.status === "published")));
  return <div className="course-materials-page">
    <NavHeader />
    <main className="cm-main">
      <div className="cm-page-heading"><div><h1>Course materials</h1></div><Button className="cm-add-material" onClick={() => setShowUpload(true)} disabled={loading || !!loadError}><FontAwesomeIcon icon={faPlus} /> Upload material</Button></div>
      {loading && <div className="cm-connection" role="status"><Spinner animation="border" size="sm" /> Connecting to Course Materials…</div>}
      {loadError && <Alert variant="danger" role="alert"><strong>Course Materials is unavailable</strong><p>{loadError}</p><Button size="sm" variant="outline-danger" onClick={() => connect()}>Reconnect</Button></Alert>}
      {needsSignIn && <InstructorSignIn onSignedIn={() => connect()} />}
      {caps && !caps.localDevelopment && <div className="cm-account"><button className="cm-text-button" onClick={async () => { await signOut(); setCatalog(null); setMaterials([]); await connect(); }}>Sign out</button></div>}
      {!loading && !loadError && caps && (!caps.processingEnabled || !caps.publishEnabled || !caps.uploadEnabled) && <div className="cm-capabilities" aria-label="Service readiness"><details className="cm-service-details"><summary><span className={caps.processingEnabled && caps.publishEnabled && caps.uploadEnabled ? "cm-services-ready" : "cm-services-attention"}>{caps.processingEnabled && caps.publishEnabled && caps.uploadEnabled ? "Services online" : "Services need attention"}</span><span>Service details</span></summary><div className="cm-service-values">
        <span>{caps.localDevelopment ? "Loopback development access" : "Instructor authenticated"}</span>
        <span>Storage: {caps.storageConfigured ? caps.uploadMode === "s3" ? "S3 configured" : "local" : "not configured"}</span>
        <span>Worker: {caps.workerOnline ? "online" : "offline"}</span>
        <span>Processing: {caps.processingSwitchEnabled === false ? "disabled" : caps.pipelineConfigured ? "configured" : "needs configuration"}</span>
        <span>Publishing: {caps.publishSwitchEnabled === false ? "disabled" : caps.publishingConfigured ? "configured" : "needs configuration"}</span>
        {(!caps.processingEnabled || !caps.publishEnabled) && <details><summary>Service requirements</summary><p>{caps.storageReason}</p><p>{caps.processingReason}</p><p>{caps.publishReason}</p>{!caps.workerOnline && <p>Start the external worker using the same configuration and durable data directory as the API.</p>}</details>}
      </div></details></div>}
      {!loading && !loadError && !caps?.uploadEnabled && <Alert variant="warning">Uploads are unavailable. Ask your administrator to configure private storage.</Alert>}
      <div className="cm-feedback" aria-live="polite">{notice && <Alert variant="success"><FontAwesomeIcon icon={faCheck} /> {notice}</Alert>}{actionError && <Alert variant="danger" role="alert">{actionError}</Alert>}{pollError && <Alert variant="warning">{pollError} <button className="cm-text-button" onClick={() => connect()}>Reconnect</button></Alert>}</div>
      <section className="cm-card cm-library" aria-label="Course materials list">
        <div className="cm-library-tools">
          <div className="cm-search"><FontAwesomeIcon icon={faMagnifyingGlass} /><Form.Control type="search" aria-label="Search course materials" placeholder="Search materials…" value={filter} onChange={(e) => setFilter(e.target.value)} /></div>
          <div className="cm-toolbar-actions"><Form.Select aria-label="Filter by status" value={statusFilter} onChange={e => setStatusFilter(e.target.value)}>
            <option value="all">All statuses</option><option value="review">Needs review</option><option value="active">In progress</option><option value="published">Published</option><option value="failed">Failed or cancelled</option>
          </Form.Select><Button variant="outline-secondary" size="sm" aria-label="Refresh materials" title="Refresh" disabled={loading || !!loadError} onClick={() => refresh().catch((e) => setPollError(e.message))}><FontAwesomeIcon icon={faRotateRight} /></Button></div>
        </div>
        {loading ? <div className="cm-empty" role="status"><Spinner animation="border" size="sm" /><p>Loading your materials…</p></div> : loadError ? <div className="cm-empty"><h3>Connect to see your materials</h3><p>Your library will appear when the backend is available.</p></div> : visibleMaterials.length === 0 ? <div className="cm-empty"><span><FontAwesomeIcon icon={faFilePdf} /></span><h3>No course materials yet</h3><Button onClick={() => setShowUpload(true)}><FontAwesomeIcon icon={faPlus} /> Upload your first material</Button></div> : shown.length === 0 ? <div className="cm-empty"><h3>{statusFilter === "active" ? "Nothing processing right now" : "No matching materials"}</h3><p>Try another status or search.</p></div> : <div className="cm-material-list">{shown.map((item) => <article key={item.id} className="cm-material-row"><div className="cm-row-file"><FontAwesomeIcon icon={faFilePdf} /></div><div className="cm-material-info"><h3>{item.title}</h3><p>{item.courseName}</p><span>{caps?.documentTypes[item.documentType] || item.documentType} · {formatFileSize(item.fileSize)} · {item.lessonIds.length ? `${item.lessonIds.length} lesson${item.lessonIds.length > 1 ? "s" : ""}` : "Unassigned"}</span>{item.failedStage && <p className="cm-row-error">Stage: {item.failedStage}</p>}{item.error && <p className="cm-row-error">{item.error}</p>}</div><div className="cm-material-status"><span className={`cm-status cm-status-${item.status}`}><i />{item.cancelRequested ? "Cancelling…" : STATUS_LABELS[item.status] || item.status}</span><span className="cm-row-access">{caps?.visibilities[item.visibility] || item.visibility}</span></div><div className="cm-row-actions">
          {(["awaiting_review", "ready_to_publish"].includes(item.status)) ? <Button size="sm" onClick={() => setReviewId(item.id)}>Review & publish</Button> :
          <Button variant="outline-primary" size="sm" onClick={() => setReviewId(item.id)}>{item.reviewAvailable ? "View material" : "View details"}</Button>}
          {item.status === "uploading" && <Button size="sm" variant="outline-primary" disabled={busyId === item.id} onClick={() => action(item, "complete")}>Verify upload</Button>}
          {item.status === "uploaded" && <Button size="sm" disabled={!item.actions.process || busyId === item.id} title={caps?.processingReason || "Extract and compile this PDF"} onClick={() => action(item, "process")}>Start processing</Button>}
          {["failed", "cancelled", "publication_uncertain"].includes(item.status) && <Button size="sm" disabled={!item.actions.retry || busyId === item.id} onClick={() => action(item, "retry")}>{item.publicationRecovery ? "Reconcile" : item.status === "cancelled" ? "Restart processing" : "Retry"}</Button>}
          {item.actions.cancel && <Button size="sm" variant="outline-secondary" disabled={busyId === item.id} onClick={() => setConfirmation({record:item,kind:"cancel"})}>Cancel</Button>}
          {item.actions.delete && <Button className="cm-delete-button" size="sm" variant="outline-danger" aria-label={`Delete ${item.title}`} title="Delete from library" disabled={busyId === item.id} onClick={() => setConfirmation({record:item,kind:"delete"})}><FontAwesomeIcon icon={faTrashCan} /><span>Delete</span></Button>}
        </div></article>)}</div>}
      </section>
    </main>
    <Modal show={showUpload} onHide={closeUpload} centered scrollable size="lg" className="cm-upload-modal" backdrop={uploading ? "static" : true}>
      <Modal.Header closeButton><Modal.Title>Upload course material</Modal.Title></Modal.Header>
      <Modal.Body>{actionError && <Alert variant="danger" role="alert" className="m-3">{actionError}</Alert>}        <section className="cm-card cm-upload-card" aria-labelledby="upload-heading">
          <Form noValidate onSubmit={submit} className="cm-upload-form">
            <fieldset disabled={uploading}>
              <input type="file" ref={inputRef} accept="application/pdf,.pdf" className="cm-file-input" id="material-file" aria-label="Choose PDF file"
                onChange={(e) => { if (e.target.files[0]) chooseFile(e.target.files[0]); e.target.value = ""; }} />
              <div className={`cm-dropzone ${dragging ? "is-dragging" : ""} ${file ? "has-file" : ""} ${errors.file ? "has-error" : ""}`}
                onDragOver={(e) => { e.preventDefault(); if (!uploading) setDragging(true); }} onDragLeave={() => setDragging(false)}
                onDrop={(e) => { e.preventDefault(); setDragging(false); if (uploading) return;
                  if (e.dataTransfer.files.length !== 1) { setErrors((previous) => ({ ...previous, file: "Upload one PDF at a time." })); return; }
                  chooseFile(e.dataTransfer.files[0]); }}>
                {file ? <><span className="cm-file-symbol"><FontAwesomeIcon icon={faFilePdf} /></span><div className="cm-file-description"><strong>{file.name}</strong><span>{formatFileSize(file.size)} · PDF document</span></div>
                  <div className="cm-file-actions"><button type="button" className="cm-text-button" onClick={() => inputRef.current.click()}>Replace</button><button type="button" className="cm-remove-file" aria-label="Remove selected PDF" onClick={removeFile}><FontAwesomeIcon icon={faXmark} /></button></div></>
                  : <><FontAwesomeIcon icon={faCloudArrowUp} className="cm-upload-icon" /><p>{fileChecking ? "Checking PDF…" : <>Drop your PDF here or <button type="button" className="cm-browse" onClick={() => inputRef.current.click()}>browse files</button></>}</p><span>PDF only · up to 25 MB</span></>}
              </div>
              {errors.file && <p className="cm-field-error" role="alert">{errors.file}</p>}
              <div className="cm-form-grid">
                <Form.Group controlId="material-title"><Form.Label>Title</Form.Label><Form.Control value={form.title} maxLength={180} placeholder="e.g. Week 3 discussion" isInvalid={!!errors.title} onChange={(e) => field("title", e.target.value)} aria-required="true" /><Form.Control.Feedback type="invalid">{errors.title}</Form.Control.Feedback></Form.Group>
                <Form.Group controlId="material-author"><Form.Label>Author <span className="cm-optional">Optional</span></Form.Label><Form.Control value={form.author} maxLength={180} placeholder="e.g. OpenStax" isInvalid={!!errors.author} onChange={(e) => field("author", e.target.value)} /><Form.Control.Feedback type="invalid">{errors.author}</Form.Control.Feedback></Form.Group>
                <Form.Group controlId="material-type"><Form.Label>Document type</Form.Label><Form.Select value={form.documentType} onChange={(e) => field("documentType", e.target.value)} disabled={!caps}>{Object.entries(caps?.documentTypes || { textbook: "Textbook / reference" }).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</Form.Select></Form.Group>
                <Form.Group controlId="material-access"><Form.Label>Access</Form.Label><Form.Select value={form.visibility} onChange={(e) => field("visibility", e.target.value)} disabled={!caps}>{Object.entries(caps?.visibilities || { private_tutor: "Private tutor" }).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</Form.Select><p className="cm-help">{form.visibility === "private_tutor" ? "Private reference for Oski, including solution guidance." : "Student-facing reference content. Solutions still follow Oski’s disclosure rules."}</p></Form.Group>
                <Form.Group controlId="material-course" className="cm-full-width"><Form.Label>Course</Form.Label><Form.Select value={form.courseId} disabled={loading || !!loadError || (!courses.length && !catalog?.canCreateCourse)} isInvalid={!!errors.courseId} onChange={(e) => field("courseId", e.target.value)} aria-required="true"><option value="">{loading ? "Loading courses…" : "Select a course"}</option>{courses.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}{catalog?.canCreateCourse && <option value="__new__">＋ Create new course…</option>}</Form.Select><Form.Control.Feedback type="invalid">{errors.courseId}</Form.Control.Feedback></Form.Group>
                {form.newCourse && <div className="cm-full-width cm-new-course">
                  <Form.Group controlId="new-course-name"><Form.Label>New course name</Form.Label><Form.Control maxLength={180} placeholder="e.g. Introduction to Biology" value={form.newCourse.courseName} onChange={e => field("newCourse", { ...form.newCourse, courseName: e.target.value })} isInvalid={!!errors.newCourse} /><Form.Control.Feedback type="invalid">{errors.newCourse}</Form.Control.Feedback></Form.Group>
                  <Form.Group controlId="new-course-lessons"><Form.Label>Lessons</Form.Label><Form.Control as="textarea" rows={3} placeholder="One lesson name per line" value={form.newCourse.lessons.join("\n")} onChange={e => field("newCourse", { ...form.newCourse, lessons: e.target.value.split("\n") })} /><Form.Text>One name per line, up to 50 lessons. This material will be assigned to all of them.</Form.Text></Form.Group>
                  <p className="cm-help">The course stays pending until you review and publish this material. Publishing also adds these lessons and enables course Office Hours.</p>
                </div>}
              </div>
              {!form.newCourse && <LessonPicker course={course} selected={form.lessonIds} onChange={(ids) => field("lessonIds", ids)} disabled={uploading} />}
              {errors.lessonIds && <p className="cm-field-error" role="alert">{errors.lessonIds}</p>}
            </fieldset><div className="cm-upload-dialog-footer"><span><FontAwesomeIcon icon={faShieldHalved} /> Review required before publishing</span><div><Button type="button" variant="outline-secondary" onClick={closeUpload}>{uploading ? "Cancel upload" : "Cancel"}</Button><Button type="submit" disabled={uploading || loading || fileChecking || !!loadError || !caps?.uploadEnabled}><FontAwesomeIcon icon={faArrowUpFromBracket} /> {uploading ? "Uploading…" : "Upload PDF"}</Button></div></div>
          </Form>
        </section>
</Modal.Body>
    </Modal>
    <Modal show={!!confirmation} onHide={() => !busyId && setConfirmation(null)} centered className="cm-confirm-modal">
      <Modal.Header closeButton={!busyId}><Modal.Title>{confirmation?.kind === "delete" ? "Delete from your library?" : "Cancel this operation?"}</Modal.Title></Modal.Header>
      <Modal.Body><p className="cm-confirm-title">{confirmation?.record.title}</p>
        {confirmation?.kind === "delete" ? <><p>Remove this material from your library?</p>{confirmation.record.status === "published" && <Alert variant="warning">This removes the library entry only. Oski will continue to use the published material in its assigned lessons.</Alert>}</> : <p>Queued work stops immediately. An active extraction or compilation operation may finish and incur charges; later stages will stop.</p>}
        {actionError && <Alert variant="danger">{actionError}</Alert>}
      </Modal.Body><Modal.Footer><Button variant="outline-secondary" disabled={!!busyId} onClick={() => setConfirmation(null)}>Keep material</Button><Button variant={confirmation?.kind === "delete" ? "danger" : "primary"} disabled={!!busyId} onClick={confirmAction}>{busyId ? "Working…" : confirmation?.kind === "delete" ? "Delete from library" : "Cancel operation"}</Button></Modal.Footer>
    </Modal>
    {selectedMaterial && <MaterialReview key={selectedMaterial.id} material={selectedMaterial} courses={courses} capabilities={caps} onClose={() => setReviewId(null)} onUpdated={updateRecord} />}
  </div>;
}
