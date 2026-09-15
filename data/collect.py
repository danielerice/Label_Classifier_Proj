"""Pull all issues from a GitHub repo via GraphQL to gzipped JSONL.

Why GraphQL and not REST: the REST /issues endpoint failed three ways on this
repo. Offset pagination is capped (page 400 -> 422). `since` returns an empty
list below some date floor. And walking a `since` cursor lost 60% of items with
no error raised — see results/raw_audit.json from the REST attempt.

GraphQL's issues connection has true cursor pagination (opaque `after` cursor,
no offsets), deterministic CREATED_AT ordering, and excludes pull requests at
the source — roughly a third of the items for the same coverage.

Output is deliberately shaped to match the REST response so data/prepare.py
needs no changes.

Run from the repo root:
    python data/collect.py
"""
import os
import json
import gzip
import time
from pathlib import Path

import yaml
import requests
from dotenv import load_dotenv

load_dotenv()

CFG = yaml.safe_load(open("config/axes.yaml"))
OWNER, NAME = CFG["repo"].split("/")
OUT = Path("data/raw")
OUT.mkdir(parents=True, exist_ok=True)
STATE = OUT / "_cursor.json"

QUERY = """
query($owner:String!, $name:String!, $cursor:String) {
  rateLimit { remaining resetAt }
  repository(owner:$owner, name:$name) {
    issues(first:100, after:$cursor,
           orderBy:{field:CREATED_AT, direction:ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title body createdAt updatedAt closedAt state
        author { login }
        comments { totalCount }
        labels(first:60) { nodes { name } pageInfo { hasNextPage } }
      }
    }
  }
}
"""

S = requests.Session()
S.headers.update({
    "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
    "Content-Type": "application/json",
})


def gql(cursor, tries=8):
    payload = {"query": QUERY,
               "variables": {"owner": OWNER, "name": NAME, "cursor": cursor}}
    for attempt in range(tries):
        try:
            r = S.post("https://api.github.com/graphql", json=payload, timeout=60)
        except requests.exceptions.RequestException as e:
            # GitHub closes idle/long-lived connections. Across ~640 requests
            # this will happen; it is not a reason to lose the run.
            wait = min(5 * 2 ** attempt, 120)
            print(f"  network error ({type(e).__name__}), retry in {wait}s",
                  flush=True)
            time.sleep(wait)
            continue
        if r.status_code == 200:
            body = r.json()
            if "errors" in body:
                # GraphQL reports errors with HTTP 200. Retry transient ones,
                # fail loudly on the rest rather than returning partial data.
                msgs = "; ".join(e.get("message", "?") for e in body["errors"])
                if any(w in msgs.lower() for w in ("timeout", "rate limit", "secondary")):
                    time.sleep(30 * (attempt + 1))
                    continue
                raise RuntimeError(f"GraphQL error: {msgs}")
            return body["data"]
        if r.status_code in (403, 429, 502, 503):
            time.sleep(30 * (attempt + 1))
            continue
        r.raise_for_status()
    raise RuntimeError(f"failed after {tries} tries at cursor {cursor}")


def to_rest_shape(node):
    """Match the REST /issues field names so prepare.py is unchanged."""
    return {
        "number": node["number"],
        "title": node["title"] or "",
        "body": node["body"] or "",
        "created_at": node["createdAt"],
        "updated_at": node["updatedAt"],
        "closed_at": node["closedAt"],
        "state": (node["state"] or "").lower(),
        "user": {"login": (node["author"] or {}).get("login")},
        "comments": node["comments"]["totalCount"],
        "labels": [{"name": l["name"]} for l in node["labels"]["nodes"]],
    }


def main():
    state = json.load(open(STATE)) if STATE.exists() else {"cursor": None, "batch": 0}
    cursor, batch = state["cursor"], state["batch"]
    if cursor:
        print(f"resuming at batch {batch}")

    total, truncated_labels = 0, 0
    while True:
        data = gql(cursor)
        conn = data["repository"]["issues"]
        nodes = conn["nodes"]

        if not nodes and batch == 0:
            raise SystemExit(
                "first request returned no issues. Check GITHUB_TOKEN and the\n"
                "`repo` value in config/axes.yaml before assuming an empty repo.")
        if not nodes:
            break

        truncated_labels += sum(
            1 for n in nodes if n["labels"]["pageInfo"]["hasNextPage"])

        batch += 1
        path = OUT / f"batch_{batch:05d}.jsonl.gz"
        tmp = path.with_name(path.name + ".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            for n in nodes:
                f.write(json.dumps(to_rest_shape(n)) + "\n")
        tmp.rename(path)          # atomic: a killed run leaves no half file
        total += len(nodes)

        cursor = conn["pageInfo"]["endCursor"]
        json.dump({"cursor": cursor, "batch": batch}, open(STATE, "w"))

        if batch % 25 == 0:
            rl = data["rateLimit"]
            print(f"batch {batch}: {total} issues  "
                  f"(#{nodes[-1]['number']}, {nodes[-1]['createdAt'][:10]}, "
                  f"quota {rl['remaining']})", flush=True)

        if not conn["pageInfo"]["hasNextPage"]:
            break
        time.sleep(0.3)

    print(f"\ndone. {total} issues in {batch} batches.")
    if truncated_labels:
        print(f"WARNING: {truncated_labels} issues had >60 labels and were "
              f"truncated. Raise the labels(first:) limit and re-run.")


if __name__ == "__main__":
    main()
