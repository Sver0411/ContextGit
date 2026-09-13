"""V5 acceptance tests: directed Agent Context handoffs and receipts."""

import copy
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from context_git.gitstate import collect as git_collect  # noqa: E402
from context_git.network import (  # noqa: E402
    NetworkError,
    _object_id,
    _object_meta,
    build_handoff,
    build_profile,
    build_receipt,
    empty_network_manifest,
    validate_agent_id,
    validate_handoff,
    validate_network_manifest,
    validate_receipt,
)
from context_git.storage import Store  # noqa: E402
from helpers import TempRepoTest, cli  # noqa: E402


class TestNetworkProtocol(TempRepoTest):
    def _context(self):
        root = self.make_repo(files={
            "pyproject.toml": '[project]\nname = "network-demo"\nversion = "0.1"\n',
            "app.py": "print('demo')\n",
        })
        cli(root, "init")
        cli(root, "snapshot", "--no-prompt", "--set", "goal=Agent network",
            "--set", "capabilities_required=image-generation")
        return root, Store(root).head_object()

    def test_agent_ids_and_profiles_are_bounded_and_redacted(self):
        self.assertEqual(validate_agent_id("ALICE-1"), "alice-1")
        for value in ("a", "1alice", "alice/team", "alice team", "a" * 65):
            with self.subTest(value=value), self.assertRaises(NetworkError):
                validate_agent_id(value)
        profile = build_profile(
            "alice", "Alice token=ghp_test", {
                "name": "generic", "capabilities": ["git", "filesystem"]
            },
        )
        self.assertNotIn("ghp_test", json.dumps(profile))
        self.assertIn("[REDACTED", profile["display_name"])

    def test_handoff_is_content_addressed_and_capability_aware(self):
        _, ctx = self._context()
        target = build_profile("bob", "Bob", {
            "name": "generic", "capabilities": ["filesystem", "git"],
            "capability_profile": {},
        })
        handoff = build_handoff(
            "alice", "bob", ctx, "main", "Continue validation", 24, target
        )
        self.assertRegex(handoff["handoff_id"], r"^hnd_[0-9a-f]{16}$")
        self.assertEqual(handoff["compatibility"]["percent"], 0)
        self.assertEqual(handoff["compatibility"]["missing"], ["image-generation"])
        validate_handoff(handoff, handoff["handoff_id"])

        tampered = copy.deepcopy(handoff)
        tampered["message"] = "changed"
        with self.assertRaisesRegex(NetworkError, "identity verification"):
            validate_handoff(tampered, handoff["handoff_id"])

        inconsistent = copy.deepcopy(handoff)
        inconsistent["compatibility"]["missing"] = []
        inconsistent["handoff_id"] = _object_id("hnd", inconsistent, "handoff_id")
        with self.assertRaisesRegex(NetworkError, "inconsistent"):
            validate_handoff(inconsistent, inconsistent["handoff_id"])

    def test_only_recipient_can_create_valid_receipt(self):
        _, ctx = self._context()
        handoff = build_handoff("alice", "bob", ctx, "main")
        with self.assertRaisesRegex(NetworkError, "only the handoff recipient"):
            build_receipt(handoff, "mallory", "accepted")
        receipt = build_receipt(handoff, "bob", "completed", "Work finished")
        validate_receipt(receipt, receipt["receipt_id"])
        altered = copy.deepcopy(receipt)
        altered["status"] = "rejected"
        with self.assertRaisesRegex(NetworkError, "identity verification"):
            validate_receipt(altered, receipt["receipt_id"])

    def test_manifest_limits_and_indexes_are_fail_closed(self):
        manifest = empty_network_manifest({
            "name": "network-demo", "repository_fingerprint": None,
        })
        manifest["agents"]["Alice"] = {
            "agent_id": "alice", "display_name": "Alice", "adapter": "generic",
            "capabilities": [], "capability_profile": {},
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        }
        with self.assertRaises(NetworkError):
            validate_network_manifest(manifest)
        manifest["agents"] = {}
        manifest["handoffs"]["hnd_0000000000000000"] = {
            "sha256": "sha256:short", "size": 2,
        }
        with self.assertRaisesRegex(NetworkError, "handoff index"):
            validate_network_manifest(manifest)

    def test_profiles_reject_untrusted_capability_shapes(self):
        profile = build_profile("alice", "Alice", {
            "name": "generic", "capabilities": ["git"],
            "capability_profile": {"git": {"status": "available"}},
        })
        profile["capability_profile"]["git"]["status"] = "certainly"
        manifest = empty_network_manifest({
            "name": "network-demo", "repository_fingerprint": None,
        })
        manifest["agents"]["alice"] = profile
        with self.assertRaisesRegex(NetworkError, "capability profile"):
            validate_network_manifest(manifest)

    def test_expired_handoff_is_still_valid_protocol_data(self):
        _, ctx = self._context()
        handoff = build_handoff("alice", "bob", ctx, "main")
        handoff["created_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat().replace("+00:00", "Z")
        handoff["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat().replace("+00:00", "Z")
        handoff["handoff_id"] = _object_id("hnd", handoff, "handoff_id")
        validate_handoff(handoff, handoff["handoff_id"])


class TestNetworkFileRemoteCLI(TempRepoTest):
    def _repo(self):
        return self.make_repo(files={
            "pyproject.toml": '[project]\nname = "network-demo"\nversion = "0.1"\n',
            "app.py": "print('demo')\n",
        })

    def _remote(self):
        path = Path(tempfile.mkdtemp(prefix="cgit-network-"))
        self._tmpdirs.append(str(path))
        return path

    def _setup_sender(self, remote):
        alice = self._repo()
        cli(alice, "init")
        cli(alice, "snapshot", "--no-prompt", "--set", "goal=Ship V5",
            "--set", "current_objective=delegate review",
            "--set", "capabilities_required=image-generation")
        cli(alice, "remote", "add", "origin", str(remote))
        cli(alice, "push")
        cli(alice, "network", "register", "alice", "--name", "Alice",
            "--agent", "codex")
        return alice

    def _setup_recipient(self, remote):
        bob = self._repo()
        cli(bob, "init")
        cli(bob, "remote", "add", "origin", str(remote))
        cli(bob, "pull")
        cli(bob, "network", "register", "bob", "--name", "Bob",
            "--agent", "generic")
        return bob

    def test_end_to_end_handoff_accept_and_receipt(self):
        remote = self._remote()
        alice = self._setup_sender(remote)
        bob = self._setup_recipient(remote)
        alice_git = git_collect(alice)
        bob_git = git_collect(bob)

        agents = json.loads(cli(alice, "network", "agents", "--json").stdout)
        self.assertEqual(set(agents["agents"]), {"alice", "bob"})
        sent = json.loads(cli(
            alice, "network", "send", "bob", "--json",
            "--message", "Review the image export path",
        ).stdout)
        handoff = sent["handoff"]
        handoff_id = handoff["handoff_id"]
        self.assertEqual(handoff["compatibility"]["missing"], ["image-generation"])

        context_manifest = json.loads(
            (remote / "manifest.json").read_text(encoding="utf-8")
        )
        network_manifest = json.loads(
            (remote / "network.json").read_text(encoding="utf-8")
        )
        self.assertEqual(context_manifest["version"], "1.0")
        self.assertEqual(network_manifest["version"], "1.0")
        self.assertIn(handoff_id, network_manifest["handoffs"])

        inbox = json.loads(cli(bob, "network", "inbox", "--json").stdout)
        self.assertEqual([item["handoff_id"] for item in inbox["handoffs"]], [handoff_id])
        accepted = json.loads(cli(
            bob, "network", "accept", handoff_id, "--switch", "--json"
        ).stdout)
        self.assertTrue(accepted["switched"])
        self.assertEqual(Store(bob).current_branch(), accepted["branch"])
        self.assertEqual(Store(bob).head(), handoff["context_id"])
        self.assertIn("clean", cli(bob, "verify").stdout)

        reply = json.loads(cli(
            bob, "network", "reply", handoff_id, "accepted", "--json",
            "--message", "Review started",
        ).stdout)
        self.assertEqual(reply["receipt"]["status"], "accepted")
        duplicate = cli(
            bob, "network", "reply", handoff_id, "accepted", expect=6
        )
        self.assertIn("already been published", duplicate.stderr)
        cli(bob, "network", "reply", handoff_id, "completed",
            "--message", "Review finished")
        terminal = cli(
            bob, "network", "reply", handoff_id, "rejected", expect=6
        )
        self.assertIn("terminal status completed", terminal.stderr)
        status = json.loads(cli(
            alice, "network", "status", handoff_id, "--json"
        ).stdout)
        self.assertEqual(status["handoffs"][0]["receipts"][-1]["status"], "completed")

        self.assertEqual(git_collect(alice)["head"], alice_git["head"])
        self.assertEqual(git_collect(alice)["branch"], alice_git["branch"])
        self.assertEqual(git_collect(bob)["head"], bob_git["head"])
        self.assertEqual(git_collect(bob)["branch"], bob_git["branch"])

    def test_send_requires_registration_recipient_and_published_context(self):
        remote = self._remote()
        alice = self._setup_sender(remote)
        refused = cli(alice, "network", "send", "bob", expect=6)
        self.assertIn("recipient is not registered", refused.stderr)

        self._setup_recipient(remote)
        cli(alice, "commit", "--no-prompt", "--set", "completed=local only")
        unpublished = cli(alice, "network", "send", "bob", expect=6)
        self.assertIn("push it before sending", unpublished.stderr)

        stranger = self._repo()
        cli(stranger, "init")
        cli(stranger, "remote", "add", "origin", str(remote))
        no_identity = cli(stranger, "network", "inbox", expect=6)
        self.assertIn("no local network identity", no_identity.stderr)

    def test_agent_id_takeover_and_remote_replacement_are_explicit(self):
        remote = self._remote()
        alice = self._setup_sender(remote)
        other = self._repo()
        cli(other, "init")
        cli(other, "remote", "add", "origin", str(remote))
        cli(other, "pull")
        takeover = cli(
            other, "network", "register", "alice", "--agent", "generic", expect=6
        )
        self.assertIn("already registered", takeover.stderr)
        cli(other, "network", "register", "alice", "--agent", "generic", "--force")

        replacement = self._remote()
        cli(alice, "remote", "add", "origin", str(replacement), "--force")
        self.assertIsNone(Store(alice).network_identity())

    def test_tampered_handoff_is_not_cached(self):
        remote = self._remote()
        alice = self._setup_sender(remote)
        bob = self._setup_recipient(remote)
        sent = json.loads(cli(alice, "network", "send", "bob", "--json").stdout)
        handoff_id = sent["handoff"]["handoff_id"]
        path = remote / "network" / "handoffs" / (handoff_id + ".json")
        obj = json.loads(path.read_text(encoding="utf-8"))
        obj["message"] = "tampered"
        path.write_text(json.dumps(obj), encoding="utf-8")
        refused = cli(bob, "network", "inbox", expect=6)
        self.assertRegex(refused.stderr, "identity verification|SHA-256")
        self.assertEqual(Store(bob).list_network_objects("handoffs"), [])

    def test_handoff_to_unpublished_context_is_not_cached(self):
        remote = self._remote()
        alice = self._setup_sender(remote)
        bob = self._setup_recipient(remote)
        sent = json.loads(cli(alice, "network", "send", "bob", "--json").stdout)
        old_id = sent["handoff"]["handoff_id"]
        handoff = copy.deepcopy(sent["handoff"])
        handoff["context_id"] = "ctx_deadbeef"
        handoff["handoff_id"] = _object_id("hnd", handoff, "handoff_id")

        network_manifest_path = remote / "network.json"
        manifest = json.loads(network_manifest_path.read_text(encoding="utf-8"))
        del manifest["handoffs"][old_id]
        manifest["handoffs"][handoff["handoff_id"]] = _object_meta(handoff)
        network_manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        object_path = remote / "network" / "handoffs" / (
            handoff["handoff_id"] + ".json"
        )
        object_path.write_text(
            json.dumps(handoff, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        refused = cli(bob, "network", "inbox", expect=6)
        self.assertIn("missing from the remote manifest", refused.stderr)
        self.assertEqual(Store(bob).list_network_objects("handoffs"), [])

    def test_verify_scans_malformed_network_cache_files(self):
        root = self._repo()
        cli(root, "init")
        directory = Store(root).network_handoffs_dir
        directory.mkdir(parents=True)
        (directory / "malformed.json").write_text(
            '{"note":"token=ghp_abcdefghijklmnopqrstuvwxyz123456"}\n',
            encoding="utf-8",
        )
        result = cli(root, "verify", expect=2)
        self.assertIn("potential leak", result.stdout)

    def test_handoff_expiry_and_wrong_recipient_are_refused(self):
        remote = self._remote()
        alice = self._setup_sender(remote)
        bob = self._setup_recipient(remote)
        sent = json.loads(cli(
            alice, "network", "send", "bob", "--json", "--expires-hours", "1"
        ).stdout)
        handoff_id = sent["handoff"]["handoff_id"]
        cli(bob, "network", "inbox")

        charlie = self._repo()
        cli(charlie, "init")
        cli(charlie, "remote", "add", "origin", str(remote))
        cli(charlie, "pull")
        cli(charlie, "network", "register", "charlie", "--agent", "generic")
        cli(charlie, "network", "inbox")
        # The inbox filters Charlie's view; direct acceptance still checks
        # ownership even if its local cache contains the advertised object.
        refused = cli(charlie, "network", "accept", handoff_id, expect=6)
        self.assertIn("another agent", refused.stderr)

        local = Store(bob).load_network_object("handoffs", handoff_id)
        local["created_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat().replace("+00:00", "Z")
        local["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat().replace("+00:00", "Z")
        local["handoff_id"] = _object_id("hnd", local, "handoff_id")
        # Save under its new valid id to exercise the local expiry gate.
        Store(bob).save_network_object("handoffs", local)
        expired = cli(bob, "network", "accept", local["handoff_id"], expect=6)
        self.assertIn("expired", expired.stderr)


class TestNetworkHttpRemoteCLI(TempRepoTest):
    TOKEN_ENV = "CONTEXT_GIT_V5_HTTP_TOKEN"
    TOKEN = "v5-http-test-token-value"

    def _repo(self):
        return self.make_repo(files={
            "pyproject.toml": '[project]\nname = "network-http-demo"\nversion = "0.1"\n',
            "app.py": "print('demo')\n",
        })

    def _server(self):
        state = {"files": {}, "etags": {}, "requests": []}
        expected_auth = "Bearer " + self.TOKEN

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def _record(self):
                state["requests"].append({
                    "method": self.command, "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "if_match": self.headers.get("If-Match"),
                    "if_none_match": self.headers.get("If-None-Match"),
                })
                if self.headers.get("Authorization") != expected_auth:
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return False
                return True

            def do_GET(self):
                if not self._record():
                    return
                data = state["files"].get(self.path)
                if data is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                if self.path in state["etags"]:
                    self.send_header("ETag", state["etags"][self.path])
                self.end_headers()
                self.wfile.write(data)

            def do_PUT(self):
                if not self._record():
                    return
                size = int(self.headers.get("Content-Length") or 0)
                data = self.rfile.read(size)
                is_manifest = self.path in ("/context/manifest.json", "/context/network.json")
                existing = state["files"].get(self.path)
                if is_manifest:
                    if existing is None:
                        valid = self.headers.get("If-None-Match") == "*"
                    else:
                        valid = self.headers.get("If-Match") == state["etags"].get(self.path)
                    if not valid:
                        self.send_response(412)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                elif existing is not None and existing != data:
                    self.send_response(409)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                state["files"][self.path] = data
                if is_manifest:
                    state["etags"][self.path] = '"{}"'.format(
                        hashlib.sha256(data).hexdigest()
                    )
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:{}/context".format(server.server_port), state

    def test_authenticated_http_network_round_trip_and_etag(self):
        url, state = self._server()
        old = os.environ.get(self.TOKEN_ENV)
        os.environ[self.TOKEN_ENV] = self.TOKEN
        try:
            alice = self._repo()
            cli(alice, "init")
            cli(alice, "snapshot", "--no-prompt", "--set", "goal=HTTP network")
            cli(alice, "remote", "add", "origin", url,
                "--allow-insecure-http", "--auth-env", self.TOKEN_ENV)
            cli(alice, "push")
            cli(alice, "network", "register", "alice", "--agent", "codex")
            first_network_etag = state["etags"]["/context/network.json"]

            bob = self._repo()
            cli(bob, "init")
            cli(bob, "remote", "add", "origin", url,
                "--allow-insecure-http", "--auth-env", self.TOKEN_ENV)
            cli(bob, "pull")
            cli(bob, "network", "register", "bob", "--agent", "generic")
            sent = json.loads(cli(
                alice, "network", "send", "bob", "--json"
            ).stdout)
            inbox = json.loads(cli(bob, "network", "inbox", "--json").stdout)
            self.assertEqual(
                inbox["handoffs"][0]["handoff_id"], sent["handoff"]["handoff_id"]
            )
            self.assertTrue(any(
                req["path"] == "/context/network.json"
                and req["if_match"] == first_network_etag
                for req in state["requests"]
            ))
            self.assertTrue(all(
                req["authorization"] == "Bearer " + self.TOKEN
                for req in state["requests"]
            ))
        finally:
            if old is None:
                os.environ.pop(self.TOKEN_ENV, None)
            else:
                os.environ[self.TOKEN_ENV] = old


if __name__ == "__main__":
    unittest.main()
