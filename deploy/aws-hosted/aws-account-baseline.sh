#!/usr/bin/env bash
# aws-account-baseline.sh - one-time security baseline for an AWS account + region.
#
# Run this ONCE per account and region, BEFORE launching the university instance.
# It is safe to run again: it checks each item first and only changes what is off.
#
# What it turns on:
#   1. EBS encryption by default (every new disk, snapshot and image is encrypted)
#   2. GuardDuty threat detection, with Runtime Monitoring and automatic agent
#      management for EC2
#   3. Amazon Inspector vulnerability scanning for EC2 (and ECR container images)
#   4. The Data Lifecycle Manager default role (needed for the daily snapshot policy)
#
# Scope: GuardDuty's EC2 agent and Inspector cover EVERY instance in the region, including
# the demo box if it lives in this account and region (an agent is installed on it and it is
# billed per instance). To leave an instance out, tag it GuardDutyManaged=false (no agent) and
# InspectorEc2Exclusion=true (no scanning). The script lists the other instances it will cover.
#
# Where to run it:
#   - AWS CloudShell (recommended): open the AWS console, click the CloudShell icon
#     (the >_ symbol in the top bar), Actions > Upload file, pick this file, then:
#         bash aws-account-baseline.sh --dry-run
#         bash aws-account-baseline.sh
#   - Or locally in Git Bash once AWS CLI v2 is installed and signed in.
#
# Options:
#   --region R   AWS region (default us-east-2)
#   --dry-run    read the current state, then PRINT every change instead of making it
#   --yes        skip the "type yes" question (normally the script previews first, then asks)
#   --help       show this help
set -euo pipefail
export MSYS_NO_PATHCONV=1   # stop Git Bash on Windows rewriting "/..." arguments

ORIG_ARGS=("$@")
REGION="us-east-2"
DRY_RUN=0
YES=0

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --region) REGION="${2:?--region needs a value}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --yes) YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

# Preview first, then ask, unless this is already a dry run or --yes was given.
if [ "$DRY_RUN" = 0 ] && [ "$YES" = 0 ]; then
  bash "$0" "${ORIG_ARGS[@]}" --dry-run
  echo
  read -r -p "Apply the changes listed above? Type yes to continue: " answer
  [ "$answer" = "yes" ] || { echo "Stopped. Nothing was changed."; exit 1; }
fi

# --- helpers -----------------------------------------------------------------
# q: a READ-ONLY aws call in text mode. Strips Windows carriage returns.
q() { aws "$@" --region "$REGION" --output text | tr -d '\r'; }

# show: print an argument so the line can be pasted back into bash as-is.
show() {
  case "$1" in
    *[!A-Za-z0-9_./:=,@+-]*|'') printf " '%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")" ;;
    *) printf ' %s' "$1" ;;
  esac
}
# change: a command that CHANGES something. In --dry-run it is only printed.
change() {
  if [ "$DRY_RUN" = "1" ]; then
    printf '  [dry-run] would run: aws'
    for a in "$@"; do show "$a"; done
    printf ' --region %s\n' "$REGION"
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
  SUMMARY="${SUMMARY}$(printf '  %-34s %s' "$1" "$s")"$'\n'
}

command -v aws >/dev/null 2>&1 || {
  echo "ERROR: the aws command is not installed. Use AWS CloudShell, or install AWS CLI v2." >&2
  exit 1
}

ACCOUNT="$(q sts get-caller-identity --query Account)"
echo "== Localization OS account baseline =="
echo "   account: $ACCOUNT   region: $REGION   dry-run: $DRY_RUN"
echo

# --- 1. EBS encryption by default --------------------------------------------
echo "[1/4] EBS encryption by default"
ebs="$(q ec2 get-ebs-encryption-by-default --query EbsEncryptionByDefault)"
if [ "$ebs" = "True" ]; then
  echo "  already ON"; note "EBS encryption by default" "already on"
else
  change ec2 enable-ebs-encryption-by-default
  note "EBS encryption by default" "TURNED ON"
fi

