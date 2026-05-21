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
    "required_approving_review_count": 0,
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
- `required_approving_review_count: 0` — no human review needed for paths
  with no CODEOWNER (currently `jobs/`). See section 6 for the auto-merge
  setup that makes this safe.
- `require_code_owner_reviews: true` — CODEOWNED paths (`policies/`,
  `schemas/`, `scripts/`, `.github/`, `tests/`, `CODEOWNERS`) still need
  an admin approval.
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

## 4. Fork-PR workflow approval policy

GitHub gates workflows from fork PRs to prevent supply-chain attacks (a
malicious PR could modify the workflow to exfiltrate secrets). The policy
picks who needs admin approval before the workflow runs:

| `approval_policy` | Auto-run for | Requires approval |
|---|---|---|
| `first_time_contributors_new_to_github` | Almost everyone | Only brand-new GitHub accounts |
| `first_time_contributors` (GitHub default) | Anyone who's had a PR merged here | Everyone else, including org members on first PR |
| `all_external_contributors` (**what we use**) | All org members | All non-org users (random public forks) |

```bash
gh api -X PUT repos/runway-lab/runway-jobs/actions/permissions/fork-pr-contributor-approval \
  -f approval_policy=all_external_contributors
```

So: invite interns into the `runway-lab` org (membership only — no repo
collaborator access, they still fork+PR) and their workflows auto-run.
Random public submitters still need an admin to click "Approve and run".

> Trade-off: trust boundary moves from "merged once before" to "org
> member". Our `validate` workflow only has `contents: read` and no
> secrets, so the supply-chain blast radius from a compromised intern
> account is small. Re-evaluate before any workflow gets write/secrets.

## 5. (Optional) Require signed commits

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

## 6. Auto-merge for `jobs/` PRs

To avoid admin click-through on every intern submission, `jobs/`-only PRs
auto-merge once the `validate` check passes. This relies on three pieces:

1. `CODEOWNERS` does **not** list `/jobs/`. Combined with branch
   protection's `required_approving_review_count = 0` +
   `require_code_owner_reviews = true`, that means:
   - PRs touching only `/jobs/` need no human approval.
   - PRs touching `/policies/`, `/schemas/`, `/scripts/`, `/.github/`,
     `/tests/`, or `/CODEOWNERS` still need an admin (CODEOWNER) approval.

2. `.github/workflows/validate.yml` is the required status check. It runs
   the validator (schema + 11 policy rules + 27 pytest tests). A failing
   validator blocks merge.

3. `.github/workflows/auto-merge-jobs.yml` runs on `pull_request_target`,
   verifies the diff is `jobs/`-only, and calls
   `gh pr merge --auto --squash`. `--auto` queues; GitHub merges as soon as
   the required check is green.

4. **`allow_auto_merge` must be enabled on the repo** — `--auto` is a
   no-op (and errors) otherwise:
   ```bash
   gh api -X PATCH repos/runway-lab/runway-jobs -f allow_auto_merge=true
   ```

### Trust model after auto-merge

| Path | Gate |
|------|------|
| `jobs/*` only | validator (CI) — no human |
| anything else | admin approval via CODEOWNERS |

The validator is now the **only code-layer gate** for job specs. The
agent's local re-validation (using its checked-in copy of `policies/`)
remains the runtime safety net. If a malicious spec passes the validator,
it will also pass the agent's same check — tightening
`forbidden_substrings` / `forbidden_string_regex` (and ideally adding a
`run:` format whitelist later) is how we harden over time.

## 7. Agent identity

Create a GitHub App `runway-agent` with the minimum scopes:

- Repository permissions: `contents: read`, `issues: write`, `metadata: read`,
  `pull_requests: write` (for status comments), `statuses: write`.
- Subscribe to events: `push`, `pull_request`, `issue_comment` (only needed
  if/when we re-introduce comment-driven actions; not used in the
  branch-protection-only flow).

Install the App on `runway-lab/runway-jobs` only. Each agent host gets its own
installation token; no shared PAT.

## 8. Verifying the gate

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
