"""Grant one role to multiple users in a ghub/non-ghub GitLab service."""

import copy
import json
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
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
ROLE_LEVELS = {"maintainer": 40, "developer": 30, "reporter": 20, "guest": 10}
REPOSITORIES = {
    "ghub": (ACCESS_FILE, PLAYBOOK),
    "non-ghub": ("group_vars/gitlab", "gitlab-repos.yaml"),
}
MAX_USERS = 50
RESET_PLAYBOOK = "gitlab-repos.yaml"
SCOPED_VARS_FILE = "group_vars/gitlab-bot"
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
USERNAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}$")


def repository_files(repo_kind):
    if repo_kind not in REPOSITORIES:
        raise WorkflowError("Nhóm repo không hợp lệ; chọn ghub hoặc non-ghub.")
    return REPOSITORIES[repo_kind]

def parse_usernames(value):
    """Accept commas/whitespace and retain each username once, in input order."""
    values = [user for user in re.split(r"[,\s]", value.strip()) if user] if isinstance(value, str) else value
    if not isinstance(values, (tuple, list)) or not values:
        raise WorkflowError("Nhập ít nhất một username GitLab.")
    users, seen = [], set()
    for username in values:
        if not isinstance(username, str) or not USERNAME.fullmatch(username):
            raise WorkflowError("Tên user không hợp lệ; ví dụ: anhtn, tuanpv.")
        key = username.casefold()
        if key not in seen:
            users.append(username)
            seen.add(key)
    if len(users) > MAX_USERS:
        raise WorkflowError("Mỗi lượt tối đa %s user." % MAX_USERS)
    return tuple(users)

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
    mode: str = "update"
    repo_kind: str = "ghub"
    accesses: tuple = ()
    user_modes: tuple = ()
    @property
    def usernames(self):
        return tuple(access.username for access in self.accesses) or (self.username,)

    @property
    def access_file(self):
        return repository_files(self.repo_kind)[0]

    @property
    def playbook(self):
        return repository_files(self.repo_kind)[1]


@dataclass(frozen=True)
class GitReport:
    status: str
    diff: str
    porcelain: str
    branch: str
    head: str
    access_file: str = ACCESS_FILE


class GitCheckError(WorkflowError):
    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


@dataclass(frozen=True)
class AppliedChange:
    request: AccessRequest
    content: bytes
    report: GitReport
    mode: str = "update"


@dataclass(frozen=True)
class AccessResult:
    command: tuple
    recap: str
    elapsed_seconds: float = 0.0
    project_count: int = 0
    total_project_count: int = 0


@dataclass(frozen=True)
class ResetResult:
    namespace: str
    repo_kind: str
    head: str
    result: AccessResult


@dataclass
class Finalization:
    action: str = ""
    branch: str = ""
    commit: str = ""
    push_confirmed: bool = False
    pushed: bool = False
    restored: bool = False
    commits: tuple = ()


@dataclass(frozen=True)
class BranchPushInfo:
    branch: str
    difference: str
    commit: str
    message: str
    commits: tuple = ()


def _node_value(node, key):
    if not isinstance(node, MappingNode):
        raise WorkflowError("Cấu trúc YAML không hợp lệ.")
    for key_node, value in node.value:
        if key_node.value == key:
            return value
    raise WorkflowError("YAML thiếu key %s." % key)


def _load(source, access_file=ACCESS_FILE):
    try:
        text = source.decode("utf-8")
        # An alias could make a change to one list affect several services.
        if any(isinstance(token, AliasToken) for token in yaml.scan(text)):
            raise WorkflowError("Không tự sửa YAML có alias; cần danh sách users riêng cho từng role.")
        document = yaml.load(text, Loader=UniqueKeyLoader)
        nodes = yaml.compose(text, Loader=UniqueKeyLoader)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise WorkflowError("Không đọc được YAML trong %s." % access_file) from exc
    if not isinstance(document, dict):
        raise WorkflowError("File %s phải chứa một YAML mapping." % access_file)
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
    if (parent is not None and (not isinstance(parent, str) or not parent) or not isinstance(entry, dict)):
        raise WorkflowError("Cấu hình namespace %s không hợp lệ." % namespace)
    services = entry.get("project")
    if not isinstance(services, list):
        raise WorkflowError("Namespace %s thiếu danh sách project." % namespace)
    return (parent.rstrip("/") + "/" if parent else "") + namespace, services


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
    text, document, nodes = _load(request.source, request.access_file)
    project, project_node = _project(document, nodes, request.namespace, request.service)
    access = _access(project, project_node)
    expected = copy.deepcopy(document)
    expected_project, _ = _project(expected, nodes, request.namespace, request.service)
    expected_access = {entry["level"]: entry for entry in expected_project["users_access"]}
    replacements = []
    selected = set(request.usernames)
    for role, (entry, node) in access.items():
        users = entry["users"]
        if role == request.role:
            updated = users + [user for user in request.usernames if user not in users]
        else:
            updated = [user for user in users if user not in selected]
        expected_access[role]["users"] = updated
        if updated != users:
            replacements.append((node.start_mark.index, node.end_mark.index,
                                 json.dumps(updated, ensure_ascii=False)))
    # Replace only the selected service's user lists; retain every other byte.
    for start, end, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[end:]
    content = text.encode("utf-8")
    if _load(content, request.access_file)[1] != expected:
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


