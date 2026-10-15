# Launching a hosted, single-tenant instance on AWS

An operator guide for the kit in this folder: one Localization OS instance on an AWS EC2 host
(Amazon Linux 2023), the app in a non-root Docker container behind Caddy, Cloudflare in front as
the web application firewall, no inbound SSH port ever. It covers the parts that are easy to get
wrong. Read `README.md` first for what each script does and for running scripts in AWS CloudShell.

Placeholders used below: `tms.example.com` (your hostname), `i-0123456789abcdef0` (your instance
id), `ACCOUNT` (your 12-digit AWS account id), `ops@example.com` (the address that receives
alerts). Replace them everywhere; never copy a real value into a ticket, a log or the repository.

How to read this: every step that changes something is safe to preview first. Scripts take
`--dry-run`; commands that can interrupt service say so.

## Launch-day checklist

- [ ] EBS encryption by default is on in the instance region and in the backup-copy region.
- [ ] You sign in as a named IAM user with MFA, not as root.
- [ ] The instance has a security group with NO inbound rules at launch, a 30 GB encrypted disk,
      IMDSv2 required and metadata hop limit 1 (section 1).
- [ ] The two app secrets (session secret, encryption key) and the Cloudflare origin key are in
      your password manager; no long-lived AWS access key file is on the server.
- [ ] The first admin exists before the site is public (section 4).
- [ ] Cloudflare: orange cloud, Full (strict), Origin certificate, Bot Fight Mode off, web ports
      open to Cloudflare only, `trusted_proxy_ips` set to Caddy alone (section 5).
- [ ] The private origin header is configured for each zone and hostname; Caddy is validated;
      a missing or wrong header returns 403 and the public `/ready` returns 200 (section 5).
- [ ] The app image AND the pinned Caddy image were scanned before they served (sections 3, 5).
- [ ] Daily snapshots are kept 35 days with an encrypted cross-region copy.
- [ ] The nightly verified application archive is installed and one run is recorded OK (section 6).
- [ ] Both regional SNS emails are confirmed with authenticated unsubscribe (section 7).
- [ ] After-hours alerting is set up and every alert was tested (section 7).
- [ ] A restore drill from the cross-region copy has been done once (section 8).
- [ ] `aws-verify.sh` output is saved and every change is in your change log (section 9).

## 0. Account level, once

1. Turn on MFA for the root user, then do daily work as a named IAM (or IAM Identity Center) user
   that also has MFA.
2. Turn on EBS encryption by default before launching anything (it cannot be added to a disk
   later): EC2 > Dashboard > Account attributes > EBS encryption > Manage > Enable. Do it in the
   backup-copy region as well.
3. Fast path for the baseline (EBS default encryption, GuardDuty with Runtime Monitoring, Amazon
   Inspector, the snapshot role): in CloudShell run `bash aws-account-baseline.sh --dry-run`, read
   the plan, then `bash aws-account-baseline.sh` and type `yes`.

## 1. Launch the instance

1. EC2 > Launch instances. Image: Amazon Linux 2023. Type: `t3.medium` or larger (large glossary
   or TM imports can use most of a GB on their own).
2. Key pair: proceed without one. You reach the host through Session Manager, then a private
   network such as Tailscale. Port 22 is never opened.
3. Security group: create one with no inbound rules at all (remove the SSH rule the wizard adds,
   do not add HTTP or HTTPS). Nothing on the internet can reach the host until section 5 opens
   80/443 to Cloudflare only.
4. Storage: 30 GiB gp3, encrypted.
5. Advanced details: **Metadata version: V2 only (token required)**, Metadata accessible:
   Enabled, **Metadata response hop limit: 1**. Do not keep the AL2023 default of 2.
6. Allocate an Elastic IP and associate it with the instance. Cloudflare needs the address.
7. Fast path: in CloudShell run `bash aws-instance-setup.sh --instance-id i-0123456789abcdef0`
   and type `yes`. It requires IMDSv2, attaches the Systems Manager role, tags the disk
   `Backup=daily` and creates the snapshot policy.

### Instance metadata hop limit

IMDSv2 with hop limit 1 stops a container on the bridge network from reaching the instance role.
Systems Manager, GuardDuty and the audit timer use the metadata service on the host; the app
must not. Do not use host networking for the app.

**Pre-check before applying this to an existing instance.** If the app calls S3, Bedrock or any
other AWS SDK service from inside its container and relies on the instance role, a hop limit of 1
breaks those calls. List the relevant environment variable names without printing values:

```bash
sudo docker exec locosapp sh -c 'env | cut -d= -f1 | grep -i -e "^AWS_" -e S3 -e BEDROCK'
```

