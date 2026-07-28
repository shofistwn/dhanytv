#!/usr/bin/env python3
"""generate_favorites.py

Generates custom dhanytv-favorites.m3u playlist from master dhanytv.m3u
using discrete token matching, alias expansion, Jawa Timur regional
preference scoring, and health checks.
"""

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set
import urllib.request
import urllib.error

DEFAULT_HEADER = '#EXTM3U url-tvg="https://raw.githubusercontent.com/dhasap/dhanytv/main/epg.xml"'

ALIAS_MAP = {
    "mnctv": ["mnc tv", "mnctv", "mnc"],
    "gtv": ["global tv", "gtv"],
    "rtv": ["rajawali tv", "rajawali", "rtv"],
    "transtv": ["trans tv", "transtv"],
    "trans7": ["trans 7", "trans7"],
    "tvone": ["tv one", "tvone"],
    "metrotv": ["metro tv", "metrotv"],
    "kompastv": ["kompas tv", "kompastv"],
    "nusantaratv": ["nusantara tv", "nusantaratv"],
    "jawapos": ["jawa pos", "jawapos", "jawa pos tv"],
    "jawapostv": ["jawa pos", "jawapos", "jawa pos tv"],
    "jowotv": ["jowo", "channel jowo", "jowo tv"],
    "hanacarakatv": ["hanacaraka", "hanacarakatv", "hanacaraka tv"],
    "staratv": ["stara tv", "staratv"],
    "spotv": ["spo tv", "spotv"],
    "spotv2": ["spo tv 2", "spotv 2", "spotv2"],
    "sportstars": ["sportstar", "sportstars", "sportstars 1"],
    "sportstars2": ["sportstar 2", "sportstars 2"],
    "sportstars3": ["sportstar 3", "sportstars 3"],
    "sportstars4": ["sportstar 4", "sportstars 4"],
}

NOISE_TOKENS = {
    "hd", "fhd", "uhd", "sd", "4k", "1080p", "720p", "v", "v+", "video",
    "dash", "mpd", "hls", "cad", "24", "7", "tanpa", "drm", "channel", "feed"
}

JATIM_KEYWORDS = {
    "jawa timur", "jatim", "malang", "surabaya", "kediri", "madiun",
    "jember", "banyuwangi", "pasuruan", "blitar", "tuban", "lamongan",
    "gresik", "sidoarjo", "probolinggo", "mojokerto", "jombang", "nganjuk"
}

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
    custom_group: Optional[str] = None

def load_config_rules(config_path: Path) -> List[ChannelRule]:
    rules = []
    current_group = None
    if not config_path.exists():
        return rules
    for line in config_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current_group = line[1:-1].strip()
            continue
        if not line.startswith("#"):
            rules.append(ChannelRule(keyword=line, custom_group=current_group))
    return rules

def update_group_title(extinf: str, new_group: str) -> str:
    if 'group-title="' in extinf:
        return re.sub(r'group-title="[^"]*"', f'group-title="{new_group}"', extinf)
    m = re.search(r',(.+)$', extinf)
    if m:
        start_idx = m.start()
        return extinf[:start_idx] + f' group-title="{new_group}"' + extinf[start_idx:]
    return extinf

def tokenize(text: str) -> List[str]:
    return re.findall(r'[a-z0-9]+', text.lower())

def match_channel_tokens(kw_str: str, entry: M3UEntry) -> bool:
    cand_name = entry.name
    tvg_name_match = re.search(r'tvg-name="([^"]*)"', entry.extinf)
    cand_tvg = tvg_name_match.group(1) if tvg_name_match else ""

    names = [cand_name]
    if cand_tvg:
        names.append(cand_tvg)

    kw_tokens = tokenize(kw_str)
    kw_nums = extract_channel_numbers(kw_str)

    norm_kw = "".join(kw_tokens)
    target_phrases = [" ".join(kw_tokens)]
    if norm_kw in ALIAS_MAP:
        target_phrases.extend(ALIAS_MAP[norm_kw])

    for name in names:
        cand_tokens = tokenize(name)
        cand_nums = extract_channel_numbers(name)

        if kw_nums != cand_nums:
            continue

        clean_cand_tokens = [t for t in cand_tokens if t not in NOISE_TOKENS]
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

