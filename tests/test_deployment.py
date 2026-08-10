"""Static deployment and supply-chain invariants with no YAML dependency."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class DeploymentTests(unittest.TestCase):
    def read(self, relative):
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_container_is_pinned_non_root_and_has_a_readiness_probe(self):
        dockerfile = self.read("Dockerfile")
        self.assertRegex(dockerfile.splitlines()[0], r"^FROM .+@sha256:[0-9a-f]{64}$")
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn("/readyz", dockerfile)
        self.assertNotIn("apk add", dockerfile, "Campfire needs no runtime package install")

    def test_compose_exposes_only_the_pinned_proxy_and_drops_app_privilege(self):
        compose = self.read("compose.yaml")
        self.assertRegex(compose, r"image: caddy:[^\s]+@sha256:[0-9a-f]{64}")
        campfire_service = compose.split("  caddy:", 1)[0]
        self.assertNotIn("    ports:", campfire_service)
        self.assertIn("cap_drop:\n      - ALL", campfire_service)
        self.assertIn("read_only: true", campfire_service)
        self.assertIn("CAMPFIRE_TRUSTED_PROXIES: 172.31.238.2", compose)
        self.assertIn("ipv4_address: 172.31.238.2", compose)

    def test_proxy_discards_metadata_logs_and_replaces_forwarding_input(self):
        caddyfile = self.read("deploy/Caddyfile")
        self.assertGreaterEqual(caddyfile.count("output discard"), 2)
        self.assertIn("header_up X-Forwarded-For {remote_host}", caddyfile)
        self.assertIn("max_size 9MB", caddyfile)

    def test_every_workflow_action_is_pinned_to_a_commit(self):
        workflows = list((ROOT / ".github" / "workflows").glob("*.yml"))
        self.assertTrue(workflows)
        for workflow in workflows:
            for line in workflow.read_text(encoding="utf-8").splitlines():
                match = re.search(r"\buses:\s*[^@\s]+@([^\s]+)", line)
                if match:
                    self.assertRegex(match.group(1), r"^[0-9a-f]{40}$", line)

    def test_security_policy_and_release_verification_are_discoverable(self):
        policy = self.read("SECURITY.md")
        self.assertIn("Report a vulnerability", policy)
        self.assertIn("gh attestation verify", policy)
        self.assertIn("SHA256SUMS", policy)


if __name__ == "__main__":
    unittest.main()

