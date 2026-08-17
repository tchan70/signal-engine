"""engine/regime.py classifier and trade_constructor month/expiry fixes."""
import math
from datetime import date

import pytest

from engine.regime import RegimeDetector


def test_flat_closes_calm():
    label, vol = RegimeDetector.classify_closes([100.0] * 6)
    assert label == "calm"
    assert vol == pytest.approx(0.0)


def test_wild_closes_stressed():
    closes = [100, 103, 99.9, 103.2, 99.5, 103.5]
    label, vol = RegimeDetector.classify_closes([float(c) for c in closes])
    assert label == "stressed"
    assert vol > 0.28


def test_insufficient_data_unknown():
    label, vol = RegimeDetector.classify_closes([100.0, 101.0])
    assert label == "unknown"
    assert vol is None


def test_refresh_never_raises_on_broken_executor(config):
    class Broken:
        def get_daily_closes(self, *a, **k):
            raise RuntimeError("api down")
    det = RegimeDetector(config, executor=Broken())
    out = det.refresh()
    assert out["multiplier"] == 1.0


def test_refresh_disabled(config):
    cfg = dict(config)
    cfg["regime"] = {"enabled": False}
    det = RegimeDetector(cfg, executor=None)
    assert det.refresh()["multiplier"] == 1.0


# ── H12/D7: month parsing in trade_constructor ───────────────────────────────




def _ctor_takes_config():
    import inspect
    from engine.trade_constructor import TradeConstructor
    params = inspect.signature(TradeConstructor.__init__).parameters
    return len(params) > 1
