# Architecture

Campfire is a single-process, single-host application by design. “Single
process” does not mean “single file”: security-sensitive responsibilities have
separate modules and explicit dependency directions.

```text
server.py                 development/process entry point
    │
    └── campfire/http.py  HTTP routing, authorization calls, response policy
          ├── config.py   environment parsing and filesystem locations
          ├── database.py SQLite schema, migration, connections, serializers
          ├── security.py password/session/invite primitives and rate limits
          ├── uploads.py  hostile image-name and file-signature validation
          ├── realtime.py in-process live-event fan-out
          └── services/   actor-authorized domain queries and workflows

static/                   dependency-free browser client
tests/                    owning-module unit tests
docs/                     security, privacy, API, operations, and roadmap
```

## Boundary rules

- `server.py` contains no application behavior; importing it has no side
  effects beyond importing the application.
- Configuration is read in one module. Other modules do not independently
  reinterpret environment variables.
- Database initialization and row serialization live in `database.py`.
- Password and token algorithms live in `security.py`, not request handlers.
- Raw upload validation is isolated because it handles hostile bytes and names.
- The HTTP layer remains responsible for authenticating requests and applying
  resource-level authorization before calling persistence operations.
- The browser client never receives or stores the session token directly.

## Current request path

```text
browser request
  → same-origin/security-header policy
  → session lookup
  → community/channel authorization
  → SQLite operation or private file read
  → JSON/SSE/image response
```

## Why this is not microservices

For a handful of friends, splitting chat, identity, storage, and presence into
network services would add secrets, failure modes, logs, and deployment burden
without improving privacy. Module boundaries give us testability now and allow
later extraction only where operations require it. Voice/screen media is the
exception because WebRTC routing is a specialized workload and will run in a
separate self-hosted SFU.

## Next structural step

`campfire/http.py` intentionally remains the composition point. Community
membership and message ownership are the extracted domain services so far;
account workflows will follow as role management arrives. `services/messages.py`
decides who may edit or delete, while the HTTP layer keeps the decision about
which status code reveals what.
Those service functions should accept an explicit database connection and
actor identity, making authorization testable without opening a socket.

Before supporting multiple Campfire processes, the in-memory event broker and
rate limiter must be replaced with shared infrastructure. That complexity is
not justified for the current single-host target.
