"""SP3 — sleeve live EscuadronLadderOkx (grid ladder round-trip spot, canary). Tests contra
FakeOkxSpot, sin plata real. Cubre envelope, schema, régimen propio, planner puro, ejecución,
reconcile, loop. Patrón de test_short_okx.py."""
import threading
import main


# --- FakeOkxSpot: stand-in de core_okx (exchange ccxt + lock), sin red ni plata ---
class _FakeExSpot:
    def __init__(self):
        self.orders = {}
        self._cancelados = {}
        self._n = 0

    def create_limit_buy_order(self, sym, qty, price, params=None):
        self._n += 1
        oid = f"b{self._n}"
        self.orders[oid] = {"id": oid, "symbol": sym, "side": "buy", "amount": qty,
                            "price": price, "status": "open", "filled": 0.0}
        return self.orders[oid]

    def create_limit_sell_order(self, sym, qty, price, params=None):
        self._n += 1
        oid = f"s{self._n}"
        self.orders[oid] = {"id": oid, "symbol": sym, "side": "sell", "amount": qty,
                            "price": price, "status": "open", "filled": 0.0}
        return self.orders[oid]

    def create_market_sell_order(self, sym, qty, params=None):
        self._n += 1
        oid = f"m{self._n}"
        self.orders[oid] = {"id": oid, "symbol": sym, "side": "sell", "amount": qty,
                            "status": "closed", "filled": qty, "average": 0.0}
        return self.orders[oid]

    def cancel_order(self, oid, sym=None):
        o = self.orders.pop(oid, None)
        self._cancelados[oid] = {"id": oid, "status": "canceled",
                                 "filled": float((o or {}).get("filled") or 0.0)}

    def fetch_order(self, oid, sym=None):
        # Una orden cancelada NO se lee como llena: devolvía {"status":"closed","filled":0}
        # para todo id desconocido, así que el fake no sabía expresar "cancelada" y el
        # agujero de estados terminales era intesteable (y cualquier cancel+reconcile daba
        # un fill fantasma). Directiva 20 aplicada al doble de pruebas.
        if oid in self.orders:
            return self.orders[oid]
        if oid in self._cancelados:
            return self._cancelados[oid]
        return {"id": oid, "status": "closed", "filled": 0.0}

    def _cancelar(self, oid, filled=0.0):   # helper de test: OKX canceló/expiró/rechazó
        o = self.orders.pop(oid, None)
        self._cancelados[oid] = {"id": oid, "status": "canceled", "filled": float(filled),
                                 "average": (o or {}).get("price")}

    def _fill(self, oid, average=None):   # helper de test: simula un fill
        o = self.orders.get(oid)
        if o:
            o["status"] = "closed"
            o["filled"] = o["amount"]
            if average is not None:
                o["average"] = average


class _FakeCore:
    def __init__(self):
        self.exchange_trade = _FakeExSpot()
        self.api_trade_lock = threading.RLock()
        self._free = {}                 # ccy -> free spot balance (para el cap del sell, Fix A)

    def get_balance(self, force_refresh=False):
        return {ccy: {"free": v} for ccy, v in self._free.items()}

    def invalidate_balance(self):
        pass


# =============================================================================
# Task 1 — envelope de config (_validar_ladder, lista de errores; [] si off)
# =============================================================================
def test_validar_ladder_aborta_capital_excesivo(monkeypatch):
    monkeypatch.setitem(main.CONFIG, "ladder_enabled", True)
    monkeypatch.setitem(main.CONFIG, "ladder_max_capital", 600.0)
    monkeypatch.setattr(main, "PARES_LADDER", ["LTC/USDT"])
    assert any("LADDER_MAX_CAPITAL" in e for e in main._validar_ladder())


def test_validar_ladder_ok_envelope_chico(monkeypatch):
    monkeypatch.setitem(main.CONFIG, "ladder_enabled", True)
    monkeypatch.setitem(main.CONFIG, "ladder_max_capital", 100.0)
    monkeypatch.setattr(main, "PARES_LADDER", ["LTC/USDT"])
    assert main._validar_ladder() == []


def test_validar_ladder_off_no_chequea(monkeypatch):
    monkeypatch.setitem(main.CONFIG, "ladder_enabled", False)
    monkeypatch.setitem(main.CONFIG, "ladder_max_capital", 9999.0)
    monkeypatch.setattr(main, "PARES_LADDER", [])
    assert main._validar_ladder() == []


def test_validar_ladder_envelope_es_el_TOTAL_no_el_de_un_par(monkeypatch):
    """El cap se aplica POR SÍMBOLO (_comprometido(sym) en _tick), así que N pares con cap C
    despliegan N·C. El guard validaba C contra 500 ⟹ con 6 pares a 100 dejaba pasar $600 reales
    creyendo que eran 100. RED de este fix (hallado el 2026-08-02 al pasar el canary a 2 pares)."""
    monkeypatch.setitem(main.CONFIG, "ladder_enabled", True)
    monkeypatch.setitem(main.CONFIG, "ladder_max_capital", 100.0)
    monkeypatch.setattr(main, "PARES_LADDER", ["LTC/USDT", "XRP/USDT", "ADA/USDT",
                                               "SOL/USDT", "DOGE/USDT", "TRX/USDT"])
    errs = main._validar_ladder()
    assert any("LADDER_MAX_CAPITAL" in e for e in errs), "600 USDT totales deben abortar"
    assert any("600" in e for e in errs), "el error debe nombrar la exposición TOTAL, no el cap por par"


