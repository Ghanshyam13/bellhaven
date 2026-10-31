from __future__ import annotations
"""Daily pipeline: scrape website -> snapshot CRM -> match -> queue proposals for human review.

    python pipeline.py                 # normal run (this is what the schedule calls)
    python pipeline.py --skip-scrape   # reuse data/website_locations.json (debugging)

The pipeline NEVER writes to the CRM. It only fills the review queue (SQLite). Writes happen in the review app
after a person approves a proposal, through executor.py.
"""
import argparse
import json
import sys
import time

import matcher
import scraper
import store
from config import CRM_SNAPSHOT_FILE, REPORT_FILE, WEBSITE_FILE
from crm import CRM


def run(skip_scrape: bool = False, db_path=None, crm: CRM | None = None, quiet: bool = False) -> dict:
    started = store.now()
    crm = crm or CRM()

    # 1. website  (a failed/partial scrape raises before anything is touched)
    if skip_scrape:
        website = json.loads(WEBSITE_FILE.read_text())
    else:
        website = scraper.scrape_all()
        WEBSITE_FILE.write_text(json.dumps(website, indent=1))

    # 2. CRM snapshot
    accounts = crm.all_accounts()
    contacts: dict[str, int] = {}
    for c in crm.all_contacts():
        contacts[c["account_id"]] = contacts.get(c["account_id"], 0) + 1
    CRM_SNAPSHOT_FILE.write_text(json.dumps(accounts, indent=1))

    # 3. match
    result = matcher.build(website, accounts, contacts, today=time.strftime("%Y-%m-%d", time.gmtime()))
    proposals = [p.to_dict() for p in result["proposals"]]
    report = result["report"]

    # Safety valve: a broken scrape (site layout change, partial page) must not mass-flag the CRM.
    bh_children = [a for a in accounts if (a.get("parent_name") or "").startswith("Bellhaven")]
    orphan_n = len(report["orphans"])
    if orphan_n > max(5, 0.25 * len(bh_children)):
        raise RuntimeError(f"{orphan_n} of {len(bh_children)} Bellhaven accounts look orphaned - refusing to queue "
                           f"proposals; the website scrape is probably incomplete")

    # 4. queue (idempotent)
    with store.connect(db_path) as con:
        stats = store.upsert_proposals(con, proposals)
        counts = store.counts(con)
        store.record_run(con, started, {**stats, **report["counts"]})
    report["queue"] = counts
    report["generated_at"] = store.now()
    REPORT_FILE.write_text(json.dumps(report, indent=1))

    summary = {"website_locations": len(website["locations"]), "crm_accounts": len(accounts),
               "proposals_generated": len(proposals), **stats, "queue": counts}
    if not quiet:
        print(json.dumps(summary, indent=2))
        if stats["redetected_applied"]:
            print(f"WARNING: {stats['redetected_applied']} already-applied proposal(s) were detected again - "
                  f"something reverted a change in the CRM. They were NOT re-queued.", file=sys.stderr)
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-scrape", action="store_true")
    args = ap.parse_args()
    try:
        run(skip_scrape=args.skip_scrape)
    except Exception as e:  # non-zero exit so the scheduler shows a failed run
        print(f"PIPELINE FAILED: {e}", file=sys.stderr)
        sys.exit(1)
