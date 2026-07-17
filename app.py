"""Small HDHomeRun-compatible facade for user-authorized M3U live-TV sources."""

from __future__ import annotations

import html
import json
import mimetypes
import os
import re
import socket
import subprocess
import time
import base64
from dataclasses import dataclass, field
import http.cookiejar
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlparse
from urllib.request import Request, urlopen, HTTPCookieProcessor, build_opener

PORT = int(os.environ.get("PORT", "5004"))
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
PLAYLIST = CONFIG_DIR / "channels.m3u"
GUIDE = CONFIG_DIR / "guide.xml"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Read SCRAPER_PROVIDERS environment default: comma-separated list
env_providers = os.environ.get("SCRAPER_PROVIDERS", "2ix2tv,livedetv,lolmt2,2ix2")
default_providers = [p.strip() for p in env_providers.split(",") if p.strip()]

# Load/create config.json in the mounted config directory
config_data = {
    "PORT": PORT,
    "PUBLIC_BASE_URL": os.environ.get("PUBLIC_BASE_URL", ""),
    "TRANSCODE_UPSCALING": os.environ.get("TRANSCODE_UPSCALING", "false").lower() == "true",
    "TRANSCODE_RESOLUTION": os.environ.get("TRANSCODE_RESOLUTION", "1920x1080"),
    "TRANSCODE_SHARPEN": 0.5,
    "TRANSCODE_DENOISE": 1.0,
    "SCRAPER_PROVIDERS": default_providers
}

# Try reading from config.json if it exists
if CONFIG_FILE.exists():
    try:
        loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        # Merge loaded config, converting types appropriately
        if "PORT" in loaded:
            config_data["PORT"] = int(loaded["PORT"])
        if "PUBLIC_BASE_URL" in loaded:
            config_data["PUBLIC_BASE_URL"] = str(loaded["PUBLIC_BASE_URL"])
        if "TRANSCODE_UPSCALING" in loaded:
            val = loaded["TRANSCODE_UPSCALING"]
            config_data["TRANSCODE_UPSCALING"] = val if isinstance(val, bool) else str(val).lower() == "true"
        if "TRANSCODE_RESOLUTION" in loaded:
            config_data["TRANSCODE_RESOLUTION"] = str(loaded["TRANSCODE_RESOLUTION"])
        if "TRANSCODE_SHARPEN" in loaded:
            config_data["TRANSCODE_SHARPEN"] = float(loaded["TRANSCODE_SHARPEN"])
        if "TRANSCODE_DENOISE" in loaded:
            config_data["TRANSCODE_DENOISE"] = float(loaded["TRANSCODE_DENOISE"])
        if "SCRAPER_PROVIDERS" in loaded:
            provs = loaded["SCRAPER_PROVIDERS"]
            if isinstance(provs, list):
                config_data["SCRAPER_PROVIDERS"] = [str(p).strip() for p in provs if str(p).strip()]
            elif isinstance(provs, str):
                config_data["SCRAPER_PROVIDERS"] = [p.strip() for p in provs.split(",") if p.strip()]
        print(f"INFO: Loaded configuration from {CONFIG_FILE.name}", flush=True)
    except Exception as e:
        print(f"WARNING: Error reading config.json ({e}), using environment defaults.", flush=True)
else:
    # Save default config.json as a template
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(config_data, indent=2), encoding="utf-8")
        print(f"INFO: Created default configuration template at {CONFIG_FILE.name}", flush=True)
    except Exception as e:
        print(f"WARNING: Could not write default config.json template ({e})", flush=True)

# Assign settings to constants
PORT = config_data["PORT"]
PUBLIC_BASE_URL_CONFIG = config_data["PUBLIC_BASE_URL"]
TRANSCODE_UPSCALING = config_data["TRANSCODE_UPSCALING"]
TRANSCODE_RESOLUTION = config_data["TRANSCODE_RESOLUTION"]
TRANSCODE_SHARPEN = config_data["TRANSCODE_SHARPEN"]
TRANSCODE_DENOISE = config_data["TRANSCODE_DENOISE"]
SCRAPER_PROVIDERS = config_data["SCRAPER_PROVIDERS"]


@dataclass
class Channel:
    number: str
    name: str
    url: str
    tvg_id: str
    logo: str = ""
    provider_urls: dict[str, str] = field(default_factory=dict)


def attribute(value: str, name: str) -> str:
    match = re.search(rf'(?:^|\s){re.escape(name)}="([^"]*)"', value)
    return match.group(1) if match else ""


