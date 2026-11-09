# Daily JSON schema (`daily/YYYY-MM-DD.json`)

```jsonc
{
  "date": "2026-11-08",
  "runLabel": "Sun 8 Nov 2026",            // shown in the header
  "sources": "SimplifyJobs lists, Greenhouse/Lever/Ashby search, …",
  "jobs": [
    {
      "id": "nyt-pd",                       // short unique slug
      "status": "verified",                 // "verified" | "check"
      "fit": 7,                             // verified only, 1–10
      "fitBreakdown": {"track":3,"path":0.5,"location":2,"sponsorship":1.5,"level":1},
      "company": "The New York Times",
      "title": "Product Design Intern",
      "track": "UI/UX · Product design",
      "type": "Intern · 10 weeks",
      "location": "New York, NY (hybrid)",
      "nyc": true,                          // NYC or NYC-hybrid (remote counts as false)
      "remote": false,
      "tags": [["Known H-1B filer","ok"],["$40/hr","plain"]],  // kinds: ok | warn | bad | plain
      "details": {"Requisition":"…","Sponsorship":"…","H-1B history":"…","Posted":"…","Deadline":"…"},
      "why": "…",                           // verified: why it fits
      "resume": "digital-design.pdf",       // verified: file in resumes/original/
      "swaps": ["Swap Echo Garden for Pantry"],  // optional, at most 3
      "lead": "Pantry · Neighborhood Archive",
      "note": "…",                          // optional heads-up
      "todo": ["…"],                        // "check" jobs: steps to verify
      "apply": "https://…",
      "applyLabel": "View on Simplify"      // optional; default "Open posting"
    }
  ],
  "excluded": [["Company · Title", "reason"]]
}
```
