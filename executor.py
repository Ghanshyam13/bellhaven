"""Applies an APPROVED proposal to the CRM.

Safety properties (all enforced here, independently of the matcher):
  1. Optimistic concurrency - before each PATCH the live account is re-read and compared with the values the
     proposal was built from ("expect"). If someone changed the record meanwhile, nothing is written.
  2. Billing SOP - a PATCH that changes parent_id is refused when the live account has lifetime_revenue > 0
     AND outstanding_ar > 0. A CHOW link may only set chow_current_account.
  3. Idempotent creates - before POSTing, look for an account already at that address under the target parent
     and adopt it instead of creating a second one (double-click / retry safe).
  4. Resumable - progress (e.g. the id of a just-created account) is stored per proposal, so a failure half way
     through a two-step change (create + link) is retried without creating a duplicate.
  5. Read-back - every write is verified by reading the record again.
"""
from __future__ import annotations

from crm import CRM, CRMError, extract_account_id
from matcher import has_billing_history, norm_street


class ApplyError(Exception):
    """A step could not be applied; message is shown to the reviewer."""


class StaleError(ApplyError):
    """The CRM no longer matches what the proposal was built from."""


def _resolve(fields: dict, vars_: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if isinstance(v, str) and v.startswith("$"):
            if v[1:] not in vars_:
                raise ApplyError(f"step depends on '{v[1:]}' which has not been created yet")
            v = vars_[v[1:]]
        out[k] = v
    return out


def _do_create(crm: CRM, action: dict, log: list) -> str:
    fields = action["fields"]
    # idempotency: adopt an account that is already there
    existing = [
        a for a in crm.find_accounts(zip=fields["billing_zip"])
        if a.get("parent_id") == fields["parent_id"]
        and norm_street(a["billing_street"]) == norm_street(fields["billing_street"])
        and not a.get("duplicate_of_account")
    ]
    if existing:
        log.append({"op": "create", "adopted_existing": existing[0]["account_id"],
                    "note": "an account at this address under the target parent already exists; not creating another"})
        return existing[0]["account_id"]
    resp = crm.create_account(fields)
    new_id = extract_account_id(resp)
    if not new_id and not crm.dry_run:  # response shape undocumented -> find what we just created
        found = [a for a in crm.find_accounts(zip=fields["billing_zip"])
                 if a.get("parent_id") == fields["parent_id"]
                 and norm_street(a["billing_street"]) == norm_street(fields["billing_street"])]
        new_id = found[0]["account_id"] if found else None
    if not new_id:
        raise ApplyError(f"account was created but its id could not be determined (response: {resp})")
    log.append({"op": "create", "request": fields, "response": resp, "account_id": new_id})
    return new_id


def _do_patch(crm: CRM, action: dict, vars_: dict, log: list) -> None:
    acct_id = action["account_id"]
    fields = _resolve(action["fields"], vars_)
    live = crm.get_account(acct_id)

    for key, want in (action.get("expect") or {}).items():
        have = live.get(key)
        if (have if have is not None else "") != (want if want is not None else ""):
            raise StaleError(f"{live['name']} ({acct_id}): field '{key}' is now {have!r} but the proposal was built "
                             f"from {want!r}. Nothing was written - re-run the pipeline.")

    # ---- billing SOP, enforced at write time
    if "parent_id" in fields and fields["parent_id"] != live.get("parent_id") and has_billing_history(live):
        raise ApplyError(
            f"SOP VIOLATION blocked: {live['name']} has lifetime_revenue {live['lifetime_revenue']} and "
            f"outstanding_ar {live['outstanding_ar']}. Its parent must not be changed; use a change-of-ownership "
            f"(new account + chow_current_account) instead.")
    if "chow_current_account" in fields and len(fields) != 1:
        raise ApplyError("a change-of-ownership link must set chow_current_account and nothing else on the old account")

    if all(live.get(k, "") == v for k, v in fields.items()):
        log.append({"op": "patch", "account_id": acct_id, "skipped": "already has these values"})
        return
    resp = crm.patch_account(acct_id, fields)
    if not crm.dry_run:
        after = crm.get_account(acct_id)
        bad = {k: (after.get(k), v) for k, v in fields.items() if (after.get(k) or "") != v}
        if bad:
            raise ApplyError(f"CRM accepted the PATCH but the record does not reflect it: {bad}")
    log.append({"op": "patch", "account_id": acct_id, "request": fields, "response": resp})


def apply_proposal(proposal: dict, crm: CRM) -> tuple[dict, list]:
    """Run every step of an approved proposal. Returns (progress, log). Raises ApplyError."""
    progress = proposal.get("progress") or {}
    done = progress.setdefault("done", [])
    vars_ = progress.setdefault("vars", {})
    log: list = []
    try:
        for i, action in enumerate(proposal["payload"]["actions"]):
            if i in done:
                continue
            if action["op"] == "create":
                new_id = _do_create(crm, action, log)
                if action.get("result_key"):
                    vars_[action["result_key"]] = new_id
            elif action["op"] == "patch":
                _do_patch(crm, action, vars_, log)
            else:
                raise ApplyError(f"unknown action {action['op']}")
            done.append(i)
    except CRMError as e:
        raise ApplyError(str(e)) from e
    finally:
        progress["_log"] = log
    return progress, log