def namespace_variables(source, namespace, access_file=ACCESS_FILE, service=None):
    """Keep shared variables, but limit groups/projects to the selected namespace."""
    _, document, nodes = _load(source, access_file)
    _, projects = _namespace(document, namespace)
    if not projects:
        raise WorkflowError("Namespace %s chưa có project để chạy Ansible." % namespace)
    total = sum(len(entry.get("project", [])) for entry in document["gitlab_projects"].values()
                if isinstance(entry, dict) and isinstance(entry.get("project"), list))
    scoped = copy.deepcopy(document)
    scoped["gitlab_groups"] = [group for group in scoped["gitlab_groups"]
                               if isinstance(group, dict) and group.get("path") == namespace]
    scoped["gitlab_projects"] = {namespace: scoped["gitlab_projects"][namespace]}
    if service is not None:
        services = (service,) if isinstance(service, str) else tuple(dict.fromkeys(service))
        scoped["gitlab_projects"][namespace]["project"] = [
            _project(document, nodes, namespace, selected)[0] for selected in services
        ]
    return scoped, len(scoped["gitlab_projects"][namespace]["project"]), total


@contextmanager
def scoped_variables_file(repo, variables):
    """Create this run's private vars file without replacing an existing file."""
    path = repo / SCOPED_VARS_FILE
    content = yaml.safe_dump(variables, allow_unicode=True, sort_keys=False).encode("utf-8")
    try:
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise WorkflowError("%s đã tồn tại; kiểm tra file còn lại trước khi chạy lại." % SCOPED_VARS_FILE) from exc
    created = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        yield SCOPED_VARS_FILE
    finally:
        try:
            current = path.lstat()
        except FileNotFoundError:
            pass
        else:
            if (current.st_dev, current.st_ino) != (created.st_dev, created.st_ino):
                raise WorkflowError("File biến tạm đã bị thay thế; giữ lại để kiểm tra.")
            path.unlink()


