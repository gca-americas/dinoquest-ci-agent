---
name: ci-dinoquest
description: >
  Agentic CI pipeline for DinoQuest. Reads the GitHub PR context to understand
  what changed, scopes tests intelligently (frontend-only, backend-only, or full),
  builds the Docker image via Cloud Build, pushes to Artifact Registry, interprets
  failures with LLM reasoning, and posts a commit status + PR comment back to GitHub.
---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `GITHUB_OWNER` | `weimeilin79` | GitHub repository owner |
| `GITHUB_REPO` | `dinoquest-io` | GitHub repository name |
| `REGION` | `us-central1` | GCP region and Artifact Registry location prefix |
| `ARTIFACT_REGISTRY_REPO` | `dinoquest` | Artifact Registry repository name |
| `TARGET_BRANCH` | `main` | Target (base) branch |

---

## Overview

This skill runs the CI pipeline for DinoQuest agentically. Unlike a static YAML pipeline that
always runs every step, this skill **reads what actually changed** and makes decisions:

- Touched only `frontend/`? → skip backend tests, run type-check only
- Touched only `backend/main.py`? → skip frontend lint, run pytest only
- Touched `Dockerfile` or `requirements.txt`? → full build + both test suites
- PR title contains "hotfix"? → fast-track, skip slow checks, flag for canary fast-lane
- PR touches auth, API keys, or CORS config? → add security scan step automatically

---

## Step 1: Resolve GCP Project and Show Pipeline Preview

PROJECT_ID is available from environment. Output a pipeline flow diagram showing all
steps as pending so the caller can see what is coming:

```
  DinoQuest CI Pipeline
  ══════════════════════════════════════════════════

  ┌─────────────────────────────┐
  │  Read GitHub PR Context     │
  │  Detect Changed Files       │
  │  Generate PR Summary        │
  └──────────────┬──────────────┘
                 │
                 ▼
  ┌─────────────────────────────┐
  │     Classify Change Scope   │
  │   FRONTEND · BACKEND · FULL │
  └──────────────┬──────────────┘
                 │
                 ▼
  ┌─────────────────────────────┐
  │      Security Scan          │  ← in-agent, scans diff text
  │  • Hardcoded secrets check  │  → blocks CI if found
  │  • Model Armor advisory     │  → warning only, never blocks
  └──────────────┬──────────────┘
                 │
                 ▼
  ┌─────────────────────────────┐
  │     Backend pytest          │  ← BACKEND or FULL scope only
  │  pytest backend/tests/ -v   │  → own Cloud Build job, no Docker push
  └──────────────┬──────────────┘
                 │
                 ▼
  ┌──────────────────────────────────────────┐
  │              Cloud Build                 │
  │  docker build && push image              │
  └──────────────────┬───────────────────────┘
                     │
                     ▼
  ┌─────────────────────────────┐
  │     Artifact Registry       │
  │     (verify image tag)      │
  └──────────────┬──────────────┘
                 │
                 ▼
  ┌─────────────────────────────┐
  │   Post to GitHub PR         │
  │   Commit status + Report    │
  └─────────────────────────────┘

  ══════════════════════════════════════════════════
```

---

## Step 2: Read GitHub PR Context and Get Changed Files

All repository context comes from the GitHub API — no local git checkout.

### 2a: Get PR details

If a `pr_number` was provided in the request, call `get_pr(pr_number)` to retrieve:
- `BRANCH` — the PR's head branch name
- `COMMIT_SHA` — the PR's head SHA
- `PR_TITLE` — used for fast-track and scope decisions
- `PR_BODY` — for context

If no `pr_number` was provided but a `branch_name` was, call `list_prs(branch_name)` to
find the open PR for that branch. Use the first result.

### 2b: Get changed files

Call `get_pr_files(pr_number)` to retrieve the full list of changed filenames.
Store as `CHANGED_FILES` — used for scope classification in Step 3.

### 2c: Create PR if needed, then enrich body from diff

**Never ask the user for a PR title or description.** Derive everything autonomously.

**If no open PR exists for this branch:**

**Step 1 — Create PR with initial title from branch name:**
- `title`: one sentence, ≤72 chars, conventional-commits prefix.
  Derive from branch name: `level_2` → `"feat: add Level 2 gameplay and backend changes"`,
  `fix-oom` → `"fix: resolve OOM in leaderboard endpoint"`.
