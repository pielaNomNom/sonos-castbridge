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
import html
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
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>sonos-castbridge · target</title>
<style>
  :root {{
    --bg: #15161a;
    --panel: #1d1f24;
    --inset: #101114;
    --line: #2b2d33;
    --text: #ede9e0;
    --text-dim: #8c8f98;
    --amber: #e2a34c;
    --good: #6fcf97;
    --mono: ui-monospace, "SF Mono", "IBM Plex Mono", Menlo, Consolas, monospace;
    --sans: -apple-system, "Segoe UI", system-ui, sans-serif;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    min-height: 100vh;
    background: var(--bg);
    background-image: radial-gradient(ellipse 900px 500px at 50% -10%, rgba(226,163,76,0.07), transparent 60%);
    color: var(--text);
    font-family: var(--sans);
    display: flex;
    justify-content: center;
    padding: 3.5rem 1.25rem 4rem;
  }}
  main {{ width: 100%; max-width: 460px; }}
  .eyebrow {{
    font-family: var(--mono);
    font-size: 0.72rem;
    letter-spacing: 0.18em;
    color: var(--text-dim);
    text-transform: uppercase;
    margin: 0 0 0.4rem;
  }}
  h1 {{
    font-size: 1.5rem;
    font-weight: 650;
    letter-spacing: -0.01em;
    margin: 0 0 1.75rem;
  }}
  .readout {{
    background: var(--inset);
    border: 1px solid var(--line);
    border-radius: 4px;
    padding: 1rem 1.15rem;
    margin-bottom: 1.5rem;
    display: flex;
    align-items: center;
    gap: 0.85rem;
  }}
  .led {{
    width: 9px; height: 9px;
    border-radius: 50%;
    flex-shrink: 0;
    background: var(--text-dim);
  }}
  .readout.is-set .led {{
    background: var(--good);
    box-shadow: 0 0 8px rgba(111,207,151,0.7);
  }}
  .readout.is-unset .led {{ animation: idle-pulse 2.4s ease-in-out infinite; }}
  @keyframes idle-pulse {{
    0%, 100% {{ opacity: 0.35; }}
    50% {{ opacity: 0.9; }}
  }}
  .readout-text {{ min-width: 0; }}
  .readout-label {{
    font-family: var(--mono);
    font-size: 0.68rem;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--text-dim);
    margin-bottom: 0.2rem;
  }}
  .readout-name {{
    font-family: var(--mono);
    font-size: 0.98rem;
    color: var(--amber);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .readout.is-unset .readout-name {{ color: var(--text-dim); }}
  .readout-sub {{
    font-family: var(--mono);
    font-size: 0.76rem;
    color: var(--text-dim);
    margin-top: 0.1rem;
  }}
  form.scan {{ margin: 0 0 1.75rem; }}
  button.scan-btn {{
    width: 100%;
    font-family: var(--mono);
    font-size: 0.78rem;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--bg);
    background: var(--amber);
    border: 1px solid var(--amber);
    border-radius: 3px;
    padding: 0.85rem 1rem;
    cursor: pointer;
    transition: transform 0.08s ease, background 0.15s ease;
  }}
  button.scan-btn:hover {{ background: #edb562; }}
  button.scan-btn:active {{ transform: translateY(1px); }}
  button.scan-btn:disabled {{ opacity: 0.75; cursor: default; }}
  button:focus-visible {{ outline: 2px solid var(--amber); outline-offset: 2px; }}
  .bars {{
    display: none;
    align-items: flex-end;
    justify-content: center;
    gap: 3px;
    height: 12px;
  }}
  .scanning .bars {{ display: inline-flex; }}
  .scanning .scan-label {{ display: none; }}
  .bars span {{
    width: 3px;
    background: var(--bg);
    animation: bar-bounce 0.9s ease-in-out infinite;
  }}
  .bars span:nth-child(1) {{ animation-delay: 0s; }}
  .bars span:nth-child(2) {{ animation-delay: 0.15s; }}
  .bars span:nth-child(3) {{ animation-delay: 0.3s; }}
  .bars span:nth-child(4) {{ animation-delay: 0.45s; }}
  @keyframes bar-bounce {{
    0%, 100% {{ height: 3px; }}
    50% {{ height: 12px; }}
  }}
  h2 {{
    font-family: var(--mono);
    font-size: 0.72rem;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--text-dim);
    font-weight: 500;
    margin: 0 0 0.75rem;
  }}
  .rows {{ display: flex; flex-direction: column; gap: 0.5rem; }}
  .row {{
    display: flex;
    align-items: center;
    gap: 0.75rem;
    background: var(--panel);
    border: 1px solid var(--line);
    border-left: 2px solid var(--line);
    border-radius: 3px;
    padding: 0.7rem 0.85rem;
  }}
  .row.current {{ border-left-color: var(--amber); }}
  .row .dot {{
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--good);
    flex-shrink: 0;
  }}
  .row-info {{ flex: 1; min-width: 0; }}
  .row-name {{ font-size: 0.92rem; font-weight: 550; }}
  .row-addr {{
    font-family: var(--mono);
    font-size: 0.72rem;
    color: var(--text-dim);
    margin-top: 0.1rem;
  }}
  .row form {{ margin: 0; }}
  .pin-btn {{
    font-family: var(--mono);
    font-size: 0.68rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text);
    background: transparent;
    border: 1px solid var(--line);
    border-radius: 3px;
    padding: 0.4rem 0.65rem;
    cursor: pointer;
    transition: border-color 0.15s ease, color 0.15s ease;
  }}
  .pin-btn:hover {{ border-color: var(--amber); color: var(--amber); }}
  .empty {{
    border: 1px dashed var(--line);
    border-radius: 3px;
    padding: 1.1rem;
    font-size: 0.85rem;
    color: var(--text-dim);
    text-align: center;
  }}
  footer {{
    margin-top: 2.5rem;
    font-family: var(--mono);
    font-size: 0.68rem;
    color: #4d4f57;
    text-align: center;
  }}
  @media (prefers-reduced-motion: reduce) {{
    .led, .bars span {{ animation: none !important; }}
  }}
