"""Build GitLab HTTPS clone URLs and authenticate later Git operations."""

import os
import re
import shlex
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit


def _repo_path(path):
    path = path.strip("/")
    return path[:-4] if path.endswith(".git") else path


def repository_identity(url):
    parsed = urlsplit(url)
    if parsed.scheme in ("http", "https"):
        return parsed.scheme, parsed.netloc.lower(), _repo_path(parsed.path)
    return url.rstrip("/")


def authenticated_repo_url(repo_url, username, token):
    parsed = urlsplit(repo_url)
    if parsed.scheme != "https":
        return repo_url
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("GIT_REPO_URL must not contain credentials; use GITLAB_USERNAME/GITLAB_TOKEN")
    if not parsed.netloc or not _repo_path(parsed.path) or parsed.query or parsed.fragment:
        raise ValueError("GIT_REPO_URL must identify an HTTPS repository without query/fragment")
    for name, value in (("GITLAB_USERNAME", username), ("GITLAB_TOKEN", token)):
        if not value or not value.strip() or any(char in value for char in "\r\n\x00"):
            raise ValueError("%s must be a non-empty single-line value" % name)
    credentials = "%s:%s@%s" % (
        quote(username, safe=""), quote(token, safe=""), parsed.netloc,
    )
    return urlunsplit((parsed.scheme, credentials, parsed.path, "", ""))


def redact_git_output(output, token=""):
    # Redact before truncating, including percent-encoded token variants.
    if token:
        encoded = quote(token, safe="")
        lower_escapes = re.sub(r"%[0-9A-F]{2}", lambda match: match.group().lower(), encoded)
        for secret in sorted({token, encoded, lower_escapes}, key=len, reverse=True):
            output = output.replace(secret, "***")
    return re.sub(r"(https?://)[^\s/]+@", r"\1***@", output, flags=re.IGNORECASE)


def git_environment(repo_url, token, username=""):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    args = ["git"]
    parsed = urlsplit(repo_url)
    if parsed.scheme == "https":
        authenticated_repo_url(repo_url, username, token)
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
            "INFRA_GIT_CREDENTIAL_USERNAME": username,
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
    username = os.getenv("INFRA_GIT_CREDENTIAL_USERNAME", "")
    token = os.getenv("INFRA_GIT_CREDENTIAL_TOKEN", "")
    if username and token and not any(char in username + token for char in "\r\n\x00"):
        sys.stdout.write("username=%s\npassword=%s\n\n" % (username, token))


if __name__ == "__main__":
    credential_helper()
