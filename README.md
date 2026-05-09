# CIAgent

Autonomous CI pipeline agent with three entry points:

| Entry point | Caller | Purpose |
|---|---|---|
| `POST /tasks/send` | DinoAgent (A2A) | Automated CI triggered by the orchestrator |
| `POST /slack` | Human via Slack slash command | Direct human trigger or skill teaching |
| `POST /chat` | Human via Google Chat webhook | Direct human trigger or skill teaching |

Submits Docker builds to Cloud Build, polls for completion, verifies the image in
Artifact Registry, and reports to GitHub. When it cannot classify a build failure, it
escalates to DinoAgent for cross-agent skill teaching (Behavior B). Humans can also
teach it patterns directly via Slack or Chat — e.g. "that failure is a flaky test,
retry it" registers a new skill without going through DinoAgent.

## Project structure

```
├── main.py               # Flask entrypoint — A2A task endpoint + Agent Card
├── agent.py              # LlmAgent definition, loads ci-pipeline skill
├── tools.py              # Cloud Build, Artifact Registry, GitHub, A2A call tools
├── utils.py              # emit_event (Pub/Sub) + resolve_secret (Secret Manager)
├── skills/
│   └── ci-pipeline/
│       └── SKILL.md      # Agent playbook — edit to change CI behavior
├── requirements.txt
└── Dockerfile
```

---

## Prerequisites

### 1. Enable GCP APIs

```bash
gcloud services enable \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  pubsub.googleapis.com \
  secretmanager.googleapis.com \
  aiplatform.googleapis.com \
  --project=$PROJECT_ID
```

### 2. Create the service account

```bash
PROJECT_ID=$(gcloud config get-value project)

gcloud iam service-accounts create ci-agent \
  --display-name="CIAgent CI Pipeline" \
  --project=$PROJECT_ID
```

### 3. Grant IAM roles

```bash
SA="ci-agent@${PROJECT_ID}.iam.gserviceaccount.com"


# Submit and read Cloud Build jobs
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA}" --role="roles/cloudbuild.builds.editor" \
  --condition=None

# Read images from Artifact Registry
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA}" --role="roles/artifactregistry.reader" \
  --condition=None

# Call Vertex AI (Gemini model)
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA}" --role="roles/aiplatform.user" \
  --condition=None

# Read secrets (GitHub token)
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA}" --role="roles/secretmanager.secretAccessor" \
  --condition=None

# Publish events to harness-events topic (run after topic is created)
gcloud pubsub topics add-iam-policy-binding harness-events \
  --member="serviceAccount:${SA}" --role="roles/pubsub.publisher" \
  --project=$PROJECT_ID
```

### 4. Connect GitHub repo to Cloud Build

CIAgent submits builds via `repoSource`, which requires the GitHub repo to be
connected to Cloud Build:

```
GCP Console → Cloud Build → Repositories → Connect Repository
Select GitHub → authorize → choose weimeilin79/dinoquest-io
```

### 5. Store the GitHub token in Secret Manager

```bash

gcloud secrets add-iam-policy-binding github-token \
  --member="serviceAccount:${SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project=$PROJECT_ID
```

