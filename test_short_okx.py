"""Tests de la Pata Bajista (EscuadronShortOkx) — V1.7.0.

Todo corre contra FakeOkxFut en memoria. NUNCA toca dinero real.
El bot debe estar APAGADO al correr esta suite (no dos instancias contra OKX).
"""
import importlib
import os
import time

import pytest

import main as _main_mod


def _reload_main(env):
    for k, v in env.items():
        os.environ[k] = str(v)
    importlib.reload(_main_mod)
    return _main_mod


def _db_memoria(m):
    return m.DatabaseManager(":memory:")


class FakeOkxFut:
    """Exchange swap OKX en memoria. Replica la superficie ccxt que usa el wrapper.

    NO toca red ni dinero real. Simula fill inmediato del short y registra la posición.
    """

    def __init__(self, precio=63000.0, usdt_free=4000.0, fail_stop=False):
        self.precio = precio
        self.usdt_free = usdt_free
        self.fail_stop = fail_stop    # si True, las órdenes stop/SL tiran (simula rechazo OKX)
        self.leverage_calls = []      # (lev, symbol, params)
        self.orders = []              # cada create_order
        self.positions = {}           # symbol -> dict
        self.algos = []               # SL/stop colgados (resting)
        self.cancelled = []
        self._oid = 0

    def set_leverage(self, lev, symbol, params=None):
        self.leverage_calls.append((lev, symbol, params or {}))
        return {"leverage": lev}

    def fetch_balance(self, params=None):
        return {"USDT": {"free": self.usdt_free, "total": self.usdt_free},
                "free": {"USDT": self.usdt_free}, "info": {}}

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        params = params or {}
        is_bracket = any(k in params for k in ("stopLossPrice", "slTriggerPx", "takeProfitPrice"))
        if is_bracket and self.fail_stop:
            raise RuntimeError("okx 51280 bracket rechazado (simulado)")
        self._oid += 1
        oid = f"ord{self._oid}"
        self.orders.append(dict(id=oid, symbol=symbol, type=type_, side=side,
                                amount=amount, price=price, params=params))
        if is_bracket:
            # bracket OCO (TP+SL) colgado (resting): NO cierra la posición ahora.
            self.algos.append(dict(symbol=symbol, side=side,
                sl=params.get("stopLossPrice") or params.get("slTriggerPx"),
                tp=params.get("takeProfitPrice")))
        elif params.get("reduceOnly"):
            self.positions.pop(symbol, None)
        elif params.get("posSide") == "short":
            self.positions[symbol] = dict(symbol=symbol, contracts=amount,
                side="short", entryPrice=price or self.precio,
                marginMode=params.get("tdMode"))
        return {"id": oid, "info": {"sCode": "0", "algoId": f"algo{self._oid}"}}

    def fetch_positions(self, symbols=None, params=None):
        return list(self.positions.values())

    def cancel_order(self, oid, symbol=None, params=None):
        self.cancelled.append(oid)
        return {"id": oid}

    def private_post_trade_order_algo(self, req):
        # Endpoint nativo OKX para el bracket OCO. fail_stop simula el rechazo de OKX.
        if self.fail_stop:
            return {"code": "1", "data": [{"sCode": "51000",
                    "sMsg": "Parameter ordType error (simulado)"}]}
        self._oid += 1
        algo_id = f"algo{self._oid}"
        self.algos.append(dict(symbol=req.get("instId"), side=req.get("side"),
            ordType=req.get("ordType"), algoId=algo_id,
            sl=float(req["slTriggerPx"]) if req.get("slTriggerPx") else None,
            tp=float(req["tpTriggerPx"]) if req.get("tpTriggerPx") else None))
        return {"code": "0", "data": [{"sCode": "0", "algoId": algo_id}]}

    def private_post_trade_cancel_algos(self, reqs):
        for r in reqs:
            self.cancelled.append(r.get("algoId"))
        return {"code": "0", "data": [{"sCode": "0"}]}


class FakeTesoreria:
    """Simula GestorTesoreria.rescate_urgente: redime de earn → Trading (sube usdt_free
    del exchange fake). No toca red. Registra cada llamada para asertar el déficit."""

    def __init__(self, fx, earn=2000.0):
        self.fx = fx
        self.earn = earn
        self.rescates = []        # montos pedidos

    def rescate_urgente(self, amt):
        self.rescates.append(amt)
        got = min(self.earn, max(0.0, amt))
        self.earn -= got
        self.fx.usdt_free += got
        return got


