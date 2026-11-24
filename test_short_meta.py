"""Tests Fase 3 — meta-learner online (contexto + selección por recencia).

Cero exchange, cero capital. La selección se apoya en el feedback COMPLETO del shadow
(short_shadow_pnl): cada contexto pondera sus políticas por recencia y elige la mejor.
"""
import importlib
import os

import main as _main_mod

SYM = "BTC/USDT:USDT"
NOW = 1_000_000.0


def _reload(env=None):
    base = {"SHORT_SL_PCT": 0.04, "SHORT_TP_PCT": 0.03,
            "SHORT_META_VOL_LOW": 0.005, "SHORT_META_VOL_HIGH": 0.015,
            "SHORT_FUNDING_CONTEXT": "false",   # reset por test (evita fuga de env entre reloads)
            "SHORT_SHADOW_MAX_HOLD_S": 21600}   # default; tests que necesitan otro lo overridean
    base.update(env or {})
    for k, v in base.items():
        os.environ[k] = str(v)
    importlib.reload(_main_mod)
    return _main_mod


def _db(m):
    return m.DatabaseManager(":memory:")


def _ind(adx=25.0, rsi=60.0, macdh=-0.5, dmp=10.0, dmn=20.0, atr=100.0, close=60000.0):
    return {"ADX_14": adx, "RSI_14": rsi, "MACDh_12_26_9": macdh,
            "DMP_14": dmp, "DMN_14": dmn, "ATRr_14": atr, "close": close}


def _row(pol, pnl, ts):
    return {"politica": pol, "pnl_pct": pnl, "exit_ts": ts}


# ------------------------------------------------------------ contexto_vol
def test_contexto_vol_calmo():
    m = _reload()
    assert m.contexto_vol({"close": 60000.0, "ATRr_14": 100.0}) == "calmo"   # 0.0017 < 0.005


def test_contexto_vol_picado():
    m = _reload()
    assert m.contexto_vol({"close": 60000.0, "ATRr_14": 1200.0}) == "picado"  # 0.02 > 0.015


def test_contexto_vol_normal():
    m = _reload()
    assert m.contexto_vol({"close": 60000.0, "ATRr_14": 600.0}) == "normal"   # 0.01


def test_contexto_vol_sin_datos_es_normal():
    m = _reload()
    assert m.contexto_vol({}) == "normal"


# ------------------------------------------------ seleccionar_politica_meta
def test_meta_sin_rows_cae_a_oco_fijo():
    m = _reload()
    assert m.seleccionar_politica_meta([], 2, 0.0, 100.0, NOW) == "oco_fijo"


def test_meta_elige_la_mejor_del_contexto():
    m = _reload()
    rows = [_row("oco_fijo", 0.0, NOW), _row("oco_fijo", 0.0, NOW),
            _row("momentum", 2.0, NOW), _row("momentum", 2.0, NOW)]
    assert m.seleccionar_politica_meta(rows, 2, 0.2, 1e9, NOW) == "momentum"


def test_meta_exige_min_trades():
    m = _reload()
    rows = [_row("oco_fijo", 0.0, NOW), _row("oco_fijo", 0.0, NOW),
            _row("momentum", 5.0, NOW)]                       # 1 sola muestra
    assert m.seleccionar_politica_meta(rows, 2, 0.0, 1e9, NOW) == "oco_fijo"


def test_meta_exige_margen_sobre_baseline():
    m = _reload()
    rows = [_row("oco_fijo", 1.0, NOW), _row("oco_fijo", 1.0, NOW),
            _row("momentum", 1.1, NOW), _row("momentum", 1.1, NOW)]   # +0.1 < 0.2
    assert m.seleccionar_politica_meta(rows, 2, 0.2, 1e9, NOW) == "oco_fijo"


