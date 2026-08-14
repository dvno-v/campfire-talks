# Releasing Campfire, step by step

This guide explains how a maintainer turns reviewed source code into a tagged
Campfire release, what GitHub does automatically, and how anyone can verify the
published files afterward.

You do not need to memorize Git internals or supply-chain terminology. Follow
the numbered procedure in order, stop if a check fails, and use the
troubleshooting section before trying to work around a failure.

## Who should use which part?

- **Maintainers publishing a release** should start at "The release in one
  picture" and follow every numbered step.
- **Self-hosters downloading a release** can jump to "Verify a published
  release". You do not need permission to the repository to verify one.
- **People reviewing project security** can read "What the automation checks"
  and "What the guarantees do and do not mean".

The root [SECURITY.md](../SECURITY.md) explains supported versions and how to
report a vulnerability privately. The [self-hosting guide](SELF-HOSTING.md)
explains how to back up, deploy, upgrade, and roll back an actual instance.

## The release in one picture

```text
reviewed commit on main
          |
          | tests and security workflows pass
          v
version in campfire/__init__.py
          |
          | maintainer creates matching vX.Y.Z tag
          v
GitHub release workflow
          |
          +---- checks tag matches application version
          +---- runs the complete test suite again
          +---- creates a source .tar.gz archive
          +---- creates SHA-256 checksums
          +---- records signed build-provenance attestations
          +---- creates the GitHub release and generated notes
          v
maintainer downloads and independently verifies the result
          |
          v
release is announced
```

There is a deliberate pause between "GitHub published something" and "announce
it". The maintainer verifies the public result using the same interface a
self-hoster will use.

## Important facts before starting

Campfire releases currently publish **source and deployment files**, not a
prebuilt container image. The published archive contains the repository files
tracked by Git at the tagged commit. Self-hosters build the pinned Docker image
from that source.

The release must never contain instance data. In particular, it must not contain:

- `.env` configuration;
- a Campfire SQLite database or its WAL/SHM files;
- uploaded images;
- backup snapshots;
- certificates or private keys; or
- diagnostic logs.

`git archive` includes only files committed to Git, which provides an important
boundary. A secret that was accidentally committed is still tracked, however,
so normal review and secret scanning remain necessary.

## Words used in this guide

- **Commit**: one recorded state of the repository.
- **Branch**: a moving name such as `main` that points at the latest commit in a
  line of work.
- **Tag**: a release name such as `v0.3.0` fixed to one commit.
- **Release asset**: a file downloadable from the GitHub release page.
- **Semantic version**: the `MAJOR.MINOR.PATCH` numbering style, such as `0.3.0`.
- **Checksum**: a short fingerprint calculated from a file's exact bytes.
- **Attestation**: signed provenance saying which GitHub repository, workflow,
  and commit produced an artifact.
- **OIDC**: the short-lived identity GitHub Actions uses to request that signed
  attestation without storing a long-lived signing key.
- **Candidate commit**: the exact reviewed commit the maintainer intends to tag.

## What you need as a maintainer

Before releasing, you need:

- permission to push tags and create releases in `dvno-v/campfire-talks`;
- Git configured with your maintainer identity;
- Python 3.14;
- Docker Engine with the Compose plugin;
- the GitHub CLI (`gh`) for the independent verification commands; and
- a clean local checkout whose `origin` points at the expected repository.

Check the tools:

```bash
git --version
python3 --version
docker --version
docker compose version
gh --version
```

Authenticate the GitHub CLI if necessary:

```bash
gh auth status
```

If that reports no authenticated account, follow its suggested `gh auth login`
flow. Use an account authorized for the repository and protect it with
multi-factor authentication.

## Choosing the version number

Campfire uses versions shaped like `MAJOR.MINOR.PATCH`:

- increase **PATCH** for a compatible bug or security fix: `0.3.0` → `0.3.1`;
- increase **MINOR** for a compatible feature release: `0.3.1` → `0.4.0`; and
- increase **MAJOR** for a deliberately incompatible release after version 1.0.

While the project remains below 1.0, minor releases can still contain meaningful
operator changes. The release notes must call those out explicitly even when the
number technically remains `0.x`.

The Git tag adds a lowercase `v`, but the Python version does not:

