"""Explicit operator recovery for historical assignment edits that lost approval.

Restores the original approved metadata from the private saved worker input;
never cancels/deletes a journal or changes S3 directly. The usual worker then
rechecks authorization and reconciles remote state under the shared lock.
"""
import hashlib
import argparse
import json
import os
import time
from pathlib import Path
from .course_materials import MaterialStore, MaterialError, pipeline_data_root, source_snapshot, validate_metadata
from .course_materials_auth import approval_authorized


def restore_original(store, material_id, approved_by=None, confirmed=False):
    current = store.get(material_id)
    if current['status'] not in ('failed', 'publication_uncertain', 'awaiting_review', 'ready_to_publish'):
        raise MaterialError('Stop: this record is active or already published.', 409)
    directory = store.directory / material_id
    original = json.loads((directory / 'publish-input.json').read_text())
    state = pipeline_data_root() / 'publication-state'
    path = state / f'{material_id}.json'
    journal = json.loads((path if path.exists() else state / 'cli-publication.json').read_text())
    fingerprint = journal.get('fingerprint', journal.get('approvalFingerprint'))
    if original['id'] != material_id or original['documentId'] != material_id or original.get('approval', {}).get('fingerprint') != fingerprint:
        raise MaterialError('Saved approval does not match this publication journal.', 409)
    payload = {"documentId": original["documentId"], "title": original["title"], "author": original["author"],
               "documentType": original["documentType"], "visibility": original["visibility"], "courseName": original["courseName"],
               "lessonIds": sorted(original["lessonIds"]), "artifacts": dict(sorted(original["approval"]["artifacts"].items()))}
    if original.get("newCourse"):
        payload["newCourse"] = original["newCourse"]
    actual = hashlib.sha256((json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode()).hexdigest()
    if actual != fingerprint:
        raise MaterialError('Saved metadata was changed after approval; restore the original worker input from backup.', 409)
    validate_metadata(original, source_snapshot(), None)
    if approved_by:
        if not confirmed:
            raise MaterialError('An active course instructor must review the exact original extraction; pass --confirm-original-reviewed only after that review.', 409)
        original['approval']['approvedBy'] = approved_by
        original['approval']['approvedAt'] = time.time()
    if not approval_authorized(store, original):
        raise MaterialError('The original approving instructor no longer has course permission.', 403)
    backup = directory / f'recovery-record-{time.time_ns()}.json'
    with backup.open('x') as stream: json.dump(current, stream)
    backup.chmod(0o600)
    fields = ('title', 'author', 'documentType', 'visibility', 'courseName', 'courseId', 'lessonIds', 'approval', 'coursePlansHash', 'manifestHash')
    return store.change(material_id, (current['status'],), {**{key: original[key] for key in fields}, 'status': 'publish_queued', 'failedStage': 'publishing', 'publicationJournal': True, 'error': None})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--restore-original-approval', required=True, metavar='MATERIAL_ID')
    parser.add_argument('--approved-by', help='Optional active instructor identity for a new review of the exact original version')
    parser.add_argument('--confirm-original-reviewed', action='store_true')
    args = parser.parse_args()
    store = MaterialStore(os.environ.get('COURSE_MATERIALS_DATA_DIR') or Path(__file__).resolve().parents[2] / 'instance/course-materials')
    record = restore_original(store, args.restore_original_approval, args.approved_by, args.confirm_original_reviewed)
    print(record['id'], record['status'], '(original approval restored; worker reconciliation required)')


if __name__ == '__main__': main()
