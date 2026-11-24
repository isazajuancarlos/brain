# Ladder Grid SP3 — Implementation Plan (sleeve live `EscuadronLadderOkx`, canary)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development o superpowers:executing-plans para implementar tarea por tarea. Los steps usan checkbox (`- [ ]`).
> **Este repo NO es git en el entorno local (download-only del VPS).** Los steps "Commit" = checkpoint lógico (verificar suite verde antes de seguir). El versionado/deploy real vive en el VPS = acción del usuario.

**Goal:** Construir un sleeve spot aislado que opere un grid ladder round-trip sobre LTC/USDT en modo canary ($100), cuyo deliverable es medir fill-real vs fill-ideal antes de escalar.

**Architecture:** Hilo daemon `EscuadronLadderOkx` additivo (no toca `EscuadronGridOkx` core). Gateado por el régimen PROPIO de LTC (`clasificar_regimen` sobre velas diarias de LTC). Coloca compras maker en escalera, vende round-trip a +spacing, stopea rungs colgados, flatten al flip de régimen. Estado en tablas `ladder_*`; OKX = fuente de verdad vía reconcile boot+tick. Reusa `core_okx.execute_limit_maker/execute_limit_sell_harvest` (spot, sin core swap).

**Tech Stack:** Python 3.13, ccxt (OKX spot), sqlite3 WAL (`quantum.db`), threading daemon, pytest contra `FakeOkxSpot`. Funciones puras + inyección para tests deterministas.

## Global Constraints

- `LADDER_ENABLED` default **False**; encender es decisión explícita + deploy al VPS.
- Sleeve **spot puro, sin leverage**. Capital cap `LADDER_MAX_CAPITAL` (default 100) **por par**; `validar_config` aborta si `cap × n_pares` > 500 totales de arranque.
- Par canary: **LTC/USDT** únicamente (`LADDER_PARES`). No toca BTC/ETH grid core.
- Spec autoritativa: `ladder_grid_sp3_spec.md`. Reflejar el cambio en `bot.txt` + `CLAUDE.md` (regla operacional).
- Toda llamada OKX bajo `okx_retry` + `try/except` con log (piso fail-safe). Idioma: español (identificadores incluidos).
- TDD red→green; defaults preservan call-sites/tests; `conftest.py` resetea flags opt-in.

---

### Task 1: Config keys + `validar_config` envelope

**Files:**
- Modify: `main.py` (bloque CONFIG, ~línea 90-230; `validar_config`, ~1290-1320)
- Modify: `.env.example` (agregar keys LADDER_*)
- Test: `test_ladder_okx.py` (nuevo)

**Interfaces:**
- Produces: `CONFIG["ladder_enabled"]`(bool), `CONFIG["ladder_canary_mode"]`(bool), `CONFIG["ladder_pares"]`(list[str]), `CONFIG["ladder_max_capital"]`(float), `CONFIG["ladder_rungs"]`(int), `CONFIG["ladder_spacing_pct"]`(float), `CONFIG["ladder_stop_pct"]`(float), `CONFIG["ladder_daily_loss_cap"]`(float), `CONFIG["ladder_tick_s"]`(float). Helper `_construir_pares_ladder()`.

- [ ] **Step 1: Write failing test**

```python
# test_ladder_okx.py
import main

def test_validar_config_aborta_capital_excesivo(monkeypatch):
    monkeypatch.setitem(main.CONFIG, "ladder_enabled", True)
    monkeypatch.setitem(main.CONFIG, "ladder_max_capital", 600.0)
    errores = main.validar_config()
    assert any("LADDER_MAX_CAPITAL" in e for e in errores)

def test_validar_config_ok_envelope_chico(monkeypatch):
    monkeypatch.setitem(main.CONFIG, "ladder_enabled", True)
    monkeypatch.setitem(main.CONFIG, "ladder_max_capital", 100.0)
    monkeypatch.setitem(main.CONFIG, "ladder_pares", ["LTC/USDT"])
    errores = main.validar_config()
    assert not any("LADDER" in e for e in errores)
```

- [ ] **Step 2: Run, verify fail** — `pytest test_ladder_okx.py -v` → FAIL (keys/validación no existen).

- [ ] **Step 3: Implement** — en el bloque CONFIG agregar:

