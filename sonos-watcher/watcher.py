#!/usr/bin/env python3
"""
Small sidecar for sonos-castbridge:

1. A web UI to discover Sonos speakers on the LAN via SSDP and pick one,
   instead of hunting for the IP by hand (tcpdump/curl, as this project's
   own README's history can attest to).
2. A background loop that re-resolves the *chosen* speaker's current IP by
   MAC address every INTERVAL seconds, and restarts yt-sonos-bridge via the
   Docker socket if that IP has changed since DHCP handed out a new one.

Deliberately kept as one plain-stdlib file (no Flask/requests) to avoid
pulling in a dependency chain for something this small.
"""
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE_FILE = "/data/sonos-target.json"
ENV_FILE = "/data/sonos-target.env"
SSDP_ST = "urn:schemas-upnp-org:device:ZonePlayer:1"
# Sonos speakers announce themselves unprompted every so often rather than
# reliably answering active queries (see ssdp_discover's docstring) — this
# needs to be long enough to actually catch one of those announcements.
DISCOVERY_TIMEOUT = 15
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "60"))
BRIDGE_CONTAINER_NAME = os.environ.get("BRIDGE_CONTAINER_NAME", "yt-sonos-bridge")

# Docker Compose creates its own bridge network for the project even when
# every service uses network_mode: host, which leaves this host multi-homed
# (real LAN NIC + Docker's internal bridge). Joining a multicast group or
# sending from it with interface "0.0.0.0" lets the kernel pick either one,
# and picking the Docker bridge means queries never really leave the host.
# HOST_IP pins it to the real LAN interface explicitly.
HOST_IP = os.environ.get("HOST_IP")
if not HOST_IP:
    raise RuntimeError("HOST_IP env var must be set to this host's real LAN IP (see docker-compose.yml)")

# Bootstrap IP for zonegroup_discover — any already-known Sonos speaker on
# the household works, since it's used only to ask "who else is in your
# group" (see zonegroup_discover). Not required for the periodic MAC-based
# re-check once a target has actually been selected via the UI.
SEED_IP = os.environ.get("SEED_IP")

ZONEGROUP_SOAP_BODY = """<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:GetZoneGroupState xmlns:u="urn:schemas-upnp-org:service:ZoneGroupTopology:1"></u:GetZoneGroupState>
  </s:Body>
</s:Envelope>"""


