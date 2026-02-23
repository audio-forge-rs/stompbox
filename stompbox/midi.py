"""MIDI routing — bridges CoreMIDI input to Chain parameter control."""

from __future__ import annotations

import time
from typing import Callable, Optional

from .chain import Chain
from .coremidi import CoreMidiInput, MidiMessage, list_input_ports
from .config import MidiConfig, StompboxConfig
from .meter import MeterBridge, MidiEvent


class MidiRouter:
    """Manages MIDI input and routes messages to the chain and meter bridge.

    Connects to CoreMIDI, filters by channel, dispatches CC/note/PC messages.
    """

    def __init__(
        self,
        config: MidiConfig,
        chain: Chain,
        meters: MeterBridge,
        on_program_change: Optional[Callable[[int], None]] = None,
    ) -> None:
        self._config = config
        self._chain = chain
        self._meters = meters
        self._on_program_change = on_program_change
        self._midi_input: Optional[CoreMidiInput] = None
        self.connected_port: Optional[str] = None

    def start(self) -> None:
        if not self._config.input:
            return

        self._midi_input = CoreMidiInput(
            port_name=self._config.input,
            channel=self._config.channel,
        )
        self.connected_port = self._midi_input.start(self._on_message)

    # System realtime messages to ignore in logging (timing clock, active sensing, etc.)
    _IGNORE_KINDS = {"other"}

    def _on_message(self, msg: MidiMessage) -> None:
        """Handle an incoming MIDI message. Runs on CoreMIDI's thread."""
        # Skip system realtime noise (clock, active sensing)
        if msg.kind in self._IGNORE_KINDS and msg.data1 >= 0xF0:
            return

        now = time.monotonic()

        # Record to meter bridge for TUI display
        self._meters.record_midi(
            MidiEvent(
                timestamp=now,
                kind=msg.kind,
                channel=msg.channel,
                data1=msg.data1,
                data2=msg.data2,
            )
        )

        # Route to chain
        if msg.kind == "cc":
            self._chain.handle_cc(msg.channel, msg.data1, msg.data2)
        elif msg.kind == "note_on":
            self._chain.handle_note(msg.channel, msg.data1, msg.data2)
        elif msg.kind == "note_off":
            self._chain.handle_note(msg.channel, msg.data1, 0)
        elif msg.kind == "pc" and self._on_program_change:
            self._on_program_change(msg.data1)

    def stop(self) -> None:
        if self._midi_input is not None:
            self._midi_input.stop()
            self._midi_input = None
        self.connected_port = None

    @staticmethod
    def available_ports() -> list[str]:
        return list_input_ports()
