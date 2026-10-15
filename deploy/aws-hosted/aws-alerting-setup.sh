#!/usr/bin/env bash
# aws-alerting-setup.sh - after-hours alerting for ONE university instance, by EMAIL.
#
# Run AFTER aws-account-baseline.sh (GuardDuty) and aws-instance-setup.sh (the instance role).
# Safe to run again: every step checks first and only changes what is missing or different.
# AWS services only, no new vendor.
#
# What it sets up:
#   1. SNS topic "locos-alerts" in the instance region with an email subscription
#   2. A second "locos-alerts" topic in us-east-1 with the same subscription (Route 53
#      health check metrics exist only in us-east-1, so their alarm must live there)
#   3. EventBridge rule "locos-guardduty-high": GuardDuty findings of severity 7 or more
#      (High and Critical) -> email. The topic policy lets EventBridge publish for that rule.
#   4. CloudWatch alarms on the instance: system status check failed -> EC2 recover + email;
#      instance status check failed -> EC2 reboot + email
#   5. Route 53 health check on https://<hostname>/ready (every 30 s, 3 failures = down,
#      SNI on) plus a CloudWatch alarm on it in us-east-1 -> email
#   6. Inline policy "LocOS-Alerts-Publish" on the instance's role: sns:Publish on the ONE
#      locos-alerts topic ARN, nothing else. The daily audit-chain check that
#      host/install-audit-alert.sh installs ON the instance uses it.
#   Every alarm also emails when it clears, so you know when something recovered.
#
# Email confirmation: AWS sends one "AWS Notification - Subscription Confirmation" email
# per topic (two in total). Use authenticated confirmation (LAUNCH.md section 7 step 2), not the link. Until
# then this script and aws-verify.sh report PendingConfirmation. No SMS: SNS text messages to
# US numbers need a registered toll-free number; your phone already pushes email.
#
# Usage:
#   bash aws-alerting-setup.sh --instance-id i-0123456789abcdef0 --hostname tms.university.edu
#        --email ops@example.com [--region us-east-2] [--dry-run] [--yes]
#   --dry-run reads the current state and PRINTS every change instead of making it.
#   Without --dry-run the script first shows that same preview and asks you to type
#   yes before it changes anything (--yes skips the question).
set -euo pipefail
export MSYS_NO_PATHCONV=1

ORIG_ARGS=("$@")
REGION="us-east-2"; HC_REGION="us-east-1"; INSTANCE=""; HOST=""; EMAIL=""
DRY_RUN=0; YES=0
TOPIC="locos-alerts"
RULE="locos-guardduty-high"
TARGET_ID="locos-alerts-email"
POLICY_NAME="LocOS-Alerts-Publish"
HC_REF_PREFIX="locos-ready-"
HC_ALARM="locos-ready-healthcheck"

usage() { sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --instance-id) INSTANCE="${2:-}"; shift 2 ;;
    --hostname) HOST="${2:-}"; shift 2 ;;
    --email) EMAIL="${2:-}"; shift 2 ;;
    --region) REGION="${2:?}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --yes) YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done
HOST="$(printf '%s' "$HOST" | tr 'A-Z' 'a-z')"
EMAIL="$(printf '%s' "$EMAIL" | tr 'A-Z' 'a-z')"
if ! [[ "$INSTANCE" =~ ^i-[0-9a-f]{8,17}$ ]]; then  # whole-string match
  echo "ERROR: --instance-id i-... is required (EC2 > Instances > Instance ID column)." >&2
  exit 2
fi
if ! [[ "$HOST" =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$ ]]; then
  echo "ERROR: --hostname is required: the site's name without https://, for example tms.university.edu" >&2
  exit 2
fi
if [ "$HOST" = "tms.example.edu" ]; then
  echo "ERROR: tms.example.edu is the placeholder; use the university site's real hostname." >&2
  exit 2
fi
if [ -z "$EMAIL" ]; then
  echo "ERROR: --email is required: the address that receives the alerts, for example ops@example.com" >&2
  exit 2
fi
if ! [[ "$EMAIL" =~ ^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$ ]]; then
  echo "ERROR: --email does not look like an email address: $EMAIL" >&2
  exit 2
fi

