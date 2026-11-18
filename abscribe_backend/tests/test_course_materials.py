"""Local API/queue tests. All extraction outputs here are explicit TEST fixtures."""
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from abscribe_backend import course_materials_api as api
from abscribe_backend.services.course_materials import MaterialError, MaterialStore, source_snapshot

PDF = b"%PDF-1.4\n% TEST ONLY: minimal upload signature fixture\n%%EOF\n"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "pipeline"
    plans = root / "coursePlans.json"
    manifest = root / "documents/manifest.json"
    plans.parent.mkdir(parents=True)
    manifest.parent.mkdir(parents=True)
    plans.write_text(json.dumps([
        {"courseName": "Test Algebra", "lessons": [{"id": "lesson-a", "name": "Algebra A"}, {"id": "lesson-a", "name": "Variant A"}]},
        {"courseName": "Test Calculus", "lessons": [{"id": "lesson-b", "name": "Calculus B"}]},
        {"courseName": "!!Editor", "editor": True, "lessons": []},
    ]))
    manifest.write_text(json.dumps({"existing": {"compiled": "compiled/existing.json"}}))
    monkeypatch.setenv("COURSE_MATERIALS_PIPELINE_DATA_DIR", str(root))
    monkeypatch.setenv("COURSE_MATERIALS_LOCAL_DEV", "1")
    monkeypatch.setenv("COURSE_MATERIALS_UPLOAD_MODE", "local")
    # Tests must not inherit the operator's enabled live-pilot switches.
    monkeypatch.setenv("COURSE_MATERIALS_PROCESSING_ENABLED", "0")
    monkeypatch.setenv("COURSE_MATERIALS_PUBLISH_ENABLED", "0")
    monkeypatch.setenv("COURSE_MATERIALS_DATA_DIR", str(tmp_path / "data"))
    app = api.create_local_app()
    app.config["TESTING"] = True
    client = app.test_client()
    catalog = client.get("/api/course-materials/catalog").get_json()
    metadata = {"title": "Test reference", "author": "Test author", "filename": "test.pdf", "fileSize": len(PDF),
                "contentType": "application/pdf", "courseId": catalog["courses"][0]["id"], "lessonIds": ["lesson-a"],
                "documentType": "textbook", "visibility": "private_tutor"}
    return SimpleNamespace(client=client, app=app, metadata=metadata, data=tmp_path / "data", plans=plans, manifest=manifest, catalog=catalog)


def create(s):
    response = s.client.post("/api/course-materials/materials", json=s.metadata)
    assert response.status_code == 201
    return response.get_json()["material"]["id"]


def uploaded(s):
    material_id = create(s)
    assert s.client.put(f"/api/course-materials/materials/{material_id}/file", data=PDF, content_type="application/pdf").status_code == 200
    response = s.client.post(f"/api/course-materials/materials/{material_id}/complete", json={})
    assert response.status_code == 200
    assert response.get_json()["material"]["status"] == "uploaded"
    return material_id


def review_fixture(s, material_id):
    # Never used by the application: a TEST-only substitute for a real compiler result.
    store = MaterialStore(s.data)
    compiled = {"document_id": material_id, "sections": [{"section_id": "test-section", "title": "TEST fixture", "notes": [{"note_id": "n1", "text": "A real-shaped test result"}]}]}
    artifact = s.data / material_id / "documents" / "compiled" / f"{material_id}.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(compiled))
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    review = {"documentId": material_id, "compiledHash": digest, "demo": True, "artifacts": {f"compiled/{material_id}.json": digest},
              "compiled": compiled, "validation": {"ok": True, "errors": [], "warnings": ["TEST fixture warning"]}, "uncertainties": []}
    (s.data / material_id / "review.json").write_text(json.dumps(review))
    from abscribe_backend.services.course_materials_review import ensure_index
    ensure_index(s.data / material_id, store.get(material_id), review)
    store.change(material_id, ("uploaded",), {"status": "awaiting_review", "reviewAvailable": True})
    return {**review, **s.client.get(f"/api/course-materials/materials/{material_id}/review").get_json()}


def test_catalog_uses_authoritative_config_and_deduplicates_variants(setup):
    assert len(setup.catalog["courses"]) == 2
    assert len(setup.catalog["courses"][0]["lessons"]) == 1
    assert setup.catalog["capabilities"]["processingEnabled"] is False
    assert setup.catalog["capabilities"]["publishEnabled"] is False
    assert set(setup.catalog["capabilities"]["documentTypes"]) == {"textbook", "discussion_solutions"}


@pytest.mark.parametrize("patch", [
    {"title": ""}, {"title": "x" * 181}, {"author": "x" * 181}, {"filename": "../test.pdf"},
    {"filename": "x.exe"}, {"fileSize": 0}, {"fileSize": True}, {"fileSize": 26 * 1024 * 1024},
    {"contentType": "text/plain"}, {"documentType": "invented"}, {"visibility": "public"},
    {"courseId": "unknown"}, {"lessonIds": ["lesson-b"]}, {"lessonIds": ["lesson-a", "lesson-a"]},
    {"lessonIds": "lesson-a"}, {"lessonIds": [{}]},
])
def test_metadata_validation(setup, patch):
    response = setup.client.post("/api/course-materials/materials", json={**setup.metadata, **patch})
    assert response.status_code == 400
    assert response.get_json()["error"]


def test_no_anonymous_production_access(setup, monkeypatch):
    monkeypatch.delenv("COURSE_MATERIALS_LOCAL_DEV")
    assert setup.client.get("/api/course-materials/catalog").status_code == 401
    assert setup.client.post("/api/course-materials/materials", json=setup.metadata).status_code == 401


@pytest.mark.parametrize("kwargs", [
    {"headers": {"Origin": "https://attacker.example"}},
    {"environ_overrides": {"REMOTE_ADDR": "192.0.2.1"}},
    {"base_url": "http://attacker.example"},
])
def test_local_mode_rejects_remote_origins_hosts_and_clients(setup, kwargs):
    assert setup.client.get("/api/course-materials/catalog", **kwargs).status_code == 403


def test_pdf_content_and_upload_transition_validation(setup):
    material_id = create(setup)
    base = f"/api/course-materials/materials/{material_id}"
    assert setup.client.post(base + "/complete", json={}).status_code == 409
    assert setup.client.put(base + "/file", data=b"x" * len(PDF), content_type="application/pdf").status_code == 400
    assert setup.client.put(base + "/file", data=PDF, content_type="text/plain").status_code == 400
    assert setup.client.put(base + "/file", data=PDF[:-1], content_type="application/pdf").status_code == 400


