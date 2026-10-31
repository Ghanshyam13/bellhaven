from __future__ import annotations
"""Local stand-in for the sandbox CRM API (same endpoints/validation style) used by the tests,
so approvals can be exercised end-to-end without touching the real CRM copy."""
import copy
import itertools
import logging
import threading

from flask import Flask, jsonify, request
from werkzeug.serving import make_server

logging.getLogger("werkzeug").setLevel(logging.ERROR)
VALID_STATUS = ["Active", "Inactive", "Needs Review"]
PATCHABLE = {"name", "parent_id", "billing_street", "billing_city", "billing_state", "billing_zip", "care_type",
             "status", "phone", "chow_current_account", "duplicate_of_account", "note"}


def create_app(accounts, contacts=(), return_id=True):
    app = Flask(__name__)
    db = {a["account_id"]: copy.deepcopy(a) for a in accounts}
    app.db = db
    app.writes = []
    counter = itertools.count(1)

    def with_parent_name(a):
        p = db.get(a.get("parent_id") or "")
        a["parent_name"] = p["name"] if p else ""
        return a

    @app.get("/api/v1/accounts")
    def list_accounts():
        rows = list(db.values())
        q = request.args
        for key, col in (("city", "billing_city"), ("state", "billing_state"), ("zip", "billing_zip"),
                         ("parent_id", "parent_id")):
            if q.get(key):
                rows = [a for a in rows if a.get(col) == q[key]]
        if q.get("q"):
            rows = [a for a in rows if q["q"].lower() in a["name"].lower()]
        if q.get("street"):
            rows = [a for a in rows if q["street"].lower() in a["billing_street"].lower()]
        page, size = int(q.get("page", 1)), int(q.get("page_size", 50))
        return jsonify({"data": [with_parent_name(copy.deepcopy(a)) for a in rows[(page - 1) * size: page * size]],
                        "page": page, "page_size": size, "total": len(rows)})

    @app.get("/api/v1/accounts/<aid>")
    def get_account(aid):
        if aid not in db:
            return jsonify({"detail": "account not found"}), 404
        return jsonify(with_parent_name(copy.deepcopy(db[aid])))

    @app.patch("/api/v1/accounts/<aid>")
    def patch_account(aid):
        if aid not in db:
            return jsonify({"detail": "account not found"}), 404
        body = request.get_json()
        if "status" in body and body["status"] not in VALID_STATUS:
            return jsonify({"detail": f"status must be one of {VALID_STATUS}"}), 422
        unknown = set(body) - PATCHABLE
        if unknown:
            return jsonify({"detail": f"unknown fields {sorted(unknown)}"}), 422
        db[aid].update(body)
        app.writes.append(("PATCH", aid, body))
        return jsonify({"account_id": aid, "message": "updated", "fields": list(body)})

    @app.post("/api/v1/accounts")
    def create():
        body = request.get_json() or {}
        if not body.get("name"):
            return jsonify({"detail": "name is required"}), 422
        if body.get("status", "Active") not in VALID_STATUS:
            return jsonify({"detail": f"status must be one of {VALID_STATUS}"}), 422
        aid = f"001MOCK{next(counter):011d}"
        db[aid] = {"account_id": aid, "parent_id": "", "billing_street": "", "billing_city": "", "billing_state": "",
                   "billing_zip": "", "care_type": "", "status": "Active", "phone": "", "lifetime_revenue": 0,
                   "outstanding_ar": 0, "chow_current_account": "", "duplicate_of_account": "", "note": "",
                   "created_by_candidate": True, **body}
        app.writes.append(("POST", aid, body))
        return (jsonify({"account_id": aid, "message": "created"}) if return_id
                else jsonify({"message": "created"})), 201

    @app.get("/api/v1/contacts")
    def list_contacts():
        page, size = int(request.args.get("page", 1)), int(request.args.get("page_size", 50))
        rows = list(contacts)
        return jsonify({"data": rows[(page - 1) * size: page * size], "page": page, "page_size": size, "total": len(rows)})

    return app


class Server:
    def __init__(self, app):
        self.app = app
        self.srv = make_server("127.0.0.1", 0, app, threaded=True)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)

    @property
    def base(self):
        return f"http://127.0.0.1:{self.srv.server_port}/api/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
