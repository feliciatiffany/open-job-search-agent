#!/usr/bin/env python3
"""Pull jobs that match profile/settings.json → "search" from AIHawk's public job database and score them.

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

# --- What counts as the candidate's kind of job: all of it comes from profile/settings.json → "search" --------------------
SETTINGS = json.loads((ROOT / "profile" / "settings.json").read_text(encoding="utf-8"))
SEARCH = SETTINGS.get("search", {})
TRACKS = [(t["name"], t.get("points", 1), t["titles"]) for t in SEARCH.get("tracks", [])]  # first match wins
if not TRACKS:
    raise SystemExit('profile/settings.json needs a "search" → "tracks" list. See profile/examples/.')
INCLUDE = "|".join(f"({r})" for _, _, r in TRACKS)
LEVEL = SEARCH.get("level", "any")  # "entry" (new grad, junior, intern) · "any" · "experienced" (skip intern/junior)
EARLY = r"new grad|early career|junior|\bjr\b|associate|entry|intern|co-?op|apprentic|graduate|university|college|rotational|fellow|\b(i|1)\b\s*$|level 1"
SENIOR = r"senior|\bsr\b|staff|principal|\blead\b|manager|director|head of|\bvp\b|architect|phd"
JUNK = (  # titles that slip through any keyword search: retail, foreign-language internships…
    r"cashier|sales associate|merchandis|\bstore\b|shopper|retail clerk|\bclerk\b|"
    r"pflichtpraktikum|werkstudent|praktikum|stagiaire|alternance|28卒"
)
EXCLUDE = "|".join(x for x in (JUNK, SEARCH.get("exclude_titles", ""), SENIOR if LEVEL == "entry" else "",
                                 r"intern|co-?op|new grad|junior|\bjr\b|entry" if LEVEL == "experienced" else "") if x)
LEVEL_RX = EARLY if LEVEL == "entry" else "."
COUNTRY = (SEARCH.get("country") or "US").upper()  # a country code like "US", "GB", "DE", or "ANY"
COUNTRY_NAMES = {COUNTRY, *(n.upper() for n in SEARCH.get("country_names", []))}  # other spellings, plus cities to match in locations
COUNTRY_RE = re.compile("|".join(re.escape(n) for n in SEARCH.get("country_names", [])) or "$^", re.I)
HOME = SEARCH.get("home") or {}
HOME_LABEL = HOME.get("label", "")
NEEDS_VISA = bool(SEARCH.get("needs_sponsorship")) and COUNTRY == "US"  # H-1B data is US-only
STUDENT_VISA = bool(SEARCH.get("student_visa")) and NEEDS_VISA
ROLE_WORD = SEARCH.get("role_word", "")
GRAD_YEAR = (re.search(r"\d{4}", SETTINGS.get("graduation", "")) or [""])[0]
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
HOME_RE = re.compile(HOME["pattern"], re.I) if HOME.get("pattern") else None
REMOTE_RE = re.compile(r"\bremote\b", re.I)
NON_US_RE = re.compile(
    r"canada|toronto|vancouver|montreal|london|united kingdom|\buk\b|india|bangalore|bengaluru|germany|berlin|poland|"
    r"france|paris|spain|netherlands|amsterdam|ireland|dublin|singapore|japan|tokyo|china|brazil|mexico|philippines|"
    r"australia|sydney|israel|egypt|serbia|portugal|romania|ukraine|korea|vietnam|pakistan|nigeria|argentina|colombia|"
    r"ontario|quebec|british columbia|alberta|ho chi minh|hanoi|manila|lagos|nairobi|cairo|dubai|emea|apac|latam|europe",
    re.I,
)
RESUME = {t["name"]: t.get("resume", "") for t in SEARCH.get("tracks", [])}  # optional resume file per track
NO_PLACE = ("", "remote", "anywhere", "remote - anywhere", "fully remote", "work from home")


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
    home = bool(HOME_RE and HOME_RE.search(loc))
    is_remote = bool(remote) or bool(REMOTE_RE.search(loc))
    if home:
        return HOME_LABEL, 2, True, is_remote
    if is_remote and SEARCH.get("remote", True):
        if COUNTRY != "US" or (not NON_US_RE.search(loc) and (US_RE.search(loc) or STATE_RE.search(loc) or loc.strip().lower() in NO_PLACE)):
            return "Remote", 2, False, True
    return "Elsewhere", 1, False, is_remote


def h1b_points(company: str, h1b: dict) -> tuple[float, str]:
    if not NEEDS_VISA:
        return 1.0, ""
    info = h1b.get(norm(company))
    if not info:
        return 0.5, "Unknown"
    if info.get("cap_exempt"):
        return 2.0, "Cap-exempt"
    role = info.get("role", info.get("design", 0))
    if role > 0:
        return 1.5, f"{role} {ROLE_WORD or 'same-role'} LCAs"
    if info.get("total", 0) > 0:
        return 1.0, f"{info['total']} LCAs (none for this role)"
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
    if not STUDENT_VISA:
        return out
    y = GRAD_YEAR[-2:]
    if y and re.search(rf"(spring|fall|winter)\s*(20)?{y}|(20)?{y}\s*(spring|fall|winter)|co-?op", t):
        out.append("During school year: needs CPT")
    if y and re.search(rf"summer\s*20{y}|20{y}\s*summer", t) and "intern" in t:
        out.append(f"Summer 20{y}: check it doesn't require returning to school")
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
            return f"{name} (during school: needs CPT)" if STUDENT_VISA else f"{name} (during the school year)"
    if "summer" in t:
        return "Summer"
    if re.search(r"co-?op", t):
        return "Co-op (check dates; may need CPT)" if STUDENT_VISA else "Co-op (check dates)"
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
    ap.add_argument("--days", type=int, default=SEARCH.get("max_age_days", 45), help="skip postings older than this (undated ones are kept)")
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
        [index, INCLUDE, LEVEL_RX, EXCLUDE],
    ).fetchall()

    h1b_path = ROOT / "data" / "h1b.json"
    h1b = json.loads(h1b_path.read_text()) if h1b_path.exists() else {}
    seen_urls = set(re.findall(r"https?://\S+", (ROOT / "seen-jobs.md").read_text()))
    cutoff = datetime.now() - timedelta(days=args.days)
    jobs: dict[tuple, dict] = {}
    for title, company, location, country, remote, smin, smax, cur, posted, url, ats in rows:
        loc = location or ""
        ctry = (country or "").strip().upper()
        if COUNTRY == "US":
            if ctry and ctry not in ("US", "USA", "UNITED STATES"):
                continue
            if re.search(r",\s*[a-z]{2}\s*$", loc) and not re.search(r",\s*us\s*$", loc):  # lowercase country code, e.g. "München, BY, de"
                continue
            us = bool(ctry) or ((bool(US_RE.search(loc)) or bool(STATE_RE.search(loc))) and not NON_US_RE.search(loc))
            if not us and not (remote and not NON_US_RE.search(loc) and loc.strip().lower() in NO_PLACE):
                continue
        elif COUNTRY != "ANY":  # the database mixes codes ("DE") and names ("GERMANY"), and often leaves it blank
            if ctry and ctry not in COUNTRY_NAMES:
                continue
            if not ctry and not (COUNTRY_RE.search(loc) or (remote and loc.strip().lower() in NO_PLACE)):
                continue
        when = parse_date(posted)
        if when and when < cutoff:
            continue
        if not SEARCH.get("part_time") and re.search(r"part[- ]?time|\(pt\)", title, re.I) and not re.search(r"intern|co-?op", title, re.I):
            continue  # part-time is only OK for semester internships, unless "part_time": true
        track, tpts = track_of(title)
        if not tpts:
            continue
        jtype, ppts = job_type(title)
        where, lpts, home, is_remote = place(loc, remote)
        spts, slabel = h1b_points(company or "", h1b)
        fit = math.floor(tpts + ppts + lpts + spts + 1)  # floor: unverified jobs are scored conservatively
        posting_id = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f-]{27}|\d{6,})", url or "")
        key = ("id", posting_id.group(1)) if posting_id else (norm(company or "").replace(" ", ""), re.sub(r"\W+", " ", title.lower()).strip(), where)
        resume = RESUME.get(track, "")
        job = {
            "id": hashlib.sha1(url.encode()).hexdigest()[:12],
            "title": title.strip(),
            "company": (company or "Unknown").strip(),
            "location": loc.strip() or ("Remote" if is_remote else "Not stated"),
            "where": where,
            "home": home,
            "remote": is_remote,
            "track": track,
            "type": jtype,
            "fit": min(fit, 10),
            "fitBreakdown": {"track": tpts, "path": ppts, "location": lpts, **({"sponsorship": spts} if NEEDS_VISA else {}), "level": 1},
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
    print(f"{len(rows)} title matches → {len(out)} jobs kept → data/jobs.json")
    companies: dict[str, int] = {}
    for j in out:
        companies[j["company"]] = companies.get(j["company"], 0) + 1
    unknown = [c for c in companies if norm(c) not in h1b]
    if NEEDS_VISA:
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
