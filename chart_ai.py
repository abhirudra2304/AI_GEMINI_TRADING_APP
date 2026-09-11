"""Multimodal Visual Chart AI Module

Renders high-resolution dark-themed candlestick charts and feeds them into
Gemini 2.5 Computer Vision to evaluate:
1. Mark Minervini Volatility Contraction Patterns (VCP) & Contraction Tightening
2. Base Geometry: Cup & Handle, High Tight Flag, Double Bottom, Flat Base, False Breakout
3. Volume Signature: Institutional Accumulation vs Volume Dry-Up vs Distribution
4. Visual Quality Score (1.0 to 10.0) & Key Breakout Pivot Levels
"""
import io
import config
import json
import logging
import os
import re
from datetime import datetime
from typing import Optional, Dict, Any, Tuple

import matplotlib
matplotlib.use('Agg')  # Non-interactive background backend
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
from dotenv import load_dotenv

from utils import install_and_import

# override=True: this project's .env is the authority for its OWN
# credentials. Without it load_dotenv() silently defers to whatever is
# already in the environment - and a stale OS-level GEMINI_API_KEY (13
# chars) shadowed the real 39-char .env key, so every Gemini call failed
# 400 API_KEY_INVALID no matter how often .env was corrected (2026-09-11).
load_dotenv(override=True)
logger = logging.getLogger(__name__)

CHARTS_DIR = 'charts'


def _ensure_charts_dir() -> str:
    if not os.path.exists(CHARTS_DIR):
        os.makedirs(CHARTS_DIR, exist_ok=True)
    return CHARTS_DIR


