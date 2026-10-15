#!/usr/bin/env bash
# aws-verify.sh - READ-ONLY evidence check for the university instance.
#
# Changes nothing. Prints one PASS / FAIL / WARN line per control under a dated
# header, so the output can be saved as audit evidence
# (Security and Compliance\Records). Exits 1 if any line is FAIL.
#
# Usage:
#   bash aws-verify.sh --instance-id i-0123456789abcdef0 [--region us-east-2]
#        [--copy-region us-west-2] [--hostname tms.university.edu] [--out verify-2026-09-25.txt]
#   --out FILE also writes everything to FILE (in CloudShell: Actions > Download file).
#   --hostname checks that the Route 53 health check watches that name (optional).
#   Section 8 checks the after-hours alerting that aws-alerting-setup.sh sets up.
set -euo pipefail
export MSYS_NO_PATHCONV=1

REGION="us-east-2"; COPY_REGION="us-west-2"; INSTANCE=""; OUT=""; HOST=""
DLM_DESC="LocOS daily EBS snapshots"
HC_REGION="us-east-1"   # Route 53 health check metrics (and so their alarm) exist only here
TOPIC="locos-alerts"; RULE="locos-guardduty-high"; POLICY_NAME="LocOS-Alerts-Publish"
HC_ALARM="locos-ready-healthcheck"

while [ $# -gt 0 ]; do
  case "$1" in
    --instance-id) INSTANCE="${2:-}"; shift 2 ;;
    --region) REGION="${2:?}"; shift 2 ;;
    --copy-region) COPY_REGION="${2:?}"; shift 2 ;;
    --out) OUT="${2:?}"; shift 2 ;;
    --hostname) HOST="$(printf '%s' "${2:?}" | tr 'A-Z' 'a-z')"; shift 2 ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
if ! [[ "$INSTANCE" =~ ^i-[0-9a-f]{8,17}$ ]]; then  # whole-string match
  echo "ERROR: --instance-id i-... is required." >&2; exit 2
fi
if [ -n "$OUT" ]; then : > "$OUT"; fi

# r: read-only aws call. On ANY failure it prints exactly ERROR (never partial output), and
# every check below treats ERROR as FAIL: a call that could not be made never counts as PASS.
r() {
  local o
  if o="$(aws "$@" --output text 2>/dev/null)"; then printf '%s\n' "$o" | tr -d '\r'
  else echo "ERROR"; fi
}
FAILS=0; WARNS=0
say() { printf '%s\n' "$*"; if [ -n "$OUT" ]; then printf '%s\n' "$*" >> "$OUT"; fi; }
pass() { say "PASS  $*"; }
fail() { say "FAIL  $*"; FAILS=$((FAILS + 1)); }
warn() { say "WARN  $*"; WARNS=$((WARNS + 1)); }
iq() { r ec2 describe-instances --region "$REGION" --instance-ids "$INSTANCE" \
         --query "Reservations[0].Instances[0].$1"; }

say "Localization OS hosted instance - security evidence check"
say "Date (UTC):   $(date -u '+%Y-%m-%d %H:%M:%S')"
ACCOUNT="$(r sts get-caller-identity --query Account)"
say "Account:      $ACCOUNT"
say "Instance:     $INSTANCE   region: $REGION   copy region: $COPY_REGION"
say "Checked by:   $(r sts get-caller-identity --query Arn)"
say "------------------------------------------------------------------"

# 1. Encryption at rest
[ "$(r ec2 get-ebs-encryption-by-default --region "$REGION" --query EbsEncryptionByDefault)" = "True" ] \
  && pass "EBS encryption by default is ON in $REGION" \
  || fail "EBS encryption by default is OFF in $REGION"
vols="$(r ec2 describe-volumes --region "$REGION" \
  --filters "Name=attachment.instance-id,Values=$INSTANCE" --query 'Volumes[].[VolumeId,Encrypted]')"
