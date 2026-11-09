#!/usr/bin/env python3
"""Pull the candidate's jobs from AIHawk's public job database and score them.

The database (https://huggingface.co/datasets/feder-cr/jobs) merges 37 public sources, about 10M postings, and is
rebuilt every five hours. DuckDB reads only the columns it needs, over HTTP, so nothing big is downloaded.

Usage: .venv/bin/python scripts/fetch_jobs.py [--days 45]
Writes data/jobs.json, which the dashboard (app/) reads.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = "https://huggingface.co/datasets/feder-cr/jobs/resolve/main/manifest.json"

# --- What counts as the candidate's kind of job (titles, lowercase regex) ---------------------------------------------------------
TRACKS = [  # (track, points, regex) — first match wins
    ("Product / UI-UX design", 3, r"product design|\bux\b|\bui\b|ui/ux|ux/ui|user experience|interaction design|web design|digital product design"),
    ("Frontend / design engineer", 3, r"front[- ]?end.{0,25}(engineer|developer|dev\b)|(software|web).{0,20}front[- ]?end|\bui engineer|\bux engineer|"
     r"(product|ux|ui|web|software|creative|frontend) design engineer|design engineer.{0,10}(ux|ui|web|frontend|product)|design technologist|web developer|ui developer|web designer/developer"),
    ("Creative technology", 2, r"creative technolog|prototyp|experience design|immersive|interactive design|motion design|digital design(er)?\b"),
    ("Visual / brand design", 1, r"visual design|graphic design|brand design|content design"),
]
INCLUDE = "|".join(f"({r})" for _, _, r in TRACKS)
EARLY = r"new grad|early career|junior|\bjr\b|associate|entry|intern|co-?op|apprentic|graduate|university|college|2027|rotational|fellow|\b(i|1)\b\s*$|designer i\b|level 1"
EXCLUDE = (
    r"senior|\bsr\b|staff|principal|\blead\b|manager|director|head of|\bvp\b|architect|phd|"
    r"mechanical|electrical|hardware|asic|fpga|silicon|circuit|analog|rf |structural|civil|hvac|piping|process design|"
    r"product design engineer|physical design|layout|cad\b|tooling|packaging engineer|\.net|java\b|salesforce|sap\b|"
    r"software (engineer|engineering|developer|development)|\bswe\b|\bsde\b|backend|back-end|full[- ]?stack|"
    r"2028|class of 2028|28卒|research(er)?\b|cashier|sales associate|front[- ]?end associate|merchandis|store|shopper|"
    r"retail|clerk|front end entry level|highway|traffic|water|sanitation|automotive|aerospace|"
    r"pflichtpraktikum|werkstudent|praktikum|stagiaire|alternance"
)
US_STATES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC "
    "SD TN TX UT VT VA WA WV WI WY DC"
).split()
US_RE = re.compile(
    r"united states|\busa?\b|\bu\.s\.|remote.{0,12}(us|united states|america)|new york|nyc|brooklyn|manhattan|san francisco|"
    r"seattle|austin|boston|chicago|los angeles|any state",
    re.I,
)
STATE_RE = re.compile("|".join(rf",\s*{s}\b|\bUS-{s}\b" for s in US_STATES))  # case-sensitive: ", IN" is Indiana, ", in" is India
NYC_RE = re.compile(
    r"new york city|\bnyc\b|brooklyn|manhattan|queens|bronx|staten island|jersey city|hoboken|long island city|flushing|"
    r"(^|[;/(]\s*)new york(\s*,\s*(ny|new york|us-ny|united states|us)\b|\s*$|\s*[;/)])",
    re.I,
)
REMOTE_RE = re.compile(r"\bremote\b", re.I)
NON_US_RE = re.compile(
    r"canada|toronto|vancouver|montreal|london|united kingdom|\buk\b|india|bangalore|bengaluru|germany|berlin|poland|"
    r"france|paris|spain|netherlands|amsterdam|ireland|dublin|singapore|japan|tokyo|china|brazil|mexico|philippines|"
    r"australia|sydney|israel|egypt|serbia|portugal|romania|ukraine|korea|vietnam|pakistan|nigeria|argentina|colombia|"
    r"ontario|quebec|british columbia|alberta|ho chi minh|hanoi|manila|lagos|nairobi|cairo|dubai|emea|apac|latam|europe",
    re.I,
)
# A resume file label per track (only used if you keep separate resume files).
RESUME = {
    "Product / UI-UX design": "digital-design.pdf",
    "Frontend / design engineer": "digital-design.pdf",
    "Creative technology": "creative-technology.pdf",
    "Visual / brand design": "graphic-design.docx",
}
NO_PLACE = ("", "remote", "anywhere", "remote - anywhere", "fully remote", "work from home")
MUSEUM_RE = re.compile(r"museum|gallery|moma|smithsonian|guggenheim|whitney|cooper hewitt", re.I)


def track_of(title: str) -> tuple[str, int]:
    t = title.lower()
    for name, pts, rx in TRACKS:
        if re.search(rx, t):
            return name, pts
    return "Other", 0


def job_type(title: str) -> tuple[str, float]:
    t = title.lower()
    if re.search(r"co-?op|apprentic|rotational|fellow", t):
        return "Co-op / apprenticeship", 1.5
    if re.search(r"intern", t):
        return "Internship", 0.5
    return "Full-time", 2.0


def place(location: str, remote: bool | None) -> tuple[str, int, bool, bool]:
    loc = location or ""
    nyc = bool(NYC_RE.search(loc))
    is_remote = bool(remote) or bool(REMOTE_RE.search(loc))
    if nyc:
        return "NYC", 2, True, is_remote
    if is_remote and not NON_US_RE.search(loc) and (US_RE.search(loc) or STATE_RE.search(loc) or loc.strip().lower() in NO_PLACE):
        return "Remote (US)", 2, False, True
    return "US", 1, False, is_remote


def h1b_points(company: str, h1b: dict) -> tuple[float, str]:
    info = h1b.get(norm(company))
    if not info:
        return 0.5, "Unknown"
    if info.get("cap_exempt"):
        return 2.0, "Cap-exempt"
    if info.get("design", 0) > 0:
        return 1.5, f"{info['design']} designer LCAs"
    if info.get("total", 0) > 0:
        return 1.0, f"{info['total']} LCAs (none design)"
    return 0.5, "None found (name may not match the DOL record)"


def norm(company: str) -> str:
    c = (company or "").lower()
    c = re.sub(r"[-_](early[-_]careers?|careers?|jobs|us|inc)$", "", c)
    c = re.sub(r"[^a-z0-9 ]", " ", c)
    c = re.sub(r"\b(inc|llc|corp|corporation|co|ltd|technologies|technology|labs|the)\b", " ", c)
    return re.sub(r"\s+", " ", c).strip()


def flags(title: str) -> list[str]:
    t = title.lower()
    out = []
    if re.search(r"(spring|fall|winter)\s*20?27|20?27\s*(spring|fall|winter)|co-?op", t):
        out.append("During school year: needs CPT")
    if re.search(r"summer\s*2027|2027\s*summer", t) and "intern" in t:
        out.append("Summer 2027: check it doesn't require returning to school")
    return out


AGGREGATORS = re.compile(
    r"simplify\.jobs|builtin|indeed\.|ziprecruiter|glassdoor|newgrad-jobs|jobright|dice\.com|monster\.|careerbuilder|"
    r"wellfound|angel\.co|handshake|teal|mediabistro|talents\.vaia|startup\.jobs|remoteok|weworkremotely|jobicy|"
    r"themuse|levels\.fyi|otta|welcometothejungle|jooble|adzuna|snagajob|linkedin|swejoblists|hiring\.cafe|jobgether",
    re.I,
)


def link_source(url: str) -> str:
    """'company' when the link is the employer's own careers page or application form (incl. their ATS), else 'board'."""
    return "board" if AGGREGATORS.search(url or "") else "company"


