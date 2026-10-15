#!/usr/bin/env bash
# Offline control-flow test for the AWS scripts and the on-box audit alert. Never contacts AWS
# or Cloudflare: test/bin/aws, test/bin/curl and test/bin/docker are put first on PATH and
# return canned answers.
#   bash deploy/aws-hosted/test/run-dry.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
KIT="$(cd "$HERE/.." && pwd)"
export PATH="$HERE/bin:$PATH"
chmod +x "$HERE/bin/aws" "$HERE/bin/curl" "$HERE/bin/docker" 2>/dev/null || true
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
cd "$WORK" || exit 1
export SHIM_LOG="$WORK/calls.log" SHIM_STATE="$WORK/sg-state" SHIM_QUERIES="$WORK/queries.log"
I="i-0123456789abcdef0"
PASSES=0; FAILS=0
ok() { echo "ok    $*"; PASSES=$((PASSES + 1)); }
bad() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
expect_rc() { if [ "$1" = "$2" ]; then ok "$3 (exit $1)"; else bad "$3 (exit $1, wanted $2)"; tail -20 "$WORK/out"; fi; }
has() { if grep -qF -- "$1" "$WORK/out"; then ok "$2"; else bad "$2 (missing: $1)"; fi; }
hasnt() { if grep -qF -- "$1" "$WORK/out"; then bad "$2 (found: $1)"; else ok "$2"; fi; }
logged() { if grep -qF -- "$1" "$SHIM_LOG"; then ok "$2"; else bad "$2 (not called: $1)"; fi; }
run() { : > "$SHIM_LOG"; : > "$SHIM_STATE"; "$@" > "$WORK/out" 2>&1; echo $?; }
setup() { bash "$KIT/aws-instance-setup.sh" --instance-id "$I" "$@"; }
verify() { bash "$KIT/aws-verify.sh" --instance-id "$I" "$@"; }
W80='{"IpProtocol":"tcp","FromPort":80,"ToPort":80,"IpRanges":[{"CidrIp":"0.0.0.0/0"}],"Ipv6Ranges":[{"CidrIpv6":"::/0"}]}'
W443='{"IpProtocol":"tcp","FromPort":443,"ToPort":443,"IpRanges":[{"CidrIp":"0.0.0.0/0"}],"Ipv6Ranges":[]}'

echo "== syntax =="
for f in "$KIT"/aws-account-baseline.sh "$KIT"/aws-instance-setup.sh "$KIT"/aws-verify.sh "$KIT"/aws-alerting-setup.sh "$KIT"/host/install-audit-alert.sh "$HERE"/bin/aws "$HERE"/bin/curl "$HERE"/bin/docker; do
  if bash -n "$f"; then ok "bash -n $(basename "$f")"; else bad "bash -n $(basename "$f")"; fi
done
[ "$(command -v aws)" = "$HERE/bin/aws" ] && ok "aws resolves to the shim" || bad "aws is NOT the shim"

echo "== baseline =="
export SHIM_ALLOW_WRITE=0 LOCOS_IAM_WAIT=0 SHIM_NO_DLM_ROLE=1 SHIM_SCENARIO=fresh
rc=$(run bash "$KIT/aws-account-baseline.sh" --dry-run); expect_rc "$rc" 0 "baseline dry-run, fresh account"
has "would run: aws ec2 enable-ebs-encryption-by-default" "plans EBS default encryption"
has "would run: aws guardduty create-detector --enable --features" "plans GuardDuty detector"
has "EC2_AGENT_MANAGEMENT" "GuardDuty features include EC2 agent management"
has "would run: aws inspector2 enable --resource-types EC2 ECR" "plans Inspector"
has "would run: aws dlm create-default-role --resource-type snapshot" "plans DLM role"
has "i-0fedcba9876543210" "lists the other instances GuardDuty/Inspector will cover"
SHIM_SCENARIO=half
rc=$(run bash "$KIT/aws-account-baseline.sh" --dry-run); expect_rc "$rc" 0 "baseline dry-run, detector without runtime"
has "would run: aws guardduty update-detector --detector-id" "plans GuardDuty update"
SHIM_SCENARIO=done; SHIM_NO_DLM_ROLE=0
rc=$(run bash "$KIT/aws-account-baseline.sh" --dry-run); expect_rc "$rc" 0 "baseline dry-run, all on"
hasnt "would run" "nothing planned when everything is on"
SHIM_NO_DLM_ROLE=1
rc=$(run bash "$KIT/aws-account-baseline.sh" --dry-run); expect_rc "$rc" 0 "baseline dry-run, DLM role missing only"
has "would run: aws dlm create-default-role" "plans only the DLM role"
SHIM_SCENARIO=fresh
rc=$(run bash -c "echo no | bash '$KIT/aws-account-baseline.sh'"); expect_rc "$rc" 1 "baseline stops when the answer is not yes"
hasnt "running: aws" "no change made after a no"
SHIM_ALLOW_WRITE=1
rc=$(run bash -c "echo yes | bash '$KIT/aws-account-baseline.sh'"); expect_rc "$rc" 0 "baseline apply after yes (shim)"
logged "guardduty create-detector" "apply reached create-detector"
rc=$(run env SHIM_INSPECTOR_FAIL=1 bash "$KIT/aws-account-baseline.sh" --yes)
expect_rc "$rc" 1 "baseline fails when Inspector reports failedAccounts"
has "Amazon Inspector refused to turn on" "names the Inspector failure"
SHIM_ALLOW_WRITE=0; SHIM_NO_DLM_ROLE=0

