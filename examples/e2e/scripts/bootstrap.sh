#!/usr/bin/env bash
# Auto-discover / auto-create every AWS resource the e2e suite needs, then write
# values.local.yaml so scripts/run.sh works immediately.
#
# Safe to re-run — every create step is guarded ("does it exist? skip"). Everything
# created here is tagged `androcd-e2e=true` so cleanup can find it.
#
# Cost estimate for a full bootstrap: ~$18/month if you leave everything running
# (~$16 ALB + ~$0.30 EFS + ~$1 Lambda invocations). Delete via `scripts/teardown.sh`
# when you're done to drop this to $0.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VALUES_OUT="${VALUES_FILE:-$HERE/values.local.yaml}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
TAG_KEY="androcd-e2e"
TAG_VAL="true"
CLUSTER_NAME="androcd-e2e"

# All log helpers write to STDERR so callers that capture stdout (e.g. `x=$(fn)`)
# don't accidentally slurp log lines into the return value.
red()    { printf '\033[31m%s\033[0m\n' "$*" >&2; }
green()  { printf '\033[32m%s\033[0m\n' "$*" >&2; }
yellow() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
bold()   { printf '\033[1m%s\033[0m\n' "$*" >&2; }
dim()    { printf '\033[2m%s\033[0m\n' "$*" >&2; }

for tool in aws jq; do
  command -v "$tool" >/dev/null || { red "$tool is required"; exit 1; }
done

# ---- confirmation gate ----------------------------------------------------------
bold "=== Andro-CD e2e bootstrap ==="
identity=$(aws sts get-caller-identity 2>/dev/null) || {
  red "aws sts get-caller-identity failed — check AWS_PROFILE / env"; exit 1
}
ACCOUNT=$(echo "$identity" | jq -r .Account)
CALLER=$(echo "$identity" | jq -r .Arn)
echo "  account: $ACCOUNT"
echo "  caller:  $CALLER"
echo "  region:  $REGION"
echo
yellow "This will CREATE resources in the account above if they don't exist:"
echo "  - ecs cluster '$CLUSTER_NAME'"
echo "  - application load balancer + HTTP listener  (~\$16/mo)"
echo "  - two target groups (blue + green)"
echo "  - security group (allow inbound 80/tcp from 0.0.0.0/0)"
echo "  - ecsTaskExecutionRole  (free — just IAM)"
echo "  - ELBBlueGreenRole      (free — just IAM)"
echo "  - EcsHookInvokerRole    (free — just IAM)"
echo "  - androcdSchedulerRole  (free — just IAM)"
echo "  - CloudWatch alarm 'androcd-e2e-5xx' (dummy — never fires)"
echo "  - Lambda 'androcd-e2e-canary-hook' (returns SUCCEEDED — costs pennies)"
echo "  - EFS filesystem + one access point (~\$0.30/mo if empty)"
echo "  - Cloud Map namespace 'androcd-e2e.local'"
echo
yellow "It will REUSE the default VPC if present, otherwise create test VPC 10.99.0.0/16."
echo
read -r -p "Proceed? [y/N] " ok
[ "$ok" != "y" ] && [ "$ok" != "Y" ] && { echo "aborted"; exit 1; }

# ---- helpers --------------------------------------------------------------------
aws_ec2()  { aws ec2 --region "$REGION" "$@"; }
aws_elb()  { aws elbv2 --region "$REGION" "$@"; }
aws_iam()  { aws iam "$@"; }              # IAM is global
aws_lam()  { aws lambda --region "$REGION" "$@"; }
aws_efs()  { aws efs --region "$REGION" "$@"; }
aws_ecs()  { aws ecs --region "$REGION" "$@"; }
aws_cw()   { aws cloudwatch --region "$REGION" "$@"; }
aws_sd()   { aws servicediscovery --region "$REGION" "$@"; }

wait_for() {
  # $1 human label, $2 command that exits 0 when ready, $3 max seconds (default 60)
  local label="$1" cmd="$2" limit="${3:-60}" waited=0
  printf "  waiting for %s..." "$label" >&2
  while ! eval "$cmd" >/dev/null 2>&1; do
    [ "$waited" -ge "$limit" ] && { echo >&2; red "  timeout on $label"; return 1; }
    sleep 3; waited=$((waited+3)); printf "." >&2
  done
  echo " ready" >&2
}

