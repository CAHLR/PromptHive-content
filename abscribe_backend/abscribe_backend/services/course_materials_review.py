"""Immutable review metadata plus a disk index of bounded text/asset fragments.

Only approval/publication verifies the complete artifact set. Normal review never
constructs the compiled document in memory; individual assets verify only themselves.
"""
import hashlib
import json
import os
import sqlite3
from functools import lru_cache
from pathlib import Path
import ijson
from ijson.common import ObjectBuilder
from .course_materials import MaterialError

FRAGMENT_CHARS = 1500
PAGE_SIZE = 12
INDEX_FORMAT = 2
MAX_COMPILED_BYTES = 256 * 1024 * 1024


def file_hash(path, copy_to=None):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
            if copy_to is not None: copy_to.write(block)
    return digest.hexdigest()


@lru_cache(maxsize=8)
def _metadata(path, stamp):
    # Legacy review.json duplicated the entire compiled document. Skip its events
    # without constructing that object. New workers omit it entirely.
    result = {}; builder = None; key = None; depth = 0
    with Path(path).open('rb') as stream:
        for prefix, event, value in ijson.parse(stream):
            if prefix == '' and event == 'map_key':
                key = value
                builder = None if key == 'compiled' else ObjectBuilder()
                depth = 0
                continue
            if builder is None or prefix == '':
                continue
            builder.event(event, value)
            if event in ('start_map', 'start_array'): depth += 1
            elif event in ('end_map', 'end_array'): depth -= 1
            if depth == 0 and event != 'map_key':
                result[key] = builder.value; builder = None
    if not result.get('artifacts') or not result.get('compiledHash'):
        raise MaterialError('Retry processing: this extraction has no immutable artifact metadata.', 409)
    return result


def metadata(directory):
    path = directory / 'review.json'
    try:
        stat = path.stat()
        if stat.st_size > MAX_COMPILED_BYTES:
            raise MaterialError('Review metadata exceeds the supported size limit.', 409)
        return _metadata(str(path), (stat.st_ino, stat.st_mtime_ns, stat.st_size))
    except (OSError, ValueError, ijson.JSONError):
        raise MaterialError('Review metadata is unavailable. Retry processing or contact the operator.', 503)


def extraction_version(meta):
    return hashlib.sha256(json.dumps({'format': INDEX_FORMAT, 'compiledHash': meta['compiledHash'], 'artifacts': meta['artifacts'], 'validation': meta.get('validation', {}), 'uncertainties': meta.get('uncertainties', [])}, sort_keys=True).encode()).hexdigest()


def artifact_path(directory, rel):
    root = (directory / 'documents').resolve()
    path = (root / rel).resolve()
    if not rel or '\\' in rel or Path(rel).is_absolute() or '..' in Path(rel).parts or not path.is_relative_to(root) or not path.is_file():
        raise MaterialError('Review artifact not found.', 404)
    return path


def artifact_file(directory, rel, expected):
    path = artifact_path(directory, rel)
    if file_hash(path) != expected:
        raise MaterialError('Extracted content changed. Retry processing and review the new extraction.', 409)
    return path


def asset_snapshot(directory, rel, expected):
    from tempfile import SpooledTemporaryFile
    path = artifact_path(directory, rel)
    snapshot = SpooledTemporaryFile(max_size=1024 * 1024)
    try:
        if file_hash(path, copy_to=snapshot) != expected:
            raise MaterialError('Review asset changed after extraction.', 409)
        snapshot.seek(0)
        return path, snapshot
    except Exception:
        snapshot.close()
        raise


def compiled_file(directory, record, meta, verify=True):
    rel = f"compiled/{record['documentId']}.json"
    if meta['artifacts'].get(rel) != meta['compiledHash']:
        raise MaterialError('Compiled output does not match the reviewed version.', 409)
    if verify: return artifact_file(directory, rel, meta['compiledHash'])
    return directory / 'documents' / rel


