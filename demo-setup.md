# Demo Setup Guide

This guide covers everything needed to run the full Remediation → CI → CD pipeline in
under 2 minutes by enabling `DEMO_MODE` across all three agents.

---

## What DEMO_MODE skips

| Agent | Real step (slow) | Demo replacement |
|---|---|---|
| **CIAgent** | Cloud Build pytest run (~5 min) | Instant pass |
| **CIAgent** | Cloud Build docker build (~5 min) | Retag pre-built image to `:latest` |
| **CIAgent** | Cloud Build status polling | Instant `SUCCESS` |
| **CIAgent** | Artifact Registry image scan | Instant `found=true` |
| **RemediationAgent** | Local pytest + pip install (~60s) | Instant pass |
| **CDAgent** | Canary metrics polling | Instant `OK` verdict — deploy and traffic shifts run for real |
| **CDAgent** | `write_deployment_pattern` Firestore write | Skipped — emits theater event only, no Firestore pollution |
| **CDAgent** | `create_github_release` | Skipped — returns fake URL, no demo tags in repo |
| **RemediationAgent** | `rollback_traffic` LRO wait | Instant — returns success without waiting for Cloud Run rollout |
| **RemediationAgent** | `clone_repo` git fetch | Uses cached `/tmp/repo_cache` as-is if it exists |

GitHub API calls (PR creation, commit status, PR comments, releases) and Cloud Run
read calls (`get_service`, `list_revisions`) are **not** mocked — they run for real so
the demo shows live data.

---

## Step 1 — Pre-build and pre-tag the two demo images

CIAgent's demo mode **does not run Cloud Build**. Instead it retags a pre-built image
to `:latest` so CDAgent can deploy it. You must have exactly two images ready in
Artifact Registry **before** every demo run.

**Registry:** `us-central1-docker.pkg.dev/io26-keynote-demo-staging/dinoquest`

| Tag | Used when | Source branch |
|---|---|---|
| `app:level_2` | CIAgent processes the `level_2` feature branch PR | `level_2` |
| `app:incident_solution` | CIAgent processes any `incident_*` branch (OOM fix) | `main` (or fixed branch) |

---

### Build image 1 — `app:level_2`

This image represents the `level_2` feature branch. Build it once and re-use across demo runs.

**Option A — via Cloud Build (recommended, no local Docker needed):**
```bash
gcloud builds submit \
  --no-source \
  --project=io26-keynote-demo-staging \
  --region=us-central1 \
  --config=- <<'EOF'
steps:
- name: 'gcr.io/cloud-builders/git'
  args: ['clone', '--depth=1', '--branch=level_2',
         'https://github.com/weimeilin79/dinoquest-io', '/workspace/repo']
- name: 'gcr.io/cloud-builders/docker'
  args: ['build', '-t',
         'us-central1-docker.pkg.dev/io26-keynote-demo-staging/dinoquest/app:level_2',
         '/workspace/repo']
images:
- 'us-central1-docker.pkg.dev/io26-keynote-demo-staging/dinoquest/app:level_2'
EOF
```

**Option B — local Docker build:**
```bash
git clone --depth=1 --branch=level_2 https://github.com/weimeilin79/dinoquest-io /tmp/dinoquest-level2
docker build -t us-central1-docker.pkg.dev/io26-keynote-demo-staging/dinoquest/app:level_2 /tmp/dinoquest-level2
docker push us-central1-docker.pkg.dev/io26-keynote-demo-staging/dinoquest/app:level_2
```

---

### Build image 2 — `app:incident_solution`

This image represents the OOM-fixed codebase. It is deployed when RemediationAgent
opens an `incident_*` PR and CIAgent picks it up.

```bash
git clone --depth=1 --branch=main https://github.com/weimeilin79/dinoquest-io /tmp/dinoquest-main
docker build -t us-central1-docker.pkg.dev/io26-keynote-demo-staging/dinoquest/app:incident_solution /tmp/dinoquest-main
docker push us-central1-docker.pkg.dev/io26-keynote-demo-staging/dinoquest/app:incident_solution
```

