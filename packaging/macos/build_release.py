"""Build a self-contained arm64 app/DMG and a source companion from tracked files."""

import argparse
import hashlib
import importlib.metadata
import io
import json
import plistlib
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import zipfile
from pathlib import Path

BASE = "0dae104cf4799344a26902233b3fb1ef14aab327"
VERSION = "0.1.0-macos.3"
ROOT = Path(__file__).resolve().parents[2]


def run(*args):
    result = subprocess.run([str(a) for a in args], cwd=ROOT, capture_output=True, text=True)
    if result.returncode:
        print(result.stderr[-10000:], file=sys.stderr)
        result.check_returncode()
    return result.stdout


def sha(path):
    return hashlib.file_digest(path.open("rb"), "sha256").hexdigest()


def source_zip(output, commit):
    archive = subprocess.check_output(["git", "archive", "--format=tar", commit], cwd=ROOT)
    core = {}
    with (
        tarfile.open(fileobj=io.BytesIO(archive)) as tar,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as dest,
    ):
        for member in tar.getmembers():
            if not member.isfile():
                continue
            path = Path(member.name)
            allowed = path.parts[0] in {"okx_workbench", "tests", "scripts", "docs", "packaging", ".github"}
            allowed |= len(path.parts) == 1 and path.name in {
                ".env.example",
                ".gitignore",
                "README.md",
                "pyproject.toml",
                "uv.lock",
                "安装源码依赖.command",
                "启动源码离线演练.command",
            }
            if (
                not allowed
                or path.name == ".env"
                or any(part in {".secrets", "data", "__pycache__", ".venv"} for part in path.parts)
            ):
                raise RuntimeError(f"Unexpected tracked artifact: {path}")
            content = tar.extractfile(member).read()
            if path.parts[0] == "okx_workbench" or path.name in {"uv.lock", "pyproject.toml"}:
                core[member.name] = hashlib.sha256(content).hexdigest()
            item = zipfile.ZipInfo("OKX-Workbench-Source/" + member.name, (2026, 11, 13, 0, 0, 0))
            item.external_attr = (0o100000 | member.mode) << 16
            item.compress_type = zipfile.ZIP_DEFLATED
            dest.writestr(item, content)
    return core


def copy_licenses(directory):
    directory.mkdir(parents=True)
    python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("CPython license is required for redistributing the embedded runtime")
    shutil.copyfile(python_license, directory / "CPython-LICENSE.txt")
    lines = []
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"]
        lines.append(f"{name}=={dist.version}")
        for file in dist.files or []:
            if any("license" in part.lower() or "copying" in part.lower() for part in file.parts):
                source = Path(dist.locate_file(file))
                if source.is_file():
                    target = directory / name / Path(*file.parts[1:])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
    (directory / "DEPENDENCIES.txt").write_text("\n".join(sorted(lines)) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    work, output = args.build_dir.resolve(), args.output_dir.resolve()
    if work.exists() or output.exists():
        raise RuntimeError("Use new empty build/output paths; no existing files are overwritten")
    run("git", "diff", "--exit-code", BASE, "--", "okx_workbench", "uv.lock", "pyproject.toml")
    if run("git", "status", "--porcelain").strip():
        raise RuntimeError("Commit packaging changes before release")
    work.mkdir(parents=True)
    output.mkdir(parents=True)
    commit = run("git", "rev-parse", "HEAD").strip()
    prefix = "OKX-Workbench-0.1.0-macos.3-arm64"
    source = output / (prefix + "-source.zip")
    core = source_zip(source, commit)
    run(
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        work / "frozen",
        "--workpath",
        work / "analysis",
        ROOT / "packaging/macos/OKXServer.spec",
    )
    stage = work / "image"
    bundle = stage / "OKX Workbench.app"
    macos = bundle / "Contents/MacOS"
    resources = bundle / "Contents/Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)
    shutil.copytree(work / "frozen/OKXServer", resources / "server", symlinks=True)
    run(
        "xcrun",
        "swiftc",
        "-O",
        "-target",
        "arm64-apple-macos26.5",
        "-framework",
        "AppKit",
        ROOT / "packaging/macos/Launcher.swift",
        ROOT / "packaging/macos/SecureCredentialFile.swift",
        "-o",
        macos / "OKXWorkbench",
    )
    info = {
        "CFBundleIdentifier": "com.zlin.okxworkbench.offline",
        "CFBundleName": "OKX Workbench",
        "CFBundleDisplayName": "OKX 工作台",
        "CFBundleExecutable": "OKXWorkbench",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "3",
        "LSMinimumSystemVersion": "26.5",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "OKX Workbench — independent demo strategy workbench",
    }
    (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    copy_licenses(resources / "licenses")
    manifest = {
        "version": VERSION,
        "application_source_commit": BASE,
        "packaging_commit": commit,
        "architecture": "arm64",
        "minimum_macos": "26.5",
        "tested_macos": "26.5.1",
        "embedded_python": sys.version.split()[0],
        "pyinstaller": importlib.metadata.version("pyinstaller"),
        "default_mode": "demo",
        "default_source": "offline",
        "supported_sources": ["offline", "okx_demo"],
        "desktop_max_order_usdt": "25",
        "demo_credentials_storage": "native input, user-owned 0600 file outside bundle/repository",
        "live_supported": False,
        "credentials_included": False,
        "signature": "ad-hoc",
        "developer_id": False,
        "notarized": False,
        "latest_real_okx_demo_revalidated": False,
        "core_file_sha256": core,
        "source_zip_sha256": sha(source),
    }
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    (resources / "RELEASE.json").write_text(text)
    (output / "RELEASE.json").write_text(text)
    (stage / "RELEASE.json").write_text(text)
    for target in (stage, output):
        shutil.copyfile(ROOT / "packaging/macos/快速开始.md", target / "快速开始.md")
    shutil.copyfile(source, stage / source.name)
    (stage / "Applications").symlink_to("/Applications")
    (stage / "SHA256SUMS-source.txt").write_text(f"{sha(source)}  {source.name}\n")
    run("codesign", "--force", "--deep", "--sign", "-", "--timestamp=none", bundle)
    run("codesign", "--verify", "--deep", "--strict", bundle)
    dmg = output / (prefix + ".dmg")
    run("hdiutil", "create", "-volname", "OKX Workbench", "-srcfolder", stage, "-format", "UDZO", "-ov", dmg)
    checksums = [f"{sha(path)}  {path.name}" for path in (dmg, source, output / "RELEASE.json")]
    (output / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n")
    print(
        json.dumps(
            {"dmg": dmg.name, "bytes": dmg.stat().st_size, "packaging_commit": commit, "sha256": sha(dmg)},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
