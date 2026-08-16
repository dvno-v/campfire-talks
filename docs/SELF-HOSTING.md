# Self-hosting Campfire, step by step

You do not need to be a professional system administrator to follow this guide.

If a command fails, stop at that command and use the troubleshooting section.
Do not keep entering later commands in the hope that the problem fixes itself.

## What this guide builds

The supported public setup has three programs, all managed by Docker Compose:

```text
browser -- HTTPS/WSS :443 --> Caddy -- private --> Campfire
             |                    `-- private --> LiveKit signaling
             `-- UDP :7882 / TURN UDP :443 / ICE TCP :7881 --> LiveKit SFU

Campfire --> SQLite database and uploaded images
LiveKit  --> ephemeral encrypted-frame routing; no media volume
```

Caddy is the only HTTP component exposed to the internet. It obtains and renews
both HTTPS certificates and passes application and LiveKit signaling requests
over a private Docker network. LiveKit additionally exposes only the WebRTC
ICE/TURN ports media requires. Campfire stores its database and uploaded images
in a persistent Docker volume; LiveKit has no persistent media volume.

The included configuration deliberately does **not** expose Campfire's port
8000 to the host or the internet.

## The short version

These are the major stages. The detailed instructions below explain each one.

1. Get a Linux server and two names under a domain.
2. Point `chat.example.net` and `media.example.net` at the server.
3. Allow TCP 80, 443, and 7881 plus UDP 443 and 7882.
4. Install Docker Engine with the Docker Compose plugin.
5. Put the Campfire repository on the server.
6. Copy `.env.example` to `.env` and enter the real domain.
7. Prepare private media and backup keys.
8. Validate, build, and start the Compose stack.
9. Check the readiness URL and create the first account.
10. Create a backup and copy it away from the server.

For a private test on the same computer, you can skip all of this, install
`requirements.txt` in a virtual environment, and run `python -m campfire
serve`, then open <http://localhost:8000>. The rest of this guide is for an
internet-facing server.

## A few words used in this guide

- **Host** or **server**: the Linux machine running Docker. A small VPS is fine.
- **Domain**: the name people type, such as `chat.example.net`.
- **DNS record**: the setting that connects that name to the server's IP address.
- **Container**: an isolated process created from an image. Campfire, Caddy,
  and LiveKit each run in one.
- **Image**: the packaged filesystem used to create a container. This is not a
  chat image upload.
- **Volume**: persistent Docker-managed storage. It survives container
  replacement.
- **Compose**: the `compose.yaml` file and `docker compose` command that manage
  Campfire, Caddy, their network, and their volumes together.
- **Repository** or **checkout**: the directory containing this source code,
  including `compose.yaml` and `Dockerfile`.

## Before you begin

You need:

- a Linux server you can reach with SSH;
- a domain or subdomain you control;
- the server's public IPv4 address and, if enabled, its public IPv6 address;
- current Docker Engine and the Docker Compose plugin; and
- enough disk space for the operating system, Docker, chat data, and backups.

Docker's official installation instructions are at
<https://docs.docker.com/engine/install/>. Installation differs by Linux
distribution, so this guide does not give one universal installation command.
After installing it, both of these commands must work:

```bash
docker --version
docker compose version
```

If Docker says `permission denied` when connecting to its socket, either run the
Docker commands with `sudo` or follow Docker's documented post-installation
steps. Membership in the Docker group is effectively administrator access to
the host. Do not solve the problem by making the Docker socket world-writable.

This guide uses `chat.example.net` and `media.example.net` as placeholders.
Replace both with names you control.

## Step 1: point the domain at the server

Sign in to the company that manages DNS for your domain. Create an **A record**:

```text
Name/host: chat
Type:      A
Value:     YOUR_SERVER_IPV4_ADDRESS
```

For example, an A record named `chat` under `example.net` creates
`chat.example.net`.

Create a second A record named `media` with the same address. If using IPv6,
create matching correct AAAA records for both names.

If the server has working public IPv6, also create an **AAAA record** containing
that IPv6 address. If it does not, do not create an AAAA record. A wrong AAAA
record can make the site fail for visitors whose devices prefer IPv6 even while
IPv4 works.

DNS changes can take time to propagate. From your own computer, check what the
name resolves to:

