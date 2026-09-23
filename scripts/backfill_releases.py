#!/usr/bin/env python3
"""One-time backfill of tags and GitHub releases for already-published charts.

Reads the published index.yaml and, for every listed chart version (oldest
first):
  1. locates the master-branch commit that introduced the version,
  2. creates an annotated `<chart>-v<version>` tag dated with the index build
     timestamp and pushes it,
  3. creates the GitHub release using the same title/body format as
     create_release.py.

Idempotent: existing tags and releases are skipped, so the script can be
re-run at any time.

Usage:
    backfill_releases.py [--index-url URL | --index-file PATH]
                         [--chart NAME]... [--dry-run]

Run from a clone with full history, with `gh` authenticated against the
origin repository. Requires PyYAML to parse the index.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import create_release as cr  # noqa: E402

DEFAULT_INDEX_URL = "https://juniorjpdj.github.io/charts/index.yaml"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def git(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ, **env) if env else None
    return subprocess.run(["git", *args], text=True, capture_output=True, env=full_env)


def out(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def load_index(path: str | None, url: str) -> dict:
    """Load the helm repository index."""
    if path:
        raw = Path(path).read_text()
    else:
        print(f"fetching index from {url}")
        with urllib.request.urlopen(url, timeout=30) as response:
            raw = response.read().decode()
    return yaml.safe_load(raw)


def git_date(iso: str | None, sha: str) -> str:
    """Normalize an index timestamp to a git-compatible UTC date.

    Falls back to the commit's own date when the index has none.
    """
    if not iso:
        return out(["git", "log", "-1", "--format=%cI", sha])
    # strip sub-microsecond digits and normalize 'Z', for datetime.fromisoformat
    normalized = re.sub(r"\.(\d{1,6})\d*", r".\1", iso).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized).astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S+0000")


def local_tags(prefix: str) -> set[str]:
    proc = git("tag", "--list", f"{prefix}-v*")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
    return set(proc.stdout.split())


def introduced_commits(chart: str) -> dict[str, str]:
    """Map chart version -> master commit that introduced it.

    Walks the first-parent (master) history of the chart's Chart.yaml, newest
    first; a version is introduced by the oldest commit carrying it (the bump
    itself), not by later no-bump commits.
    """
    proc = git("log", "--first-parent", "--format=%H", "--",
               f"{cr.CHARTS_DIR}/{chart}/Chart.yaml")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())

    history: list[tuple[str, str]] = []  # newest -> oldest
    for sha in proc.stdout.splitlines():
        show = git("show", f"{sha}:{cr.CHARTS_DIR}/{chart}/Chart.yaml")
        if show.returncode != 0:
            continue
        match = cr.VERSION_LINE_RE.search(show.stdout)
        if match:
            history.append((sha, match.group(1)))

    introduced: dict[str, str] = {}
    for sha, version in reversed(history):  # oldest first, so the bump wins
        introduced.setdefault(version, sha)
    return introduced


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    index_group = parser.add_mutually_exclusive_group()
    index_group.add_argument("--index-file",
                             help="local index.yaml instead of fetching it")
    index_group.add_argument("--index-url", default=DEFAULT_INDEX_URL)
    parser.add_argument("--chart", action="append", dest="charts",
                        help="limit the backfill to these charts (repeatable)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    os.chdir(out(["git", "rev-parse", "--show-toplevel"]))
    index = load_index(args.index_file, args.index_url)
    entries: dict = index.get("entries") or {}

    try:
        existing_releases = cr.existing_release_tags()
    except RuntimeError as err:
        if not args.dry_run:
            sys.exit(f"Cannot list existing releases: {err}")
        print(f"warning: cannot list existing releases, assuming none ({err})")
        existing_releases = set()

    charts = args.charts or sorted(entries)
    resolver = cr.AuthorResolver()  # shared: one lookup per unique author email
    for chart in charts:
        if chart not in entries:
            print(f"[{chart}]: not present in the index, skipping")
            continue
        if not (Path(cr.CHARTS_DIR) / chart).is_dir():
            print(f"[{chart}]: no {cr.CHARTS_DIR}/{chart} directory, skipping")
            continue

        releases = sorted(entries[chart], key=lambda e: cr.version_key(str(e["version"])))
        commits = introduced_commits(chart)
        tags = local_tags(chart)

        # (version, sha, tag) per index entry, oldest first
        plan: list[tuple[str, str, str, str | None]] = []
        for entry in releases:
            version = str(entry["version"])
            sha = commits.get(version)
            if sha is None:
                print(f"[{chart}]: no commit found introducing {version}, skipping")
                continue
            plan.append((version, sha, f"{chart}-v{version}", entry.get("created")))

        # phase 1: annotated tags, dated with the index build timestamp
        new_tags: list[str] = []
        for version, sha, tag, created in plan:
            if tag in tags:
                continue
            date = git_date(created, sha)
            if args.dry_run:
                print(f"[dry] [{chart}]: would tag {tag} ({date}) at {cr.short(sha)}")
                continue
            env = {"GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date}
            proc = git("tag", "-a", tag, "-m", f"{chart} v{version}", sha, env=env)
            if proc.returncode != 0:
                sys.exit(f"tagging {tag} failed: {proc.stderr.strip()}")
            print(f"[{chart}]: tagged {tag} ({date}) at {cr.short(sha)}")
            new_tags.append(tag)

        if new_tags and not args.dry_run:
            proc = git("push", "origin", *new_tags)
            if proc.returncode != 0:
                sys.exit(f"pushing tags failed: {proc.stderr.strip()}")

        # phase 2: releases, ascending, so each body sees its previous tag
        prev_tag: str | None = None
        prev_sha: str | None = None
        for version, sha, tag, _ in plan:
            if tag in existing_releases:
                prev_tag, prev_sha = tag, sha
                continue
            body = cr.build_body(chart, version, sha, prev_tag,
                                 resolver, prev_sha=prev_sha)
            if args.dry_run:
                print(f"[dry] [{chart}]: would create {tag} (prev: {prev_tag or 'none'})")
                print(body.text, end="")
            else:
                # tags were created and pushed in phase 1 - never pass --target
                cr.publish(chart, version, sha, body.text, tag_exists=True)
                time.sleep(0.25)  # pace release creation against secondary limits
            prev_tag, prev_sha = tag, sha


if __name__ == "__main__":
    main()
