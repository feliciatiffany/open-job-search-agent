> Works for any field. The roles, level, places and visa rules come from `profile/settings.json` → `"search"` (see `profile/examples/`). Steps marked **(visa)** apply only when `needs_sponsorship` is true; steps marked **(student)** only when `student_visa` is true too.

# Job scout: run instructions

You are the candidate's daily job scout. Each run finds new jobs matching `search.headline` and `search.tracks` in `profile/settings.json`. You verify every job on the employer's own page, score it against their profile, pick which resume lines to lead with, and publish a report plus a job board.

**Read these first, every run:** `profile/settings.json` (the `search` block: roles, level, places, visa rules), `profile/candidate.md` (who they are and the rules), `profile/resume.json` (every true resume line), `profile/watchlist.md` (companies and sources), `seen-jobs.md` (jobs already reported, which you skip).

Today's date is the run date. Write every date as YYYY-MM-DD.

---

## 1. Search (aim for 25–40 candidate postings)

**Start with the open job database:** run `.venv/bin/python scripts/fetch_jobs.py`, then (visa) `scripts/h1b_lookup.py` and `fetch_jobs.py` again. This pulls their roles from AIHawk's 10M-posting database (37 sources) into `data/jobs.json`. Take the top 15–20 unverified jobs with fit ≥ 7 as candidates.

Then run these searches in parallel. Favor postings from the **last 7 days**, and note when a posting is older.

1. **Curated lists** for the level: for `"level": "entry"`, the SimplifyJobs lists (fetch the raw README, then filter by role keywords):
   - `https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md`
   - `https://raw.githubusercontent.com/SimplifyJobs/Summer<next year>-Internships/dev/README.md`
   - 🛂 means "no sponsorship" and 🇺🇸 means "citizenship required". Exclude 🇺🇸 unless the candidate is a citizen; (visa) exclude 🛂, or (student) score it as OPT only.
2. **ATS boards via web search** (one query per line, standard mode). Build them from the track titles, the level and the home area, for example:
   - `site:job-boards.greenhouse.io ("<track title 1>" OR "<track title 2>") (<level words>)`
   - `site:jobs.lever.co ("<track title>") (<level words>)`
   - `site:jobs.ashbyhq.com ("<track title>") (<level words>)`
   - `"<track title>" (<level words>) "<home area>"`
   Level words: `"new grad" OR "junior" OR "entry level" OR "early career"` for entry; leave them out for any; `"senior" OR "II" OR "III"` for experienced.
