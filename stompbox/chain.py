"""Plugin chain management — loads and processes audio through pedalboard plugins."""

from __future__ import annotations

import math
import sys
from collections import deque
from pathlib import Path
from typing import Optional

import numpy as np
from pedalboard import (
    Bitcrush,
    Chorus,
    Clipping,
    Compressor,
    Convolution,
    Delay,
    Distortion,
    Gain,
    HighpassFilter,
    HighShelfFilter,
    LadderFilter,
    Limiter,
    LowpassFilter,
    LowShelfFilter,
    NoiseGate,
    PeakFilter,
    Pedalboard,
    Phaser,
    PitchShift,
    Reverb,
    load_plugin,
)

from .config import PluginConfig
from .meter import FLOOR_DB, Meter

BUILTIN_PLUGINS: dict[str, type] = {
    "Bitcrush": Bitcrush,
    "Chorus": Chorus,
    "Clipping": Clipping,
    "Compressor": Compressor,
    "Convolution": Convolution,
    "Delay": Delay,
    "Distortion": Distortion,
    "Gain": Gain,
    "HighpassFilter": HighpassFilter,
    "HighShelfFilter": HighShelfFilter,
    "LadderFilter": LadderFilter,
    "Limiter": Limiter,
    "LowpassFilter": LowpassFilter,
    "LowShelfFilter": LowShelfFilter,
    "NoiseGate": NoiseGate,
    "PeakFilter": PeakFilter,
    "Phaser": Phaser,
    "PitchShift": PitchShift,
    "Reverb": Reverb,
}


