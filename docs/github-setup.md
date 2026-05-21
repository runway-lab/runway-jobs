# GitHub Setup (one-time, admin only)

These steps cannot be done from a normal PR — an org admin must apply them
through the GitHub UI or `gh` CLI. They unlock the permission model that the
rest of the repo assumes.

## 1. Make the repository public

The Free plan only enforces branch protection on public repos. The job specs
in this repo never contain credentials, internal hostnames, dataset paths, or
bucket names — those go through `${PLACEHOLDER}` substitution at the agent. A
public registry is therefore safe and unlocks the GitHub-side gates below.

```bash
gh repo edit runway-lab/runway-jobs --visibility public --accept-visibility-change-consequences
```

## 2. Team permissions on the repo

```bash
gh api -X PUT orgs/runway-lab/teams/runway-admins/repos/runway-lab/runway-jobs    -f permission=admin
gh api -X PUT orgs/runway-lab/teams/runway-reviewers/repos/runway-lab/runway-jobs -f permission=push
gh api -X PUT orgs/runway-lab/teams/runway-agents/repos/runway-lab/runway-jobs    -f permission=pull
# runway-interns: do NOT add as collaborators. They submit via fork + PR.
```

The agents team only needs `pull`; status updates use the agent's GitHub App
token, not collaborator permissions.

## 3. Branch protection on `main`

```bash
gh api -X PUT repos/runway-lab/runway-jobs/branches/main/protection \
  -H "Accept: application/vnd.github+json" \
  -f required_status_checks.strict=true \
  -f 'required_status_checks.contexts[]=schema + policy' \
  -F enforce_admins=true \
  -F required_pull_request_reviews.required_approving_review_count=1 \
  -F required_pull_request_reviews.require_code_owner_reviews=true \
  -F required_pull_request_reviews.dismiss_stale_reviews=true \
  -F restrictions= \
  -F required_linear_history=true \
  -F allow_force_pushes=false \
  -F allow_deletions=false \
  -F required_conversation_resolution=true
```

Settings, in plain English:

- Require PRs into `main` (no direct push, even admins).
- Require the `schema + policy` status check (from `.github/workflows/validate.yml`).
- Require 1 review.
- Require CODEOWNERS to be among the reviewers (so `policies/`, `schemas/`,
  `scripts/`, `.github/` changes need an admin specifically).
- Dismiss stale reviews on new commits.
- No force pushes, no deletions, linear history.
- Admins are included in enforcement.

## 4. (Recommended) Require signed commits

```bash
gh api -X POST repos/runway-lab/runway-jobs/branches/main/protection/required_signatures \
  -H "Accept: application/vnd.github+json"
```

## 5. Agent identity

Create a GitHub App `runway-agent` with the minimum scopes:

- Repository permissions: `contents: read`, `issues: write`, `metadata: read`,
  `pull_requests: write` (for status comments), `statuses: write`.
- Subscribe to events: `push`, `pull_request`, `issue_comment` (only needed
  if/when we re-introduce comment-driven actions; not used in the
  branch-protection-only flow).

Install the App on `runway-lab/runway-jobs` only. Each agent host gets its own
installation token; no shared PAT.

## 6. Verifying the gate

After everything is applied, the following must all be true:

- An intern (not a collaborator) cannot push to `main`. They open a PR from a
  fork.
- The PR cannot merge until `schema + policy` passes.
- The PR cannot merge until a `runway-reviewers` or `runway-admins` member
  approves.
- Edits to `policies/`, `schemas/`, `scripts/`, or `.github/` additionally
  require a `runway-admins` approval (CODEOWNERS).
- Force pushes and branch deletion on `main` are rejected for everyone,
  including admins.