Check the configured credential source too: absent environment variables do NOT prove the app is
independent of the instance role. If it relies on the role, STOP: move those calls to explicit
scoped credentials and test them first, or record an accepted hop-limit-2 exception and skip the
setup script and the metadata repair for that host. `aws-verify.sh` still reports the exception as
FAIL. Only proceed with hop limit 1 after confirming the app does not rely on the instance role.

Apply, then read the result until `State` is `applied` (HttpTokens `required`, HttpEndpoint
`enabled`, HttpPutResponseHopLimit `1`), and save the output:

```bash
aws ec2 modify-instance-metadata-options --region us-east-2 --instance-id i-0123456789abcdef0 \
  --http-tokens required --http-endpoint enabled --http-put-response-hop-limit 1
aws ec2 describe-instances --region us-east-2 --instance-ids i-0123456789abcdef0 \
  --query 'Reservations[0].Instances[0].MetadataOptions' --output json
```

Retest every configured AWS-backed app operation after the change and keep the dependency check
and any exception in your change log.

## 2. Host preparation

1. Get a shell without SSH: EC2 > Instances > Connect > Session Manager. You are `ssm-user`; use
   `sudo`. For the rest of the setup work as `ec2-user` (`sudo -iu ec2-user`), never with
   `sudo docker`: `sudo` discards the shell's variables, so a `docker run -e SECRET` would pass an
   EMPTY secret and the app would quietly create its own keyfiles in the data folder.
2. Install a private-network client (for example Tailscale, from its signed package repository)
   and approve the node. Restrict its access rules to what the host needs.
3. Install Docker (`sudo dnf install -y docker && sudo systemctl enable --now docker`, add
   `ec2-user` to the `docker` group). Cap container logs for every container, so a redeploy
   inherits it:

   ```bash
   cat /etc/docker/daemon.json   # if it exists, add the two keys instead of replacing it
   echo '{"log-driver":"json-file","log-opts":{"max-size":"10m","max-file":"5"}}' | sudo tee /etc/docker/daemon.json
   sudo systemctl restart docker
   ```

   On a host that already serves the site this restart stops it for a few seconds.

## 3. The image

1. Build from a `git archive` of the release commit, with the two required build args:
   `docker build --build-arg WITH_SAML=1 --build-arg RELEASE=<release> --build-arg RELEASE_COMMIT=<commit> -t localizationos:launch1 .`
   Add `--build-arg WITH_LDAP=1` only if the customer needs LDAP. SAML is built in by the
   Dockerfile (never add it by hand afterwards); the build fails if the SAML import smoke test
   fails (look for the `SAML smoke:` line).

2. **SAML lxml fallback.** If the build stops at `SAML smoke FAILED` or at `import xmlsec` (the
   lxml and xmlsec wheels disagree on libxml2), copy the printed versions into your change log and
   rebuild with the proven pairing, which swaps only lxml for its 6.0.0 binary wheel:
   `docker build --build-arg WITH_SAML=1 --build-arg SAML_LXML=6.0.0 --build-arg RELEASE=<release> --build-arg RELEASE_COMMIT=<commit> -t localizationos:launch1 .`
   That image differs from `requirements.lock` by that one lxml pin; record it. **Known advisory:
   lxml 6.0.0 is inside PYSEC-2026-87** (an XXE, fixed in lxml 6.1.0), and the fallback build prints
   `WARNING:` lines saying so. Before the fallback image serves anyone, audit it:

   ```bash
   docker run --rm --entrypoint /opt/venv/bin/python localizationos:launch1 -m pip freeze > image-freeze-launch1.txt
   pip-audit -r image-freeze-launch1.txt --no-deps --disable-pip
   ```

   PYSEC-2026-87 will be listed for 6.0.0: that is a warning, not a stop. Paste the audit output
   into your change log. Any OTHER advisory it lists is not covered: do not start the container;
   fix it first or record a named approver's written acceptance.

3. **Scan the image BEFORE it serves (the build is not the gate).** With the image tagged but
   before any `docker run` of it, capture its immutable ID in this shell and scan that ID (redo
   both lines after any rebuild):

   ```bash
   APP_ID="$(docker image inspect --format '{{.Id}}' localizationos:launch1)" && sh scripts/scan_image.sh "$APP_ID"
   ```

   Section 4 starts `"$APP_ID"`, never the tag, so a later build that moves the tag cannot change
   what serves. File the report. A failing scan (any HIGH or CRITICAL with a fix available) or a
   scanner error means STOP; accepting a failing scan is a named approver's decision, recorded the
   same day. Then repeat the monthly rebuild, rescan and redeploy with `aws-redeploy.sh`.

## 4. Data, secrets, first start and first admin

