# Example setups

Each file holds a ready-made `"search"` block for a different kind of job seeker. To use one, copy its `"search"` object into `profile/settings.json` (replacing the sample designer's), then run `./run.sh --refresh`.

| File | Who it's for |
|---|---|
| `software-engineer.json` | Mid-level backend/platform engineer, Seattle or remote, no visa needed |
| `data-analyst.json` | New-grad data analyst in the UK, London or remote |
| `marketing.json` | Marketer at any level, Chicago or remote |
| *(the default in `settings.json`)* | Entry-level designer graduating soon, NYC or remote, on an F-1 visa |

## The `search` settings

| Key | What it does |
|---|---|
| `headline` | One line describing the roles you want. The AI fit check uses it. |
| `tracks` | The kinds of role you want, in priority order. `titles` is a lowercase regular expression matched against job titles; `points` (1–3) is how much it adds to the fit score; `on` sets whether it's shown by default; `key` links it to the role tags in `profile/resume.json`. |
| `exclude_titles` | Titles to always skip (regular expression). |
| `level` | `"entry"` (new grad, junior, intern, and skips senior titles), `"experienced"` (skips intern and junior titles), or `"any"`. |
| `country` | A country code such as `"US"`, `"GB"` or `"DE"`, or `"ANY"`. For countries other than the US, list other spellings and big cities in `country_names`. |
| `home` | Your preferred area: a `label` for the filter chip and a `pattern` (regular expression) that matches its locations. |
| `remote` | Include remote jobs. |
| `needs_sponsorship` | Turns on US H-1B checks: each employer's filing history, the visa filter and the visa tags. |
| `student_visa` | Also turns on F-1 student labels: OPT, and "needs CPT" for semester internships. |
| `role_word` | One word for people in your role (e.g. "designer"). Used for LinkedIn searches and H-1B labels. |
| `h1b_role_titles` | Which H-1B job titles count as your kind of role (regular expression). |
| `part_time` | Include part-time jobs. |
| `max_age_days` | Skip postings older than this. |

## Make `resume.json` match

`profile/resume.json` tags every experience, project and skill list by role key, so the tailored resume can pick the best lines for each job. Use the same `key`s as your tracks, for example:

```json
"skills": {"backend": ["Go", "PostgreSQL", "Kafka"], "platform": ["Kubernetes", "Terraform", "AWS"]},
"experience": [{"id": "acme", "title": "Software Engineer / Acme", "tracks": {"backend": 3, "platform": 2}, "bullets": ["…"]}]
```
