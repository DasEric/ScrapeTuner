# ScrapeTuner: Dynamic Live TV Proxy for Plex & Jellyfin

**ScrapeTuner** is a lightweight IPTV facade and scraper proxy that emulates a SiliconDust HDHomeRun network tuner. It dynamically scrapes free live TV streams from sources like `2ix2.com` on-the-fly, remuxes them into a continuous MPEG-TS stream, and maps them to standard German EPG (Electronic Program Guide) channels.

*Note: ScrapeTuner is designed to easily expand. Support for more free live TV streaming platforms and websites will be added in future updates!*

---

## Key Features

- **Dynamic Page Scraping:** Automatically extracts raw HLS stream manifests (`.m3u8`) from web player pages on-the-fly when a channel is tuned. This resolves issues with expiring tokens or IP-bound URLs.
- **Continuous HLS-to-MPEG-TS Streaming:** Multiplexes HLS segments on-the-fly into a continuous raw MPEG-TS (`video/mp2t`) stream. This is required for out-of-the-box compatibility with the Plex DVR playback engine (which does not natively support HLS playlists).
- **Auto-Discovery Mode:** If no custom playlist is provided, the tuner automatically crawls and serves all available channels (50+ channels).
- **EPG & XMLTV Aligned:** Standardized channel names and channel IDs matching Plex's built-in Gracenote database and public German XMLTV providers (e.g. `RTL.de`, `ProSieben.de`, `KabelEins.de`, `DasErste.de`).
- **Clean Logging:** Suppresses socket broken pipe errors (which occur when a client pauses or switches channels) and prints clean, English warning logs if a stream is currently offline at the source website.

---

## Quick Start (Docker Compose)

The easiest way to run ScrapeTuner is using Docker Compose. 

The pre-built Docker image is available on Docker Hub: [DasEric/ScrapeTuner](https://hub.docker.com/r/DasEric/ScrapeTuner).

1. Create a `compose.yaml` file:
   ```yaml
   services:
     scrapetuner:
       image: DasEric/ScrapeTuner:latest
       container_name: scrapetuner
       ports:
         - "5004:5004"
       volumes:
         - ./config:/config
       environment:
         - PORT=5004
         - CONFIG_DIR=/config
         - PUBLIC_BASE_URL=http://<YOUR-HOST-IP>:5004
       restart: unless-stopped
   ```
2. Start the container:
   ```bash
   docker compose up -d
   ```
   *(Note: You can still build it locally using `build: .` in your compose file and running `docker compose up -d --build` if you prefer).*
3. Verify the endpoints are running:
   - Discovery: `http://<YOUR-HOST-IP>:5004/discover.json`
   - Tuner lineup: `http://<YOUR-HOST-IP>:5004/lineup.json`

---

## Setup in Plex

1. Navigate to **Settings > Live TV & DVR > Add Device**.
2. Plex may scan for local tuners. If it does not find it automatically, click **"Enter its network address manually"** and type:
   `http://<YOUR-HOST-IP>:5004`
3. Select your EPG source (e.g., Select country **Germany** and choice **HD+** or **Kathrein**).
4. Because the tuner names (like `ProSieben`, `Sat.1`, `RTLZWEI`, `kabel eins`, `VOX`) are mapped to the official broadcast names, Plex will **automatically associate** the channels with its built-in TiVo/Gracenote guide data.

---

## Setup in Jellyfin

1. Navigate to **Dashboard > Live TV**.
2. Click **"+"** next to **Tuner Devices** and select **M3U Tuner**.
3. Set the Tuner URL to:
   `http://<YOUR-HOST-IP>:5004/playlist.m3u`
4. Under **TV-Programm-Datenquellen** (EPG Sources), click **"+"**, select **XMLTV**, and use one of the daily updated German IPTV-Org guides:
   - HD+ Lineup: `https://iptv-org.github.io/epg/guides/de/hd-plus.de.xml`
   - MagentaTV Lineup: `https://iptv-org.github.io/epg/guides/de/magentatv.de.xml`
5. Since the channel EPG IDs (e.g., `RTL.de`, `ProSieben.de`) match the XMLTV file layout, Jellyfin will map the program schedule to your TV guide automatically.

---

## Custom Channel Playlists (Optional)

If you prefer to define a custom playlist instead of scraping all channels, you can place a `channels.m3u` file inside the `./config` directory. 

You can mix direct HLS URLs and `2ix2.com` web pages:
```m3u
#EXTM3U
#EXTINF:-1 tvg-id="RTL.de" tvg-chno="10001" group-title="TV",RTL
https://www.2ix2.com/rtl-live/

#EXTINF:-1 tvg-id="ProSieben.de" tvg-chno="10007" group-title="TV",ProSieben
https://www.2ix2.com/pro-7/

#EXTINF:-1 tvg-id="custom-stream" tvg-chno="10056" group-title="TV",My Local Stream
https://stream.example.org/live/master.m3u8
```

---

## NAS Deployment & Troubleshooting (Crucial)

If you are running ScrapeTuner on a Linux-based NAS (like **Ugreen UGOS**, **Synology DSM**, or **QNAP QTS**) alongside Plex/Jellyfin in Docker, pay close attention to the following network requirements:

### 1. Host Network Mode Requirement
Plex and ScrapeTuner often run on the same machine. In default `bridge` network mode, Docker disables NAT loopback (hairpin NAT). This means Plex cannot connect back to the host's physical IP address to reach the tuner container, causing playback to fail with a *"Channel could not be tuned"* error.
- **Solution:** Always run ScrapeTuner in **Host Network Mode** (`network_mode: host` in Docker Compose, or Selecting `host` network in the NAS Docker GUI).

### 2. Recreating the Container
If you switch a container from `bridge` to `host` network mode (or vice versa), **you must delete and recreate the container**. Simply restarting or updating the container will *not* apply the network mode change in Docker. 
- **Docker Compose:** Run `docker compose down && docker compose up -d` to automatically recreate the container.
- **NAS GUI:** Stop the container, delete it, and recreate a new container with the network set to `host`.

### 3. Base URL Formatting
Make sure the `PUBLIC_BASE_URL` environment variable is fully formatted as:
`http://<YOUR-NAS-IP>:5004` (e.g. `http://10.0.0.6:5004`).
- Ensure it contains the `http://` prefix.
- Ensure it contains the port `:5004` at the end.
*(Note: ScrapeTuner contains built-in fallback checks that will automatically prepend `http://` if it is missing, but it is best practice to configure it correctly).*

---

## Network & Privacy Note

- **Local Network Use Only:** The proxy re-routes HLS video packets through the container to serve them as MPEG-TS. Never expose port `5004` to the public internet.
- **VPN / Proxy:** If your host network uses a VPN or blocks outgoing connections, some player endpoints or scraper fetches might time out. Ensure the container has unrestricted access to the web.
