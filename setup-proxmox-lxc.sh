#!/bin/bash
# Reference script showing how the two LXC containers in this project were
# provisioned on Proxmox VE. Not meant to be run blindly — read it, adjust
# VMIDs/storage/bridge names for your environment, then run the pieces you need.
set -euo pipefail

TEMPLATE="local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst"
BRIDGE="vmbr0"

### 1. mDNS/SSDP relay container ##############################################
# Needs TWO network interfaces on the same LAN/subnet. This isn't for bridging
# separate VLANs — it's a workaround for access points whose own firmware fails
# to flood multicast traffic between their own 2.4GHz and 5GHz radios. A wired
# device that re-transmits what it hears reaches both bands correctly because
# it enters the network through the switch port, not through a WiFi radio hop.
pct create 103 "$TEMPLATE" \
  --hostname mdns-relay \
  --memory 512 --swap 512 --cores 1 \
  --rootfs local-lvm:2 \
  --net0 name=eth0,bridge=$BRIDGE,ip=dhcp \
  --net1 name=eth1,bridge=$BRIDGE,ip=dhcp \
  --unprivileged 0 \
  --onboot 1 \
  --features nesting=1

pct start 103
# Inside the container:
#   apt-get install -y git python3 python3-netifaces
#   git clone https://github.com/alsmith/multicast-relay.git /opt/multicast-relay
#   copy systemd/multicast-relay.service to /etc/systemd/system/
#   systemctl enable --now multicast-relay

### 2. YouTube/YouTube Music -> Sonos cast bridge #############################
# Deliberately a SEPARATE, single-NIC container. Running it in the same
# container as the relay above causes it to advertise itself twice (once per
# interface IP) via DIAL/SSDP. Regular YouTube tolerates that; YouTube Music's
# cast handshake does not and hangs on "loading" forever. See README.
pct create 107 "$TEMPLATE" \
  --hostname yt-sonos-bridge \
  --memory 512 --swap 512 --cores 1 \
  --rootfs local-lvm:6 \
  --net0 name=eth0,bridge=$BRIDGE,ip=dhcp \
  --unprivileged 0 \
  --onboot 1 \
  --features nesting=1,keyctl=1

pct start 107
# Inside the container:
#   curl -fsSL https://get.docker.com | sh
#   cp .env.example .env   # fill in SONOS_DEVICE_IP and SERVER_ENDPOINT
#   docker compose up -d --build