```bash
getent ahosts chat.example.net
getent ahosts media.example.net
```

You should see the server's public address. If you see an old or different
address, wait or correct the DNS record before continuing. Caddy cannot obtain
the HTTPS certificate until public DNS points to this server.

## Step 2: allow the required network traffic

The following inbound traffic must reach the server:

| Protocol | Port | Purpose |
| --- | ---: | --- |
| TCP | 22 | SSH administration; the exact port may differ on your server |
| TCP | 80 | Initial HTTPS certificate checks and HTTP-to-HTTPS redirects |
| TCP | 443 | The Campfire website over HTTPS |
| UDP | 443 | LiveKit's embedded TURN/UDP relay |
| TCP | 7881 | WebRTC ICE/TCP fallback when UDP cannot pass |
| UDP | 7882 | Direct WebRTC media through LiveKit's UDP mux |

Cloud providers often have a firewall in their web dashboard in addition to a
firewall inside Linux. Check both. Keep SSH restricted to addresses you trust
when practical. Never open the Docker API socket to the internet, and do not
open port 8000.

Firewall commands vary between distributions and providers. Be careful when
changing them over SSH: blocking your SSH port can lock you out of the server.

## Step 3: put Campfire on the server

Connect to the server with SSH and enter the Campfire repository. If you are
already reading this file from a checkout on that server, simply `cd` to its
root directory.

A fresh source checkout looks like this:

```bash
git clone https://github.com/dvno-v/campfire-talks.git
cd campfire-talks
```

Confirm you are in the right place:

```bash
pwd
ls compose.yaml Dockerfile .env.example deploy/Caddyfile
```

The `ls` command should print all four paths without a "No such file" error.
All remaining commands in this guide assume this is your current directory.

For a serious deployment, use a tagged release and verify its checksum and
attestation as described in [RELEASES.md](RELEASES.md). A checkout of the master
branch is convenient for evaluation but can change between deployments.

## Step 4: create the configuration file

Compose reads local settings from a file named `.env`. Create it from the
example and make it readable only by your account:

```bash
cp .env.example .env
chmod 600 .env
```

Open it with an editor. `nano` is a beginner-friendly option if it is installed:

```bash
nano .env
```

Set both hostnames. Do not include a scheme, path, or trailing slash. The media
name must differ from the application name. A valid file looks like this:

```dotenv
CAMPFIRE_DOMAIN=chat.example.net
CAMPFIRE_MEDIA_DOMAIN=media.example.net
CAMPFIRE_LIVEKIT_API_KEY=campfire-media
CAMPFIRE_MAX_STORAGE_BYTES=10737418240
CAMPFIRE_STORAGE_WARNING_PERCENT=90
CAMPFIRE_MAX_UPLOAD_BYTES=8388608
CAMPFIRE_MAX_REQUEST_BODY=9MB
```

In `nano`, press `Ctrl+O`, then Enter to save, and `Ctrl+X` to exit.

The settings mean:

- `CAMPFIRE_DOMAIN` is the public hostname. Compose constructs the exact HTTPS
  origin from it.
- `CAMPFIRE_MEDIA_DOMAIN` is the separate LiveKit signaling hostname. Both DNS
  names may point to the same public address.
- `CAMPFIRE_LIVEKIT_API_KEY` names the matching entry in the private LiveKit key
  file prepared next. It is an identifier, not the secret itself.
- `CAMPFIRE_MAX_STORAGE_BYTES` is the maximum space Campfire will allow uploaded
  images to consume. `10737418240` bytes is 10 GiB. It is an application safety
  ceiling, not extra disk space.
- `CAMPFIRE_STORAGE_WARNING_PERCENT=90` shows a warning at 90% usage.

Choose a storage ceiling smaller than the server's available disk. Leave room
for Linux, Docker, the SQLite database, Caddy's certificate data, and local
backups. As rough conversion helpers, 1 GiB is `1073741824` bytes and 5 GiB is
`5368709120` bytes.

The domain is not a password, but keeping `.env` private is a good habit because
future versions may add sensitive settings.

## Step 5: prepare media and backup keys