def test_fake_okx_fut_basico():
    fx = FakeOkxFut()
    fx.set_leverage(3, "BTC/USDT:USDT", {"mgnMode": "isolated"})
    assert fx.leverage_calls[0][0] == 3
    # apertura short (sin stop) → registra posición
    fx.create_order("BTC/USDT:USDT", "market", "sell", 1.0, None,
                    {"posSide": "short", "tdMode": "isolated"})
    assert len(fx.fetch_positions()) == 1
    assert fx.fetch_positions()[0]["marginMode"] == "isolated"
    # stop separado → va a algos y NO cierra la posición (resting)
    fx.create_order("BTC/USDT:USDT", "market", "buy", 1.0, None,
                    {"posSide": "short", "reduceOnly": True, "stopLossPrice": 66000})
    assert len(fx.algos) == 1
    assert len(fx.fetch_positions()) == 1


# ---------------------------------------------------------------------------
# Task 4: TradingCoreShortOkx wrapper
# ---------------------------------------------------------------------------
def test_abrir_short_bracket_oco_tp_y_sl():
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_LEVERAGE": "3",
                      "SHORT_SL_PCT": "0.04", "SHORT_TP_PCT": "0.03"})
    fx = FakeOkxFut(precio=63000.0)
    core = m.TradingCoreShortOkx(exchange=fx)
    res = core.abrir_short("BTC/USDT:USDT", precio=63000.0, qty=0.01)
    lev, sym, params = fx.leverage_calls[-1]
    assert lev == 3 and params.get("mgnMode") == "isolated"
    # entrada: market sell, SIN bracket adjunto (evita el rechazo 51280)
    entry = fx.orders[0]
    assert entry["side"] == "sell" and entry["type"] == "market"
    assert "stopLossPrice" not in entry["params"] and "takeProfitPrice" not in entry["params"]
    # bracket OCO: SL +4% arriba (pérdida), TP -3% abajo (ganancia), en UNA orden
    assert len(fx.algos) == 1 and fx.algos[0]["side"] == "buy"
    assert abs(fx.algos[0]["sl"] - 63000.0 * 1.04) < 1.0
    assert abs(fx.algos[0]["tp"] - 63000.0 * 0.97) < 1.0
    assert res["sl_order_id"] and abs(res["tp_px"] - 63000.0 * 0.97) < 1.0


def test_sl_falla_cierra_short_fallback():
    # Si el SL no se puede colgar, abrir_short cierra el short y levanta (nunca queda desnudo).
    m = _reload_main({"OKX_FUT_ENABLED": "true"})
    fx = FakeOkxFut(precio=63000.0, fail_stop=True)
    core = m.TradingCoreShortOkx(exchange=fx)
    with pytest.raises(RuntimeError):
        core.abrir_short("BTC/USDT:USDT", precio=63000.0, qty=0.01)
    assert fx.fetch_positions() == []   # posición cerrada por el fallback, no queda desnuda


def test_cerrar_short_es_reduce_only():
    m = _reload_main({"OKX_FUT_ENABLED": "true"})
    fx = FakeOkxFut()
    core = m.TradingCoreShortOkx(exchange=fx)
    core.abrir_short("BTC/USDT:USDT", precio=63000.0, qty=0.01)
    core.cerrar_short_market("BTC/USDT:USDT", qty=0.01)
    assert fx.orders[-1]["params"].get("reduceOnly") is True
    assert fx.orders[-1]["side"] == "buy"
    assert fx.fetch_positions() == []


# ---------------------------------------------------------------------------
# Task 5: Señal de entrada
# ---------------------------------------------------------------------------
def _ind(close=62000, ema200=63000, adx=25, rsi=58, macdh=-0.5, dmp=15, dmn=25):
    # dmp/dmn = +DI/−DI (los produce df.ta.adx). Default bear-confirmado: −DI ≥ +DI.
    return {"close": close, "EMA_200": ema200, "ADX_14": adx,
            "RSI_14": rsi, "MACDh_12_26_9": macdh, "DMP_14": dmp, "DMN_14": dmn}


def test_senal_short_dispara_en_rally_bajista():
    m = _reload_main({"OKX_FUT_ENABLED": "true"})
    assert m.senal_short(_ind()) is True


def test_senal_short_no_dispara():
    m = _reload_main({"OKX_FUT_ENABLED": "true"})
    assert m.senal_short(_ind(adx=15)) is False          # sin tendencia (chop)
    assert m.senal_short(_ind(rsi=45)) is False          # sin rebote (RSI bajo)
    assert m.senal_short(_ind(macdh=0.5)) is False       # momentum aún alcista
    assert m.senal_short(None) is False


def test_senal_short_filtro_direccional_bloquea_toros():
    # Filtro direccional: NO shortear cuando +DI domina (toros al mando), aunque ADX/RSI/MACDh
    # pasen. Es el cuadrante que stopeó al ETH (rally fuerte ADX alto con +DI >> −DI).
    m = _reload_main({"OKX_FUT_ENABLED": "true"})
    assert m.senal_short(_ind(dmp=40, dmn=10)) is False   # +DI >> −DI → bloqueado
    assert m.senal_short(_ind(dmp=10, dmn=40)) is True    # −DI domina → el rebote se dio vuelta