def test_upload_persists_across_app_restart_without_processing(setup):
    material_id = uploaded(setup)
    restarted = api.create_local_app().test_client()
    record = restarted.get(f"/api/course-materials/materials/{material_id}").get_json()["material"]
    assert record["status"] == "uploaded"
    assert record["sha256"]
    assert "owner" not in record and "etag" not in record
    base = f"/api/course-materials/materials/{material_id}"
    assert restarted.post(base + "/process", json={}).status_code == 503
    assert restarted.put(base + "/file", data=PDF, content_type="application/pdf").status_code == 409
    assert restarted.post(base + "/complete", json={}).status_code == 200
    assert restarted.post(base + "/publish", json={}).status_code == 503
    assert MaterialStore(setup.data).get(material_id)["status"] == "uploaded"


def test_unavailable_uploads_do_not_create_records(setup, monkeypatch):
    monkeypatch.setenv("COURSE_MATERIALS_UPLOAD_MODE", "disabled")
    assert setup.client.post("/api/course-materials/materials", json=setup.metadata).status_code == 503
    assert MaterialStore(setup.data).list() == []


def test_job_claim_is_durable_exclusive_and_expiry_requires_retry(setup, monkeypatch):
    material_id = uploaded(setup)
    monkeypatch.setattr(api, "available", lambda: {**setup.catalog["capabilities"], "processingEnabled": True})
    base = f"/api/course-materials/materials/{material_id}"
    assert setup.client.post(base + "/process", json={}).status_code == 202
    assert setup.client.post(base + "/process", json={}).status_code == 409
    first = MaterialStore(setup.data)
    record, token = first.claim()
    second = MaterialStore(setup.data)
    assert second.claim() is None
    assert second.get(material_id)["status"] == "extracting"
    with pytest.raises(MaterialError):
        second.change(material_id, ("extracting",), {"status": "failed"}, token="stale-owner")
    with first.connect() as db:
        db.execute("UPDATE materials SET lease=? WHERE id=?", (time.time() - 100, material_id))
    assert second.claim() is None
    assert second.get(material_id)["status"] == "failed"
    assert setup.client.post(base + "/retry", json={}).status_code == 202
    assert second.get(material_id)["attempt"] == 2


def test_review_approval_is_explicit_and_never_publishes_or_changes_repo(setup):
    material_id = uploaded(setup)
    reviewed = review_fixture(setup, material_id)
    original_plans = setup.plans.read_bytes()
    original_manifest = setup.manifest.read_bytes()
    base = f"/api/course-materials/materials/{material_id}"
    assert setup.client.post(base + "/approval", json={"reviewed": False, "compiledHash": "test-compiled-hash"}).status_code == 400
    assert setup.client.post(base + "/approval", json={"reviewed": True, "compiledHash": "wrong-version"}).status_code == 400
    response = setup.client.post(base + "/approval", json={**reviewed, "reviewed": True})
    assert response.status_code == 200
    assert response.get_json()["package"]["livePublished"] is False
    assert response.get_json()["package"]["assignmentPatch"]["operation"] == "add_document_id"
    assert response.get_json()["material"]["status"] == "ready_to_publish"
    assert setup.client.post(base + "/publish", json={}).status_code == 503
    assert setup.plans.read_bytes() == original_plans
    assert setup.manifest.read_bytes() == original_manifest


def test_stale_allowlist_blocks_approval(setup):
    material_id = uploaded(setup)
    reviewed = review_fixture(setup, material_id)
    setup.manifest.write_text("{}")
    response = setup.client.post(f"/api/course-materials/materials/{material_id}/approval", json={**reviewed, "reviewed": True})
    assert response.status_code == 409


def test_assignment_changes_invalidate_approval(setup):
    material_id = uploaded(setup)
    reviewed = review_fixture(setup, material_id)
    base = f"/api/course-materials/materials/{material_id}"
    setup.client.post(base + "/approval", json={**reviewed, "reviewed": True})
    assert setup.client.put(base + "/assignments", json={"lessonIds": ["lesson-b"]}).status_code == 400
    response = setup.client.put(base + "/assignments", json={"lessonIds": []})
    assert response.status_code == 200
    assert response.get_json()["material"]["status"] == "awaiting_review"
    assert response.get_json()["material"]["approval"] is None
    assert setup.client.post(base + "/approval", json={**reviewed, "reviewed": True}).status_code == 400


def test_instructor_permissions_and_origin_protection(setup, monkeypatch):
    import flask_security
    monkeypatch.delenv("COURSE_MATERIALS_LOCAL_DEV")
    user = SimpleNamespace(is_authenticated=True, is_active=True, has_role=lambda role: role == "instructor", get_id=lambda: "teacher-1")
    monkeypatch.setattr(flask_security, "current_user", user)
    monkeypatch.setenv("COURSE_MATERIALS_INSTRUCTOR_COURSES", json.dumps({"teacher-1": ["Test Calculus"]}))
    catalog = setup.client.get("/api/course-materials/catalog").get_json()
    assert [c["name"] for c in catalog["courses"]] == ["Test Calculus"]
    headers = {"Origin": "http://localhost", "X-Course-Materials-Request": "1"}
    assert setup.client.post("/api/course-materials/materials", json=setup.metadata, headers=headers).status_code == 403
    assert setup.client.post("/api/course-materials/materials", json=setup.metadata).status_code == 403


def test_s3_presign_exact_size_no_credentials_exposed(setup, monkeypatch):
    class FakeS3:
        def generate_presigned_post(self, **kwargs):
            assert ["content-length-range", len(PDF), len(PDF)] in kwargs["Conditions"]
            assert kwargs["ExpiresIn"] == 600
            assert kwargs["Key"].startswith("prompthive/uploads/ph-")
            return {"url": "https://TEST-ONLY.invalid/upload", "fields": {"key": kwargs["Key"]}}
    monkeypatch.setenv("COURSE_MATERIALS_UPLOAD_BUCKET", "TEST-ONLY")
    monkeypatch.setattr(api, "available", lambda: {**setup.catalog["capabilities"], "uploadMode": "s3", "uploadEnabled": True})
    monkeypatch.setattr(api, "s3_client", lambda: FakeS3())
    response = setup.client.post("/api/course-materials/materials", json=setup.metadata)
    assert response.status_code == 201
    assert response.get_json()["upload"]["method"] == "POST"
    assert "objectKey" not in response.get_json()["material"]


