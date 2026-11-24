import math

import lab
import entrenar


# ============================ Task 1: núcleo evaluador ============================
def test_lcb_apareado_positivo_consistente():
    r = lab.lcb_apareado([0.5, 0.5, 0.5, 0.5])
    assert r["n"] == 4
    assert abs(r["mean"] - 0.5) < 1e-9
    assert r["lcb"] > 0


def test_lcb_apareado_alta_varianza_no_significativo():
    r = lab.lcb_apareado([5.0, -5.0, 4.0, -4.0, 5.0, -5.0])
    assert abs(r["mean"]) < 1.0
    assert r["lcb"] <= 0


def test_lcb_apareado_n1_defensivo():
    r = lab.lcb_apareado([1.0])
    assert r["n"] == 1
    assert r["lcb"] <= 0


def test_lcb_apareado_vacio():
    r = lab.lcb_apareado([])
    assert r["n"] == 0
    assert r["lcb"] <= 0


def test_veredicto_desplegar():
    full = {"net": 3.0, "realiz": 3.5, "trab": 0.5}
    d = lab.veredicto(full, {"net": 1.0, "realiz": 1.0}, {"net": 2.0, "realiz": 2.0},
                      lcb=0.4, fee_taker={"net": 2.5, "realiz": 3.0})
    assert d["robusto"] and d["por_cosecha"] and d["fee_proof"] and d["significativo"]
    assert d["veredicto"] == "DESPLEGAR"


def test_veredicto_descartar_no_robusto():
    full = {"net": 1.0, "realiz": 1.0, "trab": 0.0}
    d = lab.veredicto(full, {"net": -1.0, "realiz": -1.0}, {"net": 2.0, "realiz": 2.0}, lcb=0.4)
    assert not d["robusto"]
    assert d["veredicto"] == "DESCARTAR"


def test_veredicto_descartar_no_significativo():
    full = {"net": 3.0, "realiz": 3.0, "trab": 0.0}
    d = lab.veredicto(full, {"net": 1.0, "realiz": 1.0}, {"net": 2.0, "realiz": 2.0}, lcb=-0.1)
    assert d["significativo"] is False
    assert d["veredicto"] == "DESCARTAR"


def test_veredicto_medir_mas_significativo_none():
    full = {"net": 2.0, "realiz": 2.0, "trab": 0.0}
    d = lab.veredicto(full, {"net": 1.0, "realiz": 1.0}, {"net": 1.0, "realiz": 1.0},
                      lcb=None, fee_taker=None)
    assert d["significativo"] is None
    assert d["veredicto"] == "DESPLEGAR"


def test_veredicto_medir_mas_beta():
    full = {"net": 3.0, "realiz": 0.5, "trab": 2.5}
    d = lab.veredicto(full, {"net": 1.0, "realiz": 0.2}, {"net": 2.0, "realiz": 0.3}, lcb=0.4)
    assert not d["por_cosecha"]
    assert d["veredicto"] == "MEDIR-MÁS"


# ============================ Task 2: sim_rebalance ============================
def _flat(n, v=100.0):
    return [v] * n


def test_rebalance_sin_desvio_sin_trades():
    closes = {"A": _flat(50), "B": _flat(50)}
    r = lab.sim_rebalance(closes, modo="umbral", umbral_pct=15.0, fee=0.001)
    assert r["n_rebal"] == 0


def test_rebalance_umbral_menor_mas_trades():
    n = 200
    closes = {"A": [100 * (1.01 ** i) for i in range(n)],
              "B": [100 * (0.99 ** i) for i in range(n)]}
    chico = lab.sim_rebalance(closes, umbral_pct=5.0, fee=0.0)
    grande = lab.sim_rebalance(closes, umbral_pct=25.0, fee=0.0)
    assert chico["n_rebal"] >= grande["n_rebal"]


def test_rebalance_fee_dragea():
    n = 200
    closes = {"A": [100 * (1.01 ** i) for i in range(n)],
              "B": [100 * (0.99 ** i) for i in range(n)]}
    sin_fee = lab.sim_rebalance(closes, umbral_pct=5.0, fee=0.0)
    con_fee = lab.sim_rebalance(closes, umbral_pct=5.0, fee=0.002)
    assert con_fee["net_pct"] <= sin_fee["net_pct"]