def test_senal_short_rsi_min_55():
    # RSI más exigente: un rebote tibio (RSI 52) ya no dispara.
    m = _reload_main({"OKX_FUT_ENABLED": "true"})
    assert m.senal_short(_ind(rsi=52)) is False
    assert m.senal_short(_ind(rsi=58)) is True


def test_senal_short_dispara_con_precio_sobre_ema200():
    # El gate redundante close<EMA200(15m) se removió: el régimen diario ya filtra
    # la tendencia. Un rally por ENCIMA del EMA200 15m que se da vuelta SÍ dispara.
    m = _reload_main({"OKX_FUT_ENABLED": "true"})
    assert m.senal_short(_ind(close=64000, ema200=63000, adx=25, rsi=58, macdh=-0.5)) is True


# ---------------------------------------------------------------------------
# Task 6: Sizing
# ---------------------------------------------------------------------------
def test_size_respeta_equity_pct_y_leverage():
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_MAX_EQUITY_PCT": "0.18",
                      "SHORT_LEVERAGE": "3", "SHORT_CANARY_MODE": "false"})
    qty = m.calcular_size_short(equity=4000.0, precio=60000.0, ml_mult=1.0)
    assert abs(qty - (4000 * 0.18 * 3) / 60000.0) < 1e-6


def test_size_canary_usa_minimo():
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_CANARY_MODE": "true"})
    qty = m.calcular_size_short(equity=4000.0, precio=60000.0, ml_mult=1.0)
    assert qty == m.SHORT_QTY_MINIMA


# ---------------------------------------------------------------------------
# Task 7: EscuadronShortOkx — flujo de entrada
# ---------------------------------------------------------------------------
def _montar_escuadron(m, fx, mm="BAJISTA", ind=None, precio=62000.0, tesoreria=None):
    core = m.TradingCoreShortOkx(exchange=fx)
    db = _db_memoria(m)
    esc = m.EscuadronShortOkx(core_short=core, db=db, tesoreria=tesoreria)
    with m.regime_lock:
        m.modo_mercado = mm
    m.regime_evaluado = True   # simula que RadarRegimen ya evaluó (no estamos en boot)
    # Datos cacheados bajo símbolo SPOT (como en producción: MotorIndicadores/WebSocketFeedOkx).
    m.PRECIOS_RAM["okx"] = {"BTC/USDT": {"bid": precio, "ask": precio},
                            "ETH/USDT": {"bid": precio, "ask": precio}}
    m._test_ind = ind if ind is not None else _ind(close=62000, ema200=63000)
    m.MotorIndicadores.get_indicators = staticmethod(lambda v, s: m._test_ind)
    m.is_price_fresh = lambda v, s: True
    m.is_panico_global = lambda: False
    m.is_vol_spike = lambda v, s: False
    return esc, fx, db


def test_entrada_dispara_en_bajista_con_senal():
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_CANARY_MODE": "true"})
    fx = FakeOkxFut(precio=62000.0, usdt_free=4000.0)
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA")
    esc._gestionar("BTC/USDT:USDT")
    assert len(db.short_get_posiciones()) == 1
    assert fx.orders[-1]["params"]["posSide"] == "short"


def test_no_entrada_en_alcista():
    m = _reload_main({"OKX_FUT_ENABLED": "true"})
    fx = FakeOkxFut()
    esc, fx, db = _montar_escuadron(m, fx, mm="ALCISTA")
    esc._gestionar("BTC/USDT:USDT")
    assert db.short_get_posiciones() == []


def test_no_doble_posicion():
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_CANARY_MODE": "true"})
    fx = FakeOkxFut(precio=62000.0)
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA")
    esc._gestionar("BTC/USDT:USDT")
    esc._gestionar("BTC/USDT:USDT")   # segunda vez, ya hay short
    assert len(db.short_get_posiciones()) == 1


# ---------------------------------------------------------------------------
# Subsistema A: fondear el short al 18% real (tesorería → margen)
# ---------------------------------------------------------------------------
def test_asegurar_margen_redime_deficit():
    # free chico + earn suficiente → redime exactamente el déficit (target - free).
    m = _reload_main({"OKX_FUT_ENABLED": "true"})
    fx = FakeOkxFut(precio=62000.0, usdt_free=50.0)
    tes = FakeTesoreria(fx, earn=2000.0)
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA", tesoreria=tes)
    esc._asegurar_margen(200.0)
    assert len(tes.rescates) == 1
    assert abs(tes.rescates[0] - 150.0) < 1e-6          # déficit = 200 - 50
    assert fx.usdt_free >= 200.0                          # free quedó fondeado


