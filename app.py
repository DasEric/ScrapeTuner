"""Small HDHomeRun-compatible facade for user-authorized M3U live-TV sources."""

from __future__ import annotations

import base64
import binascii
import html
import http.cookiejar
import json
import mimetypes
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
from itertools import islice
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse
from urllib.request import Request, urlopen, HTTPRedirectHandler, HTTPCookieProcessor, build_opener

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
    "SCRAPER_PROVIDERS": default_providers,
    "CHANNEL_SOURCE_OVERRIDES": {},
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
        if "CHANNEL_SOURCE_OVERRIDES" in loaded:
            config_data["CHANNEL_SOURCE_OVERRIDES"] = loaded["CHANNEL_SOURCE_OVERRIDES"]
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
FFMPEG_FOUND = shutil.which("ffmpeg") is not None
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
UPSTREAM_ERRORS = (OSError, ValueError, HTTPException)

PROVIDER_HOME = {
    "2ix2tv": "https://2ix2tv.de/",
    "livedetv": "https://www.livedetv.com/",
    "lolmt2": "https://lolmt2.com/",
    "nydus": "https://nydus.org/stream/",
    "raidrush": "https://raidrush.net/stream/",
    "2ix2": "https://www.2ix2.com/",
}
PROVIDER_ALIASES = {
    urlparse(url).hostname.removeprefix("www."): key for key, url in PROVIDER_HOME.items()
}
TILE_PROVIDERS = {"2ix2tv", "lolmt2", "nydus", "raidrush"}
TILE_EMBED_HOST = re.compile(
    r"s\d+\.(?:2ix2tv|2ix2com|2ix2-alternative|mysql-export-crypt|mysql2dumpcrypt|"
    r"cryptstream|tv24livestream[12]?)\.de"
)


def normalize_providers(values: list[str]) -> list[str]:
    providers = []
    for value in values:
        key = value.strip().lower().removeprefix("www.")
        key = PROVIDER_ALIASES.get(key, key)
        if key not in PROVIDER_HOME:
            print(f"WARNING: Unknown scraper provider '{value}' configured. Skipping.", flush=True)
        elif key not in providers:
            providers.append(key)
    return providers


SCRAPER_PROVIDERS = normalize_providers(config_data["SCRAPER_PROVIDERS"])


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


def get_provider_headers(provider: str) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if provider in PROVIDER_HOME:
        headers["Referer"] = PROVIDER_HOME[provider]
    return headers


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
_provider_catalogs: dict[str, tuple[float, dict[str, Channel]]] = {}
_provider_catalog_locks = {provider: Lock() for provider in PROVIDER_HOME}
PROVIDER_REFRESH_SECONDS = 300


class ProviderMarkup(HTMLParser):
    """Read the small part of the markup used by channel cards and players."""

    def __init__(self, page: str):
        super().__init__(convert_charrefs=True)
        self.tiles = []
        self.cards = []
        self.frames = []
        self.media = []
        self.tile = None
        self.anchor = None
        self.in_channel_name = False
        self.feed(page)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        classes = (attrs.get("class") or "").split()
        if "tvsender" in classes:
            click = re.fullmatch(r"""\s*showPlayerHQ\(\s*(['"])([a-zA-Z0-9_-]+)\1\s*\)\s*;?\s*""",
                                 attrs.get("onclick") or "")
            self.tile = click[2] if click else None
        if tag == "a":
            self.anchor = [attrs.get("href") or "", "", "", ""]
            self.in_channel_name = False
        elif tag == "span" and "colorchannel" in classes:
            self.in_channel_name = True
        elif tag == "img":
            logo = attrs.get("data-src") or attrs.get("src") or ""
            name = attrs.get("alt") or ""
            if self.tile:
                self.tiles.append((self.tile, logo, name or self.tile))
                self.tile = None
            if self.anchor and not self.anchor[1]:
                self.anchor[1:3] = logo, name
        elif tag == "iframe" and attrs.get("src"):
            self.frames.append(attrs["src"])
        elif tag in ("source", "video") and attrs.get("src"):
            self.media.append(attrs["src"])

    def handle_data(self, data):
        if self.anchor and self.in_channel_name:
            self.anchor[3] += data

    def handle_endtag(self, tag):
        if tag == "span":
            self.in_channel_name = False
        elif tag == "div":
            self.tile = None
        elif tag == "a" and self.anchor:
            self.cards.append(self.anchor)
            self.anchor = None
            self.in_channel_name = False