def test_malformed_and_missing_resources_return_clear_errors(setup):
    assert setup.client.get("/api/course-materials/materials/missing").status_code == 404
    assert setup.client.post("/api/course-materials/materials", json=[]).status_code == 400
    material_id = uploaded(setup)
    assert setup.client.get(f"/api/course-materials/materials/{material_id}/review").status_code == 409
    assert setup.client.put(f"/api/course-materials/materials/{material_id}/assignments", json=[1]).status_code == 400


def test_course_scope_protects_individual_records_and_review(setup, monkeypatch):
    import flask_security
    material_id = uploaded(setup)
    monkeypatch.delenv("COURSE_MATERIALS_LOCAL_DEV")
    monkeypatch.setattr(flask_security, "current_user", SimpleNamespace(
        is_authenticated=True, is_active=True, has_role=lambda role: role == "instructor", get_id=lambda: "teacher-2"))
    monkeypatch.setenv("COURSE_MATERIALS_INSTRUCTOR_COURSES", json.dumps({"teacher-2": ["Test Calculus"]}))
    assert setup.client.get("/api/course-materials/materials").get_json()["materials"] == []
    assert setup.client.get(f"/api/course-materials/materials/{material_id}").status_code == 404
    assert setup.client.get(f"/api/course-materials/materials/{material_id}/review").status_code == 404


@pytest.mark.parametrize("success", [True, False])
def test_external_worker_records_result_without_flask_request(setup, monkeypatch, success):
    from abscribe_backend.services import course_materials_worker as worker
    material_id = uploaded(setup)
    store = MaterialStore(setup.data)
    store.change(material_id, ("uploaded",), {"status": "queued", "attempt": 1})
    record, token = store.claim()
    monkeypatch.setenv("COMPILER_DRY_RUN", "1")
    monkeypatch.setattr(worker, "capabilities", lambda: {"pipelineConfigured": True})
    def fake_process(command, **kwargs):
        assert command[0] == "node"
        assert command[2] == str(worker.pipeline_package_root())
        assert command[3] == str(setup.plans.parent)
        assert kwargs["cwd"] == worker.pipeline_package_root()
        assert command[-1] == str(setup.data / material_id)
        assert "COMPILER_DRY_RUN" not in kwargs["env"]
        if success:
            (setup.data / material_id / "review.json").write_text(json.dumps({"validation": {"ok": True}, "compiled": {"document_id": material_id, "sections": [{"section_id": "test-section", "notes": [{"note_id": "n1", "text": "Fixture"}]}]}, "demo": True}))
        if success:
            directory = setup.data / material_id
            result = json.loads((directory / "review.json").read_text())
            compiled = directory / "documents/compiled" / f"{material_id}.json"
            compiled.parent.mkdir(parents=True, exist_ok=True); compiled.write_text(json.dumps(result["compiled"]))
            result["compiledHash"] = hashlib.sha256(compiled.read_bytes()).hexdigest()
            result["artifacts"] = {f"compiled/{material_id}.json": result["compiledHash"]}
            (directory / "review.json").write_text(json.dumps(result))
        return SimpleNamespace(poll=lambda: 0 if success else 1, returncode=0 if success else 1)
    monkeypatch.setattr(worker.subprocess, "Popen", fake_process)
    worker.run_job(store, record, token)
    result = MaterialStore(setup.data).get(material_id)
    assert result["status"] == ("awaiting_review" if success else "failed")
    assert result["reviewAvailable"] is success


def test_upload_completion_automatically_queues(setup, monkeypatch):
    material_id = create(setup)
    base = f"/api/course-materials/materials/{material_id}"
    setup.client.put(base + "/file", data=PDF, content_type="application/pdf")
    monkeypatch.setattr(api, "available", lambda: {**setup.catalog["capabilities"], "processingEnabled": True})
    response = setup.client.post(base + "/complete", json={})
    assert response.status_code == 200
    assert response.json["uploadVerified"] is True
    assert response.json["material"]["status"] == "queued"
    assert setup.client.post(base + "/complete", json={}).json["material"]["status"] == "queued"


def test_verified_upload_survives_queue_failure(setup, monkeypatch):
    material_id = create(setup)
    base = f"/api/course-materials/materials/{material_id}"
    setup.client.put(base + "/file", data=PDF, content_type="application/pdf")
    monkeypatch.setattr(api, "available", lambda: {**setup.catalog["capabilities"], "processingEnabled": True})
    def fail(*args, **kwargs): raise MaterialError("Queue unavailable", 503)
    monkeypatch.setattr(api, "queue_record", fail)
    response = setup.client.post(base + "/complete", json={})
    assert response.status_code == 200 and response.json["uploadVerified"]
    assert response.json["material"]["status"] == "uploaded"
    assert "Queue unavailable" in response.json["ingestion"]["error"]


def test_publish_queues_exact_approval_and_never_claims_success(setup, monkeypatch):
    material_id = uploaded(setup)
    reviewed = review_fixture(setup, material_id)
    base = f"/api/course-materials/materials/{material_id}"
    monkeypatch.setattr(api, "available", lambda: {**setup.catalog["capabilities"], "publishEnabled": True})
    response = setup.client.post(base + "/publish", json={**reviewed, "reviewed": True})
    assert response.status_code == 202
    assert response.json["material"]["status"] == "publish_queued"
    assert setup.client.post(base + "/publish", json={}).json["material"]["status"] == "publish_queued"


def test_changed_artifact_invalidates_review_and_approval(setup):
    material_id = uploaded(setup)
    reviewed = review_fixture(setup, material_id)
    artifact = setup.data / material_id / "documents/compiled" / f"{material_id}.json"
    artifact.write_text("{}")
    assert setup.client.post(f"/api/course-materials/materials/{material_id}/approval", json={**reviewed, "reviewed": True}).status_code == 409


def test_capability_separation(setup):
    caps = setup.client.get("/api/course-materials/catalog").json["capabilities"]
    assert caps["storageConfigured"] and not caps["workerOnline"]
    assert not caps["pipelineConfigured"] and not caps["publishingConfigured"]
    assert caps["processingMissing"] and caps["publishingMissing"]
    assert caps["processingSwitchEnabled"] is False and caps["publishSwitchEnabled"] is False
    assert caps["publicationScope"] == "local-artifacts-and-shared-runtime-config"