1. Data folder: `sudo mkdir -p /srv/loc-data`, find the app user's id with
   `docker run --rm --entrypoint id localizationos:launch1` (expect `uid=999`), then
   `sudo chown 999:999 /srv/loc-data`. A wrong owner means the app cannot write its database.
2. Generate the two secrets on the host, in an unrecorded local terminal (never a recorded
   session), and store them only in your password manager. Losing the encryption key makes
   encrypted uploads and stored credentials unreadable forever (`docs/SECRETS.md`).
   - Session secret: `openssl rand -hex 32`
   - Encryption key (a Fernet key):
     `docker run --rm --entrypoint python localizationos:launch1 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
   - Load them without echo or history:
     `read -rs -p "Paste the session secret (hidden) > " LOC_TMS_SESSION_SECRET; echo; export LOC_TMS_SESSION_SECRET`
     and the same for `LOC_TMS_ENCRYPTION_KEY`. The prefixed `LOC_TMS_` names win over everything
     and never write a keyfile. Never write the secrets to a file yourself.
3. Create a Docker network with fixed addresses (the proxy trust setting in section 5 depends on
   it): `docker network create --subnet 172.30.0.0/24 locos`
4. Start the app (it refuses to run if `APP_ID` is unset, which is the scan gate):

   ```bash
   docker run -d --name locosapp --restart unless-stopped --log-opt max-size=10m --log-opt max-file=5 \
     --network locos -p 127.0.0.1:8000:8000 -v /srv/loc-data:/data \
     -e LOC_TMS_DATA_DIR=/data -e LOC_TMS_SERVER=1 -e LOC_TMS_SECURE_COOKIES=1 \
     -e LOC_TMS_SESSION_SECRET -e LOC_TMS_ENCRYPTION_KEY "${APP_ID:?scan the image in section 3 first}"
   unset LOC_TMS_SESSION_SECRET LOC_TMS_ENCRYPTION_KEY
   ```

   Set no cloud provider key unless you intend to use that engine. `LOC_TMS_SECURE_COOKIES=1`
   marks the session cookie Secure: TLS ends at Caddy, so the app cannot see HTTPS by itself.
5. Check the secrets really arrived: `docker exec locosapp ls -a /data` must NOT list `.enc_key`
   or `.session_secret` (the app writes those only when it got no secret from the environment). If
   either is there, remove the container and the data folder (safe only now, before anyone has used
   the install) and repeat steps 1 to 4 as `ec2-user`.
6. Claim the first admin privately BEFORE the site is public, entirely on the host:

   ```bash
   read -r -p "Admin email > " LOC_TMS_ADMIN_EMAIL; export LOC_TMS_ADMIN_EMAIL
   read -rs -p "Paste the admin password (hidden) > " LOC_TMS_ADMIN_PASSWORD; echo; export LOC_TMS_ADMIN_PASSWORD
   docker exec -e LOC_TMS_ADMIN_EMAIL -e LOC_TMS_ADMIN_PASSWORD locosapp python -m app.cli init --from-env
   unset LOC_TMS_ADMIN_EMAIL LOC_TMS_ADMIN_PASSWORD
   ```

   Only "Initialized. Admin created" means it made your admin (the password needs at least 12
   characters). Check that `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/auth/setup`
   prints `303`, and that the audit log shows exactly one first-admin row.
7. Configure the engine in Setup AI and make it the default (the code default provider is
   `claude`, which fails loudly without a key). After DNS works, pin the host from the box:
   `docker exec locosapp python -m app.cli config set app_base_url https://tms.example.com --yes`
   and `... config set security_allowed_hosts tms.example.com --yes` (the `--yes` is required
   without a terminal).

### Two-step sign-in (MFA)

Two-step sign-in is available to every user (Account > Two-step sign-in) and can be required for
everyone (Security > "Who must use two-step sign-in"). Before raising `mfa_required` for accounts
whose passwords may have been exposed, rotate those passwords first, and configure SMTP so
enrollment, disable and replacement-code notices reach the account owner: otherwise a password
holder can be the first to enroll a new factor. Break-glass if an enrolled admin loses the phone
AND the recovery codes: on the host,
`docker exec locosapp python -m app.cli user mfa-reset <admin email> --yes`, then sign in and set
it up again. SSO users use their identity provider's MFA.

## 5. Cloudflare, Caddy and the origin header

**Validate-before-change rule:** run `caddy validate` before EVERY change under `/srv/caddy`,
including rotation, certificates and hostname edits. Prepare the complete candidate directory at
`/srv/caddy-next`, validate it mounted as `/etc/caddy`, then install it into `/srv/caddy`. On
initial setup, assemble the complete directory first and validate before starting Caddy. Never
leave an invalid file on disk: if reload reports an error, fix or restore the file immediately.
The running proxy still uses the old config, but the next restart will not start.

