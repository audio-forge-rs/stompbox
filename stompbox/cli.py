"""CLI entry point for stompbox."""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

from . import __version__


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="stompbox",
        description="MIDI-controlled audio pedalboard",
    )
    parser.add_argument("--version", action="version", version=f"stompbox {__version__}")

    sub = parser.add_subparsers(dest="command")

    # --- run (default) ---
    run_p = sub.add_parser("run", help="Start the pedalboard engine")
    run_p.add_argument(
        "-c", "--config",
        type=Path,
        default=None,
        help="Path to stompbox.yml config file",
    )
    run_p.add_argument(
        "--headless",
        action="store_true",
        help="Run without TUI (overrides config mode)",
    )
    run_p.add_argument(
        "--tui",
        action="store_true",
        help="Run with TUI (overrides config mode)",
    )

    # --- init ---
    init_p = sub.add_parser("init", help="Create a new stompbox project")
    init_p.add_argument(
        "directory",
        type=Path,
        nargs="?",
        default=Path.cwd(),
        help="Project directory (default: current directory)",
    )

    # --- devices ---
    sub.add_parser("devices", help="List audio and MIDI devices")

    args = parser.parse_args(argv)

    # Default to 'run' if no subcommand
    if args.command is None:
        args.command = "run"
        args.config = None
        args.headless = False
        args.tui = False

    if args.command == "init":
        _cmd_init(args)
    elif args.command == "devices":
        _cmd_devices()
    elif args.command == "run":
        _cmd_run(args)


def _cmd_init(args: argparse.Namespace) -> None:
    from .project import init_project

    config_path = init_project(args.directory)
    print(f"Project initialized: {config_path.parent}")
    print(f"  Config:  {config_path}")
    print(f"  Chains:  {config_path.parent / 'chains'}")
    print(f"  Records: {config_path.parent / 'recordings'}")
    print()
    print("Run with:  stompbox run")


def _cmd_devices() -> None:
    import sounddevice as sd
    from .coremidi import list_input_ports

    print("── Audio Devices ──")
    devices = sd.query_devices()
    for i, dev in enumerate(devices):
        d = dev  # type: ignore
        ins = d["max_input_channels"]
        outs = d["max_output_channels"]
        direction = []
        if ins > 0:
            direction.append(f"in:{ins}")
        if outs > 0:
            direction.append(f"out:{outs}")
        marker = ""
        try:
            default_in = sd.query_devices(kind="input")
            default_out = sd.query_devices(kind="output")
            if d["name"] == default_in["name"] and ins > 0:
                marker = " ◂ default input"
            if d["name"] == default_out["name"] and outs > 0:
                marker += " ◂ default output"
        except Exception:
            pass
        print(f"  [{i}] {d['name']}  ({', '.join(direction)}){marker}")

    print()
    print("── MIDI Input Ports ──")
    ports = list_input_ports()
    if ports:
        for p in ports:
            print(f"  ▸ {p}")
    else:
        print("  (no MIDI input ports found)")


def _cmd_run(args: argparse.Namespace) -> None:
    from .config import StompboxConfig
    from .project import find_project_config

    # Find config
    config_path = args.config
    if config_path is None:
        config_path = find_project_config()
    if config_path is None:
        print("No stompbox.yml found. Run 'stompbox init' to create a project.", file=sys.stderr)
        sys.exit(1)

    config = StompboxConfig.load(config_path)

    # Mode override
    if args.headless:
        config.mode = "headless"
    elif args.tui:
        config.mode = "tui"

    if config.mode == "tui":
        from .engine_proxy import EngineProxy
        engine = EngineProxy(config)
        engine.start()  # spawn child before Textual takes over fds
        _run_tui(engine)
    else:
        from .engine import Engine
        engine = Engine(config)
        _run_headless(engine)


def _run_tui(engine) -> None:
    from .tui.app import StompboxApp

    app = StompboxApp(engine)
    app.run()


def _run_headless(engine) -> None:
    engine.start()

    # Graceful shutdown on SIGINT/SIGTERM
    shutdown = False

    def _signal_handler(sig, frame):
        nonlocal shutdown
        shutdown = True

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    print(f"Stompbox running headless — {engine.chain_name}")
    print(f"  Audio: {engine.input_device_name} → {engine.output_device_name}")
    print(f"  Format: {engine.sample_rate}Hz / {engine.buffer_size} buffer / {engine.channels}ch")
    midi_port = engine.midi_port_name
    if midi_port:
        print(f"  MIDI: {midi_port}")
    print(f"  Chain: {len(engine.chain.slots)} plugins")
    print()
    print("Press Ctrl-C to stop.")
    print()

    try:
        while not shutdown:
            time.sleep(0.5)
            # Periodic status in headless mode
            in_lvl, in_pk = engine.chain.input_meter.read()
            out_lvl, out_pk = engine.chain.output_meter.read()
            xruns = engine.xruns
            status = f"\r  IN: {format_db(in_lvl)}  OUT: {format_db(out_lvl)}"
            if xruns:
                status += f"  xruns: {xruns}"
            print(status, end="", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        print("\nStopping...")
        engine.stop()
        print("Done.")


def format_db(db: float) -> str:
    if db <= -90:
        return "  -∞  "
    return f"{db:+.1f}dB"
