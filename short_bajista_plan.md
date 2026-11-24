# Pata Bajista (EscuadronShortOkx) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Agregar al Quantum Bot una pata short apalancada, disciplinada y gateada por régimen BAJISTA, que opera dinero real en OKX perps (BTC/ETH) con leverage bajo, margen isolated y stop server-side persistente.

**Architecture:** Nuevo hilo daemon `EscuadronShortOkx` en `main.py` (espejo del `EscuadronGridOkx` spot), con wrapper `TradingCoreShortOkx` para aislar la lógica de derivados. Reutiliza `RadarRegimen`, `MotorIndicadores`, `GestorTesoreria`, `sys_caja_negra`, `kill_switch`. Estado en tablas `short_*`. TDD contra `FakeOkxFut` en memoria; primer despliegue real gateado por canary.

**Tech Stack:** Python 3.13, ccxt (OKX swap/linear perps), sqlite3 (WAL), pytest, threading.

---

## Notas de entorno (leer antes de empezar)

- **No es repo git** localmente. Los pasos "Checkpoint" son puntos lógicos de verificación; si querés versionado, corré `git init` primero y reemplazá Checkpoint por `git commit`.
- **`main.py` es el único archivo de runtime.** Todo el código de producción va ahí. Tests en `test_short_okx.py` (raíz, como `test_liquidar.py`).
- **Antes de codear señales, VERIFICÁ los nombres de claves** que devuelve `MotorIndicadores.get_indicators("okx", sym)` leyendo su implementación en `main.py`. El plan asume `close`, `ADX_14`, `EMA_200`, `RSI_14`, `MACDh_12_26_9` (naming pandas_ta). Si difieren, ajustá las constantes `K_*` de la Task 5 — son el único punto donde se nombran.
- **ccxt OKX swap:** símbolo unificado `BTC/USDT:USDT` / `ETH/USDT:USDT` (instId `BTC-USDT-SWAP`). El wrapper usa params OKX-específicos (`tdMode`, `posSide`, `slTriggerPx`, `slOrdPx`) por confiabilidad sobre la API unificada.
- **El bot debe estar APAGADO** mientras corren los tests (no dos instancias contra la misma cuenta). Los tests NO tocan plata real (todo contra `FakeOkxFut`).

---

## Estructura de archivos

| Archivo | Acción | Responsabilidad |
|---|---|---|
| `main.py` | Modificar | CONFIG short, `validar_config`, tablas `short_*` + métodos DB, `TradingCoreShortOkx`, `EscuadronShortOkx`, globals/locks, registro en `main()`, panel HUD, KILL/shutdown |
| `test_short_okx.py` | Crear | `FakeOkxFut` + suite de tests |
| `.env.example` | Modificar | Keys de config short |
| `CLAUDE.md` | Modificar | Sección V1.7.0, revierte "no futuros", conteo hilos |
| `bot.txt` | Modificar | `EscuadronShortOkx`, schema `short_*`, canary |
| memoria proyecto | Modificar | Reapertura de futuros + racional |

---

## Task 1: Config + validación

**Files:**
- Modify: `main.py` (bloque CONFIG, cerca de `"liquidate_only"` ~línea 81; y `validar_config` ~línea 722)
- Test: `test_short_okx.py`

- [ ] **Step 1: Write the failing test**

```python
# test_short_okx.py
import importlib, os
import pytest

def _reload_main(env):
    for k, v in env.items():
        os.environ[k] = str(v)
    import main
    importlib.reload(main)
    return main

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
    with pytest.raises(ValueError, match="SHORT_LEVERAGE"):
        m.validar_config()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest test_short_okx.py -k config -v`
Expected: FAIL (`KeyError: 'fut_enabled'`)

- [ ] **Step 3: Add CONFIG keys**

En el dict `CONFIG`, junto a `"liquidate_only"`, agregar:
```python
    "fut_enabled":            _env_bool("OKX_FUT_ENABLED", False),
    "short_max_equity_pct":   _env_float("SHORT_MAX_EQUITY_PCT", 0.18),
    "short_leverage":         int(_env_float("SHORT_LEVERAGE", 3)),
    "short_sl_pct":           _env_float("SHORT_SL_PCT", 0.04),
    "short_tp_tiers":         [float(x) for x in os.getenv("SHORT_TP_TIERS", "2,4").split(",")],
    "short_max_posiciones":   int(_env_float("SHORT_MAX_POSICIONES", 2)),
    "short_daily_loss_cap_usd": _env_float("SHORT_DAILY_LOSS_CAP_USD", 60.0),
    "short_canary_mode":      _env_bool("SHORT_CANARY_MODE", True),
```

- [ ] **Step 4: Add validación**

