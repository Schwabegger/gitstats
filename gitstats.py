#!/usr/bin/env python3
"""
gitstats.py — A modern git repository statistics generator.
Analyzes a git repository and produces a self-contained HTML report
with interactive charts and detailed statistics.

Usage:
    python3 gitstats.py <git_repo_path> [output_directory]

Requirements:
    - Python 3.7+
    - Git installed and accessible in PATH
"""

import subprocess
import os
import sys
import json
import re
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


def collect_stats(repo_path):
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

    stats["generated_at"] = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    print("  Data collection complete.")
    return stats


# ─── HTML Report Generator ──────────────────────────────────────────────────

def generate_html(stats):
    """Generate a self-contained HTML report."""

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
            <td class="author-name">{esc(a['name'])}</td>
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
            <div class="value">{format_number(stats['total_commits'])}</div>
            <div class="sub">{format_number(stats['total_commits_all'])} including merges</div>
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

document.addEventListener('DOMContentLoaded', () => {{
    _init['general'] = true;
}});
</script>
</body>
</html>"""

    return report_html


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 gitstats.py <git_repo_path> [output_directory]")
        print()
        print("  git_repo_path    Path to a git repository")
        print("  output_directory  Where to write the report (default: ./gitstats_report)")
        sys.exit(1)

    repo_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "gitstats_report"

    if not os.path.isdir(repo_path):
        print(f"Error: '{repo_path}' is not a directory.")
        sys.exit(1)

    stats = collect_stats(repo_path)

    os.makedirs(output_dir, exist_ok=True)
    out_file = os.path.join(output_dir, "index.html")

    print(f"Generating report...")
    html = generate_html(stats)

    with open(out_file, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Report written to: {os.path.abspath(out_file)}")
    print(f"Open it in a browser to view the statistics.")


if __name__ == "__main__":
    main()
