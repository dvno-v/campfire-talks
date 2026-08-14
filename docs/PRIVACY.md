# Privacy and data inventory

Campfire contains no analytics SDK, advertisements, tracking pixels, remote
fonts, CDN scripts, telemetry endpoint, or third-party identity provider.

## Persisted on the host

| Data | Purpose | Default lifetime |
| --- | --- | --- |
| Username and password hash | Authentication | Until manually deleted |
| Session-token digest, user link, creation time, and expiry | Staying signed in and showing active sessions | 30 days; revoked rows are deleted immediately and expired rows at startup |
| Passkey credential ID, public key, sign count, chosen name, and timestamps | WebAuthn sign-in and credential management | Until the passkey or account is deleted |
| WebAuthn challenge, random ceremony-token digest, purpose, and expiry | Binding one browser ceremony to one request | 5 minutes at most; deleted when consumed or expired |
| Communities, memberships, and roles | Authorization | Until manually deleted |
| Community bans (account, prior role, moderator, time) | Preventing invite re-entry | Until unbanned or the account/community is deleted |
| Channel messages | Conversation history | Indefinite by default, or the community's message retention window |
| Shared images and metadata | User-requested image sharing | Indefinite by default, or the community's image retention window |
| Community retention windows (two day counts) | Scheduled deletion of old history | Until the community is deleted |
| Invite digest and usage | Private onboarding | Up to 7 days; expired rows are removed at startup |
| Channel posting rules (minimum role, slow-mode seconds, uploads allowed) | Community moderation | Until the channel is deleted |
| Channel kind (`text` or `voice`) | Selecting the authorized feature path | Until the channel is deleted |
| Voice lease digest, account/channel, room-key fingerprint, creation and expiry | Atomic participant/key limit | Deleted on leave or after 45 seconds |
| Read markers (one message id per account and channel) | Unread markers | Until the account or channel is deleted |
| Notification modes (account default and per-channel) | User-chosen notification preferences | Until the account or channel is deleted |
| Migration version, name, and application time | Safe reproducible upgrades | Lifetime of the database |
| Random upload reservation token, declared byte count, and expiry | Atomic enforcement of the instance storage ceiling | Settled with the upload, or 15 minutes after interruption |
| Empty process-lock files | Preventing concurrent servers and live restore | Lifetime of the database path |

Raw passwords, raw session tokens, raw passkey ceremony tokens, authenticator
private keys/biometrics, raw invite codes, media room keys, and media frames are
never stored by Campfire.
Campfire does not persist IP addresses, user agents, device names, locations,
or session activity. Authentication rate-limit state exists only in memory for
five minutes.

The account-security screen lists only active sessions and shows their creation
and expiry times. Revoking one deletes its row immediately and closes any live
event stream using it within about two seconds. A password change overwrites
the prior password hash, deletes every session except the one confirming the
change, and issues that surviving session a new token, so the cookie in play
beforehand stops working too. Neither revoked-session history nor prior
password hashes are retained; older backups can contain the replaced rows until
those backups are rotated. For sessions created before that field existed, the
first upgraded release uses its startup time as the creation time because no
earlier timestamp was stored. The release that gave sessions a stable
identifier rebuilds the table in place and carries the tokens across, so
upgrading does not sign anyone out.

A passkey's public credential lets the server verify signatures; the private
key and any fingerprint/face/PIN check remain with the authenticator. Campfire
stores the chosen label and last successful use so the account can recognize
and remove credentials. Registration/removal requires the password, which
remains the recovery method. A consumed challenge row is deleted whether the
verification succeeds or fails, preventing replay.

An account can download everything Campfire stores about it as a single JSON
file: the account row without its password hash, when each session began and
expires without its digest, memberships and roles, every message it wrote,
image metadata, passkey names and timestamps without credential material, read markers, active voice-lease metadata without its token digest, notification preferences, invites it created, and
bans it received. The file is plain text and depends on nothing Campfire hosts,
so leaving does not mean losing your own history. It contains no one else's
messages and nothing the account could not already see in the interface.

