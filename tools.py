import base64
import json
import logging
import os
import ssl
import threading
import time

import certifi
import google.auth
import google.auth.transport.requests
import requests
from google.cloud.devtools import cloudbuild_v1

from utils import emit_event

log = logging.getLogger(__name__)
GITHUB_API = "https://api.github.com"
_gh_lock = threading.Lock()  # serialize SSL handshakes — concurrent threads corrupt OpenSSL 3 error queue
_DEMO_MODE = os.environ.get("DEMO_MODE", "").lower() in ("1", "true", "yes")

# Cloud Build 2nd gen (connectedRepository) config — required in Cloud Run, optional locally in DEMO_MODE
_CB_CONNECTION = os.environ.get("CLOUD_BUILD_CONNECTION", "")
_CB_REPO = os.environ.get("CLOUD_BUILD_REPO", "")
_CB_REGION = os.environ.get("CLOUD_BUILD_REGION", "us-central1")

def _cb_client() -> cloudbuild_v1.CloudBuildClient:
    """Cloud Build client — used only for get_build polling (no DeveloperConnectConfig needed)."""
    from google.api_core.client_options import ClientOptions
    if _CB_REGION == "global":
        return cloudbuild_v1.CloudBuildClient()
    return cloudbuild_v1.CloudBuildClient(
        client_options=ClientOptions(api_endpoint=f"{_CB_REGION}-cloudbuild.googleapis.com")
    )


