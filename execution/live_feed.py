"""Strict full-session MoldUDP64 receiver policy; no historical-file source."""
from __future__ import annotations

import hashlib
import ipaddress
import socket
from collections import OrderedDict
from datetime import datetime
from zoneinfo import ZoneInfo


class FeedFault(RuntimeError):
    pass


class SessionSequencer:
    def __init__(self, session: bytes, session_date: str, max_age_ns: int, future_tolerance_ns=5_000_000):
        if len(session) != 10 or not session_date:
            raise FeedFault("An exact 10-byte session and exchange trading date are required.")
        self.session = session
        self.session_date = session_date
        self.max_age_ns = max_age_ns
        self.future_tolerance_ns = future_tolerance_ns
        self.expected = 1
        self.started = False
        self.faulted = False
        self.recent: OrderedDict[int, bytes] = OrderedDict()

    def _fault(self, message: str):
        self.faulted = True
        raise FeedFault(message)

    def consume(self, packet: bytes, now: datetime | None = None) -> list[bytes]:
        if self.faulted:
            raise FeedFault("Feed is faulted; automatic sequence recovery is disabled.")
        if len(packet) < 20 or packet[:10] != self.session:
            self._fault("Unexpected or malformed MoldUDP64 session.")
        first = int.from_bytes(packet[10:18], "big")
        count = int.from_bytes(packet[18:20], "big")
        if first > self.expected:
            self._fault("ITCH sequence gap; startup requires session sequence 1.")
        if count == 0xFFFF:
            self._fault("MoldUDP64 session ended.")
        if count == 0:
            if len(packet) != 20:
                self._fault("Heartbeat contains unexpected payload.")
            return []
        messages = []
        offset = 20
        for index in range(count):
            if offset + 2 > len(packet):
                self._fault("Truncated MoldUDP64 message length.")
            size = int.from_bytes(packet[offset:offset + 2], "big")
            offset += 2
            if size < 11 or offset + size > len(packet):
                self._fault("Invalid ITCH message size.")
            raw = packet[offset:offset + size]
            offset += size
            messages.append((first + index, raw))
        if offset != len(packet):
            self._fault("Unexpected MoldUDP64 trailing bytes.")

        current = (now or datetime.now(ZoneInfo("America/New_York"))).astimezone(ZoneInfo("America/New_York"))
        if current.date().isoformat() != self.session_date:
            self._fault("ITCH session date does not match the current exchange day.")
        clock_ns = ((current.hour * 60 + current.minute) * 60 + current.second) * 1_000_000_000
        clock_ns += current.microsecond * 1000
        committed = []
        for sequence, raw in messages:
            digest = hashlib.blake2b(raw, digest_size=16).digest()
            if sequence < self.expected:
                if sequence in self.recent and self.recent[sequence] != digest:
                    self._fault("Conflicting duplicate market message.")
                continue
            if not self.started:
                if sequence != 1 or len(raw) != 12 or raw[0:1] != b"S" or raw[11:12] != b"O":
                    self._fault("Full live feed must start with session message 1, System Event O.")
                self.started = True
            timestamp = int.from_bytes(raw[5:11], "big")
            age = clock_ns - timestamp
            if age > self.max_age_ns or age < -self.future_tolerance_ns:
                self._fault("Source timestamp stale or clock skew exceeds the configured bound.")
            self.recent[sequence] = digest
            if len(self.recent) > 4096:
                self.recent.popitem(last=False)
            committed.append(raw)
            self.expected += 1
        return committed


def open_multicast(group: str, port: int, interface: str, source: str) -> socket.socket:
    if not ipaddress.IPv4Address(group).is_multicast:
        raise FeedFault("ITCH_GROUP must be an IPv4 multicast address.")
    ipaddress.IPv4Address(interface)
    ipaddress.IPv4Address(source)
    if not 1 <= port <= 65535:
        raise FeedFault("Invalid multicast port.")
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        receiver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        receiver.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        receiver.bind(("", port))
        membership = socket.inet_aton(group) + socket.inet_aton(interface)
        receiver.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        receiver.setblocking(False)
        return receiver
    except BaseException:
        receiver.close()
        raise
