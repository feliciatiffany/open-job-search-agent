#!/usr/bin/env python3
"""Local server for the job dashboard. Only listens on this computer (127.0.0.1).

Usage: .venv/bin/python app/server.py   (or ./run.sh)
  GET  /                 the dashboard (app/index.html)
  GET  /api/jobs         data/jobs.json + verified jobs from daily/*.json, merged
  GET  /api/state        data/tracker.json (your statuses, notes, added jobs)
  POST /api/state        save data/tracker.json
  POST /api/refresh      re-run scripts/fetch_jobs.py
  GET  /api/inbox        Gmail replies matched to jobs (data/private/inbox.json)
  POST /api/inbox/connect|scan|disconnect|dismiss   Gmail via app password in the macOS Keychain (scripts/inbox.py)
Static files (app/, daily/, applications/, profile/resume.json, profile/settings.json) are served read-only.
"""
from __future__ import annotations

import csv
import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
import re
import subprocess
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ai  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import inbox  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TRACKER = DATA / "tracker.json"
FITCHECK = DATA / "fitcheck.json"
PORT = int(os.environ.get("PORT", "8765"))
ALLOWED_STATIC = ("app/", "daily/", "applications/", "profile/resume.json", "profile/settings.json")
_refresh_lock = threading.Lock()
_state_lock = threading.Lock()


CLOSED_TEXT = re.compile(
    r"no longer (accepting|available|open)|position has been filled|job (is )?not found|this job (has )?(expired|closed)|"
    r"posting (has )?(closed|expired)|page (you.re looking for|not found)|not accepting applications", re.I)


def _get(url: str, timeout: int = 20) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(400_000).decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, ""


def posting_live(url: str) -> str:
    """'open', 'closed' or 'unknown', checked on the employer's own board where possible."""
    try:
        m = re.search(r"greenhouse\.io/([^/]+)/jobs/(\d+)|[?&]gh_jid=(\d+)", url)
        if m and m.group(1):
            code, _ = _get(f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}")
            return "open" if code == 200 else "closed" if code == 404 else "unknown"
        m = re.search(r"jobs\.lever\.co/([^/]+)/([0-9a-f-]{36})", url)
        if m:
            code, _ = _get(f"https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}")
            return "open" if code == 200 else "closed" if code == 404 else "unknown"
        m = re.search(r"jobs\.ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", url)
        if m:
            code, body = _get(f"https://api.ashbyhq.com/posting-api/job-board/{m.group(1)}")
            return ("open" if m.group(2) in body else "closed") if code == 200 else "unknown"
        code, body = _get(url)
        if code in (404, 410):
            return "closed"
        if code == 200:
            return "closed" if CLOSED_TEXT.search(body) else "open"
    except Exception:
        pass
    return "unknown"


def connections() -> list[dict]:
    """LinkedIn's own export (Settings → Data privacy → Get a copy of your data → Connections), saved as data/linkedin/Connections.csv."""
    path = DATA / "linkedin" / "Connections.csv"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith("First Name")), 0)
    return [
        {"name": f"{r.get('First Name', '')} {r.get('Last Name', '')}".strip(), "url": r.get("URL", ""), "company": r.get("Company", ""), "position": r.get("Position", "")}
        for r in csv.DictReader(lines[start:])
    ]


def find_job(job_id: str) -> dict | None:
    state = json.loads(TRACKER.read_text()) if TRACKER.exists() else {}
    return next((j for j in state.get("added", []) + merged_jobs()["jobs"] if j["id"] == job_id), None)


def cached_posting(job: dict) -> str:
    cache = DATA / "cache" / "postings"
    cache.mkdir(parents=True, exist_ok=True)
    f = cache / (re.sub(r"[^A-Za-z0-9_-]", "_", job["id"]) + ".txt")
    if f.exists():
        return f.read_text(encoding="utf-8")
    text = ai.posting_text(job.get("url", ""))
    if text:
        f.write_text(text, encoding="utf-8")
    return text


def posting_id(url: str) -> str | None:
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f-]{27}|\d{6,})", url or "")
    return m.group(1) if m else None


