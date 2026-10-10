# Pharos GitLab Mirror Runbook

**GitLab repo:** https://gitlab.cee.redhat.com/sp-resilience-team/ai/pharos  
**Source (GitHub):** https://github.com/spre-sre/pharos (branch: `main`)  
**Jira:** SPRE-6925  
**Mirror direction:** GitHub → GitLab (pull mirror, read-only)

---

## How it works

GitLab's built-in pull mirror polls `https://github.com/spre-sre/pharos.git` and fast-forwards `main` on the GitLab side. Only `main` is mirrored (`only_mirror_protected_branches: true`; `main` is the sole protected branch). Mirror updates bypass push protection.

The mirror runs as the user whose PAT was used to configure it (mirror_user_id is tied to that account). Branch protection is set to push = mirror user only, merge = No one. See SPRE-7016 for the follow-up to replace this with a dedicated service account.

Default sync interval: ~5 minutes (GitLab CE/EE default for pull mirrors).

---

## Check sync status

**Via GitLab UI:**  
Settings → Repository → Mirroring repositories → check "Last update" and status icon.

**Via API:**
```bash
export GITLAB_API_URL=https://gitlab.cee.redhat.com/api/v4
export GITLAB_PERSONAL_ACCESS_TOKEN=<your-pat>
curl -s "${GITLAB_API_URL}/projects/345992" \
  -H "PRIVATE-TOKEN: ${GITLAB_PERSONAL_ACCESS_TOKEN}" | \
  python3 -c "
import sys, json; d = json.load(sys.stdin)
print('last_update_at:', d.get('mirror_last_update_at'))
print('last_success:', d.get('mirror_last_successful_update_at'))
print('status:', d.get('mirror_last_update_status'))
print('import_error:', d.get('import_error'))
"
```

**Healthy output:**
```
last_update_at: 2026-10-09T...
last_success:   2026-10-09T...
status:         finished
import_error:   None
```

---

## Force an immediate sync

```bash
export GITLAB_API_URL=https://gitlab.cee.redhat.com/api/v4
export GITLAB_PERSONAL_ACCESS_TOKEN=<your-pat>
curl -s -X POST "${GITLAB_API_URL}/projects/345992/mirror/pull" \
  -H "PRIVATE-TOKEN: ${GITLAB_PERSONAL_ACCESS_TOKEN}"
# Returns 200 on success; sync runs async — check status ~30s later
```

**Via GitLab UI:**  
Settings → Repository → Mirroring repositories → click the refresh icon.

---

## Recover from divergence

`mirror_overwrites_diverged_branches: false` — divergence causes mirror failure (alert), not silent reset.

**Signs:** `mirror_last_update_status: failed`, `import_error` contains "diverged".

**Recovery:**
1. No human can push/merge to `main` (push = mirror user only, merge = No one). Divergence should not occur normally. Investigate if a maintainer temporarily changed the protection.
2. To force-reset GitLab `main` to match GitHub (after confirming GitHub is authoritative):

```bash
export GITLAB_API_URL=https://gitlab.cee.redhat.com/api/v4
export GITLAB_PERSONAL_ACCESS_TOKEN=<your-pat>
# Temporarily allow overwrite
curl -s -X PUT "${GITLAB_API_URL}/projects/345992" \
  -H "PRIVATE-TOKEN: ${GITLAB_PERSONAL_ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"mirror_overwrites_diverged_branches": true}'
# Trigger sync
curl -s -X POST "${GITLAB_API_URL}/projects/345992/mirror/pull" \
  -H "PRIVATE-TOKEN: ${GITLAB_PERSONAL_ACCESS_TOKEN}"
# Restore safe setting after sync succeeds
curl -s -X PUT "${GITLAB_API_URL}/projects/345992" \
  -H "PRIVATE-TOKEN: ${GITLAB_PERSONAL_ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"mirror_overwrites_diverged_branches": false}'
```

---

## Platform issue: mirror fails with "not allowed to push to protected branch"

**Symptom:** Mirror status shows failed, `import_error` contains:
```
GitLab: You are not allowed to push code to protected branches on this project.
```

**Root cause:** On gitlab.cee.redhat.com (self-hosted), the pull mirror bot does not bypass branch protection when `push_access_level = 0 (No one)`. The mirror runs as the user who configured it. Setting push = No one blocks the mirror itself.

**Fix:** Protect `main` with push = mirror user only instead of No one. First find the mirror user's GitLab user ID:

```bash
export GITLAB_API_URL=https://gitlab.cee.redhat.com/api/v4
export GITLAB_PERSONAL_ACCESS_TOKEN=<your-pat>
# Find mirror user ID (replace <mirror-username> with the actual username)
curl -s "${GITLAB_API_URL}/users?username=<mirror-username>" \
  -H "PRIVATE-TOKEN: ${GITLAB_PERSONAL_ACCESS_TOKEN}" | python3 -c "
import sys, json; d = json.load(sys.stdin)
print('user_id:', d[0]['id'])
"
```

