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
gh api -X PATCH repos/runway-lab/runway-jobs -f visibility=public
```

(Older `gh` versions lack `gh repo edit --visibility`'s confirmation flag; the
API call is non-interactive and works everywhere.)

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
cat <<'JSON' | gh api -X PUT repos/runway-lab/runway-jobs/branches/main/protection \
  -H "Accept: application/vnd.github+json" --input -
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["schema + policy"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "require_code_owner_reviews": true,
    "dismiss_stale_reviews": true
  },
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true
}
JSON
```

(Nested keys via `-f/-F` flat-syntax are unreliable for this endpoint; passing
the full body as JSON is the safest form.)

Settings, in plain English:

- Require PRs into `main` (no direct push).
- Require the `schema + policy` status check (from `.github/workflows/validate.yml`).
- Require 1 review.
- Require CODEOWNERS to be among the reviewers (so `policies/`, `schemas/`,
  `scripts/`, `.github/` changes need an admin specifically).
- Dismiss stale reviews on new commits.
- No force pushes, no deletions, linear history.
- `enforce_admins: false` — admins can use `gh pr merge --admin` to bypass
  the review requirement on their own PRs. **Reason: solo admin.** GitHub
  hard-rule forbids approving your own PR, so with a single admin in
  `runway-admins`, `enforce_admins: true` causes operational deadlock.
  Intern PRs (from forks) still require admin review — the bypass only
  matters when the admin is also the PR author.

> When `runway-admins` grows past one person, flip this back:
> `gh api -X PATCH repos/runway-lab/runway-jobs/branches/main/protection/enforce_admins`.

## 4. (Optional) Require signed commits

Not currently enabled. Signing adds **audit-trail integrity** (the
`Author:` field in `git log` becomes cryptographically attested rather than
just claimed) and gives defense-in-depth if a contributor's PAT leaks but
their signing key does not. It is **not** load-bearing for the approval
gate — that is already enforced by branch protection + CODEOWNERS.

Cost: every contributor and every agent that writes commits must have a
signing key set up, or pushes are rejected. (Merges via GitHub's web UI are
auto-signed by GitHub and unaffected.)

When you are ready to turn it on:

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