def verified_jobs() -> list[dict]:
    """Jobs the scout opened on the employer's own page, newest run wins."""
    out: dict[str, dict] = {}
    for f in sorted((ROOT / "daily").glob("*.json")):
        day = json.loads(f.read_text())
        for j in day.get("jobs", []):
            if j.get("status") != "verified":
                continue
            details = j.get("details", {})
            pay = next((t for t, kind in j.get("tags", []) if kind == "plain" and "$" in t), None)
            out[j["apply"]] = {
                "id": "v-" + (posting_id(j["apply"]) or j["id"]),
                "title": j["title"],
                "company": j["company"],
                "location": j["location"],
                "where": "NYC" if j.get("nyc") else ("Remote (US)" if j.get("remote") else "US"),
                "nyc": bool(j.get("nyc")),
                "remote": bool(j.get("remote")),
                "track": j.get("track", ""),
                "type": "Internship" if "intern" in j.get("type", "").lower() else "Full-time",
                "fit": j.get("fit"),
                "fitBreakdown": j.get("fitBreakdown"),
                "h1b": details.get("H-1B history", "Unknown"),
                "sponsorshipText": details.get("Sponsorship"),
                "salary": pay,
                "posted": (re.search(r"\d{4}-\d{2}-\d{2}", details.get("Posted", "")) or [None])[0],
                "postedText": details.get("Posted"),
                "url": j["apply"],
                "resume": j.get("resume"),
                "swaps": j.get("swaps", []),
                "why": j.get("why"),
                "lead": j.get("lead"),
                "note": j.get("note"),
                "details": details,
                "flags": [],
                "verified": True,
                "season": "Full-time" if "intern" not in j.get("type", "").lower() else "Summer",
                "linkSource": "company",
                "verifiedOn": day.get("date"),
            }
    return list(out.values())


