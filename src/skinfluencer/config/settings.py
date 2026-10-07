from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[1]
DATA_ROOT = Path(os.getenv("SKINFLUENCER_DATA_ROOT", REPO_ROOT / "data"))
DATABASE_PATH = Path(os.getenv("SKINFLUENCER_DB", DATA_ROOT / "database" / "skinfluencer.sqlite"))
SCHEMA_PATH = PACKAGE_ROOT / "storage" / "schema.sql"
STATIC_DIR = PACKAGE_ROOT / "web" / "static"
OFFICIAL_DOMAINS_PATH = REPO_ROOT / "config" / "official_domains.json"
