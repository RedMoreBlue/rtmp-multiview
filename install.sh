#!/usr/bin/env bash
# RTMP Multiview installer for Debian 12/13 (LXC or VM). Run as root from this folder.
#
#   ./install.sh          base install (ffmpeg, python, web UI, systemd service)
#   ./install.sh --ndi    + NDI 6 runtime (libndi) for output.type "ndi"
#   ./install.sh --omt    + build libomt/libvmx from source for output.type "omt"
#   ./install.sh --desktop        + "RTMP Multiview" app launcher (fullscreen window) for
#                                   machines with a desktop; config in ~/.config/rtmp-multiview/
#   ./install.sh --autostart      (with --desktop) start the fullscreen multiview at login
#   ./install.sh --no-service     don't enable the headless background service
#
# Options can be combined. Re-running is safe: it updates the program and keeps
# /etc/rtmp-multiview/config.json.
set -euo pipefail
cd "$(dirname "$0")"
WITH_NDI=0
WITH_OMT=0
WITH_DESKTOP=0
WITH_AUTOSTART=0
WITH_SERVICE=1
for a in "$@"; do
  case "$a" in
    --ndi) WITH_NDI=1 ;;
    --omt) WITH_OMT=1 ;;
    --desktop) WITH_DESKTOP=1 ;;
    --autostart) WITH_DESKTOP=1; WITH_AUTOSTART=1 ;;
    --no-service) WITH_SERVICE=0 ;;
    *) echo "unknown option: $a"; exit 1 ;;
  esac
done
[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }
arch=$(uname -m)

echo "==> Packages"
apt-get update
apt-get install -y --no-install-recommends \
  python3 python3-numpy python3-pil ffmpeg fonts-dejavu-core avahi-daemon \
  ca-certificates curl

echo "==> Files"
id multiview >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin multiview
install -d /opt/rtmp-multiview /etc/rtmp-multiview
install -m 755 multiview.py /opt/rtmp-multiview/multiview.py
if [[ ! -f /etc/rtmp-multiview/config.json ]]; then
  install -m 600 config.example.json /etc/rtmp-multiview/config.json
fi
chown -R multiview:multiview /etc/rtmp-multiview
chmod 700 /etc/rtmp-multiview
chmod 600 /etc/rtmp-multiview/config.json
install -m 644 rtmp-multiview.service /etc/systemd/system/rtmp-multiview.service

if [[ $WITH_NDI -eq 1 ]]; then
  echo "==> NDI 6 runtime (you are accepting the NDI SDK licence by continuing)"
  tmp=$(mktemp -d)
  curl -fL -o "$tmp/ndi.tar.gz" https://downloads.ndi.tv/SDK/NDI_SDK_Linux/Install_NDI_SDK_v6_Linux.tar.gz
  tar -xzf "$tmp/ndi.tar.gz" -C "$tmp"
  (cd "$tmp" && yes | PAGER=cat sh ./Install_NDI_SDK_v6_Linux.sh >/dev/null)
  case "$arch" in
    x86_64)  ndiarch=x86_64-linux-gnu ;;
    aarch64) ndiarch=aarch64-rpi4-linux-gnueabi ;;
    *) echo "unsupported arch $arch"; exit 1 ;;
  esac
  cp -P "$tmp"/NDI\ SDK\ for\ Linux/lib/$ndiarch/libndi.so* /usr/local/lib/
  ldconfig
  rm -rf "$tmp"
  ls /usr/local/lib/libndi.so.6 >/dev/null && echo "    libndi.so.6 OK"
fi

