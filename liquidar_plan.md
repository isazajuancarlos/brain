# Liquidación total OKX → USDT — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Script standalone `liquidar.py` que deja toda la cuenta OKX en USDT (rescata cripto de Earn, cancela órdenes, vende a market, limpia la DB), con dry-run por defecto.

**Architecture:** Una clase `LiquidadorOKX` con **inyección de dependencias** (exchange + ruta de DB + reloj/sleep) para que toda la lógica sea testeable contra un `FakeOkx` en memoria, sin tocar la API real. La capa de I/O (ccxt OKX, sqlite) se construye solo en `main()`. Toda acción real está gateada por `--ejecutar`.

**Tech Stack:** Python 3, ccxt (cliente OKX), sqlite3 (stdlib), pytest (nuevo, solo para tests). Spec de referencia: `liquidar_spec.md`.

> **Nota git:** este workspace NO es un repo git. Los pasos de `commit` quedan como checkpoints opcionales: si querés versionar, corré `git init` una vez al inicio; si no, omití los pasos de commit (el resto del plan funciona igual).

> **Nota convención:** el proyecto es plano, sin subdirectorios (ver CLAUDE.md). Por eso `liquidar.py` y `test_liquidar.py` van en la raíz, no en `tests/`.

---

### Task 0: Scaffolding + harness de tests (`FakeOkx`)

**Files:**
- Create: `liquidar.py`
- Create: `test_liquidar.py`
- Modify: `requirements.txt` (agregar `pytest`)

- [ ] **Step 1: Agregar pytest a requirements.txt**

Agregar una línea al final de `requirements.txt`:

```
pytest
```

Instalar: `pip install pytest`

- [ ] **Step 2: Crear el esqueleto de `liquidar.py` con constantes y la clase vacía**

```python
"""
liquidar.py — Liquidación total OKX → USDT (one-shot, standalone).
Ver liquidar_spec.md. El bot (main.py) DEBE estar apagado al correr esto.
"""
import argparse
import time

OKX_CUENTA_FUNDING = "6"
OKX_CUENTA_TRADING = "18"
POLL_TIMEOUT_S = 60.0
POLL_INTERVAL_S = 1.5


class LiquidadorOKX:
    def __init__(self, exchange, db_conn, *, ejecutar=False, convert_dust=False,
                 clean_db=True, clock=time.monotonic, sleep=time.sleep,
                 poll_timeout=POLL_TIMEOUT_S, poll_interval=POLL_INTERVAL_S):
        self.ex = exchange
        self.db = db_conn
        self.ejecutar = ejecutar
        self.convert_dust = convert_dust
        self.clean_db = clean_db
        self.clock = clock
        self.sleep = sleep
        self.poll_timeout = poll_timeout
        self.poll_interval = poll_interval
        self.log = []  # lista de strings; reporte final

    def _registrar(self, msg):
        self.log.append(msg)
        print(msg)
```

- [ ] **Step 3: Crear el `FakeOkx` en `test_liquidar.py`**

`FakeOkx` simula una cuenta OKX en memoria: balances de trading/funding/earn, mercados, y órdenes abiertas. `redempt` mueve earn→funding; `transfer` mueve funding→trading; `create_market_sell_order` convierte coin→USDT en trading al `precio[ccy]`.

```python
import sqlite3
import pytest
from liquidar import LiquidadorOKX, OKX_CUENTA_FUNDING, OKX_CUENTA_TRADING


class FakeOkx:
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
        return {ccy: {"free": q, "used": 0.0, "total": q} for ccy, q in self.trading.items()}

    def fetch_ticker(self, sym):
        return {"last": self.precios.get(sym.split("/")[0], 0.0)}

    def fetch_open_orders(self):
        return list(self._open_orders)

    def amount_to_precision(self, sym, qty):
        # redondeo simple a 6 decimales para el fake
        return f"{float(qty):.6f}"

    def private_get_finance_savings_balance(self, params):
        ccy = params.get("ccy")
        items = self.earn.items() if ccy is None else [(ccy, self.earn.get(ccy, 0.0))]
        return {"data": [{"ccy": c, "amt": str(q)} for c, q in items if q > 0]}

    def private_get_asset_balances(self, params):
        ccy = params["ccy"]
        return {"data": [{"ccy": ccy, "availBal": str(self.funding.get(ccy, 0.0))}]}

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


def test_harness_fake_redempt_y_transfer():
    ex = FakeOkx(earn={"ADA": 100.0})
    ex.private_post_finance_savings_purchase_redempt({"ccy": "ADA", "amt": "100", "side": "redempt"})
    assert ex.earn["ADA"] == 0.0 and ex.funding["ADA"] == 100.0
    ex.private_post_asset_transfer({"ccy": "ADA", "amt": "100",
                                    "from": OKX_CUENTA_FUNDING, "to": OKX_CUENTA_TRADING})
    assert ex.trading["ADA"] == 100.0
```

