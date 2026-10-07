from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "src/skinfluencer/storage/schema.sql"
DEFAULT_DB = ROOT / "data/database/skinfluencer.sqlite"


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply the current Skinfluencer SQLite schema/migrations.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()

    if not args.database.exists():
        raise SystemExit(f"Database not found: {args.database}")

    connection = sqlite3.connect(args.database)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA.read_text(encoding="utf-8"))
        connection.commit()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        print(f"Schema applied successfully. user_version={version}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
