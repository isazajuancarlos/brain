import sqlite3
import pytest
from liquidar import LiquidadorOKX, OKX_CUENTA_FUNDING, OKX_CUENTA_TRADING


class FakeOkx:
    """Cuenta OKX simulada en memoria. redempt mueve earn→funding;
    transfer mueve funding→trading; market sell convierte coin→USDT en trading."""

    def __init__(self, trading=None, funding=None, earn=None, precios=None,
                 markets=None, open_orders=None):
        self.trading = dict(trading or {})   # {ccy: qty}
        self.funding = dict(funding or {})
        self.earn = dict(earn or {})         # savings flexible {ccy: qty}
        self.precios = dict(precios or {})   # {ccy: precio_usdt}
        self.markets = markets or {}         # {sym: {'limits':{'amount':{'min':..},'cost':{'min':..}}}}
        self._open_orders = list(open_orders or [])
        self.ordenes_market = []             # registro de ventas ejecutadas
        self.dust_converted = []

    # --- lectura ---
    def load_markets(self):
        return self.markets

    def fetch_balance(self):
        # Estructura realista de ccxt: claves de nivel superior (info/timestamp/
        # datetime no-dict) + mapas free/used/total + entradas por-moneda.
        free = {ccy: q for ccy, q in self.trading.items()}
        result = {"info": {}, "timestamp": 0, "datetime": None,
                  "free": dict(free), "used": {c: 0.0 for c in free},
                  "total": dict(free)}
        for ccy, q in self.trading.items():
            result[ccy] = {"free": q, "used": 0.0, "total": q}
        return result

    def fetch_ticker(self, sym):
        return {"last": self.precios.get(sym.split("/")[0], 0.0)}

    def fetch_open_orders(self):
        return list(self._open_orders)

    def amount_to_precision(self, sym, qty):
        return f"{float(qty):.6f}"

    def private_get_finance_savings_balance(self, params):
        ccy = params.get("ccy")
        items = self.earn.items() if ccy is None else [(ccy, self.earn.get(ccy, 0.0))]
        return {"data": [{"ccy": c, "amt": str(q)} for c, q in items if q > 0]}

    def private_get_asset_balances(self, params):
        ccy = params.get("ccy")
        items = list(self.funding.items()) if ccy is None else [(ccy, self.funding.get(ccy, 0.0))]
        return {"data": [{"ccy": c, "availBal": str(q)} for c, q in items]}

    # --- escritura ---
    def cancel_order(self, oid, sym):
        self._open_orders = [o for o in self._open_orders if o["id"] != oid]

    def private_post_finance_savings_purchase_redempt(self, params):
        ccy, amt, side = params["ccy"], float(params["amt"]), params["side"]
        if side == "redempt":
            amt = min(amt, self.earn.get(ccy, 0.0))
            self.earn[ccy] = self.earn.get(ccy, 0.0) - amt
            self.funding[ccy] = self.funding.get(ccy, 0.0) + amt  # settlement instantáneo
        return {"code": "0"}

    def private_post_asset_transfer(self, params):
        ccy, amt = params["ccy"], float(params["amt"])
        src, dst = params["from"], params["to"]
        bal = self.funding if src == OKX_CUENTA_FUNDING else self.trading
        dest = self.trading if dst == OKX_CUENTA_TRADING else self.funding
        amt = min(amt, bal.get(ccy, 0.0))
        bal[ccy] = bal.get(ccy, 0.0) - amt
        dest[ccy] = dest.get(ccy, 0.0) + amt
        return {"code": "0"}

    def create_market_sell_order(self, sym, qty):
        ccy = sym.split("/")[0]
        qty = float(qty)
        self.trading[ccy] = self.trading.get(ccy, 0.0) - qty
        self.trading["USDT"] = self.trading.get("USDT", 0.0) + qty * self.precios[ccy]
        order = {"id": f"ord-{len(self.ordenes_market)}", "symbol": sym,
                 "amount": qty, "average": self.precios[ccy]}
        self.ordenes_market.append(order)
        return order


