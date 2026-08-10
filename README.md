# Campfire

Campfire is a self-hosted, Discord-style chat server for a small group of people
who already know each other. It is deliberately dependency-free: Python serves
the API and the static client, SQLite stores everything, and browsers receive
live messages over Server-Sent Events. No build step, no package manager, no
third-party application service. The supported public deployment adds a local
HTTPS reverse proxy; Campfire itself still makes no outbound service calls.

It is built around a few refusals — no advertising, no analytics, no telemetry,
no public discovery, no remote assets — and around collecting only the data a
requested feature actually needs.

## Where it stands

**Private text chat is complete.** Accounts, communities, channels, messages,
images, roles, moderation, retention and data export all work and are covered by
tests.

**Reproducible self-hosting is complete.** Versioned transactional migrations,
verified database/upload snapshots, offline restore, fail-closed public
configuration, a non-root Docker image, and a minimal Caddy Compose deployment
are covered by the [self-hosting guide](docs/SELF-HOSTING.md).

Milestones 1–3 of the [roadmap](ROADMAP.md) are done. What is missing is voice,
direct messages, reactions, replies, and search.

## Run it

```bash
python3 -m campfire serve
```

Open <http://localhost:8000>.

For an internet-facing instance, use the included non-root Compose/Caddy stack:

```bash
cp .env.example .env
# Set CAMPFIRE_DOMAIN, then prepare backups as described in the guide.
docker compose build --pull campfire
docker compose up -d
```

Do not publish the application port directly. The complete
[self-hosting runbook](docs/SELF-HOSTING.md) covers DNS, permissions, signed
release verification, upgrades, verified backups, restore, and rollback.

The first account you create becomes the owner of its own community — no invite
needed. Every account after the first needs one, so registration is closed by
default rather than open by default.

To invite someone: pick your community, press **⌁** in the channel sidebar
header, and create a 24-hour code. Codes are shown once, at creation, because
only their digest is stored. Revoking a code invalidates every copy of it
immediately.

Data lands in `data/campfire.db` and `data/uploads/`. Snapshot and verify both
together with `python3 -m campfire backup BACKUP_DIR`; copying one path alone is
not a consistent backup.

## Using it

### Communities and channels

A community holds channels and members. The rail on the left switches between
communities; **+** creates one and **↪** joins one with an invite code.
Administrators can add channels with **+** in the sidebar header.

Every member of a community can read all of its channels. What a channel can
restrict is *contributing* to it.

### Channel rules

Administrators get a **⚙** beside the channel name. Each channel can set:

- **Who can post** — every member, moderators and above, or administrators only.
  Useful for an announcements channel that everyone reads and few write to.
- **Slow mode** — up to five minutes between messages from the same person.
  Moderators and above are exempt, because they are who answers a flood.
- **Image uploads** — on or off per channel.

The composer disables itself and says which rule applies, rather than letting
someone type a message that will be refused.

### Roles and moderation

Four roles form a ladder: **owner → administrator → moderator → member**. The
owner is derived from the community itself and cannot be reassigned by editing a
role. Administrators manage channels and invites; moderators remove messages and
members. Anyone can only act on roles below their own — never on a peer, never
on themselves.

Open the **People** panel from the chat header. The **⋮** on a member row offers
kick and ban; a role dropdown appears for owners. Banning also revokes any
invites that member created for the community, so codes they had already handed
out stop working. **⊘** in the sidebar header lists and lifts bans.

### Messages and images

Messages are editable by their author and removable by the author or any
moderator — elevated roles can remove someone's words but never rewrite them.
The **⋮** on a message row holds those actions; it appears on hover with a mouse
and stays visible on touch.

**+** in the composer shares a PNG, JPEG, GIF or WebP up to 8 MiB. Every upload
is rebuilt from the parts a decoder needs, which strips EXIF, GPS, timestamps
and colour profiles — a photo from a phone does not carry where it was taken
into the channel. Deleting the message deletes the stored file with it.

### Unread markers and notifications

Each channel tracks the last message you saw and draws a "new messages" divider
where you left off. Read markers are private: nobody, including the owner, can
see whether you have read something.

