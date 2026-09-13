"""V4 acceptance tests: portable, authenticated Remote Context sync."""

import copy
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from context_git import context as context_mod  # noqa: E402
from context_git.gitstate import collect as git_collect  # noqa: E402
from context_git.remote import (  # noqa: E402
    HttpTransport,
    RemoteError,
    empty_manifest,
    make_remote_config,
    normalise_remote_url,
    portable_object,
    validate_context_object,
    validate_manifest,
)
from context_git.storage import Store  # noqa: E402
from helpers import TempRepoTest, cli  # noqa: E402


class TestRemoteProtocol(TempRepoTest):
    def _context(self):
        root = self.make_repo(files={
            "pyproject.toml": '[project]\nname = "remote-demo"\nversion = "0.1"\n',
            "app.py": "print('demo')\n",
        })
        obj = context_mod.build(root, {"goal": "ship remote context"},
                                context_branch="main")
        return root, obj

    def test_portable_copy_preserves_identity_and_source(self):
        root, obj = self._context()
        original = copy.deepcopy(obj)
        portable = portable_object(obj)
        self.assertEqual(obj, original)
        self.assertEqual(portable["project"]["root"], ".")
        self.assertEqual(portable["git"]["repo_root"], ".")
        self.assertTrue(context_mod.verify_context_id(portable))
        self.assertEqual(portable["context_id"], obj["context_id"])
        self.assertEqual(Path(obj["project"]["root"]), root.resolve())

    def test_v1_v2_scalar_parent_identity_is_still_verified(self):
        _, obj = self._context()
        legacy = copy.deepcopy(obj)
        legacy.pop("parent_context_ids")
        legacy["context_id"] = None
        h = hashlib.sha256()
        h.update(b"root")
        h.update(legacy["created_at"].encode("utf-8"))
        h.update(((legacy.get("git") or {}).get("head") or "no-git").encode("utf-8"))
        core = json.dumps(
            context_mod.core_view(legacy), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        )
        h.update(core.encode("utf-8"))
        legacy["context_id"] = "ctx_" + h.hexdigest()[:8]
        self.assertTrue(context_mod.verify_context_id(legacy))
        validate_context_object(legacy, legacy["context_id"])

    def test_tampering_and_residual_secrets_are_refused(self):
        _, obj = self._context()
        tampered = copy.deepcopy(obj)
        tampered["goal"] = "different"
        with self.assertRaisesRegex(RemoteError, "Context id verification"):
            validate_context_object(tampered, obj["context_id"])

        leaked = copy.deepcopy(obj)
        leaked["metadata"]["notes"] = "token=ghp_test"
        leaked["context_id"] = context_mod.compute_context_id(
            leaked, leaked.get("parent_context_id"),
            leaked.get("parent_context_ids"),
        )
        with self.assertRaisesRegex(RemoteError, "secret scan"):
            validate_context_object(leaked, leaked["context_id"])

    def test_remote_url_policy(self):
        root = self.make_repo(files={"app.py": "pass\n"})
        outside = Path(tempfile.mkdtemp(prefix="cgit-remote-"))
        self._tmpdirs.append(str(outside))
        self.assertEqual(normalise_remote_url(outside, root), outside.resolve().as_uri())
        with self.assertRaisesRegex(RemoteError, "outside"):
            normalise_remote_url(root / "shared", root)
        with self.assertRaisesRegex(RemoteError, "contain the project"):
            normalise_remote_url(root.parent, root)
        with self.assertRaisesRegex(RemoteError, "network host"):
            normalise_remote_url("file://example.test/shared", root)
        self.assertEqual(
            normalise_remote_url("https://contexts.example.test/team/demo/", root),
            "https://contexts.example.test/team/demo",
        )
        with self.assertRaisesRegex(RemoteError, "credentials"):
            normalise_remote_url("https://user:pass@example.test/demo", root)
        with self.assertRaisesRegex(RemoteError, "loopback"):
            normalise_remote_url("http://example.test/demo", root, True)
        with self.assertRaisesRegex(RemoteError, "loopback"):
            normalise_remote_url("http://localhost:8080/demo", root)
        with self.assertRaisesRegex(RemoteError, "invalid port"):
            normalise_remote_url("https://example.test:not-a-port/demo", root)
        self.assertEqual(
            normalise_remote_url("http://localhost:8080/demo", root, True),
            "http://localhost:8080/demo",
        )
        with self.assertRaisesRegex(RemoteError, "environment variable"):
            make_remote_config("https://example.test/demo", root, "BAD-NAME")

    def test_manifest_rejects_invalid_protocol_and_dangling_ref(self):
        manifest = empty_manifest({"name": "demo", "repository_fingerprint": None})
        bad_protocol = copy.deepcopy(manifest)
        bad_protocol["protocol"] = "UACP/99.0"
        with self.assertRaisesRegex(RemoteError, "protocol"):
            validate_manifest(bad_protocol)
        manifest["refs"]["heads"]["main"] = "ctx_12345678"
        with self.assertRaisesRegex(RemoteError, "missing object"):
            validate_manifest(manifest)