The web container never sees `backups/` or `backup.key`; a networkless operator
container mounts those only for an explicit backup, verification, or restore.
Campfire and LiveKit do both need the separate LiveKit API mapping. All three
application/operator containers use the deliberately unprivileged numeric user
`10001`, so prepare the private paths for that user:

```bash
sudo install -d -o 10001 -g 10001 -m 700 backups secrets
sudo dd if=/dev/urandom of=secrets/backup.key bs=32 count=1 status=none
sudo chown 10001:10001 secrets/backup.key
sudo chmod 600 secrets/backup.key
openssl rand -hex 32 | sed 's/^/campfire-media: /' | sudo tee secrets/livekit-keys.yaml >/dev/null
sudo chown 10001:10001 secrets/livekit-keys.yaml
sudo chmod 600 secrets/livekit-keys.yaml
```

Keep another protected copy of `backup.key` somewhere separate from both the
server and its encrypted backups. Someone with the key can read every backup;
without it, nobody can recover them. Both `backups/` and `secrets/` are ignored
by Git. Never commit, email, or place the key alongside an off-server backup.

The name before the colon in `livekit-keys.yaml` must exactly match
`CAMPFIRE_LIVEKIT_API_KEY`. The 64 hex characters after it are the API secret;
they let a holder mint LiveKit permissions, so protect this file like a server
credential. Campfire and LiveKit mount the same one-line file, avoiding a
duplicate secret or an inspectable secret environment variable. This API secret
is not a call's media key: room keys are generated only in browsers and are not
backed up.

Check the numeric ownership:

```bash
ls -ldn backups secrets
ls -ln secrets/backup.key secrets/livekit-keys.yaml
```

The owner and group columns should say `10001`; directories should begin with
`drwx------` and both files with `-rw-------`. Do not print either key as a
diagnostic.

## Step 6: validate the configuration

Ask Compose to render and validate the full configuration:

```bash
docker compose config --quiet
```

Success produces no output and returns to the prompt. Silence is good here.

If it says `CAMPFIRE_DOMAIN is missing a value`, check that:

- the file is named exactly `.env`, not `.env.txt`;
- it is in the same directory as `compose.yaml`; and
- the domain line contains a value with no spaces around `=`.

You can also display the rendered model with `docker compose config`. That is a
useful diagnostic, but it is much longer.

## Step 7: build and start Campfire

Build the application image using the pinned Python base image:

```bash
docker compose build --pull campfire
```

The first build downloads layers and can take a few minutes. Later builds reuse
cached layers. A successful build ends without an error and creates the local
`campfire` image.

Start Campfire, Caddy, and LiveKit in the background:

```bash
docker compose up -d
```

`-d` means "detached": the services continue running after the command returns.
Now inspect them:

```bash
docker compose ps
```

You should see `campfire`, `caddy`, and `livekit`. Campfire may say
`health: starting` for several seconds, followed by `healthy`. Caddy publishes
TCP 80/443. LiveKit publishes UDP 443/7882 and TCP 7881. Campfire port 8000 and
LiveKit signaling port 7880 must not appear as host-published ports.

If either service exits or remains unhealthy, inspect the application output:

```bash
docker compose logs --tail=100 campfire
docker compose logs --tail=100 caddy
docker compose logs --tail=100 livekit
```

Routine request and Caddy runtime logging are intentionally discarded, so the
Caddy output may be sparse. The troubleshooting section has more targeted
checks.

## Step 8: confirm HTTPS and readiness

Caddy requests a trusted certificate automatically. The first request may take
a short while during that process. Test the readiness endpoint, replacing the
example domain:

```bash
curl --fail --show-error https://chat.example.net/readyz
```

A healthy response resembles:

```json
{"status":"ready","checks":{"database":"ok","storage":"ok"}}
```

`curl --fail` returns an error for an HTTP failure status. If DNS was only just
changed, wait a little and retry. If it continues failing, work through the DNS,
ports, certificate, and readiness entries in the troubleshooting section.

Finally, open this address in a browser:

```text
https://chat.example.net
```

Do not continue if the browser shows a certificate warning. A correct public
deployment obtains a certificate trusted by the browser; clicking through a
warning would hide a DNS, firewall, or proxy problem.

