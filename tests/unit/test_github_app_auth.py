"""Unit tests for GitHubAppAuth: App JWT claims, installation-token exchange, and the
module-level token cache. Uses an RSA key generated here — never the App's real key — a fake
HTTP session, and a controllable clock (no network, no real time).
"""

from datetime import UTC, datetime

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import integrations.github as github_module
from integrations.github import GitHubAppAuth, GitHubClientError

APP_ID = "12345"
INSTALLATION_ID = "67890"
NOW = 1_800_000_000


@pytest.fixture(scope="module")
def rsa_keys() -> tuple[str, str]:
    """A throwaway 2048-bit key pair: (private PEM, public PEM). The private key uses
    PKCS#1 ("BEGIN RSA PRIVATE KEY"), the format GitHub issues App keys in."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return private_pem, public_pem


@pytest.fixture(autouse=True)
def _clear_token_cache():
    github_module._installation_token_cache.clear()


class Clock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} error")

    def json(self):
        return self._body


class FakeTokenSession:
    """Issues a new installation token per POST, each valid for `lifetime` seconds from the
    clock's current time, and records every request."""

    def __init__(self, clock: Clock, lifetime: int = 3600, status_code: int = 201):
        self._clock = clock
        self._lifetime = lifetime
        self._status_code = status_code
        self.requests: list[dict] = []

    def post(self, url, headers, timeout):
        self.requests.append({"url": url, "headers": headers})
        expires_at = datetime.fromtimestamp(self._clock() + self._lifetime, tz=UTC)
        body = {
            "token": f"ghs_token_{len(self.requests)}",
            "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        return FakeResponse(self._status_code, body)


def _auth(private_pem: str, clock: Clock, session) -> GitHubAppAuth:
    return GitHubAppAuth(
        app_id=APP_ID,
        installation_id=INSTALLATION_ID,
        private_key_pem=private_pem,
        session=session,
        clock=clock,
    )


def _decode(token: str, public_pem: str) -> dict:
    # The fixed test clock is not "now", so time-based checks are disabled; the signature is
    # still verified against the public half of the test key.
    return jwt.decode(
        token,
        public_pem,
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )


def test_app_jwt_is_rs256_signed_with_expected_claims(rsa_keys):
    private_pem, public_pem = rsa_keys
    clock = Clock(NOW)

    token = _auth(private_pem, clock, FakeTokenSession(clock)).app_jwt()

    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    claims = _decode(token, public_pem)
    assert claims["iss"] == APP_ID
    assert claims["iat"] == NOW - 60
    assert claims["exp"] == NOW + 9 * 60
    assert claims["exp"] - claims["iat"] <= 10 * 60


def test_installation_token_is_exchanged_with_app_jwt(rsa_keys):
    private_pem, public_pem = rsa_keys
    clock = Clock(NOW)
    session = FakeTokenSession(clock)

    token = _auth(private_pem, clock, session).installation_token()

    assert token == "ghs_token_1"
    [request] = session.requests
    assert request["url"] == (
        f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    )
    scheme, app_jwt = request["headers"]["Authorization"].split(" ", 1)
    assert scheme == "Bearer"
    assert _decode(app_jwt, public_pem)["iss"] == APP_ID


def test_cached_token_is_reused_across_instances(rsa_keys):
    """Each warm Lambda invocation builds a new GitHubAppAuth; the module-level cache is what
    lets it skip the exchange."""
    private_pem, _ = rsa_keys
    clock = Clock(NOW)
    session = FakeTokenSession(clock)

    first = _auth(private_pem, clock, session).installation_token()
    clock.now += 50 * 60  # 50 min into a 60-min token: still outside the 5-min margin
    second = _auth(private_pem, clock, session).installation_token()

    assert first == second == "ghs_token_1"
    assert len(session.requests) == 1


def test_token_is_renewed_within_five_minutes_of_expiry(rsa_keys):
    private_pem, _ = rsa_keys
    clock = Clock(NOW)
    session = FakeTokenSession(clock)
    auth = _auth(private_pem, clock, session)

    first = auth.installation_token()
    clock.now = NOW + 3600 - 4 * 60  # 4 min before expiry: inside the refresh margin
    second = auth.installation_token()

    assert first == "ghs_token_1"
    assert second == "ghs_token_2"
    assert len(session.requests) == 2


def test_failed_exchange_raises_and_caches_nothing(rsa_keys):
    private_pem, _ = rsa_keys
    clock = Clock(NOW)
    session = FakeTokenSession(clock, status_code=401)

    with pytest.raises(GitHubClientError, match="installation token exchange failed"):
        _auth(private_pem, clock, session).installation_token()

    assert github_module._installation_token_cache == {}