echo "== instance setup: dry runs =="
rc=$(run bash "$KIT/aws-instance-setup.sh" --dry-run); expect_rc "$rc" 2 "refuses without an instance id"
rc=$(run bash "$KIT/aws-instance-setup.sh" --instance-id bogus --dry-run); expect_rc "$rc" 2 "refuses a malformed id"
SHIM_SCENARIO=fresh
rc=$(run setup --cloudflare-only --dry-run); expect_rc "$rc" 0 "setup dry-run, fresh instance, cloudflare-only"
has "modify-instance-metadata-options --instance-id $I" "plans metadata repair"
has "--http-tokens required --http-endpoint enabled --http-put-response-hop-limit 1" \
  "plans IMDSv2 and hop limit 1"
has "iam create-role --role-name LocOS-Instance-SSM" "plans SSM role"
has "iam create-instance-profile --instance-profile-name LocOS-Instance-SSM" "plans instance profile"
has "iam add-role-to-instance-profile --instance-profile-name LocOS-Instance-SSM" "plans role into profile"
has "associate-iam-instance-profile --instance-id $I --iam-instance-profile Name=LocOS-Instance-SSM" "plans profile association"
has "create-tags --resources vol-0aaa1111bbbb2222c --tags Key=Backup,Value=daily" "plans volume tag"
has "dlm create-lifecycle-policy" "plans DLM policy"
has '"Target": "us-west-2"' "policy copies to us-west-2"
has "authorize-security-group-ingress --group-id sg-0123abcd" "plans Cloudflare rules"
has "revoke-security-group-ingress --group-id sg-0123abcd --ip-permissions '[$W80]'" "plans exact world 80 revoke (v4 and ::/0)"
has "revoke-security-group-ingress --group-id sg-0123abcd --ip-permissions '[$W443]'" "plans exact world 443 revoke"
ls "$WORK"/.locos-dlm-policy-*.json >/dev/null 2>&1 && bad "temp policy file left behind" || ok "temp policy file cleaned up"
rc=$(run env SHIM_PROFILE_EMPTY=1 bash "$KIT/aws-instance-setup.sh" --instance-id "$I" --dry-run)
expect_rc "$rc" 0 "setup dry-run, profile left empty by an earlier failed run"
hasnt "create-instance-profile" "does not recreate the existing profile"
has "add-role-to-instance-profile --instance-profile-name LocOS-Instance-SSM --role-name LocOS-Instance-SSM" "finishes the half-done profile"
SHIM_SCENARIO=half
rc=$(run setup --dry-run); expect_rc "$rc" 0 "setup dry-run, instance with another profile"
has "attach-role-policy --role-name ExistingAppRole" "adds SSM policy to the EXISTING role"
hasnt "associate-iam-instance-profile" "does not replace the existing profile"
has "dlm update-lifecycle-policy --policy-id policy-0123456789abcdef0" "updates the existing DLM policy"
has "port 22 (SSH) open to the internet" "reports world-open SSH"
hasnt "authorize-security-group-ingress" "no SG change without --cloudflare-only"
SHIM_SCENARIO=done
rc=$(run setup --cloudflare-only --dry-run); expect_rc "$rc" 0 "setup dry-run, all done"
hasnt "authorize-security-group-ingress" "no duplicate Cloudflare rules"
hasnt "revoke-security-group-ingress" "nothing to revoke"
rc=$(run env SHIM_HOP_LIMIT=2 bash "$KIT/aws-instance-setup.sh" --instance-id "$I" --dry-run)
expect_rc "$rc" 0 "setup dry-run, only hop limit wrong"
has "modify-instance-metadata-options --instance-id $I" "plans hop-only metadata repair"
has "--http-tokens required --http-endpoint enabled --http-put-response-hop-limit 1" \
  "repairs hop limit alone"
SHIM_SCENARIO=stale
rc=$(run setup --cloudflare-only --dry-run); expect_rc "$rc" 0 "setup dry-run, stale Cloudflare range"
has "199.27.128.0/21" "removes the range Cloudflare no longer publishes"
has "other volumes are also tagged Backup=daily" "warns about another Backup=daily volume"

echo "== instance setup: apply (shim) =="
SHIM_ALLOW_WRITE=1; SHIM_SCENARIO=fresh
rc=$(run setup --cloudflare-only --yes); expect_rc "$rc" 0 "apply, fresh, cloudflare-only"
a="$(grep -n '^ec2 authorize-security-group-ingress' "$SHIM_LOG" | head -1 | cut -d: -f1)"
v="$(grep -n '^ec2 revoke-security-group-ingress' "$SHIM_LOG" | head -1 | cut -d: -f1)"
[ -n "$a" ] && [ -n "$v" ] && [ "$a" -lt "$v" ] && ok "Cloudflare rules added BEFORE world rules revoked" \
  || bad "authorize/revoke order wrong (authorize line $a, revoke line $v)"
