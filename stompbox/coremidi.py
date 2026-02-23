"""Direct CoreMIDI binding via ctypes for macOS.

Provides real MIDI input without python-rtmidi dependency.
Uses Apple's CoreMIDI framework through ctypes/objc bridge.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import struct
import sys
import threading
import time
from typing import Callable, Optional

if sys.platform != "darwin":
    raise ImportError("CoreMIDI is only available on macOS")


# --- Load frameworks ---

_cm = ctypes.cdll.LoadLibrary(
    ctypes.util.find_library("CoreMIDI")  # type: ignore[arg-type]
)
_cf = ctypes.cdll.LoadLibrary(
    ctypes.util.find_library("CoreFoundation")  # type: ignore[arg-type]
)

# --- CoreFoundation types ---

CFStringRef = ctypes.c_void_p
CFAllocatorRef = ctypes.c_void_p
kCFAllocatorDefault = CFAllocatorRef(None)
kCFStringEncodingUTF8 = 0x08000100

_cf.CFStringCreateWithCString.restype = CFStringRef
_cf.CFStringCreateWithCString.argtypes = [CFAllocatorRef, ctypes.c_char_p, ctypes.c_uint32]

_cf.CFStringGetCString.restype = ctypes.c_bool
_cf.CFStringGetCString.argtypes = [CFStringRef, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]

_cf.CFStringGetLength.restype = ctypes.c_long
_cf.CFStringGetLength.argtypes = [CFStringRef]

_cf.CFRelease.restype = None
_cf.CFRelease.argtypes = [ctypes.c_void_p]


def _cfstr(s: str) -> CFStringRef:
    return _cf.CFStringCreateWithCString(kCFAllocatorDefault, s.encode("utf-8"), kCFStringEncodingUTF8)


def _cfstr_to_py(cfstr: CFStringRef) -> str:
    if not cfstr:
        return ""
    buf = ctypes.create_string_buffer(1024)
    _cf.CFStringGetCString(cfstr, buf, 1024, kCFStringEncodingUTF8)
    return buf.value.decode("utf-8", errors="replace")


# --- CoreMIDI types ---

MIDIClientRef = ctypes.c_uint32
MIDIPortRef = ctypes.c_uint32
MIDIEndpointRef = ctypes.c_uint32
MIDIObjectRef = ctypes.c_uint32
OSStatus = ctypes.c_int32
MIDINotificationPtr = ctypes.c_void_p
ItemCount = ctypes.c_ulong

# MIDIPacket struct (variable length)
# timestamp: UInt64, length: UInt16, data: [UInt8]


# --- CoreMIDI function signatures ---

# Client creation
_cm.MIDIClientCreate.restype = OSStatus
_cm.MIDIClientCreate.argtypes = [CFStringRef, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(MIDIClientRef)]

# Port creation (old API for compat, works on all macOS versions)
_MIDI_READ_PROC = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)

_cm.MIDIInputPortCreate.restype = OSStatus
_cm.MIDIInputPortCreate.argtypes = [MIDIClientRef, CFStringRef, _MIDI_READ_PROC, ctypes.c_void_p, ctypes.POINTER(MIDIPortRef)]

# Source enumeration
_cm.MIDIGetNumberOfSources.restype = ItemCount
_cm.MIDIGetNumberOfSources.argtypes = []

_cm.MIDIGetSource.restype = MIDIEndpointRef
_cm.MIDIGetSource.argtypes = [ItemCount]

# Connection
_cm.MIDIPortConnectSource.restype = OSStatus
_cm.MIDIPortConnectSource.argtypes = [MIDIPortRef, MIDIEndpointRef, ctypes.c_void_p]

_cm.MIDIPortDisconnectSource.restype = OSStatus
_cm.MIDIPortDisconnectSource.argtypes = [MIDIPortRef, MIDIEndpointRef]

# Endpoint properties
_cm.MIDIObjectGetStringProperty.restype = OSStatus
_cm.MIDIObjectGetStringProperty.argtypes = [MIDIObjectRef, CFStringRef, ctypes.POINTER(CFStringRef)]

# Property key
kMIDIPropertyDisplayName = _cfstr("displayName")
kMIDIPropertyName = _cfstr("name")

# Disposal
_cm.MIDIClientDispose.restype = OSStatus
_cm.MIDIClientDispose.argtypes = [MIDIClientRef]

_cm.MIDIPortDispose.restype = OSStatus
_cm.MIDIPortDispose.argtypes = [MIDIPortRef]


def _endpoint_name(endpoint: MIDIEndpointRef) -> str:
    name_ref = CFStringRef()
    status = _cm.MIDIObjectGetStringProperty(endpoint, kMIDIPropertyDisplayName, ctypes.byref(name_ref))
    if status != 0:
        status = _cm.MIDIObjectGetStringProperty(endpoint, kMIDIPropertyName, ctypes.byref(name_ref))
    if status != 0:
        return f"Source {endpoint}"
    result = _cfstr_to_py(name_ref)
    _cf.CFRelease(name_ref)
    return result


def list_input_ports() -> list[str]:
    """List available MIDI input port names."""
    count = _cm.MIDIGetNumberOfSources()
    names = []
    for i in range(count):
        ep = _cm.MIDIGetSource(i)
        names.append(_endpoint_name(ep))
    return names


# --- MIDI message parsing ---

def _parse_packets(packet_list_ptr: ctypes.c_void_p) -> list[tuple[int, bytes]]:
    """Parse a MIDIPacketList into (timestamp, raw_bytes) tuples."""
    # MIDIPacketList: numPackets (UInt32), then packets
    # MIDIPacket: timeStamp (UInt64), length (UInt16), data[256] (variable)
    base = ctypes.cast(packet_list_ptr, ctypes.POINTER(ctypes.c_ubyte))
    buf = (ctypes.c_ubyte * 65536).from_address(ctypes.addressof(base.contents))

    num_packets = struct.unpack_from("<I", bytes(buf[0:4]))[0]
    offset = 4
    results = []

    for _ in range(num_packets):
        if offset + 10 > len(buf):
            break
        timestamp = struct.unpack_from("<Q", bytes(buf[offset : offset + 8]))[0]
        length = struct.unpack_from("<H", bytes(buf[offset + 8 : offset + 10]))[0]
        data = bytes(buf[offset + 10 : offset + 10 + length])
        results.append((timestamp, data))
        # Align to next packet (packets are not padded in macOS)
        offset += 10 + length

    return results


# --- Public MIDI input class ---

# MIDI status byte masks
_STATUS_NOTE_OFF = 0x80
_STATUS_NOTE_ON = 0x90
_STATUS_POLY_PRESSURE = 0xA0
_STATUS_CC = 0xB0
_STATUS_PROGRAM_CHANGE = 0xC0
_STATUS_CHANNEL_PRESSURE = 0xD0
_STATUS_PITCH_BEND = 0xE0


class MidiMessage:
    """Parsed MIDI message."""

    __slots__ = ("kind", "channel", "data1", "data2", "raw", "timestamp")

    def __init__(self, kind: str, channel: int, data1: int, data2: int, raw: bytes, timestamp: float):
        self.kind = kind
        self.channel = channel
        self.data1 = data1
        self.data2 = data2
        self.raw = raw
        self.timestamp = timestamp

    def __repr__(self) -> str:
        return f"MidiMessage({self.kind}, ch={self.channel}, d1={self.data1}, d2={self.data2})"

    @classmethod
    def parse(cls, data: bytes, ts: float) -> Optional["MidiMessage"]:
        if not data:
            return None
        status = data[0]
        if status < 0x80:
            return None  # Running status not handled in this simple parser

        kind_byte = status & 0xF0
        channel = status & 0x0F

        if kind_byte == _STATUS_NOTE_OFF and len(data) >= 3:
            return cls("note_off", channel, data[1], data[2], data, ts)
        if kind_byte == _STATUS_NOTE_ON and len(data) >= 3:
            if data[2] == 0:
                return cls("note_off", channel, data[1], 0, data, ts)
            return cls("note_on", channel, data[1], data[2], data, ts)
        if kind_byte == _STATUS_CC and len(data) >= 3:
            return cls("cc", channel, data[1], data[2], data, ts)
        if kind_byte == _STATUS_PROGRAM_CHANGE and len(data) >= 2:
            return cls("pc", channel, data[1], 0, data, ts)
        if kind_byte == _STATUS_PITCH_BEND and len(data) >= 3:
            value = data[1] | (data[2] << 7)
            return cls("pitch_bend", channel, value, 0, data, ts)
        if kind_byte == _STATUS_POLY_PRESSURE and len(data) >= 3:
            return cls("poly_pressure", channel, data[1], data[2], data, ts)
        if kind_byte == _STATUS_CHANNEL_PRESSURE and len(data) >= 2:
            return cls("channel_pressure", channel, data[1], 0, data, ts)

        return cls("other", 0, data[0], 0, data, ts)


class CoreMidiInput:
    """Real-time MIDI input using macOS CoreMIDI.

    Receives messages from one or all MIDI sources and dispatches them
    to a callback on a CoreMIDI background thread.
    """

    def __init__(self, port_name: Optional[str] = None, channel: Optional[int] = None):
        self._target_port = port_name
        self._filter_channel = channel  # 1-based, None = omni
        self._client = MIDIClientRef(0)
        self._port = MIDIPortRef(0)
        self._connected_sources: list[MIDIEndpointRef] = []
        self._callback: Optional[Callable[[MidiMessage], None]] = None
        self._running = False

        # Must keep a reference to the ctypes callback to prevent GC
        self._c_callback = _MIDI_READ_PROC(self._read_proc)

    def start(self, callback: Callable[[MidiMessage], None]) -> Optional[str]:
        """Start receiving MIDI. Returns connected port name or None."""
        self._callback = callback

        name_cfstr = _cfstr("Stompbox")
        status = _cm.MIDIClientCreate(name_cfstr, None, None, ctypes.byref(self._client))
        _cf.CFRelease(name_cfstr)
        if status != 0:
            return None

        port_cfstr = _cfstr("Input")
        status = _cm.MIDIInputPortCreate(
            self._client, port_cfstr, self._c_callback, None, ctypes.byref(self._port)
        )
        _cf.CFRelease(port_cfstr)
        if status != 0:
            _cm.MIDIClientDispose(self._client)
            return None

        connected_name = self._connect_sources()
        self._running = True
        return connected_name

    def _connect_sources(self) -> Optional[str]:
        """Connect to matching MIDI source(s). Returns first connected name."""
        count = _cm.MIDIGetNumberOfSources()
        if count == 0:
            return None

        connected_name = None

        for i in range(count):
            ep = _cm.MIDIGetSource(i)
            name = _endpoint_name(ep)

            # If no target specified, or target matches
            connect = False
            if self._target_port is None or self._target_port.lower() == "all":
                connect = True
            elif self._target_port.lower() in name.lower():
                connect = True

            if connect:
                status = _cm.MIDIPortConnectSource(self._port, ep, None)
                if status == 0:
                    self._connected_sources.append(ep)
                    if connected_name is None:
                        connected_name = name

        return connected_name

    def _read_proc(self, packet_list: ctypes.c_void_p, _read_proc_ref: ctypes.c_void_p, _src_conn_ref: ctypes.c_void_p) -> None:
        """CoreMIDI read callback — runs on CoreMIDI's thread."""
        if not self._callback:
            return

        now = time.monotonic()
        try:
            packets = _parse_packets(packet_list)
        except Exception:
            return

        for _ts, data in packets:
            msg = MidiMessage.parse(data, now)
            if msg is None:
                continue
            if self._filter_channel is not None and hasattr(msg, "channel"):
                if msg.channel != self._filter_channel - 1:
                    continue
            try:
                self._callback(msg)
            except Exception:
                pass

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False

        for ep in self._connected_sources:
            _cm.MIDIPortDisconnectSource(self._port, ep)
        self._connected_sources.clear()

        if self._port.value:
            _cm.MIDIPortDispose(self._port)
            self._port = MIDIPortRef(0)
        if self._client.value:
            _cm.MIDIClientDispose(self._client)
            self._client = MIDIClientRef(0)

        self._callback = None

    def __del__(self) -> None:
        self.stop()