# ---- 1) VPC + subnets + IGW/routing --------------------------------------------
bold ""
bold "[1/9] Networking (VPC / subnets)"

VPC_ID=$(aws_ec2 describe-vpcs --filters "Name=is-default,Values=true" \
  --query 'Vpcs[0].VpcId' --output text 2>/dev/null)

if [ "$VPC_ID" = "None" ] || [ -z "$VPC_ID" ]; then
  # No default VPC — reuse ours if we made one before, else create it.
  VPC_ID=$(aws_ec2 describe-vpcs --filters "Name=tag:$TAG_KEY,Values=$TAG_VAL" \
    --query 'Vpcs[0].VpcId' --output text 2>/dev/null || echo "None")
  if [ "$VPC_ID" = "None" ] || [ -z "$VPC_ID" ]; then
    echo "  no default VPC found — creating test VPC 10.99.0.0/16"
    VPC_ID=$(aws_ec2 create-vpc --cidr-block 10.99.0.0/16 \
      --tag-specifications "ResourceType=vpc,Tags=[{Key=$TAG_KEY,Value=$TAG_VAL},{Key=Name,Value=androcd-e2e}]" \
      --query 'Vpc.VpcId' --output text)
    aws_ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-hostnames
    IGW=$(aws_ec2 create-internet-gateway \
      --tag-specifications "ResourceType=internet-gateway,Tags=[{Key=$TAG_KEY,Value=$TAG_VAL}]" \
      --query 'InternetGateway.InternetGatewayId' --output text)
    aws_ec2 attach-internet-gateway --vpc-id "$VPC_ID" --internet-gateway-id "$IGW" >/dev/null
    RT=$(aws_ec2 describe-route-tables --filters "Name=vpc-id,Values=$VPC_ID" \
      --query 'RouteTables[0].RouteTableId' --output text)
    aws_ec2 create-route --route-table-id "$RT" --destination-cidr-block 0.0.0.0/0 \
      --gateway-id "$IGW" >/dev/null
    green "  ✓ created VPC $VPC_ID + IGW + route"
  else
    green "  ✓ reusing previously-created test VPC $VPC_ID"
  fi
else
  green "  ✓ using default VPC $VPC_ID"
fi

# Subnets — pick two in different AZs. Create them in the test VPC if needed.
# Portable (bash 3.2 on macOS has no mapfile).
SUBNETS=()
while IFS= read -r line; do
  [ -n "$line" ] && SUBNETS+=("$line")
done < <(aws_ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'Subnets[?MapPublicIpOnLaunch==`true` || Tags[?Key==`'$TAG_KEY'`]].[SubnetId,AvailabilityZone]' \
  --output text | sort -k2 | awk '{print $1}')

if [ "${#SUBNETS[@]}" -lt 2 ]; then
  echo "  creating 2 test subnets in $VPC_ID"
  AZ_A=$(aws_ec2 describe-availability-zones --query 'AvailabilityZones[0].ZoneName' --output text)
  AZ_B=$(aws_ec2 describe-availability-zones --query 'AvailabilityZones[1].ZoneName' --output text)
  SUB_A=$(aws_ec2 create-subnet --vpc-id "$VPC_ID" --cidr-block 10.99.1.0/24 \
    --availability-zone "$AZ_A" \
    --tag-specifications "ResourceType=subnet,Tags=[{Key=$TAG_KEY,Value=$TAG_VAL}]" \
    --query 'Subnet.SubnetId' --output text)
  SUB_B=$(aws_ec2 create-subnet --vpc-id "$VPC_ID" --cidr-block 10.99.2.0/24 \
    --availability-zone "$AZ_B" \
    --tag-specifications "ResourceType=subnet,Tags=[{Key=$TAG_KEY,Value=$TAG_VAL}]" \
    --query 'Subnet.SubnetId' --output text)
  aws_ec2 modify-subnet-attribute --subnet-id "$SUB_A" --map-public-ip-on-launch >/dev/null
  aws_ec2 modify-subnet-attribute --subnet-id "$SUB_B" --map-public-ip-on-launch >/dev/null
  SUBNETS=("$SUB_A" "$SUB_B")
  green "  ✓ created subnets ${SUBNETS[*]}"