```python
    "ladder_enabled":         _env_bool("LADDER_ENABLED", False),
    "ladder_canary_mode":     _env_bool("LADDER_CANARY_MODE", True),
    "ladder_pares":           _construir_pares_ladder(),
    "ladder_max_capital":     _env_float("LADDER_MAX_CAPITAL", 100.0),
    "ladder_rungs":           int(os.getenv("LADDER_RUNGS", 4)),
    "ladder_spacing_pct":     _env_float("LADDER_SPACING_PCT", 1.5),
    "ladder_stop_pct":        _env_float("LADDER_STOP_PCT", 15.0),
    "ladder_daily_loss_cap":  _env_float("LADDER_DAILY_LOSS_CAP", 15.0),
    "ladder_tick_s":          _env_float("LADDER_TICK_S", 60.0),
```

`_construir_pares_ladder()` (cerca de `_construir_universo_okx`):

```python
def _construir_pares_ladder():
    raw = os.getenv("LADDER_PARES", "LTC/USDT")
    pares = [p.strip().upper() for p in raw.split(",") if p.strip()]
    return [p if "/" in p else f"{p}/USDT" for p in pares]
```

En `validar_config`, dentro del bloque que arma `errores`:

```python
    if CONFIG["ladder_enabled"]:
        total_max = CONFIG["ladder_max_capital"] * max(1, len(PARES_LADDER))
        if total_max > 500.0:
            errores.append(f"LADDER_MAX_CAPITAL fuera del envelope: {len(PARES_LADDER)} par(es) x ...")
        if not CONFIG["ladder_pares"]:
            errores.append("LADDER_ENABLED=true pero LADDER_PARES vacío")
        if not CONFIG["ladder_canary_mode"]:
            warnings.append("LADDER_CANARY_MODE=false: capital real escalado sobre el ladder")
```

(usar la lista `warnings` existente si la hay; si no, log de warning.)

- [ ] **Step 4: Run, verify pass** — `pytest test_ladder_okx.py -v` → PASS.
- [ ] **Step 5: Checkpoint** — agregar las keys a `.env.example`; suite completa verde.

---

### Task 2: Schema DB `ladder_*`

**Files:**
- Modify: `main.py` (`DatabaseManager._crear_tablas` o equivalente, donde se crean short_*/okx_*)
- Test: `test_ladder_okx.py`

**Interfaces:**
- Produces: tablas `ladder_orders`, `ladder_positions`, `ladder_realized`, `ladder_fill_log`. Métodos `db.ladder_registrar_orden/ladder_marcar_fill/ladder_registrar_realizado/ladder_fill_resumen`.

- [ ] **Step 1: Failing test**

```python
def test_db_crea_tablas_ladder(tmp_path):
    db = main.DatabaseManager(str(tmp_path / "t.db"))
    cols = db._exec("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ladder_%'", fetch="all")
    nombres = {c[0] for c in cols}
    assert {"ladder_orders", "ladder_positions", "ladder_realized", "ladder_fill_log"} <= nombres
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement** — junto a las otras CREATE TABLE:

```python
        cur.execute("CREATE TABLE IF NOT EXISTS ladder_orders ("
                    "order_id TEXT PRIMARY KEY, symbol TEXT, side TEXT, rung_idx INTEGER, "
                    "price REAL, qty REAL, state TEXT, ts REAL)")
        cur.execute("CREATE TABLE IF NOT EXISTS ladder_positions ("
                    "symbol TEXT, rung_idx INTEGER, qty REAL, avg_cost REAL, "
                    "sell_order_id TEXT, bought_ts REAL, PRIMARY KEY(symbol, rung_idx))")
        cur.execute("CREATE TABLE IF NOT EXISTS ladder_realized ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, qty REAL, buy_px REAL, "
                    "sell_px REAL, pnl_neto REAL, motivo TEXT, ts REAL)")
        cur.execute("CREATE TABLE IF NOT EXISTS ladder_fill_log ("
                    "order_id TEXT PRIMARY KEY, symbol TEXT, rung_idx INTEGER, price REAL, "
                    "ts_colocado REAL, ts_tocado REAL, lleno INTEGER DEFAULT 0, ts_lleno REAL)")
