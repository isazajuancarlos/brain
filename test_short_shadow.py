"""Tests del Gestor de Salida SHADOW — Fase 1 (multiverso de MEDICIÓN).

Cero exchange, cero capital: todo por inyección directa a `_procesar`. Verifica que
cada política de salida candidata registra su PnL contrafactual correctamente y que
el ciclo de la posición virtual (abrir/gestionar/retirar) es correcto.

El bot debe estar APAGADO al correr esta suite.
"""
import importlib
import os

import main as _main_mod

SYM = "BTC/USDT:USDT"


def _reload(env=None):
    base = {
        "SHORT_SL_PCT": 0.04,
        "SHORT_TP_PCT": 0.03,
        "SHORT_SHADOW_CHANDELIER_K": 2.5,
        "SHORT_SHADOW_ML_ADVERSO": 0.60,
        "SHORT_SHADOW_MAX_HOLD_S": 21600,
    }
    base.update(env or {})
    for k, v in base.items():
        os.environ[k] = str(v)
    importlib.reload(_main_mod)
    return _main_mod


def _db(m):
    return m.DatabaseManager(":memory:")


def _ind(adx=25.0, rsi=60.0, macdh=-0.5, dmp=10.0, dmn=20.0, atr=100.0, close=60000.0):
    """Indicadores que, con los defaults, hacen disparar senal_short (ADX>20, RSI>55,
    MACDh<0, −DI≥+DI)."""
    return {"ADX_14": adx, "RSI_14": rsi, "MACDh_12_26_9": macdh,
            "DMP_14": dmp, "DMN_14": dmn, "ATRr_14": atr, "close": close}


def _rows(db):
    return db._exec("SELECT politica, entry_px, exit_px, pnl_pct, motivo "
                    "FROM short_shadow_pnl ORDER BY id", fetch="all") or []


def _de(rows, politica):
    return [r for r in rows if r[0] == politica]


# ---------------------------------------------------------------- ciclo de vida

def test_abre_virtual_cuando_senal_dispara():
    m = _reload(); g = m.GestorSalidaShadow(_db(m))
    g._procesar(SYM, 60000.0, _ind(), None, regime_ok=True, now=1000.0)
    assert SYM in g._abiertas
    vp = g._abiertas[SYM]
    assert vp["entry_px"] == 60000.0
    assert vp["vivas"] == set(m.POLITICAS_SALIDA.keys())


def test_no_abre_si_regime_no_es_bajista():
    m = _reload(); g = m.GestorSalidaShadow(_db(m))
    g._procesar(SYM, 60000.0, _ind(), None, regime_ok=False, now=1000.0)
    assert SYM not in g._abiertas


def test_no_abre_si_senal_no_dispara():
    m = _reload(); g = m.GestorSalidaShadow(_db(m))
    g._procesar(SYM, 60000.0, _ind(rsi=40.0), None, regime_ok=True, now=1000.0)
    assert SYM not in g._abiertas


def test_una_virtual_por_simbolo():
    m = _reload(); g = m.GestorSalidaShadow(_db(m))
    g._procesar(SYM, 60000.0, _ind(), None, regime_ok=True, now=1000.0)
    g._procesar(SYM, 59000.0, _ind(), None, regime_ok=True, now=1060.0)  # señal sigue, ya hay vp
    assert g._abiertas[SYM]["entry_px"] == 60000.0   # no se reabrió


# ---------------------------------------------------------------- políticas

def test_oco_fijo_registra_tp_con_pnl_positivo():
    m = _reload(); db = _db(m); g = m.GestorSalidaShadow(db)
    g._procesar(SYM, 60000.0, _ind(), None, regime_ok=True, now=1000.0)   # abre
    g._procesar(SYM, 58000.0, _ind(), None, regime_ok=True, now=1060.0)   # px ≤ TP(58200)
    oco = _de(_rows(db), "oco_fijo")
    assert len(oco) == 1
    assert oco[0][4] == "tp-fijo"
    assert abs(oco[0][3] - 3.0) < 1e-6      # (60000-58200)/60000 = +3%