En `validar_config()`, agregar (solo valida si fut_enabled):
```python
    if CONFIG["fut_enabled"]:
        if not (1 <= CONFIG["short_leverage"] <= 5):
            raise ValueError(f"SHORT_LEVERAGE fuera de rango seguro [1-5]: {CONFIG['short_leverage']}")
        if not (0.0 < CONFIG["short_max_equity_pct"] <= 0.30):
            raise ValueError(f"SHORT_MAX_EQUITY_PCT fuera de rango [0-0.30]: {CONFIG['short_max_equity_pct']}")
        if not (0.01 <= CONFIG["short_sl_pct"] <= 0.10):
            raise ValueError(f"SHORT_SL_PCT fuera de rango [0.01-0.10]: {CONFIG['short_sl_pct']}")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest test_short_okx.py -k config -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Checkpoint** — config short cargada y validada.

---

## Task 2: Schema DB `short_*` + métodos

**Files:**
- Modify: `main.py` (creación de tablas en `DatabaseManager._init_schema`/equivalente; nuevos métodos)
- Test: `test_short_okx.py`

- [ ] **Step 1: Write the failing test**

```python
# test_short_okx.py
import sqlite3, time

def _db_memoria(m):
    db = m.DatabaseManager(":memory:")
    return db

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest test_short_okx.py -k positions_crud -v`
Expected: FAIL (`AttributeError: ... short_abrir_posicion`)

- [ ] **Step 3: Crear tablas**

En el método donde se crean las tablas (junto a `okx_grid_orders`):
```python
        self._exec("""CREATE TABLE IF NOT EXISTS short_positions(
            symbol TEXT PRIMARY KEY, entry_px REAL, qty REAL, lever INTEGER,
            sl_order_id TEXT, open_ts REAL)""")
        self._exec("""CREATE TABLE IF NOT EXISTS short_realized_pnl(
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, entry_px REAL,
            exit_px REAL, pnl REAL, close_ts REAL)""")
        self._exec("""CREATE TABLE IF NOT EXISTS short_cooldown_state(
            symbol TEXT PRIMARY KEY, last_open_ts REAL)""")
```

- [ ] **Step 4: Agregar métodos a `DatabaseManager`**

```python
    def short_abrir_posicion(self, symbol, entry_px, qty, lever, sl_order_id):
        self._exec("""INSERT OR REPLACE INTO short_positions
            (symbol, entry_px, qty, lever, sl_order_id, open_ts)
            VALUES (?,?,?,?,?,?)""",
            (symbol, entry_px, qty, lever, sl_order_id, time.time()))
        self._exec("""INSERT OR REPLACE INTO short_cooldown_state(symbol, last_open_ts)
            VALUES (?,?)""", (symbol, time.time()))

    def short_get_posiciones(self):
        rows = self._exec("SELECT symbol, entry_px, qty, lever, sl_order_id, open_ts "
                          "FROM short_positions", fetch="all") or []
        return [dict(symbol=r[0], entry_px=r[1], qty=r[2], lever=r[3],
                     sl_order_id=r[4], open_ts=r[5]) for r in rows]

    def short_cerrar_posicion(self, symbol, exit_px, pnl):
        row = self._exec("SELECT entry_px FROM short_positions WHERE symbol=?",
                         (symbol,), fetch="one")
        entry = row[0] if row else 0.0
        self._exec("DELETE FROM short_positions WHERE symbol=?", (symbol,))
        self._exec("""INSERT INTO short_realized_pnl
            (symbol, entry_px, exit_px, pnl, close_ts) VALUES (?,?,?,?,?)""",
            (symbol, entry, exit_px, pnl, time.time()))

    def short_pnl_hoy(self):
        import datetime
        medianoche = datetime.datetime.now().replace(hour=0, minute=0, second=0,
                     microsecond=0).timestamp()
        row = self._exec("SELECT COALESCE(SUM(pnl),0) FROM short_realized_pnl "
                         "WHERE close_ts>=?", (medianoche,), fetch="one")
        return float(row[0]) if row else 0.0

    def short_cooldown(self, symbol):
        row = self._exec("SELECT last_open_ts FROM short_cooldown_state WHERE symbol=?",
                         (symbol,), fetch="one")
        return float(row[0]) if row else 0.0
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest test_short_okx.py -k positions_crud -v`
Expected: PASS

- [ ] **Step 6: Checkpoint** — schema `short_*` y métodos DB listos.

---

## Task 3: `FakeOkxFut` (harness de tests, sin código de producción)

**Files:**
- Modify: `test_short_okx.py`

- [ ] **Step 1: Escribir el fake + su test de auto-verificación**

```python
# test_short_okx.py
class FakeOkxFut:
    """Exchange swap OKX en memoria. Replica la superficie ccxt que usa el wrapper."""
    def __init__(self, precio=63000.0, usdt_free=4000.0):
        self.precio = precio
        self.usdt_free = usdt_free
        self.leverage_calls = []      # (lev, symbol, params)
        self.orders = []              # cada create_order
        self.positions = {}           # symbol -> dict
        self.cancelled = []
        self._oid = 0

    def set_leverage(self, lev, symbol, params=None):
        self.leverage_calls.append((lev, symbol, params or {}))
        return {"leverage": lev}

    def fetch_balance(self, params=None):
        return {"USDT": {"free": self.usdt_free, "total": self.usdt_free},
                "free": {"USDT": self.usdt_free}, "info": {}}

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        self._oid += 1
        oid = f"ord{self._oid}"
        params = params or {}
        self.orders.append(dict(id=oid, symbol=symbol, type=type_, side=side,
                                amount=amount, price=price, params=params))
        # simular fill inmediato del short y registrar posición
        if params.get("posSide") == "short" and not params.get("reduceOnly"):
            self.positions[symbol] = dict(symbol=symbol, contracts=amount,
                side="short", entryPrice=price or self.precio,
                marginMode=params.get("tdMode"))
        if params.get("reduceOnly"):
            self.positions.pop(symbol, None)
        return {"id": oid, "info": {"sCode": "0", "algoId": f"algo{self._oid}"}}

    def fetch_positions(self, symbols=None, params=None):
        return list(self.positions.values())

    def cancel_order(self, oid, symbol=None, params=None):
        self.cancelled.append(oid)
        return {"id": oid}