def test_meta_recency_favorece_lo_reciente():
    m = _reload()
    rows = [_row("oco_fijo", 0.0, NOW), _row("oco_fijo", 0.0, NOW)]
    # momentum: pasado bueno, reciente malo. chandelier: pasado malo, reciente bueno.
    for _ in range(3):
        rows += [_row("momentum", 5.0, NOW - 1000), _row("momentum", -3.0, NOW),
                 _row("chandelier", -3.0, NOW - 1000), _row("chandelier", 5.0, NOW)]
    # half-life 100s: lo de hace 1000s pesa ~0.001 → gana el reciente (chandelier)
    assert m.seleccionar_politica_meta(rows, 2, 0.0, 100.0, NOW) == "chandelier"


# ------------------------------------------------ DB: filtro por contexto
def test_rows_por_contexto_filtra():
    m = _reload(); db = _db(m)
    db.short_shadow_registrar("momentum", SYM, 60000, 59000, 1.0, "x", 100, 200, contexto="calmo")
    db.short_shadow_registrar("momentum", SYM, 60000, 61000, -1.0, "y", 100, 200, contexto="picado")
    calmo = db.short_shadow_rows_por_contexto("calmo")
    assert len(calmo) == 1 and calmo[0]["pnl_pct"] == 1.0


# ------------------------------------------------ shadow registra contexto
def test_shadow_registra_el_contexto_de_entrada():
    m = _reload({"SHORT_SHADOW_MAX_HOLD_S": 100}); db = _db(m)
    g = m.GestorSalidaShadow(db)
    ind = _ind(atr=100.0, close=60000.0)                    # 0.0017 → calmo
    g._procesar(SYM, 60000.0, ind, None, regime_ok=True, now=1000.0)
    g._procesar(SYM, 60000.0, ind, None, regime_ok=True, now=1300.0)   # max-hold cierra todas
    ctxs = db._exec("SELECT DISTINCT contexto FROM short_shadow_pnl", fetch="all")
    assert ctxs and ctxs[0][0] == "calmo"


# ------------------------------------------------ backfill (replay histórico)
def _bar(ts, close, macdh=-0.5, rsi=60.0, adx=25.0, dmp=10.0, dmn=20.0, atr=100.0,
         high=None, low=None):
    return {"ts": ts, "close": close, "high": high if high is not None else close,
            "low": low if low is not None else close, "ADX_14": adx, "RSI_14": rsi,
            "MACDh_12_26_9": macdh, "DMP_14": dmp, "DMN_14": dmn, "ATRr_14": atr}


def test_backfill_replay_registra_trades_en_bajista():
    m = _reload(); db = _db(m)
    bars = [_bar(1000, 60000.0),          # senal dispara → abre virtual
            _bar(1900, 58000.0)]          # cae a TP → oco_fijo registra
    n = m.backfill_shadow_bars(db, SYM, bars, [True, True])
    assert n == 2
    pols = [r[0] for r in db._exec("SELECT politica FROM short_shadow_pnl", fetch="all")]
    assert "oco_fijo" in pols


def test_backfill_no_registra_fuera_de_bajista():
    m = _reload(); db = _db(m)
    bars = [_bar(1000, 60000.0), _bar(1900, 58000.0)]
    m.backfill_shadow_bars(db, SYM, bars, [False, False])    # régimen NO bajista
    assert db._exec("SELECT COUNT(*) FROM short_shadow_pnl", fetch="one")[0] == 0


def test_backfill_registra_contexto():
    m = _reload(); db = _db(m)
    # atr/close = 100/60000 = 0.0017 → 'calmo'
    bars = [_bar(1000, 60000.0, atr=100.0), _bar(1900, 58000.0, atr=100.0)]
    m.backfill_shadow_bars(db, SYM, bars, [True, True])
    ctxs = db.short_shadow_rows_por_contexto("calmo")
    assert len(ctxs) >= 1


# ------------------------------------------------ readiness report
def test_shadow_listo_marca_contexto_con_muestra():
    m = _reload(); db = _db(m)
    for _ in range(5):
        db.short_shadow_registrar("oco_fijo", SYM, 60000, 60000, 0.0, "x", 0, 1.0, contexto="normal")
        db.short_shadow_registrar("momentum", SYM, 60000, 59000, 1.0, "y", 0, 1.0, contexto="normal")
    rep = db.short_shadow_listo(3)
    assert rep["normal"]["listo"] is True
    assert rep["normal"]["lider"] == "momentum"