1. DNS: an A record for the hostname to the Elastic IP, **Proxied** (orange cloud). No AAAA record.
2. SSL/TLS mode **Full (strict)**; Always Use HTTPS on; minimum TLS 1.2.
3. Origin certificate: SSL/TLS > Origin Server > Create certificate (RSA 2048). The key is shown
   once. Put both on the host, readable only by root:
   `sudo install -d -m 700 /srv/caddy`, `sudo install -m 600 /dev/null /srv/caddy/origin-key.pem`
   (new installs only; never overwrite an existing key), then paste the certificate into
   `/srv/caddy/origin.pem` and the key into `/srv/caddy/origin-key.pem`. Keep a copy of the key in
   your password manager in case the host is rebuilt.
4. Confirm the Cloudflare Free Managed Ruleset is enabled. Turn **Bot Fight Mode OFF**: it
   challenges API and CLI clients and cannot be bypassed on the free plan.
5. Before starting Caddy, confirm the first admin exists (section 4):
   `curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: tms.example.com' http://127.0.0.1:8000/auth/setup`
   must print `303`. `200` means no admin exists yet (starting Caddy would put an unclaimed setup
   form on the internet); `400` means the host pin refused the Host header.
6. Copy `caddy/Caddyfile.cloudflare` to `/srv/caddy/Caddyfile` and change `tms.example.edu` to
   your hostname.

### Origin authentication

Create a different random origin proof for each installation. Run this only on initial setup or
deliberate rotation:

```bash
sudo install -m 600 /dev/null /srv/caddy/origin-auth.caddy
printf '@origin header X-Locos-Origin %s\n' "$(openssl rand -hex 32)" \
  | sudo tee /srv/caddy/origin-auth.caddy >/dev/null
sudo cat /srv/caddy/origin-auth.caddy
```

In the Cloudflare dashboard: Rules > Transform Rules > Modify Request Header > Create rule. Match
**Hostname equals your hostname**, covering ALL paths including `/ready`, `/api` and static files.
**Set static** header `X-Locos-Origin` to the 64-character value just generated. Use **Set**,
never Add: overwrite any value supplied by a visitor. Deploy the rule on the same zone as the
enabled managed WAF. Keep the value in your password manager; do not put it in tickets, logs,
screenshots or the repository. The shared Cloudflare Origin Pull CA alone is insufficient: it
authenticates other Cloudflare customers too. This header identifies your zone.

Scan the pinned Caddy image before starting it, then validate. A failing Caddy scan means STOP:
do not start Caddy until the finding is fixed (a reviewed pin update) or a named approver accepts
the risk in writing. Use the digest pinned in `caddy/Caddyfile.cloudflare` and `aws-redeploy.sh`:

```bash
CADDY_IMAGE=caddy:2@sha256:0c994536bddb66445885237f1a5dcc1916bccea922661c76b4e9fc24061f9b52
docker pull "$CADDY_IMAGE" && sh scripts/scan_image.sh "$CADDY_IMAGE"
docker run --rm -v /srv/caddy:/etc/caddy:ro "$CADDY_IMAGE" \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker run -d --name caddy --restart unless-stopped \
  --log-opt max-size=10m --log-opt max-file=5 --network locos --ip 172.30.0.10 \
  -p 80:80 -p 443:443 -v /srv/caddy:/etc/caddy:ro \
  -v caddy_data:/data -v caddy_config:/config "$CADDY_IMAGE"
```

Test directly against Caddy over loopback, bypassing the security group (`-k` only because browsers
do not trust the Origin CA certificate):

```bash
curl -ksS --resolve tms.example.com:443:127.0.0.1 -o /dev/null -w '%{http_code}\n' https://tms.example.com/ready
curl -ksS --resolve tms.example.com:443:127.0.0.1 -H 'X-Locos-Origin: wrong' -o /dev/null -w '%{http_code}\n' https://tms.example.com/ready
curl -sS -H 'X-Locos-Origin: wrong' -o /dev/null -w '%{http_code}\n' https://tms.example.com/ready
```

The first two MUST return **403**. After step 7 below opens Cloudflare access, the third MUST
return **200** through public DNS, proving your zone overwrites a supplied wrong header. Repeat the
direct tests for `/auth/login` and `/api`; neither may reach the app. Missing authentication files
must make Caddy validation fail. No path is exempt, including health checks: Route 53 uses the
public hostname through Cloudflare. Caddy strips the header before forwarding to the app. Do not
enable request-header logging at Caddy or Cloudflare. The `--ip 172.30.0.10` matters: it is the
address the app trusts in step 8.

