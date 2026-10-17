"""One-shot informational reachability probe for configured source sites.

Reachability does not verify that the site's parser still works. Existing
rejection reasons are preserved; a host must be rechecked by a maintainer
before removing its entry.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SITES_PATH = ROOT / "config" / "sites.json"
REJECTED_PATH = ROOT / "config" / "_rejected.json"


def check_sites(include_rejected: bool = False) -> int:
    sites = json.loads(SITES_PATH.read_text(encoding="utf-8"))
    rejected = json.loads(REJECTED_PATH.read_text(encoding="utf-8")) if REJECTED_PATH.exists() else {}
    if not isinstance(rejected, dict):
        raise ValueError(f"{REJECTED_PATH} must contain a JSON object")

    newly_rejected: dict[str, str] = {}
    failures = 0
    with httpx.Client(timeout=10, follow_redirects=True) as client:
        for site_id, settings in sorted(sites.items()):
            url = settings.get("base_url")
            if not url:
                print(f"ERR  {site_id}: missing base_url")
                failures += 1
                continue
            was_rejected = url in rejected
            if was_rejected and not include_rejected:
                continue
            try:
                response = client.get(url)
                if response.status_code < 400:
                    label = "REVIVED" if was_rejected else "OK"
                    print(f"{label:7} {site_id:20} {response.status_code} {url}")
                else:
                    label = "DOWN"
                    detail = f"HTTP {response.status_code}"
                    print(f"{label:7} {site_id:20} {detail} {url}")
                    failures += 1
                    if not was_rejected:
                        newly_rejected[url] = f"Site is down: {detail}"
            except httpx.HTTPError as exc:
                detail = f"{type(exc).__name__}: {exc}"
                print(f"ERR     {site_id:20} {detail} {url}")
                failures += 1
                if not was_rejected:
                    newly_rejected[url] = f"Site is down: {detail}"

    if newly_rejected:
        rejected.update(newly_rejected)
        REJECTED_PATH.write_text(
            json.dumps(rejected, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Recorded {len(newly_rejected)} new down-site entries in {REJECTED_PATH}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--include-rejected",
        action="store_true",
        help="Probe rejected sites too and report any that appear reachable",
    )
    args = parser.parse_args()
    return 1 if check_sites(args.include_rejected) else 0


if __name__ == "__main__":
    raise SystemExit(main())
