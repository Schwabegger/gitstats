#!/usr/bin/env python3
"""
gitstats.py — A modern git repository statistics generator.
Analyzes a git repository and produces a self-contained HTML report
with interactive charts and detailed statistics.

Usage:
    python3 gitstats.py <git_repo_path> [output_directory] [--blame [cache_dir]]

Requirements:
    - Python 3.7+
    - Git installed and accessible in PATH
"""

import subprocess
import os
import sys
import json
import re
import argparse
import hashlib
import configparser
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import html as html_module

# ─── Git Data Collection ────────────────────────────────────────────────────

def run_git(repo_path, args, timeout=300):
    """Run a git command and return stdout."""
    cmd = ["git", "-C", repo_path] + args
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "GIT_PAGER": "cat"}
        )
        return result.stdout
    except subprocess.TimeoutExpired:
        return ""
    except Exception as e:
        print(f"  Warning: git command failed: {' '.join(args[:3])}... ({e})")
        return ""


DEFAULT_BLAME_FILTER = """\
# gitstats blame filter configuration
# Lines starting with # are comments.
#
# [whitelist]
# If any extensions listed here, ONLY these are blamed. Blacklist is ignored for extensions.
# extensions = .py .js .ts
#
# [blacklist]
# Ignored when whitelist extensions are set.
# Note: binary files are always excluded automatically regardless of this config.
# extensions = .css .generated.cs ...
#
# [path_whitelist]
# Always included, even if path_blacklist would exclude it. Same syntax as path_blacklist.
# Use to carve out exceptions inside a blacklisted folder.
# patterns =
#
# [path_blacklist]
# Always applied regardless of extension whitelist.
# No leading slash = substring match anywhere in path (e.g. migration)
# Leading slash = anchored to repo root (e.g. /src/legacy matches src/legacy/... only)
# patterns = migration /src/legacy

[blacklist]
extensions = .css .scss .sass .less

[path_blacklist]
patterns = migration vendor node_modules dist build __pycache__ .min. .bundle. fixtures seed
"""


def load_blame_filter(cache_dir):
    """Load (and create if missing) blame_filter.ini from cache_dir."""
    config_path = Path(cache_dir) / "blame_filter.ini"
    if not config_path.exists():
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        config_path.write_text(DEFAULT_BLAME_FILTER)
        print()
        print(f"  Created blame filter config: {config_path}")
        print()
        print("  Review it — add/remove extensions and path patterns to suit your repo.")
        print("  Then re-run the same command to generate the report.")
        print()
        sys.exit(0)

    cp = configparser.ConfigParser(allow_no_value=True)
    cp.read_string(DEFAULT_BLAME_FILTER)   # load defaults first
    cp.read(str(config_path))              # override with user file

    def split_values(section, key):
        raw = cp.get(section, key, fallback="")
        return set(v.strip().lower() for v in raw.split() if v.strip())

    whitelist_exts    = split_values("whitelist", "extensions")
    blacklist_exts    = split_values("blacklist", "extensions")
    path_whitelist    = split_values("path_whitelist", "patterns")
    path_blacklist    = split_values("path_blacklist", "patterns")

    return {
        "whitelist_exts": whitelist_exts,
        "blacklist_exts": blacklist_exts,
        "path_whitelist": path_whitelist,
        "path_blacklist": path_blacklist,
    }


def _path_matches(flower, patterns):
    for pat in patterns:
        if pat.startswith("/"):
            if flower.startswith(pat[1:]):
                return True
        else:
            if pat in flower:
                return True
    return False


def blame_file_allowed(fpath, filt):
    """Return True if this file should be blamed."""
    flower = fpath.lower()
    # Path whitelist overrides path blacklist
    if filt["path_whitelist"] and _path_matches(flower, filt["path_whitelist"]):
        pass  # don't apply path blacklist, fall through to extension check
    elif _path_matches(flower, filt["path_blacklist"]):
        return False
    ext = Path(fpath).suffix.lower()
    if filt["whitelist_exts"]:
        return ext in filt["whitelist_exts"]
    return ext not in filt["blacklist_exts"]


def blame_cache_key(repo_path, commit_hash, filt=None):
    repo_id = hashlib.md5(os.path.abspath(repo_path).encode()).hexdigest()[:8]
    if filt:
        filt_sig = hashlib.md5(json.dumps({
            "wl": sorted(filt["whitelist_exts"]),
            "bl": sorted(filt["blacklist_exts"]),
            "pw": sorted(filt["path_whitelist"]),
            "pb": sorted(filt["path_blacklist"]),
        }, sort_keys=True).encode()).hexdigest()[:6]
    else:
        filt_sig = "nofilt"
    return f"{repo_id}_{commit_hash}_{filt_sig}.json"


def load_blame_cache(cache_dir, repo_path, commit_hash, filt=None):
    if not cache_dir:
        return None
    p = Path(cache_dir) / blame_cache_key(repo_path, commit_hash, filt)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return None
    return None


def save_blame_cache(cache_dir, repo_path, commit_hash, data, filt=None):
    if not cache_dir:
        return
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    p = Path(cache_dir) / blame_cache_key(repo_path, commit_hash, filt)
    p.write_text(json.dumps(data))


def blame_snapshot(repo_path, commit_hash, filt, cache_dir=None):
    """Return {author: line_count} for filtered tracked files at commit_hash."""
    cached = load_blame_cache(cache_dir, repo_path, commit_hash, filt)
    if cached is not None:
        return cached

    ls = run_git(repo_path, ["ls-tree", "-r", "--name-only", commit_hash], timeout=30)
    all_files = [f for f in ls.strip().split("\n") if f.strip()]
    files = [f for f in all_files if blame_file_allowed(f, filt)]

    cmd_base = ["git", "-C", repo_path]

    # Get binary file list for this commit in one shot — faster than per-file checks
    numstat_out = subprocess.run(
        cmd_base + ["diff", "--numstat", "4b825dc642cb6eb9a060e54bf8d69288fbee4904", commit_hash],
        capture_output=True, timeout=60,
        env={**os.environ, "GIT_PAGER": "cat"}
    ).stdout.decode("utf-8", errors="replace")
    binary_files = set()
    for line in numstat_out.splitlines():
        if line.startswith("-\t-\t"):
            binary_files.add(line[4:])

    author_lines = defaultdict(int)
    for fpath in files:
        if fpath in binary_files:
            continue

        cmd = cmd_base + ["blame", "--line-porcelain", commit_hash, "--", fpath]
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=60,
                env={**os.environ, "GIT_PAGER": "cat"}
            )
            raw = result.stdout.decode("utf-8", errors="replace")
            for line in raw.split("\n"):
                if line.startswith("author "):
                    author_lines[line[7:].strip()] += 1
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass

    result = dict(author_lines)
    save_blame_cache(cache_dir, repo_path, commit_hash, result, filt)
    return result


def collect_blame_over_time(repo_path, bucket_last_hash, sampled_buckets, filt, cache_dir=None):
    """Run blame at each sampled bucket. Returns {label: {author: lines}}."""
    print(f"  Running git blame at {len(sampled_buckets)} snapshots (this may take a while)...")
    timeline = {}
    for i, key in enumerate(sampled_buckets):
        h = bucket_last_hash[key]
        cached = load_blame_cache(cache_dir, repo_path, h, filt)
        src = "cache" if cached is not None else "git blame"
        print(f"    [{i+1}/{len(sampled_buckets)}] {key} ({src})...")
        data = blame_snapshot(repo_path, h, filt, cache_dir)
        timeline[key] = data
    return timeline


