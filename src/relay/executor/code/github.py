"""The GitHub REST calls a code task needs: find or create the repository, open a DRAFT PR.

Deliberately nothing else: no merge, no review approval, no branch deletion, no default-branch
updates. The token only ever leaves the worker in these requests and in the push's
``Authorization`` header (see :func:`push_auth_env`).
"""

from __future__ import annotations

import base64
import re
from typing import Any

import httpx

TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_REPO_URL = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?$"
)


class GitHubError(RuntimeError):
    pass


def parse_repo(url: str | None) -> str | None:
    """``owner/name`` of a github.com remote URL (https or ssh form); None otherwise."""
    m = _REPO_URL.match(url or "")
    return f"{m['owner']}/{m['name']}" if m else None


def https_url(full_name: str) -> str:
    return f"https://github.com/{full_name}.git"


def push_auth_env(token: str) -> dict[str, str]:
    """Env giving ``git push`` the token as an HTTP header for github.com only (not in argv)."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }


class GitHub:
    def __init__(self, token: str, api_url: str = "https://api.github.com") -> None:
        self._client = httpx.Client(
            base_url=api_url.rstrip("/"),
            timeout=TIMEOUT,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def __enter__(self) -> GitHub:
        return self

    def __exit__(self, *exc: object) -> None:
        self._client.close()

    def _call(self, method: str, path: str, **kw: Any) -> Any:
        r = self._client.request(method, path, **kw)
        if r.is_error:
            raise GitHubError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def get_repo(self, full_name: str) -> dict[str, Any] | None:
        r = self._client.get(f"/repos/{full_name}")
        if r.status_code == 404:
            return None
        if r.is_error:
            raise GitHubError(f"GET /repos/{full_name} -> {r.status_code}: {r.text[:300]}")
        return dict(r.json())

    def ensure_private_repo(self, owner: str, name: str) -> dict[str, Any]:
        """``owner/name`` if it exists, else a new PRIVATE repository (user or organisation)."""
        if owner:
            found = self.get_repo(f"{owner}/{name}")
            if found is not None:
                return found
        login = str(self._call("GET", "/user")["login"])
        path = "/user/repos" if owner in ("", login) else f"/orgs/{owner}/repos"
        return dict(self._call("POST", path, json={"name": name, "private": True}))

    def open_draft_pr(
        self, full_name: str, head: str, base: str, title: str, body: str
    ) -> dict[str, Any]:
        """The open PR for ``head`` if there is one (a replayed step), else a new DRAFT PR."""
        owner = full_name.split("/", 1)[0]
        params = {"head": f"{owner}:{head}", "state": "open"}
        existing = self._call("GET", f"/repos/{full_name}/pulls", params=params)
        if existing:
            return dict(existing[0])
        payload = {"title": title, "head": head, "base": base, "body": body, "draft": True}
        return dict(self._call("POST", f"/repos/{full_name}/pulls", json=payload))
