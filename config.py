from __future__ import annotations
"""Shared configuration for the Bellhaven CRM sync pipeline."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader (no dependency). Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.environ.get("BH_DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

SITE_BASE = os.environ.get("BH_SITE_BASE", "https://analyst-assessment-production.up.railway.app")
CRM_BASE = os.environ.get("BH_CRM_BASE", SITE_BASE + "/api/v1")

# Read from the environment / .env so the token is never committed.
CRM_TOKEN = os.environ.get("BH_CRM_TOKEN", "")

# The parent account that Bellhaven communities belong under.
BELLHAVEN_PARENT_NAME = "Bellhaven Senior Living (Parent Account)"

WEBSITE_FILE = DATA_DIR / "website_locations.json"
CRM_SNAPSHOT_FILE = DATA_DIR / "crm_snapshot.json"
REPORT_FILE = DATA_DIR / "classification.json"
DB_FILE = Path(os.environ.get("BH_DB_FILE", DATA_DIR / "review.db"))
