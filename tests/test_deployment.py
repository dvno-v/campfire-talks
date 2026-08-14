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
        self.assertNotIn("./backups:/backups", campfire_service)
        self.assertNotIn('VOLUME ["/data", "/backups"]', self.read("Dockerfile"))
        self.assertIn("internal: true", compose)
        self.assertIn("pids_limit: 128", campfire_service)
        self.assertIn("mem_limit: 768m", campfire_service)

    def test_operator_container_alone_can_reach_backups_and_keys(self):
        compose = self.read("compose.yaml")
        operator = compose.split("  campfire-ops:", 1)[1].split("\nnetworks:", 1)[0]
        self.assertIn("profiles:\n      - operations", operator)
        self.assertIn("./backups:/backups", operator)
        self.assertIn("./secrets:/run/secrets:ro", operator)
        self.assertIn("network_mode: none", operator)
        self.assertIn("cap_drop:\n      - ALL", operator)
        self.assertRegex(self.read(".dockerignore"), r"(?m)^secrets$")

    def test_proxy_discards_metadata_logs_and_replaces_forwarding_input(self):
        caddyfile = self.read("deploy/Caddyfile")
        self.assertGreaterEqual(caddyfile.count("output discard"), 2)
        self.assertIn("header_up X-Forwarded-For {remote_host}", caddyfile)
        self.assertIn("max_size 9MB", caddyfile)
        self.assertIn("read_header 5s", caddyfile)
        self.assertIn("read_body 2m", caddyfile)
        self.assertIn("max_header_size 32KB", caddyfile)
        self.assertIn("unhealthy_request_count 64", caddyfile)
        self.assertNotIn("flush_interval -1", caddyfile)

    def test_media_server_is_pinned_isolated_and_has_only_required_media_ports(self):
        compose = self.read("compose.yaml")
        media = compose.split("  livekit:", 1)[1].split("  campfire-ops:", 1)[0]
        self.assertRegex(media, r"image: livekit/livekit-server:v[0-9.]+@sha256:[0-9a-f]{64}")
        self.assertIn('"7881:7881/tcp"', media)
        self.assertIn('"7882:7882/udp"', media)
        self.assertIn('"443:443/udp"', media)
        self.assertNotIn('"7880:7880"', media)
        self.assertIn("read_only: true", media)
        self.assertIn("cap_drop:\n      - ALL", media)
        self.assertIn('user: "10001:10001"', media)
        self.assertIn("./secrets/livekit-keys.yaml:/run/secrets/livekit-keys.yaml:ro", media)

        config = self.read("deploy/livekit.yaml")
        self.assertIn("max_participants: 8", config)
        self.assertIn("udp_port: 7882", config)
        self.assertIn("tcp_port: 7881", config)
        self.assertIn("udp_port: 443", config)
        self.assertIn("level: warn", config)
        for disabled_subsystem in ("egress", "ingress", "webhook", "prometheus"):
            self.assertNotRegex(config, rf"(?m)^{disabled_subsystem}:")

        caddyfile = self.read("deploy/Caddyfile")
        self.assertIn("{$CAMPFIRE_MEDIA_DOMAIN}", caddyfile)
        self.assertIn("reverse_proxy livekit:7880", caddyfile)

    def test_local_media_profile_is_loopback_only_and_does_not_discover_wan_ip(self):
        compose = self.read("compose.local.yaml")
        self.assertIn("CAMPFIRE_HOST: 127.0.0.1", compose)
        self.assertIn("CAMPFIRE_ORIGIN: http://127.0.0.1:8000", compose)
        self.assertIn("CAMPFIRE_MEDIA_URL: ws://127.0.0.1:7880", compose)
        self.assertEqual(compose.count("network_mode: host"), 2)
        self.assertNotIn("ports:", compose)
        self.assertRegex(compose, r"image: livekit/livekit-server:v[0-9.]+@sha256:[0-9a-f]{64}")

        config = self.read("deploy/livekit.local.yaml")
        self.assertIn("bind_addresses:\n  - 127.0.0.1", config)
        self.assertIn("node_ip: 127.0.0.1", config)
        self.assertIn("use_external_ip: false", config)
        self.assertIn("enable_loopback_candidate: true", config)
        self.assertIn("enabled: false", config)

    def test_browser_media_dependencies_are_exact_and_reproducibly_built(self):
        package = self.read("package.json")
        self.assertRegex(package, r'"livekit-client": "[0-9.]+"')
        self.assertRegex(package, r'"esbuild": "[0-9.]+"')
        self.assertIn("--minify", package)
        self.assertTrue((ROOT / "package-lock.json").is_file())
        self.assertTrue((ROOT / "static" / "voice.js").is_file())
        self.assertTrue((ROOT / "static" / "livekit-e2ee-worker.js").is_file())
        workflow = self.read(".github/workflows/test.yml")
        self.assertIn("npm ci --ignore-scripts", workflow)
        self.assertIn("npm run build", workflow)
        release = self.read(".github/workflows/release.yml")
        self.assertIn("npm ci --ignore-scripts", release)
        self.assertIn("git diff --exit-code -- static/voice.js", release)
        security = self.read(".github/workflows/security.yml")
        self.assertIn("npm audit --audit-level=high", security)
        source = self.read("frontend/voice.js")
        self.assertIn("crypto.getRandomValues", source)
        self.assertIn("sessionStorage", source)
        self.assertIn("isE2EESupported", source)
        self.assertLess(source.index("await room.setE2EEEnabled(true)"),
                        source.index("await room.connect"))
        self.assertIn("publication.isEncrypted", source)
        self.assertIn("state.room === room && state.connected", source)
        self.assertIn("TURN is not part of the loopback test", source)
        self.assertIn("echoCancellation: false", source)
        self.assertIn("noiseSuppression: false", source)
        self.assertIn("autoGainControl: false", source)
        self.assertIn("Track.Source.ScreenShareAudio", source)
        self.assertIn("voice-stop-floating", source)
        self.assertIn("iceTransportPolicy: 'relay'", source)

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

    def test_production_server_and_passkey_dependencies_are_pinned(self):
        requirements = self.read("requirements.txt")
        self.assertRegex(requirements, r"(?m)^uvicorn==[0-9.]+$")
        self.assertRegex(requirements, r"(?m)^webauthn==[0-9.]+$")
        server = self.read("campfire/http.py")
        self.assertIn("from .asgi import serve", server)
        self.assertNotIn("ThreadingHTTPServer((HOST, PORT), App)", server)
        frontend = self.read("static/app.js")
        self.assertIn("navigator.credentials.create", frontend)
        self.assertIn("navigator.credentials.get", frontend)


if __name__ == "__main__":
    unittest.main()