def collect_stats(repo_path, blame=False, blame_cache_dir=None):
    """Collect all statistics from the git repository."""
    stats = {}
    repo_path = os.path.abspath(repo_path)

    # Validate it's a git repo
    check = run_git(repo_path, ["rev-parse", "--is-inside-work-tree"])
    if "true" not in check:
        check_bare = run_git(repo_path, ["rev-parse", "--is-bare-repository"])
        if "true" not in check_bare:
            print(f"Error: '{repo_path}' is not a valid git repository.")
            sys.exit(1)

    # ── Blame filter — check before any heavy work ──
    blame_filt = None
    if blame:
        blame_filt = load_blame_filter(blame_cache_dir)  # exits if config was just created
        if blame_filt["whitelist_exts"]:
            print(f"Blame filter: whitelist {sorted(blame_filt['whitelist_exts'])}")
        else:
            print(f"Blame filter: blacklist {sorted(blame_filt['blacklist_exts'])}")
        if blame_filt["path_whitelist"]:
            print(f"Blame path whitelist: {sorted(blame_filt['path_whitelist'])}")
        if blame_filt["path_blacklist"]:
            print(f"Blame path blacklist: {sorted(blame_filt['path_blacklist'])}")

    print("Collecting repository data...")

    # ── Project info ──
    stats["repo_path"] = repo_path
    stats["repo_name"] = os.path.basename(repo_path.rstrip("/").rstrip("\\"))
    remote = run_git(repo_path, ["config", "--get", "remote.origin.url"]).strip()
    stats["remote_url"] = remote if remote else "N/A"

    # ── Commit log ──
    print("  Parsing commit history...")
    log_format = "%H|%at|%aN|%aE|%s"
    raw_log = run_git(repo_path, [
        "log", "--all", "--no-merges",
        f"--pretty=format:{log_format}",
        "--shortstat"
    ])

    commits = []
    lines = raw_log.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        parts = line.split("|", 4)
        if len(parts) >= 5 and len(parts[0]) == 40:
            commit = {
                "hash": parts[0],
                "timestamp": int(parts[1]) if parts[1].isdigit() else 0,
                "author": parts[2],
                "email": parts[3],
                "subject": parts[4],
                "files_changed": 0,
                "insertions": 0,
                "deletions": 0,
            }
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines):
                stat_line = lines[j].strip()
                m_files = re.search(r'(\d+) files? changed', stat_line)
                m_ins = re.search(r'(\d+) insertions?\(\+\)', stat_line)
                m_del = re.search(r'(\d+) deletions?\(-\)', stat_line)
                if m_files:
                    commit["files_changed"] = int(m_files.group(1))
                    commit["insertions"] = int(m_ins.group(1)) if m_ins else 0
                    commit["deletions"] = int(m_del.group(1)) if m_del else 0
                    i = j + 1
                    commits.append(commit)
                    continue
            commits.append(commit)
        i += 1

    if not commits:
        print("  Retrying with simpler log format...")
        raw_log = run_git(repo_path, [
            "log", "--all", "--no-merges",
            f"--pretty=format:{log_format}"
        ])
        for line in raw_log.strip().split("\n"):
            parts = line.strip().split("|", 4)
            if len(parts) >= 5 and len(parts[0]) == 40:
                commits.append({
                    "hash": parts[0],
                    "timestamp": int(parts[1]) if parts[1].isdigit() else 0,
                    "author": parts[2],
                    "email": parts[3],
                    "subject": parts[4],
                    "files_changed": 0, "insertions": 0, "deletions": 0,
                })

    stats["commits"] = commits
    stats["total_commits"] = len(commits)

    if not commits:
        print("  Warning: No commits found.")
        stats["first_commit_ts"] = 0
        stats["last_commit_ts"] = 0
    else:
        timestamps = [c["timestamp"] for c in commits if c["timestamp"] > 0]
        stats["first_commit_ts"] = min(timestamps) if timestamps else 0
        stats["last_commit_ts"] = max(timestamps) if timestamps else 0

    total_all = run_git(repo_path, ["rev-list", "--all", "--count"]).strip()
    stats["total_commits_all"] = int(total_all) if total_all.isdigit() else len(commits)
    total_head = run_git(repo_path, ["rev-list", "HEAD", "--count"]).strip()
    stats["total_commits_head"] = int(total_head) if total_head.isdigit() else None

    # Parse GitHub base URL from remote for commit links
    raw_remote = stats.get("remote_url", "")
    github_base = ""
    m = re.match(r"git@github\.com:(.+?)(?:\.git)?$", raw_remote)
    if m:
        github_base = f"https://github.com/{m.group(1)}"
    else:
        m = re.match(r"https?://github\.com/(.+?)(?:\.git)?$", raw_remote)
        if m:
            github_base = f"https://github.com/{m.group(1)}"
    stats["github_base"] = github_base

    # ── Current file stats ──
    print("  Counting files and lines...")
    ls_output = run_git(repo_path, ["ls-files"])
    all_files = [f for f in ls_output.strip().split("\n") if f.strip()]
    stats["total_files"] = len(all_files)

    ext_counter = Counter()
    for f in all_files:
        ext = Path(f).suffix.lower()
        ext_counter[ext if ext else "(no ext)"] += 1
    stats["file_extensions"] = dict(ext_counter.most_common(25))

    total_lines = 0
    lines_by_ext = defaultdict(int)
    for fpath in all_files:
        full = os.path.join(repo_path, fpath)
        try:
            with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                count = sum(1 for _ in fh)
                total_lines += count
                ext = Path(fpath).suffix.lower() or "(no ext)"
                lines_by_ext[ext] += count
        except (OSError, IOError):
            pass

    stats["total_lines"] = total_lines
    stats["lines_by_extension"] = dict(
        sorted(lines_by_ext.items(), key=lambda x: -x[1])[:20]
    )

    # ── Author statistics ──
    print("  Analyzing authors...")
    author_data = defaultdict(lambda: {
        "commits": 0, "insertions": 0, "deletions": 0,
        "first_commit": float("inf"), "last_commit": 0,
        "active_days": set(),
        "monthly_commits": defaultdict(int),
        "monthly_insertions": defaultdict(int),
        "monthly_deletions": defaultdict(int),
    })
    for c in commits:
        a = c["author"]
        author_data[a]["commits"] += 1
        author_data[a]["insertions"] += c["insertions"]
        author_data[a]["deletions"] += c["deletions"]
        ts = c["timestamp"]
        if ts > 0:
            if ts < author_data[a]["first_commit"]:
                author_data[a]["first_commit"] = ts
            if ts > author_data[a]["last_commit"]:
                author_data[a]["last_commit"] = ts
            day_key = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            author_data[a]["active_days"].add(day_key)
            ym = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")
            author_data[a]["monthly_commits"][ym] += 1
            author_data[a]["monthly_insertions"][ym] += c["insertions"]
            author_data[a]["monthly_deletions"][ym] += c["deletions"]

    all_months = set()
    for data in author_data.values():
        all_months.update(data["monthly_commits"].keys())
    all_months_sorted = sorted(all_months)

    authors_list = []
    for name, data in author_data.items():
        authors_list.append({
            "name": name,
            "commits": data["commits"],
            "insertions": data["insertions"],
            "deletions": data["deletions"],
            "first_commit": data["first_commit"] if data["first_commit"] != float("inf") else 0,
            "last_commit": data["last_commit"],
            "active_days": len(data["active_days"]),
            "monthly_commits": [data["monthly_commits"].get(m, 0) for m in all_months_sorted],
            "monthly_insertions": [data["monthly_insertions"].get(m, 0) for m in all_months_sorted],
            "monthly_deletions": [data["monthly_deletions"].get(m, 0) for m in all_months_sorted],
        })
    authors_list.sort(key=lambda x: -x["commits"])
    stats["authors"] = authors_list
    stats["total_authors"] = len(authors_list)
    stats["all_months"] = all_months_sorted

    # ── Per-author commit list ──
    author_commits_map = defaultdict(list)
    for c in sorted(commits, key=lambda x: -x["timestamp"]):
        author_commits_map[c["author"]].append({
            "hash": c["hash"],
            "ts": c["timestamp"],
            "subject": c["subject"],
        })
    stats["author_commits"] = {k: v for k, v in author_commits_map.items()}

    # ── Per-author activity patterns ──
    print("  Computing per-author activity patterns...")
    author_hour = defaultdict(lambda: [0]*24)
    author_dow = defaultdict(lambda: [0]*7)
    for c in commits:
        if c["timestamp"] <= 0:
            continue
        dt = datetime.fromtimestamp(c["timestamp"], tz=timezone.utc)
        author_hour[c["author"]][dt.hour] += 1
        author_dow[c["author"]][dt.weekday()] += 1
    stats["author_hour"] = {k: v for k, v in author_hour.items()}
    stats["author_dow"] = {k: v for k, v in author_dow.items()}

    # ── Activity data ──
    print("  Computing activity patterns...")
    hour_counts = [0] * 24
    dow_counts = [0] * 7
    month_counts = [0] * 12
    year_month_counts = defaultdict(int)
    year_counts = defaultdict(int)
    daily_counts = defaultdict(int)
    hour_dow_counts = defaultdict(int)

    for c in commits:
        if c["timestamp"] <= 0:
            continue
        dt = datetime.fromtimestamp(c["timestamp"], tz=timezone.utc)
        hour_counts[dt.hour] += 1
        dow_counts[dt.weekday()] += 1
        month_counts[dt.month - 1] += 1
        ym = dt.strftime("%Y-%m")
        year_month_counts[ym] += 1
        year_counts[dt.year] += 1
        day_key = dt.strftime("%Y-%m-%d")
        daily_counts[day_key] += 1
        hour_dow_counts[(dt.hour, dt.weekday())] += 1

    stats["hour_of_day"] = hour_counts
    stats["day_of_week"] = dow_counts
    stats["month_of_year"] = month_counts

    sorted_ym = sorted(year_month_counts.items())
    stats["year_month"] = {"labels": [x[0] for x in sorted_ym], "values": [x[1] for x in sorted_ym]}

    sorted_years = sorted(year_counts.items())
    stats["yearly"] = {"labels": [str(x[0]) for x in sorted_years], "values": [x[1] for x in sorted_years]}

    heatmap = []
    for dow in range(7):
        for hour in range(24):
            heatmap.append({"hour": hour, "dow": dow, "count": hour_dow_counts.get((hour, dow), 0)})
    stats["hour_dow_heatmap"] = heatmap
    stats["active_days"] = len(daily_counts)

    # ── Lines of code over time ──
    print("  Tracking code growth over time...")
    sorted_commits = sorted(commits, key=lambda c: c["timestamp"])

    # Determine granularity based on repo age
    valid_ts = [c["timestamp"] for c in sorted_commits if c["timestamp"] > 0]
    if len(valid_ts) >= 2:
        repo_span_days = (max(valid_ts) - min(valid_ts)) / 86400
    else:
        repo_span_days = 0

    if repo_span_days < 90:
        time_grain = "day"
        time_fmt = "%Y-%m-%d"
    elif repo_span_days < 365:
        time_grain = "week"
        time_fmt = None  # handled below
    else:
        time_grain = "month"
        time_fmt = "%Y-%m"

    stats["time_grain"] = time_grain
    print(f"  Time granularity: {time_grain} (repo spans {int(repo_span_days)} days)")

    def time_key(ts):
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        if time_grain == "day":
            return dt.strftime("%Y-%m-%d")
        elif time_grain == "week":
            # ISO week: YYYY-Www
            iso = dt.isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        else:
            return dt.strftime("%Y-%m")

    loc_buckets = {}
    running_loc = 0
    running_added = 0
    running_removed = 0

    for c in sorted_commits:
        if c["timestamp"] <= 0:
            continue
        running_added += c["insertions"]
        running_removed += c["deletions"]
        running_loc = running_added - running_removed
        key = time_key(c["timestamp"])
        loc_buckets[key] = running_loc

    sorted_loc = sorted(loc_buckets.items())
    stats["loc_over_time"] = {
        "labels": [x[0] for x in sorted_loc],
        "values": [x[1] for x in sorted_loc],
    }
    stats["total_insertions"] = running_added
    stats["total_deletions"] = running_removed
    stats["net_loc"] = running_loc

    # ── Notable commits (largest changes) ──
    print("  Finding notable commits...")
    notable = sorted(commits, key=lambda c: c["insertions"] + c["deletions"], reverse=True)
    stats["notable_commits"] = []
    for c in notable[:20]:
        if c["insertions"] + c["deletions"] == 0:
            break
        stats["notable_commits"].append({
            "hash": c["hash"],
            "timestamp": c["timestamp"],
            "author": c["author"],
            "subject": c["subject"],
            "insertions": c["insertions"],
            "deletions": c["deletions"],
            "files_changed": c["files_changed"],
        })

    # ── File count over time (accurate: git ls-tree at snapshots) ──
    print(f"  Tracking file count over time ({time_grain} snapshots)...")
    bucket_last_hash = {}
    for c in sorted_commits:
        if c["timestamp"] <= 0:
            continue
        key = time_key(c["timestamp"])
        bucket_last_hash[key] = c["hash"]

    sorted_buckets = sorted(bucket_last_hash.keys())
    # For monthly/weekly, sample down if too many points
    if len(sorted_buckets) > 60:
        step = max(1, len(sorted_buckets) // 50)
        sampled = sorted_buckets[::step]
        if sorted_buckets[-1] not in sampled:
            sampled.append(sorted_buckets[-1])
    else:
        sampled = sorted_buckets

    file_count_timeline = {"labels": [], "values": []}
    for key in sampled:
        h = bucket_last_hash[key]
        out = run_git(repo_path, ["ls-tree", "-r", "--name-only", h], timeout=30)
        count = len([l for l in out.strip().split("\n") if l.strip()]) if out.strip() else 0
        file_count_timeline["labels"].append(key)
        file_count_timeline["values"].append(count)
        print(f"    {key}: {count} files")

    stats["files_over_time"] = file_count_timeline

    # ── Blame-based LOC over time (optional) ──
    stats["blame_loc_over_time"] = None
    stats["blame_filter"] = None
    if blame:
        blame_timeline = collect_blame_over_time(
            repo_path, bucket_last_hash, sampled, blame_filt, blame_cache_dir
        )
        # Collect all authors seen across all snapshots
        all_blame_authors = set()
        for snap in blame_timeline.values():
            all_blame_authors.update(snap.keys())
        # Build per-author series aligned to sampled labels
        blame_labels = sampled
        blame_series = {}
        for author in all_blame_authors:
            blame_series[author] = [blame_timeline[k].get(author, 0) for k in blame_labels]
        stats["blame_loc_over_time"] = {
            "labels": blame_labels,
            "series": blame_series,
        }
        stats["blame_filter"] = {
            "whitelist_exts": sorted(blame_filt["whitelist_exts"]),
            "blacklist_exts": sorted(blame_filt["blacklist_exts"]),
            "path_whitelist": sorted(blame_filt["path_whitelist"]),
            "path_blacklist": sorted(blame_filt["path_blacklist"]),
        }

    # ── Tags ──
    print("  Listing tags...")
    tags_raw = run_git(repo_path, ["tag", "-l", "--sort=-creatordate",
                                    "--format=%(refname:short)|%(creatordate:unix)|%(subject)"])
    tags = []
    for line in tags_raw.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("|", 2)
        if len(parts) >= 1:
            tag = {"name": parts[0]}
            if len(parts) >= 2 and parts[1].isdigit():
                tag["date"] = datetime.fromtimestamp(int(parts[1]), tz=timezone.utc).strftime("%Y-%m-%d")
            else:
                tag["date"] = ""
            tag["subject"] = parts[2] if len(parts) >= 3 else ""
            tags.append(tag)
    stats["tags"] = tags[:50]

    branches_raw = run_git(repo_path, ["branch", "-a", "--no-color"]).strip()
    stats["total_branches"] = len([b for b in branches_raw.split("\n") if b.strip()]) if branches_raw else 0

    stats["recent_commits"] = sorted_commits[-15:][::-1] if sorted_commits else []

    # ── Commits bucketed by time_key (for chart drill-down) ──
    commits_by_bucket = defaultdict(list)
    for c in sorted_commits:
        if c["timestamp"] <= 0:
            continue
        commits_by_bucket[time_key(c["timestamp"])].append({
            "hash": c["hash"],
            "ts": c["timestamp"],
            "author": c["author"],
            "subject": c["subject"],
            "insertions": c["insertions"],
            "deletions": c["deletions"],
            "files_changed": c["files_changed"],
        })
    stats["commits_by_bucket"] = dict(commits_by_bucket)

    stats["generated_at"] = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    print("  Data collection complete.")
    return stats


# ─── HTML Report Generator ──────────────────────────────────────────────────

def generate_html(stats, blame=False):

    def esc(s):
        return html_module.escape(str(s))

    def format_date(ts):
        if not ts or ts == float("inf"):
            return "N/A"
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

    def format_number(n):
        return f"{n:,}"

    first_date = format_date(stats.get("first_commit_ts", 0))
    last_date = format_date(stats.get("last_commit_ts", 0))

    if stats.get("first_commit_ts") and stats.get("last_commit_ts"):
        age_days = (stats["last_commit_ts"] - stats["first_commit_ts"]) / 86400
    else:
        age_days = 0

    active_days = stats.get("active_days", 0)
    active_pct = (active_days / age_days * 100) if age_days > 0 else 0
    avg_commits_per_active_day = stats["total_commits"] / active_days if active_days > 0 else 0

    # Author table rows
    author_rows = ""
    for i, a in enumerate(stats["authors"][:50]):
        pct = (a["commits"] / stats["total_commits"] * 100) if stats["total_commits"] else 0
        author_rows += f"""
        <tr>
            <td>{i+1}</td>
            <td class="author-name"><a href="#" class="author-link" data-author="{esc(a['name'])}">{esc(a['name'])}</a></td>
            <td class="num">{format_number(a['commits'])}</td>
            <td class="num">{pct:.1f}%</td>
            <td class="num add">+{format_number(a['insertions'])}</td>
            <td class="num del">-{format_number(a['deletions'])}</td>
            <td>{format_date(a['first_commit'])}</td>
            <td>{format_date(a['last_commit'])}</td>
            <td class="num">{a['active_days']}</td>
        </tr>"""

    ext_rows = ""
    for ext, count in sorted(stats["file_extensions"].items(), key=lambda x: -x[1]):
        loc = stats["lines_by_extension"].get(ext, 0)
        ext_rows += f"""
        <tr>
            <td><code>{esc(ext)}</code></td>
            <td class="num">{format_number(count)}</td>
            <td class="num">{format_number(loc)}</td>
        </tr>"""

    tag_rows = ""
    for t in stats.get("tags", []):
        tag_rows += f"""
        <tr>
            <td><code>{esc(t['name'])}</code></td>
            <td>{esc(t.get('date', ''))}</td>
            <td>{esc(t.get('subject', '')[:80])}</td>
        </tr>"""

    recent_rows = ""
    for c in stats.get("recent_commits", []):
        recent_rows += f"""
        <tr>
            <td><code>{esc(c['hash'][:8])}</code></td>
            <td>{format_date(c['timestamp'])}</td>
            <td>{esc(c['author'])}</td>
            <td class="commit-msg">{esc(c['subject'][:90])}</td>
        </tr>"""

    notable_rows = ""
    for c in stats.get("notable_commits", []):
        notable_rows += f"""
        <tr>
            <td><code>{esc(c['hash'][:8])}</code></td>
            <td>{format_date(c['timestamp'])}</td>
            <td>{esc(c['author'])}</td>
            <td class="num add">+{format_number(c['insertions'])}</td>
            <td class="num del">-{format_number(c['deletions'])}</td>
            <td class="num">{format_number(c['files_changed'])}</td>
            <td class="commit-msg">{esc(c['subject'][:70])}</td>
        </tr>"""

    top_authors_detail = []
    for a in stats["authors"][:20]:
        top_authors_detail.append({
            "name": a["name"],
            "commits": a["commits"],
            "insertions": a["insertions"],
            "deletions": a["deletions"],
            "mc": a["monthly_commits"],
            "mi": a["monthly_insertions"],
            "md": a["monthly_deletions"],
        })

    # ── Blame filter info panel ──
    bf = stats.get("blame_filter")
    if bf:
        def _tags(items, cls="tag"):
            return "".join(f'<span class="{cls}">{esc(x)}</span>' for x in items) or '<span class="none">none</span>'
        ext_heading = "Extension whitelist (only these)" if bf["whitelist_exts"] else "Extension blacklist (excluded)"
        ext_items = bf["whitelist_exts"] or bf["blacklist_exts"]
        blame_filter_html = f"""
    <h2 class="section-title" style="margin-top:2rem;">Actual Lines Present in Project (git blame)</h2>
    <p style="color:var(--text2);font-size:.85rem;margin:-1rem 0 1rem;">
        Lines attributed to each author at each snapshot — accounts for code deleted by others.
        Binary files always excluded. Extension and path rules from config applied.
    </p>
    <details class="blame-filter-details">
        <summary>Blame filter config</summary>
        <div class="blame-filter-body">
            <div class="blame-filter-group">
                <h4>{esc(ext_heading)}</h4>
                <div class="tag-list">{_tags(ext_items)}</div>
            </div>
            <div class="blame-filter-group">
                <h4>Path whitelist <span style="font-weight:400;text-transform:none;letter-spacing:0">(overrides blacklist)</span></h4>
                <div class="tag-list">{_tags(bf["path_whitelist"])}</div>
            </div>
            <div class="blame-filter-group">
                <h4>Path blacklist <span style="font-weight:400;text-transform:none;letter-spacing:0">(always applied)</span></h4>
                <div class="tag-list">{_tags(bf["path_blacklist"], "tag always")}</div>
            </div>
        </div>
    </details>"""
    else:
        blame_filter_html = ""

    author_activity = {}
    for a in stats["authors"][:20]:
        n = a["name"]
        author_activity[n] = {
            "hour": stats["author_hour"].get(n, [0]*24),
            "dow": stats["author_dow"].get(n, [0]*7),
        }

    chart_data = json.dumps({
        "hourOfDay": stats["hour_of_day"],
        "dayOfWeek": stats["day_of_week"],
        "monthOfYear": stats["month_of_year"],
        "yearMonth": stats["year_month"],
        "yearly": stats["yearly"],
        "locOverTime": stats.get("loc_over_time", {"labels": [], "values": []}),
        "filesOverTime": stats.get("files_over_time", {"labels": [], "values": []}),
        "topAuthors": [{"name": a["name"], "commits": a["commits"]} for a in stats["authors"][:15]],
        "heatmap": stats.get("hour_dow_heatmap", []),
        "allMonths": stats.get("all_months", []),
        "authorDetails": top_authors_detail,
        "authorActivity": author_activity,
        "authorCommits": stats.get("author_commits", {}),
        "githubBase": stats.get("github_base", ""),
        "blameLocOverTime": stats.get("blame_loc_over_time"),
        "commitsByBucket": stats.get("commits_by_bucket", {}),
    })

    report_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GitStats — {esc(stats['repo_name'])}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=DM+Sans:ital,wght@0,400;0,500;0,700;1,400&display=swap');

:root {{
    --bg: #0e1117;
    --surface: #161b22;
    --surface2: #1c2333;
    --border: #30363d;
    --text: #e6edf3;
    --text2: #8b949e;
    --accent: #58a6ff;
    --accent2: #3fb950;
    --accent3: #d2a8ff;
    --accent4: #f78166;
    --accent5: #ffa657;
    --danger: #f85149;
    --radius: 10px;
    --mono: 'JetBrains Mono', monospace;
    --sans: 'DM Sans', -apple-system, sans-serif;
}}

* {{ margin: 0; padding: 0; box-sizing: border-box; }}

body {{
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    line-height: 1.6;
    -webkit-font-smoothing: antialiased;
}}

.shell {{
    max-width: 1340px;
    margin: 0 auto;
    padding: 2rem 1.5rem 4rem;
}}

header {{
    text-align: center;
    padding: 3rem 0 2.5rem;
    border-bottom: 1px solid var(--border);
    margin-bottom: 2.5rem;
}}
header h1 {{
    font-family: var(--mono);
    font-size: 2rem;
    font-weight: 600;
    letter-spacing: -0.02em;
    color: var(--accent);
}}
header h1 span {{ color: var(--text2); font-weight: 400; }}
header p {{
    color: var(--text2);
    margin-top: .5rem;
    font-size: .9rem;
    word-break: break-all;
}}

nav {{
    display: flex;
    gap: .5rem;
    justify-content: center;
    margin-bottom: 2.5rem;
    flex-wrap: wrap;
}}
nav a {{
    font-family: var(--mono);
    font-size: .8rem;
    padding: .55rem 1.1rem;
    border-radius: 6px;
    background: var(--surface);
    color: var(--text2);
    text-decoration: none;
    border: 1px solid var(--border);
    transition: all .15s;
    cursor: pointer;
    user-select: none;
}}
nav a:hover, nav a.active {{
    background: var(--accent);
    color: var(--bg);
    border-color: var(--accent);
}}

.section {{ display: none; }}
.section.active {{ display: block; }}
.section-title {{
    font-family: var(--mono);
    font-size: 1.15rem;
    font-weight: 600;
    margin-bottom: 1.5rem;
    padding-bottom: .75rem;
    border-bottom: 1px solid var(--border);
    color: var(--accent3);
}}

.stat-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 1rem;
    margin-bottom: 2rem;
}}
.stat-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1.25rem;
}}
.stat-card .label {{
    font-size: .75rem;
    text-transform: uppercase;
    letter-spacing: .08em;
    color: var(--text2);
    margin-bottom: .35rem;
    font-family: var(--mono);
}}
.stat-card .value {{
    font-size: 1.6rem;
    font-weight: 700;
    font-family: var(--mono);
    color: var(--text);
}}
.stat-card .sub {{
    font-size: .78rem;
    color: var(--text2);
    margin-top: .25rem;
}}

