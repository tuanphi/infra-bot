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
from typing import Optional
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from config import Settings
from git_auth import authenticated_repo_url, git_environment, redact_git_output


PLAYBOOK = "gitlab-repos.yaml"
INVENTORY = "nonprod"
TAG = "project_user_access"
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$")
ACCESS_LEVEL_ROLES = {
    0: "no access", 5: "minimal access", 10: "guest", 15: "planner",
    20: "reporter", 25: "security manager", 30: "developer",
    40: "maintainer", 50: "owner",
}
_MEMBER_NOT_FOUND = object()


class WorkflowError(Exception):
    """A stopped run; its stage and concise explanation are safe for Telegram."""


@dataclass(frozen=True)
class Result:
    branch: str
    commit: str
    mr_url: str
    recap: str


@dataclass(frozen=True)
class ProjectUserAccess:
    user_id: int
    username: str
    name: str
    access_level: Optional[int] = None
    expires_at: Optional[str] = None

    @property
    def role(self):
        if self.access_level is None:
            return ""
        return ACCESS_LEVEL_ROLES.get(self.access_level, "unknown (%s)" % self.access_level)


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


def git_command(settings, args, cwd=None, timeout=120):
    try:
        prefix, env = git_environment(
            settings.git_repo_url, settings.gitlab_token, settings.gitlab_username,
        )
    except ValueError as exc:
        raise WorkflowError("Cấu hình Git HTTPS không hợp lệ: %s" % exc) from None
    try:
        return _command(prefix + list(args), cwd=cwd, timeout=timeout, env=env)
    except WorkflowError as exc:
        # TimeoutExpired contains argv, which may include the authenticated clone URL.
        raise WorkflowError(redact_git_output(str(exc), settings.gitlab_token)) from None


def clone_repository(settings, repo, description="repo Ansible"):
    try:
        url = authenticated_repo_url(
            settings.git_repo_url, settings.gitlab_username, settings.gitlab_token,
        )
    except ValueError as exc:
        raise WorkflowError("Cấu hình Git HTTPS không hợp lệ: %s" % exc) from None
    try:
        code, output = git_command(settings, [
            "clone", "--no-tags", "--single-branch", "--branch",
            settings.git_target_branch, "--", url, str(repo),
        ], timeout=settings.git_clone_timeout_seconds)
    finally:
        # Git persists the clone URL. Remove credentials even on partial failure.
        if (Path(repo) / ".git").is_dir():
            _git(repo, "remote", "set-url", "origin", settings.git_repo_url, settings=settings)
    if code:
        detail = redact_git_output(output, settings.gitlab_token).strip()[-1200:]
        raise WorkflowError("Không clone được %s (exit %s).\n%s" % (
            description, code, detail or "Git không trả về chi tiết lỗi.",
        ))