if [[ $WITH_OMT -eq 1 ]]; then
  echo "==> OMT: building libvmx + libomt (needs ~2 GB disk, a few minutes)"
  case "$arch" in
    x86_64)
      grep -qw avx2 /proc/cpuinfo || { echo "libvmx needs a CPU with AVX2"; exit 1; }
      plat=x64 ;;
    aarch64) plat=arm64 ;;
    *) echo "unsupported arch $arch"; exit 1 ;;
  esac
  icu=$(apt-cache search --names-only '^libicu[0-9]+$' | awk '{print $1}' | sort -V | tail -1)
  apt-get install -y --no-install-recommends git clang zlib1g-dev $icu
  if [[ ! -x /opt/dotnet/dotnet ]]; then
    curl -fsSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh
    bash /tmp/dotnet-install.sh --channel 8.0 --install-dir /opt/dotnet
  fi
  export PATH=/opt/dotnet:$PATH DOTNET_ROOT=/opt/dotnet DOTNET_CLI_TELEMETRY_OPTOUT=1
  src=/usr/local/src/omt
  rm -rf "$src"; mkdir -p "$src"
  for r in libomtnet libomt libvmx; do
    git clone --depth 1 "https://github.com/openmediatransport/$r.git" "$src/$r"
  done
  (cd "$src/libvmx/build" && bash "./buildlinux$plat.sh")
  (cd "$src/libomt/build" && bash "./buildlinux$plat.sh")
  vmx=$(find "$src/libvmx" -name libvmx.so | head -1)
  omt=$(find "$src/libomt" -path '*native*' -name libomt.so | head -1)
  [[ -n "$vmx" && -n "$omt" ]] || { echo "OMT build output not found"; exit 1; }
  install -m 755 "$vmx" "$omt" /usr/local/lib/
  ldconfig
  echo "    libomt.so + libvmx.so installed in /usr/local/lib"
  echo "    set \"type\": \"omt\" in /etc/rtmp-multiview/config.json to use it"
fi

# command line launchers
cat > /usr/local/bin/rtmp-multiview <<'EOF'
#!/bin/sh
# headless / command line: rtmp-multiview [config.json] [--display|--fullscreen|--no-output]
exec /usr/bin/python3 /opt/rtmp-multiview/multiview.py "$@"
EOF
cat > /usr/local/bin/rtmp-multiview-gui <<'EOF'
#!/bin/sh
# fullscreen window for a desktop session, per-user config (created on first start)
cfg="${XDG_CONFIG_HOME:-$HOME/.config}/rtmp-multiview/config.json"
exec /usr/bin/python3 /opt/rtmp-multiview/multiview.py "$cfg" --fullscreen "$@"
EOF
chmod 755 /usr/local/bin/rtmp-multiview /usr/local/bin/rtmp-multiview-gui

if [[ $WITH_DESKTOP -eq 1 ]]; then
  echo "==> Desktop launcher"
  # Intel GPU video decode drivers (i965 = Haswell/2014 Mac mini, iHD = newer) for "hwaccel": "vaapi"
  if [[ "$arch" == "x86_64" ]]; then
    apt-get install -y --no-install-recommends i965-va-driver intel-media-va-driver vainfo || true
  fi
  cat > /usr/share/applications/rtmp-multiview.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=RTMP Multiview
Comment=2x2 multiview of RTMP feeds (fullscreen; press f to toggle, q to quit)
Exec=/usr/local/bin/rtmp-multiview-gui
Icon=video-display
Terminal=false
Categories=AudioVideo;Video;
EOF
  if [[ $WITH_AUTOSTART -eq 1 ]]; then
    install -d /etc/xdg/autostart
    cp /usr/share/applications/rtmp-multiview.desktop /etc/xdg/autostart/rtmp-multiview.desktop
    echo "    autostart at login enabled (/etc/xdg/autostart/rtmp-multiview.desktop)"
  fi
fi

systemctl daemon-reload
systemctl enable --now avahi-daemon
ip=$(hostname -I | awk '{print $1}')
echo
if [[ $WITH_SERVICE -eq 1 ]]; then
  systemctl enable rtmp-multiview
  systemctl restart rtmp-multiview
  echo "Done. Open http://$ip:8080/ to set the 4 stream URLs."
  echo "Logs: journalctl -u rtmp-multiview -f"
else
  systemctl disable --now rtmp-multiview 2>/dev/null || true
  echo "Done (no background service)."
fi
if [[ $WITH_DESKTOP -eq 1 ]]; then
  echo "Desktop: start \"RTMP Multiview\" from the app menu, or run rtmp-multiview-gui."
  [[ $WITH_SERVICE -eq 1 ]] && echo "  Note: the service already uses port 8080; the desktop app then runs without its own web UI."
fi