# --- 2. GuardDuty + Runtime Monitoring + EC2 agent management ------------------
echo "[2/4] GuardDuty"
FEATURES='[{"Name":"RUNTIME_MONITORING","Status":"ENABLED","AdditionalConfiguration":[{"Name":"EC2_AGENT_MANAGEMENT","Status":"ENABLED"}]}]'
det="$(q guardduty list-detectors --query 'DetectorIds[0]')"
if [ -z "$det" ] || [ "$det" = "None" ]; then
  change guardduty create-detector --enable --features "$FEATURES"
  note "GuardDuty detector" "CREATED with runtime monitoring"
else
  status="$(q guardduty get-detector --detector-id "$det" --query Status)"
  rm_status="$(q guardduty get-detector --detector-id "$det" \
    --query "Features[?Name=='RUNTIME_MONITORING'] | [0].Status")"
  agent_status="$(q guardduty get-detector --detector-id "$det" \
    --query "Features[?Name=='RUNTIME_MONITORING'] | [0].AdditionalConfiguration[?Name=='EC2_AGENT_MANAGEMENT'] | [0].Status")"
  echo "  detector $det: status=$status runtime=$rm_status ec2-agent=$agent_status"
  if [ "$status" = "ENABLED" ] && [ "$rm_status" = "ENABLED" ] && [ "$agent_status" = "ENABLED" ]; then
    note "GuardDuty + runtime monitoring" "already on"
  else
    change guardduty update-detector --detector-id "$det" --enable --features "$FEATURES"
    note "GuardDuty + runtime monitoring" "TURNED ON"
  fi
fi

# --- 3. Amazon Inspector (EC2 + ECR) -----------------------------------------
echo "[3/4] Amazon Inspector"
ec2_scan="$(q inspector2 batch-get-account-status --query 'accounts[0].resourceState.ec2.status')"
ecr_scan="$(q inspector2 batch-get-account-status --query 'accounts[0].resourceState.ecr.status')"
echo "  EC2 scanning: $ec2_scan   ECR scanning: $ecr_scan"
if [ "$ec2_scan" = "ENABLED" ] && [ "$ecr_scan" = "ENABLED" ]; then
  note "Inspector EC2 + ECR scanning" "already on"
else
  if [ "$DRY_RUN" = "1" ]; then
    change inspector2 enable --resource-types EC2 ECR
  else
    # enable reports a refused account in failedAccounts but still exits 0, so read it.
    echo "  running: aws inspector2 enable ..."
    failed="$(q inspector2 enable --resource-types EC2 ECR \
      --query 'failedAccounts[].[accountId,errorCode,errorMessage]')"
    if [ -n "$failed" ] && [ "$failed" != "None" ]; then
      echo "ERROR: Amazon Inspector refused to turn on: $failed" >&2
      exit 1
    fi
  fi
  note "Inspector EC2 + ECR scanning" "TURNED ON (takes a few minutes)"
fi

# --- 4. Data Lifecycle Manager default role ----------------------------------
echo "[4/4] Data Lifecycle Manager default role"
if aws iam get-role --role-name AWSDataLifecycleManagerDefaultRole >/dev/null 2>&1; then
  echo "  role AWSDataLifecycleManagerDefaultRole exists"
  note "DLM default role" "already exists"
else
  change dlm create-default-role --resource-type snapshot
  note "DLM default role" "CREATED"
fi

echo
insts="$(q ec2 describe-instances --filters Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[].Instances[].[InstanceId, Tags[?Key==`Name`] | [0].Value]')"
if [ -n "$insts" ]; then
  echo "Instances in $REGION that GuardDuty runtime monitoring and Inspector will also cover"
  echo "(tag GuardDutyManaged=false and InspectorEc2Exclusion=true on any you want left out):"
  printf '%s\n' "$insts" | sed 's/^/    /'
fi

echo
echo "== Summary ($REGION) =="
printf '%s' "$SUMMARY"
if [ "$DRY_RUN" = "1" ]; then
  echo "Dry run only: nothing was changed. Run again without --dry-run to apply."
else
  echo "Done. Next: launch the instance, then run aws-instance-setup.sh."
fi
