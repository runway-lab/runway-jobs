# runway-jobs

Job registry and approval surface for Runway (`rwy`).

This repository is the control plane for experiment requests. It must not
store cloud, Slurm, SSH, or agent credentials. Agents poll approved job specs
from this repo and execute them from machines that already have the relevant
infra access.

The repo is **public** by design — see `docs/github-setup.md` for the
permission model. Job specs never contain bucket names, internal hostnames,
or dataset paths; those go through `${PLACEHOLDER}` substitution on the agent
side.

## Layout

- `jobs/`: submitted job specs (one YAML per run).
- `policies/`: limits, allowed backends, placeholder allowlist, leak patterns.
- `schemas/`: JSON schema for job specs.
- `scripts/`: validation helpers (also used by CI).
- `examples/`: valid example to copy.
- `docs/`: discussion notes and the GitHub admin setup guide.

## Flow

1. Intern forks the repo and opens a PR adding `jobs/<run_id>.yaml`.
2. `validate` workflow runs schema + policy lint. Merge is blocked until it
   passes.
3. CODEOWNERS routes approval to `runway-reviewers` / `runway-admins`.
   Changes to `policies/`, `schemas/`, `scripts/`, `.github/` additionally
   require admin approval.
4. After merge to `main`, `rwy-agent` (running on the relevant backend host)
   picks up the new spec, re-runs validation locally against its checked-in
   policy, resolves `${...}` placeholders from its local config, and
   executes.
5. Agents write status / ETA / log links back as commit statuses or PR
   comments.

There is no `/rwy approve` comment flow. Merge to `main` is the approval —
GitHub branch protection + CODEOWNERS enforces who can do it.

## Placeholders

- `${VAR}` (uppercase): resolved by the agent from its local config before
  execution. Must be declared in `policies/default.yaml`
  → `allowed_placeholders`. The repo never sees the resolved value.
- `{run_id}` and `{backend_id}`: resolved at execution time by the agent.
  Required in `spec.artifacts.uri` so candidates never write to the same
  path.

## Local Validation

```bash
python -m pip install pyyaml jsonschema
python scripts/validate_jobs.py                  # jobs/
python scripts/validate_jobs.py --include-examples  # + examples/
```
