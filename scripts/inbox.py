#!/usr/bin/env python3
"""Read the candidate's Gmail (read-only) and find replies to their job applications: received, interview, offer, rejected.

How it connects: Gmail IMAP with an *app password* (Google Account → Security → 2-Step Verification → App passwords).
The password is kept in the macOS Keychain (service "open-job-search-agent-gmail"), never in the repo.
Messages are opened read-only (BODY.PEEK), so nothing is marked as read, moved or deleted.

Email details (subjects, senders, snippets) go to data/private/inbox.json, which is gitignored.
Only the resulting status change goes into data/tracker.json.

Usage:
  python3 scripts/inbox.py                     scan emails since the start date in profile/settings.json and update the tracker
  python3 scripts/inbox.py --since 2026-10-01  change the start date (it's remembered)
"""
from __future__ import annotations

import email
import email.header
import email.utils
import html
import imaplib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVATE = ROOT / "data" / "private"
INBOX = PRIVATE / "inbox.json"
TRACKER = ROOT / "data" / "tracker.json"
SERVICE = "open-job-search-agent-gmail"

# ---------- credentials (macOS Keychain) ----------

def save_credentials(address: str, app_password: str) -> None:
    pw = app_password.replace(" ", "")
    test_login(address, pw)
    subprocess.run(["security", "delete-generic-password", "-s", SERVICE], capture_output=True)
    subprocess.run(["security", "add-generic-password", "-s", SERVICE, "-a", address, "-w", pw, "-U"], check=True, capture_output=True)


def forget_credentials() -> None:
    subprocess.run(["security", "delete-generic-password", "-s", SERVICE], capture_output=True)


