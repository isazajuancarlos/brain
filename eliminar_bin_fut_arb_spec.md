# Spec — Reducir `main.py` a OKX spot+earn (quitar Binance-trading, Futuros, ARB)

- **Fecha:** 2026-06-05
- **Estado:** Aprobado (diseño), pendiente de plan de implementación
- **Sub-proyecto:** 2 de 3. (1 = `liquidar.py` ✅ hecho. 3 = verificación local + docs.)

## 1. Objetivo

Simplificar el bot a **trading solo en OKX (spot + earn)**, eliminando todo el trading de Binance, toda la rama de Futuros y todo el ARB. **Binance se conserva únicamente como fuente de datos** (read client, con las credenciales existentes) para el cerebro de régimen/ML — no se opera en Binance.

## 2. Principio rector

- **Trading:** SOLO OKX.
- **Datos de mercado (OHLCV BTC/ETH + funding BTC):** siguen viniendo de Binance vía `exchange_read` (REST público/autenticado). `RadarRegimen`, `Selector` y `MotorIndicadores` **no se tocan** en su lógica de datos.
- **HUD:** 100% OKX (no muestra nada de Binance, aunque el cerebro lo use por detrás).

## 3. KEEP — qué se conserva

- **Núcleo OKX intacto:** `TradingCoreOkx`, `WebSocketFeedOkx`, `OmniRiskOkx`, `CentinelaAsincronoOkx`, `EscudoDCAOkx`, `EscuadronGridOkx`, `GestorTesoreria`, `GestorInventario`.
- **Cerebro compartido:** `MotorIndicadores`, `RadarRegimen`, `Selector`, `KillSwitchMonitor`, `RadarTelemetria` (HUD, modificado), `DatabaseManager`, `LoggerCircular`.
- **Cliente de datos Binance:** `TradingCoreBin` se **reduce a un proveedor de datos** (solo `exchange_read`). Se renombra a **`FuenteBinance`** para que el nombre no mienta (toca su `class`, los 3 constructores que lo reciben — `MotorIndicadores`/`RadarRegimen`/`Selector` — y `main()`).
- **ML:** `xgboost_oracle.joblib` + helpers (`ml_features_para`, `ml_size_multiplier`) consumidos por `EscuadronGridOkx`. `entrenar.py` queda como está (standalone; verificar que no importe símbolos removidos).

## 4. REMOVE — Binance trading (no datos)

- Hilos/clases: `WebSocketFeedBin`, `OmniRiskBin`, `CentinelaAsincronoBin`, `EscudoDCABin`, `EscuadronGridBin`, `GestorInventarioBin`.
- De `FuenteBinance` (ex-`TradingCoreBin`): `exchange_trade`, `api_trade_lock`, `inventario`, métodos de trading (`execute_trade`, `allocate_budget`, `release_budget`, `reconciliar_portfolio`, `sincronizar_bin_spot_arranque`, `_post_sell` y afines), adopción de `bin_portfolio`.
- Catálogos/seed BIN de trading: `CATALOGO_MEMES_BIN` si no lo usa OKX; rama `binance` de `portfolio_seed.json` (opcional, inocua si queda).

## 5. REMOVE — Futuros (todo)

- De `FuenteBinance`: `exchange_fut_*`, `_init_futuros`, `_fut_host/_fut_sign/_fut_signed_request/_fut_public_request/_fut_step_round/_fut_to_binance_sym/_cargar_fut_symbol_info`, `get_fut_balance`/`invalidate_fut_balance`, `sincronizar_fut_arranque`, `allocate_notional_fut`/`release_notional_fut`, `posiciones_fut_RAM`, `fut_symbol_info`, `reserved_notional_fut_*`, `fut_balance_*`, `circuit_breaker_activo`.
- Hilos: `SensorPosicionesFut`, `CircuitBreakerFut`.
- HUD: `_tabla_fut`.
- Config: `fut_sandbox`, `bin_fut_api_key`, `bin_fut_api_secret`, `fut_simbolos`, `fut_max_notional_glob`, `futuros_habilitado`, y el bloque FUT de `validar_config` (main.py:834-851).
- Archivo: `test_fut_keys.py` (borrar).

## 6. REMOVE — ARB (todo)

- Hilos/clases: `FundingScanner`, `ArbExecutor`, `WalletBridge`.
- Config: todas las `arb_*` (`arb_venues`, `arb_intra_venue`, `arb_fee_perp`, `arb_live`, `arb_catalyst_path`, etc.) y su validación.
- DB: dejar de crear/leer `arb_*` (incl. `arb_realized_pnl`, `arb_signals`).
- Archivo: `catalyst_signal.json` (borrar; el fallback hardcoded en `main.py` también se va con `FundingScanner`).

## 7. MODIFY — puntos de acoplamiento