class PluginSlot:
    """Wraps a single plugin with bypass, metering, and MIDI mapping."""

    def __init__(
        self,
        plugin: object,
        name: str,
        midi_cc: Optional[dict[int, str]] = None,
        midi_notes: Optional[dict[int, str]] = None,
    ) -> None:
        self.plugin = plugin
        self.board = Pedalboard([plugin])
        self.name = name
        self.bypassed = False
        self.meter = Meter()
        self.midi_cc = midi_cc or {}  # {cc_number: param_name}
        self.midi_notes = midi_notes or {}  # {note_number: "bypass"}
        self.is_instrument: bool = getattr(plugin, "is_instrument", False)
        self._midi_queue: deque = deque()  # raw MIDI bytes for instrument plugins
        self._instrument_initialized = False

        # Cache available parameter names for display
        self._param_names: list[str] = []
        try:
            if hasattr(plugin, "parameters"):
                self._param_names = list(plugin.parameters.keys())
        except Exception:
            pass

    def push_midi(self, midi_bytes: bytes) -> None:
        """Enqueue raw MIDI bytes for instrument plugins. Thread-safe (GIL)."""
        self._midi_queue.append(midi_bytes)

    def process(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """Process audio and update meter. Audio shape: (channels, samples)."""
        if self.bypassed:
            # Drain MIDI queue even when bypassed to avoid stale buildup
            self._midi_queue.clear()
            peak = float(np.max(np.abs(audio))) if audio.size > 0 else 0.0
            self.meter.push(peak)
            return audio

        if self.is_instrument:
            result = self._process_instrument(audio, sample_rate)
        else:
            result = self.board(audio, sample_rate)

        peak = float(np.max(np.abs(result))) if result.size > 0 else 0.0
        self.meter.push(peak)
        return result

    def _process_instrument(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """Generate audio from MIDI for instrument plugins."""
        # Drain MIDI queue into pedalboard-compatible list
        messages = []
        while self._midi_queue:
            try:
                messages.append((self._midi_queue.popleft(), 0.0))
            except IndexError:
                break

        num_channels = audio.shape[0]
        num_samples = audio.shape[1]
        duration = num_samples / sample_rate

        reset = not self._instrument_initialized
        self._instrument_initialized = True

        result = self.plugin(
            messages,
            duration=duration,
            sample_rate=float(sample_rate),
            num_channels=num_channels,
            buffer_size=num_samples,
            reset=reset,
        )
        return np.ascontiguousarray(result, dtype=np.float32)

    def set_param(self, name: str, value: float) -> None:
        if hasattr(self.plugin, name):
            try:
                setattr(self.plugin, name, value)
            except Exception:
                pass

    def get_param(self, name: str) -> Optional[float]:
        if hasattr(self.plugin, name):
            try:
                return float(getattr(self.plugin, name))
            except Exception:
                pass
        return None

    def toggle_bypass(self) -> bool:
        self.bypassed = not self.bypassed
        return self.bypassed

    @property
    def level_db(self) -> float:
        return self.meter.read()[0]

    @property
    def peak_db(self) -> float:
        return self.meter.read()[1]


class Chain:
    """Ordered list of PluginSlots with signal-flow processing and MIDI routing."""

    def __init__(self, slots: Optional[list[PluginSlot]] = None) -> None:
        self.slots: list[PluginSlot] = slots or []
        self.input_meter = Meter()
        self.output_meter = Meter()

    @classmethod
    def from_config(cls, plugin_configs: list[PluginConfig]) -> Chain:
        slots: list[PluginSlot] = []
        for pc in plugin_configs:
            plugin = _load_plugin(pc)
            if plugin is None:
                continue

            name = pc.label()

            midi_cc: dict[int, str] = {}
            midi_notes: dict[int, str] = {}
            midi_cfg = pc.midi
            if "cc" in midi_cfg:
                midi_cc = {int(k): str(v) for k, v in midi_cfg["cc"].items()}
            if "notes" in midi_cfg:
                midi_notes = {int(k): str(v) for k, v in midi_cfg["notes"].items()}

            slot = PluginSlot(plugin, name, midi_cc, midi_notes)

            for param, value in pc.params.items():
                slot.set_param(param, value)

            slots.append(slot)

        return cls(slots)

    def process(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """Process audio through the full chain. Shape: (channels, samples)."""
        # Input metering
        if audio.size > 0:
            self.input_meter.push(float(np.max(np.abs(audio))))
        else:
            self.input_meter.push(0.0)

        for slot in self.slots:
            audio = slot.process(audio, sample_rate)

        # Output metering
        if audio.size > 0:
            self.output_meter.push(float(np.max(np.abs(audio))))
        else:
            self.output_meter.push(0.0)

        return audio

    def handle_cc(self, channel: int, cc: int, value: int) -> None:
        """Route a MIDI CC to plugin parameters and instrument plugins."""
        normalized = value / 127.0
        for slot in self.slots:
            # Forward raw CC to instrument plugins
            if slot.is_instrument and not slot.bypassed:
                slot.push_midi(bytes([0xB0 | (channel & 0x0F), cc & 0x7F, value & 0x7F]))

            if cc in slot.midi_cc:
                param_name = slot.midi_cc[cc]
                current = slot.get_param(param_name)
                if current is not None and param_name.endswith("_db"):
                    slot.set_param(param_name, -60.0 + normalized * 60.0)
                elif current is not None and param_name.endswith("_hz"):
                    slot.set_param(param_name, 20.0 + normalized * 19980.0)
                else:
                    slot.set_param(param_name, normalized)

    def handle_note(self, channel: int, note: int, velocity: int) -> None:
        """Route a MIDI note to instrument plugins and bypass toggles."""
        for slot in self.slots:
            # Forward raw note to instrument plugins
            if slot.is_instrument and not slot.bypassed:
                if velocity > 0:
                    slot.push_midi(bytes([0x90 | (channel & 0x0F), note & 0x7F, velocity & 0x7F]))
                else:
                    slot.push_midi(bytes([0x80 | (channel & 0x0F), note & 0x7F, 0]))

            if note in slot.midi_notes:
                action = slot.midi_notes[note]
                if action == "bypass" and velocity > 0:
                    slot.toggle_bypass()

    def reset_peaks(self) -> None:
        self.input_meter.reset_peak()
        self.output_meter.reset_peak()
        for slot in self.slots:
            slot.meter.reset_peak()


def _suppress_stdio():
    """Context manager to silence stdout/stderr from noisy native plugins."""
    import contextlib
    import os

    @contextlib.contextmanager
    def _quiet():
        # Save real file descriptors
        old_stdout_fd = os.dup(1)
        old_stderr_fd = os.dup(2)
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            yield
        finally:
            os.dup2(old_stdout_fd, 1)
            os.dup2(old_stderr_fd, 2)
            os.close(old_stdout_fd)
            os.close(old_stderr_fd)
            os.close(devnull)

    return _quiet()


def _load_plugin(pc: PluginConfig) -> object | None:
    if pc.path:
        p = Path(pc.path)
        if not p.exists():
            print(f"Plugin not found: {pc.path}", file=sys.stderr)
            return None
        try:
            with _suppress_stdio():
                return load_plugin(str(p))
        except Exception as e:
            print(f"Failed to load {pc.path}: {e}", file=sys.stderr)
            return None

    if pc.plugin:
        cls = BUILTIN_PLUGINS.get(pc.plugin)
        if cls is None:
            print(
                f"Unknown plugin: {pc.plugin}. Available: {', '.join(sorted(BUILTIN_PLUGINS))}",
                file=sys.stderr,
            )
            return None
        try:
            return cls()
        except Exception as e:
            print(f"Failed to create {pc.plugin}: {e}", file=sys.stderr)
            return None

    return None
