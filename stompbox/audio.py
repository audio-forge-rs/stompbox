"""Audio I/O using sounddevice (PortAudio) and pedalboard file I/O."""

from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

from .chain import Chain
from .meter import MeterBridge

# Audio file extensions recognized as file targets (not device names)
_AUDIO_EXTENSIONS = {".wav", ".aiff", ".aif", ".flac", ".mp3", ".ogg"}


def is_audio_file(name: str) -> bool:
    if not name or name == "default":
        return False
    return Path(name).suffix.lower() in _AUDIO_EXTENSIONS


def list_devices() -> list[dict]:
    """Return sounddevice device list."""
    return sd.query_devices()  # type: ignore[return-value]


def find_device(name: str, kind: str = "input") -> Optional[int]:
    """Find a device index by partial name match. kind: 'input' or 'output'."""
    if name == "default":
        return None  # None = system default
    devices = sd.query_devices()
    for i, dev in enumerate(devices):  # type: ignore[arg-type]
        if kind == "input" and dev["max_input_channels"] == 0:  # type: ignore[index]
            continue
        if kind == "output" and dev["max_output_channels"] == 0:  # type: ignore[index]
            continue
        if name.lower() in dev["name"].lower():  # type: ignore[index]
            return i
    return None


def _device_channels(dev_id: Optional[int], kind: str, desired: int) -> int:
    """Query a device's actual channel count, clamped to desired."""
    try:
        if dev_id is None:
            info = sd.query_devices(kind=kind)
        else:
            info = sd.query_devices(dev_id)
        key = "max_input_channels" if kind == "input" else "max_output_channels"
        available = int(info[key])  # type: ignore[index]
        return min(available, desired) if available > 0 else desired
    except Exception:
        return desired


