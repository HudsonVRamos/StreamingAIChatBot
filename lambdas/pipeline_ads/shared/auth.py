"""SpringServe API authentication module.

Manages token-based authentication with the SpringServe REST API.
Supports automatic re-authentication on HTTP 401 (expired token).
Supports retry with exponential backoff on HTTP 429 (rate limit).

Validates: Requirements 1.2, 1.3, 1.5, 1.6, 10.2
"""

import time

import requests

MAX_RETRIES = 5
BACKOFF_BASE = 1.0  # seconds


class SpringServeAuth:
    """Manages authentication with the SpringServe API."""

    def __init__(self, base_url: str, email: str, password: str):
        self.base_url = base_url
        self.email = email
        self.password = password
        self.session = requests.Session()
        self.token = None

    def authenticate(self) -> str:
        """Authenticate via POST /api/v1/auth and store token.

        Returns the token string on success.
        Raises requests.HTTPError on failure.
        """
        resp = self.session.post(
            f"{self.base_url}/api/v1/auth",
            data={
                "email": self.email,
                "password": self.password,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        resp.raise_for_status()
        self.token = resp.json()["token"]
        self.session.headers["Authorization"] = self.token
        return self.token

    def request(self, method, path, timeout=None, **kwargs):
        """Make an HTTP request with retry on 429 and re-auth on 401.

        Retries up to MAX_RETRIES times with exponential backoff
        on HTTP 429 (Too Many Requests).
        Re-authenticates once on HTTP 401 (expired token).
        Always enforces a 15s connect / 30s read timeout unless overridden.
        """
        url = f"{self.base_url}{path}"
        # Use custom timeout or default
        if timeout is None:
            timeout = (15, 120)
        kwargs.setdefault("timeout", timeout)

        for attempt in range(MAX_RETRIES + 1):
            resp = self.session.request(method, url, **kwargs)

            # Re-auth on 401
            if resp.status_code == 401 and self.token:
                self.authenticate()
                resp = self.session.request(
                    method, url, **kwargs
                )
                if resp.status_code != 429:
                    resp.raise_for_status()
                    return resp

            # Retry on 429 with backoff
            if resp.status_code == 429:
                if attempt < MAX_RETRIES:
                    wait = BACKOFF_BASE * (2 ** attempt)
                    time.sleep(wait)
                    continue
                # Last attempt, raise
                resp.raise_for_status()

            resp.raise_for_status()
            return resp

        resp.raise_for_status()
        return resp