.info-table {{
    width: 100%;
    border-collapse: collapse;
    margin-bottom: 2rem;
    font-size: .88rem;
}}
.info-table td {{
    padding: .6rem .8rem;
    border-bottom: 1px solid var(--border);
}}
.info-table td:first-child {{
    color: var(--text2);
    font-family: var(--mono);
    font-size: .8rem;
    width: 200px;
    white-space: nowrap;
}}

.data-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: .82rem;
    margin-bottom: 2rem;
}}
.data-table thead th {{
    font-family: var(--mono);
    font-size: .72rem;
    text-transform: uppercase;
    letter-spacing: .06em;
    color: var(--text2);
    padding: .6rem .7rem;
    text-align: left;
    border-bottom: 2px solid var(--border);
    position: sticky;
    top: 0;
    background: var(--bg);
}}
.data-table thead th.num {{ text-align: right; }}
.data-table tbody td {{
    padding: .55rem .7rem;
    border-bottom: 1px solid var(--border);
    color: var(--text);
}}
.data-table tbody td.num {{ text-align: right; font-family: var(--mono); font-size: .8rem; }}
.data-table tbody td.add {{ color: var(--accent2); }}
.data-table tbody td.del {{ color: var(--danger); }}
.data-table tbody td.author-name {{ font-weight: 500; }}
.author-link {{ color: var(--accent); text-decoration: none; }}
.author-link:hover {{ text-decoration: underline; }}
#author-modal {{ display:none; position:fixed; inset:0; z-index:1000; background:rgba(0,0,0,.7); align-items:center; justify-content:center; }}
#author-modal.open {{ display:flex; }}
#author-modal-box {{ background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); width:min(780px,95vw); max-height:80vh; display:flex; flex-direction:column; }}
#author-modal-header {{ padding:1rem 1.25rem; border-bottom:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; }}
#author-modal-title {{ font-weight:600; font-size:1rem; }}
#author-modal-close {{ background:none; border:none; color:var(--text2); cursor:pointer; font-size:1.2rem; line-height:1; }}
#author-modal-close:hover {{ color:var(--text); }}
#author-modal-body {{ overflow-y:auto; padding:.75rem 1.25rem 1.25rem; }}
.modal-commit-row {{ display:grid; grid-template-columns:7ch 14ch 1fr; gap:.5rem; padding:.4rem 0; border-bottom:1px solid var(--border); font-size:.85rem; align-items:baseline; }}
.modal-commit-row:last-child {{ border-bottom:none; }}
.modal-commit-hash {{ font-family:var(--mono); color:var(--accent); }}
.modal-commit-hash a {{ color:inherit; text-decoration:none; }}
.modal-commit-hash a:hover {{ text-decoration:underline; }}
.modal-commit-date {{ color:var(--text2); }}
.modal-commit-msg {{ word-break:break-word; }}
.data-table tbody td.commit-msg {{
    max-width: 400px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}}
