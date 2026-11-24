"""Tests del núcleo PURO de agregación del colector de microflujo (sin red, sin ccxt.pro)."""
import colector_microflujo as cm


# ============================ OBI (order book imbalance) ============================
def test_obi_balanceado_cero():
    assert abs(cm.obi([[100.0, 5.0]], [[101.0, 5.0]])) < 1e-9


def test_obi_bid_pesado_positivo():
    # más volumen en bids → presión compradora → OBI > 0
    r = cm.obi([[100.0, 5.0], [99.0, 3.0]], [[101.0, 2.0], [102.0, 2.0]])
    assert abs(r - (8.0 - 4.0) / 12.0) < 1e-9
    assert r > 0


def test_obi_ask_pesado_negativo():
    r = cm.obi([[100.0, 1.0]], [[101.0, 9.0]])
    assert r < 0


def test_obi_vacio_cero():
    assert cm.obi([], []) == 0.0


# ============================ AgregadorMinuto: trades ============================
def test_trades_cvd_y_conteo():
    ag = cm.AgregadorMinuto("BTC-USDT-SWAP")
    base = 1_000_000 * 60000  # minuto entero
    ag.procesar({"tipo": "trade", "ts": base + 1000, "side": "buy", "px": 100.0, "sz": 1.0})
    ag.procesar({"tipo": "trade", "ts": base + 2000, "side": "sell", "px": 101.0, "sz": 2.0})
    ag.procesar({"tipo": "trade", "ts": base + 3000, "side": "buy", "px": 99.0, "sz": 1.0})
    bar = ag.cerrar()
    assert bar["cvd_delta"] == 1.0 - 2.0 + 1.0           # +1 -2 +1 = 0.0
    assert bar["n_buys"] == 2 and bar["n_sells"] == 1
    assert bar["vol"] == 4.0
    assert bar["open"] == 100.0 and bar["high"] == 101.0 and bar["low"] == 99.0 and bar["close"] == 99.0


def test_rollover_emite_barra_cerrada():
    ag = cm.AgregadorMinuto("BTC-USDT-SWAP")
    m0 = 1_000_000 * 60000
    m1 = 1_000_001 * 60000
    assert ag.procesar({"tipo": "trade", "ts": m0 + 1000, "side": "buy", "px": 100.0, "sz": 1.0}) is None
    # un trade en el minuto siguiente cierra y EMITE la barra del minuto anterior
    cerrada = ag.procesar({"tipo": "trade", "ts": m1 + 500, "side": "sell", "px": 102.0, "sz": 1.0})
    assert cerrada is not None
    assert cerrada["ts_min"] == 1_000_000
    assert cerrada["close"] == 100.0 and cerrada["n_buys"] == 1
    # la barra nueva ya arrancó con el segundo trade
    nueva = ag.cerrar()
    assert nueva["ts_min"] == 1_000_001 and nueva["n_sells"] == 1


def test_evento_viejo_se_descarta():
    ag = cm.AgregadorMinuto("BTC-USDT-SWAP")
    m1 = 1_000_001 * 60000
    m0 = 1_000_000 * 60000
    ag.procesar({"tipo": "trade", "ts": m1 + 1000, "side": "buy", "px": 100.0, "sz": 1.0})
    # evento de un minuto pasado → descartado, no rompe ni cambia la barra actual
    assert ag.procesar({"tipo": "trade", "ts": m0 + 1000, "side": "sell", "px": 50.0, "sz": 9.0}) is None
    bar = ag.cerrar()
    assert bar["ts_min"] == 1_000_001 and bar["n_sells"] == 0 and bar["vol"] == 1.0


# ============================ AgregadorMinuto: book / oi / funding / liq ============================
def test_book_promedia_obi():
    ag = cm.AgregadorMinuto("BTC-USDT-SWAP")
    base = 1_000_000 * 60000
    ag.procesar({"tipo": "book", "ts": base + 100, "bids": [[100.0, 6.0]], "asks": [[101.0, 2.0]]})
    ag.procesar({"tipo": "book", "ts": base + 200, "bids": [[100.0, 2.0]], "asks": [[101.0, 6.0]]})
    bar = ag.cerrar()
    # obi1 = (6-2)/8 = +0.5 ; obi2 = (2-6)/8 = -0.5 ; promedio 0, last -0.5
    assert abs(bar["obi_avg"]) < 1e-9
    assert abs(bar["obi_last"] - (-0.5)) < 1e-9


def test_oi_funding_liq_ultimo_y_acumulado():
    ag = cm.AgregadorMinuto("BTC-USDT-SWAP")
    base = 1_000_000 * 60000
    ag.procesar({"tipo": "oi", "ts": base + 10, "oi": 1000.0})
    ag.procesar({"tipo": "oi", "ts": base + 20, "oi": 1010.0})
    ag.procesar({"tipo": "funding", "ts": base + 30, "rate": 0.0001})
    ag.procesar({"tipo": "liq", "ts": base + 40, "notional": 50000.0})
    ag.procesar({"tipo": "liq", "ts": base + 50, "notional": 25000.0})
    bar = ag.cerrar()
    assert bar["oi"] == 1010.0                  # último
    assert bar["funding"] == 0.0001
    assert bar["n_liqs"] == 2 and bar["liq_notional"] == 75000.0


def test_cvd_acumulado_entre_barras():
    ag = cm.AgregadorMinuto("BTC-USDT-SWAP")
    m0, m1 = 1_000_000 * 60000, 1_000_001 * 60000
    ag.procesar({"tipo": "trade", "ts": m0 + 1, "side": "buy", "px": 100.0, "sz": 3.0})
    b0 = ag.procesar({"tipo": "trade", "ts": m1 + 1, "side": "buy", "px": 100.0, "sz": 2.0})
    assert b0["cvd_cum"] == 3.0
    b1 = ag.cerrar()
    assert b1["cvd_cum"] == 5.0                 # 3 + 2 acumulado