else
  SUBNETS=("${SUBNETS[0]}" "${SUBNETS[1]}")
  green "  ✓ using subnets ${SUBNETS[*]}"
fi
SUBNET_A="${SUBNETS[0]}"; SUBNET_B="${SUBNETS[1]}"

# ---- 2) Security group ----------------------------------------------------------
bold ""
bold "[2/9] Security group"
SG_ID=$(aws_ec2 describe-security-groups \
  --filters "Name=vpc-id,Values=$VPC_ID" "Name=tag:$TAG_KEY,Values=$TAG_VAL" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")
if [ "$SG_ID" = "None" ] || [ -z "$SG_ID" ]; then
  SG_ID=$(aws_ec2 create-security-group --group-name androcd-e2e --description "Andro-CD e2e" \
    --vpc-id "$VPC_ID" \
    --tag-specifications "ResourceType=security-group,Tags=[{Key=$TAG_KEY,Value=$TAG_VAL}]" \
    --query 'GroupId' --output text)
  aws_ec2 authorize-security-group-ingress --group-id "$SG_ID" \
    --protocol tcp --port 80 --cidr 0.0.0.0/0 >/dev/null 2>&1 || true
  # EFS + NFS from within the SG (self-referencing)
  aws_ec2 authorize-security-group-ingress --group-id "$SG_ID" \
    --protocol tcp --port 2049 --source-group "$SG_ID" >/dev/null 2>&1 || true
  green "  ✓ created SG $SG_ID (80/tcp from anywhere, 2049 self)"
else
  green "  ✓ reusing SG $SG_ID"
fi

# ---- 3) IAM roles ---------------------------------------------------------------
bold ""
bold "[3/9] IAM roles"

ensure_role() {
  local name="$1" trust_service="$2" managed_policy="${3:-}"
  if aws_iam get-role --role-name "$name" >/dev/null 2>&1; then
    echo "  ✓ role '$name' already exists"
    return
  fi
  local trust
  trust=$(cat <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
  "Principal":{"Service":"$trust_service"},"Action":"sts:AssumeRole"}]}
EOF
)
  aws_iam create-role --role-name "$name" --assume-role-policy-document "$trust" \
    --tags "Key=$TAG_KEY,Value=$TAG_VAL" >/dev/null
  if [ -n "$managed_policy" ]; then
    aws_iam attach-role-policy --role-name "$name" --policy-arn "$managed_policy" >/dev/null
  fi
  green "  ✓ created role '$name'"
}

ensure_role ecsTaskExecutionRole ecs-tasks.amazonaws.com \
  arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
ensure_role ELBBlueGreenRole elasticloadbalancing.amazonaws.com
ensure_role EcsHookInvokerRole ecs.amazonaws.com
ensure_role androcdSchedulerRole scheduler.amazonaws.com

# Attach an inline "invoke Lambda + PassRole" policy to EcsHookInvokerRole so the
# canary hook can actually fire.
aws_iam put-role-policy --role-name EcsHookInvokerRole --policy-name InvokeHookLambda \
  --policy-document '{"Version":"2012-10-17","Statement":[
    {"Effect":"Allow","Action":"lambda:InvokeFunction","Resource":"*"}]}' >/dev/null

# Attach RunTask + PassRole to the scheduler role so EventBridge can launch tasks.
aws_iam put-role-policy --role-name androcdSchedulerRole --policy-name RunEcsTasks \
  --policy-document '{"Version":"2012-10-17","Statement":[
    {"Effect":"Allow","Action":["ecs:RunTask","iam:PassRole"],"Resource":"*"}]}' >/dev/null

EXECUTION_ROLE_ARN=$(aws_iam get-role --role-name ecsTaskExecutionRole --query 'Role.Arn' --output text)
BG_ROLE_ARN=$(aws_iam get-role --role-name ELBBlueGreenRole --query 'Role.Arn' --output text)
HOOK_ROLE_ARN=$(aws_iam get-role --role-name EcsHookInvokerRole --query 'Role.Arn' --output text)
SCHED_ROLE_ARN=$(aws_iam get-role --role-name androcdSchedulerRole --query 'Role.Arn' --output text)