if [ -z "$vols" ] || [ "$vols" = "ERROR" ]; then fail "could not list volumes of $INSTANCE"
else
  while read -r vid enc; do
    [ "$enc" = "True" ] && pass "volume $vid is encrypted" || fail "volume $vid is NOT encrypted"
  done <<< "$vols"
fi

# 2. Instance metadata and Systems Manager
[ "$(iq MetadataOptions.HttpEndpoint)" = "enabled" ] \
  && pass "IMDS endpoint is enabled" || fail "IMDS endpoint is not enabled"
[ "$(iq MetadataOptions.HttpTokens)" = "required" ] \
  && pass "IMDSv2 is required" || fail "IMDSv2 is not required (http-tokens optional)"
[ "$(iq MetadataOptions.HttpPutResponseHopLimit)" = "1" ] \
  && pass "IMDS response hop limit is 1" || fail "IMDS response hop limit is not 1"
[ "$(iq MetadataOptions.State)" = "applied" ] \
  && pass "IMDS settings are applied" || fail "IMDS settings are not yet applied"
ping="$(r ssm describe-instance-information --region "$REGION" \
  --filters "Key=InstanceIds,Values=$INSTANCE" --query 'InstanceInformationList[0].PingStatus')"
[ "$ping" = "Online" ] && pass "registered in Systems Manager (Online)" \
  || fail "not Online in Systems Manager (status: $ping)"

# 3. GuardDuty
det="$(r guardduty list-detectors --region "$REGION" --query 'DetectorIds[0]')"
if [ -z "$det" ] || [ "$det" = "None" ] || [ "$det" = "ERROR" ]; then
  fail "no GuardDuty detector in $REGION"
else
  gd() { r guardduty get-detector --region "$REGION" --detector-id "$det" --query "$1"; }
  [ "$(gd Status)" = "ENABLED" ] && pass "GuardDuty detector $det enabled" || fail "GuardDuty detector $det not enabled"
  [ "$(gd "Features[?Name=='RUNTIME_MONITORING'] | [0].Status")" = "ENABLED" ] \
    && pass "GuardDuty Runtime Monitoring enabled" || fail "GuardDuty Runtime Monitoring not enabled"
  [ "$(gd "Features[?Name=='RUNTIME_MONITORING'] | [0].AdditionalConfiguration[?Name=='EC2_AGENT_MANAGEMENT'] | [0].Status")" = "ENABLED" ] \
    && pass "GuardDuty EC2 agent management enabled" || fail "GuardDuty EC2 agent management not enabled"
  cov="$(r guardduty list-coverage --region "$REGION" --detector-id "$det" \
    --query "Resources[?ResourceId=='$INSTANCE'] | [0].CoverageStatus")"
  [ "$cov" = "HEALTHY" ] && pass "GuardDuty runtime agent healthy on the instance" \
    || warn "GuardDuty runtime coverage for the instance: $cov (can take up to an hour after launch)"
fi

# 4. Amazon Inspector
[ "$(r inspector2 batch-get-account-status --region "$REGION" --query 'accounts[0].resourceState.ec2.status')" = "ENABLED" ] \
  && pass "Inspector EC2 scanning enabled" || fail "Inspector EC2 scanning not enabled"
scan="$(r inspector2 list-coverage --region "$REGION" \
  --filter-criteria "{\"resourceId\":[{\"comparison\":\"EQUALS\",\"value\":\"$INSTANCE\"}]}" \
  --query 'coveredResources[0].scanStatus.statusCode')"
[ "$scan" = "ACTIVE" ] && pass "Inspector is actively scanning the instance" \
  || fail "Inspector is not scanning the instance (status: $scan)"

# 5. Snapshot policy (Data Lifecycle Manager)
pid="$(r dlm get-lifecycle-policies --region "$REGION" --query "Policies[?Description=='$DLM_DESC'] | [0].PolicyId")"
if [ -z "$pid" ] || [ "$pid" = "None" ] || [ "$pid" = "ERROR" ]; then
  fail "no DLM policy named '$DLM_DESC'"
