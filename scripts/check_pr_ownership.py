"""Per-file ownership check for jobs/ modifications and deletions.

For every file in this PR with status ``modified``, ``removed``, or
``renamed`` under ``jobs/``, fetches the file's pre-PR (on-main)
content and verifies its ``metadata.owner`` equals the PR author.

This prevents:

- Alice modifying Bob's spec (even if she rewrites ``owner: bob`` to
  ``owner: alice`` to bypass the validator's post-state check).
- Alice deleting Bob's spec — the validator's owner check doesn't fire
  on removed files because there's no post-state to check.
- Alice renaming Bob's spec (status=renamed; we check at the old path).

Added files are intentionally NOT handled here — they're new on main,
so there's no pre-state to compare against. The validator already
checks new files' ``metadata.owner == PR author``.

Reads everything from environment:
  GITHUB_REPOSITORY  - e.g. runway-lab/runway-jobs
  PR_NUMBER          - GitHub PR number
  PR_LOGIN           - PR author's GitHub login
  GH_TOKEN           - for `gh api` calls
  BASE_BRANCH        - optional, default "main"
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys

import yaml


def _gh_api(*args: str) -> dict | list:
    result = subprocess.run(
        ["gh", "api", *args],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def _fetch_file_owner(repo: str, path: str, ref: str) -> tuple[str | None, str | None]:
    """Returns (owner, error). owner is None on error."""
    try:
        meta = _gh_api(f"repos/{repo}/contents/{path}?ref={ref}")
    except subprocess.CalledProcessError as e:
        return None, f"could not fetch {path} on {ref}: {e.stderr.strip()}"
    content_b64 = meta.get("content", "")
    encoding = meta.get("encoding", "")
    if encoding != "base64":
        return None, f"unexpected encoding for {path}: {encoding!r}"
    try:
        text = base64.b64decode(content_b64).decode("utf-8")
        data = yaml.safe_load(text)
    except (ValueError, yaml.YAMLError) as e:
        return None, f"unparseable YAML at {path}: {e}"
    owner = (data or {}).get("metadata", {}).get("owner")
    if not owner:
        return None, f"no metadata.owner found in {path}"
    return owner, None


def main() -> int:
    try:
        repo = os.environ["GITHUB_REPOSITORY"]
        pr_number = os.environ["PR_NUMBER"]
        pr_author = os.environ["PR_LOGIN"]
    except KeyError as missing:
        print(f"required env var missing: {missing}", file=sys.stderr)
        return 2
    base_branch = os.environ.get("BASE_BRANCH", "main")

    files = _gh_api("--paginate", f"repos/{repo}/pulls/{pr_number}/files")

    relevant = []
    for f in files:
        path = f["filename"]
        status = f["status"]
        if not (path.startswith("jobs/") and path.endswith(".yaml")):
            continue
        if status == "added":
            continue  # validator handles new files
        check_path = f.get("previous_filename") if status == "renamed" else path
        relevant.append((status, path, check_path))

    if not relevant:
        print("No modified/removed/renamed jobs/*.yaml in this PR; nothing to check.")
        return 0

    errors: list[str] = []
    for status, new_path, old_path in relevant:
        owner, err = _fetch_file_owner(repo, old_path, base_branch)
        if err:
            errors.append(f"{status} {new_path}: {err}")
            continue
        if owner != pr_author:
            errors.append(
                f"{status} {new_path}: original owner is {owner!r}, "
                f"but PR author is {pr_author!r}. You can only modify, "
                f"delete, or rename your own spec files."
            )

    if errors:
        print("Per-file ownership check failed:", file=sys.stderr)
        for e in errors:
            print(f"- {e}", file=sys.stderr)
        return 1

    print(
        f"Per-file ownership check passed for {len(relevant)} "
        f"modified/removed/renamed jobs/ file(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