- [ ] **Step 4: Correr el test del harness**

Run: `pytest test_liquidar.py -v`
Expected: PASS (`test_harness_fake_redempt_y_transfer`)

- [ ] **Step 5: Commit (opcional)**

```bash
git add liquidar.py test_liquidar.py requirements.txt
git commit -m "scaffold liquidar.py + FakeOkx test harness"
```

---

### Task 1: Snapshot — construir el plan de liquidación

**Files:**
- Modify: `liquidar.py`
- Test: `test_liquidar.py`

- [ ] **Step 1: Test del snapshot (excluye USDT, une trading+funding+earn)**

```python
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
```

- [ ] **Step 2: Correr el test para verque falla**

Run: `pytest test_liquidar.py::test_snapshot_excluye_usdt_y_une_balances -v`
Expected: FAIL (`AttributeError: 'LiquidadorOKX' object has no attribute 'snapshot'`)

- [ ] **Step 3: Implementar `snapshot()`**

```python
    def _earn_balances(self):
        res = self.ex.private_get_finance_savings_balance({})
        return {r["ccy"]: float(r["amt"]) for r in res.get("data", [])}

    def _precio(self, ccy):
        t = self.ex.fetch_ticker(f"{ccy}/USDT")
        return float(t.get("last") or 0.0)

    def snapshot(self):
        bal = self.ex.fetch_balance()
        trading = {c: float(v.get("free", 0)) for c, v in bal.items()}
        earn = self._earn_balances()
        ccys = {c for c in set(trading) | set(earn) if c != "USDT"}
        plan = []
        for ccy in sorted(ccys):
            tr = trading.get(ccy, 0.0)
            er = earn.get(ccy, 0.0)
            total = tr + er
            if total <= 1e-9:
                continue
            precio = self._precio(ccy)
            plan.append({"ccy": ccy, "trading": tr, "earn": er,
                         "total": total, "precio": precio,
                         "valor_usd": total * precio})
        return plan
```

- [ ] **Step 4: Correr el test**

Run: `pytest test_liquidar.py::test_snapshot_excluye_usdt_y_une_balances -v`
Expected: PASS

- [ ] **Step 5: Commit (opcional)**

```bash
git add liquidar.py test_liquidar.py
git commit -m "snapshot: build liquidation plan from balances+earn"
```

---

### Task 2: Clasificación de polvo (qty vendible vs sub-mínimo)

**Files:**
- Modify: `liquidar.py`
- Test: `test_liquidar.py`

- [ ] **Step 1: Test de clasificación de polvo**

```python
def test_es_polvo_por_min_amount_y_min_cost():
    markets = {"ADA/USDT": {"limits": {"amount": {"min": 1.0}, "cost": {"min": 5.0}}}}
    ex = FakeOkx(markets=markets, precios={"ADA": 0.30})
    liq = LiquidadorOKX(ex, db_temporal())
    liq.ex.load_markets()
    # qty 0.5 < min amount 1.0  → polvo
    assert liq.es_polvo("ADA/USDT", 0.5, 0.30) is True
    # qty 10 (cost 3.0) < min cost 5.0 → polvo
    assert liq.es_polvo("ADA/USDT", 10.0, 0.30) is True
    # qty 20 (cost 6.0) ≥ ambos mínimos → vendible
    assert liq.es_polvo("ADA/USDT", 20.0, 0.30) is False
```

- [ ] **Step 2: Correr el test para verificar que falla**

Run: `pytest test_liquidar.py::test_es_polvo_por_min_amount_y_min_cost -v`
Expected: FAIL (`AttributeError: ... 'es_polvo'`)

- [ ] **Step 3: Implementar `es_polvo()`**

