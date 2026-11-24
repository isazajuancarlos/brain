"""Money-path semantics: unifica el redondeo de montos de MOVIMIENTO de saldo
(transfer / purchase / redeem) a FLOOR.

Contexto: el bot arrastró la clase de bug "pide más de lo disponible" cuatro veces
(51008/58350). La causa raíz es `round()`, que redondea HACIA ARRIBA
(16.5618266480 → 16.56182665) y sobre-solicita. V1.9.9' arregló UN call-site
(GestorInventario._transfer) con floor, pero el propio comentario del fix decía
"mismo bug le pega a la tesorería" — y nunca se aplicó ahí. Estos tests fijan el
primitivo puro `piso_monto` y verifican que TODOS los call-sites (tesorería +
inventario, transfer/purchase/redeem) lo usan → floor, nunca round-up.
"""
import main


# ---------- primitivo puro ----------

def test_piso_monto_nunca_excede():
    for x, d in [(16.5618266480, 8), (16.56185, 4), (0.123456789, 6),
                 (100.9999999, 2), (1234.5678, 3)]:
        assert main.piso_monto(x, d) <= x


def test_piso_monto_caso_58350_real():
    # El caso exacto del 2026-07-02: funding 16.5618266480 TRX.
    # round(x, 8) = 16.56182665 > x → OKX 58350. floor debe quedar por debajo.
    x = 16.5618266480
    assert round(x, 8) > x          # documenta el bug de round
    assert main.piso_monto(x, 8) <= x
    assert main.piso_monto(x, 8) == 16.56182664


def test_piso_monto_usdt_4dec_no_sube():
    # USDT (tesorería) cuantiza a 4 decimales; floor no debe superar el disponible.
    x = 16.56185
    assert main.piso_monto(x, 4) <= x
    assert main.piso_monto(x, 4) == 16.5618


def test_piso_monto_deja_solo_polvo():
    # La diferencia con el valor real es siempre < 1 unidad del último decimal.
    for x, d in [(16.5618266480, 8), (999.123456, 4), (0.5, 2)]:
        assert 0 <= x - main.piso_monto(x, d) < 10 ** (-d)


def test_piso_monto_cero_y_negativo():
    assert main.piso_monto(0.0, 8) == 0.0
    assert main.piso_monto(-5.0, 8) == 0.0    # montos nunca negativos: defensivo


# ---------- call-sites: todos flooran ----------

class _FakeEx:
    def __init__(self):
        self.transfers = []
        self.earn = []

    def private_post_asset_transfer(self, params):
        self.transfers.append(params)
        return {"code": "0", "data": [{}]}

    def private_post_finance_savings_purchase_redempt(self, params):
        self.earn.append(params)
        return {"code": "0", "data": [{}]}


class _FakeCore:
    def __init__(self):
        self.exchange_trade = _FakeEx()


def _teso():
    return main.GestorTesoreria(_FakeCore(), None)


def _inv():
    return main.GestorInventario(_FakeCore(), None)


def test_tesoreria_transfer_floorea():
    # 16.56185 USDT: round(,4) subiría; floor no.
    t = _teso()
    ok = t._transferir(16.56185, main.OKX_CUENTA_FUNDING, main.OKX_CUENTA_TRADING)
    assert ok
    amt = float(t.core.exchange_trade.transfers[-1]["amt"])
    assert amt <= 16.56185
    assert amt == main.piso_monto(16.56185, 4)


def test_tesoreria_purchase_y_redempt_floorean():
    t = _teso()
    t._purchase(16.56185)
    t._redempt(16.56185)
    for p in t.core.exchange_trade.earn:
        assert float(p["amt"]) <= 16.56185


def test_inventario_transfer_floorea_caso_trx():
    inv = _inv()
    ok = inv._transfer("TRX", 16.5618266480, main.OKX_CUENTA_FUNDING, main.OKX_CUENTA_TRADING)
    assert ok
    amt = float(inv.core.exchange_trade.transfers[-1]["amt"])
    assert amt <= 16.5618266480
    assert amt == main.piso_monto(16.5618266480, 8)