def test_validar_ladder_dos_pares_dentro_del_envelope_pasa(monkeypatch):
    # discrimina: el fix no debe volverse restrictivo de más — 2×$50 = $100 total, holgado
    monkeypatch.setitem(main.CONFIG, "ladder_enabled", True)
    monkeypatch.setitem(main.CONFIG, "ladder_max_capital", 50.0)
    monkeypatch.setattr(main, "PARES_LADDER", ["XRP/USDT", "LTC/USDT"])
    assert main._validar_ladder() == []


def test_validar_ladder_pares_vacio(monkeypatch):
    monkeypatch.setitem(main.CONFIG, "ladder_enabled", True)
    monkeypatch.setitem(main.CONFIG, "ladder_max_capital", 100.0)
    monkeypatch.setattr(main, "PARES_LADDER", [])
    assert any("LADDER_PARES" in e for e in main._validar_ladder())


# =============================================================================
# Task 2 — schema DB ladder_* + métodos
# =============================================================================
def test_db_crea_tablas_ladder(tmp_path):
    db = main.DatabaseManager(str(tmp_path / "t.db"))
    rows = db._exec("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ladder_%'",
                    fetch="all") or []
    nombres = {r[0] for r in rows}
    assert {"ladder_orders", "ladder_positions", "ladder_realized", "ladder_fill_log"} <= nombres


def test_ladder_fill_resumen(tmp_path):
    db = main.DatabaseManager(str(tmp_path / "t.db"))
    db.ladder_registrar_orden("o1", "LTC/USDT", "buy", 0, 100.0, 0.1)
    db.ladder_registrar_orden("o2", "LTC/USDT", "buy", 1, 99.0, 0.1)
    db.ladder_marcar_tocado("o1")
    db.ladder_marcar_fill("o1")
    r = db.ladder_fill_resumen()
    assert r["colocados"] == 2 and r["tocados"] == 1 and r["llenos"] == 1
    assert abs(r["fill_rate"] - 1.0) < 1e-9          # 1 lleno / 1 tocado


def test_ladder_posicion_roundtrip(tmp_path):
    db = main.DatabaseManager(str(tmp_path / "t.db"))
    db.ladder_set_posicion("LTC/USDT", 0, 0.1, 100.0, "sell1")
    pos = db.ladder_get_posiciones()
    assert len(pos) == 1 and pos[0]["rung_idx"] == 0 and pos[0]["sell_order_id"] == "sell1"
    db.ladder_registrar_realizado("LTC/USDT", 0.1, 100.0, 101.5, 0.13, "round-trip")
    db.ladder_del_posicion("LTC/USDT", 0)
    assert db.ladder_get_posiciones() == []
    assert db.ladder_pnl_hoy() > 0


# =============================================================================
# Task 3 — regimen_propio (gate por el régimen del PROPIO activo)
# =============================================================================
def test_regimen_propio_downtrend():
    # serie diaria en declive sostenido y baja vol → TENDENCIA_DOWN (el ladder se congela)
    velas = [[i * 86400000, 200 - i * 0.3, 200 - i * 0.3 + 0.3, 200 - i * 0.3 - 0.3,
              200 - i * 0.3, 100] for i in range(280)]
    assert main.regimen_propio(velas) == "TENDENCIA_DOWN"


def test_regimen_propio_datos_insuficientes_caos():
    # fail-safe: sin datos suficientes → CAOS (no opera)
    assert main.regimen_propio([[0, 100, 100, 100, 100, 1]] * 10) == "CAOS"


# =============================================================================
# Task 4 — planner puro ladder_acciones
# =============================================================================
def _cfg():
    return {"rungs": 4, "spacing_pct": 1.5, "stop_pct": 15.0, "monto_rung": 20.0}


def test_planner_arma_escalera_en_rango_sin_posicion():
    estado = {"rungs": [{"state": "armed", "buy_px": 0} for _ in range(4)], "ref": None}
    acc = main.ladder_acciones(estado, 100.0, "RANGO", _cfg())
    buys = [a for a in acc if a["tipo"] == "buy"]
    assert len(buys) == 4 and all(a["price"] < 100.0 for a in buys)


def test_planner_no_arma_si_no_rango():
    estado = {"rungs": [{"state": "armed", "buy_px": 0} for _ in range(4)], "ref": None}
    acc = main.ladder_acciones(estado, 100.0, "TENDENCIA_DOWN", _cfg())
    assert all(a["tipo"] != "buy" for a in acc)


def test_planner_flatten_al_salir_de_rango():
    estado = {"rungs": [{"state": "holding", "buy_px": 98, "qty": 0.2, "rung_idx": 0}], "ref": 100.0}
    acc = main.ladder_acciones(estado, 95.0, "TENDENCIA_DOWN", _cfg())
    assert any(a["tipo"] == "flatten" for a in acc)


def test_planner_stop_rung_underwater():
    estado = {"rungs": [{"state": "holding", "buy_px": 100, "qty": 0.2, "rung_idx": 0}], "ref": 100.0}
    acc = main.ladder_acciones(estado, 80.0, "RANGO", _cfg())   # -20% > stop 15%
    assert any(a["tipo"] == "stop" for a in acc)