.data-table tbody tr:hover {{ background: var(--surface); }}

code {{
    font-family: var(--mono);
    font-size: .82rem;
    color: var(--accent5);
}}

.chart-row {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1.25rem;
    margin-bottom: 1.5rem;
}}
.chart-box {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1.25rem;
}}
.chart-box.full {{ grid-column: 1 / -1; }}
.chart-box h3 {{
    font-family: var(--mono);
    font-size: .78rem;
    color: var(--text2);
    text-transform: uppercase;
    letter-spacing: .06em;
    margin-bottom: 1rem;
}}
.chart-box canvas {{ width: 100% !important; }}

.heatmap-wrap {{ overflow-x: auto; margin-bottom: 2rem; }}
.heatmap {{
    display: grid;
    grid-template-columns: 60px repeat(24, 1fr);
    gap: 2px;
    min-width: 600px;
}}
.heatmap .label {{
    font-family: var(--mono);
    font-size: .7rem;
    color: var(--text2);
    display: flex;
    align-items: center;
    padding-right: .5rem;
}}
.heatmap .cell {{
    aspect-ratio: 1;
    border-radius: 3px;
    min-height: 18px;
    transition: transform .1s;
    cursor: default;
}}
.heatmap .cell:hover {{ transform: scale(1.3); z-index: 2; }}
.heatmap .hour-label {{
    font-family: var(--mono);
    font-size: .65rem;
    color: var(--text2);
    text-align: center;
}}

.table-wrap {{ overflow-x: auto; }}

/* ── Contribution cards ── */
.contrib-controls {{
    display: flex;
    flex-wrap: wrap;
    gap: .75rem;
    align-items: center;
    margin-bottom: 1.5rem;
}}
.contrib-controls label {{
    font-family: var(--mono);
    font-size: .75rem;
    color: var(--text2);
}}
.contrib-controls select,
.contrib-controls input[type="range"] {{
    font-family: var(--mono);
    font-size: .78rem;
    background: var(--surface);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 5px;
    padding: .35rem .6rem;
    outline: none;
}}
.contrib-controls select:focus {{ border-color: var(--accent); }}
.btn-group {{
    display: inline-flex;
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
}}
.btn-group button {{
    font-family: var(--mono);
    font-size: .72rem;
    padding: .4rem .8rem;
    background: var(--surface);
    color: var(--text2);
    border: none;
    border-right: 1px solid var(--border);
    cursor: pointer;
    transition: all .12s;
}}
.btn-group button:last-child {{ border-right: none; }}
.btn-group button.active {{
    background: var(--accent);
    color: var(--bg);
}}
.btn-group button:hover:not(.active) {{
    background: var(--surface2);
    color: var(--text);
}}

.contrib-cards {{
    display: grid;
    gap: 1rem;
    margin-bottom: 2rem;
}}
.contrib-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1rem 1.25rem;
}}
.contrib-card .card-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: .75rem;
    flex-wrap: wrap;
    gap: .5rem;
}}
.contrib-card .card-header .author-info {{
    display: flex;
    align-items: baseline;
    gap: .75rem;
}}
.contrib-card .card-header .rank {{
    font-family: var(--mono);
    font-size: .7rem;
    color: var(--text2);
    min-width: 24px;
}}
.contrib-card .card-header .name {{
    font-weight: 600;
    font-size: .95rem;
}}
.contrib-card .card-header .stat-pills {{
    display: flex;
    gap: .5rem;
    font-family: var(--mono);
    font-size: .72rem;
}}
.contrib-card .card-header .pill {{
    padding: .15rem .5rem;
    border-radius: 4px;
    background: var(--surface2);
    color: var(--text2);
}}
.contrib-card .card-header .pill.commits {{ color: var(--accent); }}
.contrib-card .card-header .pill.add {{ color: var(--accent2); }}
.contrib-card .card-header .pill.del {{ color: var(--danger); }}
.contrib-card canvas {{ width: 100% !important; }}

.cmp-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1.25rem;
    display: flex;
    flex-direction: column;
    gap: .6rem;
}}
.cmp-card .label {{
    font-size: .75rem;
    text-transform: uppercase;
    letter-spacing: .08em;
    color: var(--text2);
    font-family: var(--mono);
}}
.cmp-row {{
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: .5rem;
}}
.cmp-row .author-tag {{
    font-family: var(--mono);
    font-size: .72rem;
    padding: .1rem .45rem;
    border-radius: 4px;
}}
.cmp-row .val {{
    font-family: var(--mono);
    font-size: 1.1rem;
    font-weight: 700;
}}
.cmp-bar-wrap {{
    height: 4px;
    background: var(--surface2);
    border-radius: 2px;
    overflow: hidden;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 2px;
}}
.cmp-bar-a {{ height: 100%; border-radius: 2px 0 0 2px; }}
.cmp-bar-b {{ height: 100%; border-radius: 0 2px 2px 0; }}

.blame-filter-details {{
    margin-bottom: 1.5rem;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
}}
.blame-filter-details summary {{
    padding: .6rem 1rem;
    font-family: var(--mono);
    font-size: .78rem;
    color: var(--text2);
    cursor: pointer;
    user-select: none;
    list-style: none;
    display: flex;
    align-items: center;
    gap: .5rem;
}}
.blame-filter-details summary::before {{
    content: '▶';
    font-size: .6rem;
    transition: transform .15s;
}}
.blame-filter-details[open] summary::before {{ transform: rotate(90deg); }}
.blame-filter-details summary:hover {{ color: var(--text); }}
.blame-filter-body {{
    padding: .75rem 1rem 1rem;
    border-top: 1px solid var(--border);
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 1rem;
}}
.blame-filter-group h4 {{
    font-family: var(--mono);
    font-size: .7rem;
    text-transform: uppercase;
    letter-spacing: .06em;
    color: var(--text2);
    margin-bottom: .4rem;
}}
.blame-filter-group .tag-list {{
    display: flex;
    flex-wrap: wrap;
    gap: .3rem;
}}
.blame-filter-group .tag {{
    font-family: var(--mono);
    font-size: .72rem;
    padding: .15rem .45rem;
    border-radius: 4px;
    background: var(--surface2);
    color: var(--text2);
    border: 1px solid var(--border);
}}
.blame-filter-group .tag.always {{ border-color: var(--accent4); color: var(--accent4); }}
.blame-filter-group .none {{ color: var(--text2); font-size: .78rem; font-style: italic; }}

.bucket-panel {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    margin-top: 1rem;
    overflow: hidden;
}}
.bucket-panel-header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: .75rem 1rem;
    border-bottom: 1px solid var(--border);
    font-family: var(--mono);
    font-size: .8rem;
}}
.bucket-panel-header .bucket-label {{
    color: var(--accent);
    font-weight: 600;
}}
.bucket-panel-header .bucket-meta {{
    color: var(--text2);
    font-size: .75rem;
}}
.bucket-panel-header .freeze-btn {{
    font-family: var(--mono);
    font-size: .72rem;
    padding: .25rem .65rem;
    border-radius: 4px;
    border: 1px solid var(--border);
    background: var(--surface2);
    color: var(--text2);
    cursor: pointer;
    transition: all .12s;
}}
.bucket-panel-header .freeze-btn.frozen {{
    border-color: var(--accent5);
    color: var(--accent5);
    background: var(--surface);
}}
.bucket-panel-header .freeze-btn:hover {{ color: var(--text); }}
.bucket-table-wrap {{ overflow-x: auto; max-height: 340px; overflow-y: auto; }}
.bucket-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: .8rem;
}}
.bucket-table thead th {{
    font-family: var(--mono);
    font-size: .7rem;
    text-transform: uppercase;
    letter-spacing: .06em;
    color: var(--text2);
    padding: .5rem .7rem;
    text-align: left;
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
    background: var(--surface);
    cursor: pointer;
    user-select: none;
    white-space: nowrap;
}}
.bucket-table thead th:hover {{ color: var(--text); }}
.bucket-table thead th.sort-asc::after {{ content: ' ↑'; color: var(--accent); }}
.bucket-table thead th.sort-desc::after {{ content: ' ↓'; color: var(--accent); }}
.bucket-table thead th.num {{ text-align: right; }}
.bucket-table tbody td {{
    padding: .45rem .7rem;
    border-bottom: 1px solid var(--border);
    color: var(--text);
}}
.bucket-table tbody td.num {{ text-align: right; font-family: var(--mono); font-size: .78rem; }}
.bucket-table tbody td.add {{ color: var(--accent2); }}
.bucket-table tbody td.del {{ color: var(--danger); }}
.bucket-table tbody tr:hover {{ background: var(--surface2); }}

footer {{
    text-align: center;
    padding: 2rem 0;
    border-top: 1px solid var(--border);
    margin-top: 3rem;
    color: var(--text2);
    font-size: .78rem;
}}

@media (max-width: 768px) {{
    .chart-row {{ grid-template-columns: 1fr; }}
    .stat-grid {{ grid-template-columns: repeat(2, 1fr); }}
    header h1 {{ font-size: 1.4rem; }}
    .contrib-controls {{ flex-direction: column; align-items: flex-start; }}
}}
</style>
</head>
<body>
<div class="shell">

<header>
    <h1>{esc(stats['repo_name'])} <span>— gitstats</span></h1>
    <p>Report generated {esc(stats['generated_at'])} &nbsp;·&nbsp; {esc(stats['remote_url'])}</p>