def test_rebalance_earn_sobre_usdt():
    closes = {"A": _flat(365)}
    r = lab.sim_rebalance(closes, usdt_target=1.0, fee=0.0)
    assert r["net_pct"] > 0
    assert r["n_rebal"] == 0


# ============================ Task 3: inventory_skew (no-regresión) ============================
def _bars_sinteticos(n=300):
    bars = []
    for i in range(n):
        px = 100.0 + 5.0 * math.sin(i / 7.0)
        bars.append({"close": px, "high": px + 0.5, "low": px - 0.5, "atr_pct": 1.0})
    return bars


def test_skew_cero_es_no_regresion():
    bars = _bars_sinteticos()
    op = [True] * len(bars)
    base = entrenar.sim_grid_ladder(bars, op, spacing_pct=2.0, n_rungs=5)
    skew0 = entrenar.sim_grid_ladder(bars, op, spacing_pct=2.0, n_rungs=5, inventory_skew=0.0)
    assert base == skew0


def test_skew_positivo_cambia_resultado():
    bars = _bars_sinteticos()
    op = [True] * len(bars)
    base = entrenar.sim_grid_ladder(bars, op, spacing_pct=2.0, n_rungs=5)
    skewed = entrenar.sim_grid_ladder(bars, op, spacing_pct=2.0, n_rungs=5, inventory_skew=0.5)
    assert (skewed["n_roundtrips"], skewed["net_pct"]) != (base["n_roundtrips"], base["net_pct"])


# ============================ Task 4: eval_rebalance helper ============================
def test_rebal_edge():
    assert lab.rebal_edge(12.0, 5.0) == 7.0
    assert lab.rebal_edge(-3.0, 4.0) == -7.0


# ============================ Task 5: eval_skew helper ============================
def test_agregar_deltas_promedia():
    rows = [{"net": 2.0, "realiz": 1.0, "trab": 0.5},
            {"net": 4.0, "realiz": 3.0, "trab": 1.5}]
    a = lab.agregar_deltas(rows)
    assert a["net"] == 3.0 and a["realiz"] == 2.0 and a["trab"] == 1.0


def test_agregar_deltas_vacio():
    a = lab.agregar_deltas([])
    assert a == {"net": 0.0, "realiz": 0.0, "trab": 0.0}


def test_agregar_deltas_sin_trab_tolerante():
    # rows sin clave 'trab' (infinity/funding) → trab=0.0, no crashea
    a = lab.agregar_deltas([{"net": 2.0, "realiz": 2.0}, {"net": 4.0, "realiz": 4.0}])
    assert a["net"] == 3.0 and a["trab"] == 0.0


# ============================ Task 6: eval_vol helper ============================
def test_mejor_vol_label_elige_robusto_mayor():
    res = {"fijo": {"net": 3.0, "rob": True}, "atr_a": {"net": 5.0, "rob": False},
           "atr_b": {"net": 4.0, "rob": True}}
    assert lab.mejor_vol_label(res) == "atr_b"


def test_mejor_vol_label_sin_robustos_cae_a_fijo():
    res = {"fijo": {"net": 1.0, "rob": False}, "atr_a": {"net": 2.0, "rob": False}}
    assert lab.mejor_vol_label(res) == "fijo"


# ============================ Task 7: protections overlays ============================
def test_lowprofit_overlay_desactiva_bajo_umbral():
    assert lab.lowprofit_overlay([-1.0, -2.0, 0.5], umbral=0.0) == 2


def test_lowprofit_overlay_limpio_no_dispara():
    assert lab.lowprofit_overlay([1.0, 2.0, 0.5], umbral=0.0) == 0


def test_maxdd_overlay_dispara_en_caida():
    assert lab.maxdd_overlay([1.0, 1.2, 1.0, 1.05], max_dd_pct=10.0) >= 1


def test_maxdd_overlay_sin_caida_no_dispara():
    assert lab.maxdd_overlay([1.0, 1.01, 1.02, 1.03], max_dd_pct=10.0) == 0


# ============================ Task 8: edge positioning ============================
def test_pesos_edge_equal_si_empatan():
    p = lab.pesos_edge({"A": 2.0, "B": 2.0})
    assert abs(p["A"] - 0.5) < 1e-9 and abs(p["B"] - 0.5) < 1e-9


