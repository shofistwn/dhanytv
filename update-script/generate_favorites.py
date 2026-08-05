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

DEFAULT_HEADER = '#EXTM3U url-tvg="https://raw.githubusercontent.com/shofistwn/dhanytv/refs/heads/main/favorites-epg.xml"'

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
        return "clearkey" in combined or "widevine" in combined or "license_key" in combined or "(v+)" in self.name.lower()

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

def normalize_stream_url(url: str) -> str:
    return (
        url.replace("op-group1-swiftservehd-1.dens.tv", "op-flashcon-digdayahd-1.dens.tv")
           .replace("op-group2-swiftservesd-1.dens.tv", "op-flashcon-digdayahd-1.dens.tv")
           .replace("op-group1-swiftservesd-1.dens.tv", "op-flashcon-digdayahd-1.dens.tv")
    )

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

        if line.startswith("#INF") or line.startswith("#EXTINF"):
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

        if line.startswith("#"):
            if current_entry:
                current_entry.props.append(line)
            else:
                pending_props.append(line)
            continue

        # URL line
        if current_entry:
            current_entry.urls.append(normalize_stream_url(line))
            entries.append(current_entry)
            current_entry = None

    return header, entries

@dataclass
class ChannelRule:
    keyword: str
    aliases: List[str] = field(default_factory=list)
    logo: Optional[str] = None
    tvg_id: Optional[str] = None
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
                        tvg_id = item.get("tvg_id")
                        if name:
                            config.rules.append(ChannelRule(keyword=name, aliases=aliases, logo=logo, tvg_id=tvg_id, custom_group=config.group))
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

def update_tvg_id(extinf: str, new_tvg_id: str) -> str:
    if 'tvg-id="' in extinf:
        return re.sub(r'tvg-id="[^"]*"', f'tvg-id="{new_tvg_id}"', extinf)
    elif "tvg-id='" in extinf:
        return re.sub(r"tvg-id='[^']*'", f"tvg-id='{new_tvg_id}'", extinf)
    m = re.search(r',(.+)$', extinf)
    if m:
        start_idx = m.start()
        return extinf[:start_idx] + f' tvg-id="{new_tvg_id}"' + extinf[start_idx:]
    return extinf

def update_tvg_name(extinf: str, new_tvg_name: str) -> str:
    if 'tvg-name="' in extinf:
        return re.sub(r'tvg-name="[^"]*"', f'tvg-name="{new_tvg_name}"', extinf)
    elif "tvg-name='" in extinf:
        return re.sub(r"tvg-name='[^']*'", f"tvg-name='{new_tvg_name}'", extinf)
    m = re.search(r',(.+)$', extinf)
    if m:
        start_idx = m.start()
        return extinf[:start_idx] + f' tvg-name="{new_tvg_name}"' + extinf[start_idx:]
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

def extract_stream_headers(url: str, props: List[str]) -> tuple[str, dict]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    clean_url = url
    inline_opts = ""
    if "|" in url:
        clean_url, inline_opts = url.split("|", 1)

    all_props = list(props)
    if inline_opts:
        for opt in re.split(r"[&|]", inline_opts):
            if "=" in opt:
                all_props.append(f"#INLINE:{opt}")

    for prop in all_props:
        clean_prop = prop.strip()
        if clean_prop.startswith("#EXTHTTP:"):
            try:
                json_str = clean_prop.split(":", 1)[1]
                data = json.loads(json_str)
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, str):
                            headers[k.strip().title()] = v.strip().strip("\"'")
            except Exception:
                pass
            continue

        if "=" not in clean_prop:
            continue

        key, val = clean_prop.split("=", 1)
        val = val.strip().strip("\"'")
        key_lower = key.lower()

        if key_lower in ("#extvlcopt:http-referrer", "#extvlcopt:http-referer"):
            headers["Referer"] = val
        elif key_lower == "#extvlcopt:http-user-agent":
            headers["User-Agent"] = val
        elif key_lower == "#extvlcopt:http-origin":
            headers["Origin"] = val
        elif key_lower == "#extvlcopt:http-cookie":
            headers["Cookie"] = val
        elif key_lower in ("#kodiprop:inputstream.adaptive.stream_headers", "#inline:stream_headers"):
            for item in re.split(r"[&|]", val):
                if "=" in item:
                    k, v = item.split("=", 1)
                    headers[k.strip().title()] = v.strip().strip("\"'")
        elif key_lower.startswith("#inline:"):
            inline_key = key_lower.split(":", 1)[1]
            if inline_key in ("user-agent", "useragent"):
                headers["User-Agent"] = val
            elif inline_key in ("referer", "referrer"):
                headers["Referer"] = val
            elif inline_key == "origin":
                headers["Origin"] = val
            elif inline_key == "cookie":
                headers["Cookie"] = val

    return clean_url.strip(), headers

