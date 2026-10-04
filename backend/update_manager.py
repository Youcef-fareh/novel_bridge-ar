"""Find and download verified updates published with GitHub Releases."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

GITHUB_REPOSITORY = "Youcef-fareh/novel_bridge-ar"
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
_SHA256_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")


@dataclass(frozen=True)
class UpdatePlan:
    version: str
    release_url: str
    release_notes: str
    asset_name: str
    asset_url: str
    checksum_url: str
    size_bytes: int
    is_incremental: bool


def _version_key(version: str) -> tuple[int, int, int, int, str]:
    match = _VERSION_RE.fullmatch(version.strip())
    if not match:
        raise ValueError(f"Unsupported application version: {version}")
    major, minor, patch = (int(match.group(index)) for index in range(1, 4))
    prerelease = match.group(4) or ""
    return major, minor, patch, int(not prerelease), prerelease


def read_installed_version() -> str:
    if getattr(sys, "frozen", False):
        version_file = Path(sys.executable).resolve().parent / "VERSION"
    else:
        version_file = Path(__file__).resolve().parents[1] / "VERSION"
    return version_file.read_text(encoding="utf-8-sig").strip()


def select_update(releases: list[dict], current_version: str) -> Optional[UpdatePlan]:
    current_key = _version_key(current_version)
    stable_releases = [
        release for release in releases
        if not release.get("draft") and not release.get("prerelease")
    ]
    if not stable_releases:
        return None

    latest = max(stable_releases, key=lambda release: _version_key(release["tag_name"]))
    latest_version = latest["tag_name"].removeprefix("v")
    if _version_key(latest_version) <= current_key:
        return None

    assets = {asset["name"]: asset for asset in latest.get("assets", [])}
    delta_name = f"NovelBridgeAR_Update_{current_version.removeprefix('v')}_to_{latest_version}.exe"
    full_name = "NovelBridgeAR_Setup.exe"
    asset_name = delta_name if delta_name in assets else full_name
    asset = assets.get(asset_name)
    checksum = assets.get(f"{asset_name}.sha256")
    if not asset or not checksum:
        return None

    return UpdatePlan(
        version=latest_version,
        release_url=latest.get("html_url", ""),
        release_notes=latest.get("body", "").strip(),
        asset_name=asset_name,
        asset_url=asset["browser_download_url"],
        checksum_url=checksum["browser_download_url"],
        size_bytes=int(asset.get("size", 0)),
        is_incremental=asset_name == delta_name,
    )


def check_for_update(current_version: str) -> Optional[UpdatePlan]:
    repository = os.getenv("NOVELBRIDGE_GITHUB_REPOSITORY", GITHUB_REPOSITORY)
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/releases?per_page=100",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "NovelBridgeAR"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        releases = json.load(response)
    return select_update(releases, current_version)


def download_update_asset(plan: UpdatePlan, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    checksum_request = urllib.request.Request(
        plan.checksum_url, headers={"User-Agent": "NovelBridgeAR"}
    )
    with urllib.request.urlopen(checksum_request, timeout=20) as response:
        checksum_text = response.read(4096).decode("ascii", errors="replace")
    checksum_match = _SHA256_RE.search(checksum_text)
    if not checksum_match:
        raise ValueError("The update release did not provide a valid SHA-256 checksum.")
    expected_digest = checksum_match.group(1).lower()

    destination = directory / Path(plan.asset_name).name
    temporary = destination.with_suffix(destination.suffix + ".download")
    digest = hashlib.sha256()
    request = urllib.request.Request(
        plan.asset_url, headers={"User-Agent": "NovelBridgeAR"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != expected_digest:
            raise ValueError("The downloaded update failed its SHA-256 check.")
        temporary.replace(destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)