# ---- 4) ALB + listener ----------------------------------------------------------
bold ""
bold "[4/9] Application load balancer"
ALB_ARN=$(aws_elb describe-load-balancers --names androcd-e2e-alb \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || echo "")
if [ -z "$ALB_ARN" ] || [ "$ALB_ARN" = "None" ]; then
  ALB_ARN=$(aws_elb create-load-balancer --name androcd-e2e-alb \
    --subnets "$SUBNET_A" "$SUBNET_B" --security-groups "$SG_ID" \
    --scheme internet-facing --type application \
    --tags "Key=$TAG_KEY,Value=$TAG_VAL" \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text)
  green "  ✓ created ALB $ALB_ARN"
  wait_for "ALB provisioning" \
    "aws_elb describe-load-balancers --load-balancer-arns $ALB_ARN --query 'LoadBalancers[0].State.Code' --output text | grep -q active" \
    180
else
  green "  ✓ reusing ALB $ALB_ARN"
fi

# ---- 5) Target groups blue + green ---------------------------------------------
bold ""
bold "[5/9] Target groups (blue + green)"
ensure_tg() {
  local name="$1"
  local arn
  arn=$(aws_elb describe-target-groups --names "$name" \
    --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || echo "")
  if [ -z "$arn" ] || [ "$arn" = "None" ]; then
    arn=$(aws_elb create-target-group --name "$name" --protocol HTTP --port 80 \
      --vpc-id "$VPC_ID" --target-type ip --health-check-path / \
      --tags "Key=$TAG_KEY,Value=$TAG_VAL" \
      --query 'TargetGroups[0].TargetGroupArn' --output text)
    green "  ✓ created TG '$name'"
  else
    green "  ✓ reusing TG '$name'"
  fi
  echo "$arn"
}
BLUE_TG=$(ensure_tg androcd-e2e-blue)
GREEN_TG=$(ensure_tg androcd-e2e-green)

# ---- 6) Listener + production/test rules ---------------------------------------
LISTENER_ARN=$(aws_elb describe-listeners --load-balancer-arn "$ALB_ARN" \
  --query 'Listeners[?Port==`80`].ListenerArn | [0]' --output text 2>/dev/null || echo "")
if [ -z "$LISTENER_ARN" ] || [ "$LISTENER_ARN" = "None" ]; then
  LISTENER_ARN=$(aws_elb create-listener --load-balancer-arn "$ALB_ARN" \
    --protocol HTTP --port 80 \
    --default-actions "Type=forward,TargetGroupArn=$BLUE_TG" \
    --tags "Key=$TAG_KEY,Value=$TAG_VAL" \
    --query 'Listeners[0].ListenerArn' --output text)
  green "  ✓ created HTTP listener"
else
  green "  ✓ reusing listener"
fi

ensure_rule() {
  # $1 priority, $2 path pattern, $3 tag value; returns rule ARN
  local prio="$1" path="$2" tag="$3"
  local existing
  existing=$(aws_elb describe-rules --listener-arn "$LISTENER_ARN" \
    --query "Rules[?Priority=='$prio'].RuleArn | [0]" --output text 2>/dev/null || echo "")
  if [ -n "$existing" ] && [ "$existing" != "None" ]; then
    echo "$existing"; return
  fi
  aws_elb create-rule --listener-arn "$LISTENER_ARN" --priority "$prio" \
    --conditions "Field=path-pattern,Values=$path" \
    --actions "Type=forward,TargetGroupArn=$BLUE_TG" \
    --tags "Key=$TAG_KEY,Value=$TAG_VAL,Key=role,Value=$tag" \
    --query 'Rules[0].RuleArn' --output text
}
PROD_RULE=$(ensure_rule 100 "/prod/*" prod)
TEST_RULE=$(ensure_rule 200 "/test/*" test)
green "  ✓ production + test rules ready"

# ---- 7) EFS + access point -----------------------------------------------------
bold ""
bold "[6/9] EFS filesystem + access point"
EFS_ID=$(aws_efs describe-file-systems \
  --query "FileSystems[?Tags[?Key=='$TAG_KEY' && Value=='$TAG_VAL']].FileSystemId | [0]" \
  --output text 2>/dev/null || echo "")
