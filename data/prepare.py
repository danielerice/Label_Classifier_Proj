"""Raw JSONL -> data/processed/{train,val,test}.parquet + label_stats.json

All repo-specific behaviour comes from config/axes.yaml. This file should not
need editing to target a different repository.

Run from the repo root:
    python data/prepare.py
"""
import re
import json
import gzip
from pathlib import Path
from collections import Counter, defaultdict

import yaml
import pandas as pd

CFG = yaml.safe_load(open("config/axes.yaml"))
PROC = Path("data/processed")

# --------------------------------------------------------------- text cleaning

HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
FORM_HEADER = re.compile(r"^#{1,4}\s+.*$", re.MULTILINE)
FENCE = re.compile(r"```.*?```", re.DOTALL)

# rustbot's command grammar is deliberately flexible:
#   @rustbot label A-diagnostics
#   @rustbot label: +T-lang, -T-compiler
#   @rustbot modify labels to +T-lang and -T-compiler
#   @rustbot labels "+good first issue"
# plus assignment commands that aren't label-setting but are boilerplate anyway.
BOT_CMD = re.compile(
    r"^\s*@rustbot\s+(?:modify\s+)?labels?\b.*$"
    r"|^\s*@rustbot\s+(?:claim|release-assignment|ping|note|blocked|author|ready)\b.*$"
    r"|^\s*r\?\s*@?[\w\-/]+\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def collapse_fences(text, max_lines):
    """Keep the head of a code block, drop the tail. Backtraces and rustc
    output run to thousands of tokens; the first lines carry the signal."""
    def repl(m):
        lines = m.group(0).splitlines()
        if len(lines) <= max_lines + 1:
            return m.group(0)
        return "\n".join(lines[:max_lines]) + "\n... [truncated] ...\n```"
    return FENCE.sub(repl, text)


def clean(title, body):
    t = CFG["text"]
    body = body or ""
    if t["strip_html_comments"]:
        body = HTML_COMMENT.sub(" ", body)
    if t["strip_bot_commands"]:
        body = BOT_CMD.sub(" ", body)
    body = collapse_fences(body, t["code_fence_max_lines"])
    if t["strip_form_headers"]:
        body = FORM_HEADER.sub(" ", body)
    text = f"{title or ''}\n\n{body}"
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()[: t["max_chars"]]


# -------------------------------------------------------------------- loading

def load_raw():
    files = sorted(Path("data/raw").glob("*.jsonl.gz"))
    if not files:
        raise SystemExit("no files in data/raw — run data/collect.py first")
    seen, rows = set(), []
    for p in files:
        with gzip.open(p, "rt", encoding="utf-8") as f:
            for line in f:
                it = json.loads(line)
                if "pull_request" in it:      # the issues endpoint returns PRs too
                    continue
                if it["number"] in seen:
                    continue
                seen.add(it["number"])
                rows.append({
                    "number": it["number"],
                    "title": it.get("title") or "",
                    "body": it.get("body") or "",
                    "created_at": it["created_at"],
                    "closed_at": it.get("closed_at"),
                    "state": it["state"],
                    "user": (it.get("user") or {}).get("login"),
                    "n_comments": it.get("comments", 0),
                    "labels": [l["name"] for l in it.get("labels", [])],
                })
    df = pd.DataFrame(rows)
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True)
    return df.sort_values("created_at").reset_index(drop=True)


# ------------------------------------------------------------ axis derivation

AXIS_RX = {name: re.compile(spec["pattern"]) for name, spec in CFG["axes"].items()}
EXCL_LABELS = set(CFG.get("excluded_labels") or {})
EXCL_RX = [re.compile(e["pattern"]) for e in (CFG.get("excluded_patterns") or [])]


def axis_of(label):
    """Return the axis name for a label, or None. Declaration order wins."""
    if label in EXCL_LABELS:
        return None
    if any(rx.search(label) for rx in EXCL_RX):
        return None
    for axis, rx in AXIS_RX.items():
        if rx.match(label):
            return axis
    return None