def merged_jobs() -> dict:
    path = DATA / "jobs.json"
    base = json.loads(path.read_text()) if path.exists() else {"jobs": []}
    verified = verified_jobs()
    vids = {posting_id(v["url"]) for v in verified} - {None}
    jobs = [j for j in base["jobs"] if posting_id(j["url"]) not in vids]
    base["jobs"] = verified + jobs
    base["verifiedCount"] = len(verified)
    return base


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, fmt, *args):  # quiet
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            self.path = "/app/index.html"
        elif p == "/api/jobs":
            return self._json(merged_jobs())
        elif p == "/api/fitcheck":
            return self._json(json.loads(FITCHECK.read_text()) if FITCHECK.exists() else {})
        elif p == "/api/inbox":
            data = json.loads(inbox.INBOX.read_text()) if inbox.INBOX.exists() else {}
            data.pop("handled", None)
            return self._json({**data, "connected": inbox.connected_address() is not None, "address": inbox.connected_address()})
        elif p == "/api/connections":
            return self._json({"connections": connections()})
        elif p == "/api/state":
            return self._json(json.loads(TRACKER.read_text()) if TRACKER.exists() else {"jobs": {}, "added": []})
        elif not p.lstrip("/").startswith(ALLOWED_STATIC):
            return self._json({"error": "not found"}, 404)
        return super().do_GET()

    def do_POST(self):
        p = self.path.split("?")[0]
        if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self._json({"error": "forbidden"}, 403)
        if p == "/api/state":
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0)
            try:
                state = json.loads(raw)
                assert isinstance(state, dict)
            except Exception:
                return self._json({"error": "bad json"}, 400)
            DATA.mkdir(exist_ok=True)
            with _state_lock:
                # Studio-owned fields (edited resume, letter, chat) always win from disk, so a dashboard save can't erase them.
                disk = json.loads(TRACKER.read_text()) if TRACKER.exists() else {"jobs": {}}
                for jid, dj in disk.get("jobs", {}).items():
                    for k in ("resumeDoc", "coverLetter", "chat", "versions"):
                        if k in dj:
                            state.setdefault("jobs", {}).setdefault(jid, {})[k] = dj[k]
            tmp = TRACKER.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False))
            tmp.replace(TRACKER)
            return self._json({"ok": True})
        if p == "/api/patch":  # merge fields into one job's state without touching anything else
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
            with _state_lock:
                state = json.loads(TRACKER.read_text()) if TRACKER.exists() else {"jobs": {}, "added": []}
                job = {**state["jobs"].get(body["id"], {}), **body.get("fields", {})}
                for k in body.get("remove", []):
                    job.pop(k, None)
                state["jobs"][body["id"]] = job
                tmp = TRACKER.with_suffix(".tmp")
                tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False))
                tmp.replace(TRACKER)
            return self._json({"ok": True})
        if p == "/api/ai":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
            job = find_job(body.get("jobId", ""))
            if not job:
                return self._json({"error": "job not found"}, 404)
            try:
                out = ai.ask(job, body.get("message", ""), body.get("resumeDoc"), body.get("coverLetter", ""),
                             body.get("history", []), cached_posting(job))
                return self._json(out)
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        if p == "/api/inbox/connect":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
            try:
                inbox.save_credentials(body.get("address", "").strip(), body.get("password", ""))
                return self._json({"ok": True})
            except Exception as e:
                msg = "Gmail said no. Check the address and use a 16-letter app password, not your normal password." if "AUTHENTICATIONFAILED" in str(e) or "Invalid credentials" in str(e) else str(e)
                return self._json({"error": msg}, 400)
        if p == "/api/inbox/disconnect":
            inbox.forget_credentials()
            return self._json({"ok": True})
        if p == "/api/inbox/scan":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
            try:
                with _state_lock:
                    r = inbox.scan(body.get("since"), {j["id"]: j for j in merged_jobs().get("jobs", [])})
                return self._json({"ok": True, "changes": r["changes"], "messagesRead": r["messagesRead"], "unmatched": len(r["unmatched"])})
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        if p == "/api/inbox/dismiss":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
            with _state_lock:
                data = json.loads(inbox.INBOX.read_text()) if inbox.INBOX.exists() else {}
                data["dismissed"] = sorted(set(data.get("dismissed", [])) | {body.get("id")})
                data["unmatched"] = [r for r in data.get("unmatched", []) if r["id"] != body.get("id")]
                inbox.INBOX.write_text(json.dumps(data, indent=1, ensure_ascii=False))
            return self._json({"ok": True})
        if p == "/api/fitcheck":  # AI reads each posting's description and scores fit 1-5 (results kept in data/fitcheck.json)
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
            state = json.loads(TRACKER.read_text()) if TRACKER.exists() else {}
            pool = {j["id"]: j for j in state.get("added", []) + merged_jobs()["jobs"]}
            jobs = [pool[i] for i in body.get("ids", [])[:24] if i in pool]
            if not jobs:
                return self._json({"error": "no jobs"}, 400)
            with ThreadPoolExecutor(8) as ex:
                texts = list(ex.map(cached_posting, jobs))
            items = [{"id": j["id"], "title": j.get("title"), "company": j.get("company"), "location": j.get("location"), "text": t} for j, t in zip(jobs, texts)]
            try:
                with ThreadPoolExecutor(2) as ex:
                    parts = list(ex.map(ai.fit_check, [items[i:i + 12] for i in range(0, len(items), 12)]))
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            today = __import__("datetime").date.today().isoformat()
            with _state_lock:
                store = json.loads(FITCHECK.read_text()) if FITCHECK.exists() else {}
                for x in (r for part in parts for r in part):
                    store[x["id"]] = {**x, "checked": today, "loaded": bool(texts[[j["id"] for j in jobs].index(x["id"])])}
                FITCHECK.write_text(json.dumps(store, indent=1, ensure_ascii=False))
            return self._json({"ok": True, "results": {i: store[i] for i in (j["id"] for j in jobs) if i in store}})
        if p == "/api/check-live":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
            items = body.get("jobs", [])[:60]
            with ThreadPoolExecutor(8) as ex:
                results = list(ex.map(lambda j: (j["id"], posting_live(j["url"])), items))
            return self._json({"results": dict(results)})
        if p == "/api/autofill":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
            url = body.get("url", "")
            if not url.startswith("http"):
                return self._json({"error": "missing url"}, 400)
            subprocess.Popen([sys.executable, str(ROOT / "scripts" / "autofill.py"), url, "--job", body.get("id", ""), "--label", body.get("label", "")],
                             cwd=ROOT, stdout=open(DATA / "autofill.log", "w"), stderr=subprocess.STDOUT)
            return self._json({"ok": True})
        if p == "/api/refresh":
            if not _refresh_lock.acquire(blocking=False):
                return self._json({"error": "a refresh is already running"}, 409)
            try:
                r = subprocess.run([sys.executable, str(ROOT / "scripts" / "fetch_jobs.py")], capture_output=True, text=True, timeout=900)
                return self._json({"ok": r.returncode == 0, "log": (r.stdout + r.stderr)[-2000:]})
            finally:
                _refresh_lock.release()
        return self._json({"error": "not found"}, 404)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Job dashboard running at {url}  (Ctrl+C to stop)")
    if "--no-open" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
