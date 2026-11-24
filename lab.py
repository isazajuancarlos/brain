"""Banco standalone de medición de features candidatas (ver lab_features_spec.md).

Mide/compara/evalúa los hallazgos de la investigación de bots open-source/comerciales con el rigor
del bot: robustez por mitades + gate apareado (LCB>0) + fee-proof + por-cosecha → veredicto
DESPLEGAR/MEDIR-MÁS/DESCARTAR. NO toca bot/DB/capital/.env. Importa primitivos probados de entrenar.py.

Tanda 1 (V1.9.2): inventory-skew, vol-spacing, protections, threshold-rebalance, edge-positioning.
Tanda 2 (V1.9.3): infinity-grid (capturar el bull), funding-farm (basis trade delta-neutral).

Uso:
    python lab.py --all --dias 730
    python lab.py --rebalance --dias 730 --pares BTC/USDT,ETH/USDT,LTC/USDT
"""
import math

from entrenar import _bars_15m, _regimen_estado_por_ts, sim_grid_ladder

PARES_LADDER_DEFAULT = ["LTC/USDT", "ADA/USDT", "BTC/USDT", "SOL/USDT", "XRP/USDT"]
PARES_REBAL_DEFAULT = ["BTC/USDT", "ETH/USDT", "LTC/USDT"]
EARN_DAILY = 0.05 / 365.0
LADDER_KW = dict(spacing_pct=2.0, n_rungs=5, stop_pct=15.0, flatten_on_exit=True, fee=0.0008)


def _ccxt_binance():
    import ccxt
    return ccxt.binance({"enableRateLimit": True,
                         "options": {"defaultType": "spot", "adjustForTimeDifference": True}})


# =============================================================================
# NÚCLEO EVALUADOR — el rigor del bot, agnóstico del hallazgo.
# =============================================================================
def lcb_apareado(deltas, z=1.645):
    """δ = variante − baseline por unidad pareada. Devuelve media y límite inferior de confianza
    (LCB = mean − z·se) con n real. Significativo ⟺ lcb>0. Defensivo: n<2 → lcb≤0."""
    n = len(deltas)
    if n < 2:
        m = deltas[0] if n == 1 else 0.0
        return {"mean": m, "se": float("inf"), "n": n, "lcb": min(0.0, m) if n == 1 else 0.0}
    mean = sum(deltas) / n
    var = sum((d - mean) ** 2 for d in deltas) / (n - 1)
    se = math.sqrt(var / n)
    return {"mean": mean, "se": se, "n": n, "lcb": mean - z * se}


def veredicto(full, mitad1, mitad2, lcb, fee_taker=None):
    """Aplica el rigor del bot a un hallazgo normalizado.
    full/mitad1/mitad2: dicts con net, realiz, opcional trab. lcb: float|None (None = sin baseline
    pareable, ej. protections). fee_taker: dict|None (None = sin chequeo de fee)."""
    robusto = (mitad1["net"] > 0 and mitad2["net"] > 0 and full["realiz"] > 0)
    significativo = None if lcb is None else (lcb > 0)
    fee_proof = (fee_taker is None) or (fee_taker["realiz"] > 0)
    por_cosecha = (full["realiz"] > 0 and full["realiz"] >= full.get("trab", 0.0))
    if (not robusto) or (significativo is False):
        v = "DESCARTAR"
    elif robusto and por_cosecha and fee_proof and (significativo is True or significativo is None):
        v = "DESPLEGAR"
    else:
        v = "MEDIR-MÁS"
    return {"robusto": robusto, "significativo": significativo, "fee_proof": fee_proof,
            "por_cosecha": por_cosecha, "veredicto": v}


def fmt_veredicto(nombre, d, edge):
    sig = {True: "sig✓", False: "sig✗", None: "sig—"}[d["significativo"]]
    return (f"  {nombre:<22} edge {edge:+6.2f}%  "
            f"{'rob✓' if d['robusto'] else 'rob✗'} {sig} "
            f"{'fee✓' if d['fee_proof'] else 'fee✗'} {'cos✓' if d['por_cosecha'] else 'cos✗'}  "
            f"→ {d['veredicto']}")


def agregar_deltas(rows):
    """Promedia net/realiz/trab de una lista de resultados normalizados (por par)."""
    if not rows:
        return {"net": 0.0, "realiz": 0.0, "trab": 0.0}
    n = len(rows)
    return {"net": round(sum(r["net"] for r in rows) / n, 2),
            "realiz": round(sum(r["realiz"] for r in rows) / n, 2),
            "trab": round(sum(r.get("trab", 0.0) for r in rows) / n, 2)}


def _norm_ladder(r):
    return {"net": r["net_pct"], "realiz": r["realizado_pct"], "trab": r["inv_trabado_pct"]}


# =============================================================================
# SIM_REBALANCE — estrategia NUEVA (portfolio-level), threshold vs periódico vs HODL vs earn.
# =============================================================================
def sim_rebalance(closes_por_activo, modo="umbral", umbral_pct=15.0, periodo=30,
                  fee=0.001, earn_daily=EARN_DAILY, usdt_target=0.0):
    """PURO. Portfolio target equal-weight sobre los activos + leg USDT (usdt_target a earn).
    modo 'umbral': rebalancea al target cuando algún peso se desvía > umbral_pct (relativo) del target.
    modo 'periodico': cada `periodo` días. Cada rebalanceo paga fee sobre el turnover."""
    syms = list(closes_por_activo.keys())
    n = min((len(v) for v in closes_por_activo.values()), default=0)
    if n < 2 or not syms:
        return {"net_pct": 0.0, "n_rebal": 0, "max_dd": 0.0, "fin_pesos": {}}
    cripto_target = (1.0 - usdt_target) / len(syms)
    target = {s: cripto_target for s in syms}
    target["_USDT"] = usdt_target
    val = {s: target[s] for s in syms}
    val["_USDT"] = usdt_target
    n_rebal = 0
    peak = 1.0
    max_dd = 0.0

    def equity():
        return sum(val.values())

    def rebalancear(eq):
        nonlocal n_rebal
        turnover = 0.0
        for k in val:
            objetivo = target[k] * eq
            turnover += abs(objetivo - val[k])
            val[k] = objetivo
        costo = (turnover / 2.0) * fee
        val["_USDT"] -= costo
        n_rebal += 1

    for i in range(1, n):
        for s in syms:
            prev = closes_por_activo[s][i - 1]
            val[s] *= (closes_por_activo[s][i] / prev) if prev else 1.0
        val["_USDT"] *= (1 + earn_daily)
        eq = equity()
        disparar = False
        if eq > 0:
            if modo == "umbral":
                for k in val:
                    if target[k] > 0 and abs(val[k] / eq - target[k]) > (umbral_pct / 100.0) * target[k]:
                        disparar = True
                        break
            elif modo == "periodico" and i % periodo == 0:
                disparar = True
        if disparar:
            rebalancear(eq)
        eq = equity()
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak if peak > 0 else 0.0)
    eq = equity()
    return {"net_pct": round((eq - 1.0) * 100, 2), "n_rebal": n_rebal,
            "max_dd": round(max_dd * 100, 2),
            "fin_pesos": {k: round(v / eq, 3) for k, v in val.items()} if eq > 0 else {}}


