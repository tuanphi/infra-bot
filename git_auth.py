"""Provide GitLab HTTPS credentials to Git without embedding tokens in URLs."""

import os
import shlex
import sys
from pathlib import Path
from urllib.parse import urlsplit


def _repo_path(path):
    path = path.strip("/")
    return path[:-4] if path.endswith(".git") else path


def repository_identity(url):
    parsed = urlsplit(url)
    if parsed.scheme in ("http", "https"):
        return parsed.scheme, parsed.netloc.lower(), _repo_path(parsed.path)
    return url.rstrip("/")


def git_environment(repo_url, token):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    args = ["git"]
    parsed = urlsplit(repo_url)
    if parsed.scheme == "https":
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("GIT_REPO_URL must not contain credentials")
        if not parsed.netloc or not _repo_path(parsed.path):
            raise ValueError("GIT_REPO_URL must identify an HTTPS repository")
        if not token or "\n" in token or "\r" in token:
            raise ValueError("GITLAB_TOKEN must be a non-empty single-line value")
        helper = "!%s %s" % (
            shlex.quote(sys.executable), shlex.quote(str(Path(__file__).resolve()))
        )
        args += [
            "-c", "credential.helper=",
            "-c", "credential.helper=" + helper,
            "-c", "credential.useHttpPath=true",
        ]
        env.update({
            "GIT_ASKPASS": "",
            "INFRA_GIT_CREDENTIAL_HOST": parsed.netloc.lower(),
            "INFRA_GIT_CREDENTIAL_PATH": _repo_path(parsed.path),
            "INFRA_GIT_CREDENTIAL_TOKEN": token,
        })
    return args, env


def credential_helper():
    # Git calls the helper with get/store/erase. Only get returns credentials.
    if len(sys.argv) != 2 or sys.argv[1] != "get":
        return
    request = {}
    for line in sys.stdin:
        line = line.rstrip("\r\n")
        if not line:
            break
        key, separator, value = line.partition("=")
        if separator:
            request[key] = value
    if (
        request.get("protocol") != "https"
        or request.get("host", "").lower() != os.getenv("INFRA_GIT_CREDENTIAL_HOST")
        or _repo_path(request.get("path", "")) != os.getenv("INFRA_GIT_CREDENTIAL_PATH")
    ):
        return
    token = os.getenv("INFRA_GIT_CREDENTIAL_TOKEN", "")
    if token and "\n" not in token and "\r" not in token:
        sys.stdout.write("username=oauth2\npassword=%s\n\n" % token)


if __name__ == "__main__":
    credential_helper()
