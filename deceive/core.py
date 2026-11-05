#!/usr/bin/env python3
"""Appear offline in League of Legends / Legends of Runeterra on macOS.

macOS port of molenzwiebel/Deceive (GPL-3.0). Same mechanism, no Windows deps:

  1. launch the Riot Client with --client-config-url pointing at a local server
  2. rewrite chat.host / chat.port / chat.affinities in the clientconfig response
  3. terminate TLS for deceive-localhost.molenzwiebel.xyz on loopback and relay
     to the real chat server (the certificate is publicly trusted, so no keychain work)
  4. rewrite <presence> stanzas so friends see you offline while chat keeps working
  5. add a fake friend "Deceive Active!" so status can be toggled from game chat,
     plus a loopback JSON control endpoint

Usage:
  python3 deceive_mac.py rc                 # just the Riot Client
  python3 deceive_mac.py lol --status offline
  python3 deceive_mac.py lol --debug        # log every stanza
  python3 deceive_mac.py --selftest         # offline unit checks
"""

from __future__ import annotations

import argparse
import http.client
import http.server
import json
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

FAKE_JID = "41c322a1-b328-495b-a004-5ccd3e45eae8@eu1.pvp.net"
FAKE_JID_BYTES = FAKE_JID.encode()
LOCALHOST_DOMAIN = "deceive-localhost.molenzwiebel.xyz"
CONFIG_HOST = "clientconfig.rpg.riotgames.com"
GEO_PAS_URL = "https://riot-geo.pas.si.riotgames.com/pas/v1/service/chat"
CERT_URL = "https://mln.cx/deceive/localhost.pfx"
USER_AGENT = "Deceive v1.18.0"
ROSTER_MARKER = b"<query xmlns='jabber:iq:riotgames:roster'>"
DIRECTED_PRESENCE = re.compile(rb"^<presence\b[^>]*?\bto\s*=")

CACHE_DIR = Path.home() / "Library" / "Caches" / "deceive-mac"
LOG_PATH = Path.home() / "Library" / "Logs" / "deceive-mac.log"
RIOT_CLIENT_INSTALLS = Path("/Users/Shared/Riot Games/RiotClientInstalls.json")
RIOT_CLIENT_FALLBACK = Path("/Applications/Riot Client.app/Contents/MacOS/RiotClientServices")

PRODUCTS = {
    "lol": "league_of_legends",
    "tft": "teamfighttactics",
    "lor": "bacon",
    "lion": "lion",
    "valorant": "valorant",
    "rc": None,
}

DEBUG = False
_log_lock = threading.Lock()
_log_file = None