else
  dq() { r dlm get-lifecycle-policy --region "$REGION" --policy-id "$pid" --query "Policy.$1"; }
  [ "$(dq State)" = "ENABLED" ] && pass "DLM policy $pid is ENABLED" || fail "DLM policy $pid is not ENABLED"
  [ "$(dq "PolicyDetails.TargetTags[?Key=='Backup'] | [0].Value")" = "daily" ] \
    && pass "DLM policy targets Backup=daily" || fail "DLM policy does not target Backup=daily"
  [ "$(dq 'PolicyDetails.Schedules[0].RetainRule.Count')" = "35" ] \
    && pass "DLM keeps 35 daily snapshots" || fail "DLM retention is not 35"
  xr="$(dq 'PolicyDetails.Schedules[0].CrossRegionCopyRules[0].[Target || TargetRegion, Encrypted, RetainRule.Interval, RetainRule.IntervalUnit]')"
  [ "$(echo $xr)" = "$COPY_REGION True 35 DAYS" ] \
    && pass "DLM copies to $COPY_REGION, encrypted, kept 35 days" \
    || fail "DLM cross-region copy rule is not '$COPY_REGION encrypted 35 DAYS' (found: $(echo $xr))"
fi
others="$(r ec2 describe-volumes --region "$REGION" --filters Name=tag:Backup,Values=daily \
  --query 'Volumes[].[VolumeId, Attachments[0].InstanceId]')"
if [ "$others" = "ERROR" ]; then fail "could not list volumes tagged Backup=daily"
else
  extra="$(printf '%s\n' "$others" | awk -v i="$INSTANCE" 'NF && $2 != i {print $1}' | tr '\n' ' ')"
  [ -z "$extra" ] && pass "only this instance's volumes are tagged Backup=daily" \
    || warn "other volumes tagged Backup=daily are also snapshotted and copied: $extra"
fi

# 6. Snapshots actually exist, recently, in both regions
now="$(date -u +%s)"
# age_hours ISO-TIME: whole hours since that time
age_hours() { local epoch; epoch="$(date -u -d "$1" +%s)" || return 1; echo $(( (now - epoch) / 3600 )); }
if [ -z "$vols" ] || [ "$vols" = "ERROR" ] || [ "$vols" = "None" ]; then
  fail "cannot verify backups without the instance's volumes"
elif [ -z "$pid" ] || [ "$pid" = "ERROR" ] || [ "$pid" = "None" ]; then
  fail "cannot verify backups without the intended DLM policy"
