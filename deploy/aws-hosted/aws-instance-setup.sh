#!/usr/bin/env bash
# aws-instance-setup.sh - harden and back up ONE launched EC2 instance.
#
# Run AFTER the instance is launched (and after aws-account-baseline.sh).
# Safe to run again: every step checks first and only changes what is missing.
#
# What it does:
#   1. Requires IMDSv2 with response hop limit 1 (host access, not Docker bridge access)
#   2. Makes sure the instance has an IAM role with AmazonSSMManagedInstanceCore
#      (needed by Systems Manager, GuardDuty's runtime agent and Inspector).
#      Creates role + instance profile "LocOS-Instance-SSM" if the instance has none;
#      if it already has one, the policy is added to THAT role (nothing is replaced).
#   3. Tags every EBS volume of the instance Backup=daily
#   4. Creates (or updates) the Data Lifecycle Manager policy "LocOS daily EBS snapshots":
#      daily at 07:00 UTC, keep 35, copy tags, encrypted copy to the copy region kept 35 days
#   5. With --cloudflare-only: web ports 80/443 open ONLY to Cloudflare's published ranges
#      (the world-open 80/443 rules are removed only after the Cloudflare rules exist).
#      Any port 22 rule open to the internet is REPORTED, never added. Afterwards the groups
#      are read again and the script fails if anything still lets the internet reach 80/443.
#   Scope: only the named instance's volumes are tagged; the snapshot policy covers every
#   volume in the region tagged Backup=daily, so the script warns about any others.
#
# Usage:
#   bash aws-instance-setup.sh --instance-id i-0123456789abcdef0 [--region us-east-2]
#        [--copy-region us-west-2] [--cloudflare-only] [--dry-run]
#   --dry-run reads the current state and PRINTS every change instead of making it.
#   Without --dry-run the script first shows that same preview and asks you to type
#   yes before it changes anything (--yes skips the question).
set -euo pipefail
export MSYS_NO_PATHCONV=1

ORIG_ARGS=("$@")
REGION="us-east-2"; COPY_REGION="us-west-2"; INSTANCE=""; CF_ONLY=0; DRY_RUN=0; YES=0
ROLE="LocOS-Instance-SSM"
SSM_POLICY="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
DLM_DESC="LocOS daily EBS snapshots"

usage() { sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --instance-id) INSTANCE="${2:-}"; shift 2 ;;
    --region) REGION="${2:?}"; shift 2 ;;
    --copy-region) COPY_REGION="${2:?}"; shift 2 ;;
    --cloudflare-only) CF_ONLY=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --yes) YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done
if ! [[ "$INSTANCE" =~ ^i-[0-9a-f]{8,17}$ ]]; then  # whole-string match
  echo "ERROR: --instance-id i-... is required (EC2 > Instances > Instance ID column)." >&2
  exit 2
fi
[ "$COPY_REGION" != "$REGION" ] || { echo "ERROR: copy region must differ from $REGION" >&2; exit 2; }

# Preview first, then ask, unless this is already a dry run or --yes was given.
if [ "$DRY_RUN" = 0 ] && [ "$YES" = 0 ]; then
  bash "$0" "${ORIG_ARGS[@]}" --dry-run
  echo
  read -r -p "Apply the changes listed above? Type yes to continue: " answer
  [ "$answer" = "yes" ] || { echo "Stopped. Nothing was changed."; exit 1; }
fi

q() { aws "$@" --region "$REGION" --output text | tr -d '\r'; }
show() {  # print an argument so the line can be pasted back into bash as-is
  case "$1" in
    *[!A-Za-z0-9_./:=,@+-]*|'') printf " '%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")" ;;
    *) printf ' %s' "$1" ;;
  esac
}
change() {
  if [ "$DRY_RUN" = "1" ]; then
    printf '  [dry-run] would run: aws'; for a in "$@"; do show "$a"; done; printf ' --region %s\n' "$REGION"
  else
    echo "  running: aws $1 $2 ..."
    aws "$@" --region "$REGION" --output text | tr -d '\r' >/dev/null
  fi
}
SUMMARY=""
# note: add a line to the final summary. Upper-case status = a change; in a dry run
# it is shown as [planned] because nothing was actually changed.
note() {
  local s="$2"
  case "$s" in [A-Z]*) if [ "$DRY_RUN" = 1 ]; then s="[planned] $s"; fi ;; esac
  SUMMARY="${SUMMARY}$(printf '  %-30s %s' "$1" "$s")"$'\n'
}
iq() { q ec2 describe-instances --instance-ids "$INSTANCE" --query "Reservations[0].Instances[0].$1"; }