**Rotation without a 403 window.** Back up `/srv/caddy`, copy it to `/srv/caddy-next`, generate a
new value, and edit the candidate `origin-auth.caddy` to accept both values:
`@origin header X-Locos-Origin <old> <new>`. Values in one header matcher are OR'd. Use a fresh
backup and candidate directory for each rotation:

```bash
CADDY_BACKUP="/srv/caddy.bak-$(date +%Y%m%d-%H%M%S)"
sudo cp -a /srv/caddy "$CADDY_BACKUP"
# STOP if /srv/caddy-next already exists; resolve the previous change first.
sudo test ! -e /srv/caddy-next && sudo cp -a /srv/caddy /srv/caddy-next
```

After editing the candidate, validate and install (validate before EVERY installation):

```bash
docker run --rm -v /srv/caddy-next:/etc/caddy:ro "$CADDY_IMAGE" \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && \
  sudo cp -a /srv/caddy-next/. /srv/caddy/ && \
  docker exec caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```

If reload fails, validate the backup and immediately restore its files. After a successful
dual-value reload, switch the Cloudflare rule to the new value and confirm public **200**. Take
another dated backup of the live dual-value config, so a rollback still accepts the new Cloudflare
value. Then remove the old value from the candidate, validate, install and reload with the same
command, and repeat the 200/403 checks. Never disable authentication to rotate.

### Existing host: Caddy backup, swap and rollback

With the new `Caddyfile.cloudflare`, Caddy will not start without `/etc/caddy/origin-auth.caddy`.
Do not copy the new-host launch command onto a host that already serves traffic.

**Precondition check.** Confirm the DNS record is Proxied and require a `cf-ray` response header:

```bash
curl -fsSI https://tms.example.com/ready | grep -i '^cf-ray:'
sudo docker inspect caddy --format '{{json .Mounts}} {{json .NetworkSettings.Networks}}'
```

