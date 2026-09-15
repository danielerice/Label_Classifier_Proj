import os, requests
from dotenv import load_dotenv
load_dotenv()

S = requests.Session()
S.headers.update({
    "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
})

for page in (1, 400, 900, 1300):
    r = S.get("https://api.github.com/repos/rust-lang/rust/issues",
              params={"state": "all", "per_page": 100, "sort": "created",
                      "direction": "asc", "page": page}, timeout=30)
    data = r.json() if r.ok else None
    n = len(data) if isinstance(data, list) else None
    first = data[0]["created_at"][:10] if n else "-"
    print(f"page {page:5}  {r.status_code}  n={n}  first={first}")