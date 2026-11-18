"""External, restartable SQLite queue consumer. Run separately from Flask/Gunicorn.

Only explicitly queued jobs call cloud APIs. Publication uses the bundled pipeline coordinator.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from abscribe_backend.services.course_materials import (
    ACTIVE_STATES, MaterialStore, capabilities, pipeline_package_root, pipeline_data_root, read_uploaded_pdf,
)
from abscribe_backend.services.course_materials_config import load_config


def run_job(store, record, token):
    if record["status"] == "publishing":
        return run_publication(store, record, token)
    directory = store.directory / record["id"]
    directory.mkdir(parents=True, exist_ok=True)
    stage_path = directory / "stage.json"
    review_path = directory / "review.json"
    stage_path.unlink(missing_ok=True)
    review_path.unlink(missing_ok=True)
    (directory / "processing-error.json").unlink(missing_ok=True)
    child = None
    try:
        if not capabilities()["pipelineConfigured"]:
            raise RuntimeError("Processing configuration is incomplete")
        pdf = read_uploaded_pdf(store, record)
        if hashlib.sha256(pdf).hexdigest() != record["sha256"]:
            raise ValueError("Uploaded source changed after verification")
        documents = directory / "documents"
        (documents / "raw").mkdir(parents=True, exist_ok=True)
        (documents / "raw" / f"{record['id']}.pdf").write_bytes(pdf)
        metadata_path = directory / "input.json"
        metadata_path.write_text(json.dumps(record))
        adapter = Path(__file__).with_name("course_materials_pipeline.mjs")
        env = {**os.environ, "AWS_REGION": os.environ.get("COURSE_MATERIALS_COMPILE_REGION", "us-west-2")}
        # Never allow a server-wide dry-run flag to produce apparently real output.
        env.pop("COMPILER_DRY_RUN", None)
        env["COURSE_MATERIALS_CANCEL_FILE"] = str(directory / "cancel-requested")
        with (directory / "worker.log").open("a") as log:
            child = subprocess.Popen(["node", str(adapter), str(pipeline_package_root()), str(pipeline_data_root()), str(directory)],
                                     stdout=log, stderr=log, env=env, cwd=pipeline_package_root(), start_new_session=True)
            started = time.monotonic()
            last_phase = "extracting"
            while child.poll() is None:
                if not store.heartbeat(record["id"], token):
                    raise RuntimeError("Lost worker lease")
                if time.monotonic() - started > 3600:
                    raise TimeoutError("Processing exceeded the one-hour limit")
                if stage_path.exists():
                    stage = json.loads(stage_path.read_text())["status"]
                    if stage in ACTIVE_STATES and stage != last_phase and not store.get(record["id"]).get("cancelRequested"):
                        store.change(record["id"], ACTIVE_STATES, {"status": stage}, token)
                        last_phase = stage
                time.sleep(2)
            if store.get(record["id"]).get("cancelRequested"):
                marker = pipeline_data_root() / "publication-state/compilation.json"
                if marker.exists() and json.loads(marker.read_text()).get("documentId") == record["id"]:
                    raise RuntimeError("Cancellation requires recovery of interrupted compilation registration.")
                store.change(record["id"], ACTIVE_STATES, {"status": "cancelled", "cancelRequested": False, "approval": None, "error": None}, token)
                return
            if child.returncode != 0 or not review_path.is_file():
                details = directory / "processing-error.json"
                raise RuntimeError(json.loads(details.read_text())["error"] if details.is_file() else "Pipeline failed; see private worker.log")
        review = json.loads(review_path.read_text())
        if review["validation"]["ok"]:
            from .course_materials_review import ensure_index, metadata
            ensure_index(directory, record, metadata(directory))
            store.change(record["id"], ACTIVE_STATES, lambda current: {
                "status": "cancelled" if current.get("cancelRequested") else "awaiting_review",
                "cancelRequested": False, "reviewAvailable": not current.get("cancelRequested"), "error": None}, token)
        else:
            store.change(record["id"], ACTIVE_STATES, {"status": "failed", "reviewAvailable": True,
                         "failedStage": "validating", "error": "Validation failed. Open Review for details before retrying."}, token)
    except Exception as error:
        if child and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        with (directory / "worker.log").open("a") as log:
            log.write(f"\nWorker failure: {error}\n")
        try:
            store.change(record["id"], ACTIVE_STATES, {"status": "failed", "cancelRequested": False, "failedStage": json.loads(stage_path.read_text()).get("status", "extracting") if stage_path.exists() else store.get(record["id"])["status"], "error": str(error)}, token)
        except Exception:
            pass  # A newer lease owns this record; never overwrite it.


def run_publication(store, record, token):
    from .course_materials_auth import approval_authorized
    directory = store.directory / record["id"]
    result_path = directory / "publication-result.json"
    child = None
    try:
        if not capabilities()["publishingConfigured"]:
            raise RuntimeError("Publication configuration is incomplete")
        if not approval_authorized(store, record):
            raise RuntimeError("Approving instructor no longer has permission for this course")
        result_path.unlink(missing_ok=True)
        (directory / "publish-input.json").write_text(json.dumps(record))
        with (directory / "worker.log").open("a") as log:
            child = subprocess.Popen(["node", str(Path(__file__).with_name("course_materials_publish.mjs")), str(pipeline_package_root()), str(pipeline_data_root()), str(directory)], stdout=log, stderr=log, env=os.environ.copy(), cwd=pipeline_package_root(), start_new_session=True)
            started = time.monotonic()
            while child.poll() is None:
                if not store.heartbeat(record["id"], token) or time.monotonic() - started > 3600:
                    raise RuntimeError("Publication interrupted; reconcile the publication journal")
                time.sleep(2)
        if not result_path.is_file():
            raise RuntimeError("Publisher interrupted before returning verification")
        result = json.loads(result_path.read_text())
        if result["ok"]:
            receipt = result.get("publication", {})
            if receipt.get("activation") != "verified" or not receipt.get("version") or not receipt.get("publishedAt") or not receipt.get("manifestHash") or not receipt.get("coursePlansHash") or not receipt.get("artifacts"):
                raise RuntimeError("Publisher returned an incomplete verification receipt")
            store.change(record["id"], ("publishing",), {"status": "published", "publication": result["publication"], "error": None, "failedStage": None}, token)
        else:
            uncertain = result.get("activation") == "uncertain" or record.get("activation") == "uncertain"
            store.change(record["id"], ("publishing",), {"status": "publication_uncertain" if uncertain else "failed", "failedStage": "publishing", "publicationPhase": result.get("phase"), "publicationJournal": result.get("journalExists"), "activation": "uncertain" if uncertain else result.get("activation"),
                         "error": ("Activation may have succeeded. Retry to reconcile verification. " if uncertain else "Publication stopped before lesson activation; content uploads may be partial. ") + result["error"]}, token)
    except Exception as error:
        if child and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try: child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL); child.wait()
        with (directory / "worker.log").open("a") as log:
            log.write(f"\nPublication worker error: {error}\n")
        # After a child starts, activation is ambiguous until the durable journal
        # is reconciled. Do not present this as an ordinary failed upload.
        store.change(record["id"], ("publishing",), {"status": "publication_uncertain" if child or record.get("activation") == "uncertain" else "failed", "activation": "uncertain" if child or record.get("activation") == "uncertain" else "not_activated", "failedStage": "publishing", "error": str(error)}, token)


def main():
    load_config()
    directory = os.environ.get("COURSE_MATERIALS_DATA_DIR") or Path(__file__).resolve().parents[2] / "instance/course-materials"
    store = MaterialStore(directory)
    print("Course-materials worker ready; waiting for explicitly queued jobs.", flush=True)
    last_sync = 0
    while True:
        if time.monotonic() - last_sync > 60:
            try:
                # Read-only S3 configuration fetch; never invokes extraction or publication.
                sync = subprocess.run(["node", str(pipeline_package_root() / "scripts/sync-runtime-config.mjs")],
                    cwd=pipeline_package_root(), env=os.environ.copy(), capture_output=True, text=True, timeout=40)
                if sync.returncode: print("Runtime configuration sync deferred: " + sync.stderr.strip(), flush=True)
            except (OSError, subprocess.SubprocessError) as error:
                print("Runtime configuration sync unavailable: " + str(error), flush=True)
            last_sync = time.monotonic()
        store.heartbeat()
        claimed = store.claim()
        if claimed:
            run_job(store, *claimed)
        else:
            time.sleep(3)


if __name__ == "__main__":
    main()