- `body`: placeholder — `"CI pipeline initialising — diff summary will be added shortly."`
- Call `create_pr(title, body, head=BRANCH, base="main")` → captures `PR_NUMBER` and `COMMIT_SHA`.

**Step 2 — MANDATORY: Fetch the diff and post rich PR summary.**
Immediately after `create_pr` (or after finding the existing PR), call `scan_pr_diff(PR_NUMBER)`.
Then IMMEDIATELY call `post_pr_comment(PR_NUMBER, body)` with the rich summary below.
**Do NOT skip this step. Do NOT proceed to Step 3 until both tool calls are done.**

Post this exact structure as the PR comment:

```
## PR Summary

<3-5 bullet points from the actual diff — name functions, endpoints, config keys,
dependency versions, game mechanics added/changed. Be specific, not generic.>

## Changed Files
| File | Change | Description |
|------|--------|-------------|
<one row per file: filename | added/modified/deleted | one-line description of what changed>

## Test Plan
<what CI will run: frontend lint / backend pytest / full build — derived from file paths>

## Risk
<LOW / MEDIUM / HIGH> — <one sentence explaining why>
```

If one already exists (409 / duplicate error from `create_pr`), use the existing PR from 2a
and still call `scan_pr_diff` + `post_pr_comment` to post the rich summary.

Capture:
- `PR_NUMBER` — for later comment posting
- `COMMIT_SHA` — for status posting

---

## Step 3: Classify Change Scope (Agentic Decision)

Analyze `CHANGED_FILES` and reason about scope. Do NOT submit all tests blindly.

| Changed paths | Scope | Tests to include in Cloud Build |
|---|---|---|
| Only `frontend/**` | FRONTEND | TypeScript compile check only |
| Only `backend/**` | BACKEND | pytest only (if tests exist) |
| `Dockerfile`, `requirements.txt`, or both paths | FULL | Both test suites + build |
| `frontend/src/services/geminiService.ts` | FRONTEND + API | Type-check + Gemini prompt format |
| Any file with "auth", "firebase", "cors", or "key" in path | SECURITY FLAG | Add security scan phase |
| `backend/main.py` changed (always) | SECURITY FLAG | Contains Gemini endpoint — always scan |

Record the scope decision and emit it in the final report with the reasoning.

**Agentic check — PR intent:**
- Title contains "hotfix" or "fix crash" → set `FAST_TRACK=true`, note in report
- Title contains "feat" or "level" → set `FAST_TRACK=false`, full checks required

### Visualize the scoped pipeline

After determining scope, output a plain-text pipeline diagram showing each step,
whether it will RUN or SKIP, and a one-line reason. Use this format:

