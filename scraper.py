from __future__ import annotations
"""Scrape every Bellhaven community from the public website.

Sources of truth, in order of discovery:
  1. /communities?page=N  (paginated directory; we trust the "Page X of Y" label,
     NOT the absence of a next link, because out-of-range pages silently
     return the last page again)
  2. Links on the homepage (the directory is NOT guaranteed to be complete: a
     brand new community can be announced on the homepage before it is in the
     directory)
Every discovered /communities/<slug> detail page is then fetched and parsed.
"""
import json
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

from config import SITE_BASE, WEBSITE_FILE

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "bellhaven-crm-sync/1.0"


def get(path: str) -> str:
    url = path if path.startswith("http") else SITE_BASE + path
    last = None
    for attempt in range(4):
        try:
            r = SESSION.get(url, timeout=30)
            if r.status_code == 200:
                return r.text
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:  # network blip
            last = str(e)
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def slug_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        m = re.fullmatch(r"/communities/([a-z0-9\-]+)", a["href"].strip())
        if m:
            out.append(m.group(1))
    return out


def discover_slugs() -> dict[str, str]:
    """Return {slug: where-it-was-found}."""
    found: dict[str, str] = {}
    page, total_pages, declared_total = 1, None, None
    while True:
        html = get(f"/communities?page={page}")
        m = re.search(r"Page (\d+) of (\d+)\s*·\s*(\d+) communities listed", html)
        if m:
            total_pages, declared_total = int(m.group(2)), int(m.group(3))
        for s in slug_links(html):
            found.setdefault(s, f"directory:page{page}")
        if total_pages is None or page >= total_pages:
            break
        page += 1

    home = get("/")
    for s in slug_links(home):
        found.setdefault(s, "homepage")

    in_dir = sum(1 for v in found.values() if v.startswith("directory"))
    if declared_total is not None and in_dir != declared_total:
        print(f"WARN: directory declares {declared_total} but {in_dir} slugs parsed", file=sys.stderr)
    return found


def parse_detail(slug: str, html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    name = h1.get_text(" ", strip=True) if h1 else ""
    dl = soup.find("dl", class_="detail")
    fields = {}
    if dl:
        for dt in dl.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            if dd is None:
                continue
            key = dt.get_text(" ", strip=True).lower()
            if key == "address":
                lines = [t.strip() for t in dd.get_text("\n").split("\n") if t.strip()]
                fields["address_lines"] = lines
            elif key == "care offerings":
                fields["care"] = [b.get_text(" ", strip=True) for b in dd.find_all("span", class_="badge")]
            else:
                fields[key] = dd.get_text(" ", strip=True)

    street = city = state = zipc = ""
    lines = fields.get("address_lines", [])
    if lines:
        street = lines[0]
        tail = lines[-1] if len(lines) > 1 else ""
        m = re.match(r"(.+?),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?$", tail)
        if m:
            city, state, zipc = m.group(1).strip(), m.group(2), m.group(3)
    return {
        "slug": slug,
        "url": f"{SITE_BASE}/communities/{slug}",
        "name": name,
        "street": street,
        "city": city,
        "state": state,
        "zip": zipc,
        "care_offerings": fields.get("care", []),
        "administrator": fields.get("administrator", ""),
        "phone": fields.get("phone", ""),
    }


def scrape() -> list[dict]:
    slugs = discover_slugs()
    out = []
    for slug, where in slugs.items():
        rec = parse_detail(slug, get(f"/communities/{slug}"))
        rec["discovered_via"] = where
        if not (rec["name"] and rec["street"] and rec["city"] and rec["state"] and rec["zip"]):
            print(f"WARN: incomplete parse for {slug}: {rec}", file=sys.stderr)
        out.append(rec)
    return out


def scrape_about() -> str:
    """Plain text of the About page. It names the companies Bellhaven absorbed,
    which the matcher uses as evidence that Harborview / Cedar Trail children
    now belong under the Bellhaven parent."""
    soup = BeautifulSoup(get("/about"), "html.parser")
    wrap = soup.find("div", class_="wrap")
    return wrap.get_text(" ", strip=True) if wrap else ""


def scrape_all() -> dict:
    locs = scrape()
    if len(locs) < 5:
        # A broken scrape must never be allowed to turn every CRM account into an "orphan".
        raise RuntimeError(f"suspiciously few locations scraped ({len(locs)}); aborting")
    return {
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base": SITE_BASE,
        "about_text": scrape_about(),
        "locations": locs,
    }


def main():
    data = scrape_all()
    WEBSITE_FILE.write_text(json.dumps(data, indent=1))
    locs = data["locations"]
    print(f"scraped {len(locs)} locations -> {WEBSITE_FILE}")
    via_home = [l["name"] for l in locs if l["discovered_via"] == "homepage"]
    if via_home:
        print("only on homepage (not in directory):", via_home)


if __name__ == "__main__":
    main()