def test_pesos_edge_favorece_dominante():
    p = lab.pesos_edge({"A": 3.0, "B": 1.0})
    assert p["A"] > p["B"]
    assert abs(p["A"] + p["B"] - 1.0) < 1e-9


def test_pesos_edge_todo_negativo_equal_weight():
    p = lab.pesos_edge({"A": -1.0, "B": -2.0})
    assert abs(p["A"] - 0.5) < 1e-9 and abs(p["B"] - 0.5) < 1e-9


# ============================ Tanda 2: infinity grid ============================
def test_infinity_plano_sin_trades():
    # precio plano → ni sube ni baja un step → 0 sells/buys
    r = lab.sim_infinity_grid(_flat(100), step_pct=5.0, sell_frac=0.2, fee=0.0)
    assert r["n_sells"] == 0 and r["n_buys"] == 0


def test_infinity_sube_vende_y_queda_bajo_hodl():
    # alza monótona: el infinity grid vende fracciones (realiza) pero capta < HODL
    closes = [100 * (1.02 ** i) for i in range(120)]
    r = lab.sim_infinity_grid(closes, step_pct=5.0, sell_frac=0.2, fee=0.0)
    hodl = (closes[-1] / closes[0] - 1) * 100
    assert r["n_sells"] >= 1
    assert r["net_pct"] < hodl          # vende en la subida → cede upside


def test_infinity_fee_dragea():
    closes = [100 + 10 * math.sin(i / 5.0) for i in range(200)]
    sin_fee = lab.sim_infinity_grid(closes, fee=0.0)
    con_fee = lab.sim_infinity_grid(closes, fee=0.002)
    assert con_fee["net_pct"] <= sin_fee["net_pct"]


# ============================ Tanda 2: funding farm ============================
def test_funding_farm_positivo_rinde():
    # funding todo +0.01%/8h → net APR > 0
    serie = [(i * 28800000, 0.0001) for i in range(100)]
    r = lab.sim_funding_farm(serie, fee=0.0)
    assert r["gross_apr"] > 0 and r["net_apr"] > 0


def test_funding_farm_cero_neutro():
    serie = [(i * 28800000, 0.0) for i in range(100)]
    r = lab.sim_funding_farm(serie, fee=0.0)
    assert abs(r["gross_apr"]) < 1e-6


def test_funding_farm_fee_dragea():
    serie = [(i * 28800000, 0.0001) for i in range(100)]
    sin_fee = lab.sim_funding_farm(serie, fee=0.0)
    con_fee = lab.sim_funding_farm(serie, fee=0.001)
    assert con_fee["net_apr"] <= sin_fee["net_apr"]


def test_funding_farm_vacio_defensivo():
    r = lab.sim_funding_farm([], fee=0.0)
    assert r["gross_apr"] == 0.0 and r["net_apr"] == 0.0


# ============================ Tanda 3: regime-flow (OI + funding) ============================
def test_signo():
    assert lab.signo(0.5) == 1 and lab.signo(-0.5) == -1 and lab.signo(0.0) == 0


def test_pct_change():
    r = lab.pct_change([100.0, 110.0, 99.0])
    assert abs(r[0] - 10.0) < 1e-9 and abs(r[1] - (-10.0)) < 1e-9


def test_pct_change_corto():
    assert lab.pct_change([100.0]) == []


def test_oi_signal_cuadrantes():
    assert lab.oi_signal(5.0, 2.0) == 1     # OI↑ precio↑ = longs nuevos (continuación up)
    assert lab.oi_signal(5.0, -2.0) == -1   # OI↑ precio↓ = shorts nuevos (continuación down)
    assert lab.oi_signal(-5.0, 2.0) == 0    # OI↓ = sin convicción nueva → neutro
    assert lab.oi_signal(-5.0, -2.0) == 0


def test_funding_signal_fade():
    assert lab.funding_signal(0.001, hi=0.0005) == -1   # longs apiñados → fade short
    assert lab.funding_signal(-0.001, hi=0.0005) == 1   # shorts apiñados → fade long
    assert lab.funding_signal(0.0001, hi=0.0005) == 0


