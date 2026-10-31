from __future__ import annotations
"""Exercises the review app's HTTP routes against the mock CRM.
Run in its own process:  python -m unittest tests.test_app -v
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="bhapp_"))
shutil.copy(ROOT / "tests" / "fixtures" / "website_locations.json", TMP / "website_locations.json")

from tests.mock_crm import Server, create_app  # noqa: E402

ACCOUNTS = json.loads((ROOT / "tests" / "fixtures" / "accounts.json").read_text())
CONTACTS = []
for p in (1, 2):
    CONTACTS += json.loads((ROOT / "tests" / "fixtures" / f"contacts_p{p}.json").read_text())["data"]

MOCK_APP = create_app(ACCOUNTS, CONTACTS)
SRV = Server(MOCK_APP).__enter__()
os.environ.update({"BH_DATA_DIR": str(TMP), "BH_DB_FILE": str(TMP / "review.db"),
                   "BH_CRM_BASE": SRV.base, "BH_CRM_TOKEN": "x"})

import app as review_app  # noqa: E402
import pipeline  # noqa: E402
import store  # noqa: E402
from crm import CRM  # noqa: E402


class AppRoutes(unittest.TestCase):
    def test_approve_reject_and_double_click(self):
        pipeline.run(skip_scrape=True, crm=CRM(), quiet=True)
        c = review_app.app.test_client()
        with store.connect() as con:
            rows = {r["title"]: r for r in store.list_proposals(con)}
        tiffin = rows["Change of ownership: Bellhaven of Tiffin  ->  new account under Bellhaven"]
        n_writes = len(MOCK_APP.writes)

        r = c.post(f"/approve/{tiffin['id']}", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(MOCK_APP.writes), n_writes + 2)          # POST new account + PATCH chow pointer
        old = MOCK_APP.db["001U6RW32TY0WSXZZB"]
        self.assertEqual(old["parent_id"], "001FWSQ30SFW6S7604")       # SOP: parent untouched
        self.assertTrue(old["chow_current_account"])

        c.post(f"/approve/{tiffin['id']}")                             # double click
        self.assertEqual(len(MOCK_APP.writes), n_writes + 2)          # nothing more written

        union = rows["Create account: Bellhaven at Union Square"]
        c.post(f"/reject/{union['id']}", data={"reason": "not sure it is ours"})
        with store.connect() as con:
            self.assertEqual(store.get(con, union["id"])["status"], "rejected")
            self.assertEqual(store.get(con, tiffin["id"])["status"], "applied")

        # bulk approve every remaining high-confidence proposal
        with store.connect() as con:
            ids = [p["id"] for p in store.list_proposals(con, "pending") if p["confidence"] == "high"]
        c.post("/bulk", data={"action": "approve", "ids": ids, "status": "pending"})
        with store.connect() as con:
            self.assertEqual(sum(1 for p in store.list_proposals(con, "pending") if p["confidence"] == "high"), 0)
            self.assertEqual(store.counts(con)["failed"], 0)

        # rerun: the rejected item must not come back, nothing decided is re-queued
        s = pipeline.run(skip_scrape=True, crm=CRM(), quiet=True)
        self.assertEqual(s["new"], 0)
        with store.connect() as con:
            self.assertEqual(store.get(con, union["id"])["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
