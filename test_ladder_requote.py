"""SP4 — re-quote del ladder (ladder_requote_spec.md): la escalera sigue al mercado en vez de
quedarse anclada al precio del día que empezó el RANGO. PURO, sin red.

Lo que hay que probar (y que los tests DISCRIMINEN, directiva 8):
  1. sin los knobs, conducta bit-a-bit idéntica a hoy  → protege al canary vivo;
  2. con deriva arriba y sin re-quote, la escalera queda inalcanzable (el RED de la feature);
  3. el re-quote la vuelve alcanzable;
  4. un rung `holding` NO se toca al re-centrar;
  5. un rung `stopped` NO se re-arma si el re-centrado va hacia ABAJO (no caza el cuchillo);
  6. ...pero SÍ se re-arma si el precio se recuperó por encima.
"""
import entrenar


def _bar(close, high=None, low=None, atr_pct=2.0):
    return {"close": close, "high": high if high is not None else close,
            "low": low if low is not None else close, "atr_pct": atr_pct,
            "rsi": 50, "adx": 10, "ema200": close * 0.9, "green": True}


def _deriva_arriba():
    """ref=100 en bar0 (escalera 98/96/94...). El precio sube a ~115 y oscila 113↔116:
    oscilación del 2.6% de sobra para cosechar, pero los rungs viejos (≤98) nunca se tocan."""
    bars = [_bar(100)]
    for _ in range(4):                       # rampa hasta 115, sin volver a tocar 98
        bars.append(_bar(104))
        bars.append(_bar(110))
        bars.append(_bar(115))
    for _ in range(20):                      # dientes de sierra 112 ↔ 117
        bars.append(_bar(113, high=114, low=112))
        bars.append(_bar(116, high=117, low=115))
    return bars, [True] * len(bars)


# =============================================================================
# 1. No-regresión: sin los knobs nuevos, nada cambia (el canary corre esta lógica)
# =============================================================================
def test_sin_knobs_conducta_identica():
    bars, oper = _deriva_arriba()
    base = entrenar.sim_grid_ladder(bars, oper, spacing_pct=2.0, n_rungs=5)
    explicito = entrenar.sim_grid_ladder(bars, oper, spacing_pct=2.0, n_rungs=5,
                                         recenter_drift_pct=None, refresh_bars=None)
    assert base == explicito


# =============================================================================
# 2-3. La patología y su arreglo
# =============================================================================
def test_deriva_arriba_deja_la_escalera_inalcanzable():
    """RED de la feature: con la escalera anclada en 100, una oscilación cosechable en 113-116
    no produce NI UNA compra. Si este test se cae, la feature perdió su razón de ser."""
    bars, oper = _deriva_arriba()
    r = entrenar.sim_grid_ladder(bars, oper, spacing_pct=2.0, n_rungs=5)
    assert r["n_buys"] == 0
    assert r["n_roundtrips"] == 0


def test_requote_por_deriva_vuelve_a_alcanzar_el_mercado():
    bars, oper = _deriva_arriba()
    r = entrenar.sim_grid_ladder(bars, oper, spacing_pct=2.0, n_rungs=5, recenter_drift_pct=3.0)
    assert r["n_buys"] > 0
    assert r["n_roundtrips"] > 0


def test_requote_por_tiempo_vuelve_a_alcanzar_el_mercado():
    bars, oper = _deriva_arriba()
    r = entrenar.sim_grid_ladder(bars, oper, spacing_pct=2.0, n_rungs=5, refresh_bars=4)
    assert r["n_buys"] > 0


def test_drift_grande_no_re_cotiza():
    """Discrimina: si el umbral de deriva es mayor que la deriva real, la feature NO debe actuar
    (si actuara, el knob no estaría midiendo lo que dice medir — directiva 16)."""
    bars, oper = _deriva_arriba()
    sin = entrenar.sim_grid_ladder(bars, oper, spacing_pct=2.0, n_rungs=5)
    con = entrenar.sim_grid_ladder(bars, oper, spacing_pct=2.0, n_rungs=5, recenter_drift_pct=500.0)
    assert sin == con


# =============================================================================
# 4. Un rung con inventario no se toca
# =============================================================================
def test_recentrado_no_toca_un_holding():
    # ref=100 → rung1 buy=98. bar1 low 98 compra (sell target 99.96). Luego el precio sube a 130:
    # el re-quote mueve los rungs ARMADOS, pero la venta del lote comprado sigue en 99.96 y se
    # ejecuta en la subida → exactamente 1 round-trip con su qty original.
    bars = [_bar(100), _bar(98, high=98, low=98)]
    bars += [_bar(105), _bar(115), _bar(130, high=130, low=129)]
    r = entrenar.sim_grid_ladder(bars, [True] * len(bars), spacing_pct=2.0, n_rungs=1,
                                 recenter_drift_pct=3.0)
    assert r["n_roundtrips"] == 1
    # vendió a su target viejo (99.96), no a 130: la ganancia realizada es la del spacing, no la del rally
    assert 0.0 < r["realizado_pct"] < 1.0


# =============================================================================
# 5-6. El stop de SP2 sobrevive al re-quote
# =============================================================================
def _stop_y_luego(mover_a):
    """Compra en 98, la cortan por stop a 93.1, y después el precio se va a `mover_a`."""
    bars = [_bar(100), _bar(98, high=98, low=98), _bar(94, high=95, low=92)]
    bars += [_bar(mover_a, high=mover_a, low=mover_a) for _ in range(6)]
    return bars, [True] * len(bars)


def test_stopped_no_se_rearma_si_el_recentrado_va_hacia_abajo():
    """El cuchillo sigue cayendo (92 → 80): re-armar ahí es re-comprar lo que el stop acaba de
    cortar. El rung debe quedarse `stopped` ⟹ ninguna compra nueva."""
    bars, oper = _stop_y_luego(80)
    r = entrenar.sim_grid_ladder(bars, oper, spacing_pct=2.0, n_rungs=1,
                                 stop_pct=5.0, recenter_drift_pct=3.0)
    assert r["n_stops"] == 1
    assert r["n_buys"] == 1              # solo la compra original, ninguna re-compra
    assert r["qty_final"] == 0.0


def test_stopped_si_se_rearma_cuando_el_precio_se_recupera():
    """El precio se recuperó por encima de la referencia (92 → 108): el motivo del stop ya no
    aplica, la escalera vuelve a armarse."""
    bars, oper = _stop_y_luego(108)
    bars.append(_bar(107, high=108, low=105))    # dip que toca el rung re-centrado (108·0.98=105.84)
    oper.append(True)
    r = entrenar.sim_grid_ladder(bars, oper, spacing_pct=2.0, n_rungs=1,
                                 stop_pct=5.0, recenter_drift_pct=3.0)
    assert r["n_stops"] == 1
    assert r["n_buys"] == 2              # la original + la re-armada tras recuperarse


# =============================================================================
# 7. Invariantes de siempre bajo el knob nuevo
# =============================================================================
def test_requote_no_sobregira_capital():
    bars, oper = _deriva_arriba()
    r = entrenar.sim_grid_ladder(bars, oper, spacing_pct=2.0, n_rungs=8, invest_per_rung_pct=0.3,
                                 capital0=1000.0, recenter_drift_pct=2.0)
    assert r["cash_final"] >= -1e-6


def test_requote_no_compra_fuera_de_operable():
    bars, _ = _deriva_arriba()
    r = entrenar.sim_grid_ladder(bars, [False] * len(bars), spacing_pct=2.0, n_rungs=5,
                                 recenter_drift_pct=2.0, refresh_bars=2)
    assert r["n_buys"] == 0
