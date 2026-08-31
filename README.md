# sonos-castbridge

A small self-hosted relay server that sits on your LAN and fixes Sonos
discovery/casting on networks where cheap routers mess up multicast (mDNS/
IGMP) between WiFi bands — and, as a side effect, gives **Android** a way to
cast music to Sonos (YouTube / YouTube Music) that's functionally equivalent
to what AirPlay already gives **iPhone** for free.

Fix two related home-network problems with Sonos speakers:

1. **AirPlay / the Sonos app can't find the speaker at all**, on networks where
   the router/AP has a single SSID for 2.4GHz and 5GHz but fails to flood
   multicast traffic (mDNS/SSDP) between its own two radios.
2. **Android has no way to cast to Sonos** from apps that don't have a native
   Sonos integration — AirPlay doesn't exist on Android, and Google Cast
   (Chromecast) isn't supported by Sonos hardware. YouTube and YouTube Music
   are the main practical case.

Both fixes run as small, always-on services on a home server (this was built
and tested on Proxmox LXC containers, but any always-on Linux box on the LAN
works).

## Requirements

**mDNS/SSDP relay:**
- Python 3 + `netifaces`
- **Two distinct network interfaces on the same LAN/subnet** — not optional,
  see below for why
- Negligible CPU/RAM (512MB / 1 core is plenty; it's just forwarding packets)

**YouTube/YouTube Music → Sonos bridge:**
- Docker + Docker Compose v2
- One network interface, on the same LAN as the Sonos speaker (see below for
  why it must NOT share a host with the relay above)
- **At least 2 CPU cores, 3 recommended.** Measured live with `docker stats`
  during an actual track change: yt-dlp + deno spike to 100% CPU for several
  seconds while deciphering YouTube's signature and downloading the audio.
  On a single core this is exactly what causes playback to stutter/delay at
  every track change — it's not a bug, it's the container running out of
  CPU. 1 core "works" but noticeably lags.
- 1GB RAM (peaked around 360MB during testing; 512MB is cutting it close
  once Node + yt-dlp + deno are all resident at once)

These are already set in [`docker-compose.yml`](docker-compose.yml)
(`cpus: "2.0"`, `mem_limit: 1024m`) and in
[`setup-proxmox-lxc.sh`](setup-proxmox-lxc.sh).

## Part 1 — mDNS/SSDP relay

**Symptom:** same WiFi network, same SSID, same subnet — but a phone on one
band can't see a Sonos speaker connected to the other band. This isn't a VLAN
problem; it's a bug in cheap/consumer AP firmware that fails to bridge
multicast traffic *between its own radios*, even though it happily forwards
multicast between its wired LAN port and either radio.

**Fix:** a wired device that listens for mDNS (`224.0.0.251:5353`), SSDP
(`239.255.255.250:1900`), and Sonos discovery broadcasts, and re-transmits
whatever it hears back onto the LAN. Because the retransmission arrives via
the switch port rather than a WiFi radio hop, the AP forwards it to *both*
bands correctly.

Uses [alsmith/multicast-relay](https://github.com/alsmith/multicast-relay).

### Why it needs two network interfaces

`multicast-relay` refuses to re-transmit a packet back out an interface whose
sender is already on that interface's subnet — normal behavior, meant to stop
you from creating broadcast storms on a single flat network. Since our
"upstream" and "downstream" are the exact same LAN, the tool needs two
*distinct* interfaces (even on the same subnet) plus `--oneInterface` to get
past that check. See [`setup-proxmox-lxc.sh`](setup-proxmox-lxc.sh) and
[`systemd/multicast-relay.service`](systemd/multicast-relay.service).

## Part 2 — YouTube / YouTube Music → Sonos cast bridge

Sonos speaks UPnP/DLNA natively. This container makes itself discoverable via
DIAL (the protocol the YouTube app's "Link with TV" / Cast button also uses),
receives the video ID YouTube/YouTube Music wants to play, pulls the audio
with `yt-dlp`, and hands Sonos a direct URL to play via UPnP.

Built on [TetrisBlack/yt-sonos-bridge](https://github.com/TetrisBlack/yt-sonos-bridge).
This repo just adds two things the upstream image is missing out of the box:

- **A JS runtime for yt-dlp.** YouTube requires executing JS to decipher its
  signature scheme; without a runtime, extraction fails with
  `Failed to extract signature decipher algorithm` and HTTP 403. The
  [`Dockerfile`](Dockerfile) installs `deno` on top of the base image.
- **Automatic yt-dlp updates.** YouTube changes its anti-bot measures often
  enough that a pinned yt-dlp binary goes stale within weeks and starts
  failing with the same 403. [`entrypoint.sh`](entrypoint.sh) runs
  `yt-dlp -U` on every container start.

### Run it on its own network interface — not sharing one with the relay

If you run this bridge inside the *same* multi-homed container as the relay
in Part 1, it advertises itself via DIAL/SSDP once per interface IP. Regular
YouTube tolerates having two apparent "locations" for the same device and
just picks one. **YouTube Music does not** — its cast handshake gets confused
by the ambiguity and hangs on "loading" forever, with nothing useful logged on
either side. Running the bridge in its own single-NIC container/host fixes it
immediately. This took a raw packet capture on the DIAL port to actually spot
(the sender's connection attempt never even reached application-level logs).

### Multiple rooms / multiple speakers

The relay from Part 1 needs no changes — it's protocol-level and doesn't
care how many Sonos zones exist on the LAN, one instance covers the whole
house.

The bridge is different: it's hardcoded to one `SONOS_DEVICE_IP` and shows
up as a single named entry in the Cast picker. For per-room casting, run
**one bridge instance per room**, each with its own Sonos target IP and
its own DIAL port (hardcoded upstream at `8099`, exposed here as a Docker
build arg so each instance can get a distinct one). The audio-serving port
doesn't need a separate patch — it already derives from `SERVER_ENDPOINT`.

See [`docker-compose.multi-room.yml.example`](docker-compose.multi-room.yml.example).
This is intentionally still one-container-per-service on a single host —
it does **not** need the single-NIC-per-instance treatment from the section
above, because that bug was about one device ambiguously advertising two
different locations for the *same* identity. Multiple rooms are genuinely
different DIAL devices with different names, which senders handle fine.

## Part 3 — sonos-watcher (pick a speaker, survive DHCP)

Setting `SONOS_DEVICE_IP` by hand means two problems in practice: finding
the IP in the first place (this project's own history involved tcpdump),
and the bridge silently going stale if DHCP ever hands that speaker a
different address.

`sonos-watcher` is a small sidecar (`./sonos-watcher`, plain Python stdlib,
no dependencies) that fixes both:

- **A web UI** (port `8088`) to discover Sonos speakers and pick one, instead
  of hunting for an IP by hand.
- **A background loop** that re-resolves the *chosen* speaker's current IP
  by MAC address every `CHECK_INTERVAL_SECONDS` (default 60s), and restarts
  `yt-sonos-bridge` via the Docker socket if DHCP has changed it.

### Discovery is zone-topology-based, not SSDP

The obvious way to discover Sonos speakers is an SSDP `M-SEARCH` for
`urn:schemas-upnp-org:device:ZonePlayer:1`. It doesn't work reliably against
real Sonos hardware — confirmed here by testing directly against a speaker,
from multiple hosts, with multiple `ST` values, over many attempts: no
replies, ever. What Sonos speakers *do* is periodically broadcast their own
unprompted `NOTIFY` announcements, which is what most control apps actually
key off (SSDP is still sent as a courtesy, since some other UPnP devices
answer it fine).

Rather than depend on catching one of those announcements within some
timeout window, `sonos-watcher` uses the same mechanism the real Sonos app
uses: it asks one *already-known* speaker's `ZoneGroupTopology` service
("who else is in your household?") via a direct SOAP call. This needs a
bootstrap IP (`SEED_IP`, reused from `SONOS_DEVICE_IP` in `.env`) the first
time; after a speaker is selected once, its own last-known IP is used as the
seed for future lookups, so this keeps working even if you started from a
speaker that later got renamed or dropped from the group. SSDP is kept as a
fallback for a from-scratch setup with literally no Sonos IP known yet.

### A gotcha this surfaced: Docker's own network vs. the real LAN

Docker Compose creates a default bridge network for the project even when
every service uses `network_mode: host`, which leaves the host multi-homed
(the real LAN NIC *and* Docker's internal bridge, e.g. `172.18.0.1`).
Joining a multicast group or sending from it without pinning the interface
lets the kernel pick either one — and it doesn't always pick the one that
actually reaches the LAN. `HOST_IP` (also reused from `SERVER_ENDPOINT` in
`.env`) exists specifically to force `sonos-watcher`'s sockets onto the real
NIC. If you fork this further and add more multicast/broadcast code, keep
this in mind — it's an easy thing to lose an afternoon to.

## Setup

```bash
git clone <this-repo>
cd sonos-castbridge
cp .env.example .env
$EDITOR .env   # set SONOS_DEVICE_IP, SERVER_ENDPOINT and HOST_IP
docker compose up -d --build
```

This brings up all three pieces from Part 2 and 3: `yt-sonos-bridge`,
`autoheal` (restarts it if its health check fails), and `sonos-watcher`
(discovery UI + DHCP-change tracking). `SONOS_DEVICE_IP` only needs to be
roughly right at first boot — visit `http://<HOST_IP>:8088`, pick your
speaker from the list, and `sonos-watcher` takes over from there.

`SONOS_DEVICE_IP` is the IP of the target speaker (or group coordinator).
`SERVER_ENDPOINT` and `HOST_IP` must both be this host's own LAN IP — Sonos
fetches audio from it directly, and sonos-watcher needs it to avoid binding
to Docker's own internal network by mistake (see Part 3).

For the mDNS/SSDP relay, see [`setup-proxmox-lxc.sh`](setup-proxmox-lxc.sh)
and drop [`systemd/multicast-relay.service`](systemd/multicast-relay.service)
into `/etc/systemd/system/` on whatever box runs it.

## Troubleshooting

**Cast target shows up but playback fails / 403 in logs:**
`docker exec yt-sonos-bridge /app/yt-dlp -U` — the entrypoint already does
this on every restart, but you can force it without restarting.

**YouTube Music shows the device but "loading" never finishes:**
Confirm the bridge is on a single network interface (see above). Capture
traffic on the DIAL port (`tcpdump -i <iface> port 8099`) while casting —
if you see the phone's request land with a `User-Agent` containing
`youtube.music`, but with a `Location` pointing at two different IPs across
separate SSDP announcements, that's this exact bug.

**AirPlay/Sonos app still can't find the speaker after setting up the relay:**
Verify the relay is actually running (`systemctl status multicast-relay`)
and that its two interfaces are genuinely distinct NICs on the same subnet,
not the same interface passed twice (that fails to even start, with
"Address already in use" trying to join the multicast group twice on the
same device).

## Credits

- [alsmith/multicast-relay](https://github.com/alsmith/multicast-relay)
- [TetrisBlack/yt-sonos-bridge](https://github.com/TetrisBlack/yt-sonos-bridge)
- [patrickkfkan/yt-cast-receiver](https://github.com/patrickkfkan/yt-cast-receiver) (used by yt-sonos-bridge)

## License

[PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0) — see [LICENSE](LICENSE). Free for personal/noncommercial use; commercial use requires permission.
