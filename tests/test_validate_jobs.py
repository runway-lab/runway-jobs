"""Tests for the spec validator.

Each test mutates one field of a known-good job spec and asserts the
validator flags exactly that mutation. Keeps tests independent so a future
schema/policy change does not cascade through dozens of fixtures.
"""

from __future__ import annotations

import copy
import pathlib
import sys

import jsonschema
import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import validate_jobs  # noqa: E402

EXAMPLE_PATH = ROOT / "examples" / "job.yaml"
POLICY_PATH = ROOT / "policies" / "default.yaml"
SCHEMA_PATH = ROOT / "schemas" / "job.schema.json"
FAKE_PATH = pathlib.Path("<test>")


@pytest.fixture
def good_job() -> dict:
    return yaml.safe_load(EXAMPLE_PATH.read_text())


@pytest.fixture
def policy() -> dict:
    return yaml.safe_load(POLICY_PATH.read_text())


@pytest.fixture
def schema_validator() -> jsonschema.Draft202012Validator:
    schema = yaml.safe_load(SCHEMA_PATH.read_text())
    return jsonschema.Draft202012Validator(schema)


def errors_from(
    schema_validator: jsonschema.Draft202012Validator,
    policy: dict,
    job: dict,
    pr_author: str | None = None,
) -> list[str]:
    """Mirror what main() does for a single in-memory job."""
    schema_errors = list(schema_validator.iter_errors(job))
    if schema_errors:
        return [
            "schema at "
            + (".".join(str(p) for p in e.path) or "<root>")
            + f": {e.message}"
            for e in schema_errors
        ]
    return validate_jobs.validate_policy(
        FAKE_PATH, job, policy, pr_author=pr_author
    )


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


def test_example_is_valid(schema_validator, policy, good_job):
    assert errors_from(schema_validator, policy, good_job) == []


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_schema_rejects_missing_spec(schema_validator, policy, good_job):
    del good_job["spec"]
    errs = errors_from(schema_validator, policy, good_job)
    assert any("spec" in e for e in errs)


def test_schema_rejects_wrong_apiversion(schema_validator, policy, good_job):
    good_job["apiVersion"] = "runway/v0"
    errs = errors_from(schema_validator, policy, good_job)
    assert any("runway/v1alpha1" in e for e in errs)


def test_schema_rejects_bad_metadata_name(schema_validator, policy, good_job):
    good_job["metadata"]["name"] = "Has Spaces"
    errs = errors_from(schema_validator, policy, good_job)
    assert any("name" in e.lower() for e in errs)


def test_schema_rejects_unknown_top_level_field(
    schema_validator, policy, good_job
):
    good_job["unexpected"] = True
    errs = errors_from(schema_validator, policy, good_job)
    assert any("unexpected" in e for e in errs)


def test_schema_rejects_nonpositive_max_hours(
    schema_validator, policy, good_job
):
    good_job["spec"]["resources"]["max_hours"] = 0
    errs = errors_from(schema_validator, policy, good_job)
    assert any("max_hours" in e or "0" in e for e in errs)


# ---------------------------------------------------------------------------
# Policy: limits
# ---------------------------------------------------------------------------


def test_limit_max_gpus(schema_validator, policy, good_job):
    good_job["spec"]["resources"]["gpus"] = policy["limits"]["max_gpus_per_job"] + 1
    errs = errors_from(schema_validator, policy, good_job)
    assert any("max_gpus_per_job" in e for e in errs)


def test_limit_max_hours(schema_validator, policy, good_job):
    good_job["spec"]["resources"]["max_hours"] = policy["limits"]["max_hours"] + 1
    errs = errors_from(schema_validator, policy, good_job)
    assert any("max_hours" in e for e in errs)


def test_limit_max_profile_seconds(schema_validator, policy, good_job):
    good_job["spec"]["selection"]["profile_seconds"] = (
        policy["limits"]["max_profile_seconds"] + 1
    )
    errs = errors_from(schema_validator, policy, good_job)
    assert any("max_profile_seconds" in e for e in errs)


# ---------------------------------------------------------------------------
# Policy: allowed lists
# ---------------------------------------------------------------------------


def test_allowed_backends_rejects_unknown(schema_validator, policy, good_job):
    good_job["spec"]["backends"] = ["nowhere"]
    errs = errors_from(schema_validator, policy, good_job)
    assert any("backends" in e and "nowhere" in e for e in errs)


def test_allowed_gpu_types_rejects_unknown(schema_validator, policy, good_job):
    good_job["spec"]["resources"]["gpu_type"] = "RTX-9999"
    errs = errors_from(schema_validator, policy, good_job)
    assert any("gpu_type" in e for e in errs)


def test_allowed_repo_prefix_rejects_external(
    schema_validator, policy, good_job
):
    good_job["spec"]["code"]["repo"] = "evil-org/example"
    errs = errors_from(schema_validator, policy, good_job)
    assert any("allowed_repo_prefixes" in e for e in errs)


# ---------------------------------------------------------------------------
# Policy: placeholder allowlist
# ---------------------------------------------------------------------------


def test_placeholder_allowed_when_declared(schema_validator, policy, good_job):
    # The example already uses ${ARTIFACTS_BUCKET}; baseline test covers it,
    # but exercise SLURM_PARTITION too to make sure the allowlist is read.
    good_job["spec"]["run"] = "srun --partition ${SLURM_PARTITION} python a.py"
    assert errors_from(schema_validator, policy, good_job) == []


def test_placeholder_rejected_when_undeclared(
    schema_validator, policy, good_job
):
    good_job["spec"]["run"] = "echo ${SECRET_TOKEN}"
    errs = errors_from(schema_validator, policy, good_job)
    assert any("SECRET_TOKEN" in e for e in errs)