**🔔** in the sidebar header sets notifications account-wide or per channel.
Desktop notifications name the channel and the author, never the message,
because they can appear on a lock screen. Muting is applied in your browser, not
on the server, so muting never tells the server what you are willing to be told.

### Your account and your data

**⚙** in the sidebar footer opens account security: change your password, see
every active session with when it began, and sign any of them out remotely — the
revoked browser's live connection closes within a couple of seconds.

The same screen downloads everything Campfire holds about you as one JSON file,
and deletes your account. Deletion erases your messages and images everywhere,
including communities that removed you. A community you owned passes to its most
senior remaining member; one with nobody else in it is deleted with it.

### Retention and storage

Administrators click the community name to set how long it keeps messages and
how long it keeps images. Both default to forever, so nothing is ever pruned
until someone chooses. Images can be given the shorter window — they are most of
the disk a small instance uses.

The same screen reports what images occupy, what the ceiling is, and what is
actually on disk. Setting `CAMPFIRE_MAX_STORAGE_BYTES` refuses uploads past the
ceiling before their bytes cross the network.

### On a phone

The community rail and channel list fold into a drawer behind **☰** in the
header. Nothing is dropped at a small size — every control is still reachable.

## Configuration

All settings are environment variables. Defaults suit a single machine on a
trusted network.

| Variable | Default | What it does |
| --- | --- | --- |
| `CAMPFIRE_HOST` | `127.0.0.1` | Interface to bind |
| `CAMPFIRE_PORT` | `8000` | Port to listen on |
| `CAMPFIRE_DB` | `data/campfire.db` | SQLite database path |
| `CAMPFIRE_UPLOAD_DIR` | `data/uploads` | Where image bytes are stored |
| `CAMPFIRE_MAX_UPLOAD_BYTES` | `8388608` | Per-image size limit |
| `CAMPFIRE_MAX_STORAGE_BYTES` | `0` | Total image ceiling; `0` means none |
| `CAMPFIRE_STORAGE_WARNING_PERCENT` | `90` | Warn when image or filesystem capacity reaches this percentage |
| `CAMPFIRE_RETENTION_SWEEP_SECONDS` | `3600` | How often retention runs |
| `CAMPFIRE_SECURE_COOKIES` | `0` | Set to `1` when serving over HTTPS |
| `CAMPFIRE_ORIGIN` | unset | Public origin, no trailing slash |
| `CAMPFIRE_TRUSTED_PROXIES` | unset | Proxies whose forwarded addresses to believe |
| `CAMPFIRE_ACCESS_LOG` | `0` | Request logging; leave off unless diagnosing |
| `CAMPFIRE_MAX_EVENT_STREAMS` | `200` | Concurrent live connections allowed |
| `CAMPFIRE_MAX_EVENT_STREAMS_PER_USER` | `8` | Live connections per account |

```bash
CAMPFIRE_PORT=9000 CAMPFIRE_DB=/srv/campfire.db python3 -m campfire serve
```

`CAMPFIRE_ORIGIN` and `CAMPFIRE_TRUSTED_PROXIES` are security-sensitive. The
[self-hosting guide](docs/SELF-HOSTING.md) explains what each one changes. A
non-loopback listener fails before migration or binding unless HTTPS origin,
secure cookies, trusted proxy, and a storage ceiling are all configured.

The operator CLI also provides `check-config`, `migrate`, `backup`,
`verify-backup`, and offline `restore --confirm` commands.

## Test it

```bash
python3 -m unittest discover -s tests -v
```

## Documentation

- [ROADMAP.md](ROADMAP.md) — milestones and the acceptance criteria for each
- [docs/SECURITY.md](docs/SECURITY.md) — implemented controls, known gaps, threat model
- [docs/PRIVACY.md](docs/PRIVACY.md) — every piece of data stored and why
- [docs/SELF-HOSTING.md](docs/SELF-HOSTING.md) — deployment, backups, configuration
- [docs/API.md](docs/API.md) — the HTTP contract
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — module boundaries and where things live
- [docs/RELEASES.md](docs/RELEASES.md) — signed releases and automated scanning
- [SECURITY.md](SECURITY.md) — private vulnerability reporting and release verification

Read the security and privacy documents before inviting anyone. Campfire is an
original implementation and uses no Discord branding or assets.