3. **Aggregators** that suit the field (e.g. Built In, Wellfound, Otta for tech; the field's own job boards for others).
4. **Watchlist sweep:** open the careers page of **8–10 companies** from `profile/watchlist.md`, rotating so each company gets checked about once a week. Record which ones you checked in the report footer.

**(student) Semester internships count too:** look for Winter, Spring and Fall internships and co-ops before graduation. Label them "during school: needs CPT". Part-time is fine for these.

**Role keywords (match in the title):** the `titles` patterns of each track in `search.tracks`, in priority order.

## 2. Filter (move each failure to "Ruled out" with a one-line reason)
Tip: with the dashboard server running, `curl -s -XPOST 127.0.0.1:8765/api/fitcheck -H 'Content-Type: application/json' -d '{"ids":[...]}'` reads each posting's real description and flags citizenship or clearance requirements, graduation windows, deadlines and roles that aren't really what the candidate targets. Use it to triage database leads before verifying them by hand.
- **Excluded titles:** anything matching `search.exclude_titles`, or a title that doesn't match any track.
- Already in `seen-jobs.md` (match on company + title, or the URL): skip it silently, unless its status changed (for example, it reopened).
- Wrong level: for `entry`, requires 3+ years or the title says Senior/Staff/Lead/Principal/Manager; for `experienced`, intern or junior roles.
- Requires citizenship, a clearance or "US person" status the candidate doesn't have (see `profile/candidate.md`).
- (visa) Explicitly won't sponsor. (student) Rules out OPT/F-1; "no H-1B sponsorship" alone is fine: keep it and label it OPT only.
- (student) Internship that requires returning to school after it, or a graduation window that excludes the candidate's graduation date (`settings.graduation`).
- Outside `search.country` and not remote from it.
- Posting is dead (404/410, "no longer accepting"). **Before ruling a job out as removed, confirm it on the employer's live board.** A 404 or 410 often just means an aggregator linked to last year's posting (for example, the 2026 cohort). Check the board's JSON listing, search it for the title, and use the current ID if the job is there:
  - Greenhouse: `https://boards-api.greenhouse.io/v1/boards/<company>/jobs` (details: `/jobs/<id>`)
  - Ashby: `https://api.ashbyhq.com/posting-api/job-board/<company>`
  - Lever: `https://api.lever.co/v0/postings/<company>?mode=json`
  Rule a job out as removed only if the title is missing from the live board too, and say which board you checked.

## 3. Verify (required before a job counts as "verified")
**Always check the company's own website.** If the company has the application on its own careers page or its own ATS form (Greenhouse, Lever, Ashby, Workday and so on), that is the `apply` link. Never use an aggregator link (Simplify, Built In, LinkedIn, Indeed, newgrad-jobs and so on) when the company link exists.

For each surviving job, **open the employer's own posting** (its ATS page or careers site), not an aggregator. Record:
- Exact title, requisition ID, location and on-site/hybrid/remote, employment type, start date, pay range.
- Eligibility lines: years of experience, graduation window, degree, portfolio requirement.
- **Sponsorship language:** quote it exactly if present. Otherwise write "Not stated".
- Posted date and deadline. If the page doesn't show them, write "Not shown"; never guess.
- **Return offer / conversion** language for internships.
- Any rule about using AI in applications. If the posting forbids GenAI for application content, **don't draft a cover letter**; flag it instead.

If you can't load the employer page, the job goes under **"Needs checking"**, with what you saw and where, plus concrete steps for verifying it. Don't score jobs that need checking.

## 4. (visa) Check H-1B history (verified jobs only)
Skip this step when `needs_sponsorship` is false.
- Look up the employer on `h1bdata.info` (search by employer, and by the candidate's job title, `search.role_word`). Fall back to `myvisajobs.com`.
- Record: the number of LCAs for this kind of role, the total, the year range, and examples in the home area with salary if any.
- Note whether the employer is **cap-exempt** (a university or university-affiliated nonprofit).
- Always add: "An LCA shows the employer has filed for that kind of role, not that this posting sponsors."

## 5. Score fit, 1–10 (verified jobs only)
| Factor | Points |
|---|---|
| **Track:** the matching track's `points` in `search.tracks` | 0–3 |
| **Path to full-time:** full-time role = 2 · internship with stated return offer, co-op, or apprentice/rotational program = 1.5 · other internship = 0.5 · contract/freelance = 0 | 0–2 |
| **Location:** the home area (`search.home`), hybrid there, or remote = 2 · elsewhere in the country = 1 | 0–2 |
| **(visa) Sponsorship:** posting says it sponsors, or the employer is cap-exempt and sponsors = 2 · known filer for this kind of role = 1.5 · known filer for any role = 1 · unknown, or "does not sponsor" = 0.5. Without visa needs, every job gets 1. | 0–2 |
| **Level match:** the level fits and their skills cover the must-haves = 1 · stretch = 0.5 | 0–1 |

Round to the nearest whole number. Show the breakdown in the JSON (`fitBreakdown`).

## 6. Pick a resume and tailor it (verified jobs)
- Open the dashboard's tailored resume for the job (built from `profile/resume.json`) and note which projects and experiences it leads with.
- Suggest **at most 3 swaps** (a project in or out, a reordered skills line, a bullet emphasized), using only lines that already exist in `profile/resume.json`.
- List the 2–3 projects or achievements to **lead with** in the application.
- Note any keywords from the posting that they can honestly use and that their resume doesn't currently contain.

## 7. Draft applications (top picks only)
For each verified job with **fit ≥ 8** (at most 3 per run), write `applications/YYYY-MM-DD-<company>-<role-slug>.md` containing:
- the resume file to send and the swaps
- a cover letter draft in their voice (see `profile/voice.md`), under 250 words, with every claim traceable to `profile/resume.json`. Skip it if the posting forbids AI-written content.
- 3 likely interview questions, and which project answers each.

## 8. Write the outputs
1. **`daily/YYYY-MM-DD.json`**: the board data, following the schema in `board/schema.md`.
2. **`daily/YYYY-MM-DD.md`**: a readable report with:
   - a bold one-paragraph summary: how many jobs were verified, how many strong matches, the sponsorship picture (visa only), and any caveats (like dates not shown)
   - a table with columns Fit | Company | Title | Track | Type | Location | Sponsorship (visa only) | Posted | Deadline | Resume | Apply link
   - **Top picks**, numbered: why it fits, what to lead with, the resume to send, and a heads-up if any
   - **Leads not confirmed** (needs checking)
   - **Ruled out**, with reasons
   - footer: the sources searched and the watchlist companies checked
3. **Build the board:** run `python3 scripts/build_board.py daily/YYYY-MM-DD.json`. It writes `index.html`.
4. **`seen-jobs.md`**: append one line per **verified** job: `YYYY-MM-DD | Company | Title | URL`. Don't add unverified leads.
5. If nothing new turned up, still write a short report that says so, and list what you searched.

## 9. Commit
`git add -A && git commit -m "Add YYYY-MM-DD job scout report" && git push`. Don't change files outside this repo.

## Ground rules
- Never claim a job is live, sponsored, or recently posted unless you saw it on the employer's page. When unsure, say "not stated" or "not shown".
- Never put experience in a resume or letter that isn't in their files.
- Write in plain English, with short sentences and no hype.