def test_asegurar_margen_no_redime_si_free_alcanza():
    # free ya cubre el target → no toca earn.
    m = _reload_main({"OKX_FUT_ENABLED": "true"})
    fx = FakeOkxFut(precio=62000.0, usdt_free=300.0)
    tes = FakeTesoreria(fx, earn=2000.0)
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA", tesoreria=tes)
    esc._asegurar_margen(200.0)
    assert tes.rescates == []
    assert fx.usdt_free == 300.0


def test_asegurar_margen_sin_tesoreria_es_noop():
    # tesoreria=None (compat hacia atrás): degrada al comportamiento previo, no rompe.
    m = _reload_main({"OKX_FUT_ENABLED": "true"})
    fx = FakeOkxFut(precio=62000.0, usdt_free=50.0)
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA", tesoreria=None)
    esc._asegurar_margen(200.0)                          # no debe lanzar
    assert fx.usdt_free == 50.0


def test_asegurar_margen_ignora_target_sub_minimo():
    # target por debajo del margen mínimo → no vale redimir.
    m = _reload_main({"OKX_FUT_ENABLED": "true"})
    fx = FakeOkxFut(precio=62000.0, usdt_free=0.0)
    tes = FakeTesoreria(fx, earn=2000.0)
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA", tesoreria=tes)
    esc._asegurar_margen(m.SHORT_MIN_MARGEN_USD - 1.0)
    assert tes.rescates == []


def test_gestionar_fondea_antes_de_sizear():
    # Integración: free son migajas pero el earn da para el 18% → la entrada se dimensiona
    # con el margen fondeado, no con las migajas. Es el fix de la sub-deployment.
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_CANARY_MODE": "false",
                      "SHORT_MAX_EQUITY_PCT": "0.18", "SHORT_LEVERAGE": "3"})
    fx = FakeOkxFut(precio=62000.0, usdt_free=20.0)      # casi sin free posteable
    tes = FakeTesoreria(fx, earn=2000.0)
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA", tesoreria=tes)
    m.earn_balance_usdt_okx = 1000.0                     # equity = free + earn ≈ 1020
    esc._gestionar("BTC/USDT:USDT")
    assert len(db.short_get_posiciones()) == 1
    pos = db.short_get_posiciones()[0]
    margen_real = pos["entry_px"] * pos["qty"] / m.CONFIG["short_leverage"]
    # sin fondeo el margen sería ~20*0.95≈19; con fondeo apunta al 18% de ~1020 ≈ 183.
    assert margen_real > 100.0, f"margen {margen_real} → no se fondeó"
    assert len(tes.rescates) == 1


def test_gestionar_no_fondea_si_senal_no_dispara():
    # No sacar earn para nada: si la señal no dispara, no se redime.
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_CANARY_MODE": "false"})
    fx = FakeOkxFut(precio=62000.0, usdt_free=20.0)
    tes = FakeTesoreria(fx, earn=2000.0)
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA",
                                    ind=_ind(rsi=45), tesoreria=tes)   # señal apagada
    m.earn_balance_usdt_okx = 1000.0
    esc._gestionar("BTC/USDT:USDT")
    assert db.short_get_posiciones() == []
    assert tes.rescates == []


# ---------------------------------------------------------------------------
# Task 8: Salidas — flip de régimen + TP tiers
# ---------------------------------------------------------------------------
def test_flip_a_alcista_cierra_shorts():
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_CANARY_MODE": "true"})
    fx = FakeOkxFut(precio=62000.0)
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA")
    esc._gestionar("BTC/USDT:USDT")
    assert len(db.short_get_posiciones()) == 1
    with m.regime_lock:
        m.modo_mercado = "ALCISTA"
    esc._gestionar_salidas()
    assert db.short_get_posiciones() == []
    assert fx.orders[-1]["params"].get("reduceOnly") is True


def test_tp_backstop_cierra_al_alcanzar_pct():
    # El TP primario es server-side (OCO); este es el backstop in-process. Cierra al -TP%.
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_CANARY_MODE": "true", "SHORT_TP_PCT": "0.03"})
    fx = FakeOkxFut(precio=62000.0)
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA", precio=62000.0)
    esc._gestionar("BTC/USDT:USDT")
    assert len(db.short_get_posiciones()) == 1
    m.PRECIOS_RAM["okx"]["BTC/USDT"] = {"bid": 62000.0 * 0.97 - 1}   # cae 3% → gain >= tp_pct
    esc._gestionar_salidas()
    assert db.short_get_posiciones() == []


def test_reconcile_limpia_short_cerrado_off_bot():
    # Medida de seguridad: si OKX cerró el short (TP/SL server-side) con el bot apagado,
    # el reconcile al arranque limpia la DB.
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_CANARY_MODE": "true"})
    fx = FakeOkxFut(precio=62000.0)
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA")
    esc._gestionar("BTC/USDT:USDT")
    assert len(db.short_get_posiciones()) == 1
    fx.positions.clear()             # OKX cerró la posición off-bot
    esc.reconciliar_shorts()
    assert db.short_get_posiciones() == []


