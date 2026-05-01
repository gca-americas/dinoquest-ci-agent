import logging
import os
import threading
from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset
from google.adk.tools.agent_tool import AgentTool

from utils import emit_event, resolve_secret

_cid = threading.local()

def set_correlation_id(cid: str) -> None:
    _cid.value = cid

def _cid_get() -> str:
    return getattr(_cid, "value", "")

from tools import (
    create_github_branch,
    create_github_pr,
    get_build_status,
    get_github_pr,
    get_github_pr_diff,
    get_github_pr_files,
    list_github_prs,
    post_github_commit_status,
    post_github_pr_comment,
    register_skill,
    run_backend_tests,
    submit_cloud_build,
    verify_artifact_image,
    write_github_file,
)

_SKILL_DIR = Path(__file__).parent / "skills" / "ci-dinoquest"
log = logging.getLogger(__name__)

AGENT_CARD = {
    "name": "CIAgent",
    "description": "Autonomous CI pipeline — builds, tests, and pushes container images for DinoQuest.",
    "version": "1.0",
    "capabilities": ["build_container", "run_tests", "push_artifact", "analyze_build_failure"],
    "skills": [
        {"id": "build_container", "name": "Build Container", "description": "Submit Docker build to Cloud Build"},
        {"id": "push_artifact", "name": "Push Artifact", "description": "Verify image in Artifact Registry"},
        {"id": "analyze_build_failure", "name": "Analyze Build Failure", "description": "Diagnose failures and escalate to DinoAgent"},
    ],
}