# Preview first, then ask, unless this is already a dry run or --yes was given.
if [ "$DRY_RUN" = 0 ] && [ "$YES" = 0 ]; then
  bash "$0" "${ORIG_ARGS[@]}" --dry-run
  echo
  read -r -p "Apply the changes listed above? Type yes to continue: " answer
  [ "$answer" = "yes" ] || { echo "Stopped. Nothing was changed."; exit 1; }
fi

# Temporary JSON files (relative paths work for both the Windows and the Linux aws).
TMP_PREFIX="./.locos-alert-$$"
# On any unplanned stop, say so plainly: set -e stops at the first failed AWS call, and the
# AWS error is printed just above this message.
on_exit() {
  local rc=$?
  rm -f "$TMP_PREFIX"-*.json
  if [ "$rc" != 0 ]; then
    echo "STOPPED (exit $rc). Nothing after the failed step was done. Any change listed above as" >&2
    echo "'running:' WAS made. Fix the cause shown above and run this again; it only redoes what" >&2
    echo "is still missing. aws-verify.sh shows the real state." >&2
  fi
}
trap on_exit EXIT
die() { echo "ERROR: $*" >&2; exit 1; }

q_in() { local rg="$1"; shift; aws "$@" --region "$rg" --output text | tr -d '\r'; }
q() { q_in "$REGION" "$@"; }
# probe_in REGION aws-args...: 0 = it exists, 1 = AWS says it does not exist.
# Any OTHER failure (no permission, throttling, network) stops the script: a failed read
# must never be mistaken for "missing" or "fine".
probe_in() {
  local rg="$1" err; shift
  if err="$(aws "$@" --region "$rg" --output text 2>&1 >/dev/null)"; then return 0; fi
  case "$err" in
    *"(NotFound)"*|*"(NoSuchEntity)"*|*"(ResourceNotFoundException)"*) return 1 ;;
  esac
  echo "ERROR: could not read: aws $1 $2 (region $rg)" >&2
  printf '%s\n' "$err" | tr -d '\r' | sed '/^$/d; s/^/       /' | head -5 >&2
  exit 1
}
show() {  # print an argument so the line can be pasted back into bash as-is
  case "$1" in
    *[!A-Za-z0-9_./:=,@+-]*|'') printf " '%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")" ;;
    *) printf ' %s' "$1" ;;
  esac
}
change_in() {
  local rg="$1"; shift
  if [ "$DRY_RUN" = "1" ]; then
    printf '  [dry-run] would run: aws'; for a in "$@"; do show "$a"; done; printf ' --region %s\n' "$rg"
  else
    echo "  running: aws $1 $2 ($rg) ..."
    aws "$@" --region "$rg" --output text | tr -d '\r' >/dev/null
  fi
}
change() { change_in "$REGION" "$@"; }
# jfile NAME: path of a temporary JSON file; the content comes from stdin. In a dry run the
# content is shown too (on stderr, because stdout is the path), so the preview says exactly
# what would be sent.
jfile() {
  local f="$TMP_PREFIX-$1.json"
  cat > "$f"
  if [ "$DRY_RUN" = 1 ]; then { echo "  $1 that would be sent:"; sed 's/^/    /' "$f"; } >&2; fi
  printf '%s' "$f"
}
SUMMARY=""
# note: add a line to the final summary. Upper-case status = a change; in a dry run
# it is shown as [planned] because nothing was actually changed.
note() {
  local s="$2"
  case "$s" in [A-Z]*) if [ "$DRY_RUN" = 1 ]; then s="[planned] $s"; fi ;; esac
  SUMMARY="${SUMMARY}$(printf '  %-34s %s' "$1" "$s")"$'\n'
}
nows() { tr -d ' \t\r\n'; }

command -v aws >/dev/null 2>&1 || die "the aws command is not installed. Use AWS CloudShell, or install AWS CLI v2."

ACCOUNT="$(q sts get-caller-identity --query Account)"
printf '%s' "$ACCOUNT" | grep -Eq '^[0-9]{12}$' || die "could not read the AWS account id (got: $ACCOUNT)"
TOPIC_ARN="arn:aws:sns:$REGION:$ACCOUNT:$TOPIC"
TOPIC1_ARN="arn:aws:sns:$HC_REGION:$ACCOUNT:$TOPIC"
RULE_ARN="arn:aws:events:$REGION:$ACCOUNT:rule/$RULE"
SYS_ALARM="locos-ec2-system-check-$INSTANCE"
INST_ALARM="locos-ec2-instance-check-$INSTANCE"
RECOVER="arn:aws:automate:$REGION:ec2:recover"
REBOOT="arn:aws:automate:$REGION:ec2:reboot"
PENDING=0