```
SA="ci-agent@${PROJECT_ID}.iam.gserviceaccount.com"
                                                                                                                     

# ci-agent needs to be a Cloud Build builder
   gcloud projects add-iam-policy-binding $PROJECT_ID \
     --member="serviceAccount:${SA}" \
     --role="roles/cloudbuild.builds.builder" 2>&1

   # ci-agent needs to act as itself when running the build steps
   gcloud iam service-accounts add-iam-policy-binding $SA \
     --member="serviceAccount:${SA}" \
     --role="roles/iam.serviceAccountUser" \
     --project=$PROJECT_ID 2>&1
                                                      
```
Connect GitHub to Cloud Build

  1. Go to Cloud Build → Triggers (make sure you're in project gca-america-virtual-ta-test)                                                 
  2. Click Git Repository
  3. Select GitHub (Cloud Build GitHub App)                                                                                                 
  4. Click Continue — it'll open GitHub OAuth                                                                                               
  5. Authenticate and select the repo weimeilin79/dinoquest-io                                                                              
  6. Check the box to confirm, click Connect                                                                                                
  7. It'll ask if you want to create a trigger — click Skip for now (we submit builds via API, not triggers) 

```
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:ci-agent@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/developerconnect.admin" 
```

---

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | Yes | — | GCP project ID |
| `GOOGLE_GENAI_USE_VERTEXAI` | Yes | — | Set to `True` |
| `HOST` | **Yes (prod)** | `localhost` | Public hostname of this service — used to build the A2A agent card `url` field. Must be set to the Cloud Run hostname (e.g. `ci-agent-xxx-uc.a.run.app`) or callers will POST to `localhost` instead of this service. |
| `PROTOCOL` | **Yes (prod)** | `http` | `https` in Cloud Run, `http` for local dev |
| `PORT` | No | `8080` | Listening port |
| `CLOUD_RUN_REGION` | No | `us-central1` | Region for Cloud Build and Artifact Registry |
| `ARTIFACT_REGISTRY_REPO` | No | `dinoquest` | Artifact Registry repo name |
| `GITHUB_OWNER` | No | `weimeilin79` | GitHub repo owner |
| `GITHUB_REPO` | No | `dinoquest-io` | GitHub repo name |
| `GITHUB_TOKEN` | For GitHub reporting | — | GitHub PAT — use Secret Manager in prod |
| `GITHUB_TOKEN_SECRET` | For GitHub reporting (prod) | — | Secret Manager resource name |
| `CDAGENT_URL` | For CD trigger | — | URL of deployed CDAgent — CIAgent calls this after a successful build to start canary deploy |
| `DINOAGENT_URL` | For Behavior B | — | URL of deployed DinoAgent, e.g. `https://remediation-agent-xxx-uc.a.run.app` |
| `HARNESS_EVENTS_TOPIC` | For dino-theater | — | `projects/{project}/topics/harness-events` |
| `SLACK_WEBHOOK_URL` | For Slack notifications | — | Incoming webhook URL |
| `SLACK_WEBHOOK_SECRET` | For Slack notifications (prod) | — | Secret Manager resource name — reuse `slack-webhook` secret from DinoAgent |

---

## Running locally

```bash
cd CIAgent
pip install -r requirements.txt
cp .env.example .env   # fill in values
gcloud auth application-default login
python main.py
```

### Send a test A2A task

```bash
curl -X POST http://localhost:8080/tasks/send \
  -H "Content-Type: application/json" \
  -d '{
    "id": "test-task-001",
    "message": {
      "role": "user",
      "parts": [{
        "text": "Build and push image us-central1-docker.pkg.dev/PROJECT/dinoquest/app:abc123 from branch level_2. PR number 42, commit SHA abc123."
      }]
    },
    "metadata": {"correlation_id": "test-001", "from_agent": "human"}
  }'
```

### Check the Agent Card

```bash
curl http://localhost:8080/.well-known/agent.json
```

---

## Deploying to Cloud Run

### 1. Build and push the image

```bash

PROJECT_ID=gca-america-virtual-ta-test                    
  PROJECT_NUMBER=984439674425                                                                                                               
   
gcloud projects add-iam-policy-binding gca-america-virtual-ta-test \
  --member="serviceAccount:ci-agent@${PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/developerconnect.readTokenAccessor"
                                                                                                                                            
# Build execution SA — also needs it when specified as custom serviceAccount
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:ci-agent@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/developerconnect.admin"

gcloud builds submit \                                                                                        
  --no-source \                                                
  --config=/Users/christina/Desktop/work/io/CIAgent/test-build.yaml \
  --project=gca-america-virtual-ta-test \
  --region=us-central1


PROJECT_ID=$(gcloud config get-value project)
IMAGE="us-central1-docker.pkg.dev/${PROJECT_ID}/dinoquest/ci-agent:latest"

gcloud builds submit --tag $IMAGE . --project=$PROJECT_ID
```

### 2. Deploy

```bash
SA="ci-agent@${PROJECT_ID}.iam.gserviceaccount.com"
TOPIC="projects/${PROJECT_ID}/topics/harness-events"
CLOUD_BUILD_CONNECTION="MyGithub"
CLOUD_BUILD_REPO="weimeilin79-dinoquest"
CLOUD_BUILD_REGION="us-central1"
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")
CDAGENT_URL=https://cd-agent-${PROJECT_NUMBER}.us-central1.run.app
DINOAGENT_URL=https://dino-agent-${PROJECT_NUMBER}.us-central1.run.app
GITHUB_OWNER=weimeilin79
GITHUB_REPO=dinoquest

CI_AGENT_URL=$(gcloud run services describe ci-agent \
  --region=us-central1 --format="value(status.url)" --project=$PROJECT_ID | sed 's|https://||')

gcloud run deploy ci-agent \
  --image=$IMAGE \
  --region=us-central1 \
  --service-account=$SA \
  --memory=1Gi \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID}" \
  --set-env-vars="GOOGLE_GENAI_USE_VERTEXAI=True" \
  --set-env-vars="HOST=${CI_AGENT_URL},PROTOCOL=https" \
  --set-env-vars="HARNESS_EVENTS_TOPIC=${TOPIC}" \
  --set-env-vars="SLACK_WEBHOOK_SECRET=projects/${PROJECT_ID}/secrets/ci-slack-webhook/versions/latest" \
  --set-env-vars="GITHUB_OWNER=${GITHUB_OWNER},GITHUB_REPO=${GITHUB_REPO}" \
  --set-env-vars="CLOUD_BUILD_CONNECTION=${CLOUD_BUILD_CONNECTION},CLOUD_BUILD_REPO=${CLOUD_BUILD_REPO},CLOUD_BUILD_REGION=${CLOUD_BUILD_REGION}" \
  --set-env-vars="CDAGENT_URL=${CDAGENT_URL}" \
  --set-env-vars="DINOAGENT_URL=${DINOAGENT_URL}" \
  --set-secrets="GITHUB_TOKEN=github-token:latest" \
  --allow-unauthenticated \
  --timeout=600 \
  --project=$PROJECT_ID \
  --min-instances=1
```

Print log
```
gcloud alpha logging tail "resource.type=cloud_run_revision AND resource.labels.service_name=ci-agent" --project=$PROJECT_ID --format="value(timestamp,textPayload)"
```

### 3. Updating after code changes

```bash
gcloud builds submit --tag $IMAGE . && \
gcloud run services update ci-agent --image=$IMAGE --region=us-central1
```

---

## Cloud Run background thread behavior

When CIAgent receives an A2A request from RemediationAgent, it immediately returns an
acknowledgment and runs the full CI pipeline in a background thread. This keeps the
caller's HTTP connection short, but has an important Cloud Run implication:

After the ACK is returned, Cloud Run has no active request. It can scale down the
instance after the idle timeout (~15 min in practice). CIAgent's pipeline takes 3–5 min.
This is the same risk CDAgent already accepts, and it works in the demo. The `daemon=False`
flag tells Python not to exit until the thread finishes, but Cloud Run sends SIGKILL
regardless after the grace period. If you want a guarantee, set `min-instances=1` on
both agents — that keeps the instance alive permanently. For a demo this is the right
call anyway (no cold starts).

The deploy command above already includes `--min-instances=1` for this reason.

### Background task timeout

`_AGENT_TURN_TIMEOUT_S` in `main.py` caps how long a single CI run may take before
`asyncio.wait_for` cancels it and the task is logged as timed out. Default: **300s
(5 min)** — generous for a normal CI pipeline including the ~1s CDAgent handoff ack.

On timeout:
- If `post_ci_report_to_slack` already ran in the agent's flow, no extra Slack
  message is posted (the user already has the CI report).
- Otherwise, a `"CI run timed out after 300s — see logs."` notice is posted so
  the user isn't left wondering.

Raise this value if you've extended the pipeline (e.g. real `pytest`, longer
Cloud Build) and the cap starts hitting on legitimate runs. Lower it if you'd
rather fail fast in a demo.

