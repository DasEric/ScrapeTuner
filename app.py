"""Small HDHomeRun-compatible facade for user-authorized M3U live-TV sources."""

from __future__ import annotations

import html
import json
import mimetypes
import os
import re
import socket
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlparse
from urllib.request import Request, urlopen

PORT = int(os.environ.get("PORT", "5004"))
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
PLAYLIST = CONFIG_DIR / "channels.m3u"
GUIDE = CONFIG_DIR / "guide.xml"


@dataclass(frozen=True)
class Channel:
    number: str
    name: str
    url: str
    tvg_id: str
    logo: str = ""


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
    "rtl2": "RTLZwei.de",
    "vox": "Vox.de",
    "n-tv": "n-tv.de",
    "ntv": "n-tv.de",
    "rtl-nitro": "RTLNitro.de",
    "rtlnitro": "RTLNitro.de",
    "superrtl": "SuperRTL.de",
    "super-rtl": "SuperRTL.de",
    "pro7": "ProSieben.de",
    "pro-7": "ProSieben.de",
    "kabel1": "KabelEins.de",
    "kabel-eins": "KabelEins.de",
    "sat1": "Sat1.de",
    "sat-1": "Sat1.de",
    "sixx": "Sixx.de",
    "dmax": "DMAX.de",
    "sky-sport-news": "SkySportNews.de",
    "sport1": "Sport1.de",
    "prosieben-maxx": "ProSiebenMaxx.de",
    "kabeleins-doku": "KabelEinsDoku.de",
    "kabel-1-doku": "KabelEinsDoku.de",
    "tlc": "TLC.de",
    "sat1gold": "Sat1Gold.de",
    "sat-1-gold": "Sat1Gold.de",
    "tele5": "Tele5.de",
    "tele-5": "Tele5.de",
    "comedy-central": "ComedyCentralGermany.de",
    "welt": "Welt.de",
    "zdfneo": "ZDFneo.de",
    "zdf-neo": "ZDFneo.de",
    "disney-channel": "DisneyChannel.de",
    "ard": "DasErste.de",
    "das-erste": "DasErste.de",
    "zdf": "ZDF.de",
    "zdfinfo": "ZDFinfo.de",
    "zdf-info": "ZDFinfo.de",
    "kika": "KiKa.de",
    "3sat": "3sat.de",
    "arte": "Arte.de",
    "tagesschau24": "Tagesschau24.de",
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
    "serv-tv": "ServusTVDeutschland.de"
}

_2ix2_channels_cache: list[Channel] = []
_2ix2_last_scraped: float = 0.0


def get_2ix2_channels() -> list[Channel]:
    global _2ix2_channels_cache, _2ix2_last_scraped
    now = time.time()
    if _2ix2_channels_cache and (now - _2ix2_last_scraped < 43200):
        return _2ix2_channels_cache

    homepage_url = "https://www.2ix2.com/"
    req = Request(
        homepage_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
    )
    try:
        with urlopen(req, timeout=15) as response:
            homepage = response.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"Error scraping 2ix2 homepage: {e}", flush=True)
        if _2ix2_channels_cache:
            return _2ix2_channels_cache
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
        name = html.unescape(name).strip()
        name = EPG_NAME_MAP.get(name, name)
        if logo.startswith("//"):
            logo = "https:" + logo
        num = str(10000 + len(channels) + 1)
        channels.append(Channel(
            number=num,
            name=name,
            url=url,
            tvg_id=EPG_ID_MAP.get(slug, f"2ix2-{slug}"),
            logo=logo
        ))

    if channels:
        _2ix2_channels_cache = channels
        _2ix2_last_scraped = now
        print(f"Successfully scraped {len(channels)} channels from 2ix2.com", flush=True)
    return _2ix2_channels_cache or channels


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
    """Read M3U playlist if it exists, otherwise auto-discover channels from 2ix2.com."""
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
    else:
        return get_2ix2_channels()


def public_base(handler: BaseHTTPRequestHandler) -> str:
    configured = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if configured:
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
            stream_url = channel.url
            if "2ix2.com" in stream_url:
                resolved = resolve_2ix2_stream(stream_url)
                if not resolved:
                    print(f"ERROR: Stream for '{channel.name}' on 2ix2.com is currently not available!", flush=True)
                    self.send_error(HTTPStatus.BAD_GATEWAY, f"Stream for '{channel.name}' on 2ix2.com is currently not available")
                    return
                stream_url = resolved

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


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), TunerHandler).serve_forever()