def test_forward_returns():
    r = lab.forward_returns([100.0, 110.0, 121.0], k=1)
    assert abs(r[0] - 10.0) < 1e-9 and abs(r[1] - 10.0) < 1e-9


def test_ic_deltas():
    d = lab.ic_deltas([1, -1], [2.0, -4.0])
    assert d == [2.0, 4.0]


def test_ic_deltas_cero_neutro():
    d = lab.ic_deltas([0, 1], [5.0, 3.0])
    assert d == [0.0, 3.0]


# ============================ Task 9: tabla consolidada ============================
def test_baseline_mediano_no_es_el_mejor():
    """El baseline de la cartera debe ser el par MEDIANO. Si alguien lo cambia por max() la cartera
    se compara contra una elección con retrovisor y todo edge se evapora; si lo cambia por min(),
    la cartera gana siempre. Este test fija la decisión."""
    nets = [-5.0, 0.5, 1.0, 3.0, 12.0]
    assert lab.baseline_mediano(nets) == 1.0
    assert lab.baseline_mediano(nets) != max(nets)
    assert lab.baseline_mediano(nets) != min(nets)


def test_baseline_mediano_par_toma_el_de_arriba():
    # n par: el de arriba del medio ⟹ le exige MÁS a la cartera (conservador)
    assert lab.baseline_mediano([1.0, 2.0, 3.0, 4.0]) == 3.0


def test_baseline_mediano_vacio():
    assert lab.baseline_mediano([]) == 0.0


def test_tabla_consolidada_ordena_por_veredicto_y_edge():
    res = [
        ("FEAT_MEDIR", {"robusto": True, "significativo": True, "fee_proof": True, "por_cosecha": True,
                        "veredicto": "MEDIR-MÁS"}, 1.0),
        ("FEAT_DESPLEGAR", {"robusto": True, "significativo": True, "fee_proof": True, "por_cosecha": True,
                            "veredicto": "DESPLEGAR"}, 0.5),
        ("FEAT_DESCARTAR", {"robusto": False, "significativo": False, "fee_proof": True, "por_cosecha": False,
                            "veredicto": "DESCARTAR"}, 9.0),
    ]
    out = lab.tabla_consolidada(res)
    # DESPLEGAR antes que MEDIR-MÁS antes que DESCARTAR, pese al edge alto del descartado
    assert out.index("FEAT_DESPLEGAR") < out.index("FEAT_MEDIR") < out.index("FEAT_DESCARTAR")


# ============================ Tanda 4: microflujo (CVD + OBI), Etapa 3 ============================
def test_cvd_divergence_cuadrantes():
    # precio sube pero CVD vende = divergencia bajista → -1 (rally sin flujo)
    assert lab.cvd_divergence(-50.0, 2.0) == -1
    # precio baja pero CVD compra = divergencia alcista → +1 (dip absorbido)
    assert lab.cvd_divergence(50.0, -2.0) == 1
    # flujo confirma el precio (ambos suben / ambos bajan) → 0 (no aporta más que el precio)
    assert lab.cvd_divergence(50.0, 2.0) == 0
    assert lab.cvd_divergence(-50.0, -2.0) == 0
    # sin movimiento de precio → 0
    assert lab.cvd_divergence(50.0, 0.0) == 0


def test_obi_signal_bandas():
    assert lab.obi_signal(0.3, hi=0.05) == 1     # bids pesados → presión compradora
    assert lab.obi_signal(-0.3, hi=0.05) == -1   # asks pesados → presión vendedora
    assert lab.obi_signal(0.02, hi=0.05) == 0    # dentro de la banda → neutro


def test_deltas_microflujo_cvd_predictivo():
    # serie oscilante; CVD construido para que la divergencia prediga el forward k=1
    closes = [100.0, 102.0, 100.0, 102.0, 100.0]
    cvds = [0.0, -5.0, 5.0, -5.0, 0.0]      # bajista en techos, alcista en pisos
    obis = [0.0, 0.0, 0.0, 0.0, 0.0]
    d_cvd, d_obi = lab._deltas_microflujo(closes, cvds, obis, k=1, obi_hi=0.05)
    assert len(d_cvd) == 3                   # t válido = 1,2,3
    assert all(d > 0 for d in d_cvd)         # la divergencia acertó cada vez
    assert sum(d_cvd) > 0
    assert sum(d_obi) == 0                    # OBI plano → señal 0 → δ 0