def log(message: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    line = f"[{stamp}] {message}"
    with _log_lock:
        print(line, file=sys.stderr, flush=True)
        if _log_file is not None:
            _log_file.write(line + "\n")
            _log_file.flush()


def debug(message: str) -> None:
    if DEBUG:
        log(message)


# --------------------------------------------------------------------------- TLS


def load_certificate() -> tuple[ssl.SSLContext, float]:
    """Return a server SSLContext for LOCALHOST_DOMAIN plus the expiry timestamp.

    The pfx is the same publicly trusted certificate upstream Deceive uses, so
    clients validate it through the normal trust store.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cert_path = CACHE_DIR / "localhost.pem"

    expiry = _cert_expiry(cert_path)
    if expiry is None or expiry < time.time() + 20 * 86400:
        log("downloading proxy certificate")
        try:
            request = urllib.request.Request(CERT_URL, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=30) as response:
                pfx = response.read()
            _write_pem(pfx, cert_path)
            refreshed = _cert_expiry(cert_path)
            if refreshed is None:
                raise ValueError("certificate file is not readable after download")
            expiry = refreshed
        except Exception as exc:  # noqa: BLE001
            if expiry is None:
                raise SystemExit(f"cannot obtain the proxy certificate: {exc}") from exc
            log(f"certificate refresh failed, using cached one: {exc}")

    try:
        cert_path.chmod(0o600)  # the PEM holds the private key
    except OSError:
        pass

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_path))  # combined PEM: key + cert
    return ctx, expiry


def _cert_expiry(path: Path) -> float | None:
    """Read the notAfter timestamp of the first certificate in a PEM file."""
    if not path.exists():
        return None
    try:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(path.read_bytes())
        return cert.not_valid_after_utc.timestamp()
    except Exception:  # noqa: BLE001
        pass
    try:
        out = subprocess.run(
            ["openssl", "x509", "-in", str(path), "-noout", "-enddate"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return ssl.cert_time_to_seconds(out.split("=", 1)[1])
    except Exception:  # noqa: BLE001
        return None


def _write_pem(pfx: bytes, cert_path: Path) -> None:
    try:
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
            pkcs12,
        )

        key, cert, chain = pkcs12.load_key_and_certificates(pfx, b"")
        if key is None or cert is None:
            raise ValueError("pfx without key or certificate")
        parts = [cert.public_bytes(Encoding.PEM)]
        if chain:
            parts += [c.public_bytes(Encoding.PEM) for c in chain]
        parts.append(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
        cert_path.write_bytes(b"".join(parts))
        cert_path.chmod(0o600)
        return
    except ImportError:
        pass

    # Fallback: openssl CLI writes key + cert + chain into one file.
    pfx_path = cert_path.with_suffix(".pfx")
    pfx_path.write_bytes(pfx)
    subprocess.run(
        ["openssl", "pkcs12", "-in", str(pfx_path), "-nodes", "-passin", "pass:",
         "-out", str(cert_path)],
        check=True, capture_output=True,
    )


# ------------------------------------------------------------------- clientconfig


def rewrite_config(payload: dict, chat_port: int,
                   upstream_host: str | None) -> tuple[dict, tuple[str, int] | None]:
    """Point the chat server at loopback; return the config plus the real endpoint."""
    real_host = None
    real_port = None

    host = payload.get("chat.host")
    if isinstance(host, str):
        real_host = host
        payload["chat.host"] = LOCALHOST_DOMAIN

    port = payload.get("chat.port")
    if isinstance(port, int):
        real_port = port
        payload["chat.port"] = chat_port

    affinities = payload.get("chat.affinities")
    if isinstance(affinities, dict):
        if upstream_host:
            real_host = upstream_host
        for name in list(affinities):
            affinities[name] = LOCALHOST_DOMAIN

    if real_host is None or real_port is None:
        return payload, None
    return payload, (real_host, real_port)


def geo_affinity_host(authorization: str | None) -> str | None:
    """Ask Riot's PAS for the player's affinity and map it to a chat host."""
    if not authorization:
        return None
    try:
        request = urllib.request.Request(GEO_PAS_URL, headers={
            "Authorization": authorization,
            "User-Agent": USER_AGENT,
        })
        with urllib.request.urlopen(request, timeout=10) as response:
            token = response.read().decode().strip()
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        payload = json.loads(__import__("base64").urlsafe_b64decode(body))
        host = payload.get("affinity")
        return host if isinstance(host, str) else None
    except Exception as exc:  # noqa: BLE001
        debug(f"affinity lookup failed: {exc}")
        return None


class ConfigProxy(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, chat_port: int, on_chat_server):
        super().__init__(("127.0.0.1", 0), ConfigHandler)
        self.chat_port = chat_port
        self.on_chat_server = on_chat_server


class ConfigHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ConfigProxy

    def do_GET(self) -> None:  # noqa: N802
        debug(f"clientconfig request {self.path}")
        headers = {}
        for name in ("user-agent", "x-riot-entitlements-jwt", "authorization", "accept"):
            value = self.headers.get(name)
            if value:
                headers[name] = value

        try:
            conn = http.client.HTTPSConnection(CONFIG_HOST, timeout=30)
            conn.request("GET", self.path, headers=headers)
            response = conn.getresponse()
            body = response.read()
            status = response.status
            conn.close()
        except Exception as exc:  # noqa: BLE001
            log(f"clientconfig upstream error: {exc}")
            self.send_error(502)
            return

        debug(f"clientconfig {status} {self.path}")

        if status == 200:
            try:
                payload = json.loads(body)
                upstream = None
                if payload.get("chat.affinity.enabled"):
                    affinity = geo_affinity_host(headers.get("authorization"))
                    affinities = payload.get("chat.affinities") or {}
                    upstream = affinities.get(affinity) if affinity else None
                payload, endpoint = rewrite_config(payload, self.server.chat_port, upstream)
                body = json.dumps(payload).encode()
                if endpoint:
                    self.server.on_chat_server(*endpoint)
            except Exception as exc:  # noqa: BLE001
                log(f"clientconfig rewrite failed, forwarding untouched: {exc}")

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        debug("clientconfig POST is not forwarded")
        self.send_error(501)

    def log_message(self, fmt, *args) -> None:
        debug("clientconfig http: " + fmt % args)


# --------------------------------------------------------------- stanza plumbing


class StanzaSplitter:
    """Extract complete top-level XML elements from a byte stream.

    Handles the XMPP stream header (`<stream:stream ...>` never closes until the
    connection ends) so it is emitted immediately instead of buffered forever.
    """

    MAX_BUFFER = 4 * 1024 * 1024

    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.buffer += data
        out: list[bytes] = []
        while True:
            stanza = self._next()
            if stanza is None:
                break
            out.append(stanza)
        if len(self.buffer) > self.MAX_BUFFER:
            log("stanza buffer overflow, flushing raw bytes")
            out.append(bytes(self.buffer))
            self.buffer.clear()
        return out

    def drain(self) -> bytes:
        rest = bytes(self.buffer)
        self.buffer.clear()
        return rest

    def _next(self) -> bytes | None:
        buf = self.buffer
        stripped = 0
        while stripped < len(buf) and buf[stripped:stripped + 1].isspace():
            stripped += 1
        if stripped:
            del buf[:stripped]
        if not buf:
            return None
        if buf[0:1] != b"<":
            index = buf.find(b"<")
            if index == -1:
                return None
            del buf[:index]
            return None
        end = _scan_element(buf)
        if end is None:
            return None
        stanza = bytes(buf[:end])
        del buf[:end]
        return stanza


def _scan_element(buf: bytearray) -> int | None:
    depth = 0
    index = 0
    length = len(buf)

    while index < length:
        if buf[index:index + 1] != b"<":
            index += 1
            continue
        if buf.startswith(b"<!--", index):
            end = buf.find(b"-->", index + 4)
            if end == -1:
                return None
            index = end + 3
            continue
        if buf.startswith(b"<![CDATA[", index):
            end = buf.find(b"]]>", index + 9)
            if end == -1:
                return None
            index = end + 3
            continue
        if buf.startswith(b"<?", index) or buf.startswith(b"<!", index):
            end = buf.find(b">", index + 2)
            if end == -1:
                return None
            index = end + 1
            continue

        quote = b""
        cursor = index + 1
        while cursor < length:
            char = buf[cursor:cursor + 1]
            if quote:
                if char == quote:
                    quote = b""
            elif char in (b'"', b"'"):
                quote = char
            elif char == b">":
                break
            cursor += 1
        if cursor >= length:
            return None

        tag = bytes(buf[index:cursor + 1])
        closing = tag.startswith(b"</")
        self_closing = tag.endswith(b"/>")

        if closing:
            depth -= 1
            if depth <= 0:
                return cursor + 1
        elif self_closing:
            if depth == 0:
                return cursor + 1
        else:
            depth += 1
            if tag.startswith(b"<stream:"):
                return cursor + 1  # stream header: never self-closing
        index = cursor + 1

    return None


SECRET_ELEMENT = re.compile(
    r"<([A-Za-z0-9_.:-]*(?:token|cookie|password|secret))>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)


def debug_stanza(prefix: str, stanza: bytes) -> None:
    """Log a stanza with credentials masked; the log file outlives the session."""
    if not DEBUG:
        return
    text = stanza.decode("utf-8", "replace")
    text = SECRET_ELEMENT.sub(lambda m: f"<{m.group(1)}>REDACTED</{m.group(1)}>", text)
    debug(f"{prefix} {text[:300]}")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def find_child(element, name: str):
    if element is None:
        return None
    for child in element:
        if local_name(child.tag) == name:
            return child
    return None


def remove_child(element, name: str) -> None:
    if element is None:
        return
    child = find_child(element, name)
    if child is not None:
        element.remove(child)


for prefix, uri in (("", "jabber:client"),):
    ET.register_namespace(prefix, uri)


# ------------------------------------------------------------------- presence
GAME_PRESENCES = ("bacon", "lion", "keystone", "riot_client", "teamfighttactics", "valorant")


def rewrite_presence(raw: bytes, status: str) -> bytes | None:
    """Mask a <presence> stanza. Returns None to drop it, else the stanza to send."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        log(f"unparseable stanza forwarded as-is: {exc}")
        return raw

    if local_name(root.tag) != "presence":
        return raw

    if root.get("to") is not None:
        return raw  # lobby presence: leave it online so chat and invites keep working

    games = find_child(root, "games")
    league = find_child(games, "league_of_legends")
    league_state = find_child(league, "st")

    if status == "chat" and league_state is not None and (league_state.text or "") == "dnd":
        return raw  # in a match: keep Riot's own dnd

    show = find_child(root, "show")
    if show is not None:
        show.text = status
    if league_state is not None:
        league_state.text = status

    if status == "chat":
        return _serialize(root)

    remove_child(root, "status")

    if league is not None:
        if status == "mobile":
            remove_child(league, "p")
            remove_child(league, "m")
        else:
            games.remove(league)

    for name in GAME_PRESENCES:
        remove_child(games, name)

    return _serialize(root)


def _serialize(root) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=False)