```text
Python:  __version__ = "0.3.0"
Git tag: v0.3.0
Archive: campfire-v0.3.0.tar.gz
```

Do not add words, build dates, or a second `v` to the version.

## Step 1: prepare the release change

Release preparation should happen in an ordinary reviewed branch and pull
request, not as an unreviewed edit directly on `main`.

Update the application version in `campfire/__init__.py`:

```python
__version__ = "0.3.0"
```

Use the version you actually intend to release. Then update every document whose
instructions or claims changed. At minimum, review:

- `README.md` for the current project status;
- `ROADMAP.md` for completed or newly planned work;
- `docs/SELF-HOSTING.md` for new settings, migrations, or operator actions;
- `docs/API.md` for changed HTTP behavior;
- `docs/SECURITY.md` and `docs/PRIVACY.md` for changed controls or data; and
- `.env.example`, `compose.yaml`, and `deploy/Caddyfile` for deployment changes.

GitHub generates initial release notes from merged work when the release is
published. Good pull-request titles and descriptions therefore matter. Prepare
additional hand-written notes if operators need warnings, manual steps, or a
clear upgrade/rollback explanation that generated notes would miss.

Confirm the version Python sees:

```bash
python3 -c 'from campfire import __version__; print(__version__)'
```

It should print only the intended version, for example:

```text
0.3.0
```

## Step 2: run the local release checks

Run the full test suite from the repository root:

```bash
npm ci --ignore-scripts --no-audit --no-fund
npm run build
git diff --exit-code -- static/voice.js static/livekit-e2ee-worker.js
npm audit --audit-level=high
python3 -m unittest discover -s tests -v
```

The command must finish with `OK`. A skipped, interrupted, or partially run
suite is not a passing suite.

Validate the safe private defaults:

```bash
python3 -m campfire check-config
```

Expected output:

```text
configuration is safe
```

Build the exact container candidate:

```bash
docker build --tag campfire:release-check .
```

Then validate the public Compose model with a harmless example domain:

```bash
env CAMPFIRE_DOMAIN=chat.example.net CAMPFIRE_MEDIA_DOMAIN=media.example.net \
  docker compose config --quiet
```

That final command prints nothing on success. It does not contact the example
domain or start a server; it only proves that Compose can render the model.

If any check fails, fix it in the release branch, review the fix, and restart
this step. Do not create a tag to see whether GitHub happens to pass.

## Step 3: inspect exactly what will be released

Review all changes before the preparation commit is merged:

```bash
git status --short
git diff --check
git diff
```

Look specifically for:

- the correct version string;
- accidental credentials, tokens, hostnames, or personal paths;
- generated databases, uploads, backups, or logs;
- undocumented configuration or schema changes;
- temporary debugging code;
- dependencies that are unpinned or unexplained; and
- deployment pins changed without a matching review of their upstream release.

`git diff --check` should produce no output. `git status --short` should list
only files intentionally changed by the release work.

Commit the preparation, push its branch, and have the pull request reviewed in
the ordinary way. The exact collaboration commands depend on the project's
branch naming and review process; the important requirement is that release
preparation reaches `main` through review rather than bypassing it.

## Step 4: wait for automation on the candidate commit

After the preparation pull request is merged, open the commit on GitHub and
confirm that both normal workflows passed:

- **Tests** runs the Python suite, validates configuration, builds the container,
  and parses the Compose model.
- **Security scanning** analyzes Python with CodeQL and scans the built container
  with Trivy for fixed high and critical vulnerabilities.

The security workflow also runs every Monday and can be started manually. A
green run from an older commit is not evidence for a newer candidate. Confirm
the successful jobs belong to the exact commit you intend to tag.

From the command line, recent runs can be inspected with:

```bash
gh run list --workflow test.yml --limit 5
gh run list --workflow security.yml --limit 5
```

The GitHub web interface is often easier for checking that every job and step is
green. Do not release while a relevant run is pending, cancelled, skipped, or
red.

## Step 5: synchronize and identify the exact commit

Return to the local checkout and update `main` without creating an accidental
merge commit:

```bash
git switch main
git pull --ff-only origin main
git status --short
git log -1 --oneline
```

`git status --short` must print nothing. If it lists files, stop and preserve or
finish that work before releasing. Do not discard someone else's local changes
just to make the tree clean.