```

Métodos en `DatabaseManager` (espejo de los `short_*`): `ladder_registrar_orden(order_id, symbol, side, rung_idx, price, qty)`, `ladder_marcar_fill(order_id, ts_lleno)`, `ladder_registrar_realizado(symbol, qty, buy_px, sell_px, pnl_neto, motivo)`, `ladder_fill_resumen()` → dict `{colocados, tocados, llenos, fill_rate}`.

- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Checkpoint.**

---

### Task 3: Evaluador de régimen PROPIO live (`regimen_propio`)

**Files:**
- Modify: `main.py` (cerca de `clasificar_regimen` / `Selector`)
- Test: `test_ladder_okx.py`

**Interfaces:**
- Consumes: `clasificar_regimen(dict)->（estado, conf)`, `SELECTOR_EMA_SLOPE_DAYS`.
- Produces: `regimen_propio(velas_diarias)->str` (estado 4-clases), PURA sobre una lista de velas OHLCV diarias.

- [ ] **Step 1: Failing test**

```python
def test_regimen_propio_downtrend():
    # serie diaria claramente bajista → TENDENCIA_DOWN (se congela el ladder)
    velas = [[i*86400000, 100-i*0.5, 100-i*0.5, 99-i*0.5, 100-i*0.5, 10] for i in range(260)]
    assert main.regimen_propio(velas) == "TENDENCIA_DOWN"
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement** — función pura que calcula desv/slope/adx/atr de la última vela (idéntico a `_regimen_estado_por_ts` de entrenar.py pero in-process, usando pandas_ta) y llama `clasificar_regimen`. Devuelve el estado; si datos insuficientes → "CAOS" (fail-safe: no opera).

- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Checkpoint.**

---

### Task 4: Planner puro del ladder (`ladder_acciones`)

**Files:**
- Modify: `main.py`
- Test: `test_ladder_okx.py`

**Interfaces:**
- Produces: `ladder_acciones(estado_ladder, precio, regimen, cfg)->list[Accion]` donde `Accion` = dict `{"tipo": "buy"|"sell"|"stop"|"flatten"|"rearm", "rung_idx", "price", "qty"}`. PURA: dado el estado actual de los rungs + precio + régimen + config, devuelve las órdenes a ejecutar este tick. Espejo de la lógica de `sim_grid_ladder` pero como planner (no ejecuta).

- [ ] **Step 1: Failing tests** (varios):

```python
def _cfg(): return {"rungs": 4, "spacing_pct": 1.5, "stop_pct": 15.0,
                    "ref": 100.0, "monto_rung": 20.0}

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
    acc = main.ladder_acciones(estado, 80.0, "RANGO", _cfg())  # -20% > stop 15%
    assert any(a["tipo"] == "stop" for a in acc)
```

- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** `ladder_acciones` reproduciendo la mecánica del sim (re-center en entrada a RANGO, buys armados, stop underwater, flatten al salir). Sin I/O.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Checkpoint.**

---

### Task 5: `FakeOkxSpot` + ejecución de acciones (`_ejecutar_accion`)

**Files:**
- Modify: `main.py` (`EscuadronLadderOkx._ejecutar_accion`)
- Test: `test_ladder_okx.py` (define `FakeOkxSpot`)

**Interfaces:**
- Consumes: `core_okx.execute_limit_maker(sym, price, monto)->order_id|None`, `execute_limit_sell_harvest(sym, price, qty)->res`.
- Produces: `EscuadronLadderOkx._ejecutar_accion(accion)` que traduce una `Accion` del planner a la llamada OKX correspondiente (bajo `okx_retry`), persiste en `ladder_orders`/`ladder_fill_log`.

- [ ] **Step 1:** Definir `FakeOkxSpot` (registra órdenes, simula fills on demand) y test: una `Accion {"tipo":"buy",...}` → llama `execute_limit_maker` y registra la orden + fila en `ladder_fill_log`.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** `_ejecutar_accion` (dispatch por `accion["tipo"]`).
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Checkpoint.**

---

### Task 6: `reconciliar_ladder` (boot + tick)

**Files:**
- Modify: `main.py` (`EscuadronLadderOkx.reconciliar_ladder`)
- Test: `test_ladder_okx.py`

**Interfaces:**
- Consumes: estado de órdenes OKX (vía `FakeOkxSpot.fetch_orders`/equiv).
- Produces: `reconciliar_ladder(contexto="boot")` — sincroniza rungs desde OKX (verdad): buy lleno → registra fill (`ladder_marcar_fill`) y arma el sell; sell lleno → `ladder_registrar_realizado` + re-arma. Grace para órdenes recién puestas.

