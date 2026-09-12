"""Security tests: secrets must never survive into a Context Object."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from context_git.security import redact, scan_text, scan_object  # noqa: E402
from context_git.common import is_forbidden_path  # noqa: E402


class TestRedaction(unittest.TestCase):
    def test_openai_key(self):
        out = redact("the key is sk-abc123def456ghi789jkl012 done")
        self.assertNotIn("sk-abc123def456ghi789jkl012", out)
        self.assertIn("[REDACTED:api-key]", out)

    def test_short_fake_tokens_from_acceptance_spec(self):
        out = redact("sk-test123 ghp_test")
        self.assertNotIn("sk-test123", out)
        self.assertNotIn("ghp_test", out)
        self.assertEqual(scan_text(out), [])

    def test_openai_project_key(self):
        out = redact("sk-proj-AaaabbbbCCCCddddeeee1234")
        self.assertNotIn("sk-proj-", out.replace("[REDACTED:api-key]", ""))

    def test_anthropic_key(self):
        out = redact("key sk-ant-api03-xxxx-yyyy-zzzz-1234")
        self.assertIn("[REDACTED:api-key]", out)

    def test_github_token(self):
        out = redact("ghp_" + "a1B2c3D4e5" * 4)
        self.assertIn("[REDACTED:github-token]", out)
        self.assertNotIn("ghp_", out.replace("[REDACTED:github-token]", ""))

    def test_github_fine_grained(self):
        out = redact("github_pat_" + "A1b2C3d4E5" * 3)
        self.assertIn("[REDACTED:github-token]", out)

    def test_aws_access_key(self):
        out = redact("AKIAIOSFODNN7EXAMPLE")
        self.assertIn("[REDACTED:aws-access-key]", out)

    def test_private_key_block(self):
        body = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0123456789abcd" * 2
        out = redact(
            "-----BEGIN RSA PRIVATE KEY-----\n" + body
            + "\n-----END RSA PRIVATE KEY-----"
        )
        self.assertIn("[REDACTED:private-key]", out)
        self.assertNotIn(body, out)
        self.assertNotIn("END RSA PRIVATE KEY", out)
        self.assertEqual(scan_text(out), [])

    def test_bearer_header(self):
        out = redact("Authorization: Bearer eyadslfkj1234567890abcdef==")
        self.assertIn("[REDACTED:", out)

    def test_password_assignment(self):
        out = redact('password = "hunter2secret"')
        self.assertNotIn("hunter2secret", out)
        self.assertIn("[REDACTED:credential-value]", out)

    def test_token_yaml(self):
        out = redact("token: super_secret_value_123")
        self.assertNotIn("super_secret_value_123", out)

    def test_database_url(self):
        out = redact("postgres://admin:p4ssw0rd@db.example.com/prod")
        self.assertNotIn("p4ssw0rd", out)
        self.assertIn("[REDACTED:url-password]", out)

    def test_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        out = redact("token " + jwt)
        self.assertNotIn("SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV", out)

    def test_secret_key_env_style(self):
        out = redact("SECRET_KEY=django-insecure-abc123def456ghi789")
        self.assertNotIn("django-insecure-abc123def456ghi789", out)

    def test_idempotent(self):
        once = redact("sk-abc123def456ghi789jkl012")
        twice = redact(once)
        self.assertEqual(once, twice)

    def test_plain_text_untouched(self):
        text = "Refactored the auth middleware to use httpOnly cookies."
        self.assertEqual(redact(text), text)


class TestResidualScan(unittest.TestCase):
    def test_scan_text_catches_survivors(self):
        text = "we use sk-live1234567890abcdefxyz for billing"
        findings = scan_text(text)
        self.assertTrue(any("OpenAI" in r or "key" in r.lower() for r, _ in findings))

    def test_scan_text_clean(self):
        self.assertEqual(scan_text("implemented auth; tests pass"), [])

    def test_scan_object_deep(self):
        obj = {"a": {"b": ["ghp_" + "a1B2c3D4e5" * 4]}}
        self.assertTrue(scan_object(obj))


class TestForbiddenPaths(unittest.TestCase):
    def test_env_files(self):
        for p in (".env", ".env.local", "config/.env.production", ".env.bak"):
            self.assertTrue(is_forbidden_path(p), p)

    def test_key_files(self):
        for p in ("id_rsa", "keys/server.pem", "cert.key", "id_ed25519",
                  "keystore.jks", "upload.pfx"):
            self.assertTrue(is_forbidden_path(p), p)

    def test_credential_dirs(self):
        for p in (".ssh/config", ".aws/credentials", ".kube/config",
                  ".gnupg/secring.gpg"):
            self.assertTrue(is_forbidden_path(p), p)

    def test_named_stores(self):
        for p in ("credentials.json", ".netrc", "secrets.yaml",
                  "terraform.tfstate", ".npmrc"):
            self.assertTrue(is_forbidden_path(p), p)

    def test_normal_files_allowed(self):
        for p in ("src/auth.ts", "package.json", "README.md", "docs/env-guide.md"):
            self.assertFalse(is_forbidden_path(p), p)


if __name__ == "__main__":
    unittest.main()
