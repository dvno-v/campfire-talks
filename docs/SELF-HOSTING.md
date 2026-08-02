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
python3 server.py
```

The proxy must preserve long-running Server-Sent Event responses on
`/api/events`, enforce HTTPS, set sensible request-size/time limits, and avoid
access logs if metadata minimization is required. Back up the SQLite database
to encrypted storage with access restricted to the service account.

`CAMPFIRE_ORIGIN` is security-sensitive: it must exactly match the browser's
public origin, without a trailing slash. Enabling secure cookies while browsing
over plain HTTP will prevent sign-in from working.

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