If it is not proxied, STOP; do not install origin-auth on that host (record "N/A, direct Let's
Encrypt origin"). If evidence is inconclusive, STOP and investigate. Also review client-IP
handling before enabling Cloudflare: preserving Caddy's address alone does not make forwarded
visitor addresses trustworthy.

Before any edits, back up the actual config directory and the full inspect output. These commands
assume inspect confirms the config directory is `/srv/caddy`; otherwise STOP and substitute its
actual path throughout:

```bash
umask 077
CADDY_STAMP=$(date +%Y%m%d-%H%M%S)
CADDY_BACKUP="/srv/caddy.bak-$CADDY_STAMP"
sudo cp -a /srv/caddy "$CADDY_BACKUP"
sudo docker inspect caddy > "$HOME/caddy-inspect-$CADDY_STAMP.json"
sudo docker inspect caddy --format \
  '{{range $n, $v := .NetworkSettings.Networks}}{{$n}} {{$v.IPAddress}}{{println}}{{end}}'
```

Record the network and address as `HOST_NETWORK` and `HOST_IP`, the image ID as `HOST_IMAGE`
(never its mutable tag), and the `/data` and `/config` mount sources as `HOST_DATA` and
`HOST_CONFIG` from the saved inspect output. Preserve every other launch option, mount and network
attachment; adapt the command below from inspect first. Do not guess these values or reuse the
new-host `locos` network and `172.30.0.10`. Make sure `caddy-old` and `/srv/caddy-next` do not
already exist, copy `/srv/caddy` to `/srv/caddy-next`, and prepare the Cloudflare template,
certificates and origin-auth file there. Configure the header rule for that hostname's zone before
swapping.

Scan the running image first, even for an upgrade (the retained `caddy-old` keeps that image and a
rollback may start it again):

```bash
HOST_IMAGE="$(sudo docker inspect --format '{{.Image}}' caddy)"
sh scripts/scan_image.sh "${HOST_IMAGE:?}"
```

STOP on scan exit 1 or 2; any risk acceptance needs a named approver and reason in the change log.
For an image upgrade, pull the reviewed pinned replacement, resolve its local image ID into
`HOST_IMAGE` and scan that exact ID before validation or stopping Caddy. Then validate the
candidate and swap, renaming the old container (never remove it during the swap):

```bash
: "${HOST_NETWORK:?}" "${HOST_IP:?}" "${HOST_DATA:?}" "${HOST_CONFIG:?}"
sudo docker run --rm -v /srv/caddy-next:/etc/caddy:ro "${HOST_IMAGE:?}" \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo cp -a /srv/caddy-next/. /srv/caddy/ && \
  sudo docker rename caddy caddy-old && sudo docker stop caddy-old && \
  sudo docker run -d --name caddy --restart unless-stopped \
    --log-opt max-size=10m --log-opt max-file=5 \
    --network "${HOST_NETWORK:?}" --ip "${HOST_IP:?}" \
    -p 80:80 -p 443:443 -v /srv/caddy:/etc/caddy:ro \
    -v "${HOST_DATA:?}:/data" -v "${HOST_CONFIG:?}:/config" "${HOST_IMAGE:?}"
sudo docker inspect caddy --format '{{json .NetworkSettings.Networks}}'
```

STOP if validation fails. Confirm the address still matches `trusted_proxy_ips`, verify real visitor
addresses in the audit log, and repeat the direct **403** and public **200** checks. On failure,
restore the backup BEFORE starting the retained container. Exact rollback:

```bash
sudo docker run --rm -v "$CADDY_BACKUP:/etc/caddy:ro" "${HOST_IMAGE:?}" \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && \
sudo mv /srv/caddy "/srv/caddy.failed-$CADDY_STAMP" && \
  sudo cp -a "$CADDY_BACKUP" /srv/caddy && \
  sudo docker rm -f caddy && sudo docker start caddy-old
```

If creation failed before a new `caddy` exists, omit `docker rm -f caddy` and start `caddy-old`
after restoring the files. Verify public availability and log the rollback. Keep `caddy-old` and
the backup until all checks pass; remove the old container only then.

### Close the web ports to everyone but Cloudflare

7. In CloudShell run `bash aws-instance-setup.sh --instance-id i-0123456789abcdef0 --cloudflare-only`
   and type `yes`. It adds the current Cloudflare ranges on 80/443 first, removes every rule that
   lets the whole internet reach 80 or 443 in whatever shape it has, removes old Cloudflare ranges
   Cloudflare no longer publishes, then READS THE GROUP AGAIN and stops with the rule to delete by
   hand if anything still lets the internet in. It reports (never adds) any port 22 rule open to the
   internet.
8. **Tell the app to trust Caddy, and only Caddy**, so the audit log, the access log and the login
   lockout record the visitor's real address. The exact value is:

   ```
   172.30.0.10/32
   ```

   `docker exec locosapp python -m app.cli config set trusted_proxy_ips "172.30.0.10/32" --yes`,
   then `docker restart locosapp`. Why exactly this: `client_ip` trusts `X-Forwarded-For` only when
   the socket peer is in `trusted_proxy_ips`. Caddy trusts Cloudflare, reads the visitor from
   `CF-Connecting-IP` only and REPLACES `X-Forwarded-For` with that one address
   (`header_up X-Forwarded-For {client_ip}` in `Caddyfile.cloudflare`), so the app must NOT trust
   the Cloudflare ranges. Trust only Caddy's own address, not the whole `172.30.0.0/24`, so a
   request from the host itself cannot claim a visitor address. `tests/verify_deploy_security.py`
   (`cloudflare_ip`) proves this against the real code. When Cloudflare changes its ranges, update
   the Caddyfile and the security group together (this value does not change).
9. The security group admits other Cloudflare customers, including Workers. The private origin
   header above is therefore a required launch control; Cloudflare IP filtering alone is
   insufficient.
10. Check it: sign out, sign in again, open the audit log; the newest sign-in must show your own
    public address, not `172.30.0.10` and not a Cloudflare address.
11. Know the Cloudflare limits: on the free plan uploads over 100 MB are refused and a request
    that takes longer than about 100 seconds ends with error 524. For a very large TM import,
    split the file or import on the host over the private network:
    `docker exec locosapp python -m app.cli tm import <project_id> /data/<file>.tmx --source <LOC> --target <LOC>`.

## 6. Backups

1. `aws-instance-setup.sh` already tagged the disk `Backup=daily` and created the snapshot policy
   (check EC2 > Lifecycle Manager). Daily snapshots, kept 35 days, with an encrypted copy to a
   second region kept 35 days. Do not forget the cross-region copy: `aws-verify.sh` checks the
   copies exist and are recent.
2. **Nightly verified application archive.** The snapshot backs up the disk; this layer makes a
   consistent, verified whole-install archive (database, uploads, keyfiles) on a schedule. Copy
   `host/install-app-backup.sh` to the host and, as `ec2-user`, run
   `bash install-app-backup.sh --topic-arn arn:aws:sns:us-east-2:ACCOUNT:locos-alerts`, read the
   preview and type `yes`. It installs `/usr/local/bin/locos-app-backup`,
   `/etc/locos-app-backup.conf` and a systemd service and timer (nightly at 06:00 UTC, before the
   07:00 snapshot; `--time HH:MM` changes it). Each run creates `db backup --full` under
   `/data/backups/`, immediately runs `db verify-backup`, removes a bad archive, records the
   result, and only on a verified success expires successful archives older than 35 days. A failed
   run emails the alerts topic. Check with `systemctl list-timers locos-app-backup.timer`.
3. **Go-live check** (both safe): `sudo /usr/local/bin/locos-app-backup --dry-run` creates and
   verifies one archive and records `OK ... mode=dry` (it keeps every existing archive);
   `sudo /usr/local/bin/locos-app-backup --status` prints the last recorded result. Until an `OK`
   line ending `mode=run` exists, the scheduled verified archive is not proven: run the dry run
   now and check `--status` after the first nightly run. Ad hoc, without the timer:
   `docker exec locosapp python -m app.cli db backup --full /data/backups/<date>.tar.gz` then
   `... db verify-backup /data/backups/<date>.tar.gz` (`docs/BACKUP.md`).
4. Never upload a backup archive anywhere as-is. An install that keeps its secrets as keyfiles puts
   `.enc_key` and `.session_secret` in the archive in the clear. This kit injects the secrets as
   `LOC_TMS_` variables, so the archive should contain no keyfiles, but treat every archive as
   secret anyway; the container settings on the disk (and so every snapshot) hold the two values.

## 7. After-hours alerting

Every alert is an EMAIL to `ops@example.com`, which your phone pushes. SNS can only send SMS to US
numbers from a registered origination number, so email it is; the phone must be set to RING for
these emails in its night mode, and the night test below proves it.

| Alert | Watches | Fires after | Automatic action |
|---|---|---|---|
| Site down | Route 53 health check on `https://tms.example.com/ready` every 30 s | about 3 to 4 minutes | none |
| Host problem | EC2 `StatusCheckFailed_System` | 2 minutes | Recover |
| OS not answering | EC2 `StatusCheckFailed_Instance` | 3 minutes | Reboot |
| Security finding | GuardDuty severity 7 or more | about 5 minutes | none |
| Audit log tampered | daily on the host: `audit verify` | 13:00 UTC | none |

Each alarm also sends an `OK:` email when it clears. `/ready` answers 200 when the database
answers within 2 seconds and 503 otherwise; it needs no sign-in and is exempt from the host pin.
The Route 53 checkers reach the instance through Cloudflare like a visitor, so one check covers
DNS, Cloudflare, the security group, Caddy, the app and its database. If the checker rows show 403
or 503, look at Cloudflare Security > Events for path `/ready` and add a Skip rule for
`/ready` with user agent `Amazon-Route53-Health-Check-Service`.

1. Fast path (does the topic, rule, alarms, health check and role policy; safe to rerun):
   `bash aws-alerting-setup.sh --instance-id i-0123456789abcdef0 --hostname tms.example.com --email ops@example.com --dry-run`,
   read the plan, then run it again without `--dry-run` and type `yes`. `--email` is required.
2. **Authenticated confirmation for BOTH regional subscriptions.** Use the named IAM identity that
   owns the topics. Do not click the confirmation link: copy its address from each AWS
   notification email and extract the `Token` query parameter (URL-decode if escaped). Treat it as
   private. Substitute the account number and each region's token:

   ```bash
   aws sns confirm-subscription --region us-east-2 \
     --topic-arn arn:aws:sns:us-east-2:ACCOUNT:locos-alerts \
     --token '<Token from the us-east-2 email>' --authenticate-on-unsubscribe true
   aws sns confirm-subscription --region us-east-1 \
     --topic-arn arn:aws:sns:us-east-1:ACCOUNT:locos-alerts \
     --token '<Token from the us-east-1 email>' --authenticate-on-unsubscribe true
   ```

   For each returned SubscriptionArn verify, in the matching region:

   ```bash
   aws sns get-subscription-attributes --region us-east-2 --subscription-arn '<ARN>' \
     --query Attributes.ConfirmationWasAuthenticated --output text
   ```

   Both must print **true**. Missing, false or unreadable is a launch blocker: it means possession
   of an alert email is enough to unsubscribe. `aws-verify.sh` checks every confirmed email in both
   regions.

   **Existing subscriptions:** if false, first retry the confirmation with the original unexpired
   token. If unavailable, consumed or expired, arrange a monitored maintenance window and replace
   one region at a time:

   ```bash
   aws sns list-subscriptions-by-topic --region us-east-2 \
     --topic-arn arn:aws:sns:us-east-2:ACCOUNT:locos-alerts
   aws sns unsubscribe --region us-east-2 --subscription-arn '<unprotected ARN>'
   aws sns subscribe --region us-east-2 \
     --topic-arn arn:aws:sns:us-east-2:ACCOUNT:locos-alerts \
     --protocol email --notification-endpoint ops@example.com
   ```

   Confirm the fresh email as above, verify **true**, and receive a test email before moving on.
   Repeat with `us-east-1`. The setup script leaves pending requests alone. Never delete the SNS
   topics. Test delivery in BOTH regions after confirmation with `aws sns publish` to each topic.
3. **The daily audit-log check on the host.** Copy `host/install-audit-alert.sh` to the host and, as
   `ec2-user`, run `bash install-audit-alert.sh --topic-arn arn:aws:sns:us-east-2:ACCOUNT:locos-alerts`
   (preview, then `yes`). It writes `/usr/local/bin/locos-audit-alert`, a config file and a systemd
   service and timer (daily 13:00 UTC), runs `docker exec locosapp python -m app.cli audit verify`
   (a pure read), and sends a Monday "still watching" email when all is well. It publishes with the
   instance role, so no key is stored on the host. The container must keep the name `locosapp`.
4. **Test every alert, now and at the start of each season.** On the host,
   `sudo /usr/local/bin/locos-audit-alert --dry-run` then `--test` (sends one email). Force the
   site-down alarm with `aws cloudwatch set-alarm-state --alarm-name locos-ready-healthcheck --state-value ALARM --state-reason "test" --region us-east-1`
   and put it back to `OK`. For GuardDuty create ONE sample finding:
   `aws guardduty create-sample-findings --detector-id <id> --finding-types 'Backdoor:EC2/C&CActivity.B!DNS' --region us-east-2`
   (keep the single quotes), then archive the `[SAMPLE]` findings; never use the console's
   "Generate sample findings" button. **Never force the two EC2 alarms**: their reboot and recover
   actions are real. Check them in CloudWatch instead. Save
   `bash aws-verify.sh --instance-id i-0123456789abcdef0 --hostname tms.example.com --out verify-alerting-YYYY-MM-DD.txt`
   (every alerting line must be PASS).
5. **Night test (required).** Put the phone in exactly the mode it is in at night, screen locked,
   and run the site-down test again. Pass means the phone makes its loud sound for the ALARM email.
   If it stays silent, fix the Do Not Disturb or Focus exception or the sound and repeat. Repeat
   at the start of each season and after any phone change.
6. Monthly during the season, run `aws-verify.sh`; the "email subscription confirmed" lines prove
   both subscriptions are still there.

### When an alert fires

- **Site down.** Open the site over cellular. If it is down: `sudo docker ps`,
  `curl -s http://127.0.0.1:8000/ready`, `sudo docker logs --tail 100 locosapp`,
  `sudo docker restart locosapp` if hung; check Cloudflare's status page. If the site works for you
  but the alarm stays on, it is the checkers.
- **Host or OS check.** EC2 is already recovering or rebooting; wait for the OK email, then check
  the site. If it repeats, suspect memory: `free -m`, `sudo dmesg | tail -50`.
- **GuardDuty finding.** `[SAMPLE]` means a test. For a real finding do not reboot or terminate
  (that destroys evidence): snapshot the volume first, then follow your incident response plan.
- **Audit log BROKEN.** Treat as a security incident until a benign cause is shown. Do not restart,
  redeploy or restore; snapshot the volume and open the audit log at the first bad row.

## 8. Restore drill

Do one at first launch, then before each busy season. Never delete a snapshot: the drill only makes
a NEW volume from the backup copy. The restored copy must never boot with production's private
network identity or do production's outgoing work, so remove the node key from the restored disk
with a helper instance BEFORE it first boots and switch off scheduled connectors and outbound
mail in the copy. Create a volume from the cross-region snapshot, attach it to a helper instance as
a second disk, clean it, boot it from the restored disk, confirm the app starts and the data age
and contents are right, then terminate the helper and delete the two drill volumes. Record the
date, snapshot used, data age, start and end times and the result.

## 9. Verify and record

Run `bash aws-verify.sh --instance-id i-0123456789abcdef0 --hostname tms.example.com --out verify-YYYY-MM-DD.txt`
and keep the output. It checks the encryption default, IMDS settings, security group, snapshots and
copies, GuardDuty, Inspector and alerting. Log every change you make (Cloudflare, security group,
scan results, rotations) in your change log. Monthly in season: rebuild the image, rescan it and
redeploy with `aws-redeploy.sh` (it scans the exact image ID before it swaps and takes a backup
first).

## Off-season

Stop the instance rather than terminating it; the Elastic IP, snapshots and alerting keep their
cost low. Expect no Monday heartbeat while stopped. At the start of the next season start the
instance, run `aws-verify.sh`, rerun the alert tests and the night test, and do a restore drill.