def test_deltas_microflujo_ventanas_no_solapadas():
    # con k=2 las ventanas forward NO se solapan: t = 1, 3, 5, ...
    closes = [100.0 + i for i in range(12)]
    cvds = [0.0] * 12
    obis = [0.5] * 12                          # OBI fuerte y constante
    d_cvd, d_obi = lab._deltas_microflujo(closes, cvds, obis, k=2, obi_hi=0.05)
    # t válido: 1..(n-1-k)=1..9, paso k=2 → t=1,3,5,7,9 = 5 ventanas independientes
    assert len(d_obi) == 5
    assert all(d > 0 for d in d_obi)          # OBI+ y precio sube → acierta


def test_cargar_microflujo_ordena_por_minuto(tmp_path):
    import colector_microflujo as cm
    db = str(tmp_path / "mf.db")
    conn = cm._conn(db)
    # insertar DESORDENADO; el loader debe devolver por ts_min ascendente
    for tsmin, px, cvd, ob in [(102, 100.0, 1.0, 0.2), (100, 98.0, -1.0, -0.1),
                               (101, 99.0, 2.0, 0.0)]:
        cm.guardar_barra(conn, {"ts_min": tsmin, "symbol": "BTC-USDT-SWAP",
                                "open": px, "high": px, "low": px, "close": px,
                                "vol": 1.0, "cvd_delta": cvd, "cvd_cum": cvd, "n_buys": 1, "n_sells": 0,
                                "obi_avg": ob, "obi_last": ob, "oi": None, "funding": None,
                                "n_liqs": 0, "liq_notional": 0.0})
    conn.close()
    closes, cvds, obis = lab._cargar_microflujo(db, "BTC-USDT-SWAP")
    assert closes == [98.0, 99.0, 100.0]      # ordenado por minuto 100,101,102
    assert cvds == [-1.0, 2.0, 1.0]
    assert obis == [-0.1, 0.0, 0.2]


def test_cargar_microflujo_obi_none_coalesce(tmp_path):
    import colector_microflujo as cm
    db = str(tmp_path / "mf2.db")
    conn = cm._conn(db)
    cm.guardar_barra(conn, {"ts_min": 100, "symbol": "ETH-USDT-SWAP",
                            "open": 50.0, "high": 50.0, "low": 50.0, "close": 50.0,
                            "vol": 1.0, "cvd_delta": None, "cvd_cum": 0.0, "n_buys": 0, "n_sells": 0,
                            "obi_avg": None, "obi_last": None, "oi": None, "funding": None,
                            "n_liqs": 0, "liq_notional": 0.0})
    conn.close()
    closes, cvds, obis = lab._cargar_microflujo(db, "ETH-USDT-SWAP")
    assert closes == [50.0] and cvds == [0.0] and obis == [0.0]   # None → 0.0


def test_eval_microflujo_sin_datos_devuelve_vacio(tmp_path):
    db = str(tmp_path / "vacia.db")            # no existe aún
    res = lab.eval_microflujo(db=db)
    assert res == []                           # sin barras → no veredicto, no crashea


def test_eval_microflujo_estructura(tmp_path):
    import colector_microflujo as cm
    db = str(tmp_path / "mf3.db")
    conn = cm._conn(db)
    for i in range(40):
        px = 100.0 + (i % 4)
        cm.guardar_barra(conn, {"ts_min": 1000 + i, "symbol": "BTC-USDT-SWAP",
                                "open": px, "high": px, "low": px, "close": px,
                                "vol": 1.0, "cvd_delta": (-5.0 if i % 2 else 5.0), "cvd_cum": 0.0,
                                "n_buys": 1, "n_sells": 1, "obi_avg": 0.1, "obi_last": 0.1,
                                "oi": None, "funding": None, "n_liqs": 0, "liq_notional": 0.0})
    conn.close()
    res = lab.eval_microflujo(db=db, pares=["BTC-USDT-SWAP"], k=2)
    nombres = [r[0] for r in res]
    assert "microflujo-CVD" in nombres and "microflujo-OBI" in nombres
    for nombre, d, ic in res:
        assert isinstance(nombre, str) and "veredicto" in d and isinstance(ic, float)