```python
    def es_polvo(self, sym, qty, precio):
        m = self.ex.markets.get(sym, {})
        limits = m.get("limits", {})
        min_amt = (limits.get("amount", {}) or {}).get("min") or 0.0
        min_cost = (limits.get("cost", {}) or {}).get("min") or 0.0
        if qty < min_amt:
            return True
        if precio > 0 and qty * precio < min_cost:
            return True
        return False
```

- [ ] **Step 4: Correr el test**

Run: `pytest test_liquidar.py::test_es_polvo_por_min_amount_y_min_cost -v`
Expected: PASS

- [ ] **Step 5: Commit (opcional)**

```bash
git add liquidar.py test_liquidar.py
git commit -m "es_polvo: clasifica saldos sub-minimo"
```

---

### Task 3: Limpieza de DB (opción i del spec)

**Files:**
- Modify: `liquidar.py`
- Test: `test_liquidar.py`

- [ ] **Step 1: Test de limpieza de DB**

```python
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
```

- [ ] **Step 2: Correr el test para verificar que falla**

Run: `pytest test_liquidar.py::test_limpiar_db_borra_solo_liquidados -v`
Expected: FAIL (`AttributeError: ... 'limpiar_db'`)

- [ ] **Step 3: Implementar `limpiar_db()`**

```python
    def limpiar_db(self, symbols, ccys):
        cur = self.db.cursor()
        for sym in symbols:
            for tabla in ("okx_portfolio", "okx_tp_state", "okx_dca_state",
                          "okx_cooldown_state", "okx_grid_orders", "okx_pending_sells"):
                cur.execute(f"DELETE FROM {tabla} WHERE symbol=?", (sym,))
        for ccy in ccys:
            cur.execute("DELETE FROM okx_inventory_earn WHERE ccy=?", (ccy,))
        self.db.commit()
```

- [ ] **Step 4: Correr el test**

Run: `pytest test_liquidar.py::test_limpiar_db_borra_solo_liquidados -v`
Expected: PASS

- [ ] **Step 5: Commit (opcional)**

```bash
git add liquidar.py test_liquidar.py
git commit -m "limpiar_db: borra estado por-simbolo de liquidados"
```

---

### Task 4: Rescate de Earn (redempt → poll funding → transfer a trading)

**Files:**
- Modify: `liquidar.py`
- Test: `test_liquidar.py`

- [ ] **Step 1: Test del rescate (settlement OK) y del timeout**

```python
def test_rescatar_earn_ok():
    ex = FakeOkx(earn={"ADA": 100.0})
    liq = LiquidadorOKX(ex, db_temporal(), ejecutar=True,
                        sleep=lambda s: None, poll_timeout=10, poll_interval=0.0)
    movido = liq.rescatar_earn("ADA", 100.0)
    assert movido == 100.0
    assert ex.trading["ADA"] == 100.0 and ex.earn["ADA"] == 0.0


def test_rescatar_earn_timeout_no_mueve():
    class FundingVacio(FakeOkx):
        def private_post_finance_savings_purchase_redempt(self, params):
            return {"code": "0"}  # NO settlea a funding → simula demora
    ex = FundingVacio(earn={"ADA": 100.0})
    liq = LiquidadorOKX(ex, db_temporal(), ejecutar=True,
                        sleep=lambda s: None, poll_timeout=0.01, poll_interval=0.0)
    assert liq.rescatar_earn("ADA", 100.0) == 0.0
    assert ex.trading.get("ADA", 0.0) == 0.0
```

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run: `pytest test_liquidar.py -k rescatar -v`
Expected: FAIL (`AttributeError: ... 'rescatar_earn'`)

- [ ] **Step 3: Implementar `rescatar_earn()` y helpers de I/O**

