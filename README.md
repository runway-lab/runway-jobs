# runway-jobs

Job registry and approval surface for Runway (`rwy`).

This repository is the control plane for experiment requests. It should not
store cloud, Slurm, SSH, or agent credentials. Agents poll approved job specs
from this repo and execute them from machines that already have the relevant
infra access.

## Layout

- `jobs/`: submitted job specs.
- `policies/`: limits for GPUs, runtime, repositories, images, and backends.
- `schemas/`: JSON schemas for job specs.
- `scripts/`: validation helpers used by GitHub Actions.
- `examples/`: valid examples for users to copy.

## Flow

1. Intern opens a PR adding `jobs/<run_id>.yaml`.
2. GitHub Actions validates schema and policy.
3. Reviewer approves/merges.
4. `rwy-agent` processes approved jobs for its backend.
5. Agents write status/ETA/log links back to the registry.

## Local Validation

```bash
python -m pip install pyyaml jsonschema
python scripts/validate_jobs.py
```