def test_planner_no_stop_si_no_underwater():
    estado = {"rungs": [{"state": "holding", "buy_px": 100, "qty": 0.2, "rung_idx": 0}], "ref": 100.0}
    acc = main.ladder_acciones(estado, 99.0, "RANGO", _cfg())   # -1% < stop 15%
    assert all(a["tipo"] != "stop" for a in acc)


# =============================================================================
# Task 5 — _ejecutar_accion (dispatch a maker buy/sell/market, persiste + fill-log)
# =============================================================================
def _esc(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "PARES_LADDER", ["LTC/USDT"])
    db = main.DatabaseManager(str(tmp_path / "t.db"))
    core = _FakeCore()
    return main.EscuadronLadderOkx(core, db), core, db


def test_ejecutar_buy_coloca_y_registra(tmp_path, monkeypatch):
    esc, core, db = _esc(tmp_path, monkeypatch)
    esc._ejecutar_accion("LTC/USDT", {"tipo": "buy", "rung_idx": 0, "price": 100.0, "qty": 0.1})
    assert len(core.exchange_trade.orders) == 1
    rows = db._exec("SELECT order_id, side FROM ladder_orders", fetch="all")
    assert rows and rows[0][1] == "buy"
    assert db.ladder_fill_resumen()["colocados"] == 1


def test_ejecutar_stop_market_sell_y_realizado(tmp_path, monkeypatch):
    esc, core, db = _esc(tmp_path, monkeypatch)
    db.ladder_set_posicion("LTC/USDT", 0, 0.1, 100.0, "s1")
    esc._ejecutar_accion("LTC/USDT", {"tipo": "stop", "rung_idx": 0, "qty": 0.1, "buy_px": 100.0})
    assert db.ladder_get_posiciones() == []                 # liberó el slot
    real = db._exec("SELECT motivo FROM ladder_realized", fetch="all")
    assert real and real[0][0] == "stop"


# =============================================================================
# Task 6 — reconciliar_ladder (OKX = verdad: fills → posición+sell; sell lleno → round-trip)
# =============================================================================
def test_reconcile_buy_lleno_crea_posicion_y_sell(tmp_path, monkeypatch):
    esc, core, db = _esc(tmp_path, monkeypatch)
    esc._ejecutar_accion("LTC/USDT", {"tipo": "buy", "rung_idx": 0, "price": 100.0, "qty": 0.1})
    oid = next(iter(core.exchange_trade.orders))
    core.exchange_trade._fill(oid, average=100.0)           # OKX reporta el buy lleno
    esc.reconciliar_ladder()
    assert db.ladder_get_posiciones()                       # creó la posición
    assert db.ladder_fill_resumen()["llenos"] == 1
    assert any(o["side"] == "sell" for o in core.exchange_trade.orders.values())  # colocó el sell


def test_reconcile_sell_lleno_cierra_roundtrip(tmp_path, monkeypatch):
    esc, core, db = _esc(tmp_path, monkeypatch)
    esc._ejecutar_accion("LTC/USDT", {"tipo": "buy", "rung_idx": 0, "price": 100.0, "qty": 0.1})
    bo = next(iter(core.exchange_trade.orders))
    core.exchange_trade._fill(bo, average=100.0)
    esc.reconciliar_ladder()                                # crea posición + sell
    sell_oid = next(o for o, v in core.exchange_trade.orders.items() if v["side"] == "sell")
    core.exchange_trade._fill(sell_oid, average=101.5)      # el sell round-trip llena
    esc.reconciliar_ladder()
    assert db.ladder_get_posiciones() == []                 # round-trip cerrado, slot libre
    real = db._exec("SELECT motivo, pnl_neto FROM ladder_realized", fetch="all")
    assert real and real[-1][0] == "round-trip" and real[-1][1] > 0


def test_reconcile_rearma_sell_huerfana_tras_fallo(tmp_path, monkeypatch):
    """Bug real de la canary (2026-06-30, TRX rung0): el buy llenó pero la sell round-trip falló
    (OKX 51008 'available balance insufficient' — el TRX recién comprado/redimido de Earn no estaba
    disponible en el instante del fill) → posición huérfana con sell_order_id vacío. El reconcile
    solo revisita órdenes state='open', así que nunca re-armaba la sell y la holding quedaba sin
    salida. Debe reintentar la sell en el siguiente reconcile cuando el balance ya liquidó."""
    esc, core, db = _esc(tmp_path, monkeypatch)
    esc._ejecutar_accion("LTC/USDT", {"tipo": "buy", "rung_idx": 0, "price": 100.0, "qty": 0.1})
    bo = next(iter(core.exchange_trade.orders))
    core.exchange_trade._fill(bo, average=100.0)

    orig_sell = core.exchange_trade.create_limit_sell_order   # 1ª sell falla (como el 51008 en vivo)
    core.exchange_trade.create_limit_sell_order = lambda *a, **k: (_ for _ in ()).throw(
        Exception("okx 51008 available balance insufficient"))
    esc.reconciliar_ladder()
    pos = db.ladder_get_posiciones()
    assert pos and pos[0]["sell_order_id"] == ""              # huérfana: holding sin sell
    assert not any(o["side"] == "sell" for o in core.exchange_trade.orders.values())

    core.exchange_trade.create_limit_sell_order = orig_sell   # balance ya liquidado → re-arma
    esc.reconciliar_ladder()
    pos = db.ladder_get_posiciones()
    assert pos and pos[0]["sell_order_id"]                    # sell re-armada
    assert pos[0]["rung_idx"] == 0 and pos[0]["avg_cost"] == 100.0   # misma posición, no duplicada
    ventas = [o for o in core.exchange_trade.orders.values() if o["side"] == "sell"]
    assert len(ventas) == 1 and abs(ventas[0]["price"] - 100.0 * 1.015) < 1e-6   # a +spacing