```python
    def _funding_balance_ccy(self, ccy):
        res = self.ex.private_get_asset_balances({"ccy": ccy})
        for r in res.get("data", []):
            if r.get("ccy") == ccy:
                return float(r.get("availBal", 0))
        return 0.0

    def _redempt(self, ccy, amt):
        self.ex.private_post_finance_savings_purchase_redempt(
            {"ccy": ccy, "amt": str(round(amt, 8)), "side": "redempt", "rate": "0.01"})

    def _transfer(self, ccy, amt, src, dst):
        self.ex.private_post_asset_transfer(
            {"ccy": ccy, "amt": str(round(amt, 8)), "from": src, "to": dst, "type": "0"})

    def rescatar_earn(self, ccy, amt):
        if not self.ejecutar:
            self._registrar(f"[dry-run] rescataría {amt:.6f} {ccy} de Earn")
            return amt
        self._redempt(ccy, amt)
        t0 = self.clock()
        funding = 0.0
        while (self.clock() - t0) <= self.poll_timeout:
            self.sleep(self.poll_interval)
            funding = self._funding_balance_ccy(ccy)
            if funding >= amt * 0.99:
                break
        if funding < amt * 0.99:
            self._registrar(f"WARN: settlement {ccy} no llegó ({funding:.6f}); se omite")
            return 0.0
        self._transfer(ccy, funding, OKX_CUENTA_FUNDING, OKX_CUENTA_TRADING)
        self._registrar(f"rescatado {funding:.6f} {ccy} Earn→Trading")
        return funding
```

- [ ] **Step 4: Correr los tests**

Run: `pytest test_liquidar.py -k rescatar -v`
Expected: PASS (ambos)

- [ ] **Step 5: Commit (opcional)**

```bash
git add liquidar.py test_liquidar.py
git commit -m "rescatar_earn: redempt + poll funding + transfer"
```

---

### Task 5: Venta a market (con redondeo y ruta de polvo)

**Files:**
- Modify: `liquidar.py`
- Test: `test_liquidar.py`

- [ ] **Step 1: Test de venta y de detección de polvo**

```python
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
```

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run: `pytest test_liquidar.py -k vender -v`
Expected: FAIL (`AttributeError: ... 'vender'`)

- [ ] **Step 3: Implementar `vender()`**

```python
    def vender(self, ccy, qty, precio):
        sym = f"{ccy}/USDT"
        qty_r = float(self.ex.amount_to_precision(sym, qty))
        if self.es_polvo(sym, qty_r, precio):
            self._registrar(f"polvo: {ccy} qty {qty_r} (~${qty_r*precio:.2f}) — no vendible")
            return {"ccy": ccy, "vendido": False, "polvo": True, "qty": qty_r}
        if not self.ejecutar:
            self._registrar(f"[dry-run] vendería {qty_r} {ccy} (~${qty_r*precio:.2f})")
            return {"ccy": ccy, "vendido": True, "polvo": False, "qty": qty_r}
        order = self.ex.create_market_sell_order(sym, qty_r)
        self._registrar(f"vendido {qty_r} {ccy} @ ~{order.get('average')}")
        return {"ccy": ccy, "vendido": True, "polvo": False, "qty": qty_r, "order": order}
```

- [ ] **Step 4: Correr los tests**

Run: `pytest test_liquidar.py -k vender -v`
Expected: PASS (ambos)

- [ ] **Step 5: Commit (opcional)**

```bash
git add liquidar.py test_liquidar.py
git commit -m "vender: market sell con redondeo + ruta polvo"
```

---

### Task 6: Cancelar órdenes abiertas

**Files:**
- Modify: `liquidar.py`
- Test: `test_liquidar.py`

- [ ] **Step 1: Test de cancelación**

```python
def test_cancelar_ordenes_abiertas():
    ex = FakeOkx(open_orders=[{"id": "g1", "symbol": "ADA/USDT"},
                              {"id": "g2", "symbol": "ETH/USDT"}])
    liq = LiquidadorOKX(ex, db_temporal(), ejecutar=True)
    n = liq.cancelar_ordenes()
    assert n == 2 and ex.fetch_open_orders() == []
```

- [ ] **Step 2: Correr el test para verificar que falla**

Run: `pytest test_liquidar.py::test_cancelar_ordenes_abiertas -v`
Expected: FAIL (`AttributeError: ... 'cancelar_ordenes'`)

- [ ] **Step 3: Implementar `cancelar_ordenes()`**

```python
    def cancelar_ordenes(self):
        abiertas = self.ex.fetch_open_orders()
        if not self.ejecutar:
            self._registrar(f"[dry-run] cancelaría {len(abiertas)} órdenes abiertas")
            return len(abiertas)
        n = 0
        for o in abiertas:
            try:
                self.ex.cancel_order(o["id"], o["symbol"])
                n += 1
            except Exception as e:
                self._registrar(f"WARN cancel {o.get('id')}: {e}")
        self._registrar(f"canceladas {n} órdenes abiertas")
        return n
```

- [ ] **Step 4: Correr el test**