echo "== Localization OS after-hours alerting =="
echo "   account: $ACCOUNT  region: $REGION (+ $HC_REGION for the health check alarm)"
echo "   instance: $INSTANCE  hostname: $HOST  email: $EMAIL  dry-run: $DRY_RUN"
echo "   Planned: 2 SNS topics + email, GuardDuty High/Critical rule, 2 EC2 status alarms,"
echo "            Route 53 health check on https://$HOST/ready + alarm, sns:Publish for the instance role"
echo "   Rough monthly cost (estimates from list prices, 2026-09-25; check the AWS Pricing Calculator):"
echo "     Route 53 HTTPS health check, non-AWS endpoint (Cloudflare)   about \$2.75"
echo "     3 CloudWatch alarms (the first 10 alarms in an account are free)  \$0 to \$0.30"
echo "     SNS email (first 1,000 a month free), EventBridge GuardDuty rule  \$0"
echo "     Total about \$3 a month, about \$12 a season."
echo

# --- 0. Read the instance role first (fail early) --------------------------------------
echo "[0/6] Instance role"
profile_arn="$(q ec2 describe-instances --instance-ids "$INSTANCE" --query 'Reservations[0].Instances[0].IamInstanceProfile.Arn')"
if [ -z "$profile_arn" ] || [ "$profile_arn" = "None" ]; then
  die "instance $INSTANCE has no IAM role. Run aws-instance-setup.sh --instance-id $INSTANCE first."
fi
ROLE="$(aws iam get-instance-profile --instance-profile-name "${profile_arn##*/}" \
  --query 'InstanceProfile.Roles[0].RoleName' --output text | tr -d '\r')"
if [ -z "$ROLE" ] || [ "$ROLE" = "None" ]; then
  die "instance profile ${profile_arn##*/} has no role inside it. Run aws-instance-setup.sh first."
fi
echo "  role: $ROLE"
det="$(q guardduty list-detectors --query 'DetectorIds[0]')"
if [ -z "$det" ] || [ "$det" = "None" ]; then
  echo "  WARNING: GuardDuty is not on in $REGION, so the GuardDuty rule will never fire."
  echo "           Run aws-account-baseline.sh first (the rule is still created)."
  note "GuardDuty in $REGION" "OFF - run aws-account-baseline.sh"
fi

# --- 1 + 2. SNS topics and the email subscription --------------------------------------
# ensure_topic REGION ARN: topic exists (create if not) and EMAIL is subscribed.
ensure_topic() {
  local rg="$1" arn="$2" exists=1 subs sub authenticated
  if probe_in "$rg" sns get-topic-attributes --topic-arn "$arn"; then
    echo "  topic $arn exists"; note "SNS topic ($rg)" "already exists"
  else
    exists=0
    change_in "$rg" sns create-topic --name "$TOPIC"
    note "SNS topic ($rg)" "CREATED"
  fi
  subs=""
  if [ "$exists" = 1 ] || [ "$DRY_RUN" = 0 ]; then
    subs="$(q_in "$rg" sns list-subscriptions-by-topic --topic-arn "$arn" \
      --query "Subscriptions[?Protocol=='email' && Endpoint=='$EMAIL'].SubscriptionArn" | tr '\t' '\n')"
  fi
  if printf '%s\n' "$subs" | grep -q '^arn:aws:sns:'; then
    while read -r sub; do
      case "$sub" in arn:aws:sns:*) ;; *) continue ;; esac
      authenticated="$(q_in "$rg" sns get-subscription-attributes \
        --subscription-arn "$sub" --query Attributes.ConfirmationWasAuthenticated)"
      if [ "$authenticated" = "true" ]; then
        note "Email subscription ($rg)" "confirmed, unsubscribe protected ($EMAIL)"
      else
        note "Email subscription ($rg)" "UNPROTECTED - repair in LAUNCH.md section 7 step 2"
        PENDING=1
      fi
    done <<< "$subs"
  elif printf '%s\n' "$subs" | grep -qi 'pending'; then
    echo "  $EMAIL is subscribed but NOT confirmed yet (use CloudShell, LAUNCH.md section 7 step 2)"
    note "Email subscription ($rg)" "PendingConfirmation - authenticated confirmation required"
    PENDING=1
  else
    change_in "$rg" sns subscribe --topic-arn "$arn" --protocol email --notification-endpoint "$EMAIL"
    note "Email subscription ($rg)" "REQUESTED - PendingConfirmation (authenticated required)"
    PENDING=1
  fi
}
echo "[1/6] SNS topic $TOPIC in $REGION"
ensure_topic "$REGION" "$TOPIC_ARN"
if [ "$HC_REGION" != "$REGION" ]; then
  echo "[2/6] SNS topic $TOPIC in $HC_REGION (for the Route 53 health check alarm)"
  ensure_topic "$HC_REGION" "$TOPIC1_ARN"
