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
- Active invite metadata and revocation are owner-only. Revocation deletes the
  stored digest, invalidating previously copied codes without retaining them.
- Authentication attempts are limited in memory to eight per client and
  operation scope per five-minute window. Addresses are not persisted. Behind a
  reverse proxy set `CAMPFIRE_TRUSTED_PROXIES`, or every user shares one bucket
  and a single attacker can lock out the instance; forwarding headers are
  ignored unless the peer is a proxy the operator named.
- Sessions use 256-bit random tokens, 30-day expiry, `HttpOnly`, and
  `SameSite=Strict`. Set `CAMPFIRE_SECURE_COOKIES=1` in HTTPS deployments.
  Only a SHA-256 digest of each token is stored, and only the original token is
  accepted, so a stolen database or backup yields no usable session.
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
- Editing a message requires authorship; deleting one requires authorship or
  ownership of its community. Ownership deliberately does not grant the right to
  rewrite another member's words, only to remove them. Non-members receive
  `404` rather than a denial that would confirm the message exists.
- Community member lists verify the requesting actor's current membership and
  return `404` to outsiders rather than revealing whether a community exists.
- Image uploads use a narrow format allowlist, an 8 MiB size limit, magic-byte
  validation, random non-user-controlled storage names, and private authorized
  retrieval. SVG and arbitrary documents are rejected.
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
- There is no multi-factor authentication, password reset, session management
  screen, account deletion, invisible-mode preference, or moderator role model. Deletion is currently the
  only moderation action, and only the community owner has it.
- Messages are not end-to-end encrypted and are retained until someone deletes
  them. Deletion does not reach backups taken beforehand.
- Voice and screen sharing are not implemented yet.
- Image decoding/re-encoding, EXIF removal, malware scanning, and storage quotas
  are not implemented yet. Signature checks are useful defense in depth, not
  proof that an image is harmless.
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
