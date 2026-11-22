"""Standalone launcher for the independently provisioned ZLIN-21 v2 CLI."""

import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

SCRIPT = Path(__file__).resolve()
DEPLOYMENT = Path("/opt/zlin21/v2")
CONFIG = Path("/etc/zlin21/v2/authority.json")
PIN = CONFIG.with_suffix(".sha256")
STATE = Path("/var/lib/zlin21/v2")
VERIFIER = Path("/usr/bin/openssl")
VERIFIER_ENV = {"LC_ALL": "C", "OPENSSL_CONF": "/dev/null"}


def root_controlled(path):
    """Check a chain which the research account must not be able to replace."""
    for entry in (*reversed(path.parents), path):
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(f"untrusted ownership or permissions: {entry}")


def private_state(path, uid, *, production):
    if not path.is_absolute() or path != path.resolve():
        raise ValueError("state directory must be a canonical absolute path")
    if production:
        if path != STATE:
            raise ValueError("wrong production state directory")
        root_controlled(path.parent)
    elif path.is_relative_to(STATE.parent):
        raise ValueError("synthetic state cannot use the production state tree")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("state directory must be private to the evaluation account")


def launcher_path(root):
    return root / "research/spot_defense/v2/launcher.py"


def production_ready():
    if Path(sys.executable).resolve() != Path("/usr/bin/python3").resolve():
        raise ValueError("wrong production Python interpreter")
    if not CONFIG.is_file() or not PIN.is_file():
        raise ValueError("independent production trust root has not been provisioned")
    if SCRIPT != launcher_path(DEPLOYMENT):
        raise ValueError("production launcher is outside the provisioned installation")
    for path in (CONFIG, PIN, SCRIPT, VERIFIER, Path(sys.executable).resolve()):
        root_controlled(path)
    # Check permissions before importing any code from the installation.
    for path in (DEPLOYMENT / "research").rglob("*"):
        root_controlled(path)


def load_authority(config_path, expected_hash, *, synthetic):
    from research.spot_defense.v2.evidence import checksum, digest, fields, instant, unpack
    from research.spot_defense.v2.gate import ReviewAuthority, command_sha256, source_sha256

    body = config_path.read_bytes()
    if digest(body) != checksum(expected_hash):
        raise ValueError("authority configuration hash differs from the trusted pin")
    config = unpack(body)
    fields(
        config,
        "schema profile reviewer_id reviewer_public_key reviewer_key_sha256 revision "
        "implementation_sha256 commands_sha256 launcher_sha256 verifier_path verifier_sha256 "
        "verifier_version python_path python_sha256 state_directory state_uid research_uid synthetic_now",
    )
    profile = "synthetic" if synthetic else "production"
    if type(config["schema"]) is not int or config["schema"] != 1 or config["profile"] != profile:
        raise ValueError("authority profile mismatch")
    uid = os.geteuid()
    if type(config["state_uid"]) is not int or config["state_uid"] != uid:
        raise ValueError("launcher must run as the pinned evaluation account")
    if not synthetic:
        research_uid = config["research_uid"]
        if type(research_uid) is not int or min(research_uid, uid) <= 0 or research_uid == uid:
            raise ValueError("research account must be separate from the production evaluation account")
        if config["synthetic_now"] is not None:
            raise ValueError("production clock cannot be overridden")
    now = instant(config["synthetic_now"]) if synthetic else datetime.now(UTC)
    state_directory = Path(config["state_directory"])
    private_state(state_directory, uid, production=not synthetic)

    interpreter = Path(sys.executable).resolve()
    if (
        config["launcher_sha256"] != digest(SCRIPT.read_bytes())
        or config["implementation_sha256"] != source_sha256()
        or config["commands_sha256"] != command_sha256()
        or config["python_path"] != str(interpreter)
        or config["python_sha256"] != digest(interpreter.read_bytes())
        or config["verifier_path"] != str(VERIFIER)
        or config["verifier_sha256"] != digest(VERIFIER.read_bytes())
    ):
        raise ValueError("launcher, code, commands or verifier differ from the authority binding")
    version = subprocess.run(
        [str(VERIFIER), "version"], check=True, capture_output=True, text=True, env=VERIFIER_ENV, timeout=10
    ).stdout.rstrip("\n")
    if version != config["verifier_version"]:
        raise ValueError("verifier version differs from the authority binding")
    key = config["reviewer_public_key"].encode("ascii")
    if digest(key) != checksum(config["reviewer_key_sha256"]):
        raise ValueError("reviewer key fingerprint differs from the authority binding")
    public_key_info = subprocess.run(
        [str(VERIFIER), "pkey", "-pubin", "-text_pub", "-noout"],
        input=key,
        check=True,
        capture_output=True,
        env=VERIFIER_ENV,
        timeout=10,
    ).stdout
    if not public_key_info.startswith(b"ED25519 Public-Key:"):
        raise ValueError("approval key must be Ed25519")
    return ReviewAuthority(
        config["reviewer_id"],
        key,
        config["reviewer_key_sha256"],
        config["revision"],
        config["implementation_sha256"],
        config["commands_sha256"],
        state_directory,
        profile,
        expected_hash,
        VERIFIER,
    ), now


def main():
    try:
        if not sys.flags.isolated or not sys.dont_write_bytecode:
            raise ValueError("invoke launcher with the pinned Python interpreter and -I -B")
        args = sys.argv[1:]
        root = SCRIPT.parents[3]
        if args[:1] == ["--synthetic"]:
            if len(args) < 4:
                raise ValueError("--synthetic requires an authority path, SHA-256 and command")
            config, pin = Path(args[1]), args[2]
            args = args[3:]
            synthetic = True
        else:
            production_ready()
            config, pin, synthetic = CONFIG, PIN.read_text(encoding="ascii").strip(), False
        sys.path.insert(0, str(root))
        from research.spot_defense.v2.run import entrypoint

        authority, now = load_authority(config, pin, synthetic=synthetic)
        entrypoint(args, authority=authority, now=now)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        print(f"evaluation launcher refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
