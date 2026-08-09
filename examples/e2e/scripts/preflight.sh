#!/usr/bin/env bash
# Verify local environment is ready to run the Andro-CD e2e suite.
# Runs read-only AWS calls only — safe on any account.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VALUES="${VALUES_FILE:-$HERE/values.local.yaml}"
ANDROCD_URL="${ANDROCD_URL:-http://localhost:8080}"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow(){ printf '\033[33m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

fail=0

bold "=== Andro-CD e2e preflight ==="

# 1) Required tools
for tool in aws jq curl; do
  if ! command -v "$tool" >/dev/null; then
    red "✗ $tool is not installed"
    fail=1
  else
    green "✓ $tool"
  fi
done

# 2) Values file
if [ ! -f "$VALUES" ]; then
  red "✗ Values file not found: $VALUES"
  yellow "  → cp $HERE/values.yaml $VALUES  and fill it in"
  exit 1
fi
green "✓ Values file: $VALUES"

# 3) AWS credentials work
if identity=$(aws sts get-caller-identity 2>/dev/null); then
  arn=$(echo "$identity" | jq -r .Arn)
  account=$(echo "$identity" | jq -r .Account)
  green "✓ AWS identity: $arn (account $account)"
else
  red "✗ aws sts get-caller-identity failed — check AWS_PROFILE / env vars"
  fail=1
fi

# 4) Unfilled placeholders in values file
if grep -qE 'REPLACE_ME|ACCOUNT:|/YOUR-ALB/' "$VALUES"; then
  red "✗ $VALUES still has placeholder values:"
  grep -nE 'REPLACE_ME|ACCOUNT:|/YOUR-ALB/' "$VALUES" | sed 's/^/    /'
  fail=1
else
  green "✓ Values file has no obvious placeholders"
fi

# 5) Andro-CD reachable
if curl -sf "$ANDROCD_URL/healthz" >/dev/null; then
  green "✓ Andro-CD reachable at $ANDROCD_URL"
else
  red "✗ Andro-CD not reachable at $ANDROCD_URL"
  yellow "  → check the URL, or set ANDROCD_URL=http://... before running"
  fail=1
fi

# 6) Read a few key AWS resources to confirm the values are real
if command -v yq >/dev/null; then
  region=$(yq eval '.region' "$VALUES")
  blue_tg=$(yq eval '.blue_tg' "$VALUES")
  if [ -n "$blue_tg" ] && [ "$blue_tg" != "null" ]; then
    if aws elbv2 describe-target-groups --region "$region" --target-group-arns "$blue_tg" >/dev/null 2>&1; then
      green "✓ blue_tg exists in AWS"
    else
      red "✗ blue_tg ARN not found in AWS: $blue_tg"
      fail=1
    fi
  fi
else
  yellow "! yq not installed — skipping deep AWS checks (install with 'brew install yq')"
fi

echo
if [ "$fail" -ne 0 ]; then
  red "Preflight FAILED — fix the items above before running scripts/run.sh"
  exit 1
fi
green "Preflight OK — ready to run scripts/run.sh"
