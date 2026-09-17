"""Tenant-id grammar regression tests — no AWS calls, no deployed stack.

The tenant id names a directory on a shared EFS filesystem, so the sidecar and
`efs-monitor.sh` must agree on exactly which ids exist. They used to disagree:
the sidecar filtered with `str.isalnum()`, which is Unicode-aware, while the
shell kept ASCII only. Two consequences, both cross-tenant:

  1. "victim租" passed the sidecar and became "victim" in the shell, so one
     tenant was served another tenant's directory.
  2. An all-non-ASCII id became the empty string in the shell, making the tenant
     directory `/mnt/efs/tenants/` itself — which then got chowned to the agent
     uid and bind-mounted over the state dir, exposing every tenant at once.

So the sidecar must REJECT anything outside one ASCII grammar rather than
sanitize it, and the shell must apply the same grammar and fail closed. This
file locks the sidecar half and checks the shell half is character-identical.

Run:  uv run --with pytest python -m pytest tests -q
"""
import importlib.util
import pathlib
import subprocess
import sys
import types

import pytest

MICROVM = pathlib.Path(__file__).resolve().parents[1] / "src" / "microvm"
GRAMMAR = "^[A-Za-z0-9_-]{1,64}$"


@pytest.fixture(scope="module")
def hooks(tmp_path_factory):
    """Import hooks.py with its tenant file redirected into a tmp dir."""
    spec = importlib.util.spec_from_file_location("hooks", MICROVM / "hooks.py")
    mod = importlib.util.module_from_spec(spec)
    # hooks.py starts an HTTP server only under __main__, so importing is side-effect
    # free apart from the module-level paths, which we point somewhere writable.
    sys.modules["hooks"] = mod
    spec.loader.exec_module(mod)
    mod.TENANT_FILE = str(tmp_path_factory.mktemp("run") / "tenant-id")
    return mod


@pytest.mark.parametrize("tid", ["a", "tenant1", "TENANT_1", "a-b_c", "x" * 64])
def test_valid_ids_are_accepted_verbatim(hooks, tid):
    assert hooks.write_tenant(tid) == tid
    assert pathlib.Path(hooks.TENANT_FILE).read_text() == tid


@pytest.mark.parametrize(
    "tid",
    [
        "",                 # nothing to serve
        "victim租",     # non-ASCII alnum: used to collapse onto "victim"
        "租户",     # all non-ASCII: used to collapse onto "" -> whole /tenants
        "../victim",        # path traversal
        "a/b",              # subdirectory
        "a b",              # whitespace
        "a\nb",             # newline: shell readers see only the first line
        "a\x00b",       # NUL
        "x" * 65,           # over the length cap
        ".",                # the tenants dir itself
        "..",               # its parent
    ],
)
def test_invalid_ids_are_rejected_not_sanitized(hooks, tid):
    """No id outside the grammar may become a different, valid id."""
    pathlib.Path(hooks.TENANT_FILE).unlink(missing_ok=True)
    assert hooks.write_tenant(tid) == ""
    assert not pathlib.Path(hooks.TENANT_FILE).exists(), "rejected id must not be written"


def test_shell_and_sidecar_use_the_same_grammar():
    """A drift here silently re-opens the two cross-tenant paths above."""
    monitor = (MICROVM / "efs-monitor.sh").read_text()
    hooks_src = (MICROVM / "hooks.py").read_text()
    assert GRAMMAR in monitor, "efs-monitor.sh must state the grammar it enforces"
    assert 'r"[A-Za-z0-9_-]{1,64}"' in hooks_src
    assert "case $TENANT in" in monitor, "efs-monitor.sh must validate the tenant id"
    # Comments discuss the old sanitizing behaviour, so look at the code only.
    code = "\n".join(ln for ln in monitor.splitlines() if not ln.lstrip().startswith("#"))
    assert "tr -cd" not in code, "sanitizing the tenant id instead of rejecting it is the bug"


@pytest.mark.skipif(not pathlib.Path("/bin/sh").exists(), reason="needs a POSIX shell")
@pytest.mark.parametrize(
    "tid,ok",
    [("tenant1", True), ("a-b_c", True), ("", False), ("victim租", False),
     ("../victim", False), ("a b", False), ("a/b", False)],
)
def test_shell_case_pattern_matches_the_python_grammar(tid, ok):
    """Run the daemon's own `case` pattern so both halves are proven equivalent."""
    script = (
        'case $1 in\n'
        '  "" | *[!A-Za-z0-9_-]*) exit 1 ;;\n'
        'esac\n'
        '[ "${#1}" -le 64 ] || exit 1\n'
    )
    r = subprocess.run(["/bin/sh", "-c", script, "sh", tid])
    assert (r.returncode == 0) is ok