def test_fake_okx_fut_basico():
    fx = FakeOkxFut()
    fx.set_leverage(3, "BTC/USDT:USDT", {"mgnMode": "isolated"})
    assert fx.leverage_calls[0][0] == 3
    fx.create_order("BTC/USDT:USDT", "limit", "sell", 0.01, 63500,
                    {"posSide": "short", "tdMode": "isolated", "slTriggerPx": 66000})
    assert len(fx.fetch_positions()) == 1
    assert fx.fetch_positions()[0]["marginMode"] == "isolated"
```

- [ ] **Step 2: Run test to verify it passes**

Run: `python -m pytest test_short_okx.py -k fake_okx -v`
Expected: PASS

- [ ] **Step 3: Checkpoint** — harness de derivados en memoria listo.

---

## Task 4: `TradingCoreShortOkx` (wrapper de derivados)

**Files:**
- Modify: `main.py` (nueva clase, cerca de las clases core OKX)
- Test: `test_short_okx.py`

- [ ] **Step 1: Write the failing test**

```python
# test_short_okx.py
def test_abrir_short_setea_isolated_y_sl():
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_LEVERAGE": "3", "SHORT_SL_PCT": "0.04"})
    fx = FakeOkxFut(precio=63000.0)
    core = m.TradingCoreShortOkx(exchange=fx)
    res = core.abrir_short("BTC/USDT:USDT", precio=63000.0, qty=0.01)
    # leverage isolated aplicado
    lev, sym, params = fx.leverage_calls[-1]
    assert lev == 3 and params.get("mgnMode") == "isolated"
    # orden short con SL adjunto a ~ +4% (stop arriba en un short)
    o = fx.orders[-1]
    assert o["side"] == "sell" and o["params"]["posSide"] == "short"
    assert o["params"]["tdMode"] == "isolated"
    sl = o["params"]["slTriggerPx"]
    assert abs(sl - 63000.0 * 1.04) < 1.0   # stop 4% arriba
    assert res["sl_order_id"]