```
DinoQuest CI Pipeline — <SCOPE> scope
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  PR context read         ✔  <N> files changed → <scope>
         │
         ▼
  Scope Decision          ✔  FULL + SECURITY FLAG
         │
    ┌────┼────┐
    ▼    ▼    ▼
[✔] Frontend Lint         RUN   — frontend/** changed
[✔] Backend pytest        RUN   — backend/main.py changed
[✔] Security Scan         RUN   — SECURITY FLAG raised
    └────┬────┘
         │
         ▼
  Cloud Build             RUN   — gcloud builds submit
         │
         ▼
  Artifact Registry       RUN   — verify image push
         │
         ▼
  Post to GitHub PR       RUN   — commit status + CI report comment
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**Proceed automatically after outputting this diagram.** No user confirmation needed.

---

## Step 4: Security Scan (Conditional — SECURITY FLAG scope only)

If Step 3 raised a SECURITY FLAG, run a targeted scan across two areas before
submitting to Cloud Build.

### 4a — Hardcoded Secrets

Call `scan_pr_diff(pr_number)` to get the raw diff. Check for:

```
Patterns to flag: (api_key|secret|password|token)\s*=
Exclude safe patterns: os\.getenv, process\.env, getenv
```

If any matches are found → **fail CI immediately.** Call `post_commit_status` with
`state=failure` and description "Potential hardcoded credential detected — review before merging."
Then call `post_pr_comment` with the specific lines found. Stop.

Also check: if `allow_origins=["*"]` appears in the diff → fail CI with security note.

### 4b — Prompt Injection Check (Advisory Only)

Use the `scan_pr_diff` output already fetched in 4a to check:
1. Does `backend/main.py` in the diff call `check_prompt_safety` before the Gemini API call?
2. Does `backend/requirements.txt` in the diff include `google-cloud-modelarmor`?

**This check is advisory — it never blocks CI.** Record the result (WIRED / NOT WIRED) in
the CI report under "Security Scan". Do not modify any files. Do not stop the pipeline.

**Always proceed to Step 5 after Step 4, regardless of Model Armor status.**

---

## Step 5: Run Backend Tests, Then Build via Cloud Build

### 5a — Backend pytest (outside Cloud Build)

If scope is `BACKEND` or `FULL`, call `run_ci_backend_tests(branch_name)` to run
`pytest backend/tests/ -v` against the PR branch source.

- If tests **fail** → call `post_commit_status(sha, state="failure", description="Backend tests failed")`,
  call `post_pr_comment` with the failure output, and stop. Do not submit to Cloud Build.
- If tests **pass** (or scope is `FRONTEND`) → proceed to 5b.

### 5b — Cloud Build (Docker build + push)

Call `submit_build(commit_sha, branch_name, test_scope)`.
The tool constructs the image URI from server-side configuration. Never pass an image URI yourself.

The build steps executed inside Cloud Build:

| Scope | Step |
|---|---|
| `BACKEND` | `docker build && push` |
| `FRONTEND` | `docker build && push` |
| `FULL` | `docker build && push` |

If the Cloud Build step exits non-zero, Docker push is skipped.

Poll for build completion by calling `get_ci_build_status(build_id)` every 30 seconds.
Time out after 15 minutes.

**If build fails:** use your reasoning to classify the failure from the error message and log:
- `PERMISSION_DENIED` / `403` / `does not have permission` → IAM infrastructure error, name the missing permission
- `npm install` / `yarn` failure → dependency issue, name the failing package
- `pip install` failure → Python package conflict, name the incompatible version
- `COPY` / `ADD` failure → missing file in repo, name the file
- Transient registry error (503/429) → retry once before failing
- Test failure → describe the failing test, the assertion, and the suggested fix

For all failures: post a clear diagnosis as the PR comment. Do not contact any external agent.

---

## Step 6: Verify Image in Artifact Registry

After a successful build, call `verify_image(image_tag)` to confirm the image was pushed.
Confirm `found: true`. Record the full image URI for use by CDAgent.

---

## Step 7: Post Status to GitHub

**MANDATORY: Always call `post_commit_status` FIRST — before anything else in this step.**
This overwrites any previous run's stale commit status. Never skip it regardless of outcome.

**On success:**
Call `post_commit_status(sha, state="success", description="CI passed — image pushed to Artifact Registry")`

**On failure:**
Call `post_commit_status(sha, state="failure", description="<short reason>")`

**MANDATORY: Then call `post_pr_comment(pr_number, body)` with the full CI report below.**
**Do NOT skip either call. Both are required on every CI run.**

---

## Step 8: Emit CI Report

Post the following as the PR comment via `post_pr_comment`. Use the same content as your
final Slack reply text. Fill in ticks (✅) or crosses (❌) for each step based on actual results.

```
## DinoQuest CI Report

**PR:** #<number> — <title>
**Branch:** `<branch>` → `main`
**Commit:** `<short-sha>`

---

### Pipeline Results

```
DinoQuest CI Pipeline — <SCOPE> scope
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Read PR context         <✅/❌>  <N> files changed → <scope>
         │
         ▼
  Scope decision          <✅/❌>  <SCOPE> <+ SECURITY FLAG if raised>
         │
    ┌────┼────┐
    ▼    ▼    ▼
[<✅/⏭️>] PR diff summary     <RUN/SKIP>  — <reason>
[<✅/⏭️>] Security scan        <RUN/SKIP>  — <reason>
[<✅/❌/⏭️>] Backend pytest     <PASS/FAIL/SKIP> — <reason>
    └────┬────┘
         │
         ▼
  Cloud Build             <✅/❌>  build_id: <id>
  • Docker build & push   <✅/❌>
         │
         ▼
  Artifact Registry       <✅/❌>  image verified
         │
         ▼
  GitHub commit status    <✅/❌>  <success/failure>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

### Change Scope
- **Files changed:** <N>
- **Scope:** `<FRONTEND / BACKEND / FULL>`
- **Fast-track:** <YES / NO>
- **Security flag:** <YES / NO>
- **Reasoning:** <1-2 sentences>

### Security Scan
- Hardcoded secrets: <CLEAN / FLAGGED>
- Model Armor: <WIRED / NOT WIRED> _(advisory only)_

### Build
- **Cloud Build ID:** `<id>`
- **Image:** `<full image URI>`
- **Status:** <SUCCESS ✅ / FAILED ❌>

<### Failure Analysis _(only if failed)_
<LLM interpretation of what failed, why, and suggested fix>>

---

**Overall: <PASSED ✅ / FAILED ❌>**
```

