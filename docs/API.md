# HTTP API

The browser client and API share one origin. JSON requests must send
`Content-Type: application/json`. Authentication uses the HttpOnly
`campfire_session` cookie; JavaScript cannot read it.

All errors have the form:

```json
{"error": "Human-readable explanation"}
```

## Authentication

### `POST /api/register`

```json
{
  "username": "sam",
  "password": "a long unique passphrase",
  "invite": "code supplied by a community owner"
}
```

The first account on an empty instance must omit `invite` and becomes the owner
of a new community. Every later account needs a valid invite. Usernames contain
2–24 ASCII letters, numbers, or underscores; passwords contain 12–1024
characters.

### `POST /api/login`

```json
{"username": "sam", "password": "a long unique passphrase"}
```

### `POST /api/logout`

Deletes the current server-side session and expires the browser cookie.

### `GET /api/me`

Returns `{"user": null}` when signed out or the current public user object.

## Initial application state

### `GET /api/bootstrap`

Returns the signed-in user and every community/channel they may access.

## Communities and channels

### `GET /api/communities/{community_id}/members`

Returns public member objects containing `id`, `username`, and either the
`owner` or `member` role. The owner is sorted first. Callers who are not current
members receive `404`, which avoids confirming whether a private community
exists.

### `POST /api/communities`

```json
{"name": "Friday crew"}
```

Creates an owned community with a `general` channel.

### `POST /api/channels`

```json
{"community_id": 1, "name": "memes"}
```

Only the community owner may create a channel.

## Invitations

### `POST /api/invites`

```json
{"community_id": 1, "max_uses": 10, "lifetime_hours": 24}
```

Only the owner may create one. Usage is clamped to 1–25 and lifetime to 1–168
hours. The response includes the raw code exactly once; share it through a
trusted side channel. Campfire stores only its digest. Avoid putting invite
codes in URLs, screenshots, or long-lived chat histories.

### `GET /api/communities/{community_id}/invites`

Returns active invite metadata to the community owner: internal invite ID,
creator, creation and expiry times, use count, and maximum uses. Raw invite
codes are never returned because Campfire stores only their digests. Expired and
fully used invitations are omitted. Other members receive `403`.

### `DELETE /api/invites/{invite_id}`

Immediately deletes an invitation when requested by its community owner. Every
outstanding copy of the code becomes invalid on the next use. Missing invites
and invites owned by another user both return `404`.

### `POST /api/invites/join`

```json
{"invite": "the invite code"}
```

Adds an existing account to the invitation’s community.

## Messages

### `GET /api/channels/{channel_id}/messages`

Returns the latest 100 messages in ascending order. `?after={message_id}` may be
used to request only newer messages.

### `POST /api/channels/{channel_id}/messages`

```json
{"body": "hello friends"}
```

Message bodies contain 1–4000 characters. Both reads and writes require current
community membership.

### `POST /api/channels/{channel_id}/uploads`

The request body is the raw image bytes. Required headers are `Content-Length`,
`Content-Type`, and `X-Campfire-Filename` (the original filename encoded with
`encodeURIComponent`). The default maximum is 8 MiB. PNG, JPEG, GIF, and WebP
are accepted only when filename extension, declared MIME type, and file
signature agree. A successful upload creates and broadcasts a message with an
`attachment` object.

### `GET /api/attachments/{attachment_id}`

Returns the image with its detected MIME type only when the current user is
still a member of its channel’s community. Responses use `no-store` and
`nosniff`; uploaded files never become public static paths.

### `GET /api/events`

Opens a Server-Sent Events stream. Each `data` event contains a message object.
The server checks the connected user’s channel membership again before sending
each event and emits a keepalive every 20 seconds.

## Browser request protection

State-changing requests with `Sec-Fetch-Site: cross-site` or an `Origin` that
does not match `CAMPFIRE_ORIGIN` are rejected. Non-browser API clients that omit
those browser headers are still responsible for protecting their session
cookie and using trusted HTTPS.