Or via Cloud Build:
```bash
gcloud builds submit \
  --no-source \
  --project=io26-keynote-demo-staging \
  --region=us-central1 \
  --config=- <<'EOF'
steps:
- name: 'gcr.io/cloud-builders/git'
  args: ['clone', '--depth=1', '--branch=main',
         'https://github.com/weimeilin79/dinoquest-io', '/workspace/repo']
- name: 'gcr.io/cloud-builders/docker'
  args: ['build', '-t',
         'us-central1-docker.pkg.dev/io26-keynote-demo-staging/dinoquest/app:incident_solution',
         '/workspace/repo']
images:
- 'us-central1-docker.pkg.dev/io26-keynote-demo-staging/dinoquest/app:incident_solution'
EOF
```

---

### Verify both images exist before each demo

```bash
gcloud artifacts docker images list \
  us-central1-docker.pkg.dev/io26-keynote-demo-staging/dinoquest/app \
  --include-tags \
  --project=io26-keynote-demo-staging \
  | grep -E 'level_2|incident_solution'
```

Expected output (two lines, one per tag):
```
us-central1-docker.pkg.dev/...  SHA256:...  level_2          ...
us-central1-docker.pkg.dev/...  SHA256:...  incident_solution ...
```

If either tag is missing, build it before proceeding. The demo will silently fall back
to deploying whatever `:latest` currently is if the retag fails.

---

## Step 2 — Set DEMO_MODE on each Cloud Run service

```bash
# CIAgent
gcloud run services update ci-agent \
  --region=us-central1 \
  --project=io26-keynote-demo-staging \
  --update-env-vars=DEMO_MODE=true

# CDAgent
gcloud run services update cd-agent \
  --region=us-central1 \
  --project=io26-keynote-demo-staging \
  --update-env-vars=DEMO_MODE=true

# RemediationAgent
gcloud run services update remediation-agent \
  --region=us-central1 \
  --project=io26-keynote-demo-staging \
  --update-env-vars=DEMO_MODE=true
```

To turn demo mode off after the demo:

```bash
for svc in ci-agent cd-agent remediation-agent; do
  gcloud run services update $svc \
    --region=us-central1 \
    --project=io26-keynote-demo-staging \
    --update-env-vars=DEMO_MODE=false
done
```

---

## Step 3 — Flush dedup cache (before each demo run)

RemediationAgent deduplicates errors within a 15-minute window. Reset it before each run:

```bash
curl -X POST https://<remediation-agent-url>/flush-dedup
```

---

## Step 4 — Pre-warm instances (optional but recommended)

Cold starts add 10-20s to each agent. Hit the health endpoints to warm them up:

```bash
curl https://<remediation-agent-url>/health
curl https://<ci-agent-url>/health
curl https://<cd-agent-url>/health
```

---

## Demo timing (with DEMO_MODE=true)

| Stage | Approx time |
|---|---|
| OOM alert → RemediationAgent wakes | ~5s |
| Memory bump + repo clone (cached) | ~10s |
| Code fix + commit + open PR | ~15s |
| CIAgent: PR scan + tests + build | ~20s |
| CIAgent: post PR summary | ~5s |
| CDAgent: deploy + canary + promote (real LROs) | ~2 min |
| **Total end-to-end** | **~3 min** |

Without DEMO_MODE, the same pipeline takes 15-20 minutes due to Cloud Build waits.
CDAgent always does a real deploy so the theater shows a live revision change.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| CIAgent build returns `retag failed` | Pre-built image missing | Run Step 1 |
| CDAgent fake revision `demo-XXXXX` causes shift_traffic confusion | Agent tries to look up fake revision | Expected — CDAgent skips all LRO waits in demo mode; theater events still fire |
| RemediationAgent skips tests but still slow | `clone_repo` doing a fresh clone (no cache) | Pre-warm the instance or run one pipeline first to populate `/tmp/repo_cache` |
| Dedup blocks the demo trigger | Didn't flush before run | `curl -X POST .../flush-dedup` |