# =============================================================================
# V1.9.19 — el reconcile no tenía estado TERMINAL (canceled/expired/rejected).
# `if status not in ("closed","filled"): continue` dejaba la fila state='open' PARA SIEMPRE:
# _comprometido seguía contando su nocional (comía del cap sin exposición real) y _estado
# veía el rung 'pending' indefinidamente → nunca se re-armaba, en silencio. Misma clase que
# las 11.5 h de V1.9.17. El core sí maneja los tres estados (main.py:3679); el ladder no.
# =============================================================================

def test_accion_reconcile_pura():
    a = main.ladder_accion_reconcile
    assert a("closed", 0.1) == "fill"
    assert a("filled", 0.1) == "fill"
    assert a("canceled", 0.0) == "terminal"
    assert a("cancelled", 0.0) == "terminal"      # ambas grafías del mismo estado
    assert a("expired", 0.0) == "terminal"
    assert a("rejected", 0.0) == "terminal"
    assert a("canceled", 0.05) == "fill"          # cancelada CON parcial: la cripto existe
    assert a("open", 0.0) == "esperar"
    assert a(None, 0.0) == "esperar"              # dato ausente → no concluir (directiva 20)


def test_reconcile_buy_cancelada_libera_el_rung(tmp_path, monkeypatch):
    """La buy cancelada no dejó exposición: la fila debe salir de 'open' para que
    _comprometido deje de contarla y el rung vuelva a 'armed'."""
    esc, core, db = _esc(tmp_path, monkeypatch)
    esc._ejecutar_accion("LTC/USDT", {"tipo": "buy", "rung_idx": 0, "price": 100.0, "qty": 0.1})
    oid = next(iter(core.exchange_trade.orders))
    assert esc._comprometido("LTC/USDT") > 0                 # antes: cuenta el nocional
    core.exchange_trade._cancelar(oid)                       # OKX la canceló
    esc.reconciliar_ladder()
    assert esc._comprometido("LTC/USDT") == 0                # ya no come del cap
    assert esc._estado("LTC/USDT", 4)["rungs"][0]["state"] == "armed"   # re-armable
    assert db.ladder_get_posiciones() == []                  # no inventó posición


def test_reconcile_sell_cancelada_deja_la_posicion_con_salida(tmp_path, monkeypatch):
    """La trampa del fix: marcar la sell terminal SIN limpiar sell_order_id deja la
    posición retenida y _rearmar_ventas_huerfanas la salta (solo mira sell_order_id
    vacío) → cripto sin salida para siempre. Cambiar un silencio por otro."""
    esc, core, db = _esc(tmp_path, monkeypatch)
    esc._ejecutar_accion("LTC/USDT", {"tipo": "buy", "rung_idx": 0, "price": 100.0, "qty": 0.1})
    bo = next(iter(core.exchange_trade.orders))
    core.exchange_trade._fill(bo, average=100.0)
    esc.reconciliar_ladder()                                  # posición + sell
    sell_oid = next(o for o, v in core.exchange_trade.orders.items() if v["side"] == "sell")
    core.exchange_trade._cancelar(sell_oid)                   # OKX canceló la salida
    esc.reconciliar_ladder()
    pos = db.ladder_get_posiciones()
    assert pos and pos[0]["qty"] == 0.1                       # la cripto SIGUE ahí
    assert pos[0]["sell_order_id"] and pos[0]["sell_order_id"] != sell_oid   # salida nueva
    vivas = [o for o in core.exchange_trade.orders.values() if o["side"] == "sell"]
    assert len(vivas) == 1                                    # exactamente una, no dos


def test_reconcile_buy_cancelada_con_parcial_no_pierde_la_cripto(tmp_path, monkeypatch):
    """Cancelada tras llenar parte: esa cripto EXISTE en la cuenta. Tratarla como
    terminal a secas la volvería inventario huérfano que el ladder nunca vende."""
    esc, core, db = _esc(tmp_path, monkeypatch)
    esc._ejecutar_accion("LTC/USDT", {"tipo": "buy", "rung_idx": 0, "price": 100.0, "qty": 0.1})
    oid = next(iter(core.exchange_trade.orders))
    core.exchange_trade._cancelar(oid, filled=0.04)           # llenó 0.04 de 0.1
    esc.reconciliar_ladder()
    pos = db.ladder_get_posiciones()
    assert pos and abs(pos[0]["qty"] - 0.04) < 1e-12          # la qty REAL, no la pedida
    assert pos[0]["sell_order_id"]                            # y con su salida colocada


