# Gestor de Salida SHADOW — Fase 1 (multiverso de medición)

> Estado: spec aprobada (delegada por el usuario, 2026-06-16). Fase 1 de la idea
> "surfista completo" (gestión dinámica de la salida del short bajista). Las fases
> 2 (controlador que gobierna capital real) y 3 (meta-learner online) quedan
> DIFERIDAS hasta que esta fase produzca datos. Ver `short_bajista_spec.md`.

## Problema

La Pata Bajista (`EscuadronShortOkx`) hoy es **fire-and-forget**: abre con un OCO
server-side (TP −3% / SL +4%) y solo cierra por flip de régimen, TP-backstop o SL.
No hay gestión dinámica de la posición abierta: ni *trailing* que cabalgue una caída
fuerte más allá del TP fijo, ni salida anticipada cuando el rebote se reanuda hacia
el SL. Filosóficamente (pedido del usuario): **el SL debe ser la excepción, no la
regla** — cuanto más se evita y más ganancia se asegura, mejor.

Pero el edge del short está **sin validar** (4 trades, breakeven ~59%). Construir un
gestor dinámico que gobierne capital real AHORA sería apostar sobre datos que no
tenemos. **YAGNI:** primero medir, después gobernar.

## Qué construye la Fase 1

Un **multiverso de MEDICIÓN**: corre varias políticas de salida candidatas en
**paper** (cero capital, cero órdenes) sobre entradas virtuales gatilladas por la
misma `senal_short`. Registra el **PnL contrafactual** de cada política → genera el
dataset que decide, más adelante, cuál política merece gobernar capital real.

- **Riesgo cero:** no abre órdenes, no toca OKX, no toca el capital. El OCO-fijo
  sigue gobernando las posiciones reales sin cambios.
- **Decoupla medición de despliegue:** corre aunque `OKX_FUT_ENABLED=false`. Mide
  las salidas incluso con el short apagado.
- **Maximiza muestras:** simula una entrada virtual cada vez que la señal dispara en
  régimen BAJISTA (no se topea por max-posiciones ni margen — esos son límites de
  capital, irrelevantes para medir la CALIDAD de la salida).

## Políticas candidatas (cada una pura, una sola responsabilidad)

Firma común: `politica(vp, ctx) -> (exit_px, motivo) | None`.
- `vp`  = posición virtual: `entry_px`, `mejor_px` (mínimo alcanzado = más favorable
  al short), `entry_ts`, `vivas` (set de políticas aún abiertas).
- `ctx` = `px` (bid actual), `ind` (dict de indicadores), `ml_prob` (o None).

Todas las políticas dinámicas comparten un **SL duro de catástrofe** (`_pol_sl_duro`):
nunca quedan desnudas, igual que el OCO server-side real es el piso bajo todo.

| Política       | Sale cuando…                                              | Captura |
|----------------|-----------------------------------------------------------|---------|
| `oco_fijo`     | px ≤ entry·(1−TP) (TP) · ó · px ≥ entry·(1+SL) (SL)       | baseline actual |
| `chandelier`   | px ≥ mejor_px + k·ATR (trailing) · ó · SL duro            | "cabalga el cuchillo" |
| `momentum`     | MACDh>0 ó +DI>−DI (el rebote se reanuda) · ó · SL duro    | salida anticipada |
| `ml_adverso`   | ml_prob ≥ umbral (ML long invertido = peligro) · ó · SL   | precursor "animales" |

`chandelier`/`momentum`/`ml_adverso` **no** tienen TP fijo: cabalgan hasta su gatillo
o el SL duro — ahí está la apuesta de "dejar correr".

## Ciclo (`_procesar`, testeable por inyección)

`_procesar(sym, px, ind, ml_prob, regime_ok, now)`:
1. Si no hay virtual abierta para `sym`: si `regime_ok and senal_short(ind)` → abrir
   virtual (todas las políticas vivas). Una virtual por símbolo a la vez.