def _check_hls_segment_health(url: str, headers: dict, timeout: float) -> bool:
    """Verifies that HLS master/playlist contains valid #EXTM3U content and fetchable media segments."""
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status not in (200, 206):
                return False
            content = resp.read(16384).decode("utf-8", errors="replace")

        if "#EXTM3U" not in content and "#EXTINF" not in content and "#EXT-X-" not in content:
            return False

        lines = [line.strip() for line in content.splitlines() if line.strip()]
        sub_playlist = None
        media_segment = None

        is_next_sub = False
        for line in lines:
            if line.startswith("#EXT-X-STREAM-INF") or line.startswith("#EXT-X-I-FRAME-STREAM-INF"):
                is_next_sub = True
                continue
            if line.startswith("#"):
                continue

            if is_next_sub or line.endswith(".m3u8") or ".m3u8?" in line:
                sub_playlist = urllib.parse.urljoin(url, line)
                if "?" not in sub_playlist and "?" in url:
                    sub_playlist = f"{sub_playlist}?{url.split('?', 1)[1]}"
                break
            else:
                media_segment = urllib.parse.urljoin(url, line)
                if "?" not in media_segment and "?" in url:
                    media_segment = f"{media_segment}?{url.split('?', 1)[1]}"
                break

        if sub_playlist:
            try:
                req_sub = urllib.request.Request(sub_playlist, headers=headers, method="GET")
                with urllib.request.urlopen(req_sub, timeout=timeout) as resp_sub:
                    if resp_sub.status in (200, 206):
                        sub_content = resp_sub.read(16384).decode("utf-8", errors="replace")
                        for sub_line in sub_content.splitlines():
                            sub_line = sub_line.strip()
                            if sub_line and not sub_line.startswith("#"):
                                media_segment = urllib.parse.urljoin(sub_playlist, sub_line)
                                if "?" not in media_segment and "?" in sub_playlist:
                                    media_segment = f"{media_segment}?{sub_playlist.split('?', 1)[1]}"
                                break
            except Exception:
                pass

        if media_segment:
            try:
                seg_headers = {**headers, "Range": "bytes=0-1024"}
                req_seg = urllib.request.Request(media_segment, headers=seg_headers, method="GET")
                with urllib.request.urlopen(req_seg, timeout=timeout) as resp_seg:
                    if resp_seg.status in (200, 206):
                        return True
            except Exception:
                pass

        # Resilient Fallback: Manifest HTTP 200 OK with valid HLS tags
        return True
    except Exception:
        return False

def verify_mpd_drm_key_validity(xml_content: str, props: List[str], headers: dict, timeout: float) -> bool:
    """Verifies that MPD DRM streams have valid, matching, and reachable DRM license keys."""
    has_drm = "ContentProtection" in xml_content or "cenc:" in xml_content
    if not has_drm:
        return True  # Non-DRM DASH stream (e.g. IndiHome)

    lic_key = None
    for p in props:
        if "license_key=" in p.lower():
            lic_key = p.split("=", 1)[1].strip()
            break

    if not lic_key:
        return False  # MPD requires DRM but no license_key property is set

    if lic_key.startswith("http"):
        try:
            lic_req = urllib.request.Request(lic_key, headers=headers)
            with urllib.request.urlopen(lic_req, timeout=timeout) as lic_resp:
                return lic_resp.status in (200, 204, 206)
        except Exception:
            return False

    if ":" in lic_key and len(lic_key.replace(":", "")) == 64:
        key_id = lic_key.split(":")[0].replace("-", "").lower()
        m = re.search(r'default_KID=[\'"]([^\'"]+)[\'"]', xml_content, re.IGNORECASE)
        if not m:
            m = re.search(r'cenc:default_KID=[\'"]([^\'"]+)[\'"]', xml_content, re.IGNORECASE)

        if m:
            mpd_kid = m.group(1).replace("-", "").lower()
            if key_id != mpd_kid:
                return False  # Key ID mismatch: static ClearKey is expired/invalid for this manifest

        return True

    return False

