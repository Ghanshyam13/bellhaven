# Bellhaven → CRM ownership sync

Keeps the CRM's picture of Bellhaven Senior Living accurate: scrape the website, match every location to a CRM
account, propose fixes, and let a human approve each change before anything is written.

```
website ──scraper.py──┐
                      ├─► matcher.py ─► proposals (SQLite review queue) ─► review app ─approve─► executor.py ─► CRM API
CRM API ──crm.py──────┘                      ▲ pipeline.py (daily)            app.py
```

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env            # paste your token into BH_CRM_TOKEN
python pipeline.py              # scrape + match + fill the review queue (never writes to the CRM)
python app.py                   # review app -> http://127.0.0.1:5055
python -m unittest tests.test_e2e tests.test_app    # verification against a local mock CRM
```

`BH_DRY_RUN=1 python app.py` shows the exact API calls an approval would make without making them.

## What the pipeline found (35 website locations, 121 CRM accounts)

| Outcome | Count | Examples |
|---|---|---|
| Confident match, nothing to do | 16 | Altoona, Toledo, Grove City … (cosmetic name/street variants ignored) |
| Fix on a correct match | 9 | Lima, Findlay, Zanesville (parent), Chagrin Falls, Chesterton, Willow Creek (renamed), Ashtabula (PO Box), Portsmouth (ZIP typo) |
| Duplicates (7 losing copies) | 7 | Kettering ×3, Monroe ×3, Erie ×2, Port Clinton ×2, Owosso ×2 — one survivor each |
| Missing from CRM → create | 4 | Batavia, Carlisle PA, Hudson (Amberly Manor), New Albany (Union Square) |
| Change of ownership (billing SOP) | 3 | Tiffin, Marietta (new account + pointer), Sandusky (pointer to existing Millstone account) |
| Under Bellhaven, gone from website | 2 | Alliance, Coldwater → flagged *Needs Review* |

25 proposals in total. Each carries its evidence (website value vs CRM value), the reasoning, and a confidence level.

## How matching works

1. **Address is the key**, not the name. Street is normalised (`Boulevard`=`Blvd`, `Northwest`=`NW`, `Pike`=`Pk`…)
   and compared with the ZIP. Names are unreliable here: the whole point is that ownership changes rename facilities.
2. **Fallbacks, in order:** same street + city but wrong ZIP (typo); then a CRM record that only has a *PO Box*,
   matched on identical name + city. Phone is **not** a match key (most phones differ between site and CRM) but an
   identical phone is shown as supporting evidence and used as a tie-breaker.
3. **The About page is evidence.** It says Bellhaven absorbed Harborview Care Group (2025) and select Cedar Trail
   communities (2026). That is what justifies moving a Harborview/Cedar child that *is* on the website under
   Bellhaven. An account at a website address owned by any *other* operator is never touched automatically — it
   becomes a "needs a human" item.
4. **Directory ≠ complete.** The directory lists 34 communities; the homepage says 35 and links *Bellhaven Meadows
   of Findlay*, which is not in the directory. The scraper reads both. It also paginates from the
   "Page X of Y" label because `?page=4` silently returns page 3.

## Judgment calls (please read)

- **The billing SOP is enforced twice**: the matcher routes any parent move on an account with revenue **and** AR > 0
  to a change of ownership, *and* `executor.py` re-reads the live account and refuses such a PATCH at write time.
  - *Tiffin* ($84,000 revenue / $12,400 AR) and *Marietta* ($51,250 / $3,800), both under Cedar Trail: old accounts are
    untouched except `chow_current_account`; a new account is created under Bellhaven.
  - *Lima* ($47,000 revenue, AR 0) and *Findlay* ($22,000, AR 0): revenue alone does not trigger the SOP → re-parented directly.
  - *Sandusky* ($130,000 / $5,200): not on the Bellhaven site, and **Millstone Care of Sandusky already exists at the
    identical address**. I read that as a sale. The old account stays as is (still Bellhaven, still Active) and only
    gets `chow_current_account` → the Millstone account. No new account is created because the correct one exists.
    Confidence is *medium*: the CRM does not record the sale, I inferred it.
- **Duplicates / survivor choice.** Survivor = account with billing history, else already under Bellhaven, else phone
  equals the website, else the one holding contacts, else closest street/name. Losers become `Inactive` +
  `duplicate_of_account`, their parent untouched. Owosso (phone) and Monroe/Erie/Port Clinton (already Bellhaven, hold
  the contacts) are clear. **Kettering is a coin flip**: three accounts under three different owners, none with contacts
  or a matching phone. The pipeline picks one deterministically and marks those two proposals *medium confidence* —
  a reviewer who prefers another survivor should reject and fix by hand.
  Contacts on a losing copy are *not* migrated.
- **Near misses that are deliberately not matches:** `Union Square Senior Living` (Juniper Point, same ZIP, different
  street → *Bellhaven at Union Square* gets a create, flagged medium), `Maplewood Senior Care Center` (Stonebridge, same
  town, different street), `Amberly Manor` in Colorado Springs (same name as the Hudson, OH location), and
  `Bellhaven of New Carlisle` vs the Carlisle, PA location.
- **Orphans (Alliance, Coldwater)** are set to *Needs Review* with a dated note, not Inactive and not re-parented:
  disappearing from a website cannot tell a sale from a closure.
- **Cosmetic name differences are not "fixes"** (`Rehab and` vs `Rehabilitation &`, `at` vs `of`, `Healthcare Centre`).
  Only real renames are proposed.
- Care type is compared through a mapping (Short-Term Rehabilitation & Nursing = Skilled Nursing, Memory Support =
  Memory Care). No disagreements were found. Erie and Findlay list two offerings; the CRM holds one value, so creates
  use the first.

## Re-runs are safe

- Every proposal has a **fingerprint** of *what would change* (target account + field values; run-date text and
  evidence are excluded). The `proposals` table keeps it unique.
- Rejected / applied / failed rows are never turned back into pending items. Pending rows are refreshed; pending rows
  that stop applying (someone fixed the CRM) go *stale*.
- The matcher is also **state-based**: accounts already marked duplicate or carrying `chow_current_account` leave the
  candidate pool, flagged orphans are skipped, so once changes land the matcher simply stops producing them.
  `tests/test_e2e.py` proves it: approve everything except one rejection → second run has 0 pending, third run makes 0 writes.
- **The pipeline never writes to the CRM**; only an approval does. A broken scrape cannot damage data: fewer than 5
  locations, or more than 25% of Bellhaven accounts looking orphaned, aborts the run before anything is queued.
- Approval is locked (`pending → applying`) so a double click cannot write twice; creates adopt an existing account
  at the same address instead of duplicating; a two-step change (create + link) resumes where it stopped; every write is
  read back; if the live record changed since the proposal, nothing is written and the item goes *stale*.

## Scheduling

`.github/workflows/daily-sync.yml` (cron `17 11 * * *`, plus manual dispatch) or `crontab.example` for a server.
Caveat I would fix before real production: the decision store is SQLite. On an ephemeral runner it survives via the
Actions cache; in production I would put it in a managed Postgres and host the review app next to it.

## Known limits

- No contact migration, and no care-type/phone sync beyond what is listed above.
- The `POST /accounts` response shape is undocumented, so the executor falls back to looking the new account up by
  address if no id is returned. It has been exercised against a local mock of the API, **not** against a live POST.
- Website scraping depends on the current HTML (`dl.detail`, card links). Layout changes raise an error instead of
  producing partial data.
