#!/usr/bin/env python3
"""
myowndpi - macOS icin sadece Discord ve Roblox'a uygulanan DPI bypass proxy'si.

Nasil calisir:
  * 127.0.0.1:8881 uzerinde bir HTTP/HTTPS (CONNECT) proxy acar.
  * Ayni portta /proxy.pac servis eder; macOS'a bu PAC verilir, boylece
    SADECE Discord/Roblox domainleri proxy'den gecer, gerisi DIRECT gider.
  * Izinli domainler icin DNS'i DoH (Cloudflare / Google, IP uzerinden)
    ile cozer -> DNS zehirlemesini atlatir.
  * TLS ClientHello'yu SNI'nin ortasindan iki TLS kaydina ve ayri TCP
    paketlerine boler -> SNI tabanli DPI engelini atlatir.
  * Izinli listede olmayan istekler hicbir degisiklik yapilmadan dogrudan
    gecirilir (sadece HTTPS_PROXY ortam degiskenini okuyan uygulamalardan gelir).

Sadece Python standart kutuphanesi kullanir (ek kurulum yok).
"""

import argparse
import asyncio
import json
import logging
import random
import socket
import ssl
import time
import urllib.request

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8881

# Sadece bu domainler (ve alt domainleri) tunellenir.
ALLOWED_SUFFIXES = (
    # Discord
    "discord.com", "discordapp.com", "discord.gg", "discordapp.net",
    "discord.media", "discordcdn.com", "discord.dev", "discord.new",
    "discord.gift", "discord.gifts", "discord.co", "discordstatus.com",
    "dis.gd", "discord-attachments-uploads-prd.storage.googleapis.com",
    # Roblox
    "roblox.com", "rbxcdn.com", "rbx.com", "robloxlabs.com",
    "rbxinfra.com", "rbxtrk.com", "roblox.qq.com", "robloxcdn.com",
    "roblox-poc.global.ssl.fastly.net",
)

DOH_SERVERS = (
    "https://1.1.1.1/dns-query",
    "https://8.8.8.8/resolve",
    "https://1.0.0.1/dns-query",
)

log = logging.getLogger("myowndpi")


def is_allowed(host: str) -> bool:
    host = host.lower().rstrip(".")
    return any(host == s or host.endswith("." + s) for s in ALLOWED_SUFFIXES)


# --------------------------------------------------------------------------
# DNS over HTTPS
# --------------------------------------------------------------------------

_dns_cache = {}  # host -> (expires_at, [ips])
_ssl_ctx = ssl.create_default_context()


def _doh_query(server: str, host: str):
    url = f"{server}?name={host}&type=A"
    req = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
    with urllib.request.urlopen(req, timeout=5, context=_ssl_ctx) as resp:
        data = json.loads(resp.read().decode())
    ips, ttl = [], 300
    for ans in data.get("Answer", []):
        if ans.get("type") == 1:
            ips.append(ans["data"])
            ttl = min(ttl, int(ans.get("TTL", 300)))
    return ips, max(ttl, 30)


async def resolve(host: str):
    try:
        socket.inet_aton(host)
        return [host]
    except OSError:
        pass
    cached = _dns_cache.get(host)
    if cached and cached[0] > time.time():
        return cached[1]
    loop = asyncio.get_running_loop()
    last_err = None
    for server in DOH_SERVERS:
        try:
            ips, ttl = await loop.run_in_executor(None, _doh_query, server, host)
            if ips:
                _dns_cache[host] = (time.time() + ttl, ips)
                return ips
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise OSError(f"DoH cozumleme basarisiz: {host} ({last_err})")


# --------------------------------------------------------------------------
# TLS ClientHello parcalama
# --------------------------------------------------------------------------

def find_sni_offset(record: bytes):
    """ClientHello icinde SNI host adinin baslangic offsetini dondurur."""
    try:
        if record[0] != 0x16 or record[5] != 0x01:
            return None
        p = 5 + 4          # record header + handshake header
        p += 2 + 32        # version + random
        p += 1 + record[p]                                  # session id
        p += 2 + int.from_bytes(record[p:p + 2], "big")     # cipher suites
        p += 1 + record[p]                                  # compression
        ext_end = p + 2 + int.from_bytes(record[p:p + 2], "big")
        p += 2
        while p + 4 <= ext_end:
            etype = int.from_bytes(record[p:p + 2], "big")
            elen = int.from_bytes(record[p + 2:p + 4], "big")
            if etype == 0:  # server_name
                # list len(2) + name type(1) + name len(2)
                return p + 4 + 5
            p += 4 + elen
    except IndexError:
        return None
    return None


def split_client_hello(data: bytes):
    """ClientHello'yu SNI'nin ortasindan bolunmus iki TLS kaydina cevirir."""
    if len(data) < 6 or data[0] != 0x16:
        return [data]
    rec_len = int.from_bytes(data[3:5], "big")
    if len(data) < 5 + rec_len:
        return [data]  # eksik kayit: bozmadan oldugu gibi gonder
    record, rest = data[:5 + rec_len], data[5 + rec_len:]
    payload = record[5:]
    sni = find_sni_offset(record)
    cut = (sni - 5 + 2) if sni else 1  # SNI'nin 2. byte'indan kes
    cut = max(1, min(cut, len(payload) - 1))
    hdr = record[:3]
    part1 = hdr + cut.to_bytes(2, "big") + payload[:cut]
    part2 = hdr + (len(payload) - cut).to_bytes(2, "big") + payload[cut:]
    chunks = [part1[:3], part1[3:], part2]  # ilk paketi de ayrica parcala
    if rest:
        chunks.append(rest)
    return chunks


# --------------------------------------------------------------------------
# Proxy
# --------------------------------------------------------------------------

