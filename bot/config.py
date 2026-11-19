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
STRATEGY_VERSIONS = ("v7.4", "v8.0")


def load_preset(version: str) -> dict:
    """Strategy block of config/presets/<version>.yaml (Pine defaults of that version)."""
    with open(PRESETS_DIR / f"{version}.yaml", "r", encoding="utf-8") as fh:
        return (yaml.safe_load(fh) or {}).get("strategy", {})


@dataclass
class StrategySettings:
    # 0. Logic version: "v7.4" (original, default) | "v8.0" (per-trigger lock upgrade)
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
    # normalise timeframe strings
    for tf in ("idmTf", "chochStructureTf", "obTimeframe", "fvgTimeframe", "htfTimeframe"):
        v = getattr(s, tf)
        setattr(s, tf, "" if v in (None, "") else str(v))
    s.chart_timeframe = int(bot.get("chart_timeframe", 5))
    s.mintick = float(bot.get("mintick", 0.05))
    return s


def _validate_hedge(h: dict) -> None:
    mode = str(h.get("pair_mode", "debit") or "debit").strip().lower()
    if mode not in ("debit", "credit"):
        raise ValueError(f"hedge.pair_mode must be 'debit' or 'credit', got {h.get('pair_mode')!r}")
    lo, hi = ((h.get("credit_min", 10), h.get("credit_max", 15)) if mode == "credit"
              else (h.get("range_min", 20), h.get("range_max", 30)))
    if float(lo) <= 0 or float(hi) < float(lo):
        raise ValueError(f"hedge {mode} range must be 0 < min <= max (got {lo}..{hi})")
    if mode == "credit":
        bmin, bmax = float(h.get("buy_min_premium", 0) or 0), float(h.get("buy_max_premium", 0) or 0)
        if bmin and bmax and bmin > bmax:
            raise ValueError("hedge.buy_min_premium must be <= buy_max_premium")
        if min(bmin, bmax, float(h.get("sell_max_premium", 0) or 0)) < 0:
            raise ValueError("hedge premium limits cannot be negative")


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
