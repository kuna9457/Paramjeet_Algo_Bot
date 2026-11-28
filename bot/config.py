"""Loads config/settings.yaml + .env into simple attribute objects."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS = ROOT / "config" / "settings.yaml"
PRESETS_DIR = ROOT / "config" / "presets"
STRATEGY_VERSIONS = ("v7.4", "v8.0", "v8.7", "v9.2")
SL_MODES = ("Fixed Points (Preset)", "Swing High/Low", "Custom Points", "ATR Adaptive (3.0x)")


def load_preset(version: str) -> dict:
    """Strategy block of config/presets/<version>.yaml (Pine defaults of that version)."""
    with open(PRESETS_DIR / f"{version}.yaml", "r", encoding="utf-8") as fh:
        return (yaml.safe_load(fh) or {}).get("strategy", {})


@dataclass
class StrategySettings:
    # 0. Logic version: "v7.4" (original, default) | "v8.0" (per-trigger lock) | "v8.7" (HTML-backtest sync)
    #    | "v9.2" (Trial v9.2: ATR SL, 1.25R / 8-bar 0.5R exits, 0.55R break-even, 2-loss hold)
    strategy_version: str = "v7.4"
    # 1. IDM
    strat1_IDM: bool = True
    idmTf: str = "15"
    idmLookback: int = 3
    strat6_IDM_Precision: bool = False
    strat8_HTF_IDM_Trap: bool = False
    # 2. CHoCH
    strat2_CHOCH: bool = True
    chochStructureTf: str = "60"
    strat9_Fake_CHOCH: bool = True
    htfSwingLength: int = 8
    swingLength: int = 15
    # 3. OB
    strat3_OB: bool = False
    obTimeframe: str = ""
    obMitigation: str = "Wick Touch"
    maxOrderBlocks: int = 5
    # 4. FVG
    strat4_FVG: bool = False
    fvgTimeframe: str = ""
    fvgAtrMultiplier: float = 0.25
    maxFvgCount: int = 5
    # 5. HTF / filters
    strat5_MTF: bool = True
    htfTimeframe: str = "60"
    strat7_PD_Filter: bool = True
    strat12_OB_Confirm: bool = True
    strat13_Psychology1: bool = True
    strat10_Momentum_CISD: bool = False
    minDisplacementPct: float = 40.0
    strat11_3Condition: bool = False
    strat16_POI_Confluence: bool = False
    filterEmaSqueeze: bool = False
    enableChopFilter: bool = False
    chopIndexMax: float = 61.8
    enableAdxFilter: bool = False
    adxThreshold: float = 20.0
    opposingZoneBufferAtr: float = 1.5
    signalCooldownBars: int = 5
    filterDuplicateTrades: bool = True
    strictTriggerLock: bool = True  # v8.0 only: no duplicate FVG/IDM/OB/CHoCH till its own target/SL
    allowMultiTriggerEntries: bool = False
    # 6. SL / targets
    slMode: str = "Fixed Points (Preset)"
    slFixedPreset: str = "80 Points"
    slCustomPoints: float = 80.0
    slSwingLookback: int = 20
    slBufferPoints: float = 5.0
    strat14_Psychology2: bool = True
    maxAllowedSlPoints: float = 160.0
    minTargetRr: float = 1.5
    subMinTargetAction: str = "Extend to Min R:R"
    enableMaxTargetCap: bool = False
    maxTargetRr: float = 4.0
    enableBreakEven: bool = True
    tp1RrMultiple: float = 1.0
    strat15_LiquidityRun: bool = False
    costPerTradePoints: float = 3.0
    maxTradeDuration: int = 150
    # v9.2 only
    enableTimeExit: bool = True     # v9.2: time exit after maxTradeDuration bars only when true (Pine default false)
    enableLossHold: bool = False    # v9.2: pause new entries after 2 consecutive losses (Pine default true)
    holdDurationHours: int = 2      # v9.2: length of that pause (1 - 6 h)
    # fixes
    fix_ob_mitigation: bool = False
    fix_sl_lookback: bool = False
    # injected from bot section
    chart_timeframe: int = 5
    mintick: float = 0.05


class Section:
    """Dict -> attribute access."""

    def __init__(self, data: dict | None):
        self.__dict__.update(data or {})

    def get(self, key: str, default: Any = None) -> Any:
        return self.__dict__.get(key, default)

    def __repr__(self) -> str:
        return f"Section({self.__dict__})"


@dataclass
class Settings:
    bot: Section
    brokers: Section
    hedge: Section
    safety: Section
    alerts: Section
    storage: Section
    strategy: StrategySettings
    raw: dict = field(default_factory=dict)


def _build_strategy(d: dict, bot: dict) -> StrategySettings:
    valid = {f.name: f for f in fields(StrategySettings)}
    kwargs = {}
    for k, v in (d or {}).items():
        if k not in valid:
            raise ValueError(f"Unknown strategy setting: {k}")
        kwargs[k] = v
    s = StrategySettings(**kwargs)
    s.strategy_version = str(s.strategy_version).strip()
    if s.strategy_version not in STRATEGY_VERSIONS:
        raise ValueError(f"strategy_version must be one of {STRATEGY_VERSIONS}, got {s.strategy_version!r}")
    if s.slMode not in SL_MODES:
        raise ValueError(f"slMode must be one of {SL_MODES}, got {s.slMode!r}")
    if s.slMode == "ATR Adaptive (3.0x)" and s.strategy_version != "v9.2":
        raise ValueError("slMode 'ATR Adaptive (3.0x)' exists only in strategy_version v9.2")
    if not 1 <= int(s.holdDurationHours) <= 6:
        raise ValueError("holdDurationHours must be 1 - 6")
    # normalise timeframe strings
    for tf in ("idmTf", "chochStructureTf", "obTimeframe", "fvgTimeframe", "htfTimeframe"):
        v = getattr(s, tf)
        setattr(s, tf, "" if v in (None, "") else str(v))
    s.chart_timeframe = int(bot.get("chart_timeframe", 5))
    s.mintick = float(bot.get("mintick", 0.05))
    return s


def _validate_hedge(h: dict) -> None:
    for lo_key, hi_key in (("sell_min_premium", "sell_max_premium"), ("hedge_diff_min", "hedge_diff_max"),
                           ("atm_sell_min_premium", "atm_sell_max_premium"), ("min_days_to_expiry", "max_days_to_expiry")):
        lo, hi = float(h.get(lo_key, 0) or 0), float(h.get(hi_key, 0) or 0)
        if lo < 0 or hi < 0:
            raise ValueError(f"hedge.{lo_key} / {hi_key} cannot be negative")
        if lo and hi and lo > hi:
            raise ValueError(f"hedge.{lo_key} must be <= {hi_key} (got {lo:g}..{hi:g})")
    for key in ("buy_strike_rule", "sell_strike_rule"):
        rule = h.get(key, "ends_with_50")
        if rule not in ("any", "ends_with_00", "ends_with_50"):
            raise ValueError(f"hedge.{key} must be any / ends_with_00 / ends_with_50, got {rule!r}")
    mode = h.get("strike_mode", "premium_band")
    if mode not in ("atm_offset", "premium_band"):
        raise ValueError(f"hedge.strike_mode must be atm_offset / premium_band, got {mode!r}")
    step = int(h.get("strike_step", 50) or 50)
    so, ho = int(h.get("sell_otm_points", 50) or 0), int(h.get("hedge_otm_points", 200) or 0)
    if so < 0 or so % step or ho % step:
        raise ValueError(f"hedge.sell_otm_points / hedge_otm_points must be multiples of strike_step ({step})")
    if mode == "atm_offset" and ho <= so:
        raise ValueError("hedge.hedge_otm_points must be further out of the money than sell_otm_points")
    if ho > step * int(h.get("strike_scan_width", 10) or 10):
        raise ValueError("hedge.hedge_otm_points is outside the scanned strikes - raise strike_scan_width")
    for key in ("atm_sell_strike_rule", "atm_buy_strike_rule"):
        if h.get(key, "any") not in ("any", "ends_with_00", "ends_with_50"):
            raise ValueError(f"hedge.{key} must be any / ends_with_00 / ends_with_50, got {h.get(key)!r}")
    if mode == "atm_offset" and h.get("atm_sell_strike_rule", "any") != "any" and so < 50:
        raise ValueError("hedge.sell_otm_points must be at least 50 when the sold strike must end with 00 / 50 "
                         "(otherwise there is no trade whenever ATM has the other ending)")
    if float(h.get("max_bid_ask_spread", 0) or 0) < 0:
        raise ValueError("hedge.max_bid_ask_spread cannot be negative")
    if h.get("expiry_mode") == "min_days" and int(h.get("min_days_to_expiry", 7) or 0) < 2:
        raise ValueError("hedge.min_days_to_expiry must be at least 2 (never trade 0 / 1 day to expiry)")


def load_settings(path: str | Path | None = None) -> Settings:
    load_dotenv(ROOT / ".env")
    p = Path(path) if path else DEFAULT_SETTINGS
    with open(p, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    bot = raw.get("bot", {})
    _validate_hedge(raw.get("hedge", {}) or {})
    return Settings(
        bot=Section(bot),
        brokers=Section(raw.get("brokers", {})),
        hedge=Section(raw.get("hedge", {})),
        safety=Section(raw.get("safety", {})),
        alerts=Section(raw.get("alerts", {})),
        storage=Section(raw.get("storage", {})),
        strategy=_build_strategy(raw.get("strategy", {}), bot),
        raw=raw,
    )


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default) or default