Also confirm `https://media.example.net` reaches the LiveKit signaling edge. A
plain browser page may be empty or return a small service response; it must have
a trusted certificate and must not resolve to an unrelated host. Complete the
direct-UDP and forced-TURN call checks in [MEDIA.md](MEDIA.md) after creating
two accounts.

## Step 9: create the first account

The first account created on an empty instance is allowed without an invite and
creates the first community. Every later account needs an invite code, so public
registration closes automatically after that first account.

Choose the first username and password carefully because that account owns the
initial community. Once signed in, use the invite control in the channel
sidebar to create a time-limited invite for each friend.

In **Account security**, add a passkey from a device, password manager, or
hardware security key and test signing out and back in. WebAuthn depends on the
exact HTTPS domain, so a passkey registered here is deliberately not valid for
a different hostname. Keep the password protected: it remains the recovery and
passkey-management credential.

At this point the server is usable. The next step—making a tested backup—is what
turns a working experiment into something you can recover.

## Step 10: create and verify the first encrypted backup

Use a unique filename for every backup. Dates in `YYYY-MM-DD` order sort
naturally. The operator profile has no network and is the only container that
can see `/backups` and `/run/secrets`:

```bash
docker compose --profile operations run --rm --no-deps campfire-ops \
  backup-encrypted /backups/2026-08-14-first-working-deploy.campfire-backup \
  --key-file /run/secrets/backup.key
```

Replace the date with today's date. The destination must not already exist,
preventing accidental overwrite. Success prints one JSON line containing
`"ok": true`. Authenticate the AES-256-GCM file and then verify its internal
manifest, database, and every referenced image:

```bash
docker compose --profile operations run --rm --no-deps campfire-ops \
  verify-encrypted-backup \
  /backups/2026-08-14-first-working-deploy.campfire-backup \
  --key-file /run/secrets/backup.key
```

Plaintext snapshot staging exists only transiently on the already-plaintext
`campfire-data` volume and is removed. The host backup directory receives only
the atomically published encrypted file. Verification authenticates the whole
archive before safe extraction, then checks SQLite integrity, schema, every
recorded size and SHA-256 hash, and the database/upload relationship.

The backup is still on the same server. It protects against a bad upgrade or
accidental data replacement, but not total server loss. Copy completed
`.campfire-backup` files to another machine or provider, while keeping the key
in a different protected location.

For example, run the following from your own computer—not from the server—and
replace the login, server address, and absolute repository path:

```bash
scp \
  operator@YOUR_SERVER:/absolute/path/to/campfire-talks/backups/2026-08-14-first-working-deploy.campfire-backup \
  ./
```

SSH protects the transfer and the file remains encrypted at rest. After copying,
verify the off-server copy with a separately supplied key before depending on
it. Do not copy `backup.key` into the same folder or provider account.

## Everyday command cheat sheet

Run these commands from the repository directory:

```bash
# See whether the services are running
docker compose ps

# Start or apply a Compose configuration change
docker compose up -d

# Stop services without deleting their persistent volumes
docker compose stop

# Restart the application
docker compose restart campfire

# Show recent application output
docker compose logs --tail=100 campfire

# Follow new application output until Ctrl+C
docker compose logs --follow campfire

# Check the public readiness endpoint
curl --fail --show-error https://chat.example.net/readyz

# Validate the public configuration inside a temporary container
docker compose run --rm --no-deps campfire check-config
```

`docker compose down` removes the containers and private network but keeps named
volumes unless `--volumes` is added. Do **not** add `--volumes` during ordinary
maintenance; that requests deletion of the persistent Campfire and Caddy data.

## Where the data actually lives

The Compose deployment uses three named volumes and two private host
directories:

| Storage | Contents | Must be backed up? |
| --- | --- | --- |
| `campfire-data` volume | SQLite database and uploaded images | Yes, through Campfire's backup command |
| `caddy-data` volume | HTTPS certificates and Caddy state | Usually no; Caddy can obtain certificates again |
| `caddy-config` volume | Caddy runtime configuration state | Usually no; it is recreated from the checked-in Caddyfile |
| local `backups/` directory | Authenticated encrypted Campfire backup files | Yes; copy these off the server |
| local `secrets/` directory | The separate raw backup key | Yes, but never beside the backups |