def credentials() -> tuple[str, str] | None:
    if os.environ.get("GMAIL_ADDRESS") and os.environ.get("GMAIL_APP_PASSWORD"):
        return os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    r = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-g"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    acct = re.search(r'"acct"<blob>="([^"]*)"', r.stdout)
    pw = re.search(r'^password: "(.*)"$', r.stderr, re.M)
    return (acct.group(1), pw.group(1)) if acct and pw else None


def connected_address() -> str | None:
    c = credentials()
    return c[0] if c else None


def test_login(address: str, pw: str) -> None:
    m = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        m.login(address, pw)
    finally:
        try:
            m.logout()
        except Exception:
            pass

# ---------- classify ----------

REJECTED = re.compile(
    r"unfortunately|not (be )?moving forward|won.t be moving forward|will not be moving forward|decided (not )?to (move|proceed|pursue) "
    r"(forward )?with (other|another|a different)|other candidates (whose|who)|not (been )?selected|regret to inform|"
    r"position has (been|now been) filled|no longer (being )?considered|not (to )?proceed with your (application|candidacy)|"
    r"decided to pursue other|we have decided to go (in )?(another|a different) direction|not a match at this time|"
    r"will not be (able to )?(offer|extend)|unable to offer you", re.I)
OFFER = re.compile(r"pleased to (offer|extend)|offer letter|extend (you )?an offer|we.d like to offer you|formal offer", re.I)
INTERVIEW = re.compile(
    r"schedule (a|an|your|some) (time|call|interview|chat|conversation)|interview (invitation|request|confirmation|scheduled)|"
    r"(like|love|want) to interview you|invite you to (an? )?interview|phone screen|recruiter (call|screen)|your interview|"
    r"availability|available (times|to (chat|talk|meet))|next steps? (in|of) (the|our) (process|hiring)|move (you )?forward to|"
    r"calendly\.com|goodtime\.io|design (challenge|exercise|task)|take.home|portfolio (review|presentation)|"
    r"would (love|like) to (chat|talk|connect|learn more)|invite you to", re.I)
RECEIVED = re.compile(
    r"thank(s| you) for (applying|your application|your interest)|(we.ve|we have) received your application|"
    r"application (has been )?(received|submitted)|your application (to|for)|successfully (applied|submitted)", re.I)
NOISE = re.compile(
    r"job alert|jobs? (you may|for you|recommended)|new jobs|recommended for you|weekly digest|newsletter|is hiring|"
    r"jobs similar|people also viewed|webinar|event invitation|% off|sale\b|your daily|top picks", re.I)
# Words that look like a rejection but come in interview emails ("unfortunately I'm out next week…") are rare; a rejection
# that politely mentions "interview" is common, so rejection is checked first.


# A reply about a job application talks about one. Newsletters, bills and thesis interviews don't.
JOB_CONTEXT = re.compile(r"\b(your application|applied|applying|application (for|to|status)|candidacy|candidate|the position|this position|"
                         r"the role|this role|recruit(er|ing)|hiring (team|manager|process)|talent (acquisition|team)|job (posting|opening)|"
                         r"internship|new grad|offer letter)\b", re.I)
SETTINGS = json.loads((ROOT / "profile" / "settings.json").read_text())
MY_ADDRESSES = re.compile("|".join(re.escape(a) for a in SETTINGS.get("my_addresses", [])) or r"^$", re.I)


def classify(subject: str, body: str) -> str | None:
    text = f"{subject}\n{body[:4000]}"
    if NOISE.search(subject) or not JOB_CONTEXT.search(text):
        return None
    if OFFER.search(text):
        return "offer"
    if REJECTED.search(text):
        return "rejected"
    hit = INTERVIEW.search(text)
    # Receipts often say "if your background is a match, we'll reach out to schedule an interview": that's not an invite.
    conditional = hit and re.search(r"\b(if|should|once|in the event)\b[^.]{0,120}" + re.escape(hit.group(0)), text, re.I)
    if hit and not conditional and not re.search(r"interview tips|how to (ace|prepare)", text, re.I):
        return "interview"
    if RECEIVED.search(text):
        return "received"
    return None

# ---------- read mail ----------

# Only emails from this day on are read (profile/settings.json → email_start_date).
START_DATE = SETTINGS.get("email_start_date", "2026-11-01")
GMAIL_QUERY = ('after:{after} -category:promotions -category:social -from:me '
               '(application OR applying OR applied OR candidacy OR interview OR "next steps" OR unfortunately OR position OR offer OR role)')


def _dec(v: str | None) -> str:
    if not v:
        return ""
    out = []
    for part, enc in email.header.decode_header(v):
        out.append(part.decode(enc or "utf-8", "ignore") if isinstance(part, bytes) else part)
    return "".join(out)


def _body(msg: email.message.Message) -> str:
    plain, rich = [], []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() != "text" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        try:
            t = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
        except Exception:
            continue
        (plain if part.get_content_subtype() == "plain" else rich).append(t)
    text = "\n".join(plain) if plain else re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", " ", "\n".join(rich)))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def fetch(since: str = START_DATE, limit: int = 400) -> list[dict]:
    c = credentials()
    if not c:
        raise RuntimeError("Gmail isn't connected yet")
    m = imaplib.IMAP4_SSL("imap.gmail.com")
    m.login(*c)
    try:
        m.select('"[Gmail]/All Mail"', readonly=True)
        typ, data = m.uid("SEARCH", "X-GM-RAW", '"' + GMAIL_QUERY.format(after=since.replace("-", "/")).replace('"', '\\"') + '"')
        uids = (data[0] or b"").split()[-limit:]
        out = []
        for i in range(0, len(uids), 50):
            typ, rows = m.uid("FETCH", b",".join(uids[i:i + 50]), "(X-GM-MSGID X-GM-THRID BODY.PEEK[])")
            for row in rows:
                if not isinstance(row, tuple):
                    continue
                meta = row[0].decode("utf-8", "ignore")
                msg = email.message_from_bytes(row[1])
                thr = re.search(r"X-GM-THRID (\d+)", meta)
                name, addr = email.utils.parseaddr(_dec(msg.get("From")))
                try:
                    dt = email.utils.parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc)
                except Exception:
                    dt = datetime.now(timezone.utc)
                body = _body(msg)
                out.append({
                    "id": (msg.get("Message-ID") or "").strip("<> ") or meta,
                    "thread": f"{int(thr.group(1)):x}" if thr else "",
                    "date": dt.strftime("%Y-%m-%d"), "ts": dt.isoformat(),
                    "fromName": name, "from": addr, "subject": _dec(msg.get("Subject")),
                    "body": body[:6000],
                    "bulk": bool(msg.get("List-Id") or re.search(r"bulk|list", msg.get("Precedence", ""), re.I)),
                })
        return out
    finally:
        try:
            m.logout()
        except Exception:
            pass

# ---------- match to tracked jobs ----------

def norm(s: str) -> str:
    s = re.sub(r"[^a-z0-9]", "", (s or "").lower())
    return re.sub(r"^the|(inc|llc|corp|corporation|careers?|jobs|recruiting|talent|hq)$", "", s)


ATS_DOMAINS = re.compile(r"greenhouse|lever\.co|ashbyhq|myworkday|workday|smartrecruiters|icims|jobvite|bamboohr|rippling|"
                         r"linkedin|indeed|handshake|wellfound|gem\.com|dover|taleo|successfactors|oraclecloud", re.I)


def company_of(m: dict) -> str:
    """Best guess at which company an unmatched email is from."""
    s = m["subject"]
    for pat in (r"(?:applying|application|interest) (?:to|at|with|for) ([A-Z][\w&.' -]{1,40}?)(?:[!.,:|-]| for | as |$)",
                r"\bat ([A-Z][\w&.' -]{1,40}?)(?:[!.,:|-]|$)", r"^([A-Z][\w&.' ]{1,30}) (?:\||-|:)"):
        mm = re.search(pat, s)
        if mm:
            return mm.group(1).strip()
    if m["fromName"] and not re.search(r"no.?reply|notifications?|team$|recruit|talent|careers|hiring", m["fromName"], re.I):
        return re.sub(r"\s+(via|from|@).*$", "", m["fromName"]).strip()
    dom = m["from"].split("@")[-1]
    if not ATS_DOMAINS.search(dom):
        return dom.split(".")[-2].capitalize() if dom.count(".") >= 1 else dom
    return m["fromName"] or dom


def match(m: dict, jobs: dict) -> str | None:
    hay_head = norm(m["fromName"] + " " + m["from"] + " " + m["subject"])
    hay_body = norm(m["body"][:2500])
    best, score = None, 0
    for jid, j in jobs.items():
        co = norm(j.get("company", ""))
        if len(co) < 3:
            continue
        s = 3 if co in hay_head else 1 if co in hay_body else 0
        if not s:
            continue
        t = norm(j.get("title", ""))[:25]
        if t and t in norm(m["subject"] + " " + m["body"][:2500]):
            s += 2
        if j.get("status") in ("applied", "interview", "offer"):
            s += 1
        if s > score:
            best, score = jid, s
    return best

# ---------- scan ----------

RANK = {"received": 1, "interview": 2, "offer": 3, "rejected": 4}


def scan(since: str | None = None, lookup: dict | None = None) -> dict:
    """lookup: job id → job (title, company) for tracker entries that only store a status."""
    prev = json.loads(INBOX.read_text()) if INBOX.exists() else {}
    since = since or prev.get("since") or START_DATE
    msgs = [m for m in fetch(since) if m["date"] >= since]
    state = json.loads(TRACKER.read_text()) if TRACKER.exists() else {"jobs": {}, "added": []}
    added = {a["id"]: a for a in state.get("added", [])}
    lookup = {**(lookup or {}), **added}
    names = {}  # company/title per tracked job, used only for matching (not written back)
    for jid, j in state["jobs"].items():
        src = lookup.get(jid, {})
        names[jid] = {"company": j.get("company") or src.get("company", ""), "title": j.get("title") or src.get("title", ""),
                      "status": j.get("status")}
    old = json.loads(INBOX.read_text()) if INBOX.exists() else {}
    handled = set(old.get("handled", []))

    matched, unmatched = {}, []
    for m in sorted(msgs, key=lambda x: x["ts"]):
        if MY_ADDRESSES.search(m["from"] + " " + m["fromName"]):
            continue  # the candidate's own emails
        kind = classify(m["subject"], m["body"])
        if not kind or (m.get("bulk") and kind != "received"):
            continue  # mailing lists (newsletters, Meetup, MoMA) are never replies
        rec = {k: m[k] for k in ("id", "thread", "date", "fromName", "from", "subject")}
        rec["kind"] = kind
        rec["snippet"] = m["body"][:280]
        rec["link"] = f"https://mail.google.com/mail/u/0/#all/{m['thread']}" if m["thread"] else ""
        jid = match(m, names)
        if jid:
            matched.setdefault(jid, []).append(rec)
        else:
            rec["company"] = company_of(m)
            unmatched.append(rec)

    changes = []
    for jid, recs in matched.items():
        job = state["jobs"][jid]
        new = [r for r in recs if r["id"] not in handled]
        if not new:
            continue
        latest = max(new, key=lambda r: (r["date"], RANK[r["kind"]]))
        cur = job.get("status")
        nxt = cur
        if latest["kind"] == "rejected" and cur != "offer":
            nxt = "rejected"
        elif latest["kind"] == "offer":
            nxt = "offer"
        elif latest["kind"] == "interview" and cur in (None, "saved", "applied", "closed"):
            nxt = "interview"
        elif latest["kind"] == "received" and cur in (None, "saved"):
            nxt = "applied"
        first_receipt = min((r["date"] for r in recs if r["kind"] == "received"), default=None)
        if not job.get("appliedOn") and (first_receipt or nxt in ("applied", "interview", "rejected", "offer")):
            job["appliedOn"] = first_receipt or min(r["date"] for r in recs)
        job["lastEmail"] = {"date": latest["date"], "kind": latest["kind"], "subject": latest["subject"][:120], "link": latest["link"]}
        if nxt != cur:
            job["history"] = (job.get("history", []) + [{"status": cur, "on": latest["date"]}])[-10:]
            job["status"] = nxt
            job["statusFrom"] = "email"
            job["updated"] = latest["date"]
            changes.append({"id": jid, "company": names[jid]["company"], "title": names[jid]["title"], "from": cur, "to": nxt, "date": latest["date"]})
        handled.update(r["id"] for r in recs)

    TRACKER.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    PRIVATE.mkdir(parents=True, exist_ok=True)
    keep_unmatched = {r["id"]: r for r in old.get("unmatched", []) if r["id"] not in old.get("dismissed", []) and r["date"] >= since and classify(r["subject"], r.get("snippet", "")) and not MY_ADDRESSES.search(r["from"])}
    old["byJob"] = {k: [r for r in v if r["date"] >= since] for k, v in old.get("byJob", {}).items()}
    keep_unmatched.update({r["id"]: r for r in unmatched if r["id"] not in old.get("dismissed", [])})
    out = {
        "scannedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "address": connected_address(), "since": since, "messagesRead": len(msgs),
        "byJob": {**old.get("byJob", {}), **matched},
        "unmatched": sorted(keep_unmatched.values(), key=lambda r: r["date"], reverse=True)[:80],
        "dismissed": old.get("dismissed", []),
        "handled": sorted(handled),
        "changes": changes,
    }
    INBOX.write_text(json.dumps(out, indent=1, ensure_ascii=False))
    return out


if __name__ == "__main__":
    since = sys.argv[sys.argv.index("--since") + 1] if "--since" in sys.argv else None
    sys.path.insert(0, str(ROOT / "app"))
    import server  # noqa: E402  (for the merged job list)
    r = scan(since, {j["id"]: j for j in server.merged_jobs().get("jobs", [])})
    print(f"Read {r['messagesRead']} emails · {len(r['byJob'])} tracked jobs with replies · {len(r['unmatched'])} other replies")
    for c in r["changes"]:
        print(f"  {c['company']}: {c['from']} → {c['to']} ({c['date']})")