def ensure_index(directory, record, meta):
    version = extraction_version(meta)
    target = directory / f'review-index-{version}.sqlite3'
    compiled = compiled_file(directory, record, meta, verify=False)
    stat = compiled.stat()
    stamp = [stat.st_ino, stat.st_mtime_ns, stat.st_size]
    if target.exists():
        with sqlite3.connect(target) as db:
            saved = db.execute('SELECT value FROM info WHERE key="stamp"').fetchone()
        if saved and json.loads(saved[0]) == stamp: return target
        # Same extraction hash with changed source is never silently reused.
        compiled_file(directory, record, meta)
    if stat.st_size > MAX_COMPILED_BYTES:
        raise MaterialError('Compiled output exceeds the 256 MB review limit; split the source and ingest again.', 409)
    compiled_file(directory, record, meta)
    temporary = directory / f'.review-index-{version}-{os.getpid()}.sqlite3'
    temporary.unlink(missing_ok=True)
    try:
        with sqlite3.connect(temporary) as db:
            db.execute('CREATE TABLE info(key TEXT PRIMARY KEY,value TEXT)')
            db.execute('CREATE TABLE sections(id INTEGER PRIMARY KEY,title TEXT,source_id TEXT)')
            db.execute('CREATE TABLE fragments(id INTEGER PRIMARY KEY,section INTEGER,path TEXT,text TEXT,asset TEXT)')
            db.execute('CREATE INDEX fragment_section ON fragments(section,id)')
            section = -1; sequence = 0
            with compiled.open('rb') as stream:
                for prefix, event, value in ijson.parse(stream):
                    if prefix == 'sections.item' and event == 'start_map':
                        section += 1
                        db.execute('INSERT INTO sections VALUES (?,?,?)', (section, f'Section {section + 1}', str(section)))
                    if section < 0 or not prefix.startswith('sections.item.') or event not in ('string', 'number', 'boolean'):
                        continue
                    path = prefix.removeprefix('sections.item.')
                    if path in ('title', 'section_id'):
                        db.execute('UPDATE sections SET ' + ('title' if path == 'title' else 'source_id') + '=? WHERE id=?', (str(value)[:200], section))
                    text = str(value)
                    asset = text if path.split('.')[-1] in ('path', 'crop_path', 'csv_path') and text in meta['artifacts'] and text.startswith('assets/') else None
                    for offset in range(0, max(1, len(text)), FRAGMENT_CHARS):
                        sequence += 1
                        db.execute('INSERT INTO fragments VALUES (?,?,?,?,?)', (sequence, section, path[:300], text[offset:offset + FRAGMENT_CHARS], asset))
            if [compiled.stat().st_ino, compiled.stat().st_mtime_ns, compiled.stat().st_size] != stamp:
                raise MaterialError('Extraction changed while indexing. Refresh the review.', 409)
            db.execute('INSERT INTO info VALUES ("stamp",?)', (json.dumps(stamp),))
        temporary.replace(target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def indexed_review(directory, record, meta):
    target = directory / f'review-index-{extraction_version(meta)}.sqlite3'
    if not target.exists():
        raise MaterialError('Review index is not prepared. The operator must run course_materials_review --prepare for this extraction.', 503)
    compiled = compiled_file(directory, record, meta, verify=False)
    stat = compiled.stat()
    with sqlite3.connect(target) as db:
        saved = db.execute('SELECT value FROM info WHERE key="stamp"').fetchone()
    if not saved or json.loads(saved[0]) != [stat.st_ino, stat.st_mtime_ns, stat.st_size]:
        raise MaterialError('Extraction changed after indexing. Refresh or ask the operator to rebuild the review index.', 409)
    return target


def note_fragments(meta):
    for group in [meta.get('validation', {}).get('errors', []), meta.get('validation', {}).get('warnings', []), meta.get('uncertainties', [])]:
        for note in group:
            text = str(note)
            for offset in range(0, max(1, len(text)), FRAGMENT_CHARS):
                yield text[offset:offset + FRAGMENT_CHARS]


def review_page(directory, record, version=None, section=0, cursor=0):
    meta = metadata(directory); current = extraction_version(meta)
    if cursor and not version:
        raise MaterialError('A review cursor requires its extraction version.', 400)
    if version and version != current:
        raise MaterialError('Extraction version changed. Refresh review before continuing.', 409)
    index = indexed_review(directory, record, meta)
    with sqlite3.connect(index) as db:
        db.row_factory = sqlite3.Row
        if cursor and not db.execute('SELECT 1 FROM fragments WHERE section=? AND id=?', (section, cursor)).fetchone():
            raise MaterialError('Cursor does not belong to this section/version.', 400)
        sections = [dict(row) for row in db.execute('SELECT * FROM sections WHERE id>=? ORDER BY id LIMIT 101', (max(0, section - 50),))]
        rows = [dict(row) for row in db.execute('SELECT * FROM fragments WHERE section=? AND id>? ORDER BY id LIMIT ?', (section, cursor, PAGE_SIZE + 1))]
        total_sections = db.execute('SELECT COUNT(*) FROM sections').fetchone()[0]
        if section >= max(1, total_sections):
            raise MaterialError('Review section not found.', 404)
    more = len(rows) > PAGE_SIZE; rows = rows[:PAGE_SIZE]
    # Metadata and sections are bounded too. Complete warnings remain accessible
    # through a separate paginated notes endpoint or explicit JSON download.
    validation = meta.get('validation', {})
    return {'documentId': record['documentId'], 'compiledHash': meta['compiledHash'], 'version': current, 'demo': meta.get('demo', False),
            'validation': {'ok': validation.get('ok', False), 'errors': [str(v)[:FRAGMENT_CHARS] for v in validation.get('errors', [])[:3]], 'warnings': [str(v)[:FRAGMENT_CHARS] for v in validation.get('warnings', [])[:3]]},
            'uncertainties': [str(v)[:FRAGMENT_CHARS] for v in meta.get('uncertainties', [])[:3]],
            'notesCount': sum(1 for _ in note_fragments(meta)),
            'sections': sections[:100], 'sectionCount': total_sections, 'section': section, 'items': rows,
            'nextCursor': rows[-1]['id'] if more and rows else None, 'cursor': cursor, 'pageLimits': {'fragments': PAGE_SIZE, 'characters': FRAGMENT_CHARS}}


def main():
    import argparse
    from .course_materials import MaterialStore
    parser = argparse.ArgumentParser(description='Prepare a private review index locally; no cloud calls.')
    parser.add_argument('--prepare', required=True, help='Material ID')
    args = parser.parse_args()
    store = MaterialStore(os.environ.get('COURSE_MATERIALS_DATA_DIR') or Path(__file__).resolve().parents[2] / 'instance/course-materials')
    record = store.get(args.prepare); directory = store.directory / record['id']
    print(ensure_index(directory, record, metadata(directory)))


if __name__ == '__main__':
    main()