logged '"CidrIpv6":"::/0"' "::/0 on port 80 actually revoked"
has "re-checked: no rule lets the whole internet reach 80 or 443" "re-reads the group after revoking"
SHIM_SCENARIO=alltraffic
rc=$(run setup --cloudflare-only --yes); expect_rc "$rc" 0 "apply, All traffic rule"
logged '"IpProtocol":"-1","IpRanges":[{"CidrIp":"0.0.0.0/0"}]' "revokes the All traffic rule in its own shape"
[ "$(grep -c '^ec2 revoke-security-group-ingress' "$SHIM_LOG")" = 1 ] && ok "All traffic rule revoked once, not per port" \
  || bad "All traffic rule revoke count: $(grep -c '^ec2 revoke-security-group-ingress' "$SHIM_LOG")"
SHIM_SCENARIO=range
rc=$(run setup --cloudflare-only --yes); expect_rc "$rc" 0 "apply, tcp 80-443 range rule"
logged '"FromPort":80,"ToPort":443' "revokes the 80-443 range in its own shape"
rc=$(run env SHIM_REVOKE_NOOP=1 bash "$KIT/aws-instance-setup.sh" --instance-id "$I" --cloudflare-only --yes)
expect_rc "$rc" 1 "apply FAILS when a revoke silently changes nothing"
has "STILL open to the whole internet" "says the web ports are still open"
hasnt "Cloudflare-only on" "no success summary"
SHIM_SCENARIO=fresh
rc=$(run env SHIM_SG_FAIL_AFTER_REVOKE=1 bash "$KIT/aws-instance-setup.sh" --instance-id "$I" --cloudflare-only --yes)
expect_rc "$rc" 1 "apply FAILS when the re-check cannot read the group"
has "could not read security group sg-0123abcd" "names the failed read"
hasnt "re-checked: no rule" "no false all-clear after a failed read"
hasnt "Cloudflare-only on" "no success summary after a failed read"
SHIM_ALLOW_WRITE=0; SHIM_SCENARIO=half
rc=$(run env SHIM_SG_FAIL=1 bash "$KIT/aws-instance-setup.sh" --instance-id "$I" --dry-run)
expect_rc "$rc" 1 "dry run FAILS when the port 22 check cannot read the group"
has "could not read security group sg-0123abcd (port 22)" "port 22 read failure is loud"
SHIM_ALLOW_WRITE=1; SHIM_SCENARIO=fresh; SHIM_NO_DLM_ROLE=1
rc=$(run setup --yes); expect_rc "$rc" 1 "apply stops when the baseline was not run"
has "run aws-account-baseline.sh first" "explains why"
SHIM_NO_DLM_ROLE=0
rc=$(run bash -c "echo no | bash '$KIT/aws-instance-setup.sh' --instance-id $I"); expect_rc "$rc" 1 "stops when the answer is not yes"
hasnt "running: aws" "no change made after a no"
SHIM_ALLOW_WRITE=0

echo "== verify =="
SHIM_SCENARIO=done
rc=$(run verify --out "$WORK/evidence.txt"); expect_rc "$rc" 0 "verify, all good"
hasnt "FAIL  " "no FAIL lines"
has "Result: 0 FAIL, 0 WARN" "summary line, no warnings"
has "PASS  port 443: every source is a current Cloudflare range" "Cloudflare-only PASS only when every source is Cloudflare"
has "PASS  newest completed snapshot copy of vol-0aaa1111bbbb2222c in us-west-2 is 20 h old" "copy age checked"
[ -s "$WORK/evidence.txt" ] && grep -q "Date (UTC)" "$WORK/evidence.txt" && ok "--out file written with dated header" || bad "--out file missing"
rc=$(run env SHIM_COPY_AGE_H=80 bash "$KIT/aws-verify.sh" --instance-id "$I"); expect_rc "$rc" 1 "verify fails on an old copy"
has "FAIL  newest completed snapshot copy of vol-0aaa1111bbbb2222c in us-west-2 is 80 h old" "names the stale copy"
SHIM_SCENARIO=stale
rc=$(run verify); expect_rc "$rc" 0 "verify, stale Cloudflare range present"
has "WARN  port 443: also open to non-Cloudflare sources: 199.27.128.0/21" "flags a non-Cloudflare source"
has "WARN  other volumes tagged Backup=daily" "flags another Backup=daily volume"
SHIM_SCENARIO=fresh
rc=$(run verify); expect_rc "$rc" 1 "verify, fresh account fails"
has "FAIL  EBS encryption by default is OFF" "flags EBS default"
has "FAIL  volume vol-0aaa1111bbbb2222c is NOT encrypted" "flags unencrypted volume"
has "WARN  port 443 open to the whole internet" "warns world-open 443"
SHIM_SCENARIO=half
rc=$(run verify); expect_rc "$rc" 1 "verify, half"
has "FAIL  port 22 (SSH) is open to the internet" "flags world-open SSH"
SHIM_SCENARIO=alltraffic
rc=$(run verify); expect_rc "$rc" 1 "verify, All traffic rule"
has "FAIL  port 22 (SSH) is open to the internet" "All traffic counts as SSH open"
has "WARN  port 80 open to the whole internet" "All traffic counts as web open"
SHIM_SCENARIO=apierror
rc=$(run verify); expect_rc "$rc" 1 "verify, AWS calls failing"
hasnt "PASS  port" "no PASS for a port when the group could not be read"
hasnt "PASS  IMDSv2" "no PASS for IMDSv2 when the instance could not be read"
has "FAIL  could not read the instance's security groups" "a failed call is a FAIL"
grep -Eq '^[a-z0-9]+ (enable|create|update|modify|attach|associate|authorize|revoke|put|subscribe|set|change|publish|delete|archive)' "$SHIM_LOG" && bad "verify made a change" || ok "verify made no change calls"