def test_inventario_purchase_y_redempt_floorean():
    inv = _inv()
    inv._purchase("TRX", 16.5618266480)
    inv._redempt("TRX", 16.5618266480)
    for p in inv.core.exchange_trade.earn:
        assert float(p["amt"]) <= 16.5618266480


# ---------- B (V1.9.14): primitivos okx-semantica extraídos ----------

class _FakeExB:
    """Fake más completo: balances + control de fallos + captura de params."""
    def __init__(self, earn=0.0, funding=0.0):
        self.params = []
        self.earn = earn
        self.funding = funding
        self.raise_transfer = False
        self.raise_purchase_msg = None
        self.raise_balance = False

    def private_post_asset_transfer(self, p):
        if self.raise_transfer:
            raise Exception("boom transfer")
        self.params.append(("transfer", p)); return {"code": "0"}

    def private_post_finance_savings_purchase_redempt(self, p):
        if p.get("side") == "purchase" and self.raise_purchase_msg:
            raise Exception(self.raise_purchase_msg)
        self.params.append((p.get("side"), p)); return {"code": "0"}

    def private_get_finance_savings_balance(self, p):
        if self.raise_balance:
            raise Exception("neterr")
        return {"data": [{"ccy": p["ccy"], "amt": str(self.earn)}]}

    def private_get_asset_balances(self, p):
        if self.raise_balance:
            raise Exception("neterr")
        return {"data": [{"ccy": p["ccy"], "availBal": str(self.funding)}]}


def test_okx_transfer_floorea_y_captura():
    ex = _FakeExB()
    assert main.okx_transfer(ex, "TRX", 16.5618266480, "6", "18")   # default 8 dec
    _, p = ex.params[-1]
    assert p["ccy"] == "TRX"
    assert p["amt"] == str(main.piso_monto(16.5618266480, 8))
    assert float(p["amt"]) <= 16.5618266480


def test_okx_transfer_decimales_param():
    ex = _FakeExB()
    main.okx_transfer(ex, "USDT", 16.56185, "6", "18", decimales=4)
    assert ex.params[-1][1]["amt"] == str(main.piso_monto(16.56185, 4))


def test_okx_transfer_falla_devuelve_false():
    ex = _FakeExB(); ex.raise_transfer = True
    assert main.okx_transfer(ex, "TRX", 1.0, "6", "18") is False


def test_okx_earn_purchase_marca_unsupported():
    ex = _FakeExB(); ex.raise_purchase_msg = "58001 currency not supported"
    marcados = []
    assert main.okx_earn_purchase(ex, "DOGE", 5.0, on_unsupported=marcados.append) is False
    assert marcados == ["DOGE"]


def test_okx_earn_purchase_sin_callback_no_revienta():
    ex = _FakeExB(); ex.raise_purchase_msg = "58001"
    assert main.okx_earn_purchase(ex, "DOGE", 5.0) is False   # on_unsupported None → no revienta


def test_okx_saldo_earn_parsea_y_falla_cero():
    assert main.okx_saldo_earn(_FakeExB(earn=123.5), "USDT") == 123.5
    ex = _FakeExB(); ex.raise_balance = True
    assert main.okx_saldo_earn(ex, "USDT") == 0.0


def test_okx_saldo_funding_parsea_y_falla_cero():
    assert main.okx_saldo_funding(_FakeExB(funding=77.25), "TRX") == 77.25
    ex = _FakeExB(); ex.raise_balance = True
    assert main.okx_saldo_funding(ex, "TRX") == 0.0


def test_delegacion_tesoreria_4dec_inventario_8dec():
    # Tras el refactor, la tesorería (USDT) sigue en 4 dec; el inventario (ccy) en 8. La
    # duplicación se fue, pero el decimal por-ccy se PRESERVA (no verificable contra OKX vivo).
    t = _teso(); i = _inv()
    t._transferir(1.123456789, main.OKX_CUENTA_FUNDING, main.OKX_CUENTA_TRADING)
    i._transfer("TRX", 1.123456789, main.OKX_CUENTA_FUNDING, main.OKX_CUENTA_TRADING)
    assert t.core.exchange_trade.transfers[-1]["amt"] == str(main.piso_monto(1.123456789, 4))
    assert i.core.exchange_trade.transfers[-1]["amt"] == str(main.piso_monto(1.123456789, 8))
