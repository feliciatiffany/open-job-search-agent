#!/usr/bin/env python3
"""Build index.html from board/template.html and a daily JSON file.

Usage: python3 scripts/build_board.py daily/YYYY-MM-DD.json
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_JOB = {"id", "status", "company", "title", "track", "type", "location", "nyc", "tags", "details", "apply"}


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    data = json.loads(Path(sys.argv[1]).read_text())
    for key in ("runLabel", "jobs", "excluded"):
        if key not in data:
            sys.exit(f"missing top-level key: {key}")
    for job in data["jobs"]:
        missing = REQUIRED_JOB - job.keys()
        if missing:
            sys.exit(f"{job.get('id', '?')}: missing {sorted(missing)}")
        if job["status"] == "verified" and not {"fit", "why", "lead"} <= job.keys():
            sys.exit(f"{job['id']}: verified jobs need fit, why, lead")
        if job["status"] == "check" and "todo" not in job:
            sys.exit(f"{job['id']}: needs-checking jobs need todo")
    template = (ROOT / "board" / "template.html").read_text()
    # Escape "</" so posting text can never close the <script> block.
    payload = json.dumps(data, ensure_ascii=False, indent=1).replace("</", "<\\/")
    (ROOT / "index.html").write_text(template.replace("__DATA__", payload, 1))
    verified = sum(j["status"] == "verified" for j in data["jobs"])
    print(f"index.html built: {verified} verified, {len(data['jobs']) - verified} to check, {len(data['excluded'])} ruled out")


if __name__ == "__main__":
    main()
