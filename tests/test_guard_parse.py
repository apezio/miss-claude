#!/usr/bin/env python3
"""prevent-misswork.py judges what a command EXECUTES, not the words it contains.

Runs the hook as a subprocess (JSON event on stdin, CLAUDE_MISS_ROLE in env) against
throwaway repos: a feature worktree on claude/x, the same repo checked out on master.
stdlib only:  python3 -m unittest tests/test_guard_parse.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, ".claude", "hooks", "prevent-misswork.py")
SHIP = "/home/x/repo/scripts/miss-ship.py"
TMP = None
REPO = None      # main checkout, on master (its staging is `working`)
WT = None        # feature worktree on claude/x
INT = None       # integration checkout on working


def git(cwd, *a):
    subprocess.run(["git", "-C", cwd, *a], check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))


def setUpModule():
    global TMP, REPO, WT, INT
    TMP = tempfile.mkdtemp(prefix="guardparse-")
    REPO = os.path.join(TMP, "repo")
    os.makedirs(REPO)
    git(REPO, "init", "-q", "-b", "master")
    open(os.path.join(REPO, "f"), "w").close()
    git(REPO, "add", "f")
    git(REPO, "commit", "-qm", "init")
    git(REPO, "branch", "working")
    WT = os.path.join(TMP, "wt", "x")
    git(REPO, "worktree", "add", "-q", WT, "-b", "claude/x", "working")
    INT = os.path.join(TMP, "wt", ".integration")
    git(REPO, "worktree", "add", "-q", INT, "working")


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def run_hook(command, role, cwd):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("MISS_") and k not in ("PRIMARY_REPO", "BASE_BRANCH")}
    env.update({"CLAUDE_MISS_ROLE": role, "PRIMARY_REPO": REPO,
                "WORKTREES_DIR": os.path.join(TMP, "wt"), "BASE_BRANCH": "working"})
    ev = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}
    r = subprocess.run([sys.executable, HOOK], input=json.dumps(ev), capture_output=True,
                       text=True, env=env)
    return r.returncode, r.stderr


class Base(unittest.TestCase):
    role = "feature"

    def cwd(self):
        return WT

    def allowed(self, *commands):
        for c in commands:
            rc, err = run_hook(c, self.role, self.cwd())
            self.assertEqual(rc, 0, "expected ALLOWED (%s): %r\n%s" % (self.role, c, err))

    def blocked(self, *commands):
        for c in commands:
            rc, err = run_hook(c, self.role, self.cwd())
            self.assertEqual(rc, 2, "expected BLOCKED (%s): %r" % (self.role, c))


PY_HEREDOC = "python3 - <<'EOF'\nimport subprocess\n" \
             "print('never: git push; sudo systemctl restart x')\nEOF"


class FeatureAllows(Base):
    def test_mentions_are_data(self):
        self.allowed(
            'grep -n "npm ci" scripts/deploy.sh | head',
            "cat scripts/deploy-remote.sh",
            'git commit -m "run scripts/deploy.sh; git push"',
            "git commit -m 'run scripts/deploy.sh; git push'",
            'git commit -m "docs: git push is done by the ship script\n\n'
            'Also: sudo systemctl restart is the deploy step."',
            'echo "git push sudo migrate"',
            PY_HEREDOC,
            "cat <<'EOF' > /tmp/x\ngit push\nsudo ls\nEOF",
            "cat <<EOF\ngit push\nEOF",
            'grep -rn "sudo systemctl restart" docs/',
            'echo "git merge --ff-only"',
            "echo hi # git push",
            "# don't push\necho hi",
        )

    def test_everyday_git_and_shell(self):
        self.allowed(
            "git checkout -- file", "git checkout -b x", "git switch -c x",
            "git checkout HEAD -- file", "git worktree list", "git branch -a",
            "git stash push -u -m t", "git add app.py && git commit -m x",
            "npm run lint 2>&1 | tail",
            "find . -name '*.sh' -exec grep -l deploy {} +",
            "diff <(sed s/x// a) <(sed s/x// b)",
            "echo '$(git push)'", "echo $((1+2))",
            "for f in a b; do echo $f; done",
            "if git status; then echo ok; fi",
            "systemctl status foo", "git -C %s log --oneline -3" % REPO,
            "python3 - <<EOF\nprint('git push')\nEOF",
            "ssh host 'git status'",
        )

    def test_ship_script(self):
        self.allowed(
            'python3 %s --approval "YES SHIP" --request "git push it; deploy" --tests "ok"'
            % SHIP,
            'python3 %s --approval "YES SHIP" --request "git push it; deploy" --tests "ok"'
            ' | tail -10' % SHIP,
        )


class FeatureBlocks(Base):
    def test_plain_and_git_option_forms(self):
        self.blocked(
            "git push", "git -C /x push", "git -c a=b push", "git --no-pager push",
            "git merge --ff-only x", "git worktree add ../x", "git branch -d x",
            "git branch -m y", "git branch -D x", "git branch --delete x",
            "git switch working", "git checkout working", "git checkout file",
            "npm run lint && git push", "git push origin working # python3 miss-ship.py",
        )

    def test_evasion_shapes(self):
        self.blocked(
            "echo hi\ngit push",
            "echo $(git push)", "echo `git push`", 'echo "$(git push)"', 'echo "`git push`"',
            "X=$(git push) echo hi", "diff <(git push) /dev/null",
            'bash -c "git push"', "bash -lc 'git push'", 'eval "git push"',
            "bash <<'EOF'\ngit push\nEOF", "sh <<EOF\ngit push\nEOF",
            "sudo bash <<'EOF'\nls\nEOF",
            "bash <<< 'git push'",
            "ssh host 'git push'", "ssh -p 20022 -o X=y user@host git push",
            "ssh host <<'EOF'\ncd /x\nsudo systemctl restart x\nEOF",
            "sudo -u alice git push", "sudo systemctl restart x", "systemctl --user restart x",
            "env FOO=1 git push", "nohup git push &", "timeout 60 git push",
            "xargs -I{} git push {}", "find . -exec git push \\;",
            'su -c "git push" alice', 'bash -c "echo hi; sudo ls"',
            'python3 %s --approval "YES SHIP" --request x; git push' % SHIP,
            "git commit -m 'unbalanced; git push",          # unparseable -> fallback
            "python3 - <<EOF\n$(git push)\nEOF",             # unquoted tag: expansion runs
            "(git push)", "{ git push; }", "true || git push", "git push 2>&1 | tail",
            "/usr/bin/git push", "\\git push", '"git" push', "command git push",
            "trap 'git push' EXIT", "tmux send-keys 'git push' Enter",
            "if true; then git push; fi", "time git push",
        )


class MasterBlocks(Base):
    def cwd(self):
        return REPO

    def test_blocked(self):
        self.blocked(
            "git commit -m x", "git add f", "rm -rf x", "cp a b", "mv a b",
            "echo x > f", "echo x >> f", "echo x | tee f", "curl -sSLo f url",
            "curl -o f url", "wget url", "pip install x", "pip3 install x",
            "python3 -m pip install x", "sudo ls", "chmod +x f",
            "git checkout -- f", "git checkout working", "git switch working",
            "git stash", "git push", "cmd &> f",
        )

    def test_allowed(self):
        self.allowed(
            "git checkout -b x", "git switch -c x", "git status", "cat f", "grep -rn x .",
            'echo "x > y"', "cmd 2>&1 | tail", "curl -s url", 'grep -rn "rm -rf" docs/',
            "echo x >&2", "git log --oneline", "git branch -a", "git diff",
        )


class Integrator(Base):
    role = "integrator"

    def cwd(self):
        return INT

    def test_allowed(self):
        self.allowed(
            "git merge --ff-only claude/x", "git -C %s merge --ff-only claude/x" % INT,
            "git push origin working", "git merge --abort",
            'git commit -m "never git push --force"',
            "sudo systemctl restart mission-dashboard.service",
        )

    def test_blocked(self):
        self.blocked(
            "git push --force origin working", "git push -f", "git push --force-with-lease",
            "git push --force-with-lease=working origin working", "git push origin +working",
            "git -C /x push --force", "git rebase working", "git merge claude/x",
            "git merge --no-ff claude/x", 'bash -c "git merge claude/x"',
            "echo $(git rebase working)",
        )


if __name__ == "__main__":
    unittest.main()