Run: `pytest test_liquidar.py::test_cancelar_ordenes_abiertas -v`
Expected: PASS

- [ ] **Step 5: Commit (opcional)**

```bash
git add liquidar.py test_liquidar.py
git commit -m "cancelar_ordenes: barre limit abiertas de OKX"
```

---

### Task 7: Orquestación `ejecutar_liquidacion()` (orden correcto + dry-run)

**Files:**
- Modify: `liquidar.py`
- Test: `test_liquidar.py`

- [ ] **Step 1: Test end-to-end con FakeOkx (flujo completo) y test dry-run (no toca nada)**

```python
def _markets_basicos(ccys):
    return {f"{c}/USDT": {"limits": {"amount": {"min": 0.0001}, "cost": {"min": 1.0}}}
            for c in ccys}

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
    # No queda cripto no-USDT en trading
    assert ex.trading.get("ETH", 0.0) == pytest.approx(0.0)
    assert ex.trading.get("ADA", 0.0) == pytest.approx(0.0)
    # USDT subió (500 + 0.02*3000 + 100*0.30 = 590)
    assert ex.trading["USDT"] == pytest.approx(590.0)
    # USDT en earn intacto
    assert ex.earn["USDT"] == 300.0
    # órdenes canceladas y DB limpia
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
```

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run: `pytest test_liquidar.py -k "flujo_completo or dry_run" -v`
Expected: FAIL (`AttributeError: ... 'ejecutar_liquidacion'`)

- [ ] **Step 3: Implementar `ejecutar_liquidacion()`**

```python
    def ejecutar_liquidacion(self):
        plan = self.snapshot()
        self._registrar(f"=== PLAN ({'EJECUTAR' if self.ejecutar else 'DRY-RUN'}) ===")
        for p in plan:
            self._registrar(f"  {p['ccy']}: total {p['total']:.6f} "
                            f"(trading {p['trading']:.6f} + earn {p['earn']:.6f}) "
                            f"~${p['valor_usd']:.2f}")
        if not plan:
            self._registrar("Nada que liquidar.")
            return

        # 1. cancelar órdenes
        self.cancelar_ordenes()
        # 2. rescatar earn de cada ccy con saldo parqueado
        for p in plan:
            if p["earn"] > 1e-9:
                self.rescatar_earn(p["ccy"], p["earn"])
        # 3. re-leer trading y vender
        bal = self.ex.fetch_balance()
        liquidados, polvo, ccys_ok = [], [], []
        for p in plan:
            ccy = p["ccy"]
            qty = float(bal.get(ccy, {}).get("free", 0)) if self.ejecutar else p["total"]
            if qty <= 1e-9:
                continue
            res = self.vender(ccy, qty, p["precio"])
            if res["vendido"]:
                liquidados.append(f"{ccy}/USDT")
                ccys_ok.append(ccy)
            elif res["polvo"]:
                polvo.append(ccy)
                if self.convert_dust:
                    self._convertir_polvo(ccy, res["qty"])
        # 4. limpiar DB
        if self.clean_db and self.ejecutar:
            self.limpiar_db(liquidados, ccys_ok)
        # 5. reporte
        self._registrar(f"=== FIN === vendidos: {liquidados or 'ninguno'} | "
                        f"polvo: {polvo or 'ninguno'}")

    def _convertir_polvo(self, ccy, qty):
        # OKX small-balance convert. Endpoint exacto a confirmar en runbook (Task 9).
        self._registrar(f"[convert-dust] {ccy} {qty} (endpoint pendiente de confirmar)")
```

- [ ] **Step 4: Correr los tests**

Run: `pytest test_liquidar.py -k "flujo_completo or dry_run" -v`
Expected: PASS (ambos)

- [ ] **Step 5: Correr toda la suite**

Run: `pytest test_liquidar.py -v`
Expected: PASS (todos)

- [ ] **Step 6: Commit (opcional)**

```bash
git add liquidar.py test_liquidar.py
git commit -m "ejecutar_liquidacion: orquesta cancel→rescate→venta→limpieza"
```

---

### Task 8: CLI `main()` (args, cliente real, confirmación)

**Files:**
- Modify: `liquidar.py`
- Test: `test_liquidar.py`

- [ ] **Step 1: Test del parser de argumentos**