</header>

<nav>
    <a class="active" data-sec="general">General</a>
    <a data-sec="activity">Activity</a>
    <a data-sec="authors">Authors</a>
    <a data-sec="contributions">Contributions</a>
    <a data-sec="compare">Compare</a>
    <a data-sec="files">Files</a>
    <a data-sec="lines">Lines</a>
    <a data-sec="tags">Tags</a>
</nav>

<!-- ═══════ GENERAL ═══════ -->
<div id="sec-general" class="section active">
    <h2 class="section-title">General</h2>
    <div class="stat-grid">
        <div class="stat-card">
            <div class="label">Total Commits</div>
            <div class="value">{format_number(stats['total_commits_head']) if stats.get('total_commits_head') else format_number(stats['total_commits'])}</div>
            <div class="sub">{format_number(stats['total_commits'])} excl. merges · {format_number(stats['total_commits_all'])} all branches</div>
        </div>
        <div class="stat-card">
            <div class="label">Authors</div>
            <div class="value">{format_number(stats['total_authors'])}</div>
            <div class="sub">avg {avg_commits_per_active_day:.1f} commits / active day</div>
        </div>
        <div class="stat-card">
            <div class="label">Files</div>
            <div class="value">{format_number(stats['total_files'])}</div>
            <div class="sub">{format_number(len(stats['file_extensions']))} extension types</div>
        </div>
        <div class="stat-card">
            <div class="label">Lines of Code</div>
            <div class="value">{format_number(stats['total_lines'])}</div>
            <div class="sub">current count from working tree</div>
        </div>
        <div class="stat-card">
            <div class="label">Project Age</div>
            <div class="value">{int(age_days):,}d</div>
            <div class="sub">{active_days} active days ({active_pct:.1f}%)</div>
        </div>
        <div class="stat-card">
            <div class="label">Branches</div>
            <div class="value">{stats['total_branches']}</div>
            <div class="sub">{len(stats.get('tags', []))} tags</div>
        </div>
    </div>
    <table class="info-table">
        <tr><td>Repository</td><td>{esc(stats['repo_path'])}</td></tr>
        <tr><td>First Commit</td><td>{first_date}</td></tr>
        <tr><td>Last Commit</td><td>{last_date}</td></tr>
    </table>
    <h2 class="section-title">Recent Commits</h2>
    <div class="table-wrap">
    <table class="data-table">
        <thead><tr><th>Hash</th><th>Date</th><th>Author</th><th>Message</th></tr></thead>
        <tbody>{recent_rows}</tbody>
    </table>
    </div>
</div>

<!-- ═══════ ACTIVITY ═══════ -->
<div id="sec-activity" class="section">
    <h2 class="section-title">Activity</h2>
    <div class="chart-row">
        <div class="chart-box"><h3>Commits by Hour of Day</h3><canvas id="chart-hour"></canvas></div>
        <div class="chart-box"><h3>Commits by Day of Week</h3><canvas id="chart-dow"></canvas></div>
    </div>
    <div class="chart-row">
        <div class="chart-box"><h3>Commits by Month of Year</h3><canvas id="chart-month"></canvas></div>
        <div class="chart-box"><h3>Commits by Year</h3><canvas id="chart-year"></canvas></div>
    </div>
    <div class="chart-row">
        <div class="chart-box full"><h3>Commits over Time (Monthly)</h3><canvas id="chart-ym"></canvas></div>
    </div>
    <h2 class="section-title">Hour of Week Heatmap</h2>
    <div class="heatmap-wrap">
        <div class="heatmap" id="heatmap">
            <div class="label"></div>
            {"".join(f'<div class="hour-label">{h}</div>' for h in range(24))}
        </div>
    </div>
    <h2 class="section-title">Activity by Author</h2>
    <div class="contrib-controls">
        <label>Author:</label>
        <select id="author-activity-select">
            {"".join(f'<option value="{esc(a["name"])}">{esc(a["name"])}</option>' for a in stats["authors"][:20])}
        </select>
    </div>
    <div class="chart-row">
        <div class="chart-box"><h3 id="author-hour-title">Commits by Hour of Day</h3><canvas id="chart-author-hour"></canvas></div>
        <div class="chart-box"><h3 id="author-dow-title">Commits by Day of Week</h3><canvas id="chart-author-dow"></canvas></div>
    </div>
</div>

<!-- ═══════ AUTHORS ═══════ -->
<div id="sec-authors" class="section">
    <h2 class="section-title">Authors ({stats['total_authors']})</h2>
    <div class="chart-row">
        <div class="chart-box full"><h3>Top Authors by Commits</h3><canvas id="chart-authors"></canvas></div>
    </div>
    <div class="table-wrap">
    <table class="data-table">
        <thead><tr>
            <th>#</th><th>Author</th><th class="num">Commits</th><th class="num">%</th>
            <th class="num">Lines +</th><th class="num">Lines −</th>
            <th>First Commit</th><th>Last Commit</th><th class="num">Active Days</th>
        </tr></thead>
        <tbody>{author_rows}</tbody>
    </table>
    </div>
</div>

<!-- ═══════ CONTRIBUTIONS ═══════ -->
<div id="sec-contributions" class="section">
    <h2 class="section-title">Contributions by Author</h2>
    <div class="contrib-controls">
        <label>Metric:</label>
        <div class="btn-group" id="contrib-metric">
            <button class="active" data-metric="commits">Commits</button>
            <button data-metric="additions">Additions</button>
            <button data-metric="deletions">Deletions</button>
        </div>
        <label>Sort by:</label>
        <div class="btn-group" id="contrib-sort">
            <button class="active" data-sort="total">Total</button>
            <button data-sort="recent">Recent</button>
        </div>
        <label>Time range:</label>
        <select id="contrib-range">
            <option value="all" selected>All time</option>
            <option value="12">Last 12 months</option>
            <option value="6">Last 6 months</option>
            <option value="3">Last 3 months</option>
        </select>
    </div>
    <div id="contrib-cards" class="contrib-cards"></div>
</div>

<!-- ═══════ COMPARE ═══════ -->
<div id="sec-compare" class="section">
    <h2 class="section-title">Compare Contributors</h2>
    <div class="contrib-controls" style="margin-bottom:2rem;">
        <label>Author A:</label>
        <select id="cmp-a">
            {"".join(f'<option value="{esc(a["name"])}">{esc(a["name"])}</option>' for a in stats["authors"][:50])}
        </select>
        <label style="margin-left:1rem;">Author B:</label>
        <select id="cmp-b">
            {"".join(f'<option value="{esc(a["name"])}" {"selected" if i==1 else ""}>{esc(a["name"])}</option>' for i, a in enumerate(stats["authors"][:50]))}
        </select>
        <label style="margin-left:1rem;">Time range:</label>
        <select id="cmp-range">
            <option value="all" selected>All time</option>
            <option value="12">Last 12 months</option>
            <option value="6">Last 6 months</option>
            <option value="3">Last 3 months</option>
        </select>
    </div>
    <div id="cmp-stat-cards" class="stat-grid" style="margin-bottom:2rem;"></div>
    <div class="chart-row">
        <div class="chart-box full"><h3 id="cmp-monthly-title">Commits &amp; ±LOC per Month</h3><canvas id="chart-cmp-monthly"></canvas></div>
    </div>
    <div class="chart-row">
        <div class="chart-box"><h3>Hour of Day</h3><canvas id="chart-cmp-hour"></canvas></div>
        <div class="chart-box"><h3>Day of Week</h3><canvas id="chart-cmp-dow"></canvas></div>
    </div>
    <div class="chart-row">
        <div class="chart-box full"><h3>Cumulative Net LOC over Time (insertions − deletions, approximation)</h3><canvas id="chart-cmp-netloc"></canvas></div>
    </div>
    <div class="chart-row">
        <div class="chart-box full"><h3>Cumulative Additions over Time</h3><canvas id="chart-cmp-additions"></canvas></div>
    </div>
    {blame_filter_html}
    {"" if not blame else '''
    <div class="chart-row">
        <div class="chart-box full">
            <h3>Lines in Project — Selected Authors (blame) <span id="cmp-blame-hint" style="font-weight:400;color:var(--text2);font-size:.7rem;margin-left:.5rem;">click a point to see commits</span></h3>
            <canvas id="chart-cmp-blame"></canvas>
            <div id="blame-commit-panel" style="display:none;" class="bucket-panel">
                <div class="bucket-panel-header">
                    <span class="bucket-label" id="bcp-label"></span>
                    <span class="bucket-meta" id="bcp-meta"></span>
                    <button class="freeze-btn" id="bcp-freeze">freeze</button>
                </div>
                <div class="bucket-table-wrap">
                    <table class="bucket-table" id="bcp-table">
                        <thead><tr>
                            <th data-col="hash">Hash</th>
                            <th data-col="ts">Date</th>
                            <th data-col="author">Author</th>
                            <th data-col="ins" class="num">+Lines</th>
                            <th data-col="del" class="num">-Lines</th>
                            <th data-col="files" class="num">Files</th>
                            <th data-col="subject">Message</th>
                        </tr></thead>
                        <tbody id="bcp-body"></tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>
    <div class="chart-row">
        <div class="chart-box full"><h3>All Authors — Lines in Project at Latest Snapshot (blame)</h3><canvas id="chart-cmp-blame-bar"></canvas></div>
    </div>
    '''}
</div>

<!-- ═══════ FILES ═══════ -->
<div id="sec-files" class="section">
    <h2 class="section-title">Files ({format_number(stats['total_files'])})</h2>
    <div class="chart-row">
        <div class="chart-box full"><h3>File Count over Time</h3><canvas id="chart-files-time"></canvas></div>
    </div>
    <h2 class="section-title">Extensions</h2>
    <div class="table-wrap">
    <table class="data-table">
        <thead><tr><th>Extension</th><th class="num">Files</th><th class="num">Lines</th></tr></thead>
        <tbody>{ext_rows}</tbody>
    </table>
    </div>
</div>

<!-- ═══════ LINES ═══════ -->
<div id="sec-lines" class="section">
    <h2 class="section-title">Lines of Code</h2>
    <div class="stat-grid">
        <div class="stat-card">
            <div class="label">Current Total (working tree)</div>
            <div class="value">{format_number(stats['total_lines'])}</div>
            <div class="sub">actual lines in tracked files right now</div>
        </div>
        <div class="stat-card">
            <div class="label">Net from Diffs (added − removed)</div>
            <div class="value">{format_number(stats['net_loc'])}</div>
            <div class="sub">cumulative insertions minus deletions</div>
        </div>
        <div class="stat-card">
            <div class="label">Total Added</div>
            <div class="value" style="color:var(--accent2)">+{format_number(stats['total_insertions'])}</div>
            <div class="sub">across all commits</div>
        </div>
        <div class="stat-card">
            <div class="label">Total Removed</div>
            <div class="value" style="color:var(--danger)">−{format_number(stats['total_deletions'])}</div>
            <div class="sub">across all commits</div>
        </div>
    </div>
    <div class="chart-row">
        <div class="chart-box full">
            <h3>Net Lines of Code over Time (cumulative insertions − deletions)</h3>
            <canvas id="chart-loc"></canvas>
        </div>
    </div>
    <h2 class="section-title">Notable Commits (Largest Changes)</h2>
    <p style="color:var(--text2);font-size:.85rem;margin:-1rem 0 1rem;">
        These commits had the biggest impact on lines of code — check these to understand sudden spikes or drops in the graph above.
    </p>
    <div class="table-wrap">
    <table class="data-table">
        <thead><tr>
            <th>Hash</th><th>Date</th><th>Author</th>
            <th class="num">Lines +</th><th class="num">Lines −</th>
            <th class="num">Files</th><th>Message</th>
        </tr></thead>
        <tbody>{notable_rows}</tbody>
    </table>
    </div>