EPG_NAME_MAP = {
    "RTL": "RTL",
    "RTL2": "RTLZWEI",
    "VOX": "VOX",
    "N-TV": "n-tv",
    "RTL Nitro": "NITRO",
    "Super RTL": "SUPER RTL",
    "Pro7": "ProSieben",
    "Kabel 1": "kabel eins",
    "Sat1": "Sat.1",
    "Sixx": "sixx",
    "Dmax": "DMAX",
    "Sport1": "SPORT1",
    "ProSieben Maxx": "ProSieben MAXX",
    "Kabel 1 Doku": "Kabel Eins Doku",
    "TLC": "TLC",
    "Sat1 Gold": "Sat.1 Gold",
    "Tele 5": "Tele 5",
    "Comedy Central": "Comedy Central",
    "WELT": "WELT",
    "ZDFneo": "ZDFneo",
    "Disney Channel": "Disney Channel",
    "ARD": "Das Erste",
    "ZDF": "ZDF",
    "ZDFinfo": "ZDFinfo",
    "KiKa": "KiKa",
    "3Sat": "3sat",
    "Arte": "arte",
    "Tagesschau": "tagesschau24",
    "MDR Fernsehen": "MDR Fernsehen",
    "WDR Fernsehen": "WDR Fernsehen",
    "BR Fernsehen": "BR Fernsehen",
    "ONE TV": "ONE",
    "RBB Fernsehen": "rbb Fernsehen",
    "Phoenix": "PHOENIX",
    "HR Fernsehen": "hr-fernsehen",
    "NDR Fernsehen": "NDR Fernsehen",
    "MTV": "MTV",
    "SWR Fernsehen": "SWR Fernsehen",
    "SR Fernsehen": "SR Fernsehen",
    "ORF1": "ORF 1",
    "ORF2": "ORF 2",
    "ORF3": "ORF III",
    "Servus TV": "ServusTV"
}

EPG_ID_MAP = {
    "rtl": "RTL.de",
    "rtlt": "RTL.de",
    "rtl2": "RTLZwei.de",
    "rtl-2": "RTLZwei.de",
    "rtlzwei": "RTLZwei.de",
    "rtl-zwei": "RTLZwei.de",
    "vox": "Vox.de",
    "voxt": "Vox.de",
    "n-tv": "n-tv.de",
    "ntv": "n-tv.de",
    "rtl-nitro": "RTLNitro.de",
    "rtlnitro": "RTLNitro.de",
    "nitro": "RTLNitro.de",
    "superrtl": "SuperRTL.de",
    "super-rtl": "SuperRTL.de",
    "pro7": "ProSieben.de",
    "pro-7": "ProSieben.de",
    "prosieben": "ProSieben.de",
    "kabel1": "KabelEins.de",
    "kabel-1": "KabelEins.de",
    "kabel-eins": "KabelEins.de",
    "sat1": "Sat1.de",
    "sat-1": "Sat1.de",
    "sixx": "Sixx.de",
    "dmax": "DMAX.de",
    "sky-sport-news": "SkySportNews.de",
    "sport1": "Sport1.de",
    "prosieben-maxx": "ProSiebenMaxx.de",
    "prosiebenmaxx": "ProSiebenMaxx.de",
    "kabeleins-doku": "KabelEinsDoku.de",
    "kabel-1-doku": "KabelEinsDoku.de",
    "kabel-eins-doku": "KabelEinsDoku.de",
    "tlc": "TLC.de",
    "sat1gold": "Sat1Gold.de",
    "sat-1-gold": "Sat1Gold.de",
    "sat1-gold": "Sat1Gold.de",
    "tele5": "Tele5.de",
    "tele-5": "Tele5.de",
    "comedy-central": "ComedyCentralGermany.de",
    "comedycentral": "ComedyCentralGermany.de",
    "welt": "Welt.de",
    "zdfneo": "ZDFneo.de",
    "zdf-neo": "ZDFneo.de",
    "disney-channel": "DisneyChannel.de",
    "disney": "DisneyChannel.de",
    "ard": "DasErste.de",
    "das-erste": "DasErste.de",
    "daserste": "DasErste.de",
    "zdf": "ZDF.de",
    "zdfinfo": "ZDFinfo.de",
    "zdf-info": "ZDFinfo.de",
    "kika": "KiKa.de",
    "3sat": "3sat.de",
    "arte": "Arte.de",
    "tagesschau24": "Tagesschau24.de",
    "tagesschau": "Tagesschau24.de",
    "mdr": "MDRSachsen.de",
    "wdr": "WDRKoeln.de",
    "br": "BRFernsehenSued.de",
    "one": "One.de",
    "rbb": "rbbBerlin.de",
    "phoenix": "Phoenix.de",
    "hr": "hrfernsehen.de",
    "ndr": "NDRHamburg.de",
    "mtv": "MTVGermany.de",
    "swr": "SWRFernsehenBW.de",
    "sr": "SRFernsehen.de",
    "servustv": "ServusTVDeutschland.de",
    "serv-tv": "ServusTVDeutschland.de",
    "deluxemusic": "DeluxeMusic.de",
    "deluxe-music": "DeluxeMusic.de",
    "nickelodeon": "Nickelodeon.de",
    "nick": "Nickelodeon.de"
}


