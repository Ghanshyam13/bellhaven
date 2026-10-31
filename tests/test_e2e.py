from __future__ import annotations
"""End-to-end verification against a mock CRM seeded with the real sandbox data.

Run:  python -m unittest tests.test_e2e -v
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="bhtest_"))
os.environ["BH_DATA_DIR"] = str(TMP)
os.environ["BH_DB_FILE"] = str(TMP / "review.db")
os.environ["BH_CRM_TOKEN"] = "test-token"
shutil.copy(ROOT / "tests" / "fixtures" / "website_locations.json", TMP / "website_locations.json")

import executor  # noqa: E402
import pipeline  # noqa: E402
import store  # noqa: E402
from crm import CRM  # noqa: E402
from tests.mock_crm import Server, create_app  # noqa: E402

ACCOUNTS = json.loads((ROOT / "tests" / "fixtures" / "accounts.json").read_text())
CONTACTS = []
for p in (1, 2):
    CONTACTS += json.loads((ROOT / "tests" / "fixtures" / f"contacts_p{p}.json").read_text())["data"]

BH = "0015QAPLGS3FVYEEEM"
CEDAR = "001FWSQ30SFW6S7604"


def by_name(db, name):
    return [a for a in db.values() if a["name"] == name]


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if (TMP / "review.db").exists():
            (TMP / "review.db").unlink()
        cls.app = create_app(ACCOUNTS, CONTACTS)
        cls.srv = Server(cls.app).__enter__()
        cls.crm = CRM(base=cls.srv.base, token="x")
        cls.before = {a["account_id"]: dict(a) for a in ACCOUNTS}

    @classmethod
    def tearDownClass(cls):
        cls.srv.__exit__()

    # ------------------------------------------------------------------ helpers
    def queue(self):
        with store.connect() as con:
            return store.list_proposals(con)

    def approve(self, row):
        with store.connect() as con:
            row = store.get(con, row["id"])
        progress, log = executor.apply_proposal(row, self.crm)
        with store.connect() as con:
            store.update(con, row["id"], status="applied", progress=progress, apply_log=log, decided_at=store.now())

    # ------------------------------------------------------------------ the story
    def test_full_flow(self):
        s1 = pipeline.run(skip_scrape=True, crm=self.crm, quiet=True)
        self.assertEqual(s1["proposals_generated"], 25)
        self.assertEqual(s1["queue"]["pending"], 25)

        # Reject one (Union Square create), approve everything else
        rows = self.queue()
        rejected = next(r for r in rows if r["title"] == "Create account: Bellhaven at Union Square")
        with store.connect() as con:
            store.update(con, rejected["id"], status="rejected", decided_at=store.now())
        for r in rows:
            if r["id"] != rejected["id"]:
                self.approve(r)

        db = self.app.db
        # --- SOP: every account with revenue AND AR must keep its original parent
        for aid, old in self.before.items():
            if old["lifetime_revenue"] > 0 and old["outstanding_ar"] > 0:
                self.assertEqual(db[aid]["parent_id"], old["parent_id"], f"SOP broken for {old['name']}")
                self.assertEqual(db[aid]["status"], old["status"])

        # --- CHOW (new account + pointer): Tiffin & Marietta
        for nm, old_id in (("Bellhaven of Tiffin", "001U6RW32TY0WSXZZB"), ("Bellhaven of Marietta", "001A34WFSUYHCRBLFT")):
            old = db[old_id]
            self.assertEqual(old["parent_id"], CEDAR)
            new = db[old["chow_current_account"]]
            self.assertEqual(new["parent_id"], BH)
            self.assertEqual(new["name"], nm)
            self.assertEqual(new["status"], "Active")
        # --- Sandusky: linked to the existing Millstone account, no new account created
        self.assertEqual(db["001SXSF4ELF0Z2LGDM"]["chow_current_account"], "0017JP8Z1UQ763BVK3")
        self.assertEqual(db["001SXSF4ELF0Z2LGDM"]["parent_id"], BH)
        self.assertEqual(len(by_name(db, "Bellhaven of Sandusky")), 1)

        # --- no-billing re-parents go straight through
        self.assertEqual(db["001LGFPBJY4N9MB6KL"]["parent_id"], BH)       # Lima (Harborview, AR 0)
        self.assertEqual(db["001UKEFGADQ8YCZ4YM"]["parent_id"], BH)       # Findlay (no parent, AR 0)
        # --- fixes
        self.assertEqual(db["001NXP9X46CWEPSLSV"]["billing_street"], "3156 W Prospect Rd")   # PO Box -> street
        self.assertEqual(db["001CF3LDWVRGL09P4F"]["billing_zip"], "45662")                   # zip typo
        self.assertEqual(db["001RJU1X4NBWC1Q0G7"]["name"], "Bellhaven of Chagrin Falls")
        # --- duplicates: Inactive + pointer, parent untouched
        for lid, sid in (("001QU150PM4Z15UA71", "001EGU7BMJ942ZTRE6"), ("0011AB44D05WLA9HTX", "001U1750VLVJAGG1S5"),
                         ("00159PL81N38KM4FHM", "001U1750VLVJAGG1S5"), ("001BLYF02K97SZLZHH", "001CVBBCSDM7YHN220")):
            self.assertEqual(db[lid]["status"], "Inactive")
            self.assertEqual(db[lid]["duplicate_of_account"], sid)
            self.assertEqual(db[lid]["parent_id"], self.before[lid]["parent_id"])
        # --- creates (and the rejected one did NOT happen)
        for nm in ("Bellhaven of Batavia", "Bellhaven of Carlisle", "Amberly Manor"):
            made = [a for a in by_name(db, nm) if a.get("created_by_candidate")]
            self.assertEqual(len(made), 1, nm)
            self.assertEqual(made[0]["parent_id"], BH)
        self.assertEqual(by_name(db, "Bellhaven at Union Square"), [])
        # --- orphans only flagged
        for oid in ("00116ETS45BL7DTQP7", "0016PVXH4B25HWR7QE"):
            self.assertEqual(db[oid]["status"], "Needs Review")
            self.assertEqual(db[oid]["parent_id"], BH)

        # ============ idempotency: run again, nothing new, rejected stays rejected ============
        n_accounts = len(db)
        s2 = pipeline.run(skip_scrape=True, crm=self.crm, quiet=True)
        self.assertEqual(s2["queue"]["pending"], 0, s2)
        self.assertEqual(s2["new"], 0)
        self.assertEqual(s2["queue"]["rejected"], 1)
        self.assertEqual(s2["queue"]["applied"], 24)
        self.assertEqual(s2["went_stale"], 0)
        # converged purely from CRM state: the only thing the matcher still wants is the rejected create
        self.assertEqual(s2["proposals_generated"], 1)
        # a third run must not touch the CRM either
        writes = len(self.app.writes)
        pipeline.run(skip_scrape=True, crm=self.crm, quiet=True)
        self.assertEqual(len(self.app.writes), writes)
        self.assertEqual(len(self.app.db), n_accounts)

    def test_zz_sop_guard_in_executor(self):
        """Even a hand-crafted proposal cannot re-parent an account with revenue AND AR."""
        bad = {"id": 0, "progress": {}, "payload": {"actions": [
            {"op": "patch", "account_id": "0019Y4J61ZBG4R00CD", "fields": {"parent_id": CEDAR}}]}}
        with self.assertRaises(executor.ApplyError) as cm:
            executor.apply_proposal(bad, self.crm)
        self.assertIn("SOP VIOLATION", str(cm.exception))

    def test_zz_stale_detection(self):
        row = {"id": 0, "progress": {}, "payload": {"actions": [
            {"op": "patch", "account_id": "001L3LGR6W69J8DYYV", "fields": {"name": "X"},
             "expect": {"name": "Something else entirely"}}]}}
        with self.assertRaises(executor.StaleError):
            executor.apply_proposal(row, self.crm)


class CreateWithoutIdInResponse(unittest.TestCase):
    def test_find_by_address_fallback_and_adoption(self):
        app = create_app(ACCOUNTS, CONTACTS, return_id=False)
        with Server(app) as srv:
            crm = CRM(base=srv.base, token="x")
            fields = {"name": "Bellhaven of Batavia", "parent_id": BH, "billing_street": "2000 Hospital Dr",
                      "billing_city": "Batavia", "billing_state": "OH", "billing_zip": "45103",
                      "care_type": "Skilled Nursing", "phone": "x", "status": "Active"}
            row = {"id": 1, "progress": {}, "payload": {"actions": [{"op": "create", "fields": fields}]}}
            executor.apply_proposal(row, crm)
            self.assertEqual(len([w for w in app.writes if w[0] == "POST"]), 1)
            # second approve (double click / retry) must adopt, not create again
            executor.apply_proposal({"id": 1, "progress": {}, "payload": row["payload"]}, crm)
            self.assertEqual(len([w for w in app.writes if w[0] == "POST"]), 1)


if __name__ == "__main__":
    unittest.main()