def fake_player_roster_item(status: str) -> bytes:
    return (
        f"<item jid='{FAKE_JID}' name='&#9;Deceive Active!' subscription='both'"
        f" puuid='41c322a1-b328-495b-a004-5ccd3e45eae8'>"
        "<group priority='9999'>Deceive</group>"
        f"<state>{status}</state>"
        "<id name='&#9;Deceive Active!' tagline='...'/>"
        "<lol name='&#9;Deceive Active!'/>"
        "<platforms><riot name='&#9;Deceive Active' tagline='...'/></platforms>"
        "</item>"
    ).encode()


def fake_player_presence() -> bytes:
    stamp = int(time.time() * 1000)
    return (
        f"<presence from='{FAKE_JID}/RC-Deceive' id='b-{uuid.uuid4()}'>"
        "<games>"
        f"<keystone><st>chat</st><s.t>{stamp}</s.t><s.p>keystone</s.p><pty/></keystone>"
        f"<league_of_legends><st>chat</st><s.t>{stamp}</s.t><s.p>league_of_legends</s.p>"
        "<s.c>live</s.c><p>{&quot;pty&quot;:true}</p></league_of_legends>"
        f"<bacon><st>chat</st><s.t>{stamp}</s.t><s.l>bacon_availability_online</s.l>"
        "<s.p>bacon</s.p></bacon>"
        "</games>"
        "<show>chat</show><platform>riot</platform><status/>"
        "</presence>"
    ).encode()


