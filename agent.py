import logging
import os
from contextvars import ContextVar
from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset
from google.adk.tools.agent_tool import AgentTool
from google.genai import types

from utils import emit_event, resolve_secret

# ContextVars (not threading.local) so per-request state stays isolated when
# multiple requests run concurrently as asyncio tasks on the same event loop.
_cid_var: ContextVar[str] = ContextVar("ci_cid", default="")
_slack_posted_var: ContextVar[bool] = ContextVar("ci_slack_posted", default=False)

def set_correlation_id(cid: str) -> None:
    _cid_var.set(cid)

def _cid_get() -> str:
    return _cid_var.get()

def reset_slack_posted() -> None:
    _slack_posted_var.set(False)

def was_slack_posted() -> bool:
    return _slack_posted_var.get()

from tools import (
    create_github_pr,
    get_build_status as _get_build_status_impl,
    get_github_pr,
    get_github_pr_diff,
    get_github_pr_files,
    list_github_prs,
    post_github_commit_status,
    post_github_pr_comment,
    run_backend_tests as _run_backend_tests_impl,
    submit_cloud_build,
    verify_artifact_image,
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


def build_agent(slack_post_fn=None) -> LlmAgent:
    project_id = os.environ["GOOGLE_CLOUD_PROJECT"]
    region = os.environ.get("CLOUD_RUN_REGION", "us-central1")
    artifact_repo = os.environ.get("ARTIFACT_REGISTRY_REPO", "dinoquest")
    github_owner = os.environ.get("GITHUB_OWNER", "weimeilin79")
    github_repo_name = os.environ.get("GITHUB_REPO", "dinoquest-io")
    github_token = resolve_secret("GITHUB_TOKEN", "GITHUB_TOKEN_SECRET") or ""

    # ── GitHub read tools ──────────────────────────────────────────────────

    def get_pr(pr_number: int) -> str:
        """Get PR details: title, branch, head SHA, state."""
        log.info("CI [step 2a] get_pr | pr=%s", pr_number)
        emit_event("CIAgent", "thinking",
                   {"summary": f"Generating summary for PR #{pr_number}"},
                   _cid_get())
        result = get_github_pr(github_owner, github_repo_name, pr_number, github_token)
        log.info("CI [step 2a] get_pr done | %s", result[:200])
        return result

    def list_prs(branch: str) -> str:
        """Find open PRs for a branch. Use when no pr_number was provided."""
        log.info("CI [step 2a] list_prs | branch=%s", branch)
        emit_event("CIAgent", "thinking",
                   {"summary": f"Generating summary for PR on branch {branch}"},
                   _cid_get())
        result = list_github_prs(github_owner, github_repo_name, branch, github_token)
        log.info("CI [step 2a] list_prs done | %s", result[:200])
        return result

    def get_pr_files(pr_number: int) -> str:
        """Get the list of changed files in a PR. Used for scope classification."""
        log.info("CI [step 2b] get_pr_files | pr=%s", pr_number)
        emit_event("CIAgent", "thinking",
                   {"summary": f"Detecting changed files in PR #{pr_number}"},
                   _cid_get())
        result = get_github_pr_files(github_owner, github_repo_name, pr_number, github_token)
        log.info("CI [step 2b] get_pr_files done | %s", result[:200])
        return result

    def scan_pr_diff(pr_number: int) -> str:
        """Get the raw diff for secret scanning and scope classification."""
        log.info("CI [step 4a] scan_pr_diff | pr=%s", pr_number)
        emit_event("CIAgent", "thinking",
                   {"summary": f"Classifying scope and security scan of PR #{pr_number} diff"},
                   _cid_get())
        result = get_github_pr_diff(github_owner, github_repo_name, pr_number, github_token)
        log.info("CI [step 4a] scan_pr_diff done | diff_len=%s", len(result))
        return result

    # ── GitHub write tools ─────────────────────────────────────────────────

    def create_pr(title: str, body: str, head: str, base: str = "main") -> str:
        """Create a GitHub PR. Returns number and SHA. Returns error if PR already exists."""
        log.info("CI [step 2c] create_pr | head=%s base=%s title=%.80s", head, base, title)
        result = create_github_pr(github_owner, github_repo_name, title, body, head, base, github_token)
        log.info("CI [step 2c] create_pr done | %s", result)
        return result

    def post_commit_status(sha: str, state: str, description: str) -> str:
        """Post a commit status to GitHub. state: success | failure | pending | error."""
        log.info("CI [step 7] post_commit_status | sha=%.12s state=%s desc=%.80s", sha, state, description)
        emit_event("CIAgent", "thinking",
                   {"summary": f"Pushing commit status: state={state} — {description}"},
                   _cid_get())
        result = post_github_commit_status(github_owner, github_repo_name, sha, state, description, github_token)
        log.info("CI [step 7] post_commit_status done | %s", result)
        return result

    def post_pr_comment(pr_number: int, body: str) -> str:
        """Post the full CI report as a PR comment."""
        log.info("CI [step 7] post_pr_comment | pr=%s body_len=%s", pr_number, len(body))
        emit_event("CIAgent", "thinking",
                   {"summary": f"Posting CI report to GitHub PR #{pr_number}"},
                   _cid_get())
        result = post_github_pr_comment(github_owner, github_repo_name, pr_number, body, github_token)
        log.info("CI [step 7] post_pr_comment done | %s", result)
        return result

    # ── Cloud Build tools ──────────────────────────────────────────────────

    def run_ci_backend_tests(branch_name: str) -> str:
        """Run pytest backend/tests/ -v on the branch via Cloud Build (Cloud Build fetches from GitHub directly).
        Returns pass/fail status. Call before submit_build for BACKEND or FULL scope."""
        log.info("CI [step 5a] run_ci_backend_tests | branch=%s", branch_name)
        emit_event("CIAgent", "pipeline_step",
                   {"step": f"Cloud Build: running backend pytest for {branch_name}"},
                   _cid_get())
        result = _run_backend_tests_impl(github_owner, github_repo_name, branch_name, github_token, project_id)
        log.info("CI [step 5a] run_ci_backend_tests done | %s", result[:200])
        return result

    def submit_build(commit_sha: str, branch_name: str, test_scope: str = "FULL") -> str:
        """Submit a Cloud Build job with test steps matching the scope.

        test_scope: FRONTEND (type-check only), BACKEND (pytest only), or FULL (both).
        Always tags the image as :latest — do NOT pass a project ID or construct the URI.
        """
        image_uri = f"{region}-docker.pkg.dev/{project_id}/{artifact_repo}/app:latest"
        log.info("CI [step 5] submit_build | image=%s branch=%s scope=%s", image_uri, branch_name, test_scope)
        emit_event("CIAgent", "thinking",
                   {"summary": f"Submitting Cloud Build job for branch {branch_name} (scope={test_scope})"},
                   _cid_get())
        result = submit_cloud_build(project_id, region, image_uri, github_owner, github_repo_name, branch_name, github_token, test_scope)
        log.info("CI [step 5] submit_build done | %s", result)
        return result

    def get_ci_build_status(build_id: str) -> str:
        """Poll a Cloud Build job. Call every 30s until status is SUCCESS or FAILURE."""
        log.info("CI [step 5] get_ci_build_status | build_id=%s", build_id)
        emit_event("CIAgent", "thinking",
                   {"summary": f"Cloud Build: polling Docker image build {build_id[:8]}…"},
                   _cid_get())
        result = _get_build_status_impl(project_id, build_id)
        log.info("CI [step 5] get_ci_build_status done | %s", result)
        return result

    def verify_image(image_tag: str) -> str:
        """Confirm image_tag exists in Artifact Registry after a successful build."""
        log.info("CI [step 6] verify_image | tag=%s", image_tag)
        emit_event("CIAgent", "thinking",
                   {"summary": f"Artifact Registry: verifying image {image_tag.split('/')[-1]}"},
                   _cid_get())
        result = verify_artifact_image(project_id, region, artifact_repo, image_tag)
        log.info("CI [step 6] verify_image done | %s", result)
        return result

    def post_ci_report_to_slack(report: str) -> str:
        """Post the final CI pipeline report (with ✅ ❌ ⏭️ ticks) to Slack.
        MUST be called after post_pr_comment and BEFORE announce_a2a_to_cd / cd_agent,
        so the CI report reaches Slack before CDAgent posts its own report."""
        log.info("CI [step 8] post_ci_report_to_slack | len=%s", len(report or ""))
        if slack_post_fn:
            slack_post_fn(report)
            _slack_posted_var.set(True)
            return "{\"status\": \"posted\"}"
        log.warning("post_ci_report_to_slack: no slack_post_fn configured")
        return "{\"status\": \"skipped\", \"reason\": \"slack disabled\"}"

    def announce_a2a_to_cd(ci_report: str, deploy_preview: str) -> str:
        """Hand off to CDAgent. This is atomic:
          1. Posts the FULL CI pipeline report (ci_report) to Slack so the user
             sees the CI outcome immediately — before cd_agent is even called.
             This is the single source of the CI Slack message; do NOT also call
             post_ci_report_to_slack.
          2. Emits the a2a_call_sent event for the theater animation.
        Call this IMMEDIATELY before cd_agent so the user is notified the moment
        the handoff begins, regardless of how long CD takes to ack or finish.
        ci_report      — full pipeline diagram with ✅ ❌ ⏭️ ticks for every step.
        deploy_preview — one-line summary for the theater bubble."""
        if slack_post_fn:
            slack_post_fn(ci_report)
            _slack_posted_var.set(True)
            log.info("CI [step 8] CI report posted to Slack via announce_a2a_to_cd | len=%s", len(ci_report or ""))
        else:
            log.warning("announce_a2a_to_cd: no slack_post_fn configured — CI report not posted")
        emit_event("CIAgent", "a2a_call_sent", {
            "target_agent": "CDAgent",
            "method": "deploy",
            "args_preview": deploy_preview[:200],
        }, _cid_get())
        return "{\"status\": \"announced\"}"

    cdagent_url = os.environ.get("CDAGENT_URL", "")
    cd_agent_tool = None
    if cdagent_url:
        _cd_remote = RemoteA2aAgent(
            name="cd_agent",
            agent_card=f"{cdagent_url}/.well-known/agent-card.json",
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
        "\n\nMANDATORY FINAL STEP — after post_commit_status AND post_pr_comment are both done, "
        "you MUST trigger CDAgent before your task is complete. Do NOT stop after post_pr_comment.\n"
        "Step A (REQUIRED, FIRST): Call announce_a2a_to_cd with TWO arguments:\n"
        "   ci_report      = the FULL CI pipeline report (same pipeline diagram with "
        "✅ ❌ ⏭️ ticks you would put in your final reply, including the 'CD Handover' "
        "row marked as triggered). This is what gets posted to Slack — make it complete.\n"
        "   deploy_preview = one-line deploy summary for the theater (e.g. "
        "'Deploy <branch> @ <sha[:7]>').\n"
        "announce_a2a_to_cd posts the CI report to Slack atomically as part of this step "
        "so the user is notified the instant the handoff begins. Do NOT call "
        "post_ci_report_to_slack separately — announce_a2a_to_cd handles that.\n"
        "Step B (IMMEDIATELY AFTER A): Call cd_agent EXACTLY ONCE with:\n"
        "   'Deploy image <IMAGE_URI>\n"
        "   PR: #<PR_NUMBER> — <PR_TITLE>\n"
        "   Branch: <BRANCH_NAME>\n"
        "   Commit: <FULL_COMMIT_SHA>\n"
        "   Changed files: <comma-separated filenames>'\n"
        "   IMAGE_URI = the EXACT `image_uri` field from the submit_build tool response. "
        "Never construct or invent it — copy it verbatim.\n"
        "ORDERING IS CRITICAL: Step A first, Step B second. Do not interleave other tool "
        "calls between them. Do not call cd_agent before announce_a2a_to_cd. "
        "cd_agent only returns a fast 'task accepted' ack; CD's full report posts to Slack "
        "later from CDAgent itself."
    ) if cd_agent_tool else ""

    _retry_config = types.GenerateContentConfig(
        http_options=types.HttpOptions(
            retry_options=types.HttpRetryOptions(initial_delay=1, attempts=5)
        )
    )

    return LlmAgent(
        name="ci_pipeline",
        model="gemini-3-flash-preview",
        generate_content_config=_retry_config,
        instruction=(
            "You are an autonomous CI pipeline agent for DinoQuest. "
            "Follow the ci-dinoquest skill exactly. "
            "All repository context comes from GitHub API calls — there is no local git checkout. "
            "You are fully autonomous — never ask the user for anything. "
            "YOUR VERY FIRST ACTION must be a tool call — call list_prs immediately with the branch name. "
            "Do NOT write any text before your first tool call. Do NOT describe what you plan to do. "
            "CRITICAL: Extract the branch name directly from the user message. "
            "If the message says 'branch level_2' or 'on level_2', the branch is 'level_2' — use it immediately. "
            "CRITICAL: If list_prs returns no open PRs, call create_pr immediately yourself. "
            "Never ask the user to create a PR, provide a branch name, PR title, description, "
            "commit SHA, or any other input — derive everything from what was given. "
            "When a build fails, classify the failure yourself from the error message and post the "
            "diagnosis directly as a PR comment.\n\n"
            "MANDATORY — after create_pr or finding an existing PR:\n"
            "  1. Call scan_pr_diff(pr_number) to read the diff.\n"
            "  2. Call post_pr_comment(pr_number, body) with a rich PR summary "
            "(## PR Summary, ## Changed Files table, ## Test Plan, ## Risk).\n\n"
            "MANDATORY — at the end of every CI run, pass or fail:\n"
            "  1. post_commit_status(sha, state, description) — always call this first.\n"
            "  2. post_pr_comment(pr_number, body) with the full CI report including pipeline "
            "diagram with ✅ ❌ ⏭️ ticks for every step.\n\n"
            "FINAL SLACK REPLY — your last text response must be the pipeline diagram "
            "with ticks so it appears in Slack. Show every step, its result, and a one-line reason."
            + cd_instruction
        ),
        tools=[
            ci_toolset,
            get_pr,
            list_prs,
            get_pr_files,
            scan_pr_diff,
            create_pr,
            post_commit_status,
            post_pr_comment,
            run_ci_backend_tests,
            submit_build,
            get_ci_build_status,
            verify_image,
            post_ci_report_to_slack,
            announce_a2a_to_cd,
            *([cd_agent_tool] if cd_agent_tool else []),
        ],
    )