---

## Demo mode (keynote / live demo)

Set `DEMO_MODE=true` and the full pipeline runs end-to-end in ~60 seconds.
GitHub API calls (PR read, diff scan, commit status, PR comment) all hit real APIs.
Cloud Build is skipped — instead, a pre-built image already in Artifact Registry is
retagged to `:latest` so CDAgent can deploy it immediately.

### Image retagging rules

| Branch pattern | Source tag retagged to `:latest` |
|---|---|
| `incident_*` (from RemediationAgent) | `incident_solution` |
| anything else (e.g. `level_2`) | `level_2` |

Pre-build the source images once before the demo:

```bash
# Build and tag the level_2 image
gcloud builds submit --tag \
  us-central1-docker.pkg.dev/${PROJECT_ID}/dinoquest/app:level_2 \
  --project=$PROJECT_ID

# Build and tag the incident solution image
# (checkout the incident fix branch first, then:)
gcloud builds submit --tag \
  us-central1-docker.pkg.dev/${PROJECT_ID}/dinoquest/app:incident_solution \
  --project=$PROJECT_ID
```

### Step comparison

| Step | Real mode | Demo mode |
|---|---|---|
| Read PR, get files, scan diff | Live GitHub API | Live GitHub API |
| Scope classification | Agent reasons | Agent reasons |
| Security scan | Real diff check | Real diff check |
| Cloud Build submit | Real build (~8 min) | Retaggs pre-built image to `:latest` |
| Build status poll | Polls every 30s | Returns SUCCESS immediately |
| Artifact Registry verify | Real API check | Returns `found: true` |
| Post commit status + PR comment | Live GitHub API | Live GitHub API |

