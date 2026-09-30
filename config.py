"""Configuration for the Telegram-to-Ansible runner."""

import os
from dataclasses import dataclass


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

    @classmethod
    def from_env(cls):
        required = (
            "TELEGRAM_TOKEN", "ALLOWED_GROUP_ID", "ALLOWED_USER_IDS",
            "GIT_REPO_URL", "GIT_TARGET_BRANCH", "GITLAB_URL",
            "GITLAB_PROJECT_ID", "GITLAB_TOKEN",
        )
        missing = [name for name in required if not os.environ.get(name, "").strip()]
        if missing:
            raise ValueError("Missing configuration: " + ", ".join(missing))

        ids = frozenset(
            int(item.strip()) for item in os.environ["ALLOWED_USER_IDS"].split(",")
            if item.strip()
        )
        if not ids:
            raise ValueError("ALLOWED_USER_IDS must contain at least one Telegram user ID")
        timeout = int(os.getenv("ANSIBLE_TIMEOUT_SECONDS", "1800"))
        if timeout <= 0:
            raise ValueError("ANSIBLE_TIMEOUT_SECONDS must be positive")
        return cls(
            telegram_token=os.environ["TELEGRAM_TOKEN"],
            allowed_group_id=int(os.environ["ALLOWED_GROUP_ID"]),
            allowed_user_ids=ids,
            git_repo_url=os.environ["GIT_REPO_URL"],
            git_target_branch=os.environ["GIT_TARGET_BRANCH"],
            gitlab_url=os.environ["GITLAB_URL"].rstrip("/"),
            gitlab_project_id=os.environ["GITLAB_PROJECT_ID"],
            gitlab_token=os.environ["GITLAB_TOKEN"],
            ansible_timeout_seconds=timeout,
            ansible_binary=os.getenv("ANSIBLE_PLAYBOOK_BIN", "ansible-playbook"),
        )