def test_reconcile_sell_cancelada_con_parcial_conserva_el_resto(tmp_path, monkeypatch):
    """Vendió una parte y la cancelaron: se realiza SOLO lo vendido y el resto queda en
    la posición con salida nueva. Registrar el round-trip completo inventaría un PnL que
    no ocurrió y dejaría la cripto restante huérfana."""
    esc, core, db = _esc(tmp_path, monkeypatch)
    esc._ejecutar_accion("LTC/USDT", {"tipo": "buy", "rung_idx": 0, "price": 100.0, "qty": 0.1})
    bo = next(iter(core.exchange_trade.orders))
    core.exchange_trade._fill(bo, average=100.0)
    esc.reconciliar_ladder()
    sell_oid = next(o for o, v in core.exchange_trade.orders.items() if v["side"] == "sell")
    core.exchange_trade._cancelar(sell_oid, filled=0.06)      # vendió 0.06 de 0.1
    esc.reconciliar_ladder()

    real = db._exec("SELECT qty FROM ladder_realized", fetch="all")
    assert real and abs(real[-1][0] - 0.06) < 1e-12           # realiza solo lo vendido
    pos = db.ladder_get_posiciones()
    assert pos and abs(pos[0]["qty"] - 0.04) < 1e-12          # el resto sigue en la posición
    assert abs(pos[0]["avg_cost"] - 100.0) < 1e-12            # sin tocar el costo base
    assert pos[0]["sell_order_id"] and pos[0]["sell_order_id"] != sell_oid   # salida re-armada


def test_reconcile_orden_abierta_sigue_esperando(tmp_path, monkeypatch):
    """Discriminación: el estado terminal no puede tragarse una orden viva."""
    esc, core, db = _esc(tmp_path, monkeypatch)
    esc._ejecutar_accion("LTC/USDT", {"tipo": "buy", "rung_idx": 0, "price": 100.0, "qty": 0.1})
    esc.reconciliar_ladder()
    abiertas = db._exec("SELECT COUNT(*) FROM ladder_orders WHERE state='open'", fetch="one")
    assert abiertas[0] == 1                                   # sigue 'open'
    assert esc._estado("LTC/USDT", 4)["rungs"][0]["state"] == "pending"


# =============================================================================
# Task 7 — _tick (reconcile→régimen→planner→caps→ejecutar) + run loop
# =============================================================================
def _esc_tick(tmp_path, monkeypatch, regimen="RANGO", precio=100.0):
    esc, core, db = _esc(tmp_path, monkeypatch)
    monkeypatch.setattr(esc, "_precio", lambda sym: precio)
    monkeypatch.setattr(esc, "_regimen", lambda sym: regimen)
    monkeypatch.setitem(main.CONFIG, "ladder_rungs", 4)
    monkeypatch.setitem(main.CONFIG, "ladder_max_capital", 100.0)
    monkeypatch.setitem(main.CONFIG, "ladder_spacing_pct", 1.5)
    monkeypatch.setitem(main.CONFIG, "ladder_stop_pct", 15.0)
    return esc, core, db


def test_tick_arma_escalera_en_rango_dentro_del_cap(tmp_path, monkeypatch):
    esc, core, db = _esc_tick(tmp_path, monkeypatch, regimen="RANGO", precio=100.0)
    esc._tick()
    buys = [o for o in core.exchange_trade.orders.values() if o["side"] == "buy"]
    assert len(buys) == 4
    comprometido = sum(o["amount"] * o["price"] for o in buys)
    assert comprometido <= 100.0 + 1e-6                      # no excede el envelope


def test_tick_no_opera_fuera_de_rango(tmp_path, monkeypatch):
    esc, core, db = _esc_tick(tmp_path, monkeypatch, regimen="TENDENCIA_DOWN", precio=100.0)
    esc._tick()
    assert not any(o["side"] == "buy" for o in core.exchange_trade.orders.values())


def test_tick_loss_cap_congela_compras(tmp_path, monkeypatch):
    esc, core, db = _esc_tick(tmp_path, monkeypatch, regimen="RANGO", precio=100.0)
    monkeypatch.setitem(main.CONFIG, "ladder_daily_loss_cap", 15.0)
    db.ladder_registrar_realizado("LTC/USDT", 0.1, 100.0, 80.0, -20.0, "stop")   # -20 < -15
    esc._tick()
    assert not any(o["side"] == "buy" for o in core.exchange_trade.orders.values())


# =============================================================================
# Task 8 — arranque condicional (_build_ladder respeta el flag)
# =============================================================================
def test_build_ladder_respeta_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "PARES_LADDER", ["LTC/USDT"])
    db = main.DatabaseManager(str(tmp_path / "t.db"))
    core = _FakeCore()
    monkeypatch.setitem(main.CONFIG, "ladder_enabled", False)
    assert main._build_ladder(core, db) is None
    monkeypatch.setitem(main.CONFIG, "ladder_enabled", True)
    assert isinstance(main._build_ladder(core, db), main.EscuadronLadderOkx)


# =============================================================================
# Safety — KILL cierra holdings + cancela resting (contrato del kill-switch)
# =============================================================================
def test_kill_cierra_holdings_y_cancela_resting(tmp_path, monkeypatch):
    esc, core, db = _esc(tmp_path, monkeypatch)
    # una holding + una orden buy resting
    db.ladder_set_posicion("LTC/USDT", 0, 0.1, 100.0, "s1")
    esc._ejecutar_accion("LTC/USDT", {"tipo": "buy", "rung_idx": 1, "price": 98.0, "qty": 0.1})
    esc.cerrar_todo_kill()
    assert db.ladder_get_posiciones() == []                  # holdings market-sell
    real = db._exec("SELECT motivo FROM ladder_realized", fetch="all")
    assert real and real[-1][0] == "KILL"
    abiertas = db._exec("SELECT COUNT(*) FROM ladder_orders WHERE state='open'", fetch="one")
    assert abiertas[0] == 0                                   # resting canceladas