Compare the commit ID from `git log -1` with the successful candidate commit on
GitHub. They must be the same. Check the version again:

```bash
python3 -c 'from campfire import __version__; print(__version__)'
```

Also confirm the intended tag does not already exist locally or remotely. For a
`0.3.0` release:

```bash
git tag --list v0.3.0
git ls-remote --tags origin refs/tags/v0.3.0
```

Both commands should produce no tag result. Release tags are permanent names;
never reuse an existing version for different bytes.

## Step 6: create the release tag locally

Create an annotated tag. Replace `0.3.0` in both places with the version printed
by Python:

```bash
git tag --annotate v0.3.0 --message "Campfire 0.3.0"
```

An annotated tag records the tagger, time, and message in addition to its target
commit. Review it before pushing:

```bash
git show --no-patch --decorate v0.3.0
```

Check that:

- the tag name is correct;
- its target is the candidate commit;
- the message names the same version; and
- `campfire.__version__` at that tag has the matching value.

You can perform the final version check without changing branches:

```bash
git show v0.3.0:campfire/__init__.py
```

If the local tag is wrong and has **not** been pushed, delete only that local
tag and create it correctly:

```bash
git tag --delete v0.3.0
```

This is safe only before publishing. Recheck the exact tag name before entering
the delete command.

## Step 7: push only the tag

Pushing a `v*` tag starts the signed release workflow. This is the point at which
the release name becomes public infrastructure, so pause and check the command:

```bash
git push origin v0.3.0
```

Push the one explicit tag, not every local tag at once. Do not use `git push
--tags` as a release habit.

Once a tag is pushed, do not move it, delete it, or reuse its version to hide a
mistake. People may already have fetched it. If the tagged release is defective,
fix the problem in a reviewed commit and publish a new patch version.

## Step 8: watch the signed release workflow

Open the repository's **Actions** page and select **Signed release**. The run for
the new tag performs these steps in order:

1. Checks out the complete tagged history.
2. Installs Python 3.14.
3. imports `campfire.__version__` and requires the tag to equal `v` plus that
   value.
4. Runs the full test suite against the tagged commit.
5. Creates `campfire-vX.Y.Z.tar.gz` using `git archive`.
6. Creates `SHA256SUMS` with the archive's SHA-256 checksum.
7. Requests GitHub/Sigstore provenance attestations for both files.
8. Creates a GitHub release for the existing tag with generated notes and the
   two downloadable assets.

Using the GitHub CLI, find and watch the run:

```bash
gh run list --workflow release.yml --limit 5
gh run watch RUN_ID
```

Replace `RUN_ID` with the numeric database ID shown by the first command. A
successful run finishes with every step green.

The workflow receives a short-lived GitHub token for release publication and an
OIDC identity for provenance. No long-lived artifact-signing key is stored in
repository secrets.

## Step 9: inspect the GitHub release

Do not announce it yet. Open the release page and check:

- the release title/tag is exactly `vX.Y.Z`;
- it points to the intended commit;
- it is not unexpectedly marked as a draft or prerelease;
- generated notes describe the changes sensibly;
- operator-impacting changes are clearly explained; and
- both `campfire-vX.Y.Z.tar.gz` and `SHA256SUMS` are present.

Attestations are recorded by GitHub rather than appearing as ordinary extra
files in the asset list. They are queried with `gh attestation verify` in the
next step.

If generated notes are incomplete, edit the release description to add context.
Do not replace the archive or checksum asset under the same version. A release
version should always identify one stable set of bytes.

## Step 10: verify the published release independently

Verify the files downloaded from the public release, not files left in a local
build directory. This tests the same distribution path a self-hoster uses.

Create a new directory and download the assets. The example uses `v0.3.0`; replace
it with the actual tag:

```bash
mkdir release-check-v0.3.0
gh release download v0.3.0 \
  --repo dvno-v/campfire-talks \
  --dir release-check-v0.3.0
cd release-check-v0.3.0
```

List the downloaded files:

```bash
ls -l
```

You should see:

```text
SHA256SUMS
campfire-v0.3.0.tar.gz
```

First check the archive's bytes against the published checksum:

```bash
sha256sum --check SHA256SUMS
```

Expected output:

```text
campfire-v0.3.0.tar.gz: OK
```