def test_cerrar_short_es_reduce_only():
    m = _reload_main({"OKX_FUT_ENABLED": "true"})
    fx = FakeOkxFut()
    core = m.TradingCoreShortOkx(exchange=fx)
    core.abrir_short("BTC/USDT:USDT", precio=63000.0, qty=0.01)
    core.cerrar_short_market("BTC/USDT:USDT", qty=0.01)
    assert fx.orders[-1]["params"].get("reduceOnly") is True
    assert fx.orders[-1]["side"] == "buy"
    assert fx.fetch_positions() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest test_short_okx.py -k short_setea -v`
Expected: FAIL (`AttributeError: TradingCoreShortOkx`)

- [ ] **Step 3: Implementar el wrapper**

```python
# main.py
class TradingCoreShortOkx:
    """Wrapper OKX swap (linear perp) para la pata bajista. Isolated + SL server-side."""
    def __init__(self, exchange):
        self.ex = exchange          # ccxt.okx con options.defaultType='swap', o FakeOkxFut

    def _set_isolated_lev(self, symbol):
        self.ex.set_leverage(CONFIG["short_leverage"], symbol,
                             params={"mgnMode": "isolated", "posSide": "short"})

    def abrir_short(self, symbol, precio, qty):
        self._set_isolated_lev(symbol)
        sl_px = round(precio * (1 + CONFIG["short_sl_pct"]), 2)   # stop ARRIBA en short
        params = {
            "tdMode": "isolated", "posSide": "short",
            "slTriggerPx": sl_px, "slOrdPx": "-1",   # -1 = stop a mercado
        }
        # maker: limit sell ligeramente por encima del precio (vender en fuerza)
        px_entry = round(precio * 1.0004, 2)
        res = self.ex.create_order(symbol, "limit", "sell", qty, px_entry, params)
        sl_id = (res.get("info", {}) or {}).get("algoId") or res.get("id")
        return {"order_id": res.get("id"), "sl_order_id": sl_id,
                "entry_px": px_entry, "sl_px": sl_px}

    def cerrar_short_market(self, symbol, qty):
        params = {"tdMode": "isolated", "posSide": "short", "reduceOnly": True}
        return self.ex.create_order(symbol, "market", "buy", qty, None, params)

    def get_posiciones(self, symbols=None):
        return self.ex.fetch_positions(symbols)

    def usdt_disponible(self):
        bal = self.ex.fetch_balance()
        return float((bal.get("free") or {}).get("USDT", 0.0))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest test_short_okx.py -k "short_setea or reduce_only" -v`
Expected: PASS (2)

- [ ] **Step 5: Checkpoint** — wrapper de derivados con isolated + SL adjunto.

---

## Task 5: Señal de entrada (función pura)

**Files:**
- Modify: `main.py` (constantes de claves de indicador + método estático en `EscuadronShortOkx`, o función módulo `_senal_short`)
- Test: `test_short_okx.py`

> **VERIFICAR antes:** abrir `MotorIndicadores.get_indicators` en `main.py` y confirmar los nombres exactos de claves. Ajustar las `K_*` si difieren.

- [ ] **Step 1: Write the failing test**

```python
# test_short_okx.py
def _ind(close=62000, ema200=63000, adx=25, rsi=58, macdh=-0.5):
    return {"close": close, "EMA_200": ema200, "ADX_14": adx,
            "RSI_14": rsi, "MACDh_12_26_9": macdh}

def test_senal_short_dispara_en_rally_bajista():
    m = _reload_main({"OKX_FUT_ENABLED": "true"})
    # downtrend (close<ema200), tendencia (adx>20), rebote (rsi>50), momentum girando (macdh<0)
    assert m.senal_short(_ind()) is True

def test_senal_short_no_dispara():
    m = _reload_main({"OKX_FUT_ENABLED": "true"})
    assert m.senal_short(_ind(close=64000)) is False          # close>ema200 (no downtrend)
    assert m.senal_short(_ind(adx=15)) is False               # sin tendencia (chop)
    assert m.senal_short(_ind(rsi=45)) is False               # sin rebote
    assert m.senal_short(_ind(macdh=0.5)) is False            # momentum aún alcista
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest test_short_okx.py -k senal_short -v`
Expected: FAIL (`AttributeError: senal_short`)

- [ ] **Step 3: Implementar la señal**

```python
# main.py (constantes arriba con las demás)
K_CLOSE, K_EMA200, K_ADX, K_RSI, K_MACDH = "close", "EMA_200", "ADX_14", "RSI_14", "MACDh_12_26_9"
SHORT_RSI_MIN = 50.0
SHORT_ADX_MIN = 20.0

def senal_short(ind):
    """Rally que se agota dentro de un downtrend. Pura, testeable."""
    if not ind:
        return False
    close = float(ind.get(K_CLOSE, 0))
    ema200 = float(ind.get(K_EMA200, 0))
    adx = float(ind.get(K_ADX, 0))
    rsi = float(ind.get(K_RSI, 0))
    macdh = float(ind.get(K_MACDH, 0))
    if close <= 0 or ema200 <= 0:
        return False
    return (close < ema200          # downtrend intacto
            and adx > SHORT_ADX_MIN  # hay tendencia, no chop
            and rsi > SHORT_RSI_MIN  # rebote local hacia resistencia
            and macdh < 0)           # momentum del rebote agotándose
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest test_short_okx.py -k senal_short -v`
Expected: PASS (2)

- [ ] **Step 5: Checkpoint** — señal de entrada aislada y testeada.

---

## Task 6: Sizing (respeta envelope + canary)

**Files:**
- Modify: `main.py` (función `calcular_size_short`)
- Test: `test_short_okx.py`

- [ ] **Step 1: Write the failing test**

```python
# test_short_okx.py
def test_size_respeta_equity_pct_y_leverage():
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_MAX_EQUITY_PCT": "0.18",
                      "SHORT_LEVERAGE": "3", "SHORT_CANARY_MODE": "false"})
    # equity 4000, 18% = 720 margen; notional = 720*3 = 2160; qty = 2160/precio
    qty = m.calcular_size_short(equity=4000.0, precio=60000.0, ml_mult=1.0)
    assert abs(qty - (4000*0.18*3)/60000.0) < 1e-6

