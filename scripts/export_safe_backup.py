from __future__ import annotations

import datetime as dt
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / 'data'
BACKUP_ROOT = ROOT / 'backups'
DB_PATH = DATA_DIR / 'tracker.db'
SECRET_FIELDS = {
    'api_key',
    'api_secret',
    'api_passphrase',
    'binance_api_key',
    'binance_api_secret',
}
SAFE_DATA_FILES = (
    'available_traders.json',
    'traders_with_uid.json',
)


def _git_head() -> str:
    try:
        return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    except Exception:
        return 'unknown'


def _sanitize_row(table: str, row: dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    if table == 'copy_settings':
        for key in SECRET_FIELDS:
            if key in data:
                data[key] = ''
        return data

    if table == 'copy_profile_settings':
        raw = data.get('settings_json')
        if isinstance(raw, str) and raw:
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {'_raw': 'UNPARSEABLE'}
            if isinstance(payload, dict):
                for key in SECRET_FIELDS:
                    if key in payload:
                        payload[key] = ''
            data['settings_json'] = payload
        return data

    return data


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open('w', encoding='utf-8', newline='\n') as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            fh.write('\n')


def export_safe_backup() -> Path:
    timestamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_dir = BACKUP_ROOT / f'safe-backup-{timestamp}'
    tables_dir = backup_dir / 'tables'
    data_dir = backup_dir / 'data'
    backup_dir.mkdir(parents=True, exist_ok=False)
    tables_dir.mkdir(parents=True, exist_ok=False)
    data_dir.mkdir(parents=True, exist_ok=False)

    manifest: dict[str, Any] = {
        'created_at': dt.datetime.now().astimezone().isoformat(timespec='seconds'),
        'git_head': _git_head(),
        'source_database': str(DB_PATH.relative_to(ROOT)) if DB_PATH.exists() else None,
        'sanitized_fields': sorted(SECRET_FIELDS),
        'included_tables': [],
        'row_counts': {},
        'copied_data_files': [],
        'excluded_local_only': [
            '.env',
            'API KEY.txt',
            '.venv/',
            'logs/',
            '*.log',
            'data/*.db',
            '*.pid',
            '*.lock',
        ],
    }

    if DB_PATH.exists():
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        schema_rows = cur.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        schema_sql: list[str] = []
        for entry in schema_rows:
            table = entry['name']
            sql = entry['sql'] or ''
            manifest['included_tables'].append(table)
            schema_sql.append(f'-- {table}\n{sql};\n')

            rows = [
                _sanitize_row(table, dict(row))
                for row in cur.execute(f'SELECT * FROM "{table}"')
            ]
            manifest['row_counts'][table] = len(rows)
            _write_jsonl(tables_dir / f'{table}.jsonl', rows)

        (backup_dir / 'schema.sql').write_text('\n'.join(schema_sql), encoding='utf-8', newline='\n')
        conn.close()

    for name in SAFE_DATA_FILES:
        src = DATA_DIR / name
        if src.exists():
            shutil.copy2(src, data_dir / name)
            manifest['copied_data_files'].append(str((data_dir / name).relative_to(backup_dir)))

    (backup_dir / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
        newline='\n',
    )
    (backup_dir / 'README.md').write_text(
        '# Safe Backup\n\n'
        f"Created at: {manifest['created_at']}\n\n"
        'This snapshot contains sanitized runtime data that is safe to push to GitHub.\n\n'
        'Included:\n'
        '- database schema in `schema.sql`\n'
        '- table exports in `tables/*.jsonl`\n'
        '- non-secret data files from `data/`\n\n'
        'Secrets removed:\n'
        '- Bitget API key / secret / passphrase\n'
        '- Binance API key / secret\n\n'
        'Not included:\n'
        '- `.env`\n'
        '- local virtualenv\n'
        '- raw SQLite database\n'
        '- logs and runtime lock/pid files\n',
        encoding='utf-8',
        newline='\n',
    )
    return backup_dir


if __name__ == '__main__':
    out = export_safe_backup()
    print(out.relative_to(ROOT))