else
  echo "[2/6] $REGION is $HC_REGION: one topic serves both"
fi

# --- 3. GuardDuty High/Critical findings -> email -----------------------------------------
echo "[3/6] EventBridge rule $RULE (GuardDuty severity 7 or more)"
# Topic policy: the standard owner statement AWS puts on every new topic (CloudWatch alarms
# publish through it), plus EventBridge for THIS rule only, plus CloudWatch for this account.
TOPIC_POLICY="$(jfile topic-policy <<EOF
{"Version":"2012-10-17","Id":"locos-alerts-policy","Statement":[
{"Sid":"OwnerAccount","Effect":"Allow","Principal":{"AWS":"*"},"Action":["SNS:GetTopicAttributes","SNS:SetTopicAttributes","SNS:AddPermission","SNS:RemovePermission","SNS:DeleteTopic","SNS:Subscribe","SNS:ListSubscriptionsByTopic","SNS:Publish"],"Resource":"$TOPIC_ARN","Condition":{"StringEquals":{"AWS:SourceOwner":"$ACCOUNT"}}},
{"Sid":"LocOSEventBridgeGuardDuty","Effect":"Allow","Principal":{"Service":"events.amazonaws.com"},"Action":"sns:Publish","Resource":"$TOPIC_ARN","Condition":{"ArnEquals":{"aws:SourceArn":"$RULE_ARN"}}},
{"Sid":"LocOSCloudWatchAlarms","Effect":"Allow","Principal":{"Service":"cloudwatch.amazonaws.com"},"Action":"sns:Publish","Resource":"$TOPIC_ARN","Condition":{"StringEquals":{"aws:SourceAccount":"$ACCOUNT"}}}
]}
EOF
)"
cur_policy=""
if probe_in "$REGION" sns get-topic-attributes --topic-arn "$TOPIC_ARN"; then
  cur_policy="$(q sns get-topic-attributes --topic-arn "$TOPIC_ARN" --query Attributes.Policy | nows)"
fi
case "$cur_policy" in
  *LocOSEventBridgeGuardDuty*"$RULE_ARN"*LocOSCloudWatchAlarms*)
    note "Topic policy (EventBridge)" "already allows the rule" ;;
  *)
    change sns set-topic-attributes --topic-arn "$TOPIC_ARN" --attribute-name Policy \
      --attribute-value "file://$TOPIC_POLICY"
    note "Topic policy (EventBridge)" "SET (owner + this rule + CloudWatch)" ;;
esac
PATTERN='{"source":["aws.guardduty"],"detail-type":["GuardDuty Finding"],"detail":{"severity":[{"numeric":[">=",7]}]}}'
PATTERN_FILE="$(printf '%s\n' "$PATTERN" | jfile event-pattern)"
rule_now=""
if probe_in "$REGION" events describe-rule --name "$RULE"; then
  rule_now="$(q events describe-rule --name "$RULE" --query '[State, EventPattern]' | nows)"
fi
if [ "$rule_now" = "ENABLED$(printf '%s' "$PATTERN" | nows)" ]; then
  note "EventBridge rule $RULE" "already enabled"
else
  change events put-rule --name "$RULE" --event-pattern "file://$PATTERN_FILE" --state ENABLED \
    --description "LocOS: GuardDuty findings severity 7+ (High, Critical) to email"
  if [ -z "$rule_now" ]; then note "EventBridge rule $RULE" "CREATED"; else note "EventBridge rule $RULE" "UPDATED (was: ${rule_now:0:40}...)"; fi
