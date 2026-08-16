# Campfire roadmap

Campfire is being built for a small, trusted, self-hosted friend group. Privacy,
operator clarity, and recoverability take priority over growth features.

## Product principles

1. No advertising, analytics, behavioral telemetry, public discovery, or remote
   assets.
2. Collect only data required to deliver a user-requested feature.
3. Default to invite-only communities and least privilege.
4. Make retention, backups, and administrator visibility explicit.
5. Prefer a boring, operable single-host deployment before distributed scale.
6. Treat voice/screen sharing and end-to-end encryption as security projects,
   not UI checkboxes.

## Milestone 1 — Private text community

Status: complete

- [x] Accounts, hardened sessions, and rate-limited login
- [x] WebAuthn passkeys with password-confirmed credential management
- [x] Invite-only onboarding with expiring hashed codes
- [x] Communities, owner-managed channels, and membership authorization
- [x] Persistent text chat and authorized live delivery
- [x] Authorized PNG/JPEG/GIF/WebP sharing with bounded private storage
- [x] Member list with owner indicators
- [x] Online presence
- [x] Message edit/delete with clear ownership rules
- [x] Owner invite list and immediate invite revocation
- [x] Unread markers and basic notification preferences

Exit criteria: two browser profiles can safely join, chat, and share images;
users cannot read another community’s content by changing identifiers; restart
does not lose data; all persistent data is documented.

## Milestone 2 — Community control and data lifecycle

Status: complete

- [x] Roles: owner, administrator, moderator, member
- [x] Kick/ban with role hierarchy and invite re-entry prevention
- [x] Channel permissions, slow mode, and upload controls *(contribution
      rules; per-channel read restrictions are deferred to a later milestone)*
- [x] Account password change, active-session list, and remote session revocation
- [x] Per-community message and attachment retention settings
- [x] User account deletion and self-service data export *(export is
      self-service rather than operator-assisted; no operator step is needed)*
- [x] Attachment deletion when its message is removed *(delivered with message
  deletion in Milestone 1)*
- [x] Storage quota and visible disk-usage reporting

Exit criteria: a community owner can manage abuse and retention without editing
SQLite manually, and deletion behavior is covered by integration tests.

## Milestone 3 — Reproducible self-hosting

Status: complete

- [x] Versioned database migrations and transactional encrypted backup/restore commands
- [x] Docker image and isolated, resource-bounded Compose deployment
- [x] HTTPS reverse-proxy example with metadata-minimized logs
- [x] Bounded production ASGI HTTP layer with proxy/request timeouts
- [x] Health/readiness endpoints and storage-capacity warnings
- [x] Configuration validation that fails closed for unsafe public deployments
- [x] Signed releases, dependency scanning, and a vulnerability-reporting policy

SQLite remains the preferred small-instance database. PostgreSQL is considered
only when operational needs justify it.

Exit criteria: a new operator can deploy, upgrade, back up, restore, and roll
back from the documentation without losing messages or files.

## Milestone 4 — Voice and screen sharing

Status: implemented; deployment field verification required

- [x] Self-hosted LiveKit proof of concept and documented firewall/TURN requirements
- [x] Voice channels with device selection, mute/deafen, and connection indicators
- [x] User-initiated screen/window/tab sharing with an always-visible stop control
  that ends video and audio together
- [x] Optional capture of shared-application audio, published as a track separate
  from the microphone, with capture-side processing disabled
- [x] Media quality controls suitable for home upload bandwidth
- [x] E2EE for media with an explicit room-key distribution design
- [x] No recording, transcription, or media telemetry by default

Shared-application audio depends on the browser and operating system, not on
Campfire. Full desktop audio is available on Chromium-based browsers on
Windows; other platforms are limited to tab audio or no audio at all, and
Firefox and Safari cannot capture it. The client advertises what the current
browser supports rather than failing silently, and a share with no audio track
remains a valid share.

The upload ceiling is the server's, not the sharer's. A self-hosted SFU on a
home connection sends one copy of the stream per viewer, so the constraint is
viewers multiplied by bitrate. Screen share is therefore capped by default at
1080p30, simulcast is enabled so constrained viewers receive a lower layer, and
voice channels have a documented participant limit.

Exit criteria: a small group can sustain voice and one screen share through both
direct UDP and TURN fallback within a measured upstream budget, a share with
audio survives the same path on a supported browser, and the documented
encryption claims are verified.

The implementation and reproducible static checks are complete. The direct-UDP,
forced-TURN, browser-audio, and WAN-budget measurements are necessarily
deployment-specific and remain an operator acceptance step; the exact procedure
and evidence to record are in [docs/MEDIA.md](docs/MEDIA.md).

## Milestone 5 — Friend-group polish

Status: in progress

- [x] Paged channel history, so a conversation past one page stays readable
- [x] Campfire's own question and confirmation dialogs, replacing every
      `window.prompt`/`confirm`/`alert`. A browser may suppress those, and an
      invite code is shown exactly once
- [x] Author grouping, day dividers, and a view that stays where it is being
      read instead of following every arrival
- [x] Keyboard focus states and `prefers-reduced-motion` throughout
- [x] A live region on the message list, so an arriving message is announced
- [ ] Direct messages and small group chats
- [ ] Emoji reactions, replies, search, and spoiler markup
- [ ] Optional push notifications with an honest third-party metadata warning
- [ ] Installable PWA — needs a service worker, and therefore a decision about
      what may be cached and how an upgrade invalidates it
- [ ] A full screen-reader pass over the dialogs, member list, and voice dock
- [ ] Import/export format with no dependency on Campfire infrastructure

## Security gate for every feature

Before a feature is marked complete, document its stored data, authorization
rules, size/rate limits, deletion path, backup effect, hostile-input handling,
and tests. Any cryptographic claim needs a written protocol and external review.