if [ -z "$EFS_ID" ] || [ "$EFS_ID" = "None" ]; then
  EFS_ID=$(aws_efs create-file-system --creation-token "androcd-e2e-$RANDOM" \
    --performance-mode generalPurpose --throughput-mode bursting \
    --tags "Key=$TAG_KEY,Value=$TAG_VAL" "Key=Name,Value=androcd-e2e" \
    --query 'FileSystemId' --output text)
  green "  ✓ created EFS $EFS_ID"
  wait_for "EFS available" \
    "aws_efs describe-file-systems --file-system-id $EFS_ID --query 'FileSystems[0].LifeCycleState' --output text | grep -q available" \
    120
  # mount targets in each subnet
  for sub in "$SUBNET_A" "$SUBNET_B"; do
    aws_efs create-mount-target --file-system-id "$EFS_ID" --subnet-id "$sub" \
      --security-groups "$SG_ID" >/dev/null 2>&1 || true
  done
else
  green "  ✓ reusing EFS $EFS_ID"
fi

EFS_AP=$(aws_efs describe-access-points --file-system-id "$EFS_ID" \
  --query "AccessPoints[?Tags[?Key=='$TAG_KEY']].AccessPointId | [0]" \
  --output text 2>/dev/null || echo "")
if [ -z "$EFS_AP" ] || [ "$EFS_AP" = "None" ]; then
  EFS_AP=$(aws_efs create-access-point --file-system-id "$EFS_ID" \
    --posix-user Uid=1000,Gid=1000 \
    --root-directory 'Path=/e2e,CreationInfo={OwnerUid=1000,OwnerGid=1000,Permissions=0755}' \
    --tags "Key=$TAG_KEY,Value=$TAG_VAL" \
    --query 'AccessPointId' --output text)
  green "  ✓ created EFS access point $EFS_AP"
else
  green "  ✓ reusing access point $EFS_AP"
fi

# ---- 8) CloudWatch alarm (dummy, never fires) ----------------------------------
bold ""
bold "[7/9] CloudWatch rollback alarm"
ALARM_NAME="androcd-e2e-5xx"
if ! aws_cw describe-alarms --alarm-names "$ALARM_NAME" \
     --query 'MetricAlarms[0].AlarmName' --output text 2>/dev/null | grep -q "$ALARM_NAME"; then
  aws_cw put-metric-alarm --alarm-name "$ALARM_NAME" \
    --alarm-description "Andro-CD e2e — dummy alarm, never fires" \
    --metric-name HTTPCode_Target_5XX_Count --namespace AWS/ApplicationELB \
    --statistic Sum --period 60 --evaluation-periods 1 \
    --threshold 1000000 --comparison-operator GreaterThanThreshold \
    --treat-missing-data notBreaching \
    --tags "Key=$TAG_KEY,Value=$TAG_VAL"
  green "  ✓ created CloudWatch alarm '$ALARM_NAME'"
else
  green "  ✓ reusing alarm '$ALARM_NAME'"
fi

# ---- 9) Lambda hook + Cloud Map namespace + Cluster ----------------------------
bold ""
bold "[8/9] Lambda canary hook + Cloud Map namespace + ECS cluster"

# Lambda role (basic execution + logs)
ensure_role LambdaAndrocdE2eRole lambda.amazonaws.com \
  arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
LAMBDA_ROLE_ARN=$(aws_iam get-role --role-name LambdaAndrocdE2eRole --query 'Role.Arn' --output text)

LAMBDA_NAME="androcd-e2e-canary-hook"
LAMBDA_ARN=$(aws_lam get-function --function-name "$LAMBDA_NAME" \
  --query 'Configuration.FunctionArn' --output text 2>/dev/null || echo "")
if [ -z "$LAMBDA_ARN" ] || [ "$LAMBDA_ARN" = "None" ]; then
  tmp=$(mktemp -d)
  cat >"$tmp/index.py" <<'PY'
def handler(event, context):
    # Any hookStatus of SUCCEEDED lets the canary advance to the next stage.
    return {"hookStatus": "SUCCEEDED"}