fi
# The email body. EventBridge cannot set the email subject for SNS ("AWS Notification Message").
TARGETS_FILE="$(jfile targets <<EOF
[{"Id":"$TARGET_ID","Arn":"$TOPIC_ARN","InputTransformer":{
"InputPathsMap":{"severity":"\$.detail.severity","type":"\$.detail.type","title":"\$.detail.title","region":"\$.region","account":"\$.account","id":"\$.detail.id"},
"InputTemplate":"\"LOCALIZATION OS SECURITY ALERT. GuardDuty finding, severity <severity> (7 or more is High or Critical). <title> Finding type: <type>. Account <account>, region <region>. Open it: https://<region>.console.aws.amazon.com/guardduty/home?region=<region>#/findings?fId=<id> . First steps: RUNBOOK-hosted-instance-launch.md LAUNCH.md section 7, then the Incident Response Plan.\""}}]
EOF
)"
tgt=""
if [ -n "$rule_now" ]; then
  tgt="$(q events list-targets-by-rule --rule "$RULE" --query "Targets[?Arn=='$TOPIC_ARN'].Id")"
fi
if [ -n "$tgt" ] && [ "$tgt" != "None" ]; then
  note "Rule target" "already sends to $TOPIC"
elif [ "$DRY_RUN" = 1 ]; then
  change events put-targets --rule "$RULE" --targets "file://$TARGETS_FILE"
  note "Rule target" "ADDED (email via $TOPIC)"
else
  # put-targets reports a refused target in FailedEntryCount but still exits 0, so read it.
  echo "  running: aws events put-targets ($REGION) ..."
  failed="$(q events put-targets --rule "$RULE" --targets "file://$TARGETS_FILE" --query FailedEntryCount)"
  [ "$failed" = "0" ] || die "EventBridge refused the email target (FailedEntryCount: $failed)"
  note "Rule target" "ADDED (email via $TOPIC)"
fi

# --- 4. EC2 status check alarms ------------------------------------------------------------
# alarm_state REGION NAME: one line, fields joined with | so an empty list (no OK actions)
# cannot shift them: alarm actions|OK actions|health check id or -|state|actions enabled|
# metric|statistic|comparison|threshold. Actions sorted, space separated. "None" = no alarm.
alarm_state() {
  q_in "$1" cloudwatch describe-alarms --alarm-names "$2" --query "$ALARM_Q"
}
# MetricAlarms[:1] ... | [0]: a projection, so NO alarm gives null ("None"), never a join() on
# null (a JMESPath error). Every field has a default, so a metric-math alarm cannot break it.
# Single quotes: the `[]` literals are JMESPath, not shell. Tested with the real jmespath
# library by test/jmespath_check.py.
ALARM_Q='MetricAlarms[:1].join(`"|"`, [join(`" "`, sort(AlarmActions || `[]`)), join(`" "`, sort(OKActions || `[]`)), (Dimensions[?Name==`"HealthCheckId"`].Value)[0] || `"-"`, StateValue || `"-"`, to_string(ActionsEnabled), MetricName || `"-"`, Statistic || `"-"`, ComparisonOperator || `"-"`, to_string(Threshold)]) | [0]'
sorted() { printf '%s\n' "$@" | sort | tr '\n' ' ' | sed 's/ $//'; }
# ensure_alarm REGION NAME "ALARM ACTIONS" "OK ACTIONS" HCID-or-dash "METRIC STAT COMPARISON THRESHOLD"
#              put-metric-alarm-args...
ensure_alarm() {
  local rg="$1" name="$2" want_a want_o want_d="$5" want_m="$6" have a o d en m
  want_a="$(sorted $3)"; want_o="$(sorted $4)"; shift 6
  have="$(alarm_state "$rg" "$name")"
  IFS='|' read -r a o d _ en m <<< "$have"
  m="$(printf '%s' "$m" | tr '|' ' ' | sed 's/\.0$//')"
  if [ "$a" = "$want_a" ] && [ "$o" = "$want_o" ] && [ "$d" = "$want_d" ] && [ "$en" = "true" ] \
     && [ "$m" = "$want_m" ]; then
    note "Alarm $name" "already set"
    return
  fi
  [ -z "$en" ] || [ "$en" = "true" ] || echo "  alarm $name has its actions switched off; putting it again turns them back on"
  if [ -n "$have" ] && [ "$have" != "None" ]; then
    # Show WHAT differs: the alarm is replaced whole, so any extra action or other setting
    # added by hand is removed.
    echo "  alarm $name differs from the standard settings and will be replaced:"
    echo "    now:  alarm actions [$a] OK actions [$o] health check $d enabled $en watches $m"
    echo "    want: alarm actions [$want_a] OK actions [$want_o] health check $want_d enabled true watches $want_m"
  fi
  change_in "$rg" cloudwatch put-metric-alarm --alarm-name "$name" --actions-enabled "$@"
  if [ -z "$have" ] || [ "$have" = "None" ]; then note "Alarm $name" "CREATED"; else note "Alarm $name" "UPDATED to the standard settings"; fi
}
echo "[4/6] EC2 status check alarms"
ensure_alarm "$REGION" "$SYS_ALARM" "$RECOVER $TOPIC_ARN" "$TOPIC_ARN" "-" \
  "StatusCheckFailed_System Maximum GreaterThanOrEqualToThreshold 1" \
  --alarm-description "LocOS $INSTANCE: AWS hardware or host problem (system status check). EC2 recovers the instance onto new hardware automatically; check the site." \
  --namespace AWS/EC2 --metric-name StatusCheckFailed_System --dimensions "Name=InstanceId,Value=$INSTANCE" \
  --statistic Maximum --period 60 --evaluation-periods 2 --datapoints-to-alarm 2 --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold --treat-missing-data missing \
  --alarm-actions "$RECOVER" "$TOPIC_ARN" --ok-actions "$TOPIC_ARN"