def zonegroup_discover(seed_ip):
    """Ask one already-known Sonos speaker for its household's full zone
    topology — the same mechanism the real Sonos app uses, and far more
    reliable than raw SSDP M-SEARCH against these speakers (see
    ssdp_discover's docstring for why that path doesn't work here).
    Returns [{"ip":..., "mac":..., "name":...}, ...] or [] on any failure.
    """
    if not seed_ip:
        return []
    req = urllib.request.Request(
        f"http://{seed_ip}:1400/ZoneGroupTopology/Control",
        data=ZONEGROUP_SOAP_BODY.encode(),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": '"urn:schemas-upnp-org:service:ZoneGroupTopology:1#GetZoneGroupState"',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            envelope = r.read().decode(errors="ignore")
    except Exception as e:
        print(f"[sonos-watcher] zonegroup_discover({seed_ip}) failed: {e}")
        return []

    # The interesting bit is itself XML-escaped inside <ZoneGroupState>.
    m = re.search(r"<ZoneGroupState>(.*?)</ZoneGroupState>", envelope, re.DOTALL)
    if not m:
        return []
    inner = (
        m.group(1)
        .replace("&lt;", "<").replace("&gt;", ">")
        .replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")
    )

    found = {}
    for member in re.finditer(r"<ZoneGroupMember\b[^>]*>", inner):
        tag = member.group(0)
        loc_m = re.search(r'Location="http://([\d.]+):1400', tag)
        name_m = re.search(r'ZoneName="([^"]*)"', tag)
        uuid_m = re.search(r'UUID="RINCON_([0-9A-Fa-f]{12})', tag)
        if not (loc_m and uuid_m):
            continue
        ip = loc_m.group(1)
        mac_hex = uuid_m.group(1)
        mac = ":".join(mac_hex[i:i + 2] for i in range(0, 12, 2)).upper()
        found[ip] = {"ip": ip, "mac": mac, "name": name_m.group(1) if name_m else ip}
    return list(found.values())


def ssdp_discover(timeout=DISCOVERY_TIMEOUT):
    """Return [{"ip":..., "mac":..., "name":...}] for Sonos speakers on the
    LAN.

    Sonos doesn't reliably answer active SSDP M-SEARCH (confirmed by
    testing directly against a real speaker here — replies never arrived,
    from any host, with any ST value, over many attempts). What DOES work
    reliably is that Sonos speakers periodically broadcast their own NOTIFY
    announcements on the SSDP multicast group unprompted. So: still send
    the M-SEARCH (harmless, and other UPnP devices may honor it), but treat
    ANY sender on the group as a candidate and let _fetch_device_info's
    "does port 1400 return valid Sonos XML" check be the actual filter,
    rather than depending on parsing the SSDP packet's own headers.
    """
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        "MAN: \"ssdp:discover\"\r\n"
        "MX: 2\r\n"
        f"ST: {SSDP_ST}\r\n\r\n"
    ).encode()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Bind to the SSDP port and join the multicast group before sending:
    # some devices answer M-SEARCH by re-announcing to the multicast group
    # rather than replying by clean unicast to the querier. A socket that
    # only sent from an ephemeral port would miss those.
    sock.bind((HOST_IP, 1900))
    # Pin both group membership and the outgoing interface to the real LAN
    # NIC explicitly (see HOST_IP comment above) instead of leaving it to
    # default routing, which can pick Docker's own bridge network instead.
    mreq = socket.inet_aton("239.255.255.250") + socket.inet_aton(HOST_IP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(HOST_IP))
    sock.settimeout(timeout)
    sock.sendto(msg, ("239.255.255.250", 1900))

    found = {}
    tried = set()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            sock.settimeout(max(0.1, deadline - time.time()))
            _data, addr = sock.recvfrom(4096)
        except socket.timeout:
            break
        ip = addr[0]
        if ip in tried or ip == HOST_IP:
            continue
        tried.add(ip)
        device = _fetch_device_info(ip)
        if device:
            found[ip] = device
    sock.close()
    return list(found.values())


def _fetch_device_info(ip):
    try:
        with urllib.request.urlopen(f"http://{ip}:1400/xml/device_description.xml", timeout=2) as r:
            xml = r.read().decode(errors="ignore")
        name_m = re.search(r"<roomName>(.*?)</roomName>", xml) or re.search(r"<friendlyName>(.*?)</friendlyName>", xml)
        mac_m = re.search(r"<MACAddress>(.*?)</MACAddress>", xml)
        if not mac_m:
            return None
        return {
            "ip": ip,
            "mac": mac_m.group(1).strip().upper(),
            "name": (name_m.group(1).strip() if name_m else ip),
        }
    except Exception:
        return None


def discover_devices(timeout=DISCOVERY_TIMEOUT):
    """Zonegroup topology first (reliable, needs a seed IP), SSDP as a
    fallback for a from-scratch setup with no Sonos IP known yet at all."""
    state = load_state()
    seed = state.get("ip") or SEED_IP
    devices = zonegroup_discover(seed)
    if devices:
        return devices
    return ssdp_discover(timeout)


