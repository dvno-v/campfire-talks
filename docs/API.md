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

Returns the signed-in user and every community/channel they may access. Each
community carries the caller's effective `role`; each channel also carries the
caller's own reading state:

| Field | Meaning |
| --- | --- |
| `unread` | messages newer than the read marker, excluding the caller's own |
| `last_read_message_id` | highest message id the caller has marked read, or `0` |
| `notify` | `all`, `none`, or `null` when the account default applies |

Alongside the communities, `notifications.default_mode` gives the account-wide
default (`all` unless it has been changed).

## Communities and channels

### `GET /api/communities/{community_id}/members`

Returns public member objects containing `id`, `username`, `online`, and an
`owner`, `administrator`, `moderator`, or `member` role. Privileged roles are
sorted first. Callers who are not current members receive `404`, which avoids
confirming whether a private community exists.

`online` is true while that account holds at least one open `/api/events`
stream. It is computed in memory at request time and never stored, so it resets
to false for everyone when the process restarts.

### `PATCH /api/communities/{community_id}/members/{user_id}`

```json
{"role": "moderator"}
```

The community owner may assign `administrator`, `moderator`, or `member` to a
current non-owner member. Members cannot promote themselves. The owner role is
derived from community ownership and cannot be assigned or removed through
this endpoint. Outsiders receive `404`; non-owner members receive `403`.

### `DELETE /api/communities/{community_id}/members/{user_id}`

Kicks a current member. Moderators may kick members, administrators may also
kick moderators, and owners may kick any non-owner. Actors cannot kick
themselves, peers, or higher roles. A kick removes the membership and that
account's community-specific read markers and notification overrides. Existing
messages and attachments remain in community history. The account may rejoin
with a valid invite.

### `POST /api/communities/{community_id}/members/{user_id}/ban`

Accepts an empty JSON object. It applies the same hierarchy as kicking, removes
the membership, and stores a persistent account ban. A banned account receives
`403` from invite joining before the invite's use count changes.

### `GET /api/communities/{community_id}/bans`

Moderators and above receive the banned account's ID and username, its role at
the time of the ban, the banning moderator when that account still exists, and
the creation time. No free-form reason or moderation notes are stored.

### `DELETE /api/communities/{community_id}/bans/{user_id}`

Unbans an account when the caller's current role is higher than the account's
role at the time it was banned. The account is not automatically re-added; it
may use an invite again.

### `POST /api/communities`

```json
{"name": "Friday crew"}
```

Creates an owned community with a `general` channel.

### `POST /api/channels`

```json
{"community_id": 1, "name": "memes"}
```

Community owners and administrators may create a channel. Members of that
community are told over `/api/events`, so the channel appears without a reload.

## Invitations

### `POST /api/invites`

```json
{"community_id": 1, "max_uses": 10, "lifetime_hours": 24}
```

Only owners and administrators may create one. Usage is clamped to 1–25 and
lifetime to 1–168 hours. The response includes the raw code exactly once; share
it through a trusted side channel. Campfire stores only its digest. Avoid
putting invite codes in URLs, screenshots, or long-lived chat histories.

### `GET /api/communities/{community_id}/invites`

Returns active invite metadata to community owners and administrators: internal
invite ID, creator, creation and expiry times, use count, and maximum uses. Raw
invite codes are never returned because Campfire stores only their digests.
Expired and fully used invitations are omitted. Other members receive `403`.

### `DELETE /api/invites/{invite_id}`

Immediately deletes an invitation when requested by an owner or administrator
of its community. Every outstanding copy of the code becomes invalid on the
next use. Missing or unauthorized invites both return `404`.

### `POST /api/invites/join`

```json
{"invite": "the invite code"}
```

Adds an existing account to the invitation’s community. Existing members are
told over `/api/events`, so the arrival appears in their member list without a
reload. Registering with an invite announces the same event. A persistent ban
is checked before the invitation is consumed.

## Messages

### `GET /api/channels/{channel_id}/messages`

Returns the latest 100 messages in ascending order. `?after={message_id}` may be
used to request only newer messages. Edited messages carry an `edited_at`
timestamp; unedited ones carry `null`.

### `POST /api/channels/{channel_id}/messages`

```json
{"body": "hello friends"}
```

Message bodies contain 1–4000 characters. Both reads and writes require current
community membership.

### `PATCH /api/messages/{message_id}`

```json
{"body": "the corrected text"}
```

Only the message's author may edit it; community ownership does not confer the
right to rewrite another person's words, so owners receive `403`. The response
is the updated message with `edited_at` set. Messages carrying an image return
`409` because their content is the file, which can only be deleted.

### `DELETE /api/messages/{message_id}`