def normalize_slug(slug: str) -> str:
    cleaned = slug.lower().strip()
    # Strip common suffixes/prefixes
    cleaned = re.sub(r'-(?:live|livestream|stream|tv|fernsehen|de)$', '', cleaned)
    cleaned = re.sub(r'^(?:live|stream)-', '', cleaned)
    # Remove all non-alphanumeric characters
    cleaned = re.sub(r'[^a-z0-9]', '', cleaned)
    return cleaned


def lookup_name(parsed_name: str) -> str:
    parsed_clean = parsed_name.strip()
    for k, v in EPG_NAME_MAP.items():
        if k.lower() == parsed_clean.lower():
            return v
    return parsed_clean.title()


def lookup_epg_id(slug: str) -> str:
    norm = normalize_slug(slug)
    # Search normalized keys in EPG_ID_MAP
    for k, v in EPG_ID_MAP.items():
        if normalize_slug(k) == norm:
            return v
    return f"iptv-{norm}"


# Global Cache for Scraped Playlists
_aggregated_channels_cache: list[Channel] = []
_last_scraped_time: float = 0.0


# 1. 2ix2tv / nydus / lolmt2 Scraper
def get_2ix2tv_channels(host_url: str = "https://2ix2tv.de/") -> list[Channel]:
    req = Request(
        host_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
    )
    try:
        with urlopen(req, timeout=12) as response:
            content = response.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"WARNING: Error scraping {host_url}: {e}", flush=True)
        return []

    # class="tvsender" onclick="showPlayerHQ('xxx');"><img src="logo" alt="name"
    matches = re.finditer(
        r'class="tvsender"\s+onclick="showPlayerHQ\(\'([^\'\s]+)\'\);"[^>]*>.*?src="([^"]+)"\s+alt="([^"]+)"',
        content,
        re.DOTALL | re.IGNORECASE
    )

    channels: list[Channel] = []
    seen_slugs = set()
    for m in matches:
        slug, logo, name = m.groups()
        slug_lower = slug.lower()
        if slug_lower in seen_slugs:
            continue
        seen_slugs.add(slug_lower)
        
        name_clean = lookup_name(html.unescape(name))
        tvg = lookup_epg_id(slug_lower)
        if logo.startswith("//"):
            logo = "https:" + logo
        elif logo.startswith("/"):
            logo = "https://nydus.org" + logo
            
        ch = Channel(
            number="",
            name=name_clean,
            url=f"https://2ix2tv.de/{slug_lower}",
            tvg_id=tvg,
            logo=logo
        )
        ch.provider_urls["2ix2tv"] = f"https://2ix2tv.de/{slug_lower}"
        ch.provider_urls["lolmt2"] = f"https://lolmt2.com/{slug_lower}"
        ch.provider_urls["nydus"] = f"https://lolmt2.com/{slug_lower}"
        ch.provider_urls["raidrush"] = f"https://lolmt2.com/{slug_lower}"
        channels.append(ch)
        
    return channels


def resolve_2ix2tv_stream(slug: str) -> str:
    url = f"https://nydus.org/stream/embedplayer_hq.php?id={slug}&ewr=1"
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://2ix2tv.de/"
        }
    )
    try:
        with urlopen(req, timeout=10) as response:
            content = response.read().decode("utf-8", errors="ignore")
            match = re.search(r'function initClappr\(\)\{\s*var zdec\s*=\s*["\']([^"\']+)["\']', content)
            if not match:
                print(f"WARNING: initClappr base64 not found for {slug} on nydus.org", flush=True)
                return ""
                
            zdec = match.group(1)
            zdec_decoded = base64.b64decode(zdec).decode('utf-8', errors='ignore')
            inner_b64 = re.search(r"atob\(['\"]([^'\"]+)['\"]\)", zdec_decoded)
            if inner_b64:
                embed_url = base64.b64decode(inner_b64.group(1)).decode('utf-8', errors='ignore')
                
                # Auto-rewrite Flussonic hopslan embed URLs to direct index.m3u8 playlists
                if "hopslan.com" in embed_url and "embed.html" in embed_url:
                    hop_match = re.search(r'https?://([^/]+)/([^/]+)/embed\.html', embed_url)
                    if hop_match:
                        host, stream_name = hop_match.groups()
                        return f"https://{host}/{stream_name}/index.m3u8"
                return embed_url
            else:
                direct_url = re.search(r'drawIFR2\([\'"]([^\'"]+)[\'"]\)', zdec_decoded)
                if direct_url:
                    return direct_url.group(1)
    except Exception as e:
        print(f"Error resolving 2ix2tv stream for slug '{slug}': {e}", flush=True)
    return ""


