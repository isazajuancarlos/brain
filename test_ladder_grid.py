"""SP1 — sim_grid_ladder: grid ladder round-trip (compra abajo, vende las MISMAS unidades arriba,
repite). PURO, sin red. Verifica las invariantes que deciden si el rediseño puede cosechar RANGO:
conservación del round-trip, fee, gate de régimen, capital finito, monotonía del spacing."""
import entrenar


def _bar(close, high=None, low=None, atr_pct=2.0):
    return {"close": close, "high": high if high is not None else close,
            "low": low if low is not None else close, "atr_pct": atr_pct,
            "rsi": 50, "adx": 10, "ema200": close * 0.9, "green": True}


def test_round_trip_compra_abajo_vende_arriba():
    # ref=100 @bar0; spacing 2% → rung1 buy=98, sell target 98*1.02=99.96.
    # bar1 low toca 98 (compra), bar2 high toca 100 (vende) → un round-trip.
    bars = [_bar(100), _bar(99, high=99, low=98), _bar(100, high=100, low=99)]
    r = entrenar.sim_grid_ladder(bars, [True] * 3, spacing_pct=2.0, n_rungs=1)
    assert r["n_buys"] >= 1
    assert r["n_roundtrips"] >= 1
    assert r["qty_final"] == 0.0          # cerró: vendió lo que compró


def test_fee_reduce_realizado():
    bars = [_bar(100), _bar(99, high=99, low=98), _bar(100, high=100, low=99)]
    sin_fee = entrenar.sim_grid_ladder(bars, [True] * 3, spacing_pct=2.0, n_rungs=1, fee=0.0)
    con_fee = entrenar.sim_grid_ladder(bars, [True] * 3, spacing_pct=2.0, n_rungs=1, fee=0.01)
    assert sin_fee["realizado_pct"] > con_fee["realizado_pct"]
    assert sin_fee["realizado_pct"] > 0   # sin fee el round-trip +2% es ganancia


def test_no_compra_si_no_operable():
    bars = [_bar(100), _bar(99, high=99, low=98), _bar(100, high=100, low=99)]
    r = entrenar.sim_grid_ladder(bars, [False] * 3, spacing_pct=2.0, n_rungs=5)
    assert r["n_buys"] == 0
    assert r["n_roundtrips"] == 0


def test_capital_finito_no_sobregira():
    # invest_per_rung alto + muchos rungs tocados: cash nunca negativo
    bars = [_bar(100)] + [_bar(80, high=82, low=80) for _ in range(10)]
    r = entrenar.sim_grid_ladder(bars, [True] * 11, spacing_pct=2.0, n_rungs=8,
                                 invest_per_rung_pct=0.3, capital0=1000.0)
    assert r["cash_final"] >= -1e-6


def test_spacing_ancho_no_aumenta_roundtrips():
    # un dientes-de-sierra: spacing más ancho cosecha ≤ que el más apretado
    bars = [_bar(100)]
    for _ in range(20):
        bars += [_bar(97, high=97, low=96), _bar(100, high=101, low=99)]
    oper = [True] * len(bars)
    apretado = entrenar.sim_grid_ladder(bars, oper, spacing_pct=1.0, n_rungs=3)
    ancho = entrenar.sim_grid_ladder(bars, oper, spacing_pct=3.0, n_rungs=3)
    assert apretado["n_roundtrips"] >= ancho["n_roundtrips"]


def test_compra_y_venta_no_cierran_en_misma_barra():
    # conservador: una barra que baja al rung Y sube al sell NO debe contar round-trip en esa barra
    # (la venta del lote recién comprado espera a una barra posterior)
    bars = [_bar(100), _bar(100, high=101, low=98)]   # bar1: low 98 compra, high 101 ≥ 99.96
    r = entrenar.sim_grid_ladder(bars, [True] * 2, spacing_pct=2.0, n_rungs=1)
    assert r["n_buys"] >= 1
    assert r["n_roundtrips"] == 0          # compró en bar1 pero no vendió en la misma barra


def test_sin_bars_devuelve_cero():
    r = entrenar.sim_grid_ladder([], [], spacing_pct=2.0, n_rungs=5)
    assert r["n_buys"] == 0 and r["net_pct"] == 0.0


# =============================================================================
# SP2 — stop a rungs colgados (stop_pct underwater / max_hold_bars zombi)
# =============================================================================
def test_stop_pct_corta_rung_underwater():
    # ref=100, spacing 2% → rung1 buy=98. bar1 low 98 compra; bar2 low 92 (underwater >5% desde 98)
    # con stop_pct=5 → corta a 98*0.95=93.1. Realiza pérdida, libera el slot.
    bars = [_bar(100), _bar(98, high=98, low=98), _bar(94, high=95, low=92)]
    r = entrenar.sim_grid_ladder(bars, [True] * 3, spacing_pct=2.0, n_rungs=1, stop_pct=5.0)
    assert r["n_stops"] >= 1
    assert r["realizado_pct"] < 0          # cortó en pérdida
    assert r["qty_final"] == 0.0           # liberó el slot


def test_sin_stop_no_corta():
    # mismo path pero sin stop_pct → el rung queda colgado (no se corta), n_stops==0
    bars = [_bar(100), _bar(98, high=98, low=98), _bar(94, high=95, low=92)]
    r = entrenar.sim_grid_ladder(bars, [True] * 3, spacing_pct=2.0, n_rungs=1)
    assert r["n_stops"] == 0
    assert r["qty_final"] > 0              # sigue colgado


def test_max_hold_bars_corta_zombi():
    # compra a 98 en bar1; se queda ahí sin tocar target (99.96) ni stop; max_hold=2 → corta en bar3
    bars = [_bar(100), _bar(98, high=98, low=98), _bar(98), _bar(98), _bar(98)]
    r = entrenar.sim_grid_ladder(bars, [True] * 5, spacing_pct=2.0, n_rungs=1, max_hold_bars=2)
    assert r["n_stops"] >= 1
    assert r["qty_final"] == 0.0


def test_stop_conserva_capital():
    # el corte no vende más unidades que las del rung; cash coherente
    bars = [_bar(100), _bar(98, high=98, low=98), _bar(90, high=92, low=88)]
    r = entrenar.sim_grid_ladder(bars, [True] * 3, spacing_pct=2.0, n_rungs=1,
                                 stop_pct=5.0, invest_per_rung_pct=0.1, capital0=1000.0)
    assert r["cash_final"] >= -1e-6
    assert r["qty_final"] == 0.0
