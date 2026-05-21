#!/usr/bin/env python3
"""Validate Runway job specs against schema and policy."""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
from typing import Any, Iterable

import jsonschema
import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
JOBS_DIR = ROOT / "jobs"
EXAMPLES_DIR = ROOT / "examples"
POLICY_PATH = ROOT / "policies" / "default.yaml"
SCHEMA_PATH = ROOT / "schemas" / "job.schema.json"

# Placeholders the agent resolves at execution time (run_id / backend_id).
# These are NOT validated against allowed_placeholders.
RUNTIME_PLACEHOLDERS = {"run_id", "backend_id"}
RUNTIME_PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
AGENT_PLACEHOLDER_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def load_yaml(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    return data


def load_schema(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def iter_strings(value: Any, path: str = "") -> Iterable[tuple[str, str]]:
    """Yield (json-path, string) for every string leaf in `value`."""
    if isinstance(value, str):
        yield path or "<root>", value
    elif isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            yield from iter_strings(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from iter_strings(child, f"{path}[{index}]")


def validate_placeholders(
    job: dict[str, Any], allowed: set[str]
) -> list[str]:
    errors: list[str] = []
    for field_path, value in iter_strings(job):
        for match in AGENT_PLACEHOLDER_RE.finditer(value):
            name = match.group(1)
            if name not in allowed:
                errors.append(
                    f"{field_path}: placeholder ${{{name}}} is not in "
                    f"allowed_placeholders"
                )
    return errors


def validate_forbidden_substrings(
    job: dict[str, Any], substrings: list[str]
) -> list[str]:
    errors: list[str] = []
    lowered = [s.lower() for s in substrings]
    for field_path, value in iter_strings(job):
        haystack = value.lower()
        for raw, needle in zip(substrings, lowered):
            if needle and needle in haystack:
                errors.append(
                    f"{field_path}: contains forbidden substring {raw!r}"
                )
    return errors


def validate_forbidden_regex(
    job: dict[str, Any], patterns: list[str]
) -> list[str]:
    errors: list[str] = []
    compiled = [(p, re.compile(p)) for p in patterns]
    for field_path, value in iter_strings(job):
        for raw, regex in compiled:
            if regex.search(value):
                errors.append(
                    f"{field_path}: matches forbidden pattern {raw!r}"
                )
    return errors


def validate_artifact_uri(
    job: dict[str, Any], allowed_prefixes: list[str]
) -> list[str]:
    uri = job["spec"]["artifacts"]["uri"]
    if not allowed_prefixes:
        return []
    if not any(uri.startswith(prefix) for prefix in allowed_prefixes):
        return [
            f"spec.artifacts.uri={uri!r} does not start with any "
            f"allowed_artifact_uri_prefixes entry"
        ]
    return []


def validate_runtime_placeholders(job: dict[str, Any]) -> list[str]:
    uri = job["spec"]["artifacts"]["uri"]
    found = set(RUNTIME_PLACEHOLDER_RE.findall(uri))
    missing = RUNTIME_PLACEHOLDERS - found
    if missing:
        return [
            "spec.artifacts.uri must include "
            + " and ".join(f"{{{p}}}" for p in sorted(RUNTIME_PLACEHOLDERS))
        ]
    unknown = found - RUNTIME_PLACEHOLDERS
    if unknown:
        return [
            f"spec.artifacts.uri has unknown runtime placeholders: "
            f"{sorted(unknown)}"
        ]
    return []


def validate_owner_matches_author(
    job: dict[str, Any], pr_author: str | None
) -> list[str]:
    """In CI on a PR, spec.metadata.owner must equal the PR author.

    Without this, Bob could submit a spec with ``owner: alice`` and the
    agent — which keys secrets by owner — would inject Alice's wandb / HF
    tokens into Bob's training script. The check is skipped (pr_author
    falsy) for local dev runs and for push-to-main events where the
    submitter context is no longer meaningful.
    """
    if not pr_author:
        return []
    owner = job["metadata"]["owner"]
    if owner != pr_author:
        return [
            f"metadata.owner={owner!r} does not match the PR author "
            f"({pr_author!r}); each intern must submit their own specs"
        ]
    return []


def validate_policy(
    path: pathlib.Path,
    job: dict[str, Any],
    policy: dict[str, Any],
    pr_author: str | None = None,
) -> list[str]:
    errors: list[str] = []
    spec = job["spec"]
    resources = spec["resources"]
    selection = spec["selection"]

    limits = policy.get("limits", {})
    if resources["gpus"] > limits.get("max_gpus_per_job", 0):
        errors.append(
            f"resources.gpus={resources['gpus']} exceeds "
            f"max_gpus_per_job={limits.get('max_gpus_per_job')}"
        )

    if resources["max_hours"] > limits.get("max_hours", 0):
        errors.append(
            f"resources.max_hours={resources['max_hours']} exceeds "
            f"max_hours={limits.get('max_hours')}"
        )

    if selection["profile_seconds"] > limits.get("max_profile_seconds", 0):
        errors.append(
            f"selection.profile_seconds={selection['profile_seconds']} "
            f"exceeds max_profile_seconds={limits.get('max_profile_seconds')}"
        )

    allowed_backends = set(policy.get("allowed_backends", []))
    unknown_backends = sorted(set(spec["backends"]) - allowed_backends)
    if unknown_backends:
        errors.append(
            f"backends contains unsupported values: {unknown_backends}"
        )

    allowed_gpu_types = set(policy.get("allowed_gpu_types", []))
    if resources["gpu_type"] not in allowed_gpu_types:
        errors.append(
            f"resources.gpu_type={resources['gpu_type']!r} is not allowed"
        )

    repo = spec["code"]["repo"]
    prefixes = policy.get("allowed_repo_prefixes", [])
    if not any(repo.startswith(prefix) for prefix in prefixes):
        errors.append(
            f"code.repo={repo!r} is outside allowed_repo_prefixes={prefixes}"
        )

    errors.extend(validate_runtime_placeholders(job))
    errors.extend(
        validate_artifact_uri(job, policy.get("allowed_artifact_uri_prefixes", []))
    )
    errors.extend(
        validate_placeholders(job, set(policy.get("allowed_placeholders", [])))
    )
    errors.extend(
        validate_forbidden_substrings(
            job, policy.get("forbidden_substrings", [])
        )
    )
    errors.extend(
        validate_forbidden_regex(
            job, policy.get("forbidden_string_regex", [])
        )
    )
    errors.extend(validate_owner_matches_author(job, pr_author))

    return [f"{path}: {error}" for error in errors]


def collect_paths(include_examples: bool) -> list[pathlib.Path]:
    paths = list(JOBS_DIR.glob("*.yaml")) + list(JOBS_DIR.glob("*.yml"))
    if include_examples:
        paths += list(EXAMPLES_DIR.glob("*.yaml")) + list(
            EXAMPLES_DIR.glob("*.yml")
        )
    return sorted(paths)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--include-examples",
        action="store_true",
        help="Also validate files under examples/ (used by CI smoke test).",
    )
    parser.add_argument(
        "--pr-author",
        default=os.environ.get("VALIDATOR_PR_AUTHOR") or None,
        help=(
            "If set, every spec's metadata.owner must equal this GitHub "
            "login. The check applies only to paths listed in --pr-changed "
            "(or to all spec files if --pr-changed is omitted)."
        ),
    )
    parser.add_argument(
        "--pr-changed",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "File path added or modified in the current PR. The owner-vs-"
            "author check applies only to these paths; the rest of the "
            "policy checks always apply to every spec in jobs/. May be "
            "repeated; an empty list (default) means apply owner check "
            "to every spec."
        ),
    )
    args = parser.parse_args()

    policy = load_yaml(POLICY_PATH)
    schema = load_schema(SCHEMA_PATH)
    validator = jsonschema.Draft202012Validator(schema)

    pr_author = args.pr_author or None
    # Resolve --pr-changed paths against the repo root so they match
    # pathlib comparisons below.
    pr_changed_set: set[pathlib.Path] = {
        (ROOT / p).resolve() for p in (args.pr_changed or [])
    }
    if pr_author:
        if pr_changed_set:
            print(
                f"Enforcing metadata.owner == {pr_author!r} on "
                f"{len(pr_changed_set)} changed file(s)"
            )
        else:
            print(f"Enforcing metadata.owner == {pr_author!r} on all specs")

    job_paths = collect_paths(include_examples=args.include_examples)

    errors: list[str] = []
    for path in job_paths:
        try:
            job = load_yaml(path)
            schema_errors = sorted(
                validator.iter_errors(job), key=lambda e: list(e.path)
            )
            for error in schema_errors:
                loc = ".".join(str(part) for part in error.path) or "<root>"
                errors.append(
                    f"{path}: schema error at {loc}: {error.message}"
                )
            if not schema_errors:
                # Owner-vs-author check applies only to (a) files inside
                # jobs/ (not examples/) AND (b) files this PR is actually
                # adding/modifying (not pre-existing specs already on
                # main from prior PRs).
                in_jobs = JOBS_DIR in path.parents
                in_change = (
                    not pr_changed_set
                    or path.resolve() in pr_changed_set
                )
                effective_pr_author = (
                    pr_author if (in_jobs and in_change) else None
                )
                errors.extend(
                    validate_policy(
                        path, job, policy, pr_author=effective_pr_author
                    )
                )
        except Exception as exc:  # pylint: disable=broad-except
            errors.append(f"{path}: {exc}")

    if errors:
        print("Validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(f"Validated {len(job_paths)} job spec(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