The exact Docker-generated volume name normally includes the repository
directory or Compose project name. You do not need to find or copy the volume
directory manually. Use Campfire's backup command so SQLite and uploads are
captured as one consistent snapshot.

Do not copy only `campfire.db` while the application is running. SQLite may have
current state in `campfire.db-wal`, and a lone database copy can disagree with
the uploaded files it authorizes.

## How online backups work

The backup command can run while Campfire is serving users. It briefly reserves
the database against new writes, creates a consistent SQLite snapshot, and
copies exactly the uploads referenced by that snapshot. New messages pause
during this operation, so a very large instance should back up during a quiet
period. Existing readers and live event streams can continue.

Inside the encrypted archive the authenticated snapshot has exactly this
shape:

```text
campfire.db
manifest.json
uploads/
└── ...stored image files...
```

Campfire builds the plaintext snapshot transiently on the data volume, streams
it into AES-256-GCM, and only publishes the requested ciphertext file after
every copy and filesystem sync succeeds. A missing upload, bad size, unsafe
filename, or write error aborts instead of publishing a backup that merely
looks complete.

A sensible small-instance policy is:

- create a backup before every upgrade;
- create scheduled daily or weekly backups according to how much chat you can
  afford to lose;
- verify every new backup;
- keep more than one generation; and
- regularly copy them to encrypted storage outside this host.

A backup you have never verified or restored in a rehearsal is only a hope.

## Restore and disaster recovery

Restore **replaces the current database and uploads**. It is intentionally an
offline action and requires the explicit `--confirm` flag.

Before restoring, make sure you selected the correct backup file. Verify
it one more time while the current server is still running:

```bash
docker compose --profile operations run --rm --no-deps campfire-ops \
  verify-encrypted-backup /backups/2026-08-14-before-upgrade.campfire-backup \
  --key-file /run/secrets/backup.key
```

Then stop Campfire, restore, migrate if necessary, and start the stack:

```bash
docker compose stop campfire

docker compose --profile operations run --rm --no-deps campfire-ops \
  restore-encrypted /backups/2026-08-14-before-upgrade.campfire-backup \
  --key-file /run/secrets/backup.key --confirm

docker compose --profile operations run --rm --no-deps campfire-ops migrate

docker compose up -d

curl --fail --show-error https://chat.example.net/readyz
```

What each command does:

1. `stop campfire` closes the live application so data cannot change.
2. `restore ... --confirm` verifies the entire backup before touching live data,
   stages its files privately, and installs them together.
3. `migrate` moves an older restored schema forward to the version required by
   the current image.
4. `up -d` starts or recreates the services.
5. `curl` confirms that database and storage checks pass.

Restore takes an exclusive operation lock and refuses to run if Campfire or a
backup operation still holds that lock. If an installation step fails after
replacement starts, it puts the previous database and uploads back.

After restoring, sign in and check an older text message and an older image, then
send a new message. Readiness proves the service can operate; these manual checks
prove you restored the data you intended.

## Upgrading safely

Do not discover your rollback procedure for the first time during a failed
production upgrade. Rehearse it once on a disposable copy.

For each release:

1. Read the release notes for configuration or migration changes.
2. Verify the downloaded release checksum and signed attestation as described in
   [RELEASES.md](RELEASES.md).
3. Create and verify a uniquely named pre-upgrade backup.
4. Copy that backup away from the server.
5. Fetch or check out the new verified release.
6. Render the configuration with `docker compose config --quiet`.
7. Build the new image while the old service remains available.
8. Stop Campfire, run migrations with the new image, and start the stack.
9. Check readiness, sign in, read old content, and send a new message.
10. Keep the pre-upgrade backup until the new release has survived your normal
    backup-retention period.

The core commands after preparing and verifying the backup are:

```bash
docker compose config --quiet
docker compose build --pull campfire
docker compose stop campfire
docker compose --profile operations run --rm --no-deps campfire-ops migrate
docker compose up -d
curl --fail --show-error https://chat.example.net/readyz
```

Startup also applies pending migrations automatically. Running `migrate`
explicitly while Campfire is stopped makes the maintenance event easier to
observe and troubleshoot.

Each schema migration runs in its own transaction and is recorded only after it
succeeds. An interrupted migration rolls back and is retried on the next start.
Campfire also refuses a database created by a newer application version.

