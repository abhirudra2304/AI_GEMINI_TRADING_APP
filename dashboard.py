"""
Terminal Dashboard (IAS Plug-in Module)

Pure, read-only presentation layer for the Institutional Adaptive
Scanner. Renders a precomputed "master state" object to the terminal
using Rich. This module performs no calculation, scoring, ranking,
filtering, sorting, or business logic of any kind — it only formats and
displays whatever values it is given, in the order it is given them.

Design notes
------------
- Completely independent: no imports from config.py, data_broker.py,
  scanner_engine.py, or any other IAS module. TerminalDashboard only
  knows about plain dicts/dataclasses via duck-typed attribute/key
  access — it never imports the dataclasses it displays, so it can't
  accidentally depend on (or be broken by) their internals.
- Read-only end to end: no database access, no broker API calls, no
  mutation of `master_state` or anything else. `render_dashboard()`
  returns nothing; its only effect is printing to the terminal.
- Defensive by construction: every section is built inside its own
  try/except and every field is read through `_get()`, which returns
  "N/A" (or a caller-supplied default) instead of raising for a missing
  key, missing attribute, or None value. A malformed or partially empty
  `master_state` degrades individual panels to "No Data Available"; it
  never crashes the whole render.
- Only rich.layout, rich.table, rich.panel, rich.columns, rich.text and
  rich.console are used — no other UI framework.
- Forward-compatible by construction: any top-level `master_state` key
  outside the known section names (e.g. a future 'news_sentiment' or
  'options_flow' module) is picked up automatically and rendered as an
  extra generic panel in an "Additional Modules" row, without touching
  the layout code for the existing sections. See `_render_extension_row`.
- Logs at WARNING (with traceback) whenever a section's data is malformed
  enough to fall back to "No Data Available", and DEBUG on each
  `render_dashboard()`/`refresh()` call, via a module-level `logger`
  (standard `logging` module). Logging is purely diagnostic — it never
  changes what's rendered or raises.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Safe accessors / formatters (no business logic — pure formatting helpers)
# --------------------------------------------------------------------------- #

_MISSING = "N/A"


def _get(source: Any, key: str, default: Any = _MISSING) -> Any:
    """Reads `key` from `source` (dict or object) without ever raising.

    Args:
        source: A dict, a dataclass/object instance, or None.
        key: Field/attribute name to read.
        default: Value to return if `source` is None, `key` is absent,
            the read raises, or the resolved value is itself None.
    """
    try:
        if source is None:
            return default
        if isinstance(source, dict):
            value = source.get(key, default)
        else:
            value = getattr(source, key, default)
        return default if value is None else value
    except Exception:
        return default


def _fmt_num(value: Any, decimals: int = 2) -> str:
    """Formats a number to `decimals` places; passes through "N/A"
    unchanged; falls back to str(value) for anything unformattable."""
    if value == _MISSING:
        return _MISSING
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_pct(value: Any, decimals: int = 1) -> str:
    """Formats a value already expressed 0-100 as a '%' string."""
    if value == _MISSING:
        return _MISSING
    try:
        return f"{float(value):.{decimals}f}%"
    except (TypeError, ValueError):
        return str(value)


def _fmt_accel(value: Any) -> str:
    """Formats RS Acceleration, distinguishing "not enough trailing
    history to compute a 20-session-ago baseline yet" (NaN) from a
    missing/absent value ("N/A")."""
    if value == _MISSING:
        return _MISSING
    try:
        f_value = float(value)
        if f_value != f_value:  # NaN never equals itself
            return "N/A (insufficient history)"
        return f"{f_value:.2f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_patterns(value: Any) -> str:
    """Formats a pattern list (or single pattern string) as
    comma-joined text."""
    if value == _MISSING or value is None:
        return _MISSING
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(v) for v in value) if value else _MISSING
    return str(value)


def _no_data_panel(title: str) -> Panel:
    """The standard fallback rendered in place of any section that has
    no usable data — never an exception."""
    return Panel(
        Text("No Data Available", style="italic dim", justify="center"),
        title=title,
        border_style="grey50",
    )


def _safe_iterable(value: Any) -> Union[Sequence, Tuple]:
    """Returns `value` if it's a non-empty list/tuple, else an empty
    tuple — so callers can always iterate safely."""
    if isinstance(value, (list, tuple)) and len(value) > 0:
        return value
    return ()


# --------------------------------------------------------------------------- #
# TerminalDashboard
# --------------------------------------------------------------------------- #

class TerminalDashboard:
    """Renders a precomputed IAS master state to the terminal.

    Expected `master_state` shape (all keys optional — missing sections
    render as "No Data Available"):
        {
            "market_context": <object/dict with regime, confidence, score>,
            "sector_data": [<object/dict with rank, sector, percentile,
                              sector_multiplier>, ...],
            "relative_strength": [<object/dict with ticker, rs_rank,
                                    rs_percentile, rs_acceleration>, ...],
            "institutional_flow": [<object/dict with ticker, rvol,
                                     delivery_percent, accumulation_score>, ...],
            "breakouts": [<object/dict with ticker, detected_patterns,
                            base_quality, risk_reward_pct, confidence>, ...],
            "alpha_picks": [<object/dict with ticker, alpha_score,
                              probability, position_size, sector,
                              detected_patterns>, ...],
            "scanner_stats": <object/dict with stocks_scanned,
                               stocks_passed, market_regime, hit_rate,
                               false_positives, missed_winners, model_version>,
            "system_info": <object/dict with scanner_version,
                             position_size_recommendation>,
            ... any future section, e.g. "news_sentiment" ...
        }

    All lists are displayed in the order given — this class never sorts,
    filters, or recomputes anything.

    Example:
        dashboard = TerminalDashboard(master_state)
        dashboard.render_dashboard()
    """

    KNOWN_SECTIONS = frozenset(
        {
            "market_context",
            "sector_data",
            "relative_strength",
            "institutional_flow",
            "breakouts",
            "alpha_picks",
            "scanner_stats",
            "system_info",
        }
    )

    def __init__(self, master_state: Optional[Dict[str, Any]] = None):
        """
        Args:
            master_state: Precomputed dict of section name -> data, as
                described above. May be partially populated or None.
        """
        self.master_state: Dict[str, Any] = master_state if isinstance(master_state, dict) else {}
        self.console = Console()

    def refresh(self, master_state: Dict[str, Any]) -> None:
        """Replaces the displayed state and re-renders.

        Args:
            master_state: New master state dict to display.
        """
        self.master_state = master_state if isinstance(master_state, dict) else {}
        logger.debug("refresh: master_state replaced (%d top-level keys)", len(self.master_state))
        self.render_dashboard()

    def render_dashboard(self) -> None:
        """Renders the full dashboard to the terminal. Returns nothing;
        all output goes to `self.console`. Never raises — every section
        is individually fault-isolated."""
        logger.debug("render_dashboard: rendering with %d top-level keys", len(self.master_state))
        layout = self._build_layout()
        self.console.print(layout)

    # -- layout assembly -------------------------------------------------------- #

    def _build_layout(self) -> Layout:
        layout = Layout(name="root")

        extension_row = self._render_extension_row()

        sections = [
            Layout(self._render_header(), name="header", size=9),
            self._render_middle_row(),
            self._render_bottom_row(),
        ]
        if extension_row is not None:
            sections.append(Layout(extension_row, name="extensions", ratio=1))
        sections.append(Layout(self._render_footer(), name="footer", size=11))

        layout.split_column(*sections)
        return layout

    # -- header ------------------------------------------------------------------ #

    @staticmethod
    def _regime_style(regime: Any) -> str:
        """Bull -> green, Bear -> red, anything else (incl. Sideways /
        unknown) -> yellow. Pure display-color mapping, not analysis."""
        if not isinstance(regime, str):
            return "yellow"
        upper = regime.upper()
        if "BULL" in upper:
            return "bold green"
        if "BEAR" in upper:
            return "bold red"
        return "bold yellow"

    def _resolve_position_size(self) -> str:
        """Reads a header-level position-size recommendation from
        system_info first, falling back to the top alpha pick's
        position_size if system_info doesn't carry one. Both are just
        reads of already-computed values — no calculation happens here."""
        system_info = self.master_state.get("system_info")
        from_system_info = _get(system_info, "position_size_recommendation", None)
        if from_system_info is not None:
            return _fmt_num(from_system_info)

        alpha_picks = _safe_iterable(self.master_state.get("alpha_picks"))
        if alpha_picks:
            top_pick_size = _get(alpha_picks[0], "position_size", None)
            if top_pick_size is not None:
                return _fmt_num(top_pick_size)

        return _MISSING

    def _render_header(self) -> Panel:
        try:
            market_context = self.master_state.get("market_context")
            if market_context is None:
                return _no_data_panel("IAS — Market Overview")

            regime = _get(market_context, "regime")
            confidence = _get(market_context, "confidence")
            score = _get(market_context, "score", _get(market_context, "regime_score"))
            position_size = self._resolve_position_size()
            scanner_version = _get(self.master_state.get("system_info"), "scanner_version")
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            style = self._regime_style(regime)

            body = Text()
            body.append("Market Regime:  ", style="bold")
            body.append(f"{regime}\n", style=style)
            body.append("Confidence:     ", style="bold")
            body.append(f"{_fmt_pct(confidence)}\n")
            body.append("Regime Score:   ", style="bold")
            body.append(f"{_fmt_num(score)}\n")
            body.append("Position Size:  ", style="bold")
            body.append(f"{position_size}\n")
            body.append("Time:           ", style="bold")
            body.append(f"{now_str}\n")
            body.append("Scanner Ver.:   ", style="bold")
            body.append(f"{scanner_version}")

            return Panel(body, title="IAS — Market Overview", border_style=style)
        except Exception:
            logger.warning("_render_header: failed to render, falling back to No Data Available", exc_info=True)
            return _no_data_panel("IAS — Market Overview")

    # -- middle row ---------------------------------------------------------------- #

    def _render_middle_row(self) -> Layout:
        layout = Layout(name="middle", ratio=2)
        layout.split_row(
            Layout(self._render_sector_table()),
            Layout(self._render_rs_table()),
            Layout(self._render_institutional_table()),
        )
        return layout

    def _render_sector_table(self):
        title = "Top Sector Rotation"
        rows = _safe_iterable(self.master_state.get("sector_data"))
        if not rows:
            return _no_data_panel(title)
        try:
            table = Table(title=title, expand=True)
            table.add_column("Rank", justify="right")
            table.add_column("Sector")
            table.add_column("Percentile", justify="right")
            table.add_column("Multiplier", justify="right")
            for idx, row in enumerate(rows, start=1):
                table.add_row(
                    str(_get(row, "rank", idx)),
                    str(_get(row, "sector")),
                    _fmt_num(_get(row, "percentile")),
                    _fmt_num(_get(row, "sector_multiplier", _get(row, "multiplier"))),
                )
            return table
        except Exception:
            logger.warning("_render_sector_table: failed to render, falling back to No Data Available", exc_info=True)
            return _no_data_panel(title)

    def _render_rs_table(self):
        title = "Top Relative Strength"
        rows = _safe_iterable(self.master_state.get("relative_strength"))
        if not rows:
            return _no_data_panel(title)
        try:
            table = Table(title=title, expand=True)
            table.add_column("Ticker")
            table.add_column("RS Rank", justify="right")
            table.add_column("RS Pctl", justify="right")
            table.add_column("Accel", justify="right")
            for row in rows:
                table.add_row(
                    str(_get(row, "ticker")),
                    str(_get(row, "rs_rank")),
                    _fmt_num(_get(row, "rs_percentile")),
                    _fmt_accel(_get(row, "rs_acceleration")),
                )
            return table
        except Exception:
            logger.warning("_render_rs_table: failed to render, falling back to No Data Available", exc_info=True)
            return _no_data_panel(title)

    def _render_institutional_table(self):
        title = "Institutional Flow"
        rows = _safe_iterable(self.master_state.get("institutional_flow"))
        if not rows:
            return _no_data_panel(title)
        try:
            table = Table(title=title, expand=True)
            table.add_column("Ticker")
            table.add_column("RVOL", justify="right")
            table.add_column("Delivery %", justify="right")
            table.add_column("Accum. Score", justify="right")
            for row in rows:
                table.add_row(
                    str(_get(row, "ticker")),
                    _fmt_num(_get(row, "rvol")),
                    _fmt_pct(_get(row, "delivery_percent")),
                    _fmt_num(_get(row, "accumulation_score")),
                )
            return table
        except Exception:
            logger.warning(
                "_render_institutional_table: failed to render, falling back to No Data Available", exc_info=True
            )
            return _no_data_panel(title)

    # -- bottom row ---------------------------------------------------------------- #

    def _render_bottom_row(self) -> Layout:
        layout = Layout(name="bottom", ratio=2)
        layout.split_row(
            Layout(self._render_breakout_table()),
            Layout(self._render_alpha_picks_panel()),
        )
        return layout

    def _render_breakout_table(self):
        title = "Breakout Candidates"
        rows = _safe_iterable(self.master_state.get("breakouts"))
        if not rows:
            return _no_data_panel(title)
        try:
            table = Table(title=title, expand=True)
            table.add_column("Ticker")
            table.add_column("Pattern")
            table.add_column("Base Qty", justify="right")
            table.add_column("Risk %", justify="right")
            table.add_column("Confidence", justify="right")
            for row in rows:
                table.add_row(
                    str(_get(row, "ticker")),
                    _fmt_patterns(_get(row, "detected_patterns", _get(row, "pattern"))),
                    _fmt_num(_get(row, "base_quality")),
                    _fmt_pct(_get(row, "risk_reward_pct", _get(row, "risk_pct"))),
                    _fmt_num(_get(row, "confidence")),
                )
            return table
        except Exception:
            logger.warning("_render_breakout_table: failed to render, falling back to No Data Available", exc_info=True)
            return _no_data_panel(title)

    def _render_alpha_picks_panel(self) -> Panel:
        title = "★★★★★ FINAL ALPHA PICKS ★★★★★"
        rows = _safe_iterable(self.master_state.get("alpha_picks"))
        if not rows:
            return Panel(
                Text("No Data Available", style="italic dim", justify="center"),
                title=title,
                border_style="bold yellow",
            )
        try:
            table = Table(expand=True, show_edge=False, header_style="bold yellow")
            table.add_column("Ticker", style="bold")
            table.add_column("Alpha", justify="right")
            table.add_column("Prob.", justify="right")
            table.add_column("Pos. Size", justify="right")
            table.add_column("Sector")
            table.add_column("Pattern")
            for row in rows:
                table.add_row(
                    str(_get(row, "ticker")),
                    _fmt_num(_get(row, "alpha_score")),
                    _fmt_num(_get(row, "probability")),
                    _fmt_num(_get(row, "position_size")),
                    str(_get(row, "sector")),
                    _fmt_patterns(_get(row, "detected_patterns", _get(row, "pattern"))),
                )
            return Panel(
                table,
                title=title,
                border_style="bold yellow",
                style="on grey15",
                padding=(1, 1),
            )
        except Exception:
            logger.warning(
                "_render_alpha_picks_panel: failed to render, falling back to No Data Available", exc_info=True
            )
            return Panel(
                Text("No Data Available", style="italic dim", justify="center"),
                title=title,
                border_style="bold yellow",
            )

    # -- extension row (future modules) --------------------------------------------- #

    def _render_extension_row(self) -> Optional[Columns]:
        """Renders any top-level `master_state` key not in
        `KNOWN_SECTIONS` as a generic panel, so future modules (news
        sentiment, options flow, FII/DII flow, earnings calendar, ...)
        show up automatically without any change to the fixed layout
        sections above. Returns None (no row) when there are no such
        keys, so the existing layout is unaffected today."""
        extra_keys = [k for k in self.master_state.keys() if k not in self.KNOWN_SECTIONS]
        if not extra_keys:
            return None

        panels = []
        for key in extra_keys:
            panels.append(self._render_generic_section(key, self.master_state.get(key)))
        return Columns(panels, equal=True, expand=True)

    def _render_generic_section(self, key: str, value: Any) -> Panel:
        """Best-effort generic renderer for an unknown section: a list of
        rows becomes a table of whatever fields the first row has; a
        dict/dataclass becomes a key/value table; anything else is shown
        as text. Formatting only — no interpretation of the values."""
        title = key.replace("_", " ").title()
        try:
            rows = _safe_iterable(value)
            if rows:
                sample = rows[0]
                field_names = list(sample.keys()) if isinstance(sample, dict) else list(vars(sample).keys())
                table = Table(title=title, expand=True)
                for field_name in field_names:
                    table.add_column(str(field_name))
                for row in rows:
                    table.add_row(*(str(_get(row, f)) for f in field_names))
                return Panel(table, border_style="cyan")

            if isinstance(value, dict) or hasattr(value, "__dict__"):
                items = value.items() if isinstance(value, dict) else vars(value).items()
                table = Table(show_header=False, expand=True)
                table.add_column("Field")
                table.add_column("Value")
                for field_name, field_value in items:
                    table.add_row(str(field_name), str(field_value))
                return Panel(table, title=title, border_style="cyan")

            return Panel(Text(str(value)), title=title, border_style="cyan")
        except Exception:
            logger.warning("_render_generic_section(%s): failed to render, falling back to No Data Available", key, exc_info=True)
            return _no_data_panel(title)

    # -- footer ------------------------------------------------------------------ #

    def _render_footer(self) -> Panel:
        title = "Scanner Statistics"
        stats = self.master_state.get("scanner_stats")
        if stats is None:
            return _no_data_panel(title)
        try:
            table = Table(show_header=False, expand=True, box=None)
            table.add_column("Field", style="bold")
            table.add_column("Value")

            fields = [
                ("Stocks Scanned", _get(stats, "stocks_scanned")),
                ("Stocks Passed", _get(stats, "stocks_passed")),
                ("Market Regime", _get(stats, "market_regime")),
                ("Current Hit Rate", _fmt_pct(_get(stats, "hit_rate"))),
                ("False Positives (Last Week)", _get(stats, "false_positives_last_week", _get(stats, "false_positives"))),
                ("Missed Winners (Last Week)", _get(stats, "missed_winners_last_week", _get(stats, "missed_winners"))),
                ("Current Model Version", _get(stats, "model_version")),
            ]
            for label, val in fields:
                table.add_row(label, str(val))

            return Panel(table, title=title, border_style="blue")
        except Exception:
            logger.warning("_render_footer: failed to render, falling back to No Data Available", exc_info=True)
            return _no_data_panel(title)