def match_rules_entry(entry: M3UEntry, rules: List[ChannelRule]) -> Optional[tuple[int, ChannelRule]]:
    for idx, rule in enumerate(rules):
        kw_str = rule.keyword.strip()
        if not kw_str:
            continue

        if match_channel_tokens(kw_str, entry):
            return idx, rule

    return None

def is_premium_source(entry: M3UEntry) -> bool:
    """Check if entry is from a premium source (Vision+, Vidio)."""
    name_lower = entry.name.lower()
    if "(v+)" in name_lower or "(video)" in name_lower:
        return True
    combined_props = " ".join(entry.props).lower()
    return "visionplus.id" in combined_props or "vidio.com" in combined_props

# Matches HD labels: "HD", "FHD", superscript "ᴴᴰ", "(1080p)", "(720p)", etc.
_RES_SCORE = [
    (re.compile(r'(?:\bfhd\b|\b1080p?\b)', re.IGNORECASE), 150),
    (re.compile(r'(?:\bhd\b|\b720p?\b|ᴴᴰ)', re.IGNORECASE), 100),
    (re.compile(r'(?:\b480p?\b|\bsd\b)', re.IGNORECASE), 0),
]

def check_stream_health(url: str, props: List[str], timeout: float = 2.0) -> bool:
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

    # Try HEAD first (avoids downloading stream body), fall back to
    # GET with Range header for servers that reject HEAD on streams.
    for method, extra in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
        try:
            req_headers = {**headers, **extra}
            req = urllib.request.Request(url, headers=req_headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status in (200, 206)
        except urllib.error.HTTPError as e:
            if method == "HEAD" and e.code == 405:
                continue  # server doesn't support HEAD, try GET+Range
            return False
        except Exception:
            return False
    return False

def calculate_entry_priority(entry: M3UEntry) -> int:
    score = 0
    name = entry.name
    full_text = f"{name} {entry.extinf}".lower()

    # Regional preference: Jawa Timur
    for kw in JATIM_KEYWORDS:
        if kw in full_text:
            score += 5000
            break

    # Premium source (V+ / Video)
    if is_premium_source(entry):
        score += 1000

    # Resolution hierarchy (first match wins)
    for pattern, points in _RES_SCORE:
        if pattern.search(name):
            score += points
            break

    # ChannelFeed — third-party re-stream, less reliable
    if "(channelfeed)" in name.lower():
        score -= 100

    if entry.urls:
        score += 10
    return score

def filter_entries(
    entries: List[M3UEntry],
    rules: List[ChannelRule],
    single_group: str = "FAVORITES"
) -> List[M3UEntry]:
    matched_entries: List[tuple[int, M3UEntry]] = []

    for entry in entries:
        matched_res = match_rules_entry(entry, rules)
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
        best_entry = max(group_items, key=calculate_entry_priority)
        if single_group:
            best_entry.extinf = update_group_title(best_entry.extinf, single_group)
            best_entry.group = single_group
        deduped_entries.append(best_entry)

    return deduped_entries

def main():
    parser = argparse.ArgumentParser(description="Generate dhanytv-favorites.m3u playlist.")
    parser.add_argument("-s", "--source", default="dhanytv.m3u", help="Path to master source playlist (default: dhanytv.m3u)")
    parser.add_argument("-o", "--output", default="favorites.m3u", help="Path to output favorites playlist (default: favorites.m3u)")
    parser.add_argument("-c", "--config", default="update-script/favorites.txt", help="Path to keywords config file")

    args = parser.parse_args()

    source_path = Path(args.source)
    output_path = Path(args.output)
    config_path = Path(args.config)

    print(f"Reading master playlist from '{source_path}'...")
    header, entries = parse_m3u(source_path)
    print(f"Total channels in master playlist: {len(entries)}")

    rules = load_config_rules(config_path)
    if rules:
        print(f"Loaded {len(rules)} rules from '{config_path}'")

    filtered = filter_entries(
        entries=entries,
        rules=rules,
        single_group="FAVORITES"
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