def test_provisioned_login_scopes_revocation_and_csrf(setup, monkeypatch):
    from werkzeug.security import generate_password_hash
    from abscribe_backend.services.course_materials_auth import ensure_accounts
    monkeypatch.delenv("COURSE_MATERIALS_LOCAL_DEV")
    monkeypatch.setenv("COURSE_MATERIALS_SECRET_KEY", "test-only-secret-" * 4)
    setup.app.secret_key = "test-only-secret-" * 4
    store = MaterialStore(setup.data); ensure_accounts(store)
    with store.connect() as db:
        db.execute("INSERT INTO instructors VALUES (?,?,?,1)", ("teacher@example.test", generate_password_hash("test password"), json.dumps(["Test Calculus"])))
    headers = {"Origin": "http://localhost", "X-Course-Materials-Request": "1"}
    assert setup.client.post("/api/course-materials/session", json={"email": "teacher@example.test", "password": "test password"}).status_code == 403
    assert setup.client.post("/api/course-materials/session", headers=headers, json={"email": "teacher@example.test", "password": "wrong"}).status_code == 401
    assert setup.client.post("/api/course-materials/session", headers=headers, json={"email": "teacher@example.test", "password": "test password"}).status_code == 200
    assert [c["name"] for c in setup.client.get("/api/course-materials/catalog").json["courses"]] == ["Test Calculus"]
    assert setup.client.post("/api/course-materials/materials", headers=headers, json=setup.metadata).status_code == 403
    with store.connect() as db: db.execute("UPDATE instructors SET active=0")
    assert setup.client.get("/api/course-materials/catalog").status_code == 401


@pytest.mark.parametrize("outcome,expected", [("verified", "published"), ("uncertain", "publication_uncertain"), ("not_activated", "failed")])
def test_publication_worker_requires_verified_result(setup, monkeypatch, outcome, expected):
    from abscribe_backend.services import course_materials_worker as worker
    material_id = uploaded(setup)
    review_fixture(setup, material_id)
    store = MaterialStore(setup.data)
    store.change(material_id, ("awaiting_review",), {"status": "publish_queued", "approval": {"approvedBy": "local-instructor"}})
    record, token = store.claim()
    monkeypatch.setattr(worker, "capabilities", lambda: {"publishingConfigured": True})
    def fake_process(command, **kwargs):
        result = {"ok": outcome == "verified", "publication": {"activation": "verified", "version": "test", "publishedAt": "2026-10-15T00:00:00Z", "manifestHash": "test-hash", "coursePlansHash": "test-hash", "artifacts": {"test": "test-hash"}}, "activation": outcome, "phase": "activating", "error": "TEST injected failure"}
        (setup.data / material_id / "publication-result.json").write_text(json.dumps(result))
        return SimpleNamespace(poll=lambda: 0, returncode=0)
    monkeypatch.setattr(worker.subprocess, "Popen", fake_process)
    worker.run_job(store, record, token)
    assert store.get(material_id)["status"] == expected
    if expected != "published": assert "publication" not in store.get(material_id)


def test_worker_configuration_mismatch_is_not_ready(setup, monkeypatch):
    store = MaterialStore(setup.data); store.heartbeat()
    assert store.worker_online()
    monkeypatch.setenv("COURSE_MATERIALS_PUBLISH_ENABLED", "1")
    assert not store.worker_online()


def test_api_and_worker_share_explicit_environment_file(tmp_path, monkeypatch):
    from abscribe_backend.services.course_materials_config import load_config
    config = tmp_path / "materials.env"
    config.write_text("COURSE_MATERIALS_TEST_CONFIG=loaded\n")
    monkeypatch.setenv("COURSE_MATERIALS_ENV_FILE", str(config))
    monkeypatch.delenv("COURSE_MATERIALS_TEST_CONFIG", raising=False)
    assert load_config() == str(config)
    assert __import__("os").environ["COURSE_MATERIALS_TEST_CONFIG"] == "loaded"
    monkeypatch.delenv("COURSE_MATERIALS_TEST_CONFIG")


def test_review_assets_are_hash_checked_and_scoped(setup):
    material_id = uploaded(setup)
    reviewed = review_fixture(setup, material_id)
    artifact = f"assets/figures/{material_id}/test.png"
    path = setup.data / material_id / "documents" / artifact
    path.parent.mkdir(parents=True); path.write_bytes(b"TEST fixture asset")
    reviewed["artifacts"][artifact] = hashlib.sha256(path.read_bytes()).hexdigest()
    (setup.data / material_id / "review.json").write_text(json.dumps(reviewed))
    base = f"/api/course-materials/materials/{material_id}/assets/"
    assert setup.client.get(base + artifact).status_code == 200
    assert setup.client.get(base + f"compiled/{material_id}.json").status_code == 404
    path.write_bytes(b"changed")
    assert setup.client.get(base + artifact).status_code == 409


def test_weak_legacy_secret_cannot_authenticate_provisioned_user(setup, monkeypatch):
    from abscribe_backend.services.course_materials_auth import ensure_accounts
    monkeypatch.delenv("COURSE_MATERIALS_LOCAL_DEV")
    monkeypatch.delenv("COURSE_MATERIALS_SECRET_KEY", raising=False)
    setup.app.secret_key = "Not actually very secret."
    store = MaterialStore(setup.data); ensure_accounts(store)
    with store.connect() as db:
        db.execute("INSERT INTO instructors VALUES (?,?,?,1)", ("test@example.test", "unused", json.dumps(["Test Algebra"])))
    with setup.client.session_transaction() as sess: sess["course_materials_instructor"] = "test@example.test"
    assert setup.client.get("/api/course-materials/catalog").status_code == 401


def test_publication_failure_freezes_assignments_and_reconciles_original(setup, monkeypatch):
    material_id = uploaded(setup); reviewed = review_fixture(setup, material_id)
    base = f"/api/course-materials/materials/{material_id}"
    monkeypatch.setattr(api, 'available', lambda: {**setup.catalog['capabilities'], 'publishEnabled': True})
    assert setup.client.post(base + '/publish', json={**reviewed, 'reviewed': True}).status_code == 202
    store = MaterialStore(setup.data); approved = store.get(material_id)['approval']
    store.change(material_id, ('publish_queued',), {'status':'failed','failedStage':'publishing','publicationPhase':'content_uploaded'})
    assert setup.client.put(base + '/assignments', json={'lessonIds':[]}).status_code == 409
    assert setup.client.post(base + '/approval', json={**reviewed,'reviewed':True}).status_code == 409
    assert store.get(material_id)['approval'] == approved
    assert setup.client.post(base + '/reconcile', json={}).status_code == 202
    assert store.get(material_id)['approval'] == approved
    assert store.get(material_id)['lessonIds'] == ['lesson-a']