# =============================================================================
# SIM_INFINITY_GRID — grid sin techo (Pionex), value-target floating, single-asset vs cash.
# El ángulo "capturar el bull": en vez de congelar en TENDENCIA_UP, mantiene exposición y vende
# fracciones en cada +step (lockea ganancia) y re-compra en cada −step. Cede upside vs HODL pero
# cushiona el bear con el cash cosechado. El cash ocioso rinde earn.
# =============================================================================
def sim_infinity_grid(closes, step_pct=5.0, sell_frac=0.2, fee=0.0, earn_daily=EARN_DAILY,
                      capital0=1.0):
    """PURO. Arranca 100% en el activo; ref flota. Si px ≥ ref·(1+step) → vende sell_frac de las
    unidades (a cash), ref=px. Si px ≤ ref·(1−step) → compra con sell_frac del cash, ref=px.
    El cash rinde earn_daily por barra. Devuelve net_pct, n_sells, n_buys, expo_final."""
    if len(closes) < 2:
        return {"net_pct": 0.0, "n_sells": 0, "n_buys": 0, "expo_final": 0.0}
    px0 = closes[0]
    qty = capital0 / px0
    cash = 0.0
    ref = px0
    n_sells = n_buys = 0
    up = 1 + step_pct / 100.0
    dn = 1 - step_pct / 100.0
    for px in closes[1:]:
        cash *= (1 + earn_daily)
        if px >= ref * up and qty > 0:
            vender = qty * sell_frac
            cash += vender * px * (1 - fee)
            qty -= vender
            ref = px
            n_sells += 1
        elif px <= ref * dn and cash > 0:
            gastar = cash * sell_frac
            qty += (gastar / px) * (1 - fee)
            cash -= gastar
            ref = px
            n_buys += 1
    equity = cash + qty * closes[-1]
    expo = (qty * closes[-1]) / equity if equity > 0 else 0.0
    return {"net_pct": round((equity / capital0 - 1) * 100, 2), "n_sells": n_sells,
            "n_buys": n_buys, "expo_final": round(expo, 3)}


# =============================================================================
# SIM_FUNDING_FARM — basis trade delta-neutral (long spot + short perp), cobra funding.
# Yield ESTRUCTURAL (como earn), NO direccional. Distinto del ARB matado (ese multi-venue/señal).
# Mide el yield NETO de funding para ver si el premio justifica romper el envelope (perp 24/7 +
# liquidación + coin-margin). El leg spot puede rendir earn en paralelo (no se cuenta acá, sería bonus).
# =============================================================================
def funding_apr(rates):
    """APR del funding (8h → 3/día × 365). rates = lista de tasas por período de 8h."""
    if not rates:
        return 0.0
    return (sum(rates) / len(rates)) * 3 * 365 * 100


def sim_funding_farm(funding_series, fee=0.0005, earn_daily=EARN_DAILY):
    """PURO. Delta-neutral: short perp + long spot. Recibe funding cuando rate>0, paga cuando <0.
    El precio se cancela entre las patas (neutral). funding_series = [(ts_ms, rate), ...] (8h).
    net = gross_funding_apr − fees de entrada/salida amortizados (2 patas)."""
    if not funding_series:
        return {"gross_apr": 0.0, "net_apr": 0.0, "pct_pos": 0.0, "n": 0}
    rates = [r for _, r in funding_series]
    gross = funding_apr(rates)
    n = len(rates)
    dias = max(1.0, n / 3.0)
    # 2 patas (spot+perp), entrada+salida = 4 fills de fee; amortizado al período → APR
    costo_apr = (4 * fee) * (365.0 / dias) * 100
    pct_pos = 100.0 * sum(1 for r in rates if r > 0) / n
    return {"gross_apr": round(gross, 2), "net_apr": round(gross - costo_apr, 2),
            "pct_pos": round(pct_pos, 1), "n": n}


# =============================================================================
# OVERLAYS DE PROTECTIONS (puros) + helpers de los adaptadores
# =============================================================================
def lowprofit_overlay(realiz_por_ventana, umbral=0.0):
    """nº de ventanas cuyo realiz quedó <= umbral (se 'desactivaría' el par esa ventana)."""
    return sum(1 for r in realiz_por_ventana if r <= umbral)


def maxdd_overlay(equity_curve, max_dd_pct=10.0):
    """nº de cruces del drawdown sobre max_dd_pct (cada cruce = 1 disparo; re-arma al recuperar
    la mitad del umbral)."""
    if not equity_curve:
        return 0
    peak = equity_curve[0]
    disparos = 0
    armado = True
    for e in equity_curve:
        peak = max(peak, e)
        dd = (peak - e) / peak * 100 if peak > 0 else 0.0
        if dd > max_dd_pct and armado:
            disparos += 1
            armado = False
        elif dd <= max_dd_pct * 0.5:
            armado = True
    return disparos


def rebal_edge(net_variante, net_baseline):
    return round(net_variante - net_baseline, 2)


def mejor_vol_label(resultados):
    robustos = {k: v for k, v in resultados.items() if v["rob"]}
    if robustos:
        return max(robustos, key=lambda k: robustos[k]["net"])
    return "fijo"


def pesos_edge(net_por_par):
    syms = list(net_por_par)
    if not syms:
        return {}
    pos = {s: max(0.0, net_por_par[s]) for s in syms}
    tot = sum(pos.values())
    if tot <= 0:
        return {s: 1.0 / len(syms) for s in syms}
    return {s: pos[s] / tot for s in syms}


# =============================================================================
# ADAPTADORES (con red, vía entrenar._bars_15m). Devuelven (nombre, dict_veredicto, edge).
# =============================================================================
_CACHE_RANGO = {}
_CACHE_DIR = ".cache_lab"