def test_sell_hace_flush_to_spot(tmp_path, monkeypatch):
    # el LTC del ladder puede estar en Earn (GestorInventario barre alts, default true) →
    # el sell debe flush_to_spot antes (como execute_limit_sell_harvest del grid)
    esc, core, db = _esc(tmp_path, monkeypatch)
    llamados = []

    class _Inv:
        def flush_to_spot(self, sym, qty):
            llamados.append((sym, qty))

    core.inventario = _Inv()
    db.ladder_set_posicion("LTC/USDT", 0, 0.1, 100.0, "s1")
    esc._ejecutar_accion("LTC/USDT", {"tipo": "stop", "rung_idx": 0, "qty": 0.1, "buy_px": 100.0})
    assert llamados and llamados[0][0] == "LTC/USDT"


# =============================================================================
# V1.9.9 — Fix A: capar la venta al free spot real (el qty registrado es BRUTO; el fee del buy
# se cobró en la cripto → vender el bruto da OKX 51008, visto 2026-07-02 TRX rung0)
# =============================================================================
def test_sell_capa_qty_al_free_disponible(tmp_path, monkeypatch):
    esc, core, db = _esc(tmp_path, monkeypatch)
    core._free = {"LTC": 0.09}                        # 0.1 comprado − fee ≈ 0.09 disponible
    esc._sell_limit("LTC/USDT", 0, 101.5, 0.1)        # pide vender el bruto 0.1
    ventas = [o for o in core.exchange_trade.orders.values() if o["side"] == "sell"]
    assert len(ventas) == 1
    assert abs(ventas[0]["amount"] - 0.09) < 1e-9     # capado al disponible, no 0.1


def test_sell_no_capa_si_free_alcanza(tmp_path, monkeypatch):
    esc, core, db = _esc(tmp_path, monkeypatch)
    core._free = {"LTC": 0.5}                          # alcanza de sobra
    esc._sell_limit("LTC/USDT", 0, 101.5, 0.1)
    ventas = [o for o in core.exchange_trade.orders.values() if o["side"] == "sell"]
    assert len(ventas) == 1 and abs(ventas[0]["amount"] - 0.1) < 1e-9   # vende el bruto entero


def test_sell_free_desconocido_intenta_bruto(tmp_path, monkeypatch):
    # sin lectura de balance (harness viejo, get_balance vacío) → no regresiona: intenta el bruto
    esc, core, db = _esc(tmp_path, monkeypatch)
    esc._sell_limit("LTC/USDT", 0, 101.5, 0.1)
    ventas = [o for o in core.exchange_trade.orders.values() if o["side"] == "sell"]
    assert len(ventas) == 1 and abs(ventas[0]["amount"] - 0.1) < 1e-9


# =============================================================================
# V1.9.9 — Fix B: el _flush del ladder consolida funding→trading (el barrido a Earn deja cripto
# varada en funding tras un purchase fallido; sin traerla, el sell no llena). Self-healing.
# =============================================================================
def test_flush_consolida_funding_a_trading(tmp_path, monkeypatch):
    esc, core, db = _esc(tmp_path, monkeypatch)
    movimientos = []

    class _Inv:
        def flush_to_spot(self, sym, qty):
            pass

        def _funding_balance_ccy(self, ccy):
            return 16.56 if ccy == "LTC" else 0.0

        def _transfer(self, ccy, amt, src, dst):
            movimientos.append((ccy, amt, src, dst))
            return True

    core.inventario = _Inv()
    esc._flush("LTC/USDT", 0.1)
    assert movimientos and movimientos[0][0] == "LTC"
    assert movimientos[0][2] == main.OKX_CUENTA_FUNDING and movimientos[0][3] == main.OKX_CUENTA_TRADING
    assert abs(movimientos[0][1] - 16.56) < 1e-9


def test_flush_funding_cero_no_transfiere(tmp_path, monkeypatch):
    esc, core, db = _esc(tmp_path, monkeypatch)
    movimientos = []

    class _Inv:
        def flush_to_spot(self, sym, qty):
            pass

        def _funding_balance_ccy(self, ccy):
            return 0.0

        def _transfer(self, ccy, amt, src, dst):
            movimientos.append((ccy, amt, src, dst))
            return True

    core.inventario = _Inv()
    esc._flush("LTC/USDT", 0.1)
    assert movimientos == []                          # nada varado → no-op


# =============================================================================
# V1.9.9 — Fix C: el barrido a Earn (GestorInventario) NO debe tocar los ccy del ladder (un
# purchase fallido varaba la cripto en funding y rompía el sell). _syms_objetivo los excluye.
# =============================================================================
def test_gestor_inventario_excluye_ccy_del_ladder(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "PARES_LADDER", ["TRX/USDT"])
    monkeypatch.setattr(main, "PARES_OKX_SPOT", ["BTC/USDT", "ETH/USDT", "TRX/USDT"])
    monkeypatch.setitem(main.CONFIG, "okx_inv_incluye_alts", False)
    # Explícito, no heredado del .env: la exclusión ahora exige un ladder VIVO
    # (ladder_ccys_gestionados). Sin esta línea el test pasaba solo en la máquina
    # cuyo .env tenía LADDER_ENABLED=true — verde por entorno, no por código.
    monkeypatch.setitem(main.CONFIG, "ladder_enabled", True)
    db = main.DatabaseManager(str(tmp_path / "t.db"))
    gi = main.GestorInventario(_FakeCore(), db)
    syms = gi._syms_objetivo()
    assert "TRX/USDT" not in syms                      # ladder excluido del barrido
    assert "BTC/USDT" in syms and "ETH/USDT" in syms   # el resto intacto


