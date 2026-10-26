"""Reproducible Windows build for RDP Nexus.

    python scripts/build.py                 # tests -> exe (installer folder + portable) -> installer -> checksums
    python scripts/build.py --skip-tests
    python scripts/build.py --no-installer  # when Inno Setup is not installed
    python scripts/build.py --expect-version 1.2.3   # CI: fail if the tag and app version differ

Outputs (all in ./release):
    RDPNexus-<version>-setup.exe      Inno Setup installer (per-user, no admin rights)
    RDPNexus-<version>-portable.exe   single-file portable executable
    SHA256SUMS.txt                    checksums of the files above
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(ROOT, "dist")
RELEASE = os.path.join(ROOT, "release")
ASSETS = os.path.join(ROOT, "assets")
ISS = os.path.join(ROOT, "installer", "setup.iss")

VERSION_INFO = """# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers={tup}, prodvers={tup}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'ADevlooper'),
      StringStruct('FileDescription', 'RDP Nexus'),
      StringStruct('FileVersion', '{ver}'),
      StringStruct('InternalName', 'RDPNexus'),
      StringStruct('LegalCopyright', 'Copyright (c) 2026 ADevlooper. MIT License.'),
      StringStruct('OriginalFilename', 'RDPNexus.exe'),
      StringStruct('ProductName', 'RDP Nexus'),
      StringStruct('ProductVersion', '{ver}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def run(cmd: list[str]) -> None:
    print(">", " ".join(f'"{c}"' if " " in c else c for c in cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def app_version() -> str:
    with open(os.path.join(SRC, "app_info.py"), encoding="utf-8") as fh:
        m = re.search(r'^APP_VERSION\s*=\s*"(\d+\.\d+\.\d+)"', fh.read(), re.M)
    if not m:
        raise SystemExit("APP_VERSION not found in src/app_info.py")
    return m.group(1)


def find_iscc() -> str | None:
    candidates = [os.environ.get("ISCC"), shutil.which("iscc")]
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        if base:
            candidates.append(os.path.join(base, "Inno Setup 6", "ISCC.exe"))
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def pyinstaller(version_file: str, onefile: bool) -> str:
    dist = os.path.join(DIST, "portable" if onefile else "")
    args = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--log-level", "WARN",
        "--name", "RDPNexus",
        "--icon", os.path.join(ASSETS, "app.ico"),
        "--add-data", f"{os.path.join(ASSETS, 'app.ico')};assets",
        "--add-data", f"{os.path.join(ASSETS, 'app.png')};assets",
        "--collect-data", "customtkinter",
        "--hidden-import", "pystray._win32",
        "--version-file", version_file,
        "--paths", SRC,
        "--distpath", dist,
        "--workpath", os.path.join(BUILD, "portable" if onefile else "onedir"),
        "--specpath", BUILD,
    ]
    if onefile:
        args.append("--onefile")
    args.append(os.path.join(SRC, "main.py"))
    run(args)
    return os.path.join(dist, "RDPNexus.exe") if onefile else os.path.join(dist, "RDPNexus")


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-tests", action="store_true")
    ap.add_argument("--no-installer", action="store_true")
    ap.add_argument("--expect-version")
    args = ap.parse_args()

    version = app_version()
    if args.expect_version and args.expect_version.lstrip("v") != version:
        raise SystemExit(f"Tag version {args.expect_version} does not match APP_VERSION {version}")
    print(f"Building RDP Nexus {version}")

    if not args.skip_tests:
        run([sys.executable, "-m", "pytest", "-q"])

    for folder in (DIST, RELEASE):
        shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(BUILD, exist_ok=True)
    os.makedirs(RELEASE, exist_ok=True)

    version_file = os.path.join(BUILD, "version_info.txt")
    parts = tuple(int(p) for p in version.split(".")) + (0,)
    with open(version_file, "w", encoding="utf-8") as fh:
        fh.write(VERSION_INFO.format(tup=parts, ver=version))

    onedir = pyinstaller(version_file, onefile=False)
    portable = pyinstaller(version_file, onefile=True)
    artifacts = [shutil.copy2(portable, os.path.join(RELEASE, f"RDPNexus-{version}-portable.exe"))]

    if not args.no_installer:
        iscc = find_iscc()
        if not iscc:
            raise SystemExit("Inno Setup (ISCC.exe) not found. Install Inno Setup 6, set ISCC=<path>, "
                             "or pass --no-installer.")
        run([iscc, "/Q", f"/DAppVersion={version}", f"/DSourceDir={onedir}", f"/DOutputDir={RELEASE}", ISS])
        artifacts.append(os.path.join(RELEASE, f"RDPNexus-{version}-setup.exe"))

    with open(os.path.join(RELEASE, "SHA256SUMS.txt"), "w", encoding="utf-8", newline="\n") as fh:
        for path in artifacts:
            fh.write(f"{sha256(path)}  {os.path.basename(path)}\n")

    print("\nArtifacts:")
    for path in artifacts + [os.path.join(RELEASE, "SHA256SUMS.txt")]:
        print(f"  {os.path.relpath(path, ROOT)}  ({os.path.getsize(path) / 1_048_576:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
