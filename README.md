# Stompbox — Production Audio Pedalboard

What it does

- Real-time audio processing through Spotify's pedalboard library — chains VST3/AU plugins or 19 built-in
effects (Reverb, Compressor, Delay, Distortion, etc.)
- MIDI input via native CoreMIDI (ctypes, no broken python-rtmidi dependency) — CC→parameter mapping,
note→bypass toggles, program change→chain switching
- Audio I/O via sounddevice/PortAudio — handles mono→stereo, any device combo, or file output (WAV/FLAC/MP3)
- Lock-free metering with peak hold and decay

Architecture

Engine (headless-capable core)
├── Chain → PluginSlot[] (per-slot metering, bypass, MIDI map)
├── AudioIO (sounddevice duplex stream or file writer)
├── MidiRouter → CoreMidiInput (native macOS CoreMIDI)
└── MeterBridge (lock-free level tracking)

TUI

Textual-based dashboard at 30fps with:
- Status bar: chain name, sample rate, buffer size, devices
- Chain view: pedal boxes with levels, bypass state, CC assignments
- VU meters: sub-character precision, color-coded green→yellow→red, peak hold
- MIDI monitor: live message ticker

Keybindings: 1-9 bypass toggle, SPC master bypass, [/] chain presets, r reset peaks, q quit

Config (YAML)

```
mode: tui                 # or headless
audio:
  input: default          # or device name, or file path
  output: default         # or device name, or .wav/.flac path
chain:
  - plugin: Reverb        # built-in
    params: { room_size: 0.5 }
    midi: { cc: { 11: wet_level } }
  - path: /path/to.vst3   # external plugin
```

CLI

```
python3 -m stompbox init [dir]    # Scaffold project with 3 preset chains
python3 -m stompbox run           # Start (TUI or headless per config)
python3 -m stompbox run --headless
python3 -m stompbox devices       # List audio + MIDI devices
```

To launch the TUI:

```
python3 -m stompbox run
```
