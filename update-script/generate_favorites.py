#!/usr/bin/env python3
"""generate_favorites.py

Generates custom dhanytv-favorites.m3u playlist from master dhanytv.m3u
using discrete token matching, JSON configuration, alias expansion,
regional preference scoring, and deep health checks.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set
import urllib.parse
import urllib.request
import urllib.error

DEFAULT_HEADER = '#EXTM3U url-tvg="https://raw.githubusercontent.com/dhasap/dhanytv/main/epg.xml"'

RESOLUTION_RE = re.compile(r'\b(?:1080p?|720p?|480p?|360p?|240p?|4k|8k|fhd|uhd|sd)\b', re.IGNORECASE)

def extract_channel_numbers(text: str) -> Set[str]:
    cleaned = RESOLUTION_RE.sub('', text)
    return set(re.findall(r'\d+', cleaned))

@dataclass
class M3UEntry:
    extinf: str
    name: str
    group: str
    props: List[str] = field(default_factory=list)
    urls: List[str] = field(default_factory=list)

    def is_dash_drm(self) -> bool:
        combined = " ".join(self.props + self.urls + [self.extinf]).lower()
        return ".mpd" in combined or "clearkey" in combined or "(v+)" in self.name.lower()

    def to_m3u_block(self) -> str:
        lines = []
        lines.extend(self.props)
        lines.append(self.extinf)
        lines.extend(self.urls)
        return "\n".join(lines)

def parse_group_title(extinf: str) -> str:
    m = re.search(r'group-title="([^"]*)"', extinf)
    return m.group(1) if m else ""

def parse_channel_name(extinf: str) -> str:
    if ',' in extinf:
        return extinf.rsplit(',', 1)[1].strip()
    return ""

def parse_m3u(file_path: Path) -> tuple[str, List[M3UEntry]]:
    if not file_path.exists():
        print(f"Error: Source playlist '{file_path}' not found.", file=sys.stderr)
        sys.exit(1)

    lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    header = DEFAULT_HEADER
    entries: List[M3UEntry] = []
    pending_props: List[str] = []
    current_entry: Optional[M3UEntry] = None

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#EXTM3U"):
            header = line
            continue

        if line.startswith("#KODIPROP:") or line.startswith("#EXTVLCOPT:") or line.startswith("#EXTGRP:") or line.startswith("#EXTHTTP:"):
            if current_entry:
                current_entry.props.append(line)
            else:
                pending_props.append(line)
            continue

        if line.startswith("#EXTINF"):
            name = parse_channel_name(line)
            group = parse_group_title(line)
            current_entry = M3UEntry(
                extinf=line,
                name=name,
                group=group,
                props=pending_props.copy(),
                urls=[]
            )
            pending_props.clear()
            continue

        if not line.startswith("#"):
            if current_entry:
                current_entry.urls.append(line)
                entries.append(current_entry)
                current_entry = None

    return header, entries

@dataclass
class ChannelRule:
    keyword: str
    aliases: List[str] = field(default_factory=list)
    logo: Optional[str] = None
    custom_group: Optional[str] = None

@dataclass
class AppConfig:
    group: str = "FAVORITES"
    noise_tokens: Set[str] = field(default_factory=set)
    regional_keywords: Set[str] = field(default_factory=set)
    rules: List[ChannelRule] = field(default_factory=list)

def load_config(config_path: Path) -> AppConfig:
    config = AppConfig()
    if not config_path.exists():
        print(f"Warning: Config file '{config_path}' not found. Using defaults.", file=sys.stderr)
        return config

    if config_path.suffix.lower() == ".json":
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            if "group" in data and data["group"]:
                config.group = data["group"]
            if "noise_tokens" in data and isinstance(data["noise_tokens"], list):
                config.noise_tokens = set(t.lower() for t in data["noise_tokens"])
            if "regional_keywords" in data:
                if isinstance(data["regional_keywords"], list):
                    config.regional_keywords = set(k.lower() for k in data["regional_keywords"])
                elif isinstance(data["regional_keywords"], dict):
                    kw_set = set()
                    for v in data["regional_keywords"].values():
                        if isinstance(v, list):
                            kw_set.update(k.lower() for k in v)
                    config.regional_keywords = kw_set

            if "channels" in data and isinstance(data["channels"], list):
                for item in data["channels"]:
                    if isinstance(item, dict):
                        name = item.get("name", "").strip()
                        aliases = item.get("aliases", [name])
                        logo = item.get("logo")
                        if name:
                            config.rules.append(ChannelRule(keyword=name, aliases=aliases, logo=logo, custom_group=config.group))
                    elif isinstance(item, str) and item.strip():
                        name = item.strip()
                        config.rules.append(ChannelRule(keyword=name, aliases=[name], custom_group=config.group))
            return config
        except Exception as e:
            print(f"Warning: Failed to parse JSON config '{config_path}': {e}. Falling back to TXT.", file=sys.stderr)

    # Fallback TXT parser
    current_group = "FAVORITES"
    for line in config_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_group = line[1:-1].strip()
            continue
        if "=" in line:
            name, alias_str = line.split("=", 1)
            name = name.strip()
            aliases = [a.strip() for a in alias_str.split(",") if a.strip()]
        else:
            name = line
            aliases = [name]
        config.rules.append(ChannelRule(keyword=name, aliases=aliases, custom_group=current_group))
    config.group = current_group
    return config

def update_group_title(extinf: str, new_group: str) -> str:
    if 'group-title="' in extinf:
        return re.sub(r'group-title="[^"]*"', f'group-title="{new_group}"', extinf)
    m = re.search(r',(.+)$', extinf)
    if m:
        start_idx = m.start()
        return extinf[:start_idx] + f' group-title="{new_group}"' + extinf[start_idx:]
    return extinf

def update_channel_name(extinf: str, new_name: str) -> str:
    if ',' in extinf:
        prefix = extinf.rsplit(',', 1)[0]
        return f"{prefix},{new_name}"
    return extinf

def update_logo_url(extinf: str, new_logo: str) -> str:
    if 'tvg-logo="' in extinf:
        return re.sub(r'tvg-logo="[^"]*"', f'tvg-logo="{new_logo}"', extinf)
    elif "tvg-logo='" in extinf:
        return re.sub(r"tvg-logo='[^']*'", f"tvg-logo='{new_logo}'", extinf)
    m = re.search(r',(.+)$', extinf)
    if m:
        start_idx = m.start()
        return extinf[:start_idx] + f' tvg-logo="{new_logo}"' + extinf[start_idx:]
    return extinf

def tokenize(text: str) -> List[str]:
    return re.findall(r'[a-z0-9]+', text.lower())

def match_channel_tokens(rule: ChannelRule, entry: M3UEntry, noise_tokens: Set[str]) -> bool:
    cand_name = entry.name
    tvg_name_match = re.search(r'tvg-name="([^"]*)"', entry.extinf)
    cand_tvg = tvg_name_match.group(1) if tvg_name_match else ""

    names = [cand_name]
    if cand_tvg:
        names.append(cand_tvg)

    kw_str = rule.keyword
    kw_tokens = tokenize(kw_str)
    kw_nums = extract_channel_numbers(kw_str)

    target_phrases = [" ".join(kw_tokens)]
    for alias in rule.aliases:
        alias_tokens = tokenize(alias)
        if alias_tokens:
            target_phrases.append(" ".join(alias_tokens))

    for name in names:
        cand_tokens = tokenize(name)
        cand_nums = extract_channel_numbers(name)

        if kw_nums != cand_nums:
            continue

        clean_cand_tokens = [t for t in cand_tokens if t not in noise_tokens]
        clean_cand_str = " ".join(clean_cand_tokens)

        for phrase in target_phrases:
            phrase_tokens = tokenize(phrase)
            phrase_str = " ".join(phrase_tokens)

            if phrase_str == clean_cand_str or phrase_str == " ".join(cand_tokens):
                return True

            if len(phrase_tokens) == 1:
                if phrase_tokens[0] in clean_cand_tokens:
                    if phrase_tokens[0] == "btv" and "bangladesh" in cand_tokens:
                        continue
                    return True
            else:
                phrase_len = len(phrase_tokens)
                for i in range(len(clean_cand_tokens) - phrase_len + 1):
                    if clean_cand_tokens[i:i+phrase_len] == phrase_tokens:
                        return True

    return False

def match_rules_entry(entry: M3UEntry, rules: List[ChannelRule], noise_tokens: Set[str]) -> Optional[tuple[int, ChannelRule]]:
    for idx, rule in enumerate(rules):
        if not rule.keyword.strip():
            continue

        if match_channel_tokens(rule, entry, noise_tokens):
            return idx, rule

    return None

# Matches HD labels: "HD", "FHD", superscript "ᴴᴰ", "(1080p)", "(720p)", etc.
_RES_SCORE = [
    (re.compile(r'(?:\bfhd\b|\b1080p?\b)', re.IGNORECASE), 150),
    (re.compile(r'(?:\bhd\b|\b720p?\b|ᴴᴰ)', re.IGNORECASE), 100),
    (re.compile(r'(?:\b480p?\b|\bsd\b)', re.IGNORECASE), 0),
]

def _check_hls_segment_health(url: str, headers: dict, timeout: float) -> bool:
    """Verifies that HLS master/playlist contains at least one fetchable media segment."""
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status not in (200, 206):
                return False
            content = resp.read(16384).decode("utf-8", errors="replace")

        lines = [line.strip() for line in content.splitlines() if line.strip()]
        sub_playlist = None
        media_segment = None

        for line in lines:
            if line.startswith("#"):
                continue
            if line.endswith(".m3u8") or ".m3u8?" in line:
                sub_playlist = urllib.parse.urljoin(url, line)
                break
            elif not line.startswith("#"):
                media_segment = urllib.parse.urljoin(url, line)
                break

        if sub_playlist:
            req_sub = urllib.request.Request(sub_playlist, headers=headers, method="GET")
            with urllib.request.urlopen(req_sub, timeout=timeout) as resp_sub:
                if resp_sub.status not in (200, 206):
                    return False
                sub_content = resp_sub.read(16384).decode("utf-8", errors="replace")
            for sub_line in sub_content.splitlines():
                sub_line = sub_line.strip()
                if sub_line and not sub_line.startswith("#"):
                    media_segment = urllib.parse.urljoin(sub_playlist, sub_line)
                    break

        if not media_segment:
            return True

        seg_headers = {**headers, "Range": "bytes=0-1024"}
        req_seg = urllib.request.Request(media_segment, headers=seg_headers, method="GET")
        with urllib.request.urlopen(req_seg, timeout=timeout) as resp_seg:
            return resp_seg.status in (200, 206)
    except Exception:
        return False

def _check_mpd_segment_health(url: str, headers: dict, timeout: float) -> bool:
    """Verifies that DASH MPD manifest contains at least one fetchable init/media segment."""
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status not in (200, 206):
                return False
            xml_content = resp.read(16384).decode("utf-8", errors="replace")

        m = re.search(r'initialization=[\'"]([^\'"]+)[\'"]', xml_content)
        if not m:
            m = re.search(r'media=[\'"]([^\'"]+)[\'"]', xml_content)

        if not m:
            return True

        seg_file = m.group(1).replace("$Number$", "1")
        seg_url = urllib.parse.urljoin(url, seg_file)

        seg_headers = {**headers, "Range": "bytes=0-1024"}
        req_seg = urllib.request.Request(seg_url, headers=seg_headers, method="GET")
        with urllib.request.urlopen(req_seg, timeout=timeout) as resp_seg:
            return resp_seg.status in (200, 206)
    except Exception:
        return False

def check_stream_health(url: str, props: List[str], timeout: float = 3.0) -> bool:
    if not url or not url.startswith("http"):
        return False
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    for prop in props:
        if prop.startswith("#EXTVLCOPT:http-referrer="):
            headers["Referer"] = prop.split("=", 1)[1].strip()
        elif prop.startswith("#EXTVLCOPT:http-user-agent="):
            headers["User-Agent"] = prop.split("=", 1)[1].strip()

    is_mpd = ".mpd" in url.lower() or any("clearkey" in p.lower() for p in props)

    if is_mpd:
        return _check_mpd_segment_health(url, headers, timeout=timeout)

    if ".m3u8" in url.lower():
        return _check_hls_segment_health(url, headers, timeout=timeout)

    for method, extra in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
        try:
            req_headers = {**headers, **extra}
            req = urllib.request.Request(url, headers=req_headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status in (200, 206)
        except Exception:
            return False
    return False

def calculate_entry_priority(entry: M3UEntry, regional_keywords: Set[str]) -> int:
    score = 0
    name = entry.name
    full_text = f"{name} {entry.extinf}".lower()

    # Regional preference (word boundary match to avoid partial substring hits like 'medan' in 'sumedang')
    for kw in regional_keywords:
        pattern = r'\b' + re.escape(kw.lower()) + r'\b'
        if re.search(pattern, full_text):
            score += 5000
            break

    # Resolution hierarchy (first match wins)
    for pattern, points in _RES_SCORE:
        if pattern.search(name):
            score += points
            break

    # Compatibility vs DRM scoring: favor clean HLS streams over DASH ClearKey DRM
    if entry.is_dash_drm():
        score -= 50
    else:
        score += 30

    # ChannelFeed — third-party re-stream, less reliable
    if "(channelfeed)" in name.lower():
        score -= 100

    if entry.urls:
        score += 10
    return score

def filter_entries(
    entries: List[M3UEntry],
    config: AppConfig
) -> List[M3UEntry]:
    matched_entries: List[tuple[int, M3UEntry]] = []

    for entry in entries:
        matched_res = match_rules_entry(entry, config.rules, config.noise_tokens)
        if matched_res is not None:
            rule_idx, matched_rule = matched_res
            matched_entries.append((rule_idx, entry))

    dead_urls: Set[str] = set()
    if matched_entries:
        print("Running concurrent HTTP health check to discard dead / 403 streams...")
        url_props: dict[str, List[str]] = {}
        urls_to_check: List[str] = []
        for _, entry in matched_entries:
            for url in entry.urls:
                if url and url.startswith("http") and url not in url_props:
                    url_props[url] = entry.props
                    urls_to_check.append(url)

        def verify_worker(u: str) -> Optional[str]:
            if not check_stream_health(u, url_props.get(u, [])):
                return u
            return None

        with ThreadPoolExecutor(max_workers=20) as executor:
            results = executor.map(verify_worker, urls_to_check)
            for res in results:
                if res:
                    dead_urls.add(res)
        if dead_urls:
            print(f"Discarded {len(dead_urls)} dead/403 stream URLs")

    # Drop entries whose URLs are all dead
    if dead_urls:
        matched_entries = [
            (idx, e) for idx, e in matched_entries
            if not all(u in dead_urls for u in e.urls)
        ]

    grouped_by_rule: dict[int, List[M3UEntry]] = {}
    for rule_idx, entry in matched_entries:
        grouped_by_rule.setdefault(rule_idx, []).append(entry)

    deduped_entries: List[M3UEntry] = []
    for rule_idx in sorted(grouped_by_rule.keys()):
        group_items = grouped_by_rule[rule_idx]
        best_entry = max(group_items, key=lambda e: calculate_entry_priority(e, config.regional_keywords))
        rule = config.rules[rule_idx] if rule_idx < len(config.rules) else None
        if config.group:
            best_entry.extinf = update_group_title(best_entry.extinf, config.group)
            best_entry.group = config.group
        if rule:
            if rule.keyword:
                clean_name = rule.keyword.strip()
                best_entry.extinf = update_channel_name(best_entry.extinf, clean_name)
                best_entry.name = clean_name
            if rule.logo:
                best_entry.extinf = update_logo_url(best_entry.extinf, rule.logo)
        deduped_entries.append(best_entry)

    return deduped_entries

def main():
    parser = argparse.ArgumentParser(description="Generate dhanytv-favorites.m3u playlist.")
    parser.add_argument("-s", "--source", default="dhanytv.m3u", help="Path to master source playlist (default: dhanytv.m3u)")
    parser.add_argument("-o", "--output", default="favorites.m3u", help="Path to output favorites playlist (default: favorites.m3u)")
    parser.add_argument("-c", "--config", default="update-script/favorites.json", help="Path to config file (default: update-script/favorites.json)")

    args = parser.parse_args()

    source_path = Path(args.source)
    output_path = Path(args.output)
    config_path = Path(args.config)

    print(f"Reading master playlist from '{source_path}'...")
    header, entries = parse_m3u(source_path)
    print(f"Total channels in master playlist: {len(entries)}")

    config = load_config(config_path)
    if config.rules:
        print(f"Loaded {len(config.rules)} channel rules from '{config_path}'")

    filtered = filter_entries(
        entries=entries,
        config=config
    )

    out_content = [header, ""]
    for entry in filtered:
        out_content.append(entry.to_m3u_block())
        out_content.append("")

    output_path.write_text("\n".join(out_content), encoding="utf-8")

    print(f"Successfully generated '{output_path}'")
    print(f"Filtered channels count: {len(filtered)} / {len(entries)}")

if __name__ == "__main__":
    main()
