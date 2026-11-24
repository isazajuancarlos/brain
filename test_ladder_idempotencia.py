"""A (V1.9.13) — idempotencia de las escrituras reintentadas del ladder.

Contexto: el ladder envuelve create_limit_buy/sell y create_market_sell en okx_retry,
que reintenta ante RequestTimeout/NetworkError. Un timeout AMBIGUO (la orden llegó a
OKX pero la respuesta se perdió) hacía que el reintento DUPLICARA la orden — sin ningún
clOrdId que lo impidiera (cero en todo el código). Doble-orden / doble-venta = la clase
"semántica de exchange" que arrastra el bot.

Fix: cada colocación genera UN clOrdId estable que se reusa en todos los reintentos →
OKX rechaza el duplicado (at-most-once). Si la orden aterrizó pero el create terminó en
excepción, se recupera por clOrdId (fetch idempotente) en vez de orfanarla.
"""
import threading
import ccxt
import main


# ---------- primitivo puro ----------

def test_nuevo_clordid_valido_okx():
    for _ in range(50):
        cid = main.nuevo_clordid("lad")
        assert cid.isalnum()          # OKX exige alfanumérico
        assert 1 <= len(cid) <= 32    # y ≤32 chars
        assert cid.startswith("lad")


def test_nuevo_clordid_unico():
    ids = {main.nuevo_clordid("lad") for _ in range(1000)}
    assert len(ids) == 1000           # sin colisiones entre colocaciones


# ---------- fake OKX que modela la semántica de clOrdId ----------

class _FakeOkx:
    """Modela OKX: clOrdId duplicado → rechazado (at-most-once). fail_mode inyecta el
    escenario timeout-aterrizado o el 403-nunca-aterriza."""

    def __init__(self, fail_mode=None):
        self.orders = {}          # oid -> order
        self._by_clord = {}       # clOrdId -> oid
        self._n = 0
        self.create_attempts = []  # clOrdId visto en cada intento de create
        self.fail_mode = fail_mode
        self._timeout_usado = False

    def _create(self, sym, side, qty, price, status, params):
        cid = (params or {}).get("clOrdId")
        self.create_attempts.append(cid)
        if cid and cid in self._by_clord:              # OKX rechaza el duplicado (solo con clOrdId)
            raise ccxt.ExchangeError(f"51020 Duplicated client order ID: {cid}")
        if self.fail_mode == "always_403":
            raise ccxt.DDoSProtection("403 forbidden cloudflare")
        # la orden SIEMPRE aterriza aquí (registra), luego decide si "responde"
        self._n += 1
        oid = f"o{self._n}"
        self.orders[oid] = {"id": oid, "symbol": sym, "side": side, "amount": qty,
                            "price": price, "status": status, "filled": qty if status == "closed" else 0.0,
                            "average": price or 0.0}
        if cid:
            self._by_clord[cid] = oid
        if self.fail_mode == "timeout_landed" and not self._timeout_usado:
            self._timeout_usado = True
            raise ccxt.RequestTimeout("timed out (pero la orden aterrizó)")
        return self.orders[oid]

    def create_limit_buy_order(self, sym, qty, price, params=None):
        return self._create(sym, "buy", qty, price, "open", params)

    def create_limit_sell_order(self, sym, qty, price, params=None):
        return self._create(sym, "sell", qty, price, "open", params)

    def create_market_sell_order(self, sym, qty, params=None):
        return self._create(sym, "sell", qty, None, "closed", params)

    def fetch_order(self, oid, sym=None, params=None):
        cid = (params or {}).get("clOrdId")
        if cid:
            roid = self._by_clord.get(cid)
            return self.orders.get(roid) if roid else None
        return self.orders.get(oid)