class AudioIO:
    """Manages real-time audio streaming through a plugin chain.

    Handles device I/O via sounddevice and optional file recording
    via pedalboard's WriteableAudioFile.
    """

    def __init__(
        self,
        chain: Chain,
        meters: MeterBridge,
        input_device: str = "default",
        output_device: str = "default",
        sample_rate: int = 44100,
        buffer_size: int = 512,
        channels: int = 2,
    ) -> None:
        self.chain = chain
        self.meters = meters
        self.sample_rate = sample_rate
        self.buffer_size = buffer_size
        self.channels = channels  # desired processing channels
        self.xrun_count = 0
        self._stream: Optional[sd.Stream | sd.OutputStream | sd.InputStream] = None
        self._file_writer: Optional[_FileWriter] = None

        # Resolve devices
        self._input_disabled = input_device is None  # YAML null = no input
        self._output_is_file = is_audio_file(output_device) if output_device else False

        if self._input_disabled:
            self._input_device_id = None
            self._input_channels = channels
        else:
            self._input_device_id = find_device(input_device, "input")
            self._input_channels = _device_channels(self._input_device_id, "input", channels)

        if self._output_is_file:
            self._output_device_id = None
            self._output_file_path = output_device
            self._output_channels = channels
        else:
            self._output_device_id = find_device(output_device, "output") if output_device else None
            self._output_file_path = None
            self._output_channels = _device_channels(self._output_device_id, "output", channels)

        # Resolved names for display
        if self._input_disabled:
            self.input_device_name = "(none)"
        else:
            self.input_device_name = self._resolve_name(self._input_device_id, "input", input_device)
        if self._output_is_file:
            self.output_device_name = output_device
        else:
            self.output_device_name = self._resolve_name(self._output_device_id, "output", output_device)

    @staticmethod
    def _resolve_name(dev_id: Optional[int], kind: str, original: str) -> str:
        if dev_id is None:
            try:
                default = sd.query_devices(kind=kind)
                return default["name"]  # type: ignore[index]
            except Exception:
                return original
        try:
            return sd.query_devices(dev_id)["name"]  # type: ignore[index]
        except Exception:
            return original

    def start(self) -> None:
        """Open audio stream and begin processing."""
        from .chain import _suppress_stdio

        with _suppress_stdio():
            if self._input_disabled:
                self._start_output_only()
            elif self._output_is_file:
                self._start_file_output()
            else:
                self._start_device_output()

    def _start_output_only(self) -> None:
        """Output-only stream: feed silence into chain, output to device.

        Used for instrument/synth plugins that generate audio from MIDI.
        """
        self._stream = sd.OutputStream(
            samplerate=self.sample_rate,
            blocksize=self.buffer_size,
            device=self._output_device_id,
            channels=self._output_channels,
            dtype="float32",
            callback=self._output_only_callback,
            finished_callback=self._stream_finished,
        )
        self._stream.start()

    def _output_only_callback(
        self,
        outdata: np.ndarray,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        """OutputStream callback — feeds silence through the chain."""
        if status:
            self.xrun_count += 1

        # Silent input buffer: (channels, samples)
        silence = np.zeros((self.channels, frames), dtype=np.float32)
        audio = self.chain.process(silence, self.sample_rate)

        out = audio.T
        out_ch = outdata.shape[1]
        proc_ch = out.shape[1] if out.ndim > 1 else 1

        if proc_ch >= out_ch:
            outdata[:] = out[:, :out_ch]
        else:
            outdata[:, :proc_ch] = out
            outdata[:, proc_ch:] = out[:, :1]

    def _start_device_output(self) -> None:
        """Duplex stream: input device → chain → output device."""
        self._stream = sd.Stream(
            samplerate=self.sample_rate,
            blocksize=self.buffer_size,
            device=(self._input_device_id, self._output_device_id),
            channels=(self._input_channels, self._output_channels),
            dtype="float32",
            callback=self._duplex_callback,
            finished_callback=self._stream_finished,
        )
        self._stream.start()

    def _start_file_output(self) -> None:
        """Input device → chain → file. Uses an InputStream + file writer thread."""
        from pedalboard.io import WriteableAudioFile

        self._file_writer = _FileWriter(
            self._output_file_path,  # type: ignore[arg-type]
            self.sample_rate,
            self.channels,
        )
        self._file_writer.start()

        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            blocksize=self.buffer_size,
            device=self._input_device_id,
            channels=self._input_channels,
            dtype="float32",
            callback=self._input_to_file_callback,
        )
        self._stream.start()

    def _duplex_callback(
        self,
        indata: np.ndarray,
        outdata: np.ndarray,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        """sounddevice duplex callback. Runs on the audio thread."""
        if status:
            self.xrun_count += 1

        # indata shape: (samples, input_channels)
        # Convert to (channels, samples) for pedalboard — always process as stereo
        in_channels = indata.shape[1]
        if in_channels == 1 and self.channels >= 2:
            # Mono → stereo: duplicate the channel
            stereo = np.concatenate([indata, indata], axis=1)
            audio = np.ascontiguousarray(stereo.T, dtype=np.float32)
        else:
            audio = np.ascontiguousarray(indata.T, dtype=np.float32)

        # Process through the chain
        audio = self.chain.process(audio, self.sample_rate)

        # Write to output: (channels, samples) → (samples, channels)
        out = audio.T
        out_ch = outdata.shape[1]
        proc_ch = out.shape[1] if out.ndim > 1 else 1

        if proc_ch >= out_ch:
            outdata[:] = out[:, :out_ch]
        else:
            outdata[:, :proc_ch] = out
            outdata[:, proc_ch:] = out[:, :1]  # Fill remaining with first channel

    def _input_to_file_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        """InputStream callback for file-output mode."""
        if status:
            self.xrun_count += 1

        # Handle mono→stereo if needed
        in_channels = indata.shape[1]
        if in_channels == 1 and self.channels >= 2:
            stereo = np.concatenate([indata, indata], axis=1)
            audio = np.ascontiguousarray(stereo.T, dtype=np.float32)
        else:
            audio = np.ascontiguousarray(indata.T, dtype=np.float32)

        audio = self.chain.process(audio, self.sample_rate)

        if self._file_writer:
            self._file_writer.push(audio)

    def _stream_finished(self) -> None:
        pass

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

        if self._file_writer is not None:
            self._file_writer.stop()
            self._file_writer = None

    @property
    def is_active(self) -> bool:
        return self._stream is not None and self._stream.active


class _FileWriter:
    """Threaded audio file writer. Audio callback pushes buffers; writer thread flushes to disk."""

    def __init__(self, path: str, sample_rate: int, channels: int) -> None:
        from pedalboard.io import WriteableAudioFile

        self._path = path
        self._file = WriteableAudioFile(path, sample_rate, channels)
        self._queue: queue.Queue[Optional[np.ndarray]] = queue.Queue(maxsize=256)
        self._thread = threading.Thread(target=self._write_loop, daemon=True, name="stompbox-file-writer")
        self._running = False

    def start(self) -> None:
        self._running = True
        self._thread.start()

    def push(self, audio: np.ndarray) -> None:
        """Non-blocking push from audio callback."""
        try:
            self._queue.put_nowait(audio)
        except queue.Full:
            pass  # Drop frames rather than block audio thread

    def _write_loop(self) -> None:
        while self._running:
            try:
                audio = self._queue.get(timeout=0.1)
                if audio is not None:
                    self._file.write(audio)
            except queue.Empty:
                continue
            except Exception:
                break

    def stop(self) -> None:
        self._running = False
        self._queue.put(None)  # Sentinel
        self._thread.join(timeout=2.0)
        try:
            self._file.close()
        except Exception:
            pass
