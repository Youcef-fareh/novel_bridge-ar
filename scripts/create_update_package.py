"""Create a portable baseline and a changed-files-only Inno update installer."""
from __future__ import annotations

import argparse
import hashlib
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Optional

_APP_ID_LINE = "AppId={{B3A7E1F2-9C4D-4E8A-B1F3-2D5A6C7E8F9A}"
_PRESERVED_FILES = {
    ".env",
    "config/google_client_secret.json",
    "config/glossary.json",
    "config/sites.json",
}
_PRESERVED_DIRS = {"data", "output"}


def _version(app_dir: Path) -> str:
    return (app_dir / "VERSION").read_text(encoding="utf-8-sig").strip().removeprefix("v")


def _preserve(relative_path: PurePosixPath) -> bool:
    path = relative_path.as_posix().casefold()
    return path in _PRESERVED_FILES or relative_path.parts[0].casefold() in _PRESERVED_DIRS


def create_portable_archive(source_dir: Path, output_path: Path) -> Path:
    source_dir = source_dir.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source in sorted(source_dir.rglob("*")):
            if source.is_file():
                archive.write(source, (Path("NovelBridgeAR") / source.relative_to(source_dir)).as_posix())
    return output_path


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_update_package(
    previous_dir: Path,
    current_dir: Path,
    build_dir: Path,
) -> Optional[Path]:
    previous_dir = previous_dir.resolve()
    current_dir = current_dir.resolve()
    build_dir = build_dir.resolve()
    if build_dir.exists():
        shutil.rmtree(build_dir)

    changed_files: list[Path] = []
    for current_file in sorted(path for path in current_dir.rglob("*") if path.is_file()):
        relative = PurePosixPath(current_file.relative_to(current_dir).as_posix())
        if _preserve(relative):
            continue
        previous_file = previous_dir.joinpath(*relative.parts)
        if not previous_file.is_file() or _digest(previous_file) != _digest(current_file):
            changed_files.append(relative)

    if not changed_files:
        return None

    base_version = _version(previous_dir)
    target_version = _version(current_dir)
    payload_dir = build_dir / "payload"
    payload_dir.mkdir(parents=True)
    for relative in changed_files:
        destination = payload_dir.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(current_dir.joinpath(*relative.parts), destination)

    source_lines = []
    for relative in changed_files:
        source_path = "\\".join(("payload", *relative.parts))
        parent = relative.parent.as_posix()
        destination = "{app}" if parent == "." else "{app}\\" + parent.replace("/", "\\")
        source_lines.append(
            f'Source: "{source_path}"; DestDir: "{destination}"; Flags: ignoreversion'
        )

    update_name = f"NovelBridgeAR_Update_{base_version}_to_{target_version}"
    script = "\n".join([
        "; Generated changed-files-only update package. Do not edit by hand.",
        "[Setup]",
        _APP_ID_LINE,
        "AppName=NovelBridge AR",
        f"AppVersion={target_version}",
        r"DefaultDirName={autopf}\NovelBridge AR",
        "DisableDirPage=yes",
        "Uninstallable=no",
        "PrivilegesRequired=admin",
        "PrivilegesRequiredOverridesAllowed=dialog",
        r"OutputDir=..\Output",
        f"OutputBaseFilename={update_name}",
        "Compression=lzma2/max",
        "SolidCompression=yes",
        "WizardStyle=modern",
        "",
        "[Languages]",
        'Name: "english"; MessagesFile: "compiler:Default.isl"',
        "",
        "[Files]",
        *source_lines,
        "",
        "[Run]",
        r'Filename: "{app}\NovelBridgeAR.exe"; Flags: nowait',
        "",
    ])
    build_dir.mkdir(parents=True, exist_ok=True)
    script_path = build_dir / "update.iss"
    script_path.write_text(script, encoding="utf-8")
    return script_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--portable-output", type=Path, required=True)
    parser.add_argument("--previous-dir", type=Path)
    parser.add_argument("--update-build-dir", type=Path, required=True)
    args = parser.parse_args()

    create_portable_archive(args.source, args.portable_output)
    if args.previous_dir and args.previous_dir.is_dir():
        script = create_update_package(args.previous_dir, args.source, args.update_build_dir)
        if script:
            print(f"Prepared incremental update script: {script}")
        else:
            print("No changed application files; no incremental update is needed.")
    else:
        print("No prior portable release is available; this release will use the full installer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())