ensure_alarm "$REGION" "$INST_ALARM" "$REBOOT $TOPIC_ARN" "$TOPIC_ARN" "-" \
  "StatusCheckFailed_Instance Maximum GreaterThanOrEqualToThreshold 1" \
  --alarm-description "LocOS $INSTANCE: the operating system stopped answering (instance status check). EC2 reboots it automatically; check the site." \
  --namespace AWS/EC2 --metric-name StatusCheckFailed_Instance --dimensions "Name=InstanceId,Value=$INSTANCE" \
  --statistic Maximum --period 60 --evaluation-periods 3 --datapoints-to-alarm 3 --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold --treat-missing-data missing \
  --alarm-actions "$REBOOT" "$TOPIC_ARN" --ok-actions "$TOPIC_ARN"

# --- 5. Route 53 health check on /ready + its alarm in us-east-1 ---------------------------
echo "[5/6] Route 53 health check https://$HOST/ready"
hcs="$(q_in "$HC_REGION" route53 list-health-checks --query \
  "HealthChecks[?HealthCheckConfig.ResourcePath=='/ready'].[Id, HealthCheckConfig.FullyQualifiedDomainName, HealthCheckConfig.ResourcePath, HealthCheckConfig.Type, to_string(HealthCheckConfig.Port), to_string(HealthCheckConfig.RequestInterval), to_string(HealthCheckConfig.FailureThreshold), to_string(HealthCheckConfig.EnableSNI)]")"
HCID="$(printf '%s\n' "$hcs" | awk -F'\t' -v h="$HOST" '$2 == h && $3 == "/ready" {print $1; exit}')"
others="$(printf '%s\n' "$hcs" | awk -F'\t' -v h="$HOST" 'NF > 1 && $2 != h {print $1 " (" $2 ")"}')"
if [ -n "$others" ]; then
  echo "  NOTE: other LocOS health checks exist (an old hostname?); delete them in Route 53 if unused:"
  printf '%s\n' "$others" | sed 's/^/    /'
fi
if [ -n "$HCID" ]; then
  cfg="$(printf '%s\n' "$hcs" | awk -F'\t' -v i="$HCID" '$1 == i {print $4, $5, $6, $7, $8}')"
  if [ "$cfg" = "HTTPS 443 30 3 true" ]; then
    note "Health check $HCID" "already exists"
  else
    echo "  WARNING: health check $HCID has non-standard settings ($cfg; want HTTPS 443 30 3 true)."
    echo "           Delete it in Route 53 > Health checks and run this again to recreate it."
    note "Health check $HCID" "exists, NON-STANDARD settings (see above)"
  fi
