# Campfire security model

This document describes implemented controls, known gaps, and the intended
threat model. “Self-hosted” does not automatically mean “secure.”

## Intended threat model

Campfire currently aims to protect a small, invite-only group against automated
login attacks, cross-site browser attacks, accidental public registration, and
passive interception when deployed behind HTTPS.

It does **not** currently protect message contents from the server operator or
an attacker who obtains the database. Text messages are not end-to-end
encrypted. A compromised host, browser, or administrator can read them.

## Implemented controls

- Passwords use PBKDF2-HMAC-SHA256 with a random 128-bit salt and 600,000
  iterations. Existing hashes from the first prototype are upgraded on login.
- After the initial owner account, registration requires a random, expiring
  invite. Only a SHA-256 digest of each invite is stored.
- Active invite metadata and revocation require an administrator-or-higher
  community role. Revocation deletes the stored digest, invalidating previously
  copied codes without retaining them.
- Authentication attempts are limited in memory to eight per client and
  operation scope per five-minute window. Addresses are not persisted. Behind a
  reverse proxy set `CAMPFIRE_TRUSTED_PROXIES`, or every user shares one bucket
  and a single attacker can lock out the instance; forwarding headers are
  ignored unless the peer is a proxy the operator named.
- Sessions use 256-bit random tokens, 30-day expiry, `HttpOnly`, and
  `SameSite=Strict`. Set `CAMPFIRE_SECURE_COOKIES=1` in HTTPS deployments.
  Only a SHA-256 digest of each token is stored, and only the original token is
  accepted, so a stolen database or backup yields no usable session. Accounts
  can list their own active sessions and revoke any one without learning its
  token; another account receives the same `404` as a missing session.
- Password changes require the existing password, use a separate rate-limit
  scope, replace the stored hash, and revoke every session except the caller's.
  Live streams re-check their opening session every two seconds, so remote
  revocation also terminates an already connected browser.
- Account deletion requires the current password, uses its own rate-limit
  scope, and is authorized only for the caller's own account: there is no
  endpoint by which one account can delete another. Ownership of a community is
  reassigned before the account row is removed, so a deletion cannot leave a
  community pointing at a user that no longer exists.
- The account export is an authenticated read of the caller's own data only.
  It contains no password hash, session token digest, or invite digest, so a
  leaked export cannot be replayed as a credential.
- Usernames are unique without regard to capitalization. Sign-in resolves them
  case-insensitively, so allowing `Sam` and `sam` to coexist would let one
  account answer for the other. Startup fails closed if an older database
  already contains such a pair.
- Requests with an unparsable body are answered exactly once and, where the
  declared length cannot be trusted, the connection is closed. Two responses to
  one request would let a shared proxy connection serve the second to another
  client.
- Rate-limiter keys include caller-supplied values such as attempted usernames,
  so expired entries are swept to bound memory.
- State-changing browser requests reject cross-site fetches and mismatched
  `Origin` headers.
- Responses apply a same-origin Content Security Policy, deny framing and MIME
  sniffing, disable indexing, and restrict camera, microphone, and display
  capture to the application origin.
- Every channel read, write, and live event verifies community membership.
- Presence is disclosed only across an existing shared community, is derived
  from open streams rather than stored, and records no last-seen time.
- Arrival and channel-creation events are delivered only to members of the
  community they concern, re-checked per event rather than trusted from the
  moment the stream opened.
- Community roles form an explicit privilege ladder: members can chat,
  moderators can remove messages and members, administrators can also manage
  channels and invites, and owners can additionally assign roles. A moderator
  can act only on lower roles, never themselves, a peer, or a higher role.
  Ownership is derived from the community and cannot be reassigned through the
  role endpoint. Every role and moderation action is verified server-side; the
  browser's controls are not trusted.
- Kicking deletes membership plus community-specific read and notification
  state. Banning additionally writes one unique account/community ban and
  checks it before consuming an invite. The removed account receives only its
  own removal event; subsequent community events fail the normal membership
  check.
- Editing a message requires authorship; deleting one requires authorship or a
  moderator-or-higher role. Elevated roles deliberately do not grant the right
  to rewrite another member's words, only to remove them. Non-members receive
  `404` rather than a denial that would confirm the message exists.
- Community member lists verify the requesting actor's current membership and
  return `404` to outsiders rather than revealing whether a community exists.
- Read markers and notification modes are per-account and readable only by
  their owner; setting either verifies channel membership first. A marker only
  moves forward and is clamped to the newest message that exists, so a hostile
  client cannot rewind another session's position or silence messages by
  claiming to have read into the future.
- Image uploads use a narrow format allowlist, an 8 MiB size limit, magic-byte
  validation, random non-user-controlled storage names, and private authorized
  retrieval. SVG and arbitrary documents are rejected.
- Uploads are rebuilt from the parts needed to decode them, discarding EXIF,
  XMP, comments, timestamps, appended trailers and colour profiles, so a shared
  photo does not carry where it was taken. Anything that does not parse exactly
  as its format requires is refused rather than stored unstripped.
- Live streams are bounded per host and per account, so opening connections
  cannot exhaust threads and memory; past the limit Campfire answers `503`.
- Access logging is disabled unless the operator explicitly enables it.

The password parameters follow OWASP's PBKDF2-HMAC-SHA256 guidance. Python's
standard library also recommends salted, tunably slow password derivation:

- [OWASP Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)
- [Python `hashlib` key derivation documentation](https://docs.python.org/3/library/hashlib.html#key-derivation)
- [OWASP CSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
- [OWASP HTTP Security Headers Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/HTTP_Headers_Cheat_Sheet.html)
- [OWASP File Upload Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html)

## Known gaps before public exposure

- The standard-library HTTP server has not undergone production hardening or an
  independent security audit.
- There is no multi-factor authentication, password reset, account deletion,
  or invisible-mode preference. Message deletion is
  joined by kick/ban, but channel-specific permissions, slow mode, audit logs,
  and temporary/time-limited moderation actions are not implemented yet.
- Bans identify an account, not a person, network, device, or browser. Someone
  who obtains another invite can create a differently named account; Campfire
  intentionally does not persist IP addresses or add device fingerprinting to
  prevent this.
- Messages are not end-to-end encrypted and are retained until someone deletes
  them. Deletion does not reach backups taken beforehand.
- Voice and screen sharing are not implemented yet.
- Uploads are rebuilt without metadata, but Campfire does not decode pixels, so
  this is not a defence against a malicious decoder input. Malware scanning and
  storage quotas are not implemented. Signature checks are useful defence in
  depth, not proof that an image is harmless.
- The in-memory limiter resets on restart and is not shared across processes.
- SQLite backups are not automatically encrypted.
- Dependency/container scanning and a disclosure process are not yet automated.

For a few trusted friends behind a VPN or private overlay network, these gaps
are manageable if everyone understands them. For an internet-facing service,
address them and obtain a security review first.

## Reporting a vulnerability

Until a private reporting address is configured, do not publish a working
exploit or sensitive instance data in a public issue. Contact the instance
operator directly and agree on a disclosure path.