def fetch_page(url: str, headers: dict[str, str], open_url=None) -> tuple[str, str]:
    if open_url is None:
        open_url = urlopen
    with open_url(Request(url, headers=headers), timeout=7) as response:
        body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise ValueError("page exceeds size limit")
        return response.url, body.decode("utf-8", errors="replace")


def http_url(base: str, value: str) -> str:
    value = value.strip()
    if not value or value.startswith(("#", "[", "<")):
        return ""
    url = urljoin(base, value)
    parsed = urlparse(url)
    return url if parsed.scheme in ("http", "https") and parsed.hostname else ""


def normalize_channel_overrides(values: object) -> dict[str, str]:
    if not isinstance(values, dict):
        print("WARNING: CHANNEL_SOURCE_OVERRIDES must map channel IDs to direct stream URLs.", flush=True)
        return {}
    overrides = {}
    for channel_id, source in values.items():
        key = channel_id.strip().casefold() if isinstance(channel_id, str) else ""
        try:
            url = http_url("", source) if isinstance(source, str) else ""
        except ValueError:
            url = ""
        if not key or not url:
            print("WARNING: Ignoring invalid entry in CHANNEL_SOURCE_OVERRIDES.", flush=True)
            continue
        overrides[key] = url
    return overrides


CHANNEL_SOURCE_OVERRIDES = normalize_channel_overrides(config_data["CHANNEL_SOURCE_OVERRIDES"])


def provider_for_url(url: str) -> str:
    try:
        hostname = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return PROVIDER_ALIASES.get(hostname.removeprefix("www."), "")


def tile_player(page: str, base: str, slug: str) -> str:
    """Use the literal iframe target advertised by this listing, not a mirror's."""
    special = re.search(
        r"""\bif\s*\(\s*id\s*={2,3}\s*['"]""" + re.escape(slug) +
        r"""['"]\s*\)\s*\{\s*player\.src\s*=\s*(['"])(.*?)\1""", page
    )
    if special:
        return http_url(base, special[2])
    template = re.search(
        r"""\bplayer\.src\s*=\s*(['"])([^'"]+)\1\s*\+\s*(?:id\b|encodeURIComponent\(\s*id\s*\))"""
        r"""(?:\s*\+\s*(['"])([^'"]*)\3(?!\s*\+))?""",
        page,
    )
    return http_url(base, template[2] + quote(slug, safe="") + (template[4] or "")) if template else ""


def discover_provider(provider: str) -> list[Channel]:
    base, page = fetch_page(PROVIDER_HOME[provider], get_provider_headers(provider))
    markup = ProviderMarkup(page)
    entries = []
    if provider in TILE_PROVIDERS:
        for slug, logo, name in markup.tiles:
            player = tile_player(page, base, slug)
            if player:
                entries.append((slug, logo, name, player))
    else:
        for href, logo, alt, title in markup.cards:
            target = http_url(base, href)
            if not target or provider_for_url(target) != provider:
                continue
            slug = urlparse(target).path.strip("/")
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", slug):
                continue
            name = title.strip() if provider == "livedetv" else alt
            if name and logo:
                entries.append((slug, logo, name, target))

    channels = {}
    for slug, logo, name, target in entries:
        tvg_id = lookup_epg_id(slug)
        channels.setdefault(tvg_id.lower(), Channel(
            "", lookup_name(name), target, tvg_id, http_url(base, logo), {provider: target},
        ))
    return list(channels.values())


def provider_catalog(provider: str) -> dict[str, Channel]:
    """Refresh source URLs independently of the stable, numbered tuner lineup."""
    with _provider_catalog_locks[provider]:
        now = time.monotonic()
        checked, previous = _provider_catalogs.get(provider, (float("-inf"), {}))
        if now - checked < PROVIDER_REFRESH_SECONDS:
            return previous
        try:
            channels = discover_provider(provider)
            current = {channel.tvg_id.casefold(): channel for channel in channels}
        except UPSTREAM_ERRORS as error:
            print(f"WARNING: Channel discovery for '{provider}' failed ({type(error).__name__}).", flush=True)
            current = {}
        # Keep the previous URLs during an outage; cache failures to avoid a
        # network retry for every client request.
        catalog = current or previous
        _provider_catalogs[provider] = now, catalog
        return catalog


def channel_provider_urls(channel: Channel) -> dict[str, str]:
    urls = dict(channel.provider_urls)
    for provider in SCRAPER_PROVIDERS:
        fresh = provider_catalog(provider).get(channel.tvg_id.casefold())
        if fresh:
            urls.update(fresh.provider_urls)
    return urls


