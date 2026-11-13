import fcntl
import json
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="native macOS file primitive")

HARNESS = r"""
import Foundation
import Darwin

@main struct Harness {
    static func main() throws {
        let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        let action = CommandLine.arguments[2]
        if action == "prune" { SecureCredentialFile.removeEmptyOrphans(in: root); return }
        let file = root.appendingPathComponent("demo-credentials.json")
        let payload = Data("{\"api_key\":\"fake-test-marker\"}".utf8)
        umask(0)
        enum Injected: Error { case failure }
        do {
            try SecureCredentialFile.write(payload, to: file) { stage, path in
                let name: String
                switch stage { case .created: name = "created"; case .published: name = "published"; case .written: name = "written" }
                var info = stat()
                precondition(lstat(path.path, &info) == 0 && (info.st_mode & 0o777) == 0o600)
                if name != "written" { precondition(info.st_size == 0) }
                if action == "fail-" + name { throw Injected.failure }
                if action == "crash-" + name { kill(getpid(), SIGKILL) }
            }
            print("saved")
        } catch { print("failed") }
    }
}
"""


@pytest.fixture(scope="module")
def native_writer(tmp_path_factory):
    root = tmp_path_factory.mktemp("native-writer")
    harness = root / "Harness.swift"
    harness.write_text(HARNESS)
    executable = root / "writer"
    subprocess.run(
        [
            "xcrun",
            "swiftc",
            "packaging/macos/SecureCredentialFile.swift",
            str(harness),
            "-o",
            str(executable),
        ],
        check=True,
        capture_output=True,
    )
    return executable


def test_native_file_is_0600_from_birth_even_with_umask_zero(native_writer, tmp_path):
    result = subprocess.run(
        [str(native_writer), str(tmp_path), "save"], check=True, capture_output=True, text=True
    )
    assert result.stdout.strip() == "saved"
    file = tmp_path / "demo-credentials.json"
    assert file.stat().st_mode & 0o777 == 0o600
    assert json.loads(file.read_text())["api_key"] == "fake-test-marker"
    assert not list(tmp_path.glob(".demo-credentials.empty-*"))


@pytest.mark.parametrize("stage", ["created", "published", "written"])
def test_write_failure_removes_temporary_and_partial_copies(native_writer, tmp_path, stage):
    file = tmp_path / "demo-credentials.json"
    file.write_text("previous-config")
    file.chmod(0o600)
    result = subprocess.run(
        [str(native_writer), str(tmp_path), "fail-" + stage], check=True, capture_output=True, text=True
    )
    assert result.stdout.strip() == "failed"
    assert not list(tmp_path.glob(".demo-credentials.empty-*"))
    if stage == "created":
        assert file.read_text() == "previous-config"
    else:
        assert not file.exists()  # Incomplete replacement requires explicit re-entry.


@pytest.mark.parametrize("stage", ["created", "published", "written"])
def test_kill_cannot_leave_plaintext_temporary_copy(native_writer, tmp_path, stage):
    result = subprocess.run([str(native_writer), str(tmp_path), "crash-" + stage], capture_output=True)
    assert result.returncode == -9
    for path in tmp_path.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
        if path.name.startswith(".demo-credentials.empty-"):
            assert path.stat().st_size == 0
    subprocess.run([str(native_writer), str(tmp_path), "prune"], check=True)
    assert not list(tmp_path.glob(".demo-credentials.empty-*"))
    if stage == "published":
        assert (tmp_path / "demo-credentials.json").read_bytes() == b""


def test_publish_does_not_follow_destination_symlink(native_writer, tmp_path):
    target = tmp_path / "unrelated"
    target.write_text("unchanged")
    (tmp_path / "demo-credentials.json").symlink_to(target)
    subprocess.run([str(native_writer), str(tmp_path), "save"], check=True, capture_output=True)
    assert target.read_text() == "unchanged"
    assert not (tmp_path / "demo-credentials.json").is_symlink()


def test_native_writer_rejects_running_backend_without_touching_config(native_writer, tmp_path):
    file = tmp_path / "demo-credentials.json"
    file.write_text("previous-config")
    with (tmp_path / "desktop.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [str(native_writer), str(tmp_path), "save"], check=True, capture_output=True, text=True
        )
    assert result.stdout.strip() == "failed" and file.read_text() == "previous-config"
    assert not list(tmp_path.glob(".demo-credentials.empty-*"))


def test_filesystem_publish_failure_does_not_leave_temporary_copy(native_writer, tmp_path):
    (tmp_path / "demo-credentials.json").mkdir()
    result = subprocess.run(
        [str(native_writer), str(tmp_path), "save"], check=True, capture_output=True, text=True
    )
    assert result.stdout.strip() == "failed"
    assert not list(tmp_path.glob(".demo-credentials.empty-*"))
