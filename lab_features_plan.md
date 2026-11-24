# lab.py — Banco de medición de features — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Construir `lab.py`, un banco standalone que mide/compara/evalúa los 5 hallazgos de la
investigación de bots (inventory skew, vol-spacing, protections, threshold-rebalance, edge-positioning)
con el rigor del bot (robustez por mitades + gate apareado + fee-proof + por-cosecha), emitiendo un
veredicto DESPLEGAR/MEDIR-MÁS/DESCARTAR por hallazgo.

**Architecture:** `lab.py` standalone en la raíz; importa primitivos probados de `entrenar.py`
(`_bars_15m`, `_regimen_estado_por_ts`, `sim_grid_ladder`, `_atr_mult`) sin duplicarlos. Núcleo
evaluador puro + 5 adaptadores + CLI. `sim_grid_ladder` se extiende aditivamente con `inventory_skew=0.0`
(default = cero cambio). Read-only de mercado, no toca bot/DB/capital/.env.

**Tech Stack:** Python 3, ccxt (vía entrenar.py), pytest. Sin deps nuevas.

## Global Constraints

- **NO tocar bot/DB/capital/.env/secrets.** `lab.py` no escribe `quantum.db`, no coloca órdenes, no
  lee `.env`. Mismo envelope que `screener_pares.py`/`entrenar.py --*-sweep`.
- **Repo NO es git** → los pasos "Checkpoint" corren tests; no hay `git commit`.
- **Idioma español** en docs/comentarios/identificadores (convención del proyecto).
- **`sim_grid_ladder` solo se extiende aditivamente:** `inventory_skew=0.0` default ⟹ resultado
  idéntico bit-a-bit al actual (protege el canary del ladder vivo y `test_ladder_grid.py`).
- **No predictivo.** Nada de ML-árbol/timing/ballenas (linaje muerto).
- Tests sin red: barras sintéticas inyectadas; los adaptadores con red no se testean end-to-end.

---

### Task 1: Núcleo evaluador (`lcb_apareado`, `veredicto`, `fmt_veredicto`)

**Files:**
- Create: `lab.py`
- Test: `test_lab.py`

**Interfaces:**
- Produces:
  - `lcb_apareado(deltas: list[float], z: float = 1.645) -> dict` → `{"mean","se","n","lcb"}`
  - `veredicto(full: dict, mitad1: dict, mitad2: dict, lcb, fee_taker=None) -> dict`
    donde cada `dict` de resultado tiene claves `net`, `realiz`, opcional `trab`; `lcb` es float o None.
    Devuelve `{"robusto","significativo","fee_proof","por_cosecha","veredicto"}`.
  - `fmt_veredicto(nombre: str, d: dict, edge: float) -> str`

- [ ] **Step 1: Write the failing tests**

```python
# test_lab.py
import math
import lab


def test_lcb_apareado_positivo_consistente():
    # δ todos iguales y positivos → se=0 → lcb=mean>0
    r = lab.lcb_apareado([0.5, 0.5, 0.5, 0.5])
    assert r["n"] == 4
    assert abs(r["mean"] - 0.5) < 1e-9
    assert r["lcb"] > 0


def test_lcb_apareado_alta_varianza_no_significativo():
    # media ~0 con varianza alta → lcb < 0
    r = lab.lcb_apareado([5.0, -5.0, 4.0, -4.0, 5.0, -5.0])
    assert abs(r["mean"]) < 1.0
    assert r["lcb"] <= 0


def test_lcb_apareado_n1_defensivo():
    r = lab.lcb_apareado([1.0])
    assert r["n"] == 1
    assert r["lcb"] <= 0          # con n=1 no se afirma significancia


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
    # protections: sin baseline pareable (lcb=None) pero robusto+cosecha+fee → DESPLEGAR
    full = {"net": 2.0, "realiz": 2.0, "trab": 0.0}
    d = lab.veredicto(full, {"net": 1.0, "realiz": 1.0}, {"net": 1.0, "realiz": 1.0},
                      lcb=None, fee_taker=None)
    assert d["significativo"] is None
    assert d["veredicto"] == "DESPLEGAR"


def test_veredicto_medir_mas_beta():
    # robusto + significativo pero NO por cosecha (trab domina) → MEDIR-MÁS
    full = {"net": 3.0, "realiz": 0.5, "trab": 2.5}
    d = lab.veredicto(full, {"net": 1.0, "realiz": 0.2}, {"net": 2.0, "realiz": 0.3}, lcb=0.4)
    assert not d["por_cosecha"]
    assert d["veredicto"] == "MEDIR-MÁS"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest test_lab.py -x -q`
Expected: FAIL with `AttributeError: module 'lab' has no attribute 'lcb_apareado'` (o `ModuleNotFoundError`).

- [ ] **Step 3: Write minimal implementation**