# ---------------------------------------------------------------------------------------
H="tms.univ.test"
T2="arn:aws:sns:us-east-2:123456789012:locos-alerts"
T1="arn:aws:sns:us-east-1:123456789012:locos-alerts"
HCNEW="11111111-2222-3333-4444-555555555555"
export SHIM_FILES="$WORK/files.log"
MUT='^[a-z0-9]+ (enable|create|update|modify|attach|associate|authorize|revoke|put|subscribe|set|change|publish|delete|archive)'
nochange() { if grep -Eq "$MUT" "$SHIM_LOG"; then bad "$1 ($(grep -E "$MUT" "$SHIM_LOG" | head -1 | cut -c1-60))"; else ok "$1"; fi; }
alert() { bash "$KIT/aws-alerting-setup.sh" --instance-id "$I" --hostname "$H" --email ops@example.com "$@"; }
# rk: like run, but KEEPS the fake's state (to prove a second run changes nothing)
rk() { : > "$SHIM_LOG"; "$@" > "$WORK/out" 2>&1; echo $?; }

echo "== alerting setup: refusals =="
SHIM_ALLOW_WRITE=0; SHIM_SCENARIO=done; SHIM_ALERT=none; export SHIM_ALERT
rc=$(run bash "$KIT/aws-alerting-setup.sh" --hostname "$H" --dry-run); expect_rc "$rc" 2 "refuses without an instance id"
rc=$(run bash "$KIT/aws-alerting-setup.sh" --instance-id "$I" --dry-run); expect_rc "$rc" 2 "refuses without a hostname"
has "--hostname is required" "says the hostname is missing"
rc=$(run bash "$KIT/aws-alerting-setup.sh" --instance-id "$I" --hostname https://tms.univ.test/ --dry-run); expect_rc "$rc" 2 "refuses a URL as the hostname"
rc=$(run bash "$KIT/aws-alerting-setup.sh" --instance-id "$I" --hostname tms.example.edu --dry-run); expect_rc "$rc" 2 "refuses the placeholder hostname"
rc=$(run bash "$KIT/aws-alerting-setup.sh" --instance-id "$I" --hostname "$H" --dry-run); expect_rc "$rc" 2 "refuses without an email"
has "--email is required" "says the email is missing"
rc=$(run alert --email not-an-email --dry-run); expect_rc "$rc" 2 "refuses a bad email"
[ -s "$SHIM_LOG" ] && bad "a refused run called aws" || ok "refused runs make no AWS call"

echo "== alerting setup: dry run, nothing set up yet =="
rc=$(run alert --dry-run); expect_rc "$rc" 0 "alerting dry-run, nothing set up"
nochange "dry run makes no change calls"
has "would run: aws sns create-topic --name locos-alerts --region us-east-2" "plans the us-east-2 topic"
has "would run: aws sns create-topic --name locos-alerts --region us-east-1" "plans the us-east-1 topic"
has "sns subscribe --topic-arn $T2 --protocol email --notification-endpoint ops@example.com --region us-east-2" "plans the email subscription (us-east-2)"
has "sns subscribe --topic-arn $T1 --protocol email --notification-endpoint ops@example.com --region us-east-1" "plans the email subscription (us-east-1)"
has "sns set-topic-attributes --topic-arn $T2 --attribute-name Policy --attribute-value" "plans the topic policy"
has '"Principal":{"Service":"events.amazonaws.com"},"Action":"sns:Publish","Resource":"'"$T2"'","Condition":{"ArnEquals":{"aws:SourceArn":"arn:aws:events:us-east-2:123456789012:rule/locos-guardduty-high"}}' "EventBridge may publish for that one rule only"
has "events put-rule --name locos-guardduty-high --event-pattern" "plans the GuardDuty rule"
has '"source":["aws.guardduty"],"detail-type":["GuardDuty Finding"],"detail":{"severity":[{"numeric":[">=",7]}]}' "rule matches severity 7 or more"
has "events put-targets --rule locos-guardduty-high --targets" "plans the rule target"
has '"InputTemplate":' "target has a readable message"
has "--alarm-name locos-ec2-system-check-$I" "plans the system check alarm"
has "--metric-name StatusCheckFailed_System --dimensions Name=InstanceId,Value=$I" "system alarm watches the instance"
has "--alarm-actions arn:aws:automate:us-east-2:ec2:recover $T2 --ok-actions $T2" "system alarm recovers + emails (and on OK)"
has "--metric-name StatusCheckFailed_Instance" "plans the instance check alarm"
has "--alarm-actions arn:aws:automate:us-east-2:ec2:reboot $T2" "instance alarm reboots + emails"
has "route53 create-health-check --caller-reference locos-ready-$H-" "plans the health check"
has '{"Type":"HTTPS","FullyQualifiedDomainName":"tms.univ.test","Port":443,"ResourcePath":"/ready","RequestInterval":30,"FailureThreshold":3,"EnableSNI":true' "health check: HTTPS /ready 443 SNI 30 s x3"
has "--namespace AWS/Route53 --metric-name HealthCheckStatus" "plans the health check alarm"
has "--comparison-operator LessThanThreshold --treat-missing-data breaching --alarm-actions $T1 --ok-actions $T1 --region us-east-1" "health alarm lives in us-east-1 and emails"
has "iam put-role-policy --role-name ExistingAppRole --policy-name LocOS-Alerts-Publish" "plans the inline policy on the instance role"
has "estimates" "costs are labelled as estimates"
has "Total about \$3 a month" "states the rough monthly cost"
has "Dry run only: nothing was changed." "says nothing changed"
ls "$WORK"/.locos-alert-* >/dev/null 2>&1 && bad "temp JSON files left behind" || ok "temp JSON files cleaned up"

echo "== alerting setup: preview then no =="
rc=$(run bash -c "echo no | bash '$KIT/aws-alerting-setup.sh' --instance-id $I --hostname $H --email ops@example.com"); expect_rc "$rc" 1 "stops when the answer is not yes"
hasnt "running: aws" "no change made after a no"
nochange "no change call after a no"

echo "== alerting setup: apply, then run again =="
SHIM_ALLOW_WRITE=1; : > "$SHIM_FILES"
rc=$(run alert --yes); expect_rc "$rc" 0 "alerting apply (shim)"
logged "sns create-topic" "created the topics"
logged "events put-targets" "added the rule target"
logged "route53 change-tags-for-resource --resource-type healthcheck --resource-id $HCNEW" "tagged the new health check"
logged "Name=HealthCheckId,Value=$HCNEW" "health alarm watches the NEW health check id"
has "PendingConfirmation" "reports PendingConfirmation"
has "OPERATOR: use the confirmation link's Token" "requires authenticated confirmation"
has "bash install-audit-alert.sh --topic-arn $T2" "prints the on-box install command"
pol="$(awk '/^== iam put-role-policy/ {f = 1; next} /^== / {f = 0} f' "$SHIM_FILES")"
[ "$(printf '%s' "$pol" | grep -o '"Resource"' | wc -l | tr -d ' ')" = 1 ] && ok "inline policy has exactly one Resource" || bad "inline policy Resource count: $pol"
printf '%s' "$pol" | grep -qF "\"Action\":\"sns:Publish\",\"Resource\":\"$T2\"}" && ok "inline policy = sns:Publish on exactly $T2" || bad "inline policy scope wrong: $pol"
printf '%s' "$pol" | grep -q '\*' && bad "inline policy contains a wildcard" || ok "inline policy has no wildcard"
SHIM_ALLOW_WRITE=0
rc=$(rk alert --dry-run); expect_rc "$rc" 0 "second run against the applied state"
hasnt "would run" "second run plans no change (idempotent)"
nochange "second run makes no change calls"
has "PendingConfirmation - authenticated" "pending until authenticated confirmation"

echo "== alerting setup: other states =="
SHIM_ALERT=done
rc=$(run alert --dry-run); expect_rc "$rc" 0 "dry-run, everything already set"
hasnt "would run" "nothing planned when everything is set"
has "confirmed, unsubscribe protected (ops@example.com)" "reports protected subscription"
rc=$(run env SHIM_ALARMS_DISABLED=1 bash "$KIT/aws-alerting-setup.sh" --instance-id "$I" --hostname "$H" --email ops@example.com --dry-run)
expect_rc "$rc" 0 "dry-run, alarms with their actions disabled (end of season)"
has "has its actions switched off" "notices disabled alarm actions"
has "put-metric-alarm --alarm-name locos-ready-healthcheck --actions-enabled" "puts the alarm back with actions enabled"
SHIM_ALERT=wide
rc=$(run alert --dry-run); expect_rc "$rc" 0 "dry-run, inline policy too wide"
has "existing policy differs" "notices the too-wide policy"
has "iam put-role-policy --role-name ExistingAppRole --policy-name LocOS-Alerts-Publish" "replaces it"
SHIM_ALERT=apierror
rc=$(run alert --dry-run); expect_rc "$rc" 1 "stops when an alerting read fails"
has "ERROR: could not read: aws sns get-topic-attributes" "names the failed read"
has "STOPPED" "says it stopped"
hasnt "== Summary" "no summary after a failed read"
SHIM_SCENARIO=fresh; SHIM_ALERT=none
rc=$(run alert --dry-run); expect_rc "$rc" 1 "stops when the instance has no role"
has "Run aws-instance-setup.sh" "says to run the instance setup first"
SHIM_SCENARIO=done; SHIM_ALLOW_WRITE=1
rc=$(run env SHIM_TARGETS_FAIL=1 bash "$KIT/aws-alerting-setup.sh" --instance-id "$I" --hostname "$H" --email ops@example.com --yes)
expect_rc "$rc" 1 "apply FAILS when EventBridge refuses the target"
has "EventBridge refused the email target" "names the refused target"
SHIM_ALLOW_WRITE=0

echo "== on-box audit alert =="
ROOTD="$WORK/root"; TARN="$T2"
export LOCOS_ROOT="$ROOTD" LOCOS_SUDO="" LOCOS_SYSTEMCTL=true
INST="$KIT/host/install-audit-alert.sh"
CHK="$ROOTD/usr/local/bin/locos-audit-alert"
rc=$(run bash "$INST" --dry-run); expect_rc "$rc" 2 "installer refuses without a topic ARN"
rc=$(run bash "$INST" --topic-arn bogus --dry-run); expect_rc "$rc" 2 "installer refuses a malformed ARN"
rc=$(run bash "$INST" --topic-arn "$TARN" --time 23:57 --dry-run); expect_rc "$rc" 2 "installer refuses a time the random delay could push past midnight"
# a multiline --container used to pass the line-anchored grep validator and the
# second line broke out of the single quotes in the sh -c prerequisite check and EXECUTED.
rc=$(run bash "$INST" --topic-arn "$TARN" --container "$(printf 'locosapp\nx')" --dry-run); expect_rc "$rc" 2 "installer refuses a multiline container name"
rc=$(run bash "$INST" --topic-arn "$(printf '%s\nx' "$TARN")" --dry-run); expect_rc "$rc" 2 "installer refuses a multiline topic ARN"
rc=$(run bash "$INST" --topic-arn "$TARN" --time "$(printf '13:00\nx')" --dry-run); expect_rc "$rc" 2 "installer refuses a multiline time"
MARK="$WORK/injection-marker"
rc=$(run bash "$INST" --topic-arn "$TARN" --container "$(printf "locosapp\n'; : > '%s'; echo '" "$MARK")" --dry-run)
expect_rc "$rc" 2 "installer refuses a container name that tries to break out of its quotes"
[ -e "$MARK" ] && bad "the injected second line EXECUTED during --dry-run" || ok "a multiline container name executes nothing, even with --dry-run"
rc=$(run bash "$INST" --topic-arn "$TARN" --dry-run); expect_rc "$rc" 0 "installer dry-run"
has "would write /usr/local/bin/locos-audit-alert" "shows the check it would install"
has "OnCalendar=*-*-* 13:00:00 UTC" "daily timer at 13:00 UTC"
has "python -m app.cli audit verify" "runs the app's audit verify command"
[ -e "$CHK" ] && bad "dry run wrote files" || ok "dry run wrote nothing"
rc=$(run bash -c "echo no | bash '$INST' --topic-arn $TARN"); expect_rc "$rc" 1 "installer stops when the answer is not yes"
[ -e "$CHK" ] && bad "files written after a no" || ok "nothing written after a no"
rc=$(run bash "$INST" --topic-arn "$TARN" --yes); expect_rc "$rc" 0 "installer apply"
[ -x "$CHK" ] && ok "check installed" || bad "check not installed"
grep -qx "TOPIC_ARN=$TARN" "$ROOTD/etc/locos-audit-alert.conf" && ok "config holds the topic ARN" || bad "config missing the topic ARN"
[ -f "$ROOTD/etc/systemd/system/locos-audit-alert.timer" ] && ok "timer unit written" || bad "timer unit missing"
rc=$(run bash "$INST" --topic-arn "$TARN" --yes); expect_rc "$rc" 0 "installer re-run"
has "unchanged: /usr/local/bin/locos-audit-alert" "re-run leaves identical files alone"
export LOCOS_AUDIT_CONF="$ROOTD/etc/locos-audit-alert.conf"
grep -qx "HEARTBEAT_DAY=1" "$LOCOS_AUDIT_CONF" && ok "weekly email day defaults to Monday" || bad "HEARTBEAT_DAY not 1"
export LOCOS_WAIT_S=2 LOCOS_POLL_S=1 LOCOS_RETRY_S=0 LOCOS_TODAY_DOW=3
SHIM_ALLOW_WRITE=1
rc=$(run env SHIM_AUDIT=ok bash "$CHK"); expect_rc "$rc" 0 "check: chain OK"
grep -q '^sns publish' "$SHIM_LOG" && bad "chain OK sent an email" || ok "chain OK sends nothing"
rc=$(run env SHIM_AUDIT=broken bash "$CHK"); expect_rc "$rc" 1 "check: chain BROKEN"
logged "sns publish | sns publish --region us-east-2 --topic-arn $TARN --subject LocOS CRITICAL: audit log chain BROKEN" "BROKEN emails a CRITICAL alert to the topic"
logged "first bad row id: 58" "the email carries the app's output"
rc=$(run env SHIM_AUDIT=down bash "$CHK"); expect_rc "$rc" 1 "check: container down"
logged "--subject LocOS WARNING: app container locosapp not running" "container down is reported as such"
logged "waited 2 s" "waits for the container before reporting it down"
rc=$(run env SHIM_AUDIT=error bash "$CHK"); expect_rc "$rc" 1 "check: fails twice"
has "retrying in 0 s" "retries once before reporting"
[ "$(grep -c '^docker exec .*audit verify' "$SHIM_LOG")" = 2 ] && ok "ran audit verify exactly twice" || bad "audit verify runs: $(grep -c '^docker exec .*audit verify' "$SHIM_LOG")"
logged "--subject LocOS WARNING: audit log check could not run" "a check that cannot run emails after the retry"
rc=$(run env SHIM_AUDIT=flaky bash "$CHK"); expect_rc "$rc" 0 "check: fails once, then OK"
grep -q '^sns publish' "$SHIM_LOG" && bad "a slow start sent an email" || ok "a slow start that recovers sends nothing"
rc=$(run env SHIM_AUDIT=noisy bash "$CHK"); expect_rc "$rc" 0 "check: OK followed by 12 log lines"
grep -q '^sns publish' "$SHIM_LOG" && bad "extra log lines caused an email" || ok "classified on the full output"
rc=$(run env SHIM_AUDIT=ok LOCOS_TODAY_DOW=1 bash "$CHK"); expect_rc "$rc" 0 "check: Monday, chain OK"
logged "--subject LocOS weekly: after-hours alerting is watching" "Monday sends the weekly still-watching email"
rc=$(run env SHIM_AUDIT=broken LOCOS_TODAY_DOW=1 bash "$CHK"); expect_rc "$rc" 1 "check: Monday, chain BROKEN"
logged "--subject LocOS CRITICAL" "a break on Monday is still CRITICAL, not the weekly email"
rc=$(run env SHIM_AUDIT=broken bash "$CHK" --dry-run); expect_rc "$rc" 1 "check --dry-run on a broken chain"
grep -q '^sns publish' "$SHIM_LOG" && bad "--dry-run sent an email" || ok "--dry-run sends nothing"
has "dry run: nothing sent" "--dry-run says nothing was sent"
rc=$(run env SHIM_AUDIT=ok bash "$CHK" --test); expect_rc "$rc" 0 "check --test"
logged "--subject LocOS TEST (ignore): audit alert email works" "--test sends one TEST email"
rc=$(run env SHIM_AUDIT=broken SHIM_PUBLISH_FAIL=1 bash "$CHK"); expect_rc "$rc" 3 "check fails loudly when the email cannot be sent"
has "could not send the email" "names the send failure"
SHIM_ALLOW_WRITE=0
unset LOCOS_ROOT LOCOS_SUDO LOCOS_SYSTEMCTL LOCOS_AUDIT_CONF LOCOS_WAIT_S LOCOS_POLL_S LOCOS_RETRY_S LOCOS_TODAY_DOW

echo "== verify: alerting =="
SHIM_SCENARIO=done; SHIM_ALERT=done
rc=$(run verify --hostname "$H"); expect_rc "$rc" 0 "verify, alerting all set"
has "PASS  alerting: SNS topic locos-alerts exists in us-east-2" "topic us-east-2"
has "PASS  alerting: SNS topic locos-alerts exists in us-east-1" "topic us-east-1"
has "PASS  alerting: email subscription confirmed on locos-alerts (us-east-2)" "confirmed subscription us-east-2"
has "PASS  alerting: email subscription confirmed on locos-alerts (us-east-1)" "confirmed subscription us-east-1"
has "PASS  alerting: topic policy lets EventBridge rule locos-guardduty-high publish" "topic policy"
has "PASS  alerting: EventBridge rule locos-guardduty-high enabled" "rule enabled"
has "PASS  alerting: rule locos-guardduty-high sends to locos-alerts" "rule target"
has "PASS  alerting: alarm locos-ec2-system-check-$I set (system status check: recover + email)" "system alarm"
has "PASS  alerting: alarm locos-ec2-instance-check-$I set (instance status check: reboot + email)" "instance alarm"
has "PASS  alerting: Route 53 health check abcdef12-3456-7890-abcd-ef1234567890 on https://$H/ready" "health check settings"
has "is healthy (8 of 8 checkers report Success)" "health check healthy"
has "PASS  alerting: alarm locos-ready-healthcheck set (site down: email), state OK" "health alarm in us-east-1"
has "PASS  alerting: role ExistingAppRole may sns:Publish to locos-alerts only" "inline policy scoped"
rc=$(run verify --hostname other.univ.test); expect_rc "$rc" 1 "verify, health check for another hostname"
has "FAIL  alerting: no Route 53 health check on /ready for other.univ.test" "hostname mismatch is a FAIL"
rc=$(run env SHIM_ALARMS_DISABLED=1 bash "$KIT/aws-verify.sh" --instance-id "$I"); expect_rc "$rc" 1 "verify, alarm actions disabled"
has "FAIL  alerting: alarm locos-ec2-system-check-$I has its actions switched off" "disabled actions are a FAIL"
rc=$(run env SHIM_ALARM_NO_OK=1 bash "$KIT/aws-verify.sh" --instance-id "$I"); expect_rc "$rc" 1 "verify, alarms without an OK action"
has "FAIL  alerting: alarm locos-ec2-system-check-$I does not email when it clears" "missing OK action named as such"
hasnt "switched off" "no misleading 'switched off' when only the OK action is missing"
rc=$(run env SHIM_ALARM_AVG=1 bash "$KIT/aws-verify.sh" --instance-id "$I"); expect_rc "$rc" 1 "verify, alarm on the wrong statistic"
has "FAIL  alerting: alarm locos-ready-healthcheck watches 'HealthCheckStatus Average LessThanThreshold 1'" "wrong statistic is a FAIL"
rc=$(run env SHIM_ALARM_AVG=1 bash "$KIT/aws-alerting-setup.sh" --instance-id "$I" --hostname "$H" --email ops@example.com --dry-run)
expect_rc "$rc" 0 "setup dry-run, alarm on the wrong statistic"
has "put-metric-alarm --alarm-name locos-ec2-system-check-$I" "setup puts a drifted alarm back"
has "now:  alarm actions [arn:aws:automate:us-east-2:ec2:recover $T2] OK actions [$T2] health check - enabled true watches StatusCheckFailed_System Average GreaterThanOrEqualToThreshold 1" "preview shows the CURRENT alarm values"
has "want: alarm actions [arn:aws:automate:us-east-2:ec2:recover $T2] OK actions [$T2] health check - enabled true watches StatusCheckFailed_System Maximum GreaterThanOrEqualToThreshold 1" "preview shows the standard values next to them"
SHIM_ALERT=pending
rc=$(run verify); expect_rc "$rc" 1 "verify, email not confirmed"
has "FAIL  alerting: PendingConfirmation (us-east-2)" "PendingConfirmation is a FAIL"
SHIM_ALERT=unhealthy
rc=$(run verify); expect_rc "$rc" 1 "verify, site down"
has "FAIL  alerting: health check abcdef12-3456-7890-abcd-ef1234567890 is NOT healthy (0 of 8" "unhealthy check is a FAIL"
has "FAIL  alerting: alarm locos-ready-healthcheck is IN ALARM" "alarm in ALARM is a FAIL"
SHIM_ALERT=wide
rc=$(run verify); expect_rc "$rc" 1 "verify, inline policy too wide"
has "FAIL  alerting: LocOS-Alerts-Publish on role ExistingAppRole is not exactly sns:Publish" "wide policy is a FAIL"
SHIM_ALERT=none
rc=$(run verify); expect_rc "$rc" 1 "verify, alerting not set up"
has "FAIL  alerting: SNS topic $T2 missing or unreadable" "missing topic is a FAIL"
SHIM_ALERT=apierror
rc=$(run verify); expect_rc "$rc" 1 "verify, alerting calls failing"
hasnt "PASS  alerting" "no alerting PASS when the calls fail"
has "FAIL  alerting: SNS topic $T1 missing or unreadable" "failed topic read is a FAIL"
has "FAIL  alerting: could not read the policy of locos-alerts" "failed policy read is a FAIL"
has "FAIL  alerting: EventBridge rule locos-guardduty-high missing or unreadable" "failed rule read is a FAIL"
has "FAIL  alerting: could not read the targets of rule" "failed target read is a FAIL"
has "FAIL  alerting: could not read alarm locos-ec2-system-check-$I" "failed alarm read is a FAIL"
has "FAIL  alerting: could not list the Route 53 health checks" "failed health check read is a FAIL"
has "FAIL  alerting: inline policy LocOS-Alerts-Publish missing or unreadable" "failed policy read is a FAIL"
nochange "verify made no change calls (alerting)"
unset SHIM_ALERT

# The fake aws answers --query with canned text, so it cannot catch a query that is wrong in
# JMESPath itself. Every query recorded above now runs through the REAL jmespath library
# (test/jmespath_check.py) against empty and populated AWS responses, with pinned results for
# the queries the shell compares, a count of query templates and sites, and a field-name check.
# NOT optional: without a python that has jmespath this run FAILS (a green run that skipped the
# only real-library check would be a false green). JMESPATH_PYTHON picks the python; the repo's
# own .venv is tried before python3 and python.
echo "== real jmespath on every query the scripts sent =="
PY=""
for c in "${JMESPATH_PYTHON:-}" "$KIT/../../.venv/Scripts/python.exe" "$KIT/../../.venv/bin/python" python3 python; do
  [ -n "$c" ] || continue
  if "$c" -c 'import jmespath' >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  bad "real jmespath check: no python with the jmespath package found"
  echo "      Install it (pip install jmespath) or set JMESPATH_PYTHON=/path/to/python."
else
  qf="$SHIM_QUERIES"; hf="$HERE/jmespath_check.py"; pf="$WORK/probe.log"; nf="$WORK/nopin.log"
  # Probes: two misspelled fields must FAIL by name, and a lost pin must FAIL.
  { cat "$SHIM_QUERIES"
    printf 'cloudwatch describe-alarms\tMetricAlarms[0].MetricNmae\n'
    printf 'ec2 describe-instances\tReservations[0].Instances[0].MetadataOptions.HttpTokenz\n'
  } > "$pf"
  grep -v 'MetricAlarms\[:1\]' "$SHIM_QUERIES" > "$nf"
  if command -v cygpath >/dev/null 2>&1; then
    qf="$(cygpath -w "$qf")"; hf="$(cygpath -w "$hf")"; pf="$(cygpath -w "$pf")"; nf="$(cygpath -w "$nf")"
  fi
  if "$PY" "$hf" "$qf" > "$WORK/jq.out" 2>&1; then ok "every query evaluates in real jmespath: $(tail -1 "$WORK/jq.out")"
  else bad "real jmespath check"; cat "$WORK/jq.out"; fi
  "$PY" "$hf" "$pf" > "$WORK/jq-probe.out" 2>&1; code=$?
  if [ "$code" -eq 1 ] && grep -q "unknown field 'MetricNmae'" "$WORK/jq-probe.out" \
    && grep -q "unknown field 'HttpTokenz'" "$WORK/jq-probe.out"; then
    ok "a misspelled field fails the check (MetricNmae, HttpTokenz)"
  else bad "misspelled-field probe (exit $code)"; cat "$WORK/jq-probe.out"; fi
  "$PY" "$hf" "$nf" > "$WORK/jq-nopin.out" 2>&1; code=$?
  if [ "$code" -eq 1 ] && grep -q "pinned query never recorded (cloudwatch describe-alarms)" "$WORK/jq-nopin.out"; then
    ok "a pinned query that is no longer sent fails the check"
  else bad "lost-pin probe (exit $code)"; cat "$WORK/jq-nopin.out"; fi
fi

echo
echo "Result: $PASSES ok, $FAILS FAIL (all offline; nothing ran against AWS)"
[ "$FAILS" -eq 0 ]
