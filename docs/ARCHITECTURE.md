# Architecture

Campfire is a single-process, single-host application by design. “Single
process” does not mean “single file”: security-sensitive responsibilities have
separate modules and explicit dependency directions.

```text
server.py                 compatibility process entry point
    │
    └── campfire/__main__.py  serve/migrate/backup/restore operator commands
          ├── operations.py  consistent snapshot verification and restore
          ├── migrations/   immutable ordered schema versions
          ├── config.py      parsing, validation, and filesystem locations
          ├── database.py    connections, migration runner, serializers
          └── http.py        routing, authorization calls, response policy
                ├── security.py  password/session/invite primitives and limits
                ├── uploads.py   hostile image-name and signature validation
                ├── realtime.py  in-process event fan-out and presence
                └── services/    authorized domain queries and workflows

static/                   dependency-free browser client
tests/                    owning-module unit tests
docs/                     security, privacy, API, operations, and roadmap
```

## Boundary rules

- `server.py` contains no application behavior; importing it has no side
  effects beyond importing the application.
- Configuration is read in one module. Other modules do not independently
  reinterpret environment variables.
- Database connections, migration transactions, and row serialization live in
  `database.py`; immutable version steps live under `migrations/`.
- `operations.py` owns backup manifests, hashes, SQLite snapshots, and offline
  restore. HTTP handlers do not reinterpret those filesystem workflows.
- Password and token algorithms live in `security.py`, not request handlers.
- Raw upload validation is isolated because it handles hostile bytes and names.
- The HTTP layer remains responsible for authenticating requests and applying
  resource-level authorization before calling persistence operations.
- The browser client never receives or stores the session token directly.
- Each stylesheet in `static/` owns one panel; `shell.css` owns the responsive
  shell and is linked last, so it is the only file permitted to override the
  others' layout. Panel stylesheets do not carry their own breakpoints.
- `menu.css` is the exception to one-panel ownership: the row actions menu is
  shared by messages and members. Both render one trigger per row and fill a
  single popup element as it opens, rather than each keeping its own hidden
  menu in the DOM. Row actions stay out of sight until a row is hovered, and
  are always present on touch, where there is no hover to reveal them with.

## Responsive shell

The desktop layout is four columns: community rail, channel list, conversation,
member list. They fold in the order they can be spared.

| Width | Rail | Channels | Members |
| --- | --- | --- | --- |
| Above 1100px | column | column | column |
| 761–1100px | column | column | slide-over |
| 760px and below | drawer | drawer | slide-over |

The rail and the channel list slide over together as one drawer, because a
phone that could switch channels but not communities — or that could do neither
— would put invites, bans, notification settings, account security, and
sign-out permanently out of reach. Nothing in the shell is hidden with
`display: none` at a breakpoint; every control has a way in at every width.

## Current request path

```text
browser request
  → same-origin/security-header policy
  → session lookup
  → community/channel authorization
  → SQLite operation or private file read
  → JSON/SSE/image response
```

`/healthz` stops at the HTTP layer. `/readyz` additionally performs a bounded
schema read and filesystem-permission checks, returning only named states and
capacity-warning codes. These probes are separate because a dependency failure
should remove an instance from traffic without telling a supervisor to restart
a live process indefinitely.

## Why this is not microservices

For a handful of friends, splitting chat, identity, storage, and presence into
network services would add secrets, failure modes, logs, and deployment burden
without improving privacy. Module boundaries give us testability now and allow
later extraction only where operations require it. Voice/screen media is the
exception because WebRTC routing is a specialized workload and will run in a
separate self-hosted SFU.

## Domain services

`campfire/http.py` intentionally remains the composition point. Community
membership and roles, message ownership, per-account reading state, and account
security are extracted domain services. `services/accounts.py` owns password
replacement, session listing/revocation, and the export and deletion of a whole
account; `services/communities.py` resolves
the privilege ladder and owns membership removal and ban workflows,
`services/channels.py` owns per-channel posting rules,
`services/retention.py` owns scheduled deletion of expired history,
`services/storage.py` owns disk accounting and the upload ceiling,
`services/messages.py` decides who may edit or delete, and
`services/notifications.py` decides what each account has read and wants told.
The HTTP layer keeps the decision about which status code reveals what. Those
service functions accept an explicit database connection and actor identity,
making authorization testable without opening a socket.

Before supporting multiple Campfire processes, the operation lock, in-memory event broker and
rate limiter must be replaced with shared infrastructure. That complexity is
not justified for the current single-host target.

Notification delivery stays in the browser for the same reason. The server
publishes every event a member is entitled to and `services/notifications.py`
stores only what each account chose; deciding what to announce server-side
would mean the process tracking who is willing to be interrupted, which is
state Campfire would then have to protect, back up, and explain.

## The concurrency ceiling

A live stream holds a thread and a database connection until it closes, so
concurrent users are bounded by threads rather than by anything Campfire
chooses. The limits in `config.py` make that boundary explicit and refuse work
past it instead of degrading, but they do not raise it. Lifting the ceiling
means an event loop and a WebSocket gateway: a future scale project and a
rewrite of this module, not a tuning exercise for the single-host target.
