"""Lock-free audio level metering with peak hold."""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field


FLOOR_DB = -96.0


class Meter:
    """Single-channel level meter with peak hold and decay.

    Written from the audio thread (push), read from the TUI thread (read).
    No locks — relies on CPython's GIL for atomic float writes.
    """

    HOLD_TIME = 1.5  # seconds before peak starts falling
    FALL_RATE = 15.0  # dB/second fall rate

    __slots__ = ("_level", "_peak", "_peak_time")

    def __init__(self) -> None:
        self._level: float = FLOOR_DB
        self._peak: float = FLOOR_DB
        self._peak_time: float = 0.0

    def push(self, linear_peak: float) -> None:
        """Push a new peak sample value (linear amplitude). Audio-thread safe."""
        if linear_peak <= 0.0:
            db = FLOOR_DB
        else:
            db = 20.0 * math.log10(linear_peak)
            db = max(db, FLOOR_DB)
        self._level = db
        if db > self._peak:
            self._peak = db
            self._peak_time = time.monotonic()

    def read(self) -> tuple[float, float]:
        """Return (level_db, peak_db) with lazy peak decay."""
        level = self._level
        peak = self._peak
        now = time.monotonic()
        elapsed = now - self._peak_time
        if elapsed > self.HOLD_TIME:
            fallen = peak - self.FALL_RATE * (elapsed - self.HOLD_TIME)
            peak = max(fallen, level, FLOOR_DB)
            if fallen <= level:
                self._peak = level
                self._peak_time = now
        return level, peak

    def reset_peak(self) -> None:
        self._peak = self._level
        self._peak_time = time.monotonic()


@dataclass
class MidiEvent:
    """A timestamped MIDI message for the log."""

    timestamp: float
    kind: str  # "cc", "note_on", "note_off", "pc", "other"
    channel: int = 0
    data1: int = 0
    data2: int = 0

    def describe(self) -> str:
        if self.kind == "cc":
            return f"CC#{self.data1}={self.data2}"
        if self.kind == "note_on":
            return f"{_note_name(self.data1)} vel={self.data2}"
        if self.kind == "note_off":
            return f"{_note_name(self.data1)} off"
        if self.kind == "pc":
            return f"PC#{self.data1}"
        return f"0x{self.data1:02X}"

    @property
    def channel_display(self) -> str:
        return f"Ch{self.channel + 1}"


def _note_name(note: int) -> str:
    names = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
    octave = (note // 12) - 1
    return f"{names[note % 12]}{octave}"


@dataclass
class MeterBridge:
    """Aggregates all metering data for the engine."""

    input_meter: Meter = field(default_factory=Meter)
    output_meter: Meter = field(default_factory=Meter)
    midi_log: deque = field(default_factory=lambda: deque(maxlen=64))

    def record_midi(self, event: MidiEvent) -> None:
        self.midi_log.append(event)

    def recent_midi(self, max_age: float = 8.0) -> list[MidiEvent]:
        cutoff = time.monotonic() - max_age
        return [e for e in self.midi_log if e.timestamp > cutoff]

    def reset_peaks(self) -> None:
        self.input_meter.reset_peak()
        self.output_meter.reset_peak()
