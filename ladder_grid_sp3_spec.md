# Ladder Grid SP3 — Spec de diseño (sleeve live `EscuadronLadderOkx`, canary)

**Fecha:** 2026-06-22
**Estado:** diseño aprobado (brainstorm), pendiente de plan de implementación.
**Alcance:** SP3 = **primer cambio a RUNTIME + CAPITAL** del hilo ladder. Sleeve live aislado, modo canary. Toca `main.py`, órdenes OKX reales, capital chico, deploy al VPS.

---

## 1. Motivación

SP1 (`--ladder-sweep`) probó que el grid ladder round-trip cosecha RANGO ~10× el grid actual, fee-proof. SP2 (`--ladder-scan`, `ladder_grid_sp2_spec.md` §9) **pasó el gate**: gate por régimen PROPIO del activo + stop a rungs colgados → 5/8 pares net+ robusto POR COSECHA (LTC +3.66% **mientras LTC caía −35%** = cosecha inequívoca). **Único riesgo sin medir:** los **fills maker idealizados** (el sim asume llenado al tocar el rung; en real puede no llenar/parcial). Ningún backtest lo resuelve — solo capital real en chico.

**SP3 = un sleeve live canary cuyo deliverable es medir fill-real vs fill-ideal** con ~$100 sobre LTC/USDT, antes de escalar un peso. Si los fills reales ≈ el sim → graduar. Si decepcionan → descartar con plata (no con fe).

## 2. Alcance / no-objetivos

- **Sí:** hilo `EscuadronLadderOkx` (spot, additivo, aislado), gate régimen-propio LTC, escalera maker round-trip, stop a colgados, canary mode, tablas `ladder_*`, reconcile, HUD, envelope en `validar_config`.
- **NO:** no toca `EscuadronGridOkx` (grid core BTC/ETH) — thread/capital/tablas separados. No leverage (spot puro). No multi-par en canary (solo LTC). No optimización — el canary MIDE fills, no maximiza.
- **Default OFF:** `LADDER_ENABLED=False`. Encender es decisión explícita + deploy.

## 3. Parámetros (decididos en brainstorm)

- **Par canary:** `LTC/USDT` (cosecha más limpia en SP2, líquido).
- **Capital envelope:** ~**$100** máximo. 3-5 rungs de lote mínimo OKX (~$10-20/rung).
- **Geometría:** spacing 1-2% (del barrido SP2), `stop_pct` ~15%, flatten al salir de RANGO.

## 4. Arquitectura / componentes (aislados, testeables)

### 4.1 `EscuadronLadderOkx(threading.Thread)`
Loop cada `LADDER_TICK_S` (60s). Por LTC:
1. **Régimen propio:** evalúa el régimen de LTC (§4.2). Si ≠RANGO → no arma compras; si flip RANGO→no-RANGO → **flatten** (cancela sells, market-sell inventario).
2. **Arma escalera:** si RANGO y sin ladder activo → `ref`=precio actual, coloca N **maker buy limits** en `ref·(1−k·S)`.
3. **Reconcile fills (OKX=verdad):** buy llenado → coloca **maker sell** a `fill·(1+S)`; sell llenado → round-trip realizado, re-arma el rung. (boot + cada tick).
4. **Stop a colgados:** rung holding underwater >`stop_pct` → market-sell, queda `stopped` hasta re-centrado (espejo del sim).
5. **Canary gate:** capital comprometido ≤ envelope; lotes mínimos.

### 4.2 Evaluador de régimen PROPIO live
`regimen_propio(sym) -> estado`: corre `clasificar_regimen` sobre las velas **diarias de LTC** (desv/slope/adx/atr, idéntico a `_regimen_estado_por_ts`). Fuente de datos: el feed existente (Binance read / OKX OHLCV 1d para LTC). Cachéa con TTL. Es el fix de SP2 traído a runtime.

### 4.3 Mecánica de orden
Reusa `core_okx.execute_limit_maker(sym, price, monto_usdt)` (buy) y `execute_limit_sell_harvest(sym, price, qty)` (sell) — el sleeve es **spot**, no necesita core swap. Cada rung-orden se persiste con su `order_id`.

### 4.4 Canary fill-realism (el deliverable)
Tabla `ladder_fill_log`: por cada buy maker colocado registra `{rung, precio, ts_colocado, ts_tocado (precio ≤ rung en PRECIOS_RAM), lleno (bool), ts_lleno}`. Métrica: **fill_rate = llenos / tocados** y latencia tocado→lleno. Comparado contra el supuesto del sim (llena al tocar). `ladder_fill_resumen()` agrega. **Este número decide graduar.**

### 4.5 DB (tablas `ladder_*`, prefijo por módulo)
- `ladder_orders` (order_id, symbol, side, rung_idx, price, qty, state, ts).
- `ladder_positions` (symbol, rung_idx, qty, avg_cost, sell_order_id, bought_ts).
- `ladder_realized` (symbol, qty, buy_px, sell_px, pnl_neto, motivo, ts).
- `ladder_fill_log` (§4.4).
- Migración defensiva (CREATE IF NOT EXISTS), WAL única `quantum.db`.