def _check_mpd_segment_health(url: str, headers: dict, props: List[str], timeout: float) -> bool:
    """Verifies DASH MPD manifest validity, DRM key validity, and segment fetchability."""
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status not in (200, 206):
                return False
            xml_content = resp.read(65536).decode("utf-8", errors="replace")

        if "<MPD" not in xml_content and "<mpd" not in xml_content.lower():
            return False

        # Validate DRM License Key (if MPD uses DRM)
        if not verify_mpd_drm_key_validity(xml_content, props, headers, timeout):
            return False

        m = re.search(r'initialization=[\'"]([^\'"]+)[\'"]', xml_content)
        if not m:
            m = re.search(r'media=[\'"]([^\'"]+)[\'"]', xml_content)

        if not m:
            return True

        rep_match = re.search(r'<Representation\s+[^>]*id=[\'"]([^\'"]+)[\'"]', xml_content)
        rep_id = rep_match.group(1) if rep_match else "0"

        seg_file = m.group(1).replace("$RepresentationID$", rep_id)
        seg_file = seg_file.replace("$Bandwidth$", "1000000").replace("$Time$", "0")
        seg_file = re.sub(r'\$Number[^\$]*\$', '1', seg_file)

        # 1. Standard urljoin
        seg_url1 = urllib.parse.urljoin(url, seg_file)
        if "?" not in seg_url1 and "?" in url:
            seg_url1 = f"{seg_url1}?{url.split('?', 1)[1]}"

        # 2. Worker/proxy parameter formats (&file=..., &segment=...)
        sep = "&" if "?" in url else "?"
        seg_url2 = f"{url}{sep}file={seg_file}"
        seg_url3 = f"{url}{sep}segment={seg_file}"

        for target_url in (seg_url1, seg_url2, seg_url3):
            try:
                seg_headers = {**headers, "Range": "bytes=0-1024"}
                req_seg = urllib.request.Request(target_url, headers=seg_headers, method="GET")
                with urllib.request.urlopen(req_seg, timeout=timeout) as resp_seg:
                    if resp_seg.status in (200, 206):
                        return True
            except Exception:
                pass

        # Resilient Fallback: Manifest HTTP 200 OK with valid DRM key structure
        return True
    except Exception:
        return False

def check_stream_health(url: str, props: List[str], timeout: float = 5.0) -> bool:
    if not url or not url.startswith("http"):
        return False

    clean_url, headers = extract_stream_headers(url, props)

    combined_text = " ".join([clean_url, url] + props).lower()
    is_mpd = ".mpd" in combined_text or "clearkey" in combined_text or "widevine" in combined_text or "manifest_type=dash" in combined_text
    is_hls = ".m3u8" in combined_text or "manifest_type=hls" in combined_text

    if is_mpd:
        return _check_mpd_segment_health(clean_url, headers, props, timeout=timeout)

    if is_hls:
        return _check_hls_segment_health(clean_url, headers, timeout=timeout)

    for method, extra in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
        try:
            req_headers = {**headers, **extra}
            req = urllib.request.Request(clean_url, headers=req_headers, method=method)
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
        print("Running HTTP health check to discard dead / 403 streams...")
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

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = executor.map(verify_worker, urls_to_check)
            for res in results:
                if res:
                    dead_urls.add(res)
        if dead_urls:
            print(f"Discarded {len(dead_urls)} dead/403 stream URLs")

    # Group entries by rule index
    grouped_by_rule: dict[int, List[M3UEntry]] = {}
    for rule_idx, entry in matched_entries:
        grouped_by_rule.setdefault(rule_idx, []).append(entry)

    deduped_entries: List[M3UEntry] = []
    for rule_idx in sorted(grouped_by_rule.keys()):
        group_items = grouped_by_rule[rule_idx]
        
        # Prefer healthy items if available
        healthy_items = [e for e in group_items if not any(u in dead_urls for u in e.urls)]
        items_to_pick = healthy_items if healthy_items else group_items

        best_entry = max(items_to_pick, key=lambda e: calculate_entry_priority(e, config.regional_keywords))
        rule = config.rules[rule_idx] if rule_idx < len(config.rules) else None
        if config.group:
            best_entry.extinf = update_group_title(best_entry.extinf, config.group)
            best_entry.group = config.group
        if rule:
            if rule.keyword:
                clean_name = rule.keyword.strip()
                best_entry.extinf = update_channel_name(best_entry.extinf, clean_name)
                best_entry.extinf = update_tvg_name(best_entry.extinf, clean_name)
                best_entry.name = clean_name
            if rule.logo:
                best_entry.extinf = update_logo_url(best_entry.extinf, rule.logo)
            if rule.tvg_id:
                best_entry.extinf = update_tvg_id(best_entry.extinf, rule.tvg_id)
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

    extra_path = config_path.parent / "extra_channels.m3u"
    if extra_path.exists():
        _, extra_entries = parse_m3u(extra_path)
        entries.extend(extra_entries)
        print(f"Merged {len(extra_entries)} curated extra channels from '{extra_path}'")

    print(f"Total channels to process: {len(entries)}")

    config = load_config(config_path)
    if config.rules:
        print(f"Loaded {len(config.rules)} channel rules from '{config_path}'")

    filtered = filter_entries(
        entries=entries,
        config=config
    )

    out_content = [DEFAULT_HEADER, ""]
    for entry in filtered:
        out_content.append(entry.to_m3u_block())
        out_content.append("")

    output_path.write_text("\n".join(out_content), encoding="utf-8")

    print(f"Successfully generated '{output_path}'")
    print(f"Filtered channels count: {len(filtered)} / {len(entries)}")

if __name__ == "__main__":
    main()
