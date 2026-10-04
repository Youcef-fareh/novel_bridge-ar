from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _asset(name: str, size: int = 123) -> dict:
    return {
        "name": name,
        "size": size,
        "browser_download_url": f"https://example.test/{name}",
    }


def test_update_selection_prefers_incremental_asset():
    from backend.update_manager import select_update

    releases = [{
        "tag_name": "v1.2.0",
        "html_url": "https://github.com/example/repo/releases/tag/v1.2.0",
        "body": "Bug fixes",
        "assets": [
            _asset("NovelBridgeAR_Update_1.1.0_to_1.2.0.exe", 4000),
            _asset("NovelBridgeAR_Update_1.1.0_to_1.2.0.exe.sha256"),
            _asset("NovelBridgeAR_Setup.exe", 9000),
            _asset("NovelBridgeAR_Setup.exe.sha256"),
        ],
    }]

    plan = select_update(releases, "1.1.0")

    assert plan is not None
    assert plan.version == "1.2.0"
    assert plan.is_incremental
    assert plan.size_bytes == 4000
    assert plan.release_notes == "Bug fixes"


def test_update_selection_uses_full_installer_when_delta_does_not_match():
    from backend.update_manager import select_update

    releases = [{
        "tag_name": "v1.2.0",
        "assets": [
            _asset("NovelBridgeAR_Update_1.1.0_to_1.2.0.exe"),
            _asset("NovelBridgeAR_Update_1.1.0_to_1.2.0.exe.sha256"),
            _asset("NovelBridgeAR_Setup.exe", 9000),
            _asset("NovelBridgeAR_Setup.exe.sha256"),
        ],
    }]

    plan = select_update(releases, "1.0.0")

    assert plan is not None
    assert not plan.is_incremental
    assert plan.asset_name == "NovelBridgeAR_Setup.exe"
    assert plan.size_bytes == 9000


def test_update_selection_ignores_prereleases_and_current_version():
    from backend.update_manager import select_update

    releases = [
        {"tag_name": "v1.3.0-beta.1", "prerelease": True, "assets": []},
        {"tag_name": "v1.2.0", "assets": []},
    ]

    assert select_update(releases, "1.2.0") is None


def test_update_package_contains_only_changed_app_files(tmp_path):
    from scripts.create_update_package import create_update_package

    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "VERSION").write_text("1.0.0", encoding="utf-8")
    (new / "VERSION").write_text("1.1.0", encoding="utf-8")
    (old / "NovelBridgeAR.exe").write_text("old exe", encoding="utf-8")
    (new / "NovelBridgeAR.exe").write_text("new exe", encoding="utf-8")
    (old / "unchanged.dll").write_text("same", encoding="utf-8")
    (new / "unchanged.dll").write_text("same", encoding="utf-8")
    (old / "config").mkdir()
    (new / "config").mkdir()
    (old / "config/sites.json").write_text("old config", encoding="utf-8")
    (new / "config/sites.json").write_text("custom config", encoding="utf-8")
    (new / "new.dll").write_text("new dependency", encoding="utf-8")

    script = create_update_package(old, new, tmp_path / "build")

    assert script is not None
    contents = script.read_text(encoding="utf-8")
    assert "NovelBridgeAR.exe" in contents
    assert "new.dll" in contents
    assert "unchanged.dll" not in contents
    assert "sites.json" not in contents
    assert "AppId={{B3A7E1F2-9C4D-4E8A-B1F3-2D5A6C7E8F9A}" in contents
    assert "[Languages]" in contents


def test_download_update_checks_sha256_before_returning_file(tmp_path, monkeypatch):
    import hashlib

    import backend.update_manager as manager

    payload = b"signed release payload"
    expected = hashlib.sha256(payload).hexdigest()

    class FakeResponse:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=-1):
            body, self.body = self.body, b""
            return body

    def fake_urlopen(request, timeout):
        assert timeout in (20, 60)
        if request.full_url.endswith(".sha256"):
            return FakeResponse(f"{expected}  update.exe".encode("ascii"))
        return FakeResponse(payload)

    monkeypatch.setattr(manager.urllib.request, "urlopen", fake_urlopen)
    plan = manager.UpdatePlan(
        version="1.2.0",
        release_url="https://github.com/example/repo/releases/tag/v1.2.0",
        release_notes="",
        asset_name="update.exe",
        asset_url="https://example.test/update.exe",
        checksum_url="https://example.test/update.exe.sha256",
        size_bytes=len(payload),
        is_incremental=True,
    )

    result = manager.download_update_asset(plan, tmp_path)

    assert result.read_bytes() == payload