class GitLabAccessWorkflow:
    def __init__(self, settings, access_client=None):
        self.settings = settings
        self.repo = Path(settings.git_clone_dir).resolve()
        self.path = self.repo / ACCESS_FILE
        self.access_client = access_client or GitLabClient(settings)

    def _path(self, repo_kind):
        return self.repo / repository_files(repo_kind)[0]

    def _git_output(self, *args):
        code, output = git_command(self.settings, [
            "-C", str(self.repo), "-c", "color.ui=false", "--no-pager", *args,
        ])
        output = redact_git_output(output, self.settings.gitlab_token)
        if code:
            raise WorkflowError("Git thất bại (exit %s).\n%s" % (code, output[-3000:]))
        return output

    def _require_files(self, repo_kind="ghub", playbook=None):
        root = _git(self.repo, "rev-parse", "--show-toplevel", settings=self.settings)
        if Path(root).resolve() != self.repo:
            raise WorkflowError("GIT_CLONE_DIR phải là thư mục gốc của repo Ansible.")
        access_file, default_playbook = repository_files(repo_kind)
        for name in (access_file, INVENTORY, playbook or default_playbook):
            path = self.repo / name
            if not path.is_file() or path.is_symlink() or path.resolve() != path:
                raise WorkflowError("Repo thiếu file thường %s." % name)

    def report(self, repo_kind="ghub"):
        access_file = repository_files(repo_kind)[0]
        return GitReport(
            status=self._git_output("status", "--untracked-files=all"),
            diff=self._git_output("diff", "--no-ext-diff", "--no-textconv", "--", access_file),
            porcelain=self._git_output("status", "--porcelain=v1", "-z", "--untracked-files=all"),
            branch=_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD", settings=self.settings),
            head=_git(self.repo, "rev-parse", "HEAD", settings=self.settings),
            access_file=access_file,
        )

    def _require_clean(self, repo_kind="ghub", playbook=None):
        self._require_files(repo_kind, playbook)
        report = self.report(repo_kind)
        if report.branch != REQUIRED_BRANCH or report.porcelain:
            raise GitCheckError(
                "Repo phải ở branch master và sạch. Xử lý diff hiện tại rồi thử lại.", report,
            )
        return report

    @staticmethod
    def _changes(change):
        return (change,) if isinstance(change, AppliedChange) else tuple(change)

    def _batch_files(self, changes):
        changes = self._changes(changes)
        if not changes:
            raise WorkflowError("Chưa có lượt phân quyền để xử lý.")
        originals, contents = {}, {}
        head = changes[0].request.head
        for change in changes:
            request = change.request
            name = request.access_file
            if (request.head != head or (name in contents and contents[name] != request.source)
                    or change.mode not in ("update", "sync", "unchanged")
                    or (change.mode == "update") != (change.content != request.source)):
                raise WorkflowError("Các bản YAML trong phiên không nối tiếp nhau; giữ lại để kiểm tra.")
            originals.setdefault(name, request.source)
            contents[name] = change.content
        return originals, contents

    def batch_report(self, changes):
        files = tuple(self._batch_files(changes)[0])
        report = self.report(self._changes(changes)[0].request.repo_kind)
        return replace(report, access_file=" ".join(files),
                       diff=self._git_output("diff", "--no-ext-diff", "--no-textconv", "--", *files))

    def _blob_id(self, content):
        with tempfile.NamedTemporaryFile() as handle:
            handle.write(content)
            handle.flush()
            return self._git_output("hash-object", "--no-filters", handle.name).strip()

    @staticmethod
    def _commit_message(request):
        return "gitlab-repo: Grant role %s for %s to repo %s/%s" % (
            request.role, ", ".join(request.usernames), request.namespace, request.service,
        )

    def _committed_contents(self, changes, commits):
        originals, _ = self._batch_files(changes)
        head = changes[0].request.head
        contents = dict(originals)
        for name, source in originals.items():
            if self._git_output("rev-parse", head + ":" + name).strip() != self._blob_id(source):
                raise WorkflowError("YAML gốc không khớp commit ban đầu; dừng xử lý.")
        updates = [change for change in changes if change.mode == "update"]
        if len(commits) > len(updates):
            raise WorkflowError("Số commit vượt số thay đổi YAML trong phiên.")
        for change, commit in zip(updates, commits):
            name = change.request.access_file
            if (self._git_output("rev-parse", commit + "^").strip() != head
                    or self._git_output("diff", "--name-only", "-z", head, commit, "--") != name + "\x00"
                    or self._git_output("rev-parse", commit + ":" + name).strip() != self._blob_id(change.content)
                    or _git(self.repo, "log", "-1", "--format=%s", commit, settings=self.settings)
                    != self._commit_message(change.request)):
                raise WorkflowError("Commit không khớp thay đổi đã xác nhận; dừng trước push.")
            contents[name] = change.content
            head = commit
        return head, contents

    def _validate_pending(self, changes, commits=(), branch=REQUIRED_BRANCH, allow_restored=False):
        changes = self._changes(changes)
        originals, final = self._batch_files(changes)
        head, committed = self._committed_contents(changes, commits)
        for kind in dict.fromkeys(change.request.repo_kind for change in changes):
            self._require_files(kind)
        report = self.batch_report(changes)
        expected_status = set()
        for name, content in final.items():
            path = self.repo / name
            allowed = (content, originals[name], committed[name]) if allow_restored else (content,)
            if path.is_symlink() or not path.is_file() or path.read_bytes() not in allowed:
                raise GitCheckError("YAML đã đổi ngoài các lượt trong phiên; giữ lại để kiểm tra.", report)
            if path.read_bytes() != committed[name]:
                expected_status.add(" M " + name)
        if (report.branch != branch or report.head != head
                or set(filter(None, report.porcelain.split("\x00"))) != expected_status):
            raise GitCheckError("Repo có branch/commit, staged hoặc file ngoài phiên đã thay đổi.", report)
        return report

    def _collection_report(self, repo_kind, pending):
        self._require_files(repo_kind)
        return self._validate_pending(pending) if pending else self._require_clean(repo_kind)

    def discard_pending(self, changes):
        """Cancel collection before Ansible; restore only the owned YAML files."""
        originals, final = self._batch_files(changes)
        self._validate_pending(changes, allow_restored=True)
        for name, source in originals.items():
            if source != final[name]:
                _atomic_write(self.repo / name, source)
        self._require_clean(self._changes(changes)[-1].request.repo_kind)

    def namespaces(self, repo_kind="ghub", playbook=None):
        self._require_files(repo_kind, playbook)
        _, document, _ = _load(self._path(repo_kind).read_bytes(), repository_files(repo_kind)[0])
        groups = document.get("gitlab_groups")
        if not isinstance(groups, list):
            raise WorkflowError("YAML thiếu danh sách gitlab_groups.")
        projects = document.get("gitlab_projects")
        if not isinstance(projects, dict):
            raise WorkflowError("YAML thiếu gitlab_projects.")
        result = []
        for group in groups:
            if (isinstance(group, dict) and isinstance(group.get("path"), str)
                    and group["path"] in projects):
                namespace = group["path"]
                _, services = _namespace(document, namespace)
                if services:
                    result.append(namespace)
        return tuple(result)

    def reset_namespaces(self, repo_kind):
        return self.namespaces(repo_kind, RESET_PLAYBOOK)

    def reset_namespace(self, repo_kind, namespace):
        """Apply one namespace from the freshly pulled, clean remote master."""
        repository_files(repo_kind)
        if not SLUG.fullmatch(namespace):
            raise WorkflowError("Tên namespace không hợp lệ.")
        if self.report(repo_kind).porcelain:
            raise GitCheckError("Repo còn thay đổi local; hoàn tất lượt /gitlab trước khi /reset.",
                                self.report(repo_kind))
        self._return_master(repo_kind, RESET_PLAYBOOK)
        before = self._require_clean(repo_kind, RESET_PLAYBOOK)
        remote_head = _git(self.repo, "rev-parse", "origin/" + REQUIRED_BRANCH, settings=self.settings)
        if before.head != remote_head:
            raise GitCheckError("Local master có commit ngoài origin/master; kiểm tra trước khi /reset.", before)
        source = self._path(repo_kind).read_bytes()
        result = self._run_namespace_playbook(
            namespace, repo_kind, RESET_PLAYBOOK, source,
            "YAML master không thay đổi; Ansible có thể đã áp dụng một phần.",
        )
        after = self._require_clean(repo_kind, RESET_PLAYBOOK)
        if after.head != before.head or self._path(repo_kind).read_bytes() != source:
            raise GitCheckError("Repo đã đổi trong lúc /reset; kiểm tra quyền và YAML trước khi chạy lại.", after)
        return ResetResult(namespace, repo_kind, before.head, result)

    def services(self, namespace, repo_kind="ghub"):
        self._require_files(repo_kind)
        _, document, _ = _load(self._path(repo_kind).read_bytes(), repository_files(repo_kind)[0])
        group_path, projects = _namespace(document, namespace)
        return tuple(project["path"].rsplit("/", 1)[1] for project in projects
                     if isinstance(project, dict) and isinstance(project.get("path"), str)
                     and project["path"].rsplit("/", 1)[0] == group_path)

    def preview(self, namespace, service, username, role, repo_kind="ghub", pending=()):
        usernames = parse_usernames(username)
        if role not in ROLES:
            raise WorkflowError("Role không hợp lệ.")
        report = self._collection_report(repo_kind, pending)
        source = self._path(repo_kind).read_bytes()
        _, document, nodes = _load(source, repository_files(repo_kind)[0])
        project, project_node = _project(document, nodes, namespace, service)
        yaml_access = _access(project, project_node)
        accesses = self._read_accesses(project["path"], usernames)
        access = accesses[0]
        roles = (access.role,) if access.role else ()
        request = AccessRequest(
            namespace, service, access.username, role, roles, report.head, source,
            project_path=project["path"], access=access,
            repo_kind=repo_kind, accesses=accesses,
        )
        content = _updated_content(request)
        user_modes = []
        for member in accesses:
            yaml_correct = (member.username in yaml_access[role][0]["users"]
                            and all(member.username not in entry["users"]
                                    for other, (entry, _) in yaml_access.items() if other != role))
            user_modes.append("update" if not yaml_correct else
                              "unchanged" if member.access_level == ROLE_LEVELS[role] else "sync")
        current = self._collection_report(repo_kind, pending)
        if current.head != report.head or self._path(repo_kind).read_bytes() != source:
            raise GitCheckError("Cấu hình đã đổi trong lúc kiểm tra user; chạy /gitlab lại.", current)
        return replace(request, mode=self._access_mode(request, content, accesses),
                       user_modes=tuple(user_modes))

    def _read_accesses(self, project_path, usernames):
        accesses = []
        for username in usernames:
            try:
                accesses.append(self.access_client.project_user_access(project_path, username))
            except WorkflowError as exc:
                raise WorkflowError("Không kiểm tra được user %s.\n%s" % (username, exc)) from exc
        if len({access.user_id for access in accesses}) != len(accesses):
            raise WorkflowError("Danh sách user GitLab có ID trùng; nhập lại username.")
        return tuple(accesses)

    @staticmethod
    def _check_user_ids(request, accesses):
        expected = request.accesses or ((request.access,) if request.access is not None else ())
        if expected and tuple(access.user_id for access in accesses) != tuple(access.user_id for access in expected):
            raise WorkflowError("User GitLab đã đổi sau xác nhận; chạy /gitlab lại.")

    @staticmethod
    def _access_mode(request, content, accesses):
        if content != request.source:
            return "update"
        if isinstance(accesses, ProjectUserAccess):
            accesses = (accesses,)
        return "unchanged" if all(access.access_level == ROLE_LEVELS[request.role] for access in accesses) else "sync"

    def rollback(self, change):
        # Do not overwrite someone else's subsequent edit, or reset other files.
        if change.mode != "update":
            return False
        path = self._path(change.request.repo_kind)
        try:
            if not path.is_symlink() and path.read_bytes() == change.content:
                _atomic_write(path, change.request.source)
                return True
        except FileNotFoundError:
            # A file removed by someone else must stay removed.
            return False
        return False

    def _validate_change(self, change, report):
        path = self._path(change.request.repo_kind)
        if report.branch != REQUIRED_BRANCH or report.head != change.request.head:
            raise GitCheckError("Branch/commit đã đổi; dừng trước khi chạy Ansible.", report)
        if change.mode in ("sync", "unchanged"):
            if (report.porcelain or report.diff.strip() or path.is_symlink()
                    or path.read_bytes() != change.request.source
                    or change.content != change.request.source):
                raise GitCheckError("Repo/YAML đã đổi; dừng trước khi đồng bộ quyền GitLab.", report)
            return
        if change.mode != "update":
            raise WorkflowError("Trường hợp cập nhật quyền không hợp lệ.")
        if report.porcelain != " M " + change.request.access_file + "\x00":
             raise GitCheckError("Git status có thay đổi ngoài file dự kiến hoặc có thay đổi staged; dừng luồng.", report)
        if not report.diff.strip() or path.is_symlink() or path.read_bytes() != change.content:
             raise GitCheckError("Git diff không khớp đúng user/role đã xác nhận; dừng luồng.", report)

    def prepare(self, request, pending=()):
        report = self._collection_report(request.repo_kind, pending)
        path = self._path(request.repo_kind)
        if report.head != request.head or path.read_bytes() != request.source:
            raise GitCheckError("Cấu hình đã đổi sau khi chọn role; chạy /gitlab lại để xác nhận thông tin mới.", report)
        content = _updated_content(request)
        if content == request.source:
            # Re-read membership after Yes; the live role may have changed since preview.
            accesses = self._read_accesses(request.project_path, request.usernames)
            self._check_user_ids(request, accesses)
            change = AppliedChange(request, content, report, self._access_mode(request, content, accesses))
            self._validate_pending(tuple(pending) + (change,))
            return change
        change = AppliedChange(request, content, report)
        _atomic_write(path, content)
        try:
            report = self._validate_pending(tuple(pending) + (change,))
        except Exception:
            self.rollback(change)
            raise
        return AppliedChange(request, content, report)

    @staticmethod
    def _batch_groups(changes):
        groups = {}
        for change in changes:
            if change.mode != "unchanged":
                request = change.request
                groups.setdefault((request.repo_kind, request.namespace), []).append(request.service)
        return {key: tuple(dict.fromkeys(services)) for key, services in groups.items()}

    def run_batch(self, changes):
        """Apply the final YAML once per repo group/namespace, with selected services."""
        changes = self._changes(changes)
        _, final = self._batch_files(changes)
        self._validate_pending(changes)
        self._verify_identities(changes)
        results = []
        for (kind, namespace), services in self._batch_groups(changes).items():
            access_file, playbook = repository_files(kind)
            results.append(self._run_namespace_playbook(
                namespace, kind, playbook, final[access_file],
                "Giữ toàn bộ YAML trong phiên để kiểm tra hoặc huỷ.", service=services,
            ))
            self._validate_pending(changes)
        # Later requests can supersede a role from an earlier sync request.
        latest, needs_verification = {}, set()
        for change in changes:
            for access in change.request.accesses:
                key = (change.request.project_path, access.user_id)
                latest[key] = (change.request, access)
                if change.mode == "sync":
                    needs_verification.add(key)
        for key in needs_verification:
            request, access = latest[key]
            self._verify_accesses(replace(request, accesses=(access,), username=access.username))
        return tuple(results)

    def _verify_identities(self, changes):
        live = {}
        for change in changes:
            request = change.request
            accesses = []
            for username in request.usernames:
                key = (request.project_path, username)
                if key not in live:
                    live[key] = self._read_accesses(request.project_path, (username,))[0]
                accesses.append(live[key])
            self._check_user_ids(request, tuple(accesses))

    def run_playbook(self, change):
        if change.mode == "unchanged":
            raise WorkflowError("YAML và GitLab đã đúng role; không cần chạy Ansible.")
        # Recheck after sending the complete Git outputs to Telegram.
        try:
            self._require_files(change.request.repo_kind)
            self._validate_change(change, self.report(change.request.repo_kind))
        except Exception:
            self.rollback(change)
            raise
        result = self._run_access_playbook(
            change.request,
            "YAML không thay đổi; cần kiểm tra quyền GitLab đã áp dụng."
            if change.mode == "sync" else
            "Giữ diff YAML để kiểm tra; Ansible có thể đã áp dụng một phần.",
         )
        if change.mode == "sync":
            self._validate_change(change, self.report(change.request.repo_kind))
            self._verify_accesses(change.request)
        return result

    def _verify_accesses(self, request):
        expected = {access.username: access.user_id for access in request.accesses}
        if not expected and request.access is not None:
            expected[request.access.username] = request.access.user_id
        errors = []
        for username in request.usernames:
            try:
                access = self.access_client.project_user_access(request.project_path, username)
            except WorkflowError as exc:
                errors.append("%s: chưa xác minh được quyền GitLab.\n%s" % (username, exc))
                continue
            if username in expected and access.user_id != expected[username]:
                errors.append("%s: user GitLab đã đổi; cần kiểm tra lại." % username)
            elif access.access_level != ROLE_LEVELS[request.role]:
                errors.append("%s: GitLab vẫn là %s (access_level: %s); cần role %s (%s)."
                              % (username, access.role or "chưa có membership", access.access_level,
                                 request.role, ROLE_LEVELS[request.role]))
        if errors:
            raise WorkflowError("Ansible đã chạy nhưng chưa xác nhận đủ quyền.\n" + "\n".join(errors))

    def _run_access_playbook(self, request, failure_note):
        return self._run_namespace_playbook(
            request.namespace, request.repo_kind, request.playbook,
            self._path(request.repo_kind).read_bytes(), failure_note, service=request.service,
        )

    def _run_namespace_playbook(self, namespace, repo_kind, playbook, source, failure_note, service=None):
        variables, project_count, total = namespace_variables(source, namespace, repository_files(repo_kind)[0], service)
        args = (
            self.settings.ansible_binary, "-i", INVENTORY, playbook,
            "--tags=" + namespace + ",project_user_access",
            "--extra-vars=@" + SCOPED_VARS_FILE,
        )
        env = os.environ.copy()
        env["ANSIBLE_NOCOLOR"] = "1"
        # Replacing the mapping is essential: merging would re-add other namespaces.
        env["ANSIBLE_HASH_BEHAVIOUR"] = "replace"
        callback_path = str(Path(__file__).resolve().parent / "ansible_callbacks")
        env["ANSIBLE_CALLBACK_PLUGINS"] = callback_path + (os.pathsep + env["ANSIBLE_CALLBACK_PLUGINS"]
                                                          if env.get("ANSIBLE_CALLBACK_PLUGINS") else "")
        callbacks = [name.strip() for name in env.get("ANSIBLE_CALLBACKS_ENABLED", "").split(",") if name.strip()]
        if "bot_timing" not in callbacks:
            callbacks.append("bot_timing")
        env["ANSIBLE_CALLBACKS_ENABLED"] = ",".join(callbacks)
        started = time.monotonic()
        try:
            with scoped_variables_file(self.repo, variables):
                code, output = _command(args, cwd=self.repo,
                                        timeout=self.settings.ansible_timeout_seconds, env=env)
        except WorkflowError as exc:
            raise WorkflowError("%s\n%s" % (exc, failure_note)) from exc
        elapsed = time.monotonic() - started
        if code:
            detail = redact_git_output(_recap(output) or output[-3000:], self.settings.gitlab_token)
            raise WorkflowError("Ansible thất bại (exit %s).\n%s\n%s" % (code, detail, failure_note))
        recap = _recap(output)
        timing = "⏱ Ansible: %.1f giây; project %s: %s/%s services." % (elapsed, namespace, project_count, total)
        if service is not None:
            timing += " Service: " + (service if isinstance(service, str) else ", ".join(service))
        slow_tasks = []
        for line in output.splitlines():
            if line.startswith("INFRA_BOT_TASK_TIMING "):
                try:
                    item = json.loads(line[len("INFRA_BOT_TASK_TIMING "):])
                    seconds = float(item["seconds"])
                    name = redact_git_output(str(item["task"]), self.settings.gitlab_token)[:200]
                    slow_tasks.append("%.1fs — %s" % (seconds, name))
                except (KeyError, TypeError, ValueError):
                    continue
        details = "\n🐢 Task tốn thời gian:\n" + "\n".join(slow_tasks[:5]) if slow_tasks else ""
        return AccessResult(args, (recap + "\n" + timing + details).strip(), elapsed, project_count, total)

    def _return_master(self, repo_kind="ghub", playbook=None):
        if self.report(repo_kind).porcelain:
            raise WorkflowError("Repo còn thay đổi local; chưa thể trở về master/pull.")
        _git(self.repo, "checkout", REQUIRED_BRANCH, settings=self.settings)
        _git(self.repo, "pull", "--ff-only", "origin", REQUIRED_BRANCH, settings=self.settings, timeout=self.settings.git_clone_timeout_seconds)
        self._require_clean(repo_kind, playbook)
 
    def prepare_branch(self, change, state):
        changes = self._changes(change)
        _, final = self._batch_files(changes)
        updates = [item for item in changes if item.mode == "update"]
        if not updates:
            raise WorkflowError("Không có thay đổi YAML để tạo branch.")
        if state.action == "no" or state.push_confirmed:
            raise WorkflowError("Lượt hiện tại đã bắt đầu huỷ hoặc đã xác nhận push.")
        if not state.branch:
            self._validate_pending(changes)
            stamp = datetime.now(timezone(timedelta(hours=7))).strftime("%Y%m%d-%H%M%S")
            request = changes[0].request
            branch = ("bot-%s/Update-permission" % stamp if len(changes) > 1 else
                      "bot-%s/%s/%s" % (stamp, request.namespace, request.service))
            self._git_output("check-ref-format", "--branch", branch)
            local = self._git_output("for-each-ref", "--format=%(refname)", "refs/heads/" + branch)
            remote = self._git_output("ls-remote", "--heads", "origin", "refs/heads/" + branch)
            if local.strip() or remote.strip():
                raise WorkflowError("Branch %s đã tồn tại; bấm Yes lại ở giây kế tiếp." % branch)
            self._git_output("checkout", "-b", branch)
            state.branch, state.action = branch, "yes"
        for item in updates[len(state.commits):]:
            self._validate_pending(changes, state.commits, state.branch)
            request = item.request
            path = self._path(request.repo_kind)
            if path.read_bytes() != item.content:
                _atomic_write(path, item.content)
            try:
                author = self.settings.gitlab_username or "infra-bot"
                host = urlsplit(self.settings.gitlab_url).hostname or "localhost"
                # Commit this snapshot only; future changes remain in the worktree.
                self._git_output(
                    "-c", "user.name=" + author, "-c", "user.email=%s@%s" % (author, host),
                    "commit", "--only", "-m", self._commit_message(request), "--", request.access_file,
                )
                state.commit = _git(self.repo, "rev-parse", "HEAD", settings=self.settings)
                state.commits += (state.commit,)
            finally:
                if path.is_symlink() or path.read_bytes() != item.content:
                    raise GitCheckError("YAML đã đổi trong lúc commit; không ghi đè.", self.batch_report(changes))
                if item.content != final[request.access_file]:
                    _atomic_write(path, final[request.access_file])
        return self.branch_push_info(changes, state)

    def branch_push_info(self, change, state):
        changes = self._changes(change)
        originals, final = self._batch_files(changes)
        updates = [item for item in changes if item.mode == "update"]
        commits = state.commits or ((state.commit,) if state.commit else ())
        if not state.branch or len(commits) != len(updates) or not commits:
            raise WorkflowError("Chưa chuẩn bị đủ branch và commit để xác nhận push.")
        head, _ = self._committed_contents(changes, commits)
        report = self.batch_report(changes)
        branch_head = _git(self.repo, "rev-parse", "refs/heads/" + state.branch, settings=self.settings)
        paths = set(filter(None, self._git_output(
            "diff", "--name-only", "-z", changes[0].request.head, head, "--",
        ).split("\x00")))
        expected_paths = {name for name in final if final[name] != originals[name]}
        if (branch_head != head or head != state.commit or report.porcelain
                or report.branch not in (state.branch, REQUIRED_BRANCH) or paths != expected_paths):
            raise GitCheckError("Repo/branch đã đổi sau commit; dừng trước push.", report)
        if report.branch == state.branch:
            self._validate_pending(changes, commits, state.branch)
        messages = (self._git_output("log", "--reverse", "--format=%h %s",
                                     changes[0].request.head + ".." + head).strip()
                    if len(commits) > 1 else self._commit_message(updates[0].request))
        return BranchPushInfo(
            branch=state.branch,
            difference=self._git_output(
                "diff", "--no-ext-diff", "--no-textconv",
                changes[0].request.head, head, "--", *final,
            ) or "(không có diff tổng; xem từng commit)",
            commit=head, message=messages, commits=commits,
        )

    def _publish_change(self, change, state, review):
        changes = self._changes(change)
        if review is None or review != self.branch_push_info(changes, state):
            raise WorkflowError("Thông tin branch không khớp bản đã xem; cần hiển thị và xác nhận lại.")
        state.push_confirmed = True
        try:
            if not state.pushed:
                self._git_output("push", "--set-upstream", "origin", state.branch)
                state.pushed = True
        finally:
            if state.pushed:
                self._return_master(changes[-1].request.repo_kind)

    def _cancel_change(self, change, state):
        changes = self._changes(change)
        originals, final = self._batch_files(changes)
        if state.push_confirmed or state.pushed:
            raise WorkflowError("Đã xác nhận push; chỉ có thể thử lại Yes để hoàn tất lượt hiện tại.")
        if not state.restored:
            report = self.batch_report(changes)
            if state.branch and report.branch == state.branch:
                if _git(self.repo, "rev-parse", "refs/heads/" + REQUIRED_BRANCH,
                        settings=self.settings) != changes[0].request.head:
                    raise GitCheckError("Master đã đổi; giữ YAML và branch để kiểm tra.", report)
                self._validate_pending(changes, state.commits, state.branch, allow_restored=True)
                _, committed = self._committed_contents(changes, state.commits)
                state.action = "no"
                for name, content in committed.items():
                    _atomic_write(self.repo / name, content)
                _git(self.repo, "checkout", REQUIRED_BRANCH, settings=self.settings)
            else:
                self._validate_pending(changes, allow_restored=True)
                state.action = "no"
            for name, content in originals.items():
                path = self.repo / name
                if path.read_bytes() not in (content, final[name]):
                    raise GitCheckError("YAML đã đổi trước phục hồi; không ghi đè.", self.batch_report(changes))
                if path.read_bytes() != content:
                    _atomic_write(path, content)
            self._verify_identities(changes)
            for (kind, namespace), services in self._batch_groups(changes).items():
                access_file, playbook = repository_files(kind)
                report = self._require_clean(kind)
                if report.head != changes[0].request.head:
                    raise GitCheckError("Master đã đổi trước phục hồi quyền; dừng xử lý.", report)
                self._run_namespace_playbook(
                    namespace, kind, playbook, originals[access_file],
                    "Đã phục hồi YAML nhưng chưa khôi phục đủ quyền; bấm No để thử lại.", service=services,
                )
                report = self._require_clean(kind)
                if report.head != changes[0].request.head:
                    raise GitCheckError("Master đã đổi trong lúc phục hồi quyền; dừng xử lý.", report)
            state.restored = True
        self._return_master(changes[-1].request.repo_kind)

    def finalize(self, change, answer, state, review=None):
        changes = self._changes(change)
        if not any(item.mode == "update" for item in changes):
            raise WorkflowError("Lượt đồng bộ quyền không có bước push branch hoặc huỷ YAML.")
        if answer not in ("yes", "no"):
            raise WorkflowError("Lựa chọn không hợp lệ.")
        if state.action == "no" and answer != "no":
            raise WorkflowError("Đã bắt đầu huỷ; chỉ có thể thử lại No để hoàn tất.")
        if answer == "yes":
            if review is None:
                return self.prepare_branch(changes, state)
            self._publish_change(changes, state, review)
        else:
            self._cancel_change(changes, state)
        return state
