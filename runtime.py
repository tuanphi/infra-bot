"""Clone the startup repository and serve a small HTTP health endpoint."""

import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

from git_auth import repository_identity
from workflow import WorkflowError, _git, clone_repository, validate_branch


class HealthHandler(BaseHTTPRequestHandler):
    def _respond(self, send_body):
        healthy = urlsplit(self.path).path == "/health"
        body = b'{"status":"ok"}\n' if healthy else b'{"error":"not found"}\n'
        self.send_response(200 if healthy else 404)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def do_GET(self):
        self._respond(send_body=True)

    def do_HEAD(self):
        self._respond(send_body=False)

    def log_message(self, format, *args):
        # Health probes should not fill the container log.
        pass


def start_health_server(port=8080, host="0.0.0.0"):
    server = ThreadingHTTPServer((host, port), HealthHandler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, name="http-health", daemon=True)
    thread.start()
    logging.info("Health endpoint listening on %s:%s/health", host, server.server_port)
    return server


def prepare_repository(settings):
    validate_branch(settings.git_target_branch)
    repo = Path(settings.git_clone_dir).resolve()
    timeout = settings.git_clone_timeout_seconds
    if repo.exists() and (not repo.is_dir() or (any(repo.iterdir()) and not (repo / ".git").exists())):
        raise WorkflowError("Thư mục %s đã chứa dữ liệu khác; không thể clone repo." % repo)

    if not (repo / ".git").exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        logging.info("Cloning Ansible repository into %s", repo)
        clone_repository(settings, repo, description="repo Ansible lúc khởi động")
    else:
        origin = _git(repo, "remote", "get-url", "origin", settings=settings)
        if repository_identity(origin) != repository_identity(settings.git_repo_url):
            raise WorkflowError("Repo tại %s có origin khác GIT_REPO_URL; dừng khởi động." % repo)
        if _git(repo, "status", "--porcelain", settings=settings):
            raise WorkflowError("Repo tại %s có thay đổi local; dừng để giữ nguyên dữ liệu." % repo)
        logging.info("Updating Ansible repository in %s", repo)
        remote_ref = "refs/remotes/origin/" + settings.git_target_branch
        _git(
            repo, "fetch", "--no-tags", "origin",
            "+refs/heads/%s:%s" % (settings.git_target_branch, remote_ref),
            settings=settings, timeout=timeout,
        )
        _git(repo, "checkout", "--detach", remote_ref, settings=settings)

    commit = _git(repo, "rev-parse", "--verify", "HEAD^{commit}", settings=settings)
    logging.info("Ansible repository ready at %s (commit %s)", repo, commit[:12])
    return repo