def derive(df):
    label_counts = Counter()
    axis_rows = defaultdict(set)

    for i, labels in enumerate(df["labels"]):
        for name in labels:
            axis = axis_of(name)
            if axis is None:
                continue
            label_counts[name] += 1
            axis_rows[axis].add(i)

    keep_labels = {l for l, c in label_counts.items()
                   if c >= CFG["min_label_support"]}
    keep_axes = {a for a, rows in axis_rows.items()
                 if len(rows) >= CFG["min_axis_support"]}
    keep_labels = {l for l in keep_labels if axis_of(l) in keep_axes}

    df["y"] = [sorted(set(ls) & keep_labels) for ls in df["labels"]]

    per_axis = defaultdict(dict)
    for l in sorted(keep_labels):
        per_axis[axis_of(l)][l] = label_counts[l]

    stats = {
        "repo": CFG["repo"],
        "kept_axes": {
            a: {"n_labels": len(per_axis[a]),
                "n_issues": len(axis_rows[a]),
                "labels": per_axis[a]}
            for a in sorted(keep_axes)
        },
        "dropped_axes_low_support": {
            a: len(axis_rows[a]) for a in sorted(set(axis_rows) - keep_axes)},
        "n_issues_total": int(len(df)),
        "n_issues_labeled": int((df["y"].str.len() > 0).sum()),
    }
    return df, stats


# ---------------------------------------------------------------------- split

def time_split(df):
    s = CFG["split"]
    cutoff = df["created_at"].max() - pd.Timedelta(days=s["exclude_created_after_days"])
    df = df[df["created_at"] <= cutoff]
    df = df[df["y"].str.len() > 0].reset_index(drop=True)
    n = len(df)
    a = int(n * s["train_frac"])
    b = int(n * (s["train_frac"] + s["val_frac"]))
    return df.iloc[:a], df.iloc[a:b], df.iloc[b:]


def main():
    df = load_raw()
    print(f"{len(df)} issues after removing PRs")

    # Keep the leak flag rather than discarding the evidence. It supports the
    # ablation (stripped vs. not) and the subgroup check: does the model do
    # worse on issues that never carried a command?
    df["had_bot_cmd"] = df["body"].fillna("").str.contains(BOT_CMD)
    df["text"] = [clean(t, b) for t, b in zip(df["title"], df["body"])]

    df, stats = derive(df)
    print(f"{stats['n_issues_labeled']} issues with at least one kept label\n")
    for axis, d in stats["kept_axes"].items():
        print(f"  {axis:10} {d['n_labels']:4} labels  {d['n_issues']:7} issues")
    if stats["dropped_axes_low_support"]:
        print(f"  dropped (low support): {stats['dropped_axes_low_support']}")
    print()

    tr, va, te = time_split(df)
    PROC.mkdir(parents=True, exist_ok=True)
    cols = ["number", "created_at", "text", "y", "had_bot_cmd"]
    for name, part in [("train", tr), ("val", va), ("test", te)]:
        part[cols].to_parquet(PROC / f"{name}.parquet")
        print(f"  {name:6} {len(part):7}  "
              f"{part['created_at'].min().date()} -> {part['created_at'].max().date()}")

    stats["split_sizes"] = {"train": len(tr), "val": len(va), "test": len(te)}
    stats["split_ranges"] = {
        name: [str(part["created_at"].min().date()),
               str(part["created_at"].max().date())]
        for name, part in [("train", tr), ("val", va), ("test", te)]}
    stats["bot_cmd_rate_overall"] = round(float(df["had_bot_cmd"].mean()), 4)

    # Per-label rate. The overall figure is diluted by unlabeled issues and by
    # axes where commands are rare. The search-API probe measured the rate
    # WITHIN C-bug (8.0%), so the per-label number is what's comparable to it —
    # a large gap there means BOT_CMD is missing command variants.
    by_label = {}
    for axis, d in stats["kept_axes"].items():
        for lab in d["labels"]:
            m = df["y"].apply(lambda ys: lab in ys)
            if m.sum():
                by_label[lab] = round(float(df.loc[m, "had_bot_cmd"].mean()), 4)
    stats["bot_cmd_rate_by_label"] = dict(
        sorted(by_label.items(), key=lambda kv: -kv[1]))

    # results/ is committed; data/processed/ is gitignored. This file is
    # evidence for the write-up, so it belongs in the former.
    Path("results").mkdir(exist_ok=True)
    json.dump(stats, open("results/label_stats.json", "w"), indent=2)

    print(f"\nbot command in {stats['bot_cmd_rate_overall']:.1%} of all issue bodies")
    top = list(stats["bot_cmd_rate_by_label"].items())[:5]
    for lab, rate in top:
        print(f"  {lab:28} {rate:.1%}")
    print("\nwrote results/label_stats.json")


if __name__ == "__main__":
    main()
