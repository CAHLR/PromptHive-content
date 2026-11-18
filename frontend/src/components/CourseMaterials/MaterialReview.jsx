import React, { useEffect, useRef, useState } from "react";
import { Alert, Button, Form, Modal, Spinner } from "react-bootstrap";
import { publishMaterial, reconcileMaterial, reviewAgainMaterial, reviewAssetUrl, reviewDownloadUrl, getReview, getReviewNotes, updateAssignments } from "../../services/courseMaterialsService";
import LessonPicker from "./LessonPicker";

export default function MaterialReview({ material, courses, capabilities, onClose, onUpdated }) {
  const [review, setReview] = useState(null), [error, setError] = useState("");
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState(false);
  const [reviewed, setReviewed] = useState(false), [lessons, setLessons] = useState(material.lessonIds);
  const [section, setSection] = useState(0), [cursor, setCursor] = useState(0), [back, setBack] = useState([]);
  const [reload, setReload] = useState(0), [advancedOpen, setAdvancedOpen] = useState(false), [advanced, setAdvanced] = useState(null);
  const [notes, setNotes] = useState(null), [notesCursor, setNotesCursor] = useState(0);
  const version = useRef(null);
  const course = material.newCourse ? { name: material.newCourse.courseName, lessons: material.newCourse.lessons } : courses.find((c) => c.id === material.courseId);
  const assignmentsKey = JSON.stringify(material.lessonIds);
  const frozen = material.publicationRecovery || ["uploading", "queued", "extracting", "compiling", "validating", "publish_queued", "publishing", "publication_uncertain", "published"].includes(material.status);
  useEffect(() => { setReviewed(false); setLessons(material.lessonIds); }, [assignmentsKey]);
  useEffect(() => {
    const controller = new AbortController();
    if (!material.reviewAvailable) { setLoading(false); return () => controller.abort(); }
    setLoading(true); setError(""); setReview(null); setAdvancedOpen(false); setAdvanced(null); setNotes(null);
    getReview(material.id, controller.signal, {version: version.current, section, cursor}).then((data) => {
      if (!controller.signal.aborted) { version.current = data.version; setReview(data); }
    }).catch((e) => { if (e.name !== "AbortError") setError(e.message); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [material.id, material.reviewAvailable, assignmentsKey, section, cursor, reload]);
  useEffect(() => {
    if (!advancedOpen || !review) { setAdvanced(null); return undefined; }
    const controller = new AbortController();
    // Explicit, bounded advanced-page fetch. Closing cancels and releases it.
    getReview(material.id, controller.signal, {version: review.version, section, cursor})
      .then((data) => { if (!controller.signal.aborted) setAdvanced(data.items); })
      .catch((e) => { if (e.name !== "AbortError") setError(e.message); });
    return () => controller.abort();
  }, [advancedOpen, review?.version, section, cursor]);
  function refresh() { version.current = null; setSection(0); setCursor(0); setBack([]); setReviewed(false); setReload((n) => n + 1); }
  async function action(kind) {
    setBusy(true); setError("");
    try {
      const result = kind === "assign" ? await updateAssignments(material.id, lessons) : kind === "reconcile" ? await reconcileMaterial(material.id) : kind === "review_again" ? await reviewAgainMaterial(material.id) : await publishMaterial(material.id, review);
      onUpdated(result.material); setReviewed(false);
      if (kind === "review_again") refresh();
      else if (kind !== "assign") onClose();
    } catch (e) { setError(e.message); } finally { setBusy(false); }
  }
  async function loadNotes() {
    try { const data = await getReviewNotes(material.id, review.version, notesCursor); setNotes(data); }
    catch (e) { setError(e.message); }
  }
  const changed = JSON.stringify([...lessons].sort()) !== JSON.stringify([...material.lessonIds].sort());
  return <Modal show onHide={busy ? undefined : onClose} size="lg" scrollable className="cm-review">
    <Modal.Header closeButton={!busy}><Modal.Title>{material.reviewAvailable ? "Review course material" : "Material details"}</Modal.Title></Modal.Header>
    <Modal.Body>
      <h4>{material.title}</h4><p className="cm-help">{material.courseName} · {material.filename}</p>
      {error && <Alert variant="danger" role="alert">{error} <Button size="sm" variant="link" onClick={refresh}>Refresh review</Button></Alert>}
      {material.error && <Alert variant="warning">{material.error}</Alert>}
      {material.publicationRecovery && material.status !== "published" && <Alert variant="info">Publication recovery owns this content and its original assignments. Use Reconcile publication to verify or finish that exact approved version. Assignment edits and reapproval are locked. If a conflict persists, the operator must inspect the saved journal; do not delete it.</Alert>}
      {material.newCourse && material.status !== "published" && <Alert variant="info">Pending course: publishing also creates {material.courseName}, its lessons, and course Office Hours.</Alert>}
      <LessonPicker course={course} selected={lessons} onChange={setLessons} disabled={busy || frozen} id="review-lessons" />
      <Button size="sm" variant="outline-primary" disabled={busy || frozen || !changed} onClick={() => action("assign")}>Save assignments</Button>
      {loading && <p role="status"><Spinner size="sm" animation="border" /> Loading review page…</p>}
      {review && <>
        <hr />{review.demo && <Alert variant="warning">TEST / DEMO extraction. Not a real cloud processing result.</Alert>}
        <Alert variant={review.validation.ok ? "success" : "danger"}>{review.validation.ok ? "Structure validation passed. Review educational accuracy before publishing." : "Validation failed; approval is blocked."}</Alert>
        <section className="cm-warnings"><h5>Validation notes & uncertainties ({review.notesCount})</h5>
          <ul>{(notes?.notes || [...review.validation.errors, ...review.validation.warnings, ...review.uncertainties]).map((note, i) => <li key={i}>{note}</li>)}</ul>
          {review.notesCount > 0 && <><Button size="sm" variant="link" onClick={loadNotes}>Load notes page</Button><Button size="sm" variant="link" disabled={!notes?.nextCursor} onClick={async () => { try { const data = await getReviewNotes(material.id, review.version, notes.nextCursor); setNotesCursor(notes.nextCursor); setNotes(data); } catch (e) { setError(e.message); } }}>Next notes</Button></>}
        </section>
        <div className="cm-review-heading"><h5>Extracted content</h5><Button size="sm" variant="link" onClick={refresh}>Refresh review</Button></div>
        <p className="cm-help">Check the extracted content and source images. Longer sections continue on the next page. Review all pages and validation notes before approving.</p>
        <div className="cm-review-pagination"><Button size="sm" disabled={section === 0} onClick={() => {setSection(section - 1); setCursor(0); setBack([]);}}>Previous section</Button>
          <Form.Select aria-label="Extracted section" value={section} onChange={(e) => {setSection(Number(e.target.value)); setCursor(0); setBack([]);}}>{review.sections.map((item) => <option key={item.id} value={item.id}>{item.id + 1}. {item.title}</option>)}</Form.Select>
          <Button size="sm" disabled={section >= review.sectionCount - 1} onClick={() => {setSection(section + 1); setCursor(0); setBack([]);}}>Next section</Button></div>
        <div className="cm-review-page-controls"><Button size="sm" variant="outline-primary" disabled={!back.length} onClick={() => {setCursor(back[back.length - 1]); setBack(back.slice(0, -1));}}>Previous content page</Button><span>Page {back.length + 1}</span><Button size="sm" variant="outline-primary" disabled={review.nextCursor === null} onClick={() => {setBack([...back, cursor]); setCursor(review.nextCursor);}}>Next content page</Button></div>
        <ReadableContent items={review.items} materialId={material.id} />
        <div className="cm-review-pagination"><Button size="sm" variant="outline-primary" disabled={!back.length} onClick={() => {setCursor(back[back.length - 1]); setBack(back.slice(0, -1));}}>Previous page</Button><span>Page {back.length + 1}</span><Button size="sm" variant="outline-primary" disabled={review.nextCursor === null} onClick={() => {setBack([...back, cursor]); setCursor(review.nextCursor);}}>Next page</Button></div>
        <Button variant="link" size="sm" aria-expanded={advancedOpen} onClick={() => setAdvancedOpen(!advancedOpen)}>{advancedOpen ? "Close advanced JSON" : "Open advanced JSON"}</Button>
        {advancedOpen && <section><p>Raw fragments for this page only. Download the full immutable compiled JSON separately.</p><a href={reviewDownloadUrl(material.id, review.version)}>Download full compiled JSON</a>{advanced ? <pre>{JSON.stringify(advanced, null, 2)}</pre> : <p role="status">Loading advanced page…</p>}</section>}
        {!frozen && ["awaiting_review", "ready_to_publish"].includes(material.status) && <Form.Check id="material-reviewed" checked={reviewed} onChange={(e) => setReviewed(e.target.checked)} label="I reviewed the complete extraction, warnings, and assignments for educational accuracy." />}
      </>}
      {review && !frozen && <Alert variant="warning">Publishing makes this material available to Oski in the selected lessons and their course Office Hours. Configuration caches may take several minutes to refresh.</Alert>}
      {!loading && !review && !material.reviewAvailable && <Alert variant="info">No extraction is available yet.</Alert>}
      {material.publication && <Alert variant="success">Verified {new Date(material.publication.publishedAt).toLocaleString()}. Oski may retain cached configuration for several minutes.
        <details><summary>Publication details</summary>{material.publication.publicationMethod ? <p>Published through <code>npm run publish-docs</code> using the bundled pipeline. Course: {material.publication.courseName}. Lessons: {material.publication.lessonIds?.join(", ")}. Configuration hash: <code>{material.publication.coursePlansHash}</code>.</p> : <p>Legacy publication commit: <code>{material.publication.version}</code>. Back up its private archive and journal with the checkout.</p>}</details></Alert>}
      {!capabilities?.publishEnabled && <p className="cm-help">{capabilities?.publishReason}</p>}
    </Modal.Body>
    <Modal.Footer><Button variant="outline-secondary" disabled={busy} onClick={onClose}>Close</Button>
      {material.actions?.reviewAgain && <Button disabled={busy} onClick={() => action("review_again")}>Review again</Button>}
      {material.publicationRecovery && ["failed", "publication_uncertain"].includes(material.status) && <Button disabled={busy || !capabilities?.publishEnabled} onClick={() => action("reconcile")}>Reconcile publication</Button>}
      {!frozen && ["awaiting_review", "ready_to_publish"].includes(material.status) && <Button disabled={busy || loading || !reviewed || !review?.validation.ok || !lessons.length || changed || !capabilities?.publishEnabled} onClick={() => action("publish")}>{busy ? "Queueing…" : "Approve & Publish"}</Button>}
    </Modal.Footer>
  </Modal>;
}

export function ReadableContent({items = [], materialId}) {
  const technical = [], groups = [];
  for (const item of items) {
    const field = item.path?.split(".").pop() || "";
    if (!item.asset && (/(^|_)id$/.test(field) || ["visibility", "question_type", "asset_type", "confidence"].includes(field) || item.path?.startsWith("metadata."))) {
      technical.push(item); continue;
    }
    const label = contentLabel(item.path);
    if (groups[groups.length - 1]?.label !== label) groups.push({label, items:[]});
    groups[groups.length - 1].items.push(item);
  }
  return <section aria-label="Review page">{groups.map((group, index) => <article className="cm-learning-object" key={`${group.label}-${index}`}>
    <h6>{group.label}</h6>
    {group.items.map(item => item.asset && materialId ? <figure key={item.id}>{/\.(png|jpe?g|webp|gif)$/i.test(item.asset) && <img loading="lazy" className="cm-review-asset" alt="Extracted source asset" src={reviewAssetUrl(materialId, item.asset)} />}<a href={reviewAssetUrl(materialId, item.asset)} target="_blank" rel="noreferrer">Open source asset</a></figure> : <p className="cm-content-text" key={item.id}>{item.text}</p>)}
  </article>)}
  {technical.length > 0 && <details className="cm-technical-content"><summary>Reference details ({technical.length} fields)</summary>{technical.map(item => <div key={item.id}><strong>{item.path.split(".").pop().replaceAll("_", " ")}</strong><span>{item.text}</span></div>)}</details>}
  </section>;
}

function contentLabel(path = "") {
  const value = path.toLowerCase();
  if (value.includes("assets")) return "Source figure";
  if (value.includes("concepts")) return "Key concepts";
  if (value.includes("source")) return "Source information";
  if (value.includes("metadata")) return "Document details";
  if (value.includes("solution") || value.includes("explanation")) return "Explanation & solution";
  if (value.includes("example")) return "Worked example";
  if (value.includes("definition")) return "Definition";
  if (value.includes("notes")) return "Reference notes";
  if (value.includes("problems") || value.includes("question")) return "Question";
  const field = path.split(".").filter(part => part !== "item").pop() || "Content";
  return field.replaceAll("_", " ").replace(/^./, char => char.toUpperCase());
}