def test_ingestion_failure_can_still_edit_assignments(setup):
    material_id = uploaded(setup); store = MaterialStore(setup.data)
    store.change(material_id, ('uploaded',), {'status':'failed','failedStage':'extracting'})
    assert setup.client.put(f'/api/course-materials/materials/{material_id}/assignments',json={'lessonIds':[]}).status_code == 200


def test_single_image_only_hashes_requested_asset(setup, monkeypatch):
    from abscribe_backend.services import course_materials_review as reviews
    material_id = uploaded(setup); reviewed = review_fixture(setup, material_id); directory=setup.data / material_id
    for name in ['one','unrelated']:
        rel=f'assets/figures/{material_id}/{name}.png'; p=directory / 'documents' / rel; p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(name.encode());reviewed['artifacts'][rel]=hashlib.sha256(p.read_bytes()).hexdigest()
    (directory / 'review.json').write_text(json.dumps({k:v for k,v in reviewed.items() if k != 'compiled'}))
    original=reviews.file_hash; touched=[]
    def track(path, **kwargs): touched.append(path.name); return original(path, **kwargs)
    monkeypatch.setattr(reviews,'file_hash',track)
    monkeypatch.setattr(api,'source_snapshot',lambda: (_ for _ in ()).throw(AssertionError('Asset loaded catalog')))
    response=setup.client.get(f'/api/course-materials/materials/{material_id}/assets/assets/figures/{material_id}/one.png')
    assert response.status_code == 200 and touched == ['one.png']
    assert setup.client.get(f'/api/course-materials/materials/{material_id}/assets/assets/not-reviewed.png').status_code == 404


def test_huge_single_learning_object_is_indexed_and_served_in_bounded_pages(setup, monkeypatch):
    from abscribe_backend.services import course_materials_review as reviews
    material_id=uploaded(setup);store=MaterialStore(setup.data);directory=setup.data/material_id
    compiled={'document_id':material_id,'sections':[{'section_id':'huge','title':'Huge section','problems':[{'problem_id':'huge-problem','prompt':'Representative long reference. '*100000,'solution':{'text':'Complete solution at the end.'}}]}]}
    path=directory/'documents/compiled'/f'{material_id}.json';path.parent.mkdir(parents=True);path.write_text(json.dumps(compiled));digest=hashlib.sha256(path.read_bytes()).hexdigest()
    meta={'compiledHash':digest,'artifacts':{f'compiled/{material_id}.json':digest},'validation':{'ok':True,'errors':[],'warnings':['WARN '*1400]},'uncertainties':[]}
    (directory/'review.json').write_text(json.dumps(meta));reviews.ensure_index(directory,store.get(material_id),reviews.metadata(directory));store.change(material_id,('uploaded',),{'status':'awaiting_review','reviewAvailable':True})
    monkeypatch.setattr(reviews,'ensure_index',lambda *a: (_ for _ in ()).throw(AssertionError('Index built in Flask')))
    monkeypatch.setattr(reviews,'file_hash',lambda *a: (_ for _ in ()).throw(AssertionError('Full document hashed while paging')))
    base=f'/api/course-materials/materials/{material_id}/review'
    import tracemalloc
    tracemalloc.start();first=setup.client.get(base);_,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
    assert peak < 2 * 1024 * 1024
    assert first.status_code==200 and len(first.data)<100000
    page=first.json;assert 'compiled' not in page and 'artifacts' not in page and len(page['items'])<=20
    assert max(len(item['text']) for item in page['items'])<=3000
    second=setup.client.get(base,query_string={'version':page['version'],'section':0,'cursor':page['nextCursor']}).json
    assert second['items'][0]['id'] > page['items'][-1]['id'] and second['version']==page['version']
    notes=setup.client.get(base+'/notes',query_string={'version':page['version']}).json
    assert ''.join(notes['notes'])=='WARN '*1400
    meta['artifacts']['assets/new.png']='new-hash';(directory/'review.json').write_text(json.dumps(meta))
    assert setup.client.get(base,query_string={'version':page['version']}).status_code==409


def test_cursor_input_and_explicit_download_version_validation(setup):
    material_id=uploaded(setup);review_fixture(setup,material_id);base=f'/api/course-materials/materials/{material_id}/review'
    assert setup.client.get(base+'?cursor=-1').status_code==400
    assert setup.client.get(base+'?section=not-an-integer').status_code==400
    assert setup.client.get(base+'/download?version=stale').status_code==409
    version=setup.client.get(base).json['version']
    result=setup.client.get(base+'/download',query_string={'version':version})
    assert result.status_code==200 and 'attachment' in result.headers['Content-Disposition']