def test_shadow_listo_falso_sin_muestra_suficiente():
    m = _reload(); db = _db(m)
    db.short_shadow_registrar("momentum", SYM, 60000, 59000, 1.0, "y", 0, 1.0, contexto="normal")
    rep = db.short_shadow_listo(30)
    assert rep["normal"]["listo"] is False


# ------------------------------------------------ gate APAREADO (varianza/confianza)
def _pair(deltas, base=0.0, now=NOW):
    """Trades virtuales apareados: oco_fijo=base, momentum=base+delta, entry_ts distinto/trade."""
    rows = []
    for i, d in enumerate(deltas):
        rows.append({"politica": "oco_fijo", "pnl_pct": base, "exit_ts": now, "entry_ts": float(i), "symbol": "S"})
        rows.append({"politica": "momentum", "pnl_pct": base + d, "exit_ts": now, "entry_ts": float(i), "symbol": "S"})
    return rows


def test_apareado_gradua_ventaja_consistente():
    m = _reload()
    assert m.seleccionar_politica_meta_apareado(_pair([1.0] * 10), 5, 1e18, NOW) == "momentum"


def test_apareado_rechaza_alta_varianza():
    m = _reload()
    # media +0.5 pero varianza enorme (+6/−5 alternados) → LCB < 0 → no se gradúa
    assert m.seleccionar_politica_meta_apareado(_pair([6.0, -5.0] * 5), 5, 1e18, NOW) == "oco_fijo"


def test_apareado_exige_n_efectivo():
    m = _reload()
    assert m.seleccionar_politica_meta_apareado(_pair([1.0] * 3), 5, 1e18, NOW) == "oco_fijo"


def test_apareado_sin_pares_cae_a_oco_fijo():
    m = _reload()
    rows = [{"politica": "momentum", "pnl_pct": 5.0, "exit_ts": NOW, "entry_ts": 1.0, "symbol": "S"}]
    assert m.seleccionar_politica_meta_apareado(rows, 1, 1e18, NOW) == "oco_fijo"


# ------------------------------------------------ funding como contexto (Punto C)
def test_contexto_funding_neg_pos_neutro():
    m = _reload({"SHORT_FUNDING_THR": 0.0005})
    assert m.contexto_funding(-0.001) == "neg"      # shorts amontonados (squeeze-up risk)
    assert m.contexto_funding(0.001) == "pos"       # longs amontonados (rally inflado)
    assert m.contexto_funding(0.0) == "neutro"


def test_contexto_short_default_es_solo_vol():
    m = _reload()                                   # SHORT_FUNDING_CONTEXT default false
    assert m.contexto_short({"close": 60000.0, "ATRr_14": 100.0, "funding": -0.01}) == "calmo"


def test_contexto_short_con_funding_combina():
    m = _reload({"SHORT_FUNDING_CONTEXT": "true", "SHORT_FUNDING_THR": 0.0005})
    ind = {"close": 60000.0, "ATRr_14": 100.0, "funding": -0.01}    # calmo + funding neg
    assert m.contexto_short(ind) == "calmo|neg"


def test_get_set_funding():
    m = _reload()
    m.set_funding("BTC/USDT:USDT", -0.0008, ts=123.0)
    assert m.get_funding("BTC/USDT:USDT") == -0.0008
    assert m.get_funding("ETH/USDT:USDT") == 0.0    # default sin dato


def test_shadow_registra_contexto_con_funding():
    m = _reload({"SHORT_FUNDING_CONTEXT": "true", "SHORT_FUNDING_THR": 0.0005,
                 "SHORT_SHADOW_MAX_HOLD_S": 100})
    db = _db(m); g = m.GestorSalidaShadow(db)
    ind = _ind(atr=100.0, close=60000.0); ind["funding"] = -0.01    # calmo|neg
    g._procesar(SYM, 60000.0, ind, None, regime_ok=True, now=1000.0)
    g._procesar(SYM, 60000.0, ind, None, regime_ok=True, now=1300.0)
    ctxs = db._exec("SELECT DISTINCT contexto FROM short_shadow_pnl", fetch="all")
    assert ctxs and ctxs[0][0] == "calmo|neg"


