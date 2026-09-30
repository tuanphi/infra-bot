"""Run an already committed Ansible branch, then open its GitLab MR."""

import os
import json
import re
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError

from config import Settings


PLAYBOOK = "gitlab-repos.yaml"
INVENTORY = "nonprod"
TAG = "project_user_access"
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$")


class WorkflowError(Exception):
    """A stopped run; its stage and concise explanation are safe for Telegram."""


@dataclass(frozen=True)
class Result:
    branch: str
    commit: str
    mr_url: str
    recap: str


def validate_branch(branch):
    # No options, ref expressions, traversal-like sequences, or ambiguous refs.
    if not BRANCH_RE.fullmatch(branch) or ".." in branch or "//" in branch or branch.endswith((".", "/")):
        raise WorkflowError("Tên branch không hợp lệ.")
    if any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/")):
        raise WorkflowError("Tên branch không hợp lệ.")


def _command(args, cwd=None, timeout=120, env=None):
    try:
        proc = subprocess.Popen(
            args, cwd=cwd, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace",
            start_new_session=True,
        )
    except OSError as exc:
        raise WorkflowError("Không khởi chạy được %s: %s" % (args[0], exc)) from exc
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise WorkflowError("Lệnh %s vượt quá %s giây." % (args[0], timeout)) from exc
    return proc.returncode, output


def _git(repo, *args):
    code, output = _command(["git", "-C", str(repo), *args])
    if code != 0:
        # Git output may contain URLs with embedded credentials; do not relay it.
        raise WorkflowError("Git thất bại ở bước %s (exit %s)." % (args[0], code))
    return output.strip()


def _recap(output):
    plain = re.sub(r"\x1b\[[0-9;]*m", "", output)
    match = re.search(r"(?m)^PLAY RECAP\s*\*+.*$", plain)
    if match:
        lines = ["PLAY RECAP"]
        for line in plain[match.end():].splitlines():
            if re.match(r"^\S+\s+:\s+ok=\d+\s+changed=\d+\s+", line):
                lines.append(line.strip())
        if len(lines) > 1:
            return "\n".join(lines)[:1800]
    return ""


class GitLabClient:
    def __init__(self, settings, opener=None):
        self.settings = settings
        self.opener = opener or urlopen

    def _request_json(self, url, headers, payload=None):
        req = Request(
            url, data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={**headers, **({"Content-Type": "application/json"} if payload is not None else {})},
            method="POST" if payload is not None else "GET",
        )
        with self.opener(req, timeout=30) as response:
            return json.load(response)

    def create_or_get_mr(self, branch, commit, requester, recap):
        base = "%s/api/v4/projects/%s/merge_requests" % (
            self.settings.gitlab_url, quote(self.settings.gitlab_project_id, safe="")
        )
        headers = {"PRIVATE-TOKEN": self.settings.gitlab_token}
        query = {
            "state": "opened", "source_branch": branch,
            "target_branch": self.settings.git_target_branch,
        }
        try:
            matches = self._request_json(base + "?" + urlencode(query), headers)
            if matches:
                return matches[0]["web_url"]

            description = (
                "Ansible execution passed before opening this MR.\n\n"
                "Command: `ansible-playbook -i nonprod gitlab-repos.yaml --tags=project_user_access`\n"
                "Commit tested: `%s`\nRequested by Telegram user ID: `%s`\n"
                "Completed (UTC): %s\n\n```\n%s\n```"
                % (commit, requester, datetime.now(timezone.utc).isoformat(), recap)
            )
            created = self._request_json(
                base, headers,
                payload={
                    "source_branch": branch,
                    "target_branch": self.settings.git_target_branch,
                    "title": "GitLab project_user_access: %s" % branch,
                    "description": description,
                    "remove_source_branch": True,
                },
            )
            return created["web_url"]
        except (URLError, ValueError, KeyError, TypeError, OSError) as exc:
            # An HTTP error may include an untrusted server response; report status only.
            status = getattr(exc, "code", None)
            detail = "HTTP %s" % status if status else type(exc).__name__
            raise WorkflowError(
                "Ansible đã thành công nhưng không tạo/đọc được GitLab MR (%s)." % detail
            ) from exc


class Pipeline:
    def __init__(self, settings: Settings, mr_client=None):
        self.settings = settings
        self.mr_client = mr_client or GitLabClient(settings)

    def run(self, branch: str, requester: int) -> Result:
        validate_branch(branch)
        base_branch = self.settings.git_target_branch
        validate_branch(base_branch)
        if branch == base_branch:
            raise WorkflowError("Branch nguồn phải khác branch đích.")

        with tempfile.TemporaryDirectory(prefix="infra-ansible-bot-") as temp:
            repo = Path(temp) / "infra"
            code, _ = _command([
                "git", "clone", "--no-tags", "--single-branch", "--branch",
                base_branch, self.settings.git_repo_url, str(repo),
            ], timeout=180)
            if code:
                raise WorkflowError("Không clone được repo Ansible (exit %s)." % code)

            source_ref = "refs/remotes/origin/" + branch
            target_ref = "refs/remotes/origin/" + base_branch
            _git(repo, "fetch", "--no-tags", "origin", "+refs/heads/%s:%s" % (branch, source_ref))
            commit = _git(repo, "rev-parse", "--verify", source_ref + "^{commit}")
            base_commit = _git(repo, "rev-parse", "--verify", target_ref + "^{commit}")
            code, _ = _command([
                "git", "-C", str(repo), "diff", "--quiet",
                base_commit + "..." + commit,
            ])
            if code == 0:
                raise WorkflowError("Branch không có thay đổi so với %s; không thể tạo MR có diff." % base_branch)
            if code != 1:
                raise WorkflowError("Không so sánh được source branch với branch đích.")

            _git(repo, "switch", "--detach", commit)
            if not (repo / INVENTORY).is_file() or not (repo / PLAYBOOK).is_file():
                raise WorkflowError("Branch thiếu file nonprod hoặc gitlab-repos.yaml ở thư mục gốc.")

            # The fixed argv is intentionally identical to the requested command.
            ansible_args = [
                self.settings.ansible_binary, "-i", INVENTORY, PLAYBOOK,
                "--tags=" + TAG,
            ]
            env = os.environ.copy()
            env["ANSIBLE_NOCOLOR"] = "1"
            code, output = _command(
                ansible_args, cwd=repo, timeout=self.settings.ansible_timeout_seconds, env=env
            )
            recap = _recap(output)
            if code:
                raise WorkflowError(
                    "Ansible thất bại (exit %s). %s" % (code, recap or "Không có PLAY RECAP.")
                )
            if not recap:
                raise WorkflowError("Ansible exit 0 nhưng không có PLAY RECAP hợp lệ; dừng trước MR.")

            # A moved branch would make the MR review a different commit.
            remote = _git(repo, "ls-remote", "--exit-code", "origin", "refs/heads/" + branch)
            if not remote or remote.split()[0] != commit:
                raise WorkflowError("Branch đã đổi commit trong lúc chạy; dừng trước khi tạo MR.")

            mr_url = self.mr_client.create_or_get_mr(branch, commit, requester, recap)
            return Result(branch=branch, commit=commit, mr_url=mr_url, recap=recap)
