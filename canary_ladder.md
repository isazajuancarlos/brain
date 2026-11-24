# Runbook — Canary del Ladder Sleeve (SP3)

Verificación del sleeve `EscuadronLadderOkx` antes de dejarlo solo. El canary mide **fill-real vs
fill-ideal** con capital mínimo ($100) sobre LTC/USDT. NO escalar hasta validar fills.

## 0. Pre-deploy (en el VPS, acción del usuario)
- `git pull` / subir `main.py` + `.env` al VPS.
- En `.env`: `LADDER_ENABLED=true`, `LADDER_CANARY_MODE=true`, `LADDER_PARES=LTC/USDT`,
  `LADDER_MAX_CAPITAL=100`. Confirmar que hay ~$100 USDT libres en OKX spot.
- Reiniciar el bot.

## 1. Arranque (primeros minutos)
- [ ] Boot banner muestra: `▤ Ladder sleeve ACTIVO — LTC/USDT spot, cap $100 [CANARY]`.
- [ ] `validar_config` no abortó (cap POR PAR x nº de pares ≤ 500 **totales**, pares válidos).
- [ ] Sin errores `Ladder ...` en `logs/errors.log`.

## 2. Primer ciclo en RANGO (cuando LTC clasifique RANGO propio)
- [ ] El sleeve coloca hasta 4 órdenes límite **maker BUY** en OKX (escalera bajo el precio).
      Verificar en la app OKX: 4 órdenes abiertas LTC/USDT, suma ≤ $100.
- [ ] Tabla `ladder_orders`: 4 filas side=buy state=open. `ladder_fill_log`: 4 filas colocadas.
- [ ] Si LTC NO está en RANGO propio (TENDENCIA_DOWN/CAOS): el sleeve NO coloca nada (correcto —
      congela). Esperar a que clasifique RANGO.

## 3. Primer fill (cuando el precio toque un rung)
- [ ] `ladder_fill_log`: la fila del rung tocado tiene `ts_tocado` set; si llenó, `lleno=1`+`ts_lleno`.
- [ ] Al llenar un buy: aparece la posición en `ladder_positions` y un **sell limit** a +spacing en OKX.
- [ ] Al llenar el sell: `ladder_realized` registra el round-trip (pnl_neto), la posición se libera.

## 4. El número que decide (tras ≥4 semanas o ≥30 round-trips)
- [ ] `ladder_fill_resumen()` → `fill_rate` = llenos/tocados. **Graduar solo si ≥~70%** (los maker
      resting llenan como el sim asumió) Y el PnL realizado no contradice el sim.
- [ ] Si `fill_rate` bajo → los fills idealizados inflaron el backtest → **descartar**
      (`LADDER_ENABLED=false`), documentar.

## 5. Protección (verificar que disparan)
- [ ] Flip de régimen (LTC → TENDENCIA_DOWN): el sleeve hace **flatten** (market-sell holdings) y deja
      de armar. `ladder_realized` con motivo `flatten`.
- [ ] Rung underwater > `LADDER_STOP_PCT` (15%): **stop** (market-sell), motivo `stop`.
- [ ] `touch KILL`: cierra todo a mercado (vía kill_switch, como los demás motores).
- [ ] Pérdida diaria ≤ -$15: congela compras nuevas hasta el otro día.

## 6. Graduación (config, no código)
- `LADDER_CANARY_MODE=false` + subir `LADDER_MAX_CAPITAL` gradual + evaluar 2º par (validado por
  `entrenar.py --ladder-scan`). NO antes del verde de fill_rate.

## Notas
- El sleeve NO toca BTC/ETH (grid core) — tablas/capital separados.
- En Ctrl+C los maker limits quedan resting (el reconcile los recupera al rearrancar).
- Spec: `ladder_grid_sp3_spec.md`. Mediciones reusables: `entrenar.py --ladder-sweep/--ladder-scan`.