# 2. livedetv Scraper & Session Cookie Resolver
def get_livedetv_channels() -> list[Channel]:
    host_url = "https://livedetv.com/"
    req = Request(
        host_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
    )
    try:
        with urlopen(req, timeout=12) as response:
            content = response.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"WARNING: Error scraping {host_url}: {e}", flush=True)
        return []

    # Parse channel link cards
    matches = re.finditer(
        r'<a href="(https?://(?:www\.)?livedetv\.com/([^"/]+)/?)"[^>]*>.*?<img[^>]+src="([^"]+)"[^>]*>.*?<span class="colorchannel">([^<]+)</span>',
        content,
        re.DOTALL | re.IGNORECASE
    )

    channels: list[Channel] = []
    seen_slugs = set()
    for m in matches:
        href, slug, logo, name = m.groups()
        slug_lower = slug.lower()
        if slug_lower in seen_slugs or slug_lower in ("sayfa", "kategori", "giris", "kayit", "kanallistesi", "ulke"):
            continue
        seen_slugs.add(slug_lower)
        
        name_clean = lookup_name(html.unescape(name))
        tvg = lookup_epg_id(slug_lower)
        
        ch = Channel(
            number="",
            name=name_clean,
            url=href,
            tvg_id=tvg,
            logo=logo
        )
        ch.provider_urls["livedetv"] = href
        channels.append(ch)
        
    return channels


def resolve_livedetv_stream(channel_url: str) -> str:
    cj = http.cookiejar.CookieJar() if "http.cookiejar" in globals() else http.cookiejar.CookieJar()
    # build a clean custom opener to maintain PHP Session cookies across nested frame fetches
    opener = build_opener(HTTPCookieProcessor(cj))
    opener.addheaders = [
        ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"),
        ("Accept-Language", "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7")
    ]
    
    try:
        # Step 1: Open main channel page to start session cookies
        req1 = Request(channel_url)
        with opener.open(req1, timeout=8) as r1:
            r1.read()
            
        # Step 2: Fetch embed frame page
        slug = channel_url.rstrip("/").rsplit("/", 1)[-1]
        embed_url = f"https://www.livedetv.com/embed/{slug}/"
        req2 = Request(embed_url, headers={"Referer": channel_url})
        with opener.open(req2, timeout=8) as r2:
            embed_content = r2.read().decode("utf-8", errors="ignore")
            
        # Step 3: Fetch yayin page using dynamic security bcrypt token
        yayin_match = re.search(r'<iframe[^>]+src=["\'](https?://www\.livedetv\.com/yayin/[^"\']+)["\']', embed_content)
        if not yayin_match:
            return ""
            
        yayin_url = yayin_match.group(1)
        req3 = Request(
            yayin_url,
            headers={
                "Referer": embed_url,
                "Sec-Fetch-Dest": "iframe",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin"
            }
        )
        with opener.open(req3, timeout=8) as r3:
            yayin_content = r3.read().decode("utf-8", errors="ignore")
            
        # Step 4: Fetch token.php page
        token_match = re.search(r'src=["\'](https?://www\.livedetv\.com/token\.php\?stream=[^"\']+)["\']', yayin_content)
        if not token_match:
            return ""
            
        token_url = token_match.group(1)
        req4 = Request(token_url, headers={"Referer": yayin_url})
        with opener.open(req4, timeout=8) as r4:
            token_content = r4.read().decode("utf-8", errors="ignore")
            
        # Step 5: Extract final direct helpfullive.info HLS stream URL
        m3u8_match = re.search(r'(https?://[^\s"\'`<>]+?\.m3u8[^\s"\'`<>]*)', token_content)
        if m3u8_match:
            return m3u8_match.group(1)
            
    except Exception as e:
        print(f"Error resolving livedetv stream for {channel_url}: {e}", flush=True)
    return ""


# 3. Legacy 2ix2 Scraper & Resolver
def get_2ix2_channels() -> list[Channel]:
    homepage_url = "https://www.2ix2.com/"
    req = Request(
        homepage_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
    )
    try:
        with urlopen(req, timeout=12) as response:
            homepage = response.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"WARNING: Error scraping 2ix2 homepage: {e}", flush=True)
        return []

    matches = re.finditer(
        r'href="(https://www\.2ix2\.com/([a-zA-Z0-9\-]+)/?)"[^>]*>\s*<img\s+src="([^"]+)"\s+alt="([^"]+)"',
        homepage,
        re.IGNORECASE
    )

    channels: list[Channel] = []
    seen_urls = set()
    for m in matches:
        url, slug, logo, name = m.groups()
        if url in seen_urls:
            continue
        seen_urls.add(url)
        name_clean = lookup_name(html.unescape(name))
        tvg = lookup_epg_id(slug.lower())
        if logo.startswith("//"):
            logo = "https:" + logo
            
        ch = Channel(
            number="",
            name=name_clean,
            url=url,
            tvg_id=tvg,
            logo=logo
        )
        ch.provider_urls["2ix2"] = url
        channels.append(ch)
    return channels