def test_hosted_https_origin_session_images_upload_publish_logout_and_revocation(setup, monkeypatch):
    """Models Netlify same-origin proxy forwarding to a separate HTTPS Flask host.
    Public deployment/browser verification remains a separate deployment check.
    """
    from werkzeug.security import generate_password_hash
    from abscribe_backend.services.course_materials_auth import ensure_accounts
    from abscribe_backend.services import course_materials_review as reviews
    monkeypatch.delenv('COURSE_MATERIALS_LOCAL_DEV')
    monkeypatch.setenv('COURSE_MATERIALS_SECRET_KEY','secure-test-only-key-'*3)
    monkeypatch.setenv('COURSE_MATERIALS_ALLOWED_ORIGINS','https://cahlprompthive.netlify.app')
    monkeypatch.setenv('COURSE_MATERIALS_UPLOAD_BUCKET','TEST-ONLY')
    setup.app.secret_key='secure-test-only-key-'*3;setup.app.config.update(SESSION_COOKIE_SECURE=True,SESSION_COOKIE_SAMESITE='Lax')
    store=MaterialStore(setup.data);ensure_accounts(store)
    with store.connect() as db: db.execute('INSERT INTO instructors VALUES (?,?,?,1)',('hosted@example.test',generate_password_hash('test-password'),json.dumps(['Test Algebra'])))
    base='/api/course-materials';url={'base_url':'https://api.example.test'}
    headers={'Origin':'https://cahlprompthive.netlify.app','X-Course-Materials-Request':'1'}
    assert setup.client.post(base+'/session',json={'email':'hosted@example.test','password':'test-password'},headers={**headers,'Origin':'https://attacker.example'},**url).status_code==403
    login=setup.client.post(base+'/session',json={'email':'hosted@example.test','password':'test-password'},headers=headers,**url)
    assert login.status_code==200
    cookie=login.headers['Set-Cookie'];assert 'Secure' in cookie and 'HttpOnly' in cookie and 'SameSite=Lax' in cookie and 'Domain=' not in cookie
    assert setup.client.get(base+'/catalog',headers=headers,**url).status_code==200
    assert setup.client.get(base+'/catalog',headers=headers,**url).status_code==200 # refresh
    class Cloud:
        def generate_presigned_post(self,**kw): return {'url':'https://TEST-ONLY.invalid','fields':{'key':kw['Key']}}
    monkeypatch.setattr(api,'available',lambda:{**setup.catalog['capabilities'],'uploadEnabled':True,'uploadMode':'s3','processingEnabled':True,'publishEnabled':True})
    monkeypatch.setattr(api,'s3_client',lambda:Cloud())
    response=setup.client.post(base+'/materials',json=setup.metadata,headers=headers,**url)
    assert response.status_code==201;material_id=response.json['material']['id'];item=base+'/materials/'+material_id
    monkeypatch.setattr(api,'read_uploaded_pdf',lambda *a:PDF)
    assert setup.client.post(item+'/complete',json={},headers=headers,**url).json['material']['status']=='queued'
    # Test-only compiler output, outside the running UI/cloud.
    store.change(material_id,('queued',),{'status':'uploaded'})
    monkeypatch.setenv('COURSE_MATERIALS_LOCAL_DEV','1');reviewed=review_fixture(SimpleNamespace(**{**setup.__dict__, 'client':setup.app.test_client()}),material_id);monkeypatch.delenv('COURSE_MATERIALS_LOCAL_DEV')
    directory=setup.data/material_id;asset=f'assets/figures/{material_id}/source.png';path=directory/'documents'/asset;path.parent.mkdir(parents=True);path.write_bytes(b'TEST image')
    meta={k:v for k,v in reviewed.items() if k!='compiled'};meta['artifacts'][asset]=hashlib.sha256(path.read_bytes()).hexdigest();(directory/'review.json').write_text(json.dumps(meta));reviews.ensure_index(directory,store.get(material_id),reviews.metadata(directory))
    assert setup.client.get(item+'/assets/'+asset,headers=headers,**url).status_code==200
    reviewed=setup.client.get(item+'/review',headers=headers,**url).json
    assert setup.client.post(item+'/publish',json={**reviewed,'reviewed':True},headers=headers,**url).status_code==202
    logout=setup.client.delete(base+'/session',headers=headers,**url)
    assert logout.status_code==200
    assert setup.client.get(base+'/catalog',headers=headers,**url).status_code==401
    assert setup.client.post(base+'/session',json={'email':'hosted@example.test','password':'test-password'},headers=headers,**url).status_code==200
    with store.connect() as db: db.execute('UPDATE instructors SET active=0')
    assert setup.client.get(item+'/assets/'+asset,headers=headers,**url).status_code==401


def test_historical_recovery_restores_original_approval_without_discarding_journal(setup):
    import subprocess
    from abscribe_backend.services.course_materials_recovery import restore_original
    material_id=uploaded(setup);reviewed=review_fixture(setup,material_id);base=f'/api/course-materials/materials/{material_id}'
    assert setup.client.post(base+'/approval',json={**reviewed,'reviewed':True}).status_code==200
    store=MaterialStore(setup.data);original=store.get(material_id);directory=setup.data/material_id
    (directory/'publish-input.json').write_text(json.dumps(original))
    root=setup.plans.parent
    journal=root/'publication-state'/'cli-publication.json';journal.parent.mkdir();journal.write_text(json.dumps({'sourceDocumentId':material_id,'approvalFingerprint':original['approval']['fingerprint'],'phase':'prepared'}))
    store.change(material_id,('ready_to_publish',),{'status':'failed','failedStage':'publishing','approval':None,'lessonIds':[]})
    assert setup.client.put(base+'/assignments',json={'lessonIds':[]}).status_code==409
    result=restore_original(store,material_id)
    assert result['status']=='publish_queued' and result['approval']==original['approval'] and result['lessonIds']==original['lessonIds']
    assert journal.exists() and list(directory.glob('recovery-record-*.json'))


def test_changed_validation_notes_invalidate_review_version_at_approval(setup):
    material_id=uploaded(setup);reviewed=review_fixture(setup,material_id);path=setup.data/material_id/'review.json'
    meta=json.loads(path.read_text());meta['validation']['warnings'].append('New validation warning');path.write_text(json.dumps(meta))
    response=setup.client.post(f'/api/course-materials/materials/{material_id}/approval',json={**reviewed,'reviewed':True})
    assert response.status_code==409


def test_host_only_session_cookie_even_when_server_name_is_configured(setup, monkeypatch):
    from flask.sessions import SecureCookieSessionInterface
    setup.app.config['SERVER_NAME']='api.example.test'
    assert SecureCookieSessionInterface().get_cookie_domain(setup.app) is None


def test_cursors_require_version_and_valid_section_membership(setup):
    material_id=uploaded(setup);reviewed=review_fixture(setup,material_id);base=f'/api/course-materials/materials/{material_id}/review'
    assert setup.client.get(base+'?cursor=1').status_code==400
    assert setup.client.get(base+'?cursor=9999999999999999999999999').status_code==400
    assert setup.client.get(base,query_string={'version':reviewed['version'],'cursor':999999}).status_code==400


def test_review_image_stream_is_an_immutable_verified_snapshot(setup, monkeypatch):
    from abscribe_backend.services import course_materials_review as reviews
    material_id=uploaded(setup);reviewed=review_fixture(setup,material_id);directory=setup.data/material_id
    rel=f'assets/figures/{material_id}/snapshot.png';path=directory/'documents'/rel;path.parent.mkdir(parents=True);path.write_bytes(b'approved bytes')
    reviewed['artifacts'][rel]=hashlib.sha256(path.read_bytes()).hexdigest();(directory/'review.json').write_text(json.dumps(reviewed))
    original=reviews.file_hash
    def swap_after_hash(path,**kwargs):
        digest=original(path,**kwargs);path.write_bytes(b'changed after hash');return digest
    monkeypatch.setattr(reviews,'file_hash',swap_after_hash)
    base=f'/api/course-materials/materials/{material_id}/assets/{rel}'
    response=setup.client.get(base)
    assert response.status_code==200 and response.data==b'approved bytes'
    assert setup.client.get(base).status_code==409


