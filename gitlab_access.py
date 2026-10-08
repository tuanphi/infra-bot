"""Edit one GitLab service's users_access and run the requested Ghub playbook."""

import copy
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import yaml
from yaml.nodes import MappingNode, SequenceNode
from yaml.tokens import AliasToken

from git_auth import redact_git_output
from workflow import GitLabClient, ProjectUserAccess, WorkflowError, _command, _git, _recap, git_command


ACCESS_FILE = "group_vars/gitlab-ghub"
PLAYBOOK = "gitlab-repos-ghub.yaml"
INVENTORY = "nonprod"
REQUIRED_BRANCH = "master"
ROLES = ("maintainer", "developer", "reporter", "guest")
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
USERNAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}$")


class UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise WorkflowError("YAML có key không hợp lệ hoặc bị trùng.")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


@dataclass(frozen=True)
class AccessRequest:
    namespace: str
    service: str
    username: str
    role: str
    current_roles: tuple
    head: str
    source: bytes
    project_path: str = ""
    access: Optional[ProjectUserAccess] = None


@dataclass(frozen=True)
class GitReport:
    status: str
    diff: str
    porcelain: str
    branch: str
    head: str


class GitCheckError(WorkflowError):
    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


@dataclass(frozen=True)
class AppliedChange:
    request: AccessRequest
    content: bytes
    report: GitReport


@dataclass(frozen=True)
class AccessResult:
    command: tuple
    recap: str


@dataclass
class Finalization:
    action: str = ""
    branch: str = ""
    commit: str = ""
    push_confirmed: bool = False
    pushed: bool = False
    mr_url: str = ""
    restored: bool = False


@dataclass(frozen=True)
class MergeRequestInfo:
    branch: str
    difference: str
    commit: str
    message: str


def _node_value(node, key):
    if not isinstance(node, MappingNode):
        raise WorkflowError("Cấu trúc YAML không hợp lệ.")
    for key_node, value in node.value:
        if key_node.value == key:
            return value
    raise WorkflowError("YAML thiếu key %s." % key)


def _load(source):
    try:
        text = source.decode("utf-8")
        # An alias could make a change to one list affect several services.
        if any(isinstance(token, AliasToken) for token in yaml.scan(text)):
            raise WorkflowError("Không tự sửa YAML có alias; cần danh sách users riêng cho từng role.")
        document = yaml.load(text, Loader=UniqueKeyLoader)
        nodes = yaml.compose(text, Loader=UniqueKeyLoader)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise WorkflowError("Không đọc được YAML trong %s." % ACCESS_FILE) from exc
    if not isinstance(document, dict):
        raise WorkflowError("File %s phải chứa một YAML mapping." % ACCESS_FILE)
    return text, document, nodes


def _namespace(document, namespace):
    if not SLUG.fullmatch(namespace):
        raise WorkflowError("Tên namespace không hợp lệ.")
    groups = document.get("gitlab_groups")
    projects = document.get("gitlab_projects")
    if not isinstance(groups, list) or not isinstance(projects, dict):
        raise WorkflowError("YAML thiếu gitlab_groups hoặc gitlab_projects.")
    matches = [group for group in groups if isinstance(group, dict) and group.get("path") == namespace]
    if len(matches) != 1 or namespace not in projects:
        raise WorkflowError("Namespace %s không tồn tại duy nhất trong gitlab_groups/gitlab_projects." % namespace)
    group = matches[0]
    parent = group.get("parent")
    entry = projects[namespace]
    if not isinstance(parent, str) or not parent or not isinstance(entry, dict):
        raise WorkflowError("Cấu hình namespace %s không hợp lệ." % namespace)
    services = entry.get("project")
    if not isinstance(services, list):
        raise WorkflowError("Namespace %s thiếu danh sách project." % namespace)
    return parent.rstrip("/") + "/" + namespace, services