Then verify that GitHub's signed provenance connects the archive to this
repository's release workflow:

```bash
gh attestation verify campfire-v0.3.0.tar.gz \
  --repo dvno-v/campfire-talks
```

The workflow attests the checksum file as well, so verify it too:

```bash
gh attestation verify SHA256SUMS \
  --repo dvno-v/campfire-talks
```

Both commands must report successful verification. Finally, inspect the archive
shape without extracting or running it:

```bash
tar --list --gzip --file campfire-v0.3.0.tar.gz | head -30
```

Every entry should be underneath the single `campfire-v0.3.0/` prefix. There
must be no `.env`, databases, uploads, backups, or logs.

Leave the temporary verification directory with `cd ..` when finished. You may
then remove that local download when you no longer need it; this does not affect
the release on GitHub.

## Step 11: announce and monitor the release

Only after the workflow and independent verification pass should the release be
announced as available.

An announcement should contain:

- the exact version and release-page link;
- a short description of important changes;
- any security impact without prematurely exposing an uncoordinated weakness;
- required configuration or migration actions;
- a reminder to create and verify a pre-upgrade backup;
- a link to the self-hosting upgrade and rollback instructions; and
- known limitations or regressions.

After announcement, watch for failed deployments, readiness failures, data
migration reports, and security reports. Keep the previous release and its
documentation available so operators can follow the documented snapshot-based
rollback process.

## Verify a published release as a self-hoster

If you did not create the release, your shortest safe verification path is:

```bash
mkdir release-check-v0.3.0
gh release download v0.3.0 \
  --repo dvno-v/campfire-talks \
  --dir release-check-v0.3.0
cd release-check-v0.3.0
sha256sum --check SHA256SUMS
gh attestation verify campfire-v0.3.0.tar.gz \
  --repo dvno-v/campfire-talks
gh attestation verify SHA256SUMS \
  --repo dvno-v/campfire-talks
```

Replace every `0.3.0` with the version you are downloading. Do not mix the
archive from one version with `SHA256SUMS` from another.

Only extract or build the archive after all three verification commands pass.
Then return to the [self-hosting guide](SELF-HOSTING.md) for the pre-upgrade
backup, build, migration, readiness, and rollback procedure.

## Checksums and attestations in plain English

Imagine a checksum as the tamper-evident number printed on a sealed package. If
one byte of the archive changes, its SHA-256 checksum changes. Running
`sha256sum --check` proves the archive matches the value in `SHA256SUMS`.

A checksum alone does not prove who printed the number. An attacker able to
replace both the archive and checksum could make those two malicious files agree.

The provenance attestation supplies the missing origin evidence. GitHub Actions
uses a short-lived identity to sign a statement describing the artifact digest,
repository, workflow, and source context. `gh attestation verify --repo
dvno-v/campfire-talks` checks that signed statement and requires it to belong to
this repository.

Use both mechanisms:

- the checksum is a convenient, portable byte-for-byte integrity check; and
- the attestation ties those bytes to the expected GitHub build identity.

Neither mechanism promises that the source code is bug-free or benevolent. They
prove what bytes were published and where those bytes came from. Code review,
tests, scanning, and cautious deployment handle different parts of the risk.

## What the automation checks

### Tests workflow

`.github/workflows/test.yml` runs for pushes to `main` and pull requests. It:

- runs all Python tests on Python 3.14;
- validates the default private configuration;
- builds the Docker image; and
- renders the public Compose model with an example domain.

### Security workflow

`.github/workflows/security.yml` runs for pushes to `main`, pull requests, a
weekly schedule, and manual requests. It has two jobs:

- **CodeQL** performs static analysis of the Python source; and
- **Trivy** builds the candidate Docker image and fails for known fixed HIGH or
  CRITICAL operating-system/application package vulnerabilities.

`ignore-unfixed: true` means Trivy does not fail the release merely because an
upstream project has disclosed an issue for which no fixed package exists. Such
findings can still require human assessment; the setting is not a declaration
that they are harmless.

### Release workflow

`.github/workflows/release.yml` runs only when a tag whose name begins with `v`
is pushed. It verifies the tag/version relationship, reruns tests, builds the
source assets, attests them, and publishes the release.