2. Actualizar `mejor_px = min(mejor_px, px)`.
3. Evaluar cada política viva; la que dispara registra su PnL contrafactual y muere.
4. `max-hold` (default 6h = 24 velas 15m, horizonte del label ML): las políticas que
   sigan vivas cierran a mercado (`motivo="max-hold"`).
5. Cuando todas murieron, retirar la virtual (el símbolo puede re-entrar).

El gate de entrada es solo `regime BAJISTA + senal_short` (no panic/vol/cooldown):
queremos máximas muestras limpias para rankear SALIDAS. El flip-de-régimen NO se
modela como salida porque aplicaría por igual a todas las políticas → no cambia el
ranking.

## Estado / esquema

Tabla `short_shadow_pnl(id, politica, symbol, entry_px, exit_px, pnl_pct, motivo,
entry_ts, exit_ts)`. `pnl_pct = (entry−exit)/entry·100` (agnóstico al capital, porque
es virtual → comparable entre políticas). Métodos DB:
`short_shadow_registrar(...)` y `short_shadow_resumen()` (agregado por política:
n, pnl_total, pnl_avg, wins).

## Hilo / wiring

`GestorSalidaShadow(threading.Thread, daemon)` — hilo propio, independiente de la
Pata Bajista. Gateado por `SHORT_SHADOW_ENABLED` (default **true**: es riesgo cero).
`_tick` recolecta `ind`/`px`/`ml_prob`/`regime` para `PARES_SHORT_OKX` y llama
`_procesar`. Se construye y arranca en `main()` aunque `fut_enabled=false`.

## Config (env)

- `SHORT_SHADOW_ENABLED` (bool, default true)
- `SHORT_SHADOW_CHANDELIER_K` (float, default 2.5) — múltiplo de ATR del trailing
- `SHORT_SHADOW_ML_ADVERSO` (float, default 0.60) — umbral ml_prob de salida adversa
- `SHORT_SHADOW_MAX_HOLD_S` (float, default 21600 = 6h)

## Fase 2 (CONSTRUIDA — V1.7.2): graduación a capital real

`seleccionar_politica_live(resumen, mode, min_trades, margen_pct)` (pura) gradúa la
política ganadora del shadow para gobernar las salidas REALES vía `SHORT_GESTOR_MODE`
(`shadow` default / `live` auto / `<política>` pin). Aplicada en
`EscuadronShortOkx._gestionar_salidas` (salidas más tempranas; el OCO server-side SL
sigue de piso). **Alcance Opción 1:** solo salidas tempranas — el OCO TP −3% topea, así
que "let-it-run pleno" (chandelier) NO se realiza acá. Tests: +11 en `test_short_okx.py`.

## Fase 3 (CONSTRUIDA — V1.7.3): meta-learner online

`SHORT_GESTOR_MODE=meta`: la selección pasa de estática (Fase 2) a **online por contexto +
recencia**. `contexto_vol(ind)` agrupa por volatilidad (calmo/normal/picado);
`seleccionar_politica_meta(rows, min_trades, margen_pct, halflife_s, now)` pondera el
feedback completo del shadow por recencia (half-life) dentro del contexto actual y elige la
mejor. El shadow guarda `contexto` por trade (`short_shadow_pnl.contexto`,
`short_shadow_rows_por_contexto`). Aplicada per-símbolo en `_gestionar_salidas`. Tests:
`test_short_meta.py` (11) + 2 integración.

## Fuera de alcance (diferido)

- **Fase 2b:** bracket **SL-only** (sin TP server-side) para realizar el "dejar correr
  más allá del TP" — toca el path de entrada validado; justificada SOLO si los datos del
  shadow coronan a chandelier.
- Label short-específico en `entrenar.py` (hoy bootstrap con el modelo long invertido).
- Precursores "animales" profundos (funding/OBI/CVD) como features de contexto.
