"""Instructor-only API. Local mode is explicitly opt-in and loopback-only."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from flask import Blueprint, Flask, g, jsonify, request, current_app, session, send_file
from flask_cors import CORS

from abscribe_backend.services.course_materials import (
    ACTIVE_STATES, MaterialError, MaterialStore, capabilities, read_uploaded_pdf, pipeline_data_root,
    s3_client, source_snapshot, validate_metadata, verify_pdf,
)

course_materials = Blueprint("course_materials", __name__, url_prefix="/api/course-materials")
LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def check_origin():
    origin = request.headers.get("Origin")
    allowed = set(filter(None, os.environ.get("COURSE_MATERIALS_ALLOWED_ORIGINS", "").split(",")))
    allowed.add(request.host_url.rstrip("/"))
    if os.environ.get("COURSE_MATERIALS_LOCAL_DEV") == "1":
        valid_origin = origin is None or urlparse(origin).hostname in LOOPBACK
    else:
        valid_origin = origin in allowed
    if not valid_origin or request.headers.get("X-Course-Materials-Request") != "1":
        raise MaterialError("This request is missing trusted origin protection.", 403)


def get_store():
    from flask import current_app
    directory = current_app.config.get("COURSE_MATERIALS_DATA_DIR") or os.environ.get("COURSE_MATERIALS_DATA_DIR")
    return MaterialStore(directory or Path(__file__).resolve().parents[1] / "instance/course-materials")


@course_materials.errorhandler(MaterialError)
def material_error(error):
    return jsonify(error=str(error)), error.status


@course_materials.errorhandler(413)
def too_large(_error):
    return jsonify(error="PDF exceeds the 25 MB upload limit."), 413


@course_materials.before_request
def authorize():
    if request.method == "OPTIONS":
        return None
    if request.content_length and request.content_length > 26 * 1024 * 1024:
        raise MaterialError("PDF exceeds the 25 MB upload limit.", 413)
    dev_requested = os.environ.get("COURSE_MATERIALS_LOCAL_DEV") == "1"
    local = dev_requested and request.remote_addr in LOOPBACK and urlparse(request.host_url).hostname in LOOPBACK
    origin = request.headers.get("Origin")
    if dev_requested and not local:
        raise MaterialError("Local development access is restricted to this computer.", 403)
    if request.path.endswith("/session"):
        if request.method != "GET":
            check_origin()
        return None
    if local:
        if origin and urlparse(origin).hostname not in LOOPBACK:
            raise MaterialError("Origin is not allowed.", 403)
        g.material_user = "local-instructor"
        g.material_courses = None
    else:
        # The legacy app currently leaves Security initialization commented out.
        # Never treat its anonymous/demo Documents behavior as upload permission.
        try:
            from flask_security import current_user
            authenticated = bool(current_user and current_user.is_authenticated and current_user.is_active)
        except (AttributeError, RuntimeError):
            authenticated = False
        from .services.course_materials_auth import session_account
        session_secret = os.environ.get("COURSE_MATERIALS_SECRET_KEY", "")
        provisioned = session_account(get_store()) if len(session_secret) >= 32 and current_app.secret_key == session_secret else None
        if provisioned and provisioned["active"]:
            g.material_user, g.material_courses = provisioned["id"], provisioned["courses"]
            if request.method not in ("GET", "HEAD"):
                check_origin()
            g.material_local = False
            return None
        if not authenticated:
            raise MaterialError("Sign in with your provisioned instructor account.", 401)
        admin = current_user.has_role("admin")
        if not admin and not current_user.has_role("instructor"):
            raise MaterialError("An instructor role is required.", 403)
        g.material_user = str(current_user.get_id())
        try:
            scopes = json.loads(os.environ.get("COURSE_MATERIALS_INSTRUCTOR_COURSES", "{}"))
            allowed = scopes.get(g.material_user, [])
            if not isinstance(allowed, list) or any(not isinstance(name, str) for name in allowed):
                raise ValueError()
        except (ValueError, AttributeError):
            raise MaterialError("Instructor course permissions are not configured correctly.", 503)
        g.material_courses = None if admin else allowed
        if request.method not in ("GET", "HEAD"):
            check_origin()
    g.material_local = local


@course_materials.route("/session", methods=["GET", "POST", "DELETE"])
def instructor_session():
    from .services.course_materials_auth import authenticate, session_account
    if len(str(os.environ.get("COURSE_MATERIALS_SECRET_KEY", ""))) < 32:
        raise MaterialError("Instructor sign-in requires COURSE_MATERIALS_SECRET_KEY (32+ random characters).", 503)
    # The main Flask app must use the same explicit secret for this session.
    if current_app.secret_key != os.environ["COURSE_MATERIALS_SECRET_KEY"]:
        raise MaterialError("The server session secret is not initialized correctly.", 503)
    if request.method == "DELETE":
        session.clear()
        return jsonify(authenticated=False)
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict) or not isinstance(data.get("email"), str) or not isinstance(data.get("password"), str) or len(data["email"]) > 255 or len(data["password"]) > 1024:
            raise MaterialError("Enter your instructor email and password.")
        actor = authenticate(get_store(), data["email"], data["password"], request.remote_addr)
    else:
        actor = session_account(get_store())
    return jsonify(authenticated=bool(actor and actor["active"]), email=actor["id"] if actor else None)


def available():
    result = capabilities(g.material_local)
    result["workerOnline"] = get_store().worker_online()
    result["processingEnabled"] = result["pipelineConfigured"] and result["workerOnline"]
    result["publishEnabled"] = result["publishingConfigured"] and result["workerOnline"]
    if result["processingEnabled"]:
        result["processingReason"] = None
    if result["publishEnabled"]:
        result["publishReason"] = None
    return result


def visible(record):
    return g.material_courses is None or record["courseName"] in g.material_courses


def material(material_id):
    record = get_store().get(material_id)
    if not visible(record) or record.get("deletedAt"):
        raise MaterialError("Material not found.", 404)
    return record


def public_record(record, caps=None):
    result = {k: v for k, v in record.items() if k not in ("owner", "objectKey", "etag")}
    caps = caps or available()
    result["publicationRecovery"] = publication_recovery(record)
    result["actions"] = {
        "cancel": not record.get("cancelRequested") and record["status"] in ("uploading", "queued", *ACTIVE_STATES, "publish_queued") and not result["publicationRecovery"],
        "delete": record["status"] in ("uploaded", "cancelled", "failed", "awaiting_review", "ready_to_publish", "published") and (record["status"] == "published" or not result["publicationRecovery"]),
        "process": record["status"] == "uploaded" and caps["processingEnabled"],
        "retry": record["status"] in ("failed", "cancelled", "publication_uncertain") and (caps["publishEnabled"] if record.get("failedStage") == "publishing" else caps["processingEnabled"]),
        "reviewAgain": record["status"] == "failed" and record.get("failedStage") == "publishing" and not result["publicationRecovery"],
        "review": bool(record.get("reviewAvailable")),
        "approve": record["status"] == "awaiting_review" and bool(record["lessonIds"]),
        "publish": record["status"] in ("awaiting_review", "ready_to_publish") and bool(record["lessonIds"]) and caps["publishEnabled"],
    }
    return result


@course_materials.get("/catalog")
def catalog():
    snapshot = source_snapshot()
    return jsonify(courses=[c for c in snapshot["courses"] if g.material_courses is None or c["name"] in g.material_courses],
                   catalogVersion=snapshot["coursePlansHash"], canCreateCourse=g.material_courses is None, capabilities=available())


@course_materials.get("/materials")
def list_materials():
    caps = available()
    include_deleted = request.args.get("includeDeleted") == "1"
    return jsonify(materials=[public_record(r, caps) for r in get_store().list() if visible(r) and (include_deleted or not r.get("deletedAt"))], capabilities=caps)


@course_materials.post("/materials/<material_id>/cancel")
def cancel_material(material_id):
    record = material(material_id)
    if record["status"] == "cancelled" or record.get("cancelRequested"):
        return jsonify(material=public_record(record))
    if not public_record(record)["actions"]["cancel"]:
        raise MaterialError("This operation cannot be cancelled safely. Wait for completion or reconcile publication.", 409)
    def update(current):
        active = current["status"] in ACTIVE_STATES
        # Recorded under the same SQLite write transaction as the state transition.
        directory = get_store().directory / material_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "cancel-requested").touch()
        return {"status": current["status"] if active else "cancelled", "cancelRequested": active,
                "approval": None, "error": None}
    record = get_store().change(material_id, ("uploading", "queued", *ACTIVE_STATES, "publish_queued"), update)
    return jsonify(material=public_record(record)), 202


@course_materials.delete("/materials/<material_id>")
def delete_material(material_id):
    record = material(material_id)
    if not public_record(record)["actions"]["delete"]:
        raise MaterialError("Cancel active work or reconcile publication before deleting this material.", 409)
    marker = pipeline_data_root() / "publication-state/compilation.json"
    if marker.exists() and json.loads(marker.read_text()).get("documentId") == material_id:
        raise MaterialError("Recover this material's interrupted compilation before deleting it.", 409)
    # Recoverable deletion: retain private source, artifacts and publication receipts.
    # Never mutate runtime S3, shared manifest, or course bindings from this action.
    get_store().change(material_id, ("uploaded", "cancelled", "failed", "awaiting_review", "ready_to_publish", "published"), {"deletedAt": time.time()})
    return jsonify(deleted=True, runtimeUnchanged=True)


@course_materials.post("/materials/<material_id>/restore")
def restore_material(material_id):
    record = get_store().get(material_id)
    if not visible(record):
        raise MaterialError("Material not found.", 404)
    record = get_store().change(material_id, (record["status"],), {"deletedAt": None})
    return jsonify(material=public_record(record))


@course_materials.post("/materials")
def create_material():
    snapshot = source_snapshot()
    incoming = request.get_json(silent=True)
    if isinstance(incoming, dict):
        incoming = {k: v for k, v in incoming.items() if k not in ("documentId", "id")}
    data = validate_metadata(incoming, snapshot, g.material_courses)
    caps = available()
    if not caps["uploadEnabled"]:
        raise MaterialError("Uploads are unavailable. Configure private S3 uploads or enable loopback-only local development storage.", 503)
    material_id = "ph-" + uuid.uuid4().hex
    now = time.time()
    record = {**data, "id": material_id, "documentId": material_id, "owner": g.material_user,
              "status": "uploading", "storage": caps["uploadMode"], "createdAt": now, "updatedAt": now,
              "attempt": 0, "error": None, "reviewAvailable": False,
              "coursePlansHash": snapshot["coursePlansHash"], "manifestHash": snapshot["manifestHash"],
              "history": [{"status": "uploading", "at": now}]}
    if caps["uploadMode"] == "s3":
        record["objectKey"] = f"prompthive/uploads/{material_id}/source.pdf"
        try:
            upload = s3_client().generate_presigned_post(
                Bucket=os.environ["COURSE_MATERIALS_UPLOAD_BUCKET"], Key=record["objectKey"],
                Fields={"Content-Type": "application/pdf"},
                Conditions=[{"Content-Type": "application/pdf"}, ["content-length-range", data["fileSize"], data["fileSize"]]], ExpiresIn=600)
            upload = {"method": "POST", "url": upload["url"], "fields": upload["fields"], "expiresIn": 600}
        except Exception:
            raise MaterialError("Could not authorize the S3 upload. Check server storage configuration and permissions.", 503)
    else:
        upload = {"method": "PUT", "url": f"/api/course-materials/materials/{material_id}/file", "expiresIn": None}
    get_store().insert(record)
    return jsonify(material=public_record(record, caps), upload=upload), 201


@course_materials.put("/materials/<material_id>/file")
def upload_local(material_id):
    record = material(material_id)
    if not g.material_local or record["storage"] != "local":
        raise MaterialError("Direct server uploads are only available in local development.", 403)
    if request.mimetype != "application/pdf" or request.content_length != record["fileSize"]:
        raise MaterialError("PDF content type and size must match the upload record.")
    data = request.get_data()
    verify_pdf(data, record["fileSize"])
    store = get_store()
    def save(_record):
        directory = store.directory / material_id
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / "source.pdf.tmp"
        temporary.write_bytes(data)
        temporary.replace(directory / "source.pdf")
        return {"uploadReceived": True}
    record = store.change(material_id, ("uploading",), save)
    return jsonify(material=public_record(record))


@course_materials.post("/materials/<material_id>/complete")
def complete(material_id):
    record = material(material_id)
    if record["status"] == "cancelled" or record.get("cancelRequested"):
        raise MaterialError("This upload was cancelled.", 409)
    if record["status"] == "uploading":
        try:
            data = read_uploaded_pdf(get_store(), record)
        except MaterialError:
            raise
        except Exception:
            raise MaterialError("Could not verify the uploaded PDF. Check storage access and try again.", 503)
        record = get_store().change(material_id, ("uploading",), {"status": "uploaded", "sha256": hashlib.sha256(data).hexdigest(), "etag": record.get("etag")})
    caps = available()
    ingestion = {"queued": record["status"] in ("queued", "extracting", "compiling", "validating")}
    if record["status"] == "uploaded":
        try:
            if not caps["processingEnabled"]:
                raise MaterialError(caps["processingReason"], 503)
            record = queue_record(record)
            ingestion["queued"] = True
        except MaterialError as error:
            # Verification succeeded. A queue/configuration failure must not
            # turn into a transfer failure or require uploading again.
            ingestion.update(error=str(error), queued=False)
        except Exception:
            ingestion.update(error="Upload verified, but queueing failed. Start processing from the material’s actions.", queued=False)
    return jsonify(material=public_record(record), capabilities=caps, uploadVerified=True, ingestion=ingestion)


@course_materials.get("/materials/<material_id>")
def get_material(material_id):
    return jsonify(material=public_record(material(material_id)))


def queue_record(record, expected=("uploaded",)):
    if not available()["processingEnabled"]:
        raise MaterialError(available()["processingReason"], 503)
    snapshot = source_snapshot()
    validate_metadata(record, snapshot, g.material_courses)
    def restart(current):
        (get_store().directory / record["id"] / "cancel-requested").unlink(missing_ok=True)
        return {"status": "queued", "cancelRequested": False, "error": None, "failedStage": None,
                  "attempt": record["attempt"] + 1, "reviewAvailable": False, "approval": None,
                  "coursePlansHash": snapshot["coursePlansHash"], "manifestHash": snapshot["manifestHash"]}
    return get_store().change(record["id"], expected, restart)


@course_materials.post("/materials/<material_id>/process")
def process(material_id):
    return jsonify(material=public_record(queue_record(material(material_id)))), 202


@course_materials.post("/materials/<material_id>/reconcile")
@course_materials.post("/materials/<material_id>/retry")
def retry(material_id):
    record = material(material_id)
    if request.path.endswith("/reconcile") and not publication_recovery(record):
        raise MaterialError("This is an ingestion job. Use Retry ingestion instead.", 409)
    if request.path.endswith("/reconcile") and record["status"] in ("publish_queued", "publishing", "published"):
        return jsonify(material=public_record(record)), 200
    if record.get("failedStage") == "publishing":
        if not available()["publishEnabled"]:
            raise MaterialError(available()["publishReason"], 503)
        if not record.get("approval"):
            raise MaterialError("Review and approve the current content before publishing.", 409)
        record = get_store().change(material_id, ("failed", "publication_uncertain"), {"status": "publish_queued", "error": None})
        return jsonify(material=public_record(record)), 202
    return jsonify(material=public_record(queue_record(record, ("failed", "cancelled")))), 202


def review_directory(record):
    if not record.get("reviewAvailable"):
        raise MaterialError("Review output is not available yet.", 409)
    return get_store().directory / record["id"]


def approval_context(record, result):
    payload = {"documentId": record["documentId"], "title": record["title"], "author": record["author"],
               "documentType": record["documentType"], "visibility": record["visibility"], "courseName": record["courseName"],
               "lessonIds": sorted(record["lessonIds"]), "artifacts": dict(sorted(result["artifacts"].items()))}
    if record.get("newCourse"):
        payload["newCourse"] = record["newCourse"]
    snapshot = source_snapshot()
    return {"approvalFingerprint": hashlib.sha256((json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode()).hexdigest(),
            "baseManifestHash": snapshot["manifestHash"], "baseCoursePlansHash": snapshot["coursePlansHash"]}


def review_output(record):
    # Full verification belongs at the approval boundary, never image requests.
    from .services.course_materials_review import metadata, artifact_file, compiled_file, extraction_version
    directory = review_directory(record)
    result = dict(metadata(directory))
    for rel, expected in result["artifacts"].items():
        artifact_file(directory, rel, expected)
    compiled_file(directory, record, result, verify=False)
    return {**result, "version": extraction_version(result), **approval_context(record, result)}


def page_arguments():
    try:
        section, cursor = int(request.args.get("section", 0)), int(request.args.get("cursor", 0))
        if not (0 <= section <= 10**9 and 0 <= cursor <= 10**9): raise ValueError()
        return section, cursor
    except ValueError:
        raise MaterialError("Invalid review page.")


@course_materials.get("/materials/<material_id>/review")
def review(material_id):
    from .services.course_materials_review import metadata, review_page
    record = material(material_id); directory = review_directory(record)
    section, cursor = page_arguments()
    page = review_page(directory, record, request.args.get("version"), section, cursor)
    return jsonify({**page, **approval_context(record, metadata(directory))})


@course_materials.get("/materials/<material_id>/review/download")
def review_download(material_id):
    from .services.course_materials_review import metadata, extraction_version, compiled_file, asset_snapshot
    record = material(material_id); directory = review_directory(record); meta = metadata(directory)
    if request.args.get("version") != extraction_version(meta):
        raise MaterialError("Extraction version changed. Refresh review.", 409)
    compiled_file(directory, record, meta, verify=False)
    _, snapshot = asset_snapshot(directory, f"compiled/{record['documentId']}.json", meta["compiledHash"])
    response = send_file(snapshot, as_attachment=True, download_name=f"{material_id}.json", mimetype="application/json", max_age=0)
    response.call_on_close(snapshot.close)
    return response


@course_materials.get("/materials/<material_id>/review/notes")
def review_notes(material_id):
    from .services.course_materials_review import metadata, extraction_version, note_fragments
    record = material(material_id); meta = metadata(review_directory(record))
    if request.args.get("version") != extraction_version(meta):
        raise MaterialError("Extraction version changed. Refresh review.", 409)
    _, cursor = page_arguments()
    from itertools import islice
    notes = list(islice(note_fragments(meta), cursor, cursor + 21))
    return jsonify(notes=notes[:20], nextCursor=cursor + 20 if len(notes) > 20 else None)


@course_materials.get("/materials/<material_id>/assets/<path:artifact>")
def review_asset(material_id, artifact):
    from .services.course_materials_review import metadata, asset_snapshot
    record = material(material_id); directory = review_directory(record)
    meta = metadata(directory)
    if not artifact.startswith("assets/") or artifact not in meta["artifacts"]:
        raise MaterialError("Review asset not found.", 404)
    path, snapshot = asset_snapshot(directory, artifact, meta["artifacts"][artifact])
    response = send_file(snapshot, download_name=path.name, conditional=True, etag=meta["artifacts"][artifact], max_age=0,
                     as_attachment=path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif"))
    response.call_on_close(snapshot.close)
    return response


@lru_cache(maxsize=8)
def publication_directory(root):
    return Path(root) / "publication-state"


def publication_recovery(record):
    if record["status"] in ("publish_queued", "publishing", "publication_uncertain", "published") or record.get("publicationJournal") or record.get("activation") == "uncertain":
        return True
    if record.get("publicationPhase") not in (None, "preflight"):
        return True  # A journal once existed: missing backup is not cancellation.
    try:
        directory = publication_directory(str(pipeline_data_root()))
        if (directory / f"{record['documentId']}.json").exists(): return True
        cli = directory / "cli-publication.json"
        if cli.is_file():
            journal = json.loads(cli.read_text())
            return journal.get("sourceDocumentId") == record["documentId"]
        return False
    except (OSError, ValueError):
        return record.get("failedStage") == "publishing"  # Unknown ownership fails closed.


@course_materials.post("/materials/<material_id>/review-again")
def review_again(material_id):
    record = material(material_id)
    if record["status"] != "failed" or record.get("failedStage") != "publishing" or publication_recovery(record):
        raise MaterialError("Only a confirmed pre-journal publication failure can return to review. Reconcile existing journals instead.", 409)
    snapshot = source_snapshot(); validate_metadata(record, snapshot, g.material_courses)
    record = get_store().change(material_id, ("failed",), {"status": "awaiting_review", "approval": None, "failedStage": None, "error": None,
        "coursePlansHash": snapshot["coursePlansHash"], "manifestHash": snapshot["manifestHash"]})
    return jsonify(material=public_record(record))


@course_materials.put("/materials/<material_id>/assignments")
def assignments(material_id):
    record = material(material_id)
    if publication_recovery(record):
        raise MaterialError("Publication recovery owns these assignments. Use Reconcile publication to finish the original approved version; contact the operator if reconciliation reports a conflict.", 409)
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        raise MaterialError("Expected a metadata object.")
    snapshot = source_snapshot()
    metadata = validate_metadata({**record, "lessonIds": data.get("lessonIds")}, snapshot, g.material_courses)
    record = get_store().change(material_id, ("uploaded", "failed", "awaiting_review", "ready_to_publish"), {
        "lessonIds": metadata["lessonIds"], "approval": None,
        "status": "awaiting_review" if record.get("reviewAvailable") else record["status"],
        "coursePlansHash": snapshot["coursePlansHash"], "manifestHash": snapshot["manifestHash"],
    })
    return jsonify(material=public_record(record))


@course_materials.post("/materials/<material_id>/approval")
def approve(material_id):
    record = material(material_id)
    data = request.get_json(silent=True) or {}
    if publication_recovery(record):
        raise MaterialError("Reconcile the original publication before changing approval.", 409)
    package = make_approval(record, data)
    record = get_store().change(material_id, ("awaiting_review", "ready_to_publish"), {"status": "ready_to_publish", "approval": package})
    return jsonify(material=public_record(record), package=package)


def make_approval(record, data):
    if not isinstance(data, dict):
        raise MaterialError("Expected an approval object.")
    review = review_output(record)
    if data.get("reviewed") is not True or data.get("compiledHash") != review["compiledHash"] or data.get("approvalFingerprint") != review["approvalFingerprint"]:
        raise MaterialError("Review this exact extraction and confirm its educational accuracy before preparing publication.")
    if data.get("reviewVersion", data.get("version")) != review["version"]:
        raise MaterialError("Extraction review metadata changed. Refresh and review the current version.", 409)
    if not review["validation"]["ok"] or not record["lessonIds"]:
        raise MaterialError("Validation must pass and at least one lesson must be assigned.", 409)
    snapshot = source_snapshot()
    validate_metadata(record, snapshot, g.material_courses)
    if snapshot["coursePlansHash"] != data.get("baseCoursePlansHash") or snapshot["manifestHash"] != data.get("baseManifestHash"):
        raise MaterialError("OATutor configuration changed during review. Refresh review and approve again.", 409)
    material_id = record["id"]
    package = {"documentId": record["documentId"], "compiledHash": review["compiledHash"], "reviewVersion": review["version"], "approvedBy": g.material_user,
               "fingerprint": review["approvalFingerprint"], "artifacts": review["artifacts"],
               "approvedAt": time.time(), "baseCoursePlansHash": snapshot["coursePlansHash"], "baseManifestHash": snapshot["manifestHash"],
               "manifestEntry": {"source": f"raw/{material_id}.pdf", "compiled": f"compiled/{material_id}.json", "title": record["title"],
                                 "author": record["author"], "course": record["courseName"], "document_type": record["documentType"], "visibility": record["visibility"]},
               "assignmentPatch": {"courseName": record["courseName"], "lessonIds": record["lessonIds"], "operation": "add_document_id", "documentId": material_id},
               "livePublished": False}
    return package


@course_materials.get("/materials/<material_id>/approval")
def approval(material_id):
    record = material(material_id)
    if not record.get("approval"):
        raise MaterialError("A reviewed publication package is not available yet.", 409)
    return jsonify(record["approval"])


@course_materials.post("/materials/<material_id>/publish")
def publish(material_id):
    record = material(material_id)
    if record["status"] in ("published", "publishing", "publish_queued"):
        return jsonify(material=public_record(record)), 200
    if not available()["publishEnabled"]:
        raise MaterialError(available()["publishReason"], 503)
    if publication_recovery(record):
        raise MaterialError("Use Reconcile publication to recover the existing approval.", 409)
    package = make_approval(record, request.get_json(silent=True) or {})
    record = get_store().change(material_id, ("awaiting_review", "ready_to_publish"), {"status": "publish_queued", "approval": package, "error": None, "failedStage": None})
    return jsonify(material=public_record(record)), 202


def create_local_app():
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 26 * 1024 * 1024
    app.secret_key = os.environ.get("COURSE_MATERIALS_SECRET_KEY")
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_DOMAIN=False, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=os.environ.get("COURSE_MATERIALS_LOCAL_DEV") != "1", PERMANENT_SESSION_LIFETIME=28800)
    origins = ["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174", "http://127.0.0.1:5174"] + list(filter(None, os.environ.get("COURSE_MATERIALS_ALLOWED_ORIGINS", "").split(",")))
    CORS(app, origins=origins,
         supports_credentials=True, allow_headers=["Content-Type", "X-Course-Materials-Request"])
    app.register_blueprint(course_materials)
    return app


if __name__ == "__main__":
    # No implicit local auth bypass: the operator must opt in via environment.
    create_local_app().run(host="127.0.0.1", port=int(os.environ.get("COURSE_MATERIALS_PORT", "8082")), debug=False, load_dotenv=False)