def _calculate_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def render_candlestick_chart(
    df_daily: pd.DataFrame,
    symbol: str,
    lookback_bars: int = 120,
    save_path: Optional[str] = None,
) -> Optional[bytes]:
    """Renders a dark-themed institutional candlestick chart with EMA20, EMA50,
    Pivot resistance, and Volume SMA.

    Returns:
        bytes: Raw PNG image bytes of the chart.
    """
    if df_daily is None or df_daily.empty or len(df_daily) < 20:
        logger.warning(f"Insufficient daily candles to render chart for {symbol}.")
        return None

    df = df_daily.copy()

    # Standardize column casing
    col_map = {c: c.capitalize() for c in df.columns}
    df = df.rename(columns=col_map)
    for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
        if col not in df.columns:
            logger.warning(f"Missing required candle column '{col}' for {symbol}.")
            return None

    # Handle datetime index
    if 'Timestamp' in df.columns:
        df['Date'] = pd.to_datetime(df['Timestamp'])
    elif isinstance(df.index, pd.DatetimeIndex):
        df['Date'] = df.index
    else:
        df['Date'] = pd.date_range(end=datetime.now(), periods=len(df), freq='D')

    df['Date'] = pd.to_datetime(df['Date']).dt.tz_localize(None)
    df = df.sort_values('Date').reset_index(drop=True)

    # Calculate indicators
    df['EMA20'] = _calculate_ema(df['Close'], 20)
    df['EMA50'] = _calculate_ema(df['Close'], 50)
    df['VolSMA20'] = df['Volume'].rolling(window=20, min_periods=5).mean()
    df['Pivot250'] = df['High'].rolling(window=min(250, len(df)), min_periods=20).max()

    # Slice to lookback window
    slice_df = df.tail(lookback_bars).copy().reset_index(drop=True)
    if len(slice_df) < 15:
        return None

    # Set up dark theme figure with 2 subplots (Price + Volume)
    fig = plt.figure(figsize=(12, 7), facecolor='#121212', dpi=120)
    gs = fig.add_gridspec(2, 1, height_ratios=[3.2, 1.0], hspace=0.08)

    ax_price = fig.add_subplot(gs[0])
    ax_vol = fig.add_subplot(gs[1], sharex=ax_price)

    # Style axes
    for ax in [ax_price, ax_vol]:
        ax.set_facecolor('#181818')
        ax.tick_params(colors='#b0b0b0', labelsize=8)
        ax.grid(True, linestyle='--', alpha=0.18, color='#555555')
        for spine in ax.spines.values():
            spine.set_color('#333333')

    # Draw Candlesticks
    dates = slice_df.index.values
    opens = slice_df['Open'].values
    highs = slice_df['High'].values
    lows = slice_df['Low'].values
    closes = slice_df['Close'].values
    volumes = slice_df['Volume'].values

    bullish = closes >= opens
    bearish = ~bullish

    # Candle wicks
    ax_price.vlines(dates[bullish], lows[bullish], highs[bullish], color='#00e676', linewidth=1.0, alpha=0.9)
    ax_price.vlines(dates[bearish], lows[bearish], highs[bearish], color='#ff1744', linewidth=1.0, alpha=0.9)

    # Candle bodies
    body_width = 0.65
    for i in range(len(slice_df)):
        o, c = opens[i], closes[i]
        bottom = min(o, c)
        height = max(abs(c - o), 0.05)
        color = '#00e676' if c >= o else '#ff1744'
        ax_price.bar(dates[i], height, bottom=bottom, width=body_width, color=color, alpha=0.95, edgecolor=color)

    # Plot Moving Averages & Pivot
    ax_price.plot(dates, slice_df['EMA20'].values, color='#00e5ff', linewidth=1.4, label='EMA 20')
    ax_price.plot(dates, slice_df['EMA50'].values, color='#ff9100', linewidth=1.4, label='EMA 50')

    if 'Pivot250' in slice_df.columns and slice_df['Pivot250'].notna().any():
        pivot_val = slice_df['Pivot250'].iloc[-1]
        ax_price.axhline(pivot_val, color='#ffd600', linestyle='--', linewidth=1.1, alpha=0.75, label=f'52W / 250d Pivot ({pivot_val:.1f})')

    # Plot Volume
    vol_colors = np.where(bullish, '#00e676', '#ff1744')
    ax_vol.bar(dates, volumes, color=vol_colors, width=body_width, alpha=0.75)
    if 'VolSMA20' in slice_df.columns:
        ax_vol.plot(dates, slice_df['VolSMA20'].values, color='#d500f9', linewidth=1.2, label='Vol SMA 20')

    # Format X-axis date labels
    step = max(len(slice_df) // 8, 1)
    tick_indices = list(range(0, len(slice_df), step))
    if tick_indices[-1] != len(slice_df) - 1:
        tick_indices.append(len(slice_df) - 1)

    tick_labels = [slice_df['Date'].iloc[i].strftime('%d %b %y') for i in tick_indices]
    ax_vol.set_xticks(tick_indices)
    ax_vol.set_xticklabels(tick_labels, rotation=0, ha='center')
    plt.setp(ax_price.get_xticklabels(), visible=False)

    # Title & Legends
    last_close = closes[-1]
    last_date = slice_df['Date'].iloc[-1].strftime('%Y-%m-%d')
    ax_price.set_title(
        f"{symbol} — Daily Price & Volume Structure  |  LTP: ₹{last_close:.2f}  |  As of {last_date}",
        color='#ffffff',
        fontsize=12,
        fontweight='bold',
        pad=10,
    )
    ax_price.legend(loc='upper left', facecolor='#1e1e1e', edgecolor='#333333', labelcolor='#ffffff', fontsize=8)
    ax_vol.legend(loc='upper left', facecolor='#1e1e1e', edgecolor='#333333', labelcolor='#ffffff', fontsize=7)

    # Save to buffer
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', facecolor=fig.get_facecolor(), edgecolor='none')
    plt.close(fig)
    buf.seek(0)
    image_bytes = buf.getvalue()

    if save_path:
        try:
            with open(save_path, 'wb') as f:
                f.write(image_bytes)
            logger.info(f"🎨 Saved candlestick chart to {save_path}")
        except Exception as e:
            logger.warning(f"Could not save chart image to file: {e}")

    return image_bytes


def evaluate_chart_with_gemini(image_bytes: bytes, symbol: str) -> Dict[str, Any]:
    """Sends the rendered candlestick chart image to Gemini 2.5 Vision
    for Minervini VCP, base quality, and breakout geometry evaluation."""
    fallback_res = {
        'pattern_type': 'UNKNOWN',
        'vcp_contractions': 'N/A',
        'visual_quality_score': 5.0,
        'visual_verdict': 'DEVELOPING_BASE',
        'volume_signature': 'CHOPPY',
        'key_pivot_price': None,
        'one_line_takeaway': 'Visual evaluation unavailable.',
    }

    if not image_bytes:
        return fallback_res

    genai = install_and_import('google-genai', critical=False)
    api_key = os.getenv('GEMINI_API_KEY')
    if not genai or not api_key:
        logger.warning("Gemini API key or google-genai package not available for Visual Chart AI.")
        return fallback_res

    prompt = (
        f"You are a master equity technician and CMT analyst specialized in Mark Minervini's Volatility Contraction Pattern (VCP) "
        f"and William O'Neil / CANSLIM base breakout methodology.\n\n"
        f"Inspect this daily candlestick chart for {symbol} (including EMA20 cyan line, EMA50 orange line, 250d Pivot yellow line, and Volume panel below).\n\n"
        "Evaluate the following visual chart properties:\n"
        "1. Volatility Contraction Pattern (VCP): Are there sequential tightening contractions (e.g. 3T / 4T: 15% -> 8% -> 3%)?\n"
        "2. Volume Signature: Does volume dry up (contract) significantly on the right side of the base / pullbacks, and surge on up-bars?\n"
        "3. Base Pattern Type: Choose exactly one: 'VCP_CONTRACTION', 'CUP_AND_HANDLE', 'HIGH_TIGHT_FLAG', 'DOUBLE_BOTTOM', 'FLAT_BASE', 'EXTENDED_CHASE', 'ERRATIC_CHOP'.\n"
        "4. Visual Quality Score: Rate the cleanliness, tightness, and institutional setup quality on a 1.0 to 10.0 scale (where 10.0 is a textbook flawless Minervini pivot).\n"
        "5. Visual Verdict: Choose exactly one: 'PRIME_BUY_PIVOT', 'DEVELOPING_BASE', 'EXTENDED_WAIT_PULLBACK', 'ERRATIC_AVOID'.\n"
        "6. Key Breakout Pivot Price: The exact price level representing the trigger/pivot.\n"
        "7. One-Line Takeaway: Concise summary of the visual chart geometry.\n\n"
        "Respond ONLY with a JSON object in this exact schema:\n"
        "{\n"
        '  "pattern_type": "VCP_CONTRACTION",\n'
        '  "vcp_contractions": "3-T Contraction: 14% -> 6.5% -> 2.2% with volume dry-up",\n'
        '  "visual_quality_score": 8.5,\n'
        '  "visual_verdict": "PRIME_BUY_PIVOT",\n'
        '  "volume_signature": "INSTITUTIONAL_ACCUMULATION",\n'
        '  "key_pivot_price": 20850.0,\n'
        '  "one_line_takeaway": "Tight 3-T volatility contraction resting on rising 20-EMA with drying volume ready for breakout."\n'
        "}"
    )

    try:
        from google.genai import types
        client = genai.Client(api_key=api_key)

        image_part = types.Part.from_bytes(data=image_bytes, mime_type="image/png")

        config_kwargs = {}
        try:
            config_kwargs["response_mime_type"] = "application/json"
        except Exception:
            pass

        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=[image_part, prompt],
            config=types.GenerateContentConfig(**config_kwargs) if config_kwargs else None
        )

        raw_text = response.text.strip() if response and response.text else ""
        if not raw_text:
            return fallback_res

        if raw_text.startswith("```"):
            raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
            raw_text = re.sub(r"\s*```$", "", raw_text)

        parsed = json.loads(raw_text)
        return {
            'pattern_type': str(parsed.get('pattern_type', 'UNKNOWN')).upper(),
            'vcp_contractions': str(parsed.get('vcp_contractions', 'N/A')),
            'visual_quality_score': float(parsed.get('visual_quality_score', 5.0)),
            'visual_verdict': str(parsed.get('visual_verdict', 'DEVELOPING_BASE')).upper(),
            'volume_signature': str(parsed.get('volume_signature', 'CHOPPY')).upper(),
            'key_pivot_price': float(parsed['key_pivot_price']) if parsed.get('key_pivot_price') is not None else None,
            'one_line_takeaway': str(parsed.get('one_line_takeaway', '')),
        }
    except Exception as e:
        logger.warning(f"Gemini Visual Chart AI analysis failed for {symbol}: {e}")
        return fallback_res


def analyze_symbol_chart(df_daily: pd.DataFrame, symbol: str, save_image: bool = True) -> Dict[str, Any]:
    """End-to-end function: renders candlestick chart and evaluates with Gemini 2.5 Vision."""
    symbol = symbol.strip().upper()
    charts_dir = _ensure_charts_dir()
    save_path = os.path.join(charts_dir, f"{symbol}.png") if save_image else None

    image_bytes = render_candlestick_chart(df_daily, symbol, lookback_bars=120, save_path=save_path)
    if not image_bytes:
        return {
            'pattern_type': 'NO_DATA',
            'vcp_contractions': 'N/A',
            'visual_quality_score': 0.0,
            'visual_verdict': 'ERRATIC_AVOID',
            'volume_signature': 'NO_DATA',
            'key_pivot_price': None,
            'one_line_takeaway': 'Could not render chart.',
            'chart_path': None,
        }

    analysis = evaluate_chart_with_gemini(image_bytes, symbol)
    analysis['chart_path'] = save_path
    return analysis