def _rango_mask(ex, sym, dias):
    """Barras 15m + máscara RANGO por régimen PROPIO del símbolo. Cacheado en proceso Y en disco:
    un barrido de K variantes sobre N pares hacía K·N descargas de ~70k barras (media hora de red
    para medir lo mismo, y la fricción es la razón por la que una medición no se repite). La clave
    incluye `dias` porque la ventana cambia el resultado. El caché es de DATOS DE MERCADO PÚBLICOS
    (no toca quantum.db ni credenciales) y es descartable: borrar .cache_lab/ solo cuesta la
    re-descarga. Para forzar datos frescos: rm -rf .cache_lab"""
    import json
    import os
    clave = (sym, dias)
    if clave in _CACHE_RANGO:
        return _CACHE_RANGO[clave]
    # JSON, no pickle: son listas de números y booleanos, y deserializar datos de disco no tiene
    # por qué poder ejecutar código.
    ruta = os.path.join(_CACHE_DIR, f"{sym.replace('/', '_')}_{dias}d.json")
    if os.path.exists(ruta):
        try:
            with open(ruta) as fh:
                bars, rango = json.load(fh)
            _CACHE_RANGO[clave] = (bars, rango)
            return _CACHE_RANGO[clave]
        except Exception as e:                      # caché corrupto: se re-descarga, no se muere
            print(f"  caché ilegible ({ruta}): {e} — re-descargando")
    bars, ts = _bars_15m(ex, sym, dias)
    est = _regimen_estado_por_ts(ex, dias, sym=sym)
    dato = (bars, [est.get((t // 86400000) * 86400000) == "RANGO" for t in ts])
    _CACHE_RANGO[clave] = dato
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(ruta, "w") as fh:
            json.dump(dato, fh)
    except Exception as e:
        print(f"  no se pudo cachear {ruta}: {e}")
    return dato


def _closes_diarios(ex, sym, dias):
    bars, ts = _bars_15m(ex, sym, dias)
    por_dia = {}
    for b, t in zip(bars, ts):
        por_dia[t // 86400000] = b["close"]
    return [por_dia[k] for k in sorted(por_dia)]


def eval_rebalance(dias=730, pares=None):
    syms = pares or PARES_REBAL_DEFAULT
    ex = _ccxt_binance()
    closes = {}
    for s in syms:
        try:
            c = _closes_diarios(ex, s, dias)
            if len(c) > 50:
                closes[s] = c
        except Exception as e:
            print(f"  rebalance: {s} sin datos ({e})")
    n = min((len(v) for v in closes.values()), default=0)
    if n < 50:
        return ("rebalance-umbral15", veredicto({"net": 0, "realiz": 0}, {"net": 0, "realiz": 0},
                                                {"net": 0, "realiz": 0}, lcb=None), 0.0)
    closes = {k: v[-n:] for k, v in closes.items()}
    mid = n // 2
    h1 = {k: v[:mid] for k, v in closes.items()}
    h2 = {k: v[mid:] for k, v in closes.items()}
    full = sim_rebalance(closes, modo="umbral", umbral_pct=15.0)
    m1 = sim_rebalance(h1, umbral_pct=15.0)
    m2 = sim_rebalance(h2, umbral_pct=15.0)
    hodl = sim_rebalance(closes, modo="umbral", umbral_pct=1e9)
    hodl1 = sim_rebalance(h1, umbral_pct=1e9)
    hodl2 = sim_rebalance(h2, umbral_pct=1e9)
    earn = sim_rebalance(closes, usdt_target=1.0)
    norm = lambda r: {"net": r["net_pct"], "realiz": r["net_pct"], "trab": 0.0}
    lcb = lcb_apareado([m1["net_pct"] - hodl1["net_pct"], m2["net_pct"] - hodl2["net_pct"]])["lcb"]
    d = veredicto(norm(full), norm(m1), norm(m2), lcb=lcb)
    edge = rebal_edge(full["net_pct"], hodl["net_pct"])
    print(f"\n[REBALANCE] umbral15% net {full['net_pct']:+.2f}% (1ª {m1['net_pct']:+.2f}/2ª "
          f"{m2['net_pct']:+.2f}, rebal {full['n_rebal']}) vs HODL {hodl['net_pct']:+.2f}% "
          f"vs earn {earn['net_pct']:+.2f}% | maxDD {full['max_dd']:.1f}%")
    return ("rebalance-umbral15", d, edge)


def eval_skew(dias=730, pares=None):
    syms = pares or PARES_LADDER_DEFAULT
    ex = _ccxt_binance()
    full_b, full_s, m1s, m2s, deltas = [], [], [], [], []
    for s in syms:
        try:
            bars, rango = _rango_mask(ex, s, dias)
        except Exception as e:
            print(f"  skew: {s} sin datos ({e})"); continue
        if len(bars) < 300:
            continue
        mid = len(bars) // 2
        base = sim_grid_ladder(bars, rango, **LADDER_KW)
        skew = sim_grid_ladder(bars, rango, inventory_skew=0.5, **LADDER_KW)
        full_b.append(_norm_ladder(base)); full_s.append(_norm_ladder(skew))
        m1s.append(_norm_ladder(sim_grid_ladder(bars[:mid], rango[:mid], inventory_skew=0.5, **LADDER_KW)))
        m2s.append(_norm_ladder(sim_grid_ladder(bars[mid:], rango[mid:], inventory_skew=0.5, **LADDER_KW)))
        deltas.append(skew["net_pct"] - base["net_pct"])
        print(f"  skew {s}: base net {base['net_pct']:+.2f}% → skew {skew['net_pct']:+.2f}% "
              f"(δ {skew['net_pct']-base['net_pct']:+.2f})")
    fb, fs = agregar_deltas(full_b), agregar_deltas(full_s)
    lcb = lcb_apareado(deltas)["lcb"]
    d = veredicto(fs, agregar_deltas(m1s), agregar_deltas(m2s), lcb=lcb)
    edge = round(fs["net"] - fb["net"], 2)
    return ("inventory-skew", d, edge)


def eval_vol(dias=730, pares=None):
    syms = pares or PARES_LADDER_DEFAULT
    ex = _ccxt_binance()
    fijo_full, atr_full, atr1, atr2, deltas = [], [], [], [], []
    for s in syms:
        try:
            bars, rango = _rango_mask(ex, s, dias)
        except Exception as e:
            print(f"  vol: {s} sin datos ({e})"); continue
        if len(bars) < 300:
            continue
        mid = len(bars) // 2
        f = sim_grid_ladder(bars, rango, atr_spacing=False, **LADDER_KW)
        a = sim_grid_ladder(bars, rango, atr_spacing=True, **LADDER_KW)
        fijo_full.append(_norm_ladder(f)); atr_full.append(_norm_ladder(a))
        atr1.append(_norm_ladder(sim_grid_ladder(bars[:mid], rango[:mid], atr_spacing=True, **LADDER_KW)))
        atr2.append(_norm_ladder(sim_grid_ladder(bars[mid:], rango[mid:], atr_spacing=True, **LADDER_KW)))
        deltas.append(a["net_pct"] - f["net_pct"])
        print(f"  vol {s}: fijo {f['net_pct']:+.2f}% → atr {a['net_pct']:+.2f}% "
              f"(δ {a['net_pct']-f['net_pct']:+.2f})")
    ff, af = agregar_deltas(fijo_full), agregar_deltas(atr_full)
    lcb = lcb_apareado(deltas)["lcb"]
    d = veredicto(af, agregar_deltas(atr1), agregar_deltas(atr2), lcb=lcb)
    edge = round(af["net"] - ff["net"], 2)
    return ("vol-spacing", d, edge)


def eval_protections(dias=730, pares=None):
    syms = pares or PARES_LADDER_DEFAULT
    ex = _ccxt_binance()
    nets, m1, m2, total_lp = [], [], [], 0
    for s in syms:
        try:
            bars, rango = _rango_mask(ex, s, dias)
        except Exception as e:
            print(f"  protections: {s} sin datos ({e})"); continue
        if len(bars) < 300:
            continue
        mid = len(bars) // 2
        r = sim_grid_ladder(bars, rango, **LADDER_KW)
        nets.append(_norm_ladder(r))
        m1.append(_norm_ladder(sim_grid_ladder(bars[:mid], rango[:mid], **LADDER_KW)))
        m2.append(_norm_ladder(sim_grid_ladder(bars[mid:], rango[mid:], **LADDER_KW)))
        win = 30 * 96
        realiz_win = []
        for j in range(0, len(bars), win):
            seg, segr = bars[j:j + win], rango[j:j + win]
            if len(seg) > 50:
                realiz_win.append(sim_grid_ladder(seg, segr, **LADDER_KW)["realizado_pct"])
        lp = lowprofit_overlay(realiz_win, umbral=0.0)
        total_lp += lp
        print(f"  protections {s}: net {r['net_pct']:+.2f}%, lowprofit desactivaría "
              f"{lp}/{len(realiz_win)} ventanas")
    full = agregar_deltas(nets)
    d = veredicto(full, agregar_deltas(m1), agregar_deltas(m2), lcb=None)
    print(f"  → LowProfitPairs desactivaría {total_lp} ventanas-par; MaxDrawdown global = baranda.")
    return ("protections", d, 0.0)


def eval_infinity(dias=730, pares=None):
    """Mide el infinity grid (capturar el bull sin congelar) vs HODL y vs earn-siempre, sobre el
    período completo (incluye el run alcista y el bear). Robustez por mitades."""
    syms = pares or ["BTC/USDT", "ETH/USDT", "LTC/USDT"]
    ex = _ccxt_binance()
    fulls, m1, m2, deltas_vs_hodl = [], [], [], []
    for s in syms:
        try:
            closes = _closes_diarios(ex, s, dias)
        except Exception as e:
            print(f"  infinity: {s} sin datos ({e})"); continue
        if len(closes) < 50:
            continue
        mid = len(closes) // 2
        r = sim_infinity_grid(closes, fee=0.0008)
        hodl = (closes[-1] / closes[0] - 1) * 100
        earn = ((1 + EARN_DAILY) ** len(closes) - 1) * 100
        r1 = sim_infinity_grid(closes[:mid], fee=0.0008)
        r2 = sim_infinity_grid(closes[mid:], fee=0.0008)
        h1 = (closes[mid - 1] / closes[0] - 1) * 100
        h2 = (closes[-1] / closes[mid] - 1) * 100
        fulls.append({"net": r["net_pct"], "realiz": r["net_pct"], "trab": 0.0})
        m1.append({"net": r1["net_pct"], "realiz": r1["net_pct"]})
        m2.append({"net": r2["net_pct"], "realiz": r2["net_pct"]})
        deltas_vs_hodl.append(r["net_pct"] - hodl)
        print(f"  infinity {s}: net {r['net_pct']:+.1f}% vs HODL {hodl:+.1f}% vs earn {earn:+.1f}% "
              f"(sells {r['n_sells']}, expo_fin {r['expo_final']:.2f})")
    full = agregar_deltas(fulls)
    # baseline = HODL (pareado por par): ¿el infinity grid mejora sobre comprar y aguantar?
    lcb = lcb_apareado(deltas_vs_hodl)["lcb"]
    d = veredicto(full, agregar_deltas(m1), agregar_deltas(m2), lcb=lcb)
    edge = round(sum(deltas_vs_hodl) / len(deltas_vs_hodl), 2) if deltas_vs_hodl else 0.0
    return ("infinity-grid", d, edge)


def _ccxt_binance_perp():
    import ccxt
    return ccxt.binance({"enableRateLimit": True,
                         "options": {"defaultType": "future", "adjustForTimeDifference": True}})


def eval_funding(dias=730, pares=None):
    """Mide el yield NETO del funding farm delta-neutral vs earn (5%). Decide si el premio justifica
    romper el envelope (perp 24/7 + liquidación). Importa _funding_historico de entrenar."""
    from entrenar import _funding_historico
    syms = pares or ["BTC/USDT", "ETH/USDT"]
    ex = _ccxt_binance_perp()
    earn_apr = EARN_DAILY * 365 * 100
    nets, m1, m2, deltas_vs_earn = [], [], [], []
    for s in syms:
        perp = s + ":USDT" if ":" not in s else s
        serie = _funding_historico(ex, perp, dias)
        if not serie:
            print(f"  funding: {perp} sin datos"); continue
        mid = len(serie) // 2
        r = sim_funding_farm(serie, fee=0.0005)
        r1 = sim_funding_farm(serie[:mid], fee=0.0005)
        r2 = sim_funding_farm(serie[mid:], fee=0.0005)
        nets.append({"net": r["net_apr"], "realiz": r["net_apr"], "trab": 0.0})
        m1.append({"net": r1["net_apr"], "realiz": r1["net_apr"]})
        m2.append({"net": r2["net_apr"], "realiz": r2["net_apr"]})
        deltas_vs_earn.append(r["net_apr"] - earn_apr)
        print(f"  funding {perp}: gross {r['gross_apr']:+.2f}% APR, net {r['net_apr']:+.2f}% APR vs "
              f"earn {earn_apr:.1f}% ({r['pct_pos']:.0f}% períodos +, n={r['n']})")
    full = agregar_deltas(nets)
    lcb = lcb_apareado(deltas_vs_earn)["lcb"]
    d = veredicto(full, agregar_deltas(m1), agregar_deltas(m2), lcb=lcb)
    edge = round(sum(deltas_vs_earn) / len(deltas_vs_earn), 2) if deltas_vs_earn else 0.0
    print("  ⚠ envelope: el funding farm exige perp short 24/7 (liquidación + coin-margin) — el "
          "edge debe SUPERAR earn por mucho para justificar romper el non-24/7.")
    return ("funding-farm", d, edge)


def eval_edge(dias=730, pares=None):
    syms = pares or PARES_LADDER_DEFAULT
    ex = _ccxt_binance()
    net_par, net_par1, net_par2 = {}, {}, {}
    for s in syms:
        try:
            bars, rango = _rango_mask(ex, s, dias)
        except Exception as e:
            print(f"  edge: {s} sin datos ({e})"); continue
        if len(bars) < 300:
            continue
        mid = len(bars) // 2
        net_par[s] = sim_grid_ladder(bars, rango, **LADDER_KW)["net_pct"]
        net_par1[s] = sim_grid_ladder(bars[:mid], rango[:mid], **LADDER_KW)["net_pct"]
        net_par2[s] = sim_grid_ladder(bars[mid:], rango[mid:], **LADDER_KW)["net_pct"]
    if not net_par:
        return ("edge-positioning", veredicto({"net": 0, "realiz": 0}, {"net": 0, "realiz": 0},
                                              {"net": 0, "realiz": 0}, lcb=None), 0.0)
    w = pesos_edge(net_par)
    eq = lambda d: sum(d[s] / len(d) for s in d)
    ew = lambda d: sum(w[s] * d[s] for s in d)
    full_ew, full_eq = ew(net_par), eq(net_par)
    m1 = {"net": ew(net_par1), "realiz": ew(net_par1)}
    m2 = {"net": ew(net_par2), "realiz": ew(net_par2)}
    lcb = lcb_apareado([ew(net_par1) - eq(net_par1), ew(net_par2) - eq(net_par2)])["lcb"]
    d = veredicto({"net": full_ew, "realiz": full_ew, "trab": 0.0}, m1, m2, lcb=lcb)
    edge = round(full_ew - full_eq, 2)
    print(f"\n[EDGE] edge-weight net {full_ew:+.2f}% vs equal-weight {full_eq:+.2f}% | pesos "
          f"{ {s: round(v, 2) for s, v in w.items()} }")
    return ("edge-positioning", d, edge)


# =============================================================================
# REGIME-FLOW (tanda 3) — ¿OI + funding llevan info de cambio de régimen que NO está en el precio?
# Test de CONTENIDO INFORMACIONAL (no estrategia → evita el sobreajuste de timing): coeficiente de
# información (IC) de señales no-precio vs baseline momentum-de-precio, pareado + walk-forward.
# Datos: OKX da ~180d de OI diario gratis (Binance solo 30d) + funding (ya lo bajamos). NO toca capital.
# =============================================================================
def signo(x):
    return 1 if x > 0 else (-1 if x < 0 else 0)


def pct_change(series):
    """% día-a-día. Devuelve len-1 valores (alineados al día t: cambio t-1→t)."""
    return [(series[i] / series[i - 1] - 1.0) * 100 if series[i - 1] else 0.0
            for i in range(1, len(series))]


def oi_signal(d_oi, d_px):
    """Señal direccional de Open Interest (continuación). OI subiendo = dinero/convicción NUEVA:
    OI↑ & precio↑ → +1 (longs nuevos), OI↑ & precio↓ → −1 (shorts nuevos). OI↓ = posiciones
    cerrándose, sin convicción nueva → 0. (El read 'OI↑+precio = posicionamiento genuino'.)"""
    if d_oi > 0 and d_px > 0:
        return 1
    if d_oi > 0 and d_px < 0:
        return -1
    return 0


def funding_signal(f, hi=0.0005):
    """Señal de FADE por funding extremo (contrarian). funding muy positivo = longs apiñados →
    fade short (−1); muy negativo = shorts apiñados → fade long (+1); normal → 0."""
    if f > hi:
        return -1
    if f < -hi:
        return 1
    return 0


def forward_returns(closes, k=3):
    """Retorno forward de k días (causal: el de t mira t→t+k). Devuelve len-k valores."""
    return [(closes[t + k] / closes[t] - 1.0) * 100 if closes[t] else 0.0
            for t in range(len(closes) - k)]


def ic_deltas(senales, fwd):
    """Contribución pareada por día = señal·retorno-forward. mean = coeficiente de información.
    LCB>0 (sobre estos δ) ⟺ la señal predice la dirección del forward."""
    n = min(len(senales), len(fwd))
    return [float(senales[t] * fwd[t]) for t in range(n)]


def _flujo_diario_okx(sym, dias=180):
    """Baja de OKX (gratis): cierre diario + OI diario + funding (8h→media diaria), alineado por día.
    Devuelve (closes, ois, fundings) como listas paralelas por día común."""
    import ccxt
    from entrenar import _funding_historico
    ex = ccxt.okx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    perp = sym if ":" in sym else sym + ":USDT"
    ohlcv = ex.fetch_ohlcv(perp, "1d", limit=300)
    px = {c[0] // 86400000: c[4] for c in ohlcv}
    oi_raw = ex.fetch_open_interest_history(perp, "1d", limit=300)
    # OKX da OI en USD (openInterestValue = contratos×precio); el de-priced se hace en el adaptador.
    oi = {o["timestamp"] // 86400000: (o.get("openInterestValue") or o.get("openInterestAmount"))
          for o in oi_raw if (o.get("openInterestValue") or o.get("openInterestAmount"))}
    fund_raw = _funding_historico(ex, perp, dias)
    fday = {}
    for ts, rate in fund_raw:
        fday.setdefault(ts // 86400000, []).append(rate)
    fund = {d: sum(v) / len(v) for d, v in fday.items()}
    dias_comunes = sorted(set(px) & set(oi) & set(fund))
    return ([px[d] for d in dias_comunes], [oi[d] for d in dias_comunes],
            [fund[d] for d in dias_comunes])


def eval_regime_flow(dias=180, pares=None):
    """Mide si OI y funding predicen el forward MÁS ALLÁ del momentum de precio (IC pareado +
    walk-forward). Baseline OI = momentum puro sign(Δpx); funding se mide vs 0. Responde la pregunta
    del 'grial': ¿hay info de cambio de régimen NO contenida en el precio? (con las 2 patas gratis)."""
    syms = pares or ["BTC/USDT", "ETH/USDT"]
    K = 3
    oi_d, oi_d1, oi_d2, fu_d, fu_d1, fu_d2 = [], [], [], [], [], []
    for s in syms:
        try:
            closes, ois, funds = _flujo_diario_okx(s, dias)
        except Exception as e:
            print(f"  regime-flow: {s} sin datos ({e})"); continue
        if len(closes) < 40:
            print(f"  regime-flow: {s} muestra fina ({len(closes)}d)"); continue
        d_px = pct_change(closes)          # len-1, alineado al día t (1..)
        d_oi_val = pct_change(ois)         # %cambio del OI en USD (= contratos×precio)
        # DE-PRECIAR: el OI-USD mete el precio; restar Δpx aísla el cambio de POSICIONES real
        d_oi = [d_oi_val[i] - d_px[i] for i in range(min(len(d_oi_val), len(d_px)))]
        fwd = forward_returns(closes, K)   # len-K, día t mira t→t+K
        # alinear: la señal del día t (usa Δ t-1→t, índice t-1 en d_px) predice fwd[t]
        n = min(len(d_px), len(fwd) - 1)
        px_sig = [signo(d_px[t]) for t in range(n)]
        oi_sig = [oi_signal(d_oi[t], d_px[t]) for t in range(n)]
        fu_sig = [funding_signal(funds[t + 1]) for t in range(n)]
        fwd_al = [fwd[t + 1] for t in range(n)]
        # forward windows NO solapados (paso K) → δ INDEPENDIENTES para un LCB honesto (las ventanas
        # solapadas autocorrelacionan los δ e inflan la significancia). OI más allá del precio:
        idx = list(range(0, n, K))
        d_oi_add = [(oi_sig[t] - px_sig[t]) * fwd_al[t] for t in idx]
        d_fu = [fu_sig[t] * fwd_al[t] for t in idx]
        mid = len(d_oi_add) // 2
        oi_d += d_oi_add; oi_d1 += d_oi_add[:mid]; oi_d2 += d_oi_add[mid:]
        fu_d += d_fu; fu_d1 += d_fu[:mid]; fu_d2 += d_fu[mid:]
        ic_oi = sum(d_oi_add) / len(d_oi_add) if d_oi_add else 0.0
        ic_fu = sum(d_fu) / len(d_fu) if d_fu else 0.0
        print(f"  {s}: IC-OI(>precio) {ic_oi:+.3f}%/op  IC-funding {ic_fu:+.3f}%/op  "
              f"(n={len(d_oi_add)} ventanas indep., k={K})")
    res = []
    for nombre, dd, d1, d2 in [("regime-flow-OI", oi_d, oi_d1, oi_d2),
                               ("regime-flow-funding", fu_d, fu_d1, fu_d2)]:
        ic = sum(dd) / len(dd) if dd else 0.0
        ic1 = sum(d1) / len(d1) if d1 else 0.0
        ic2 = sum(d2) / len(d2) if d2 else 0.0
        lcb = lcb_apareado(dd)["lcb"]
        d = veredicto({"net": ic, "realiz": ic, "trab": 0.0},
                      {"net": ic1, "realiz": ic1}, {"net": ic2, "realiz": ic2}, lcb=lcb)
        res.append((nombre, d, round(ic, 3)))
    print("  ⚠ ~180d daily = poder estadístico LIMITADO → primer vistazo, no veredicto final. Y el IC "
          "debe superar ~0.16% (2×fee) para ser operable, no solo >0.")
    return res


# =============================================================================
# MICROFLUJO (tanda 4 / Etapa 3 del 'santo grial') — ¿CVD y OBI predicen el forward MÁS ALLÁ del
# precio? La 3ª pata (flujo agresivo tick) que el histórico-gratis NO daba, recolectada en vivo por
# colector_microflujo.py → microflujo.db (barras 1m, market-wide, público). Test de IC pareado con
# ventanas NO solapadas (la lección de la tanda 3: las solapadas autocorrelan e inflan el LCB).
# NO toca el bot/quantum.db/capital — solo lee microflujo.db. Corre cuando haya ≥4-8 semanas de datos.
# =============================================================================
def cvd_divergence(cvd, d_px):
    """Señal de DIVERGENCIA flujo-vs-precio (lo que el precio solo no dice). cvd = flujo agresivo
    firmado del minuto (compra−venta); d_px = % cambio del precio en ese minuto.
    precio↑ pero CVD vende → −1 (rally sin flujo, débil); precio↓ pero CVD compra → +1 (dip absorbido).
    Flujo que CONFIRMA el precio → 0 (no aporta más que el momentum). Sin Δprecio → 0."""
    if d_px > 0 and cvd < 0:
        return -1
    if d_px < 0 and cvd > 0:
        return 1
    return 0


def obi_signal(obi, hi=0.05):
    """Señal direccional de Order Book Imbalance (no-precio). obi>hi = bids pesados → +1;
    obi<−hi = asks pesados → −1; dentro de la banda → 0."""
    if obi > hi:
        return 1
    if obi < -hi:
        return -1
    return 0


def _deltas_microflujo(closes, cvds, obis, k=15, obi_hi=0.05):
    """PURO. Núcleo de medición: arma los δ pareados (señal·forward) de CVD y OBI sobre ventanas
    forward de k minutos NO solapadas. Alineación causal: la señal al cierre de la barra t (CVD vs
    Δprecio t-1→t, u OBI del minuto t) predice el retorno t→t+k. t válido = 1..n-1-k, muestreado a
    paso k → ventanas independientes. Devuelve (d_cvd, d_obi)."""
    n = len(closes)
    d_cvd, d_obi = [], []
    for t in range(1, n - k, k):
        if not closes[t - 1] or not closes[t]:
            continue
        d_px = (closes[t] / closes[t - 1] - 1.0) * 100
        fwd = (closes[t + k] / closes[t] - 1.0) * 100
        d_cvd.append(cvd_divergence(cvds[t], d_px) * fwd)
        d_obi.append(obi_signal(obis[t], obi_hi) * fwd)
    return d_cvd, d_obi


def _cargar_microflujo(db, sym):
    """Lee microflujo.db (solo lectura) las barras 1m de un símbolo, ordenadas por minuto.
    Devuelve (closes, cvds, obis) en paralelo; descarta barras sin close (minutos sin trades);
    None de cvd/obi → 0.0. NO escribe nada."""
    import sqlite3
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        rows = c.execute("""SELECT close, cvd_delta, obi_avg FROM flujo_1m
                            WHERE symbol=? ORDER BY ts_min ASC""", (sym,)).fetchall()
    finally:
        c.close()
    closes, cvds, obis = [], [], []
    for close, cvd, obi in rows:
        if close is None:
            continue
        closes.append(float(close))
        cvds.append(float(cvd) if cvd is not None else 0.0)
        obis.append(float(obi) if obi is not None else 0.0)
    return closes, cvds, obis


def _simbolos_microflujo(db):
    """Símbolos distintos presentes en microflujo.db (vacío si la DB no existe / no tiene barras)."""
    import os
    import sqlite3
    if not os.path.exists(db):
        return []
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        return [r[0] for r in c.execute(
            "SELECT DISTINCT symbol FROM flujo_1m ORDER BY symbol")]
    except sqlite3.OperationalError:
        return []                               # tabla aún no creada
    finally:
        c.close()


def eval_microflujo(db="microflujo.db", pares=None, k=15, obi_hi=0.05):
    """Mide si CVD (divergencia flujo-vs-precio) y OBI predicen el forward de k minutos MÁS ALLÁ del
    precio — IC pareado + ventanas NO solapadas + robustez por mitades + LCB. Responde la pregunta del
    'santo grial' con la pata que faltaba (flujo agresivo market-wide). Sin datos → [] (no crashea)."""
    syms = pares or _simbolos_microflujo(db)
    if not syms:
        print("  microflujo: sin datos — corré colector_microflujo.py y volvé en ≥4-8 semanas.")
        return []
    cvd_d, cvd_d1, cvd_d2, obi_d, obi_d1, obi_d2 = [], [], [], [], [], []
    for s in syms:
        try:
            closes, cvds, obis = _cargar_microflujo(db, s)
        except Exception as e:
            print(f"  microflujo: {s} sin datos ({e})"); continue
        if len(closes) < 2 * k + 2:
            print(f"  microflujo: {s} muestra fina ({len(closes)} barras 1m)"); continue
        dc, do = _deltas_microflujo(closes, cvds, obis, k, obi_hi)
        mc, mo = len(dc) // 2, len(do) // 2
        cvd_d += dc; cvd_d1 += dc[:mc]; cvd_d2 += dc[mc:]
        obi_d += do; obi_d1 += do[:mo]; obi_d2 += do[mo:]
        ic_c = sum(dc) / len(dc) if dc else 0.0
        ic_o = sum(do) / len(do) if do else 0.0
        print(f"  {s}: IC-CVD {ic_c:+.3f}%/op  IC-OBI {ic_o:+.3f}%/op  "
              f"(n={len(dc)} ventanas indep., k={k}m)")
    res = []
    for nombre, dd, d1, d2 in [("microflujo-CVD", cvd_d, cvd_d1, cvd_d2),
                               ("microflujo-OBI", obi_d, obi_d1, obi_d2)]:
        ic = sum(dd) / len(dd) if dd else 0.0
        ic1 = sum(d1) / len(d1) if d1 else 0.0
        ic2 = sum(d2) / len(d2) if d2 else 0.0
        lcb = lcb_apareado(dd)["lcb"]
        d = veredicto({"net": ic, "realiz": ic, "trab": 0.0},
                      {"net": ic1, "realiz": ic1}, {"net": ic2, "realiz": ic2}, lcb=lcb)
        res.append((nombre, d, round(ic, 3)))
    print("  ⚠ El IC debe superar ~0.16% (2×fee) para ser operable, no solo >0. Verde → alimenta el "
          "Cerebro de Régimen (no un bot predictivo nuevo); rojo → se mata documentado.")
    return res


# =============================================================================
# TANDA 5 (SP4, ladder_requote_spec.md) — RE-QUOTE: que la escalera siga al mercado.
# La escalera se ancla al precio del día que empezó el RANGO y no se mueve más; en 365d el precio
# quedó por encima de TODA la escalera el 64-76% de las barras operables. Mide si re-cotizar por
# deriva (order_refresh_tolerance) o por tiempo (order_refresh_time) recupera cosecha — o solo hace
# churn y compra más arriba. Ambos son δ vs el MISMO ladder sin el knob (varianza común cancelada).
# =============================================================================
REQUOTE_DRIFTS = [1.5, 3.0, 5.0]
REQUOTE_REFRESH = [96, 384]        # barras de 15m: 1 día, 4 días


def baseline_mediano(nets):
    """Baseline honesto de la cartera: el par MEDIANO, no el mejor. Comparar una cartera contra el
    mejor par de la muestra es compararla contra una elección hecha con retrovisor — la trampa que
    convirtió a edge-positioning en un falso positivo (tanda 1). Con n par, toma el de arriba del
    medio (conservador: exige más a la cartera)."""
    if not nets:
        return 0.0
    return sorted(nets)[len(nets) // 2]


def _eval_ladder_variante(dias, pares, etiqueta, kw_variante, verbose=True):
    """Motor común: corre el ladder base vs base+kw_variante por par, arma el veredicto pareado.
    Devuelve (nombre, dict_veredicto, edge) + un detalle por par para imprimir."""
    syms = pares or PARES_LADDER_DEFAULT
    ex = _ccxt_binance()
    base_full, var_full, v1, v2, deltas, detalle = [], [], [], [], [], []
    for s in syms:
        try:
            bars, rango = _rango_mask(ex, s, dias)
        except Exception as e:
            print(f"  {etiqueta}: {s} sin datos ({e})"); continue
        if len(bars) < 300:
            continue
        mid = len(bars) // 2
        base = sim_grid_ladder(bars, rango, **LADDER_KW)
        var = sim_grid_ladder(bars, rango, **LADDER_KW, **kw_variante)
        base_full.append(_norm_ladder(base)); var_full.append(_norm_ladder(var))
        v1.append(_norm_ladder(sim_grid_ladder(bars[:mid], rango[:mid], **LADDER_KW, **kw_variante)))
        v2.append(_norm_ladder(sim_grid_ladder(bars[mid:], rango[mid:], **LADDER_KW, **kw_variante)))
        deltas.append(var["net_pct"] - base["net_pct"])
        detalle.append((s, base, var))
        if verbose:
            print(f"  {etiqueta} {s}: base net {base['net_pct']:+.2f}% rt={base['n_roundtrips']:<4} "
                  f"→ var net {var['net_pct']:+.2f}% rt={var['n_roundtrips']:<4} "
                  f"(δnet {var['net_pct']-base['net_pct']:+.2f}, δtrab "
                  f"{var['inv_trabado_pct']-base['inv_trabado_pct']:+.2f})")
    bf, vf = agregar_deltas(base_full), agregar_deltas(var_full)
    lcb = lcb_apareado(deltas)["lcb"]
    d = veredicto(vf, agregar_deltas(v1), agregar_deltas(v2), lcb=lcb)
    return (etiqueta, d, round(vf["net"] - bf["net"], 2)), detalle


def eval_requote(dias=730, pares=None):
    """F1 — re-quote por DERIVA. Barre el umbral; el que se reporta es el mejor por δnet, y se
    imprime el barrido entero para que se vea si el resultado depende de elegir el umbral con
    retrovisor (la trampa que hundió a edge-positioning en la tanda 1)."""
    print(f"\n[REQUOTE-DERIVA] barrido de umbral sobre {len(pares or PARES_LADDER_DEFAULT)} pares")
    mejor, resultados = None, []
    for drift in REQUOTE_DRIFTS:
        res, det = _eval_ladder_variante(dias, pares, f"requote-drift{drift:g}",
                                         {"recenter_drift_pct": drift})
        resultados.append(res)
        print(f"   → drift {drift:g}%: edge {res[2]:+.2f}%  {res[1]['veredicto']}")
        if mejor is None or res[2] > mejor[2]:
            mejor = res
    return mejor, resultados


def eval_refresh(dias=730, pares=None):
    """F2 — re-quote por TIEMPO (order_refresh_time de Hummingbot, en barras de 15m)."""
    print(f"\n[REQUOTE-TIEMPO] barrido de periodo sobre {len(pares or PARES_LADDER_DEFAULT)} pares")
    mejor, resultados = None, []
    for nb in REQUOTE_REFRESH:
        res, det = _eval_ladder_variante(dias, pares, f"requote-cada{nb//96}d", {"refresh_bars": nb})
        resultados.append(res)
        print(f"   → cada {nb//96}d: edge {res[2]:+.2f}%  {res[1]['veredicto']}")
        if mejor is None or res[2] > mejor[2]:
            mejor = res
    return mejor, resultados


CARTERA_VENTANAS = 8       # trimestres sobre 730d


def eval_cartera(dias=730, pares=None, n_ventanas=CARTERA_VENTANAS):
    """F3 — CARTERA de ladders vs un solo par. SP2 escaneó pares independientes; nunca se midió la
    cartera. Con el gate por régimen propio cada par se congela en un momento distinto, así que N
    escaleras con capital/N deberían tener menos tiempo muerto.

    Dos decisiones de rigor, ambas lecciones ya pagadas por este banco:
      · el baseline NO es 'el mejor par' (elegido con retrovisor) sino el MEDIANO (`baseline_mediano`);
      · el pareado es por VENTANA de tiempo (≈trimestres, n≈8), no por mitades: un hallazgo
        portfolio-level da UNA curva, y parear por mitades deja n=2, que la tanda 1 ya declaró
        demasiado fino para creerle a un LCB.
    """
    syms = pares or PARES_LADDER_DEFAULT
    ex = _ccxt_binance()
    datos = {}
    for s in syms:
        try:
            bars, rango = _rango_mask(ex, s, dias)
        except Exception as e:
            print(f"  cartera: {s} sin datos ({e})"); continue
        if len(bars) >= 300:
            datos[s] = (bars, rango)
    if len(datos) < 2:
        return ("cartera-ladders", veredicto({"net": 0, "realiz": 0}, {"net": 0, "realiz": 0},
                                             {"net": 0, "realiz": 0}, lcb=None), 0.0)
    n = len(datos)
    cap = 1000.0 / n                      # capital repartido: la cartera no apalanca
    completo = {s: sim_grid_ladder(b, r, capital0=cap, **LADDER_KW) for s, (b, r) in datos.items()}
    solo = {s: sim_grid_ladder(b, r, capital0=1000.0, **LADDER_KW) for s, (b, r) in datos.items()}
    prom = lambda d, k: round(sum(v[k] for v in d.values()) / n, 2)
    cart = {"net": prom(completo, "net_pct"), "realiz": prom(completo, "realizado_pct"),
            "trab": prom(completo, "inv_trabado_pct")}

    # --- pareado por ventanas: δ_v = cartera(v) − par mediano(v) ---
    largo = min(len(b) for b, _ in datos.values())
    paso = largo // n_ventanas
    deltas, cart_v = [], []
    for v in range(n_ventanas):
        ini, fin = v * paso, (v + 1) * paso if v < n_ventanas - 1 else largo
        nets = {}
        for s, (b, r) in datos.items():
            nets[s] = sim_grid_ladder(b[ini:fin], r[ini:fin], capital0=cap, **LADDER_KW)["net_pct"]
        c = sum(nets.values()) / n
        cart_v.append(c)
        deltas.append(c - baseline_mediano(list(nets.values())))
    mitad = n_ventanas // 2
    m1 = {"net": round(sum(cart_v[:mitad]) / mitad, 2), "realiz": cart["realiz"]}
    m2 = {"net": round(sum(cart_v[mitad:]) / (n_ventanas - mitad), 2), "realiz": cart["realiz"]}

    mediano = baseline_mediano([r["net_pct"] for r in solo.values()])
    for s, r in completo.items():
        print(f"  cartera {s}: net {r['net_pct']:+.2f}% realiz {r['realizado_pct']:+.2f}% "
              f"rt={r['n_roundtrips']}")
    est = lcb_apareado(deltas)
    print(f"  → CARTERA de {n}: net {cart['net']:+.2f}% ({sum(r['n_roundtrips'] for r in completo.values())} "
          f"round-trips) vs par MEDIANO solo: {mediano:+.2f}% "
          f"({baseline_mediano([r['n_roundtrips'] for r in solo.values()])} round-trips)")
    print(f"     pareado por {n_ventanas} ventanas: δ medio {est['mean']:+.3f}%, LCB {est['lcb']:+.3f} "
          f"(n={est['n']}); mitades {m1['net']:+.2f}/{m2['net']:+.2f}")
    d = veredicto(cart, m1, m2, lcb=est["lcb"])
    return ("cartera-ladders", d, round(cart["net"] - mediano, 2))


# =============================================================================
# CLI + tabla consolidada
# =============================================================================
_ORDEN = {"DESPLEGAR": 0, "MEDIR-MÁS": 1, "DESCARTAR": 2}


def tabla_consolidada(resultados):
    filas = sorted(resultados, key=lambda r: (_ORDEN.get(r[1]["veredicto"], 9), -r[2]))
    lineas = ["", "=" * 72,
              "TABLA-VEREDICTO CONSOLIDADA (rigor: robusto + significativo + fee + cosecha)"]
    for nombre, d, edge in filas:
        lineas.append(fmt_veredicto(nombre, d, edge))
    lineas.append("-" * 72)
    lineas.append("  DESPLEGAR = robusto ∧ por-cosecha ∧ fee-proof ∧ (significativo o sin-baseline)")
    lineas.append("  'DESPLEGAR' habilita una discusión de runtime/canary, NO auto-gradúa.")
    return "\n".join(lineas)


def main():
    import argparse
    p = argparse.ArgumentParser(
        description="lab.py — banco de medición de features (no toca bot/DB/capital)")
    p.add_argument("--all", action="store_true", help="corre los 5 hallazgos + tabla consolidada")
    p.add_argument("--skew", action="store_true")
    p.add_argument("--vol", action="store_true")
    p.add_argument("--protections", action="store_true")
    p.add_argument("--rebalance", action="store_true")
    p.add_argument("--edge", action="store_true")
    p.add_argument("--infinity", action="store_true", help="infinity grid (capturar el bull) vs HODL/earn")
    p.add_argument("--funding", action="store_true", help="funding farm delta-neutral vs earn")
    p.add_argument("--regime-flow", dest="regime_flow", action="store_true",
                   help="¿OI+funding predicen el régimen más allá del precio? (IC pareado, OKX ~180d)")
    p.add_argument("--requote", action="store_true",
                   help="SP4 F1: re-cotizar la escalera por DERIVA del precio (barre umbral)")
    p.add_argument("--refresh", action="store_true",
                   help="SP4 F2: re-cotizar la escalera por TIEMPO (barre periodo)")
    p.add_argument("--cartera", action="store_true",
                   help="SP4 F3: cartera de N ladders (capital/N) vs un solo par")
    p.add_argument("--microflujo", action="store_true",
                   help="Etapa 3: ¿CVD+OBI predicen el forward más allá del precio? (lee microflujo.db "
                        "del colector; NO entra en --all). Auto-descubre símbolos si no pasás --pares.")
    p.add_argument("--mf-db", dest="mf_db", type=str, default="microflujo.db",
                   help="ruta de microflujo.db (default microflujo.db)")
    p.add_argument("--mf-k", dest="mf_k", type=int, default=15, help="horizonte forward en minutos")
    p.add_argument("--dias", type=int, default=730)
    p.add_argument("--pares", type=str, default=None, help="CSV ej. BTC/USDT,ETH/USDT")
    args = p.parse_args()
    pares = [x.strip() for x in args.pares.split(",")] if args.pares else None
    res = []
    quiere = lambda f: args.all or f
    if quiere(args.skew):        res.append(eval_skew(args.dias, pares))
    if quiere(args.vol):         res.append(eval_vol(args.dias, pares))
    if quiere(args.protections): res.append(eval_protections(args.dias, pares))
    if quiere(args.rebalance):   res.append(eval_rebalance(args.dias, pares))
    if quiere(args.edge):        res.append(eval_edge(args.dias, pares))
    if quiere(args.infinity):    res.append(eval_infinity(args.dias, pares))
    if quiere(args.funding):     res.append(eval_funding(args.dias, pares))
    # SP4: se reportan TODAS las variantes del barrido, no solo la mejor (un barrido del que solo se
    # publica el ganador es sobreajuste con buena presentación — la lección de edge-positioning).
    if quiere(args.requote):     res.extend(eval_requote(args.dias, pares)[1])
    if quiere(args.refresh):     res.extend(eval_refresh(args.dias, pares)[1])
    if quiere(args.cartera):     res.append(eval_cartera(args.dias, pares))
    if quiere(args.regime_flow): res.extend(eval_regime_flow(min(args.dias, 180), pares))
    # microflujo NO entra en --all: otra fuente de datos (microflujo.db), sin red, requiere el colector.
    if args.microflujo:          res.extend(eval_microflujo(db=args.mf_db, pares=pares, k=args.mf_k))
    if not res:
        p.print_help(); return
    print(tabla_consolidada(res))


if __name__ == "__main__":
    main()