</div>

<!-- ═══════ TAGS ═══════ -->
<div id="sec-tags" class="section">
    <h2 class="section-title">Tags ({len(stats.get('tags', []))})</h2>
    <div class="table-wrap">
    <table class="data-table">
        <thead><tr><th>Tag</th><th>Date</th><th>Subject</th></tr></thead>
        <tbody>{tag_rows if tag_rows else '<tr><td colspan="3" style="color:var(--text2)">No tags found</td></tr>'}</tbody>
    </table>
    </div>
</div>

<footer>Generated by gitstats.py — a modern git statistics generator</footer>

</div>

<script>
const D = {chart_data};

// ── Nav ──
const _init = {{}};
document.querySelectorAll('nav a').forEach(a => {{
    a.addEventListener('click', () => {{
        document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
        document.querySelectorAll('nav a').forEach(b => b.classList.remove('active'));
        document.getElementById('sec-' + a.dataset.sec).classList.add('active');
        a.classList.add('active');
        const sec = a.dataset.sec;
        if (!_init[sec]) {{ _init[sec] = true; sectionInits[sec] && sectionInits[sec](); }}
    }});
}});

// ── Chart.js defaults ──
const C = {{
    blue:'#58a6ff', green:'#3fb950', purple:'#d2a8ff',
    orange:'#f78166', yellow:'#ffa657', cyan:'#56d4dd',
    pink:'#f778ba', red:'#f85149'
}};
const CL = Object.values(C);

Chart.defaults.color = '#8b949e';
Chart.defaults.borderColor = '#30363d';
Chart.defaults.font.family = "'JetBrains Mono', monospace";
Chart.defaults.font.size = 11;
Chart.defaults.plugins.legend.display = false;
Chart.defaults.animation.duration = 600;
Chart.defaults.plugins.tooltip.enabled = true;
Chart.defaults.plugins.tooltip.backgroundColor = '#1c2333ee';
Chart.defaults.plugins.tooltip.titleColor = '#e6edf3';
Chart.defaults.plugins.tooltip.bodyColor = '#e6edf3';
Chart.defaults.plugins.tooltip.borderColor = '#30363d';
Chart.defaults.plugins.tooltip.borderWidth = 1;
Chart.defaults.plugins.tooltip.cornerRadius = 6;
Chart.defaults.plugins.tooltip.padding = 10;
Chart.defaults.plugins.tooltip.titleFont = {{ family: "'JetBrains Mono', monospace", size: 11 }};
Chart.defaults.plugins.tooltip.bodyFont = {{ family: "'JetBrains Mono', monospace", size: 12 }};

function bO() {{
    return {{
        responsive: true, maintainAspectRatio: true, aspectRatio: 2,
        interaction: {{ mode: 'index', intersect: false }},
        plugins: {{ tooltip: {{ callbacks: {{
            label: c => (c.dataset.label || '') + (c.dataset.label ? ': ' : '') + c.parsed.y.toLocaleString()
        }} }} }},
        scales: {{
            x: {{ grid: {{ display: false }} }},
            y: {{ grid: {{ color: '#30363d22' }}, ticks: {{ callback: v => v.toLocaleString() }} }}
        }}
    }};
}}
function lO() {{
    return {{
        responsive: true, maintainAspectRatio: true, aspectRatio: 2.5,
        interaction: {{ mode: 'index', intersect: false }},
        plugins: {{ tooltip: {{ callbacks: {{
            label: c => {{
                const l = c.dataset.label || '';
                return (l ? l + ': ' : '') + c.parsed.y.toLocaleString();
            }}
        }} }} }},
        elements: {{ point: {{ radius: 0, hoverRadius: 5, hoverBackgroundColor: '#fff' }}, line: {{ tension: 0.3, borderWidth: 2 }} }},
        scales: {{
            x: {{ grid: {{ display: false }}, ticks: {{ maxTicksLimit: 20 }} }},
            y: {{ grid: {{ color: '#30363d22' }}, beginAtZero: true, ticks: {{ callback: v => v.toLocaleString() }} }}
        }}
    }};
}}

let aHC = null, aDC = null;

function updateAuthorActivity(name) {{
    const d = D.authorActivity[name];
    if (!d) return;
    const dow = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
    if (aHC) aHC.destroy();
    if (aDC) aDC.destroy();
    document.getElementById('author-hour-title').textContent = name + ' — Hour of Day';
    document.getElementById('author-dow-title').textContent = name + ' — Day of Week';
    aHC = new Chart('chart-author-hour', {{
        type: 'bar',
        data: {{ labels: Array.from({{length:24}}, (_,i) => i+'h'), datasets: [{{ data: d.hour, backgroundColor: C.purple+'99', borderRadius: 3 }}] }},
        options: bO()
    }});
    aDC = new Chart('chart-author-dow', {{
        type: 'bar',
        data: {{ labels: dow, datasets: [{{ data: d.dow, backgroundColor: C.orange+'99', borderRadius: 3 }}] }},
        options: bO()
    }});
}}

const sectionInits = {{
    activity: function() {{
        const dow = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
        const mon = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
        new Chart('chart-hour', {{ type:'bar', data:{{ labels:Array.from({{length:24}},(_,i)=>i+'h'), datasets:[{{ data:D.hourOfDay, backgroundColor:C.blue+'99', borderRadius:3 }}] }}, options:bO() }});
        new Chart('chart-dow', {{ type:'bar', data:{{ labels:dow, datasets:[{{ data:D.dayOfWeek, backgroundColor:C.green+'99', borderRadius:3 }}] }}, options:bO() }});
        new Chart('chart-month', {{ type:'bar', data:{{ labels:mon, datasets:[{{ data:D.monthOfYear, backgroundColor:C.purple+'99', borderRadius:3 }}] }}, options:bO() }});
        new Chart('chart-year', {{ type:'bar', data:{{ labels:D.yearly.labels, datasets:[{{ data:D.yearly.values, backgroundColor:C.orange+'99', borderRadius:3 }}] }}, options:bO() }});
        new Chart('chart-ym', {{ type:'line', data:{{ labels:D.yearMonth.labels, datasets:[{{ label:'Commits', data:D.yearMonth.values, borderColor:C.blue, backgroundColor:C.blue+'18', fill:true }}] }}, options:lO() }});
        // Heatmap
        const hm = document.getElementById('heatmap');
        const mx = Math.max(...D.heatmap.map(h=>h.count), 1);
        for (let d=0; d<7; d++) {{
            const lb = document.createElement('div'); lb.className='label'; lb.textContent=dow[d]; hm.appendChild(lb);
            for (let h=0; h<24; h++) {{
                const e = D.heatmap.find(x=>x.dow===d&&x.hour===h);
                const cnt = e?e.count:0;
                const cl = document.createElement('div'); cl.className='cell';
                cl.title = dow[d]+' '+h+':00 — '+cnt+' commits';
                cl.style.background = cnt===0 ? '#21262d' : 'rgba(88,166,255,'+(0.2+cnt/mx*0.8)+')';
                hm.appendChild(cl);
            }}
        }}
        const sel = document.getElementById('author-activity-select');
        if (sel && sel.options.length) {{
            updateAuthorActivity(sel.value);
            sel.addEventListener('change', () => updateAuthorActivity(sel.value));
        }}
    }},
    authors: function() {{
        const aN = D.topAuthors.map(a => a.name.length>20 ? a.name.slice(0,18)+'…' : a.name);
        new Chart('chart-authors', {{ type:'bar', data:{{ labels:aN, datasets:[{{ data:D.topAuthors.map(a=>a.commits), backgroundColor:D.topAuthors.map((_,i)=>CL[i%CL.length]+'cc'), borderRadius:3 }}] }}, options:bO() }});
    }},
    files: function() {{
        new Chart('chart-files-time', {{ type:'line', data:{{ labels:D.filesOverTime.labels, datasets:[{{ label:'Files', data:D.filesOverTime.values, borderColor:C.cyan, backgroundColor:C.cyan+'18', fill:true }}] }}, options:lO() }});
    }},
    lines: function() {{
        new Chart('chart-loc', {{ type:'line', data:{{ labels:D.locOverTime.labels, datasets:[{{ label:'Net LOC', data:D.locOverTime.values, borderColor:C.green, backgroundColor:C.green+'18', fill:true }}] }}, options:lO() }});
    }},
    contributions: function() {{ initContributions(); }},
    compare: function() {{ initCompare(); }},
}};


// ── Contributions tab ──
let cCharts = {{}};

function initContributions() {{
    renderContrib();
    document.querySelectorAll('#contrib-metric button').forEach(b => {{
        b.addEventListener('click', () => {{
            document.querySelectorAll('#contrib-metric button').forEach(x=>x.classList.remove('active'));
            b.classList.add('active'); renderContrib();
        }});
    }});
    document.querySelectorAll('#contrib-sort button').forEach(b => {{
        b.addEventListener('click', () => {{
            document.querySelectorAll('#contrib-sort button').forEach(x=>x.classList.remove('active'));
            b.classList.add('active'); renderContrib();
        }});
    }});
    document.getElementById('contrib-range').addEventListener('change', () => renderContrib());
}}

function renderContrib() {{
    const metric = document.querySelector('#contrib-metric button.active').dataset.metric;
    const sortBy = document.querySelector('#contrib-sort button.active').dataset.sort;
    const range = document.getElementById('contrib-range').value;
    const aM = D.allMonths;
    let si = 0;
    if (range !== 'all' && aM.length) si = Math.max(0, aM.length - parseInt(range));
    const vm = aM.slice(si);

    let authors = D.authorDetails.map((a,idx) => {{
        const mc=a.mc.slice(si), mi=a.mi.slice(si), md=a.md.slice(si);
        return {{ ...a, mc, mi, md, tC:mc.reduce((s,v)=>s+v,0), tI:mi.reduce((s,v)=>s+v,0), tD:md.reduce((s,v)=>s+v,0), idx }};
    }});

    if (sortBy === 'recent') {{
        const l3 = Math.max(0, vm.length-3);
        const k = metric==='commits'?'mc':metric==='additions'?'mi':'md';
        authors.sort((a,b) => b[k].slice(l3).reduce((s,v)=>s+v,0) - a[k].slice(l3).reduce((s,v)=>s+v,0));
    }} else {{
        const k = metric==='commits'?'tC':metric==='additions'?'tI':'tD';
        authors.sort((a,b) => b[k]-a[k]);
    }}

    Object.values(cCharts).forEach(ch=>ch.destroy());
    cCharts = {{}};
    const container = document.getElementById('contrib-cards');
    container.innerHTML = '';

    authors.forEach((a, rank) => {{
        const data = metric==='commits'?a.mc:metric==='additions'?a.mi:a.md;
        const total = data.reduce((s,v)=>s+v,0);
        if (total===0) return;
        const color = metric==='commits'?C.blue:metric==='additions'?C.green:C.red;
        const ml = metric==='commits'?'commits':metric==='additions'?'lines added':'lines removed';

        const card = document.createElement('div');
        card.className = 'contrib-card';
        card.innerHTML = `
            <div class="card-header">
                <div class="author-info">
                    <span class="rank">#${{rank+1}}</span>
                    <span class="name">${{a.name}}</span>
                </div>
                <div class="stat-pills">
                    <span class="pill commits">${{a.tC.toLocaleString()}} commits</span>
                    <span class="pill add">+${{a.tI.toLocaleString()}}</span>
                    <span class="pill del">-${{a.tD.toLocaleString()}}</span>
                </div>
            </div>
            <canvas id="cc-${{rank}}"></canvas>
        `;
        container.appendChild(card);

        cCharts['c'+rank] = new Chart('cc-'+rank, {{
            type: 'bar',
            data: {{
                labels: vm,
                datasets: [{{
                    label: a.name + ' — ' + ml,
                    data: data,
                    backgroundColor: color+'88',
                    borderColor: color,
                    borderWidth: 1,
                    borderRadius: 2,
                }}]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: true,
                aspectRatio: 8,
                interaction: {{ mode:'index', intersect:false }},
                plugins: {{ tooltip: {{ callbacks: {{
                    label: c => c.dataset.label+': '+c.parsed.y.toLocaleString()
                }} }} }},
                scales: {{
                    x: {{ grid:{{ display:false }}, ticks:{{ maxTicksLimit:15, font:{{ size:9 }} }} }},
                    y: {{ grid:{{ color:'#30363d22' }}, beginAtZero:true, ticks:{{ callback:v=>v.toLocaleString() }} }}
                }}
            }}
        }});
    }});
}}

