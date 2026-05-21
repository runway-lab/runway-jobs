<!--
Thanks for submitting a Runway job. A reviewer/admin must approve before any
agent will pick this up. The validate workflow MUST pass before merge.
-->

## Job

- Run ID / file: `jobs/<run_id>.yaml`
- Owner: @
- Eligible backends:

## Resources

- GPUs:
- GPU type:
- Max hours:

## Notes for reviewer

<!-- Why this job, anything unusual about the workload, dataset size, etc. -->

## Checklist

- [ ] Spec lives under `jobs/` only (no edits to `policies/`, `schemas/`, `scripts/`, `.github/`)
- [ ] Uses `${ARTIFACTS_BUCKET}` (or other approved placeholder) — no internal bucket/host names
- [ ] `validate` workflow passes