class _FakeDB:
    def __init__(self):
        self.ordenes = []
        self.realizados = []
        self.pos_borradas = []

    def ladder_registrar_orden(self, oid, sym, side, rung_idx, price, qty):
        self.ordenes.append((oid, sym, side, rung_idx, price, qty))

    def ladder_registrar_realizado(self, sym, qty, buy_px, px, pnl, motivo):
        self.realizados.append((sym, qty, buy_px, px, pnl, motivo))

    def ladder_del_posicion(self, sym, rung_idx):
        self.pos_borradas.append((sym, rung_idx))


class _FakeCore:
    def __init__(self, ex):
        self.exchange_trade = ex
        self.api_trade_lock = threading.RLock()
        self.inventario = None
        self._free = {}

    def get_balance(self, force_refresh=False):
        return {ccy: {"free": v} for ccy, v in self._free.items()}

    def invalidate_balance(self):
        pass


def _esc(ex, monkeypatch):
    # okx_retry sin dormir y con 2 intentos (suficiente: timeout → duplicado)
    monkeypatch.setitem(main.CONFIG, "okx_retry_intentos", 2)
    monkeypatch.setitem(main.CONFIG, "okx_retry_base_s", 0.0)
    monkeypatch.setitem(main.CONFIG, "okx_retry_max_s", 0.0)
    return main.EscuadronLadderOkx(_FakeCore(ex), _FakeDB())


# ---------- comportamiento ----------

def test_buy_pasa_clordid_al_exchange(monkeypatch):
    ex = _FakeOkx()
    esc = _esc(ex, monkeypatch)
    oid = esc._buy_maker("LTC/USDT", 0, 60.0, 1.0)
    assert oid is not None
    assert len(ex.create_attempts) == 1
    assert ex.create_attempts[0] and ex.create_attempts[0].isalnum()   # clOrdId presente


def test_buy_timeout_aterrizado_NO_duplica(monkeypatch):
    # attempt1: la orden aterriza pero devuelve RequestTimeout; okx_retry reintenta con el
    # MISMO clOrdId → OKX rechaza el duplicado → recuperación por clOrdId. UNA sola orden.
    ex = _FakeOkx(fail_mode="timeout_landed")
    esc = _esc(ex, monkeypatch)
    oid = esc._buy_maker("LTC/USDT", 0, 60.0, 1.0)
    assert len(ex.orders) == 1                       # <-- clave: NO se duplicó
    assert oid == "o1"                               # recuperó el id real de la aterrizada
    assert len(esc.db.ordenes) == 1                  # y quedó registrada (no huérfana)
    assert esc.db.ordenes[0][0] == "o1"
    # el reintento reusó el mismo clOrdId (no uno nuevo)
    assert ex.create_attempts[0] == ex.create_attempts[1]


def test_market_sell_timeout_aterrizado_NO_duplica(monkeypatch):
    # el peor caso: doble venta a mercado. Debe quedar en UNA.
    ex = _FakeOkx(fail_mode="timeout_landed")
    esc = _esc(ex, monkeypatch)
    esc._cerrar_market("LTC/USDT", 0, 1.0, 55.0, "stop")
    assert len(ex.orders) == 1                       # no doble-venta
    assert len(esc.db.realizados) == 1               # realizado registrado una vez
    assert esc.db.pos_borradas == [("LTC/USDT", 0)]


def test_buy_nunca_aterriza_devuelve_none(monkeypatch):
    # todos los intentos 403 (rechazo en el borde, nunca llegó) → recuperación no halla nada.
    ex = _FakeOkx(fail_mode="always_403")
    esc = _esc(ex, monkeypatch)
    oid = esc._buy_maker("LTC/USDT", 0, 60.0, 1.0)
    assert oid is None
    assert len(ex.orders) == 0                        # nada aterrizó
    assert len(esc.db.ordenes) == 0                   # nada registrado (sin regresión)


def test_sell_limit_normal_registra(monkeypatch):
    ex = _FakeOkx()
    esc = _esc(ex, monkeypatch)
    oid = esc._sell_limit("LTC/USDT", 0, 66.0, 1.0)
    assert oid == "o1"
    assert esc.db.ordenes[0][2] == "sell"
