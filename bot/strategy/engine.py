"""Nexus SMC Fractal Engine (Nifty) – line-by-line Python port of the Pine Script v5 indicator.

Section numbers in comments refer to the Pine source (Algo_SMC_Suite_Nifty_v7.4).
The order of operations inside on_bar() is the same as the Pine script top-to-bottom order,
because many variables are read and written in the same bar.
`None` == Pine `na`. All bars fed to on_bar() are CLOSED bars (barstate.isconfirmed == true).
Drawing-only code (boxes, lines, labels, dashboard table) is omitted.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

from ..config import StrategySettings
from ..data.candle import Candle
from ..pine.mtf import HTFContext
from ..pine.ta import (ATR, DMI, EMA, SumWindow, add, ge, gt, highest, le, lowest, lt, nmax,
                       nmin, nz, pivot_high, pivot_low, sub, true_range)


@dataclass
class Zone:  # OrderBlockZone / FairValueGapZone UDT
    top: float
    bottom: float
    isBullish: bool
    mitigated: bool = False
    tested: bool = False
    birthBar: int = 0
    touchBar: int = 0


@dataclass
class Event:
    kind: str                 # ENTRY | EXIT | TP1 | SL_MOVE
    bar_index: int
    time: str
    direction: int            # 1 long / -1 short
    trigger: str = ""
    price: Optional[float] = None
    sl: Optional[float] = None
    target: Optional[float] = None
    tp1: Optional[float] = None
    rr: Optional[float] = None
    reason: str = ""          # EXIT: CHOCH | SL | BE | TARGET | TIME
    pnl_points: Optional[float] = None

    def to_dict(self):
        return asdict(self)


@dataclass
class TradeRecord:
    direction: int
    trigger: str
    entry_bar: int
    entry_time: str
    entry: float
    sl: float
    target: float
    tp1: float
    rr: float
    exit_bar: Optional[int] = None
    exit_time: Optional[str] = None
    exit: Optional[float] = None
    reason: str = ""
    net_r: Optional[float] = None
    pnl_points: Optional[float] = None


def _tf(tf: str, chart_tf: int) -> int:
    return chart_tf if tf in ("", None) else int(tf)


class NexusEngine:
    def __init__(self, s: StrategySettings):
        self.s = s
        ctf = s.chart_timeframe
        self.chart_tf = ctf
        self.bar_index = -1
        self.O: List[float] = []
        self.H: List[float] = []
        self.L: List[float] = []
        self.C: List[float] = []
        self.T: List[str] = []
        self.last_bar_time = None

        # --- 3. global series
        self.atr = ATR(14)
        self.emaF = EMA(20)
        self.emaS = EMA(50)
        self.dmi = DMI(14, 14)
        self.trSum = SumWindow(14)

        # --- MTF contexts (one per timeframe)
        self.htfTf = _tf(s.htfTimeframe, ctf)
        self.chochTf = _tf(s.chochStructureTf, ctf)
        self.idmTfm = _tf(s.idmTf, ctf)
        self.obTfm = _tf(s.obTimeframe, ctf)
        self.useHtfStructure = s.chochStructureTf != "" and self.chochTf != ctf and ctf <= self.chochTf
        self.useHtfIdm = s.idmTf != "" and self.idmTfm != ctf and ctf <= self.idmTfm
        self.useHtfOb = s.obTimeframe != "" and self.obTfm != ctf and ctf <= self.obTfm
        self.fvgTfm = _tf(s.fvgTimeframe, ctf)
        self.useHtfFvg = s.fvgTimeframe != "" and self.fvgTfm != ctf and ctf <= self.fvgTfm
        self.lastHtfFvgTime = None
        need: Dict[int, List[int]] = {}
        need.setdefault(max(self.htfTf, ctf), [])
        if self.useHtfStructure:
            need.setdefault(self.chochTf, []).append(s.htfSwingLength)
        if self.useHtfIdm:
            need.setdefault(self.idmTfm, []).append(s.idmLookback)
        if self.useHtfOb:
            need.setdefault(self.obTfm, [])
        if self.useHtfFvg:
            need.setdefault(self.fvgTfm, [])
        self.ctx: Dict[int, HTFContext] = {tf: HTFContext(tf, lens) for tf, lens in need.items()}
        self.prev: Dict[str, Optional[float]] = {}

        # --- 4. structure state
        self.trendBias = 0
        self.activeTargetHigh = None
        self.activeTargetHighBar = None
        self.activeTargetLow = None
        self.activeTargetLowBar = None
        self.protectedStrongLow = None
        self.protectedStrongLowBar = None
        self.protectedStrongHigh = None
        self.protectedStrongHighBar = None
        self.pivotHighLevels: List[float] = []
        self.pivotHighBars: List[int] = []
        self.pivotLowLevels: List[float] = []
        self.pivotLowBars: List[int] = []
        self.lastBullChochBar = -100
        self.lastBearChochBar = -100
        self.lastBrokenChochHigh = None
        self.lastBrokenChochLow = None
        self.bullChochRetested = True
        self.bearChochRetested = True
        self.chochZoneTouchedBull = False
        self.chochZoneTouchedBear = False
        self.lastBullChochBodyBreak = False
        self.lastBearChochBodyBreak = False

        # --- 5. IDM
        self.activeIdmHigh = None
        self.activeIdmHighBar = None
        self.activeIdmLow = None
        self.activeIdmLowBar = None
        self.lastBullSweepBar = -100
        self.lastBearSweepBar = -100
        self.sweepExtremeLow = None
        self.sweepExtremeHigh = None

        # --- 6. zones
        self.bullObList: List[Zone] = []
        self.bearObList: List[Zone] = []
        self.bullFvgList: List[Zone] = []
        self.bearFvgList: List[Zone] = []

        # --- 8. gates
        self.lastLossBarIndex = -100
        self.lastLossDir = 0
        self.lastSignalBarIndex = -100
        self.activeTradeDir = 0
        self.activeTradeTrigger = ""
        self.lastBuyTriggerSource = ""
        self.lastSellTriggerSource = ""

        # --- 9. trade
        self.entryPrice = None
        self.slPrice = None
        self.initialRiskPoints = None
        self.targetPrice = None
        self.currentTradeRr = None
        self.tp1Price = None
        self.tp1Hit = False
        self.tp1HitBarIndex = -100

        # --- stats (dashboard)
        self.winTradesCount = 0
        self.lossTradesCount = 0
        self.beTradesCount = 0
        self.timeTradesCount = 0
        self.accumulatedNetR = 0.0
        self.trades: List[TradeRecord] = []
        self._open_trade: Optional[TradeRecord] = None

        # last bar diagnostic values (for dashboard / parity)
        self.diag: Dict[str, object] = {}

    # ------------------------------------------------------------------ helpers
    def _h(self, k: int):
        i = len(self.H) - 1 - k
        return self.H[i] if i >= 0 else None

    def _l(self, k: int):
        i = len(self.L) - 1 - k
        return self.L[i] if i >= 0 else None

    def _o(self, k: int):
        i = len(self.O) - 1 - k
        return self.O[i] if i >= 0 else None

    def _c(self, k: int):
        i = len(self.C) - 1 - k
        return self.C[i] if i >= 0 else None

    def _changed(self, key: str, val):
        """Pine: not na(val) and val != val[1]  (comparison with na == false)."""
        prev = self.prev.get(key)
        return val is not None and prev is not None and val != prev

    # ------------------------------------------------------------------ main
    def on_bar(self, bar: Candle) -> List[Event]:
        s = self.s
        events: List[Event] = []
        self.bar_index += 1
        bi = self.bar_index
        prev_close = self.C[-1] if self.C else None
        self.O.append(bar.open)
        self.H.append(bar.high)
        self.L.append(bar.low)
        self.C.append(bar.close)
        tstr = bar.time.isoformat()
        self.T.append(tstr)
        self.last_bar_time = bar.time
        open_, high, low, close = bar.open, bar.high, bar.low, bar.close

        for ctx in self.ctx.values():
            ctx.on_chart_bar(bar, self.chart_tf)

        # ============================================================ 3. GLOBAL SERIES
        atr14 = self.atr.update(high, low, prev_close)
        emaFast = self.emaF.update(close)
        emaSlow = self.emaS.update(close)
        _, _, adxVal = self.dmi.update(high, low, prev_close)
        atrSum14 = self.trSum.update(true_range(high, low, prev_close, False))
        hh14, ll14 = highest(self.H, 14), lowest(self.L, 14)
        chopRange14 = sub(hh14, ll14)
        if gt(chopRange14, 0) and gt(atrSum14, 0):
            chopIndex = 100.0 * (math.log10(max(atrSum14 / chopRange14, 0.0001)) / math.log10(14))
        else:
            chopIndex = 50.0

        earlyHigh20 = highest(self.H, 20)
        earlyLow20 = lowest(self.L, 20)
        globalLowestLow5 = lowest(self.L, 5)
        globalHighestHigh5 = highest(self.H, 5)
        globalLowestLow25 = lowest(self.L, 25)
        globalHighestHigh25 = highest(self.H, 25)

        rawPivotHigh = pivot_high(self.H, s.swingLength, s.swingLength)
        rawPivotLow = pivot_low(self.L, s.swingLength, s.swingLength)
        rawIntHigh = pivot_high(self.H, s.idmLookback, s.idmLookback)
        rawIntLow = pivot_low(self.L, s.idmLookback, s.idmLookback)

        hsnap = self.ctx[max(self.htfTf, self.chart_tf)].snap
        htfClose, htfEma20 = hsnap["close_1"], hsnap["ema20_1"]
        if htfClose is not None and htfEma20 is not None:
            htfBias = 1 if htfClose > htfEma20 else (-1 if htfClose < htfEma20 else 0)
        else:
            htfBias = 0

        # Structure pivots (HTF or chart)
        if self.useHtfStructure:
            sn = self.ctx[self.chochTf].snap
            htfLastPh = sn["ph"].get(s.htfSwingLength)
            htfLastPl = sn["pl"].get(s.htfSwingLength)
        else:
            htfLastPh = htfLastPl = None
        newHtfHigh = self.useHtfStructure and self._changed("htfLastPh", htfLastPh)
        newHtfLow = self.useHtfStructure and self._changed("htfLastPl", htfLastPl)
        isNewPivotHigh = newHtfHigh if self.useHtfStructure else rawPivotHigh is not None
        regPivotHighVal = htfLastPh if self.useHtfStructure else rawPivotHigh
        isNewPivotLow = newHtfLow if self.useHtfStructure else rawPivotLow is not None
        regPivotLowVal = htfLastPl if self.useHtfStructure else rawPivotLow

        # IDM pivots (HTF or chart)
        if self.useHtfIdm:
            sn = self.ctx[self.idmTfm].snap
            htfLastIh = sn["ph"].get(s.idmLookback)
            htfLastIl = sn["pl"].get(s.idmLookback)
        else:
            htfLastIh = htfLastIl = None
        isNewIdmHigh = self._changed("htfLastIh", htfLastIh) if self.useHtfIdm else rawIntHigh is not None
        regIdmHighVal = htfLastIh if self.useHtfIdm else rawIntHigh
        isNewIdmLow = self._changed("htfLastIl", htfLastIl) if self.useHtfIdm else rawIntLow is not None
        regIdmLowVal = htfLastIl if self.useHtfIdm else rawIntLow

        # ============================================================ 4. MARKET STRUCTURE
        if isNewPivotHigh:
            pBar = bi - (max(s.htfSwingLength * 2, 5) if self.useHtfStructure else s.swingLength)
            self.pivotHighLevels.insert(0, regPivotHighVal)
            self.pivotHighBars.insert(0, pBar)
            if len(self.pivotHighLevels) > 20:
                self.pivotHighLevels.pop()
                self.pivotHighBars.pop()
            if self.trendBias == 1:
                self.activeTargetHigh = regPivotHighVal
                self.activeTargetHighBar = pBar
            elif self.trendBias == -1:
                if self.protectedStrongHigh is None or regPivotHighVal < self.protectedStrongHigh:
                    self.protectedStrongHigh = regPivotHighVal
                    self.protectedStrongHighBar = pBar
            else:
                self.activeTargetHigh = regPivotHighVal
                self.activeTargetHighBar = pBar
                self.protectedStrongHigh = regPivotHighVal
                self.protectedStrongHighBar = pBar

        if isNewPivotLow:
            pBar = bi - (max(s.htfSwingLength * 2, 5) if self.useHtfStructure else s.swingLength)
            self.pivotLowLevels.insert(0, regPivotLowVal)
            self.pivotLowBars.insert(0, pBar)
            if len(self.pivotLowLevels) > 20:
                self.pivotLowLevels.pop()
                self.pivotLowBars.pop()
            if self.trendBias == -1:
                self.activeTargetLow = regPivotLowVal
                self.activeTargetLowBar = pBar
            elif self.trendBias == 1:
                if self.protectedStrongLow is None or regPivotLowVal > self.protectedStrongLow:
                    self.protectedStrongLow = regPivotLowVal
                    self.protectedStrongLowBar = pBar
            else:
                self.activeTargetLow = regPivotLowVal
                self.activeTargetLowBar = pBar
                self.protectedStrongLow = regPivotLowVal
                self.protectedStrongLowBar = pBar

        if self.trendBias == 0:
            if htfBias != 0:
                self.trendBias = htfBias
            elif bi >= 20:
                self.activeTargetHigh = earlyHigh20
                self.activeTargetHighBar = bi - 10
                self.protectedStrongHigh = earlyHigh20
                self.protectedStrongHighBar = bi - 10
                self.activeTargetLow = earlyLow20
                self.activeTargetLowBar = bi - 10
                self.protectedStrongLow = earlyLow20
                self.protectedStrongLowBar = bi - 10

        isBullishChoch = isBearishChoch = isBullishBos = isBearishBos = False

        if self.trendBias == 1:
            if self.activeTargetHigh is not None and close > self.activeTargetHigh:
                isBullishBos = True
                self.lastBullChochBodyBreak = (close > open_) and (close > self.activeTargetHigh)
                lenSinceHigh = max(bi - self.activeTargetHighBar, 1)
                lowestPullback, lowestBar = low, bi
                for k in range(0, min(lenSinceHigh, 500) + 1):
                    lk = self._l(k)
                    if lt(lk, lowestPullback):
                        lowestPullback, lowestBar = lk, bi - k
                self.protectedStrongLow = lowestPullback
                self.protectedStrongLowBar = lowestBar
                self.activeTargetHigh = None
            elif (self.protectedStrongLow is not None and close < self.protectedStrongLow
                  and (bi - self.lastBullChochBar) >= 10):
                isBearishChoch = True
                self.trendBias = -1
                self.lastBearChochBar = bi
                self.lastBrokenChochLow = self.protectedStrongLow
                self.bearChochRetested = False
                self.bullChochRetested = True
                self.chochZoneTouchedBear = False
                self.lastBearChochBodyBreak = (close < open_) and (close < self.protectedStrongLow)
                majorPeakVal, majorPeakBar = high, bi
                if self.pivotHighLevels:
                    majorPeakVal, majorPeakBar = self.pivotHighLevels[0], self.pivotHighBars[0]
                psl_bar = self.protectedStrongLowBar if self.protectedStrongLowBar is not None else bi - 10
                scanLen = min(max(bi - min(psl_bar, majorPeakBar), 5), 500)
                for k in range(0, scanLen + 1):
                    hk = self._h(k)
                    if gt(hk, majorPeakVal):
                        majorPeakVal, majorPeakBar = hk, bi - k
                self.protectedStrongHigh = majorPeakVal
                self.protectedStrongHighBar = majorPeakBar
                self.protectedStrongLow = None
                self.activeTargetHigh = None
                self.activeTargetLow = low
                self.activeTargetLowBar = bi
        elif self.trendBias == -1:
            if self.activeTargetLow is not None and close < self.activeTargetLow:
                isBearishBos = True
                self.lastBearChochBodyBreak = (close < open_) and (close < self.activeTargetLow)
                lenSinceLow = max(bi - self.activeTargetLowBar, 1)
                highestBounce, highestBar = high, bi
                for k in range(0, min(lenSinceLow, 500) + 1):
                    hk = self._h(k)
                    if gt(hk, highestBounce):
                        highestBounce, highestBar = hk, bi - k
                self.protectedStrongHigh = highestBounce
                self.protectedStrongHighBar = highestBar
                self.activeTargetLow = None
            elif (self.protectedStrongHigh is not None and close > self.protectedStrongHigh
                  and (bi - self.lastBearChochBar) >= 10):
                isBullishChoch = True
                self.trendBias = 1
                self.lastBullChochBar = bi
                self.lastBrokenChochHigh = self.protectedStrongHigh
                self.bullChochRetested = False
                self.bearChochRetested = True
                self.chochZoneTouchedBull = False
                self.lastBullChochBodyBreak = (close > open_) and (close > self.protectedStrongHigh)
                majorValleyVal, majorValleyBar = low, bi
                if self.pivotLowLevels:
                    majorValleyVal, majorValleyBar = self.pivotLowLevels[0], self.pivotLowBars[0]
                psh_bar = self.protectedStrongHighBar if self.protectedStrongHighBar is not None else bi - 10
                scanLen = min(max(bi - min(psh_bar, majorValleyBar), 5), 500)
                for k in range(0, scanLen + 1):
                    lk = self._l(k)
                    if lt(lk, majorValleyVal):
                        majorValleyVal, majorValleyBar = lk, bi - k
                self.protectedStrongLow = majorValleyVal
                self.protectedStrongLowBar = majorValleyBar
                self.protectedStrongHigh = None
                self.activeTargetLow = None
                self.activeTargetHigh = high
                self.activeTargetHighBar = bi

        protectedHigh = self.protectedStrongHigh
        protectedLow = self.protectedStrongLow
        isBullishRegime = self.trendBias == 1
        isBearishRegime = self.trendBias == -1

        # ============================================================ 5. IDM
        if isNewIdmHigh:
            iBar = bi - (5 if self.useHtfIdm else s.idmLookback)
            if isBearishRegime:
                self.activeIdmHigh = regIdmHighVal
                self.activeIdmHighBar = iBar
        if isNewIdmLow:
            iBar = bi - (5 if self.useHtfIdm else s.idmLookback)
            if isBullishRegime:
                self.activeIdmLow = regIdmLowVal
                self.activeIdmLowBar = iBar

        bullSweepTriggered = bearSweepTriggered = False
        if self.activeIdmLow is not None and low < self.activeIdmLow:
            if close > self.activeIdmLow:
                bullSweepTriggered = True
                self.lastBullSweepBar = bi
                self.sweepExtremeLow = low
                self.activeIdmLow = None
            elif atr14 is not None and close < self.activeIdmLow - atr14 * 0.5:
                self.activeIdmLow = None
        if self.activeIdmHigh is not None and high > self.activeIdmHigh:
            if close < self.activeIdmHigh:
                bearSweepTriggered = True
                self.lastBearSweepBar = bi
                self.sweepExtremeHigh = high
                self.activeIdmHigh = None
            elif atr14 is not None and close > self.activeIdmHigh + atr14 * 0.5:
                self.activeIdmHigh = None

        if isBullishChoch:
            self.lastBearSweepBar = -100
        if isBearishChoch:
            self.lastBullSweepBar = -100
        recentBullIdmSweep = (bi - self.lastBullSweepBar) <= 15
        recentBearIdmSweep = (bi - self.lastBearSweepBar) <= 15

        # ============================================================ 6. ORDER BLOCKS & FVG
        if self.useHtfOb:
            o1, h1, l1, c1, t1 = self.ctx[self.obTfm].snap["ohlct_1"]
        else:
            o1 = h1 = l1 = c1 = t1 = None
        htfObOpen, htfObHigh, htfObLow, htfObClose, htfObTime = o1, h1, l1, c1, t1
        pO, pH, pL, pC = (self.prev.get("htfObOpen"), self.prev.get("htfObHigh"),
                          self.prev.get("htfObLow"), self.prev.get("htfObClose"))

        isBullishDisplacement = (bi >= 4 and gt(self._l(1), self._h(3))
                                 and ge(sub(self._l(1), self._h(3)), (atr14 * 0.25) if atr14 is not None else None)
                                 and gt(self._c(2), self._o(2)))
        isBearishDisplacement = (bi >= 4 and lt(self._h(1), self._l(3))
                                 and ge(sub(self._l(3), self._h(1)), (atr14 * 0.25) if atr14 is not None else None)
                                 and lt(self._c(2), self._o(2)))

        isNewHtfObBar = self.useHtfOb and self._changed("htfObTime", htfObTime)
        if isNewHtfObBar:
            if gt(htfObClose, htfObOpen) and gt(pO, pC):
                obTop, obBtm = pH, pL
                if atr14 is not None and le(sub(obTop, obBtm), atr14 * 4.0):
                    self._push_zone(self.bullObList, Zone(obTop, obBtm, True, birthBar=bi), s.maxOrderBlocks)
            if lt(htfObClose, htfObOpen) and gt(pC, pO):
                obTop, obBtm = pH, pL
                if atr14 is not None and le(sub(obTop, obBtm), atr14 * 4.0):
                    self._push_zone(self.bearObList, Zone(obTop, obBtm, False, birthBar=bi), s.maxOrderBlocks)

        if not self.useHtfOb:
            # Bullish OB
            if isBullishChoch or isBullishBos:
                anchorBar = self.protectedStrongLowBar if self.protectedStrongLowBar is not None else bi - 15
                distFromAnchor = max(bi - anchorBar, 5)
                lookbackRange = min(max(distFromAnchor + 5, 10), 50)
                lowestRedLow, lowestRedIdx = 1e10, None
                for i in range(1, lookbackRange + 1):
                    ci, oi, li = self._c(i), self._o(i), self._l(i)
                    if ci is not None and lt(ci, oi) and lt(li, lowestRedLow):
                        lowestRedLow, lowestRedIdx = li, i
                if lowestRedIdx is not None:
                    obTop, obBtm = self._h(lowestRedIdx), self._l(lowestRedIdx)
                    if atr14 is not None and (obTop - obBtm) <= atr14 * 3.5:
                        self._push_zone(self.bullObList, Zone(obTop, obBtm, True, birthBar=bi), s.maxOrderBlocks)
            else:
                canAdd = False
                if isBullishDisplacement and bi > 5:
                    if not self.bullObList or (bi - self.bullObList[-1].birthBar) > 5:
                        canAdd = True
                if canAdd:
                    obIdx = 3
                    for k in range(2, 6):
                        if lt(self._c(k), self._o(k)):
                            obIdx = k
                            break
                    obTop, obBtm = self._h(obIdx), self._l(obIdx)
                    if atr14 is not None and (obTop - obBtm) <= atr14 * 3.5:
                        self._push_zone(self.bullObList, Zone(obTop, obBtm, True, birthBar=bi), s.maxOrderBlocks)
            # Bearish OB
            if isBearishChoch or isBearishBos:
                anchorBar = self.protectedStrongHighBar if self.protectedStrongHighBar is not None else bi - 15
                distFromAnchor = max(bi - anchorBar, 5)
                lookbackRange = min(max(distFromAnchor + 5, 10), 50)
                highestGreenHigh, highestGreenIdx = -1e10, None
                for i in range(1, lookbackRange + 1):
                    ci, oi, hi = self._c(i), self._o(i), self._h(i)
                    if ci is not None and gt(ci, oi) and gt(hi, highestGreenHigh):
                        highestGreenHigh, highestGreenIdx = hi, i
                if highestGreenIdx is not None:
                    obTop, obBtm = self._h(highestGreenIdx), self._l(highestGreenIdx)
                    if atr14 is not None and (obTop - obBtm) <= atr14 * 3.5:
                        self._push_zone(self.bearObList, Zone(obTop, obBtm, False, birthBar=bi), s.maxOrderBlocks)
            else:
                canAdd = False
                if isBearishDisplacement and bi > 5:
                    if not self.bearObList or (bi - self.bearObList[-1].birthBar) > 5:
                        canAdd = True
                if canAdd:
                    obIdx = 3
                    for k in range(2, 6):
                        if gt(self._c(k), self._o(k)):
                            obIdx = k
                            break
                    obTop, obBtm = self._h(obIdx), self._l(obIdx)
                    if atr14 is not None and (obTop - obBtm) <= atr14 * 3.5:
                        self._push_zone(self.bearObList, Zone(obTop, obBtm, False, birthBar=bi), s.maxOrderBlocks)

        # FVG creation
        if self.useHtfFvg:
            # 3-candle gap on completed higher-timeframe bars; evaluated once when a new HTF bar closes
            l3 = self.ctx[self.fvgTfm].snap.get("last3")
            if l3 is not None and l3[1] != self.lastHtfFvgTime:
                self.lastHtfFvgTime = l3[1]
                (b3, b2, b1), hAtr = l3[0], l3[2]
                if hAtr is not None:
                    h3_, c2_, l1_ = b3[1], b2[3], b1[2]
                    h1_, l3_ = b1[1], b3[2]
                    if l1_ > h3_ and c2_ > h3_ and (l1_ - h3_) >= hAtr * 0.25:
                        self._push_zone(self.bullFvgList, Zone(l1_, h3_, True, birthBar=bi), s.maxFvgCount)
                    if h1_ < l3_ and c2_ < l3_ and (l3_ - h1_) >= hAtr * 0.25:
                        self._push_zone(self.bearFvgList, Zone(l3_, h1_, False, birthBar=bi), s.maxFvgCount)
        elif bi >= 4 and atr14 is not None:
            l1_, h3_, c2_ = self._l(1), self._h(3), self._c(2)
            h1_, l3_ = self._h(1), self._l(3)
            if l1_ > h3_ and c2_ > h3_ and (l1_ - h3_) >= atr14 * 0.25:
                self._push_zone(self.bullFvgList, Zone(l1_, h3_, True, birthBar=bi), s.maxFvgCount)
            if h1_ < l3_ and c2_ < l3_ and (l3_ - h1_) >= atr14 * 0.25:
                self._push_zone(self.bearFvgList, Zone(l3_, h1_, False, birthBar=bi), s.maxFvgCount)

        # OB first-touch / mitigation
        touchBullObFirst = touchBearObFirst = False
        matchingBullObIdx = matchingBearObIdx = -1
        for i in range(len(self.bullObList) - 1, -1, -1):
            ob = self.bullObList[i]
            if ob.mitigated:
                continue
            obMean = (ob.top + ob.bottom) * 0.5
            isViolated = close < ob.bottom
            if isViolated:
                ob.mitigated = True
                ob.tested = True
            elif bi > ob.birthBar:
                isTouchingNow = low <= ob.top and high >= ob.bottom
                if isTouchingNow and ob.touchBar == 0:
                    ob.touchBar = bi
                inRetestWindow = ob.touchBar > 0 and (bi - ob.touchBar) <= 4 and close >= ob.bottom
                if not ob.tested and (isTouchingNow or inRetestWindow):
                    touchBullObFirst = True
                    matchingBullObIdx = i
                if s.fix_ob_mitigation:  # Pine bug: `=` instead of `:=` makes this dead code
                    if s.obMitigation == "Wick Touch":
                        byStyle = ob.touchBar > 0 and (bi - ob.touchBar) >= 1 and low <= ob.top
                    elif s.obMitigation == "50% Mean Threshold":
                        byStyle = low <= obMean
                    else:
                        byStyle = close < ob.top
                    if byStyle and bi > ob.birthBar + 1 and ob.touchBar > 0 and (bi - ob.touchBar) >= 3:
                        ob.mitigated = True
        for i in range(len(self.bearObList) - 1, -1, -1):
            ob = self.bearObList[i]
            if ob.mitigated:
                continue
            obMean = (ob.top + ob.bottom) * 0.5
            isViolated = close > ob.top
            if isViolated:
                ob.mitigated = True
                ob.tested = True
            elif bi > ob.birthBar:
                isTouchingNow = high >= ob.bottom and low <= ob.top
                if isTouchingNow and ob.touchBar == 0:
                    ob.touchBar = bi
                inRetestWindow = ob.touchBar > 0 and (bi - ob.touchBar) <= 4 and close <= ob.top
                if not ob.tested and (isTouchingNow or inRetestWindow):
                    touchBearObFirst = True
                    matchingBearObIdx = i
                if s.fix_ob_mitigation:
                    if s.obMitigation == "Wick Touch":
                        byStyle = ob.touchBar > 0 and (bi - ob.touchBar) >= 1 and high >= ob.bottom
                    elif s.obMitigation == "50% Mean Threshold":
                        byStyle = high >= obMean
                    else:
                        byStyle = close > ob.bottom
                    if byStyle and bi > ob.birthBar + 1 and ob.touchBar > 0 and (bi - ob.touchBar) >= 3:
                        ob.mitigated = True

        # FVG first-touch / mitigation
        touchBullFvgFirst = touchBearFvgFirst = False
        matchingBullFvgIdx = matchingBearFvgIdx = -1
        for i in range(len(self.bullFvgList) - 1, -1, -1):
            f = self.bullFvgList[i]
            if f.mitigated:
                continue
            if close < f.bottom:
                f.mitigated = True
                f.tested = True
            elif bi > f.birthBar:
                isTouchingNow = low <= f.top and high >= f.bottom
                if isTouchingNow and f.touchBar == 0:
                    f.touchBar = bi
                inRetestWindow = f.touchBar > 0 and (bi - f.touchBar) <= 4 and close >= f.bottom
                if not f.tested and (isTouchingNow or inRetestWindow):
                    touchBullFvgFirst = True
                    matchingBullFvgIdx = i
                if low <= f.bottom or (f.touchBar > 0 and (bi - f.touchBar) >= 3):
                    f.mitigated = True
        for i in range(len(self.bearFvgList) - 1, -1, -1):
            f = self.bearFvgList[i]
            if f.mitigated:
                continue
            if close > f.top:
                f.mitigated = True
                f.tested = True
            elif bi > f.birthBar:
                isTouchingNow = high >= f.bottom and low <= f.top
                if isTouchingNow and f.touchBar == 0:
                    f.touchBar = bi
                inRetestWindow = f.touchBar > 0 and (bi - f.touchBar) <= 4 and close <= f.top
                if not f.tested and (isTouchingNow or inRetestWindow):
                    touchBearFvgFirst = True
                    matchingBearFvgIdx = i
                if high >= f.top or (f.touchBar > 0 and (bi - f.touchBar) >= 3):
                    f.mitigated = True

        # ============================================================ 7. DEALING RANGE & FILTERS
        if self.trendBias == 1 and self.protectedStrongLow is not None:
            activeRangeLow = self.protectedStrongLow
        else:
            activeRangeLow = self.pivotLowLevels[0] if self.pivotLowLevels else globalLowestLow25
        if self.trendBias == -1 and self.protectedStrongHigh is not None:
            activeRangeHigh = self.protectedStrongHigh
        else:
            activeRangeHigh = self.pivotHighLevels[0] if self.pivotHighLevels else globalHighestHigh25
        rangeHigh = max(nz(activeRangeHigh, high), high)
        rangeLow = min(nz(activeRangeLow, low), low)
        if atr14 is not None and (rangeHigh - rangeLow) < atr14 * 2.0:
            rangeHigh, rangeLow = globalHighestHigh25, globalLowestLow25
        equilibriumPrice = None if (rangeHigh is None or rangeLow is None) else (rangeHigh + rangeLow) / 2.0

        hasDippedDiscount = le(low, equilibriumPrice) or le(open_, equilibriumPrice) or le(globalLowestLow5, equilibriumPrice)
        hasDippedPremium = ge(high, equilibriumPrice) or ge(open_, equilibriumPrice) or ge(globalHighestHigh5, equilibriumPrice)
        inDiscountZone = hasDippedDiscount and le(close, equilibriumPrice)
        inPremiumZone = hasDippedPremium and ge(close, equilibriumPrice)

        passFilter_PD_Buy = (not s.strat7_PD_Filter) or inDiscountZone
        passFilter_PD_Sell = (not s.strat7_PD_Filter) or inPremiumZone
        passFilter_MTF_Buy = (not s.strat5_MTF) or htfBias >= 0
        passFilter_MTF_Sell = (not s.strat5_MTF) or htfBias <= 0
        passFilter_Trap_Buy = (not s.strat8_HTF_IDM_Trap) or htfBias >= 0 or recentBullIdmSweep
        passFilter_Trap_Sell = (not s.strat8_HTF_IDM_Trap) or htfBias <= 0 or recentBearIdmSweep
        passFilter_Strat9_Buy = (not s.strat9_Fake_CHOCH) or self.lastBullChochBodyBreak
        passFilter_Strat9_Sell = (not s.strat9_Fake_CHOCH) or self.lastBearChochBodyBreak

        candleBodySize = abs(close - open_)
        candleTotalRange = max(high - low, s.mintick)
        bodyPercentage = candleBodySize / candleTotalRange * 100.0
        hasDisplacement = (not s.strat10_Momentum_CISD) or bodyPercentage >= s.minDisplacementPct

        passFilter_OB_Confirm_Buy = (not s.strat12_OB_Confirm) or (close - low) > (high - close)
        passFilter_OB_Confirm_Sell = (not s.strat12_OB_Confirm) or (high - close) > (close - low)

        bufferDist = None if atr14 is None else atr14 * s.opposingZoneBufferAtr
        hasOverheadSupply = False
        for ob in self.bearObList:
            if not ob.mitigated and ob.bottom > close and lt(ob.bottom - close, bufferDist):
                hasOverheadSupply = True
                break
        hasUnderfootDemand = False
        for ob in self.bullObList:
            if not ob.mitigated and ob.top < close and lt(close - ob.top, bufferDist):
                hasUnderfootDemand = True
                break
        passFilter_Supply_Buy = (not s.strat13_Psychology1) or not hasOverheadSupply
        passFilter_Demand_Sell = (not s.strat13_Psychology1) or not hasUnderfootDemand

        bullConf = int(touchBullObFirst) + int(touchBullFvgFirst) + int(inDiscountZone) + int(recentBullIdmSweep)
        bearConf = int(touchBearObFirst) + int(touchBearFvgFirst) + int(inPremiumZone) + int(recentBearIdmSweep)
        passFilter_Strat16_Buy = (not s.strat16_POI_Confluence) or bullConf >= 2
        passFilter_Strat16_Sell = (not s.strat16_POI_Confluence) or bearConf >= 2

        isEmaSqueezed = s.filterEmaSqueeze and lt(
            None if (emaFast is None or emaSlow is None) else abs(emaFast - emaSlow),
            None if atr14 is None else atr14 * 0.15)
        isMarketChoppy = ((s.enableChopFilter and chopIndex >= s.chopIndexMax)
                          or (s.enableAdxFilter and lt(adxVal, s.adxThreshold)) or isEmaSqueezed)
        passFilter_Chop = not isMarketChoppy

        # candle confirmation
        isHigherHighBuy = close > open_ and gt(high, self._h(1)) and gt(close, self._h(1))
        isLowerLowSell = close < open_ and lt(low, self._l(1)) and lt(close, self._l(1))
        candleLowerWick = min(open_, close) - low
        candleUpperWick = high - max(open_, close)
        isBullRejection = candleLowerWick >= candleTotalRange * 0.28 or close >= low + candleTotalRange * 0.60
        isBearRejection = candleUpperWick >= candleTotalRange * 0.28 or close <= high - candleTotalRange * 0.60
        isDumpSequence = bi >= 3 and lt(self._c(1), self._o(1)) and lt(self._c(2), self._o(2)) and lt(self._c(3), self._o(3))
        isRocketSequence = bi >= 3 and gt(self._c(1), self._o(1)) and gt(self._c(2), self._o(2)) and gt(self._c(3), self._o(3))
        confirmedBullCandle = isHigherHighBuy and isBullRejection and ((not isDumpSequence) or gt(close, self._h(2)))
        confirmedBearCandle = isLowerLowSell and isBearRejection and ((not isRocketSequence) or lt(close, self._l(2)))

        passFilter_3Cond_Buy = (not s.strat11_3Condition) or (
            passFilter_MTF_Buy and inDiscountZone and (touchBullObFirst or touchBullFvgFirst) and confirmedBullCandle)
        passFilter_3Cond_Sell = (not s.strat11_3Condition) or (
            passFilter_MTF_Sell and inPremiumZone and (touchBearObFirst or touchBearFvgFirst) and confirmedBearCandle)

        allFiltersPass_Buy = (passFilter_PD_Buy and passFilter_MTF_Buy and passFilter_Trap_Buy and passFilter_Strat9_Buy
                              and hasDisplacement and passFilter_OB_Confirm_Buy and passFilter_Supply_Buy
                              and passFilter_Chop and passFilter_3Cond_Buy and passFilter_Strat16_Buy)
        allFiltersPass_Sell = (passFilter_PD_Sell and passFilter_MTF_Sell and passFilter_Trap_Sell and passFilter_Strat9_Sell
                               and hasDisplacement and passFilter_OB_Confirm_Sell and passFilter_Demand_Sell
                               and passFilter_Chop and passFilter_3Cond_Sell and passFilter_Strat16_Sell)

        # ============================================================ 8. TRIGGERS
        t1_Bull = s.strat3_OB and touchBullObFirst and confirmedBullCandle
        t1_Bear = s.strat3_OB and touchBearObFirst and confirmedBearCandle
        t2_Bull = s.strat4_FVG and touchBullFvgFirst and confirmedBullCandle
        t2_Bear = s.strat4_FVG and touchBearFvgFirst and confirmedBearCandle

        tol = None if atr14 is None else atr14 * 1.0
        chochLevelRetestBuy = le(low, add(nz(self.lastBrokenChochHigh, high), tol)) and low >= nz(protectedLow, low - 1000.0)
        chochPoiRetestBuy = touchBullObFirst or touchBullFvgFirst
        if (not self.bullChochRetested and bi >= self.lastBullChochBar + 2
                and (chochLevelRetestBuy or chochPoiRetestBuy)):
            self.chochZoneTouchedBull = True
        chochRetestBuy = (not self.bullChochRetested and self.chochZoneTouchedBull and bi >= self.lastBullChochBar + 2
                          and (bi - self.lastBullChochBar) <= 35 and confirmedBullCandle)
        t3_Bull = s.strat2_CHOCH and chochRetestBuy

        chochLevelRetestSell = ge(high, sub(nz(self.lastBrokenChochLow, low), tol)) and high <= nz(protectedHigh, high + 1000.0)
        chochPoiRetestSell = touchBearObFirst or touchBearFvgFirst
        if (not self.bearChochRetested and bi >= self.lastBearChochBar + 2
                and (chochLevelRetestSell or chochPoiRetestSell)):
            self.chochZoneTouchedBear = True
        chochRetestSell = (not self.bearChochRetested and self.chochZoneTouchedBear and bi >= self.lastBearChochBar + 2
                           and (bi - self.lastBearChochBar) <= 35 and confirmedBearCandle)
        t3_Bear = s.strat2_CHOCH and chochRetestSell

        hasValidBullPoi = touchBullObFirst or touchBullFvgFirst
        hasValidBearPoi = touchBearObFirst or touchBearFvgFirst
        idmValidBullPoi = (not s.strat6_IDM_Precision) or hasValidBullPoi
        idmValidBearPoi = (not s.strat6_IDM_Precision) or hasValidBearPoi
        t4_Bull = s.strat1_IDM and (bullSweepTriggered or recentBullIdmSweep) and idmValidBullPoi and confirmedBullCandle
        t4_Bear = s.strat1_IDM and (bearSweepTriggered or recentBearIdmSweep) and idmValidBearPoi and confirmedBearCandle

        rawTrigger_Buy = t4_Bull or t3_Bull or t1_Bull or t2_Bull
        rawTrigger_Sell = t4_Bear or t3_Bear or t1_Bear or t2_Bear
        if not (s.strat1_IDM or s.strat2_CHOCH or s.strat3_OB or s.strat4_FVG):
            rawTrigger_Buy = rawTrigger_Sell = False

        buyLossFreeze = self.lastLossDir == 1 and (bi - self.lastLossBarIndex) < 15 and not isBullishChoch
        sellLossFreeze = self.lastLossDir == -1 and (bi - self.lastLossBarIndex) < 15 and not isBearishChoch

        def trig_name(t3, t4, t1, t2):
            if t3 and t4:
                return "CHoCH+IDM"
            if t3:
                return "CHoCH"
            if t4:
                return "IDM"
            if t1:
                return "OB"
            if t2:
                return "FVG"
            return ""

        candBuyTrig = trig_name(t3_Bull, t4_Bull, t1_Bull, t2_Bull)
        candSellTrig = trig_name(t3_Bear, t4_Bear, t1_Bear, t2_Bear)

        cooldownElapsed = (bi - self.lastSignalBarIndex) >= s.signalCooldownBars
        canPyramidLong = (s.allowMultiTriggerEntries and self.activeTradeDir == 1 and candBuyTrig != self.activeTradeTrigger
                          and candBuyTrig != "" and (bi - self.lastSignalBarIndex) >= 15)
        canPyramidShort = (s.allowMultiTriggerEntries and self.activeTradeDir == -1 and candSellTrig != self.activeTradeTrigger
                           and candSellTrig != "" and (bi - self.lastSignalBarIndex) >= 15)
        slotAvailable_Buy = (not s.filterDuplicateTrades) or self.activeTradeDir == 0 or canPyramidLong
        slotAvailable_Sell = (not s.filterDuplicateTrades) or self.activeTradeDir == 0 or canPyramidShort

        candidateBuy = (isBullishRegime and rawTrigger_Buy and allFiltersPass_Buy and not buyLossFreeze
                        and cooldownElapsed and slotAvailable_Buy)
        candidateSell = (isBearishRegime and rawTrigger_Sell and allFiltersPass_Sell and not sellLossFreeze
                         and cooldownElapsed and slotAvailable_Sell)

        # ============================================================ 9. SL & TARGET
        triggerBuy = triggerSell = False
        if candidateBuy:
            r = self._plan_trade(1, close, atr14, recentBullIdmSweep, globalLowestLow25, globalHighestHigh25)
            if r:
                calcEntry, calcSl, slDist, calcTarget, plannedRr = r
                triggerBuy = True
                self.activeTradeDir = 1
                self.activeTradeTrigger = candBuyTrig
                self.lastSignalBarIndex = bi
                self.lastBuyTriggerSource = candBuyTrig
                if "CHoCH" in candBuyTrig:
                    self.bullChochRetested = True
                    self.chochZoneTouchedBull = False
                self._open(1, candBuyTrig, calcEntry, calcSl, slDist, calcTarget, plannedRr, tstr, events)
                if 0 <= matchingBullObIdx < len(self.bullObList):
                    self.bullObList[matchingBullObIdx].tested = True
                    self.bullObList[matchingBullObIdx].mitigated = True
                if 0 <= matchingBullFvgIdx < len(self.bullFvgList):
                    self.bullFvgList[matchingBullFvgIdx].tested = True
                    self.bullFvgList[matchingBullFvgIdx].mitigated = True
                self.lastBullSweepBar = -100

        if candidateSell:
            r = self._plan_trade(-1, close, atr14, recentBearIdmSweep, globalLowestLow25, globalHighestHigh25)
            if r:
                calcEntry, calcSl, slDist, calcTarget, plannedRr = r
                triggerSell = True
                self.activeTradeDir = -1
                self.activeTradeTrigger = candSellTrig
                self.lastSignalBarIndex = bi
                self.lastSellTriggerSource = candSellTrig
                if "CHoCH" in candSellTrig:
                    self.bearChochRetested = True
                    self.chochZoneTouchedBear = False
                self._open(-1, candSellTrig, calcEntry, calcSl, slDist, calcTarget, plannedRr, tstr, events)
                if 0 <= matchingBearObIdx < len(self.bearObList):
                    self.bearObList[matchingBearObIdx].tested = True
                    self.bearObList[matchingBearObIdx].mitigated = True
                if 0 <= matchingBearFvgIdx < len(self.bearFvgList):
                    self.bearFvgList[matchingBearFvgIdx].tested = True
                    self.bearFvgList[matchingBearFvgIdx].mitigated = True
                self.lastBearSweepBar = -100

        # ============================================================ 10. TRADE MANAGEMENT
        tradeRiskDenom = max(nz(self.initialRiskPoints, 50.0), 10.0)
        if self.activeTradeDir == 1 and bi > self.lastSignalBarIndex:
            self._manage(1, isBearishChoch, open_, high, low, close, tradeRiskDenom, tstr, events)
        if self.activeTradeDir == -1 and bi > self.lastSignalBarIndex:
            self._manage(-1, isBullishChoch, open_, high, low, close, tradeRiskDenom, tstr, events)

        # ------------------------------------------------------------ remember [1] values
        self.prev.update({
            "htfLastPh": htfLastPh, "htfLastPl": htfLastPl, "htfLastIh": htfLastIh, "htfLastIl": htfLastIl,
            "htfObOpen": htfObOpen, "htfObHigh": htfObHigh, "htfObLow": htfObLow, "htfObClose": htfObClose,
            "htfObTime": htfObTime,
        })
        self.diag = {
            "bar_index": bi, "time": tstr, "close": close, "trendBias": self.trendBias, "htfBias": htfBias,
            "atr14": atr14, "protectedStrongHigh": self.protectedStrongHigh, "protectedStrongLow": self.protectedStrongLow,
            "activeTargetHigh": self.activeTargetHigh, "activeTargetLow": self.activeTargetLow,
            "activeIdmHigh": self.activeIdmHigh, "activeIdmLow": self.activeIdmLow,
            "equilibrium": equilibriumPrice, "inDiscount": inDiscountZone, "inPremium": inPremiumZone,
            "isBullishChoch": isBullishChoch, "isBearishChoch": isBearishChoch,
            "isBullishBos": isBullishBos, "isBearishBos": isBearishBos,
            "t1_Bull": t1_Bull, "t2_Bull": t2_Bull, "t3_Bull": t3_Bull, "t4_Bull": t4_Bull,
            "t1_Bear": t1_Bear, "t2_Bear": t2_Bear, "t3_Bear": t3_Bear, "t4_Bear": t4_Bear,
            "allFiltersPass_Buy": allFiltersPass_Buy, "allFiltersPass_Sell": allFiltersPass_Sell,
            "triggerBuy": triggerBuy, "triggerSell": triggerSell,
            "activeTradeDir": self.activeTradeDir, "slPrice": self.slPrice, "targetPrice": self.targetPrice,
            "tp1Hit": self.tp1Hit, "accumulatedNetR": self.accumulatedNetR,
        }
        return events

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _push_zone(lst: List[Zone], z: Zone, max_n: int):
        lst.append(z)
        if len(lst) > max_n:
            lst.pop(0)

    def _plan_trade(self, d: int, close: float, atr14, recentSweep: bool, ll25, hh25):
        s = self.s
        calcEntry = close
        if s.slMode == "Fixed Points (Preset)":
            pts = 70.0 if s.slFixedPreset == "70 Points" else (80.0 if s.slFixedPreset == "80 Points" else 90.0)
            calcSl = calcEntry - d * pts
        elif s.slMode == "Custom Points":
            calcSl = calcEntry - d * s.slCustomPoints
        else:
            if d == 1:
                struct = (self.protectedStrongLow if self.protectedStrongLow is not None
                          else (self.pivotLowLevels[0] if self.pivotLowLevels else ll25))
                if s.strat14_Psychology2 and recentSweep and self.sweepExtremeLow is not None:
                    struct = nmin(struct, self.sweepExtremeLow)
                candidateSl = sub(struct, s.slBufferPoints)
                minSafe = sub(calcEntry, nmax(None if atr14 is None else atr14 * 1.5, 60.0))
                calcSl = nmin(candidateSl, minSafe)
                if calcSl is not None and (calcEntry - calcSl) > s.maxAllowedSlPoints:
                    calcSl = calcEntry - s.maxAllowedSlPoints
            else:
                struct = (self.protectedStrongHigh if self.protectedStrongHigh is not None
                          else (self.pivotHighLevels[0] if self.pivotHighLevels else hh25))
                if s.strat14_Psychology2 and recentSweep and self.sweepExtremeHigh is not None:
                    struct = nmax(struct, self.sweepExtremeHigh)
                candidateSl = add(struct, s.slBufferPoints)
                minSafe = add(calcEntry, nmax(None if atr14 is None else atr14 * 1.5, 60.0))
                calcSl = nmax(candidateSl, minSafe)
                if calcSl is not None and (calcSl - calcEntry) > s.maxAllowedSlPoints:
                    calcSl = calcEntry + s.maxAllowedSlPoints
        if calcSl is None:
            return None
        slDist = (calcEntry - calcSl) * d
        slAcceptable = slDist > 0 and slDist <= s.maxAllowedSlPoints

        calcTarget = None
        if s.strat15_LiquidityRun:
            best = 1e10
            levels = self.pivotHighLevels if d == 1 else self.pivotLowLevels
            for p in levels:
                dist = (p - calcEntry) * d
                if dist > 0 and dist < best:
                    best, calcTarget = dist, p
        if calcTarget is None:
            calcTarget = calcEntry + d * slDist * 2.0
        targetDist = (calcTarget - calcEntry) * d
        plannedRr = targetDist / slDist if slDist > 0 else 1.5
        targetAcceptable = True
        if plannedRr < s.minTargetRr:
            if s.subMinTargetAction == "Skip Trade":
                targetAcceptable = False
            else:
                calcTarget = calcEntry + d * slDist * s.minTargetRr
                plannedRr = s.minTargetRr
        if s.enableMaxTargetCap and plannedRr > s.maxTargetRr:
            calcTarget = calcEntry + d * slDist * s.maxTargetRr
            plannedRr = s.maxTargetRr
        if slAcceptable and targetAcceptable:
            return calcEntry, calcSl, slDist, calcTarget, plannedRr
        return None

    def _open(self, d, trig, entry, sl, slDist, target, rr, tstr, events):
        self.entryPrice = entry
        self.slPrice = sl
        self.initialRiskPoints = slDist
        self.targetPrice = target
        self.currentTradeRr = rr
        self.tp1Price = entry + d * slDist * self.s.tp1RrMultiple
        self.tp1Hit = False
        self.tp1HitBarIndex = -100
        if self._open_trade is not None:  # pyramiding / reversal overwrites (Pine behaviour)
            self._close_record(self.bar_index, tstr, entry, "REPLACED", 0.0, 0.0)
        self._open_trade = TradeRecord(d, trig, self.bar_index, tstr, entry, sl, target, self.tp1Price, rr)
        events.append(Event("ENTRY", self.bar_index, tstr, d, trig, entry, sl, target, self.tp1Price, rr))

    def _close_record(self, bi, tstr, exit_price, reason, net_r, pnl):
        t = self._open_trade
        if t is None:
            return
        t.exit_bar, t.exit_time, t.exit, t.reason, t.net_r, t.pnl_points = bi, tstr, exit_price, reason, net_r, pnl
        self.trades.append(t)
        self._open_trade = None

    def _exit(self, d, reason, price, net_r, pnl, tstr, events):
        self.accumulatedNetR += net_r
        self.activeTradeDir = 0
        self.activeTradeTrigger = ""
        self._close_record(self.bar_index, tstr, price, reason, net_r, pnl)
        events.append(Event("EXIT", self.bar_index, tstr, d, price=price, reason=reason, pnl_points=pnl,
                            sl=self.slPrice, target=self.targetPrice))

    def _manage(self, d, oppChoch, open_, high, low, close, denom, tstr, events):
        s = self.s
        bi = self.bar_index
        cost = s.costPerTradePoints
        if oppChoch:
            pnl = (close - self.entryPrice) * d - cost
            if pnl > 0:
                self.winTradesCount += 1
            else:
                self.lossTradesCount += 1
                self.lastLossBarIndex = bi
                self.lastLossDir = d
            self._exit(d, "CHOCH", close, pnl / denom, pnl, tstr, events)

        if d == 1:
            collision = high >= self.tp1Price and low <= self.slPrice
            tp1_touch = high >= self.tp1Price
        else:
            collision = low <= self.tp1Price and high >= self.slPrice
            tp1_touch = low <= self.tp1Price
        if tp1_touch and not self.tp1Hit and not collision:
            self.tp1Hit = True
            self.tp1HitBarIndex = bi
            if self.activeTradeDir == d:
                events.append(Event("TP1", bi, tstr, d, price=self.tp1Price))

        if s.enableBreakEven and self.tp1Hit and bi > self.tp1HitBarIndex:
            new_sl = max(self.slPrice, self.entryPrice) if d == 1 else min(self.slPrice, self.entryPrice)
            if new_sl != self.slPrice:
                self.slPrice = new_sl
                if self.activeTradeDir == d:
                    events.append(Event("SL_MOVE", bi, tstr, d, sl=new_sl, reason="BREAK_EVEN"))

        if d == 1:
            sl_hit = low <= self.slPrice or open_ <= self.slPrice
            tgt_hit = high >= self.targetPrice or open_ >= self.targetPrice
            sl_exit_px = open_ if open_ <= self.slPrice else self.slPrice
        else:
            sl_hit = high >= self.slPrice or open_ >= self.slPrice
            tgt_hit = low <= self.targetPrice or open_ <= self.targetPrice
            sl_exit_px = open_ if open_ >= self.slPrice else self.slPrice

        if self.activeTradeDir == d and sl_hit:
            isRealBe = (s.enableBreakEven and self.tp1Hit and bi > self.tp1HitBarIndex
                        and ((self.slPrice >= self.entryPrice) if d == 1 else (self.slPrice <= self.entryPrice)))
            if isRealBe:
                self.beTradesCount += 1
                pnl = (sl_exit_px - self.entryPrice) * d - cost
                self._exit(d, "BE", sl_exit_px, pnl / denom, pnl, tstr, events)
            else:
                self.lossTradesCount += 1
                self.lastLossBarIndex = bi
                self.lastLossDir = d
                lossPoints = (self.entryPrice - sl_exit_px) * d
                self._exit(d, "SL", sl_exit_px, -((lossPoints + cost) / denom), -lossPoints - cost, tstr, events)
        elif self.activeTradeDir == d and tgt_hit:
            self.winTradesCount += 1
            realized = self.currentTradeRr - cost / denom
            pnl = abs(self.targetPrice - self.entryPrice) - cost
            self._exit(d, "TARGET", self.targetPrice, realized, pnl, tstr, events)
        elif self.activeTradeDir == d and (bi - self.lastSignalBarIndex) >= s.maxTradeDuration:
            self.timeTradesCount += 1
            pnl = (close - self.entryPrice) * d - cost
            self._exit(d, "TIME", close, pnl / denom, pnl, tstr, events)

    # ------------------------------------------------------------------ reporting
    def stats(self) -> Dict[str, object]:
        decided = self.winTradesCount + self.lossTradesCount
        return {
            "wins": self.winTradesCount, "losses": self.lossTradesCount, "breakeven": self.beTradesCount,
            "time_exits": self.timeTradesCount,
            "win_rate": round(self.winTradesCount / decided * 100, 2) if decided else 0.0,
            "net_r": round(self.accumulatedNetR, 3),
            "trades": len(self.trades),
        }

    def position(self) -> Dict[str, object]:
        return {
            "dir": self.activeTradeDir, "trigger": self.activeTradeTrigger, "entry": self.entryPrice,
            "sl": self.slPrice, "target": self.targetPrice, "tp1": self.tp1Price, "tp1Hit": self.tp1Hit,
            "rr": self.currentTradeRr, "entry_bar": self.lastSignalBarIndex,
        }