Deletes a message when requested by its author or by a moderator,
administrator, or owner of its community. Any attachment it carries is removed
from the database and from disk in the same operation. Members without either
right receive `403`; users outside the community receive `404` rather than
learning that the message exists.

### `POST /api/channels/{channel_id}/uploads`

The request body is the raw image bytes. Required headers are `Content-Length`,
`Content-Type`, and `X-Campfire-Filename` (the original filename encoded with
`encodeURIComponent`). The default maximum is 8 MiB. PNG, JPEG, GIF, and WebP
are accepted only when filename extension, declared MIME type, and file
signature agree.

The stored image is rebuilt without EXIF, XMP, comments, timestamps, appended
trailers or colour profiles, so `byte_size` describes the rewritten file rather
than the number of bytes uploaded. An image that cannot be parsed exactly is
rejected with `415` instead of being stored unstripped. A successful upload
creates and broadcasts a message with an `attachment` object.

### `GET /api/attachments/{attachment_id}`

Returns the image with its detected MIME type only when the current user is
still a member of its channel’s community. Responses use `no-store` and
`nosniff`; uploaded files never become public static paths.

### `GET /api/events`

Opens a Server-Sent Events stream. Each `data` event carries a `type` and the
resource identifier described below:

| `type` | Payload |
| --- | --- |
| `message.created` | the new message object |
| `message.updated` | the edited message object, including `edited_at` |
| `message.deleted` | `id` and `channel_id` only |
| `presence.online` | `user_id`, sent when that account opens its first stream |
| `presence.offline` | `user_id`, sent when its last stream closes |
| `member.joined` | `community_id` and the new `member` object |
| `member.updated` | `community_id` and the member's new public role object |
| `member.removed` | `community_id`, `user_id`, and whether this was a ban |
| `channel.created` | `community_id`, `id`, and `name` |
| `stream.reset` | nothing; the client must re-read the channel it is viewing |

Each stream buffers 50 events. A client that falls further behind than that
receives `stream.reset` instead of losing events quietly, because a transcript
that is wrong without saying so is worse than one that reloads. Clients should
also re-read after any reconnect: `EventSource` reconnects on its own, and
anything published during the gap was never delivered. Re-reading rather than
fetching `?after=` is deliberate — a gap can hide edits and deletions too.

Authorization is re-checked per event, because membership can change while a
stream is open:

- message events reach members of the channel's community;
- `member.*` and `channel.*` events reach members of `community_id`; a removed
  account also receives its own `member.removed` event so it can discard the
  community immediately;
- presence events reach only accounts that already share a community with the
  subject, so being connected is never observable by strangers.

The server emits a keepalive every 20 seconds and separately polls for a closed
peer every two seconds, so a departure is announced in about two seconds rather
than waiting for a failed keepalive write.

## Unread markers and notification preferences

A *read marker* is the highest message id an account has seen in a channel.
Only that id is stored — never a time of reading — and unread totals ignore the
caller's own messages.

A *notification mode* decides whether new messages are announced. `all`
announces, `none` mutes. One mode applies to the whole account; a per-channel
mode overrides it for a single channel. Muting affects only what is announced:
a muted channel still counts its unread messages, so a client can show that it
moved without interrupting anyone.

### `GET /api/unread`

Returns every readable channel's `channel_id`, `unread`, `last_read_message_id`
and `notify`, plus the account's `default_mode`. Clients re-read this after a
reconnect or a `stream.reset`, for the same reason they re-read the channel:
anything published during a gap never arrived, and a badge that is wrong
without saying so is worse than one that reloads.

### `POST /api/channels/{channel_id}/read`

```json
{"message_id": 42}
```

Advances the caller's read marker and returns the channel's updated state. The
marker only ever moves forward, so a second tab or a stale client cannot make
read messages unread again, and it is clamped to the newest message that
actually exists, so a client cannot claim to have read into the future and
silence messages it has never seen. Non-members receive `403`.

Repeated calls converge on the same marker, so clients may send one whenever a
channel is opened or the tab regains focus.

### `PATCH /api/channels/{channel_id}/notifications`

```json
{"mode": "all"}
```

`all` and `none` set a per-channel override; `default` removes it so the
account default applies again. Any other value is `400`; a channel the caller
cannot reach is `403`. The response is the channel's updated state.

### `PATCH /api/preferences/notifications`

```json
{"default_mode": "none"}
```

Sets the account-wide default for channels with no override of their own.

## Browser request protection

State-changing requests with `Sec-Fetch-Site: cross-site` or an `Origin` that
does not match `CAMPFIRE_ORIGIN` are rejected. Non-browser API clients that omit
those browser headers are still responsible for protecting their session
cookie and using trusted HTTPS.