def fake_player_message(text: str) -> bytes:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 1)) + ".000"
    safe = (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace("'", "&apos;")
    )
    return (
        f"<message from='{FAKE_JID}/RC-Deceive' stamp='{stamp}' id='fake-{stamp}'"
        f" type='chat'><body>{safe}</body></message>"
    ).encode()


# ------------------------------------------------------------------ connection


class ChatConnection:
    def __init__(self, client_socket, server_ssl: ssl.SSLContext, host: str, port: int,
                 owner: "Deceive") -> None:
        self.owner = owner
        self.client = client_socket
        self.server = None
        self.host = host
        self.port = port
        self.server_ssl = server_ssl
        self.write_lock = threading.Lock()
        self.last_presence: bytes | None = None
        self.inserted_fake = False
        self.sent_fake_presence = False
        self.alive = True

    # -- lifecycle
    def start(self) -> None:
        threading.Thread(target=self._handshake, daemon=True).start()

    def _handshake(self) -> None:
        try:
            incoming = self.server_ssl.wrap_socket(self.client, server_side=True)
        except ssl.SSLError as exc:
            log(f"client TLS handshake failed: {exc}")
            self.close()
            return

        try:
            raw = socket.create_connection((self.host, self.port), timeout=30)
            raw.settimeout(None)
            outgoing = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        except Exception as exc:  # noqa: BLE001
            log(f"cannot reach chat server {self.host}:{self.port}: {exc}")
            incoming.close()
            self.close()
            return

        self.client = incoming
        self.server = outgoing
        log(f"relaying chat {self.host}:{self.port}")
        self.owner.register(self)

        threading.Thread(target=self._pump_client, daemon=True).start()
        self._pump_server()

    def close(self) -> None:
        self.alive = False
        self.owner.unregister(self)
        for sock in (self.client, self.server):
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass

    # -- writes
    def to_server(self, data: bytes) -> None:
        if self.server is None:
            return
        with self.write_lock:
            try:
                self.server.sendall(data)
            except OSError as exc:
                debug(f"write to server failed: {exc}")
                self.close()

    def to_client(self, data: bytes) -> None:
        if self.client is None:
            return
        with self.write_lock:
            try:
                self.client.sendall(data)
            except OSError as exc:
                debug(f"write to client failed: {exc}")
                self.close()

    # -- pumps
    def _pump_client(self) -> None:
        splitter = StanzaSplitter()
        try:
            while self.alive:
                data = self.client.recv(65536)
                if not data:
                    break
                for stanza in splitter.feed(data):
                    self._from_client(stanza)
        except OSError as exc:
            debug(f"client pump ended: {exc}")
        finally:
            self.to_server(splitter.drain())
            self.close()

    def _pump_server(self) -> None:
        splitter = StanzaSplitter()
        try:
            while self.alive:
                data = self.server.recv(65536)
                if not data:
                    break
                for stanza in splitter.feed(data):
                    self._from_server(stanza)
        except OSError as exc:
            debug(f"server pump ended: {exc}")
        finally:
            self.to_client(splitter.drain())
            self.close()

    def _from_client(self, stanza: bytes) -> None:
        debug_stanza("RC> ", stanza)
        owner = self.owner

        if b"<presence" in stanza and owner.enabled:
            if local_name(_peek_tag(stanza)) == "presence" and not DIRECTED_PRESENCE.match(stanza):
                self.last_presence = stanza
            rewritten = rewrite_presence(stanza, owner.status)
            if rewritten is None:
                debug("dropped presence (lobby chat disabled)")
                return
            self.to_server(rewritten)
            return

        if FAKE_JID_BYTES in stanza:
            if b"<body>" in stanza:
                owner.handle_chat_command(_extract_body(stanza))
            debug("dropped stanza addressed to the fake player")
            return

        self.to_server(stanza)

    def _from_server(self, stanza: bytes) -> None:
        debug_stanza("SRV>", stanza)
        if not self.inserted_fake and ROSTER_MARKER in stanza:
            index = stanza.find(ROSTER_MARKER) + len(ROSTER_MARKER)
            patched = (
                stanza[:index]
                + fake_player_roster_item(self.owner.status)
                + stanza[index:]
            )
            self.inserted_fake = True
            self.to_client(patched)
            if not self.sent_fake_presence:
                self.sent_fake_presence = True
                self.to_client(fake_player_presence())
            self.owner.send_to_client(fake_player_message(
                f"Deceive is active, you are appearing {self.owner.status}. "
                "Send online / offline / mobile / enable / disable / status here."
            ))
            return
        self.to_client(stanza)

    # -- status propagation
    def resend_presence(self) -> None:
        if self.last_presence is None:
            return
        rewritten = rewrite_presence(self.last_presence, self.owner.status)
        if rewritten is not None:
            self.to_server(rewritten)


