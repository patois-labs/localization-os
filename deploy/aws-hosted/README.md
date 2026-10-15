# deploy/aws-hosted: launch kit for a dedicated hosted instance

The step-by-step launch document is
[LAUNCH.md](LAUNCH.md).
This folder holds the files it uses.

| File | What it is | Changes anything? |
|---|---|---|
| `aws-account-baseline.sh` | Once per account and region, before launch: EBS encryption by default, GuardDuty with Runtime Monitoring and EC2 agent management, Amazon Inspector (EC2 and ECR), the Data Lifecycle Manager default role. | Yes, after a preview and a typed `yes` |
| `aws-instance-setup.sh` | After launch: IMDSv2 required, Systems Manager role on the instance, `Backup=daily` volume tags, the daily snapshot policy with an encrypted copy to us-west-2 (35 days each). With `--cloudflare-only`: web ports 80/443 open only to Cloudflare (any rule opening them to the whole internet, including "All traffic" and port ranges, is removed; the group is then read again and the script fails if anything is still open). | Yes, after a preview and a typed `yes` |
| `aws-alerting-setup.sh` | After Cloudflare is live: after-hours alerting by email (LAUNCH.md section 7). SNS topic `locos-alerts` in the instance region and in us-east-1, each with an email subscription; EventBridge rule for GuardDuty findings of severity 7 or more; EC2 status check alarms (system: recover + email, instance: reboot + email); Route 53 HTTPS health check on `https://<hostname>/ready` with its alarm in us-east-1; an inline policy letting the instance role `sns:Publish` to that one topic only. Prints the rough monthly cost (about $3, estimate). | Yes, after a preview and a typed `yes` |
| `host/install-audit-alert.sh` | Run ON the instance as `ec2-user`: a systemd timer that runs the app's `audit verify` in the container once a day and emails (SNS, instance role) when the chain is broken or the check could not run (it waits for the container after a boot and retries once), plus a short "still watching" email every Monday. `--dry-run` / `--test` on the installed check are safe. | Yes, on the box, after a preview and a typed `yes` |
| `host/install-app-backup.sh` | Run ON the instance as `ec2-user` (LAUNCH.md section 6): a systemd timer that each night (06:00 UTC, before the snapshot) runs `db backup --full` in the container, `db verify-backup`s the new archive, removes it if it fails, and only after a verified archive expires archives older than 35 days. Failures are recorded (`--status`) and emailed to the SNS topic when `--topic-arn` is given. `--dry-run` / `--status` on the installed job are safe. | Yes, on the box, after a preview and a typed `yes` |
| `aws-redeploy.sh` | On the box, after a release lands: build (optional, `--build-dir` with the two required build args), then SCAN the exact image ID (`scripts/scan_image.sh`, Trivy digest-pinned) BEFORE switching containers, and refuse to switch on a failing scan unless the operator passes `--accept-scan-risk "<reason>"` (the reason is required and persistently logged; it must be written into the change log). Backs up stopped data before swapping; restores data and the old container on startup/readiness failure. The old container is kept as `<container>-old` for rollback. `LAUNCH.md`, sections 3 and 5 | Yes, on the box, once the scan gate passes |
| `aws-verify.sh` | Evidence check: one PASS / FAIL / WARN line per control, dated, including the alerting set-up (`--hostname` to pin the health check). `--out FILE` saves it. An AWS call that fails is always a FAIL, never a PASS. Snapshot check: a completed, per-volume snapshot under 26 h and a completed us-west-2 copy under 50 h whose DLM source tags OR Description (source snapshot id + region) match; the copy marks are unconfirmed, so confirm on the first real install with a read-only `describe-snapshots`. | No, read-only |
| `caddy/Caddyfile.cloudflare` | Caddy config for running behind Cloudflare (trusted Cloudflare proxies, Origin CA certificate, same security headers as `deploy/Caddyfile`). | n/a |
| `test/run-dry.sh` | Offline test of the scripts and the on-box check against a fake `aws`, `curl` and `docker` (in `test/bin/`). Never contacts AWS. | No |

## Run order

