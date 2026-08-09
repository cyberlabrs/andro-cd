#!/usr/bin/env bash
# Delete every AWS resource bootstrap.sh created (tag androcd-e2e=true).
# Also runs cleanup.sh first to drop the ECS services + task defs the run creates.
#
# This is more aggressive than cleanup.sh — it removes the pre-created infra
# (ALB, TGs, EFS, roles, Lambda, alarm, Cloud Map ns). Use it when you're done
# with the e2e suite entirely.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
TAG_KEY="androcd-e2e"
TAG_VAL="true"

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
bold()   { printf '\033[1m%s\033[0m\n' "$*"; }

aws_ec2()  { aws ec2 --region "$REGION" "$@"; }
aws_elb()  { aws elbv2 --region "$REGION" "$@"; }
aws_iam()  { aws iam "$@"; }
aws_lam()  { aws lambda --region "$REGION" "$@"; }
aws_efs()  { aws efs --region "$REGION" "$@"; }
aws_ecs()  { aws ecs --region "$REGION" "$@"; }
aws_cw()   { aws cloudwatch --region "$REGION" "$@"; }
aws_sd()   { aws servicediscovery --region "$REGION" "$@"; }

bold "=== Andro-CD e2e TEARDOWN ==="
yellow "Will delete EVERYTHING tagged $TAG_KEY=$TAG_VAL in region $REGION."
yellow "This includes ALB, target groups, EFS, roles, Lambda, alarm, Cloud Map ns"
yellow "and — if bootstrap created it — the test VPC (10.99.0.0/16)."
echo
read -r -p "Are you sure? Type DELETE to proceed: " ok
[ "$ok" != "DELETE" ] && { echo "aborted"; exit 1; }

# ---- 1) First tear down the workloads (services, task defs, schedules) --------
bold ""
bold "[1/9] Workloads (services, task defs, schedules, scalable targets)"
if [ -x "$HERE/scripts/cleanup.sh" ]; then
  # cleanup.sh reads the cluster name from values.local.yaml — if that's gone
  # we fall back to the default. Suppress its "y/N" prompt by piping y.
  echo y | "$HERE/scripts/cleanup.sh" 2>&1 | sed 's/^/  /' || true
fi

# ---- 2) Listener rules + listener + ALB ----------------------------------------
bold ""
bold "[2/9] ALB, listener rules, target groups"
alb_arn=$(aws_elb describe-load-balancers --names androcd-e2e-alb \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || echo "")
if [ -n "$alb_arn" ] && [ "$alb_arn" != "None" ]; then
  listener=$(aws_elb describe-listeners --load-balancer-arn "$alb_arn" \
    --query 'Listeners[0].ListenerArn' --output text 2>/dev/null || echo "")
  if [ -n "$listener" ] && [ "$listener" != "None" ]; then
    mapfile -t rules < <(aws_elb describe-rules --listener-arn "$listener" \
      --query "Rules[?!IsDefault].RuleArn" --output text 2>/dev/null | tr '\t' '\n')
    for rule in "${rules[@]}"; do
      [ -z "$rule" ] && continue
      aws_elb delete-rule --rule-arn "$rule" >/dev/null 2>&1 && green "  ✓ deleted rule $rule" || true
    done
  fi
  aws_elb delete-load-balancer --load-balancer-arn "$alb_arn" >/dev/null 2>&1 && \
    green "  ✓ deleted ALB androcd-e2e-alb" || true
  # ALB deletion is async — TGs can't be deleted until targets detach; sleep briefly.
  sleep 8
fi
for name in androcd-e2e-blue androcd-e2e-green; do
  arn=$(aws_elb describe-target-groups --names "$name" \
    --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || echo "")
  [ -z "$arn" ] || [ "$arn" = "None" ] && continue
  aws_elb delete-target-group --target-group-arn "$arn" >/dev/null 2>&1 && \
    green "  ✓ deleted TG $name" || yellow "  ! could not delete $name (in use?)"
done

# ---- 3) CloudWatch alarm --------------------------------------------------------
bold ""
bold "[3/9] CloudWatch alarm"
aws_cw delete-alarms --alarm-names androcd-e2e-5xx 2>/dev/null && \
  green "  ✓ deleted alarm androcd-e2e-5xx" || true

# ---- 4) Lambda ------------------------------------------------------------------
bold ""
bold "[4/9] Lambda"
aws_lam delete-function --function-name androcd-e2e-canary-hook 2>/dev/null && \
  green "  ✓ deleted Lambda androcd-e2e-canary-hook" || true

# ---- 5) EFS (access points → mount targets → filesystem) -----------------------
bold ""
bold "[5/9] EFS"
efs_ids=$(aws_efs describe-file-systems \
  --query "FileSystems[?Tags[?Key=='$TAG_KEY' && Value=='$TAG_VAL']].FileSystemId" \
  --output text 2>/dev/null || echo "")