def test_size_canary_usa_minimo():
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_CANARY_MODE": "true"})
    qty = m.calcular_size_short(equity=4000.0, precio=60000.0, ml_mult=1.0)
    assert qty == m.SHORT_QTY_MINIMA   # tamaño mínimo en canary
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest test_short_okx.py -k size -v`
Expected: FAIL (`AttributeError: calcular_size_short`)

- [ ] **Step 3: Implementar sizing**

```python
# main.py
SHORT_QTY_MINIMA = 0.001   # contratos/coin mínimos para el canary (ajustar al lote OKX en Task 11)

def calcular_size_short(equity, precio, ml_mult=1.0):
    if CONFIG["short_canary_mode"]:
        return SHORT_QTY_MINIMA
    margen = equity * CONFIG["short_max_equity_pct"]
    notional = margen * CONFIG["short_leverage"] * max(0.5, min(2.0, ml_mult))
    if precio <= 0:
        return 0.0
    return notional / precio
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest test_short_okx.py -k size -v`
Expected: PASS (2)

- [ ] **Step 5: Checkpoint** — sizing con cap de equity y modo canary.

---

## Task 7: `EscuadronShortOkx` — flujo de entrada

**Files:**
- Modify: `main.py` (nueva clase Thread; globals `short_lock` via `get_entry_lock`)
- Test: `test_short_okx.py`

- [ ] **Step 1: Write the failing test**

```python
# test_short_okx.py
def _montar_escuadron(m, fx, mm="BAJISTA", ind=None, precio=62000.0):
    core = m.TradingCoreShortOkx(exchange=fx)
    db = _db_memoria(m)
    esc = m.EscuadronShortOkx(core_short=core, db=db)
    # inyectar régimen y datos
    with m.regime_lock:
        m.modo_mercado = mm
    m.PRECIOS_RAM["okx"] = {"BTC/USDT:USDT": {"bid": precio, "ask": precio}}
    m._test_ind = ind if ind is not None else _ind(close=62000, ema200=63000)
    # monkeypatch de fuentes externas para el test
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest test_short_okx.py -k "entrada or doble" -v`
Expected: FAIL (`AttributeError: EscuadronShortOkx`)

- [ ] **Step 3: Implementar la clase (flujo de entrada)**

```python
# main.py
SHORT_COOLDOWN_S = 1800.0   # 30 min entre entradas por símbolo

class EscuadronShortOkx(threading.Thread):
    """Pata bajista: shortea rallies BTC/ETH en régimen BAJISTA. Isolated + SL server-side."""
    TICK_S = 60.0

    def __init__(self, core_short, db):
        super().__init__(name="EscuadronShortOkx", daemon=True)
        self.core = core_short
        self.db = db

    def _equity(self):
        # USDT disponible + earn (capital total para el cap de la pata)
        try:
            base = self.core.usdt_disponible()
        except Exception:
            base = 0.0
        return base + float(globals().get("earn_balance_usdt_okx", 0.0) or 0.0)

    def _gestionar(self, sym):
        with regime_lock:
            mm = modo_mercado
        if mm != "BAJISTA":
            return
        if is_panico_global() or is_vol_spike("okx", sym):
            return
        # ya hay short en este símbolo?
        if any(p["symbol"] == sym for p in self.db.short_get_posiciones()):
            return
        # límite de posiciones simultáneas
        if len(self.db.short_get_posiciones()) >= CONFIG["short_max_posiciones"]:
            return
        # canary: máx 1 posición global
        if CONFIG["short_canary_mode"] and len(self.db.short_get_posiciones()) >= 1:
            return
        # cooldown
        if (time.time() - self.db.short_cooldown(sym)) < SHORT_COOLDOWN_S:
            return
        ind = MotorIndicadores.get_indicators("okx", sym)
        if not senal_short(ind):
            return
        px = PRECIOS_RAM.get("okx", {}).get(sym)
        if not px or not is_price_fresh("okx", sym):
            return
        precio = px["bid"]
        lock = get_entry_lock("okx", sym)
        if not lock.acquire(timeout=CONFIG["entry_lock_timeout_ms"] / 1000.0):
            return
        try:
            qty = calcular_size_short(self._equity(), precio, ml_size_multiplier("okx", sym))
            if qty <= 0:
                return
            res = self.core.abrir_short(sym, precio, qty)
            self.db.short_abrir_posicion(sym, res["entry_px"], qty,
                                         CONFIG["short_leverage"], res["sl_order_id"])
            self.db.log_caja_negra("SHORT_OPEN", sym,
                f"qty {qty:.6f} @ {res['entry_px']:.2f} SL {res['sl_px']:.2f} lev {CONFIG['short_leverage']}x")
        finally:
            lock.release()

    def run(self):
        if not CONFIG["fut_enabled"]:
            return
        time.sleep(30.0)
        while not kill_switch.is_set():
            try:
                for sym in PARES_SHORT_OKX:
                    self._gestionar(sym)
            except Exception as e:
                logger_errores.error(f"EscuadronShortOkx loop: {e}")
            for _ in range(int(self.TICK_S)):
                if kill_switch.is_set():
                    return
                time.sleep(1.0)
