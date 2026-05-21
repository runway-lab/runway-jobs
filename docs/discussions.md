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

The initial control plane is a GitHub organization and private repository:

- Org: `runway-lab`
- Repo: `runway-lab/runway-jobs`
- CLI name: `runway`, abbreviated `rwy`

Initial repository structure:

```text
jobs/              # submitted job specs
policies/          # limits and allowed backends/repos/images
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

Current GitHub Free private repository limitation:

- Branch protection is not available on private repos without GitHub Pro/Team.
- Therefore CODEOWNERS and required checks cannot be enforced at the GitHub
  branch level.

MVP workaround:

- Agents must enforce approval themselves.
- Do not run every file on `main`.
- Run only jobs with an explicit approval marker/comment from an authorized
  reviewer/admin.
- Agent must re-run validation locally before execution.

Recommended Free-plan MVP flow:

```text
intern opens issue or PR with job YAML
GitHub Action validates schema/policy
reviewer/admin comments /rwy approve
agent checks:
  - approver is in runway-reviewers or runway-admins
  - validation passed
  - files/spec are within allowed scope
  - job policy passes locally
agent executes approved candidate
agent writes status back to GitHub
```

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

1. Decide whether MVP submission uses issues or PRs.
2. Build `rwy` CLI and `rwy-agent`.
3. Implement GitHub polling/status adapter.
4. Implement fake backend adapter and tests.
5. Add GCP backend using SkyPilot.
6. Add Slurm backend using `sbatch`/`squeue`/`scancel`.
7. Add SSH backend using `nvidia-smi` polling and `flock`.
8. Define artifact storage and run ID conventions.
9. Define progress metric format for ETA.
10. Decide whether to upgrade GitHub plan for branch protection later.