def test_reconcile_no_toca_short_vivo():
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_CANARY_MODE": "true"})
    fx = FakeOkxFut(precio=62000.0)
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA")
    esc._gestionar("BTC/USDT:USDT")
    esc.reconciliar_shorts()         # OKX sigue teniendo la posición
    assert len(db.short_get_posiciones()) == 1


def test_reconcile_runtime_detecta_cierre_serverside():
    # BUG FIX: el OCO puede dispararse con el bot CORRIENDO (no solo apagado). Antes el cierre
    # server-side en runtime dejaba una fila FANTASMA en la DB hasta el próximo reinicio
    # (PnL sin registrar + slot ocupado). Cada tick del loop ahora reconcilia.
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_CANARY_MODE": "true"})
    fx = FakeOkxFut(precio=62000.0)
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA")
    esc._gestionar("BTC/USDT:USDT")
    assert len(db.short_get_posiciones()) == 1
    db._exec("UPDATE short_positions SET open_ts=?", (time.time() - 9999,))  # supera el grace
    m._test_ind = _ind(rsi=45)        # señal apagada → el tick no reabre, aísla el reconcile
    fx.positions.clear()              # OKX cerró la posición (OCO disparado) con el bot vivo
    esc._tick()                       # un tick del loop runtime
    assert db.short_get_posiciones() == []                                  # fila limpiada
    assert db._exec("SELECT COUNT(*) FROM short_realized_pnl",
                    fetch="one")[0] >= 1                                     # PnL booked


def test_reconcile_runtime_respeta_grace_recien_abierto():
    # Protección de carrera: un short recién abierto NO se reconcilia aunque fetch_positions
    # transitoriamente no lo liste (lag de la API OKX). El grace lo cubre; el boot NO aplica grace.
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_CANARY_MODE": "true"})
    fx = FakeOkxFut(precio=62000.0)
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA")
    esc._gestionar("BTC/USDT:USDT")
    fx.positions.clear()              # OKX aún no refleja la posición (lag)
    esc.reconciliar_shorts("runtime")
    assert len(db.short_get_posiciones()) == 1   # grace lo protege, no lo borra


def test_loss_cooldown_se_setea_solo_en_perdida():
    # Anti-revenge-trade: un cierre EN PÉRDIDA arma el cooldown; uno ganador NO.
    m = _reload_main({"OKX_FUT_ENABLED": "true"})
    db = _db_memoria(m)
    db.short_cerrar_posicion("ETH/USDT:USDT", 1700.0, -5.0)   # pérdida
    assert db.short_loss_cooldown("ETH/USDT:USDT") > 0
    db.short_cerrar_posicion("BTC/USDT:USDT", 62000.0, +5.0)  # ganancia
    assert db.short_loss_cooldown("BTC/USDT:USDT") == 0


def test_gestionar_respeta_loss_cooldown():
    # Tras stopear en pérdida, NO re-shortea el mismo símbolo aunque la señal dispare en BAJISTA.
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_CANARY_MODE": "true"})
    fx = FakeOkxFut(precio=62000.0)
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA")
    db.short_cerrar_posicion("BTC/USDT:USDT", 62000.0, -5.0)  # acaba de stopear en rojo
    esc._gestionar("BTC/USDT:USDT")
    assert db.short_get_posiciones() == []                    # loss cooldown lo frena


def test_salidas_no_actua_en_boot_sin_regimen_evaluado():
    # En el arranque (regime_evaluado=False) el régimen default es ALCISTA; las salidas
    # NO deben cerrar el short por "flip" antes de que RadarRegimen reevalúe.
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_CANARY_MODE": "true"})
    fx = FakeOkxFut(precio=62000.0)
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA")
    esc._gestionar("BTC/USDT:USDT")
    assert len(db.short_get_posiciones()) == 1
    m.regime_evaluado = False          # simula boot
    m.modo_mercado = "ALCISTA"          # default de boot
    esc._gestionar_salidas()
    assert len(db.short_get_posiciones()) == 1   # NO cerró (régimen aún no evaluado)


# ---------------------------------------------------------------------------
# Task 9: Circuit breaker + KILL
# ---------------------------------------------------------------------------
def test_circuit_breaker_corta_tras_cap():
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_CANARY_MODE": "true",
                      "SHORT_DAILY_LOSS_CAP_USD": "60"})
    fx = FakeOkxFut(precio=62000.0)
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA")
    db.short_cerrar_posicion("ETH/USDT:USDT", exit_px=0, pnl=-70.0)
    esc._gestionar("BTC/USDT:USDT")
    assert db.short_get_posiciones() == []   # breaker activo, no abre


def test_kill_cierra_todo():
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_CANARY_MODE": "true"})
    fx = FakeOkxFut(precio=62000.0)
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA")
    esc._gestionar("BTC/USDT:USDT")
    esc.cerrar_todo_kill()
    assert db.short_get_posiciones() == []
    assert fx.orders[-1]["params"].get("reduceOnly") is True


