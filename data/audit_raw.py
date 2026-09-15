"""Audit data/raw before trusting it.

NOTE: collect.py now uses GraphQL and fetches issues only. Pull requests share
the same number sequence, so gaps in issue numbers are EXPECTED and prove
nothing — roughly 60% of numbers will be missing because they're PRs. The
contiguity check that caught the REST cursor bug is useless here.

The real completeness check is a count comparison: the search API's `is:issue`
total against what we collected.

Run from the repo root:
    python data/audit_raw.py
"""
import os
import json
import gzip
import time
from pathlib import Path
from collections import Counter

import yaml
import requests
from dotenv import load_dotenv

load_dotenv()
CFG = yaml.safe_load(open("config/axes.yaml"))
REPO = CFG["repo"]

numbers, kind, created, labels_per = set(), Counter(), [], []
lines = 0

for p in sorted(Path("data/raw").glob("*.jsonl.gz")):
    with gzip.open(p, "rt", encoding="utf-8") as f:
        for line in f:
            it = json.loads(line)
            lines += 1
            numbers.add(it["number"])
            kind["pr" if "pull_request" in it else "issue"] += 1
            created.append(it["created_at"])
            labels_per.append(len(it.get("labels", [])))

if not lines:
    raise SystemExit("data/raw is empty — run data/collect.py first")

print(f"lines on disk      {lines}")
print(f"unique numbers     {len(numbers)}")
print(f"  issues           {kind['issue']}")
print(f"  pull requests    {kind['pr']}   (should be 0 with GraphQL)")
print(f"duplicate lines    {lines - len(numbers)}")
print(f"number range       #{min(numbers)} .. #{max(numbers)}")
print(f"created range      {min(created)[:10]} .. {max(created)[:10]}")
print(f"labels per issue   mean {sum(labels_per)/len(labels_per):.1f}, "
      f"max {max(labels_per)}, zero-label {labels_per.count(0)}")

S = requests.Session()
S.headers.update({
    "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
    "Accept": "application/vnd.github+json",
})


def search_count(q):
    r = S.get("https://api.github.com/search/issues",
              params={"q": q, "per_page": 1}, timeout=30)
    time.sleep(2.5)
    return r.json().get("total_count") if r.ok else None


live = search_count(f"repo:{REPO} is:issue")
result = {
    "lines": lines, "unique": len(numbers),
    "issues": kind["issue"], "prs": kind["pr"],
    "range": [min(numbers), max(numbers)],
    "live_issue_count": live,
}

if live:
    cov = len(numbers) / live
    print(f"\nlive is:issue      {live}")
    print(f"collected          {len(numbers)}")
    print(f"coverage           {cov:.1%}")
    result["coverage"] = round(cov, 4)
    if cov < 0.97:
        print("\n!! UNDER 97% — collection is incomplete, do not proceed to prepare.py")
    elif cov > 1.03:
        print("\n!! OVER 103% — more collected than exist. Check for PR contamination.")
    else:
        print("\nOK — proceed to data/prepare.py")

# Cross-check one axis against the probe, to confirm labels survived the
# GraphQL field mapping. Only meaningful once collection is essentially
# complete: rust's C-/A-/T- prefix taxonomy postdates its early issues, so a
# partial run that stops in 2012 legitimately finds zero C-bug. The mean
# labels-per-issue figure above is the mapping check that works at any coverage.
if result.get("coverage", 0) > 0.97:
    cbug_live = search_count(f"repo:{REPO} is:issue label:C-bug")
    cbug_local = sum(
        1 for p in sorted(Path("data/raw").glob("*.jsonl.gz"))
        for line in gzip.open(p, "rt", encoding="utf-8")
        if "C-bug" in [l["name"] for l in json.loads(line).get("labels", [])])
    print(f"\nC-bug live         {cbug_live}")
    print(f"C-bug collected    {cbug_local}")
    result["c_bug_live"], result["c_bug_local"] = cbug_live, cbug_local
    if cbug_live and cbug_local / cbug_live < 0.95:
        print("!! label mapping may be dropping labels — investigate")
else:
    print("\n(skipping C-bug cross-check: only meaningful at full coverage)")

Path("results").mkdir(exist_ok=True)
json.dump(result, open("results/raw_audit.json", "w"), indent=2)
print("\nwrote results/raw_audit.json")
