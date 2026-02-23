"""Configuration loading and validation for stompbox projects."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class PluginConfig:
    """A single plugin in the signal chain."""

    path: Optional[str] = None  # File path to VST3/AU plugin
    plugin: Optional[str] = None  # Built-in pedalboard plugin name
    preset: Optional[str] = None  # Path to preset YAML file
    params: dict = field(default_factory=dict)
    midi: dict = field(default_factory=dict)  # {cc: {num: param}, notes: {num: action}}

    def label(self) -> str:
        if self.plugin:
            return self.plugin
        if self.path:
            return Path(self.path).stem
        return "?"


@dataclass
class AudioConfig:
    """Audio I/O configuration."""

    input: Optional[str] = "default"  # null = no input (synth/instrument mode)
    output: str = "default"
    sample_rate: int = 44100
    buffer_size: int = 512
    channels: int = 2


@dataclass
class MidiConfig:
    """MIDI input configuration."""

    input: Optional[str] = None
    channel: Optional[int] = None  # None = omni
    program_change: dict = field(default_factory=dict)  # {program: chain_path}


@dataclass
class StompboxConfig:
    """Top-level stompbox configuration."""

    audio: AudioConfig = field(default_factory=AudioConfig)
    midi: MidiConfig = field(default_factory=MidiConfig)
    chain: list[PluginConfig] = field(default_factory=list)
    mode: str = "tui"
    project_dir: Optional[Path] = None

    @classmethod
    def load(cls, path: Path) -> StompboxConfig:
        path = Path(path).resolve()
        if not path.exists():
            print(f"Config not found: {path}", file=sys.stderr)
            sys.exit(1)

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        audio_data = data.get("audio", {})
        audio = AudioConfig(
            input=audio_data.get("input", "default"),
            output=audio_data.get("output", "default"),
            sample_rate=int(audio_data.get("sample_rate", 44100)),
            buffer_size=int(audio_data.get("buffer_size", 512)),
            channels=int(audio_data.get("channels", 2)),
        )

        midi_data = data.get("midi") or {}
        pc_raw = midi_data.get("program_change") or {}
        midi = MidiConfig(
            input=midi_data.get("input"),
            channel=midi_data.get("channel"),
            program_change={int(k): str(v) for k, v in pc_raw.items()},
        )

        chain = []
        for p in data.get("chain", []):
            chain.append(
                PluginConfig(
                    path=p.get("path"),
                    plugin=p.get("plugin"),
                    preset=p.get("preset"),
                    params=p.get("params", {}),
                    midi=p.get("midi", {}),
                )
            )

        project_dir = path.parent

        return cls(
            audio=audio,
            midi=midi,
            chain=chain,
            mode=data.get("mode", "tui"),
            project_dir=project_dir,
        )

    def resolve_chain_path(self, chain_ref: str) -> Path:
        """Resolve a chain file reference relative to the project dir."""
        p = Path(chain_ref)
        if p.is_absolute():
            return p
        if self.project_dir:
            return self.project_dir / p
        return p
