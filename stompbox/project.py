"""Project folder management — scaffolding and discovery."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml


DEFAULT_CONFIG = {
    "audio": {
        "input": "default",
        "output": "default",
        "sample_rate": 44100,
        "buffer_size": 512,
        "channels": 2,
    },
    "midi": {
        "input": None,
    },
    "chain": [
        {
            "plugin": "NoiseGate",
            "params": {"threshold_db": -40},
        },
        {
            "plugin": "Compressor",
            "params": {"threshold_db": -20, "ratio": 3.0},
        },
        {
            "plugin": "Reverb",
            "params": {"room_size": 0.5, "wet_level": 0.2},
        },
        {
            "plugin": "Limiter",
            "params": {"threshold_db": -1.0},
        },
    ],
    "mode": "tui",
}

CLEAN_CHAIN = {
    "chain": [
        {"plugin": "Compressor", "params": {"threshold_db": -15, "ratio": 2.0}},
        {"plugin": "Gain", "params": {"gain_db": 0.0}},
        {"plugin": "Limiter", "params": {"threshold_db": -0.5}},
    ]
}

AMBIENT_CHAIN = {
    "chain": [
        {"plugin": "Chorus", "params": {"rate_hz": 0.5, "depth": 0.4, "mix": 0.3}},
        {"plugin": "Delay", "params": {"delay_seconds": 0.35, "feedback": 0.4, "mix": 0.3}},
        {"plugin": "Reverb", "params": {"room_size": 0.9, "wet_level": 0.5, "damping": 0.8}},
        {"plugin": "Limiter", "params": {"threshold_db": -1.0}},
    ]
}

DRIVE_CHAIN = {
    "chain": [
        {"plugin": "NoiseGate", "params": {"threshold_db": -50}},
        {"plugin": "Distortion", "params": {"drive_db": 20}},
        {"plugin": "HighpassFilter", "params": {"cutoff_frequency_hz": 80}},
        {"plugin": "LowpassFilter", "params": {"cutoff_frequency_hz": 8000}},
        {"plugin": "Reverb", "params": {"room_size": 0.3, "wet_level": 0.15}},
        {"plugin": "Limiter", "params": {"threshold_db": -1.0}},
    ]
}


def init_project(directory: Path) -> Path:
    """Create a new stompbox project folder with default configs.

    Returns the path to the main config file.
    """
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)

    chains_dir = directory / "chains"
    chains_dir.mkdir(exist_ok=True)

    recordings_dir = directory / "recordings"
    recordings_dir.mkdir(exist_ok=True)

    # Main config
    config_path = directory / "stompbox.yml"
    if not config_path.exists():
        _write_yaml(config_path, DEFAULT_CONFIG)

    # Preset chains
    for name, data in [("clean", CLEAN_CHAIN), ("ambient", AMBIENT_CHAIN), ("drive", DRIVE_CHAIN)]:
        chain_path = chains_dir / f"{name}.yml"
        if not chain_path.exists():
            _write_yaml(chain_path, data)

    return config_path


def find_project_config(start: Path = Path.cwd()) -> Path | None:
    """Walk up from start looking for stompbox.yml."""
    current = start.resolve()
    for _ in range(20):
        candidate = current / "stompbox.yml"
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _write_yaml(path: Path, data: dict) -> None:
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