def test_pre_journal_publication_failure_can_explicitly_return_to_review(setup):
    import subprocess
    material_id=uploaded(setup);reviewed=review_fixture(setup,material_id);store=MaterialStore(setup.data)
    subprocess.run(['git','init','-q',str(setup.plans.parents[3])],check=True)
    store.change(material_id,('awaiting_review',),{'status':'failed','failedStage':'publishing','publicationPhase':'preflight','activation':'not_activated','publicationJournal':False,'approval':{'fingerprint':'stale'}})
    base=f'/api/course-materials/materials/{material_id}'
    assert setup.client.get(base).json['material']['actions']['reviewAgain']
    response=setup.client.post(base+'/review-again',json={})
    assert response.status_code==200 and response.json['material']['status']=='awaiting_review' and response.json['material']['approval'] is None


def test_uncertain_or_missing_previously_existing_journal_cannot_return_to_review(setup):
    material_id=uploaded(setup);review_fixture(setup,material_id);store=MaterialStore(setup.data)
    store.change(material_id,('awaiting_review',),{'status':'failed','failedStage':'publishing','publicationPhase':'content_uploaded','activation':'not_activated'})
    assert setup.client.post(f'/api/course-materials/materials/{material_id}/review-again',json={}).status_code==409


def test_distinct_material_jobs_are_serialized(setup):
    first_id = uploaded(setup)
    second_id = uploaded(setup)
    store = MaterialStore(setup.data)
    for material_id in (first_id, second_id):
        store.change(material_id, ("uploaded",), {"status": "queued"})
    first, token = store.claim()
    assert MaterialStore(setup.data).claim() is None
    store.change(first["id"], ("extracting",), {"status": "failed"}, token)
    assert store.claim()[0]["id"] != first["id"]


def test_cli_journal_freezes_original_approval_and_assignments(setup, monkeypatch, tmp_path):
    material_id = uploaded(setup)
    review_fixture(setup, material_id)
    directory = tmp_path / "publication-state"
    directory.mkdir()
    (directory / "cli-publication.json").write_text(json.dumps({"sourceDocumentId": material_id, "phase": "prepared"}))
    monkeypatch.setattr(api, "publication_directory", lambda root: directory)
    store = MaterialStore(setup.data)
    store.change(material_id, ("awaiting_review",), {"status": "failed", "failedStage": "publishing", "approval": {"original": True}})
    assert api.publication_recovery(store.get(material_id))
    response = setup.client.put(f"/api/course-materials/materials/{material_id}/assignments", json={"lessonIds": []})
    assert response.status_code == 409
    assert store.get(material_id)["approval"] == {"original": True}


def test_runtime_catalog_and_readiness_ignore_oatutor_checkout(setup, monkeypatch):
    from abscribe_backend.services.course_materials import pipeline_package_root, pipeline_data_root, capabilities
    monkeypatch.setenv("OATUTOR_REPO_PATH", "/nonexistent/must-not-be-read")
    assert pipeline_data_root() == setup.plans.parent
    assert pipeline_package_root().name == "course_materials_pipeline"
    assert pipeline_package_root().is_relative_to(__import__('pathlib').Path(__file__).resolve().parents[1])
    assert source_snapshot()["manifest"]["existing"]
    assert setup.client.get("/api/course-materials/catalog").status_code == 200
    assert not any("OATUTOR_REPO_PATH" in item for item in capabilities()["processingMissing"] + capabilities()["publishingMissing"])


def test_cancel_queued_job_cannot_be_claimed(setup):
    id = uploaded(setup); store = MaterialStore(setup.data)
    store.change(id, ("uploaded",), {"status": "queued"})
    response = setup.client.post(f"/api/course-materials/materials/{id}/cancel", json={})
    assert response.status_code == 202 and response.json["material"]["status"] == "cancelled"
    assert store.claim() is None
    assert setup.client.post(f"/api/course-materials/materials/{id}/complete", json={}).status_code == 409


def test_cancelled_job_can_restart_without_stale_cancel_marker(setup, monkeypatch):
    id = uploaded(setup); store = MaterialStore(setup.data)
    store.change(id, ("uploaded",), {"status": "queued"})
    setup.client.post(f"/api/course-materials/materials/{id}/cancel", json={})
    monkeypatch.setattr(api, "available", lambda: {**setup.catalog["capabilities"], "processingEnabled": True})
    response = setup.client.post(f"/api/course-materials/materials/{id}/retry", json={})
    assert response.status_code == 202 and response.json["material"]["status"] == "queued"
    assert not (setup.data / id / "cancel-requested").exists()
    assert not response.json["material"]["cancelRequested"]


def test_active_cancel_finishes_through_worker_cleanup(setup, monkeypatch):
    from abscribe_backend.services import course_materials_worker as worker
    id = uploaded(setup); store = MaterialStore(setup.data)
    store.change(id, ("uploaded",), {"status": "queued"}); record, token = store.claim()
    monkeypatch.setattr(worker, "capabilities", lambda: {"pipelineConfigured": True})
    def fake_process(*args, **kwargs):
        assert kwargs["env"]["COURSE_MATERIALS_CANCEL_FILE"].endswith("cancel-requested")
        response = setup.client.post(f"/api/course-materials/materials/{id}/cancel", json={})
        assert response.status_code == 202 and response.json["material"]["cancelRequested"]
        assert setup.client.delete(f"/api/course-materials/materials/{id}").status_code == 409
        return SimpleNamespace(poll=lambda: 1, returncode=1)
    monkeypatch.setattr(worker.subprocess, "Popen", fake_process)
    worker.run_job(store, record, token)
    assert store.get(id)["status"] == "cancelled"
    assert not store.get(id)["cancelRequested"]


@pytest.mark.parametrize("status", ["publishing", "publish_queued", "publication_uncertain"])
def test_cancel_delete_protect_publication_recovery(setup, status):
    id = uploaded(setup); MaterialStore(setup.data).change(id, ("uploaded",), {"status": status})
    base = f"/api/course-materials/materials/{id}"
    assert setup.client.post(base + "/cancel", json={}).status_code == 409
    assert setup.client.delete(base).status_code == 409