def resolve_2ix2_stream(page_url: str) -> str:
    req = Request(
        page_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.2ix2.com/"
        }
    )
    try:
        with urlopen(req, timeout=10) as response:
            content = response.read().decode("utf-8", errors="ignore")
            match = re.search(r'file:\s*["\']([^"\']+)["\']', content)
            if match:
                val = match.group(1).strip()
                if val and "[php_everywhere]" not in val:
                    return val
                else:
                    print(f"WARNING: Player source for {page_url} contains '[php_everywhere]' or is empty on 2ix2.com.", flush=True)
            else:
                print(f"WARNING: No stream path (file:) found in player source code on {page_url}.", flush=True)
    except Exception as e:
        print(f"Error resolving 2ix2 stream {page_url}: {e}", flush=True)
    return ""


def parse_playlist() -> list[Channel]:
    """Read M3U playlist if it exists, otherwise auto-discover and merge channels from SCRAPER_PROVIDERS."""
    if PLAYLIST.exists():
        channels: list[Channel] = []
        info = ""
        for raw in PLAYLIST.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if line.startswith("#EXTINF:"):
                info = line
            elif line and not line.startswith("#") and info:
                title = info.rsplit(",", 1)[-1].strip() or "Unnamed channel"
                tvg_id = attribute(info, "tvg-id") or f"channel-{len(channels) + 1}"
                number = attribute(info, "tvg-chno") or str(10000 + len(channels) + 1)
                channels.append(Channel(number, title, line, tvg_id, attribute(info, "tvg-logo")))
                info = ""
        return channels

    global _aggregated_channels_cache, _last_scraped_time
    now = time.time()
    # Cache channels list for 12 hours (43200 seconds)
    if _aggregated_channels_cache and (now - _last_scraped_time < 43200):
        return _aggregated_channels_cache

    print(f"INFO: Running channel discovery across providers: {SCRAPER_PROVIDERS}", flush=True)
    channels_by_key: dict[str, Channel] = {}
    ordered_keys: list[str] = []

    for provider in SCRAPER_PROVIDERS:
        provider_clean = provider.lower().strip()
        scraped: list[Channel] = []
        
        if provider_clean in ("2ix2tv", "2ix2tv.de"):
            scraped = get_2ix2tv_channels("https://2ix2tv.de/")
        elif provider_clean in ("nydus", "nydus.org", "lolmt2", "lolmt2.com", "raidrush", "raidrush.net"):
            # nydus blocks non-JS clients; scrape its mirror lolmt2.com instead
            scraped = get_2ix2tv_channels("https://lolmt2.com/")
        elif provider_clean in ("livedetv", "livedetv.com"):
            scraped = get_livedetv_channels()
        elif provider_clean in ("2ix2", "2ix2.com"):
            scraped = get_2ix2_channels()
        else:
            print(f"WARNING: Unknown scraper provider '{provider}' configured. Skipping.", flush=True)
            continue
            
        print(f"INFO: Scraper provider '{provider}' returned {len(scraped)} channels.", flush=True)
        
        for ch in scraped:
            # Match channels case-insensitively by EPG TVG ID
            match_key = ch.tvg_id.lower().strip()
            if match_key not in channels_by_key:
                channels_by_key[match_key] = ch
                ordered_keys.append(match_key)
            else:
                # Merge provider URLs into the existing channel object
                existing_ch = channels_by_key[match_key]
                for p_name, p_url in ch.provider_urls.items():
                    existing_ch.provider_urls[p_name] = p_url

    # Assign numbers starting from 10001
    final_channels: list[Channel] = []
    for idx, key in enumerate(ordered_keys):
        ch = channels_by_key[key]
        num = str(10000 + idx + 1)
        final_channels.append(Channel(
            number=num,
            name=ch.name,
            url=ch.url,
            tvg_id=ch.tvg_id,
            logo=ch.logo,
            provider_urls=ch.provider_urls
        ))

    if final_channels:
        _aggregated_channels_cache = final_channels
        _last_scraped_time = now
        print(f"INFO: Aggregated total of {len(final_channels)} unique channels across all scrapers.", flush=True)
        
    return final_channels or _aggregated_channels_cache


def public_base(handler: BaseHTTPRequestHandler) -> str:
    configured = PUBLIC_BASE_URL_CONFIG.rstrip("/")
    if configured:
        if not configured.startswith(("http://", "https://")):
            configured = "http://" + configured
        return configured
    host = handler.headers.get("Host", f"{handler.server.server_address[0]}:{PORT}")
    return f"http://{host}"


