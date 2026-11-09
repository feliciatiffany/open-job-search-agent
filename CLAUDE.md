# Open Job Search Agent (CLAUDE.md)

An open-source AI job-search assistant that works for any field. What to search for (roles, level, country, home area, visa needs) lives in `profile/settings.json` → `"search"`; `profile/examples/` has setups for other careers. The sample candidate in `profile/` (an entry-level designer) is fictional.

- Run the daily scout with `/scout` (follows `SCOUT.md`). Tailor one job with `/tailor <posting URL>`.
- Never invent experience: only use `profile/resume.json` and `profile/facts.md`.
- Never click Submit on an application.
- Dashboard: `./run.sh` (http://127.0.0.1:8765). See README.md for how the pieces fit.