# =============================================================================
# V1.9.9 — Fix B': _transfer FLOOR (round(,8) redondeaba HACIA ARRIBA por encima del disponible
# → OKX 58350; dejó 16.56 TRX varados en funding 2026-07-02, mismo bug que sufre la tesorería)
# =============================================================================
def test_transfer_floor_no_redondea_hacia_arriba(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "PARES_LADDER", ["TRX/USDT"])
    db = main.DatabaseManager(str(tmp_path / "t.db"))
    core = _FakeCore()
    capt = {}
    core.exchange_trade.private_post_asset_transfer = lambda p: capt.update(p) or {"code": "0"}
    gi = main.GestorInventario(core, db)
    avail = 16.5618266480484005                        # el availBal real que rompía en vivo
    assert gi._transfer("TRX", avail, "6", "18") is True
    amt_sent = float(capt["amt"])
    assert amt_sent <= avail                           # nunca pide más que el disponible (evita 58350)
    assert avail - amt_sent < 1e-8                     # pierde a lo sumo 1 unidad de la 8ª decimal


# =============================================================================
# V1.9.17 — El canary quedó MUDO al pasar LADDER_PARES a pares fuera de OKX_PARES_SPOT.
#
# WebSocketFeedOkx suscribía `set(PARES_OKX_SPOT) | set(portfolio)`, sin PARES_LADDER.
# Con LADDER_PARES=XRP,LTC y OKX_PARES_SPOT=BTC,ETH,TRX (config del VPS 2026-08-02),
# PRECIOS_RAM["okx"] nunca recibía XRP/LTC → EscuadronLadderOkx._precio() devolvía None
# → `continue` SILENCIOSO en _tick → el sleeve no posteó una sola orden en 11.5 h vivas
# (0 órdenes abiertas en OKX, 0 filas XRP/LTC en ladder_orders, 0 errores en el log).
# Mientras el canary corrió sobre TRX el bug no existía: TRX sí estaba en OKX_PARES_SPOT.
# =============================================================================
def test_feed_okx_suscribe_los_pares_del_ladder():
    """RED: el universo del feed debe incluir PARES_LADDER; si no, el sleeve no ve precio."""
    syms = main.simbolos_feed_okx(["BTC/USDT", "ETH/USDT", "TRX/USDT"],
                                  ["XRP/USDT", "LTC/USDT"], ["TRX/USDT"])
    assert "XRP/USDT" in syms and "LTC/USDT" in syms   # la causa del canary mudo
    assert "BTC/USDT" in syms and "ETH/USDT" in syms   # el universo del grid, intacto
    assert "TRX/USDT" in syms                          # el portfolio heredado, intacto


def test_feed_okx_no_inventa_pares_ni_duplica():
    """DISCRIMINA: el fix suma lo que el ladder pide, no abre el universo ni duplica."""
    syms = main.simbolos_feed_okx(["BTC/USDT"], [], ["BTC/USDT"])
    assert syms == ["BTC/USDT"]                        # nada de la nada, sin duplicados
    syms2 = main.simbolos_feed_okx(["BTC/USDT"], ["BTC/USDT"], [])
    assert syms2 == ["BTC/USDT"]                       # solapamiento ladder/spot no duplica


def test_ladder_grita_cuando_no_hay_precio(tmp_path, monkeypatch):
    """Directiva 20: sin precio el sleeve no opera, pero NO puede callarse. El `continue`
    mudo es lo que dejó pasar 11.5 h de canary muerto sin una línea en errors.log."""
    monkeypatch.setattr(main, "PARES_LADDER", ["XRP/USDT"])
    db = main.DatabaseManager(str(tmp_path / "t.db"))
    esc = main.EscuadronLadderOkx(_FakeCore(), db)
    monkeypatch.setattr(esc, "_precio", lambda sym: None)
    gritos = []
    monkeypatch.setattr(main.logger_errores, "error", lambda m, *a, **k: gritos.append(m))
    esc._tick()
    assert any("XRP/USDT" in g and "precio" in g.lower() for g in gritos)


# ============================================================================
# Visibilidad del sleeve (V1.9.16) — un ladder CONGELADO por régimen propio no
# se distinguía de uno MUERTO: ni línea de HUD ni log. Es la misma clase de
# fallo que las 11.5 h de canary mudo que motivaron el grito de "SIN PRECIO"
# (main.py:4840), que solo tapó una de las causas. Directiva 20: la ausencia de
# dato se grita, no se rellena con silencio.
# ============================================================================

def _est(**kw):
    base = {"ts": 1000.0, "regimen": main.REGIMEN_RANGO, "motivo": None,
            "armed": 4, "pending": 0, "holding": 0,
            "comprometido": 0.0, "cap": 50.0, "precio": 2.5}
    base.update(kw)
    return base