# ---------------------------------------------------------------------------
# Task 10: Integración
# ---------------------------------------------------------------------------
def test_import_y_clases_presentes():
    m = _reload_main({"OKX_FUT_ENABLED": "true"})
    assert hasattr(m, "EscuadronShortOkx")
    assert hasattr(m, "TradingCoreShortOkx")
    assert "BTC/USDT:USDT" in m.PARES_SHORT_OKX


def test_hud_incluye_pata_short_en_equity():
    # El HUD sub-reportaba equity: el margen del short (que salió del free) + uPnL no se sumaban.
    m = _reload_main({"OKX_FUT_ENABLED": "true"})
    db = _db_memoria(m)
    db.short_abrir_posicion("BTC/USDT:USDT", entry_px=60000.0, qty=0.003, lever=3, sl_order_id="x")
    m.PRECIOS_RAM["okx"] = {"BTC/USDT": {"bid": 59000.0}}   # bajó → el short gana
    hud = m.RadarTelemetria(core_okx=None, db=db)
    margen, upnl = hud._pata_short_margen_upnl()
    assert abs(margen - (0.003 * 60000.0 / 3)) < 1e-6        # 60.0 (margen que salió del free)
    assert abs(upnl - (60000.0 - 59000.0) * 0.003) < 1e-6     # 3.0 (short gana al bajar)


def test_hud_pata_short_cero_si_fut_off():
    m = _reload_main({"OKX_FUT_ENABLED": "false"})
    db = _db_memoria(m)
    hud = m.RadarTelemetria(core_okx=None, db=db)
    assert hud._pata_short_margen_upnl() == (0.0, 0.0)


# ---------------------------------------------------------------------------
# Regresión de la revisión exhaustiva (bugs encontrados post-implementación)
# ---------------------------------------------------------------------------
def test_spot_de_perp():
    m = _reload_main({"OKX_FUT_ENABLED": "true"})
    assert m._spot_de_perp("BTC/USDT:USDT") == "BTC/USDT"
    assert m._spot_de_perp("ETH/USDT:USDT") == "ETH/USDT"


def test_entrada_usa_precio_spot_no_perp():
    # Bug #1 (crítico): los datos se cachean bajo símbolo SPOT. Si el precio solo
    # existe bajo la clave perp, la pata NO debe abrir (antes abría en los tests por mock).
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_CANARY_MODE": "true"})
    fx = FakeOkxFut(precio=62000.0)
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA")
    m.PRECIOS_RAM["okx"] = {"BTC/USDT:USDT": {"bid": 62000, "ask": 62000}}  # SOLO perp
    esc._gestionar("BTC/USDT:USDT")
    assert db.short_get_posiciones() == []   # sin precio spot → no abre


def test_size_topea_por_disponible():
    # Bug #2 (alto): el margen objetivo se topea por el USDT realmente disponible.
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_MAX_EQUITY_PCT": "0.18",
                      "SHORT_LEVERAGE": "3", "SHORT_CANARY_MODE": "false"})
    qty = m.calcular_size_short(equity=100000.0, precio=60000.0, ml_mult=1.0, disponible=300.0)
    # topeado por disponible (con buffer 0.95 para fees), no por equity
    assert abs(qty - (300.0 * m.SHORT_MARGEN_BUFFER * 3) / 60000.0) < 1e-6
    assert m.calcular_size_short(equity=100000.0, precio=60000.0, disponible=2.0) == 0.0  # < mín


def test_size_ml_mult_no_excede_disponible():
    # Regresión sCode 51008 (2026-06-14): con ml_mult>1 y el free como restricción, el IM
    # requerido (= notional/lever) NUNCA debe superar el disponible, o OKX rechaza la orden.
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_MAX_EQUITY_PCT": "0.18",
                      "SHORT_LEVERAGE": "3", "SHORT_CANARY_MODE": "false"})
    disponible = 191.0
    precio = 64500.0
    qty = m.calcular_size_short(equity=4262.0, precio=precio, ml_mult=1.13, disponible=disponible)
    im_requerido = (qty * precio) / m.CONFIG["short_leverage"]
    assert im_requerido <= disponible, f"IM {im_requerido} > free {disponible} → 51008"


def test_abrir_short_es_market():
    # Bug #3 (alto): entrada a market (fill inmediato) para que el registro de posición sea real.
    m = _reload_main({"OKX_FUT_ENABLED": "true"})
    fx = FakeOkxFut(precio=63000.0)
    core = m.TradingCoreShortOkx(exchange=fx)
    core.abrir_short("BTC/USDT:USDT", precio=63000.0, qty=0.01)
    assert fx.orders[0]["type"] == "market"   # la ENTRADA es market
    assert fx.orders[0]["side"] == "sell"