def db_temporal():
    """sqlite en memoria con el schema mínimo que toca el liquidador."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE okx_portfolio (symbol TEXT PRIMARY KEY, average_cost REAL,"
        " total_qty REAL, initial_investment REAL, mode TEXT);"
        "CREATE TABLE okx_inventory_earn (ccy TEXT PRIMARY KEY, symbol TEXT, qty REAL,"
        " deposited_at REAL, last_flush_ms REAL, fail_count INTEGER);"
        "CREATE TABLE okx_tp_state (symbol TEXT PRIMARY KEY);"
        "CREATE TABLE okx_dca_state (symbol TEXT PRIMARY KEY);"
        "CREATE TABLE okx_cooldown_state (symbol TEXT PRIMARY KEY);"
        "CREATE TABLE okx_grid_orders (order_id TEXT PRIMARY KEY, symbol TEXT, side TEXT);"
        "CREATE TABLE okx_pending_sells (id INTEGER PRIMARY KEY, symbol TEXT);"
    )
    return conn


def _markets_basicos(ccys):
    return {f"{c}/USDT": {"limits": {"amount": {"min": 0.0001}, "cost": {"min": 1.0}}}
            for c in ccys}


# ------------------------------------------------------------------ Task 0
def test_harness_fake_redempt_y_transfer():
    ex = FakeOkx(earn={"ADA": 100.0})
    ex.private_post_finance_savings_purchase_redempt({"ccy": "ADA", "amt": "100", "side": "redempt"})
    assert ex.earn["ADA"] == 0.0 and ex.funding["ADA"] == 100.0
    ex.private_post_asset_transfer({"ccy": "ADA", "amt": "100",
                                    "from": OKX_CUENTA_FUNDING, "to": OKX_CUENTA_TRADING})
    assert ex.trading["ADA"] == 100.0


# ------------------------------------------------------------------ Task 1
def test_snapshot_excluye_usdt_y_une_balances():
    ex = FakeOkx(
        trading={"USDT": 500.0, "ETH": 0.02, "ADA": 50.0},
        earn={"ADA": 100.0, "USDT": 300.0},
        precios={"ETH": 3000.0, "ADA": 0.30},
    )
    liq = LiquidadorOKX(ex, db_temporal())
    plan = liq.snapshot()
    ccys = {p["ccy"] for p in plan}
    assert ccys == {"ETH", "ADA"}           # USDT excluido
    ada = next(p for p in plan if p["ccy"] == "ADA")
    assert ada["earn"] == 100.0 and ada["trading"] == 50.0
    assert ada["total"] == 150.0
    assert round(ada["valor_usd"], 2) == 45.0   # 150 * 0.30


def test_snapshot_incluye_funding_varado():
    # Fondos varados en Funding (de una corrida previa fallida) deben ser visibles.
    ex = FakeOkx(trading={"USDT": 100.0}, funding={"ADA": 50.43}, precios={"ADA": 0.30})
    liq = LiquidadorOKX(ex, db_temporal())
    plan = liq.snapshot()
    ada = next(p for p in plan if p["ccy"] == "ADA")
    assert ada["funding"] == pytest.approx(50.43) and ada["total"] == pytest.approx(50.43)


# ------------------------------------------------------------------ Task 2
def test_es_polvo_por_min_amount_y_min_cost():
    markets = {"ADA/USDT": {"limits": {"amount": {"min": 1.0}, "cost": {"min": 5.0}}}}
    ex = FakeOkx(markets=markets, precios={"ADA": 0.30})
    liq = LiquidadorOKX(ex, db_temporal())
    liq.ex.load_markets()
    assert liq.es_polvo("ADA/USDT", 0.5, 0.30) is True
    assert liq.es_polvo("ADA/USDT", 10.0, 0.30) is True
    assert liq.es_polvo("ADA/USDT", 20.0, 0.30) is False


# ------------------------------------------------------------------ Task 3
def test_limpiar_db_borra_solo_liquidados():
    conn = db_temporal()
    conn.executescript(
        "INSERT INTO okx_portfolio VALUES ('ADA/USDT',0.3,50,15,'LIQUIDATE_ONLY');"
        "INSERT INTO okx_portfolio VALUES ('ETH/USDT',3000,0.02,60,'ACTIVE');"
        "INSERT INTO okx_inventory_earn VALUES ('ADA','ADA/USDT',100,0,0,0);"
        "INSERT INTO okx_grid_orders VALUES ('g1','ADA/USDT','BUY');"
        "INSERT INTO okx_tp_state VALUES ('ADA/USDT');"
    )
    conn.commit()
    liq = LiquidadorOKX(FakeOkx(), conn)
    liq.limpiar_db(["ADA/USDT", "ETH/USDT"], ["ADA", "ETH"])
    assert conn.execute("SELECT COUNT(*) FROM okx_portfolio").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM okx_inventory_earn").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM okx_grid_orders").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM okx_tp_state").fetchone()[0] == 0


# ------------------------------------------------------------------ Task 4
def _liq_rapido(ex, **kw):
    return LiquidadorOKX(ex, db_temporal(), ejecutar=True, sleep=lambda s: None,
                         poll_timeout=10, poll_interval=0.0, transfer_backoff=0.0, **kw)


def test_consolidar_rescata_earn_y_barre_a_trading():
    ex = FakeOkx(earn={"ADA": 100.0})
    movido = _liq_rapido(ex).consolidar_a_trading("ADA", 100.0)
    assert movido == 100.0
    assert ex.trading["ADA"] == 100.0 and ex.earn["ADA"] == 0.0 and ex.funding.get("ADA", 0) == 0.0


def test_consolidar_recupera_funding_varado_sin_earn():
    # Escenario real del crash: ADA quedó en Funding (earn ya 0). Debe recuperarse.
    ex = FakeOkx(funding={"ADA": 50.43})
    movido = _liq_rapido(ex).consolidar_a_trading("ADA", 0.0)
    assert movido == pytest.approx(50.43, abs=1e-6)
    assert ex.trading["ADA"] == pytest.approx(50.43, abs=1e-6)


def test_consolidar_reintenta_transfer_y_logra():
    class TransferFalla1Vez(FakeOkx):
        fallas = 1
        def private_post_asset_transfer(self, params):
            if self.fallas > 0:
                self.fallas -= 1
                raise Exception("58350 Insufficient balance")
            return super().private_post_asset_transfer(params)
    ex = TransferFalla1Vez(funding={"ADA": 50.0})
    movido = _liq_rapido(ex).consolidar_a_trading("ADA", 0.0)
    assert movido == pytest.approx(50.0, abs=1e-6)
    assert ex.trading["ADA"] == pytest.approx(50.0, abs=1e-6)


def test_consolidar_timeout_funding_no_crashea():
    class FundingVacio(FakeOkx):
        def private_post_finance_savings_purchase_redempt(self, params):
            return {"code": "0"}  # NO settlea a funding → simula demora
    ex = FundingVacio(earn={"ADA": 100.0})
    liq = LiquidadorOKX(ex, db_temporal(), ejecutar=True, sleep=lambda s: None,
                        poll_timeout=0.01, poll_interval=0.0, transfer_backoff=0.0)
    assert liq.consolidar_a_trading("ADA", 100.0) == 0.0
    assert ex.trading.get("ADA", 0.0) == 0.0


# ------------------------------------------------------------------ Task 5
def test_vender_ejecuta_market_y_devuelve_usdt():
    markets = {"ADA/USDT": {"limits": {"amount": {"min": 1.0}, "cost": {"min": 5.0}}}}
    ex = FakeOkx(trading={"ADA": 100.0}, precios={"ADA": 0.30}, markets=markets)
    ex.load_markets()
    liq = LiquidadorOKX(ex, db_temporal(), ejecutar=True)
    res = liq.vender("ADA", 100.0, 0.30)
    assert res["vendido"] is True
    assert ex.trading.get("USDT", 0.0) == pytest.approx(30.0)
    assert len(ex.ordenes_market) == 1


def test_vender_polvo_no_ejecuta():
    markets = {"ADA/USDT": {"limits": {"amount": {"min": 1.0}, "cost": {"min": 5.0}}}}
    ex = FakeOkx(trading={"ADA": 0.5}, precios={"ADA": 0.30}, markets=markets)
    ex.load_markets()
    liq = LiquidadorOKX(ex, db_temporal(), ejecutar=True)
    res = liq.vender("ADA", 0.5, 0.30)
    assert res["vendido"] is False and res["polvo"] is True
    assert len(ex.ordenes_market) == 0


def test_vender_dust_sub_min_usd_sin_limites_es_polvo():
    # OKX a veces NO reporta cost.min/amount.min → el piso min_usd debe atrapar el polvo.
    markets = {"AAVE/USDT": {"limits": {"amount": {}, "cost": {}}}}
    ex = FakeOkx(trading={"AAVE": 0.000001}, precios={"AAVE": 250.0}, markets=markets)
    ex.load_markets()
    liq = LiquidadorOKX(ex, db_temporal(), ejecutar=True, min_usd=1.0)
    res = liq.vender("AAVE", 0.000001, 250.0)   # valor ~$0.00025
    assert res["polvo"] is True and res["vendido"] is False
    assert len(ex.ordenes_market) == 0


def test_vender_amount_to_precision_raise_no_crashea():
    class PrecisionRaise(FakeOkx):
        def amount_to_precision(self, sym, qty):
            raise Exception("amount below minimum precision")
    markets = {"AAVE/USDT": {"limits": {"amount": {}, "cost": {}}}}
    # valor por encima de min_usd para pasar es_polvo y forzar amount_to_precision
    ex = PrecisionRaise(trading={"AAVE": 0.1}, precios={"AAVE": 250.0}, markets=markets)
    ex.load_markets()
    liq = LiquidadorOKX(ex, db_temporal(), ejecutar=True, min_usd=1.0)
    res = liq.vender("AAVE", 0.1, 250.0)
    assert res["polvo"] is True and res["vendido"] is False


# ------------------------------------------------------------------ Task 6
def test_cancelar_ordenes_abiertas():
    ex = FakeOkx(open_orders=[{"id": "g1", "symbol": "ADA/USDT"},
                              {"id": "g2", "symbol": "ETH/USDT"}])
    liq = LiquidadorOKX(ex, db_temporal(), ejecutar=True)
    n = liq.cancelar_ordenes()
    assert n == 2 and ex.fetch_open_orders() == []


# ------------------------------------------------------------------ Task 7
def test_flujo_completo_deja_solo_usdt():
    ex = FakeOkx(
        trading={"USDT": 500.0, "ETH": 0.02},
        earn={"ADA": 100.0, "USDT": 300.0},   # USDT en earn NO se toca
        precios={"ETH": 3000.0, "ADA": 0.30},
        markets=_markets_basicos(["ETH", "ADA"]),
        open_orders=[{"id": "g1", "symbol": "ETH/USDT"}],
    )
    conn = db_temporal()
    conn.executescript(
        "INSERT INTO okx_portfolio VALUES ('ETH/USDT',3000,0.02,60,'ACTIVE');"
        "INSERT INTO okx_inventory_earn VALUES ('ADA','ADA/USDT',100,0,0,0);")
    conn.commit()
    liq = LiquidadorOKX(ex, conn, ejecutar=True, sleep=lambda s: None,
                        poll_timeout=10, poll_interval=0.0)
    liq.ejecutar_liquidacion()
    assert ex.trading.get("ETH", 0.0) == pytest.approx(0.0)
    assert ex.trading.get("ADA", 0.0) == pytest.approx(0.0)
    assert ex.trading["USDT"] == pytest.approx(590.0)   # 500 + 0.02*3000 + 100*0.30
    assert ex.earn["USDT"] == 300.0                      # earn USDT intacto
    assert ex.fetch_open_orders() == []
    assert conn.execute("SELECT COUNT(*) FROM okx_portfolio").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM okx_inventory_earn").fetchone()[0] == 0


def test_dry_run_no_modifica_nada():
    ex = FakeOkx(trading={"ETH": 0.02}, earn={"ADA": 100.0},
                 precios={"ETH": 3000.0, "ADA": 0.30},
                 markets=_markets_basicos(["ETH", "ADA"]),
                 open_orders=[{"id": "g1", "symbol": "ETH/USDT"}])
    liq = LiquidadorOKX(ex, db_temporal(), ejecutar=False)  # dry-run
    liq.ejecutar_liquidacion()
    assert ex.earn["ADA"] == 100.0           # no rescató
    assert ex.ordenes_market == []           # no vendió
    assert ex.fetch_open_orders() != []      # no canceló


# ------------------------------------------------------------------ Task 8
def test_parse_args_defaults_dry_run():
    from liquidar import parse_args
    a = parse_args([])
    assert a.ejecutar is False and a.convert_dust is False and a.no_clean_db is False
    assert a.min_usd == 1.0
    b = parse_args(["--ejecutar", "--convert-dust", "--no-clean-db"])
    assert b.ejecutar and b.convert_dust and b.no_clean_db