```python
# lab.py
"""Banco standalone de medición de features candidatas (ver lab_features_spec.md).
Mide/compara/evalúa los hallazgos de la investigación de bots con el rigor del bot
(robustez por mitades + gate apareado + fee-proof + por-cosecha). NO toca bot/DB/capital/.env."""
import math


def lcb_apareado(deltas, z=1.645):
    """δ = variante − baseline por unidad pareada. Devuelve media y límite inferior de confianza
    (LCB = mean − z·se) con n real (Kish n = n con pesos unitarios). Significativo ⟺ lcb>0.
    Defensivo: n<2 → lcb≤0 (no se afirma significancia)."""
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest test_lab.py -q`
Expected: PASS (8 passed).

- [ ] **Step 5: Checkpoint** (repo no-git)

Run: `python -m pytest test_lab.py -q && python -c "import lab; print('ok')"`
Expected: tests green + `ok`.

---

### Task 2: `sim_rebalance` (simulador puro nuevo)

**Files:**
- Modify: `lab.py`
- Test: `test_lab.py`

**Interfaces:**
- Consumes: nada de tasks previas.
- Produces:
  - `sim_rebalance(closes_por_activo: dict[str,list[float]], modo="umbral", umbral_pct=15.0,
    periodo=30, fee=0.001, earn_daily=EARN_DAILY, usdt_target=0.0) -> dict`
    → `{"net_pct","n_rebal","max_dd","fin_pesos"}`. Asume mismos largos alineados por día.
  - Constante `EARN_DAILY = 0.05/365.0`.

- [ ] **Step 1: Write the failing tests**

```python
def _flat(n, v=100.0):
    return [v] * n


def test_rebalance_sin_desvio_sin_trades():
    # precios planos → pesos no se mueven → 0 rebalanceos
    closes = {"A": _flat(50), "B": _flat(50)}
    r = lab.sim_rebalance(closes, modo="umbral", umbral_pct=15.0, fee=0.001)
    assert r["n_rebal"] == 0


def test_rebalance_umbral_menor_mas_trades():
    import math
    # A sube, B baja → desvíos; umbral chico dispara ≥ que umbral grande
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
    # 100% USDT (usdt_target=1.0): no hay cripto, el net = earn compuesto > 0
    closes = {"A": _flat(365)}
    r = lab.sim_rebalance(closes, usdt_target=1.0, fee=0.0)
    assert r["net_pct"] > 0
    assert r["n_rebal"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest test_lab.py -k rebalance -q`
Expected: FAIL (`sim_rebalance` no existe).

- [ ] **Step 3: Write minimal implementation**

```python
EARN_DAILY = 0.05 / 365.0


def sim_rebalance(closes_por_activo, modo="umbral", umbral_pct=15.0, periodo=30,
                  fee=0.001, earn_daily=EARN_DAILY, usdt_target=0.0):
    """PURO. Portfolio target equal-weight sobre los activos + leg USDT (usdt_target del total a earn).
    modo 'umbral': rebalancea al target cuando algún peso se desvía > umbral_pct (relativo) del target.
    modo 'periodico': rebalancea cada `periodo` días. Cada rebalanceo paga fee sobre el turnover.
    Devuelve net_pct, n_rebal, max_dd, fin_pesos."""
    syms = list(closes_por_activo.keys())
    n = min((len(v) for v in closes_por_activo.values()), default=0)
    if n < 2 or not syms:
        return {"net_pct": 0.0, "n_rebal": 0, "max_dd": 0.0, "fin_pesos": {}}
    cripto_target = (1.0 - usdt_target) / len(syms)
    target = {s: cripto_target for s in syms}
    target["_USDT"] = usdt_target
    # valor por sleeve (en $; equity0 = 1.0)
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
        # turnover cuenta ventas+compras; fee sobre la mitad (cada $ movido cruza una vez)
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest test_lab.py -k rebalance -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Checkpoint**

Run: `python -m pytest test_lab.py -q`
Expected: all green (12 passed).

---

### Task 3: Extender `sim_grid_ladder` con `inventory_skew` (aditivo, no-regresivo)

**Files:**
- Modify: `entrenar.py:1065-1153` (firma + lógica de placement de `sim_grid_ladder`)
- Test: `test_lab.py`

**Interfaces:**
- Consumes: `sim_grid_ladder` existente.
- Produces: `sim_grid_ladder(..., inventory_skew=0.0, inventory_target_rungs=None)` — `inventory_skew=0.0`
  ⟹ comportamiento idéntico. Con skew>0: el spacing efectivo de compra/venta se ajusta por la fracción
  de rungs en `holding` vs target (más inventario → sell más cerca del mid, buy más lejos).

- [ ] **Step 1: Write the failing no-regression + behavior tests**

```python
import entrenar


