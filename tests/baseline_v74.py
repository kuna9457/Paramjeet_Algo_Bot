"""Builds/compares a golden snapshot of v7.4 engine output (guards v7.4 parity)."""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

from bot.config import StrategySettings
from bot.data.candle import resample
from bot.strategy.engine import NexusEngine
from tests.fakes import synthetic_1m

GOLDEN = Path(__file__).parent / "data" / "v74_golden.json"

M1 = synthetic_1m(days=60)
SERIES = {"5": resample(M1, 5), "15": resample(M1, 15)}

CASES = {
    "defaults": dict(),
    "all_on": dict(strat3_OB=True, strat4_FVG=True, slMode="Swing High/Low", strat15_LiquidityRun=True,
                   enableMaxTargetCap=True, allowMultiTriggerEntries=True, enableAdxFilter=True,
                   enableChopFilter=True, filterEmaSqueeze=True, strat10_Momentum_CISD=True,
                   strat8_HTF_IDM_Trap=True, fix_ob_mitigation=True),
    "ob_fvg_multi": dict(strat3_OB=True, strat4_FVG=True, obTimeframe="15", fvgTimeframe="15",
                         allowMultiTriggerEntries=True, maxOrderBlocks=10, maxFvgCount=10,
                         strat7_PD_Filter=False, strat13_Psychology1=False),
    "v8_values_on_v74": dict(idmTf="5", idmLookback=4, strat8_HTF_IDM_Trap=True, chochStructureTf="15",
                             swingLength=20, strat3_OB=True, obTimeframe="15", maxOrderBlocks=10,
                             strat4_FVG=True, fvgTimeframe="15", fvgAtrMultiplier=0.65, maxFvgCount=10,
                             strat7_PD_Filter=False, strat13_Psychology1=False, enableAdxFilter=True,
                             allowMultiTriggerEntries=True, slFixedPreset="70 Points", strat15_LiquidityRun=True),
    "chart_tf": dict(chochStructureTf="", idmTf="", htfTimeframe=""),
}


def snapshot() -> dict:
    out = {}
    for name, kw in CASES.items():
        for tf, bars in SERIES.items():
            s = replace(StrategySettings(), chart_timeframe=int(tf), **kw)
            e = NexusEngine(s)
            ev = []
            for b in bars:
                ev += e.on_bar(b)
            out[f"{name}@{tf}"] = [x.to_dict() for x in ev]
    return json.loads(json.dumps(out, default=str))


if __name__ == "__main__":
    if sys.argv[1:] == ["write"]:
        GOLDEN.write_text(json.dumps(snapshot()))
        print("written", {k: len(v) for k, v in json.loads(GOLDEN.read_text()).items()})