def test_abrir_short_convierte_moneda_a_contratos():
    # Bug #4 (alto): OKX swap ordena en CONTRATOS. ctVal BTC=0.01, ETH=0.1.
    m = _reload_main({"OKX_FUT_ENABLED": "true"})
    fx = FakeOkxFut(precio=63000.0)
    core = m.TradingCoreShortOkx(exchange=fx)
    res = core.abrir_short("BTC/USDT:USDT", precio=63000.0, qty=0.02)   # 0.02 BTC
    assert fx.orders[-1]["amount"] == 2.0          # 0.02 / 0.01 = 2 contratos
    assert abs(res["qty"] - 0.02) < 1e-9           # moneda real ejecutada
    core.abrir_short("ETH/USDT:USDT", precio=2000.0, qty=0.5)           # 0.5 ETH
    assert fx.orders[-1]["amount"] == 5.0          # 0.5 / 0.1 = 5 contratos


# ---------------------------------------------------------------------------
# Task 1: Config + validación
# ---------------------------------------------------------------------------
def test_config_short_defaults():
    m = _reload_main({
        "OKX_FUT_ENABLED": "true", "SHORT_MAX_EQUITY_PCT": "0.18",
        "SHORT_LEVERAGE": "3", "SHORT_SL_PCT": "0.04",
        "SHORT_MAX_POSICIONES": "2", "SHORT_DAILY_LOSS_CAP_USD": "60",
        "SHORT_CANARY_MODE": "true",
    })
    assert m.CONFIG["fut_enabled"] is True
    assert m.CONFIG["short_max_equity_pct"] == 0.18
    assert m.CONFIG["short_leverage"] == 3
    assert m.CONFIG["short_sl_pct"] == 0.04
    assert m.CONFIG["short_max_posiciones"] == 2
    assert m.CONFIG["short_daily_loss_cap_usd"] == 60.0
    assert m.CONFIG["short_canary_mode"] is True


def test_validar_config_rechaza_leverage_alto():
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_LEVERAGE": "50"})
    with pytest.raises(SystemExit):
        m.validar_config()


# ---------------------------------------------------------------------------
# Task 2: Schema short_* + métodos DB
# ---------------------------------------------------------------------------
def test_short_positions_crud():
    m = _reload_main({"OKX_FUT_ENABLED": "true"})
    db = _db_memoria(m)
    db.short_abrir_posicion("BTC-USDT-SWAP", entry_px=63000.0, qty=0.01,
                            lever=3, sl_order_id="algo123")
    abiertas = db.short_get_posiciones()
    assert len(abiertas) == 1
    assert abiertas[0]["symbol"] == "BTC-USDT-SWAP"
    assert abiertas[0]["sl_order_id"] == "algo123"
    db.short_cerrar_posicion("BTC-USDT-SWAP", exit_px=61000.0, pnl=20.0)
    assert db.short_get_posiciones() == []
    assert db.short_pnl_hoy() == 20.0


# ===========================================================================
# Fase 2 (V1.7.2): controlador de graduación + gestión dinámica de salidas
# ===========================================================================
def _res(pol, n, avg):
    """Fila de short_shadow_resumen() sintética."""
    return {"politica": pol, "n": n, "pnl_total": avg * n, "pnl_avg": avg, "wins": 0}


# --- compuerta pura seleccionar_politica_live ---
def test_gate_shadow_mode_devuelve_none():
    m = _reload_main({})
    assert m.seleccionar_politica_live([_res("oco_fijo", 50, 1.0)], "shadow", 30, 0.2) is None


def test_gate_pin_devuelve_esa_politica():
    m = _reload_main({})
    assert m.seleccionar_politica_live([], "momentum", 30, 0.2) == "momentum"


def test_gate_live_sin_datos_cae_a_oco_fijo():
    m = _reload_main({})
    assert m.seleccionar_politica_live([], "live", 30, 0.2) == "oco_fijo"


def test_gate_live_gradua_la_que_supera_baseline():
    m = _reload_main({})
    res = [_res("oco_fijo", 50, 1.0), _res("chandelier", 50, 1.5)]
    assert m.seleccionar_politica_live(res, "live", 30, 0.2) == "chandelier"


def test_gate_live_ignora_muestra_insuficiente():
    m = _reload_main({})
    res = [_res("oco_fijo", 50, 1.0), _res("chandelier", 5, 3.0)]   # gran avg, pocas muestras
    assert m.seleccionar_politica_live(res, "live", 30, 0.2) == "oco_fijo"


def test_gate_live_exige_margen_sobre_baseline():
    m = _reload_main({})
    res = [_res("oco_fijo", 50, 1.0), _res("momentum", 50, 1.1)]    # solo +0.1 < 0.2 margen
    assert m.seleccionar_politica_live(res, "live", 30, 0.2) == "oco_fijo"