PLAYER_SOURCE = re.compile(r"""\b(?:file|source)\s*:\s*(['"])([^'"]+)\1""", re.I)
HLS_LITERAL = re.compile(r"""((?:https?://|//|/|\.\.?/)[^\s"'<>]+?\.m3u8(?:[?#][^\s"'<>]*)?)""", re.I)


def advertised_media_urls(page: str, markup: ProviderMarkup, base: str, *, hls_literals=False):
    player = page.replace(r"\/", "/").replace("&amp;", "&")
    sources = [*markup.media, *(m[2] for m in PLAYER_SOURCE.finditer(player))]
    if hls_literals:
        sources.extend(HLS_LITERAL.findall(player))
    seen = set()
    for source in sources:
        if (url := http_url(base, source)) and url not in seen:
            seen.add(url)
            yield url


def advertised_media(page: str, markup: ProviderMarkup, base: str) -> str:
    return next(advertised_media_urls(page, markup, base), "")


class BrowserPlayerOnly(ValueError):
    """The provider advertises a browser player, but no direct stream."""


@dataclass(frozen=True)
class WrappedHls:
    url: str
    host: str
    segment_offset: int

    def allows(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme == "https" and parsed.netloc.lower() == self.host


def advertised_wrapped_hls(page: str, base: str) -> WrappedHls | None:
    """Recognize the player configuration, without executing any scripts."""
    host = urlparse(base).netloc.lower()
    if not TILE_EMBED_HOST.fullmatch(host):
        return None
    match = re.search(r"\bconst\s+EMBED\s*=\s*(?=\{)", page)
    if not match:
        return None
    data, _ = json.JSONDecoder().raw_decode(page, match.end())
    if not isinstance(data, dict):
        return None
    source, offset = data.get("streamUrl"), data.get("hostLengthOffset")
    if not isinstance(source, str) or type(offset) is not int or not 0 <= offset <= 64:
        return None
    url = http_url(base, source)
    stream = WrappedHls(url, host, 32 + offset)
    return stream if stream.allows(url) and urlparse(url).path == "/conv/conv.php" else None


def tile_source(base: str, page: str, headers: dict[str, str]) -> str | WrappedHls:
    markup = ProviderMarkup(page)
    if source := advertised_media(page, markup, base):
        return source

    # Older and current players can wrap a literal source in base64.
    packed = re.search(r"""\b(?:var|let|const)\s+zdec\s*=\s*(['"])([A-Za-z0-9+/=]+)\1""", page)
    if not packed:
        return ""
    try:
        decoded = base64.b64decode(packed[2], validate=True).decode("utf-8")
        player = re.search(
            r"""\bdrawIFR2?\s*\(\s*(?:(['"])([^'"]+)\1|atob\(\s*(['"])([^'"]+)\3\s*\))""",
            decoded,
        )
        if player:
            value = player[2] or base64.b64decode(player[4], validate=True).decode("utf-8")
            target = http_url(base, value)
            parsed = urlparse(target)
            if target and parsed.path.lower().endswith((".m3u8", ".ts")):
                return target
            if (parsed.hostname or "").endswith(".hopslan.com") and parsed.path.endswith("/embed.html"):
                # This Flussonic player serves HLS in the same stream directory.
                return parsed._replace(path=parsed.path.removesuffix("embed.html") + "index.m3u8", fragment="").geturl()
            if target and parsed.path == "/embed.php":
                # Inspect only the known provider's HTTPS player page, once.
                # It may publish a regular HTML media source without scripts.
                if parsed.scheme == "https" and TILE_EMBED_HOST.fullmatch(parsed.netloc.lower()):
                    frame, content = fetch_page(target, {**headers, "Referer": urljoin(base, "/")})
                    if source := advertised_media(content, ProviderMarkup(content), frame):
                        return source
                    if wrapped := advertised_wrapped_hls(content, frame):
                        return wrapped
                raise BrowserPlayerOnly
            if target:
                # Several legitimate HLS endpoints end in playlist.php. Playback
                # validates their contents instead of relying on the extension.
                return target
    except (binascii.Error, UnicodeError):
        pass
    return ""


def tile_candidates(player_url: str, headers: dict[str, str]):
    pending = [player_url]
    visited = set()
    emitted = set()
    original_id = parse_qs(urlparse(player_url).query).get("id", [""])[0]
    while pending and len(visited) < 2:
        url = pending.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            base, page = fetch_page(url, headers)
            # Follow only the replacement-player button actually advertised by
            # this channel; never invent mirror IDs or try a different channel.
            for match in re.finditer(r"""\bmirr_url\s*=\s*(['"])([^'"]+)\1""", page):
                mirror = http_url(base, match[2])
                parsed = urlparse(mirror)
                mirror_id = parse_qs(parsed.query).get("id", [""])[0]
                if (parsed.scheme == "https" and parsed.netloc == urlparse(base).netloc
                        and parsed.path == urlparse(base).path
                        and mirror_id == original_id.removesuffix("_mirror") + "_mirror"):
                    pending.append(mirror)
            source = tile_source(base, page, headers)
            if source and source not in emitted:
                emitted.add(source)
                yield source
        except UPSTREAM_ERRORS as error:
            # An unusable primary frame must not suppress its advertised mirror.
            TunerHandler.log_source_error("player", error)


def livedetv_candidates(channel_url: str, headers: dict[str, str]):
    opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
    pending = [(channel_url, headers["Referer"])]
    channel_path = urlparse(channel_url).path.removeprefix("/embed/").strip("/")
    seen = set()
    sources = set()
    # Follow actual same-site player frames, including the direct /yayin/ frame
    # on channel pages. The site's /embed/ wrapper is just another entry point.
    while pending and len(seen) < 6:
        url, referer = pending.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            base, page = fetch_page(url, {**headers, "Referer": referer}, opener.open)
        except UPSTREAM_ERRORS:
            continue
        if provider_for_url(base) != "livedetv":
            continue
        markup = ProviderMarkup(page)
        for source in advertised_media_urls(page, markup, base, hls_literals=urlparse(base).path == "/token.php"):
            if source not in sources:
                sources.add(source)
                yield source
        frames = []
        urls = list(markup.frames)
        if url == channel_url:
            # Some pages expose their alternate embed only as copyable HTML.
            for snippet in re.findall(r"<textarea\b[^>]*>(.*?)</textarea\s*>", page, re.I | re.S):
                urls.extend(ProviderMarkup(html.unescape(snippet)).frames)
        for frame in urls:
            target = http_url(base, frame)
            if provider_for_url(target) != "livedetv":
                continue
            path = urlparse(target).path
            if path.startswith("/embed/") and path.removeprefix("/embed/").strip("/") != channel_path:
                continue
            if path.startswith(("/embed/", "/yayin/")) or path == "/token.php":
                frames.append((target, base))
        pending[0:0] = frames


def resolve_candidates(provider: str, url: str, headers: dict[str, str], *, custom=False):
    # M3U entries can be direct streams or supported public player pages.
    if custom and urlparse(url).path.lower().endswith((".m3u8", ".ts")):
        yield url
    elif provider in TILE_PROVIDERS:
        if custom:
            slug = urlparse(url).path.strip("/")
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", slug):
                return
            base, page = fetch_page(PROVIDER_HOME[provider], headers)
            url = tile_player(page, base, slug)
        if url:
            yield from tile_candidates(url, headers)
    elif provider == "livedetv":
        yield from livedetv_candidates(url, headers)
    elif provider == "2ix2":
        base, page = fetch_page(url, headers)
        yield from advertised_media_urls(page, ProviderMarkup(page), base)
    else:
        yield url


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
        try:
            scraped = list(provider_catalog(provider).values())
        except UPSTREAM_ERRORS as error:
            print(f"WARNING: Channel discovery for '{provider}' failed ({type(error).__name__}).", flush=True)
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


MAX_MANIFEST_BYTES = 1024 * 1024
MAX_WRAPPED_SEGMENT_BYTES = 16 * 1024 * 1024
TS_PACKET = 188


def is_mpeg_ts(prefix: bytes) -> bool:
    return len(prefix) >= 2 * TS_PACKET and prefix[0] == prefix[TS_PACKET] == 0x47


def ts_has_audio(data: bytes) -> bool:
    """Require an audio PES packet, not just an audio declaration in the PMT."""
    buffers: dict[int, bytes] = {}
    pmt_pids = set()
    audio_pids = set()
    for start in range(0, len(data), TS_PACKET):
        packet = data[start:start + TS_PACKET]
        if len(packet) != TS_PACKET or packet[0] != 0x47 or packet[1] & 0x80:
            continue
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        if pid != 0 and pid not in pmt_pids and pid not in audio_pids:
            continue
        control = (packet[3] >> 4) & 3
        if control not in (1, 3):
            continue
        offset = 4 + (1 + packet[4] if control == 3 else 0)
        payload = packet[offset:]
        if not payload:
            continue
        if pid in audio_pids:
            if (len(payload) >= 4 and packet[1] & 0x40 and payload.startswith(b"\0\0\1")
                    and (0xC0 <= payload[3] <= 0xDF or payload[3] in (0xBD, 0xFD))):
                return True
            continue
        if packet[1] & 0x40:
            pointer = payload[0] + 1
            buffers[pid] = payload[pointer:]
        elif pid in buffers:
            buffers[pid] += payload
        section = buffers.get(pid, b"")
        if len(section) < 3:
            continue
        length = 3 + (((section[1] & 15) << 8) | section[2])
        if length < 8 or length > 1024 or len(section) < length:
            continue
        section = section[:length - 4]  # Ignore the section CRC.
        buffers.pop(pid, None)
        if pid == 0 and section[0] == 0:
            for pos in range(8, len(section) - 3, 4):
                if section[pos:pos + 2] != b"\0\0":
                    pmt_pids.add(((section[pos + 2] & 31) << 8) | section[pos + 3])
        elif pid in pmt_pids and section[0] == 2 and len(section) >= 12:
            pos = 12 + (((section[10] & 15) << 8) | section[11])
            while pos + 5 <= len(section):
                stream_type = section[pos]
                stream_pid = ((section[pos + 1] & 31) << 8) | section[pos + 2]
                info_length = ((section[pos + 3] & 15) << 8) | section[pos + 4]
                if stream_type in (0x03, 0x04, 0x0F, 0x11, 0x81, 0x87):
                    audio_pids.add(stream_pid)
                if stream_type == 0x06:
                    descriptors = section[pos + 5:pos + 5 + info_length]
                    index = 0
                    while index + 2 <= len(descriptors):
                        if descriptors[index] in (0x6A, 0x7A, 0x7B, 0x7C):
                            audio_pids.add(stream_pid)
                        index += 2 + descriptors[index + 1]
                pos += 5 + info_length
    return False


def read_stream_prefix(response) -> tuple[list[str], bytes]:
    """Recognize a manifest by its contents, never by its URL or MIME type."""
    prefix = response.read(512)
    first_line = prefix.split(b"\n", 1)[0].strip().removeprefix(b"\xef\xbb\xbf")
    if first_line != b"#EXTM3U":
        return [], prefix
    body = prefix + response.read(MAX_MANIFEST_BYTES - len(prefix) + 1)
    if len(body) > MAX_MANIFEST_BYTES:
        raise ValueError("HLS manifest exceeds size limit")
    return [line.strip() for line in body.decode("utf-8-sig").splitlines() if line.strip()], prefix


def hls_entries(lines: list[str], tag: str):
    pending = None
    for line in lines:
        if line.startswith(tag):
            pending = line
        elif pending and line and not line.startswith("#"):
            yield pending, line
            pending = None


def variant_quality(tag: str) -> tuple[int, int]:
    resolution = re.search(r"(?:[:,])RESOLUTION=(\d+)x(\d+)", tag)
    bandwidth = re.search(r"(?:[:,])BANDWIDTH=(\d+)", tag)
    pixels = int(resolution[1]) * int(resolution[2]) if resolution else 0
    return pixels, int(bandwidth[1]) if bandwidth else 0


def hls_input(
    lines: list[str], base: str, headers: dict[str, str], depth=0, deadline=None, *, wrapped_open=None,
) -> str:
    if any(http_url(base, uri) for _, uri in hls_entries(lines, "#EXTINF:")):
        return base
    if depth >= 2:
        raise ValueError("no playable media playlist")
    if deadline is None:
        deadline = time.monotonic() + 10
    variants = sorted(hls_entries(lines, "#EXT-X-STREAM-INF:"), key=lambda e: variant_quality(e[0]), reverse=True)
    for tag, uri in variants[:6]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        target = http_url(base, uri)
        if not target or target == base:
            continue
        audio_group = re.search(r'(?:[:,])AUDIO="([^"]+)"', tag)
        external_audio = audio_group and any(
            line.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in line
            and f'GROUP-ID="{audio_group[1]}"' in line and 'URI="' in line for line in lines
        )
        if external_audio and (wrapped_open or not FFMPEG_FOUND):
            continue
        try:
            with (wrapped_open or urlopen)(Request(target, headers=headers), timeout=min(5, remaining)) as response:
                child_lines, _ = read_stream_prefix(response)
                resolved = hls_input(child_lines, response.url, headers, depth + 1, deadline, wrapped_open=wrapped_open)
            # Keep the master when FFmpeg needs it to load the separate audio.
            return base if external_audio else resolved
        except UPSTREAM_ERRORS:
            continue
    raise ValueError("no playable media playlist")


def wrapped_opener(source: WrappedHls):
    class SameOriginRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not source.allows(newurl):
                raise ValueError("cross-origin redirect in wrapped HLS")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = build_opener(SameOriginRedirect())

    def open_url(request, *, timeout):
        if not source.allows(request.full_url):
            raise ValueError("cross-origin URL in wrapped HLS")
        return opener.open(request, timeout=timeout)

    return open_url


def read_wrapped_segment(response, offset: int) -> bytes:
    """Read the payload using the public player's format, then validate every TS packet."""
    path = urlparse(response.url).path.lower()
    if not path.endswith((".tsl.bmp", ".tsl.csv", ".tsl.woff", ".ts")):
        return b""
    payload = response.read(MAX_WRAPPED_SEGMENT_BYTES + 1)
    if len(payload) > MAX_WRAPPED_SEGMENT_BYTES:
        return b""
    try:
        if path.endswith(".ts"):
            data = payload
        elif path.endswith(".tsl.bmp") and payload.startswith(b"BM"):
            data = binascii.unhexlify(payload[offset:])
        elif path.endswith(".tsl.csv"):
            fields = re.split(rb",|\r?\n", payload[offset - 24:])
            data = base64.b64decode(b"".join(reversed(fields)), validate=True)
        elif path.endswith(".tsl.woff"):
            size = max(0, len(payload) - 13) * 4
            if size > MAX_WRAPPED_SEGMENT_BYTES:
                return b""
            data = bytearray(size)
            data[3::4] = payload[13:]
        else:
            return b""
    except binascii.Error:
        return b""
    if (
        not is_mpeg_ts(data)
        or len(data) % TS_PACKET
        or any(data[pos] != 0x47 for pos in range(0, len(data), TS_PACKET))
    ):
        return b""
    return data


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
                if index < 0:
                    raise IndexError
                channel = parse_playlist()[index]
            except (ValueError, IndexError):
                self.send_error(HTTPStatus.NOT_FOUND, "Unknown channel")
                return
            self.tune(channel)
        elif path == "/proxy":
            target = unquote(parsed.query.removeprefix("url="))
            if not target.startswith(("http://", "https://")):
                self.send_error(HTTPStatus.BAD_REQUEST, "Only HTTP(S) stream URLs are supported")
                return
            # Default headers for generic proxy requests
            self.proxy(target, base, {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "Accept": "*/*"})
        elif path in ("/", "/health"):
            self.send_json({"status": "ok", "channels": len(parse_playlist())})
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def tune(self, channel: Channel) -> None:
        custom = not channel.provider_urls
        self.require_audio = not custom
        provider_urls = {} if custom else channel_provider_urls(channel)
        candidates = (
            [(provider_for_url(channel.url), channel.url)] if custom else
            [(provider, provider_urls[provider])
             for provider in SCRAPER_PROVIDERS if provider in provider_urls]
        )
        if override := CHANNEL_SOURCE_OVERRIDES.get(channel.tvg_id.strip().casefold()):
            candidates.insert(0, ("ChannelOverride", override))
        for provider, url in candidates:
            label = provider or "CustomM3U"
            self.tune_source = (channel.name, label)
            print(f"INFO: Trying '{channel.name}' via '{label}'.", flush=True)
            headers = get_provider_headers(provider)
            stalled_endpoints: set[tuple[str, str, str]] = set()
            try:
                sources = (url,) if provider == "ChannelOverride" else resolve_candidates(
                    provider, url, headers, custom=custom,
                )
                for target in islice(sources, 4):
                    if self.try_source(target, headers, label, stalled_endpoints):
                        return
            except BrowserPlayerOnly:
                print(f"WARNING: Source '{label}' advertises a browser player without a direct stream URL.", flush=True)
            except UPSTREAM_ERRORS as error:
                self.log_source_error(label, error)
            print(f"INFO: No playable stream from '{label}'; trying the next source.", flush=True)
        self.send_error(HTTPStatus.BAD_GATEWAY, "No configured source could start this channel")

    @staticmethod
    def log_source_error(label: str, error: Exception) -> None:
        if isinstance(error, BrowserPlayerOnly):
            print(f"WARNING: Source '{label}' has no directly readable player source.", flush=True)
            return
        if isinstance(error, HTTPError):
            if error.headers and error.headers.get("CF-Mitigated", "").lower() == "challenge":
                print(f"WARNING: Source '{label}' requires a browser challenge (HTTP {error.code}).", flush=True)
                return
            reason = f"HTTP {error.code}"
        else:
            reason = type(error).__name__
        print(f"WARNING: Source '{label}' failed ({reason}).", flush=True)

    def try_source(
        self, target: str | WrappedHls, headers: dict[str, str], label: str,
        stalled_endpoints: set[tuple[str, str, str]],
    ) -> bool:
        endpoint = None
        try:
            parsed = urlparse(target.url if isinstance(target, WrappedHls) else target)
            endpoint = parsed.scheme, parsed.netloc, parsed.path
            if endpoint in stalled_endpoints:
                return False
            if isinstance(target, WrappedHls):
                return self.stream_wrapped_hls(target)
            return bool(http_url("", target)) and self.stream_candidate(target, headers)
        except UPSTREAM_ERRORS as error:
            # Different session tokens can point to the same unresponsive server path.
            cause = error.reason if isinstance(error, URLError) else error
            if endpoint and isinstance(cause, TimeoutError):
                stalled_endpoints.add(endpoint)
            self.log_source_error(label, error)
            return False

    def begin_ts_response(self) -> None:
        channel, provider = self.tune_source
        print(f"INFO: Streaming '{channel}' via '{provider}'.", flush=True)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def stream_candidate(self, url: str, headers: dict[str, str]) -> bool:
        with urlopen(Request(url, headers=headers), timeout=6) as response:
            lines, prefix = read_stream_prefix(response)
            if lines:
                playlist = hls_input(lines, response.url, headers)
            elif is_mpeg_ts(prefix):
                try:
                    self.begin_ts_response()
                    self.wfile.write(prefix)
                    while chunk := response.read(64 * 1024):
                        self.wfile.write(chunk)
                except UPSTREAM_ERRORS:
                    pass  # The response has already started; never send another status.
                return True
            else:
                return False
        return self.stream_hls_as_ts(playlist, headers)

    def stream_wrapped_hls(self, source: WrappedHls) -> bool:
        # The player advertises a public manifest; send only validated TS payloads.
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        open_url = wrapped_opener(source)
        with open_url(Request(source.url, headers=headers), timeout=6) as response:
            lines, _ = read_stream_prefix(response)
            playlist = hls_input(lines, response.url, headers, wrapped_open=open_url)
        return self.stream_ts_segments(playlist, headers, wrapped=source, open_url=open_url)

    def stream_hls_as_ts(self, m3u8_url: str, stream_headers: dict[str, str]) -> bool:
        """Stream HLS playlist segments as a continuous MPEG-TS stream to the client."""
        if FFMPEG_FOUND:
            # Use the same request context for playback as for validation.
            headers_str = "".join(f"{k}: {v}\r\n" for k, v in stream_headers.items())
            
            cmd = [
                "ffmpeg",
                "-nostdin",
                "-rw_timeout", "5000000",
                "-headers", headers_str,
                "-f", "hls",
                "-i", m3u8_url
            ]
            if getattr(self, "require_audio", False):
                # A video-only variant must not mask a working TV source with sound.
                cmd.extend(["-map", "0:v:0", "-map", "0:a:0"])
            
            if TRANSCODE_UPSCALING:
                # Parse target resolution dimensions
                width, height = 1920, 1080
                if "x" in TRANSCODE_RESOLUTION:
                    try:
                        w_str, h_str = TRANSCODE_RESOLUTION.lower().split("x", 1)
                        width, height = int(w_str), int(h_str)
                    except ValueError:
                        pass
                
                # Build filters: denoise -> scale -> sharpen
                vf_filters = []
                if TRANSCODE_DENOISE > 0.0:
                    luma_sp = TRANSCODE_DENOISE * 1.5
                    chroma_sp = TRANSCODE_DENOISE * 1.5
                    luma_tmp = TRANSCODE_DENOISE * 3.0
                    chroma_tmp = TRANSCODE_DENOISE * 3.0
                    vf_filters.append(f"hqdn3d={luma_sp}:{chroma_sp}:{luma_tmp}:{chroma_tmp}")
                
                vf_filters.append(f"scale={width}:{height}:flags=lanczos")
                
                if TRANSCODE_SHARPEN > 0.0:
                    vf_filters.append(f"unsharp=5:5:{TRANSCODE_SHARPEN}:5:5:0.0")
                
                cmd.extend([
                    "-vf", ",".join(vf_filters),
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-tune", "zerolatency"
                ])
            else:
                # Copy video stream losslessly (zero CPU usage)
                cmd.extend([
                    "-c:v", "copy"
                ])
                
            # Transcode audio to standard AAC stereo to ensure 100% Plex/client audio compatibility
            cmd.extend([
                "-c:a", "aac",
                "-ac", "2",
                "-f", "mpegts",
                "pipe:1"
            ])
            
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError as error:
                print(f"WARNING: FFmpeg launch failed ({type(error).__name__}).", flush=True)
                return False

            chunks = Queue(maxsize=4)
            stopped = Event()

            def read_output() -> None:
                while not stopped.is_set():
                    try:
                        chunk = proc.stdout.read1(64 * 1024)
                    except OSError:
                        chunk = b""
                    while not stopped.is_set():
                        try:
                            chunks.put(chunk, timeout=0.25)
                            break
                        except Full:
                            continue
                    if not chunk:
                        return

            reader = Thread(target=read_output, daemon=True)
            reader.start()
            try:
                try:
                    chunk = chunks.get(timeout=12)
                except Empty:
                    print("WARNING: FFmpeg timed out before producing stream data.", flush=True)
                    return False
                if not chunk:
                    print("WARNING: FFmpeg exited before producing stream data.", flush=True)
                    return False
                try:
                    self.begin_ts_response()
                    while chunk:
                        self.wfile.write(chunk)
                        try:
                            chunk = chunks.get(timeout=20)
                        except Empty:
                            print("WARNING: FFmpeg stopped producing stream data; closing the client stream.", flush=True)
                            break
                except OSError:
                    pass
                return True
            finally:
                stopped.set()
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                proc.stdout.close()
                reader.join(timeout=1)

        return self.stream_ts_segments(m3u8_url, stream_headers)

    def stream_ts_segments(
        self, m3u8_url: str, stream_headers: dict[str, str], *, wrapped: WrappedHls | None = None, open_url=None,
    ) -> bool:
        # This path sends only unencrypted, already-multiplexed TS.
        open_url = open_url or urlopen
        sent_segments: set[str] = set()
        started = False
        last_progress = time.monotonic()
        while True:
            try:
                with open_url(Request(m3u8_url, headers=stream_headers), timeout=6) as response:
                    lines, _ = read_stream_prefix(response)
                    base = response.url
                if any(line.startswith(("#EXT-X-MAP:", "#EXT-X-BYTERANGE:")) or (
                    line.startswith("#EXT-X-KEY:") and line != "#EXT-X-KEY:METHOD=NONE"
                ) for line in lines):
                    return started
                segments = [url for _, uri in hls_entries(lines, "#EXTINF:") if (url := http_url(base, uri))]
                if not segments:
                    return started
                if wrapped and not sent_segments and not started and "#EXT-X-ENDLIST" not in lines:
                    # Start near the live edge rather than buffering a full window.
                    sent_segments.update(segments[:-3])
                for segment in segments:
                    if segment in sent_segments:
                        continue
                    try:
                        with open_url(Request(segment, headers=stream_headers), timeout=6) as response:
                            if wrapped:
                                data = read_wrapped_segment(response, wrapped.segment_offset)
                            else:
                                prefix = response.read(2 * TS_PACKET)
                                data = prefix if is_mpeg_ts(prefix) else b""
                            if not data:
                                continue
                            if wrapped and not started and not ts_has_audio(data):
                                print("WARNING: Wrapped TV source has no audio track; trying another source.", flush=True)
                                return False
                            if not started:
                                started = True
                                self.begin_ts_response()
                            self.wfile.write(data)
                            if not wrapped:
                                while chunk := response.read(64 * 1024):
                                    self.wfile.write(chunk)
                        sent_segments.add(segment)
                        last_progress = time.monotonic()
                    except (BrokenPipeError, ConnectionResetError):
                        return started
                    except UPSTREAM_ERRORS:
                        if started:
                            return True
                if not started or "#EXT-X-ENDLIST" in lines:
                    return started
                sent_segments.intersection_update(segments)
                duration = next((line.split(":", 1)[1] for line in lines
                                 if line.startswith("#EXT-X-TARGETDURATION:")), "6")
                duration = float(duration)
                if time.monotonic() - last_progress > max(15, duration * 3):
                    return started
                time.sleep(min(15, max(1, duration / 2)))
            except UPSTREAM_ERRORS:
                return started

    def proxy(self, target: str, base: str, stream_headers: dict[str, str]) -> None:
        request = Request(target, headers=stream_headers)
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
                            self.proxy(video_url, base, stream_headers)
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


if __name__ == "__main__":
    # Check if ffmpeg is available on startup to help users debug container updates
    if FFMPEG_FOUND:
        print("INFO: 'ffmpeg' found on system path. ScrapeTuner is ready for real-time video upscaling and audio transcoding.", flush=True)
    else:
        if TRANSCODE_UPSCALING:
            print("WARNING: TRANSCODE_UPSCALING is set to True, but 'ffmpeg' was NOT found on the system path! Upscaling will fail.", flush=True)
        else:
            print("INFO: 'ffmpeg' not found. ScrapeTuner will run in native pass-through mode.", flush=True)

    ThreadingHTTPServer(("0.0.0.0", PORT), TunerHandler).serve_forever()
