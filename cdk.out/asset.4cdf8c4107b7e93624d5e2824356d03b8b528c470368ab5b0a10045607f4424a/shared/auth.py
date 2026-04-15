"""SpringServe API authentication module.

Manages token-based authentication with the SpringServe REST API.
Supports automatic re-authentication on HTTP 401 (expired token).

Validates: Requirements 1.2, 1.3, 1.5, 1.6
"""

import requests


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

    def request(self, method, path, **kwargs):
        """Make an HTTP request with automatic re-auth on 401.

        If the response is HTTP 401 and a token was previously set,
        re-authenticates exactly once and retries the original call.
        Raises requests.HTTPError if the retry also fails.
        """
        url = f"{self.base_url}{path}"
        resp = self.session.request(method, url, **kwargs)
        if resp.status_code == 401 and self.token:
            self.authenticate()
            resp = self.session.request(method, url, **kwargs)
        resp.raise_for_status()
        return resp
