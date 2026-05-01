#!/usr/bin/env bash
# run_backend_tests.sh — Sets up the backend test environment and runs pytest.
# Usage: bash run_backend_tests.sh [--ci]
#   --ci  Fetch GEMINI_API_KEY from Secret Manager (Cloud Build context).
#         Without --ci, a placeholder value is used (tests mock the client anyway).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
CI_MODE=false

for arg in "$@"; do
  [[ "$arg" == "--ci" ]] && CI_MODE=true
done

echo "=== DinoQuest Backend Tests ==="
echo "Backend dir: $BACKEND_DIR"
echo "CI mode:     $CI_MODE"
echo ""

# ── 1. Inject GEMINI_API_KEY ────────────────────────────────────────────────
if [ "$CI_MODE" = true ]; then
  echo "Fetching GEMINI_API_KEY from Secret Manager..."
  export GEMINI_API_KEY=$(gcloud secrets versions access latest \
    --secret="GEMINI_API_KEY" \
    --format="get(payload.data)" \
    | base64 --decode)
  echo "✅ Key injected from Secret Manager."
else
  # Tests mock genai.Client — a placeholder value satisfies the startup check
  export GEMINI_API_KEY="${GEMINI_API_KEY:-test-key-for-ci}"
  echo "ℹ️  Using placeholder GEMINI_API_KEY (Gemini is mocked in tests)."
fi

echo ""

# ── 2. Install test dependencies ────────────────────────────────────────────
echo "--- Installing test dependencies ---"
cd "$BACKEND_DIR"
pip install -q -r requirements.txt
pip install -q pytest httpx

echo ""

# ── 3. Check tests exist ────────────────────────────────────────────────────
if [ ! -d "tests" ] || [ -z "$(ls tests/test_*.py 2>/dev/null)" ]; then
  echo "⚠️  WARNING: No test files found in backend/tests/."
  echo "   Consider adding tests for /api/generate, /api/log/game_end, etc."
  echo "STATUS=no_tests"
  exit 0
fi

echo "--- Running pytest ---"
echo ""

# ── 4. Run pytest ────────────────────────────────────────────────────────────
pytest tests/ -v --tb=short 2>&1
EXIT_CODE=$?

echo ""
if [ $EXIT_CODE -eq 0 ]; then
  echo "=== Result: PASSED ==="
else
  echo "=== Result: FAILED (exit $EXIT_CODE) ==="
fi

exit $EXIT_CODE