def _bars_sinteticos(n=300):
    # serie con oscilación para gatillar round-trips
    bars = []
    px = 100.0
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
    # con skew el placement difiere → al menos una métrica cambia
    assert (skewed["n_roundtrips"], skewed["net_pct"]) != (base["n_roundtrips"], base["net_pct"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest test_lab.py -k skew -q`
Expected: FAIL (`sim_grid_ladder() got an unexpected keyword argument 'inventory_skew'`).

- [ ] **Step 3: Implement additively in `entrenar.py`**

Modify the signature (line 1065-1067) to add the param:

```python
def sim_grid_ladder(bars, operable, spacing_pct=2.0, n_rungs=5, capital0=1000.0,
                    invest_per_rung_pct=0.04, fee=0.0008, atr_spacing=False,
                    flatten_on_exit=False, stop_pct=None, max_hold_bars=None,
                    inventory_skew=0.0):
```

Add a helper after `S_of` (after line 1088) to compute the inventory fraction and skewed spacings:

```python
    def _skew_factor():
        # fracción de rungs en holding (inventario colgado) vs total
        if inventory_skew <= 0.0:
            return 1.0, 1.0   # (sell_mult, buy_mult) neutro
        held = sum(1 for r in rungs if r["state"] == "holding")
        frac = held / n_rungs if n_rungs else 0.0
        # más inventario → sell más cerca del mid (spacing menor) y buy más lejos (spacing mayor)
        adj = inventory_skew * frac
        return max(0.1, 1.0 - adj), 1.0 + adj
```

Apply the buy-side multiplier where buy_px is set on re-center (line ~1110) and the sell-side
multiplier where sell_px is set on fill (line ~1139). Replace those two assignments:

```python
        # --- re-centrar rungs sin posición al entrar a un segmento operable ---
        if op and not prev_op:
            _, buy_mult = _skew_factor()
            for k, r in enumerate(rungs, 1):
                if r["state"] in ("armed", "stopped"):
                    r["state"] = "armed"
                    r["buy_px"] = px * (1 - k * S * buy_mult / 100.0)
```

```python
        # --- COMPRAS ---
        for r in rungs:
            if r["state"] == "armed" and r["buy_px"] > 0 and lo <= r["buy_px"]:
                monto = invest_per_rung_pct * capital0
                if 5.0 <= monto <= cash:
                    qty = monto / r["buy_px"]
                    cash -= monto * (1 + fee)
                    sell_mult, _ = _skew_factor()
                    r["state"] = "holding"; r["qty"] = qty
                    r["sell_px"] = r["buy_px"] * (1 + S * sell_mult / 100.0); r["bought_i"] = i
                    n_buy += 1
```

(Note: with `inventory_skew=0.0`, `_skew_factor()` returns `(1.0, 1.0)` ⟹ `S * 1.0` ⟹ identical math.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest test_lab.py -k skew -q && python -m pytest test_ladder_grid.py -q`
Expected: skew tests PASS **and** `test_ladder_grid.py` still PASS (no regression on the live ladder).

- [ ] **Step 5: Checkpoint**

Run: `python -m pytest test_lab.py test_ladder_grid.py test_ladder_okx.py -q`
Expected: all green (ladder vivo intacto).

---

### Task 4: Adaptador `eval_rebalance` + import de primitivos

**Files:**
- Modify: `lab.py`
- Test: `test_lab.py`

**Interfaces:**
- Consumes: `sim_rebalance` (Task 2), `veredicto`/`lcb_apareado` (Task 1).
- Produces: `eval_rebalance(dias=730, pares=None) -> tuple[str, dict, float]` (nombre, dict_veredicto, edge).
  Helper testeable puro: `rebal_edge(net_umbral, net_hodl) -> float`.

- [ ] **Step 1: Write the failing test (helper puro)**

```python
def test_rebal_edge():
    assert lab.rebal_edge(12.0, 5.0) == 7.0
    assert lab.rebal_edge(-3.0, 4.0) == -7.0
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest test_lab.py -k rebal_edge -q`
Expected: FAIL.

- [ ] **Step 3: Implement (helper + driver con red)**

Add imports at top of `lab.py` (after `import math`):

```python
import entrenar
from entrenar import _bars_15m, _regimen_estado_por_ts, sim_grid_ladder, _atr_mult

PARES_DEFAULT = ["BTC/USDT", "ETH/USDT", "LTC/USDT", "ADA/USDT", "SOL/USDT", "XRP/USDT"]
```

```python
def rebal_edge(net_variante, net_baseline):
    return round(net_variante - net_baseline, 2)


def _closes_diarios(exchange, sym, dias):
    bars, ts = _bars_15m(exchange, sym, dias)
    # colapsa 15m → cierre diario
    por_dia = {}
    for b, t in zip(bars, ts):
        por_dia[(t // 86400000)] = b["close"]
    return [por_dia[k] for k in sorted(por_dia)]


def eval_rebalance(dias=730, pares=None):
    import ccxt
    syms = pares or ["BTC/USDT", "ETH/USDT", "LTC/USDT"]
    ex = ccxt.binance({"enableRateLimit": True,
                       "options": {"defaultType": "spot", "adjustForTimeDifference": True}})
    closes = {}
    for s in syms:
        try:
            c = _closes_diarios(ex, s, dias)
            if len(c) > 50:
                closes[s] = c
        except Exception as e:
            print(f"  rebalance: {s} sin datos ({e})")
    n = min((len(v) for v in closes.values()), default=0)
    closes = {k: v[-n:] for k, v in closes.items()}   # alinear largos
    mid = n // 2
    def corte(d):
        return {k: v[d] for k, v in closes.items()}
    full = sim_rebalance(closes, modo="umbral", umbral_pct=15.0)
    m1 = sim_rebalance({k: v[:mid] for k, v in closes.items()}, umbral_pct=15.0)
    m2 = sim_rebalance({k: v[mid:] for k, v in closes.items()}, umbral_pct=15.0)
    hodl = sim_rebalance(closes, modo="umbral", umbral_pct=1e9)   # nunca rebalancea = HODL
    earn = sim_rebalance(closes, usdt_target=1.0)
    # normalizar al formato del evaluador (realiz≈net para rebalance; trab=0)
    def norm(r): return {"net": r["net_pct"], "realiz": r["net_pct"], "trab": 0.0}
    # δ pareado: por mitad, umbral − hodl
    h1 = sim_rebalance({k: v[:mid] for k, v in closes.items()}, umbral_pct=1e9)
    h2 = sim_rebalance({k: v[mid:] for k, v in closes.items()}, umbral_pct=1e9)
    lcb = lcb_apareado([m1["net_pct"] - h1["net_pct"], m2["net_pct"] - h2["net_pct"]])["lcb"]
    d = veredicto(norm(full), norm(m1), norm(m2), lcb=lcb)
    edge = rebal_edge(full["net_pct"], hodl["net_pct"])
    print(f"\n[REBALANCE] umbral15% net {full['net_pct']:+.2f}% (1ª {m1['net_pct']:+.2f}/2ª "
          f"{m2['net_pct']:+.2f}, rebal {full['n_rebal']}) vs HODL {hodl['net_pct']:+.2f}% "
          f"vs earn {earn['net_pct']:+.2f}% | maxDD {full['max_dd']:.1f}%")
    return ("rebalance-umbral15", d, edge)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest test_lab.py -k rebal_edge -q`
Expected: PASS.

- [ ] **Step 5: Checkpoint**

Run: `python -m pytest test_lab.py -q && python -c "import lab"`
Expected: green + import ok.

---

### Task 5: Adaptador `eval_skew`

**Files:**
- Modify: `lab.py`
- Test: `test_lab.py`

**Interfaces:**
- Consumes: `sim_grid_ladder` extendido (Task 3), `veredicto`/`lcb_apareado`.
- Produces: `eval_skew(dias=730, pares=None) -> tuple[str,dict,float]`. Helper puro:
  `agregar_deltas(rows) -> dict` (promedia net/realiz/trab de una lista de resultados por par).

- [ ] **Step 1: Write the failing test (helper puro)**

```python
def test_agregar_deltas_promedia():
    rows = [{"net": 2.0, "realiz": 1.0, "trab": 0.5},
            {"net": 4.0, "realiz": 3.0, "trab": 1.5}]
    a = lab.agregar_deltas(rows)
    assert a["net"] == 3.0 and a["realiz"] == 2.0 and a["trab"] == 1.0


def test_agregar_deltas_vacio():
    a = lab.agregar_deltas([])
    assert a == {"net": 0.0, "realiz": 0.0, "trab": 0.0}
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest test_lab.py -k agregar_deltas -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
def agregar_deltas(rows):
    if not rows:
        return {"net": 0.0, "realiz": 0.0, "trab": 0.0}
    n = len(rows)
    return {"net": round(sum(r["net"] for r in rows) / n, 2),
            "realiz": round(sum(r["realiz"] for r in rows) / n, 2),
            "trab": round(sum(r["trab"] for r in rows) / n, 2)}


def _norm_ladder(r):
    return {"net": r["net_pct"], "realiz": r["realizado_pct"], "trab": r["inv_trabado_pct"]}


def eval_skew(dias=730, pares=None):
    import ccxt
    syms = pares or ["LTC/USDT", "ADA/USDT", "BTC/USDT", "SOL/USDT", "XRP/USDT"]
    ex = ccxt.binance({"enableRateLimit": True,
                       "options": {"defaultType": "spot", "adjustForTimeDifference": True}})
    kw = dict(spacing_pct=2.0, n_rungs=5, stop_pct=15.0, flatten_on_exit=True, fee=0.0008)
    full_b, full_s, m1b, m1s, m2b, m2s, deltas = [], [], [], [], [], [], []
    for s in syms:
        try:
            bars, ts = _bars_15m(ex, s, dias)
            est = _regimen_estado_por_ts(ex, dias, sym=s)
        except Exception as e:
            print(f"  skew: {s} sin datos ({e})"); continue
        if len(bars) < 300:
            continue
        rango = [est.get((t // 86400000) * 86400000) == "RANGO" for t in ts]
        mid = len(bars) // 2
        base = sim_grid_ladder(bars, rango, **kw)
        skew = sim_grid_ladder(bars, rango, inventory_skew=0.5, **kw)
        full_b.append(_norm_ladder(base)); full_s.append(_norm_ladder(skew))
        m1b.append(_norm_ladder(sim_grid_ladder(bars[:mid], rango[:mid], **kw)))
        m1s.append(_norm_ladder(sim_grid_ladder(bars[:mid], rango[:mid], inventory_skew=0.5, **kw)))
        m2b.append(_norm_ladder(sim_grid_ladder(bars[mid:], rango[mid:], **kw)))
        m2s.append(_norm_ladder(sim_grid_ladder(bars[mid:], rango[mid:], inventory_skew=0.5, **kw)))
        deltas.append(skew["net_pct"] - base["net_pct"])
        print(f"  skew {s}: base net {base['net_pct']:+.2f}% → skew {skew['net_pct']:+.2f}% "
              f"(δ {skew['net_pct']-base['net_pct']:+.2f})")
    fb, fs = agregar_deltas(full_b), agregar_deltas(full_s)
    lcb = lcb_apareado(deltas)["lcb"]
    d = veredicto(fs, agregar_deltas(m1s), agregar_deltas(m2s), lcb=lcb)
    edge = round(fs["net"] - fb["net"], 2)
    return ("inventory-skew", d, edge)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest test_lab.py -k agregar_deltas -q`
Expected: PASS.

- [ ] **Step 5: Checkpoint**

Run: `python -m pytest test_lab.py -q`
Expected: green.

---

### Task 6: Adaptador `eval_vol`

**Files:**
- Modify: `lab.py`
- Test: `test_lab.py`

**Interfaces:**
- Consumes: `sim_grid_ladder` (`atr_spacing`), `agregar_deltas`, `_norm_ladder`, `veredicto`,`lcb_apareado`.
- Produces: `eval_vol(dias=730, pares=None) -> tuple[str,dict,float]`. Helper puro:
  `mejor_vol_label(resultados) -> str` (elige la variante de mapa atr de mayor net robusto).

- [ ] **Step 1: Write the failing test (helper puro)**

```python
def test_mejor_vol_label_elige_robusto_mayor():
    res = {"fijo": {"net": 3.0, "rob": True}, "atr_a": {"net": 5.0, "rob": False},
           "atr_b": {"net": 4.0, "rob": True}}
    # entre robustos (fijo, atr_b) elige el de mayor net → atr_b
    assert lab.mejor_vol_label(res) == "atr_b"


def test_mejor_vol_label_sin_robustos_cae_a_fijo():
    res = {"fijo": {"net": 1.0, "rob": False}, "atr_a": {"net": 2.0, "rob": False}}
    assert lab.mejor_vol_label(res) == "fijo"
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest test_lab.py -k mejor_vol -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
def mejor_vol_label(resultados):
    robustos = {k: v for k, v in resultados.items() if v["rob"]}
    if robustos:
        return max(robustos, key=lambda k: robustos[k]["net"])
    return "fijo"


def eval_vol(dias=730, pares=None):
    import ccxt
    syms = pares or ["LTC/USDT", "ADA/USDT", "BTC/USDT", "SOL/USDT", "XRP/USDT"]
    ex = ccxt.binance({"enableRateLimit": True,
                       "options": {"defaultType": "spot", "adjustForTimeDifference": True}})
    kw = dict(spacing_pct=2.0, n_rungs=5, stop_pct=15.0, flatten_on_exit=True, fee=0.0008)
    fijo_full, atr_full, fijo1, fijo2, atr1, atr2, deltas = [], [], [], [], [], [], []
    for s in syms:
        try:
            bars, ts = _bars_15m(ex, s, dias)
            est = _regimen_estado_por_ts(ex, dias, sym=s)
        except Exception as e:
            print(f"  vol: {s} sin datos ({e})"); continue
        if len(bars) < 300:
            continue
        rango = [est.get((t // 86400000) * 86400000) == "RANGO" for t in ts]
        mid = len(bars) // 2
        f = sim_grid_ladder(bars, rango, atr_spacing=False, **kw)
        a = sim_grid_ladder(bars, rango, atr_spacing=True, **kw)
        fijo_full.append(_norm_ladder(f)); atr_full.append(_norm_ladder(a))
        fijo1.append(_norm_ladder(sim_grid_ladder(bars[:mid], rango[:mid], atr_spacing=False, **kw)))
        fijo2.append(_norm_ladder(sim_grid_ladder(bars[mid:], rango[mid:], atr_spacing=False, **kw)))
        atr1.append(_norm_ladder(sim_grid_ladder(bars[:mid], rango[:mid], atr_spacing=True, **kw)))
        atr2.append(_norm_ladder(sim_grid_ladder(bars[mid:], rango[mid:], atr_spacing=True, **kw)))
        deltas.append(a["net_pct"] - f["net_pct"])
        print(f"  vol {s}: fijo {f['net_pct']:+.2f}% → atr {a['net_pct']:+.2f}% "
              f"(δ {a['net_pct']-f['net_pct']:+.2f})")
    ff, af = agregar_deltas(fijo_full), agregar_deltas(atr_full)
    lcb = lcb_apareado(deltas)["lcb"]
    d = veredicto(af, agregar_deltas(atr1), agregar_deltas(atr2), lcb=lcb)
    edge = round(af["net"] - ff["net"], 2)
    return ("vol-spacing", d, edge)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest test_lab.py -k mejor_vol -q`
Expected: PASS.

- [ ] **Step 5: Checkpoint**

Run: `python -m pytest test_lab.py -q`
Expected: green.

---

### Task 7: Adaptador `eval_protections` (overlays puros + driver)

**Files:**
- Modify: `lab.py`
- Test: `test_lab.py`

**Interfaces:**
- Consumes: `sim_grid_ladder`, `veredicto`.
- Produces: overlays puros `lowprofit_overlay(realiz_por_ventana, umbral) -> int` (nº desactivaciones)
  y `maxdd_overlay(equity_curve, max_dd_pct) -> int` (nº disparos), + driver
  `eval_protections(dias=730, pares=None) -> tuple[str,dict,float]`.

- [ ] **Step 1: Write the failing tests (overlays puros)**

```python
def test_lowprofit_overlay_desactiva_bajo_umbral():
    # ventanas con realiz [-1, -2, 0.5]; umbral 0 → 2 desactivaciones
    assert lab.lowprofit_overlay([-1.0, -2.0, 0.5], umbral=0.0) == 2


def test_lowprofit_overlay_limpio_no_dispara():
    assert lab.lowprofit_overlay([1.0, 2.0, 0.5], umbral=0.0) == 0


def test_maxdd_overlay_dispara_en_caida():
    # equity sube a 1.2 y cae a 1.0 → dd ~16.7% > 10 → ≥1 disparo
    assert lab.maxdd_overlay([1.0, 1.2, 1.0, 1.05], max_dd_pct=10.0) >= 1


def test_maxdd_overlay_sin_caida_no_dispara():
    assert lab.maxdd_overlay([1.0, 1.01, 1.02, 1.03], max_dd_pct=10.0) == 0
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest test_lab.py -k overlay -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
def lowprofit_overlay(realiz_por_ventana, umbral=0.0):
    """nº de ventanas cuyo realiz quedó <= umbral (se 'desactivaría' el par esa ventana)."""
    return sum(1 for r in realiz_por_ventana if r <= umbral)


def maxdd_overlay(equity_curve, max_dd_pct=10.0):
    """nº de veces que el drawdown desde el peak corriente cruza max_dd_pct (cada cruce = 1 disparo)."""
    peak = equity_curve[0] if equity_curve else 1.0
    disparos = 0
    armado = True
    for e in equity_curve:
        peak = max(peak, e)
        dd = (peak - e) / peak * 100 if peak > 0 else 0.0
        if dd > max_dd_pct and armado:
            disparos += 1
            armado = False
        elif dd <= max_dd_pct * 0.5:
            armado = True   # re-arma cuando se recupera a la mitad del umbral
    return disparos


def eval_protections(dias=730, pares=None):
    import ccxt
    syms = pares or ["LTC/USDT", "ADA/USDT", "BTC/USDT", "SOL/USDT", "XRP/USDT"]
    ex = ccxt.binance({"enableRateLimit": True,
                       "options": {"defaultType": "spot", "adjustForTimeDifference": True}})
    kw = dict(spacing_pct=2.0, n_rungs=5, stop_pct=15.0, flatten_on_exit=True, fee=0.0008)
    nets, m1, m2, total_lp, total_dd = [], [], [], 0, 0
    for s in syms:
        try:
            bars, ts = _bars_15m(ex, s, dias)
            est = _regimen_estado_por_ts(ex, dias, sym=s)
        except Exception as e:
            print(f"  protections: {s} sin datos ({e})"); continue
        if len(bars) < 300:
            continue
        rango = [est.get((t // 86400000) * 86400000) == "RANGO" for t in ts]
        mid = len(bars) // 2
        r = sim_grid_ladder(bars, rango, **kw)
        nets.append(_norm_ladder(r))
        m1.append(_norm_ladder(sim_grid_ladder(bars[:mid], rango[:mid], **kw)))
        m2.append(_norm_ladder(sim_grid_ladder(bars[mid:], rango[mid:], **kw)))
        # ventanas mensuales (~30d * 96 barras/d) de realiz para lowprofit
        win = 30 * 96
        realiz_win = []
        for j in range(0, len(bars), win):
            seg = bars[j:j + win]; segr = rango[j:j + win]
            if len(seg) > 50:
                realiz_win.append(sim_grid_ladder(seg, segr, **kw)["realizado_pct"])
        lp = lowprofit_overlay(realiz_win, umbral=0.0)
        total_lp += lp
        print(f"  protections {s}: net {r['net_pct']:+.2f}%, lowprofit desactivaría {lp}/{len(realiz_win)} ventanas")
    full = agregar_deltas(nets)
    # significativo=None: protections no tiene baseline pareable por unidad
    d = veredicto(full, agregar_deltas(m1), agregar_deltas(m2), lcb=None)
    print(f"  → LowProfitPairs desactivaría {total_lp} ventanas-par; MaxDrawdown global = baranda "
          f"(disparo medido en el portfolio agregado de eval_edge).")
    return ("protections", d, 0.0)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest test_lab.py -k overlay -q`
Expected: PASS.

- [ ] **Step 5: Checkpoint**

Run: `python -m pytest test_lab.py -q`
Expected: green.

---

### Task 8: Adaptador `eval_edge` (edge positioning)

**Files:**
- Modify: `lab.py`
- Test: `test_lab.py`

**Interfaces:**
- Consumes: `sim_grid_ladder`, `veredicto`, `maxdd_overlay`.
- Produces: helper puro `pesos_edge(net_por_par: dict[str,float]) -> dict[str,float]` (∝ max(0,net),
  normalizado; todo ≤0 → equal-weight), + driver `eval_edge(dias=730, pares=None)`.

- [ ] **Step 1: Write the failing tests (helper puro)**

```python
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
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest test_lab.py -k pesos_edge -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
def pesos_edge(net_por_par):
    syms = list(net_por_par)
    if not syms:
        return {}
    pos = {s: max(0.0, net_por_par[s]) for s in syms}
    tot = sum(pos.values())
    if tot <= 0:
        return {s: 1.0 / len(syms) for s in syms}
    return {s: pos[s] / tot for s in syms}


def eval_edge(dias=730, pares=None):
    import ccxt
    syms = pares or ["LTC/USDT", "ADA/USDT", "BTC/USDT", "SOL/USDT", "XRP/USDT"]
    ex = ccxt.binance({"enableRateLimit": True,
                       "options": {"defaultType": "spot", "adjustForTimeDifference": True}})
    kw = dict(spacing_pct=2.0, n_rungs=5, stop_pct=15.0, flatten_on_exit=True, fee=0.0008)
    net_par, net_par1, net_par2 = {}, {}, {}
    for s in syms:
        try:
            bars, ts = _bars_15m(ex, s, dias)
            est = _regimen_estado_por_ts(ex, dias, sym=s)
        except Exception as e:
            print(f"  edge: {s} sin datos ({e})"); continue
        if len(bars) < 300:
            continue
        rango = [est.get((t // 86400000) * 86400000) == "RANGO" for t in ts]
        mid = len(bars) // 2
        net_par[s] = sim_grid_ladder(bars, rango, **kw)["net_pct"]
        net_par1[s] = sim_grid_ladder(bars[:mid], rango[:mid], **kw)["net_pct"]
        net_par2[s] = sim_grid_ladder(bars[mid:], rango[mid:], **kw)["net_pct"]
    if not net_par:
        return ("edge-positioning", veredicto({"net": 0, "realiz": 0}, {"net": 0, "realiz": 0},
                                              {"net": 0, "realiz": 0}, lcb=None), 0.0)
    w = pesos_edge(net_par)
    eq = lambda d: sum(d[s] / len(d) for s in d)              # equal-weight
    ew = lambda d: sum(w[s] * d[s] for s in d)                # edge-weight
    full_ew, full_eq = ew(net_par), eq(net_par)
    m1 = {"net": ew(net_par1), "realiz": ew(net_par1)}
    m2 = {"net": ew(net_par2), "realiz": ew(net_par2)}
    lcb = lcb_apareado([ew(net_par1) - eq(net_par1), ew(net_par2) - eq(net_par2)])["lcb"]
    d = veredicto({"net": full_ew, "realiz": full_ew, "trab": 0.0}, m1, m2, lcb=lcb)
    edge = round(full_ew - full_eq, 2)
    print(f"\n[EDGE] edge-weight net {full_ew:+.2f}% vs equal-weight {full_eq:+.2f}% | pesos "
          f"{ {s: round(v,2) for s,v in w.items()} }")
    return ("edge-positioning", d, edge)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest test_lab.py -k pesos_edge -q`
Expected: PASS.

- [ ] **Step 5: Checkpoint**

Run: `python -m pytest test_lab.py -q`
Expected: green.

---

### Task 9: CLI `--all` + flags individuales + tabla-veredicto

**Files:**
- Modify: `lab.py`
- Test: `test_lab.py`

**Interfaces:**
- Consumes: los 5 `eval_*`, `fmt_veredicto`.
- Produces: `tabla_consolidada(resultados: list[tuple[str,dict,float]]) -> str` (puro, ordenable),
  + `main()` con argparse.

- [ ] **Step 1: Write the failing test (orden puro)**

```python
def test_tabla_consolidada_ordena_por_veredicto_y_edge():
    res = [
        ("a", {"robusto": True, "significativo": True, "fee_proof": True, "por_cosecha": True,
               "veredicto": "MEDIR-MÁS"}, 1.0),
        ("b", {"robusto": True, "significativo": True, "fee_proof": True, "por_cosecha": True,
               "veredicto": "DESPLEGAR"}, 0.5),
        ("c", {"robusto": False, "significativo": False, "fee_proof": True, "por_cosecha": False,
               "veredicto": "DESCARTAR"}, 9.0),
    ]
    out = lab.tabla_consolidada(res)
    # DESPLEGAR(b) antes que MEDIR-MÁS(a) antes que DESCARTAR(c), pese al edge alto de c
    assert out.index("b") < out.index("a") < out.index("c")
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest test_lab.py -k tabla_consolidada -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
_ORDEN = {"DESPLEGAR": 0, "MEDIR-MÁS": 1, "DESCARTAR": 2}


def tabla_consolidada(resultados):
    filas = sorted(resultados, key=lambda r: (_ORDEN.get(r[1]["veredicto"], 9), -r[2]))
    lineas = ["", "=" * 72, "TABLA-VEREDICTO CONSOLIDADA (rigor: robusto + significativo + fee + cosecha)"]
    for nombre, d, edge in filas:
        lineas.append(fmt_veredicto(nombre, d, edge))
    lineas.append("-" * 72)
    lineas.append("  DESPLEGAR = robusto ∧ por-cosecha ∧ fee-proof ∧ (significativo o sin-baseline)")
    lineas.append("  Recordá: 'DESPLEGAR' habilita una discusión de runtime/canary, NO auto-gradúa.")
    return "\n".join(lineas)


def main():
    import argparse
    p = argparse.ArgumentParser(description="lab.py — banco de medición de features (no toca bot/DB/capital)")
    p.add_argument("--all", action="store_true", help="corre los 5 hallazgos + tabla consolidada")
    p.add_argument("--skew", action="store_true")
    p.add_argument("--vol", action="store_true")
    p.add_argument("--protections", action="store_true")
    p.add_argument("--rebalance", action="store_true")
    p.add_argument("--edge", action="store_true")
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
    if not res:
        p.print_help(); return
    print(tabla_consolidada(res))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest test_lab.py -q`
Expected: PASS (todos).

- [ ] **Step 5: Checkpoint — smoke end-to-end corto**

Run: `python -m pytest test_lab.py -q && python lab.py --rebalance --dias 120 --pares BTC/USDT,ETH/USDT,LTC/USDT`
Expected: tests green; el comando imprime la línea `[REBALANCE] ...` + tabla con un veredicto (red real, ~1 min).

---

### Task 10: Documentación (bot.txt + CLAUDE.md + memoria)

**Files:**
- Modify: `bot.txt` (nota nueva al final del bloque de versiones)
- Modify: `CLAUDE.md` (lista de auxiliares + bloque de comandos de medición)
- Create: `/home/juan/.claude/projects/-mnt-data-brain/memory/lab-features.md` + línea en `MEMORY.md`

**Interfaces:** N/A (docs).

- [ ] **Step 1: Añadir a `CLAUDE.md` la lista de auxiliares**

Después de la línea de `screener_pares.py`, agregar:

```
- `lab.py` — banco standalone de medición de features candidatas (de la investigación de bots open-source/comerciales): inventory-skew, vol-spacing, protections (LowProfitPairs/MaxDrawdown), threshold-rebalance, edge-positioning. Cada hallazgo se evalúa con robustez por mitades + gate apareado + fee-proof + por-cosecha → veredicto DESPLEGAR/MEDIR-MÁS/DESCARTAR. `python lab.py --all --dias 730`. Importa primitivos de `entrenar.py`, NO toca bot/DB/capital. Tests en `test_lab.py`.
```

- [ ] **Step 2: Añadir a `CLAUDE.md` el bloque de comandos** (tras los `entrenar.py --*`):

```
python lab.py --all --dias 730        # banco de features: mide/compara/evalúa los 5 hallazgos → tabla-veredicto
python lab.py --rebalance --dias 730  # solo threshold-rebalancing vs HODL vs earn (estrategia nueva)
```

- [ ] **Step 3: Añadir nota a `bot.txt`** (resumen: qué mide `lab.py`, que no toca runtime, que `sim_grid_ladder` ganó `inventory_skew=0.0` aditivo, suite de tests +N).

- [ ] **Step 4: Crear memoria `lab-features.md`** con el veredicto de cada hallazgo (a completar tras correr `--all`) + línea en `MEMORY.md`.

- [ ] **Step 5: Checkpoint final**

Run: `python -m pytest test_lab.py test_ladder_grid.py test_ladder_okx.py -q && python -c "import lab, entrenar; print('imports ok')"`
Expected: todo verde; ladder vivo intacto; imports ok.

---

## Self-Review

**Spec coverage:**
- Núcleo evaluador (lcb_apareado/veredicto/fmt) → Task 1 ✓
- sim_rebalance → Task 2 ✓
- inventory_skew aditivo en sim_grid_ladder → Task 3 ✓
- 5 adaptadores → Tasks 4 (rebalance), 5 (skew), 6 (vol), 7 (protections), 8 (edge) ✓
- CLI --all + tabla → Task 9 ✓
- Tests test_lab.py (evaluador, rebalance, skew no-regresión, protections, edge) → cubiertos en cada task ✓
- Docs bot.txt/CLAUDE.md/memoria → Task 10 ✓

**Placeholder scan:** sin TBD/TODO; todo el código de pasos testeables está completo. Los drivers con
red (eval_*) tienen su lógica completa; sus tests cubren los helpers puros (no la red, por diseño).

**Type consistency:** `_norm_ladder` mapea `net_pct/realizado_pct/inv_trabado_pct` → `net/realiz/trab`
(las claves que consume `veredicto`/`agregar_deltas`). `sim_rebalance` devuelve `net_pct` → normalizado
en `eval_rebalance` vía `norm()`. `veredicto` consume `net/realiz/trab` consistente en todos los
adaptadores. `inventory_skew` default 0.0 en los tres lugares de Task 3.