def _peek_tag(stanza: bytes) -> str:
    try:
        return stanza[:200].split(b">", 1)[0].decode("utf-8", "ignore").lstrip("<").split()[0]
    except IndexError:
        return ""


def _extract_body(stanza: bytes) -> str:
    try:
        root = ET.fromstring(stanza)
        body = find_child(root, "body")
        return (body.text or "") if body is not None else ""
    except ET.ParseError:
        return ""


# ---------------------------------------------------------------------- app


class Deceive:
    def __init__(self, status: str = "offline") -> None:
        self.status = status
        self.enabled = True
        self.state_lock = threading.Lock()
        self.connections: list[ChatConnection] = []
        self.chat_endpoint: tuple[str, int] | None = None

    # -- connections registry
    def register(self, connection: ChatConnection) -> None:
        with self.state_lock:
            self.connections.append(connection)

    def unregister(self, connection: ChatConnection) -> None:
        with self.state_lock:
            if connection in self.connections:
                self.connections.remove(connection)

    def send_to_client(self, data: bytes) -> None:
        with self.state_lock:
            targets = list(self.connections)
        for connection in targets:
            connection.to_client(data)

    # -- status
    def set_status(self, new_status: str) -> None:
        with self.state_lock:
            self.status = new_status
            self.enabled = True
            targets = list(self.connections)
            self.save_status()
        for connection in targets:
            connection.resend_presence()
        self.send_to_client(fake_player_message(
            f"You are now appearing {'online' if new_status == 'chat' else new_status}."
        ))
        log(f"status set to {new_status}")

    def set_enabled(self, enabled: bool) -> None:
        with self.state_lock:
            self.enabled = enabled
            targets = list(self.connections)
        for connection in targets:
            connection.resend_presence()
        self.send_to_client(fake_player_message(
            "Deceive is now enabled." if enabled else "Deceive is now disabled."
        ))
        log(f"enabled={enabled}")

    def handle_chat_command(self, text: str) -> None:
        lowered = text.lower()
        if "offline" in lowered:
            self.set_status("offline")
        elif "mobile" in lowered:
            self.set_status("mobile")
        elif "online" in lowered:
            self.set_status("chat")
        elif "enable" in lowered:
            self.set_enabled(True)
        elif "disable" in lowered:
            self.set_enabled(False)
        elif "status" in lowered:
            self.send_to_client(fake_player_message(
                f"Deceive is {'enabled' if self.enabled else 'disabled'}, "
                f"appearing {self.status}."
            ))

    def save_status(self) -> None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            (CACHE_DIR / "status").write_text(self.status)
        except OSError:
            pass

    # -- chat server discovery
    def on_chat_server(self, host: str, port: int) -> None:
        if self.chat_endpoint != (host, port):
            log(f"real chat server is {host}:{port}")
        self.chat_endpoint = (host, port)

    # -- control endpoint
    def make_control_server(self) -> http.server.ThreadingHTTPServer:
        deceive = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802
                from urllib.parse import parse_qs, urlparse

                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                if parsed.path == "/status" and "set" in query:
                    value = query["set"][0]
                    if value in ("offline", "mobile", "chat"):
                        deceive.set_status(value)
                elif parsed.path == "/enabled" and "set" in query:
                    deceive.set_enabled(query["set"][0].lower() not in ("0", "false", "no"))
                elif parsed.path not in ("/", "/status", "/enabled"):
                    self.send_error(404)
                    return
                body = json.dumps({
                    "status": deceive.status,
                    "enabled": deceive.enabled,
                    "chat": deceive.chat_endpoint,
                    "connections": len(deceive.connections),
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        return server

    # -- relay
    def serve_chat(self, listener: socket.socket, tls: ssl.SSLContext) -> None:
        log(f"chat relay listening on 127.0.0.1:{listener.getsockname()[1]}")
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            if self.chat_endpoint is None:
                client.close()
                continue
            host, port = self.chat_endpoint
            ChatConnection(client, tls, host, port, self).start()


# ------------------------------------------------------------------- launcher


def riot_client_path() -> Path:
    candidates = []
    if RIOT_CLIENT_INSTALLS.exists():
        try:
            data = json.loads(RIOT_CLIENT_INSTALLS.read_text())
            for key in ("rc_live", "rc_default", "rc_beta"):
                if data.get(key):
                    candidates.append(Path(data[key]))
        except Exception as exc:  # noqa: BLE001
            log(f"cannot read RiotClientInstalls.json: {exc}")
    candidates += [
        RIOT_CLIENT_FALLBACK,
        Path("/Users/Shared/Riot Games/Riot Client.app/Contents/MacOS/RiotClientServices"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise SystemExit("Riot Client not found; launch a Riot game once, then retry")


def riot_client_running() -> bool:
    try:
        out = subprocess.run(["pgrep", "-f", "RiotClientServices"], capture_output=True, text=True)
        return out.returncode == 0
    except OSError:
        return False


class Session:
    """One Deceive run: client-config proxy, chat relay, control endpoint."""

    def __init__(self, status: str = "offline", debug: bool = False) -> None:
        global DEBUG, _log_file
        DEBUG = debug
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if _log_file is None:
            _log_file = open(LOG_PATH, "a", buffering=1)  # noqa: SIM115
        self.deceive = Deceive(status)
        self.config_url = ""
        self.control_port = 0
        self.cert_expiry = 0.0
        self._listener: socket.socket | None = None
        self._config: ConfigProxy | None = None
        self._control: http.server.ThreadingHTTPServer | None = None
        self._process: subprocess.Popen | None = None

    # -- lifecycle
    def start(self) -> tuple[str, int]:
        tls, expiry = load_certificate()
        self.cert_expiry = expiry
        log(f"proxy certificate valid until {time.strftime('%Y-%m-%d', time.localtime(expiry))}")

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        self._listener = listener

        config = ConfigProxy(listener.getsockname()[1], self.deceive.on_chat_server)
        threading.Thread(target=config.serve_forever, daemon=True).start()
        self._config = config
        self.config_url = f"http://127.0.0.1:{config.server_address[1]}"

        threading.Thread(target=self.deceive.serve_chat, args=(listener, tls),
                         daemon=True).start()

        control = self.deceive.make_control_server()
        threading.Thread(target=control.serve_forever, daemon=True).start()
        self._control = control
        self.control_port = control.server_address[1]
        log(f"control endpoint http://127.0.0.1:{self.control_port}/")
        return self.config_url, self.control_port

    def stop(self) -> None:
        for server in (self._config, self._control):
            if server is not None:
                try:
                    server.shutdown()
                    server.server_close()
                except Exception:  # noqa: BLE001
                    pass
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None

    # -- Riot Client
    def launch(self, game: str = "rc") -> bool:
        binary = riot_client_path()
        product = PRODUCTS[game]
        arguments = [f"--client-config-url={self.config_url}"]
        if product:
            arguments += [f"--launch-product={product}",
                          "--launch-patchline=live", "--allow-direct-launch"]
        log(f"launching {binary.name} ({game}, status {self.deceive.status})")
        try:
            self._process = subprocess.Popen(
                [str(binary), *arguments],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            log(f"cannot start the Riot Client: {exc}")
            return False
        return True

    def client_running(self) -> bool:
        return riot_client_running()

    def quit_client(self) -> None:
        try:
            subprocess.run(["pkill", "-f", "RiotClientServices"], check=False,
                           capture_output=True)
        except OSError:
            pass

    def wait_for_client(self, timeout: float = 45) -> bool:
        deadline = time.time() + timeout
        while not riot_client_running():
            if time.time() > deadline:
                log("Riot Client did not start")
                return False
            time.sleep(1)
        log("Riot Client is up")
        return True

    def wait_for_exit(self, poll: float = 2) -> None:
        while riot_client_running():
            time.sleep(poll)
        log("Riot Client exited")


def main() -> int:
    parser = argparse.ArgumentParser(prog="deceive",
                                    description="Appear offline in Riot games on macOS.")
    parser.add_argument("game", nargs="?", default="rc", choices=list(PRODUCTS),
                        help="which game to launch (default: the Riot Client only)")
    parser.add_argument("--status", default=None, choices=["chat", "offline", "mobile"],
                        help="presence to mask with (default: last used, else offline)")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    session = Session(args.status or saved_status(), debug=args.debug)
    session.start()
    log(f"deceive starting, log at {LOG_PATH}")

    if not _domain_resolves():
        log(f"WARNING: {LOCALHOST_DOMAIN} does not resolve to 127.0.0.1 on this network")

    if riot_client_running():
        log("Riot Client already running; close it first")
        session.stop()
        return 2

    if not session.launch(args.game):
        session.stop()
        return 3
    if not session.wait_for_client():
        session.stop()
        return 4
    try:
        session.wait_for_exit()
    except KeyboardInterrupt:
        log("interrupted")
    log("stopping")
    session.stop()
    return 0


def saved_status() -> str:
    path = CACHE_DIR / "status"
    saved = path.read_text().strip() if path.exists() else ""
    return saved if saved in ("chat", "offline", "mobile") else "offline"


def _domain_resolves() -> bool:
    try:
        return any(address[4][0] == "127.0.0.1" for address in socket.getaddrinfo(LOCALHOST_DOMAIN, 443))
    except OSError:
        return False


# ------------------------------------------------------------------- selftest


def selftest() -> int:
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        print(f"{'ok  ' if condition else 'FAIL'} {name}" + (f" ({detail})" if not condition else ""))
        if not condition:
            failures.append(name)

    presence = (
        b"<presence id='x'><show>chat</show><status>hi</status>"
        b"<games><league_of_legends><st>dnd</st><s.t>1</s.t><s.p>league_of_legends</s.p>"
        b"<p>eyJ</p><m>2</m></league_of_legends><keystone><st>chat</st></keystone>"
        b"<bacon><st>chat</st></bacon></games><platform>riot</platform></presence>"
    )

    offline = rewrite_presence(presence, "offline")
    check("offline rewrites show", b"<show>offline</show>" in offline, offline)
    check("offline drops status", b"<status>" not in offline)
    check("offline drops league", b"league_of_legends" not in offline)
    check("offline drops bacon", b"<bacon" not in offline)
    check("offline drops keystone", b"<keystone" not in offline)
    check("offline keeps presence", offline.startswith(b"<presence"))

    in_game = rewrite_presence(presence, "chat")
    check("chat keeps dnd presence untouched", in_game == presence)

    mobile = rewrite_presence(presence, "mobile")
    check("mobile keeps league", b"league_of_legends" in mobile, mobile)
    check("mobile drops payload p", b"<p>eyJ</p>" not in mobile, mobile)
    check("mobile drops payload m", b"<m>2</m>" not in mobile, mobile)
    check("mobile sets show", b"<show>mobile</show>" in mobile)

    lobby = b"<presence to='lobby@conference.x'><show>chat</show></presence>"
    check("lobby presence stays untouched", rewrite_presence(lobby, "offline") == lobby)

    splitter = StanzaSplitter()
    header = b"<stream:stream xmlns='jabber:client' xmlns:stream='http://etherx.jabber.org/streams'>"
    out = splitter.feed(header + b"<presence id='a'><show>ch")
    check("splitter emits stream header", out == [header], out)
    out = splitter.feed(b"at</show></presence><iq id='2'/>")
    check("splitter reassembles fragmented presence",
          out == [b"<presence id='a'><show>chat</show></presence>", b"<iq id='2'/>"], out)
    check("splitter leaves nothing buffered", splitter.drain() == b"")
    out = splitter.feed(b"<message><body>it's <b>fine</b></body>")
    check("splitter waits for tag close", out == [], out)
    out = splitter.feed(b"</message><unclosed>")
    check("splitter tolerates apostrophes in text",
          out == [b"<message><body>it's <b>fine</b></body></message>"], out)

    payload = {
        "chat.host": "chat.na2.lol.riotgames.com",
        "chat.port": 5223,
        "chat.affinities": {"na": "chat.na2.lol.riotgames.com", "eu": "chat.eu.lol.riotgames.com"},
        "chat.affinity.enabled": True,
        "lcds": {"keep": 1},
    }
    rewritten, endpoint = rewrite_config(payload, 4711, "chat.eu.lol.riotgames.com")
    check("config host rewritten", rewritten["chat.host"] == LOCALHOST_DOMAIN, rewritten["chat.host"])
    check("config port rewritten", rewritten["chat.port"] == 4711)
    check("config affinities rewritten",
          set(rewritten["chat.affinities"].values()) == {LOCALHOST_DOMAIN})
    check("config endpoint uses affinity", endpoint == ("chat.eu.lol.riotgames.com", 5223), endpoint)
    check("config keeps other keys", rewritten["lcds"] == {"keep": 1})

    roster = b"<iq type='result'><query xmlns='jabber:iq:riotgames:roster'><item jid='a@b'/></query></iq>"
    index = roster.find(ROSTER_MARKER) + len(ROSTER_MARKER)
    patched = roster[:index] + fake_player_roster_item("offline") + roster[index:]
    check("roster insert lands inside query", patched.count(b"<item") == 2, patched)
    check("roster insert parses as XML", _parses(patched))
    check("fake presence parses", _parses(fake_player_presence()))
    check("fake message escapes body",
          b"&lt;b&gt;" in fake_player_message("<b>") and _parses(fake_player_message("<b>")))

    print()
    if failures:
        print(f"{len(failures)} failure(s): {', '.join(failures)}")
        return 1
    print("all selftests passed")
    return 0


def _parses(data: bytes) -> bool:
    try:
        ET.fromstring(data)
        return True
    except ET.ParseError:
        return False


if __name__ == "__main__":
    sys.exit(main())