def _project(document, nodes, namespace, service):
    if not SLUG.fullmatch(service):
        raise WorkflowError("Tên service không hợp lệ; nhập phần cuối của project.path.")
    group_path, services = _namespace(document, namespace)
    expected_path = group_path + "/" + service
    matches = [(i, project) for i, project in enumerate(services)
               if isinstance(project, dict) and project.get("path") == expected_path]
    if len(matches) != 1:
        raise WorkflowError("Service %s không tồn tại duy nhất trong namespace %s." % (service, namespace))
    index, project = matches[0]
    project_nodes = _node_value(_node_value(_node_value(nodes, "gitlab_projects"), namespace), "project")
    if not isinstance(project_nodes, SequenceNode):
        raise WorkflowError("project phải là một YAML list.")
    return project, project_nodes.value[index]


def _access(project, project_node):
    entries = project.get("users_access")
    if not isinstance(entries, list):
        raise WorkflowError("Service thiếu danh sách users_access.")
    entry_nodes = _node_value(project_node, "users_access")
    if not isinstance(entry_nodes, SequenceNode):
        raise WorkflowError("users_access phải là một YAML list.")
    access = {}
    for entry, node in zip(entries, entry_nodes.value):
        if not isinstance(entry, dict):
            raise WorkflowError("users_access có entry không hợp lệ.")
        role, users = entry.get("level"), entry.get("users")
        if role not in ROLES or role in access:
            raise WorkflowError("users_access có role không hỗ trợ hoặc bị trùng.")
        if not isinstance(users, list) or any(not isinstance(user, str) for user in users):
            raise WorkflowError("users của role %s phải là danh sách username." % role)
        if len(set(users)) != len(users):
            raise WorkflowError("Danh sách users của role %s có user bị trùng." % role)
        users_node = _node_value(node, "users")
        if not isinstance(users_node, SequenceNode) or not users_node.flow_style:
            raise WorkflowError("users phải dùng dạng inline list: [\"user1\", \"user2\"].")
        access[role] = (entry, users_node)
    if set(access) != set(ROLES):
        raise WorkflowError("users_access phải có đủ maintainer, developer, reporter, guest.")
    return access


def _updated_content(request):
    text, document, nodes = _load(request.source)
    project, project_node = _project(document, nodes, request.namespace, request.service)
    access = _access(project, project_node)
    expected = copy.deepcopy(document)
    expected_project, _ = _project(expected, nodes, request.namespace, request.service)
    expected_access = {entry["level"]: entry for entry in expected_project["users_access"]}
    replacements = []
    for role, (entry, node) in access.items():
        users = entry["users"]
        if role == request.role:
            updated = list(users) if request.username in users else users + [request.username]
        else:
            updated = [user for user in users if user != request.username]
        expected_access[role]["users"] = updated
        if updated != users:
            replacements.append((node.start_mark.index, node.end_mark.index,
                                 json.dumps(updated, ensure_ascii=False)))
    # Replace only the selected service's user lists; retain every other byte.
    for start, end, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[end:]
    content = text.encode("utf-8")
    if _load(content)[1] != expected:
        raise WorkflowError("Bản sửa YAML có thay đổi ngoài user/role đã chọn; dừng luồng.")
    return content


def _atomic_write(path, content):
    temporary = None
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".gitlab-access-", delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


