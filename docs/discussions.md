# Runway Discussion Notes

This document summarizes the design decisions and operational findings from the
initial Runway (`rwy`) planning discussion.

## Problem

We want one entry point for GPU experiments across multiple environments:

- Company GCP, currently accessed through SkyPilot.
- School Slurm, accessed from a different machine/account.
- Local or lab SSH GPU servers, possibly without sudo/root.

The desired workflow is:

1. A user submits one experiment spec.
2. Runway tries eligible backends.
3. Backends that need to queue keep queuing.
4. Backends that can start begin profiling.
5. Runway estimates completion time and keeps the candidate with the earliest
   ETA.
6. Other queued or running candidates are cancelled according to policy.

## SkyPilot Findings

SkyPilot remains a good backend for company GCP, but it is not a complete
solution for all environments.

Useful distinctions:

- `sky gpus list` is a catalog/pricing view. It does not prove quota or live
  capacity.
- `sky launch --dryrun` validates planning/catalog feasibility. It does not
  reserve resources or test live capacity.
- GCP `quotaExceeded` means quota. `ZONE_RESOURCE_POOL_EXHAUSTED` /
  `insufficientCapacity` means live stock/capacity.
- Real capacity can only be tested by actual provisioning.
- Managed jobs are a better fit than a local retry loop for queued experiments,
  because the controller can keep retrying without a local terminal process.

Observed A100 probe:

- Shape: `A100-80GB:8`
- Infra: `gcp/us-central1`
- Instance type: `a2-ultragpu-8g`
- Manual one-hour probe did not acquire capacity.
- Managed job probe also stayed pending after more than two hours.
- Repeated failures were `ZONE_RESOURCE_POOL_EXHAUSTED` in `us-central1-a` and
  `us-central1-c`, not quota errors.

Observed H100/H100-MEGA notes:

- `H100-MEGA` maps to GCP A3 Mega (`a3-megagpu-8g`) and is separate from normal
  H100 quota.
- H100 family quota is exposed through the newer
  `GPUS-PER-GPU-FAMILY-per-project-region` quota with dimensions
  `gpu_family` and `region`.
- A100 quota is still visible through the older regional quota table such as
  `NVIDIA_A100_GPUS` and `NVIDIA_A100_80GB_GPUS`.

## On-Prem and SSH

SkyPilot "on-prem" support is not the same as arbitrary non-root SSH execution.

There are two practical SkyPilot modes:

- Existing Kubernetes cluster: SkyPilot can use an already-managed K8s context.
- SSH Node Pool: SkyPilot SSHes into machines and bootstraps a Kubernetes pool.

SSH Node Pools usually require passwordless sudo because SkyPilot needs to
install/configure Kubernetes, container runtime, networking, and sometimes GPU
operator components.

Conclusion:

- Existing K8s cluster: good SkyPilot target.
- SSH with passwordless sudo: possible SkyPilot SSH Node Pool target.
- SSH without sudo: do not use SkyPilot as the executor; use a lightweight SSH
  backend with `nvidia-smi` polling, `flock`, `nohup`/`tmux`, and log collection.

## Proposed System

Runway should be a gateway, not a replacement for every backend scheduler.

High-level architecture:

```text
rwy submit exp.yaml
  -> control plane stores requested experiment
  -> GCP agent submits via SkyPilot
  -> Slurm agent submits via sbatch
  -> SSH agent polls nvidia-smi and starts when a GPU is available
  -> agents report status/progress/ETA
  -> selector chooses earliest estimated completion
  -> agents cancel losing candidates
```

Each backend keeps its native execution mechanism:

- GCP: `sky jobs launch`, `sky jobs queue`, `sky jobs cancel`, `sky jobs logs`
- Slurm: `sbatch`, `squeue`, `scancel`, log files
- SSH: `ssh`, `nvidia-smi`, `flock`, `nohup`/`tmux`, process cleanup

## Pull-Based Agents

Company GCP and school Slurm may be reachable only from different machines.
Credentials should not be copied between machines.

Use pull-based agents:

```text
company machine agent -> GitHub/control plane: any GCP jobs?
school machine agent  -> GitHub/control plane: any Slurm jobs?
SSH machine agent     -> GitHub/control plane: any SSH jobs?
```

The control plane never connects inbound to laptops or login nodes. Agents are
long-running local processes that poll over outbound HTTPS.

Benefits:

- GCP credentials remain on the company-approved machine.
- School Slurm/SSH credentials remain on the school/personal side.
- No inbound network access to laptops is required.
- Agents can be stopped, restarted, and audited independently.

## ETA-Based Selection

The scheduler should not simply choose the first backend that reaches
`RUNNING`. A later-starting backend can still finish earlier.

Use speculative execution plus profiling:

```text
SUBMITTED -> QUEUED -> STARTING -> PROFILING -> RUNNING_CANDIDATE
                                      -> WINNER
                                      -> CANCELLED_LOSER
```

ETA calculation requires machine-readable progress from the training job, for
example JSON lines:

```json
{"step": 1000, "total_steps": 20000, "time": 1770000000}
```

Then:

```text
throughput = profiled_steps / profiling_seconds
remaining = total_steps - current_step
eta_finish = now + remaining / throughput
```

Recommended policy knobs:

```yaml
selection:
  policy: eta
  profile_seconds: 300
  max_concurrent_profiles: 2
  min_eta_improvement_to_switch: 0.15
  cancel_queued_after_winner_margin: 0.25
```

Important artifact rule:

Each candidate must write to a unique path:

```text
runs/<run_id>/candidates/<backend_id>/
runs/<run_id>/winner/
```

Never let multiple candidates write to the same checkpoint or result directory.

## GitHub Control Plane

The initial control plane is a GitHub organization and repository:

- Org: `runway-lab`
- Repo: `runway-lab/runway-jobs`
- CLI name: `runway`, abbreviated `rwy`

Initial repository structure:

```text
jobs/              # submitted job specs
policies/          # limits, placeholder allowlist, leak patterns
schemas/           # JSON schema for job specs
scripts/           # validation helpers
.github/workflows/ # validation workflow
CODEOWNERS
```

Created teams:

```text
runway-admins
runway-reviewers
runway-interns
runway-agents
```

### Decision: public repo, GitHub-enforced gate

GitHub Free only ships branch protection for **public** repositories. The
earlier draft tried to compensate by pushing all approval logic into the
agent (`/rwy approve` comment + agent-side approver verification), which put
the trust root on every agent host.

Switching `runway-jobs` to public is safe because the registry never holds
secrets, internal hostnames, bucket names, or dataset paths — those go
through `${PLACEHOLDER}` substitution at the agent. Public unlocks:

- Branch protection on `main` (no force push, no deletion, linear history).
- Required PR reviews + CODEOWNERS enforcement.
- Required status check from the `validate` workflow.
- Optional required signed commits.

The trust root collapses from "every agent host" to "GitHub branch
protection + admin team". One-time admin setup steps live in
`docs/github-setup.md`.

### Updated flow

```text
intern (not a collaborator) forks → PR with jobs/<run_id>.yaml
GitHub Actions `validate` runs schema + policy lint  [REQUIRED status check]
CODEOWNERS routes review:
  - jobs/                      → runway-reviewers or runway-admins
  - policies/ schemas/ scripts/ .github/ → runway-admins only
reviewer approves; PR merges to main
rwy-agent (per backend host) polls main:
  - fetch a specific commit SHA (not refs/heads/main)
  - re-run policy lint locally against its own copy
  - resolve ${VAR} placeholders from local config
  - execute
  - write status back as commit status / PR comment
```

There is no `/rwy approve` comment flow. Merging to `main` *is* the
approval, enforced by GitHub.

### Policy layer (what the validator enforces)

- `limits`: GPU count, max hours, max profile seconds.
- `allowed_backends`, `allowed_gpu_types`, `allowed_repo_prefixes`.
- `allowed_placeholders`: every `${VAR}` in any string field must be
  declared. Stops accidental injection of unknown agent-side variables.
- `allowed_artifact_uri_prefixes`: artifact URIs must start with one of
  these (placeholders allowed inside the prefix). Prevents
  hard-coded bucket names from landing in the public repo.
- `forbidden_substrings` + `forbidden_string_regex`: scanned against
  **every** string in the spec (not just `run`). Blocks well-known
  credential lookups, metadata endpoints, internal IP ranges, and
  `.corp` / `.internal` / `.intra` hostnames.

### Defense-in-depth on the agent

Even with GitHub enforcing the gate, the agent still:

- Pulls a specific commit SHA, not `refs/heads/main`, to avoid TOCTOU
  between approval and fetch.
- Re-runs the validator from a checked-in copy of `policies/` (does not
  trust the policy file fetched alongside the job).
- Uses a per-host GitHub App installation token scoped to
  `contents:read` + `statuses:write` + `pull_requests:write`. No shared
  PAT.
- Refuses any change that touches files outside `jobs/`.

## Security Rules

Do not store backend credentials in GitHub.

Credentials stay with agents:

- GCP/SkyPilot credentials: company-side agent only.
- Slurm credentials: school-side agent only.
- SSH credentials: SSH-side agent only.
- GitHub token: scoped to polling and status updates.

The job spec should not be allowed to access agent host credentials. Execution
must happen in a controlled environment and logs should redact secrets.

Policy checks should reject or flag commands that try to inspect common secret
locations, credentials, or metadata endpoints.

## Testing Plan

Start with fake backends before using real GPUs.

Fake backend states:

```text
QUEUED -> STARTING -> PROFILING -> RUNNING -> SUCCEEDED
```

Required test categories:

- Intern can submit but cannot approve their own job.
- Reviewer/admin can approve, reject, cancel, and override policy where allowed.
- Agent only runs approved jobs for its own backend.
- Duplicate polling does not duplicate-submit.
- Failed validation prevents execution.
- ETA selector cancels slower candidates.
- Queued candidates are retained or cancelled according to policy.
- Agent restart recovers local job IDs without duplicate submissions.
- Cancel after success is treated as a no-op.
- Logs and status updates do not leak credentials.

Real infra canaries should run in this order:

1. Fake backends only.
2. One real backend plus fake others.
3. GCP cheap CPU job.
4. Slurm CPU or tiny GPU job.
5. SSH single-GPU polling job.
6. Full GCP/Slurm/SSH race with a short GPU workload.

## Immediate Next Steps

1. ~~Decide whether MVP submission uses issues or PRs.~~ — PRs (fork-based)
   so that merge gates approval via branch protection.
2. ~~Decide whether to upgrade GitHub plan for branch protection later.~~ —
   resolved by making the repo public; no plan upgrade needed.
3. Apply the one-time admin steps in `docs/github-setup.md` (visibility,
   team permissions, branch protection, signed commits, GitHub App).
4. Build `rwy` CLI and `rwy-agent`.
5. Implement GitHub polling/status adapter using a GitHub App installation
   token, pulling a specific commit SHA from `main`.
6. Implement fake backend adapter and tests.
7. Add GCP backend using SkyPilot.
8. Add Slurm backend using `sbatch`/`squeue`/`scancel`.
9. Add SSH backend using `nvidia-smi` polling and `flock`.
10. Define artifact storage and run ID conventions (placeholders already in
    place: `${ARTIFACTS_BUCKET}` + `{run_id}` + `{backend_id}`).
11. Define progress metric format for ETA.