```

Agregar la constante de pares cerca de `PARES_OKX_SPOT`:
```python
PARES_SHORT_OKX = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest test_short_okx.py -k "entrada or doble" -v`
Expected: PASS (3)

- [ ] **Step 5: Checkpoint** — entrada gateada por régimen + señal + guards.

---

## Task 8: Salidas — flip de régimen + TP tiers

**Files:**
- Modify: `main.py` (`EscuadronShortOkx`: método `_gestionar_salidas`)
- Test: `test_short_okx.py`

- [ ] **Step 1: Write the failing test**

```python
# test_short_okx.py
def test_flip_a_alcista_cierra_shorts():
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_CANARY_MODE": "true"})
    fx = FakeOkxFut(precio=62000.0)
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA")
    esc._gestionar("BTC/USDT:USDT")
    assert len(db.short_get_posiciones()) == 1
    # régimen flipea a alcista
    with m.regime_lock:
        m.modo_mercado = "ALCISTA"
    esc._gestionar_salidas()
    assert db.short_get_posiciones() == []
    assert fx.orders[-1]["params"].get("reduceOnly") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest test_short_okx.py -k flip -v`
Expected: FAIL (`AttributeError: _gestionar_salidas`)

- [ ] **Step 3: Implementar salidas**

```python
# main.py — dentro de EscuadronShortOkx
    def _precio(self, sym):
        px = PRECIOS_RAM.get("okx", {}).get(sym)
        return px["bid"] if px else 0.0

    def _cerrar(self, pos, motivo):
        sym = pos["symbol"]
        try:
            self.core.cerrar_short_market(sym, pos["qty"])
        except Exception as e:
            logger_errores.error(f"SHORT cerrar {sym}: {e}")
            return
        px = self._precio(sym) or pos["entry_px"]
        pnl = (pos["entry_px"] - px) * pos["qty"]   # short: gana si baja
        self.db.short_cerrar_posicion(sym, px, pnl)
        self.db.log_caja_negra("SHORT_CLOSE", sym, f"{motivo} pnl ${pnl:.2f} @ {px:.2f}")

    def _gestionar_salidas(self):
        with regime_lock:
            mm = modo_mercado
        for pos in self.db.short_get_posiciones():
            sym = pos["symbol"]
            # 1) flip de régimen
            if mm != "BAJISTA":
                self._cerrar(pos, "flip-regimen")
                continue
            # 2) TP tiers (short gana cuando el precio baja respecto al entry)
            px = self._precio(sym)
            if px <= 0:
                continue
            gain_pct = (pos["entry_px"] - px) / pos["entry_px"] * 100
            tier = next((t for t in CONFIG["short_tp_tiers"] if gain_pct >= t), None)
            if tier is not None:
                self._cerrar(pos, f"tp-{tier}%")
```

Y en `run()`, llamar a salidas antes de entradas en cada tick:
```python
            try:
                self._gestionar_salidas()
                for sym in PARES_SHORT_OKX:
                    self._gestionar(sym)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest test_short_okx.py -k "flip or senal or entrada or doble" -v`
Expected: PASS

- [ ] **Step 5: Checkpoint** — salidas por flip y TP (el SL ya vive server-side en OKX).

---

## Task 9: Circuit breaker + KILL

**Files:**
- Modify: `main.py` (`EscuadronShortOkx`: cap diario en `_gestionar`; KILL en `run`/`graceful_shutdown`)
- Test: `test_short_okx.py`

- [ ] **Step 1: Write the failing test**

```python
# test_short_okx.py
def test_circuit_breaker_corta_tras_cap():
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_CANARY_MODE": "true",
                      "SHORT_DAILY_LOSS_CAP_USD": "60"})
    fx = FakeOkxFut(precio=62000.0)
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA")
    # simular pérdida del día por encima del cap
    db.short_cerrar_posicion("ETH/USDT:USDT", exit_px=0, pnl=-70.0)
    esc._gestionar("BTC/USDT:USDT")
    assert db.short_get_posiciones() == []   # no abre, breaker activo