### 4.6 Reconcile (boot + tick)
`reconciliar_ladder()`: consulta OKX (órdenes abiertas + fills) = fuente de verdad; sincroniza estado de rungs, registra round-trips cerrados con el bot apagado o corriendo (espejo de `reconciliar_shorts`). Grace de unos segundos para no falso-cerrar órdenes recién puestas.

### 4.7 Protección
- **Stop a colgados** (§4.1.4).
- **Flatten al flip de régimen** (§4.1.1).
- **Panic global / KILL:** integra `is_panico_global()` (no arma en panic) y `kill_switch` (cierra todo a mercado, como los demás motores).
- **Cap de pérdida diaria** (`LADDER_DAILY_LOSS_CAP`, ~$15): supera → congela el sleeve hasta el otro día.

### 4.8 `validar_config` envelope
- `LADDER_ENABLED` default **False**.
- Si True: par en whitelist (`LADDER_PARES` ⊂ genuinamente laterales), capital cap ≤ `LADDER_MAX_CAPITAL` **por par** (abort si `cap × len(LADDER_PARES)` > $500 totales de arranque), `LADDER_CANARY_MODE` warning si False (capital real escalado), **sin leverage** (spot).
- Aborta config inválida (espejo de los checks short).

### 4.9 HUD
Línea del sleeve: estado régimen LTC, rungs activos, capital comprometido, round-trips, fill_rate canary, PnL día. `[CANARY]` si aplica.

## 5. Config keys (`.env` / CONFIG)

`LADDER_ENABLED=false` · `LADDER_CANARY_MODE=true` · `LADDER_PARES=LTC/USDT` · `LADDER_MAX_CAPITAL=100` · `LADDER_RUNGS=4` · `LADDER_SPACING_PCT=1.5` · `LADDER_STOP_PCT=15` · `LADDER_DAILY_LOSS_CAP=15` · `LADDER_TICK_S=60`.

## 6. Canary → graduación (criterio explícito)

Tras ≥4 semanas o ≥30 round-trips reales, graduar (`LADDER_CANARY_MODE=false`, subir capital, evaluar 2º par) **solo si**:
- **fill_rate real ≥ ~70%** de los fills que el sim predijo (los maker resting llenan como se modeló), Y
- PnL realizado neto **no contradice** el sim (≥0 o dentro del rango esperado), Y
- el stop y el flatten funcionaron sin sorpresas (sin inventario varado fuera de envelope).
Si fill_rate bajo o PnL net-negativo por fills → **descartar** (`LADDER_ENABLED=false`), documentar (el edge del sim no sobrevivió a la microestructura real).

## 7. Error handling / failsafes

- Toda llamada OKX bajo `okx_retry` (anti-bot V1.8.2) + `try/except` con log (piso fail-safe).
- Orden rechazada / fondos insuficientes → no rearma, loguea, sigue.
- Desync DB↔OKX → reconcile reconcilia desde OKX (verdad).
- Ctrl+C → preserva órdenes resting (maker limits siguen); KILL → market-close todo.
- Sleeve cae → los maker limits y el inventario quedan; el reconcile los recupera al rearrancar.

## 8. Testing (TDD, patrón `test_short_okx.py` contra `FakeOkx`)

`test_ladder_okx.py` (sin plata real, contra un `FakeOkxSpot`):
- arma N rungs al entrar RANGO; no arma si ≠RANGO.
- buy llenado → coloca sell a +spacing; sell llenado → round-trip + re-arma.
- stop a colgado underwater → market-sell + queda stopped (no re-arma mismo tick).
- flatten al flip de régimen cierra inventario.
- canary cap: no compromete > envelope.
- reconcile registra round-trips cerrados fuera de proceso.
- `validar_config` aborta config fuera de envelope.
- `ladder_fill_log` registra tocado/lleno.

## 9. Deploy

Cambio de runtime → requiere subir `main.py` + `.env` al VPS y reiniciar (acción del usuario; el entorno local es download-only). Arranca con `LADDER_ENABLED=true` + `LADDER_CANARY_MODE=true`. Runbook de verificación canary (espejo de `canary_short.md`): confirmar que los maker limits se colocan, llenan y los sells salen, antes de dejarlo solo.

## 10. Riesgos

- **Fills reales < sim** (el riesgo central, lo que el canary mide). Mitigación: es el propósito; descarte si decepciona.
- **Régimen propio de LTC** vía clasificar_regimen calibrado en BTC: umbrales pueden no transferir. Mitigación: el canary observa; sensibilizar si el gate se porta raro.
- **Microestructura LTC** (spread, liquidez por rung): lote mínimo y spacing ≥1% lo acotan.
- **Solapamiento con otros motores:** ninguno opera LTC hoy (grid core = BTC/ETH; OmniRisk = liquidación de holdings viejos); tablas/capital separados evitan pisarse.
- **Sleeve nuevo = superficie de bug en capital real.** Mitigación: canary $100, cap de pérdida diaria, KILL, tests contra FakeOkx, runbook.

## 11. Decomposición

SP3 es **terminal** del hilo medición→diseño. Tras el canary: graduar (escalar + 2º par, cambio de config, no de código) o descartar. No hay SP4 planificado; un eventual multi-par/escalado es config + un par nuevo validado por `--ladder-scan`, no rediseño.
