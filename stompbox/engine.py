"""Core engine — orchestrates audio, MIDI, and plugin chain.

Runs identically in headless and TUI modes. The TUI reads engine state
via polling; no callbacks or event subscriptions needed.
"""

from __future__ import annotations

import os
import sys
import time
import threading
from pathlib import Path
from typing import Optional

import yaml

from .audio import AudioIO
from .chain import Chain
from .config import PluginConfig, StompboxConfig
from .meter import MeterBridge
from .midi import MidiRouter


class Engine:
    """Central coordinator. Start/stop lifecycle, state exposure for TUI."""

    def __init__(self, config: StompboxConfig) -> None:
        self.config = config
        self.meters = MeterBridge()

        # Build plugin chain
        self.chain = Chain.from_config(config.chain)

        # Audio I/O
        self.audio = AudioIO(
            chain=self.chain,
            meters=self.meters,
            input_device=config.audio.input,
            output_device=config.audio.output,
            sample_rate=config.audio.sample_rate,
            buffer_size=config.audio.buffer_size,
            channels=config.audio.channels,
        )

        # MIDI routing
        self.midi = MidiRouter(
            config=config.midi,
            chain=self.chain,
            meters=self.meters,
            on_program_change=self._on_program_change,
        )

        self._running = False
        self._chain_name = "default"
        self._saved_stdout_fd: Optional[int] = None
        self._saved_stderr_fd: Optional[int] = None
        self._devnull_fd: Optional[int] = None

    def _suppress_fds(self) -> None:
        """Redirect fd 1 and 2 to /dev/null for the engine lifetime.

        Native plugins (e.g. AU/VST3) can print from background threads
        at any time, which corrupts the TUI. We redirect the raw fds to
        /dev/null (catches C-level writes from plugin threads), then rewire
        Python's sys.stdout/stderr to dup'd copies of the real terminal
        so Textual and Python code still render correctly.
        """
        self._saved_stdout_fd = os.dup(1)
        self._saved_stderr_fd = os.dup(2)
        self._devnull_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(self._devnull_fd, 1)
        os.dup2(self._devnull_fd, 2)

        # Rewire Python's stdout/stderr to the saved real terminal fds
        # so Textual (and all Python-level I/O) still works.
        self._orig_sys_stdout = sys.stdout
        self._orig_sys_stderr = sys.stderr
        sys.stdout = os.fdopen(self._saved_stdout_fd, "w", closefd=False)
        sys.stderr = os.fdopen(self._saved_stderr_fd, "w", closefd=False)

    def _restore_fds(self) -> None:
        """Restore original stdout/stderr file descriptors."""
        # Restore Python-level streams first
        if hasattr(self, "_orig_sys_stdout"):
            sys.stdout = self._orig_sys_stdout
        if hasattr(self, "_orig_sys_stderr"):
            sys.stderr = self._orig_sys_stderr

        if self._saved_stdout_fd is not None:
            os.dup2(self._saved_stdout_fd, 1)
            os.close(self._saved_stdout_fd)
            self._saved_stdout_fd = None
        if self._saved_stderr_fd is not None:
            os.dup2(self._saved_stderr_fd, 2)
            os.close(self._saved_stderr_fd)
            self._saved_stderr_fd = None
        if self._devnull_fd is not None:
            os.close(self._devnull_fd)
            self._devnull_fd = None

    def start(self) -> None:
        """Start audio processing and MIDI input."""
        self._suppress_fds()
        self.midi.start()
        self.audio.start()
        self._running = True

    def stop(self) -> None:
        """Stop everything cleanly."""
        self._running = False
        self.audio.stop()
        self.midi.stop()
        self._restore_fds()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def chain_name(self) -> str:
        return self._chain_name

    @property
    def midi_port_name(self) -> Optional[str]:
        return self.midi.connected_port

    @property
    def input_device_name(self) -> str:
        return self.audio.input_device_name

    @property
    def output_device_name(self) -> str:
        return self.audio.output_device_name

    @property
    def sample_rate(self) -> int:
        return self.config.audio.sample_rate

    @property
    def buffer_size(self) -> int:
        return self.config.audio.buffer_size

    @property
    def channels(self) -> int:
        return self.config.audio.channels

    @property
    def xruns(self) -> int:
        return self.audio.xrun_count

    def toggle_bypass(self, slot_index: int) -> Optional[bool]:
        """Toggle bypass on a slot. Returns new bypass state or None if out of range."""
        if 0 <= slot_index < len(self.chain.slots):
            return self.chain.slots[slot_index].toggle_bypass()
        return None

    def master_bypass(self) -> bool:
        """Toggle all plugins bypassed. Returns True if all are now bypassed."""
        any_active = any(not s.bypassed for s in self.chain.slots)
        for s in self.chain.slots:
            s.bypassed = any_active
        return any_active

    def reset_peaks(self) -> None:
        self.chain.reset_peaks()
        self.meters.reset_peaks()

    def load_chain_file(self, path: str) -> bool:
        """Load a new chain from a YAML file. Returns True on success."""
        resolved = self.config.resolve_chain_path(path)
        if not resolved.exists():
            return False

        try:
            with open(resolved) as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            return False

        chain_data = data.get("chain", [])
        plugin_configs = [
            PluginConfig(
                path=p.get("path"),
                plugin=p.get("plugin"),
                params=p.get("params", {}),
                midi=p.get("midi", {}),
            )
            for p in chain_data
        ]

        new_chain = Chain.from_config(plugin_configs)

        # Hot-swap: stop audio, replace chain, restart
        was_running = self._running
        if was_running:
            self.audio.stop()

        self.chain = new_chain
        self.audio.chain = new_chain
        self.midi._chain = new_chain

        if was_running:
            self.audio.start()

        self._chain_name = resolved.stem
        return True

    def _on_program_change(self, program: int) -> None:
        """Handle MIDI program change → chain switch."""
        pc_map = self.config.midi.program_change
        if program in pc_map:
            self.load_chain_file(pc_map[program])

    def available_chains(self) -> list[str]:
        """List chain YAML files in the project's chains/ directory."""
        if not self.config.project_dir:
            return []
        chains_dir = self.config.project_dir / "chains"
        if not chains_dir.is_dir():
            return []
        return sorted(p.stem for p in chains_dir.glob("*.yml"))
