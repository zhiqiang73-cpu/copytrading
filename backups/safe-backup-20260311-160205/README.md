# Safe Backup

Created at: 2026-03-11T16:02:05+08:00

This snapshot contains sanitized runtime data that is safe to push to GitHub.

Included:
- database schema in `schema.sql`
- table exports in `tables/*.jsonl`
- non-secret data files from `data/`

Secrets removed:
- Bitget API key / secret / passphrase
- Binance API key / secret

Not included:
- `.env`
- local virtualenv
- raw SQLite database
- logs and runtime lock/pid files
