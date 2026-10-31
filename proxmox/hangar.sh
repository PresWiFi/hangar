#!/usr/bin/env bash
# Deploys Hangar to Proxmox VE: creates a Debian 12 LXC container with Docker and starts Hangar in it.
# Run it as root in the Proxmox host's shell:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/PresWiFi/hangar/main/proxmox/hangar.sh)"
#
# It asks a few questions (Enter keeps the default). To skip them, set any of these and YES=1:
#   CTID CT_HOSTNAME CORES MEMORY DISK STORAGE TEMPLATE_STORAGE BRIDGE IP GATEWAY TAILSCALE HANGAR_BRANCH
#   e.g.  YES=1 CTID=210 MEMORY=8192 TAILSCALE=1 bash -c "$(curl -fsSL .../proxmox/hangar.sh)"
#
# Update Hangar later from inside the container:  pct exec <ctid> -- update-hangar
set -euo pipefail

bold=$'\e[1m'; green=$'\e[32m'; yellow=$'\e[33m'; red=$'\e[31m'; reset=$'\e[0m'
step() { printf '\n%s==> %s%s\n' "$bold" "$*" "$reset"; }
ok()   { printf '%s  ✓ %s%s\n' "$green" "$*" "$reset"; }
warn() { printf '%s  ! %s%s\n' "$yellow" "$*" "$reset"; }
die()  { printf '%s  ✗ %s%s\n' "$red" "$*" "$reset" >&2; exit 1; }

# ask VAR "Question" default: keeps an existing value of VAR; prompts unless YES=1.
ask() {
  local var="$1" q="$2" def="$3" cur="${!1:-}"
  if [ -n "$cur" ]; then return; fi
  if [ "${YES:-0}" = 1 ]; then printf -v "$var" '%s' "$def"; return; fi
  local r; read -r -p "  $q [$def]: " r </dev/tty
  printf -v "$var" '%s' "${r:-$def}"
}

[ "$(id -u)" -eq 0 ] || die "Run this as root on the Proxmox host."
command -v pct >/dev/null && command -v pveam >/dev/null || die "This isn't a Proxmox VE host (pct and pveam are missing)."

# ---------- settings ----------

first_storage() { pvesm status -content "$1" 2>/dev/null | awk 'NR > 1 && $3 == "active" {print $1; exit}'; }

step "Hangar container settings"
ask CTID "Container ID" "$(pvesh get /cluster/nextid)"
ask CT_HOSTNAME "Hostname" "hangar"
ask CORES "CPU cores" "4"
ask MEMORY "Memory (MB)" "4096"
ask DISK "Disk size (GB)" "32"
ask STORAGE "Storage for the disk" "$(first_storage rootdir)"
ask TEMPLATE_STORAGE "Storage for the Debian template" "$(first_storage vztmpl)"
ask BRIDGE "Network bridge" "vmbr0"
ask IP "IPv4 address (dhcp, or e.g. 192.168.1.50/24)" "dhcp"
if [ "$IP" != dhcp ]; then ask GATEWAY "Gateway" "$(ip route | awk '/^default/ {print $3; exit}')"; fi
ask TAILSCALE "Install Tailscale in the container? (1 = yes, 0 = no)" "1"
ask HANGAR_BRANCH "Hangar branch" "main"

[[ "$CTID" =~ ^[0-9]+$ ]] || die "Container ID must be a number."
pct status "$CTID" >/dev/null 2>&1 && die "Container $CTID already exists. Pick another ID."
[ -n "$STORAGE" ] || die "No storage for container disks found. Set STORAGE."
[ -n "$TEMPLATE_STORAGE" ] || die "No storage for templates found. Set TEMPLATE_STORAGE."
TZ_HOST="$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo UTC)"

# ---------- template ----------

step "Debian 12 template"
pveam update >/dev/null || warn "Couldn't refresh the template list; using what's known."
TEMPLATE="$(pveam available --section system | awk '$2 ~ /^debian-12-standard_.*_amd64\.tar\.zst$/ {print $2}' | sort -V | tail -1)"
[ -n "$TEMPLATE" ] || die "No Debian 12 template found in 'pveam available'."
if pveam list "$TEMPLATE_STORAGE" | grep -q "$TEMPLATE"; then
  ok "$TEMPLATE is already downloaded"
else
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi

# ---------- container ----------

