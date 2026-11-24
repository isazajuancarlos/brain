# Reducir main.py a OKX spot+earn — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dejar `main.py` operando trading solo en OKX (spot+earn), eliminando Binance-trading, Futuros y ARB; Binance queda como fuente de datos del cerebro de régimen.

**Architecture:** Refactor de borrado incremental. Cada task elimina un subsistema completo (clase(s) + config + wiring en `main()` + HUD + DB) dejando `main.py` compilable. `TradingCoreBin` se reduce a `FuenteBinance` (solo `exchange_read`). Verificación: compilar + grep de símbolos removidos (deben dar 0) + boot smoke test final.

**Tech Stack:** Python 3, ccxt. Spec: `eliminar_bin_fut_arb_spec.md`.

> **No hay harness de tests para el bot** (es un proceso de hilos en vivo). La verificación de cada task son **guardas estáticas**: `ast.parse` (compila) + `grep` de los símbolos borrados. El test funcional real es el **boot smoke test** del Task 6, que corre el usuario.

> **Git:** workspace sin git. Pasos `commit` = checkpoints opcionales (`git init` una vez si querés versionar, o salteá).

> **Líneas se corren al borrar.** Usá los nombres de clase/función como ancla y re-`grep` antes de cada edición; los números de línea son de referencia al momento de escribir el plan.

> **Backup antes de empezar:** `cp main.py main.py.bak` (para diff/rollback si algo se rompe).

---

### Task 0: Backup y baseline

**Files:**
- Create: `main.py.bak`

- [ ] **Step 1: Backup**

Run: `cp main.py main.py.bak`

- [ ] **Step 2: Baseline compila**

Run: `./venv/bin/python -c "import ast; ast.parse(open('main.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Baseline conteo de clases (referencia)**

Run: `grep -c "^class " main.py`
Expected: imprime un número (anotalo; al final debe bajar en ~11 clases).

---

### Task 1: Eliminar ARB

**Files:**
- Modify: `main.py`
- Delete: `catalyst_signal.json`

ARB = `FundingScanner` (5412), `ArbExecutor` (5641), config `arb_*` (220-254), wiring `hilos_arb` en `main()` (6396-6399), refs en HUD (`arb_realized_pnl`), tablas `arb_*` en DB, fallback catalyst.

- [ ] **Step 1: Borrar las clases `FundingScanner` y `ArbExecutor`**

Borrar completas ambas clases (desde `class FundingScanner(threading.Thread):` hasta el final de `ArbExecutor`, justo antes de la región de `main()`/helpers de shutdown). Re-`grep -n "^class FundingScanner\|^class ArbExecutor" main.py` para anclar las líneas exactas antes de borrar.

- [ ] **Step 2: Borrar el wiring ARB en `main()`**

Borrar el bloque (main.py:6396-6399):
```python
    # 10. Hilos ARB
    hilos_arb = [FundingScanner(core_bin, core_okx, db)]
    if CONFIG["arb_live"]:
        hilos_arb.append(ArbExecutor(core_bin, core_okx, db))
```
Y en la línea de arranque, quitar `+ hilos_arb`:
```python
    for h in hilos_spot + hilos_okx + hilos_fut + hilos_arb:
```
→ (quedará así tras tasks siguientes; por ahora):
```python
    for h in hilos_spot + hilos_okx + hilos_fut:
```
Y en el conteo `total`, quitar `+ len(hilos_arb)`.

- [ ] **Step 3: Borrar la config `arb_*`**

Borrar todas las claves `"arb_..."` del dict `CONFIG` (main.py:220-254, el bloque ARB completo). Re-grep `'"arb_'` para confirmar el rango.

- [ ] **Step 4: Borrar el fallback catalyst hardcoded**

Borrar el bloque "Catalyst v5 fallback" (main.py:323 y su estructura asociada) y cualquier `arb_catalyst_path` / carga de `catalyst_signal.json`.

- [ ] **Step 5: Quitar ARB del HUD**

En `RadarTelemetria`: quitar las lecturas de `arb_realized_pnl` y los términos `arb`/`arb_pnl`/`realized_arb` de `_resumen_pnl` y del cálculo de equity (líneas ~1726-1740, ~1803-1810). El equity pierde el sumando `arb`.

- [ ] **Step 6: Quitar creación de tablas `arb_*` en DB**

En `DatabaseManager`, quitar los `CREATE TABLE ... arb_realized_pnl` / `arb_signals` y cualquier tabla `arb_*`. (Las tablas existentes en `quantum.db` quedan; solo se deja de crearlas.)

- [ ] **Step 7: Borrar `catalyst_signal.json`**

Run: `rm catalyst_signal.json`

- [ ] **Step 8: Verificar — compila + sin símbolos ARB**

Run: `./venv/bin/python -c "import ast; ast.parse(open('main.py').read()); print('OK')"`
Expected: `OK`

Run: `grep -niE "FundingScanner|ArbExecutor|arb_live|arb_realized|arb_signals|hilos_arb|catalyst" main.py | grep -v "^[0-9]*:.*#"`
Expected: 0 líneas (o solo comentarios históricos que decidas dejar).

- [ ] **Step 9: Commit (opcional)**

```bash
git add main.py && git rm catalyst_signal.json && git commit -m "remove ARB (FundingScanner/ArbExecutor/config/HUD/DB)"
```

---

### Task 2: Eliminar Futuros

**Files:**
- Modify: `main.py`
- Delete: `test_fut_keys.py`

FUT = `SensorPosicionesFut` (5207), `CircuitBreakerFut` (5267), `WalletBridge` (5329, transferencias spot↔fut para ARB), helpers `_fut_*` + estado fut en `TradingCoreBin`, `_init_futuros`, `get_fut_balance`, `sincronizar_fut_arranque`, config FUT (168-182), bloque FUT de `validar_config` (835-851), `hilos_fut` en `main()`, `_tabla_fut` en HUD.

- [ ] **Step 1: Borrar las clases `SensorPosicionesFut`, `CircuitBreakerFut`, `WalletBridge`**

Borrar las 3 clases completas (5207-5411). Re-grep para anclar.

- [ ] **Step 2: Borrar helpers y estado FUT de `TradingCoreBin`**

En `TradingCoreBin.__init__` borrar: `exchange_fut_trade/read/public`, `posiciones_fut_RAM`, `fut_symbol_info`, `reserved_notional_fut_global/per_sym`, `circuit_breaker_activo`, `fut_balance_cache`, `fut_balance_lock`, `api_fut_trade_lock`, `fut_sensor_evaluado`, y la llamada condicional `if CONFIG["futuros_habilitado"]: self._init_futuros()`.
Borrar los métodos: `_fut_host`, `_fut_to_binance_sym`, `_fut_sign`, `_fut_signed_request`, `_fut_public_request`, `_fut_step_round`, `_cargar_fut_symbol_info`, `_init_futuros`, `sincronizar_fut_arranque`, `get_fut_balance`, `invalidate_fut_balance`, `allocate_notional_fut`, `release_notional_fut`.

- [ ] **Step 3: Borrar wiring FUT en `main()`**

Borrar la llamada (main.py:6337-6338):
```python
    if CONFIG["futuros_habilitado"]:
        core_bin.sincronizar_fut_arranque()