// ── Compare tab ──
let cmpCharts = {{}};
function drawMonthlyChart(vm, mcA, mcB, miA, miB, mdA, mdB, nameA, nameB) {{
    if (cmpCharts.monthly) cmpCharts.monthly.destroy();
    document.getElementById('cmp-monthly-title').textContent = 'Commits & ±LOC per Month — ' + nameA + ' vs ' + nameB;
    const netMonthA = miA.map((v,i) => v - mdA[i]);
    // Symmetric log transform: preserves sign, log-scales magnitude, zero stays zero
    const symlog = v => v === 0 ? 0 : Math.sign(v) * Math.log1p(Math.abs(v));
    const symlogInv = v => v === 0 ? 0 : Math.sign(v) * (Math.expm1(Math.abs(v)));

    // Separate +LOC (insertions) and -LOC (deletions, negated) per author
    const insA = miA.map(symlog);
    const insB = miB.map(symlog);
    const delA = mdA.map(v => symlog(-v));
    const delB = mdB.map(v => symlog(-v));

    // Align zeros: both axes share the same zero-fraction vertically.
    const yMax = Math.max(...mcA, ...mcB, 1);
    const l1Max = Math.max(...insA, ...insB, 0.01);
    const l1Min = Math.min(...delA, ...delB, -0.01);
    const zeroFrac = Math.abs(l1Min) / (l1Max - l1Min);
    const yMin = -(zeroFrac / (1 - zeroFrac)) * yMax;
    const y1AlignedMin = l1Min;
    const y1AlignedMax = l1Max;

    const legendOpts2 = {{ display: true, labels: {{ color: '#e6edf3', font: {{ family: "'JetBrains Mono', monospace", size: 11 }} }} }};
    const baseOpts = lO();
    cmpCharts.monthly = new Chart('chart-cmp-monthly', {{
        data: {{
            labels: vm,
            datasets: [
                {{ type:'bar',  label: nameA+' commits', data: mcA, backgroundColor: C.blue+'55',   borderColor: C.blue,   borderWidth: 1, borderRadius: 2, yAxisID: 'y',  order: 2 }},
                {{ type:'bar',  label: nameB+' commits', data: mcB, backgroundColor: C.orange+'55', borderColor: C.orange, borderWidth: 1, borderRadius: 2, yAxisID: 'y',  order: 2 }},
                {{ type:'line', label: nameA+' +LOC',    data: insA, borderColor: C.green,  backgroundColor: 'transparent', borderWidth: 2, borderDash: [5,3], pointRadius: 2, pointHoverRadius: 5, tension: 0.3, yAxisID: 'y1', order: 1 }},
                {{ type:'line', label: nameB+' +LOC',    data: insB, borderColor: C.yellow, backgroundColor: 'transparent', borderWidth: 2, borderDash: [5,3], pointRadius: 2, pointHoverRadius: 5, tension: 0.3, yAxisID: 'y1', order: 1 }},
                {{ type:'line', label: nameA+' -LOC',    data: delA, borderColor: C.red,    backgroundColor: 'transparent', borderWidth: 2, borderDash: [2,2], pointRadius: 2, pointHoverRadius: 5, tension: 0.3, yAxisID: 'y1', order: 1 }},
                {{ type:'line', label: nameB+' -LOC',    data: delB, borderColor: C.pink,   backgroundColor: 'transparent', borderWidth: 2, borderDash: [2,2], pointRadius: 2, pointHoverRadius: 5, tension: 0.3, yAxisID: 'y1', order: 1 }},
            ]
        }},
        options: {{
            ...baseOpts,
            plugins: {{
                ...baseOpts.plugins,
                legend: legendOpts2,
                tooltip: {{ callbacks: {{ label: ctx => {{
                    if (ctx.dataset.yAxisID === 'y1') {{
                        const real = Math.abs(Math.round(symlogInv(ctx.parsed.y)));
                        return ctx.dataset.label + ': ' + real.toLocaleString() + ' lines';
                    }}
                    return ctx.dataset.label + ': ' + ctx.parsed.y.toLocaleString();
                }} }} }}
            }},
            scales: {{
                x: {{ grid: {{ display: false }}, ticks: {{ maxTicksLimit: 15, font: {{ size: 9 }} }} }},
                y: {{
                    type: 'linear', position: 'left',
                    min: yMin, max: yMax,
                    grid: {{ color: '#30363d22' }},
                    ticks: {{ stepSize: Math.ceil(yMax / 15), callback: v => v < 0 ? '' : v.toLocaleString() }},
                    title: {{ display: true, text: 'Commits', color: '#8b949e', font: {{ size: 10 }} }}
                }},
                y1: {{
                    type: 'linear', position: 'right',
                    min: y1AlignedMin, max: y1AlignedMax,
                    grid: {{ drawOnChartArea: false }},
                    ticks: {{ stepSize: (y1AlignedMax - y1AlignedMin) / 15, callback: v => Math.abs(Math.round(symlogInv(v))).toLocaleString() }},
                    title: {{ display: true, text: '±LOC', color: '#8b949e', font: {{ size: 10 }} }}
                }}
            }}
        }}
    }});
}}

function initCompare() {{
    renderCompare();
    document.getElementById('cmp-a').addEventListener('change', renderCompare);
    document.getElementById('cmp-b').addEventListener('change', renderCompare);
    document.getElementById('cmp-range').addEventListener('change', renderCompare);
}}