## Rolling back a failed upgrade

Database migrations are forward-only. Never run an older Campfire image against
a database that a newer release migrated.

To roll back:

1. Stop Campfire.
2. Restore the verified pre-upgrade backup with `--confirm`.
3. Check out or reinstall the previous verified release.
4. Build that previous release and start the stack.
5. Check readiness and manually test old text and image retrieval.

This restores both sides of the deployment to the same point in time: the older
application code and its matching older data.

## Health checks and storage warnings

Campfire provides two deliberately small unauthenticated endpoints:

- `/healthz` answers if the HTTP process is alive.
- `/readyz` checks the expected database schema, writable data paths, and usable
  filesystem space.

Readiness returns HTTP `503` and `Retry-After: 5` if it cannot safely serve. Its
response names only broad checks; it does not expose filesystem paths, raw
exception text, or how full the instance is. Both endpoints are unauthenticated
and reachable from the public name, so they answer only the question a probe is
entitled to ask.

Capacity is reported separately, to administrators only. The storage panel in
community settings warns when image storage or a backing filesystem reaches
`CAMPFIRE_STORAGE_WARNING_PERCENT`; the same warnings are returned by
`GET /api/storage`, which requires an administrator session. A warning does not
make the instance unavailable because text chat may still work. No usable
filesystem space *does* fail readiness.

### Image size limits

Two ceilings govern one upload and must be set together:

| Setting | Applies at | Default |
| --- | --- | --- |
| `CAMPFIRE_MAX_UPLOAD_BYTES` | Campfire, in bytes | `8388608` (8 MiB) |
| `CAMPFIRE_MAX_REQUEST_BODY` | Caddy, as a size string | `9MB` |

Caddy refuses an oversized body before Campfire ever sees it, so a proxy ceiling
at or below the application ceiling rejects uploads with an error Campfire never
wrote and cannot explain. Keep the proxy value comfortably above the byte value
whenever you raise either. Campfire publishes its own limit to the browser
through `GET /api/bootstrap`, so the client refuses an oversized image locally
using the same number the server enforces.

## Why public configuration fails closed

The private default listens only on `127.0.0.1`. The Compose service deliberately
listens on `0.0.0.0` inside its private container network, so Campfire refuses to
start unless all public safeguards are present:

- one exact `https://` public origin with no path or trailing slash;
- secure session cookies;
- a non-empty, limited trusted-proxy address or network; and
- a non-zero storage ceiling.

All numeric settings reject malformed and out-of-range values. The database and
upload paths cannot overlap or contain one another. Per-user live connections
cannot exceed the instance-wide limit.

This is why a typo may prevent startup instead of quietly falling back to a less
secure behavior. Run this after any environment or Compose change:

```bash
docker compose run --rm --no-deps campfire check-config
```

It should print:

```text
configuration is safe
```

The included stack gives Caddy the fixed private address `172.31.238.2` and
configures Campfire to trust only that address. Caddy overwrites forwarded client
address input before passing the request onward. Do not publish Campfire port
8000 or LiveKit signaling port 7880, and do not broaden the trusted-proxy
setting to `0.0.0.0/0`.

## What the container hardening does

The Campfire process runs as UID/GID `10001` rather than root. The Compose file:

- drops every Linux capability from Campfire;
- sets `no-new-privileges`;
- makes the application filesystem read-only;
- provides a small temporary filesystem and CPU, memory, PID, request,
  connection, thread, and file-descriptor ceilings;
- gives the web application only its writable data volume, never the backup
  directory or backup key;
- places the application network behind an `internal` boundary; and
- gives a separate networkless operator profile the data, backup, and read-only
  key mounts only for explicit maintenance commands.

Caddy publishes TCP 80/443 and stores certificates in its own volume. LiveKit
publishes only UDP 443/7882 and TCP 7881 for TURN/ICE media. All external image
references are pinned so a rebuild cannot silently change to an unrelated
`latest` image. Dependabot proposes pin changes for review, and the security
workflow scans built images and source.

The supplied Caddyfile obtains HTTPS certificates, rejects request bodies above
9 MB before forwarding, avoids buffering live event streams, and discards
routine access and runtime logs. Campfire access logging is also disabled. The
small bounded Docker logs therefore contain startup and exceptional maintenance
information rather than a history of who visited which route.