echo "== Localization OS instance setup =="
echo "   instance: $INSTANCE  region: $REGION  copy region: $COPY_REGION"
echo "   cloudflare-only: $CF_ONLY  dry-run: $DRY_RUN"
echo "   Planned: IMDSv2, SSM role, Backup=daily tags, DLM policy$( [ "$CF_ONLY" = 1 ] && echo ', Cloudflare-only 80/443')"
echo

# --- 1. IMDSv2 ----------------------------------------------------------------
echo "[1/5] IMDSv2"
if [ "$(iq MetadataOptions.HttpTokens)" = "required" ] &&
   [ "$(iq MetadataOptions.HttpPutResponseHopLimit)" = "1" ] &&
   [ "$(iq MetadataOptions.HttpEndpoint)" = "enabled" ]; then
  echo "  already required, hop limit 1"; note "IMDSv2" "required, hop limit 1"
else
  change ec2 modify-instance-metadata-options --instance-id "$INSTANCE" \
    --http-tokens required --http-endpoint enabled --http-put-response-hop-limit 1
  note "IMDSv2" "NOW REQUIRED, HOP LIMIT 1"
fi

# --- 2. IAM role for Systems Manager --------------------------------------------
echo "[2/5] Instance role with AmazonSSMManagedInstanceCore"
profile_arn="$(iq IamInstanceProfile.Arn)"
if [ -n "$profile_arn" ] && [ "$profile_arn" != "None" ]; then
  profile="${profile_arn##*/}"
  role="$(aws iam get-instance-profile --instance-profile-name "$profile" \
          --query 'InstanceProfile.Roles[0].RoleName' --output text | tr -d '\r')"
  echo "  instance already has profile $profile (role $role)"
  if [ -z "$role" ] || [ "$role" = "None" ]; then
    echo "ERROR: instance profile $profile has no role inside it, so nothing can be attached." >&2
    echo "       Fix: EC2 > Instances > (instance) > Actions > Security > Modify IAM role >" >&2
    echo "       choose No IAM role > Update, then run this script again (it creates $ROLE)." >&2
    exit 1
  fi
  has="$(aws iam list-attached-role-policies --role-name "$role" \
    --query "AttachedPolicies[?PolicyArn=='$SSM_POLICY'].PolicyArn" --output text | tr -d '\r')"
  if [ -n "$has" ]; then
    note "SSM policy on role $role" "already attached"
  else
    change iam attach-role-policy --role-name "$role" --policy-arn "$SSM_POLICY"
    note "SSM policy on role $role" "ATTACHED"
  fi
else
  echo "  instance has no profile; using $ROLE"
  if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
    TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
    change iam create-role --role-name "$ROLE" --assume-role-policy-document "$TRUST" \
      --description "Localization OS instance: Systems Manager access"
  fi
  change iam attach-role-policy --role-name "$ROLE" --policy-arn "$SSM_POLICY"
  # Check the profile AND the role inside it separately, so a run that stopped half-way
  # (profile created, role not yet added) is finished correctly on the next run.
  if ! aws iam get-instance-profile --instance-profile-name "$ROLE" >/dev/null 2>&1; then
    change iam create-instance-profile --instance-profile-name "$ROLE"
    in_profile="None"
  else
    in_profile="$(aws iam get-instance-profile --instance-profile-name "$ROLE" \
      --query 'InstanceProfile.Roles[0].RoleName' --output text | tr -d '\r')"
  fi
  if [ "$in_profile" != "$ROLE" ]; then
    change iam add-role-to-instance-profile --instance-profile-name "$ROLE" --role-name "$ROLE"
    wait_s="${LOCOS_IAM_WAIT:-15}"
    [ "$DRY_RUN" = 1 ] || { echo "  waiting $wait_s s for IAM to propagate"; sleep "$wait_s"; }
  fi
  change ec2 associate-iam-instance-profile --instance-id "$INSTANCE" \
    --iam-instance-profile "Name=$ROLE"
  note "Instance profile" "$ROLE ATTACHED"
fi

