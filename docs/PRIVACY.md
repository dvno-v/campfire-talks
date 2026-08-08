# Privacy and data inventory

Campfire contains no analytics SDK, advertisements, tracking pixels, remote
fonts, CDN scripts, telemetry endpoint, or third-party identity provider.

## Persisted on the host

| Data | Purpose | Default lifetime |
| --- | --- | --- |
| Username and password hash | Authentication | Until manually deleted |
| Session-token digest and user link | Staying signed in | 30 days; expired rows are removed at startup |
| Communities, memberships, and roles | Authorization | Until manually deleted |
| Community bans (account, prior role, moderator, time) | Preventing invite re-entry | Until unbanned or the account/community is deleted |
| Channel messages | Conversation history | Indefinite, until deleted by the author or a community moderator |
| Shared images and metadata | User-requested image sharing | Indefinite, until deleted by the uploader or a community moderator |
| Invite digest and usage | Private onboarding | Up to 7 days; expired rows are removed at startup |
| Read markers (one message id per account and channel) | Unread markers | Until the account or channel is deleted |
| Notification modes (account default and per-channel) | User-chosen notification preferences | Until the account or channel is deleted |

Raw passwords and raw invite codes are never stored. Campfire does not persist
IP addresses. Authentication rate-limit state exists only in memory for five
minutes.

Members of the same community can see one another's username, internal user ID,
and community role. Non-owner roles are stored on the membership; the owner
role is derived from the community's existing owner link. A role change
overwrites the previous value and remains in SQLite and its backups until that
membership is deleted; no role-change history or timestamp is kept.

A kick deletes the membership and that account's read markers and notification
overrides for the community. It does not delete messages or attachments the
account previously shared, because those remain part of the other members'
conversation history. A ban performs the same deletion and stores the banned
account, its role at that moment, the moderator account, and a timestamp. No
reason, note, IP address, device identifier, or ban expiry is stored. Unbanning
deletes the ban row immediately; older backups retain it until rotated.

Presence is derived from open event streams and held only in memory. Campfire
records no last-seen time, no session history, and no connection log, so there
is nothing to disclose about when you were here before now — restarting the
server erases presence entirely. Whether you are currently connected is visible
to people who already share a community with you, and to nobody else. There is
no way to appear offline yet; if that matters to you, close the tab.

An unread marker stores one number per account and channel: the highest message
id that account has seen. No time of reading is recorded, so the database
cannot answer when you were last in a channel, how often you open one, or how
long you spent there — only where to draw the line under what you have already
seen. Read markers are private to their account; no one, including a community
owner, can see whether you have read a message. Nothing in Campfire reports
delivery or reading back to a sender.

Notification modes are equally private and equally small: `all` or `none`, once
for the account and optionally once per channel. Muting a channel changes only
what your own browser announces; the server sends the same events either way,
because filtering them centrally would mean the server tracking what each
person is willing to be told. Desktop notifications name the channel and the
author, never the message, since they can appear on a lock screen or a shared
desktop. The browser is asked for notification permission only when you press
the button that asks for it, never on load and never as a side effect of
signing in. Until you do, Campfire says so in the notification settings rather
than leaving you to wonder why nothing appears.

Only a community owner or administrator can list its active invite metadata.
Campfire cannot display a previously created raw code because only its digest
is retained; revocation deletes that digest and its usage metadata immediately.

Deleting a message removes its row immediately. When that message carried an
image, the attachment record and the stored file are removed with it, so the
bytes stop being retrievable rather than merely becoming unreferenced. Backups
taken before a deletion still contain the content until they are rotated. An
edited message keeps no previous version: Campfire overwrites the body and
records only that an edit happened, so history cannot be recovered from the
database.

Images are stored under `data/uploads` by default using random server-generated
names. The original filename, detected media type, byte size, uploader, and
channel are stored in SQLite.

Uploads are rebuilt from the parts a decoder needs before anything is written
to disk, so the file that is stored and served is not the file that was sent. A
photo from a phone normally carries the place and time it was taken; that is
removed along with XMP, comments, timestamps, appended thumbnails and colour
profiles. Removing profiles means images are interpreted as sRGB, which is a
deliberate cost of not keeping camera-identifying data. An image that does not
parse exactly as its format requires is refused rather than stored with
metadata Campfire did not understand well enough to remove.

This protects people in the channel from the uploader's camera. It is not a
guarantee about steganography or data hidden inside the pixels themselves.

## Logs

HTTP access logs are off by default. Setting `CAMPFIRE_ACCESS_LOG=1` enables
request logging and may expose client addresses, paths, timestamps, and user
agents to the host's process-log collector. Leave it disabled unless diagnosing
a problem, and delete diagnostic logs afterward.

The host, reverse proxy, DNS provider, certificate authority, VPS provider, and
backup system may still create their own metadata. Their configuration is
outside Campfire, so operators must audit them separately.

## Honest privacy boundary

Today, the server administrator can read stored messages and observe who is
connected. “No data collection” means Campfire performs no secondary collection
or tracking; it does not mean the service can function without processing or
storing the conversations its users ask it to retain.