## Troubleshooting

### A required domain is missing a value

Compose did not find a usable `.env` value. From the repository directory run:

```bash
ls -la .env compose.yaml
sed -n '1,20p' .env
```

Confirm the file is beside `compose.yaml` and contains a line like
`CAMPFIRE_DOMAIN=chat.example.net` and a separate
`CAMPFIRE_MEDIA_DOMAIN=media.example.net`.

### Docker cannot connect to the daemon

Errors mentioning `/var/run/docker.sock`, `permission denied`, or "daemon is not
running" mean this is a Docker installation/service problem rather than a
Campfire problem. Check:

```bash
docker info
```

Start Docker using your distribution's service manager or use an account with
authorized Docker access. Do not change the socket to world-writable.

### Campfire stays unhealthy

Inspect its status and output, then run configuration validation separately:

```bash
docker compose ps
docker compose logs --tail=100 campfire
docker compose run --rm --no-deps campfire check-config
```

Common causes are an unsafe origin, incorrect permissions, a full filesystem,
or a database created by a newer release.

### The backup command says permission denied

Check the host backup and key paths:

```bash
ls -ldn backups secrets
ls -ln secrets/backup.key
```

They should belong to numeric user and group `10001`, with directory mode `700`
and key mode `600`. Repair them:

```bash
sudo chown 10001:10001 backups secrets secrets/backup.key secrets/livekit-keys.yaml
sudo chmod 700 backups secrets
sudo chmod 600 secrets/backup.key secrets/livekit-keys.yaml
```

### HTTPS does not work

Check these in order:

1. Do both domains resolve to this server's public IP?
2. Is there an incorrect AAAA record pointing somewhere else?
3. Do provider and host firewalls allow the ports in Step 2?
4. Is another process already using TCP 80/443 or UDP 443?
5. Are all three Compose services running?

Useful commands are:

```bash
getent ahosts chat.example.net
docker compose ps
sudo ss -ltnp
curl --verbose https://chat.example.net/readyz
```

The verbose curl command includes connection and certificate diagnostics. It may
display addresses and certificate names in your terminal, so avoid pasting its
complete output into a public issue without reviewing it.

### Readiness returns `503`

Read the JSON response. `"database":"failed"` points to migration, integrity,
or schema trouble. `"storage":"failed"` points to unwritable paths or a full
filesystem. Start with non-changing diagnostics:

```bash
docker compose logs --tail=100 campfire
df -h
```

If the database check failed and the logs say a migration is pending, perform
that migration offline:

```bash
docker compose stop campfire
docker compose --profile operations run --rm --no-deps campfire-ops migrate
docker compose up -d
```

Do not delete database sidecar files or Docker volumes as a troubleshooting
shortcut. Take a verified backup before any repair that changes data.

### A backup destination already exists

This is intentional overwrite protection. Choose a new unique backup filename.
Do not delete the old file until you have confirmed what it contains and
whether your retention policy still needs it.

### Restore refuses because Campfire is running

Stop only the application, then retry:

```bash
docker compose stop campfire
docker compose --profile operations run --rm --no-deps campfire-ops \
  restore-encrypted /backups/YOUR_BACKUP.campfire-backup \
  --key-file /run/secrets/backup.key --confirm
```

If it still reports a lock, check that no other backup, migration, restore, or
temporary Campfire container is running with `docker compose ps --all`.

## Native private deployment

For a loopback-only machine or a trusted VPN environment, the Python process
can run directly from a virtual environment:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
CAMPFIRE_DB=/srv/campfire/campfire.db \
CAMPFIRE_UPLOAD_DIR=/srv/campfire/uploads \
.venv/bin/python -m campfire serve
```

By default it listens only on `127.0.0.1:8000`. Use the same `check-config`,
`migrate`, directory backup/restore, and encrypted backup/restore subcommands.
Keep the database, uploads, backups, and encryption key readable only by the
service account, and retain the key separately.

A public native deployment is an advanced configuration. It still needs a
maintained HTTPS reverse proxy and every fail-closed setting described above.
The included Compose/Caddy stack is the supported public starting point because
its network boundary and proxy address already agree.