After posting the PR comment, output the same pipeline diagram as your reply text so it
appears in Slack. Use ✅ ❌ ⏭️ for each step.

---

## Skill Teaching (Incoming A2A or Chat Message)

When you receive a message whose intent is to teach you a new CI check — from a human
via Slack/Chat — follow this workflow instead of the normal CI pipeline flow.

### Recognising a teaching message

The message will contain at minimum:
- A **bug or failure description** (what went wrong in production or testing)
- A **detection pattern** (what to look for in a future PR diff)
- A **test or action** to add to the CI suite

DinoAgent typically sends: `"skill_name, description, detection_pattern, test_code, action"`.
A human might phrase it more loosely: *"add a test that asserts coins never exceed 100"*.

### Teaching workflow

1. **Register the skill** — call `ci_register_skill(skill_name, description, detection_pattern, action)`.
   This emits the `skill_registered` event so the theater shows the sparkle animation.

2. **Create a branch** — call `create_branch("ci-skill-<skill_name>")`.
   Use only lowercase letters, numbers, and hyphens in the skill name portion.
   The tool automatically appends a `YY_MM_DD_HH_MM` timestamp — use the `branch` field
   from the returned JSON in all subsequent `write_file` and `create_pr` calls.

3. **Write the test file** — call `write_file` with:
   - `path`: `backend/tests/test_<skill_name>.py`
   - `content`: a self-contained pytest module with one or more test cases that would
     have caught the described bug. Follow the style in `backend/tests/test_main.py`:
     use `client_no_mock` for endpoint tests, plain `assert` statements, no mocking
     unless Gemini is called.
   - `commit_message`: `"ci: add regression test for <skill_name> (taught by DinoAgent)"`
   - `branch`: the branch created in step 2

4. **Open a PR** — call `create_pr` with:
   - `head`: the branch from step 2
   - `base`: `"main"`
   - `title`: `"ci: regression test — <skill_name>"`
   - `body`: include the bug description, detection pattern, and a note that this was
     auto-generated from a DinoAgent teaching.

5. **Reply** with the PR URL and a one-line summary of what test was added.

### Test style guide

- File: `backend/tests/test_<skill_name>.py`
- Class: `Test{SkillNameCamelCase}`
- Each method tests one specific boundary or failure mode from the taught pattern
- Use `client_no_mock` fixture for endpoints that don't call Gemini
- Assert both the happy path (valid input passes) and the failure path (bad input is rejected)

Example — if taught "dino coins must not exceed 100":
```python
class TestCoinCap:
    def test_coins_within_cap(self, client_no_mock):
        resp = client_no_mock.post("/api/log/game_end", json={
            "dino_type": "Speedy", "dino_name": "Zippy",
            "score": 500, "coins": 100, "won": True, "speed": 8.0,
        })
        assert resp.status_code == 200

    def test_coins_exceed_cap_returns_422(self, client_no_mock):
        resp = client_no_mock.post("/api/log/game_end", json={
            "dino_type": "Speedy", "dino_name": "Zippy",
            "score": 500, "coins": 999, "won": True, "speed": 8.0,
        })
        assert resp.status_code == 422
```

---

## Edge Cases

- **No PR for this branch**: If `list_prs` returns nothing, create one via `create_pr`.
- **Already on main**: Skip PR creation — CI on main should not auto-file PRs. Run build and report directly.
- **`GEMINI_API_KEY` missing**: Backend tests will fail with `ValueError`. Note this in the report as a config issue, not a code bug. Do not fail CI — flag as warning only.
- **Cloud Build quota exceeded**: Report the quota error and retry once after 60 seconds.
- **TypeScript errors in unchanged files**: Flag as warning, do not block CI.
- **Build takes >10 min**: Poll every 30s. If >15 min, cancel and report timeout.
- **Artifact Registry repo doesn't exist**: Report the error as an infrastructure issue. Post a failure commit status and PR comment explaining the repo must be created manually before CI can push images.