def test_gate_live_elige_la_mejor_entre_varias():
    m = _reload_main({})
    res = [_res("oco_fijo", 50, 1.0), _res("chandelier", 50, 1.5), _res("momentum", 50, 2.0)]
    assert m.seleccionar_politica_live(res, "live", 30, 0.2) == "momentum"


def test_validar_config_rechaza_gestor_mode_invalido():
    m = _reload_main({"SHORT_GESTOR_MODE": "no-existe"})
    with pytest.raises(SystemExit):
        m.validar_config()


# --- gestión dinámica sobre posición real ---
def _seed_short(m, fx, db, entry=62000.0, qty=0.01):
    db.short_abrir_posicion("BTC/USDT:USDT", entry, qty, 3, "algo1")
    fx.positions["BTC/USDT:USDT"] = dict(symbol="BTC/USDT:USDT", contracts=qty,
                                         side="short", entryPrice=entry)


def test_gestor_shadow_no_cierra_dinamicamente():
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_GESTOR_MODE": "shadow"})
    fx = FakeOkxFut(precio=62000.0)
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA", ind=_ind(macdh=0.5))
    _seed_short(m, fx, db)
    esc._gestionar_salidas()
    assert len(db.short_get_posiciones()) == 1     # shadow = sin gestión dinámica


def test_gestor_live_momentum_cierra_el_short():
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_GESTOR_MODE": "momentum"})
    fx = FakeOkxFut(precio=62000.0)
    # macdh>0 → la política momentum dispara "el rebote se reanuda"
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA", ind=_ind(macdh=0.5))
    _seed_short(m, fx, db)
    esc._gestionar_salidas()
    assert db.short_get_posiciones() == []
    assert fx.fetch_positions() == []


def test_gestor_live_chandelier_cabalga_y_sale_en_retroceso():
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_GESTOR_MODE": "chandelier",
                      "SHORT_SHADOW_CHANDELIER_K": "2.5"})
    fx = FakeOkxFut(precio=60000.0)
    ind = _ind(); ind["ATRr_14"] = 100.0   # trail = mejor_px + 2.5·100 = +250
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA", ind=ind, precio=60000.0)
    _seed_short(m, fx, db, entry=60000.0)
    # tick 1: cae a 59000 (gain 1.67% < TP) → mejor_px=59000, NO sale (trail 59250 > 59000)
    m.PRECIOS_RAM["okx"]["BTC/USDT"] = {"bid": 59000.0, "ask": 59000.0}
    esc._gestionar_salidas()
    assert len(db.short_get_posiciones()) == 1
    # tick 2: rebota a 59300 (> 59000+250) → chandelier cierra
    m.PRECIOS_RAM["okx"]["BTC/USDT"] = {"bid": 59300.0, "ask": 59300.0}
    esc._gestionar_salidas()
    assert db.short_get_posiciones() == []


# ===========================================================================
# Fase 3 (V1.7.3): meta-learner online — selección por contexto sobre real
# ===========================================================================
def test_gestor_meta_elige_politica_segun_contexto_y_cierra():
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_GESTOR_MODE": "meta",
                      "SHORT_GESTOR_MIN_TRADES": "2", "SHORT_GESTOR_MARGEN_PCT": "0.0",
                      "SHORT_META_HALFLIFE_S": "1e18", "SHORT_FUNDING_CONTEXT": "false"})
    fx = FakeOkxFut(precio=60000.0)
    ind = _ind(macdh=0.5); ind["ATRr_14"] = 600.0; ind["close"] = 60000.0   # r=0.01 → 'normal'
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA", ind=ind, precio=60000.0)
    # shadow: en contexto 'normal', momentum domina con muestra suficiente (trades APAREADOS:
    # entry_ts distinto por trade virtual → el gate apareado empareja oco_fijo vs momentum)
    for i in range(4):
        db.short_shadow_registrar("oco_fijo", "BTC/USDT:USDT", 60000, 60000, 0.0, "x", i, 1.0, contexto="normal")
        db.short_shadow_registrar("momentum", "BTC/USDT:USDT", 60000, 59000, 1.5, "y", i, 1.0, contexto="normal")
    _seed_short(m, fx, db, entry=60000.0)
    esc._gestionar_salidas()
    assert db.short_get_posiciones() == []     # meta eligió momentum (contexto normal) → cerró


def test_gestor_meta_sin_datos_no_cierra():
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_GESTOR_MODE": "meta",
                      "SHORT_GESTOR_MIN_TRADES": "30"})
    fx = FakeOkxFut(precio=60000.0)
    ind = _ind(macdh=0.5); ind["ATRr_14"] = 600.0
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA", ind=ind, precio=60000.0)
    _seed_short(m, fx, db, entry=60000.0)
    esc._gestionar_salidas()
    assert len(db.short_get_posiciones()) == 1   # sin datos → meta cae a oco_fijo → sin cierre dinámico