</style>
</head>
<body>
<main>
  <p class="eyebrow">sonos-castbridge</p>
  <h1>Speaker target</h1>

  <div class="readout {readout_state}">
    <span class="led"></span>
    <div class="readout-text">
      <div class="readout-label">Pinned to</div>
      <div class="readout-name">{readout_name}</div>
      {readout_sub}
    </div>
  </div>

  <form class="scan" method="post" action="/discover">
    <button type="submit" class="scan-btn">
      <span class="scan-label">Scan network</span>
      <span class="bars"><span></span><span></span><span></span><span></span></span>
    </button>
  </form>

  {results}

  <footer>SSDP + ZoneGroupTopology &middot; re-checked every {interval}s</footer>
</main>
<script>
  var f = document.querySelector("form.scan");
  f.addEventListener("submit", function () {{
    var btn = f.querySelector("button");
    btn.disabled = true;
    btn.classList.add("scanning");
  }});
</script>
</body>
</html>
"""


def render_page(results_html=""):
    state = load_state()
    mac = state.get("mac")
    if mac:
        readout_state = "is-set"
        readout_name = html.escape(state.get("name", "?"))
        readout_sub = (
            f'<div class="readout-sub">{html.escape(mac)} &middot; '
            f'{html.escape(state.get("ip", "?"))}</div>'
        )
    else:
        readout_state = "is-unset"
        readout_name = "No target pinned"
        readout_sub = ""
    return PAGE_TEMPLATE.format(
        readout_state=readout_state,
        readout_name=readout_name,
        readout_sub=readout_sub,
        results=results_html,
        interval=CHECK_INTERVAL,
    )


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
            self._send_html(render_page())
        else:
            self._send_html("Not found", 404)

    def do_POST(self):
        if self.path == "/discover":
            devices = discover_devices()
            current_mac = load_state().get("mac")
            rows = "".join(
                f'<div class="row{" current" if d["mac"] == current_mac else ""}">'
                f'<span class="dot"></span>'
                f'<div class="row-info">'
                f'<div class="row-name">{html.escape(d["name"])}</div>'
                f'<div class="row-addr">{html.escape(d["ip"])} &middot; {html.escape(d["mac"])}</div>'
                f"</div>"
                f"<form method='post' action='/select'>"
                f"<input type='hidden' name='mac' value='{html.escape(d['mac'])}'>"
                f"<input type='hidden' name='ip' value='{html.escape(d['ip'])}'>"
                f"<input type='hidden' name='name' value='{html.escape(d['name'])}'>"
                f"<button type='submit' class='pin-btn'>Pin</button></form>"
                f"</div>"
                for d in devices
            )
            plural = "" if len(devices) == 1 else "s"
            results = (
                f'<h2>Found {len(devices)} speaker{plural}</h2><div class="rows">{rows}</div>'
                if devices else
                '<div class="empty">No speakers responded on the LAN.<br>'
                "Check that the mDNS/SSDP relay is running.</div>"
            )
            self._send_html(render_page(results))
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