1. `aws-account-baseline.sh` (LAUNCH.md section 0)
2. Launch the instance in the console (LAUNCH.md section 1)
3. `aws-instance-setup.sh --instance-id i-...` (right after launch)
4. Cloudflare set-up, then `aws-instance-setup.sh --instance-id i-... --cloudflare-only` (LAUNCH.md section 5)
5. `aws-alerting-setup.sh --instance-id i-... --hostname <hostname>`, confirm BOTH subscriptions
   using CloudShell `aws sns confirm-subscription --authenticate-on-unsubscribe true` with
   the region, topic ARN and email confirmation value (LAUNCH.md section 7 step 2; do not click the link).
   Then on the box `bash install-audit-alert.sh --topic-arn <printed ARN>`
   and the safe tests (LAUNCH.md section 7)
6. The next day (after the first 07:00 UTC snapshot): `aws-verify.sh --instance-id i-... --hostname <hostname> --out verify-YYYY-MM-DD.txt` (LAUNCH.md section 9)
   - Check 6 copy lineage is an assumption: confirm on the first real install with a read-only `aws ec2 describe-snapshots --region us-west-2 --owner-ids self --query "Snapshots[0].[Description,Tags]"` that the copy carries the `aws:dlm:source-*` tags or a Description naming the source snapshot id and region. If neither, the script FAILs by design; adjust it.

After launch, redeploys (releases and the monthly in-season image rebuild) go through
`aws-redeploy.sh`, which gates the container switch on the image scan
(`LAUNCH.md`, sections 3 and 5).

Every AWS-side script (`aws-redeploy.sh` is on-box and has no AWS calls) accepts
`--region` (default `us-east-2`). The two that change AWS state
accept `--dry-run`, which reads the current state and prints each change instead of
making it. Without `--dry-run` they show that same preview first and wait for you to
type `yes`.

Required on both university and demo hosts: IMDSv2 with response hop limit 1 after the
instance-role dependency check (LAUNCH.md section 1), and authenticated SNS unsubscribe in both
regions (LAUNCH.md section 7 step 2). The private per-zone origin header is required on the university
host; demo host: only if it is Cloudflare-proxied (see LAUNCH.md section 5). Otherwise STOP, do not
install origin-auth. Follow that step's backup, network/IP preservation and rollback procedure.
Cloudflare source ranges alone do not identify
our zone. The Caddy template requires `/etc/caddy/origin-auth.caddy`; create and mount it as
documented before starting the proxy. Operator commands and negative checks are in `LAUNCH.md`.

## Running a script in AWS CloudShell (no install needed)

1. Sign in to the AWS console as your named IAM user (not root). Check the region at
   the top right says **US East (Ohio) us-east-2**.
2. Click the **CloudShell** icon in the top bar (a small `>_` box), or search
   "CloudShell". Wait for the prompt.
3. **Actions** (top right of the CloudShell panel) > **Upload file** > pick the script
   from `deploy\aws-hosted\` on your PC > **Upload**. Repeat for each script you need.
   For `aws-instance-setup.sh` or `aws-verify.sh`, also upload `network_policy.py`
   from the same directory into the same CloudShell folder.
4. Type the command, for example `bash aws-account-baseline.sh --dry-run`, and press Enter.
5. To keep an evidence file: `bash aws-verify.sh --instance-id i-... --out verify.txt`,
   then **Actions** > **Download file** > type `verify.txt` > **Download**.

CloudShell already has the AWS CLI signed in as you. Uploaded files stay in your
CloudShell home folder between sessions (per region).

## Running locally instead

Install AWS CLI v2 (Windows installer from the AWS docs), run `aws configure sso` or
`aws configure`, then in Git Bash: `bash deploy/aws-hosted/aws-account-baseline.sh --dry-run`.
The scripts need only bash, `aws`, `curl`, `grep`, `sed` and GNU `date`; no `jq`.

## Testing the scripts without AWS

`bash deploy/aws-hosted/test/run-dry.sh` runs every script against canned answers
(scenarios: nothing configured, half configured, all configured) and checks that dry
runs make no change calls, that Cloudflare rules are added before the open rules are
removed, that the alerting set-up is idempotent and scopes the instance role to exactly
one topic ARN, that the on-box check emails only on a broken chain or a failed run, and
that the verify script fails and passes when it should. It takes a few minutes on Windows.
At the end it runs every `--query` the scripts sent through the real `jmespath` library
(`test/jmespath_check.py`, against empty and populated AWS responses), because the fake `aws`
cannot catch a query that is wrong in JMESPath itself. That step needs a python with
`jmespath` (`JMESPATH_PYTHON=/path/to/python` picks one); without it the step prints SKIP.
