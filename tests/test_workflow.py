import json
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import Settings
from workflow import GitLabClient, Pipeline, WorkflowError, validate_branch


def git(*args, cwd=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=True,
    ).stdout.strip()


class FakeMR:
    def __init__(self):
        self.calls = []

    def create_or_get_mr(self, branch, commit, requester, recap):
        self.calls.append((branch, commit, requester, recap))
        return "https://gitlab.example/merge_requests/1"


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.remote = root / "remote.git"
        self.local = root / "working"
        git("init", "--bare", str(self.remote))
        git("init", "-b", "master", str(self.local))
        git("config", "user.name", "Bot Test", cwd=self.local)
        git("config", "user.email", "bot@example.org", cwd=self.local)
        (self.local / "nonprod").write_text("[sftp-server]\nlocalhost ansible_connection=local\n")
        (self.local / "sftp-server.yaml").write_text("---\n- hosts: sftp-server\n  tasks: []\n")
        git("add", ".", cwd=self.local)
        git("commit", "-m", "base", cwd=self.local)
        git("remote", "add", "origin", str(self.remote), cwd=self.local)
        git("push", "-u", "origin", "master", cwd=self.local)
        git("switch", "-c", "feature/sftp-user", cwd=self.local)
        (self.local / "project-user.txt").write_text("user configuration\n")
        git("add", ".", cwd=self.local)
        git("commit", "-m", "add sftp user", cwd=self.local)
        git("push", "-u", "origin", "feature/sftp-user", cwd=self.local)

        self.marker = root / "invocation.json"
        self.executable = root / "fake-ansible"
        self.executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "with open(os.environ['ANSIBLE_TEST_MARKER'], 'w') as out:\n"
            "    json.dump({'args': sys.argv[1:], 'cwd': os.getcwd()}, out)\n"
            "print('PLAY RECAP ' + '*' * 50)\n"
            "print('sftp-server : ok=1 changed=1 unreachable=0 failed=0')\n"
            "sys.exit(int(os.getenv('ANSIBLE_TEST_EXIT', '0')))\n"
        )
        self.executable.chmod(0o755)
        self.env = patch.dict(os.environ, {"ANSIBLE_TEST_MARKER": str(self.marker)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.mr = FakeMR()
        settings = Settings(
            telegram_token="test", allowed_group_id=-1001,
            allowed_user_ids=frozenset({42}), git_repo_url=str(self.remote),
            git_target_branch="master", gitlab_url="https://gitlab.example",
            gitlab_project_id="devops/ansible/infra", gitlab_token="test",
            ansible_binary=str(self.executable),
        )
        self.pipeline = Pipeline(settings, mr_client=self.mr)

    def test_success_runs_exact_command_from_branch_then_creates_mr(self):
        result = self.pipeline.run("feature/sftp-user", 42)
        invocation = json.loads(self.marker.read_text())
        self.assertEqual(
            invocation["args"],
            ["-i", "nonprod", "sftp-server.yaml", "--tags=project_user_access"],
        )
        self.assertTrue(invocation["cwd"].endswith("/infra"))
        self.assertEqual(result.commit, git("rev-parse", "HEAD", cwd=self.local))
        self.assertEqual(len(self.mr.calls), 1)
        self.assertIn("PLAY RECAP", result.recap)

    def test_failed_ansible_does_not_create_mr(self):
        with patch.dict(os.environ, {"ANSIBLE_TEST_EXIT": "2"}):
            with self.assertRaisesRegex(WorkflowError, "Ansible thất bại"):
                self.pipeline.run("feature/sftp-user", 42)
        self.assertEqual(self.mr.calls, [])

    def test_branch_without_diff_does_not_run_ansible_or_create_mr(self):
        git("switch", "master", cwd=self.local)
        git("switch", "-c", "feature/empty", cwd=self.local)
        git("push", "-u", "origin", "feature/empty", cwd=self.local)
        with self.assertRaisesRegex(WorkflowError, "không có thay đổi"):
            self.pipeline.run("feature/empty", 42)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.mr.calls, [])

    def test_invalid_branch_is_rejected_before_git_or_ansible(self):
        for name in ("-bad", "../master", "foo..bar", "foo bar", "foo/.hidden"):
            with self.subTest(name=name), self.assertRaises(WorkflowError):
                validate_branch(name)

    def test_gitlab_client_creates_mr_with_tested_commit(self):
        requests_seen = []

        def opener(req, timeout):
            requests_seen.append(req)
            if req.get_method() == "GET":
                return io.BytesIO(b"[]")
            return io.BytesIO(b'{"web_url":"https://gitlab.example/mr/123"}')

        client = GitLabClient(self.pipeline.settings, opener=opener)
        url = client.create_or_get_mr("feature/sftp-user", "abc123", 42, "PLAY RECAP: ok")
        self.assertEqual(url, "https://gitlab.example/mr/123")
        self.assertIn("devops%2Fansible%2Finfra", requests_seen[0].full_url)
        payload = json.loads(requests_seen[1].data)
        self.assertEqual(payload["source_branch"], "feature/sftp-user")
        self.assertEqual(payload["target_branch"], "master")
        self.assertIn("abc123", payload["description"])


if __name__ == "__main__":
    unittest.main()