function renderCompare() {{
    const nameA = document.getElementById('cmp-a').value;
    const nameB = document.getElementById('cmp-b').value;
    const range = document.getElementById('cmp-range').value;

    const aM = D.allMonths;
    let si = 0;
    if (range !== 'all' && aM.length) si = Math.max(0, aM.length - parseInt(range));
    const vm = aM.slice(si);

    function getDetail(name) {{
        return D.authorDetails.find(a => a.name === name) || null;
    }}
    function getActivity(name) {{
        return D.authorActivity[name] || {{ hour: Array(24).fill(0), dow: Array(7).fill(0) }};
    }}

    const dA = getDetail(nameA), dB = getDetail(nameB);
    const acA = getActivity(nameA), acB = getActivity(nameB);

    // Sliced monthly data
    const mcA = dA ? dA.mc.slice(si) : Array(vm.length).fill(0);
    const mcB = dB ? dB.mc.slice(si) : Array(vm.length).fill(0);
    const miA = dA ? dA.mi.slice(si) : Array(vm.length).fill(0);
    const miB = dB ? dB.mi.slice(si) : Array(vm.length).fill(0);

    const tCA = mcA.reduce((s,v)=>s+v,0);
    const tCB = mcB.reduce((s,v)=>s+v,0);
    const tIA = miA.reduce((s,v)=>s+v,0);
    const tIB = miB.reduce((s,v)=>s+v,0);
    const tDA = dA ? dA.md.slice(si).reduce((s,v)=>s+v,0) : 0;
    const tDB = dB ? dB.md.slice(si).reduce((s,v)=>s+v,0) : 0;

    // Stat cards
    const cards = document.getElementById('cmp-stat-cards');
    function pct(a, b) {{
        const t = a + b; return t === 0 ? [50, 50] : [a/t*100, b/t*100];
    }}
    function statCard(label, vA, vB, colorA, colorB) {{
        const [pA, pB] = pct(vA, vB);
        return `<div class="cmp-card">
            <div class="label">${{label}}</div>
            <div class="cmp-row">
                <span class="author-tag" style="background:${{colorA}}22;color:${{colorA}}">${{nameA}}</span>
                <span class="val">${{vA.toLocaleString()}}</span>
            </div>
            <div class="cmp-row">
                <span class="author-tag" style="background:${{colorB}}22;color:${{colorB}}">${{nameB}}</span>
                <span class="val">${{vB.toLocaleString()}}</span>
            </div>
            <div class="cmp-bar-wrap">
                <div class="cmp-bar-a" style="background:${{colorA}};width:${{pA}}%"></div>
                <div class="cmp-bar-b" style="background:${{colorB}};width:${{pB}}%"></div>
            </div>
        </div>`;
    }}
    cards.innerHTML =
        statCard('Commits', tCA, tCB, C.blue, C.orange) +
        statCard('Lines Added', tIA, tIB, C.green, C.yellow) +
        statCard('Lines Removed', tDA, tDB, C.red, C.pink);

    // Destroy old charts
    Object.values(cmpCharts).forEach(ch => ch.destroy());
    cmpCharts = {{}};

    const dow = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];

    const mdA = dA ? dA.md.slice(si) : Array(vm.length).fill(0);
    const mdB = dB ? dB.md.slice(si) : Array(vm.length).fill(0);
    drawMonthlyChart(vm, mcA, mcB, miA, miB, mdA, mdB, nameA, nameB);

    cmpCharts.hour = new Chart('chart-cmp-hour', {{
        type: 'bar',
        data: {{
            labels: Array.from({{length:24}}, (_,i) => i+'h'),
            datasets: [
                {{ label: nameA, data: acA.hour, backgroundColor: C.blue+'88', borderColor: C.blue, borderWidth: 1, borderRadius: 2 }},
                {{ label: nameB, data: acB.hour, backgroundColor: C.orange+'88', borderColor: C.orange, borderWidth: 1, borderRadius: 2 }},
            ]
        }},
        options: {{ ...bO(), plugins: {{ ...bO().plugins, legend: {{ display: true, labels: {{ color: '#e6edf3', font: {{ family: "'JetBrains Mono', monospace", size: 11 }} }} }} }} }}
    }});

    cmpCharts.dow = new Chart('chart-cmp-dow', {{
        type: 'bar',
        data: {{
            labels: dow,
            datasets: [
                {{ label: nameA, data: acA.dow, backgroundColor: C.blue+'88', borderColor: C.blue, borderWidth: 1, borderRadius: 2 }},
                {{ label: nameB, data: acB.dow, backgroundColor: C.orange+'88', borderColor: C.orange, borderWidth: 1, borderRadius: 2 }},
            ]
        }},
        options: {{ ...bO(), plugins: {{ ...bO().plugins, legend: {{ display: true, labels: {{ color: '#e6edf3', font: {{ family: "'JetBrains Mono', monospace", size: 11 }} }} }} }} }}
    }});

    function cumsum(arr) {{ let s=0; return arr.map(v => (s+=v, s)); }}
    const legendOpts = {{ display: true, labels: {{ color: '#e6edf3', font: {{ family: "'JetBrains Mono', monospace", size: 11 }} }} }};

    // Net LOC (ins - del cumulative)
    const netA = cumsum(miA.map((v,i) => v - mdA[i]));
    const netB = cumsum(miB.map((v,i) => v - mdB[i]));
    cmpCharts.netloc = new Chart('chart-cmp-netloc', {{
        type: 'line',
        data: {{
            labels: vm,
            datasets: [
                {{ label: nameA, data: netA, borderColor: C.blue, backgroundColor: C.blue+'18', fill: false }},
                {{ label: nameB, data: netB, borderColor: C.orange, backgroundColor: C.orange+'18', fill: false }},
            ]
        }},
        options: {{ ...lO(), plugins: {{ ...lO().plugins, legend: legendOpts }} }}
    }});

    cmpCharts.additions = new Chart('chart-cmp-additions', {{
        type: 'line',
        data: {{
            labels: vm,
            datasets: [
                {{ label: nameA, data: cumsum(miA), borderColor: C.green, backgroundColor: C.green+'18', fill: false }},
                {{ label: nameB, data: cumsum(miB), borderColor: C.yellow, backgroundColor: C.yellow+'18', fill: false }},
            ]
        }},
        options: {{ ...lO(), plugins: {{ ...lO().plugins, legend: legendOpts }} }}
    }});

    // Blame charts (only rendered if data present)
    if (D.blameLocOverTime) {{
        const blameLabels = D.blameLocOverTime.labels;
        const blameSeries = D.blameLocOverTime.series;

        // Two-author line chart filtered to selected authors
        const blameA = blameSeries[nameA] || Array(blameLabels.length).fill(0);
        const blameB = blameSeries[nameB] || Array(blameLabels.length).fill(0);
        if (cmpCharts.blame) cmpCharts.blame.destroy();

        // Commit panel state
        let bcpFrozen = false;
        let bcpSortCol = 'ts';
        let bcpSortDir = -1; // -1 desc, 1 asc

        const bcpPanel = document.getElementById('blame-commit-panel');
        const bcpLabel = document.getElementById('bcp-label');
        const bcpMeta  = document.getElementById('bcp-meta');
        const bcpBody  = document.getElementById('bcp-body');
        const bcpFreeze = document.getElementById('bcp-freeze');

        bcpFreeze.onclick = () => {{
            bcpFrozen = !bcpFrozen;
            bcpFreeze.textContent = bcpFrozen ? 'unfreeze' : 'freeze';
            bcpFreeze.classList.toggle('frozen', bcpFrozen);
        }};

        // Sortable headers
        document.getElementById('bcp-table').querySelectorAll('thead th').forEach(th => {{
            th.addEventListener('click', () => {{
                const col = th.dataset.col;
                if (bcpSortCol === col) bcpSortDir *= -1;
                else {{ bcpSortCol = col; bcpSortDir = col === 'ts' || col === 'ins' || col === 'del' || col === 'files' ? -1 : 1; }}
                document.getElementById('bcp-table').querySelectorAll('thead th').forEach(h => h.classList.remove('sort-asc','sort-desc'));
                th.classList.add(bcpSortDir === 1 ? 'sort-asc' : 'sort-desc');
                renderBcpRows(bcpBody._currentBucket);
            }});
        }});

        function renderBcpRows(bucket) {{
            if (!bucket) return;
            bcpBody._currentBucket = bucket;
            const commits = (D.commitsByBucket[bucket] || []).slice();
            const col = bcpSortCol, dir = bcpSortDir;
            commits.sort((a, b) => {{
                let av, bv;
                if (col === 'ts')      {{ av = a.ts;          bv = b.ts; }}
                else if (col === 'ins') {{ av = a.insertions;  bv = b.insertions; }}
                else if (col === 'del') {{ av = a.deletions;   bv = b.deletions; }}
                else if (col === 'files') {{ av = a.files_changed; bv = b.files_changed; }}
                else if (col === 'author') {{ av = a.author.toLowerCase(); bv = b.author.toLowerCase(); }}
                else if (col === 'subject') {{ av = a.subject.toLowerCase(); bv = b.subject.toLowerCase(); }}
                else {{ av = a.hash; bv = b.hash; }}
                return av < bv ? -dir : av > bv ? dir : 0;
            }});
            const base = D.githubBase;
            bcpBody.innerHTML = commits.map(c => {{
                const date = new Date(c.ts * 1000).toISOString().slice(0,10);
                const h7 = c.hash.slice(0,7);
                const hashEl = base
                    ? `<a href="${{base}}/commit/${{c.hash}}" target="_blank" rel="noopener" style="color:var(--accent);text-decoration:none">${{h7}}</a>`
                    : h7;
                const msg = c.subject.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                return `<tr>
                    <td><code>${{hashEl}}</code></td>
                    <td>${{date}}</td>
                    <td>${{c.author}}</td>
                    <td class="num add">+${{c.insertions.toLocaleString()}}</td>
                    <td class="num del">-${{c.deletions.toLocaleString()}}</td>
                    <td class="num">${{c.files_changed}}</td>
                    <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{msg}}">${{msg}}</td>
                </tr>`;
            }}).join('');
            // set sort indicator
            document.getElementById('bcp-table').querySelectorAll('thead th').forEach(h => h.classList.remove('sort-asc','sort-desc'));
            const activeHdr = document.querySelector(`#bcp-table thead th[data-col="${{col}}"]`);
            if (activeHdr) activeHdr.classList.add(dir === 1 ? 'sort-asc' : 'sort-desc');
        }}

        function showBucketPanel(bucket) {{
            if (bcpFrozen) return;
            const commits = D.commitsByBucket[bucket] || [];
            bcpLabel.textContent = bucket;
            bcpMeta.textContent = `${{commits.length}} commit${{commits.length !== 1 ? 's' : ''}}`;
            bcpPanel.style.display = '';
            renderBcpRows(bucket);
        }}

        const blameOpts = {{
            ...lO(),
            plugins: {{ ...lO().plugins, legend: legendOpts }},
            onClick: (evt, elements, chart) => {{
                if (!elements.length) return;
                const idx = elements[0].index;
                const bucket = blameLabels[idx];
                showBucketPanel(bucket);
            }},
            onHover: (evt, elements) => {{
                evt.native.target.style.cursor = elements.length ? 'pointer' : 'default';
            }},
        }};
        // Make points visible on hover
        blameOpts.elements = {{ point: {{ radius: 3, hoverRadius: 7 }}, line: {{ tension: 0.3, borderWidth: 2 }} }};

        cmpCharts.blame = new Chart('chart-cmp-blame', {{
            type: 'line',
            data: {{
                labels: blameLabels,
                datasets: [
                    {{ label: nameA, data: blameA, borderColor: C.blue, backgroundColor: C.blue+'18', fill: false }},
                    {{ label: nameB, data: blameB, borderColor: C.orange, backgroundColor: C.orange+'18', fill: false }},
                ]
            }},
            options: blameOpts,
        }});

        // Bar chart: all authors at latest snapshot
        const lastIdx = blameLabels.length - 1;
        const barData = Object.entries(blameSeries)
            .map(([name, vals]) => ({{ name, lines: vals[lastIdx] || 0 }}))
            .filter(x => x.lines > 0)
            .sort((a,b) => b.lines - a.lines)
            .slice(0, 20);
        if (cmpCharts.blameBar) cmpCharts.blameBar.destroy();
        cmpCharts.blameBar = new Chart('chart-cmp-blame-bar', {{
            type: 'bar',
            data: {{
                labels: barData.map(x => x.name.length>20 ? x.name.slice(0,18)+'…' : x.name),
                datasets: [{{ data: barData.map(x=>x.lines), backgroundColor: barData.map((_,i)=>CL[i%CL.length]+'cc'), borderRadius: 3 }}]
            }},
            options: {{ ...bO(), plugins: {{ ...bO().plugins, tooltip: {{ callbacks: {{ label: c => 'Lines: '+c.parsed.y.toLocaleString() }} }} }} }}
        }});
    }}
}}

document.addEventListener('DOMContentLoaded', () => {{
    _init['general'] = true;

    // Author commit modal
    const modal = document.getElementById('author-modal');
    const modalTitle = document.getElementById('author-modal-title');
    const modalBody = document.getElementById('author-modal-body');
    document.getElementById('author-modal-close').addEventListener('click', () => modal.classList.remove('open'));
    modal.addEventListener('click', e => {{ if (e.target === modal) modal.classList.remove('open'); }});

    document.addEventListener('click', e => {{
        const link = e.target.closest('.author-link');
        if (!link) return;
        e.preventDefault();
        const author = link.dataset.author;
        const commits = D.authorCommits[author] || [];
        const base = D.githubBase;
        modalTitle.textContent = author + ' — ' + commits.length + ' commits';
        modalBody.innerHTML = commits.map(c => {{
            const date = new Date(c.ts * 1000).toISOString().slice(0,10);
            const hash7 = c.hash.slice(0,7);
            const hashEl = base
                ? `<a href="${{base}}/commit/${{c.hash}}" target="_blank" rel="noopener">${{hash7}}</a>`
                : hash7;
            return `<div class="modal-commit-row"><span class="modal-commit-hash">${{hashEl}}</span><span class="modal-commit-date">${{date}}</span><span class="modal-commit-msg">${{c.subject.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}}</span></div>`;
        }}).join('');
        modal.classList.add('open');
    }});
}});
</script>

<div id="author-modal">
  <div id="author-modal-box">
    <div id="author-modal-header">
      <span id="author-modal-title"></span>
      <button id="author-modal-close">✕</button>
    </div>
    <div id="author-modal-body"></div>
  </div>
</div>
</body>
</html>"""

    return report_html


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="gitstats — git repository statistics report generator"
    )
    parser.add_argument("repo_path", help="Path to a git repository")
    parser.add_argument("output_dir", nargs="?", default="gitstats_report",
                        help="Where to write the report (default: gitstats_report)")
    parser.add_argument(
        "--blame", nargs="?", const=True, metavar="CACHE_DIR",
        help="Enable blame-based LOC tracking. Optionally specify a cache directory "
             "(default: ~/.cache/gitstats_blame). Slow on first run, fast on re-runs."
    )
    args = parser.parse_args()

    if not os.path.isdir(args.repo_path):
        print(f"Error: '{args.repo_path}' is not a directory.")
        sys.exit(1)

    blame_enabled = args.blame is not None
    if blame_enabled:
        if args.blame is True:
            blame_cache_dir = os.path.expanduser("~/.cache/gitstats_blame")
        else:
            blame_cache_dir = os.path.expanduser(args.blame)
        print(f"Blame mode enabled. Cache: {blame_cache_dir}")
    else:
        blame_cache_dir = None

    stats = collect_stats(args.repo_path, blame=blame_enabled, blame_cache_dir=blame_cache_dir)

    os.makedirs(args.output_dir, exist_ok=True)
    out_file = os.path.join(args.output_dir, "index.html")

    print(f"Generating report...")
    html = generate_html(stats, blame=blame_enabled)

    with open(out_file, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Report written to: {os.path.abspath(out_file)}")
    print(f"Open it in a browser to view the statistics.")


if __name__ == "__main__":
    main()
