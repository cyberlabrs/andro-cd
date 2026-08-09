#!/usr/bin/env bash
# End-to-end runner: for each manifest in ../manifests, wait for Andro-CD to reach
# Synced+Healthy, then hit AWS directly to assert the scenario-specific outcome.
#
# Prerequisites: run scripts/preflight.sh first.
# The manifests must already be visible to Andro-CD (pushed to a repo it's polling).
# Set ANDROCD_URL, VALUES_FILE, API_TOKEN if defaults don't match.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VALUES="${VALUES_FILE:-$HERE/values.local.yaml}"
ANDROCD_URL="${ANDROCD_URL:-http://localhost:8080}"
API_TOKEN="${API_TOKEN:-}"                # if AUTH_MODE!=none, set an API token
TIMEOUT="${TIMEOUT:-600}"                 # seconds to wait for each app to settle

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow(){ printf '\033[33m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

if ! command -v yq >/dev/null; then
  red "yq is required (brew install yq)"; exit 1
fi

curl_api() {
  # $1 method, $2 path, [$3 body]
  local method="$1" path="$2" body="${3:-}"
  local args=(-sf -X "$method" -H "Content-Type: application/json")
  [ -n "$API_TOKEN" ] && args+=(-H "Authorization: Bearer $API_TOKEN")
  [ -n "$body" ] && args+=(-d "$body")
  curl "${args[@]}" "$ANDROCD_URL$path"
}

REGION=$(yq eval '.region' "$VALUES")
CLUSTER=$(yq eval '.cluster' "$VALUES")

wait_settled() {
  local name="$1" deadline=$(( $(date +%s) + TIMEOUT ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if body=$(curl_api GET "/api/apps/$name" 2>/dev/null); then
      local status health
      status=$(echo "$body" | jq -r '.syncStatus // "?"')
      health=$(echo "$body" | jq -r '.health // "?"')
      case "$status" in
        Synced) [ "$health" != "Degraded" ] && return 0 ;;
        Error)  red "$name: sync errored"; echo "$body" | jq -r .message; return 1 ;;
      esac
      printf '\r  waiting for %s: syncStatus=%s health=%s      ' "$name" "$status" "$health"
    else
      printf '\r  waiting for %s: not visible yet (Andro-CD may still be pulling git)   ' "$name"
    fi
    sleep 5
  done
  echo
  red "$name: timeout after ${TIMEOUT}s"; return 1
}

trigger_sync() {
  local name="$1"
  curl_api POST "/api/apps/$name/sync" >/dev/null || true
}

pass=0; total=0
scenario() {
  local name="$1" desc="$2"
  total=$((total+1))
  bold ""
  bold ">>> [$total] $desc"
  trigger_sync "$name"
  if ! wait_settled "$name"; then
    red "✗ $name did not settle"
    return 1
  fi
  echo
  return 0
}

check() {  # $1 label, then a command; the command must exit 0 on pass
  if "$@" >/tmp/e2e-check.out 2>&1; then
    green "  ✓ $1"
    pass=$((pass+1))
  else
    red "  ✗ $1"
    yellow "    output:"; sed 's/^/    /' /tmp/e2e-check.out
  fi
}

total_checks=0
inc_check() { total_checks=$((total_checks+1)); }

# ---------- scenarios ----------

scenario e2e-01-rolling "Rolling update — baseline"
if [ $? -eq 0 ]; then
  inc_check; check "service is ACTIVE" \
    sh -c "aws ecs describe-services --cluster $CLUSTER --services e2e-01-rolling \
      --region $REGION --query 'services[0].status' --output text | grep -q ACTIVE"
fi

scenario e2e-02-autoscaling "Autoscaling — CPU + ALB request-count policies"
if [ $? -eq 0 ]; then
  inc_check; check "CPU scaling policy exists" \
    sh -c "aws application-autoscaling describe-scaling-policies \
      --service-namespace ecs --resource-id service/$CLUSTER/e2e-02-autoscaling \
      --region $REGION --query 'ScalingPolicies[?PolicyName==\`androcd-e2e-02-autoscaling-cpu\`]' \
      --output json | jq -e '. | length > 0'"
  inc_check; check "requests scaling policy exists" \
    sh -c "aws application-autoscaling describe-scaling-policies \
      --service-namespace ecs --resource-id service/$CLUSTER/e2e-02-autoscaling \
      --region $REGION --query 'ScalingPolicies[?PolicyName==\`androcd-e2e-02-autoscaling-requests\`]' \
      --output json | jq -e '. | length > 0'"
fi

scenario e2e-03-efs-firelens "EFS volume + FireLens log router"
if [ $? -eq 0 ]; then
  inc_check; check "task def has EFS volume 'data'" \
    sh -c "aws ecs describe-task-definition --task-definition e2e-03-efs-firelens --region $REGION \
      --query 'taskDefinition.volumes[?name==\`data\`].efsVolumeConfiguration.fileSystemId' \
      --output text | grep -q '^fs-'"
  inc_check; check "task def has log-router container with firelensConfiguration" \
    sh -c "aws ecs describe-task-definition --task-definition e2e-03-efs-firelens --region $REGION \
      --query 'taskDefinition.containerDefinitions[?name==\`log-router\`].firelensConfiguration.type' \
      --output text | grep -q fluentbit"
fi

scenario e2e-04-service-connect "Service Connect with client alias"
if [ $? -eq 0 ]; then
  inc_check; check "serviceConnect is enabled on the service" \
    sh -c "aws ecs describe-services --cluster $CLUSTER --services e2e-04-service-connect --region $REGION \
      --query 'services[0].deployments[0].serviceConnectConfiguration.enabled' --output text | grep -q True"
fi

scenario e2e-05-blue-green "Native blue/green with alarm rollback"
if [ $? -eq 0 ]; then
  inc_check; check "deploymentController.type == ECS" \
    sh -c "aws ecs describe-services --cluster $CLUSTER --services e2e-05-blue-green --region $REGION \
      --query 'services[0].deploymentController.type' --output text | grep -q ECS"
  inc_check; check "deploymentConfiguration.strategy == BLUE_GREEN" \
    sh -c "aws ecs describe-services --cluster $CLUSTER --services e2e-05-blue-green --region $REGION \
      --query 'services[0].deploymentConfiguration.strategy' --output text | grep -q BLUE_GREEN"
fi

scenario e2e-06-canary "Canary with 10% slice and Lambda hook"
if [ $? -eq 0 ]; then
  inc_check; check "deploymentConfiguration.strategy == CANARY" \
    sh -c "aws ecs describe-services --cluster $CLUSTER --services e2e-06-canary --region $REGION \
      --query 'services[0].deploymentConfiguration.strategy' --output text | grep -q CANARY"
  inc_check; check "canaryPercent == 10" \
    sh -c "aws ecs describe-services --cluster $CLUSTER --services e2e-06-canary --region $REGION \
      --query 'services[0].deploymentConfiguration.canaryConfiguration.canaryPercent' --output text | grep -q '^10'"
  inc_check; check "at least one lifecycleHook wired" \
    sh -c "aws ecs describe-services --cluster $CLUSTER --services e2e-06-canary --region $REGION \
      --query 'length(services[0].deploymentConfiguration.lifecycleHooks || \`[]\`)' --output text | grep -qv '^0$'"
fi

scenario e2e-07-scheduled "EventBridge scheduled task"
if [ $? -eq 0 ]; then
  inc_check; check "EventBridge schedule 'androcd-e2e-07-scheduled' exists and is ENABLED" \
    sh -c "aws scheduler get-schedule --name androcd-e2e-07-scheduled --region $REGION \
      --query State --output text | grep -q ENABLED"
fi

scenario e2e-08-oneoff "ECSTask kind — Run now"
if [ $? -eq 0 ]; then
  yellow "  triggering: POST /api/apps/e2e-08-oneoff/run"
  run_resp=$(curl_api POST "/api/apps/e2e-08-oneoff/run" '{"count":1}' || echo '{}')
  inc_check; check "task definition e2e-08-oneoff is registered" \
    sh -c "aws ecs describe-task-definition --task-definition e2e-08-oneoff --region $REGION \
      --query 'taskDefinition.status' --output text | grep -q ACTIVE"
fi

echo
bold "=== SUMMARY ==="
if [ "$pass" -eq "$total_checks" ]; then
  green "$pass/$total_checks checks passed across $total scenarios"
else
  red "$pass/$total_checks checks passed across $total scenarios"
  yellow "Re-run with TIMEOUT=1200 if tasks are slow to reach steady state."
  exit 1
fi