def test_kill_cierra_todo():
    m = _reload_main({"OKX_FUT_ENABLED": "true", "SHORT_CANARY_MODE": "true"})
    fx = FakeOkxFut(precio=62000.0)
    esc, fx, db = _montar_escuadron(m, fx, mm="BAJISTA")
    esc._gestionar("BTC/USDT:USDT")
    esc.cerrar_todo_kill()
    assert db.short_get_posiciones() == []
    assert fx.orders[-1]["params"].get("reduceOnly") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest test_short_okx.py -k "circuit or kill" -v`
Expected: FAIL

- [ ] **Step 3: Implementar breaker + kill**

En `_gestionar`, al inicio (tras el check de régimen):
```python
        if self.db.short_pnl_hoy() <= -CONFIG["short_daily_loss_cap_usd"]:
            return   # circuit breaker: cap de pérdida diaria alcanzado
```
Nuevo método:
```python
    def cerrar_todo_kill(self):
        for pos in self.db.short_get_posiciones():
            self._cerrar(pos, "KILL")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest test_short_okx.py -k "circuit or kill" -v`
Expected: PASS (2)

- [ ] **Step 5: Checkpoint** — breaker diario + cierre total por KILL.

---

## Task 10: Integración en `main()` + HUD + shutdown

**Files:**
- Modify: `main.py` (`main()`: instanciar core swap + thread; `KillSwitchMonitor`/`graceful_shutdown`: llamar `cerrar_todo_kill`; `RadarTelemetria`: panel short)
- Test: `test_short_okx.py` (smoke de import + conteo de hilos)

- [ ] **Step 1: Write the failing test**

```python
# test_short_okx.py
def test_import_y_clases_presentes():
    m = _reload_main({"OKX_FUT_ENABLED": "true"})
    assert hasattr(m, "EscuadronShortOkx")
    assert hasattr(m, "TradingCoreShortOkx")
    assert "BTC/USDT:USDT" in m.PARES_SHORT_OKX
```

- [ ] **Step 2: Run test to verify it fails / passes**

Run: `python -m pytest test_short_okx.py -k import_y_clases -v`
Expected: PASS si Tasks 4-9 están integradas (clases ya en `main.py`).

- [ ] **Step 3: Instanciar en `main()`**

Donde se arma `core_okx`, crear el exchange swap y el thread (solo si `fut_enabled`):
```python
    hilos_short = []
    if CONFIG["fut_enabled"]:
        ex_swap = ccxt.okx({
            "apiKey": os.getenv("OKX_API_KEY"),
            "secret": os.getenv("OKX_API_SECRET"),
            "password": os.getenv("OKX_PASSPHRASE"),
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })
        core_short = TradingCoreShortOkx(ex_swap)
        esc_short = EscuadronShortOkx(core_short, db)
        hilos_short = [esc_short]
        globals()["esc_short_ref"] = esc_short   # para graceful_shutdown
```
Sumar a la lista de hilos y al conteo:
```python
    todos = hilos_okx + hilos_short
    total = 5 + len(todos)
```
Actualizar el log de arranque ("Quantum OKX-only arrancado con N hilos...") y arrancar `hilos_short`.

- [ ] **Step 4: KILL en shutdown**

En `graceful_shutdown` (y/o donde el `KillSwitchMonitor` actúa), antes de salir:
```python
    ref = globals().get("esc_short_ref")
    if ref is not None:
        try:
            ref.cerrar_todo_kill()
        except Exception as e:
            logger_errores.error(f"shutdown short: {e}")
```

- [ ] **Step 5: Panel HUD**

En `RadarTelemetria`, agregar panel compacto "PATA SHORT": por cada `short_get_posiciones()` mostrar symbol, entry, qty, uPnL aproximado (`(entry - bid) * qty`), y `pnl_hoy`. Si `fut_enabled` es false, omitir panel.

- [ ] **Step 6: Run test + smoke import**

Run: `python -c "import main; print('OK', main.PARES_SHORT_OKX)"`
Expected: imprime `OK ['BTC/USDT:USDT', 'ETH/USDT:USDT']` sin excepción.
Run: `python -m pytest test_short_okx.py -v`
Expected: toda la suite PASS.

- [ ] **Step 7: Checkpoint** — pata short integrada, 12→13 hilos, KILL/HUD conectados.

---

## Task 11: Canary — lote real y runbook de verificación

**Files:**
- Modify: `main.py` (`SHORT_QTY_MINIMA` → lote mínimo real de OKX por símbolo vía `amount_to_precision`/`market.limits`)
- Crear: `canary_short.md` (runbook de verificación manual)

- [ ] **Step 1: Ajustar lote mínimo al de OKX**

En `abrir_short`, cuantizar la qty al lote del mercado antes de ordenar:
```python
        qty = float(self.ex.amount_to_precision(symbol, qty)) if hasattr(self.ex, "amount_to_precision") else qty