```
Borrar el bloque `hilos_fut` (main.py:6384-6394) y quitar `+ hilos_fut` del loop de arranque y `+ len(hilos_fut)` del conteo.

- [ ] **Step 4: Borrar config FUT**

Borrar del dict `CONFIG` las claves: `futuros_habilitado`, `fut_sandbox`, `bin_fut_api_key`, `bin_fut_api_secret`, `fut_testnet_host`, `fut_prod_host`, `fut_simbolos`, `fut_leverage`, `fut_max_notional_per`, `fut_max_notional_glob`, `fut_margin_pct` (main.py:168-182).

- [ ] **Step 5: Borrar bloque FUT de `validar_config`**

Borrar el bloque "Futuros sandbox seguridad" (main.py:834-851), que referencia `fut_sandbox`/`bin_fut_api_key`.

- [ ] **Step 6: Quitar FUT del HUD**

Borrar el método `_tabla_fut` y su uso (`fut_section = self._tabla_fut() ...`, main.py:2172). Quitar el sumando `fut_m`/`fut_margin` del equity (`free_total = free_bin + free_okx + fut_m` → dejar `free_bin + free_okx` por ahora; el `free_bin` se va en Task 4) y las secciones FUT del HUD (`_tabla_fut`, posiciones_fut_RAM, FUT labels).

- [ ] **Step 7: Borrar `test_fut_keys.py`**

Run: `rm test_fut_keys.py`

- [ ] **Step 8: Verificar — compila + sin símbolos FUT**

Run: `./venv/bin/python -c "import ast; ast.parse(open('main.py').read()); print('OK')"`
Expected: `OK`

Run: `grep -niE "_fut_|posiciones_fut|get_fut_balance|futuros_habilitado|fut_sandbox|SensorPosicionesFut|CircuitBreakerFut|WalletBridge|_init_futuros|_tabla_fut|fut_symbol_info|notional_fut" main.py | grep -v "#"`
Expected: 0 líneas.

- [ ] **Step 9: Commit (opcional)**

```bash
git add main.py && git rm test_fut_keys.py && git commit -m "remove Futuros (FUT helpers/threads/config/validar/HUD)"
```

---

### Task 3: Eliminar Binance trading + reducir `TradingCoreBin` → `FuenteBinance`

**Files:**
- Modify: `main.py`

BIN trading = `WebSocketFeedBin` (3150), `OmniRiskBin` (3233), `CentinelaAsincronoBin` (3389), `EscudoDCABin` (3479), `EscuadronGridBin` (3614), `GestorInventarioBin` (4861). `TradingCoreBin` se reduce a data-only y se renombra.

- [ ] **Step 1: Borrar las 6 clases de trading BIN**

Borrar completas: `WebSocketFeedBin`, `OmniRiskBin`, `CentinelaAsincronoBin`, `EscudoDCABin`, `EscuadronGridBin` (3150-3748) y `GestorInventarioBin` (4861-5206). Re-grep para anclar.

- [ ] **Step 2: Reducir `TradingCoreBin` a data-only y renombrar a `FuenteBinance`**

En la clase (2261-2807):
- Renombrar `class TradingCoreBin(TradingCoreBase):` → `class FuenteBinance(TradingCoreBase):` (o `(object)` si ya no usa nada de la base; verificar qué usa de `TradingCoreBase`).
- En `__init__`: conservar SOLO `exchange_read` (cliente ccxt binance para datos). Borrar `exchange_trade`, `api_trade_lock`, `inventario`.
- Borrar todos los métodos de trading: `execute_trade`, `allocate_budget`, `release_budget`, `reconciliar_portfolio`, `sincronizar_bin_spot_arranque`, `get_balance` (si solo lo usa trading), `_post_sell` y afines. Conservar lo que use `MotorIndicadores`/`RadarRegimen`/`Selector` (esencialmente `exchange_read`).

- [ ] **Step 3: Actualizar los 3 constructores que reciben el core BIN**

`MotorIndicadores(core_bin, core_okx, db)`, `RadarRegimen(core_bin, db)`, `Selector(core_bin, db)` siguen recibiendo el objeto; solo cambia el tipo. No hace falta renombrar el parámetro `core_bin` interno (puede quedar), pero el comentario de cada clase debe aclarar "fuente de datos Binance". Verificar que solo usan `self.core_bin.exchange_read` (ya confirmado en el spec).

- [ ] **Step 4: Actualizar `main()` — construcción y sync**

- `core_bin = TradingCoreBin(db)` → `fuente_bin = FuenteBinance(db)` (renombrar la variable o dejar `core_bin` apuntando a `FuenteBinance`; elegir uno y ser consistente).
- Borrar `core_bin.sincronizar_bin_spot_arranque()` (main.py:6335).
- Borrar el bloque `hilos_spot` (BIN, main.py:6356-6365) y el wireo `core_bin.inventario = inventario_bin`.
- Quitar `+ hilos_spot` del loop de arranque y `+ len(hilos_spot)` del conteo.
- Pasar `fuente_bin` a `MotorIndicadores`/`RadarRegimen`/`Selector`.

- [ ] **Step 5: `graceful_shutdown` — quitar cancelación BIN**

Borrar el bloque que cancela órdenes de Binance (main.py:6215-6216, `core_bin_ref` + `_cancelar_ordenes_venue(..., "binance", ...)`). Mantener la de OKX. Quitar el global `core_bin_ref` si queda huérfano.

- [ ] **Step 6: Verificar — compila + sin clases BIN-trading**

Run: `./venv/bin/python -c "import ast; ast.parse(open('main.py').read()); print('OK')"`
Expected: `OK`

Run: `grep -niE "WebSocketFeedBin|OmniRiskBin|CentinelaAsincronoBin|EscudoDCABin|EscuadronGridBin|GestorInventarioBin|TradingCoreBin|sincronizar_bin_spot|hilos_spot" main.py | grep -v "#"`
Expected: 0 líneas.

- [ ] **Step 7: Commit (opcional)**

```bash
git add main.py && git commit -m "remove BIN trading; reduce TradingCoreBin to FuenteBinance (data-only)"
```

---

### Task 4: HUD → 100% OKX + repuntar panic + limpiar huérfanos

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Reescribir el equity del HUD a solo-OKX**

En `RadarTelemetria`: reescribir `_resumen_pnl` y el cálculo de equity para usar SOLO fuentes OKX:
`equity_okx = realized_okx + free_usdt_okx + earn_usdt_okx + earn_cripto_okx + valor_posiciones_okx`.
- Quitar términos: `spot_bin`, `free_bin`, `earn_bin`, `realized_bin`, `bin_realized_pnl`.
- Usar globals/tablas OKX ya existentes: `earn_balance_usdt_okx`, `earn_balance_cripto_okx`, `okx_realized_pnl`, free USDT de `core_okx`.

- [ ] **Step 2: Borrar paneles BIN del HUD**

Borrar `_panel_capital_bin` (main.py:1865) y `_earn_cripto_bin_valor` (1694), y sus usos en el layout. El HUD muestra solo el panel OKX.

- [ ] **Step 3: Quitar `core_bin` del constructor del HUD**

`RadarTelemetria.__init__(self, core_bin, core_okx, db)` → `__init__(self, core_okx, db)`. Actualizar la instanciación en `main()`: `hud = RadarTelemetria(core_okx, db)`. Verificar que el cuerpo del HUD ya no usa `self.core_bin`.

- [ ] **Step 4: Repuntar el panic global de `MotorIndicadores` a OKX**

En `MotorIndicadores` (el chequeo de pánico global BTC, ~main.py:1342-1349):
- `PRECIOS_RAM.get("binance", {}).get("BTC/USDT")` → `PRECIOS_RAM.get("okx", {}).get("BTC/USDT")`.
- `is_price_fresh("binance", "BTC/USDT")` → `is_price_fresh("okx", "BTC/USDT")`.
- (Las lecturas de `fetch_ohlcv` del panic vía `self.core_bin.exchange_read` quedan en Binance.)

- [ ] **Step 5: Borrar funciones/globals huérfanos de earn BIN**

Borrar `refrescar_earn_usdt_bin` (main.py:555) y los globals `earn_balance_usdt_bin` / `earn_balance_cripto_bin` y `_earn_usdt_cache_ts["binance"]` si ya nadie los usa (grep para confirmar 0 usos tras Steps 1-2).

- [ ] **Step 6: Verificar — compila + sin refs BIN en HUD**

Run: `./venv/bin/python -c "import ast; ast.parse(open('main.py').read()); print('OK')"`
Expected: `OK`

Run: `grep -niE "core_bin|_panel_capital_bin|earn_bin|refrescar_earn_usdt_bin|earn_balance_usdt_bin|spot_bin|bin_realized|_earn_cripto_bin" main.py | grep -v "#"`
Expected: 0 líneas (salvo el parámetro `core_bin` que aún reciben Motor/Radar/Selector como fuente de datos — si lo dejaste con ese nombre, ese match es esperado; confirmá que NO aparece en el HUD).

- [ ] **Step 7: Commit (opcional)**

```bash
git add main.py && git commit -m "HUD 100% OKX; panic repunta a precios OKX; limpia earn BIN huerfano"
```

---

### Task 5: Cleanup final de config, validar_config, DB y catálogos

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Quitar `"binance"` de los loops de DB con prefijo**

En `DatabaseManager` (main.py:1059, `for prefix, venue in (("bin", "binance"), ("okx", "okx")):`) → dejar solo `(("okx", "okx"),)`. Esto deja de crear tablas `bin_*`. (Las existentes en `quantum.db` quedan; inocuas.)

- [ ] **Step 2: Revisar catálogos/seed BIN**

- Si `CATALOGO_MEMES_BIN` (main.py:341) ya no lo usa nada de OKX, borrarlo y simplificar `clasificar_sector` (351/360) a solo `CATALOGO_MEMES_OKX`. Grep `CATALOGO_MEMES_BIN` para confirmar usos.
- `portfolio_seed.json`: la rama `"binance"` queda inocua (la lee `seed_avg_cost` solo para venue okx). Dejar o limpiar — opcional.

- [ ] **Step 3: Quitar refs `"binance"` de trading en `validar_config`**

Revisar `validar_config` (main.py:824) y quitar validaciones que apliquen a BIN trading o ARB ya removidos (ej. `arb_intra_venue`). Conservar la validación de keys OKX y de keys Binance si el `exchange_read` las usa.

- [ ] **Step 4: Verificar — compila**

Run: `./venv/bin/python -c "import ast; ast.parse(open('main.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 5: Verificar import completo (ejecuta el cuerpo del módulo)**