Then apply the branch protection (replace `<mirror-user-id>` with the ID from above):

```bash
export GITLAB_API_URL=https://gitlab.cee.redhat.com/api/v4
export GITLAB_PERSONAL_ACCESS_TOKEN=<your-pat>
# Remove current protection
curl -s -o /dev/null -w "%{http_code}" -X DELETE \
  "${GITLAB_API_URL}/projects/345992/protected_branches/main" \
  -H "PRIVATE-TOKEN: ${GITLAB_PERSONAL_ACCESS_TOKEN}"
# Re-protect: push = mirror user only, merge = No one
curl -s -X POST \
  "${GITLAB_API_URL}/projects/345992/protected_branches" \
  -H "PRIVATE-TOKEN: ${GITLAB_PERSONAL_ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"name":"main","allowed_to_push":[{"user_id":<mirror-user-id>}],"merge_access_level":0,"allow_force_push":false}'
```

Then force a sync to confirm it works.

---

## Platform issue: PaC commit-author block on gitlab.cee.redhat.com

**Symptom:** Component event shows repeated errors:
```
Author 'konflux@no-reply.konflux-ci.dev' is not a member of team
```
Konflux retries every ~15 minutes and fails each time.

**Root cause:** gitlab.cee.redhat.com requires commit authors to be registered LDAP members. Konflux PaC uses `konflux@no-reply.konflux-ci.dev` as the commit author when writing `.tekton/` onboarding files. This email is not an LDAP user and cannot be added as a project member.

**Fix:** Set the `configure-pac-no-mr` annotation on the Component to tell Konflux to skip the onboarding MR entirely:

```bash
oc annotate component pharos -n spre-tenant \
  build.appstudio.openshift.io/request=configure-pac-no-mr \
  --overwrite
```

Konflux will report "Pipelines as Code configuration is up to date" and stop retrying. The annotation cannot be set via the Konflux UI — CLI only.

**Verify:**
```bash
oc get component pharos -n spre-tenant \
  -o jsonpath='{.metadata.annotations.build\.appstudio\.openshift\.io/request}'
# Expected: configure-pac-no-mr
```

**For `.tekton/` files:** Add them via a GitHub PR on `spre-sre/pharos`. The mirror syncs them to GitLab `main` and PaC's push webhook triggers the build automatically. Never commit `.tekton/` changes directly to GitLab — the mirror will overwrite them.

---

## Sync lag measurements

| Date | GitHub merge SHA | Appeared on GitLab | Lag |
|---|---|---|---|
| 2026-10-05 | cf2049a0 (initial manual trigger) | 2026-10-05 ~23:25 UTC | ~10s (manual trigger) |
| 2026-10-09 | 4b5d6ba8 (PR #25 — .tekton/ add) | 2026-10-09 ~08:55 UTC | ~10s (scheduled sync) |

**Conclusion:** Scheduled mirror sync lag is consistently ~10s. No GitHub Action trigger needed.

**If lag ever exceeds 30 minutes:** Create a GitHub Action in `spre-sre/pharos` that calls the GitLab API to trigger an immediate pull on every push to `main`:

```yaml
# .github/workflows/sync-gitlab-mirror.yml
name: Trigger GitLab mirror sync
on:
  push:
    branches: [main]
jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger GitLab pull mirror
        run: |
          curl -s -X POST \
            "https://gitlab.cee.redhat.com/api/v4/projects/345992/mirror/pull" \
            -H "PRIVATE-TOKEN: ${{ secrets.GITLAB_MIRROR_TOKEN }}"
```

Store a GitLab PAT with `api` scope as the `GITLAB_MIRROR_TOKEN` secret in `spre-sre/pharos`.

---

## Key settings

| Setting | Value |
|---|---|
| GitLab project ID | 345992 |
| Source URL | `https://github.com/spre-sre/pharos.git` |
| Mirror type | Pull (GitHub → GitLab) |
| Mirror user | Configured as the user who set up the mirror — SPRE-7016 tracks replacing with a dedicated service account |
| Branches mirrored | `main` only (`only_mirror_protected_branches: true`) |
| Overwrite diverged | `false` (alert, don't silently reset) |
| `main` push access | Mirror user only (see SPRE-7016) |
| `main` merge access | No one |
| Force push | Disabled |
| PaC config | `configure-pac-no-mr` annotation on Component (see SPRE-6925 for full context) |
| Konflux build SA | `build-pipeline-pharos` in `spre-tenant` |
