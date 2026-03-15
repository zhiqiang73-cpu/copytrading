from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

import database as db


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text('', encoding='utf-8')
        return
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _filter_rows(rows: list[dict], trader_uids: set[str]) -> list[dict]:
    if not trader_uids:
        return rows
    return [row for row in rows if str(row.get('trader_uid') or '').strip() in trader_uids]


def export_research_summary(output_dir: Path, trader_uids: list[str] | None = None) -> Path:
    trader_filter = {str(uid).strip() for uid in (trader_uids or []) if str(uid).strip()}
    db.init_db()

    scores = _filter_rows(db.get_trader_research_scores(), trader_filter)
    execution_daily = _filter_rows(db.get_trader_execution_daily(), trader_filter)
    position_cycles = _filter_rows(db.get_trader_position_cycles(), trader_filter)
    source_events = _filter_rows(db.get_source_trader_events(), trader_filter)

    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        'generated_at_ms': int(time.time() * 1000),
        'trader_uids': sorted(trader_filter) if trader_filter else sorted({str(row.get('trader_uid') or '').strip() for row in scores + execution_daily + position_cycles + source_events if str(row.get('trader_uid') or '').strip()}),
        'research_scores': scores,
        'execution_daily': execution_daily,
        'position_cycles': position_cycles,
        'source_events': source_events,
    }

    (output_dir / 'research_summary.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    _write_csv(output_dir / 'research_scores.csv', scores)
    _write_csv(output_dir / 'execution_daily.csv', execution_daily)
    _write_csv(output_dir / 'position_cycles.csv', position_cycles)
    _write_csv(output_dir / 'source_events.csv', source_events)
    return output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description='Export trader research summary tables to JSON and CSV.')
    parser.add_argument('--trader', action='append', default=[], help='Trader UID to export. Can be repeated.')
    parser.add_argument('--output-dir', default='', help='Directory to write export files into.')
    args = parser.parse_args()

    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = Path('backups') / f'research-summary-{time.strftime("%Y%m%d-%H%M%S")}'

    final_dir = export_research_summary(output_dir, trader_uids=args.trader)
    print(final_dir)


if __name__ == '__main__':
    main()