# --- 3. Tag volumes Backup=daily --------------------------------------------------
echo "[3/5] Tag EBS volumes Backup=daily"
vols="$(iq 'BlockDeviceMappings[].Ebs.VolumeId')"
[ -n "$vols" ] && [ "$vols" != "None" ] || { echo "ERROR: no EBS volumes found" >&2; exit 1; }
for v in $vols; do
  tag="$(q ec2 describe-volumes --volume-ids "$v" --query "Volumes[0].Tags[?Key=='Backup'].Value | [0]")"
  if [ "$tag" = "daily" ]; then echo "  $v already tagged"
  else change ec2 create-tags --resources "$v" --tags Key=Backup,Value=daily; fi
done
note "Backup=daily tag" "$(echo $vols)"
# The snapshot policy picks up EVERY volume in the region tagged Backup=daily. Only this
# instance's volumes are tagged here; warn about any other volume that already carries it.
others="$(q ec2 describe-volumes --filters Name=tag:Backup,Values=daily \
  --query 'Volumes[].[VolumeId, Attachments[0].InstanceId]' | awk -v i="$INSTANCE" '$2 != i {print $1 " (" $2 ")"}')"
if [ -n "$others" ]; then
  echo "  WARNING: other volumes are also tagged Backup=daily and will be snapshotted and copied too:"
  printf '%s\n' "$others" | sed 's/^/    /'
  note "Other Backup=daily volumes" "present - check them (see output above)"
fi

# --- 4. DLM snapshot policy ---------------------------------------------------------
echo "[4/5] Data Lifecycle Manager policy"
role_arn="$(aws iam get-role --role-name AWSDataLifecycleManagerDefaultRole \
  --query Role.Arn --output text 2>/dev/null | tr -d '\r' || true)"
if [ -z "$role_arn" ]; then
  [ "$DRY_RUN" = 1 ] || { echo "ERROR: run aws-account-baseline.sh first (no DLM default role)" >&2; exit 1; }
  role_arn="arn:aws:iam::ACCOUNT:role/AWSDataLifecycleManagerDefaultRole"
fi
POLICY_FILE="./.locos-dlm-policy-$$.json"   # relative path works for Windows and Linux aws
trap 'rm -f "$POLICY_FILE"' EXIT
printf '%s\n' '{' \
  '  "PolicyType": "EBS_SNAPSHOT_MANAGEMENT",' \
  '  "ResourceTypes": ["VOLUME"],' \
  '  "TargetTags": [{"Key": "Backup", "Value": "daily"}],' \
  '  "Schedules": [{' \
  '    "Name": "Daily 0700 UTC keep 35",' \
  '    "CopyTags": true,' \
  '    "TagsToAdd": [{"Key": "CreatedBy", "Value": "DLM-LocOS"}],' \
  '    "CreateRule": {"Interval": 24, "IntervalUnit": "HOURS", "Times": ["07:00"]},' \
  '    "RetainRule": {"Count": 35},' \
  '    "CrossRegionCopyRules": [{' \
  "      \"Target\": \"$COPY_REGION\"," \
  '      "Encrypted": true,' \
  '      "CopyTags": true,' \
  '      "RetainRule": {"Interval": 35, "IntervalUnit": "DAYS"}' \
  '    }]' \
  '  }]' \
  '}' > "$POLICY_FILE"
if [ "$DRY_RUN" = 1 ]; then echo "  policy details that would be sent:"; sed 's/^/    /' "$POLICY_FILE"; fi
pid="$(q dlm get-lifecycle-policies --query "Policies[?Description=='$DLM_DESC'] | [0].PolicyId")"
if [ -z "$pid" ] || [ "$pid" = "None" ]; then
  change dlm create-lifecycle-policy --execution-role-arn "$role_arn" --description "$DLM_DESC" \
    --state ENABLED --policy-details "file://$POLICY_FILE"
  note "DLM policy" "CREATED"
else
  change dlm update-lifecycle-policy --policy-id "$pid" --execution-role-arn "$role_arn" \
    --description "$DLM_DESC" --state ENABLED --policy-details "file://$POLICY_FILE"
  note "DLM policy $pid" "UPDATED to the standard settings"
fi

