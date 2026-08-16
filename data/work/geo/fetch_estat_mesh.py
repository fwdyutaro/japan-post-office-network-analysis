"""Fetch the 2020 Population Census 500 m mesh statistics from e-Stat 統計GIS.

Product: 国勢調査 2020年 / 4次メッシュ（500mメッシュ）/ JGD2011
  T001141  人口及び世帯          (total population, households)
  T001192  ５歳階級別人口        (5-year age classes)
Delivered per prefecture as tblT<statsId>H<pref>.zip.

Access discipline: declared User-Agent, >= 3.5 s between requests, stop on
403/429.  robots.txt (fetched 2026-08-09) does not disallow /gis/.
No click-through licence screen is presented on these endpoints; the response
is the ZIP itself (Content-Disposition: attachment).
"""
from __future__ import annotations
import argparse, json, os, sys, time
from pathlib import Path
import requests

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_paths import project_root

ROOT = project_root()
DST = ROOT / "data/work/estat/mesh500"
URL = "https://www.e-stat.go.jp/gis/statmap-search/data"
DELAY = 3.5


def fetch(contact: str, *, acknowledge_site_terms: bool = False) -> list[dict]:
    """Fetch the two mesh datasets after an explicit terms acknowledgement."""
    if not acknowledge_site_terms:
        raise ValueError("--acknowledge-site-terms is required")
    contact = contact.strip()
    if not contact or "@" not in contact:
        raise ValueError("a research contact email is required")

    DST.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": f"postal-bias-research/1.0 (+mailto:{contact})"}
    log: list[dict] = []
    for stats_id in ("T001141", "T001192"):
        for pref in range(1, 48):
            code = f"{pref:02d}"
            out = DST / f"tbl{stats_id}H{code}.zip"
            if out.exists() and out.stat().st_size > 0:
                log.append({"statsId": stats_id, "code": code, "status": "cached",
                            "bytes": out.stat().st_size})
                continue
            time.sleep(DELAY)
            response = requests.get(
                URL, headers=headers,
                params={"statsId": stats_id, "code": code, "downloadType": "2"},
                timeout=180,
            )
            if response.status_code in (403, 429):
                print(f"STOP {response.status_code} on {stats_id}/{code}", file=sys.stderr)
                log.append({"statsId": stats_id, "code": code,
                            "status": response.status_code})
                (DST / "fetch_log.json").write_text(
                    json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
                raise RuntimeError(f"server stopped the request ({response.status_code})")
            content_type = response.headers.get("Content-Type", "")
            if response.status_code != 200 or not response.content.startswith(b"PK"):
                log.append({"statsId": stats_id, "code": code,
                            "status": response.status_code,
                            "content_type": content_type, "note": "not a zip"})
                print(f"WARN {stats_id}/{code} {response.status_code} {content_type}",
                      file=sys.stderr)
                continue
            out.write_bytes(response.content)
            log.append({"statsId": stats_id, "code": code, "status": 200,
                        "bytes": len(response.content),
                        "filename": response.headers.get("Content-Disposition", "")})
            print(f"{stats_id} {code} {len(response.content):,}B", flush=True)

    (DST / "fetch_log.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    total = sum(entry.get("bytes", 0) for entry in log)
    print(f"done: {len(log)} entries, {total:,} bytes")
    return log


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contact", default=os.environ.get("POSTAL_BIAS_CONTACT"))
    parser.add_argument("--acknowledge-site-terms", action="store_true")
    args = parser.parse_args()
    if not args.contact:
        parser.error("--contact or POSTAL_BIAS_CONTACT is required")
    try:
        fetch(args.contact, acknowledge_site_terms=args.acknowledge_site_terms)
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
