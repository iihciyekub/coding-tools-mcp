from __future__ import annotations

import unittest
from unittest.mock import patch

import jwt

from coding_tools_mcp.oauth import OAuthClientRegistry, OAuthConfig, create_access_token, validate_access_token


class OAuthTokenLifetimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.url = "https://mcp.example.com"
        registry = OAuthClientRegistry()
        self.client_id = registry.register({"redirect_uris": ["http://127.0.0.1/callback"]})["client_id"]
        self.config = OAuthConfig(
            password="test-only",
            server_url=self.url,
            token_secret=b"test-only-signing-key-32-bytes-long",
            registry=registry,
        )

    def test_default_token_remains_valid_years_after_issue(self) -> None:
        with patch("coding_tools_mcp.oauth.time.time", return_value=1_000_000):
            token = create_access_token(self.config, self.url, client_id=self.client_id)
        claims = jwt.decode(token, self.config.token_secret, algorithms=["HS256"], audience=self.url)
        self.assertNotIn("exp", claims)
        self.assertTrue(validate_access_token(token, self.config, self.url))

    def test_finite_token_retains_its_expiration_under_unlimited_default(self) -> None:
        finite = OAuthConfig(
            password=self.config.password,
            server_url=self.url,
            token_secret=self.config.token_secret,
            token_ttl=604800,
            registry=self.config.registry,
        )
        with patch("coding_tools_mcp.oauth.time.time", return_value=1_000_000):
            token = create_access_token(finite, self.url, client_id=self.client_id)
        claims = jwt.decode(
            token, self.config.token_secret, algorithms=["HS256"], audience=self.url,
            options={"verify_exp": False},
        )
        self.assertEqual(claims["exp"] - claims["iat"], 604800)
        self.assertFalse(validate_access_token(token, self.config, self.url))

    def test_unlimited_token_still_requires_original_key_client_and_resource(self) -> None:
        token = create_access_token(self.config, self.url, client_id=self.client_id)
        self.assertFalse(validate_access_token(token, self.config, "https://other.example.com"))
        for label, config in (
            ("rotated key", OAuthConfig(
                password=self.config.password, server_url=self.url,
                token_secret=b"different-test-only-signing-key-32-bytes",
                registry=self.config.registry,
            )),
            ("unregistered client", OAuthConfig(
                password=self.config.password, server_url=self.url, token_secret=self.config.token_secret,
            )),
        ):
            with self.subTest(label=label):
                self.assertFalse(validate_access_token(token, config, self.url))


if __name__ == "__main__":
    unittest.main()