1. **HUD (`RadarTelemetria`)** → 100% OKX:
   - Equity: `realized_okx + free_usdt_okx + earn_usdt_okx + earn_cripto_okx + valor_posiciones_okx`. Sacar `spot_bin`, `arb`, `earn_bin`, `fut_m`.
   - Borrar `_panel_capital_bin`, `_earn_cripto_bin_valor`, `_tabla_fut`. Layout solo-OKX.
   - `_resumen_pnl`: quitar `bin_realized_pnl` y `arb_realized_pnl`.
   - Constructor: ya no recibe `core_bin` (o lo recibe como `FuenteBinance` y no lo usa para mostrar nada). Decisión: **el HUD deja de recibir `core_bin`**.
2. **`MotorIndicadores` (panic global BTC):** repuntar la lectura de precio `PRECIOS_RAM["binance"]["BTC/USDT"]` → `PRECIOS_RAM["okx"]["BTC/USDT"]` (porque se borra `WebSocketFeedBin`). Las lecturas de **OHLCV** del panic (`fetch_ohlcv` vía `exchange_read`) **quedan en Binance**.
3. **`main()`:** sacar `sincronizar_bin_spot_arranque`, `sincronizar_fut_arranque`, `hilos_spot` (BIN), `hilos_fut`, `hilos_arb`, y el wireo `core_bin.inventario`. Construir `FuenteBinance` y pasarla a `MotorIndicadores`/`RadarRegimen`/`Selector`. Recalcular el contador de hilos.
4. **`graceful_shutdown`:** quitar la cancelación de órdenes de Binance (`core_bin_ref`, `_cancelar_ordenes_venue(..., "binance", ...)`). Mantener la de OKX.
5. **`DatabaseManager`:** quitar `"binance"` de los loops de creación de tablas con prefijo (main.py:1059) y dejar de crear tablas `arb_*`. Los métodos venue-genéricos pueden quedar, pero nada los llamará con `"binance"`.
6. **Globals:** `PRECIOS_RAM`/`ohlcv_cache` pueden conservar la clave `"binance"` (la usa el cerebro vía REST cache); revisar caso por caso. `_earn_usdt_cache_ts["binance"]` se va con `refrescar_earn_usdt_bin`.

## 8. Enfoque incremental (clave para no romper)

`main.py` debe quedar **compilable y arrancable después de cada paso**. Orden sugerido (lo detalla el plan):
1. ARB (el más aislado: 3 clases + config + tablas).
2. Futuros (helpers en core + 2 hilos + config + `test_fut_keys.py`).
3. Binance trading → reducir `TradingCoreBin` a `FuenteBinance` (data-only) + borrar 6 hilos BIN.
4. HUD → OKX-only.
5. `main()` / `graceful_shutdown` / `validar_config` / config / DB cleanup.
6. Verificación de arranque.

## 9. Riesgos

- **El HUD es el mayor riesgo de romper** (cálculo de equity y layout). Mitigación: reescribir su sección de equity de cero con solo las fuentes OKX ya existentes (`earn_balance_*_okx`, `okx_realized_pnl`, posiciones OKX).
- **Símbolos colgantes:** una referencia olvidada a una clase/función borrada tira `NameError` en runtime (no en import si está dentro de un método). Mitigación: grep exhaustivo de cada símbolo removido + boot smoke test.
- **El cerebro de régimen depende del `exchange_read` de Binance:** verificar que `FuenteBinance` lo construye igual y que `RadarRegimen`/`Selector` siguen recibiendo datos.

## 10. Verificación

1. **Compila:** `python -c "import ast; ast.parse(open('main.py').read())"`.
2. **Sin símbolos colgantes:** para cada clase/función/config removida, `grep` en `main.py` devuelve 0 (salvo su propia definición ya borrada). Lista explícita en el plan.
3. **`entrenar.py` no rompe:** `python -c "import ast; ast.parse(open('entrenar.py').read())"` y revisar que no importe de `main.py` símbolos removidos.
4. **Boot smoke test:** arrancar `python main.py` con un `KILL` listo / por unos segundos, confirmar que:
   - Arranca sin `NameError`/`AttributeError`.
   - El HUD renderiza solo OKX.
   - El cerebro de régimen loguea datos (RadarRegimen/Selector obtienen OHLCV+funding de Binance).
   - Se cuenta el nuevo total de hilos (sin BIN-trading/FUT/ARB).
   - Apagar con `KILL` o Ctrl+C.
5. **Conteo de hilos esperado:** 5 compartidos + 7 OKX (WS, OmniRisk, Centinela, EscudoDCA, Grid, Tesoreria, Inventario) = **12 hilos** (vs ~27 antes).

## 11. Fuera de alcance

- **Sub-proyecto 3:** actualizar `CLAUDE.md` + `bot.txt` al nuevo alcance (OKX-only), verificar deps locales. Se hace después de que el refactor compile y arranque.
- No se reescribe la lógica de OKX ni del cerebro de régimen — solo se desconecta/borra lo de Binance-trading/FUT/ARB.
- No se borra `quantum.db` ni sus tablas existentes (solo se deja de crear/usar las `bin_*`/`arb_*`).