def _git(repo, *args, settings=None, timeout=120):
    if settings is None:
        code, output = _command(["git", "-C", str(repo), *args], timeout=timeout)
    else:
        code, output = git_command(settings, ["-C", str(repo), *args], timeout=timeout)
    if code != 0:
        token = settings.gitlab_token if settings else ""
        detail = redact_git_output(output, token).strip()[-1200:]
        raise WorkflowError("Git thất bại ở bước %s (exit %s).\n%s" % (
            args[0], code, detail or "Git không trả về chi tiết lỗi.",
        ))
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

    def _access_json(self, path, allow_missing_member=False):
        url = self.settings.gitlab_url.rstrip("/") + "/api/v4/" + path
        try:
            return self._request_json(url, {"PRIVATE-TOKEN": self.settings.gitlab_token})
        except HTTPError as exc:
            if allow_missing_member and exc.code == 404:
                return _MEMBER_NOT_FOUND
            notes = {
                401: "Token GitLab không hợp lệ hoặc đã hết hạn.",
                403: "Token không có quyền đọc user/project/membership.",
                404: "User/project không tồn tại hoặc token không có quyền xem.",
            }
            raise WorkflowError(
                "Không kiểm tra được role qua GitLab API (HTTP %s). %s"
                % (exc.code, notes.get(exc.code, "Thử lại sau hoặc kiểm tra GitLab."))
            ) from None
        except (URLError, OSError, ValueError, TypeError) as exc:
            # Do not expose URLs, tokens, or untrusted response bodies.
            raise WorkflowError(
                "Không kiểm tra được role qua GitLab API (%s); kiểm tra kết nối, CA và phản hồi JSON."
                % type(exc).__name__
            ) from None

    def project_user_access(self, project_path, username):
        users = self._access_json("users?" + urlencode({"username": username}))
        if not isinstance(users, list):
            raise WorkflowError("GitLab API trả danh sách user không hợp lệ.")
        matches = [user for user in users if isinstance(user, dict)
                   and isinstance(user.get("username"), str)
                   and user["username"].casefold() == username.casefold()]
        if not matches:
            raise WorkflowError("Không tìm thấy user %s trên GitLab." % username)
        if len(matches) != 1:
            raise WorkflowError("GitLab API trả nhiều user trùng username; dừng kiểm tra role.")
        user = matches[0]
        user_id = user.get("id")
        if type(user_id) is not int or user_id <= 0 or not isinstance(user.get("name"), str):
            raise WorkflowError("GitLab API trả thông tin user không hợp lệ.")

        # The service path comes from YAML, not the Ansible MR project ID.
        project_resource = "projects/" + quote(project_path, safe="")
        member = self._access_json(
            "%s/members/all/%s" % (project_resource, user_id), allow_missing_member=True,
        )
        if member is _MEMBER_NOT_FOUND:
            # A membership 404 may also hide an inaccessible/nonexistent project.
            project = self._access_json(project_resource)
            if (not isinstance(project, dict) or type(project.get("id")) is not int
                    or project["id"] <= 0):
                raise WorkflowError("GitLab API trả thông tin project không hợp lệ.")
            return ProjectUserAccess(user_id, user["username"], user["name"])

        if (not isinstance(member, dict) or type(member.get("id")) is not int
                or member["id"] != user_id or not isinstance(member.get("username"), str)
                or member["username"].casefold() != user["username"].casefold()
                or type(member.get("access_level")) is not int or member["access_level"] < 0
                or (member.get("expires_at") is not None
                    and not isinstance(member["expires_at"], str))):
            raise WorkflowError("GitLab API trả thông tin membership không hợp lệ; dừng kiểm tra role.")
        return ProjectUserAccess(
            user_id, user["username"], user["name"], member["access_level"], member.get("expires_at"),
        )

    def create_or_get_mr(self, branch, commit, requester, recap, *,
                         title=None, command=None, target_branch=None):
        target_branch = target_branch or self.settings.git_target_branch
        command = command or "ansible-playbook -i nonprod gitlab-repos.yaml --tags=project_user_access"
        base = "%s/api/v4/projects/%s/merge_requests" % (
            self.settings.gitlab_url, quote(self.settings.gitlab_project_id, safe="")
        )
        headers = {"PRIVATE-TOKEN": self.settings.gitlab_token}
        query = {
            "state": "opened", "source_branch": branch,
            "target_branch": target_branch,
        }
        try:
            matches = self._request_json(base + "?" + urlencode(query), headers)
            if matches:
                return matches[0]["web_url"]

            description = (
                "Ansible execution passed before opening this MR.\n\n"
                "Command: `%s`\n"
                "Commit tested: `%s`\nRequested by Telegram user ID: `%s`\n"
                "Completed (UTC): %s\n\n```\n%s\n```"
                % (command, commit, requester, datetime.now(timezone.utc).isoformat(), recap)
            )
            created = self._request_json(
                base, headers,
                payload={
                    "source_branch": branch,
                    "target_branch": target_branch,
                    "title": title or "GitLab project_user_access: %s" % branch,
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
            clone_repository(self.settings, repo)

            source_ref = "refs/remotes/origin/" + branch
            target_ref = "refs/remotes/origin/" + base_branch
            _git(repo, "fetch", "--no-tags", "origin", "+refs/heads/%s:%s" % (branch, source_ref), settings=self.settings)
            commit = _git(repo, "rev-parse", "--verify", source_ref + "^{commit}", settings=self.settings)
            base_commit = _git(repo, "rev-parse", "--verify", target_ref + "^{commit}", settings=self.settings)
            code, _ = git_command(self.settings, [
                "-C", str(repo), "diff", "--quiet",
                base_commit + "..." + commit,
            ])
            if code == 0:
                raise WorkflowError("Branch không có thay đổi so với %s; không thể tạo MR có diff." % base_branch)
            if code != 1:
                raise WorkflowError("Không so sánh được source branch với branch đích.")

            _git(repo, "checkout", "--detach", commit, settings=self.settings)
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
            remote = _git(repo, "ls-remote", "--exit-code", "origin", "refs/heads/" + branch, settings=self.settings)
            if not remote or remote.split()[0] != commit:
                raise WorkflowError("Branch đã đổi commit trong lúc chạy; dừng trước khi tạo MR.")

            mr_url = self.mr_client.create_or_get_mr(branch, commit, requester, recap)
            return Result(branch=branch, commit=commit, mr_url=mr_url, recap=recap)