step "Creating container $CTID ($CT_HOSTNAME)"
NET="name=eth0,bridge=$BRIDGE,ip=$IP"
if [ "$IP" != dhcp ] && [ -n "${GATEWAY:-}" ]; then NET="$NET,gw=$GATEWAY"; fi
# Unprivileged, with nesting and keyctl so Docker runs inside.
pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
  --hostname "$CT_HOSTNAME" --cores "$CORES" --memory "$MEMORY" --swap 512 \
  --rootfs "$STORAGE:$DISK" --net0 "$NET" \
  --features nesting=1,keyctl=1 --unprivileged 1 --onboot 1 --ostype debian \
  --timezone host --description "Hangar: https://github.com/PresWiFi/hangar" >/dev/null
if [ "$TAILSCALE" = 1 ]; then
  # Tailscale needs the TUN device.
  cat >>"/etc/pve/lxc/$CTID.conf" <<'CONF'
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
CONF
fi
pct start "$CTID"
ok "Container $CTID is running"

step "Waiting for the network"
for _ in $(seq 60); do
  pct exec "$CTID" -- getent hosts github.com >/dev/null 2>&1 && break
  sleep 2
done
pct exec "$CTID" -- getent hosts github.com >/dev/null 2>&1 || die "Container $CTID has no internet access. Check the bridge and IP settings."
ok "Online"

# ---------- inside the container ----------

# Runs inside the container. Settings come in as environment variables.
read -r -d '' INSTALL <<'SCRIPT' || true
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
echo "  Installing packages and Docker (a few minutes)..."
apt-get update -qq
apt-get install -y -qq curl git ca-certificates >/dev/null
curl -fsSL https://get.docker.com | sh >/dev/null

# Hangar's container runs as uid 1000; the workspace on this side belongs to the same uid.
id hangar >/dev/null 2>&1 || useradd -m -u 1000 -s /bin/bash hangar
WORKSPACE=/home/hangar/claude
install -d -o hangar -g hangar "$WORKSPACE"

if [ -d /opt/hangar/.git ]; then git -C /opt/hangar pull -q --ff-only
else git clone -q --branch "$HANGAR_BRANCH" https://github.com/PresWiFi/hangar.git /opt/hangar; fi
cd /opt/hangar
if [ ! -f .env ]; then
  cp .env.example .env
  sed -i -e "s#^WORKSPACE=.*#WORKSPACE=$WORKSPACE#" -e "s#^TZ=.*#TZ=$TZ_HOST#" \
         -e "s#^HANGAR_HOSTNAME=.*#HANGAR_HOSTNAME=$(hostname)#" -e "s#^UID=.*#UID=1000#" -e "s#^GID=.*#GID=1000#" .env
fi

cat >/usr/local/bin/update-hangar <<'UPD'
#!/bin/bash
# Pulls the latest Hangar and rebuilds it. Sessions are restored after the restart.
set -e
cd /opt/hangar
git pull --ff-only
docker compose build --pull
docker compose up -d
UPD
chmod +x /usr/local/bin/update-hangar

echo "  Building Hangar (a few minutes)..."
docker compose up -d --build >/dev/null 2>&1 || docker compose up -d --build

if [ "$TAILSCALE" = 1 ] && ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh >/dev/null
fi
SCRIPT

step "Installing Hangar in the container"
pct exec "$CTID" -- env HANGAR_BRANCH="$HANGAR_BRANCH" TZ_HOST="$TZ_HOST" TAILSCALE="$TAILSCALE" bash -c "$INSTALL"
ok "Hangar is running"

if [ "$TAILSCALE" = 1 ]; then
  step "Tailscale"
  echo "  Open the link below to add this container to your tailnet:"
  pct exec "$CTID" -- tailscale up --hostname "$CT_HOSTNAME" || warn "Run later: pct exec $CTID -- tailscale up"
fi

# ---------- done ----------

CODE=""
for _ in $(seq 30); do
  CODE="$(pct exec "$CTID" -- docker logs hangar 2>&1 | grep -oE '[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}' | tail -1 || true)"
  [ -n "$CODE" ] && break
  sleep 2
done
LAN_IP="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
TS_IP=""
if [ "$TAILSCALE" = 1 ]; then TS_IP="$(pct exec "$CTID" -- tailscale ip -4 2>/dev/null | head -1 || true)"; fi

step "Done"
echo "  Hangar:      http://$LAN_IP:7878${TS_IP:+   or   http://$TS_IP:7878 (Tailscale)}"
if [ -n "$CODE" ]; then echo "  Setup code:  ${bold}$CODE${reset}"
else echo "  Setup code:  pct exec $CTID -- docker logs hangar"; fi
echo "  Then create the owner account, sign in with Claude, and link GitHub in Settings."
echo
echo "  Workspace:   /home/hangar/claude in container $CTID"
echo "  Update:      pct exec $CTID -- update-hangar"
echo "  Shell:       pct enter $CTID"
