#!/bin/bash
# myowndpi kontrol betigi: ./dpi.sh start | stop | status | log | discord
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$DIR/.dpi.pid"
LOGFILE="$DIR/dpi.log"
PAC_URL="http://127.0.0.1:8881/proxy.pac"

services() {
  # Aktif ag servisleri (Wi-Fi, Ethernet, ...); devre disi olanlar (*) atlanir
  networksetup -listallnetworkservices | tail -n +2 | grep -v '^\*'
}

PROXY=http://127.0.0.1:8881
ENV_VARS="HTTPS_PROXY HTTP_PROXY https_proxy http_proxy"

# Discord'un guncelleyicisi PAC'i okumuyor, sadece HTTPS_PROXY'yi okuyor.
# launchctl ile ayarlaninca Dock'tan acilan ve kendini yeniden baslatan
# uygulamalar da gorur (Mac yeniden baslayana kadar gecerli).
set_env() {
  for v in $ENV_VARS; do launchctl setenv "$v" "$PROXY"; done
  launchctl setenv NO_PROXY "localhost,127.0.0.1,::1"
  launchctl setenv no_proxy "localhost,127.0.0.1,::1"
}
unset_env() {
  for v in $ENV_VARS NO_PROXY no_proxy; do launchctl unsetenv "$v"; done
}

running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

case "${1:-}" in
  start)
    if running; then echo "Zaten calisiyor (pid $(cat "$PIDFILE"))."; exit 0; fi
    nohup /usr/bin/python3 "$DIR/dpi.py" ${DPI_ARGS:-} >"$LOGFILE" 2>&1 &
    echo $! >"$PIDFILE"
    sleep 1
    running || { echo "Baslatilamadi, log:"; cat "$LOGFILE"; exit 1; }
    while IFS= read -r s; do
      networksetup -setautoproxyurl "$s" "$PAC_URL" 2>/dev/null &&
        networksetup -setautoproxystate "$s" on 2>/dev/null &&
        echo "PAC ayarlandi: $s"
    done < <(services)
    set_env
    echo "myowndpi calisiyor. Sadece Discord ve Roblox tunelleniyor."
    echo "Discord/Roblox aciksa tamamen kapatip yeniden acin."
    ;;
  stop)
    while IFS= read -r s; do
      networksetup -setautoproxystate "$s" off 2>/dev/null
    done < <(services)
    unset_env
    if running; then kill "$(cat "$PIDFILE")"; fi
    rm -f "$PIDFILE"
    echo "myowndpi durduruldu, proxy ayarlari geri alindi."
    ;;
  status)
    if running; then echo "Calisiyor (pid $(cat "$PIDFILE"))"; else echo "Calismiyor"; fi
    echo "HTTPS_PROXY (uygulamalar): $(launchctl getenv HTTPS_PROXY || true)"
    while IFS= read -r s; do
      printf '%s: ' "$s"; networksetup -getautoproxyurl "$s" | tr '\n' ' '; echo
    done < <(services)
    ;;
  log)
    tail -f "$LOGFILE"
    ;;
  discord|roblox)
    # Ortam degiskeni artik start ile kalici; bu komut sadece uygulamayi
    # proxy ayarlariyla temiz sekilde yeniden baslatir.
    running || "$0" start
    if [ "$1" = discord ]; then app=Discord; proc=Discord; else app=Roblox; proc=RobloxPlayer; fi
    pkill -x "$proc" 2>/dev/null; sleep 1
    P=$PROXY
    open -a "$app" --env HTTPS_PROXY=$P --env HTTP_PROXY=$P \
      --env https_proxy=$P --env http_proxy=$P \
      --args --proxy-pac-url="$PAC_URL"
    echo "$app proxy ile baslatildi."
    ;;
  *)
    echo "Kullanim: $0 {start|stop|status|log|discord|roblox}"; exit 1 ;;
esac