# --- 5. Security group -------------------------------------------------------------
echo "[5/5] Security group"
sgs="$(iq 'SecurityGroups[].GroupId')"
[ -n "$sgs" ] && [ "$sgs" != "None" ] || { echo "ERROR: could not read the instance's security groups" >&2; exit 1; }
# rules_for SG PORT: one line per inbound rule that lets tcp PORT in (tcp ranges covering PORT,
# or "All traffic"), as  protocol|from|to|ipv4 sources|ipv6 sources  (UDP/ICMP ignored).
rules_for() {
  q ec2 describe-security-groups --group-ids "$1" --query \
    'SecurityGroups[0].IpPermissions[?IpProtocol==`"-1"` || (IpProtocol==`"tcp"` && FromPort<=`'"$2"'` && ToPort>=`'"$2"'`)].join(`"|"`, [IpProtocol, to_string(FromPort), to_string(ToPort), join(`","`, IpRanges[].CidrIp), join(`","`, Ipv6Ranges[].CidrIpv6)])' \
    | tr '\t' '\n' | sed '/^$/d'
}
# load_world SG PORT: sets WORLD to the rules that let 0.0.0.0/0 or ::/0 reach PORT.
# Runs in THIS shell (not inside $(...)), so a failed read stops the whole script instead of
# looking like "nothing is open".
load_world() {
  local rules
  if ! rules="$(rules_for "$1" "$2")"; then
    echo "ERROR: could not read security group $1 (port $2). The script stopped here; any" >&2
    echo "       change listed above as 'running:' WAS made. Check your AWS sign-in and" >&2
    echo "       permissions, then run this again (it only redoes what is still missing) and" >&2
    echo "       finish with aws-verify.sh to see the real state of the group." >&2
    exit 1
  fi
  WORLD="$(printf '%s\n' "$rules" | awk -F'|' '$4 ~ /(^|,)0\.0\.0\.0\/0(,|$)/ || $5 ~ /(^|,)::\/0(,|$)/')"
}
for sg in $sgs; do
  load_world "$sg" 22
  if [ -n "$WORLD" ]; then
    echo "  WARNING: $sg has port 22 (SSH) open to the internet. Remove that rule in the console"
    echo "           (EC2 > Security Groups > $sg > Inbound rules > Edit). SSH goes over Tailscale."
    note "Port 22 in $sg" "open to the internet (remove it by hand)"
  fi