def test_oco_fijo_registra_sl_con_pnl_negativo():
    m = _reload(); db = _db(m); g = m.GestorSalidaShadow(db)
    g._procesar(SYM, 60000.0, _ind(), None, regime_ok=True, now=1000.0)
    g._procesar(SYM, 62500.0, _ind(), None, regime_ok=True, now=1060.0)   # px ≥ SL(62400)
    oco = _de(_rows(db), "oco_fijo")
    assert len(oco) == 1
    assert oco[0][4] == "sl-duro"
    assert abs(oco[0][3] - (-4.0)) < 1e-6   # (60000-62400)/60000 = −4%


def test_chandelier_cabalga_la_caida_y_supera_al_tp_fijo():
    m = _reload(); db = _db(m); g = m.GestorSalidaShadow(db)
    g._procesar(SYM, 60000.0, _ind(atr=100.0), None, regime_ok=True, now=1000.0)
    g._procesar(SYM, 56000.0, _ind(atr=100.0), None, regime_ok=True, now=1060.0)  # oco toma TP; chand cabalga
    g._procesar(SYM, 55000.0, _ind(atr=100.0), None, regime_ok=True, now=1120.0)  # mejor_px baja
    g._procesar(SYM, 55300.0, _ind(atr=100.0), None, regime_ok=True, now=1180.0)  # retroceso >2.5·ATR → sale
    rows = _rows(db)
    oco = _de(rows, "oco_fijo")[0]
    chan = _de(rows, "chandelier")[0]
    assert chan[4] == "chandelier"
    assert chan[3] > oco[3]                  # cabalgar capturó MÁS que el TP fijo


def test_momentum_sale_cuando_el_rebote_se_reanuda():
    m = _reload(); db = _db(m); g = m.GestorSalidaShadow(db)
    g._procesar(SYM, 60000.0, _ind(), None, regime_ok=True, now=1000.0)
    g._procesar(SYM, 59500.0, _ind(macdh=0.5), None, regime_ok=True, now=1060.0)  # MACDh>0 = momentum revierte
    mom = _de(_rows(db), "momentum")
    assert len(mom) == 1
    assert mom[0][4] == "momentum-revierte"


def test_ml_adverso_sale_con_prob_alta():
    m = _reload(); db = _db(m); g = m.GestorSalidaShadow(db)
    g._procesar(SYM, 60000.0, _ind(), None, regime_ok=True, now=1000.0)
    g._procesar(SYM, 59500.0, _ind(), 0.80, regime_ok=True, now=1060.0)   # ml_prob 0.8 ≥ umbral 0.60
    ml = _de(_rows(db), "ml_adverso")
    assert len(ml) == 1
    assert ml[0][4] == "ml-adverso"


# ---------------------------------------------------------------- max-hold + resumen

def test_max_hold_cierra_las_politicas_vivas_y_retira_la_virtual():
    m = _reload({"SHORT_SHADOW_MAX_HOLD_S": 100}); db = _db(m); g = m.GestorSalidaShadow(db)
    g._procesar(SYM, 60000.0, _ind(), None, regime_ok=True, now=1000.0)
    g._procesar(SYM, 60000.0, _ind(), None, regime_ok=True, now=1300.0)   # 300s > 100 → max-hold
    rows = _rows(db)
    assert len(rows) == len(m.POLITICAS_SALIDA)
    assert all(r[4] == "max-hold" for r in rows)
    assert SYM not in g._abiertas                     # retirada al morir todas


def test_resumen_agrupa_por_politica():
    m = _reload({"SHORT_SHADOW_MAX_HOLD_S": 100}); db = _db(m); g = m.GestorSalidaShadow(db)
    g._procesar(SYM, 60000.0, _ind(), None, regime_ok=True, now=1000.0)
    g._procesar(SYM, 60000.0, _ind(), None, regime_ok=True, now=1300.0)
    res = db.short_shadow_resumen()
    assert len(res) == len(m.POLITICAS_SALIDA)
    assert all(r["n"] == 1 for r in res)
