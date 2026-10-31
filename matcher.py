"""Matching logic: website locations  <->  CRM accounts  ->  change proposals.

Pure functions only (no network, no database) so the behaviour is easy to test.

Vocabulary used in the proposals
--------------------------------
update        Account is the right facility but needs a fix (parent, name, address, ...).
create        Website location with no CRM account at all.
chow          Account must move to a new parent but billing history forces the SOP:
              leave the old account untouched, create a NEW account under the correct
              parent and point old.chow_current_account at it.
chow_link     Same SOP, but the correct-parent account already exists (e.g. a facility
              that was sold to another operator): only set chow_current_account.
duplicate     Several CRM accounts describe one facility: losing copies are set to
              Inactive with duplicate_of_account = survivor.
orphan        Account under the Bellhaven parent that is no longer on the website and has
              no identifiable successor: flagged Needs Review, never silently changed.
manual        Something the rules refuse to automate (conflicting owner, ...): Needs Review.

The billing SOP (applies to ANY parent move):
    lifetime_revenue > 0 AND outstanding_ar > 0   ->  never change the parent of that account.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------------------
# Normalisation helpers
# --------------------------------------------------------------------------------------
_SUFFIX = {
    "road": "rd", "rd": "rd", "avenue": "ave", "ave": "ave", "av": "ave", "boulevard": "blvd",
    "blvd": "blvd", "street": "st", "st": "st", "drive": "dr", "dr": "dr", "lane": "ln", "ln": "ln",
    "pike": "pike", "pk": "pike", "court": "ct", "ct": "ct", "place": "pl", "pl": "pl",
    "circle": "cir", "cir": "cir", "highway": "hwy", "hwy": "hwy", "parkway": "pkwy",
    "pkwy": "pkwy", "terrace": "ter", "ter": "ter", "saint": "st",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
}
_PO_BOX = re.compile(r"^\s*(p\.?\s*o\.?\s*box|post\s+office\s+box|box)\b", re.I)

CARE_MAP = {  # website label -> CRM care_type
    "short-term rehabilitation & nursing": "Skilled Nursing",
    "assisted living": "Assisted Living",
    "memory support": "Memory Care",
}


def norm_street(s: str) -> str:
    s = re.sub(r"[.,#]", " ", (s or "").lower())
    return " ".join(_SUFFIX.get(t, t) for t in s.split())


def is_po_box(s: str) -> bool:
    return bool(_PO_BOX.match(s or ""))


_NAME_SYN = {"rehab": "rehabilitation", "centre": "center", "&": "and", "healthcare": "health care"}
_NAME_STOP = {"the", "of", "at", "and", "-"}


def norm_name(s: str) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = s.replace("health care", "healthcare")
    toks = [_NAME_SYN.get(t, t) for t in s.split()]
    toks = [t for t in toks if t not in _NAME_STOP]
    return " ".join(toks)


def names_equivalent(a: str, b: str) -> bool:
    """True when two names differ only cosmetically (punctuation, of/at, Rehab/Rehabilitation...)."""
    na, nb = norm_name(a), norm_name(b)
    return na == nb or difflib.SequenceMatcher(None, na, nb).ratio() >= 0.93


def name_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm_name(a), norm_name(b)).ratio()


def _clean_parent(name: str) -> str:
    return re.sub(r"\s*\(Parent Account\)\s*$", "", name or "").strip()


def _canon_action(a: dict) -> dict:
    """Identity of one action. A create is identified by *where* the facility is (street+ZIP+parent),
    so a changed phone number on the website does not make a rejected create look new."""
    if a["op"] == "create":
        f = a["fields"]
        return {"op": "create", "street": norm_street(f.get("billing_street", "")),
                "zip": f.get("billing_zip", ""), "parent_id": f.get("parent_id", "")}
    return {"op": a["op"], "account_id": a["account_id"],
            "fields": {k: v for k, v in sorted(a["fields"].items()) if k != "note"}}


def fmt_money(n) -> str:
    return f"${int(n):,}"


def has_billing_history(a: dict) -> bool:
    """SOP trigger: revenue history AND outstanding AR > 0."""
    return (a.get("lifetime_revenue") or 0) > 0 and (a.get("outstanding_ar") or 0) > 0


def has_any_money(a: dict) -> bool:
    return (a.get("lifetime_revenue") or 0) > 0 or (a.get("outstanding_ar") or 0) > 0


# --------------------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------------------
@dataclass
class Proposal:
    kind: str
    group_key: str                       # facility the proposal belongs to (for the UI)
    group_title: str
    title: str
    summary: str
    confidence: str                      # "high" | "medium"
    actions: list[dict]                  # executable steps (see executor.py)
    diff: list[dict] = field(default_factory=list)       # human readable before/after
    evidence: list[dict] = field(default_factory=list)   # website-vs-CRM comparison rows
    notes: list[str] = field(default_factory=list)       # reasoning, flags (SOP etc.)
    links: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """Stable identity of the *decision* (what would change), independent of evidence text,
        notes (which contain the run date) and optimistic-concurrency 'expect' values.
        A rejected proposal keeps its fingerprint, so re-runs never re-propose it."""
        canon = json.dumps({"kind": self.kind, "actions": [_canon_action(a) for a in self.actions]},
                           sort_keys=True)
        return hashlib.sha1(canon.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint, "kind": self.kind, "group_key": self.group_key,
            "group_title": self.group_title, "title": self.title, "summary": self.summary,
            "confidence": self.confidence, "actions": self.actions, "diff": self.diff,
            "evidence": self.evidence, "notes": self.notes, "links": self.links,
        }


# --------------------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------------------
def _acquired_parent_ids(accounts: list[dict], about_text: str) -> dict[str, str]:
    """Parent accounts the website says Bellhaven absorbed -> {parent_id: evidence sentence}.
    Evidence comes from the About page, e.g. 'In 2025 we welcomed the Harborview Care Group family'."""
    parents = {a["parent_id"]: a["parent_name"] for a in accounts if a.get("parent_id")}
    text = re.sub(r"\s+", " ", about_text or "")
    out = {}
    for pid, pname in parents.items():
        base = _clean_parent(pname)
        words = base.split()
        probe = " ".join(words[:2]) if len(words) >= 2 else base
        if not probe or probe.lower().startswith("bellhaven"):
            continue
        m = re.search(re.escape(probe), text, re.I)
        if m:
            # the sentence containing the mention
            sent = next((s for s in re.split(r"(?<=[.!?])\s+", text) if probe.lower() in s.lower()), text)
            out[pid] = sent.strip()
    return out


def _addr_street_eq(w: dict, a: dict) -> bool:
    return (
        not is_po_box(a["billing_street"])
        and norm_street(a["billing_street"]) == norm_street(w["street"])
        and a["billing_state"].upper() == w["state"].upper()
    )


def candidates_for(w: dict, accounts: list[dict]) -> tuple[list[dict], str]:
    """Return (candidate accounts, how they matched). Ordered strongest rule first."""
    exact = [a for a in accounts if _addr_street_eq(w, a) and a["billing_zip"] == w["zip"]]
    if exact:
        return exact, "address"
    # Same street and city but the ZIP is wrong in the CRM (typo)
    zip_typo = [a for a in accounts
                if _addr_street_eq(w, a) and a["billing_city"].strip().lower() == w["city"].lower()]
    if zip_typo:
        return zip_typo, "address_zip_typo"
    # The CRM only holds a PO Box / blank street: fall back to identical facility name in the same city
    by_name = [a for a in accounts
               if a["billing_city"].strip().lower() == w["city"].lower()
               and a["billing_state"].upper() == w["state"].upper()
               and (is_po_box(a["billing_street"]) or not a["billing_street"].strip())
               and names_equivalent(a["name"], w["name"])]
    if by_name:
        return by_name, "name_city_no_street"
    return [], "none"


def _rank_key(w: dict, a: dict, bh_id: str, contacts: dict[str, int]):
    """Survivor ranking, strongest signal first (sorted descending, then lowest id)."""
    return (
        has_any_money(a),                                       # never lose a financial record
        a.get("parent_id") == bh_id,                            # already under the right parent
        a.get("status") == "Active",
        (a.get("phone") or "") == (w.get("phone") or ""),       # phone identical to website
        contacts.get(a["account_id"], 0),                       # keeps the relationships
        a["billing_street"].strip().lower() == w["street"].strip().lower(),
        round(name_similarity(a["name"], w["name"]), 3),
    )


def pick_survivor(w: dict, cands: list[dict], bh_id: str, contacts: dict[str, int]):
    ranked = sorted(cands, key=lambda a: (tuple(-int(x) if isinstance(x, bool) else -x
                                                for x in _rank_key(w, a, bh_id, contacts)),
                                          a["account_id"]))
    survivor = ranked[0]
    tie = len(ranked) > 1 and _rank_key(w, ranked[0], bh_id, contacts)[:5] == \
        _rank_key(w, ranked[1], bh_id, contacts)[:5]
    return survivor, ranked[1:], tie


def map_care(offerings: list[str]) -> list[str]:
    return [CARE_MAP[o.lower()] for o in offerings if o.lower() in CARE_MAP]


def _cmp_row(label, web, crm, ok=None, note=""):
    return {"label": label, "website": web, "crm": crm, "ok": ok, "note": note}


def _evidence_rows(w: dict, a: dict | None) -> list[dict]:
    if a is None:
        return [
            _cmp_row("Name", w["name"], "—"),
            _cmp_row("Street", w["street"], "—"),
            _cmp_row("City / State / ZIP", f"{w['city']}, {w['state']} {w['zip']}", "—"),
            _cmp_row("Care offerings", ", ".join(w["care_offerings"]), "—"),
            _cmp_row("Phone", w["phone"], "—"),
        ]
    care_ok = a["care_type"] in map_care(w["care_offerings"]) if map_care(w["care_offerings"]) else None
    return [
        _cmp_row("Name", w["name"], a["name"], names_equivalent(w["name"], a["name"]),
                 "" if names_equivalent(w["name"], a["name"]) else "differs"),
        _cmp_row("Street", w["street"], a["billing_street"],
                 norm_street(w["street"]) == norm_street(a["billing_street"]) and not is_po_box(a["billing_street"]),
                 "PO Box in CRM" if is_po_box(a["billing_street"]) else ""),
        _cmp_row("City / State / ZIP", f"{w['city']}, {w['state']} {w['zip']}",
                 f"{a['billing_city']}, {a['billing_state']} {a['billing_zip']}",
                 (w["city"].lower(), w["state"], w["zip"]) ==
                 (a["billing_city"].strip().lower(), a["billing_state"], a["billing_zip"])),
        _cmp_row("Care", ", ".join(w["care_offerings"]), a["care_type"], care_ok),
        _cmp_row("Phone", w["phone"], a["phone"], (w["phone"] == a["phone"]),
                 "identical phone - strong signal" if w["phone"] == a["phone"] else "not used as a match key"),
        _cmp_row("Parent", "Bellhaven Senior Living", a["parent_name"] or "(none)",
                 (a.get("parent_name") or "").startswith("Bellhaven")),
        _cmp_row("Billing", "", f"revenue {fmt_money(a['lifetime_revenue'])} / AR {fmt_money(a['outstanding_ar'])}"),
    ]


def new_account_fields(w: dict, bh_id: str) -> dict:
    care = map_care(w["care_offerings"])
    return {
        "name": w["name"],
        "parent_id": bh_id,
        "billing_street": w["street"],
        "billing_city": w["city"],
        "billing_state": w["state"],
        "billing_zip": w["zip"],
        "care_type": care[0] if care else "Assisted Living",
        "phone": w["phone"],
        "status": "Active",
    }


def build(website: dict, accounts: list[dict], contacts_by_account: dict[str, int] | None = None,
          today: str = "") -> dict:
    """Run the whole matching pass.

    Returns {"proposals": [Proposal...], "report": {...classification...}}
    """
    contacts = contacts_by_account or {}
    locs = website["locations"]
    by_id = {a["account_id"]: a for a in accounts}

    parent_accounts = {a["account_id"]: a for a in accounts if a["account_id"] in
                       {x["parent_id"] for x in accounts if x.get("parent_id")}}
    bh = next((a for a in parent_accounts.values() if a["name"].startswith("Bellhaven")), None)
    if bh is None:
        raise RuntimeError("Bellhaven parent account not found in CRM")
    bh_id = bh["account_id"]
    acquired = _acquired_parent_ids(accounts, website.get("about_text", ""))
    family_parents = {bh_id, ""} | set(acquired)           # parents we are allowed to move accounts out of

    # Accounts already resolved by an earlier run never take part in matching again.
    # (This is what makes the pipeline converge and re-runs safe.)
    resolved = {a["account_id"] for a in accounts
                if a.get("duplicate_of_account") or a.get("chow_current_account")
                or a["account_id"] in parent_accounts}
    pool = [a for a in accounts if a["account_id"] not in resolved]

    proposals: list[Proposal] = []
    report = {"locations": [], "orphans": [], "counts": {}}
    matched_account_ids: set[str] = set()

    for w in sorted(locs, key=lambda x: x["name"]):
        group_key, group_title = w["slug"], w["name"]
        links = {"website": w["url"]}
        cands, how = candidates_for(w, pool)
        entry = {"slug": w["slug"], "name": w["name"], "matched_via": how, "classification": "", "accounts": []}

        # ---------------------------------------------------------------- no CRM account
        if not cands:
            near = [a for a in pool
                    if a["billing_zip"] == w["zip"] or a["billing_city"].strip().lower() == w["city"].lower()]
            near = [a for a in near if name_similarity(a["name"], w["name"]) > 0.45
                    or any(t in norm_name(a["name"]) for t in norm_name(w["name"]).split() if len(t) > 4 and t != "bellhaven")]
            notes = ["No CRM account has this street address (checked street+ZIP, street+city, and "
                     "name+city for PO-Box-only records)."]
            conf = "high"
            for n in pool:  # identical name somewhere else is a name collision, not a match
                if n not in near and names_equivalent(n["name"], w["name"]):
                    notes.append(
                        f"Name collision, not a match: '{n['name']}' ({n['account_id']}) exists in "
                        f"{n['billing_city']}, {n['billing_state']} under {_clean_parent(n['parent_name']) or 'no parent'} "
                        f"- a different city and state than {w['city']}, {w['state']}.")
            for n in near:
                notes.append(
                    f"Considered and rejected: '{n['name']}' ({n['account_id']}, {n['billing_street']}, "
                    f"{n['billing_city']} {n['billing_state']} {n['billing_zip']}, "
                    f"parent: {_clean_parent(n['parent_name']) or 'none'}) - different street address, so a "
                    f"different facility.")
                conf = "medium"
            fields = new_account_fields(w, bh_id)
            if len(map_care(w["care_offerings"])) > 1:
                notes.append("Website lists several care offerings (" + ", ".join(w["care_offerings"]) +
                             "); CRM care_type holds one value, so the first listed was used.")
            proposals.append(Proposal(
                kind="create", group_key=group_key, group_title=group_title,
                title=f"Create account: {w['name']}",
                summary="Location is on the Bellhaven website but has no account in the CRM.",
                confidence=conf, actions=[{"op": "create", "fields": fields}],
                diff=[{"account": "(new account)", "field": k, "old": "", "new": v} for k, v in fields.items()],
                evidence=_evidence_rows(w, None), notes=notes, links=links))
            entry["classification"] = "missing_from_crm"
            report["locations"].append(entry)
            continue

        # ---------------------------------------------------------------- one or more candidates
        # Conflict guard: never touch an account owned by an unrelated operator.
        foreign = [a for a in cands if (a.get("parent_id") or "") not in family_parents]
        if foreign:
            for a in foreign:
                proposals.append(Proposal(
                    kind="manual", group_key=group_key, group_title=group_title,
                    title=f"Ownership conflict: {a['name']}",
                    summary=f"Account sits at a Bellhaven website address but is owned by "
                            f"{_clean_parent(a['parent_name'])}. Needs a human decision.",
                    confidence="medium",
                    actions=[{"op": "patch", "account_id": a["account_id"],
                              "fields": {"status": "Needs Review",
                                         "note": f"Address matches Bellhaven website location '{w['name']}' but parent is "
                                                 f"{_clean_parent(a['parent_name'])}. Verify ownership."},
                              "expect": {"status": a["status"], "note": a["note"]}}],
                    diff=[{"account": a["name"], "field": "status", "old": a["status"], "new": "Needs Review"}],
                    evidence=_evidence_rows(w, a), notes=[], links=links))
            cands = [a for a in cands if a not in foreign]
            if not cands:
                entry["classification"] = "ownership_conflict"
                report["locations"].append(entry)
                continue

        survivor, losers, tie = pick_survivor(w, cands, bh_id, contacts)
        matched_account_ids.update(a["account_id"] for a in cands)
        links["crm"] = [a["account_id"] for a in cands]

        notes: list[str] = []
        conf = "high"
        if how == "address_zip_typo":
            notes.append(f"Street and city match; CRM ZIP {survivor['billing_zip']} vs website {w['zip']} "
                         f"(transposed digits) - treated as a typo.")
        if how == "name_city_no_street":
            notes.append(f"CRM only has a PO Box ('{survivor['billing_street']}'); matched on identical facility name "
                         f"and city" + (", plus identical phone number" if survivor["phone"] == w["phone"] else "") + ".")
            if survivor["phone"] != w["phone"]:
                conf = "medium"

        # ---- what needs fixing on the survivor
        fields: dict = {}
        diff: list[dict] = []

        def add(field_, old, new):
            fields[field_] = new
            diff.append({"account": survivor["name"], "account_id": survivor["account_id"],
                         "field": field_, "old": old, "new": new})

        reasons = []
        sp = survivor.get("parent_id") or ""
        if sp != bh_id:
            if has_billing_history(survivor):
                pass  # handled below via the CHOW (SOP) branch
            else:
                add("parent_id", f"{survivor['parent_name'] or '(none)'}", bh_id)
                diff[-1]["new"] = "Bellhaven Senior Living (Parent Account)"
                diff[-1]["new_id"] = bh_id
                if sp in acquired:
                    notes.append(f"Parent is {_clean_parent(survivor['parent_name'])}; About page: \"{acquired[sp]}\" "
                                 f"and this location is listed on the Bellhaven site, so it belongs under Bellhaven.")
                else:
                    notes.append("Account has no parent; the location is listed on the Bellhaven website.")
                if has_any_money(survivor):
                    notes.append(f"SOP check: revenue {fmt_money(survivor['lifetime_revenue'])}, AR "
                                 f"{fmt_money(survivor['outstanding_ar'])} - AR is not above zero together with revenue "
                                 f"history, so re-parenting the existing account directly is allowed.")
                else:
                    notes.append("SOP check: no revenue history and no AR - safe to re-parent directly.")
        if not names_equivalent(w["name"], survivor["name"]):
            add("name", survivor["name"], w["name"])
            notes.append(f"Name differs from the website ('{survivor['name']}' vs '{w['name']}') while the "
                         f"address is identical"
                         + (" and the phone number matches" if survivor["phone"] == w["phone"] else "")
                         + " - the CRM name is outdated (rebrand / acquisition).")
        if norm_street(w["street"]) != norm_street(survivor["billing_street"]) or is_po_box(survivor["billing_street"]):
            add("billing_street", survivor["billing_street"], w["street"])
        if w["zip"] != survivor["billing_zip"]:
            add("billing_zip", survivor["billing_zip"], w["zip"])
        if w["city"].lower() != survivor["billing_city"].strip().lower():
            add("billing_city", survivor["billing_city"], w["city"])
        care = map_care(w["care_offerings"])
        if care and survivor["care_type"] not in care:
            add("care_type", survivor["care_type"], care[0])
        if survivor["status"] != "Active":
            add("status", survivor["status"], "Active")
            notes.append("Facility is on the live website, so a non-Active account is reactivated.")

        # ---- SOP: parent must change but billing history forbids it
        if sp != bh_id and has_billing_history(survivor):
            new_fields = new_account_fields(w, bh_id)
            proposals.append(Proposal(
                kind="chow", group_key=group_key, group_title=group_title,
                title=f"Change of ownership: {survivor['name']}  ->  new account under Bellhaven",
                summary=f"Account has revenue {fmt_money(survivor['lifetime_revenue'])} and open AR "
                        f"{fmt_money(survivor['outstanding_ar'])}, so the SOP forbids changing its parent. "
                        f"Create a new account under Bellhaven and link the old one to it.",
                confidence="high",
                actions=[
                    {"op": "create", "fields": new_fields, "result_key": "new_account"},
                    {"op": "patch", "account_id": survivor["account_id"],
                     "fields": {"chow_current_account": "$new_account"},
                     "expect": {"parent_id": sp, "lifetime_revenue": survivor["lifetime_revenue"],
                                "outstanding_ar": survivor["outstanding_ar"], "chow_current_account": ""}},
                ],
                diff=[{"account": "(new account)", "field": k, "old": "", "new": v} for k, v in new_fields.items()] +
                     [{"account": survivor["name"], "account_id": survivor["account_id"],
                       "field": "chow_current_account", "old": "", "new": "(id of the new account)"}],
                evidence=_evidence_rows(w, survivor),
                notes=[f"SOP: revenue {fmt_money(survivor['lifetime_revenue'])} AND AR "
                       f"{fmt_money(survivor['outstanding_ar'])} > 0 -> old account is left exactly as it is "
                       f"(parent stays {_clean_parent(survivor['parent_name'])}); billing keeps it.",
                       (f"About page: \"{acquired[sp]}\"" if sp in acquired else "Location is on the Bellhaven website.")]
                      + notes,
                links=links))
            entry["classification"] = "change_of_ownership"
            # a surviving account that cannot be changed + other copies: don't automate dedupe
            for lo in losers:
                proposals.append(_manual_dup_proposal(w, group_key, group_title, lo, survivor, links,
                                                      "survivor needs a change-of-ownership; resolve duplicates after it lands"))
            report["locations"].append(entry)
            continue

        # ---- plain update on the survivor
        if fields:
            patch_action = {"op": "patch", "account_id": survivor["account_id"], "fields": fields,
                            "expect": {k: survivor.get(k) for k in fields}}
            title_bits = ", ".join(sorted({"re-parent" if k == "parent_id" else
                                           "rename" if k == "name" else "fix address" if k.startswith("billing") else k
                                           for k in fields}))
            proposals.append(Proposal(
                kind="update", group_key=group_key, group_title=group_title,
                title=f"Update {survivor['name']}: {title_bits}",
                summary="Right facility, but the CRM record needs correcting.",
                confidence=conf, actions=[patch_action], diff=diff, evidence=_evidence_rows(w, survivor),
                notes=notes, links=links))
            entry["classification"] = "needs_fix"
        else:
            entry["classification"] = "confident_match"
            if how != "address":
                entry["classification"] = "confident_match"

        entry["accounts"].append({"account_id": survivor["account_id"], "role": "survivor"})

        # ---- duplicates
        for lo in losers:
            entry["classification"] = "needs_fix" if entry["classification"] == "confident_match" else entry["classification"]
            if has_billing_history(lo):
                proposals.append(_manual_dup_proposal(w, group_key, group_title, lo, survivor, links,
                                                      "duplicate carries revenue history AND open AR"))
                continue
            dup_notes = [
                f"Same street address as '{survivor['name']}' ({survivor['account_id']}); the website lists the "
                f"facility once.",
                f"Survivor chosen: " + _why_survivor(w, survivor, lo, bh_id, contacts, tie),
                "No merge/delete in the API: loser is set Inactive with duplicate_of_account pointing at the survivor.",
                f"Billing on loser: revenue {fmt_money(lo['lifetime_revenue'])}, AR {fmt_money(lo['outstanding_ar'])} "
                f"(SOP not triggered; its parent is left unchanged).",
            ]
            if contacts.get(lo["account_id"]):
                dup_notes.append(f"Loser has {contacts[lo['account_id']]} contact(s) that stay attached to it "
                                 f"(contacts are not migrated by this pipeline).")
            note_text = (f"Duplicate of {survivor['name']} ({survivor['account_id']}) - same facility listed once on "
                         f"the Bellhaven website. Marked by CRM sync {today}.").strip()
            proposals.append(Proposal(
                kind="duplicate", group_key=group_key, group_title=group_title,
                title=f"Mark duplicate: {lo['name']}  ->  {survivor['name']}",
                summary="Another CRM account already represents this facility.",
                confidence="medium" if tie else "high",
                actions=[{"op": "patch", "account_id": lo["account_id"],
                          "fields": {"status": "Inactive", "duplicate_of_account": survivor["account_id"],
                                     "note": note_text},
                          "expect": {"status": lo["status"], "duplicate_of_account": "", "parent_id": lo["parent_id"]}}],
                diff=[{"account": lo["name"], "account_id": lo["account_id"], "field": "status",
                       "old": lo["status"], "new": "Inactive"},
                      {"account": lo["name"], "account_id": lo["account_id"], "field": "duplicate_of_account",
                       "old": "", "new": survivor["account_id"]},
                      {"account": lo["name"], "account_id": lo["account_id"], "field": "note",
                       "old": lo["note"], "new": note_text}],
                evidence=_evidence_rows(w, lo), notes=dup_notes, links=links))
            entry["accounts"].append({"account_id": lo["account_id"], "role": "duplicate"})
        report["locations"].append(entry)

    # -------------------------------------------------------------------- orphans
    web_addr = {(norm_street(l["street"]), l["zip"]) for l in locs}
    for a in sorted(pool, key=lambda x: x["name"]):
        if a.get("parent_id") != bh_id or a["account_id"] in matched_account_ids:
            continue
        links = {"crm": [a["account_id"]]}
        successors = [o for o in pool if o["account_id"] != a["account_id"]
                      and not is_po_box(a["billing_street"])
                      and norm_street(o["billing_street"]) == norm_street(a["billing_street"])
                      and o["billing_zip"] == a["billing_zip"] and (o.get("parent_id") or "") != bh_id]
        gk, gt = f"orphan:{a['account_id']}", a["name"]
        if successors:
            s = successors[0]
            ev = [_cmp_row("Name", "(not on website)", a["name"]),
                  _cmp_row("Same address", f"{s['billing_street']}, {s['billing_city']} {s['billing_zip']}",
                           f"{a['billing_street']}, {a['billing_city']} {a['billing_zip']}", True),
                  _cmp_row("Parent", _clean_parent(s["parent_name"]) + "  (other account)",
                           _clean_parent(a["parent_name"]) + "  (this account)"),
                  _cmp_row("Billing", "", f"revenue {fmt_money(a['lifetime_revenue'])} / AR {fmt_money(a['outstanding_ar'])}")]
            if has_billing_history(a):
                proposals.append(Proposal(
                    kind="chow_link", group_key=gk, group_title=gt,
                    title=f"Change of ownership: {a['name']}  ->  {s['name']}",
                    summary=f"Facility is no longer on the Bellhaven site and the same address now has an account under "
                            f"{_clean_parent(s['parent_name'])}. Old account has revenue {fmt_money(a['lifetime_revenue'])} "
                            f"and open AR {fmt_money(a['outstanding_ar'])}: preserve it and link it to the new owner's account.",
                    confidence="medium",
                    actions=[{"op": "patch", "account_id": a["account_id"],
                              "fields": {"chow_current_account": s["account_id"]},
                              "expect": {"parent_id": a["parent_id"], "lifetime_revenue": a["lifetime_revenue"],
                                         "outstanding_ar": a["outstanding_ar"], "chow_current_account": ""}}],
                    diff=[{"account": a["name"], "account_id": a["account_id"], "field": "chow_current_account",
                           "old": "", "new": s["account_id"]}],
                    evidence=ev,
                    notes=[f"SOP: revenue {fmt_money(a['lifetime_revenue'])} AND AR {fmt_money(a['outstanding_ar'])} > 0 -> "
                           f"the account is left exactly as it is (still under Bellhaven, still Active) and no new account "
                           f"is created, because '{s['name']}' ({s['account_id']}) already exists under the new owner.",
                           "Inference: not on the Bellhaven website + identical address under another operator = sold. "
                           "Confidence is medium because the CRM itself does not record the sale."],
                    links={**links, "crm": [a["account_id"], s["account_id"]]}))
                report["orphans"].append({"account_id": a["account_id"], "name": a["name"], "classification": "change_of_ownership"})
            else:
                note_text = (f"Duplicate of {s['name']} ({s['account_id']}) - facility moved to "
                             f"{_clean_parent(s['parent_name'])}. Marked by CRM sync {today}.").strip()
                proposals.append(Proposal(
                    kind="duplicate", group_key=gk, group_title=gt,
                    title=f"Mark duplicate: {a['name']}  ->  {s['name']}",
                    summary="Not on the Bellhaven website; same address already has a current account under another operator.",
                    confidence="medium",
                    actions=[{"op": "patch", "account_id": a["account_id"],
                              "fields": {"status": "Inactive", "duplicate_of_account": s["account_id"], "note": note_text},
                              "expect": {"status": a["status"], "duplicate_of_account": "", "parent_id": a["parent_id"]}}],
                    diff=[{"account": a["name"], "account_id": a["account_id"], "field": "status", "old": a["status"], "new": "Inactive"},
                          {"account": a["name"], "account_id": a["account_id"], "field": "duplicate_of_account", "old": "", "new": s["account_id"]}],
                    evidence=ev, notes=["No billing history, so marking the stale copy inactive is safe."], links=links))
                report["orphans"].append({"account_id": a["account_id"], "name": a["name"], "classification": "duplicate_of_other_owner"})
            continue
        if a["status"] == "Needs Review":
            continue  # already flagged by an earlier run
        note_text = (f"Not listed on the Bellhaven website as of {today}. Possible sale, closure or rebrand - "
                     f"verify before any change.").replace("  ", " ")
        proposals.append(Proposal(
            kind="orphan", group_key=gk, group_title=gt,
            title=f"Flag for review: {a['name']} is no longer on the website",
            summary="Account is under the Bellhaven parent but the location is not on the website any more, and no "
                    "other account exists at its address.",
            confidence="medium",
            actions=[{"op": "patch", "account_id": a["account_id"],
                      "fields": {"status": "Needs Review", "note": note_text},
                      "expect": {"status": a["status"], "note": a["note"]}}],
            diff=[{"account": a["name"], "account_id": a["account_id"], "field": "status", "old": a["status"], "new": "Needs Review"},
                  {"account": a["name"], "account_id": a["account_id"], "field": "note", "old": a["note"], "new": note_text}],
            evidence=[_cmp_row("On website?", "No (35 locations checked)", "Account is under Bellhaven"),
                      _cmp_row("Address", "", f"{a['billing_street']}, {a['billing_city']}, {a['billing_state']} {a['billing_zip']}"),
                      _cmp_row("Billing", "", f"revenue {fmt_money(a['lifetime_revenue'])} / AR {fmt_money(a['outstanding_ar'])}")],
            notes=["Not deactivated and parent not touched: the website alone cannot tell sale from closure, "
                   "so a human decides. The status change only makes it visible to the sales team."],
            links=links))
        report["orphans"].append({"account_id": a["account_id"], "name": a["name"], "classification": "needs_review"})

    # -------------------------------------------------------------------- counts
    c: dict[str, int] = {}
    for e in report["locations"]:
        c[e["classification"]] = c.get(e["classification"], 0) + 1
    report["counts"] = {"locations": len(locs), **c,
                        "orphans": len(report["orphans"]), "proposals": len(proposals)}
    return {"proposals": proposals, "report": report}


def _why_survivor(w, s, lo, bh_id, contacts, tie) -> str:
    bits = []
    if s.get("parent_id") == bh_id and lo.get("parent_id") != bh_id:
        bits.append("already under the Bellhaven parent")
    if s["phone"] == w["phone"] and lo["phone"] != w["phone"]:
        bits.append("its phone number matches the website")
    if contacts.get(s["account_id"], 0) > contacts.get(lo["account_id"], 0):
        bits.append(f"it holds the contacts ({contacts.get(s['account_id'], 0)} vs {contacts.get(lo['account_id'], 0)})")
    if has_any_money(s) and not has_any_money(lo):
        bits.append("it carries the billing history")
    if not bits:
        bits.append("candidates were otherwise indistinguishable (same address, no contacts, no phone match); "
                    "picked the one whose street text/name is closest to the website, then lowest id - "
                    "reviewer may prefer a different survivor")
    return "; ".join(bits)


def _manual_dup_proposal(w, group_key, group_title, lo, survivor, links, why) -> Proposal:
    note_text = f"Possible duplicate of {survivor['name']} ({survivor['account_id']}) but not auto-resolved: {why}."
    return Proposal(
        kind="manual", group_key=group_key, group_title=group_title,
        title=f"Needs a human: possible duplicate {lo['name']}",
        summary=f"Not automated because {why}.", confidence="medium",
        actions=[{"op": "patch", "account_id": lo["account_id"],
                  "fields": {"status": "Needs Review", "note": note_text},
                  "expect": {"status": lo["status"], "note": lo["note"]}}],
        diff=[{"account": lo["name"], "account_id": lo["account_id"], "field": "status", "old": lo["status"], "new": "Needs Review"}],
        evidence=_evidence_rows(w, lo), notes=[why], links=links)
