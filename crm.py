from __future__ import annotations
"""Thin client for the Bellhaven CRM sandbox API."""
import time

import requests

from config import CRM_BASE, CRM_TOKEN


class CRMError(Exception):
    def __init__(self, status: int, detail):
        super().__init__(f"CRM API {status}: {detail}")
        self.status = status
        self.detail = detail


class CRM:
    def __init__(self, base: str = CRM_BASE, token: str = CRM_TOKEN, dry_run: bool = False):
        if not token:
            raise RuntimeError("BH_CRM_TOKEN is not set (see README / .env.example)")
        self.base = base.rstrip("/")
        self.dry_run = dry_run
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        self.write_log: list[dict] = []  # every write attempted in this process (used for audit/UI)

    # ---- transport -------------------------------------------------------
    def _req(self, method: str, path: str, *, params=None, json=None, retries: int = 3):
        url = self.base + path
        last = None
        for attempt in range(retries):
            try:
                r = self.s.request(method, url, params=params, json=json, timeout=30)
            except requests.RequestException as e:
                last = CRMError(0, str(e))
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code >= 500:  # transient; safe to retry (GET) but writes only retry on conn errors
                last = CRMError(r.status_code, r.text[:300])
                if method != "GET":
                    raise last
                time.sleep(1.5 * (attempt + 1))
                continue
            try:
                body = r.json()
            except ValueError:
                body = {"raw": r.text}
            if r.status_code >= 400:
                detail = body.get("detail", body) if isinstance(body, dict) else body
                raise CRMError(r.status_code, detail)
            return body
        raise last or CRMError(0, "request failed")

    # ---- reads -----------------------------------------------------------
    def _paged(self, path: str, **params) -> list[dict]:
        out, page = [], 1
        while True:
            d = self._req("GET", path, params={**params, "page": page, "page_size": 50})
            rows = d.get("data", [])
            out.extend(rows)
            if not rows or len(out) >= d.get("total", len(out)):
                return out
            page += 1

    def all_accounts(self) -> list[dict]:
        return self._paged("/accounts")

    def all_contacts(self) -> list[dict]:
        return self._paged("/contacts")

    def get_account(self, account_id: str) -> dict:
        return self._req("GET", f"/accounts/{account_id}")

    def find_accounts(self, **filters) -> list[dict]:
        return self._paged("/accounts", **filters)

    # ---- writes ----------------------------------------------------------
    def patch_account(self, account_id: str, fields: dict) -> dict:
        entry = {"method": "PATCH", "path": f"/accounts/{account_id}", "body": fields}
        self.write_log.append(entry)
        if self.dry_run:
            return {"dry_run": True, "account_id": account_id, "fields": list(fields)}
        resp = self._req("PATCH", f"/accounts/{account_id}", json=fields, retries=1)
        entry["response"] = resp
        return resp

    def create_account(self, fields: dict) -> dict:
        entry = {"method": "POST", "path": "/accounts", "body": fields}
        self.write_log.append(entry)
        if self.dry_run:
            return {"dry_run": True, "account_id": "DRYRUN-NEW-ACCOUNT"}
        resp = self._req("POST", "/accounts", json=fields, retries=1)
        entry["response"] = resp
        return resp


def extract_account_id(resp) -> str | None:
    """The create response shape is not documented; accept the plausible variants."""
    if not isinstance(resp, dict):
        return None
    for key in ("account_id", "id"):
        if isinstance(resp.get(key), str) and resp[key]:
            return resp[key]
    for key in ("data", "account"):
        if isinstance(resp.get(key), dict):
            got = extract_account_id(resp[key])
            if got:
                return got
    return None
