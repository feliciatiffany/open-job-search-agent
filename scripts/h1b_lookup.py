#!/usr/bin/env python3
"""Look up H-1B filing history (DOL LCA data via h1bdata.info) for companies in data/jobs.json.

Usage: .venv/bin/python scripts/h1b_lookup.py [--max 80]
Updates data/h1b.json (cached; companies already there are skipped), then re-run fetch_jobs.py to rescore.
An LCA shows the employer has filed for that kind of role — not that a given posting sponsors.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_jobs import norm  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CAP_EXEMPT = re.compile(r"university|college|school of|institute of technology|research foundation|hospital|medical center|langone|mount sinai|sloan kettering", re.I)


def lookup(company: str) -> dict:
    name = re.sub(r"[-_](early[-_]careers?|careers?|jobs)$", "", company, flags=re.I).replace("-", " ")
    url = "https://h1bdata.info/index.php?" + urllib.parse.urlencode({"em": name, "job": "", "city": "", "year": "All Years"})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (job-search bot)"})
    html = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "ignore")
    design = total = 0
    years: set[str] = set()
    example = None
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [re.sub("<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) < 5:
            continue
        total += 1
        years.add(cells[4][-4:])
        t = cells[1].upper()
        if ("DESIGN" in t and "ENGINEER" not in t) or "UX" in t or "USER EXPERIENCE" in t:
            design += 1
            example = example or f"{cells[1].title()}, {cells[3].title()}, {cells[4][-4:]}, ${cells[2]}"
    ys = sorted(y for y in years if y.isdigit())
    return {
        "name": company,
        "design": design,
        "total": total,
        "years": f"{ys[0]}–{ys[-1]}" if ys else "",
        "example": example,
        "cap_exempt": bool(CAP_EXEMPT.search(company)),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=80, help="most companies to look up this run (highest-fit first)")
    args = ap.parse_args()
    jobs = json.loads((ROOT / "data" / "jobs.json").read_text())["jobs"]
    path = ROOT / "data" / "h1b.json"
    cache = json.loads(path.read_text()) if path.exists() else {}
    todo: list[str] = []
    for j in jobs:  # already sorted best-first
        k = norm(j["company"])
        if k and k not in cache and j["company"] not in todo and j["company"] != "Unknown":
            todo.append(j["company"])
    todo = todo[: args.max]
    print(f"Looking up {len(todo)} companies…")

    def one(c: str):
        try:
            return c, lookup(c)
        except Exception as e:  # network hiccup: leave it for next time
            return c, {"error": str(e)}

    with ThreadPoolExecutor(6) as ex:
        for c, info in ex.map(one, todo):
            if "error" not in info:
                cache[norm(c)] = info
                print(f"  {c}: {info['design']} design / {info['total']} total LCAs")
    path.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    print("Saved data/h1b.json. Re-run fetch_jobs.py to rescore.")


if __name__ == "__main__":
    main()