### Enable / disable

```bash
# Enable demo mode
gcloud run services update ci-agent \
  --set-env-vars DEMO_MODE=true \
  --region=us-central1

# Disable (back to real builds)
gcloud run services update ci-agent \
  --remove-env-vars DEMO_MODE \
  --region=us-central1
```

---

## Slack setup


## Slack slash command setup

### 1. Create a Slack app

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. Name it `CIAgent`, pick your workspace → **Create App**

### 2. Add a Slash Command

1. Under **Features** → **Slash Commands** → **Create New Command**
2. Command: `/ci`
3. Request URL: `https://ci-agent-xxx-uc.a.run.app/slack` (your Cloud Run URL)
4. Short description: `Trigger CI or teach CIAgent a pattern`
5. Save

### 3. Install the app to your workspace

**Settings** → **Install App** → **Install to Workspace** → Allow

### 4. Test

```
/ci build volcano-dodge at SHA abc123, PR 42
/ci that flaky test failure is caused by test ordering — retry with pytest --last-failed
```

CIAgent responds immediately with an acknowledgement and posts the full result
back to the channel when the agent finishes.

No bot token needed — Slack's `response_url` is used for async responses.


### 1. Create a dedicated Slack channel and incoming webhook

1. In Slack, create a channel e.g. `#ci-agent`
2. Go to [api.slack.com/apps](https://api.slack.com/apps) → pick your app (or create one — see step 2 below)
3. Under **Features** → **Incoming Webhooks** → toggle on → **Add New Webhook to Workspace**
4. Choose `#ci-agent` → **Allow**
5. Copy the webhook URL — it looks like:
   ```
   https://hooks.slack.com/services/<WORKSPACE_ID>/<CHANNEL_ID>/<TOKEN>
   ```

### 2. Store it in Secret Manager

```bash
PROJECT_ID=$(gcloud config get-value project)
SA="ci-agent@${PROJECT_ID}.iam.gserviceaccount.com"
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/<WORKSPACE_ID>/<CHANNEL_ID>/<TOKEN>

# Create the secret
echo -n "${SLACK_WEBHOOK_URL}" | \
  gcloud secrets create ci-slack-webhook \
  --data-file=- \
  --project=$PROJECT_ID

# Grant CIAgent's service account access
gcloud secrets add-iam-policy-binding ci-slack-webhook \
  --member="serviceAccount:${SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project=$PROJECT_ID

gcloud run services add-iam-policy-binding ci-agent \
  --region=us-central1 \
  --member=allUsers \
  --role=roles/run.invoker   
```

Then update `SLACK_WEBHOOK_SECRET` in the deploy command to point to this new secret:
```
--set-env-vars="SLACK_WEBHOOK_SECRET=projects/${PROJECT_ID}/secrets/ci-slack-webhook/versions/latest"
```

---


### 4. Test

```
build volcano-dodge at SHA abc123
that build failure is a flaky test, ignore and continue
```

---

## How DinoAgent calls CIAgent (A2A)

DinoAgent sends a `POST /tasks/send` to CIAgent's Cloud Run URL:

```json
{
  "id": "task-uuid",
  "message": {
    "role": "user",
    "parts": [{
      "text": "Build and push image us-central1-docker.pkg.dev/PROJECT/dinoquest/app:SHA from branch main. PR 42, SHA abc123. Feature: volcano-dodge."
    }]
  },
  "metadata": {
    "correlation_id": "session-abc",
    "from_agent": "DinoAgent"
  }
}
```

CIAgent returns:

```json
{
  "id": "task-uuid",
  "status": {"state": "completed"},
  "artifacts": [{"parts": [{"text": "=== CI Report ===\n..."}]}]
}
```

---

## Behavior B — Cross-agent skill teaching

When CIAgent encounters a test failure it cannot classify:
1. It calls `POST /tasks/send` on DinoAgent's URL with the failure log
2. DinoAgent returns a skill definition JSON: `{skill_name, description, detection_pattern, action}`
3. CIAgent calls `register_skill` — emits `skill_registered` event to Pub/Sub
4. dino-theater shows: arrow CI→Dino, thinking bubble on Dino, arrow back, sparkle on CI, new card in Skills panel
5. CIAgent applies the skill's `action` guidance to the current failure

To trigger Behavior B in a demo: submit a build where the test output contains an
unfamiliar error pattern (e.g. a flaky test assertion with no obvious cause). CIAgent
will escalate if it cannot classify the failure from the log alone.



# 1. Health 

check                                                                                                  
```              
  curl https://ci-agent-984439674425.us-central1.run.app/health
```                                                                                                                                  
  # 2. Agent Card (should return JSON with capabilities)

  ```                                                                           
  curl https://ci-agent-984439674425.us-central1.run.app/.well-known/agent.json                                                    
  ```                                                                                                                                  
  # 3. Slack slash command (simulates /ci build dinoquest)                                                                         
  ```
  curl -X POST https://ci-agent-984439674425.us-central1.run.app/slack \
    -d "text=build dinoquest&user_name=christina&trigger_id=test-123&response_url=https://httpbin.org/post"                        
  ```                                                                                                                                 
  # 4. A2A call (simulates DinoAgent sending a task)

  ```                                                                               
  curl -X POST https://ci-agent-984439674425.us-central1.run.app/tasks/send \                                                      
    -H "Content-Type: application/json" \                                                                                          
    -d '{
      "id": "test-task-001",                                                                                                       
      "message": {                                                                                                                 
        "role": "user",
        "parts": [{"text": "Build and test the latest commit on main branch"}]                                                     
      },                                                                                                                           
      "metadata": {"from_agent": "DinoAgent", "correlation_id": "test-task-001"}
    }'                                                                                           ```                          
