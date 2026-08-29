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

## Setup

```bash
git clone <this-repo>
cd sonos-castbridge
cp .env.example .env
$EDITOR .env   # set SONOS_DEVICE_IP and SERVER_ENDPOINT
docker compose up -d --build
```

`SONOS_DEVICE_IP` is the IP of the target speaker (or group coordinator).
`SERVER_ENDPOINT` must be this host's own LAN IP — Sonos fetches audio from
it directly.

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

MIT — see [LICENSE](LICENSE).