def test_hud_ladder_sin_estado_grita_en_vez_de_callarse():
    """El hilo nunca publicó: la línea debe DECIRLO, no desaparecer."""
    linea = main.hud_linea_ladder("XRP/USDT", None, ahora=1000.0, tick_s=60.0)
    assert "XRP/USDT" in linea
    assert "SIN ESTADO" in linea


def test_hud_ladder_stale_delata_el_hilo_colgado():
    """Publicó hace 10 ciclos: el hilo está colgado y la línea lo marca STALE."""
    linea = main.hud_linea_ladder("LTC/USDT", _est(ts=1000.0), ahora=1600.0, tick_s=60.0)
    assert "STALE" in linea
    assert "600" in linea          # segundos sin tick, para saber desde cuándo


def test_hud_ladder_congelado_por_regimen_dice_regimen_y_no_stale():
    """El caso real del 2026-08-03: XRP/LTC en TENDENCIA_DOWN, cero órdenes.
    Debe leerse CONGELADO(regimen) — vivo pero sin operar — y NO stale."""
    est = _est(regimen=main.REGIMEN_TEND_DOWN, motivo="regimen", ts=1000.0)
    linea = main.hud_linea_ladder("XRP/USDT", est, ahora=1030.0, tick_s=60.0)
    assert "CONGELADO(regimen)" in linea
    assert main.REGIMEN_TEND_DOWN in linea
    assert "STALE" not in linea
    assert "SIN ESTADO" not in linea


def test_hud_ladder_operando_en_rango_no_dice_congelado():
    """Discrimina: en RANGO operando, la línea NO debe decir CONGELADO."""
    est = _est(regimen=main.REGIMEN_RANGO, motivo=None, armed=2, pending=1, holding=1,
               comprometido=25.0)
    linea = main.hud_linea_ladder("XRP/USDT", est, ahora=1030.0, tick_s=60.0)
    assert "CONGELADO" not in linea
    assert "OPERA" in linea
    assert "a2/p1/h1" in linea      # rungs armed/pending/holding
    assert "25.00/50.00" in linea   # capital comprometido / cap


def test_hud_ladder_sin_precio_es_motivo_propio_no_regimen():
    """Un par fuera del feed no puede operar por otra razón: el motivo la distingue."""
    est = _est(motivo="sin_precio", precio=None)
    linea = main.hud_linea_ladder("XRP/USDT", est, ahora=1030.0, tick_s=60.0)
    assert "CONGELADO(sin_precio)" in linea


def test_hud_ladder_stale_gana_sobre_congelado():
    """Un estado viejo NO debe leerse como 'congelado y todo en orden': el hilo
    colgado es el diagnóstico grave y tiene precedencia."""
    est = _est(regimen=main.REGIMEN_TEND_DOWN, motivo="regimen", ts=1000.0)
    linea = main.hud_linea_ladder("XRP/USDT", est, ahora=9000.0, tick_s=60.0)
    assert "STALE" in linea


def test_ladder_publica_estado_en_cada_tick(tmp_path, monkeypatch):
    """El _tick debe dejar rastro publicado aunque NO haga nada (régimen down)."""
    core = _FakeCore()
    db = main.DatabaseManager(str(tmp_path / "t.db"))
    monkeypatch.setitem(main.PRECIOS_RAM.setdefault("okx", {}), "XRP/USDT",
                        {"bid": 2.5, "ask": 2.51, "last": 2.5, "local_ts": main.time.time()})
    lad = main.EscuadronLadderOkx(core, db)
    lad.pares = ["XRP/USDT"]
    lad._reg_cache["XRP/USDT"] = (main.REGIMEN_TEND_DOWN, main.time.time())
    main.ladder_estado_publicado.clear()
    lad._tick()
    est = main.ladder_estado_publicado.get("XRP/USDT")
    assert est is not None, "el tick no publicó estado → el HUD seguiría mudo"
    assert est["regimen"] == main.REGIMEN_TEND_DOWN
    assert est["motivo"] == "regimen"


def test_hud_texto_emite_la_linea_del_ladder(tmp_path, monkeypatch, capsys):
    """Cableado: no basta con que la función pura exista, el HUD tiene que LLAMARLA.
    Un helper correcto que nadie invoca deja el mismo silencio que había."""
    db = main.DatabaseManager(str(tmp_path / "t.db"))
    monkeypatch.setitem(main.CONFIG, "ladder_enabled", True)
    monkeypatch.setattr(main, "PARES_LADDER", ["XRP/USDT"])
    monkeypatch.setattr(main, "ladder_estado_publicado", {})   # nadie publicó aún
    hud = main.RadarTelemetria(_FakeCore(), db)
    hud._render_text()
    salida = capsys.readouterr().out
    assert "[HUD] LADDER XRP/USDT" in salida
    assert "SIN ESTADO" in salida


def test_hud_texto_calla_el_ladder_si_esta_apagado(tmp_path, monkeypatch, capsys):
    """Discrimina: con el sleeve OFF no debe aparecer la línea (ruido inútil)."""
    db = main.DatabaseManager(str(tmp_path / "t.db"))
    monkeypatch.setitem(main.CONFIG, "ladder_enabled", False)
    monkeypatch.setattr(main, "PARES_LADDER", ["XRP/USDT"])
    hud = main.RadarTelemetria(_FakeCore(), db)
    hud._render_text()
    assert "LADDER" not in capsys.readouterr().out