@pytest.mark.parametrize("status", ["uploaded", "awaiting_review", "cancelled", "published"])
def test_delete_is_recoverable_and_never_changes_bindings_or_files(setup, status):
    id = uploaded(setup); store = MaterialStore(setup.data)
    store.change(id, ("uploaded",), {"status": status, "publication": {"activation": "verified"} if status == "published" else None})
    before = setup.manifest.read_bytes(), setup.plans.read_bytes()
    base = f"/api/course-materials/materials/{id}"
    assert setup.client.delete(base).json["runtimeUnchanged"] is True
    assert setup.client.get(base).status_code == 404
    assert not setup.client.get("/api/course-materials/materials").json["materials"]
    assert (setup.data / id / "source.pdf").read_bytes() == PDF
    assert before == (setup.manifest.read_bytes(), setup.plans.read_bytes())
    restored = setup.client.post(base + "/restore", json={})
    assert restored.status_code == 200 and restored.json["material"]["status"] == status
    assert len(setup.client.get("/api/course-materials/materials").json["materials"]) == 1


def test_cancel_delete_restore_obey_course_permissions(setup, monkeypatch):
    id = uploaded(setup); monkeypatch.setattr(api, "visible", lambda record: False)
    base = f"/api/course-materials/materials/{id}"
    assert setup.client.delete(base).status_code == 404
    assert setup.client.post(base + "/cancel", json={}).status_code == 404
    assert setup.client.post(base + "/restore", json={}).status_code == 404


def test_delete_preserves_interrupted_compilation(setup):
    id = uploaded(setup); marker = setup.plans.parent / "publication-state/compilation.json"
    marker.parent.mkdir(parents=True); marker.write_text(json.dumps({"documentId": id}))
    assert setup.client.delete(f"/api/course-materials/materials/{id}").status_code == 409
    assert marker.exists()


def test_deleted_library_is_restorable_after_refresh_and_course_scoped(setup, monkeypatch):
    id = uploaded(setup); base = f"/api/course-materials/materials/{id}"
    setup.client.delete(base)
    assert not setup.client.get("/api/course-materials/materials").json["materials"]
    records = setup.client.get("/api/course-materials/materials?includeDeleted=1").json["materials"]
    assert len(records) == 1 and records[0]["deletedAt"]
    monkeypatch.setattr(api, "visible", lambda record: False)
    assert not setup.client.get("/api/course-materials/materials?includeDeleted=1").json["materials"]


@pytest.mark.parametrize("method,suffix", [("post", "/cancel"), ("delete", ""), ("post", "/restore")])
def test_lifecycle_actions_require_hosted_authentication(setup, monkeypatch, method, suffix):
    id = uploaded(setup); monkeypatch.setenv("COURSE_MATERIALS_LOCAL_DEV", "0")
    response = getattr(setup.client, method)(f"/api/course-materials/materials/{id}{suffix}", json={})
    assert response.status_code == 401


def test_interrupted_cancel_request_does_not_leave_permanent_cancelling_status(setup):
    id = uploaded(setup); store = MaterialStore(setup.data)
    store.change(id, ("uploaded",), {"status": "queued"}); store.claim()
    store.change(id, ("extracting",), {"cancelRequested": True})
    with store.connect() as db: db.execute("UPDATE materials SET lease=0 WHERE id=?", (id,))
    assert store.claim() is None
    assert store.get(id)["status"] == "failed"
    assert store.get(id)["cancelRequested"] is False


def test_pending_course_upload_does_not_change_catalog_or_shared_files(setup):
    before = (setup.plans.read_bytes(), setup.manifest.read_bytes())
    setup.metadata.update(courseId="__new__", lessonIds=[], newCourse={"courseName": "New Biology", "lessons": ["Cells", "Ecology"]})
    mid = uploaded(setup)
    record = MaterialStore(setup.data).get(mid)
    assert record["courseName"] == "New Biology"
    assert len(record["lessonIds"]) == 2
    assert all(i.startswith("ph-lesson-") for i in record["lessonIds"])
    assert record["newCourse"]["office_hours"] is True
    assert [l["name"] for l in record["newCourse"]["lessons"]] == ["Cells", "Ecology"]
    assert before == (setup.plans.read_bytes(), setup.manifest.read_bytes())
    assert setup.client.get("/api/course-materials/catalog").get_json()["courses"] == setup.catalog["courses"]
    review = review_fixture(setup, mid)
    changed = {**record, "newCourse": {**record["newCourse"], "courseName": "Changed"}}
    assert api.approval_context(changed, review)["approvalFingerprint"] != review["approvalFingerprint"]


@pytest.mark.parametrize("draft", [
    {"courseName": " test algebra ", "lessons": ["One"]},
    {"courseName": "New", "lessons": []},
    {"courseName": "New", "lessons": ["One", "one"]},
    {"courseName": "!!Hidden", "lessons": ["One"]},
])
def test_invalid_pending_course_rejected(setup, draft):
    setup.metadata.update(newCourse=draft, courseId="__new__")
    assert setup.client.post("/api/course-materials/materials", json=setup.metadata).status_code in (400, 409)


def test_scoped_instructor_cannot_create_course(setup):
    from abscribe_backend.services.course_materials import validate_metadata
    setup.metadata["newCourse"] = {"courseName": "New", "lessons": ["One"]}
    with pytest.raises(MaterialError) as error:
        validate_metadata(setup.metadata, source_snapshot(), ["Test Algebra"])
    assert error.value.status == 403


def test_client_cannot_choose_pending_lesson_ids(setup):
    setup.metadata.update(documentId="spoofed", newCourse={"courseName": "New", "lessons": [{"name": "One", "id": "lesson-a"}]})
    mid = create(setup)
    assert MaterialStore(setup.data).get(mid)["lessonIds"] != ["lesson-a"]


def test_pending_course_can_be_approved_with_exact_definition(setup):
    setup.metadata.update(courseId="__new__", lessonIds=[], newCourse={"courseName": "New Biology", "lessons": ["Cells"]})
    mid = uploaded(setup)
    review = review_fixture(setup, mid)
    response = setup.client.post(f"/api/course-materials/materials/{mid}/approval", json={
        "reviewed": True, "compiledHash": review["compiledHash"], "approvalFingerprint": review["approvalFingerprint"],
        "reviewVersion": review["version"], "baseCoursePlansHash": review["baseCoursePlansHash"], "baseManifestHash": review["baseManifestHash"]})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["package"]["fingerprint"] == review["approvalFingerprint"]
    assert all(c["courseName"] != "New Biology" for c in json.loads(setup.plans.read_text()))
