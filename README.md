# Campfire

Campfire is a small, open-source Discord-style chat MVP. It is intentionally
dependency-free: Python serves the API and static client, SQLite stores data,
and the browser receives live messages over Server-Sent Events.

## Included in the MVP

- Account registration, login, and cookie-based sessions
- Invite-only registration after the first account
- Administrator-controlled active invite management and immediate revocation
- Communities and text channels
- Authorized member lists with roles and online presence
- Owner-assigned administrators and moderators; administrators manage channels
  and invites, while moderators can remove messages and lower-ranked members
- Role-aware kick, persistent ban, ban listing, and unban controls
- Persistent messages, editable by their author and removable by the author or
  a community moderator
- Authorized PNG, JPEG, GIF, and WebP sharing (8 MiB default limit), stored
  with EXIF and other metadata removed
- Live message delivery with automatic reconnect
- Per-channel unread markers, a “new messages” divider, and account-wide or
  per-channel notification preferences
- Rate-limited authentication and restrictive browser security headers
- Responsive three-panel chat UI

## Run it

```bash
python3 server.py
```

Open <http://localhost:8000>. The first account becomes the owner of its first
community. Use the link-shaped button beside the community name to generate an
invite code for a friend. New accounts after the first require one.

Data is written to `data/campfire.db`. Override defaults with:

```bash
CAMPFIRE_HOST=127.0.0.1 CAMPFIRE_PORT=9000 CAMPFIRE_DB=/path/to/db python3 server.py
```

## Test it

```bash
python3 -m unittest discover -s tests -v
```

## Near-term roadmap

1. Channel permissions, slow mode, and upload controls
2. Account/session controls and per-community retention settings
3. PostgreSQL, Redis, and a WebSocket gateway for multi-instance deployment
4. Voice rooms through a dedicated WebRTC SFU

The maintained milestone plan and acceptance criteria live in
[ROADMAP.md](ROADMAP.md).

Campfire is an original implementation and does not use Discord branding or
assets.

## Operating it responsibly

Do not expose the development HTTP server directly to the internet. Read the
[security model](docs/SECURITY.md), [privacy behavior](docs/PRIVACY.md), and
[self-hosting guide](docs/SELF-HOSTING.md) before inviting anyone. The current
HTTP contract is recorded in the [API guide](docs/API.md), and module ownership
is explained in the [architecture guide](docs/ARCHITECTURE.md).