def build_agent() -> LlmAgent:
    project_id = os.environ["GOOGLE_CLOUD_PROJECT"]
    region = os.environ.get("CLOUD_RUN_REGION", "us-central1")
    artifact_repo = os.environ.get("ARTIFACT_REGISTRY_REPO", "dinoquest")
    github_owner = os.environ.get("GITHUB_OWNER", "weimeilin79")
    github_repo_name = os.environ.get("GITHUB_REPO", "dinoquest-io")
    github_token = resolve_secret("GITHUB_TOKEN", "GITHUB_TOKEN_SECRET") or ""

    # ── GitHub read tools ──────────────────────────────────────────────────

    def _get_pr(pr_number: int) -> str:
        """Get PR details: title, branch, head SHA, state."""
        log.info("CI [step 2a] _get_pr | pr=%s", pr_number)
        emit_event("CIAgent", "thinking",
                   {"summary": f"Generating summary for PR #{pr_number}"},
                   _cid_get())
        result = get_github_pr(github_owner, github_repo_name, pr_number, github_token)
        log.info("CI [step 2a] _get_pr done | %s", result[:200])
        return result

    def _list_prs(branch: str) -> str:
        """Find open PRs for a branch. Use when no pr_number was provided."""
        log.info("CI [step 2a] _list_prs | branch=%s", branch)
        result = list_github_prs(github_owner, github_repo_name, branch, github_token)
        log.info("CI [step 2a] _list_prs done | %s", result[:200])
        return result

    def _get_pr_files(pr_number: int) -> str:
        """Get the list of changed files in a PR. Used for scope classification."""
        log.info("CI [step 2b] _get_pr_files | pr=%s", pr_number)
        emit_event("CIAgent", "thinking",
                   {"summary": f"Detecting changed files in PR #{pr_number}"},
                   _cid_get())
        result = get_github_pr_files(github_owner, github_repo_name, pr_number, github_token)
        log.info("CI [step 2b] _get_pr_files done | %s", result[:200])
        return result

    def _scan_pr_diff(pr_number: int) -> str:
        """Get the raw diff for secret scanning and scope classification."""
        log.info("CI [step 4a] _scan_pr_diff | pr=%s", pr_number)
        emit_event("CIAgent", "thinking",
                   {"summary": f"Classifying scope and security scan of PR #{pr_number} diff"},
                   _cid_get())
        result = get_github_pr_diff(github_owner, github_repo_name, pr_number, github_token)
        log.info("CI [step 4a] _scan_pr_diff done | diff_len=%s", len(result))
        return result

    # ── GitHub write tools ─────────────────────────────────────────────────

    def _create_pr(title: str, body: str, head: str, base: str = "main") -> str:
        """Create a GitHub PR. Returns number and SHA. Returns error if PR already exists."""
        log.info("CI [step 2c] _create_pr | head=%s base=%s title=%.80s", head, base, title)
        result = create_github_pr(github_owner, github_repo_name, title, body, head, base, github_token)
        log.info("CI [step 2c] _create_pr done | %s", result)
        return result

    def _post_commit_status(sha: str, state: str, description: str) -> str:
        """Post a commit status to GitHub. state: success | failure | pending | error."""
        log.info("CI [step 7] _post_commit_status | sha=%.12s state=%s desc=%.80s", sha, state, description)
        result = post_github_commit_status(github_owner, github_repo_name, sha, state, description, github_token)
        log.info("CI [step 7] _post_commit_status done | %s", result)
        return result

    def _post_pr_comment(pr_number: int, body: str) -> str:
        """Post the full CI report as a PR comment."""
        log.info("CI [step 7] _post_pr_comment | pr=%s body_len=%s", pr_number, len(body))
        emit_event("CIAgent", "thinking",
                   {"summary": f"Posting CI report to GitHub PR #{pr_number}"},
                   _cid_get())
        result = post_github_pr_comment(github_owner, github_repo_name, pr_number, body, github_token)
        log.info("CI [step 7] _post_pr_comment done | %s", result)
        return result

    # ── Cloud Build tools ──────────────────────────────────────────────────

    def _run_backend_tests(branch_name: str) -> str:
        """Run pytest backend/tests/ -v on the branch via Cloud Build (Cloud Build fetches from GitHub directly).
        Returns pass/fail status. Call before _submit_build for BACKEND or FULL scope."""
        log.info("CI [step 5a] _run_backend_tests | branch=%s", branch_name)
        emit_event("CIAgent", "thinking",
                   {"summary": f"Running backend pytest for branch {branch_name}"},
                   _cid_get())
        result = run_backend_tests(github_owner, github_repo_name, branch_name, github_token, project_id)
        log.info("CI [step 5a] _run_backend_tests done | %s", result[:200])
        return result

    def _submit_build(commit_sha: str, branch_name: str, test_scope: str = "FULL") -> str:
        """Submit a Cloud Build job with test steps matching the scope.

        test_scope: FRONTEND (type-check only), BACKEND (pytest only), or FULL (both).
        Constructs the image URI from env config — do NOT pass a project ID.
        """
        image_uri = f"{region}-docker.pkg.dev/{project_id}/{artifact_repo}/app:{commit_sha}"
        log.info("CI [step 5] _submit_build | image=%s branch=%s scope=%s", image_uri, branch_name, test_scope)
        emit_event("CIAgent", "thinking",
                   {"summary": f"Submitting Cloud Build job for branch {branch_name} (scope={test_scope})"},
                   _cid_get())
        result = submit_cloud_build(project_id, region, image_uri, github_owner, github_repo_name, branch_name, github_token, test_scope)
        log.info("CI [step 5] _submit_build done | %s", result)
        return result

    def _get_build_status(build_id: str) -> str:
        """Poll a Cloud Build job. Call every 30s until status is SUCCESS or FAILURE."""
        log.info("CI [step 5] _get_build_status | build_id=%s", build_id)
        emit_event("CIAgent", "thinking",
                   {"summary": f"Running test suite — polling build {build_id}"},
                   _cid_get())
        result = get_build_status(project_id, build_id)
        log.info("CI [step 5] _get_build_status done | %s", result)
        return result

    def _verify_image(image_tag: str) -> str:
        """Confirm image_tag exists in Artifact Registry after a successful build."""
        log.info("CI [step 6] _verify_image | tag=%s", image_tag)
        result = verify_artifact_image(project_id, region, artifact_repo, image_tag)
        log.info("CI [step 6] _verify_image done | %s", result)
        return result

    def _announce_a2a_to_cd(message_preview: str) -> str:
        """Emit a2a_call_sent so the theater shows the animated dot flying to CDAgent.
        Always call this immediately before calling cd_agent."""
        emit_event("CIAgent", "a2a_call_sent", {
            "target_agent": "CDAgent",
            "method": "deploy",
            "args_preview": message_preview[:200],
        }, _cid_get())
        return "{\"status\": \"announced\"}"

    def _register_skill(skill_name: str, description: str, detection_pattern: str, action: str) -> str:
        """Register a skill received from DinoAgent or a human. Emits skill_registered event."""
        log.info("SKILL TEACH [1/4] _register_skill | skill=%s | pattern=%.100s", skill_name, detection_pattern)
        result = register_skill(skill_name, description, detection_pattern, action)
        log.info("SKILL TEACH [1/4] _register_skill done | %s", result)
        return result

    def _create_branch(branch_name: str, base_branch: str = "main") -> str:
        """Create a new branch in the DinoQuest repo.
        Automatically appends a YY_MM_DD_HH_MM timestamp to the branch name.
        Returns the full branch name (with timestamp) — use it for _write_file and _create_pr."""
        from datetime import datetime
        ts = datetime.now().strftime("%y_%m_%d_%H_%M")
        full_name = f"{branch_name}-{ts}"
        log.info("SKILL TEACH [2/4] _create_branch | branch=%s from=%s", full_name, base_branch)
        result = create_github_branch(github_owner, github_repo_name, full_name, base_branch, github_token)
        log.info("SKILL TEACH [2/4] _create_branch done | %s", result)
        return result

    def _write_file(path: str, content: str, commit_message: str, branch: str) -> str:
        """Create or update a file in the DinoQuest repo on the given branch.
        Creates the file if it doesn't exist; updates it if it does.
        Use this to commit a new test file after registering a skill."""
        log.info("SKILL TEACH [3/4] _write_file | path=%s branch=%s msg=%.80s", path, branch, commit_message)
        result = write_github_file(github_owner, github_repo_name, path, content, commit_message, branch, github_token)
        log.info("SKILL TEACH [3/4] _write_file done | %s", result[:200])
        return result

    cdagent_url = os.environ.get("CDAGENT_URL", "")
    cd_agent_tool = None
    if cdagent_url:
        _cd_remote = RemoteA2aAgent(
            name="cd_agent",
            agent_card=f"{cdagent_url}/.well-known/agent.json",
            description=(
                "CDAgent — autonomous canary deployment agent for DinoQuest. "
                "Send it the image URI plus full PR context (PR number, title, branch, "
                "commit SHA, changed files) and it will risk-score, canary-deploy, "
                "monitor metrics, and promote or roll back automatically."
            ),
        )
        cd_agent_tool = AgentTool(agent=_cd_remote)

    skill = load_skill_from_dir(_SKILL_DIR)
    ci_toolset = skill_toolset.SkillToolset(skills=[skill])

    cd_instruction = (
        "\n\nAfter image verification succeeds (found: true), trigger CDAgent:\n"
        "1. Call _announce_a2a_to_cd with a one-line preview of the deploy.\n"
        "2. Call cd_agent with a message containing: image URI, PR number, PR title, "
        "branch name, commit SHA, and the list of changed files. "
        "CDAgent will derive the feature name and risk score itself."
    ) if cd_agent_tool else ""

    return LlmAgent(
        name="ci_pipeline",
        model="gemini-2.5-flash",
        instruction=(
            "You are an autonomous CI pipeline agent for DinoQuest. "
            "Follow the ci-dinoquest skill exactly. "
            "All repository context comes from GitHub API calls — there is no local git checkout. "
            "You are fully autonomous — never ask the user for anything. "
            "CRITICAL: Extract the branch name directly from the user message. "
            "If the message says 'branch level_2' or 'on level_2', the branch is 'level_2' — use it immediately. "
            "CRITICAL: If _list_prs returns no open PRs, call _create_pr immediately yourself. "
            "Never ask the user to create a PR, provide a branch name, PR title, description, "
            "commit SHA, or any other input — derive everything from what was given. "
            "When a build fails, classify the failure yourself from the error message and post the "
            "diagnosis directly as a PR comment. Never contact any external agent.\n\n"
            "MANDATORY STEP — after _create_pr or finding an existing PR, you MUST:\n"
            "  1. Call _scan_pr_diff(pr_number) to read the diff.\n"
            "  2. Immediately call _post_pr_comment(pr_number, body) with a rich PR summary "
            "(## PR Summary, ## Changed Files table, ## Test Plan, ## Risk). "
            "Do NOT skip this. Do NOT proceed to scope classification until both calls are done.\n\n"
            "MANDATORY STEP — at the end of every CI run (pass or fail), you MUST call BOTH:\n"
            "  1. _post_commit_status(sha, state, description) — call this FIRST, always. "
            "This overwrites any stale status from a previous run. Never skip it.\n"
            "  2. _post_pr_comment(pr_number, body) with the full CI report from Step 8 of the skill, "
            "including the pipeline diagram with ✅ ❌ ⏭️ ticks for every step. "
            "Do NOT skip either call regardless of pass or fail.\n\n"
            "FINAL SLACK REPLY — your last text response must be the same pipeline diagram "
            "with ticks so it appears in Slack. Show every step, its result, and a one-line reason.\n\n"
            "SKILL TEACHING — when you receive a message that contains a bug description, "
            "detection pattern, or test instructions from DinoAgent or a human, you MUST execute "
            "all four steps by calling tools — do NOT just describe what you would do:\n"
            "  1. Call _register_skill(skill_name, description, detection_pattern, action).\n"
            "  2. Call _create_branch with name 'ci-skill-' followed by the skill_name. "
            "Use the 'branch' field from the returned JSON for all subsequent calls.\n"
            "  3. Call _write_file to create 'backend/tests/test_' + skill_name + '.py' containing "
            "self-contained pytest test cases that exercise the described pattern. Write real, "
            "runnable test code — not a placeholder.\n"
            "  4. Call _create_pr(title, body, head=<branch from step 2>, base='main').\n"
            "Complete all four steps before responding."
            + cd_instruction
        ),
        tools=[
            ci_toolset,
            _get_pr,
            _list_prs,
            _get_pr_files,
            _scan_pr_diff,
            _create_pr,
            _post_commit_status,
            _post_pr_comment,
            _run_backend_tests,
            _submit_build,
            _get_build_status,
            _verify_image,
            _announce_a2a_to_cd,
            _register_skill,
            _create_branch,
            _write_file,
            *([cd_agent_tool] if cd_agent_tool else []),
        ],
    )