Run: `./venv/bin/python -c "import main; print('import OK')"`
Expected: `import OK` (esto corre el dict CONFIG y las defs; atrapa `NameError` de nivel módulo y claves CONFIG faltantes referenciadas en defaults).

- [ ] **Step 6: Commit (opcional)**

```bash
git add main.py && git commit -m "cleanup: DB prefix solo okx, catalogos/validar BIN"
```

---

### Task 6: Boot smoke test (runbook manual — lo corre el usuario)

**Files:** ninguno. Ejecución/observación.

- [ ] **Step 1: Conteo de clases final**

Run: `grep -c "^class " main.py`
Expected: baja en ~11 respecto al baseline del Task 0.

- [ ] **Step 2: Grep global de símbolos removidos (todos juntos)**

Run:
```bash
grep -niE "FundingScanner|ArbExecutor|WalletBridge|SensorPosicionesFut|CircuitBreakerFut|WebSocketFeedBin|OmniRiskBin|CentinelaAsincronoBin|EscudoDCABin|EscuadronGridBin|GestorInventarioBin|_fut_|get_fut_balance|futuros_habilitado|arb_live|refrescar_earn_usdt_bin|_tabla_fut|_panel_capital_bin" main.py | grep -v "#"
```
Expected: 0 líneas.

- [ ] **Step 3: `entrenar.py` no rompe**

Run: `./venv/bin/python -c "import ast; ast.parse(open('entrenar.py').read()); print('OK')"`
Expected: `OK`. Si `entrenar.py` importa de `main.py`, verificar que no use símbolos removidos.

- [ ] **Step 4: Boot smoke test (bot real, segundos)**

Con el bot apagado y un `KILL` a mano:
Run: `python main.py`
Observar:
- Arranca sin `NameError`/`AttributeError`.
- HUD renderiza **solo OKX** (sin paneles BIN/FUT/ARB).
- `RadarRegimen`/`Selector` loguean datos de régimen (siguen leyendo OHLCV+funding de Binance vía `FuenteBinance`).
- Mensaje de arranque cuenta **~12 hilos** (5 compartidos + 7 OKX).
Apagar: `touch KILL` (o Ctrl+C).

- [ ] **Step 5: Si todo OK, borrar el backup**

Run: `rm main.py.bak`

- [ ] **Step 6: Commit (opcional)**

```bash
git add -A && git commit -m "verify OKX-only boot; remove backup"
```
