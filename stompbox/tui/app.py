"""Stompbox TUI — Textual-based terminal interface for the audio engine.

Read-mostly dashboard: signal chain, VU meters, MIDI monitor.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Footer, Static

if TYPE_CHECKING:
    from ..engine import Engine


# ── Rendering helpers ────────────────────────────────────────────────

METER_MIN_DB = -60.0
METER_MAX_DB = 3.0
METER_RANGE = METER_MAX_DB - METER_MIN_DB

# Sub-cell precision characters (left-fill within a cell)
_FILL = " ▏▎▍▌▋▊▉█"


def _db_to_pos(db: float, width: int) -> float:
    """Map dB to a float position in [0, width]."""
    return max(0.0, min(float(width), (db - METER_MIN_DB) / METER_RANGE * width))


def _meter_color(frac: float) -> str:
    """Color for a meter position (0-1 of full range)."""
    if frac < 0.6:
        return "green"
    if frac < 0.82:
        return "yellow"
    return "red"


def render_meter(level_db: float, peak_db: float, width: int = 40) -> Text:
    """Render a horizontal VU meter bar with sub-cell precision and peak hold."""
    text = Text()
    level_pos = _db_to_pos(level_db, width)
    peak_pos = _db_to_pos(peak_db, width)
    peak_cell = int(peak_pos)

    for i in range(width):
        frac = i / width

        if i + 1 <= level_pos:
            # Fully filled
            text.append("█", style=_meter_color(frac))
        elif i < level_pos:
            # Partial fill (boundary)
            sub = int((level_pos - i) * 8)
            sub = max(1, min(8, sub))
            text.append(_FILL[sub], style=_meter_color(frac))
        elif i == peak_cell and peak_db > METER_MIN_DB + 3:
            # Peak indicator
            text.append("▎", style="bright_white")
        else:
            text.append("·", style="#333333")

    return text


def format_db(db: float) -> str:
    """Format dB value for display."""
    if db <= -90:
        return "  -∞  "
    return f"{db:+.1f}dB"


def render_chain_row(slots: list, term_width: int) -> Text:
    """Render the signal chain as a compact horizontal strip.

    Layout per slot:  ╭─ Name ─╮
                      │ -X.X dB│
                      ╰────────╯
    Connected by ─▸ arrows.
    """
    if not slots:
        return Text("  (no plugins loaded)", style="dim italic")

    num_slots = len(slots)
    # Arrow takes 4 chars: " ─▸ "
    arrows_width = (num_slots - 1) * 4
    label_in_out = 12  # "IN ─▸" + "─▸ OUT"
    avail = max(term_width - arrows_width - label_in_out - 4, num_slots * 8)
    box_w = max(10, min(18, avail // num_slots))
    inner = box_w - 2  # inside the │ borders

    line_top = Text()
    line_mid = Text()
    line_bot = Text()
    line_lvl = Text()
    line_cc = Text()

    # IN label
    line_top.append("       ")
    line_mid.append(" IN ─▸ ", style="bold")
    line_bot.append("       ")
    line_lvl.append("       ")
    line_cc.append("       ")

    for idx, slot in enumerate(slots):
        bypassed = slot.bypassed
        bdr = "#555555" if bypassed else "cyan"
        name_style = "dim strike" if bypassed else "bold"

        name = slot.name[:inner].center(inner)

        # Level
        level, _peak = slot.meter.read()
        if level <= -90:
            lvl_str = "·".center(inner)
            lvl_style = "#444444"
        else:
            lvl_str = f"{level:+.1f}".center(inner)
            if level > -1:
                lvl_style = "red"
            elif level > -6:
                lvl_style = "yellow"
            elif level > -40:
                lvl_style = "green"
            else:
                lvl_style = "#666666"

        # CC annotations
        cc_label = ""
        if slot.midi_cc:
            cc_nums = " ".join(f"CC{n}" for n in sorted(slot.midi_cc))
            cc_label = cc_nums[:inner].center(inner)

        line_top.append("╭" + "─" * inner + "╮", style=bdr)
        line_mid.append("│", style=bdr)
        line_mid.append(name, style=name_style)
        line_mid.append("│", style=bdr)
        line_lvl.append("│", style=bdr)
        line_lvl.append(lvl_str, style=lvl_style)
        line_lvl.append("│", style=bdr)
        line_bot.append("╰" + "─" * inner + "╯", style=bdr)
        line_cc.append(cc_label.ljust(box_w) if cc_label else " " * box_w, style="dim cyan")

        # Arrow to next
        if idx < num_slots - 1:
            line_top.append("    ")
            line_mid.append(" ─▸ ", style="#666666")
            line_lvl.append("    ")
            line_bot.append("    ")
            line_cc.append("    ")

    # OUT label
    line_top.append("      ")
    line_mid.append(" ─▸ OUT", style="bold")
    line_bot.append("      ")
    line_lvl.append("      ")
    line_cc.append("      ")

    result = Text()
    result.append_text(line_top)
    result.append("\n")
    result.append_text(line_mid)
    result.append("\n")
    result.append_text(line_lvl)
    result.append("\n")
    result.append_text(line_bot)
    result.append("\n")
    result.append_text(line_cc)
    return result


# ── Widgets ──────────────────────────────────────────────────────────


class StatusBar(Static):
    """Top-line status: chain name, audio format, device names."""

    DEFAULT_CSS = """
    StatusBar {
        dock: top;
        height: 1;
        background: #1a1a2e;
        color: #aaaacc;
        padding: 0 1;
    }
    """

    def __init__(self, engine: Engine, **kw):
        super().__init__(**kw)
        self._engine = engine

    def render(self) -> Text:
        e = self._engine
        t = Text()
        t.append(" STOMPBOX ", style="bold #e0e0ff on #2a2a4a")
        t.append("  ")
        t.append(e.chain_name, style="bold cyan")
        t.append("  │  ", style="#555555")
        rate_k = e.sample_rate / 1000
        if rate_k == int(rate_k):
            t.append(f"{int(rate_k)}k", style="#888888")
        else:
            t.append(f"{rate_k:.1f}k", style="#888888")
        t.append(f" · {e.buffer_size}buf · {e.channels}ch", style="#666666")
        t.append("  │  ", style="#555555")

        t.append(e.input_device_name[:20], style="#888888")
        t.append(" → ", style="#555555")
        t.append(e.output_device_name[:20], style="#888888")

        if e.xruns > 0:
            t.append(f"  ⚠ {e.xruns} xruns", style="bold red")

        return t


class ChainView(Static):
    """Signal chain visualization — the pedal boxes."""

    DEFAULT_CSS = """
    ChainView {
        height: 5;
        padding: 0 0;
        background: #111118;
    }
    """

    def __init__(self, engine: Engine, **kw):
        super().__init__(**kw)
        self._engine = engine

    def render(self) -> Text:
        w = self.size.width or 80
        return render_chain_row(self._engine.chain.slots, w)


class MeterPanel(Static):
    """Input and output level meters."""

    DEFAULT_CSS = """
    MeterPanel {
        height: 4;
        padding: 0 1;
        background: #0d0d14;
    }
    """

    def __init__(self, engine: Engine, **kw):
        super().__init__(**kw)
        self._engine = engine

    def render(self) -> Text:
        e = self._engine
        in_lvl, in_pk = e.chain.input_meter.read()
        out_lvl, out_pk = e.chain.output_meter.read()

        # Meter bar width: terminal width - labels - dB readout - peak readout - padding
        w = max(20, (self.size.width or 80) - 32)

        t = Text()

        # Separator
        t.append("  ─" * ((self.size.width or 80) // 3), style="#333333")
        t.append("\n")

        # Input meter
        t.append("  IN  ", style="bold #aaaaaa")
        t.append_text(render_meter(in_lvl, in_pk, w))
        t.append(f"  {format_db(in_lvl)}", style="#aaaaaa")
        t.append(f"  pk {format_db(in_pk)}", style="#666666")
        t.append("\n")

        # Output meter
        t.append("  OUT ", style="bold #cccccc")
        t.append_text(render_meter(out_lvl, out_pk, w))
        t.append(f"  {format_db(out_lvl)}", style="#cccccc")
        t.append(f"  pk {format_db(out_pk)}", style="#666666")

        return t


class MidiPanel(Static):
    """MIDI status — fixed-width columns, no layout jitter."""

    DEFAULT_CSS = """
    MidiPanel {
        height: 3;
        background: #16161e;
        color: #888899;
        padding: 0 1;
    }
    """

    def __init__(self, engine: Engine, **kw):
        super().__init__(**kw)
        self._engine = engine
        # Sticky state: last seen values persist until replaced
        self._last_note: str = "───"
        self._last_note_ch: str = "  "
        self._last_note_vel: str = "   "
        self._last_cc: str = "      "
        self._last_cc_ch: str = "  "
        self._last_pc: str = "    "
        self._note_age: float = 99.0
        self._cc_age: float = 99.0

    def _update_sticky(self) -> None:
        import time as _time

        now = _time.monotonic()
        recent = self._engine.meters.recent_midi(max_age=10.0)
        for ev in recent:
            if ev.kind == "note_on":
                from stompbox.meter import _note_name

                self._last_note = f"{_note_name(ev.data1):>3s}"
                self._last_note_ch = f"{ev.channel + 1:>2d}"
                self._last_note_vel = f"{ev.data2:>3d}"
                self._note_age = now - ev.timestamp
            elif ev.kind == "note_off":
                from stompbox.meter import _note_name

                self._last_note = f"{_note_name(ev.data1):>3s}"
                self._last_note_ch = f"{ev.channel + 1:>2d}"
                self._last_note_vel = "off"
                self._note_age = now - ev.timestamp
            elif ev.kind == "cc":
                self._last_cc = f"CC{ev.data1:<3d}{ev.data2:>3d}"
                self._last_cc_ch = f"{ev.channel + 1:>2d}"
                self._cc_age = now - ev.timestamp
            elif ev.kind == "pc":
                self._last_pc = f"PC {ev.data1:<3d}"

    def render(self) -> Text:
        e = self._engine
        self._update_sticky()

        t = Text()

        # Separator
        t.append("  ─" * ((self.size.width or 80) // 3), style="#333333")
        t.append("\n")

        # Fixed-width status line:
        # "  MIDI ▸ Port Name          │ omni │ Note  C#4 ch 1 vel 127 │ CC 74 127 │ PC  2  "
        t.append("  MIDI ", style="bold #777799")

        port = e.midi_port_name
        if port:
            t.append(f"▸ {port:<22s}", style="cyan")
        else:
            t.append(f"▸ {'(none)':<22s}", style="#555555")

        t.append("│ ", style="#333333")

        # Channel filter — fixed 4 chars
        ch = e.config.midi.channel
        if ch is not None:
            t.append(f"ch{ch:<2d}", style="#aaaacc")
        else:
            t.append("omni", style="#666688")

        t.append(" │ ", style="#333333")

        # Last note — fixed width: "Note C#4 ch 1 vel 127" = 21 chars
        note_dim = self._note_age > 3.0
        ns = "#555555" if note_dim else "green"
        ls = "#555555" if note_dim else "#888888"
        t.append("note ", style=ls)
        t.append(self._last_note, style=ns)
        t.append(" ch", style=ls)
        t.append(self._last_note_ch, style=ns)
        t.append(" vel", style=ls)
        t.append(self._last_note_vel, style=ns)

        t.append(" │ ", style="#333333")

        # Last CC — fixed width: "CC 74 127" = 9 chars
        cc_dim = self._cc_age > 3.0
        cs = "#555555" if cc_dim else "cyan"
        t.append(self._last_cc, style=cs)

        t.append(" │ ", style="#333333")

        # Last PC — fixed width
        t.append(self._last_pc, style="#888888")

        return t


# ── Main App ─────────────────────────────────────────────────────────


class StompboxApp(App):
    """Stompbox TUI application."""

    TITLE = "stompbox"
    CSS = """
    Screen {
        background: #111118;
        layout: vertical;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("1", "bypass(0)", "1", show=False),
        Binding("2", "bypass(1)", "2", show=False),
        Binding("3", "bypass(2)", "3", show=False),
        Binding("4", "bypass(3)", "4", show=False),
        Binding("5", "bypass(4)", "5", show=False),
        Binding("6", "bypass(5)", "6", show=False),
        Binding("7", "bypass(6)", "7", show=False),
        Binding("8", "bypass(7)", "8", show=False),
        Binding("9", "bypass(8)", "9", show=False),
        Binding("space", "master_bypass", "Bypass All", key_display="SPC"),
        Binding("r", "reset_peaks", "Reset Peaks"),
        Binding("bracketleft", "prev_chain", "Prev Chain"),
        Binding("bracketright", "next_chain", "Next Chain"),
    ]

    def __init__(self, engine: Engine, **kw):
        super().__init__(**kw)
        self.engine = engine
        self._chain_list: list[str] = []
        self._chain_index: int = 0

    def compose(self) -> ComposeResult:
        yield StatusBar(self.engine, id="status")
        yield ChainView(self.engine, id="chain")
        yield MeterPanel(self.engine, id="meters")
        yield MidiPanel(self.engine, id="midi")
        yield Footer()

    def on_mount(self) -> None:
        self.engine.start()
        self._chain_list = self.engine.available_chains()
        # 30 fps refresh for meters; 10 fps for chain view
        self.set_interval(1 / 30, self._tick_fast)
        self.set_interval(1 / 10, self._tick_slow)

    def _tick_fast(self) -> None:
        """High-frequency refresh: meters and MIDI."""
        try:
            self.query_one("#meters", MeterPanel).refresh()
            self.query_one("#midi", MidiPanel).refresh()
        except Exception:
            pass

    def _tick_slow(self) -> None:
        """Low-frequency refresh: chain view and status."""
        try:
            self.query_one("#chain", ChainView).refresh()
            self.query_one("#status", StatusBar).refresh()
        except Exception:
            pass

    def action_bypass(self, index: int) -> None:
        self.engine.toggle_bypass(index)

    def action_master_bypass(self) -> None:
        self.engine.master_bypass()

    def action_reset_peaks(self) -> None:
        self.engine.reset_peaks()

    def action_prev_chain(self) -> None:
        if not self._chain_list:
            return
        self._chain_index = (self._chain_index - 1) % len(self._chain_list)
        name = self._chain_list[self._chain_index]
        self.engine.load_chain_file(f"chains/{name}.yml")

    def action_next_chain(self) -> None:
        if not self._chain_list:
            return
        self._chain_index = (self._chain_index + 1) % len(self._chain_list)
        name = self._chain_list[self._chain_index]
        self.engine.load_chain_file(f"chains/{name}.yml")

    async def action_quit(self) -> None:
        self.engine.stop()
        self.exit()