def pac_script() -> bytes:
    suffixes = json.dumps(list(ALLOWED_SUFFIXES))
    return f"""function FindProxyForURL(url, host) {{
  host = host.toLowerCase();
  var list = {suffixes};
  for (var i = 0; i < list.length; i++) {{
    if (host == list[i] || dnsDomainIs(host, "." + list[i]))
      return "PROXY {LISTEN_HOST}:{LISTEN_PORT}";
  }}
  return "DIRECT";
}}
""".encode()


async def pipe(reader, writer):
    total = 0
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            total += len(data)
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass
    return total


async def open_upstream(host: str, port: int):
    ips = await resolve(host)
    random.shuffle(ips)
    last = None
    for ip in ips:
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(ip, port), 8)
            sock = w.get_extra_info("socket")
            if sock:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return r, w
        except Exception as e:  # noqa: BLE001
            last = e
    raise OSError(f"{host}:{port} baglanamadi ({last!r})")


async def open_direct(host: str, port: int):
    """Listede olmayan hostlar: sistem DNS'i, hicbir hile yok."""
    return await asyncio.wait_for(asyncio.open_connection(host, port), 15)


async def send_fragmented(writer, first: bytes):
    chunks = split_client_hello(first)
    log.debug("ClientHello %d bayt -> parcalar %s", len(first),
              [len(c) for c in chunks])
    for chunk in chunks:
        writer.write(chunk)
        await writer.drain()
        await asyncio.sleep(0.01)


async def handle_connect(creader, cwriter, host, port, bypass=False):
    if bypass:
        ur, uw = await open_direct(host, port)
        cwriter.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await cwriter.drain()
        await asyncio.gather(pipe(creader, uw), pipe(ur, cwriter))
        return
    ur, uw = await open_upstream(host, port)
    cwriter.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await cwriter.drain()
    first = await asyncio.wait_for(creader.read(65536), 30)
    if not first:
        uw.close()
        return
    if first[0] == 0x16:
        # ClientHello birden fazla okumaya bolunmus olabilir (Chromium'un
        # post-quantum ClientHello'su buyuk); tum TLS kaydini bekle.
        while len(first) < 5 or len(first) < 5 + int.from_bytes(first[3:5], "big"):
            more = await asyncio.wait_for(creader.read(65536), 30)
            if not more:
                break
            first += more
        await send_fragmented(uw, first)
    else:
        uw.write(first)
        await uw.drain()
    up, down = await asyncio.gather(pipe(creader, uw), pipe(ur, cwriter))
    log.info("KAPANDI %s  giden=%d gelen=%d", host, up + len(first), down)


async def handle_http(creader, cwriter, method, target, version, headers_raw,
                      bypass=False):
    # Plain HTTP: http://host[:port]/path
    rest = target.split("://", 1)[1]
    hostport, _, path = rest.partition("/")
    host, _, port = hostport.partition(":")
    port = int(port or 80)
    ur, uw = await (open_direct if bypass else open_upstream)(host, port)
    req = f"{method} /{path} {version}\r\n".encode() + headers_raw
    # Host basligini ayri pakete bolerek HTTP DPI'ini da zorlastir
    idx = req.lower().find(b"\r\nhost:")
    if idx > 0 and not bypass:
        uw.write(req[:idx + 4])
        await uw.drain()
        await asyncio.sleep(0.01)
        uw.write(req[idx + 4:])
    else:
        uw.write(req)
    await uw.drain()
    await asyncio.gather(pipe(creader, uw), pipe(ur, cwriter))


async def handle_client(creader, cwriter):
    peer = cwriter.get_extra_info("peername")
    try:
        head = await asyncio.wait_for(creader.readuntil(b"\r\n\r\n"), 30)
    except Exception:  # noqa: BLE001
        cwriter.close()
        return
    line, _, headers_raw = head.partition(b"\r\n")
    try:
        method, target, version = line.decode("latin-1").split(" ", 2)
    except ValueError:
        cwriter.close()
        return

    try:
        if method == "GET" and target in ("/proxy.pac", "/"):
            body = pac_script()
            cwriter.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/x-ns-proxy-autoconfig\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body)
            await cwriter.drain()
            cwriter.close()
            return

        if method == "CONNECT":
            host, _, port = target.rpartition(":")
            host, port = host.strip("[]"), int(port or 443)
        elif "://" in target:
            host = target.split("://", 1)[1].split("/", 1)[0].split(":")[0]
            port = None
        else:
            cwriter.close()
            return

        # Listede olmayanlar (HTTPS_PROXY ortam degiskenini okuyan diger
        # uygulamalar) hicbir degisiklik yapilmadan dogrudan gecirilir.
        bypass = not is_allowed(host)
        if bypass:
            log.debug("DIREKT %s %s", method, host)
        else:
            log.info("TUNEL %s %s", method, host)
        if method == "CONNECT":
            await handle_connect(creader, cwriter, host, port, bypass)
        else:
            await handle_http(creader, cwriter, method, target, version,
                              headers_raw, bypass)
    except Exception as e:  # noqa: BLE001
        log.warning("hata %s %s: %s", peer, target, e)
        try:
            cwriter.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            await cwriter.drain()
        except Exception:  # noqa: BLE001
            pass
        cwriter.close()


async def main_async():
    server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT)
    log.info("myowndpi calisiyor: %s:%d  (PAC: http://%s:%d/proxy.pac)",
             LISTEN_HOST, LISTEN_PORT, LISTEN_HOST, LISTEN_PORT)
    async with server:
        await server.serve_forever()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-q", "--quiet", action="store_true", help="sadece uyarilari yaz")
    ap.add_argument("-v", "--verbose", action="store_true", help="ayrintili log")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else
                        logging.WARNING if args.quiet else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