class GitLabAccessWorkflow:
    def __init__(self, settings, mr_client=None, access_client=None):
        self.settings = settings
        self.repo = Path(settings.git_clone_dir).resolve()
        self.path = self.repo / ACCESS_FILE
        self.mr_client = mr_client or GitLabClient(settings)
        self.access_client = access_client or GitLabClient(settings)

    def _git_output(self, *args):
        code, output = git_command(self.settings, [
            "-C", str(self.repo), "-c", "color.ui=false", "--no-pager", *args,
        ])
        output = redact_git_output(output, self.settings.gitlab_token)
        if code:
            raise WorkflowError("Git thất bại (exit %s).\n%s" % (code, output[-3000:]))
        return output

    def _require_files(self):
        root = _git(self.repo, "rev-parse", "--show-toplevel", settings=self.settings)
        if Path(root).resolve() != self.repo:
            raise WorkflowError("GIT_CLONE_DIR phải là thư mục gốc của repo Ansible.")
        for name in (ACCESS_FILE, INVENTORY, PLAYBOOK):
            path = self.repo / name
            if not path.is_file() or path.is_symlink() or path.resolve() != path:
                raise WorkflowError("Repo thiếu file thường %s." % name)

    def report(self):
        return GitReport(
            status=self._git_output("status", "--untracked-files=all"),
            diff=self._git_output("diff", "--no-ext-diff", "--no-textconv", "--", ACCESS_FILE),
            porcelain=self._git_output("status", "--porcelain=v1", "-z", "--untracked-files=all"),
            branch=_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD", settings=self.settings),
            head=_git(self.repo, "rev-parse", "HEAD", settings=self.settings),
        )

    def _require_clean(self):
        self._require_files()
        report = self.report()
        if report.branch != REQUIRED_BRANCH or report.porcelain:
            raise GitCheckError(
                "Repo phải ở branch master và sạch trước lượt /gitlab. Xử lý diff hiện tại rồi thử lại.", report,
            )
        return report

    def namespaces(self):
        self._require_files()
        _, document, _ = _load(self.path.read_bytes())
        groups = document.get("gitlab_groups")
        if not isinstance(groups, list):
            raise WorkflowError("YAML thiếu danh sách gitlab_groups.")
        return tuple(group["path"] for group in groups
                     if isinstance(group, dict) and isinstance(group.get("path"), str))

    def services(self, namespace):
        self._require_files()
        _, document, _ = _load(self.path.read_bytes())
        group_path, projects = _namespace(document, namespace)
        return tuple(project["path"].rsplit("/", 1)[1] for project in projects
                     if isinstance(project, dict) and isinstance(project.get("path"), str)
                     and project["path"].rsplit("/", 1)[0] == group_path)

    def preview(self, namespace, service, username, role):
        if not USERNAME.fullmatch(username):
            raise WorkflowError("Tên user không hợp lệ; nhập username GitLab, ví dụ tuanpv.")
        if role not in ROLES:
            raise WorkflowError("Role không hợp lệ.")
        report = self._require_clean()
        source = self.path.read_bytes()
        _, document, nodes = _load(source)
        project, project_node = _project(document, nodes, namespace, service)
        _access(project, project_node)
        access = self.access_client.project_user_access(project["path"], username)
        roles = (access.role,) if access.role else ()
        return AccessRequest(
            namespace, service, access.username, role, roles, report.head, source,
            project_path=project["path"], access=access,
        )

    def rollback(self, change):
        # Do not overwrite someone else's subsequent edit, or reset other files.
        try:
            if not self.path.is_symlink() and self.path.read_bytes() == change.content:
                _atomic_write(self.path, change.request.source)
                return True
        except FileNotFoundError:
            # A file removed by someone else must stay removed.
            return False
        return False

    def _validate_change(self, change, report):
        if report.branch != REQUIRED_BRANCH or report.head != change.request.head:
            raise GitCheckError("Branch/commit đã đổi; dừng trước khi chạy Ansible.", report)
        if report.porcelain != " M " + ACCESS_FILE + "\x00":
            raise GitCheckError("Git status có thay đổi ngoài file dự kiến hoặc có thay đổi staged; dừng luồng.", report)
        if not report.diff.strip() or self.path.is_symlink() or self.path.read_bytes() != change.content:
            raise GitCheckError("Git diff không khớp đúng user/role đã xác nhận; dừng luồng.", report)

    def prepare(self, request):
        report = self._require_clean()
        if report.head != request.head or self.path.read_bytes() != request.source:
            raise GitCheckError("Cấu hình đã đổi sau khi chọn role; chạy /gitlab lại để xác nhận thông tin mới.", report)
        content = _updated_content(request)
        if content == request.source:
            raise GitCheckError(
                "YAML đã chứa user %s ở role %s; không có diff để chạy Ansible. "
                "Nếu role trên GitLab khác YAML, cần đồng bộ lại bằng playbook."
                % (request.username, request.role), report,
            )
        change = AppliedChange(request, content, report)
        _atomic_write(self.path, content)
        try:
            report = self.report()
            self._validate_change(change, report)
        except Exception:
            self.rollback(change)
            raise
        return AppliedChange(request, content, report)

    def run_playbook(self, change):
        # Recheck after sending the complete Git outputs to Telegram.
        try:
            self._require_files()
            self._validate_change(change, self.report())
        except Exception:
            self.rollback(change)
            raise
        return self._run_access_playbook(
            change.request, "Giữ diff YAML để kiểm tra; Ansible có thể đã áp dụng một phần.",
        )

    def _run_access_playbook(self, request, failure_note):
        args = (
            self.settings.ansible_binary, "-i", INVENTORY, PLAYBOOK,
            "--tags=" + request.namespace + ",project_user_access",
        )
        env = os.environ.copy()
        env["ANSIBLE_NOCOLOR"] = "1"
        try:
            code, output = _command(args, cwd=self.repo,
                                    timeout=self.settings.ansible_timeout_seconds, env=env)
        except WorkflowError as exc:
            raise WorkflowError("%s\n%s" % (exc, failure_note)) from exc
        if code:
            detail = redact_git_output(_recap(output) or output[-3000:], self.settings.gitlab_token)
            raise WorkflowError("Ansible thất bại (exit %s).\n%s\n%s" % (code, detail, failure_note))
        return AccessResult(args, _recap(output))

    def _return_master(self):
        if self.report().porcelain:
            raise WorkflowError("Repo còn thay đổi local; chưa thể trở về master/pull.")
        _git(self.repo, "checkout", REQUIRED_BRANCH, settings=self.settings)
        _git(self.repo, "pull", "--ff-only", "origin", REQUIRED_BRANCH,
             settings=self.settings, timeout=self.settings.git_clone_timeout_seconds)
        self._require_clean()

    def prepare_merge_request(self, change, state):
        if state.action == "no" or state.push_confirmed:
            raise WorkflowError("Lượt hiện tại đã bắt đầu huỷ hoặc đã xác nhận push.")
        request = change.request
        if not state.branch:
            self._require_files()
            self._validate_change(change, self.report())
            stamp = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d-%H-%M")
            branch = "%s/%s-%s" % (stamp, request.namespace, request.service)
            self._git_output("check-ref-format", "--branch", branch)
            local = self._git_output("for-each-ref", "--format=%(refname)", "refs/heads/" + branch)
            remote = self._git_output("ls-remote", "--heads", "origin", "refs/heads/" + branch)
            if local.strip() or remote.strip():
                raise WorkflowError("Branch %s đã tồn tại; bấm Yes lại ở phút kế tiếp." % branch)
            self._git_output("checkout", "-b", branch)
            state.branch = branch
            state.action = "yes"

        if not state.commit:
            report = self.report()
            statuses = (" M " + ACCESS_FILE + "\x00", "M  " + ACCESS_FILE + "\x00")
            if (report.branch != state.branch or report.head != request.head
                    or report.porcelain not in statuses
                    or self.path.is_symlink() or self.path.read_bytes() != change.content):
                raise GitCheckError("Repo đã đổi trước commit; giữ dữ liệu để kiểm tra.", report)
            self._git_output("add", "--", ACCESS_FILE)
            author = self.settings.gitlab_username or "infra-bot"
            host = urlsplit(self.settings.gitlab_url).hostname or "localhost"
            self._git_output(
                "-c", "user.name=" + author, "-c", "user.email=%s@%s" % (author, host),
                "commit", "-m", "gitlab-repo: Grant role %s for %s to repo %s/%s"
                % (request.role, request.username, request.namespace, request.service),
                "--", ACCESS_FILE,
            )
            state.commit = _git(self.repo, "rev-parse", "HEAD", settings=self.settings)

        return self.merge_request_info(change, state)

    def merge_request_info(self, change, state):
        self._require_files()
        if not state.branch or not state.commit:
            raise WorkflowError("Chưa chuẩn bị đủ branch và commit để xác nhận MR.")
        report = self.report()
        branch_head = _git(self.repo, "rev-parse", "refs/heads/" + state.branch, settings=self.settings)
        parent = _git(self.repo, "rev-parse", state.commit + "^", settings=self.settings)
        paths = self._git_output("diff", "--name-only", "-z", change.request.head, state.commit, "--")
        if (report.porcelain or report.branch not in (state.branch, REQUIRED_BRANCH)
                or branch_head != state.commit or parent != change.request.head
                or paths != ACCESS_FILE + "\x00"
                or (report.branch == state.branch and self.path.read_bytes() != change.content)):
            raise GitCheckError("Repo/branch đã đổi sau commit; dừng trước push/MR.", report)
        return MergeRequestInfo(
            branch=state.branch,
            difference=self._git_output(
                "diff", "--no-ext-diff", "--no-textconv",
                change.request.head, state.commit, "--", ACCESS_FILE,
            ),
            commit=state.commit,
            message=_git(self.repo, "log", "-1", "--format=%s", state.commit, settings=self.settings),
        )

    def _publish_change(self, change, state, requester, recap, review):
        if review is None or review != self.merge_request_info(change, state):
            raise WorkflowError("Thông tin MR không khớp bản đã xem; cần hiển thị và xác nhận lại.")
        state.push_confirmed = True
        request = change.request
        try:
            if not state.pushed:
                self._git_output("push", "--set-upstream", "origin", state.branch)
                state.pushed = True
            if not state.mr_url:
                state.mr_url = self.mr_client.create_or_get_mr(
                    state.branch, state.commit, requester, recap,
                    title=state.branch, target_branch=REQUIRED_BRANCH,
                    command="ansible-playbook -i nonprod %s --tags=%s,project_user_access"
                    % (PLAYBOOK, request.namespace),
                )
        finally:
            # A pushed branch remains available for MR retries after switching back.
            if state.pushed:
                self._return_master()

    def _cancel_change(self, change, state):
        if state.push_confirmed or state.pushed or state.mr_url:
            raise WorkflowError("Đã xác nhận push; chỉ có thể thử lại Yes để hoàn tất MR.")
        if not state.restored:
            self._require_files()
            report = self.report()
            if state.branch and report.branch == state.branch:
                master_head = _git(self.repo, "rev-parse", "refs/heads/" + REQUIRED_BRANCH,
                                   settings=self.settings)
                if master_head != change.request.head:
                    raise GitCheckError("Master đã đổi sau khi chuẩn bị MR; giữ dữ liệu để kiểm tra.", report)
                if state.commit:
                    # A clean local draft can be left intact while restoring master.
                    self.merge_request_info(change, state)
                    state.action = "no"
                else:
                    # Also allow cancellation if preparation stopped after git add.
                    statuses = (" M " + ACCESS_FILE + "\x00", "M  " + ACCESS_FILE + "\x00")
                    content = self.path.read_bytes()
                    pending_edit = report.porcelain in statuses and content == change.content
                    already_restored = not report.porcelain and content == change.request.source
                    if report.head != change.request.head or not (pending_edit or already_restored):
                        raise GitCheckError("Bản sửa trước commit đã đổi; không ghi đè khi huỷ.", report)
                    state.action = "no"
                    if pending_edit:
                        self._git_output("reset", "HEAD", "--", ACCESS_FILE)
                        _atomic_write(self.path, change.request.source)
                _git(self.repo, "checkout", REQUIRED_BRANCH, settings=self.settings)
                report = self.report()
            if self.path.read_bytes() == change.content:
                self._validate_change(change, report)
                _atomic_write(self.path, change.request.source)
            elif (self.path.read_bytes() != change.request.source
                    or report.branch != REQUIRED_BRANCH or report.head != change.request.head
                    or report.porcelain):
                raise GitCheckError("Repo đã đổi; không ghi đè khi khôi phục quyền.", report)
            state.action = "no"
            self._run_access_playbook(
                change.request,
                "Đã phục hồi YAML nhưng chưa xác nhận khôi phục quyền; bấm No để thử lại.",
            )
            state.restored = True
        self._return_master()

    def finalize(self, change, answer, requester, recap, state, review=None):
        if answer not in ("yes", "no"):
            raise WorkflowError("Lựa chọn không hợp lệ.")
        if state.action == "no" and answer != "no":
            raise WorkflowError("Đã bắt đầu huỷ; chỉ có thể thử lại No để hoàn tất.")
        if answer == "yes":
            if review is None:
                return self.prepare_merge_request(change, state)
            self._publish_change(change, state, requester, recap, review)
        else:
            self._cancel_change(change, state)
        return state