- [ ] **Step 1:** test: un buy marcado lleno en el fake → reconcile registra el fill y crea la `ladder_positions`; un sell lleno → registra realizado y libera el rung.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** (espejo de `reconciliar_shorts`).
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Checkpoint.**

---

### Task 7: `EscuadronLadderOkx` thread (loop) + canary cap + cap pérdida diaria

**Files:**
- Modify: `main.py` (clase `EscuadronLadderOkx`)
- Test: `test_ladder_okx.py`

**Interfaces:**
- Consumes: Tasks 3-6 (`regimen_propio`, `ladder_acciones`, `_ejecutar_accion`, `reconciliar_ladder`).
- Produces: `EscuadronLadderOkx(core_okx, db)` con `_tick()` (testeable sin hilo) y `run()`.

- [ ] **Step 1:** test de `_tick()`: en RANGO sin posición arma ≤ N rungs y no compromete > `ladder_max_capital`; supera cap de pérdida diaria → no opera.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** `_tick` (reconcile → régimen → planner → filtrar por canary cap / panic / loss cap → ejecutar) y `run` (loop `LADDER_TICK_S`, respeta `kill_switch`).
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Checkpoint.**

---

### Task 8: Wiring en `main()` + HUD + docs

**Files:**
- Modify: `main.py` (sección de arranque de hilos OKX ~4885-4918; HUD ~2620-2660)
- Modify: `bot.txt`, `CLAUDE.md`
- Test: `test_ladder_okx.py` (smoke de arranque condicional)

**Interfaces:**
- Consumes: `EscuadronLadderOkx`, `CONFIG["ladder_enabled"]`.

- [ ] **Step 1:** test: con `ladder_enabled=False` no se instancia el sleeve; con True sí (smoke via flag).
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** — en `main()`, tras el bloque short:

```python
    hilos_ladder = []
    if CONFIG["ladder_enabled"]:
        esc_ladder = EscuadronLadderOkx(core_okx, db)
        esc_ladder.start()
        hilos_ladder = [esc_ladder]
        cy = " [CANARY]" if CONFIG["ladder_canary_mode"] else ""
        console.print(f"[green]▤ Ladder sleeve ACTIVO — {','.join(CONFIG['ladder_pares'])} "
                      f"spot, cap ${CONFIG['ladder_max_capital']:.0f}{cy}[/]")
```

Línea de HUD (estado régimen LTC, rungs activos, capital comprometido, fill_rate, PnL día). Actualizar `bot.txt` (bloque V1.9.0) y `CLAUDE.md` (NOTA V1.9.0 + sección sleeve).

- [ ] **Step 4: Run, verify pass** — suite completa verde.
- [ ] **Step 5: Checkpoint** — runbook canary `canary_ladder.md` (espejo de `canary_short.md`).

---

## Self-Review

**Spec coverage:** §4.1 EscuadronLadderOkx→T7; §4.2 régimen propio→T3; §4.3 órdenes→T5; §4.4 fill_log→T2+T5; §4.5 DB→T2; §4.6 reconcile→T6; §4.7 protección (stop/flatten/panic/KILL/loss cap)→T4(planner)+T7(loop); §4.8 validar_config→T1; §4.9 HUD→T8; §5 config keys→T1; §6 graduación = config/operación (no código, post-canary); §8 testing→cada task. Cubierto.

**Placeholder scan:** los tasks de integración (T5-T8) describen interfaz + step de test concreto + dónde implementar; el código pleno del loop se materializa en la implementación (es glue de los puros T3/T4 ya con código). Sin TBD.

**Type consistency:** `Accion` dict con `tipo/rung_idx/price/qty` consistente T4→T5; `ladder_*` métodos de DB consistentes T2→T5/T6; `regimen_propio` devuelve str usado por T4/T7.

## Decomposición / nota de ejecución

8 tasks, cada una con deliverable testeable contra `FakeOkxSpot` (cero capital real). El orden respeta dependencias (puros 1-4 → ejecución 5-6 → loop 7 → wiring 8). Post-merge: deploy al VPS con `LADDER_ENABLED=true`+`LADDER_CANARY_MODE=true` (acción del usuario) y seguir el runbook canary.