else
  HC_CONFIG="$(jfile health-check <<EOF
{"Type":"HTTPS","FullyQualifiedDomainName":"$HOST","Port":443,"ResourcePath":"/ready","RequestInterval":30,"FailureThreshold":3,"EnableSNI":true,"MeasureLatency":false,"Inverted":false}
EOF
)"
  REF="$HC_REF_PREFIX${HOST:0:36}-$(date -u +%Y%m%d%H%M%S)"
  if [ "$DRY_RUN" = 1 ]; then
    change_in "$HC_REGION" route53 create-health-check --caller-reference "$REF" --health-check-config "file://$HC_CONFIG"
    change_in "$HC_REGION" route53 change-tags-for-resource --resource-type healthcheck \
      --resource-id NEW-HEALTH-CHECK-ID --add-tags "Key=Name,Value=locos-ready-$HOST"
    HCID="NEW-HEALTH-CHECK-ID"
  else
    echo "  running: aws route53 create-health-check ..."
    HCID="$(q_in "$HC_REGION" route53 create-health-check --caller-reference "$REF" \
      --health-check-config "file://$HC_CONFIG" --query HealthCheck.Id)"
    [ -n "$HCID" ] && [ "$HCID" != "None" ] || die "Route 53 did not return a health check id"
    change_in "$HC_REGION" route53 change-tags-for-resource --resource-type healthcheck \
      --resource-id "$HCID" --add-tags "Key=Name,Value=locos-ready-$HOST"
  fi
  note "Health check ($HOST/ready)" "CREATED $HCID"
fi
ensure_alarm "$HC_REGION" "$HC_ALARM" "$TOPIC1_ARN" "$TOPIC1_ARN" "$HCID" \
  "HealthCheckStatus Minimum LessThanThreshold 1" \
  --alarm-description "LocOS: https://$HOST/ready is failing from the Route 53 checkers (site down, database not answering, or Cloudflare refusing the checkers)." \
  --namespace AWS/Route53 --metric-name HealthCheckStatus --dimensions "Name=HealthCheckId,Value=$HCID" \
  --statistic Minimum --period 60 --evaluation-periods 2 --datapoints-to-alarm 2 --threshold 1 \
  --comparison-operator LessThanThreshold --treat-missing-data breaching \
  --alarm-actions "$TOPIC1_ARN" --ok-actions "$TOPIC1_ARN"

# --- 6. Instance role may publish to the ONE topic -----------------------------------------
echo "[6/6] Inline policy $POLICY_NAME on role $ROLE"
ROLE_POLICY="$(jfile role-policy <<EOF
{"Version":"2012-10-17","Statement":[{"Sid":"PublishLocosAlertsOnly","Effect":"Allow","Action":"sns:Publish","Resource":"$TOPIC_ARN"}]}
EOF
)"
pol=""
if probe_in "$REGION" iam get-role-policy --role-name "$ROLE" --policy-name "$POLICY_NAME"; then
  pol="$(q iam get-role-policy --role-name "$ROLE" --policy-name "$POLICY_NAME" --query \
    "PolicyDocument.Statement[].[Effect, join(',', to_array(Action)), join(',', to_array(Resource))]")"
fi
if [ "$pol" = "$(printf 'Allow\tsns:Publish\t%s' "$TOPIC_ARN")" ]; then
  note "Policy $POLICY_NAME" "already sns:Publish on $TOPIC only"
else
  [ -z "$pol" ] || echo "  existing policy differs (it is replaced): $(printf '%s' "$pol" | tr '\t\n' '  ')"
  change iam put-role-policy --role-name "$ROLE" --policy-name "$POLICY_NAME" \
    --policy-document "file://$ROLE_POLICY"
  note "Policy $POLICY_NAME" "SET: sns:Publish on $TOPIC_ARN only"
fi

echo
echo "== Summary ($INSTANCE, $HOST) =="
printf '%s' "$SUMMARY"
echo
echo "Topic for the on-box audit check: $TOPIC_ARN"
if [ "$PENDING" = 1 ]; then
  echo "OPERATOR: use the confirmation link's Token in CloudShell, not the link itself."
  echo "  aws sns confirm-subscription --region REGION --topic-arn TOPIC_ARN \\"
  echo "    --token '<Token from that region email>' --authenticate-on-unsubscribe true"
  echo "See LAUNCH.md section 7 step 2 for both regions and existing unprotected subscriptions."
  echo "Check afterwards with aws-verify.sh; pending or unprotected email is not launch-ready."
fi
if [ "$DRY_RUN" = 1 ]; then
  echo "Dry run only: nothing was changed. Run again without --dry-run to apply."
else
  echo "Done. Next, on the instance (as ec2-user):"
  echo "  bash install-audit-alert.sh --topic-arn $TOPIC_ARN"
  echo "Then the safe tests in LAUNCH.md section 7, and bash aws-verify.sh --instance-id $INSTANCE --hostname $HOST"
fi