def rewrite_manifest(body: bytes, origin: str, proxy_base: str) -> bytes:
    """Keep HLS child playlists, keys and segments flowing through this proxy."""
    try:
        source = body.decode("utf-8")
    except UnicodeDecodeError:
        return body

    # Filter out separate audio/subtitles lines and clean stream-inf headers
    lines = source.splitlines()
    filtered_lines = []
    for line in lines:
        if line.startswith("#EXT-X-MEDIA:TYPE=AUDIO") or line.startswith("#EXT-X-MEDIA:TYPE=SUBTITLES"):
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            # Strip AUDIO and SUBTITLES attributes
            line = re.sub(r',?AUDIO="[^"]*"', '', line)
            line = re.sub(r',?SUBTITLES="[^"]*"', '', line)
        filtered_lines.append(line)
    source = "\n".join(filtered_lines)

    def proxied(target: str) -> str:
        return f"{proxy_base}/proxy?url={quote(urljoin(origin, target), safe='')}"

    # URI attributes occur in EXT-X-KEY, EXT-X-MAP and media/master playlist tags.
    source = re.sub(r'URI="([^"]+)"', lambda m: f'URI="{proxied(m.group(1))}"', source)
    output: list[str] = []
    for line in source.splitlines():
        if line and not line.startswith("#"):
            output.append(proxied(line))
        else:
            output.append(line)
    return ("\n".join(output) + "\n").encode("utf-8")


