# Security policy

## Supported versions

Campfire is maintained for a small self-hosted deployment. Only the latest
released version receives security fixes. Upgrade instructions always include a
pre-upgrade backup and an explicit rollback path.

## Report a vulnerability privately

Do not open a public issue containing an exploit, credentials, private instance
data, or enough detail to reproduce an unpatched vulnerability.

Use GitHub's **Report a vulnerability** form for this repository:
<https://github.com/dvno-v/campfire-talks/security/advisories/new>. If private
reporting is unavailable, open a public issue asking the maintainer to establish
a private contact channel, with no vulnerability details in that issue.

Include the affected version, deployment shape, impact, reproduction steps, and
any suggested mitigation. Minimize copied message, account, IP-address, and log
data; synthetic examples are preferred.

The maintainer aims to acknowledge a report within three business days, provide
an initial assessment within seven, and coordinate disclosure after a fix is
available. Those are response targets, not a promise that every issue can be
resolved on that schedule. Reporters may request credit or anonymity.

## Release integrity

Tagged releases publish SHA-256 checksums and GitHub/Sigstore build-provenance
attestations. Verify a downloaded asset with:

```bash
sha256sum --check SHA256SUMS
gh attestation verify campfire-vX.Y.Z.tar.gz --repo dvno-v/campfire-talks
```

The beginner-friendly [release guide](docs/RELEASES.md) explains what these
checks prove, how to verify every published asset, and what to do when a check
fails.

The repository scans Python source with CodeQL and scans the built container for
high and critical known vulnerabilities on pushes, pull requests, and weekly.
Campfire has no third-party Python packages; the container base images and
GitHub Actions are the dependency surfaces tracked by Dependabot.