done
if [ "$CF_ONLY" = 1 ]; then
  sg="${sgs%%[[:space:]]*}"
  echo "  applying Cloudflare-only web access to $sg"
  v4="$(curl -fsS https://www.cloudflare.com/ips-v4 | tr -d '\r')"
  v6="$(curl -fsS https://www.cloudflare.com/ips-v6 | tr -d '\r')"
  [ "$(echo "$v4" | grep -c /)" -ge 10 ] || { echo "ERROR: Cloudflare IPv4 list looks wrong" >&2; exit 1; }
  cf_all="$(printf '%s\n%s\n' "$v4" "$v6")"
  policy="$(dirname "$0")/network_policy.py"
  [ -f "$policy" ] || { echo "ERROR: Upload network_policy.py beside this script." >&2; exit 1; }
  printf '%s\n' "$cf_all" | python3 "$policy" cidrs || exit 1
  # (a) add the current Cloudflare ranges that are missing (exact port rules)
  for port in 80 443; do
    have="$(q ec2 describe-security-groups --group-ids "$sg" --query \
      'SecurityGroups[0].IpPermissions[?IpProtocol==`"tcp"` && FromPort==`'"$port"'` && ToPort==`'"$port"'`].[IpRanges[].CidrIp,Ipv6Ranges[].CidrIpv6][][]' | tr '\t' '\n')"
    r4=""; r6=""
    for c in $v4; do printf '%s\n' "$have" | grep -qxF "$c" || r4="$r4{\"CidrIp\":\"$c\",\"Description\":\"Cloudflare\"},"; done
    for c in $v6; do printf '%s\n' "$have" | grep -qxF "$c" || r6="$r6{\"CidrIpv6\":\"$c\",\"Description\":\"Cloudflare\"},"; done
    if [ -n "$r4$r6" ]; then
      perm="[{\"IpProtocol\":\"tcp\",\"FromPort\":$port,\"ToPort\":$port,\"IpRanges\":[${r4%,}],\"Ipv6Ranges\":[${r6%,}]}]"
      change ec2 authorize-security-group-ingress --group-id "$sg" --ip-permissions "$perm"
    else
      echo "  port $port: Cloudflare ranges already present"
    fi
  done
  # (b) remove every rule that lets the whole internet reach 80 or 443, in its exact shape
  #     (an "All traffic" or port-range rule is removed whole: it opened more than 80/443)
  done_rules=""
  for s in $sgs; do
    for port in 80 443; do
      load_world "$s" "$port"
      while IFS='|' read -r proto from to src4 src6; do
        [ -n "$proto" ] || continue
        key="$s|$proto|$from|$to|$src4|$src6"
        case " $done_rules " in *" $key "*) continue ;; esac
        done_rules="$done_rules $key"
        w4=""; w6=""
        case ",$src4," in *,0.0.0.0/0,*) w4='{"CidrIp":"0.0.0.0/0"}' ;; esac
        case ",$src6," in *,::/0,*) w6='{"CidrIpv6":"::/0"}' ;; esac
        if [ "$proto" = "-1" ]; then
          echo "  NOTE: $s has an \"All traffic\" rule open to the internet; it is removed whole."
          perm="[{\"IpProtocol\":\"-1\",\"IpRanges\":[$w4],\"Ipv6Ranges\":[$w6]}]"
        else
          if [ "$from" != "$to" ]; then echo "  NOTE: $s opens tcp $from-$to to the internet; that whole rule is removed."; fi
          perm="[{\"IpProtocol\":\"tcp\",\"FromPort\":$from,\"ToPort\":$to,\"IpRanges\":[$w4],\"Ipv6Ranges\":[$w6]}]"
        fi
        change ec2 revoke-security-group-ingress --group-id "$s" --ip-permissions "$perm"
      done <<< "$WORLD"
    done
  done
  # (c) remove old Cloudflare-labelled ranges that Cloudflare no longer publishes
  for port in 80 443; do
    old="$(q ec2 describe-security-groups --group-ids "$sg" --query \
      'SecurityGroups[0].IpPermissions[?IpProtocol==`"tcp"` && FromPort==`'"$port"'` && ToPort==`'"$port"'`].[IpRanges[?Description==`"Cloudflare"`].CidrIp,Ipv6Ranges[?Description==`"Cloudflare"`].CidrIpv6][][]' | tr '\t' '\n')"
    for c in $old; do
      printf '%s\n' "$cf_all" | grep -qxF "$c" && continue
      echo "  removing stale Cloudflare range $c on port $port"
      case "$c" in
        *:*) perm="[{\"IpProtocol\":\"tcp\",\"FromPort\":$port,\"ToPort\":$port,\"Ipv6Ranges\":[{\"CidrIpv6\":\"$c\"}]}]" ;;
        *) perm="[{\"IpProtocol\":\"tcp\",\"FromPort\":$port,\"ToPort\":$port,\"IpRanges\":[{\"CidrIp\":\"$c\"}]}]" ;;
      esac
      change ec2 revoke-security-group-ingress --group-id "$sg" --ip-permissions "$perm"
    done
  done
  # (d) read the groups again: nothing may still let the whole internet reach 80 or 443
  if [ "$DRY_RUN" = 0 ]; then
    left=""
    for s in $sgs; do for port in 80 443; do
      load_world "$s" "$port"; [ -z "$WORLD" ] || left="$left $s:$port($WORLD)"
    done; done
    if [ -n "$left" ]; then
      echo "ERROR: web ports are STILL open to the whole internet after the change:$left" >&2
      echo "       Fix by hand: EC2 > Security Groups > (group) > Inbound rules > Edit inbound rules," >&2
      echo "       delete every rule with source 0.0.0.0/0 or ::/0 that covers port 80 or 443." >&2
      exit 1
    fi
    for s in $sgs; do
      aws ec2 describe-security-groups --region "$REGION" --group-ids "$s" --output json \
        | python3 "$policy" "$cf_all" || {
          echo "ERROR: remove all inbound rules except Cloudflare TCP 80/443 rules." >&2
          exit 1
        }
    done
    echo "  re-checked: only Cloudflare TCP 80/443 inbound rules remain"
  fi
  note "Web ports 80/443" "Cloudflare-only on $sg"
else
  note "Web ports 80/443" "unchanged (use --cloudflare-only after Cloudflare is live)"
fi

echo
echo "== Summary ($INSTANCE) =="
printf '%s' "$SUMMARY"
if [ "$DRY_RUN" = 1 ]; then echo "Dry run only: nothing was changed."
else echo "Done. Next: bash aws-verify.sh --instance-id $INSTANCE"; fi
