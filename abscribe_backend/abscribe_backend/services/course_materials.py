"""Isolated course-material records and durable ingestion queue (no Mongo dependency)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import shutil
import time
import uuid
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from .course_materials_config import load_config

load_config()

DOCUMENT_TYPES = {"textbook": "Textbook / reference", "discussion_solutions": "Worksheet / discussion solutions"}
VISIBILITIES = {"private_tutor": "Private tutor", "student": "Student reference"}
MAX_PDF_BYTES = 25 * 1024 * 1024
ACTIVE_STATES = ("extracting", "compiling", "validating")
PUBLICATION_STATES = ("publishing",)


class MaterialError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def pipeline_package_root():
    return Path(__file__).resolve().parents[2] / "course_materials_pipeline"


def pipeline_data_root():
    base = Path(os.environ.get("COURSE_MATERIALS_DATA_DIR") or Path(__file__).resolve().parents[2] / "instance/course-materials")
    return Path(os.environ.get("COURSE_MATERIALS_PIPELINE_DATA_DIR") or base / "pipeline").resolve()


def source_snapshot():
    root = pipeline_data_root()
    plans_path = root / "coursePlans.json"
    manifest_path = root / "documents/manifest.json"
    try:
        plans_bytes = plans_path.read_bytes()
        manifest_bytes = manifest_path.read_bytes()
        plans = json.loads(plans_bytes)
        manifest = json.loads(manifest_bytes)
        if not isinstance(plans, list) or not isinstance(manifest, dict):
            raise ValueError()
    except (OSError, ValueError):
        raise MaterialError("Runtime course configuration is not synchronized. Start the materials worker or run npm run sync-config in the bundled pipeline with the shared environment.", 503)
    courses = []
    for course in plans:
        name = course.get("courseName", "")
        if not name or course.get("editor") or name.startswith("!!"):
            continue
        lessons = {}
        for lesson in course.get("lessons", []):
            if lesson.get("type") or not lesson.get("id"):
                continue
            lessons.setdefault(lesson["id"], {"id": lesson["id"], "name": lesson.get("name", lesson["id"]), "topics": lesson.get("topics", "")})
        courses.append({"id": hashlib.sha256(name.encode()).hexdigest()[:20], "name": name, "lessons": list(lessons.values())})
    return {"courses": courses, "plans": plans, "manifest": manifest,
            "coursePlansHash": hashlib.sha256(plans_bytes).hexdigest(),
            "manifestHash": hashlib.sha256(manifest_bytes).hexdigest()}


@lru_cache(maxsize=4)
def supported_node(executable):
    import subprocess
    if not executable: return False
    try:
        version = subprocess.check_output([executable, '--version'], text=True, timeout=3).strip()
        return version.split('.')[0] == 'v22'
    except (OSError, subprocess.SubprocessError):
        return False


def capabilities(local_dev=False):
    upload_mode = os.environ.get("COURSE_MATERIALS_UPLOAD_MODE", "local" if local_dev else "disabled")
    upload_enabled = upload_mode == "local" and local_dev
    if upload_mode == "s3":
        upload_enabled = bool(os.environ.get("COURSE_MATERIALS_UPLOAD_BUCKET") and importlib.util.find_spec("boto3"))
    pipeline = pipeline_package_root()
    processing_missing = []
    for present, requirement in [
        (importlib.util.find_spec("ijson"), "Install requirements-course-materials.txt (ijson review indexing)"),
        (os.environ.get("COURSE_MATERIALS_PROCESSING_ENABLED") == "1", "COURSE_MATERIALS_PROCESSING_ENABLED=1"),
        (os.environ.get("COURSE_DOCS_S3_BUCKET"), "COURSE_DOCS_S3_BUCKET (BDA staging bucket)"),
        (os.environ.get("BDA_PROFILE_ARN") or os.environ.get("AWS_ACCOUNT_ID"), "BDA_PROFILE_ARN or AWS_ACCOUNT_ID"),
        (os.environ.get("SEMANTIC_COMPILER_PROVIDER", "openai") == "bedrock" or os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY for the OpenAI compiler"),
        ((pipeline / "scripts/semantic-compiler.mjs").is_file(), "Bundled Course Materials compiler"),
        ((pipeline / "node_modules/ajv/dist/2020.js").is_file() and supported_node(shutil.which("node")), "Node 22 on API/worker PATH and bundled pipeline dependencies"),
    ]:
        if not present:
            processing_missing.append(requirement)
    checkout_missing = []
    for relative in ("scripts/compile-documents.mjs", "scripts/validate-compiled.mjs", "scripts/publish-docs.mjs", "scripts/course-materials-cli.mjs", "scripts/bda-client.mjs", "scripts/semantic-compiler.mjs", "scripts/embed-documents.mjs", "scripts/sync-runtime-config.mjs", "scripts/providers/llm-provider.mjs", "schemas/learning-object.schema.json", "package.json"):
        if not (pipeline / relative).is_file(): checkout_missing.append(f"Missing bundled pipeline file: {relative}")
    for dependency in ("@aws-sdk/client-s3", "@aws-sdk/client-bedrock-runtime", "@aws-sdk/client-bedrock-data-automation-runtime", "ajv", "ajv-formats", "openai"):
        if not (pipeline / "node_modules" / dependency / "package.json").is_file(): checkout_missing.append(f"Bundled pipeline dependency missing: {dependency}; run npm ci in course_materials_pipeline")
    if not (pipeline_data_root() / "documents/manifest.json").is_file(): checkout_missing.append("Runtime manifest.json not synchronized")
    plans = pipeline_data_root() / "coursePlans.json"
    if not plans.is_file(): checkout_missing.append("Runtime coursePlans.json not synchronized")
    if not shutil.which("npm"): checkout_missing.append("npm on API/worker PATH")
    writable = pipeline_data_root()
    while not writable.exists(): writable = writable.parent
    if not os.access(writable, os.W_OK): checkout_missing.append("Write access to materials pipeline data directory")
    processing_missing.extend(checkout_missing)
    publishing_missing = list(checkout_missing)
    for present, requirement in [
        (os.environ.get("COURSE_MATERIALS_PUBLISH_ENABLED") == "1", "COURSE_MATERIALS_PUBLISH_ENABLED=1"),
        (os.environ.get("COURSE_DOCS_RUNTIME_BUCKET"), "COURSE_DOCS_RUNTIME_BUCKET"),
        ((pipeline / "scripts/publication-state.mjs").is_file() and (pipeline / "node_modules/ajv/dist/2020.js").is_file() and (pipeline / "node_modules/@aws-sdk/client-s3").is_dir() and supported_node(shutil.which("node")), "Bundled coordinated publisher and Node 22 dependencies"),
    ]:
        if not present:
            publishing_missing.append(requirement)
    storage_reason = None if upload_enabled else ("Install requirements-course-materials.txt and configure COURSE_MATERIALS_UPLOAD_BUCKET." if upload_mode == "s3" else "Configure S3 upload storage, or use loopback-only local uploads.")
    return {"processingSwitchEnabled": os.environ.get("COURSE_MATERIALS_PROCESSING_ENABLED") == "1", "publishSwitchEnabled": os.environ.get("COURSE_MATERIALS_PUBLISH_ENABLED") == "1", "publicationScope": "local-artifacts-and-shared-runtime-config", "checkoutMissing": checkout_missing, "uploadMode": upload_mode, "uploadEnabled": upload_enabled,
            "storageConfigured": upload_enabled, "storageReason": storage_reason,
            "pipelineConfigured": not processing_missing, "publishingConfigured": not publishing_missing,
            "processingEnabled": False, "publishEnabled": False, "localDevelopment": local_dev,
            "processingMissing": processing_missing, "publishingMissing": publishing_missing,
            "maxFileBytes": MAX_PDF_BYTES, "documentTypes": DOCUMENT_TYPES, "visibilities": VISIBILITIES,
            "processingReason": "Missing: " + "; ".join(processing_missing) if processing_missing else "Start the course-materials worker on the API’s durable storage.",
            "publishReason": "Missing: " + "; ".join(publishing_missing) if publishing_missing else "Start the course-materials worker on the API’s durable storage."}


def configuration_fingerprint():
    # Hash secrets as part of the comparison; never expose this snapshot to clients.
    names = sorted(k for k in os.environ if k.startswith(("COURSE_MATERIALS_", "COURSE_DOCS_", "BDA_", "SEMANTIC_COMPILER_", "OPENAI_COMPILER_", "EMBEDDING_")) or k in ("AWS_PROFILE", "AWS_ACCOUNT_ID", "OPENAI_API_KEY"))
    names = [k for k in names if k not in ("COURSE_MATERIALS_PORT", "COURSE_MATERIALS_ALLOWED_ORIGINS")]
    return hashlib.sha256(json.dumps({**{k: os.environ[k] for k in names}, "_nodeExecutable": shutil.which("node")}, sort_keys=True).encode()).hexdigest()


class MaterialStore:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS materials (id TEXT PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL, updated REAL NOT NULL, lease REAL, token TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS worker (id INTEGER PRIMARY KEY, heartbeat REAL NOT NULL, configuration TEXT)")
            if "configuration" not in {r[1] for r in db.execute("PRAGMA table_info(worker)")}:
                db.execute("ALTER TABLE worker ADD COLUMN configuration TEXT")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.directory / "materials.sqlite3", timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def insert(self, record):
        with self.connect() as db:
            db.execute("INSERT INTO materials(id,owner,status,data,updated) VALUES (?,?,?,?,?)", (record["id"], record["owner"], record["status"], json.dumps(record), time.time()))
        return record

    def get(self, material_id):
        with self.connect() as db:
            row = db.execute("SELECT data FROM materials WHERE id=?", (material_id,)).fetchone()
        if not row:
            raise MaterialError("Material not found.", 404)
        return json.loads(row["data"])

    def list(self):
        with self.connect() as db:
            return [json.loads(row["data"]) for row in db.execute("SELECT data FROM materials ORDER BY updated DESC")]

    def change(self, material_id, expected, update, token=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
            if not row:
                raise MaterialError("Material not found.", 404)
            if row["status"] not in expected or (token is not None and row["token"] != token):
                raise MaterialError("Material changed. Refresh its status before trying again.", 409)
            record = json.loads(row["data"])
            values = update(record) if callable(update) else update
            record.update(values)
            record["updatedAt"] = time.time()
            if record["status"] != row["status"]:
                record.setdefault("history", []).append({"status": record["status"], "at": record["updatedAt"]})
            db.execute("UPDATE materials SET status=?,data=?,updated=? WHERE id=?", (record["status"], json.dumps(record), time.time(), material_id))
        return record

    def heartbeat(self, material_id=None, token=None):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO worker(id,heartbeat,configuration) VALUES (1,?,?)", (time.time(), configuration_fingerprint()))
            if material_id:
                result = db.execute("UPDATE materials SET lease=? WHERE id=? AND token=?", (time.time() + 90, material_id, token))
                return result.rowcount == 1

    def worker_online(self):
        with self.connect() as db:
            row = db.execute("SELECT heartbeat,configuration FROM worker WHERE id=1").fetchone()
        return bool(row and row[0] > time.time() - 45 and row[1] == configuration_fingerprint())

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Interrupted publication is idempotently reconciled; paid extraction
            # is never automatically repeated after a worker crash.
            for row in db.execute("SELECT * FROM materials WHERE status IN ('extracting','compiling','validating','publishing') AND lease < ?", (time.time(),)).fetchall():
                record = json.loads(row["data"])
                state = "publish_queued" if row["status"] == "publishing" else "failed"
                record.update(status=state, cancelRequested=False, failedStage=row["status"], error="Worker interrupted; publication will reconcile." if state == "publish_queued" else "Worker interrupted. Retry processing.", updatedAt=time.time())
                record.setdefault("history", []).append({"status": state, "at": time.time()})
                db.execute("UPDATE materials SET status=?,data=?,updated=?,token=NULL,lease=NULL WHERE id=?", (state, json.dumps(record), time.time(), row["id"]))
            if db.execute("SELECT 1 FROM materials WHERE status IN ('extracting','compiling','validating','publishing') LIMIT 1").fetchone():
                return None
            row = db.execute("SELECT * FROM materials WHERE status IN ('queued','publish_queued') ORDER BY updated LIMIT 1").fetchone()
            if not row:
                return None
            record = json.loads(row["data"])
            token = uuid.uuid4().hex
            state = "publishing" if row["status"] == "publish_queued" else "extracting"
            record.update(status=state, error=None, updatedAt=time.time())
            record.setdefault("history", []).append({"status": state, "at": time.time()})
            db.execute("UPDATE materials SET status=?,data=?,updated=?,lease=?,token=? WHERE id=?", (state, json.dumps(record), time.time(), time.time() + 90, token, row["id"]))
            return record, token


def pending_course(data, snapshot, allowed_courses):
    """Validate a private pending course; never write shared configuration here."""
    if allowed_courses is not None:
        raise MaterialError("Only administrators can create courses.", 403)
    draft = data.get("newCourse")
    if not isinstance(draft, dict):
        raise MaterialError("Enter a new course name and at least one lesson.")
    name = draft.get("courseName", "")
    lessons = draft.get("lessons")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 180 or name.strip().startswith("!!"):
        raise MaterialError("Course name must be 1–180 characters and cannot start with !!.")
    name = name.strip()
    if not isinstance(lessons, list) or not 1 <= len(lessons) <= 50:
        raise MaterialError("Add between 1 and 50 lessons.")
    normalized = []
    for lesson in lessons:
        title = lesson.get("name") if isinstance(lesson, dict) else lesson
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 180:
            raise MaterialError("Each lesson needs a name of 1–180 characters.")
        normalized.append(title.strip())
    if len({title.casefold() for title in normalized}) != len(normalized):
        raise MaterialError("Lesson names must be unique within the new course.")
    # Stored drafts have server-generated IDs; request drafts must not choose IDs.
    stored = bool(data.get("documentId"))
    ids = [lesson.get("id") for lesson in lessons] if stored else ["ph-lesson-" + uuid.uuid4().hex for _ in lessons]
    if any(not isinstance(i, str) or not i.startswith("ph-lesson-") or len(i) != 42 or any(c not in "0123456789abcdef" for c in i[10:]) for i in ids) or len(set(ids)) != len(ids):
        raise MaterialError("Invalid pending lesson IDs.")
    for course in snapshot["plans"]:
        if str(course.get("courseName", "")).strip().casefold() == name.casefold():
            existing = [(l.get("id"), l.get("name")) for l in course.get("lessons", [])]
            if not stored or course.get("courseName") != name or existing != list(zip(ids, normalized)):
                raise MaterialError("A course with this name already exists. Select it or choose another name.", 409)
        elif any(l.get("id") in ids for l in course.get("lessons", [])):
            raise MaterialError("Pending lesson ID conflicts with another course.", 409)
    return {"courseName": name, "office_hours": True, "language": "en", "courseOER": 0, "courseLicense": 0,
            "lessons": [{"id": i, "name": title, "topics": "", "allowRecycle": False,
                         "chat_display_mode": "Window", "learningObjectives": {}} for i, title in zip(ids, normalized)]}


def validate_metadata(data, snapshot, allowed_courses):
    if not isinstance(data, dict):
        raise MaterialError("Expected a metadata object.")
    result = {}
    for field, maximum, required in (("title", 180, True), ("author", 180, False), ("filename", 240, True)):
        value = data.get(field, "")
        if not isinstance(value, str) or len(value.strip()) > maximum or (required and not value.strip()):
            raise MaterialError(f"{field.capitalize()} must be {'1–' if required else 'at most '}{maximum} characters.")
        result[field] = value.strip()
    if "/" in result["filename"] or "\\" in result["filename"] or not result["filename"].lower().endswith(".pdf"):
        raise MaterialError("Choose a PDF file.")
    size = data.get("fileSize")
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= MAX_PDF_BYTES:
        raise MaterialError("PDF must be nonempty and no larger than 25 MB.")
    if data.get("contentType") != "application/pdf":
        raise MaterialError("Only application/pdf uploads are accepted.")
    if data.get("documentType") not in DOCUMENT_TYPES or data.get("visibility") not in VISIBILITIES:
        raise MaterialError("Choose a supported document type and visibility.")
    draft = pending_course(data, snapshot, allowed_courses) if data.get("newCourse") is not None else None
    course = ({"id": hashlib.sha256(draft["courseName"].encode()).hexdigest()[:20], "name": draft["courseName"], "lessons": draft["lessons"]}
              if draft else next((c for c in snapshot["courses"] if c["id"] == data.get("courseId")), None))
    if not course:
        raise MaterialError("Choose a course from the current OATutor catalog.")
    if allowed_courses is not None and course["name"] not in allowed_courses:
        raise MaterialError("You do not have access to this course.", 403)
    ids = [l["id"] for l in draft["lessons"]] if draft and not data.get("documentId") else data.get("lessonIds", [])
    if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
        raise MaterialError("Lesson assignments must be a list of unique lesson IDs.")
    valid_ids = {l["id"] for l in course["lessons"]}
    if not set(ids) <= valid_ids:
        raise MaterialError("One or more lessons do not belong to this course.")
    return {**result, "fileSize": size, "contentType": "application/pdf", "documentType": data["documentType"],
            "visibility": data["visibility"], "courseId": course["id"], "courseName": course["name"], "lessonIds": ids,
            **({"newCourse": draft} if draft else {})}


def verify_pdf(data, expected_size):
    if len(data) != expected_size or not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]:
        raise MaterialError("The upload is incomplete or is not a PDF. Choose a valid PDF and try again.")


def s3_client():
    import boto3  # Optional production upload dependency; never needed in local mode.
    return boto3.client("s3", region_name=os.environ.get("COURSE_MATERIALS_UPLOAD_REGION", "us-west-2"))


def read_uploaded_pdf(store, record):
    if record["storage"] == "local":
        path = store.directory / record["id"] / "source.pdf"
        if not path.exists():
            raise MaterialError("Upload the PDF before completing this upload.", 409)
        data = path.read_bytes()
    else:
        client = s3_client()
        kwargs = {"Bucket": os.environ["COURSE_MATERIALS_UPLOAD_BUCKET"], "Key": record["objectKey"]}
        if record.get("etag"):
            kwargs["IfMatch"] = record["etag"]
        response = client.get_object(**kwargs)
        if response["ContentLength"] != record["fileSize"]:
            raise MaterialError("Uploaded size does not match the selected PDF.")
        data = response["Body"].read(MAX_PDF_BYTES + 1)
        if not record.get("etag"):
            record["etag"] = response["ETag"]
    verify_pdf(data, record["fileSize"])
    return data