class TestFileRemoteCLI(TempRepoTest):
    def _repo(self, name="remote-demo"):
        return self.make_repo(files={
            "pyproject.toml": '[project]\nname = "{}"\nversion = "0.1"\n'.format(name),
            "app.py": "print('demo')\n",
        })

    def _remote(self):
        path = Path(tempfile.mkdtemp(prefix="cgit-remote-store-"))
        self._tmpdirs.append(str(path))
        return path

    @staticmethod
    def _head(root):
        return Store(root).head()

    @staticmethod
    def _snapshot(root, objective):
        cli(root, "commit", "--no-prompt", "--set",
            "current_objective=" + objective)
        return Store(root).head()

    def _seed(self):
        root = self._repo()
        remote = self._remote()
        cli(root, "init")
        cli(root, "snapshot", "--no-prompt", "--set", "goal=Ship V4",
            "--set", "current_objective=base")
        cli(root, "remote", "add", "origin", str(remote))
        result = json.loads(cli(root, "push", "--json").stdout)
        return root, remote, result

    def test_two_clones_round_trip_without_touching_source_git(self):
        first, remote, pushed = self._seed()
        base = self._head(first)
        first_git = git_collect(first)
        self.assertEqual(pushed["objects_uploaded"], 1)

        manifest = json.loads((remote / "manifest.json").read_text(encoding="utf-8"))
        remote_obj = json.loads(
            (remote / "contexts" / (base + ".json")).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["refs"]["heads"]["main"], base)
        self.assertRegex(manifest["objects"][base]["sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(remote_obj["project"]["root"], ".")
        self.assertEqual(remote_obj["git"]["repo_root"], ".")

        second = self._repo()
        second_git = git_collect(second)
        cli(second, "init")
        cli(second, "remote", "add", "origin", str(remote))
        pulled = json.loads(cli(second, "pull", "origin", "main", "--json").stdout)
        self.assertEqual(pulled["action"], "fast-forward")
        self.assertEqual(self._head(second), base)
        self.assertEqual(Store(second).list_remote_refs("origin")["main"], base)

        second_tip = self._snapshot(second, "continue on second clone")
        cli(second, "push")
        pulled_back = json.loads(cli(first, "pull", "--json").stdout)
        self.assertEqual(pulled_back["action"], "fast-forward")
        self.assertEqual(self._head(first), second_tip)
        self.assertEqual(git_collect(first)["head"], first_git["head"])
        self.assertEqual(git_collect(first)["branch"], first_git["branch"])
        self.assertEqual(git_collect(second)["head"], second_git["head"])
        self.assertEqual(git_collect(second)["branch"], second_git["branch"])

    def test_non_fast_forward_requires_fetch_and_explicit_merge(self):
        first, remote, _ = self._seed()
        second = self._repo()
        cli(second, "init")
        cli(second, "remote", "add", "origin", str(remote))
        cli(second, "pull")

        first_tip = self._snapshot(first, "first clone work")
        self._snapshot(second, "second clone work")
        cli(second, "push")
        refused = cli(first, "push", expect=5)
        self.assertIn("non-fast-forward", refused.stderr)
        self.assertEqual(self._head(first), first_tip)

        cli(first, "fetch")
        tracking = Store(first).list_remote_refs("origin")["main"]
        self.assertNotEqual(tracking, first_tip)
        merged = json.loads(cli(
            first, "merge", "remote:origin/main", "--resolve",
            "current_objective=ours", "--json",
        ).stdout)
        self.assertEqual(len(merged["parents"]), 2)
        cli(first, "push")
        current = json.loads((remote / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(current["refs"]["heads"]["main"], self._head(first))

    def test_corrupt_remote_is_rejected_before_local_writes(self):
        first, remote, _ = self._seed()
        ctx_id = self._head(first)
        path = remote / "contexts" / (ctx_id + ".json")
        obj = json.loads(path.read_text(encoding="utf-8"))
        obj["goal"] = "tampered in transit"
        path.write_text(json.dumps(obj), encoding="utf-8")

        second = self._repo()
        cli(second, "init")
        cli(second, "remote", "add", "origin", str(remote))
        refused = cli(second, "fetch", expect=5)
        self.assertRegex(refused.stderr, "verification|SHA-256")
        self.assertEqual(Store(second).list_context_ids(), [])
        self.assertEqual(Store(second).list_remote_refs(), {})

    def test_project_mismatch_is_refused_unless_explicit(self):
        _, remote, _ = self._seed()
        other = self._repo("different-project")
        cli(other, "init")
        cli(other, "remote", "add", "origin", str(remote))
        refused = cli(other, "fetch", expect=5)
        self.assertIn("different project", refused.stderr)
        fetched = json.loads(
            cli(other, "fetch", "--allow-other-project", "--json").stdout
        )
        self.assertGreaterEqual(fetched["objects_received"], 1)

    def test_dry_run_remote_remove_and_branch_all(self):
        root = self._repo()
        remote = self._remote()
        cli(root, "init")
        cli(root, "snapshot", "--no-prompt", "--set", "goal=Ship V4")
        cli(root, "remote", "add", "origin", str(remote))
        dry = json.loads(cli(root, "push", "--dry-run", "--json").stdout)
        self.assertTrue(dry["dry_run"])
        self.assertFalse((remote / "manifest.json").exists())
        cli(root, "push")
        ctx_id = self._head(root)
        self.assertIn("remotes/origin/main", cli(root, "branch", "--all").stdout)
        cli(root, "remote", "remove", "origin")
        self.assertEqual(Store(root).remotes(), {})
        self.assertEqual(Store(root).list_remote_refs(), {})
        self.assertIsNotNone(Store(root).load_context(ctx_id))

    def test_auth_value_is_never_persisted(self):
        root = self._repo()
        cli(root, "init")
        secret = "ghp_super_secret_test_value"
        old = os.environ.get("CONTEXT_GIT_TEST_TOKEN")
        os.environ["CONTEXT_GIT_TEST_TOKEN"] = secret
        try:
            cli(root, "remote", "add", "origin", "https://example.test/context",
                "--auth-env", "CONTEXT_GIT_TEST_TOKEN")
        finally:
            if old is None:
                os.environ.pop("CONTEXT_GIT_TEST_TOKEN", None)
            else:
                os.environ["CONTEXT_GIT_TEST_TOKEN"] = old
        stored = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in (root / ".context-git").rglob("*") if path.is_file()
        )
        self.assertNotIn(secret, stored)
        self.assertIn("CONTEXT_GIT_TEST_TOKEN", stored)

class TestHttpRemoteCLI(TempRepoTest):
    TOKEN_ENV = "CONTEXT_GIT_HTTP_TEST_TOKEN"
    TOKEN = "test-http-bearer-value"

    def _repo(self):
        return self.make_repo(files={
            "pyproject.toml": '[project]\nname = "http-remote-demo"\nversion = "0.1"\n',
            "app.py": "print('demo')\n",
        })

    def _server(self, redirect=False):
        state = {
            "objects": {}, "manifest": None, "etag": None,
            "requests": [], "redirect": redirect,
        }
        expected = "Bearer " + self.TOKEN

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def _authorised(self):
                state["requests"].append({
                    "method": self.command,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "if_match": self.headers.get("If-Match"),
                    "if_none_match": self.headers.get("If-None-Match"),
                })
                if self.headers.get("Authorization") != expected:
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return False
                return True

            def do_GET(self):
                if not self._authorised():
                    return
                if state["redirect"]:
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:9/credential-trap")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/context/manifest.json":
                    data = state["manifest"]
                    etag = state["etag"]
                elif self.path.startswith("/context/contexts/"):
                    data = state["objects"].get(self.path)
                    etag = None
                else:
                    data = None
                    etag = None
                if data is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                if etag:
                    self.send_header("ETag", etag)
                self.end_headers()
                self.wfile.write(data)

            def do_PUT(self):
                if not self._authorised():
                    return
                size = int(self.headers.get("Content-Length") or 0)
                data = self.rfile.read(size)
                if self.path == "/context/manifest.json":
                    if state["manifest"] is None:
                        valid = self.headers.get("If-None-Match") == "*"
                    else:
                        valid = self.headers.get("If-Match") == state["etag"]
                    if not valid:
                        self.send_response(412)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    state["manifest"] = data
                    state["etag"] = '"{}"'.format(hashlib.sha256(data).hexdigest())
                elif self.path.startswith("/context/contexts/"):
                    existing = state["objects"].get(self.path)
                    if existing is not None and existing != data:
                        self.send_response(409)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    state["objects"][self.path] = data
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:{}/context".format(server.server_port), state

    def test_authenticated_http_push_fetch_and_etag_update(self):
        url, state = self._server()
        root = self._repo()
        old = os.environ.get(self.TOKEN_ENV)
        os.environ[self.TOKEN_ENV] = self.TOKEN
        try:
            cli(root, "init")
            cli(root, "snapshot", "--no-prompt", "--set", "goal=HTTP sync")
            cli(root, "remote", "add", "origin", url,
                "--allow-insecure-http", "--auth-env", self.TOKEN_ENV)
            cli(root, "push")
            first_etag = state["etag"]
            cli(root, "commit", "--no-prompt", "--set", "completed=HTTP push")
            cli(root, "push")
            self.assertNotEqual(state["etag"], first_etag)
            self.assertTrue(
                any(req["if_none_match"] == "*" for req in state["requests"])
            )
            self.assertTrue(
                any(req["if_match"] == first_etag for req in state["requests"])
            )
            self.assertTrue(all(
                req["authorization"] == "Bearer " + self.TOKEN
                for req in state["requests"]
            ))

            clone = self._repo()
            cli(clone, "init")
            cli(clone, "remote", "add", "origin", url,
                "--allow-insecure-http", "--auth-env", self.TOKEN_ENV)
            cli(clone, "pull")
            self.assertEqual(Store(clone).head(), Store(root).head())
        finally:
            if old is None:
                os.environ.pop(self.TOKEN_ENV, None)
            else:
                os.environ[self.TOKEN_ENV] = old

    def test_missing_credential_and_redirect_are_refused(self):
        url, _ = self._server(redirect=True)
        old = os.environ.pop(self.TOKEN_ENV, None)
        try:
            with self.assertRaisesRegex(RemoteError, "not set"):
                HttpTransport(url, self.TOKEN_ENV).read_manifest()
            os.environ[self.TOKEN_ENV] = self.TOKEN
            with self.assertRaisesRegex(RemoteError, "status 302"):
                HttpTransport(url, self.TOKEN_ENV).read_manifest()
        finally:
            if old is None:
                os.environ.pop(self.TOKEN_ENV, None)
            else:
                os.environ[self.TOKEN_ENV] = old


if __name__ == "__main__":
    unittest.main()