```python
def test_parse_args_defaults_dry_run():
    from liquidar import parse_args
    a = parse_args([])
    assert a.ejecutar is False and a.convert_dust is False and a.no_clean_db is False
    b = parse_args(["--ejecutar", "--convert-dust", "--no-clean-db"])
    assert b.ejecutar and b.convert_dust and b.no_clean_db
```

- [ ] **Step 2: Correr el test para verificar que falla**

Run: `pytest test_liquidar.py::test_parse_args_defaults_dry_run -v`
Expected: FAIL (`ImportError: cannot import name 'parse_args'`)

- [ ] **Step 3: Implementar `parse_args()`, `construir_okx()` y `main()`**

```python
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Liquidación total OKX → USDT")
    p.add_argument("--ejecutar", action="store_true", help="ejecuta de verdad (default: dry-run)")
    p.add_argument("--convert-dust", action="store_true", help="convierte polvo sub-mínimo")
    p.add_argument("--no-clean-db", action="store_true", help="no limpia quantum.db")
    return p.parse_args(argv)


def construir_okx():
    import os
    import ccxt
    from dotenv import load_dotenv
    load_dotenv()
    ex = ccxt.okx({
        "apiKey": os.getenv("OKX_API_KEY"),
        "secret": os.getenv("OKX_API_SECRET"),
        "password": os.getenv("OKX_PASSWORD"),
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })
    ex.load_markets()
    return ex


def main():
    import sqlite3
    args = parse_args()
    print("ATENCIÓN: el bot (main.py) debe estar APAGADO. No corras dos instancias.")
    ex = construir_okx()
    conn = sqlite3.connect("quantum.db")
    liq = LiquidadorOKX(ex, conn, ejecutar=args.ejecutar,
                        convert_dust=args.convert_dust, clean_db=not args.no_clean_db)
    if args.ejecutar:
        resp = input("Vas a LIQUIDAR todo OKX a USDT. Escribí 'SI' para continuar: ")
        if resp.strip() != "SI":
            print("Abortado.")
            return
    liq.ejecutar_liquidacion()


if __name__ == "__main__":
    main()
```

> Nota: verificar el nombre real de la passphrase en `.env` (p. ej. `OKX_PASSWORD` vs `OKX_PASSPHRASE`) leyendo cómo construye el cliente `TradingCoreOkx` en `main.py`, y alinear `construir_okx()` con eso.

- [ ] **Step 4: Correr el test y la suite completa**

Run: `pytest test_liquidar.py -v`
Expected: PASS (todos)

- [ ] **Step 5: Commit (opcional)**

```bash
git add liquidar.py test_liquidar.py
git commit -m "main: CLI args + cliente OKX real + confirmacion"
```

---

### Task 9: Runbook de verificación manual (cuenta real)

**Files:**
- Ninguno de código. Esta tarea es ejecución/observación manual.

- [ ] **Step 1: Confirmar la passphrase de `.env`**

Verificar en `main.py` cómo `TradingCoreOkx` construye el cliente OKX (nombre exacto de la env var de passphrase) y que `construir_okx()` coincide.

- [ ] **Step 2: Confirmar bot apagado**

Asegurarse de que `main.py` NO está corriendo (ni local ni OVH). `ps aux | grep main.py` debe estar vacío.

- [ ] **Step 3: Dry-run contra la cuenta real**

Run: `python liquidar.py`
Expected: imprime el PLAN listando exactamente ETH, ADA, AVAX, LINK, SOL, SUI, UNI (o lo que haya en la cuenta) con valores USD razonables; NO coloca ninguna orden.

- [ ] **Step 4: Resolver el endpoint de convert-dust (si se usará)**

Si vas a usar `--convert-dust`, confirmar el endpoint OKX de conversión de saldos chicos (p. ej. `private_post_asset_convert_dust_assets`) y completar `_convertir_polvo()`. Si no, dejarlo como reporte (default).

- [ ] **Step 5: Ejecución real**

Run: `python liquidar.py --ejecutar` → escribir `SI`.
Expected: cancela órdenes, rescata Earn, vende, limpia DB; reporte final con USDT resultante.

- [ ] **Step 6: Post-check**

- Balance OKX Trading: solo USDT (+ polvo residual reportado).
- `sqlite3 quantum.db "SELECT * FROM okx_portfolio; SELECT * FROM okx_inventory_earn;"` → vacío.
- `python liquidar.py` (dry-run de nuevo) → "Nada que liquidar".