else
  for vid in $(printf '%s\n' "$vols" | awk '{print $1}'); do
    snapshots="$(r ec2 describe-snapshots --region "$REGION" --owner-ids self \
      --filters "Name=volume-id,Values=$vid" "Name=status,Values=completed" \
        "Name=tag:aws:dlm:lifecycle-policy-id,Values=$pid" \
      --query 'sort_by(Snapshots,&StartTime)[].[SnapshotId,StartTime]')"
    if [ "$snapshots" = "ERROR" ]; then
      fail "could not list completed snapshots of $vid"
      fail "could not establish source snapshots for copies of $vid in $COPY_REGION"
      continue
    elif [ -z "$snapshots" ] || [ "$snapshots" = "None" ]; then
      fail "no completed snapshot of $vid under DLM policy $pid"
      fail "no completed source snapshot for copies of $vid in $COPY_REGION"
      continue
    fi
    # Find the newest usable source and collect recent source IDs. Do not rely on
    # destination VolumeId: EC2 copies do not preserve the original volume ID.
    youngest=2147483647; source_ids=""
    while read -r sid started; do
      if ! age_h="$(age_hours "$started")"; then
        fail "invalid snapshot time for $sid of $vid"; continue
      fi
      [ "$age_h" -ge 0 ] || { fail "future snapshot time for $sid of $vid"; continue; }
      [ "$age_h" -ge "$youngest" ] || youngest="$age_h"
      if [ "$age_h" -lt 50 ]; then source_ids="${source_ids:+$source_ids,}$sid"; fi
    done <<< "$snapshots"
    [ "$youngest" -lt 26 ] && pass "newest completed snapshot of $vid is ${youngest} h old" \
      || fail "no completed snapshot of $vid younger than 26 h"

    # A previous daily snapshot may still be the latest completed copy. Limit both
    # source and copy age to 50 h, so copying old data cannot renew recovery freshness.
    # Copy lineage ASSUMPTION (unverified offline, confirm on the first real install with a
    # read-only describe-snapshots in the copy region): a us-west-2 copy belongs to this volume
    # if it carries the aws:dlm:source-snapshot-id / aws:dlm:source-region tags for a recent
    # source snapshot OR, as a fallback, its Description names a recent source snapshot id and
    # the source region (CopySnapshot writes both into the default description). Description
    # ids and regions are matched as whole tokens, so snap-a never matches snap-ab and
    # us-east-2 never matches us-east-20. Both paths require completed state.
    if [ -z "$source_ids" ]; then
      fail "no recent completed source snapshot for copies of $vid in $COPY_REGION"
      continue
    fi
    copy_newest="$(r ec2 describe-snapshots --region "$COPY_REGION" --owner-ids self       --filters "Name=status,Values=completed"         "Name=tag:aws:dlm:lifecycle-policy-id,Values=$pid"         "Name=tag:aws:dlm:source-region,Values=$REGION"         "Name=tag:aws:dlm:source-snapshot-id,Values=$source_ids"       --query 'sort_by(Snapshots,&StartTime)[-1].StartTime')"
    copy_rows="$(r ec2 describe-snapshots --region "$COPY_REGION" --owner-ids self       --filters "Name=status,Values=completed"       --query 'sort_by(Snapshots,&StartTime)[].[StartTime,Description]')"
    if [ "$copy_newest" = "ERROR" ] || [ "$copy_rows" = "ERROR" ]; then
      fail "could not list snapshot copies of $vid in $COPY_REGION"; continue
    fi
    [ "$copy_newest" = "None" ] && copy_newest=""
    tok='(^|[^A-Za-z0-9-])'; tokend='([^A-Za-z0-9-]|$)'
    desc_newest=""
    while IFS=$'	' read -r cstart cdesc; do
      [ -n "$cstart" ] || continue
      [[ "$cdesc" =~ ${tok}${REGION}${tokend} ]] || continue
      for sid in ${source_ids//,/ }; do
        if [[ "$cdesc" =~ ${tok}${sid}${tokend} ]]; then desc_newest="$cstart"; break; fi
      done
    done <<< "$copy_rows"
    # Newest candidate across both lineage paths (rows are sorted oldest to newest).
    copy_age=""
    for cand in "$copy_newest" "$desc_newest"; do
      [ -n "$cand" ] || continue
      if ! age_h="$(age_hours "$cand")"; then
        fail "invalid snapshot copy time for $vid in $COPY_REGION"; continue 2
      fi
      if [ -z "$copy_age" ] || [ "$age_h" -lt "$copy_age" ]; then copy_age="$age_h"; fi
    done
    if [ -z "$copy_age" ]; then
      fail "no completed snapshot copy of $vid in $COPY_REGION from a recent source snapshot"
    else
      [ "$copy_age" -ge 0 ] && [ "$copy_age" -lt 50 ]         && pass "newest completed snapshot copy of $vid in $COPY_REGION is ${copy_age} h old"         || fail "newest completed snapshot copy of $vid in $COPY_REGION is ${copy_age} h old (limit 50 h)"
    fi
  done
fi

# 7. Network exposure (security groups)
# rules_for SG PORT: one line per inbound rule letting tcp PORT in (tcp ranges covering PORT or
# "All traffic"), as protocol|from|to|ipv4 sources|ipv6 sources. Prints ERROR if the call fails.
rules_for() {
  r ec2 describe-security-groups --region "$REGION" --group-ids "$1" --query \
    'SecurityGroups[0].IpPermissions[?IpProtocol==`"-1"` || (IpProtocol==`"tcp"` && FromPort<=`'"$2"'` && ToPort>=`'"$2"'`)].join(`"|"`, [IpProtocol, to_string(FromPort), to_string(ToPort), join(`","`, IpRanges[].CidrIp), join(`","`, Ipv6Ranges[].CidrIpv6)])' \
    | tr '\t' '\n' | sed '/^$/d'
}
# sources: every source range in those rule lines, one per line
sources() { awk -F'|' '{print $4 "," $5}' | tr ',' '\n' | sed '/^$/d' | sort -u; }
is_world() { grep -qxE '0\.0\.0\.0/0|::/0'; }

cf="$( { curl -fsS https://www.cloudflare.com/ips-v4 && echo && curl -fsS https://www.cloudflare.com/ips-v6; } 2>/dev/null | tr -d '\r' | sed '/^$/d' || true)"
[ "$(printf '%s\n' "$cf" | grep -c /)" -ge 10 ] || cf=""
policy="$(dirname "$0")/network_policy.py"
[ -f "$policy" ] || { echo "ERROR: Upload network_policy.py beside this script." >&2; exit 1; }
if ! printf '%s\n' "$cf" | python3 "$policy" cidrs; then
  fail "Cloudflare CIDR validation failed"
  cf=""
fi

sgs="$(iq 'SecurityGroups[].GroupId')"
if [ -z "$sgs" ] || [ "$sgs" = "None" ] || [ "$sgs" = "ERROR" ]; then
  fail "could not read the instance's security groups"
  sgs=""
fi
for sg in $sgs; do
  if [ -n "$cf" ] && aws ec2 describe-security-groups --region "$REGION" \
      --group-ids "$sg" --output json | python3 "$policy" "$cf"; then
    pass "$sg: only Cloudflare TCP 80/443 inbound rules"
  else
    fail "$sg: unexpected inbound rules or network policy check failed"
  fi
done
for port in 22 80 443; do
  all=""; err=0
  for sg in $sgs; do
    out="$(rules_for "$sg" "$port")"
    if printf '%s\n' "$out" | grep -qx ERROR; then err=1; else all="$all$out"$'\n'; fi
  done
  [ -n "$sgs" ] || continue
  src="$(printf '%s' "$all" | sources)"
  if [ "$err" = 1 ]; then fail "port $port: could not read a security group ($sgs)"; continue; fi
  if [ "$port" = 22 ]; then
    printf '%s\n' "$src" | is_world && fail "port 22 (SSH) is open to the internet ($sgs)" \
      || pass "port 22 not open to the internet ($sgs)"
    continue
  fi
  if printf '%s\n' "$src" | is_world; then
    fail "port $port open to the whole internet (run aws-instance-setup.sh --cloudflare-only)"
  elif [ -z "$src" ]; then
    warn "port $port: no inbound rule allows it (the site is unreachable)"
  elif [ -z "$cf" ]; then
    fail "port $port: could not fetch Cloudflare's ranges to check the sources"
  else
    non_cf="$(printf '%s\n' "$src" | grep -vxF -f <(printf '%s\n' "$cf") | tr '\n' ' ' || true)"
    [ -z "$non_cf" ] && pass "port $port: every source is a current Cloudflare range" \
      || fail "port $port: also open to non-Cloudflare sources: $non_cf"
  fi
done

# 8. After-hours alerting (aws-alerting-setup.sh). Every read goes through r, so a failed
# call is ERROR and every check below turns ERROR into FAIL, never PASS.
tab="$(printf '\t')"
if ! printf '%s' "$ACCOUNT" | grep -Eq '^[0-9]{12}$'; then
  fail "alerting: could not read the account id, so none of the alerting checks could run"
else
  TOPIC_ARN="arn:aws:sns:$REGION:$ACCOUNT:$TOPIC"
  TOPIC1_ARN="arn:aws:sns:$HC_REGION:$ACCOUNT:$TOPIC"
  RULE_ARN="arn:aws:events:$REGION:$ACCOUNT:rule/$RULE"
  # Every confirmed email must require authenticated unsubscribe, in BOTH regions.
  topic_check() {
    local rg="$1" arn="$2" t subs ok pend endpoint sub authenticated
    t="$(r sns get-topic-attributes --region "$rg" --topic-arn "$arn" --query Attributes.TopicArn)"
    if [ "$t" != "$arn" ]; then fail "alerting: SNS topic $arn missing or unreadable"; return; fi
    pass "alerting: SNS topic $TOPIC exists in $rg"
    subs="$(r sns list-subscriptions-by-topic --region "$rg" --topic-arn "$arn" \
      --query "Subscriptions[?Protocol=='email'].[Endpoint,SubscriptionArn]")"
    if [ "$subs" = "ERROR" ]; then fail "alerting: could not list the email subscriptions of $TOPIC ($rg)"; return; fi
    ok="$(printf '%s\n' "$subs" | awk -F'\t' '$2 ~ /^arn:aws:sns:/ {printf "%s ", $1}')"
    pend="$(printf '%s\n' "$subs" | awk -F'\t' '$2 !~ /^arn:aws:sns:/ && NF > 1 {printf "%s ", $1}')"
    if [ -n "$ok" ]; then pass "alerting: email subscription confirmed on $TOPIC ($rg): $ok"
    elif [ -n "$pend" ]; then
      fail "alerting: PendingConfirmation ($rg): use authenticated confirmation in LAUNCH.md section 7 step 2"
    else fail "alerting: no email subscription on $TOPIC ($rg)"; fi
    while IFS="$tab" read -r endpoint sub; do
      case "$sub" in arn:aws:sns:*) ;; *) continue ;; esac
      authenticated="$(r sns get-subscription-attributes --region "$rg" \
        --subscription-arn "$sub" --query Attributes.ConfirmationWasAuthenticated)"
      if [ "$authenticated" = "true" ]; then
        pass "alerting: authenticated unsubscribe required ($rg): $endpoint"
      else
        fail "alerting: unsubscribe protection missing or unreadable ($rg): $endpoint"
      fi
    done <<< "$subs"
  }
  topic_check "$REGION" "$TOPIC_ARN"
  topic_check "$HC_REGION" "$TOPIC1_ARN"
  pol="$(r sns get-topic-attributes --region "$REGION" --topic-arn "$TOPIC_ARN" --query Attributes.Policy | tr -d ' \t\n')"
  case "$pol" in
    ERROR) fail "alerting: could not read the policy of $TOPIC ($REGION)" ;;
    *events.amazonaws.com*"$RULE_ARN"*) pass "alerting: topic policy lets EventBridge rule $RULE publish" ;;
    *) fail "alerting: topic policy does not let EventBridge rule $RULE publish (GuardDuty emails would be dropped)" ;;
  esac

  # GuardDuty High/Critical -> EventBridge -> SNS
  rule="$(r events describe-rule --region "$REGION" --name "$RULE" --query '[State, EventPattern]' | tr -d ' \t\n')"
  case "$rule" in
    ENABLED*'"aws.guardduty"'*'"GuardDutyFinding"'*'">=",7'*) pass "alerting: EventBridge rule $RULE enabled (GuardDuty severity 7 or more)" ;;
    ERROR) fail "alerting: EventBridge rule $RULE missing or unreadable" ;;
    *) fail "alerting: EventBridge rule $RULE is not enabled with the GuardDuty severity>=7 pattern (found: ${rule:0:80})" ;;
  esac
  tgt="$(r events list-targets-by-rule --region "$REGION" --rule "$RULE" --query "Targets[?Arn=='$TOPIC_ARN'].Id")"
  if [ "$tgt" = "ERROR" ]; then fail "alerting: could not read the targets of rule $RULE"
  elif [ -n "$tgt" ] && [ "$tgt" != "None" ]; then pass "alerting: rule $RULE sends to $TOPIC"
  else fail "alerting: rule $RULE does not send to $TOPIC_ARN"; fi

  # alarm_check REGION NAME "ALARM ACTIONS" "OK ACTIONS" DIM-or-dash "METRIC STATISTIC COMPARISON THRESHOLD" LABEL
  # The fields are joined with | (not tabs), so an empty list (no OK actions) cannot shift them.
  alarm_check() {
    local rg="$1" name="$2" want="$3" want_o="$4" want_d="$5" want_m="$6" label="$7" out a o d st en m miss="" misso=""
    out="$(r cloudwatch describe-alarms --region "$rg" --alarm-names "$name" --query "$ALARM_Q")"
    if [ "$out" = "ERROR" ]; then fail "alerting: could not read alarm $name ($rg)"; return; fi
    if [ -z "$out" ] || [ "$out" = "None" ]; then fail "alerting: alarm $name missing ($rg)"; return; fi
    IFS='|' read -r a o d st en m <<< "$out"
    m="$(printf '%s' "$m" | tr '|' ' ' | sed 's/\.0$//')"
    for w in $want; do case " $a " in *" $w "*) ;; *) miss="$miss $w" ;; esac; done
    for w in $want_o; do case " $o " in *" $w "*) ;; *) misso="$misso $w" ;; esac; done
    if [ -n "$miss" ]; then fail "alerting: alarm $name lacks alarm action(s):$miss"; return; fi
    if [ -n "$misso" ]; then fail "alerting: alarm $name does not email when it clears (OK action missing:$misso)"; return; fi
    if [ "$m" != "$want_m" ]; then fail "alerting: alarm $name watches '$m', want '$want_m'"; return; fi
    if [ "$want_d" != "-" ] && [ "$d" != "$want_d" ]; then
      fail "alerting: alarm $name watches health check $d, not $want_d"; return
    fi
    if [ "$en" != "true" ]; then fail "alerting: alarm $name has its actions switched off (ActionsEnabled=$en): it sends nothing and does nothing"; return; fi
    case "$st" in
      ALARM) fail "alerting: alarm $name is IN ALARM now ($label)" ;;
      OK) pass "alerting: alarm $name set ($label), state OK" ;;
      *) warn "alerting: alarm $name set ($label), state $st (normal for a few minutes after creation)" ;;
    esac
  }
  # Same query as aws-alerting-setup.sh: a missing alarm gives "None", never a JMESPath error.
  ALARM_Q='MetricAlarms[:1].join(`"|"`, [join(`" "`, sort(AlarmActions || `[]`)), join(`" "`, sort(OKActions || `[]`)), (Dimensions[?Name==`"HealthCheckId"`].Value)[0] || `"-"`, StateValue || `"-"`, to_string(ActionsEnabled), MetricName || `"-"`, Statistic || `"-"`, ComparisonOperator || `"-"`, to_string(Threshold)]) | [0]'
  alarm_check "$REGION" "locos-ec2-system-check-$INSTANCE" \
    "arn:aws:automate:$REGION:ec2:recover $TOPIC_ARN" "$TOPIC_ARN" "-" \
    "StatusCheckFailed_System Maximum GreaterThanOrEqualToThreshold 1" "system status check: recover + email"
  alarm_check "$REGION" "locos-ec2-instance-check-$INSTANCE" \
    "arn:aws:automate:$REGION:ec2:reboot $TOPIC_ARN" "$TOPIC_ARN" "-" \
    "StatusCheckFailed_Instance Maximum GreaterThanOrEqualToThreshold 1" "instance status check: reboot + email"

  # Route 53 health check on /ready, and its alarm in us-east-1
  hcs="$(r route53 list-health-checks --region "$HC_REGION" --query \
    "HealthChecks[?HealthCheckConfig.ResourcePath=='/ready'].[Id, HealthCheckConfig.FullyQualifiedDomainName, HealthCheckConfig.ResourcePath, HealthCheckConfig.Type, to_string(HealthCheckConfig.Port), to_string(HealthCheckConfig.RequestInterval), to_string(HealthCheckConfig.FailureThreshold), to_string(HealthCheckConfig.EnableSNI)]")"
  hcid=""
  if [ "$hcs" = "ERROR" ]; then fail "alerting: could not list the Route 53 health checks"
  else
    line="$(printf '%s\n' "$hcs" | awk -F'\t' -v h="$HOST" 'NF > 1 && $3 == "/ready" && (h == "" || $2 == h) {print; exit}')"
    if [ -z "$line" ]; then fail "alerting: no Route 53 health check on /ready${HOST:+ for $HOST}"
    else
      IFS="$tab" read -r hcid hc_host _ hc_type hc_port hc_int hc_thr hc_sni <<< "$line"
      [ "$hc_type $hc_port $hc_int $hc_thr $hc_sni" = "HTTPS 443 30 3 true" ] \
        && pass "alerting: Route 53 health check $hcid on https://$hc_host/ready (HTTPS, SNI, 30 s, 3 failures)" \
        || fail "alerting: health check $hcid settings are '$hc_type $hc_port $hc_int $hc_thr $hc_sni', want 'HTTPS 443 30 3 true'"
      obs="$(r route53 get-health-check-status --region "$HC_REGION" --health-check-id "$hcid" \
        --query 'HealthCheckObservations[].StatusReport.Status' | tr '\t' '\n' | sed '/^$/d')"
      if [ "$obs" = "ERROR" ]; then fail "alerting: could not read the status of health check $hcid"
      else
        total="$(printf '%s\n' "$obs" | sed '/^$/d' | wc -l | tr -d ' ')"
        good="$(printf '%s\n' "$obs" | grep -c '^Success' || true)"
        # Route 53 calls an endpoint healthy when more than 18% of its checkers see it healthy.
        if [ "$total" -gt 0 ] && [ $((good * 100)) -gt $((total * 18)) ]; then
          pass "alerting: health check $hcid is healthy ($good of $total checkers report Success)"
        else
          fail "alerting: health check $hcid is NOT healthy ($good of $total checkers report Success): $(printf '%s\n' "$obs" | grep -v '^Success' | head -1)"
        fi
      fi
    fi
  fi
  if [ -n "$hcid" ]; then
    alarm_check "$HC_REGION" "$HC_ALARM" "$TOPIC1_ARN" "$TOPIC1_ARN" "$hcid" \
      "HealthCheckStatus Minimum LessThanThreshold 1" "site down: email"
  else
    fail "alerting: alarm $HC_ALARM not checked (no health check found)"
  fi

  # The instance role may publish to that ONE topic (the on-box audit-chain check)
  prof="$(iq IamInstanceProfile.Arn)"
  if [ -z "$prof" ] || [ "$prof" = "None" ] || [ "$prof" = "ERROR" ]; then
    fail "alerting: could not read the instance's IAM role (needed for the audit-chain email)"
  else
    role="$(r iam get-instance-profile --instance-profile-name "${prof##*/}" --query 'InstanceProfile.Roles[0].RoleName')"
    ip="$(r iam get-role-policy --role-name "$role" --policy-name "$POLICY_NAME" --query \
      "PolicyDocument.Statement[].[Effect, join(',', to_array(Action)), join(',', to_array(Resource))]")"
    if [ -z "$role" ] || [ "$role" = "None" ] || [ "$role" = "ERROR" ]; then
      fail "alerting: could not read the role inside instance profile ${prof##*/}"
    elif [ "$ip" = "ERROR" ]; then
      fail "alerting: inline policy $POLICY_NAME missing or unreadable on role $role"
    elif [ "$ip" = "Allow${tab}sns:Publish${tab}$TOPIC_ARN" ]; then
      pass "alerting: role $role may sns:Publish to $TOPIC only ($POLICY_NAME)"
    else
      fail "alerting: $POLICY_NAME on role $role is not exactly sns:Publish on $TOPIC_ARN (found: $(printf '%s' "$ip" | tr '\t\n' '  '))"
    fi
  fi
  say "NOTE  the on-box audit-chain timer is not visible to AWS: check it on the box with"
  say "      systemctl list-timers locos-audit-alert.timer  (LAUNCH.md section 7)"
fi

say "------------------------------------------------------------------"
say "Result: $FAILS FAIL, $WARNS WARN"
[ "$FAILS" -eq 0 ]