def _cb_rest_create_build(project_id: str, build_body: dict) -> dict:
    """Submit a Cloud Build job via REST API.

    The Python SDK doesn't expose connectedRepository source config,
    but the REST API does. Returns the parsed operation JSON.
    """
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())
    endpoint = "cloudbuild.googleapis.com" if _CB_REGION == "global" else f"{_CB_REGION}-cloudbuild.googleapis.com"
    url = f"https://{endpoint}/v1/projects/{project_id}/builds"
    resp = requests.post(
        url,
        json=build_body,
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _make_tls12_context() -> ssl.SSLContext:
    """Force TLS 1.2 — Cloud Run egress proxy hangs Python 3.12 TLS 1.3 handshakes."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_verify_locations(certifi.where())
    return ctx


class _TLS12Adapter(requests.adapters.HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = _make_tls12_context()
        super().init_poolmanager(*args, **kwargs)


def _gh(method: str, url: str, **kwargs) -> requests.Response:
    """GitHub API call — forces TLS 1.2, new session per call, retries on connection errors."""
    headers = kwargs.pop("headers", {})
    headers["Connection"] = "close"
    last_exc: Exception = RuntimeError("no attempts")
    for attempt in range(5):
        if attempt:
            time.sleep(min(2 ** attempt, 30))
        try:
            with _gh_lock:
                with requests.Session() as session:
                    adapter = _TLS12Adapter(pool_connections=1, pool_maxsize=1, max_retries=0)
                    session.mount("https://", adapter)
                    session.mount("http://", adapter)
                    kwargs.setdefault("timeout", 15)
                    return session.request(method, url, headers=headers, **kwargs)
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
            log.warning("GitHub %s attempt %d failed: %s", method, attempt + 1, e)
            last_exc = e
    raise last_exc


def run_backend_tests(
    github_owner: str,
    github_repo: str,
    branch_name: str,
    github_token: str,
    project_id: str,
) -> str:
    """Run pytest backend/tests/ -v via a dedicated Cloud Build job.

    Cloud Build fetches source directly from the connected GitHub repo.
    Waits up to 5 minutes and returns pass/fail.
    """
    if _DEMO_MODE:
        log.info("[DEMO] Skipping real pytest run")
        return json.dumps({"passed": True, "output": "DEMO MODE — pytest skipped\n5 passed in 0.42s"})

    repo_resource = (
        f"projects/{project_id}/locations/{_CB_REGION}"
        f"/connections/{_CB_CONNECTION}/repositories/{_CB_REPO}"
    )
    sa_email = f"ci-agent@{project_id}.iam.gserviceaccount.com"
    bucket_name = f"{project_id}_cloudbuild"

    build_body = {
        "serviceAccount": f"projects/{project_id}/serviceAccounts/{sa_email}",
        "logsBucket": f"gs://{bucket_name}",
        "steps": [
            {
                "id": "test",
                "name": "python:3.12-slim",
                "entrypoint": "bash",
                "args": ["-c", "pip install -r backend/requirements.txt pytest && PYTHONPATH=/workspace:/workspace/backend pytest backend/tests/ -v"],
            }
        ],
        "source": {
            "connectedRepository": {
                "repository": repo_resource,
                "revision": f"refs/heads/{branch_name}",
            }
        },
    }
    try:
        op = _cb_rest_create_build(project_id, build_body)
        build_id = op["metadata"]["build"]["id"]
        log.info("pytest Cloud Build submitted: %s branch=%s repo=%s", build_id, branch_name, repo_resource)
    except Exception as e:
        return json.dumps({"error": f"pytest build submission failed: {e}"})

    client = _cb_client()
    for _ in range(20):
        time.sleep(30)
        b = client.get_build(project_id=project_id, id=build_id)
        status = b.status.name
        log.info("pytest build status: %s", status)
        if status in ("SUCCESS", "FAILURE", "CANCELLED", "TIMEOUT"):
            return json.dumps({
                "passed": status == "SUCCESS",
                "status": status,
                "log_url": b.log_url,
                "build_id": build_id,
            })

    return json.dumps({"error": "pytest build timed out after 10 minutes", "build_id": build_id})


def submit_cloud_build(
    project_id: str,
    region: str,
    image_uri: str,
    github_owner: str,
    github_repo: str,
    branch_name: str,
    github_token: str = "",
    test_scope: str = "FULL",
) -> str:
    """Submit a Cloud Build job to build and push a Docker image.

    Cloud Build fetches source directly from the connected GitHub repo — no GCS upload needed.
    """
    if _DEMO_MODE:
        import uuid
        # Determine source tag: incident_* branches use the pre-built incident_solution image,
        # all other branches (e.g. level_2) use the pre-built level_2 image.
        source_tag = "incident_solution" if branch_name.startswith("incident") else "level_2"
        image_base = image_uri.rsplit(":", 1)[0]  # strip :latest
        registry_host = image_base.split("/")[0]
        image_path = "/".join(image_base.split("/")[1:])
        fake_id = f"demo-{uuid.uuid4().hex[:8]}"
        latest_uri = f"{image_base}:latest"
        try:
            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            creds.refresh(google.auth.transport.requests.Request())
            auth_header = {"Authorization": f"Bearer {creds.token}"}
            get_resp = _gh(
                "GET",
                f"https://{registry_host}/v2/{image_path}/manifests/{source_tag}",
                headers={**auth_header, "Accept": "application/vnd.docker.distribution.manifest.v2+json"},
            )
            get_resp.raise_for_status()
            put_resp = _gh(
                "PUT",
                f"https://{registry_host}/v2/{image_path}/manifests/latest",
                data=get_resp.content,
                headers={**auth_header, "Content-Type": get_resp.headers.get("Content-Type", "application/vnd.docker.distribution.manifest.v2+json")},
            )
            put_resp.raise_for_status()
            log.info("[DEMO] Retagged %s → latest | fake_build_id=%s image=%s", source_tag, fake_id, latest_uri)
        except Exception as e:
            log.warning("[DEMO] Retag failed, continuing anyway: %s", e)
        return json.dumps({"build_id": fake_id, "status": "QUEUED", "image_uri": latest_uri})

    repo_resource = (
        f"projects/{project_id}/locations/{_CB_REGION}"
        f"/connections/{_CB_CONNECTION}/repositories/{_CB_REPO}"
    )
    sa_email = f"ci-agent@{project_id}.iam.gserviceaccount.com"
    bucket_name = f"{project_id}_cloudbuild"

    scope = test_scope.upper()
    steps = []

    if scope in ("FRONTEND", "FULL"):
        steps.append({
            "id": "frontend-build",
            "name": "node:20-slim",
            "entrypoint": "bash",
            "args": ["-c", "cd frontend && npm ci && npm run build"],
        })

    steps.append({
        "id": "docker-build",
        "name": "gcr.io/cloud-builders/docker",
        "args": ["build", "-t", image_uri, "."],
    })

    log.info("Cloud Build steps: %s | branch=%s repo=%s", [s["id"] for s in steps], branch_name, repo_resource)

    build_body = {
        "serviceAccount": f"projects/{project_id}/serviceAccounts/{sa_email}",
        "logsBucket": f"gs://{bucket_name}",
        "steps": steps,
        "images": [image_uri],
        "source": {
            "connectedRepository": {
                "repository": repo_resource,
                "revision": f"refs/heads/{branch_name}",
            }
        },
    }
    try:
        op = _cb_rest_create_build(project_id, build_body)
        build_id = op["metadata"]["build"]["id"]
        log.info("Cloud Build submitted: build_id=%s branch=%s repo=%s", build_id, branch_name, repo_resource)
        return json.dumps({"build_id": build_id, "status": "QUEUED", "image_uri": image_uri})
    except Exception as e:
        return json.dumps({"error": f"Cloud Build submission failed: {e}"})


def get_build_status(project_id: str, build_id: str) -> str:
    """Poll the current status of a Cloud Build job."""
    if _DEMO_MODE or build_id.startswith("demo-"):
        log.info("[DEMO] Returning instant SUCCESS | build_id=%s", build_id)
        return json.dumps({
            "build_id": build_id,
            "status": "SUCCESS",
            "log_url": f"https://console.cloud.google.com/cloud-build/builds/{build_id}",
            "finish_time": "2026-05-01T00:00:28Z",
        })
    client = _cb_client()
    build = client.get_build(project_id=project_id, id=build_id)
    return json.dumps({
        "build_id": build_id,
        "status": build.status.name,
        "log_url": build.log_url,
        "finish_time": str(build.finish_time) if build.finish_time else None,
    })


def verify_artifact_image(project_id: str, region: str, repo: str, image_tag: str) -> str:
    """Verify that an image tag exists in Artifact Registry."""
    if _DEMO_MODE:
        log.info("[DEMO] Returning found=true for image_tag=%s", image_tag)
        return json.dumps({"found": True, "image_tag": image_tag, "checked": 1})
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())
    base_url = (
        f"https://artifactregistry.googleapis.com/v1"
        f"/projects/{project_id}/locations/{region}/repositories/{repo}/dockerImages"
    )
    # image_tag may be a full URI (registry/repo/image:tag) — extract just the tag portion for comparison
    tag_only = image_tag.split(":")[-1] if ":" in image_tag else image_tag
    page_token = None
    total_checked = 0
    while True:
        params = {"pageToken": page_token} if page_token else {}
        resp = requests.get(
            base_url,
            params=params,
            headers={"Authorization": f"Bearer {creds.token}"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        images = data.get("dockerImages", [])
        total_checked += len(images)
        if any(tag_only in img.get("tags", []) for img in images):
            return json.dumps({"found": True, "image_tag": image_tag, "checked": total_checked})
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return json.dumps({"found": False, "image_tag": image_tag, "checked": total_checked})


def get_github_pr(owner: str, repo: str, pr_number: int, github_token: str) -> str:
    """Get PR details: branch, SHA, title, body."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}"
    try:
        resp = _gh("GET", url,
            headers={"Authorization": f"token {github_token}", "Accept": "application/vnd.github.v3+json"},
            timeout=30,
        )
        data = resp.json()
        return json.dumps({
            "number": data.get("number"),
            "title": data.get("title"),
            "body": data.get("body"),
            "branch": data.get("head", {}).get("ref"),
            "sha": data.get("head", {}).get("sha"),
            "state": data.get("state"),
        })
    except Exception as e:
        return json.dumps({"error": f"GitHub API call failed: {e}"})


def list_github_prs(owner: str, repo: str, branch: str, github_token: str) -> str:
    """Find open PRs for a given head branch."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls"
    try:
        resp = _gh("GET", url,
            params={"head": f"{owner}:{branch}", "state": "open"},
            headers={"Authorization": f"token {github_token}", "Accept": "application/vnd.github.v3+json"},
            timeout=30,
        )
        prs = [{"number": p["number"], "title": p["title"], "sha": p["head"]["sha"]} for p in resp.json()]
        return json.dumps({"prs": prs})
    except Exception as e:
        return json.dumps({"error": f"GitHub API call failed: {e}"})


def create_github_pr(
    owner: str, repo: str, title: str, body: str, head: str, base: str, github_token: str
) -> str:
    """Create a GitHub pull request. Returns the PR number and SHA.
    If a PR already exists for this branch, finds and returns it instead of failing."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls"
    auth_headers = {"Authorization": f"token {github_token}", "Accept": "application/vnd.github.v3+json"}
    try:
        resp = _gh("POST", url,
            json={"title": title, "body": body, "head": head, "base": base},
            headers=auth_headers,
            timeout=30,
        )
        data = resp.json()
        if resp.status_code == 422:
            errors = data.get("errors", [])
            is_already_exists = any(e.get("code") == "already_exists" for e in errors)
            if is_already_exists:
                for state in ("open", "all"):
                    search = _gh("GET", url,
                        params={"head": f"{owner}:{head}", "state": state, "per_page": 5},
                        headers=auth_headers,
                    )
                    prs = search.json()
                    if isinstance(prs, list) and prs:
                        pr = prs[0]
                        return json.dumps({
                            "number": pr["number"],
                            "sha": pr["head"]["sha"],
                            "url": pr["html_url"],
                            "already_existed": True,
                        })
            gh_errors = "; ".join(e.get("message", e.get("code", "")) for e in errors if e)
            return json.dumps({"error": "pr_creation_failed", "message": data.get("message", ""), "details": gh_errors or "branch may not exist or has no commits ahead of base"})
        return json.dumps({"number": data.get("number"), "sha": data.get("head", {}).get("sha"), "url": data.get("html_url")})
    except Exception as e:
        return json.dumps({"error": f"GitHub API call failed: {e}"})


def get_github_pr_files(owner: str, repo: str, pr_number: int, github_token: str) -> str:
    """Get the list of files changed in a PR."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/files"
    try:
        resp = _gh("GET", url,
            headers={"Authorization": f"token {github_token}", "Accept": "application/vnd.github.v3+json"},
            timeout=30,
        )
        files = [{"filename": f["filename"], "status": f["status"], "additions": f["additions"], "deletions": f["deletions"]} for f in resp.json()]
        return json.dumps({"files": files, "count": len(files)})
    except Exception as e:
        return json.dumps({"error": f"GitHub API call failed: {e}"})


def get_github_pr_diff(owner: str, repo: str, pr_number: int, github_token: str) -> str:
    """Get the raw unified diff for a PR. Used for secret scanning."""
    if _DEMO_MODE:
        return json.dumps({"diff": (
            "diff --git a/backend/main.py b/backend/main.py\n"
            "--- a/backend/main.py\n+++ b/backend/main.py\n"
            "@@ -120,6 +120,8 @@ async def get_leaderboard():\n"
            "-    docs = db.collection('scores').get()\n"
            "+    docs = db.collection('scores').order_by('score', direction='DESCENDING').limit(100).get()\n"
            " # DEMO MODE — secret scan skipped\n"
        )})
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}"
    try:
        resp = _gh("GET", url,
            headers={"Authorization": f"token {github_token}", "Accept": "application/vnd.github.v3.diff"},
            timeout=30,
        )
        return json.dumps({"diff": resp.text[:8000]})
    except Exception as e:
        return json.dumps({"error": f"GitHub API call failed: {e}"})


def post_github_commit_status(
    owner: str,
    repo: str,
    sha: str,
    state: str,
    description: str,
    github_token: str,
) -> str:
    """Post a commit status to GitHub. state: success | failure | pending | error."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/statuses/{sha}"
    try:
        resp = _gh("POST", url,
            json={"state": state, "description": description[:140], "context": "ci-agent"},
            headers={"Authorization": f"token {github_token}", "Accept": "application/vnd.github.v3+json"},
            timeout=30,
        )
        return json.dumps({"http_status": resp.status_code, "state": state})
    except Exception as e:
        return json.dumps({"error": f"GitHub API call failed: {e}"})


def post_github_pr_comment(owner: str, repo: str, pr_number: int, body: str, github_token: str) -> str:
    """Post a comment with the CI report on a GitHub pull request."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments"
    try:
        resp = _gh("POST", url,
            json={"body": body},
            headers={"Authorization": f"token {github_token}", "Accept": "application/vnd.github.v3+json"},
            timeout=30,
        )
        if not resp.ok:
            return json.dumps({"error": resp.text[:200], "http_status": resp.status_code})
        return json.dumps({"http_status": resp.status_code})
    except Exception as e:
        return json.dumps({"error": f"GitHub API call failed: {e}"})


def call_dinoagent_for_help(failure_log: str, correlation_id: str) -> str:
    """Send an unclassified build failure to DinoAgent and ask for a skill definition.

    DinoAgent analyzes the log and returns a JSON skill definition if it recognizes the pattern.
    Used for Behavior B — cross-agent skill teaching.
    """
    dinoagent_url = os.environ.get("DINOAGENT_URL", "")
    if not dinoagent_url:
        return json.dumps({"error": "DINOAGENT_URL not configured"})

    emit_event("CIAgent", "a2a_call_sent", {
        "target_agent": "DinoAgent",
        "method": "analyze_failure",
        "args_preview": failure_log[:200],
    }, correlation_id)

    payload = {
        "id": correlation_id,
        "message": {
            "role": "user",
            "parts": [{"text": (
                "CIAgent encountered a build failure it cannot classify. "
                "Analyze this log and if you recognize the failure pattern, return a JSON skill definition "
                "with fields: skill_name, description, detection_pattern, action.\n\n"
                f"Failure log:\n{failure_log[:3000]}"
            )}],
        },
        "metadata": {"from_agent": "CIAgent", "correlation_id": correlation_id},
    }
    try:
        resp = requests.post(f"{dinoagent_url}/tasks/send", json=payload, timeout=120)
        return resp.text
    except Exception as e:
        return json.dumps({"error": str(e)})


def create_github_branch(
    owner: str,
    repo: str,
    branch_name: str,
    base_branch: str,
    github_token: str,
) -> str:
    """Create a new branch from base_branch. Safe to call if branch already exists."""
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    ref_url = f"{GITHUB_API}/repos/{owner}/{repo}/git/refs/heads/{base_branch}"
    resp = _gh("GET", ref_url, headers=headers, timeout=30)
    if not resp.ok:
        return json.dumps({"error": f"base branch '{base_branch}' not found: {resp.text}"})
    sha = resp.json()["object"]["sha"]

    resp = _gh("POST", f"{GITHUB_API}/repos/{owner}/{repo}/git/refs",
        json={"ref": f"refs/heads/{branch_name}", "sha": sha},
        headers=headers,
        timeout=30,
    )
    if resp.ok:
        return json.dumps({"status": "created", "branch": branch_name, "sha": sha})
    if resp.status_code == 422:
        return json.dumps({"status": "already_exists", "branch": branch_name})
    log.error("create_github_branch failed: %s %s", resp.status_code, resp.text[:300])
    return json.dumps({"error": resp.text, "status_code": resp.status_code})


def write_github_file(
    owner: str,
    repo: str,
    path: str,
    content: str,
    commit_message: str,
    branch: str,
    github_token: str,
) -> str:
    """Create or update a file in a GitHub repo on the given branch.

    Automatically fetches the existing file SHA so updates work without a prior read.
    Returns the commit SHA and file URL.
    """
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"

    sha = None
    existing = _gh("GET", url, headers=headers, params={"ref": branch}, timeout=30)
    if existing.status_code == 200:
        sha = existing.json().get("sha")

    payload: dict = {
        "message": commit_message,
        "content": base64.b64encode(content.encode()).decode(),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    resp = _gh("PUT", url, headers=headers, json=payload, timeout=30)
    if resp.ok:
        data = resp.json()
        return json.dumps({
            "status": "written",
            "path": path,
            "branch": branch,
            "commit_sha": data.get("commit", {}).get("sha"),
            "url": data.get("content", {}).get("html_url"),
        })
    log.error("write_github_file failed: %s %s | path=%s branch=%s", resp.status_code, resp.text[:300], path, branch)
    return json.dumps({"error": resp.text, "status_code": resp.status_code})


def register_skill(
    skill_name: str,
    description: str,
    detection_pattern: str,
    action: str,
    correlation_id: str = "",
) -> str:
    """Register a new skill received from DinoAgent at runtime.

    Emits skill_registered event (drives the GUI sparkle + Skills panel card).
    Returns the skill guidance so the agent can apply it immediately in the current session.
    """
    emit_event("CIAgent", "skill_registered", {
        "skill_name": skill_name,
        "description": description,
        "source": "DinoAgent",
    }, correlation_id)

    return json.dumps({
        "status": "registered",
        "skill_name": skill_name,
        "guidance": f"When you detect '{detection_pattern}', take this action: {action}",
    })