def resolve_ip_by_mac(mac, timeout=DISCOVERY_TIMEOUT):
    mac = mac.upper()
    for device in discover_devices(timeout):
        if device["mac"] == mac:
            return device["ip"]
    return None


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def write_env_and_maybe_restart(ip):
    """Write SONOS_DEVICE_IP into the env file yt-sonos-bridge loads, and
    restart it via the Docker socket if the IP actually changed."""
    current = None
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            for line in f:
                if line.startswith("SONOS_DEVICE_IP="):
                    current = line.strip().split("=", 1)[1]
    if current == ip:
        return False

    os.makedirs(os.path.dirname(ENV_FILE), exist_ok=True)
    with open(ENV_FILE, "w") as f:
        f.write(f"SONOS_DEVICE_IP={ip}\n")

    print(f"[sonos-watcher] IP changed ({current} -> {ip}), restarting {BRIDGE_CONTAINER_NAME}")
    try:
        subprocess.run(["docker", "restart", BRIDGE_CONTAINER_NAME], check=True, timeout=30)
    except Exception as e:
        print(f"[sonos-watcher] Failed to restart bridge: {e}")
    return True


def watch_loop():
    while True:
        state = load_state()
        mac = state.get("mac")
        if mac:
            ip = resolve_ip_by_mac(mac)
            if ip:
                write_env_and_maybe_restart(ip)
                if ip != state.get("ip"):
                    save_state({**state, "ip": ip})
            else:
                print(f"[sonos-watcher] Could not find {mac} on the network this round")
        time.sleep(CHECK_INTERVAL)


PAGE_TEMPLATE = """<!DOCTYPE html>
<html><head><title>sonos-castbridge target</title>
<style>
body {{ font-family: sans-serif; margin: 2rem; max-width: 700px; }}
table {{ border-collapse: collapse; width: 100%; }}
td, th {{ border: 1px solid #ccc; padding: 8px; text-align: left; }}
button {{ padding: 6px 14px; cursor: pointer; }}
.current {{ background: #eaffea; }}
</style></head>
<body>
<h1>Sonos target for this bridge</h1>
<p>Currently pinned to: <b>{current}</b></p>
<form method="post" action="/discover"><button type="submit">Scan network for Sonos speakers</button></form>
{results}
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[sonos-watcher] {self.address_string()} - {fmt % args}")

    def _send_html(self, body, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path == "/":
            state = load_state()
            current = f"{state.get('name', '?')} ({state.get('mac', 'none set')})" if state.get("mac") else "nothing selected yet"
            self._send_html(PAGE_TEMPLATE.format(current=current, results=""))
        else:
            self._send_html("Not found", 404)

    def do_POST(self):
        if self.path == "/discover":
            devices = discover_devices()
            rows = "".join(
                f"<tr><td>{d['name']}</td><td>{d['ip']}</td><td>{d['mac']}</td>"
                f"<td><form method='post' action='/select' style='margin:0'>"
                f"<input type='hidden' name='mac' value='{d['mac']}'>"
                f"<input type='hidden' name='ip' value='{d['ip']}'>"
                f"<input type='hidden' name='name' value='{d['name']}'>"
                f"<button type='submit'>Use this one</button></form></td></tr>"
                for d in devices
            )
            table = (
                f"<h2>Found {len(devices)} speaker(s)</h2>"
                f"<table><tr><th>Room</th><th>IP</th><th>MAC</th><th></th></tr>{rows}</table>"
                if devices else "<p>No Sonos speakers responded. Is the mDNS/SSDP relay running?</p>"
            )
            state = load_state()
            current = f"{state.get('name', '?')} ({state.get('mac', 'none set')})" if state.get("mac") else "nothing selected yet"
            self._send_html(PAGE_TEMPLATE.format(current=current, results=table))
        elif self.path == "/select":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            params = dict(p.split("=", 1) for p in body.split("&") if "=" in p)
            mac = urllib.parse.unquote_plus(params.get("mac", ""))
            name = urllib.parse.unquote_plus(params.get("name", ""))
            ip = urllib.parse.unquote_plus(params.get("ip", "")) or resolve_ip_by_mac(mac)
            if mac and ip:
                save_state({"mac": mac, "name": name, "ip": ip})
                write_env_and_maybe_restart(ip)
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
        else:
            self._send_html("Not found", 404)


if __name__ == "__main__":
    threading.Thread(target=watch_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "8088"))
    print(f"[sonos-watcher] UI on http://0.0.0.0:{port}, checking every {CHECK_INTERVAL}s")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
