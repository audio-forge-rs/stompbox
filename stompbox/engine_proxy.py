"""Subprocess isolation proxy — runs Engine in a child process, exposes identical API.

Native AU/VST3 plugins write to fd 1/2 from C-level threads, corrupting
the Textual TUI.  The fix: engine in a child process with fd 1/2 → /dev/null,
TUI in the parent with clean stdout.  IPC via multiprocessing.Pipe.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
from typing import Optional

from .meter import MidiEvent


# ── View objects (read-only API-compatible stand-ins) ─────────────────


class _MeterView:
    """Mimics Meter.read() interface."""

    __slots__ = ("_level", "_peak")

    def __init__(self, level: float = -96.0, peak: float = -96.0) -> None:
        self._level = level
        self._peak = peak

    def read(self) -> tuple[float, float]:
        return self._level, self._peak

    def reset_peak(self) -> None:
        pass  # handled by command to child


class _SlotView:
    """Mimics PluginSlot interface for TUI rendering."""

    __slots__ = ("name", "bypassed", "meter", "midi_cc")

    def __init__(
        self,
        name: str = "",
        bypassed: bool = False,
        level: float = -96.0,
        peak: float = -96.0,
        midi_cc: dict | None = None,
    ) -> None:
        self.name = name
        self.bypassed = bypassed
        self.meter = _MeterView(level, peak)
        self.midi_cc = midi_cc or {}


class _ChainView:
    """Mimics Chain interface for TUI rendering."""

    __slots__ = ("slots", "input_meter", "output_meter")

    def __init__(self) -> None:
        self.slots: list[_SlotView] = []
        self.input_meter = _MeterView()
        self.output_meter = _MeterView()

    def reset_peaks(self) -> None:
        pass  # handled by command to child


class _MidiConfigView:
    """Mimics MidiConfig.channel access."""

    __slots__ = ("channel",)

    def __init__(self, channel: Optional[int] = None) -> None:
        self.channel = channel


class _ConfigView:
    """Mimics config.midi.channel access pattern."""

    __slots__ = ("midi",)

    def __init__(self, midi_channel: Optional[int] = None) -> None:
        self.midi = _MidiConfigView(midi_channel)


class _MetersView:
    """Mimics MeterBridge.recent_midi() interface."""

    __slots__ = ("_events",)

    def __init__(self, events: list[MidiEvent] | None = None) -> None:
        self._events = events or []

    def recent_midi(self, max_age: float = 8.0) -> list[MidiEvent]:
        cutoff = time.monotonic() - max_age
        return [e for e in self._events if e.timestamp > cutoff]


# ── Snapshot (child → parent) ────────────────────────────────────────


def _build_snapshot(engine) -> dict:
    """Read all engine state into a pickle-safe dict. Called on child main thread."""
    chain = engine.chain
    in_lvl, in_pk = chain.input_meter.read()
    out_lvl, out_pk = chain.output_meter.read()

    slots = []
    for s in chain.slots:
        lvl, pk = s.meter.read()
        slots.append({
            "name": s.name,
            "bypassed": s.bypassed,
            "level": lvl,
            "peak": pk,
            "midi_cc": dict(s.midi_cc),
            "is_instrument": s.is_instrument,
        })

    midi_events = []
    for ev in engine.meters.recent_midi(max_age=10.0):
        midi_events.append({
            "timestamp": ev.timestamp,
            "kind": ev.kind,
            "channel": ev.channel,
            "data1": ev.data1,
            "data2": ev.data2,
        })

    return {
        "chain_name": engine.chain_name,
        "sample_rate": engine.sample_rate,
        "buffer_size": engine.buffer_size,
        "channels": engine.channels,
        "input_device_name": engine.input_device_name,
        "output_device_name": engine.output_device_name,
        "midi_port_name": engine.midi_port_name,
        "midi_channel": engine.config.midi.channel,
        "xruns": engine.xruns,
        "in_meter": (in_lvl, in_pk),
        "out_meter": (out_lvl, out_pk),
        "slots": slots,
        "midi_events": midi_events,
        "available_chains": engine.available_chains(),
    }


# ── Child process entry point ────────────────────────────────────────


def _engine_worker(config, conn) -> None:
    """Child process main. Redirects fd 1/2 → /dev/null, runs engine + IPC loop."""
    # Silence stdout/stderr — the whole point of subprocess isolation
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.close(devnull)

    from .engine import Engine

    engine = Engine(config)
    engine.start()

    interval = 1.0 / 30  # 30 fps snapshots

    try:
        while True:
            deadline = time.monotonic() + interval

            # Drain all pending commands
            while conn.poll(0):
                try:
                    cmd = conn.recv()
                except (EOFError, BrokenPipeError):
                    engine.stop()
                    return

                action = cmd.get("cmd")
                if action == "stop":
                    engine.stop()
                    return
                elif action == "toggle_bypass":
                    engine.toggle_bypass(cmd["index"])
                elif action == "master_bypass":
                    engine.master_bypass()
                elif action == "reset_peaks":
                    engine.reset_peaks()
                elif action == "load_chain":
                    engine.load_chain_file(cmd["path"])

            # Send snapshot
            try:
                conn.send(_build_snapshot(engine))
            except (BrokenPipeError, OSError):
                engine.stop()
                return

            # Sleep until next tick, but wake on incoming commands
            remaining = deadline - time.monotonic()
            if remaining > 0:
                conn.poll(remaining)

    except (EOFError, BrokenPipeError):
        pass
    finally:
        try:
            engine.stop()
        except Exception:
            pass


# ── EngineProxy (parent process, drop-in Engine replacement) ─────────


class EngineProxy:
    """Drop-in Engine replacement that runs the real Engine in a child process.

    All TUI-facing properties and methods match Engine's interface exactly.
    Commands are fire-and-forget over the pipe; state is read from cached snapshots.
    """

    def __init__(self, config) -> None:
        self._config = config

        # Cached state (updated atomically by reader thread)
        self.chain = _ChainView()
        self.meters = _MetersView()
        self.config = _ConfigView(config.midi.channel)

        self._chain_name = "default"
        self._sample_rate = config.audio.sample_rate
        self._buffer_size = config.audio.buffer_size
        self._channels = config.audio.channels
        self._input_device_name = ""
        self._output_device_name = ""
        self._midi_port_name: Optional[str] = None
        self._xruns = 0
        self._available_chains: list[str] = []

        # IPC
        self._parent_conn, self._child_conn = multiprocessing.Pipe()
        ctx = multiprocessing.get_context("spawn")
        self._process = ctx.Process(
            target=_engine_worker,
            args=(config, self._child_conn),
            daemon=True,
            name="stompbox-engine",
        )
        self._reader_thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return  # already started (idempotent for TUI on_mount)
        self._running = True
        self._process.start()
        # Close child's end in parent so only the child holds it
        self._child_conn.close()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="stompbox-proxy-reader"
        )
        self._reader_thread.start()

    def _reader_loop(self) -> None:
        """Pull snapshots from child, rebuild view objects, swap atomically."""
        conn = self._parent_conn
        while self._running:
            try:
                if not conn.poll(0.1):
                    continue
                snap = conn.recv()
            except (EOFError, BrokenPipeError, OSError):
                break

            # Build new view objects
            new_chain = _ChainView()
            new_chain.input_meter = _MeterView(*snap["in_meter"])
            new_chain.output_meter = _MeterView(*snap["out_meter"])
            new_chain.slots = [
                _SlotView(
                    name=s["name"],
                    bypassed=s["bypassed"],
                    level=s["level"],
                    peak=s["peak"],
                    midi_cc=s["midi_cc"],
                )
                for s in snap["slots"]
            ]

            midi_events = [
                MidiEvent(
                    timestamp=ev["timestamp"],
                    kind=ev["kind"],
                    channel=ev["channel"],
                    data1=ev["data1"],
                    data2=ev["data2"],
                )
                for ev in snap["midi_events"]
            ]
            new_meters = _MetersView(midi_events)

            # Atomic pointer swaps (GIL guarantees safety)
            self.chain = new_chain
            self.meters = new_meters
            self._chain_name = snap["chain_name"]
            self._sample_rate = snap["sample_rate"]
            self._buffer_size = snap["buffer_size"]
            self._channels = snap["channels"]
            self._input_device_name = snap["input_device_name"]
            self._output_device_name = snap["output_device_name"]
            self._midi_port_name = snap["midi_port_name"]
            self._xruns = snap["xruns"]
            self._available_chains = snap["available_chains"]
            self.config = _ConfigView(snap["midi_channel"])

    # ── Properties (match Engine API) ────────────────────────────────

    @property
    def chain_name(self) -> str:
        return self._chain_name

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def buffer_size(self) -> int:
        return self._buffer_size

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def input_device_name(self) -> str:
        return self._input_device_name

    @property
    def output_device_name(self) -> str:
        return self._output_device_name

    @property
    def midi_port_name(self) -> Optional[str]:
        return self._midi_port_name

    @property
    def xruns(self) -> int:
        return self._xruns

    # ── Commands (fire-and-forget to child) ──────────────────────────

    def _send(self, cmd: dict) -> None:
        try:
            self._parent_conn.send(cmd)
        except (BrokenPipeError, OSError):
            pass

    def toggle_bypass(self, slot_index: int) -> Optional[bool]:
        self._send({"cmd": "toggle_bypass", "index": slot_index})
        return None

    def master_bypass(self) -> bool:
        self._send({"cmd": "master_bypass"})
        return True

    def reset_peaks(self) -> None:
        self._send({"cmd": "reset_peaks"})

    def load_chain_file(self, path: str) -> bool:
        self._send({"cmd": "load_chain", "path": path})
        return True

    def available_chains(self) -> list[str]:
        return list(self._available_chains)

    def stop(self) -> None:
        self._running = False
        self._send({"cmd": "stop"})
        try:
            self._parent_conn.close()
        except OSError:
            pass
        self._process.join(timeout=3.0)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=1.0)
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)
