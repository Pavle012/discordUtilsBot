"""Direct Minecraft Java 'Server List Ping' (no third-party API)."""
import asyncio
import contextlib
import json
import logging
import struct
import time
from dataclasses import dataclass, field

try:
    import dns.asyncresolver
    import dns.exception

    HAVE_DNS = True
except ImportError:  # dnspython is optional, but needed if the host uses an SRV record
    HAVE_DNS = False

log = logging.getLogger("assembly_bot.mcping")

PROTOCOL_VERSION = 47  # any value works for status queries
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SRV_CACHE_SECONDS = 300


@dataclass(slots=True)
class ServerStatus:
    online: bool
    players: int = 0
    max_players: int | None = None
    names: list[str] = field(default_factory=list)
    latency_ms: float | None = None


def _pack_varint(value: int) -> bytes:
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _pack_string(text: str) -> bytes:
    data = text.encode("utf-8")
    return _pack_varint(len(data)) + data


async def _read_varint(reader: asyncio.StreamReader) -> int:
    value = 0
    for position in range(0, 35, 7):
        byte = (await reader.readexactly(1))[0]
        value |= (byte & 0x7F) << position
        if not byte & 0x80:
            return value
    raise ValueError("VarInt is too large")


def _parse_varint(buf: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for position in range(0, 35, 7):
        if offset >= len(buf):
            raise ValueError("Truncated VarInt")
        byte = buf[offset]
        offset += 1
        value |= (byte & 0x7F) << position
        if not byte & 0x80:
            return value, offset
    raise ValueError("VarInt is too large")


_srv_cache: dict[str, tuple[float, tuple[str, int]]] = {}


async def resolve_srv(host: str, port: int) -> tuple[str, int]:
    if not HAVE_DNS:
        return host, port
    cached = _srv_cache.get(host)
    if cached and time.monotonic() - cached[0] < SRV_CACHE_SECONDS:
        return cached[1]

    result = (host, port)
    try:
        answers = await dns.asyncresolver.resolve(f"_minecraft._tcp.{host}", "SRV", lifetime=3)
        best = min(answers, key=lambda r: (r.priority, -r.weight))
        result = (str(best.target).rstrip("."), int(best.port))
    except dns.exception.DNSException:
        pass
    _srv_cache[host] = (time.monotonic(), result)
    return result


async def _query(host: str, port: int) -> ServerStatus:
    started = time.perf_counter()
    reader, writer = await asyncio.open_connection(host, port)
    try:
        handshake = (
            _pack_varint(0x00)
            + _pack_varint(PROTOCOL_VERSION)
            + _pack_string(host)
            + struct.pack(">H", port)
            + _pack_varint(1)  # next state: status
        )
        writer.write(_pack_varint(len(handshake)) + handshake)
        writer.write(_pack_varint(1) + _pack_varint(0x00))  # status request
        await writer.drain()

        length = await _read_varint(reader)
        if not 0 < length <= MAX_RESPONSE_BYTES:
            raise ValueError(f"Bad packet length {length}")
        payload = await reader.readexactly(length)
        latency = (time.perf_counter() - started) * 1000
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    packet_id, offset = _parse_varint(payload, 0)
    if packet_id != 0x00:
        raise ValueError(f"Unexpected packet id {packet_id}")
    str_len, offset = _parse_varint(payload, offset)
    data = json.loads(payload[offset : offset + str_len].decode("utf-8"))

    players = data.get("players") if isinstance(data, dict) else None
    if not isinstance(players, dict):
        return ServerStatus(online=True, latency_ms=latency)

    names: list[str] = []
    sample = players.get("sample")
    if isinstance(sample, list):
        for entry in sample:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                names.append(entry["name"])

    max_players = players.get("max")
    return ServerStatus(
        online=True,
        players=int(players.get("online", 0) or 0),
        max_players=int(max_players) if isinstance(max_players, int) else None,
        names=names,
        latency_ms=latency,
    )


async def ping_server(host: str, port: int, timeout: float) -> ServerStatus:
    """Never raises: any failure is reported as an offline server."""
    try:
        real_host, real_port = await resolve_srv(host, port)
        return await asyncio.wait_for(_query(real_host, real_port), timeout=timeout)
    except (OSError, EOFError, ValueError, TimeoutError, asyncio.TimeoutError) as e:
        log.debug("Ping to %s:%s failed: %r", host, port, e)
        return ServerStatus(online=False)