```
(El `FakeOkxFut` no tiene `amount_to_precision` → el `hasattr` lo saltea en tests.)
Setear `SHORT_QTY_MINIMA` al mínimo real (BTC-USDT-SWAP: 0.01 contrato = 0.0001 BTC según OKX; confirmar con `ex.market('BTC/USDT:USDT')['limits']['amount']['min']`).

- [ ] **Step 2: Escribir runbook `canary_short.md`**

Contenido: pasos para el primer despliegue real con `SHORT_CANARY_MODE=true`:
1. `.env`: `OKX_FUT_ENABLED=true`, `SHORT_CANARY_MODE=true`. Bot spot apagado primero si hay riesgo de conflicto (mismo proceso → reiniciar limpio).
2. Confirmar permiso **Trade** sobre derivados en la API key OKX y que la cuenta esté en modo de margen que habilite swaps.
3. Arrancar el bot. Esperar a que el régimen sea BAJISTA y dispare 1 short mínimo.
4. **Verificación manual en OKX (web/app):**
   - posición BTC/ETH-USDT-SWAP en **ISOLATED** (no cross),
   - **SL colgado** (algo-order activa) a ~+4%,
   - leverage = 3x.
5. Si los 3 OK → `SHORT_CANARY_MODE=false`, reiniciar → opera full envelope.
6. Si algo falla → KILL (`touch KILL`), revisar `caja_negra` y `errors.log`, corregir antes de reintentar.

- [ ] **Step 3: Checkpoint** — lote real correcto + runbook de canary listo.

---

## Task 12: Documentación

**Files:**
- Modify: `.env.example`, `CLAUDE.md`, `bot.txt`, memoria del proyecto

- [ ] **Step 1: `.env.example`**

Agregar el bloque de config short (Task 1, §7 del spec) con comentarios.

- [ ] **Step 2: `CLAUDE.md`**

Nueva subsección "## Pata Bajista V1.7.0 (EscuadronShortOkx)": qué es, gate de régimen, envelope conservador, canary. Actualizar:
- tabla de alcance (agregar fila SHORT futuros BTC/ETH, gateado BAJISTA),
- "Lo que NO hay que hacer": matizar la regla de "no reintroducir Futuros" → "no reintroducir FUT *direccional sin* leverage bajo + stop server-side + gate de régimen (ver pata bajista V1.7.0)",
- bump V1.6.0 → V1.7.0, conteo de hilos 12 → 13.

- [ ] **Step 3: `bot.txt`**

Documentar `TradingCoreShortOkx`, `EscuadronShortOkx`, esquema `short_*`, config, flujo de canary. (Regla operacional: cambio significativo ⇒ bot.txt en el mismo turno.)

- [ ] **Step 4: Memoria del proyecto**

Actualizar/crear memoria: reapertura consciente de futuros (pata bajista disciplinada), su racional y el link a `[[okx-only-simplification]]`.

- [ ] **Step 5: Checkpoint** — docs y memoria al día con V1.7.0.

---

## Self-review (cobertura del spec)

- §3 arquitectura → Tasks 4,7,10 ✓
- §4 riesgo/envelope (isolated, SL, leverage, cap diario, max pos) → Tasks 4,7,9 ✓
- §5 señal de entrada → Task 5 ✓
- §6 real + canary → Tasks 6,11 ✓
- §7 config → Task 1 ✓
- §8 testing FakeOkxFut → Tasks 3-9 ✓
- §9 docs → Task 12 ✓
- §10 riesgos (stop verificado, isolated, KILL) → Tasks 4,9,11 ✓
- Salidas (flip, TP) → Task 8 ✓

Sin placeholders de código. Nombres consistentes (`abrir_short`/`cerrar_short_market`/`short_get_posiciones`/`senal_short`/`calcular_size_short`/`cerrar_todo_kill`/`PARES_SHORT_OKX`) entre tasks. Único punto a verificar contra `main.py` antes de codear: claves de `MotorIndicadores.get_indicators` (Task 5, constantes `K_*`).
