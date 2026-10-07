"""Configuration for the Telegram-to-Ansible runner."""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def load_env_file(path="/mnt/secrets/.env", override=False):
    """Load simple one-line KEY=VALUE assignments into os.environ.

    Accept full-line comments, optional export, and matching outer quotes.
    Values are literal: no interpolation, escape decoding, inline comments,
    or multiline values. Validate the whole file before changing the environment.
    Existing environment values win unless override is True.
    """
    path = Path(path)
    values = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        assignment = re.fullmatch(
            r"(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)", line
        )
        if assignment is None:
            raise ValueError("Invalid .env assignment at %s:%s" % (path, number))

        key, value = assignment.groups()
        value = value.strip()
        if value[:1] in ("'", '"'):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError("Unclosed .env quote at %s:%s" % (path, number))
            value = value[1:-1]
        if "\x00" in value:
            raise ValueError("Invalid .env value at %s:%s" % (path, number))
        values[key] = value

    for key, value in values.items():
        if override:
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    allowed_group_id: int
    allowed_user_ids: frozenset
    git_repo_url: str
    git_target_branch: str
    gitlab_url: str
    gitlab_project_id: str
    gitlab_token: str
    ansible_timeout_seconds: int = 1800
    ansible_binary: str = "ansible-playbook"
    health_port: int = 8080
    git_clone_dir: str = "/app/infra"
    git_clone_timeout_seconds: int = 180
    gitlab_username: str = ""
    allow_private_chat: bool = False

    @classmethod
    def from_env(cls):
        required = (
            "TELEGRAM_TOKEN", "ALLOWED_GROUP_ID", "ALLOWED_USER_IDS",
            "GIT_REPO_URL", "GIT_TARGET_BRANCH", "GITLAB_URL",
            "GITLAB_PROJECT_ID", "GITLAB_TOKEN",
        )
        if urlsplit(os.getenv("GIT_REPO_URL", "")).scheme == "https":
            required += ("GITLAB_USERNAME",)
        missing = [name for name in required if not os.environ.get(name, "").strip()]
        if missing:
            raise ValueError("Missing configuration: " + ", ".join(missing))

        try:
            ids = frozenset(
                int(item.strip()) for item in os.environ["ALLOWED_USER_IDS"].split(",")
                if item.strip()
            )
        except ValueError:
            raise ValueError("ALLOWED_USER_IDS must contain numeric Telegram user IDs separated by commas") from None
        if not ids:
            raise ValueError("ALLOWED_USER_IDS must contain at least one Telegram user ID")
        try:
            group_id = int(os.environ["ALLOWED_GROUP_ID"])
        except ValueError:
            raise ValueError("ALLOWED_GROUP_ID must be a numeric Telegram group ID") from None
        private_chat = os.getenv("ALLOW_PRIVATE_CHAT", "false").strip().lower()
        if private_chat not in ("true", "false", "1", "0"):
            raise ValueError("ALLOW_PRIVATE_CHAT must be true, false, 1 or 0")
        timeout = int(os.getenv("ANSIBLE_TIMEOUT_SECONDS", "1800"))
        if timeout <= 0:
            raise ValueError("ANSIBLE_TIMEOUT_SECONDS must be positive")
        health_port = int(os.getenv("HEALTH_PORT", "8080"))
        if not 1 <= health_port <= 65535:
            raise ValueError("HEALTH_PORT must be between 1 and 65535")
        clone_timeout = int(os.getenv("GIT_CLONE_TIMEOUT_SECONDS", "180"))
        if clone_timeout <= 0:
            raise ValueError("GIT_CLONE_TIMEOUT_SECONDS must be positive")
        clone_dir = os.getenv("GIT_CLONE_DIR", "/app/infra").strip()
        if not Path(clone_dir).is_absolute():
            raise ValueError("GIT_CLONE_DIR must be an absolute path")
        return cls(
            telegram_token=os.environ["TELEGRAM_TOKEN"],
            allowed_group_id=group_id,
            allowed_user_ids=ids,
            git_repo_url=os.environ["GIT_REPO_URL"],
            git_target_branch=os.environ["GIT_TARGET_BRANCH"],
            gitlab_url=os.environ["GITLAB_URL"].rstrip("/"),
            gitlab_project_id=os.environ["GITLAB_PROJECT_ID"],
            gitlab_token=os.environ["GITLAB_TOKEN"],
            gitlab_username=os.getenv("GITLAB_USERNAME", "").strip(),
            allow_private_chat=private_chat in ("true", "1"),
            ansible_timeout_seconds=timeout,
            ansible_binary=os.getenv("ANSIBLE_PLAYBOOK_BIN", "ansible-playbook"),
            health_port=health_port,
            git_clone_dir=clone_dir,
            git_clone_timeout_seconds=clone_timeout,
        )