def test_placeholder_check_walks_nested_fields(
    schema_validator, policy, good_job
):
    # Placeholder hidden inside code.repo, not run.
    good_job["spec"]["code"]["repo"] = "runway-lab/${UNDECLARED_PROJECT}"
    errs = errors_from(schema_validator, policy, good_job)
    assert any("UNDECLARED_PROJECT" in e for e in errs)


# ---------------------------------------------------------------------------
# Policy: artifact URI prefix
# ---------------------------------------------------------------------------


def test_artifact_uri_rejects_hardcoded_bucket(
    schema_validator, policy, good_job
):
    good_job["spec"]["artifacts"]["uri"] = (
        "gs://acme-corp-internal/runs/{run_id}/candidates/{backend_id}/"
    )
    errs = errors_from(schema_validator, policy, good_job)
    assert any("allowed_artifact_uri_prefixes" in e for e in errs)


def test_artifact_uri_accepts_s3_placeholder(
    schema_validator, policy, good_job
):
    good_job["spec"]["artifacts"]["uri"] = (
        "s3://${ARTIFACTS_BUCKET}/runs/{run_id}/candidates/{backend_id}/"
    )
    assert errors_from(schema_validator, policy, good_job) == []


def test_artifact_uri_requires_runtime_placeholders(
    schema_validator, policy, good_job
):
    good_job["spec"]["artifacts"]["uri"] = "gs://${ARTIFACTS_BUCKET}/runs/"
    errs = errors_from(schema_validator, policy, good_job)
    assert any("run_id" in e and "backend_id" in e for e in errs)


# ---------------------------------------------------------------------------
# Policy: forbidden substrings (case-insensitive, all fields)
# ---------------------------------------------------------------------------


def test_forbidden_substring_in_run(schema_validator, policy, good_job):
    good_job["spec"]["run"] = "gcloud auth login && python a.py"
    errs = errors_from(schema_validator, policy, good_job)
    assert any("gcloud auth" in e for e in errs)


def test_forbidden_substring_walks_all_fields(
    schema_validator, policy, good_job
):
    # Hide the bad string in metadata.name — must still be caught.
    good_job["metadata"]["name"] = "leak-printenv"
    errs = errors_from(schema_validator, policy, good_job)
    assert any("printenv" in e for e in errs)


def test_forbidden_substring_case_insensitive(
    schema_validator, policy, good_job
):
    good_job["spec"]["run"] = "GCLOUD AUTH login"
    errs = errors_from(schema_validator, policy, good_job)
    assert any("gcloud auth" in e for e in errs)


def test_env_pipe_substring_does_not_falsely_match_everything(
    schema_validator, policy, good_job
):
    """Regression: 'env |' as a regex matched empty string everywhere.

    Now it is a substring, so it must only match when 'env | ' literally
    appears (note the trailing space we keep in the substring rule).
    """
    # Baseline good_job has no 'env | ' anywhere → must validate clean.
    assert errors_from(schema_validator, policy, good_job) == []

    # And it should still trip when actually present.
    good_job["spec"]["run"] = "env | grep TOKEN"
    errs = errors_from(schema_validator, policy, good_job)
    assert any("env" in e for e in errs)


# ---------------------------------------------------------------------------
# Policy: forbidden regex
# ---------------------------------------------------------------------------


def test_forbidden_regex_internal_ip_10(schema_validator, policy, good_job):
    good_job["spec"]["run"] = "ssh user@10.42.0.1 python a.py"
    errs = errors_from(schema_validator, policy, good_job)
    assert any("10" in e for e in errs)


def test_forbidden_regex_internal_ip_192(schema_validator, policy, good_job):
    good_job["spec"]["run"] = "scp data 192.168.1.50:/tmp/"
    errs = errors_from(schema_validator, policy, good_job)
    assert any("192" in e for e in errs)


def test_forbidden_regex_corp_hostname(schema_validator, policy, good_job):
    good_job["spec"]["run"] = "curl https://api.corp.example/foo"
    errs = errors_from(schema_validator, policy, good_job)
    assert any("corp" in e for e in errs)


def test_forbidden_regex_internal_hostname(
    schema_validator, policy, good_job
):
    good_job["spec"]["run"] = "curl https://logs.internal/foo"
    errs = errors_from(schema_validator, policy, good_job)
    assert any("internal" in e for e in errs)


def test_forbidden_regex_does_not_match_public_ip(
    schema_validator, policy, good_job
):
    # 1.1.1.1 is public — must not be flagged by the internal-IP rule.
    good_job["spec"]["run"] = "curl https://1.1.1.1/"
    assert errors_from(schema_validator, policy, good_job) == []


# ---------------------------------------------------------------------------
# Owner == PR author
# ---------------------------------------------------------------------------


def test_owner_matches_author(schema_validator, policy, good_job):
    good_job["metadata"]["owner"] = "alice"
    assert errors_from(
        schema_validator, policy, good_job, pr_author="alice"
    ) == []


def test_owner_mismatch_rejected(schema_validator, policy, good_job):
    good_job["metadata"]["owner"] = "alice"
    errs = errors_from(
        schema_validator, policy, good_job, pr_author="bob"
    )
    assert any(
        "owner" in e.lower() and "alice" in e and "bob" in e for e in errs
    ), errs


def test_owner_check_skipped_when_no_pr_author(schema_validator, policy, good_job):
    """Local dev runs and push-to-main: no PR author context → skip check."""
    good_job["metadata"]["owner"] = "anyone-at-all"
    # No pr_author passed; should still validate clean (other fields are fine).
    assert errors_from(schema_validator, policy, good_job, pr_author=None) == []
    assert errors_from(schema_validator, policy, good_job, pr_author="") == []
