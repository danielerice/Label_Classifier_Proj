# probe_leak.py  — run from repo root
import os, time, json, requests
from dotenv import load_dotenv
load_dotenv()

S = requests.Session()
S.headers.update({"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                  "Accept": "application/vnd.github+json"})

def count(q):
    r = S.get("https://api.github.com/search/issues",
              params={"q": q, "per_page": 1}, timeout=30)
    time.sleep(2.5)
    if r.status_code != 200:
        print(f"  !! {r.status_code} {r.text[:200]}")
        return None
    return r.json()["total_count"]

probes = [
    ("rust C-bug + cmd in body", 'repo:rust-lang/rust is:issue label:C-bug "@rustbot label" in:body'),
    ("rust P-high",              'repo:rust-lang/rust is:issue label:P-high'),
    ("rust P-medium",            'repo:rust-lang/rust is:issue label:P-medium'),
    ("rust P-low",               'repo:rust-lang/rust is:issue label:P-low'),
    ("rust T-compiler",          'repo:rust-lang/rust is:issue label:T-compiler'),
    ("rust T-libs-api",          'repo:rust-lang/rust is:issue label:T-libs-api'),
    ("rust C-enhancement",       'repo:rust-lang/rust is:issue label:C-enhancement'),
    ("rust all issues",          'repo:rust-lang/rust is:issue'),
    ("k8s  priority/important-soon", 'repo:kubernetes/kubernetes is:issue label:"priority/important-soon"'),
    ("k8s  priority/backlog",    'repo:kubernetes/kubernetes is:issue label:"priority/backlog"'),
    ("k8s  all issues",          'repo:kubernetes/kubernetes is:issue'),
]

out = {}
for name, q in probes:
    n = count(q)
    out[name] = {"query": q, "count": n}
    print(f"{name:22} {n}")

json.dump(out, open("results/dataset_probe.json", "w"), indent=2)