def test_backfill_usa_funding_del_bar():
    m = _reload({"SHORT_FUNDING_CONTEXT": "true", "SHORT_FUNDING_THR": 0.0005}); db = _db(m)
    b0 = _bar(1000, 60000.0, atr=100.0); b0["funding"] = -0.01   # calmo + funding neg → calmo|neg
    b1 = _bar(1900, 58000.0, atr=100.0); b1["funding"] = -0.01
    m.backfill_shadow_bars(db, SYM, [b0, b1], [True, True])
    assert len(db.short_shadow_rows_por_contexto("calmo|neg")) >= 1


# ------------------------------------------------ backfill INTRABAR (high/low)
def test_backfill_intrabar_tp_por_low():
    # close NO toca el TP, pero el LOW de la barra SÍ → con intrabar oco_fijo registra tp-fijo.
    m = _reload(); db = _db(m)
    b0 = _bar(1000, 60000.0)
    b1 = _bar(1900, 59000.0, high=59000.0, low=58000.0)   # TP=58200; low 58000 ≤ TP
    m.backfill_shadow_bars(db, SYM, [b0, b1], [True, True])
    oco = [r for r in db._exec("SELECT politica, motivo FROM short_shadow_pnl", fetch="all")
           if r[0] == "oco_fijo"]
    assert oco and oco[0][1] == "tp-fijo"


def test_backfill_intrabar_sl_por_high():
    # close NO toca el SL, pero el HIGH de la barra SÍ → con intrabar oco_fijo registra sl-duro.
    m = _reload(); db = _db(m)
    b0 = _bar(1000, 60000.0)
    b1 = _bar(1900, 61000.0, high=63000.0, low=61000.0)   # SL=62400; high 63000 ≥ SL
    m.backfill_shadow_bars(db, SYM, [b0, b1], [True, True])
    oco = [r for r in db._exec("SELECT politica, motivo FROM short_shadow_pnl", fetch="all")
           if r[0] == "oco_fijo"]
    assert oco and oco[0][1] == "sl-duro"


def test_backfill_no_reentra_dentro_de_la_misma_barra():
    # si cierra intrabar, NO re-abre hasta la próxima barra (1 entrada por barra, como en vivo).
    m = _reload(); db = _db(m)
    b0 = _bar(1000, 60000.0)
    b1 = _bar(1900, 61000.0, high=63000.0, low=61000.0)   # cierra todo por SL intrabar
    m.backfill_shadow_bars(db, SYM, [b0, b1], [True, True])
    assert SYM not in m.GestorSalidaShadow(db)._abiertas   # no quedó re-abierto en esa barra


# ------------------------------------------------ barredor de señal (param + senal_fn)
def test_senal_short_rsi_min_override():
    m = _reload()
    ind = _ind(rsi=52.0)                       # dmn=20≥dmp=10 (DI ok), macdh<0, adx>20
    assert m.senal_short(ind, rsi_min=50) is True
    assert m.senal_short(ind, rsi_min=55) is False


def test_senal_short_di_filter_off():
    m = _reload()
    ind = _ind(dmp=30.0, dmn=10.0)             # +DI domina → el filtro DI bloquea
    assert m.senal_short(ind) is False
    assert m.senal_short(ind, di_filter=False) is True


def test_backfill_acepta_senal_fn_custom():
    m = _reload(); db = _db(m)
    siempre = lambda ind: True                 # señal que siempre dispara
    bars = [_bar(1000, 60000.0), _bar(1900, 58000.0)]
    m.backfill_shadow_bars(db, SYM, bars, [True, True], senal_fn=siempre)
    n = db._exec("SELECT COUNT(*) FROM short_shadow_pnl WHERE politica='oco_fijo'", fetch="one")[0]
    assert n >= 1