Deleting an account erases the account row, its sessions, passkeys and active
passkey challenges, memberships, read
markers, notification preferences, and the invites it created, along with every
message it wrote and every image it uploaded — in all communities, including
any that had removed it. Stored image files are unlinked from disk, not merely
dereferenced. This is deliberately stronger than a kick: a kick is a moderator
acting on somebody, so their words stay part of everyone else's conversation,
while deletion is the account itself withdrawing and taking what it published
with it. A community the account owned passes to its most privileged remaining
member; a community with no other member is deleted with its channels,
messages, and images. Bans the account issued as a moderator survive, with the
moderator link cleared to NULL, because the banned account's protection should
not depend on who is still around. Backups taken before a deletion still hold
the content until they are rotated. Deletion cannot be undone, and if the last
account on an instance deletes itself, the next registration becomes the first
account again and needs no invite.

Members of the same community can see one another's username, internal user ID,
and community role. Non-owner roles are stored on the membership; the owner
role is derived from the community's existing owner link. A role change
overwrites the previous value and remains in SQLite and its backups until that
membership is deleted; no role-change history or timestamp is kept.

A kick deletes the membership, that account's read markers and notification
overrides for the community, and the digests of any invites it created for that
community — codes it had already handed out stop working. It does not delete
messages or attachments the account previously shared, because those remain
part of the other members' conversation history. A ban performs the same deletion and stores the banned
account, its role at that moment, the moderator account, and a timestamp. No
reason, note, IP address, device identifier, or ban expiry is stored. Unbanning
deletes the ban row immediately; older backups retain it until rotated.

Presence is derived from open event streams and held only in memory. Campfire
records no last-seen time, retained session history, or connection log; an
active session's creation time says when it was issued, not when it was last
used. Restarting the server erases presence entirely. Whether you are currently
connected is visible to people who already share a community with you, and to
nobody else. There is no way to appear offline yet; if that matters to you,
close the tab.

A voice channel stores its `kind` on the channel. While joining, Campfire stores
one expiring lease per account and channel: a digest of a random lease token,
the SHA-256 fingerprint of the room key, creation time, and expiry. The raw
lease and room key are not stored. Leaving removes the lease; abandoned leases
expire after 45 seconds and are pruned on startup. A backup can contain an
unexpired digest/fingerprint, but never media or the key, and restore startup
removes leases already past expiry.

LiveKit processes encrypted media and ephemeral room state. The Compose stack
has no recording, transcription, egress, ingress, webhook, or metrics-export
service and no media volume. The SFU and network operator can still observe IP
addresses, account/room identifiers, connection time, packet size/rate,
speaking activity, and connection quality. Warning/error Docker logs are small
and rotating but may contain exceptional operational metadata. Media E2EE does
not protect text chat or protect users from modified client code served by a
compromised operator. [MEDIA.md](MEDIA.md) documents the exact boundary.

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

Storage reporting counts bytes and files, never who uploaded them. The
per-community breakdown an administrator sees says how much a community holds,
not which member contributed it, because the ceiling is about the disk rather
than about anybody's behaviour.

Health and readiness probes store nothing and require no account. They report
only whether named dependencies are usable and stable capacity-warning codes;
they do not expose local paths, exception messages, or filesystem sizes. A
reverse proxy can still turn probe requests into timestamp metadata if its own
access logging is enabled, so operators should suppress those logs.

An operator-created backup is a new complete copy of the database and every
referenced image. Its manifest adds the Campfire/schema versions, creation time,
random storage filenames, byte sizes, and SHA-256 content hashes so corruption
or a mismatched file set can be detected. It adds no member-facing history, but
it retains deleted or changed data until the operator's backup rotation removes
that snapshot. The supplied Compose workflow writes the completed snapshot as
one authenticated AES-256-GCM file. Its separate raw key is never mounted into
the web container; whoever holds that key can decrypt all retained content, and
losing it makes the snapshot unrecoverable.

A community may set how long it keeps messages and how long it keeps shared
images, in whole days. Both default to keeping everything, so an instance that
never chooses is never quietly pruned. Images can be given the shorter window:
they are most of the disk a small instance uses. A sweep deletes the expired
messages and unlinks the stored image files, so the bytes stop being retrievable
rather than merely becoming unreferenced; removing a shared image removes the
message carrying it, because an upload is stored as a message with an empty
body. The sweep runs hourly and again the moment a window is set, and it records
nothing about what it removed beyond telling the affected channels to re-read.
Backups taken before a sweep still hold the content until they are rotated.

A channel's posting rules are stored on the channel, not per account: Campfire
records that a channel is in slow mode, never who was slowed by it or when. The
wait is derived from the timestamp already on the author's last message, so
enforcing it adds no new data about anybody.

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