class TunerHandler(BaseHTTPRequestHandler):
    server_version = "IPTVTuner/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def send_json(self, payload: object) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_text(self, data: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        base = public_base(self)
        if path == "/discover.json":
            self.send_json({
                "FriendlyName": "Docker IPTV Tuner",
                "ModelNumber": "HDHR-IPTV",
                "FirmwareName": "iptv-tuner",
                "FirmwareVersion": "1.0",
                "DeviceID": "D0C0DE01",
                "DeviceAuth": "iptv-tuner",
                "TunerCount": 4,
                "BaseURL": base,
                "LineupURL": f"{base}/lineup.json",
            })
        elif path == "/lineup_status.json":
            self.send_json({"ScanInProgress": 0, "ScanPossible": 0, "Source": "M3U", "SourceList": ["M3U"]})
        elif path == "/lineup.json":
            self.send_json([{
                "GuideNumber": c.number,
                "GuideName": c.name,
                "URL": f"{base}/stream/{index}",
                "HD": 1,
            } for index, c in enumerate(parse_playlist())])
        elif path == "/playlist.m3u":
            lines = ["#EXTM3U"]
            for index, c in enumerate(parse_playlist()):
                attrs = f' tvg-id="{html.escape(c.tvg_id, quote=True)}" tvg-chno="{c.number}"'
                if c.logo:
                    attrs += f' tvg-logo="{html.escape(c.logo, quote=True)}"'
                lines.extend([f"#EXTINF:-1{attrs},{c.name}", f"{base}/stream/{index}"])
            self.send_text(("\n".join(lines) + "\n").encode(), "audio/x-mpegurl; charset=utf-8")
        elif path == "/guide.xml":
            if not GUIDE.exists():
                self.send_error(HTTPStatus.NOT_FOUND, "Add /config/guide.xml to enable EPG")
                return
            self.send_text(GUIDE.read_bytes(), "application/xml; charset=utf-8")
        elif path.startswith("/stream/"):
            try:
                index = int(path.rsplit("/", 1)[1])
                channel = parse_playlist()[index]
            except (ValueError, IndexError):
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown channel")
                return
            # Priority-based stream failover/resolution loop
            stream_url = ""
            resolved_success = False
            
            for provider in SCRAPER_PROVIDERS:
                prov_key = provider.lower().strip()
                # Check if this provider offers a source link for the target channel
                if prov_key in channel.provider_urls:
                    prov_url = channel.provider_urls[prov_key]
                    print(f"INFO: Attempting to resolve stream for '{channel.name}' via provider '{provider}'...", flush=True)
                    resolved = ""
                    
                    if prov_key in ("2ix2tv", "nydus", "lolmt2", "raidrush"):
                        slug = prov_url.rstrip("/").rsplit("/", 1)[-1]
                        resolved = resolve_2ix2tv_stream(slug)
                    elif prov_key in ("livedetv", "livedetv.com"):
                        resolved = resolve_livedetv_stream(prov_url)
                    elif prov_key in ("2ix2", "2ix2.com"):
                        resolved = resolve_2ix2_stream(prov_url)
                        
                    if resolved:
                        # Quick sanity check: did it resolve to a Cloudflare challenge wrapper?
                        if "challenges.cloudflare.com" in resolved or "/embed.php" in resolved:
                            print(f"WARNING: Stream for '{channel.name}' on provider '{provider}' is protected by Cloudflare and cannot be tuned directly.", flush=True)
                            # Fall through to next provider
                            continue
                        
                        stream_url = resolved
                        resolved_success = True
                        print(f"INFO: Successfully resolved stream for '{channel.name}' via provider '{provider}': {stream_url}", flush=True)
                        break
                    else:
                        print(f"WARNING: Resolution for '{channel.name}' via provider '{provider}' failed or returned empty.", flush=True)
            
            if not resolved_success:
                print(f"ERROR: Stream for '{channel.name}' could not be resolved by any of the configured providers: {SCRAPER_PROVIDERS}", flush=True)
                self.send_error(HTTPStatus.BAD_GATEWAY, f"Stream for '{channel.name}' is currently not available on any providers.")
                return

            # Pre-resolve HLS to check if it's a playlist or needs master playlist resolution
            is_hls = False
            req = Request(stream_url, headers={"User-Agent": "IPTV-Tuner/1.0", "Accept": "*/*"})
            try:
                with urlopen(req, timeout=10) as response:
                    content_type = response.headers.get_content_type()
                    is_hls = content_type in {"application/vnd.apple.mpegurl", "application/x-mpegurl"} or urlparse(stream_url).path.lower().endswith(".m3u8")
                    if is_hls:
                        body_bytes = response.read()
                        body_str = body_bytes.decode("utf-8", errors="ignore")
                        resolved_url = response.url

                        # If it is a master playlist, extract the child playlist
                        if "#EXT-X-STREAM-INF:" in body_str:
                            video_url = ""
                            for line in body_str.splitlines():
                                line = line.strip()
                                if line and not line.startswith("#"):
                                    video_url = urljoin(resolved_url, line)
                                    break
                            if video_url:
                                if "/proxy?url=" in video_url:
                                    video_url = unquote(video_url.split("/proxy?url=", 1)[1])
                                stream_url = video_url
                            else:
                                stream_url = resolved_url
                        else:
                            stream_url = resolved_url
            except Exception as e:
                print(f"Error pre-resolving HLS stream: {e}", flush=True)

            if is_hls:
                print(f"Starting continuous HLS-to-TS stream for: {stream_url}", flush=True)
                self.stream_hls_as_ts(stream_url)
            else:
                self.proxy(stream_url, base)
        elif path == "/proxy":
            target = unquote(parsed.query.removeprefix("url="))
            if not target.startswith(("http://", "https://")):
                self.send_error(HTTPStatus.BAD_REQUEST, "Only HTTP(S) stream URLs are supported")
                return
            self.proxy(target, base)
        elif path in ("/", "/health"):
            self.send_json({"status": "ok", "channels": len(parse_playlist())})
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def stream_hls_as_ts(self, m3u8_url: str) -> None:
        """Stream HLS playlist segments as a continuous MPEG-TS stream to the client."""
        if TRANSCODE_UPSCALING:
            # Parse target resolution dimensions
            width, height = 1920, 1080
            if "x" in TRANSCODE_RESOLUTION:
                try:
                    w_str, h_str = TRANSCODE_RESOLUTION.lower().split("x", 1)
                    width, height = int(w_str), int(h_str)
                except ValueError:
                    pass

            # Build ffmpeg filters: denoise -> scale -> sharpen
            # hqdn3d removes digital compression blockiness before scaling/sharpening
            vf_filters = []
            if TRANSCODE_DENOISE > 0.0:
                # scale parameters relative to TRANSCODE_DENOISE strength
                luma_sp = TRANSCODE_DENOISE * 1.5
                chroma_sp = TRANSCODE_DENOISE * 1.5
                luma_tmp = TRANSCODE_DENOISE * 3.0
                chroma_tmp = TRANSCODE_DENOISE * 3.0
                vf_filters.append(f"hqdn3d={luma_sp}:{chroma_sp}:{luma_tmp}:{chroma_tmp}")
            
            vf_filters.append(f"scale={width}:{height}:flags=lanczos")
            
            if TRANSCODE_SHARPEN > 0.0:
                vf_filters.append(f"unsharp=5:5:{TRANSCODE_SHARPEN}:5:5:0.0")

            vf = ",".join(vf_filters)

            # Execute ffmpeg reading the HLS manifest directly and outputting MPEG-TS to stdout
            cmd = [
                "ffmpeg",
                "-re",                              # Read input at native frame rate
                "-i", m3u8_url,                     # Input HLS stream
                "-vf", vf,                          # Video filters (scale + sharpen)
                "-c:v", "libx264",                  # Encode video to H.264
                "-preset", "ultrafast",             # Minimize CPU load (crucial for NAS)
                "-tune", "zerolatency",             # Keep latency as low as possible
                "-c:a", "copy",                     # Copy audio stream losslessly
                "-f", "mpegts",                     # Output container format
                "pipe:1"                            # Output to stdout
            ]

            print(f"Launching ffmpeg upscaler: {' '.join(cmd)}", flush=True)
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except Exception as e:
                print(f"Error launching ffmpeg upscaler: {e}", flush=True)
                self.send_error(HTTPStatus.BAD_GATEWAY, f"Failed to launch ffmpeg upscaler: {e}")
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            try:
                while True:
                    chunk = proc.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                print("Client disconnected from upscaled TS stream", flush=True)
            finally:
                proc.terminate()
                proc.wait()
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        sent_segments = set()

        while True:
            # Fetch the playlist
            req = Request(m3u8_url, headers={"User-Agent": "IPTV-Tuner/1.0", "Accept": "*/*"})
            try:
                with urlopen(req, timeout=10) as resp:
                    playlist_content = resp.read().decode("utf-8", errors="ignore")
                    playlist_url = resp.url
            except Exception as e:
                print(f"Error fetching playlist in TS streamer: {e}", flush=True)
                time.sleep(2)
                continue

            # Parse segments
            lines = playlist_content.splitlines()
            segments = []
            target_duration = 6.0
            for line in lines:
                line = line.strip()
                if line.startswith("#EXT-X-TARGETDURATION:"):
                    try:
                        target_duration = float(line.split(":", 1)[1])
                    except ValueError:
                        pass
                elif line and not line.startswith("#"):
                    segments.append(urljoin(playlist_url, line))

            # Send new segments
            new_segments = [s for s in segments if s not in sent_segments]
            for seg_url in new_segments:
                print(f"Streaming TS segment to client: {seg_url}", flush=True)
                seg_req = Request(seg_url, headers={"User-Agent": "IPTV-Tuner/1.0", "Accept": "*/*"})
                try:
                    with urlopen(seg_req, timeout=15) as seg_resp:
                        while chunk := seg_resp.read(64 * 1024):
                            self.wfile.write(chunk)
                    sent_segments.add(seg_url)
                except (BrokenPipeError, ConnectionResetError):
                    print("Client disconnected from TS stream", flush=True)
                    return
                except Exception as e:
                    print(f"Error streaming TS segment {seg_url}: {e}", flush=True)

            # Keep set size reasonable
            if len(sent_segments) > 100:
                sent_segments = set(list(sent_segments)[-20:])

            # Sleep for half of target duration (e.g. 3s)
            time.sleep(max(1.0, target_duration / 2.0))

    def proxy(self, target: str, base: str) -> None:
        request = Request(target, headers={"User-Agent": "IPTV-Tuner/1.0", "Accept": "*/*"})
        try:
            with urlopen(request, timeout=20) as response:
                content_type = response.headers.get_content_type()
                is_hls = content_type in {"application/vnd.apple.mpegurl", "application/x-mpegurl"} or urlparse(target).path.lower().endswith(".m3u8")
                if is_hls:
                    body_bytes = response.read()
                    body_str = body_bytes.decode("utf-8", errors="ignore")
                    if "#EXT-X-STREAM-INF:" in body_str:
                        video_url = ""
                        for line in body_str.splitlines():
                            line = line.strip()
                            if line and not line.startswith("#"):
                                video_url = urljoin(response.url, line)
                                break
                        if video_url:
                            if "/proxy?url=" in video_url:
                                video_url = unquote(video_url.split("/proxy?url=", 1)[1])
                            self.proxy(video_url, base)
                            return
                    body = rewrite_manifest(body_bytes, response.url, base)
                    self.send_text(body, "application/vnd.apple.mpegurl")
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", response.headers.get("Content-Type", mimetypes.guess_type(target)[0] or "application/octet-stream"))
                length = response.headers.get("Content-Length")
                if length:
                    self.send_header("Content-Length", length)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    while chunk := response.read(64 * 1024):
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        except (HTTPError, URLError, TimeoutError) as error:
            self.send_error(HTTPStatus.BAD_GATEWAY, f"Upstream stream error: {error}")


import shutil

if __name__ == "__main__":
    # Check if ffmpeg is available on startup to help users debug container updates
    ffmpeg_found = shutil.which("ffmpeg") is not None
    if ffmpeg_found:
        print("INFO: 'ffmpeg' found on system path. ScrapeTuner is ready for real-time video upscaling.", flush=True)
    else:
        if TRANSCODE_UPSCALING:
            print("WARNING: TRANSCODE_UPSCALING is set to True, but 'ffmpeg' was NOT found on the system path! Upscaling will fail.", flush=True)
        else:
            print("INFO: 'ffmpeg' not found. ScrapeTuner will run in native pass-through mode.", flush=True)

    ThreadingHTTPServer(("0.0.0.0", PORT), TunerHandler).serve_forever()