def season(title: str, jtype: str) -> str:
    t = title.lower()
    if jtype == "Full-time":
        return "Full-time"
    for name, rx in (("Spring", r"spring"), ("Winter", r"winter"), ("Fall", r"\bfall\b|autumn")):
        if re.search(rx, t):
            return f"{name} (during school: needs CPT)"
    if "summer" in t:
        return "Summer"
    if re.search(r"co-?op", t):
        return "Co-op (check dates; may need CPT)"
    return "Not stated"


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")[:25]).replace(tzinfo=None)
    except ValueError:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
        return datetime.fromisoformat(m.group(1)) if m else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45, help="skip postings older than this (undated ones are kept)")
    args = ap.parse_args()

    manifest = json.load(urllib.request.urlopen(MANIFEST, timeout=60))
    index = f"{manifest['base_url']}/{manifest['index']['name']}"
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    rows = con.execute(
        f"""SELECT title, company, location, country, remote, salary_min, salary_max, salary_currency, posted_at, url, ats
            FROM read_parquet(?)
            WHERE regexp_matches(lower(title), ?) AND regexp_matches(lower(title), ?)
              AND NOT regexp_matches(lower(title), ?)""",
        [index, INCLUDE, EARLY, EXCLUDE],
    ).fetchall()

    h1b_path = ROOT / "data" / "h1b.json"
    h1b = json.loads(h1b_path.read_text()) if h1b_path.exists() else {}
    seen_urls = set(re.findall(r"https?://\S+", (ROOT / "seen-jobs.md").read_text()))
    cutoff = datetime.now() - timedelta(days=args.days)
    jobs: dict[tuple, dict] = {}
    for title, company, location, country, remote, smin, smax, cur, posted, url, ats in rows:
        loc = location or ""
        ctry = (country or "").strip().upper()
        if ctry and ctry not in ("US", "USA", "UNITED STATES"):
            continue
        if re.search(r",\s*[a-z]{2}\s*$", loc) and not re.search(r",\s*us\s*$", loc):  # lowercase country code, e.g. "München, BY, de"
            continue
        us = bool(ctry) or ((bool(US_RE.search(loc)) or bool(STATE_RE.search(loc))) and not NON_US_RE.search(loc))
        if not us and not (remote and not NON_US_RE.search(loc) and loc.strip().lower() in NO_PLACE):
            continue
        when = parse_date(posted)
        if when and when < cutoff:
            continue
        if re.search(r"part[- ]?time|\(pt\)", title, re.I) and not re.search(r"intern|co-?op", title, re.I):
            continue  # part-time is only OK for semester internships
        track, tpts = track_of(title)
        if not tpts:
            continue
        jtype, ppts = job_type(title)
        where, lpts, nyc, is_remote = place(loc, remote)
        spts, slabel = h1b_points(company or "", h1b)
        fit = math.floor(tpts + ppts + lpts + spts + 1)  # floor: unverified jobs are scored conservatively
        posting_id = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f-]{27}|\d{6,})", url or "")
        key = ("id", posting_id.group(1)) if posting_id else (norm(company or "").replace(" ", ""), re.sub(r"\W+", " ", title.lower()).strip(), where)
        resume = "museum-experience.docx" if MUSEUM_RE.search(company or "") else RESUME[track]
        job = {
            "id": hashlib.sha1(url.encode()).hexdigest()[:12],
            "title": title.strip(),
            "company": (company or "Unknown").strip(),
            "location": loc.strip() or ("Remote" if is_remote else "Not stated"),
            "where": where,
            "nyc": nyc,
            "remote": is_remote,
            "track": track,
            "type": jtype,
            "fit": min(fit, 10),
            "fitBreakdown": {"track": tpts, "path": ppts, "location": lpts, "sponsorship": spts, "level": 1},
            "h1b": slabel,
            "salary": fmt_salary(smin, smax, cur),
            "posted": when.date().isoformat() if when else None,
            "url": url,
            "ats": ats,
            "resume": resume,
            "flags": flags(title),
            "season": season(title, jtype),
            "linkSource": link_source(url),
            "reported": url in seen_urls,
        }
        old = jobs.get(key)
        if not old or (job["posted"] or "") > (old["posted"] or ""):
            jobs[key] = job

    # Second pass: the same posting under two company spellings ("thenewyorktimes" / "The New York Times", "konradgroup" / "Konrad").
    merged: dict[tuple, dict] = {}
    for j in jobs.values():
        comp = re.sub(r"^the|group$|careers$|inc$", "", norm(j["company"]).replace(" ", ""))
        k2 = (comp, re.sub(r"\W+", " ", j["title"].lower()).strip())
        if k2 not in merged or (j["posted"] or "") > (merged[k2]["posted"] or ""):
            merged[k2] = j
    out = sorted(merged.values(), key=lambda j: (-j["fit"], j["posted"] is None, -(int((j["posted"] or "0000-00-00").replace("-", "")))))
    payload = {
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "database_build": manifest.get("build"),
        "database_rows": manifest.get("rows_out"),
        "days": args.days,
        "count": len(out),
        "jobs": out,
    }
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "jobs.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"{len(rows)} title matches → {len(out)} US jobs kept → data/jobs.json")
    companies: dict[str, int] = {}
    for j in out:
        companies[j["company"]] = companies.get(j["company"], 0) + 1
    unknown = [c for c in companies if norm(c) not in h1b]
    print(f"{len(unknown)} companies have no H-1B lookup yet: run scripts/h1b_lookup.py to fill them in.")


def fmt_salary(lo, hi, cur) -> str | None:
    if not lo and not hi:
        return None
    sym = "$" if (cur or "USD").upper() == "USD" else f"{cur} "

    def f(v):
        if v is None:
            return "?"
        return f"{sym}{v / 1000:.0f}k" if v >= 1000 else f"{sym}{v:.0f}/hr"

    return f(lo) if not hi or lo == hi else f"{f(lo)}–{f(hi)}"


if __name__ == "__main__":
    main()
