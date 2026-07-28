#!/usr/bin/env python3
"""fix_epg.py

Standalone post-processor for XMLTV EPG file.
1. Filters EPG channels and programmes to only include those in favorites.m3u.
2. Normalizes programme start/stop timestamps to +0700 (WIB).
3. Deduplicates programmes per channel.
"""

from __future__ import annotations
import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

JAKARTA = timezone(timedelta(hours=7))

def _parse_xmltv_datetime(time_str: str) -> datetime | None:
    if not time_str:
        return None
    try:
        parts = time_str.strip().split()
        dt_part = parts[0]
        tz_part = parts[1] if len(parts) > 1 else "+0000"
        dt = datetime.strptime(dt_part[:14], "%Y%m%d%H%M%S")
        tz_h = int(tz_part[:3])
        tz_m = int(tz_part[0] + tz_part[3:])
        tz = timezone(timedelta(hours=tz_h, minutes=tz_m))
        return dt.replace(tzinfo=tz)
    except Exception:
        return None

def normalize_xmltv_time(time_str: str) -> tuple[float | None, str]:
    dt = _parse_xmltv_datetime(time_str)
    if dt is None:
        return None, time_str
    dt_jkt = dt.astimezone(JAKARTA)
    return dt.timestamp(), dt_jkt.strftime("%Y%m%d%H%M%S +0700")

def load_favorite_channel_ids(m3u_path: Path, json_path: Path | None = None) -> set[str]:
    channel_ids: set[str] = set()

    if m3u_path.exists():
        content = m3u_path.read_text(encoding="utf-8", errors="replace")
        ids_from_m3u = set(re.findall(r'tvg-id="([^"]+)"', content))
        channel_ids.update(ids_from_m3u)

    if json_path and json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            for rule in data.get("rules", []):
                tvg_id = rule.get("tvg_id")
                if tvg_id:
                    channel_ids.add(tvg_id)
        except Exception:
            pass

    return channel_ids

def fix_epg_file(
    input_path: Path,
    output_path: Path | None = None,
    filter_m3u: Path | None = None,
    filter_json: Path | None = None
) -> bool:
    if output_path is None:
        output_path = input_path

    if not input_path.exists() or input_path.stat().st_size == 0:
        print(f"Error: {input_path} is missing or empty.")
        return False

    allowed_channels: set[str] = set()
    if filter_m3u:
        allowed_channels = load_favorite_channel_ids(filter_m3u, filter_json)
        print(f"Loaded {len(allowed_channels)} favorite channel IDs for EPG filtering.")

    print(f"Processing EPG: {input_path} -> {output_path}")
    tree = ET.parse(input_path)
    root = tree.getroot()

    programmes_by_channel: dict[str, list[ET.Element]] = {}

    # Step 1: Remove channels not in favorites (if filter active)
    removed_channels = 0
    kept_channels = 0
    for elem in list(root):
        if elem.tag == "channel":
            ch_id = elem.get("id", "")
            if allowed_channels and ch_id not in allowed_channels:
                root.remove(elem)
                removed_channels += 1
            else:
                kept_channels += 1
        elif elem.tag == "programme":
            ch = elem.get("channel", "")
            if allowed_channels and ch not in allowed_channels:
                root.remove(elem)
            else:
                if ch not in programmes_by_channel:
                    programmes_by_channel[ch] = []
                programmes_by_channel[ch].append(elem)
                root.remove(elem)

    total_progs = 0
    kept_progs = 0

    # Step 2: Normalize timestamps and deduplicate programmes per channel
    for ch, progs in programmes_by_channel.items():
        total_progs += len(progs)
        seen_starts: set[float | str] = set()

        for prog in progs:
            st_raw = prog.get("start", "")
            sp_raw = prog.get("stop", "")
            st_epoch, st_norm = normalize_xmltv_time(st_raw)
            sp_epoch, sp_norm = normalize_xmltv_time(sp_raw)

            dedup_key = st_epoch if st_epoch is not None else st_raw
            if dedup_key not in seen_starts:
                seen_starts.add(dedup_key)
                if st_norm:
                    prog.set("start", st_norm)
                if sp_norm:
                    prog.set("stop", sp_norm)
                root.append(prog)
                kept_progs += 1

    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    print(f"EPG filtering complete: {kept_channels} channels, {kept_progs} programmes saved to {output_path}")
    return True

def main():
    parser = argparse.ArgumentParser(description="Fix, filter, and normalize XMLTV EPG timestamps to WIB (+0700).")
    parser.add_argument("input_file", nargs="?", default="epg.xml", help="Input EPG XML file (default: epg.xml)")
    parser.add_argument("output_file", nargs="?", default="favorites-epg.xml", help="Output EPG XML file (default: favorites-epg.xml)")
    parser.add_argument("-f", "--filter-m3u", default="favorites.m3u", help="Favorites M3U playlist file for filtering (default: favorites.m3u)")
    parser.add_argument("-c", "--config", default="update-script/favorites.json", help="Favorites JSON config file (default: update-script/favorites.json)")
    args = parser.parse_args()

    input_path = Path(args.input_file)
    output_path = Path(args.output_file)
    m3u_path = Path(args.filter_m3u) if args.filter_m3u else None
    json_path = Path(args.config) if args.config else None

    fix_epg_file(input_path, output_path, filter_m3u=m3u_path, filter_json=json_path)

if __name__ == "__main__":
    main()
