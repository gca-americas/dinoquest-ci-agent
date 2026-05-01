#!/usr/bin/env bash
# security_scan.sh — Scans the git diff for hardcoded credentials and insecure CORS config.
# Usage: bash security_scan.sh <BASE_BRANCH>
# Exit code 0 = clean, 1 = issues found
set -euo pipefail

BASE_BRANCH="${1:-origin/main}"
ISSUES=0

echo "=== DinoQuest Security Scan ==="
echo "Comparing against: $BASE_BRANCH"
echo ""

# ── 1. Hardcoded credentials in changed files ───────────────────────────────
echo "--- Checking for hardcoded credentials ---"
CRED_HITS=$(git diff "$BASE_BRANCH"...HEAD -- '*.py' '*.ts' '*.tsx' '*.js' '*.env*' \
  | grep '^\+' \
  | grep -v '^\+\+\+' \
  | grep -iE '(api_key|secret|password|token|private_key)\s*=\s*["\x27][^"\x27]{8,}' \
  | grep -vE '(os\.getenv|process\.env|getenv|os\.environ|import|#)' || true)

if [ -n "$CRED_HITS" ]; then
  echo "❌ FAIL — Potential hardcoded credential detected:"
  echo "$CRED_HITS"
  ISSUES=$((ISSUES + 1))
else
  echo "✅ No hardcoded credentials found."
fi

echo ""

# ── 2. CORS wildcard check ──────────────────────────────────────────────────
echo "--- Checking CORS configuration ---"
CORS_WILDCARD=$(grep -n 'allow_origins\s*=\s*\["\*"\]' backend/main.py || true)
if [ -n "$CORS_WILDCARD" ]; then
  echo "❌ FAIL — CORS allow_origins is set to wildcard '*':"
  echo "$CORS_WILDCARD"
  ISSUES=$((ISSUES + 1))
else
  echo "✅ CORS origins are not wildcarded."
fi

echo ""

# ── 3. .env files accidentally staged ──────────────────────────────────────
echo "--- Checking for .env files in diff ---"
ENV_FILES=$(git diff "$BASE_BRANCH"...HEAD --name-only | grep -E '^\.env' || true)
if [ -n "$ENV_FILES" ]; then
  echo "❌ FAIL — .env file(s) included in diff:"
  echo "$ENV_FILES"
  ISSUES=$((ISSUES + 1))
else
  echo "✅ No .env files in diff."
fi

echo ""

# ── Summary ─────────────────────────────────────────────────────────────────
if [ "$ISSUES" -gt 0 ]; then
  echo "=== Result: FAILED ($ISSUES issue(s) found) ==="
  exit 1
else
  echo "=== Result: PASSED ==="
  exit 0
fi
