# Voice, screen sharing, and media E2EE

Campfire's media path is a small-group, self-hosted LiveKit deployment. LiveKit
is an SFU: browsers send media to it and it forwards selected streams to the
other browsers. It does not mix or transcode Campfire calls.

This document is both the operating guide and the security protocol. Read it
before enabling voice. LiveKit's official references for the underlying
deployment are its [ports and firewall guide](https://docs.livekit.io/transport/self-hosting/ports-firewall/),
[self-hosted deployment guide](https://docs.livekit.io/transport/self-hosting/deployment/),
and [E2EE guide](https://docs.livekit.io/transport/encryption/).

## What is implemented

- Community administrators can create text or voice channels. A voice channel
  has no message or upload API.
- A current community member can request a short LiveKit join token. It grants
  access to one room and permits only microphone, screen-video, and
  screen-audio publication. It grants no data publication, room administration,
  recording, ingress, or egress.
- An atomic 45-second lease limits a voice channel to eight participants. The
  browser renews it every 15 seconds; a second missed renewal ends the call. A
  place nobody has renewed yet is held for only 20 seconds, so a browser that
  dies while connecting stops holding the room's key almost immediately.
- The client provides microphone and speaker selection where the browser
  supports it, mute, deafen, speaking and connection-quality indicators.
- Screen sharing has economy (720p5/0.8 Mbps), balanced
  (720p15/1.5 Mbps), and sharp (1080p30/4 Mbps) ceilings. Simulcast gives each
  viewer a lower layer when its connection cannot sustain the source layer.
- The stop-sharing control remains in the voice dock while another panel or
  text channel is open. Stopping it unpublishes and stops both the display video
  and optional display-audio tracks.
- Shared application audio is optional and separate from the microphone. Its
  capture constraints explicitly disable echo cancellation, noise suppression,
  and automatic gain control, request stereo, and disable discontinuous
  transmission. If the browser returns no audio track, the video share stays
  live and the UI says that it has no audio.
- Encoded-frame E2EE is mandatory. A browser without the required WebRTC
  encoded-transform API is refused; there is no unencrypted fallback.

There is no recording or transcription control. The Compose stack deploys no
LiveKit Egress, Ingress, SIP, agent, metrics exporter, webhook, or transcription
service. Campfire tokens cannot publish data and cannot request recording. The
LiveKit container logs warnings and errors into a small rotating local Docker
log; it does not export media telemetry. Exceptional logs can still contain
operational room or participant metadata.

## Room-key protocol

1. When somebody presses **Start encrypted call**, their browser generates 32
   bytes with `crypto.getRandomValues`.
2. The key is placed in the URL fragment as
   `#voice=CHANNEL_ID.BASE64URL_KEY`. A URL fragment is not sent in an HTTP
   request. On opening the link, Campfire copies the key into that tab's
   `sessionStorage` and immediately removes it from the address bar/history.
3. The browser computes SHA-256 of the key and sends only the 64-character
   fingerprint with its token request. Under an immediate SQLite transaction,
   the server verifies membership, removes expired leases, enforces the room
   limit, and refuses a different fingerprint while the room is occupied.
4. Campfire returns a two-minute HS256 LiveKit token for the single room. The
   token names the account and fingerprint but never contains the media key.
5. LiveKit's browser SDK derives the encoded-frame encryption material from the
   random bytes with HKDF and encrypts microphone, screen video, and screen
   audio. The SFU routes encrypted frames.
6. The call link must be distributed out of band: in person, by an already
   trusted end-to-end encrypted messenger, or through another authenticated
   channel that meets the group's threat model.
7. **Start with a new key** creates a new random key. It succeeds when the room
   is empty; while a differently keyed call is occupied, the server directs the
   user to obtain that call's current link.

Do not paste the link into Campfire text if the objective is to hide media from
the Campfire operator: text chat is server-readable. Anyone who obtains the
link can decrypt and join the call while they also have an authorized Campfire
account. A key fingerprint is not secret, but it prevents an accidental split
room; it does not authenticate who gave you the link.

E2EE hides media content from an honest-but-curious SFU and from someone who
later steals its memory or network capture. It does not hide IP addresses,
participant identity, room identity, connection timing, packet sizes, bitrate,
or who is speaking. It also cannot protect against a compromised browser or a
malicious operator who changes the JavaScript served to users. Verify releases
and the host if the operator is in the threat model. This protocol and the
LiveKit E2EE implementation need independent review before use for high-risk
communications.

## Browser and platform limits

Screen selection and audio capture belong to the browser and operating system.
Campfire cannot silently capture a screen and never chooses a surface for the
user.

- Current Chromium-family browsers can commonly capture tab audio. Chromium on
  Windows may additionally offer system audio. The person sharing must choose
  an eligible surface and enable the browser's audio checkbox.
- Firefox and Safari can share display video but are treated as not supporting
  application audio by this client. Their audio checkbox is disabled.
- A browser may still return no audio track after the user requested one. That
  is reported as a valid video-only share, not a failed share.
- Speaker selection depends on `HTMLMediaElement.setSinkId`; browsers without
  it use their system/browser default output.
- A missing encoded-transform API prevents joining because E2EE is mandatory.

Browser support changes. The returned tracks, not the user-agent label, are the
final truth. LiveKit maintains the underlying [screen-share documentation](https://docs.livekit.io/transport/media/screenshare/).

## Network and firewall model

Both `CAMPFIRE_DOMAIN` and `CAMPFIRE_MEDIA_DOMAIN` must resolve to the host.
For a home server, forward the same ports through the router and allow them in
both the provider and host firewalls.

| Protocol | Port | Owner | Purpose |
| --- | ---: | --- | --- |
| TCP | 80 | Caddy | certificate issuance and HTTPS redirect |
| TCP | 443 | Caddy | Campfire HTTPS and LiveKit WSS signaling |
| UDP | 443 | LiveKit | embedded TURN/UDP relay |
| TCP | 7881 | LiveKit | ICE/TCP fallback when direct UDP is blocked |
| UDP | 7882 | LiveKit | direct WebRTC media through the UDP mux |

Port 7880 is LiveKit's HTTP/signaling listener and remains private on the
Compose network behind Caddy. Port 8000 is Campfire and is also private. Do not
publish either.

Caddy owns TCP 443 and LiveKit owns UDP 443. This intentionally disables
Caddy's HTTP/3 listener in favour of a firewall-friendly TURN/UDP port. The
included single-IP stack does not offer TURN/TLS over TCP 443 because Caddy
already owns that socket. ICE/TCP on 7881 is the fully-UDP-blocked fallback. A
deployment that requires TURN/TLS on TCP 443 needs a second public IP or a
reviewed layer-4 routing design and is outside this Compose profile.

The host needs a real publicly reachable address. Carrier-grade NAT and HTTP
tunnels do not forward arbitrary UDP/ICE ports; use a VPS or obtain real port
forwarding instead. LiveKit discovers its external address with STUN, so the
container also needs outbound DNS, UDP, and HTTPS access.

## Loopback voice test

The production LiveKit configuration deliberately discovers a public address.
That is not suitable for a browser and LiveKit running on the same machine: the
signaling WebSocket can join successfully while ICE has no reachable media
candidate. The standalone `compose.local.yaml` instead uses host networking,
binds both services to `127.0.0.1`, advertises the loopback candidate, and
disables TURN. It is a Linux, same-machine functional test—not a deployment
profile and not a test of NAT traversal.

Create the shared API mapping once, without printing the secret:

```bash
sudo install -d -o 10001 -g 10001 -m 700 secrets
openssl rand -hex 32 | sed 's/^/campfire-media: /' | sudo tee secrets/livekit-keys.yaml >/dev/null
sudo chown 10001:10001 secrets/livekit-keys.yaml
sudo chmod 600 secrets/livekit-keys.yaml
```

Then start the standalone stack and open <http://127.0.0.1:8000> in two
different browser profiles:

```bash
docker compose -f compose.local.yaml up --build
```

Use the ordinary URL without `?forceTurn=1`; the loopback profile deliberately
has no TURN relay, and the client rejects that production-only diagnostic flag.

Create or select a voice channel, start a new encrypted call in the first
profile, and open its complete copied link in the second profile. The fragment
after `#` contains the media key and must be present. Stop the loopback stack
without deleting its database with:

```bash
docker compose -f compose.local.yaml down
```

Use the production Compose stack and the field checks below to validate real
DNS, TLS, firewall, direct UDP, and TURN behavior.

## Compose setup

Use two DNS names pointing to the same address:

```dotenv
CAMPFIRE_DOMAIN=chat.example.net
CAMPFIRE_MEDIA_DOMAIN=media.example.net
CAMPFIRE_LIVEKIT_API_KEY=campfire-media
```

Create one mapping file used by both Campfire and LiveKit. The key name must
match `.env`; the secret is 32 random bytes encoded as 64 hexadecimal
characters:

```bash
sudo install -d -o 10001 -g 10001 -m 700 secrets
openssl rand -hex 32 | sed 's/^/campfire-media: /' | sudo tee secrets/livekit-keys.yaml >/dev/null
sudo chown 10001:10001 secrets/livekit-keys.yaml
sudo chmod 600 secrets/livekit-keys.yaml
```

Never commit or print that file. Unlike the room key, this API secret lets its
holder mint arbitrary LiveKit grants. Rotate it by stopping Campfire and
LiveKit, replacing the line, and starting both together. Existing calls end.

The supplied `deploy/livekit.yaml` and Campfire both enforce eight
participants. If an advanced native deployment changes the limit, keep the
application and SFU values identical. Increasing it is a bandwidth and privacy
decision, not only a UI preference.

## Home-uplink budget

The sharer's browser uploads one primary screen layer plus its small simulcast
layers to the SFU. A home-hosted SFU then uploads one selected layer to every
viewer. At the eight-person ceiling, seven people can view one share.

Approximate worst-case server uplink, assuming 64 Kbit/s Opus audio per
forwarded stream and 20% protocol/headroom reserve:

| Screen preset | Screen to seven viewers | Eight-way voice | Suggested host upload |
| --- | ---: | ---: | ---: |
| Economy, 0.8 Mbps | 5.6 Mbps | 3.6 Mbps | at least 12 Mbps |
| Balanced, 1.5 Mbps | 10.5 Mbps | 3.6 Mbps | at least 18 Mbps |
| Sharp, 4 Mbps | 28 Mbps | 3.6 Mbps | at least 40 Mbps |

These are planning figures, not guarantees. Simulcast/adaptive subscription can
lower real use; packet overhead, retransmission, other household traffic, and
an asymmetric ISP can raise the capacity needed. Measure at the server's WAN
interface, not only at the sharing laptop.

## Required field verification

Static tests can verify authorization, token grants, limits, E2EE wiring, and
configuration. They cannot prove a particular router, ISP, firewall, browser,
microphone, or display source. Before declaring a deployment ready:

1. Join with two separate devices/networks using the ordinary URL. In browser
   WebRTC diagnostics, record the selected candidate pair and confirm direct
   UDP on port 7882.
2. Repeat with `?forceTurn=1` appended before the fragment, for example
   `https://chat.example.net/?forceTurn=1`. Confirm the selected local candidate
   is `relay` and traffic reaches UDP 443. This flag is a diagnostic; remove it
   for normal use.
3. Run voice for at least ten minutes on each path. Record round-trip time,
   packets lost, jitter, bytes sent/received, and reconnects.
4. On a supported Chromium platform, share a browser tab with its audio option
   enabled. Confirm peers receive both `screen_share` and
   `screen_share_audio`, then press **Stop sharing** and verify both end.
5. Repeat at all three quality settings while measuring the host WAN uplink.
   Keep the highest setting that leaves at least 20% spare upstream capacity.
6. Confirm the LiveKit service has no Egress/Ingress/agent companion, routine
   access logs are absent, and no media files appear in Campfire data or
   backups.
7. Use mismatched call links in two profiles and confirm the second is refused.
   Use a browser without encoded-frame E2EE and confirm it is refused rather
   than downgraded.

Record the date, browser/OS versions, direct and relay candidate types, group
size, screen preset, measured peak/average uplink, loss, jitter, and RTT. Repeat
after changing router, ISP, LiveKit, browser SDK, or participant ceiling.

## Security-gate inventory

| Gate | Media implementation |
| --- | --- |
| Stored data | Channel `kind`; expiring lease token digest, user/channel IDs, key fingerprint, creation/expiry. No room key or media. |
| Authorization | Administrator creates channels; current community member gets one-room token; token publish sources are narrowly allowlisted. |
| Limits | Twelve grants/account/minute, two-minute JWT, 45-second lease, eight participants, three bounded screen presets, no data publication. |
| Deletion | Leaving deletes the lease; expiry/startup removes abandoned leases; channel/account/member deletion cascades or explicitly removes them. LiveKit holds rooms only ephemerally. |
| Backup effect | Channel kinds and possibly still-live lease hashes/fingerprints enter a snapshot; keys and media do not. Expired leases are pruned on startup after restore. |
| Hostile input | Integer route IDs, exact 64-character lowercase hex fingerprints, bounded lease tokens, parameterized SQL, atomic count/key check, signed short JWTs. |
| Tests | Migration/schema, authorization, message refusal, grant claims, key mismatch, participant cap, renewal/release, static asset/CSP, pinned build and deployment invariants. |
