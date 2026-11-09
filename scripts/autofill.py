#!/usr/bin/env python3
"""Open a job application in Chrome and fill it in for the candidate. It NEVER clicks Submit: you review, fix, and submit.

Usage: .venv/bin/python scripts/autofill.py <application URL> [--job <dashboard job id>] [--label "Co Title"]
       add --check to fill headlessly, print what it did, and quit (a trial: nothing is ever sent)

- Builds the tailored resume PDF (and the Studio cover letter, if any) and uploads them.
- Fills standard fields from profile/answers.json. Blank answers are skipped. EEO questions get "decline to self-identify".
- Knows the big application sites (notes adapted from AIHawk's site plugins): Greenhouse, Lever, Ashby, Workday, SmartRecruiters.
  It opens the real form (/apply, /application, "Apply Manually"), picks options in dropdowns and typeaheads and reads them
  back, answers Ashby's Yes/No buttons, skips "autofill from resume" boxes and hidden robot-trap fields, and spots closed postings.
- Multi-step forms (Workday, SmartRecruiters): it keeps watching, and fills each new step as you click Next.
- Proof: a screenshot of the filled form goes to applications/proof/. When YOU press Submit and the site shows its
  confirmation page, it saves that screenshot too and marks the job Applied on the dashboard.
- Fields it couldn't fill get a yellow dashed outline.
- Uses its own Chrome profile (data/cache/chrome-profile), so site logins like Workday are remembered between runs.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SERVER = f"http://127.0.0.1:{os.environ.get('PORT', '8765')}"
A = json.loads((ROOT / "profile" / "answers.json").read_text())
SETTINGS = json.loads((ROOT / "profile" / "settings.json").read_text())
FILE_NAME = re.sub(r"[^A-Za-z0-9]+", "-", SETTINGS["name"]).strip("-")
PROOF = ROOT / "applications" / "proof"

# Question text (lowercase) → answer key. First match wins, so specific rules come first.
RULES: list[tuple[str, str]] = [
    (r"password|access code|for robots|beecatcher|leave (this )?(field )?(blank|empty)", "_skip"),
    (r"autofill|apply with|parse (your )?resume", "_skip"),
    (r"country code|dial code|phone.{0,10}country", "_phone_country"),
    (r"^country|country\b", "country"),
    (r"(intend|plan|prefer) to work|which (office|hub|location)|preferred (office|location)", "preferred_office"),
    (r"able to work (from|in)|work from .{0,30}(hub|office|nyc|new york)", "can_work_onsite"),
    (r"preferred (first )?name|nickname", "preferred_name"),
    (r"first name|given name|^first\b|firstname", "first_name"),
    (r"last name|family name|surname|lastname", "last_name"),
    (r"^(full )?name\b|full name|your name|legal name|systemfield_name", "_full_name"),
    (r"e-?mail", "email"),
    (r"phone|mobile|telephone", "phone"),
    (r"linkedin", "linkedin"),
    (r"github", "github"),
    (r"portfolio", "portfolio"),
    (r"website|personal site|\burl\b|link to your work", "website"),
    (r"gender|race|ethnic|hispanic|latino|veteran|disability|sexual orientation|transgender", "eeo"),
    (r"authori[sz]ed to work|legally (authori[sz]ed|eligible)|eligible to work|right to work", "authorized_to_work_us"),
    (r"sponsor|visa|immigration status|work permit", "require_sponsorship"),
    (r"how did you (hear|find|learn)|where did you (hear|find)|source", "how_heard"),
    (r"relocat", "willing_to_relocate"),
    (r"in[- ]office|on[- ]?site|hybrid|commut|work from (our|the) office", "can_work_onsite"),
    (r"18 years|over 18|at least 18", "over_18"),
    (r"(previously|ever) (been )?(worked|employed)|former employee|worked (for|at) .{0,40} before", "worked_here_before"),
    (r"employee|employ(ed|ment)|government|non.?compete|clearance|convicted|criminal|relative|family member", "_skip"),
    (r"pronoun", "pronouns"),
    (r"gpa|grade point", "gpa"),
    (r"graduation (year)|year of graduation|grad year|end.year", "graduation_year"),
    (r"graduation month|end.month", "graduation_month"),
    (r"graduat|completion date|expected (end|finish)", "graduation"),
    (r"\bschool\b|university|college|^institution", "school"),
    (r"degree", "degree"),
    (r"major|discipline|field of study|area of study", "major"),
    (r"\bstate\b|province", "state"),
    (r"current (city|location)|where are you (located|based)|^location|candidate.location|city|location", "location"),
    (r"current (company|employer)|^org\b|organi[sz]ation", "_skip"),
    (r"salary|compensation|pay expectation", "salary_expectation"),
]
SKIP = re.compile(r"cover letter|why (do you|are you)|tell us|describe|additional information|anything else|consent|acknowledg|"
                  r"privacy|agree|certify|captcha|signature|today.s date", re.I)
NO_DECLINE = re.compile(r"decline|don.?t wish|do not wish|prefer not|not to (say|disclose|answer)|choose not|rather not", re.I)
# Other wordings to try when a list doesn't have the exact answer.
ALTS = {"school": SETTINGS.get("school_alternates", []), "major": SETTINGS.get("major_alternates", []),
        "degree": ["Bachelor of Fine Arts", "Bachelor's Degree", "Bachelor's", "Bachelors", "BFA"]}
TYPEAHEAD_KEYS = {"location", "school", "country", "preferred_office", "degree", "major"}

# Closed postings, per site (from AIHawk's notes) and in general.
CLOSED = re.compile(r"no longer (accepting|available|open)|couldn.t find anything here|job (is )?not found|position has been filled|"
                    r"this (job|position|posting) (has )?(expired|closed|been filled)|not accepting applications|page (you.re looking for|not found)|"
                    r"job you are trying to apply for has been filled", re.I)
# What a site says after a real send. Greenhouse → /confirmation, Lever → /thanks, Workday → /jobTasks/completed/application.
CONFIRM_URL = re.compile(r"/confirmation\b|/thanks\b|/thank-you|jobTasks/completed/application|/submitted\b|application-confirmation", re.I)
CONFIRM_TEXT = re.compile(r"application (was |has been )?(successfully )?(submitted|received|sent)|thank(s| you) for (applying|your application)|"
                          r"we.ve received your application|successfully submitted|your application is (in|complete)", re.I)

COLLECT_JS = r"""
() => {
  const visible = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return (r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0') || el.type === 'file'; };
  const text = el => (el && el.innerText || '').replace(/\s+/g,' ').trim();
  function question(el){
    let t = '';
    if (el.id){ const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) t = text(l); }
    if (!t && el.getAttribute('aria-labelledby')) t = el.getAttribute('aria-labelledby').split(' ').map(id => text(document.getElementById(id))).join(' ');
    if (!t) t = el.getAttribute('aria-label') || '';
    if (!t && (el.type === 'radio' || el.type === 'checkbox')){
      const fs = el.closest('fieldset'); if (fs){ const lg = fs.querySelector('legend'); if (lg) t = text(lg); }
      if (!t){ let p = el.parentElement; for (let i = 0; i < 5 && p; i++){ const q = p.querySelector('label:not(:has(input)), legend, .label, [class*=question], [class*=label]'); if (q && !q.contains(el)){ t = text(q); break; } p = p.parentElement; } }
    }
    if (!t){ const l = el.closest('label'); if (l) t = text(l); }
    if (!t){ let p = el.parentElement; for (let i = 0; i < 3 && p && !t; i++){ t = text(p).slice(0, 160); p = p.parentElement; } }
    const extra = [el.placeholder, el.name, el.id, el.getAttribute('data-automation-id')].filter(Boolean).join(' ');
    return (t + ' ' + extra).trim().slice(0, 300);
  }
  const out = [];
  let n = window.__jsN || 0;
  document.querySelectorAll('input, textarea, select').forEach(el => {
    const type = (el.type || el.tagName).toLowerCase();
    if (['hidden','submit','button','image','reset','search'].includes(type) || el.disabled || el.readOnly && type !== 'file' || !visible(el)) return;
    if (el.closest('[aria-hidden=true]') && type !== 'file') return;
    if (!el.hasAttribute('data-js')) el.setAttribute('data-js', String(n++));
    let optionLabel = '';
    if (type === 'radio' || type === 'checkbox'){ const l = el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`); optionLabel = text(l || el.closest('label')) || el.value; }
    out.push({ idx: el.getAttribute('data-js'), tag: el.tagName.toLowerCase(), type, name: el.name || '', id: el.id || '', question: question(el), optionLabel,
      options: el.tagName === 'SELECT' ? [...el.options].map(o => o.text.trim()) : [],
      required: el.required || el.getAttribute('aria-required') === 'true' || /\*|✱/.test(question(el).slice(0, 120)),
      value: el.value || '', role: el.getAttribute('role') || '', haspopup: el.getAttribute('aria-haspopup') || '',
      autocomplete: el.getAttribute('aria-autocomplete') || '' });
  });
  // Yes/No answered with buttons (Ashby): a label followed by a pair of buttons.
  document.querySelectorAll('label, legend, [class*=question] > p').forEach(lab => {
    const box = lab.parentElement; if (!box) return;
    const btns = [...box.querySelectorAll(':scope > div button, :scope > button, :scope > div > div > button')].filter(b => /^(yes|no)$/i.test(text(b)) && visible(b));
    if (btns.length !== 2 || box.hasAttribute('data-js-yn')) return;
    box.setAttribute('data-js-yn', String(n++));
    out.push({ idx: box.getAttribute('data-js-yn'), tag: 'yesno', type: 'yesno', name: '', id: '', question: text(lab).slice(0, 300), optionLabel: '',
      options: btns.map(text), required: /\*/.test(text(lab)), value: btns.some(b => b.getAttribute('aria-pressed') === 'true' || /selected|active/i.test(b.className)) ? 'set' : '', role: '' });
  });
  window.__jsN = n;
  return out;
}
"""


def site_of(url: str) -> str:
    for name, rx in (("greenhouse", r"greenhouse\.io|gh_jid="), ("lever", r"lever\.co"), ("ashby", r"ashbyhq\.com"),
                     ("workday", r"myworkday(jobs|site)\.com|workday\.com"), ("smartrecruiters", r"smartrecruiters\.com")):
        if re.search(rx, url, re.I):
            return name
    return "other"


OTHER_COUNTRY = re.compile(r"\b(canada|canadian|united kingdom|\buk\b|europe|\beu\b|australia|india|mexico|germany|ireland|singapore|brazil)\b", re.I)
US = re.compile(r"united states|\bu\.?s\.?a?\b|america", re.I)


def answer_for(question: str) -> tuple[str | None, str | None]:
    q = question.lower()
    if SKIP.search(q):
        return None, None
    # "Are you authorized to work in Canada?": the answers file is about the US only.
    if re.search(r"authori[sz]|eligib|sponsor|visa|right to work", q) and OTHER_COUNTRY.search(q.split("?")[0]) and not US.search(q.split("?")[0]):
        return None, None
    for rx, key in RULES:
        if re.search(rx, q):
            if key == "_skip":
                return None, None
            if key == "_full_name":
                return key, f"{A['first_name']} {A['last_name']}"
            if key == "_phone_country":
                return "country", "United States"
            return key, (A.get(key) or None)
    return None, None


def pick_option(options: list[str], value: str, key: str) -> str | None:
    opts = [o for o in options if o and not re.match(r"^(select|choose|--|please)", o, re.I)]
    if key == "eeo":
        return next((o for o in opts if NO_DECLINE.search(o)), None)
    v = value.lower()
    if v in ("yes", "no"):  # "Yes, I am authorized…" style options
        hit = next((o for o in opts if re.match(rf"^{v}\b", o, re.I)), None)
        if hit:
            return hit
    for test in (lambda o: o.lower() == v, lambda o: o.lower().startswith(v), lambda o: v in o.lower(), lambda o: o.lower() in v):
        hit = next((o for o in opts if test(o)), None)
        if hit:
            return hit
    return None


def mark(loc) -> None:
    try:
        loc.evaluate("el => { const t = el.closest('.select__control, [class*=control]') || el; t.style.outline = '3px dashed #f59f00'; t.style.outlineOffset = '2px'; }")
    except Exception:
        pass


def choose_from_list(page, loc, value: str, key: str) -> str | None:
    """A typeahead or listbox (React-select, Lever location, Workday prompts): type, read the options, click the right one,
    then read the field back. Adapted from AIHawk's menus notes: a 'successful' click often leaves the field empty."""
    def rows():
        # The list the field itself names (aria-controls / aria-owns), so other lists on the page (like the phone
        # country picker) are never read. Fallbacks for sites that don't name it.
        lid = loc.get_attribute("aria-controls") or loc.get_attribute("aria-owns")
        if lid:
            r = page.locator(f"[id='{lid}'] [role=option], [id='{lid}'] .select__option")
            if r.count():
                return r
        return page.locator(".select__menu [role=option]:visible, [id^=location-]:visible, [data-automation-id=promptOption]:visible, "
                            "[role=listbox]:visible [role=option]:not(.iti__country)")

    def first_line(t: str) -> str:
        return t.strip().split("\n")[0].strip()

    loc.click()
    time.sleep(0.6)
    for alt in ([value] + ALTS.get(key, []))[:6]:
        got = _choose(page, loc, rows, first_line, alt, key)
        if got:
            return got
    page.keyboard.press("Escape")
    return None


def _choose(page, loc, rows, first_line, value: str, key: str) -> str | None:
    for attempt in ("open", "type", "short"):
        if attempt != "open":
            try:
                loc.fill("")
            except Exception:
                pass
            words = "New York" if key == "location" else value if attempt == "type" else re.split(r"[ ,(]", value)[0]
            if key == "eeo":
                words = "decline" if attempt == "type" else "not"
            loc.type(words[:40], delay=25)
            time.sleep(2.2 if key in ("school", "major") else 1.2)  # school lists are searched on the server
        r = rows()
        texts = [first_line(t) for t in r.all_inner_texts()][:300]
        if not texts:
            continue
        want = pick_option(texts, value, key) or (texts[0] if attempt != "open" and (key == "location" or len(texts) == 1) else None)
        if want:
            r.nth(texts.index(want)).click()
            time.sleep(0.4)
            return want
    return None


def build_pdf(context, url: str, path: Path) -> Path:
    page = context.new_page()
    page.goto(url, wait_until="networkidle")
    page.wait_for_function("document.querySelector('#paper h1') !== null", timeout=15000)
    page.emulate_media(media="print")
    page.pdf(path=str(path), format="Letter", print_background=True, margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
    page.close()
    print(f"✓ {path.relative_to(ROOT)}")
    return path


def server_state() -> dict | None:
    try:
        return json.loads(urllib.request.urlopen(SERVER + "/api/state", timeout=3).read())
    except Exception:
        return None


def patch(job_id: str | None, fields: dict) -> None:
    if not job_id:
        return
    try:
        req = urllib.request.Request(SERVER + "/api/patch", data=json.dumps({"id": job_id, "fields": fields}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"! Couldn't update the dashboard: {e}")


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:80] or "application"


def open_application(page, site: str) -> None:
    """From a job description to the form, the way each site does it."""
    for sel in ("#onetrust-reject-all-handler", "button:has-text('Reject all')", "button:has-text('Reject All')"):
        loc = page.locator(sel).first
        if loc.count() and loc.is_visible():
            loc.click()
            break
    url = page.url.split("?")[0].rstrip("/")
    if site == "lever" and not url.endswith("/apply"):
        page.goto(url + "/apply", wait_until="domcontentloaded")
        time.sleep(1.5)
        return
    if site == "ashby" and not url.endswith("/application"):
        page.goto(url + "/application", wait_until="domcontentloaded")
        time.sleep(2)
        return
    if site == "workday":
        for sel in ("[data-automation-id=adventureButton]", "[data-automation-id=applyManually]"):
            loc = page.locator(sel).first
            try:
                loc.wait_for(state="visible", timeout=8000)
                loc.click()
                time.sleep(2)
            except PWTimeout:
                pass
        return
    if site == "smartrecruiters":
        loc = page.locator("#st-apply").first
        if loc.count():
            loc.click()
            time.sleep(3)
        return
    if page.locator("input[type=file], input[name*=email i], input[type=email]").count():
        return
    for sel in ["a:has-text('Apply for this job')", "button:has-text('Apply for this job')", "a:has-text('Apply now')",
                "button:has-text('Apply now')", "a:has-text('Apply')", "button:has-text('Apply')"]:
        loc = page.locator(sel).first
        if loc.count() and loc.is_visible():
            loc.click()
            page.wait_for_load_state("domcontentloaded")
            time.sleep(2)
            return


def is_closed(page) -> bool:
    if re.search(r"[?&]error=true", page.url):  # Greenhouse sends a closed posting to the board with ?error=true
        return True
    try:
        body = page.locator("body").inner_text(timeout=3000)[:5000]
    except Exception:
        return False
    return bool(CLOSED.search(body)) and not page.locator("input[type=email], input[name*=email i]").count()


class Filler:
    def __init__(self, page, site: str, resume: Path | None, letter: Path | None, letter_text: str):
        self.page, self.site, self.resume, self.letter, self.letter_text = page, site, resume, letter, letter_text
        self.done: set[str] = set()  # data-js ids already handled, so a field you cleared is never refilled
        self.groups: set[str] = set()
        self.uploaded = False
        self.filled: list[str] = []
        self.left: dict[str, str] = {}

    def pass_once(self) -> int:
        page = self.page
        try:
            fields = page.evaluate(COLLECT_JS)
        except Exception:
            return 0
        before = len(self.filled)
        for f in fields:
            if f["idx"] in self.done:
                continue
            self.done.add(f["idx"])
            sel = f'[data-js-yn="{f["idx"]}"]' if f["type"] == "yesno" else f'[data-js="{f["idx"]}"]'
            loc = page.locator(sel).first
            q = f["question"]
            try:
                self.one(f, loc, q)
            except Exception as e:  # one odd field shouldn't stop the rest
                self.left[q[:60]] = f"couldn't fill: {type(e).__name__}"
                mark(loc)
        return len(self.filled) - before

    def one(self, f: dict, loc, q: str) -> None:
        page = self.page
        if f["type"] == "file":
            if re.search(r"autofill|parse|apply with", q, re.I):  # Ashby's "Autofill from resume" box: not the real field
                return
            if self.letter and re.search(r"cover", q, re.I):
                loc.set_input_files(str(self.letter))
                self.filled.append("Cover letter (PDF)")
                return
            if self.resume and not self.uploaded and re.search(r"resume|cv|attach|upload|file", q, re.I) and not re.search(r"cover|transcript|portfolio", q, re.I):
                loc.set_input_files(str(self.resume))
                self.uploaded = True
                self.filled.append("Resume (tailored PDF)")
                time.sleep(2.5 if self.site == "lever" else 1)  # Lever fills fields from the CV: read the page after it
            return
        if f["tag"] == "textarea" and self.letter_text and re.search(r"cover letter", q, re.I) and not f["value"].strip():
            loc.fill(self.letter_text)
            self.filled.append("Cover letter (pasted text)")
            return
        key, value = answer_for(q)
        if f["type"] == "yesno":
            if f["value"] or not key or not value:
                if f["required"] and not f["value"]:
                    self.left[q[:80]] = "Yes/No"
                    mark(loc)
                return
            if value.lower() not in ("yes", "no"):
                self.left[q[:80]] = "Yes/No"
                mark(loc)
                return
            want = value.capitalize()
            loc.locator(f"button:text-is('{want}')").first.click()
            self.filled.append(f"{q[:50]} → {want}")
            return
        if f["type"] in ("radio", "checkbox"):
            group = f["name"] or q
            if group in self.groups or not key or not value:
                return
            if pick_option([f["optionLabel"]], value, key):
                loc.check()
                self.groups.add(group)
                self.filled.append(f"{q[:50]} → {f['optionLabel']}")
            return
        if f["value"].strip():
            return  # already filled (by the site, the CV parser, or you)
        if not key or not value:
            if f["required"]:
                self.left[q[:80]] = ""
                mark(loc)
            return
        if f["tag"] == "select":
            opt = pick_option(f["options"], value, key)
            if opt:
                loc.select_option(label=opt)
                self.filled.append(f"{q[:50]} → {opt}")
            elif f["required"]:
                self.left[q[:80]] = "no matching option"
                mark(loc)
            return
        listy = f["role"] == "combobox" or f["haspopup"] in ("listbox", "true") or f["autocomplete"] in ("list", "both") \
            or (key in TYPEAHEAD_KEYS and re.search(r"start typing|search|select", q, re.I))
        if listy:
            got = choose_from_list(page, loc, value, key)
            if got:
                self.filled.append(f"{q[:50]} → {got}")
            else:
                self.left[q[:80]] = f"pick “{value}” from the list"
                mark(loc)
            return
        loc.fill(value)
        self.filled.append(f"{q[:50]} → {value}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--job", default=None, help="dashboard job id, for the tailored resume and to mark it Applied")
    ap.add_argument("--label", default="", help="company and title, for file names")
    ap.add_argument("--check", action="store_true", help="headless trial: fill, report, quit. Nothing is ever sent.")
    args = ap.parse_args()
    site = site_of(args.url)
    label = slug(args.label or "tailored")
    profile = ROOT / "data" / "cache" / ("chrome-check" if args.check else "chrome-profile")
    profile.mkdir(parents=True, exist_ok=True)
    PROOF.mkdir(parents=True, exist_ok=True)
    out_dir = ROOT / "applications" / "resumes"
    out_dir.mkdir(parents=True, exist_ok=True)
    state = server_state()
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile), channel="chrome", headless=args.check, viewport=None if not args.check else {"width": 1280, "height": 900},
                                                   args=[] if args.check else ["--start-maximized"])
        resume = letter = None
        letter_text = ""
        if state is None:
            print("! Dashboard server isn't running, so there's no tailored resume to upload. Start it with ./run.sh.")
        else:
            resume = build_pdf(ctx, f"{SERVER}/app/resume.html?embed=1&job={args.job or ''}", out_dir / f"{FILE_NAME}-Resume-{label}.pdf")
            letter_text = ((state.get("jobs", {}).get(args.job or "", {}) or {}).get("coverLetter") or "")
            if letter_text:
                letter = build_pdf(ctx, f"{SERVER}/app/studio.html?print=1&doc=letter&job={args.job}", out_dir / f"{FILE_NAME}-Cover-Letter-{label}.pdf")

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(args.url, wait_until="domcontentloaded")
        time.sleep(2.5)
        print(f"Site: {site}")
        if is_closed(page):
            print("✕ This posting looks closed (the page says it's no longer open). Marked it on the dashboard.")
            patch(args.job, {"posting": "closed", "postingChecked": date.today().isoformat()})
        open_application(page, site)
        if site == "workday" and not page.locator("[data-automation-id=legalNameSection_firstName]").count():
            print("Workday needs an account on this company's site. Sign in or create one in the window: I'll fill each step after that.")
        try:
            page.wait_for_selector("input, textarea, select", timeout=15000)
        except PWTimeout:
            print("! No form found yet. Sign in or click Apply yourself: I'll keep watching and fill it when it appears.")
        form_url = page.url
        filler = Filler(page, site, resume, letter, letter_text)
        filler.pass_once()
        time.sleep(1)
        filler.pass_once()  # fields that appear after the first answers (follow-up questions)

        def report() -> None:
            print(f"\n✓ Filled {len(filler.filled)} fields:")
            for x in filler.filled:
                print("   ", x)
            if filler.left:
                print(f"\n✎ Left for you ({len(filler.left)}), outlined in yellow:")
                for q, why in filler.left.items():
                    print("   ", q, f"({why})" if why else "")

        report()
        shot = PROOF / f"{date.today().isoformat()}-{label}-filled.png"
        try:
            page.screenshot(path=str(shot), full_page=True)
            print(f"\n📸 Filled form saved: {shot.relative_to(ROOT)}")
        except Exception:
            pass
        (PROOF / f"{date.today().isoformat()}-{label}-filled.json").write_text(json.dumps(
            {"url": args.url, "site": site, "filled": filler.filled, "left": filler.left, "resume": str(resume and resume.relative_to(ROOT))}, indent=1))
        if args.check:
            print("\nTrial only: nothing was sent.")
            ctx.close()
            return

        print("\nNothing has been submitted. Review every field, then press Submit yourself.")
        print("I'll keep filling new steps, and mark the job Applied when the site confirms it. Close the window when you're done.")
        confirmed = False
        try:
            while ctx.pages:
                time.sleep(1.5)
                page = ctx.pages[-1]
                filler.page = page
                if not confirmed:
                    try:
                        moved = page.url.split("?")[0] != form_url.split("?")[0]
                        body = page.locator("body").inner_text(timeout=2000)[:4000]
                        form_gone = not page.locator("input[type=file], input[type=email]").count()
                    except Exception:
                        continue
                    if (CONFIRM_URL.search(page.url) and moved) or (CONFIRM_TEXT.search(body) and (moved or form_gone)):
                        confirmed = True
                        proof = PROOF / f"{date.today().isoformat()}-{label}-submitted.png"
                        try:
                            page.screenshot(path=str(proof), full_page=True)
                        except Exception:
                            pass
                        patch(args.job, {"status": "applied", "appliedOn": date.today().isoformat(), "updated": date.today().isoformat(),
                                         "proof": str(proof.relative_to(ROOT)), "appliedVia": f"form filler ({site})"})
                        print(f"\n🎉 The site confirmed your application. Marked Applied, proof saved: {proof.relative_to(ROOT)}")
                        continue
                    if filler.pass_once():  # a new step of a multi-step form
                        report()
        except KeyboardInterrupt:
            pass
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
