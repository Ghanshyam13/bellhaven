from __future__ import annotations
"""Review app: a person sees every proposed CRM change with its evidence and approves or rejects it.
Approved changes are written to the CRM through the API (executor.py). Nothing is written without approval.

    python app.py            ->  http://127.0.0.1:5055
    BH_DRY_RUN=1 python app.py   shows the exact API calls an approval WOULD make, without making them
"""
import json
import os
from collections import OrderedDict

from flask import Flask, flash, redirect, render_template, request, url_for

import executor
import store
from config import CRM_BASE, REPORT_FILE, SITE_BASE, WEBSITE_FILE
from crm import CRM

app = Flask(__name__)
app.secret_key = os.environ.get("BH_SECRET", "local-review-app")
DRY_RUN = os.environ.get("BH_DRY_RUN") == "1"
REVIEWER = os.environ.get("BH_REVIEWER") or os.environ.get("USER") or "reviewer"

KIND_LABEL = {"update": "Fix", "create": "Create", "chow": "Change of ownership", "chow_link": "Change of ownership",
              "duplicate": "Duplicate", "orphan": "No longer on site", "manual": "Needs a human"}
STATUS_TABS = ["pending", "applied", "rejected", "failed", "stale", "all"]


@app.template_filter("pretty")
def pretty(v):
    return json.dumps(v, indent=1) if isinstance(v, (dict, list)) else v


def _apply(pid: int) -> str:
    """Approve one proposal: lock it, write via the executor, record the outcome."""
    with store.connect() as con:
        row = store.get(con, pid)
        if row is None:
            return f"proposal {pid} not found"
        if DRY_RUN:
            try:
                _, log = executor.apply_proposal(dict(row, progress={}), CRM(dry_run=True))
            except executor.ApplyError as e:
                return f"DRY RUN blocked: {e}"
            calls = "; ".join(f"{x.get('op')} {x.get('account_id', '')} {json.dumps(x.get('request', x.get('adopted_existing', '')))}" for x in log)
            return f"DRY RUN (nothing written): {calls}"
        if not store.claim(con, pid):
            return f"'{row['title']}' was already handled (status: {row['status']})"
    try:
        crm = CRM()
        progress, log = executor.apply_proposal(row, crm)
    except executor.StaleError as e:
        with store.connect() as con:
            store.update(con, pid, status="stale", error=str(e), progress=row["progress"])
        return f"NOT applied (stale): {e}"
    except executor.ApplyError as e:
        with store.connect() as con:
            store.update(con, pid, status="failed", error=str(e), progress=row["progress"])
        return f"FAILED: {e}"
    except Exception as e:  # unexpected: keep the row retryable
        with store.connect() as con:
            store.update(con, pid, status="failed", error=f"{type(e).__name__}: {e}")
        return f"FAILED: {type(e).__name__}: {e}"
    with store.connect() as con:
        store.update(con, pid, status="applied", error="", progress=progress, apply_log=log,
                     decided_at=store.now(), decided_by=REVIEWER)
    return f"Applied: {row['title']}"


@app.get("/")
def index():
    status = request.args.get("status", "pending")
    with store.connect() as con:
        store.recover_interrupted(con)
        rows = store.list_proposals(con, status)
        counts = store.counts(con)
        last = store.last_run(con)
    groups: "OrderedDict[str, list]" = OrderedDict()
    for r in rows:
        groups.setdefault(r["group_title"], []).append(r)
    return render_template("index.html", groups=groups, status=status, tabs=STATUS_TABS, counts=counts, last=last,
                           kind_label=KIND_LABEL, dry_run=DRY_RUN, crm_base=CRM_BASE, total=len(rows))


@app.post("/approve/<int:pid>")
def approve(pid):
    flash(_apply(pid))
    return redirect(request.referrer or url_for("index"))


@app.post("/reject/<int:pid>")
def reject(pid):
    with store.connect() as con:
        row = store.get(con, pid)
        if row and row["status"] in ("pending", "failed", "stale"):
            store.update(con, pid, status="rejected", decided_at=store.now(), decided_by=REVIEWER,
                         decision_note=request.form.get("reason", "").strip())
            flash(f"Rejected: {row['title']} (will not be proposed again)")
        else:
            flash("Nothing to reject")
    return redirect(request.referrer or url_for("index"))


@app.post("/bulk")
def bulk():
    ids = [int(i) for i in request.form.getlist("ids")]
    action = request.form.get("action")
    if not ids:
        flash("Nothing selected")
    elif action == "approve":
        for pid in ids:
            flash(_apply(pid))
    elif action == "reject":
        with store.connect() as con:
            for pid in ids:
                store.update(con, pid, status="rejected", decided_at=store.now(), decided_by=REVIEWER)
        flash(f"Rejected {len(ids)} proposal(s)")
    return redirect(url_for("index", status=request.form.get("status", "pending")))


@app.get("/report")
def report():
    rep = json.loads(REPORT_FILE.read_text()) if REPORT_FILE.exists() else None
    web = json.loads(WEBSITE_FILE.read_text()) if WEBSITE_FILE.exists() else {"locations": []}
    with store.connect() as con:
        counts = store.counts(con)
        last = store.last_run(con)
    return render_template("report.html", rep=rep, web=web, counts=counts, last=last, site=SITE_BASE)


if __name__ == "__main__":
    with store.connect() as con:
        store.recover_interrupted(con)
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5055)), debug=False)