for efs in $efs_ids; do
  # access points
  aps=$(aws_efs describe-access-points --file-system-id "$efs" \
    --query 'AccessPoints[].AccessPointId' --output text 2>/dev/null || echo "")
  for ap in $aps; do
    aws_efs delete-access-point --access-point-id "$ap" 2>/dev/null || true
  done
  # mount targets
  mts=$(aws_efs describe-mount-targets --file-system-id "$efs" \
    --query 'MountTargets[].MountTargetId' --output text 2>/dev/null || echo "")
  for mt in $mts; do
    aws_efs delete-mount-target --mount-target-id "$mt" >/dev/null 2>&1 || true
  done
  # wait for mount targets to disappear
  for _ in $(seq 1 30); do
    remaining=$(aws_efs describe-mount-targets --file-system-id "$efs" \
      --query 'length(MountTargets)' --output text 2>/dev/null || echo 0)
    [ "$remaining" = "0" ] && break
    sleep 3
  done
  aws_efs delete-file-system --file-system-id "$efs" 2>/dev/null && \
    green "  ✓ deleted EFS $efs" || yellow "  ! could not delete EFS $efs"
done

# ---- 6) Cloud Map namespace -----------------------------------------------------
bold ""
bold "[6/9] Cloud Map namespace"
ns=$(aws_sd list-namespaces \
  --query "Namespaces[?Name=='androcd-e2e.local'].Id | [0]" --output text 2>/dev/null || echo "")
if [ -n "$ns" ] && [ "$ns" != "None" ]; then
  aws_sd delete-namespace --id "$ns" >/dev/null 2>&1 && \
    green "  ✓ deleted namespace androcd-e2e.local" || \
    yellow "  ! could not delete namespace (services still registered?)"
fi

# ---- 7) ECS cluster -------------------------------------------------------------
bold ""
bold "[7/9] ECS cluster"
aws_ecs delete-cluster --cluster androcd-e2e >/dev/null 2>&1 && \
  green "  ✓ deleted cluster androcd-e2e" || \
  yellow "  ! could not delete cluster (services/tasks still present?)"

# ---- 8) IAM roles ---------------------------------------------------------------
bold ""
bold "[8/9] IAM roles"
detach_and_delete_role() {
  local name="$1"
  aws_iam get-role --role-name "$name" >/dev/null 2>&1 || return 0
  # detach every managed policy
  for arn in $(aws_iam list-attached-role-policies --role-name "$name" \
    --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); do
    aws_iam detach-role-policy --role-name "$name" --policy-arn "$arn" >/dev/null 2>&1 || true
  done
  # delete every inline policy
  for pol in $(aws_iam list-role-policies --role-name "$name" \
    --query 'PolicyNames' --output text 2>/dev/null); do
    aws_iam delete-role-policy --role-name "$name" --policy-name "$pol" >/dev/null 2>&1 || true
  done
  aws_iam delete-role --role-name "$name" >/dev/null 2>&1 && \
    green "  ✓ deleted role $name" || yellow "  ! could not delete role $name"
}
for r in ecsTaskExecutionRole ELBBlueGreenRole EcsHookInvokerRole androcdSchedulerRole LambdaAndrocdE2eRole; do
  detach_and_delete_role "$r"
done

# ---- 9) Test VPC (only if bootstrap created it — tag-guarded) ------------------
bold ""
bold "[9/9] Test VPC (only if bootstrap created one)"
vpc_id=$(aws_ec2 describe-vpcs --filters "Name=tag:$TAG_KEY,Values=$TAG_VAL" \
  --query 'Vpcs[0].VpcId' --output text 2>/dev/null || echo "None")
if [ -n "$vpc_id" ] && [ "$vpc_id" != "None" ]; then
  yellow "  found test VPC $vpc_id — deleting attached resources"
  # SG (skip default sg)
  for sg in $(aws_ec2 describe-security-groups --filters "Name=vpc-id,Values=$vpc_id" \
      "Name=tag:$TAG_KEY,Values=$TAG_VAL" --query 'SecurityGroups[].GroupId' --output text 2>/dev/null); do
    aws_ec2 delete-security-group --group-id "$sg" >/dev/null 2>&1 && \
      green "  ✓ deleted SG $sg" || true
  done
  # subnets
  for sub in $(aws_ec2 describe-subnets --filters "Name=vpc-id,Values=$vpc_id" \
      --query 'Subnets[].SubnetId' --output text 2>/dev/null); do
    aws_ec2 delete-subnet --subnet-id "$sub" >/dev/null 2>&1 && \
      green "  ✓ deleted subnet $sub" || true
  done
  # IGW
  igw=$(aws_ec2 describe-internet-gateways --filters "Name=attachment.vpc-id,Values=$vpc_id" \
    --query 'InternetGateways[0].InternetGatewayId' --output text 2>/dev/null || echo "None")
  if [ -n "$igw" ] && [ "$igw" != "None" ]; then
    aws_ec2 detach-internet-gateway --internet-gateway-id "$igw" --vpc-id "$vpc_id" >/dev/null 2>&1 || true
    aws_ec2 delete-internet-gateway --internet-gateway-id "$igw" >/dev/null 2>&1 && \
      green "  ✓ deleted IGW $igw" || true
  fi
  aws_ec2 delete-vpc --vpc-id "$vpc_id" >/dev/null 2>&1 && \
    green "  ✓ deleted VPC $vpc_id" || yellow "  ! could not delete VPC $vpc_id"
else
  green "  ✓ no test VPC to delete (default VPC left alone)"
fi

# ---- Drop the values file so a stale one doesn't linger ------------------------
if [ -f "$HERE/values.local.yaml" ]; then
  mv "$HERE/values.local.yaml" "$HERE/values.local.yaml.$(date +%s).bak"
  green "  ✓ moved values.local.yaml aside (kept as .bak)"
fi

bold ""
green "teardown done — verify in the AWS console."
