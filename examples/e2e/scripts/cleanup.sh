#!/usr/bin/env bash
# Delete every AWS resource the e2e suite creates. Idempotent — safe to rerun.
# Only touches resources whose name matches the `e2e-` prefix in the configured cluster.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VALUES="${VALUES_FILE:-$HERE/values.local.yaml}"

if ! command -v yq >/dev/null; then
  echo "yq required (brew install yq)"; exit 1
fi

REGION=$(yq eval '.region' "$VALUES")
CLUSTER=$(yq eval '.cluster' "$VALUES")

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

bold "=== Andro-CD e2e cleanup on cluster '$CLUSTER' in $REGION ==="
read -r -p "Delete every service, task def and schedule named 'e2e-*' in this cluster? [y/N] " ok
[ "$ok" != "y" ] && [ "$ok" != "Y" ] && { echo "aborted"; exit 1; }

# 1) ECS services
services=$(aws ecs list-services --cluster "$CLUSTER" --region "$REGION" \
  --query 'serviceArns' --output text 2>/dev/null || echo "")
for arn in $services; do
  name=$(basename "$arn")
  case "$name" in
    e2e-*)
      echo "scaling down + deleting service: $name"
      aws ecs update-service --cluster "$CLUSTER" --service "$name" --region "$REGION" \
        --desired-count 0 >/dev/null 2>&1 || true
      aws ecs delete-service --cluster "$CLUSTER" --service "$name" --region "$REGION" \
        --force >/dev/null 2>&1 || true
      green "  ✓ $name"
      ;;
  esac
done

# 2) Task definitions
families=$(aws ecs list-task-definition-families --status ACTIVE --region "$REGION" \
  --family-prefix e2e- --query 'families' --output text 2>/dev/null || echo "")
for family in $families; do
  arns=$(aws ecs list-task-definitions --family-prefix "$family" --status ACTIVE \
    --region "$REGION" --query 'taskDefinitionArns' --output text 2>/dev/null || echo "")
  for arn in $arns; do
    aws ecs deregister-task-definition --task-definition "$arn" --region "$REGION" >/dev/null 2>&1 || true
  done
  green "  ✓ deregistered task defs for $family"
done

# 3) Scheduled tasks
schedules=$(aws scheduler list-schedules --region "$REGION" \
  --query 'Schedules[?starts_with(Name, `androcd-e2e-`)].Name' --output text 2>/dev/null || echo "")
for s in $schedules; do
  aws scheduler delete-schedule --name "$s" --region "$REGION" >/dev/null 2>&1 || true
  green "  ✓ deleted schedule $s"
done

# 4) Autoscaling policies + targets
targets=$(aws application-autoscaling describe-scalable-targets --service-namespace ecs \
  --region "$REGION" \
  --query "ScalableTargets[?starts_with(ResourceId, 'service/$CLUSTER/e2e-')].ResourceId" \
  --output text 2>/dev/null || echo "")
for rid in $targets; do
  aws application-autoscaling deregister-scalable-target --service-namespace ecs \
    --resource-id "$rid" --scalable-dimension ecs:service:DesiredCount --region "$REGION" \
    >/dev/null 2>&1 || true
  green "  ✓ deregistered scalable target $rid"
done

# 5) Andro-CD-created target groups + listener rules (androcd-e2e-* prefix, from managed LB mode)
# We don't touch pre-existing blue_tg / green_tg from the values file.
tgs=$(aws elbv2 describe-target-groups --region "$REGION" \
  --query 'TargetGroups[?starts_with(TargetGroupName, `androcd-e2e-`)].TargetGroupArn' \
  --output text 2>/dev/null || echo "")
for tg in $tgs; do
  aws elbv2 delete-target-group --target-group-arn "$tg" --region "$REGION" >/dev/null 2>&1 || true
  green "  ✓ deleted target group $(basename "$tg")"
done

bold "cleanup done — pre-existing infra (VPC, ALB, EFS, alarms, Lambda) was NOT touched"