The release workflow does not publish a container image and does not replace the
candidate commit's earlier container scan. This is why the maintainer checks
that Tests and Security scanning succeeded on the exact commit before tagging.

### Dependabot

`.github/dependabot.yml` checks the npm lock, Docker image references, and GitHub
Actions every week. Updates arrive as ordinary pull requests and must pass
review and the same automation. Browser/build packages are exactly locked,
GitHub Actions are pinned to immutable commit SHAs, and container base images
are pinned to digests, so an upstream moving tag cannot silently change a build.

Campfire currently has no third-party Python packages. If one is introduced, it
must be pinned in a lock file, included in vulnerability automation and release
review, and documented as a new persistent supply-chain surface.

## Troubleshooting failed releases

### The tag/version check failed

The pushed tag did not equal `v` plus `campfire.__version__`. For example, the
tag may be `v0.3.1` while the file still says `0.3.0`.

Do not edit or force-move the published tag. Prepare a reviewed correction,
choose a new unused patch version, and create a new tag from the corrected
commit. Preserve the failed run as an audit trail.

### Tests failed in the release workflow

The tag points at code that does not pass the release environment. Do not bypass
or remove the failing test. Reproduce the failure from the tagged commit,
prepare a reviewed fix, and publish a new patch version. A test that passed only
before the final release commit is not sufficient.

### Attestation generation failed

Check the failed step and repository Actions permissions. The workflow requires:

```yaml
permissions:
  contents: write
  id-token: write
  attestations: write
```

Do not "fix" this by deleting the attestation step or adding a long-lived
signing key. Correct the GitHub permission or service problem, then rerun the
failed workflow if no release assets were published.

### Release publication failed after assets were built

Inspect whether a GitHub release already exists for the tag before retrying:

```bash
gh release view v0.3.0 --repo dvno-v/campfire-talks
```

If no release exists and the tag/commit are correct, rerunning the failed job is
reasonable:

```bash
gh run rerun RUN_ID --failed
```

If a release or assets already exist, compare them carefully before taking any
action. Do not overwrite assets in place. When provenance or published bytes
are ambiguous, publish a corrected patch version instead of trying to preserve
the old version number.

### `sha256sum --check` fails

Do not extract or run the archive. Delete only the failed local download, fetch
both assets again from the official release page, and retry. A persistent
mismatch means the release is not safe to use and should be reported privately
to the maintainers.

### Attestation verification fails

Confirm the repository argument and version filename are exact, update the
GitHub CLI if it is too old to support attestations, and download the asset again.
Do not substitute a different repository or use an option that weakens identity
checking merely to turn the result green.

### Generated release notes are incomplete

Editing explanatory release text is fine. Replacing an archive or checksum is
not. Add missing upgrade warnings, links, and acknowledgements to the release
description without changing the published artifact bytes.

### A serious problem is found after announcement

Do not move the tag or silently replace assets. If private instance data or a
signing credential was exposed, begin incident response immediately; deleting a
GitHub asset does not erase copies already downloaded.

For an ordinary defect, document the impact and prepare a new patch release. For
a vulnerability, use GitHub's private security-advisory flow, coordinate the fix
and disclosure, and follow the reporting policy in [SECURITY.md](../SECURITY.md).

## Final maintainer checklist

Use this as a last review, not as a replacement for the detailed steps:

- [ ] Version chosen and updated in `campfire/__init__.py`.
- [ ] User, operator, security, and privacy documentation reviewed.
- [ ] Full local tests passed.
- [ ] Configuration validation passed.
- [ ] Candidate container built locally.
- [ ] Compose model rendered successfully.
- [ ] Release preparation merged through review.
- [ ] Tests and Security scanning passed on the exact candidate commit.
- [ ] Local `main` is clean and matches that commit.
- [ ] Tag name is unused and exactly matches the Python version.
- [ ] Annotated tag reviewed before push.
- [ ] Signed release workflow completed successfully.
- [ ] GitHub release tag, notes, and two assets inspected.
- [ ] Downloaded archive passed its checksum.
- [ ] Archive and `SHA256SUMS` both passed attestation verification.
- [ ] Archive listing contains only expected source/deployment paths.
- [ ] Upgrade, backup, migration, and rollback instructions are linked.
- [ ] Release announced only after independent verification.