PY
  (cd "$tmp" && zip -q hook.zip index.py)
  # Lambda role takes a few seconds to propagate — retry once on transient failure.
  for i in 1 2 3 4 5; do
    if LAMBDA_ARN=$(aws_lam create-function --function-name "$LAMBDA_NAME" \
        --runtime python3.12 --role "$LAMBDA_ROLE_ARN" --handler index.handler \
        --zip-file "fileb://$tmp/hook.zip" \
        --tags "$TAG_KEY=$TAG_VAL" \
        --query 'FunctionArn' --output text 2>/dev/null); then
      break
    fi
    sleep 4
  done
  rm -rf "$tmp"
  [ -z "${LAMBDA_ARN:-}" ] && { red "Lambda create failed"; exit 1; }
  green "  ✓ created Lambda $LAMBDA_NAME"
else
  green "  ✓ reusing Lambda $LAMBDA_NAME"
fi

# Cloud Map private DNS namespace for Service Connect
SC_NS="androcd-e2e.local"
NS_ID=$(aws_sd list-namespaces \
  --query "Namespaces[?Name=='$SC_NS'].Id | [0]" --output text 2>/dev/null || echo "")
if [ -z "$NS_ID" ] || [ "$NS_ID" = "None" ]; then
  echo "  creating Cloud Map namespace '$SC_NS' (async, ~30s)..."
  OP=$(aws_sd create-private-dns-namespace --name "$SC_NS" --vpc "$VPC_ID" \
    --tags "Key=$TAG_KEY,Value=$TAG_VAL" --query 'OperationId' --output text)
  wait_for "Cloud Map namespace" \
    "aws_sd get-operation --operation-id $OP --query 'Operation.Status' --output text | grep -q SUCCESS" \
    120
  green "  ✓ created namespace '$SC_NS'"
else
  green "  ✓ reusing namespace '$SC_NS'"
fi

# ECS cluster — Andro-CD would auto-create it too, but doing it here means run.sh
# assertions on schedules etc. don't race the first sync.
if ! aws_ecs describe-clusters --clusters "$CLUSTER_NAME" \
     --query 'clusters[?status==`ACTIVE`].clusterName | [0]' --output text 2>/dev/null | grep -q "$CLUSTER_NAME"; then
  aws_ecs create-cluster --cluster-name "$CLUSTER_NAME" \
    --service-connect-defaults "namespace=$SC_NS" \
    --tags "key=$TAG_KEY,value=$TAG_VAL" >/dev/null
  green "  ✓ created ECS cluster '$CLUSTER_NAME'"
else
  green "  ✓ reusing cluster '$CLUSTER_NAME'"
fi

# ---- 10) Write values.local.yaml ------------------------------------------------
bold ""
bold "[9/9] Writing $VALUES_OUT"
cat >"$VALUES_OUT" <<YAML
# Auto-generated by scripts/bootstrap.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
# for AWS account $ACCOUNT in region $REGION. Safe to hand-edit.

cluster: $CLUSTER_NAME
region: $REGION

# --- VPC / networking ---
subnet_a: $SUBNET_A
subnet_b: $SUBNET_B
security_group: $SG_ID

# --- Load balancer ---
alb_listener: $LISTENER_ARN
blue_tg: $BLUE_TG
green_tg: $GREEN_TG
production_rule: $PROD_RULE
test_rule: $TEST_RULE

# --- IAM roles ---
execution_role: $EXECUTION_ROLE_ARN
task_role: $EXECUTION_ROLE_ARN
scheduler_role: $SCHED_ROLE_ARN
elb_bg_role: $BG_ROLE_ARN
hook_role: $HOOK_ROLE_ARN

# --- EFS ---
efs_id: $EFS_ID
efs_access_point: $EFS_AP

# --- CloudWatch alarm ---
rollback_alarm: $ALARM_NAME

# --- Lambda for canary lifecycle hook ---
canary_hook_lambda: $LAMBDA_ARN

# --- Service Connect / Cloud Map ---
sc_namespace: $SC_NS
YAML

green "  ✓ wrote $VALUES_OUT"
echo
bold "=== bootstrap complete ==="
echo "  Next: ./scripts/preflight.sh   (should be all green)"
echo "        ./scripts/run.sh         (drives Andro-CD through every scenario)"
echo "        ./scripts/teardown.sh    (deletes EVERYTHING this script created)"
