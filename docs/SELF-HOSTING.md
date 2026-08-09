# Private self-hosting

## Safest early deployment

For the current MVP, run Campfire on a machine you control and restrict access
with a private network such as a home LAN or trusted VPN. Do not forward port
8000 directly from a router.

```bash
CAMPFIRE_HOST=127.0.0.1 \
CAMPFIRE_PORT=8000 \
CAMPFIRE_DB=/srv/campfire/campfire.db \
CAMPFIRE_UPLOAD_DIR=/srv/campfire/uploads \
python3 server.py
```

## HTTPS reverse proxy

Screen sharing requires a secure browser context, and login traffic must not
travel over plain HTTP. Put a maintained reverse proxy with a trusted
certificate in front of Campfire. For a public origin such as
`https://chat.example.net`, start the application with:

```bash
CAMPFIRE_HOST=127.0.0.1 \
CAMPFIRE_PORT=8000 \
CAMPFIRE_DB=/srv/campfire/campfire.db \
CAMPFIRE_UPLOAD_DIR=/srv/campfire/uploads \
CAMPFIRE_ORIGIN=https://chat.example.net \
CAMPFIRE_SECURE_COOKIES=1 \
CAMPFIRE_TRUSTED_PROXIES=127.0.0.1 \
python3 server.py
```

### Why `CAMPFIRE_TRUSTED_PROXIES` matters

Behind a proxy, every request reaches Campfire from the proxy's address. Without
this setting the sign-in rate limiter treats all of your users as one client, so
one attacker's failed attempts lock out everybody — and the limiter would still
be doing its job as written, silently, which is why it is easy to miss.

Set it to the address or CIDR range your proxy connects from (comma-separated
for several). Campfire then reads the client address from `X-Forwarded-For`,
walking it from the right and discarding hops that are themselves trusted
proxies. A client can prepend anything it likes to that header, so your proxy
must **append** rather than replace:

```nginx
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header Host $host;
proxy_buffering off;   # required for /api/events
```

Leave the variable unset when Campfire is reached directly. Trusting a
forwarding header from an untrusted peer would let any client claim any address
and evade the limiter entirely, so the default is to ignore it.

The proxy must preserve long-running Server-Sent Event responses on
`/api/events`, enforce HTTPS, set sensible request-size/time limits, and avoid
access logs if metadata minimization is required. Back up the SQLite database
to encrypted storage with access restricted to the service account.

`CAMPFIRE_ORIGIN` is security-sensitive: it must exactly match the browser's
public origin, without a trailing slash. Enabling secure cookies while browsing
over plain HTTP will prevent sign-in from working.

Each open `/api/events` stream costs one thread and one database connection for
as long as it lasts, which is the current ceiling on concurrent users.
`CAMPFIRE_MAX_EVENT_STREAMS` (default 200) and
`CAMPFIRE_MAX_EVENT_STREAMS_PER_USER` (default 8) bound that cost: past either
limit Campfire answers `503` instead of accepting work it cannot carry. Raising
them raises real memory and thread use, so measure before doing so.

`CAMPFIRE_RETENTION_SWEEP_SECONDS` (default 3600, minimum 60) sets how often
the retention sweep runs. Retention windows themselves are set per community in
the interface, are measured in days, and default to keeping everything, so the
sweep does nothing until a community chooses. Setting a window also sweeps
immediately, so the timer only governs how promptly newly aged history is
removed.

`CAMPFIRE_MAX_UPLOAD_BYTES` controls the per-image limit and defaults to
8,388,608 bytes. Back up and restore the database and upload directory as one
consistent unit: the database contains authorization and filenames, while the
directory contains the bytes. Restrict both to the service account.

## Voice and screen-sharing direction

The planned media service is a separately self-hosted WebRTC SFU. LiveKit is a
candidate because it supports self-hosting, embedded authenticated TURN, and
end-to-end encryption for media and realtime data. We will keep media keys out
of the SFU when E2EE is enabled.

- [LiveKit self-hosting deployment](https://docs.livekit.io/transport/self-hosting/deployment/)
- [LiveKit encryption overview](https://docs.livekit.io/transport/encryption/)
- [MDN screen-capture security and permissions](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getDisplayMedia#security)

WebRTC deployment needs more than an application container: expect a domain,
trusted TLS, UDP firewall rules, sufficient upstream bandwidth, and TURN/TLS
fallback for restrictive networks.
