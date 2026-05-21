#!/usr/bin/env python3
"""Validate Runway job specs against schema and policy."""

from __future__ import annotations

import pathlib
import re
import sys
from typing import Any

import jsonschema
import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
JOBS_DIR = ROOT / "jobs"
POLICY_PATH = ROOT / "policies" / "default.yaml"
SCHEMA_PATH = ROOT / "schemas" / "job.schema.json"


def load_yaml(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    return data


def load_schema(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def validate_policy(path: pathlib.Path, job: dict[str, Any], policy: dict[str, Any]) -> list[str]:
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
            f"selection.profile_seconds={selection['profile_seconds']} exceeds "
            f"max_profile_seconds={limits.get('max_profile_seconds')}"
        )

    allowed_backends = set(policy.get("allowed_backends", []))
    unknown_backends = sorted(set(spec["backends"]) - allowed_backends)
    if unknown_backends:
        errors.append(f"backends contains unsupported values: {unknown_backends}")

    allowed_gpu_types = set(policy.get("allowed_gpu_types", []))
    if resources["gpu_type"] not in allowed_gpu_types:
        errors.append(f"resources.gpu_type={resources['gpu_type']!r} is not allowed")

    repo = spec["code"]["repo"]
    prefixes = policy.get("allowed_repo_prefixes", [])
    if not any(repo.startswith(prefix) for prefix in prefixes):
        errors.append(f"code.repo={repo!r} is outside allowed_repo_prefixes={prefixes}")

    run = spec["run"]
    for pattern in policy.get("forbidden_run_patterns", []):
        if re.search(pattern, run):
            errors.append(f"run command matches forbidden pattern: {pattern!r}")

    artifact_uri = spec["artifacts"]["uri"]
    if "{run_id}" not in artifact_uri or "{backend_id}" not in artifact_uri:
        errors.append("artifacts.uri must include both {run_id} and {backend_id}")

    return [f"{path}: {error}" for error in errors]


def main() -> int:
    policy = load_yaml(POLICY_PATH)
    schema = load_schema(SCHEMA_PATH)
    validator = jsonschema.Draft202012Validator(schema)

    job_paths = sorted(
        list(JOBS_DIR.glob("*.yaml")) + list(JOBS_DIR.glob("*.yml"))
    )

    errors: list[str] = []
    for path in job_paths:
        try:
            job = load_yaml(path)
            schema_errors = sorted(validator.iter_errors(job), key=lambda e: e.path)
            for error in schema_errors:
                loc = ".".join(str(part) for part in error.path) or "<root>"
                errors.append(f"{path}: schema error at {loc}: {error.message}")
            if not schema_errors:
                errors.extend(validate_policy(path, job, policy))
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
