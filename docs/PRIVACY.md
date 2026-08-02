# Privacy and data inventory

Campfire contains no analytics SDK, advertisements, tracking pixels, remote
fonts, CDN scripts, telemetry endpoint, or third-party identity provider.

## Persisted on the host

| Data | Purpose | Default lifetime |
| --- | --- | --- |
| Username and password hash | Authentication | Until manually deleted |
| Session-token digest and user link | Staying signed in | 30 days; expired rows are removed at startup |
| Communities and memberships | Authorization | Until manually deleted |
| Channel messages | Conversation history | Indefinite |
| Shared images and metadata | User-requested image sharing | Indefinite |
| Invite digest and usage | Private onboarding | Up to 7 days; expired rows are removed at startup |

Raw passwords and raw invite codes are never stored. Campfire does not persist
IP addresses. Authentication rate-limit state exists only in memory for five
minutes.

Members of the same community can see one another's username, internal user ID,
and whether a person owns that community. The member-list feature adds no new
persistent data beyond existing accounts and memberships.

Only a community owner can list its active invite metadata. Campfire cannot
display a previously created raw code because only its digest is retained;
revocation deletes that digest and its usage metadata immediately.

Images are stored under `data/uploads` by default using random server-generated
names. The original filename, detected media type, byte size, uploader, and
channel are stored in SQLite. Image metadata inside the uploaded bytes (for
example EXIF location data) is **not stripped yet**; users should remove
sensitive metadata before sharing.

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
