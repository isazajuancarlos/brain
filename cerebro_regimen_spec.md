# Cerebro de Régimen 4-estados — Spec (V1.8.0)

**Fecha:** 2026-06-19
**Estado:** aprobado, en implementación
**Relacionado:** `short_bajista_spec.md`, `gestor_salida_shadow_spec.md` (mismo patrón shadow→live)

---

## 1. Problema

El bot hoy es **medio ciego al mar**. La dirección de estrategia la toma de un único bit:
`modo_mercado ∈ {ALCISTA, BAJISTA}` (`RadarRegimen`: BTC vs EMA200d ±2% histéresis). El
`EscuadronGridOkx` solo mira ese bit: opera si no es BAJISTA, congela si BAJISTA.

**No existe estado "RANGO/lateral" ni "CAOS"** — y el rango es justamente donde un grid
mean-reversion cosecha mejor (oscilación = viajes redondos), mientras que el caos (vol alta)
lo destroza. El bot no distingue "chop lateral cosechable" de "bull vertical" de "vol caótica".

Además, el `Selector` (hilo ya corriendo) **ya computa** las señales necesarias
(desv, slope, adx, atr, ratio BTC/ETH, funding) + confianza ponderada + histéresis adaptativa,
pero **colapsa todo a `SPOT/FLAT`** y su salida (`modo_operacion`) **no gatea ningún motor OKX**.
La inteligencia se calcula y se tira.

## 2. Objetivo

Enriquecer la taxonomía de régimen a **4 estados** y **cablearla** para que gobierne el grid
existente (cuándo opera/congela + sizing por confianza). **Sin estrategias nuevas.** El sleeve
trend-following queda como proyecto downstream que consumirá este cerebro.

**No-objetivos (scope explícito):**
- NO construir trend-ride / multi-estrategia.
- NO tocar `RadarRegimen`, short-shadow, earn.
- NO agregar Hurst/OBI todavía (medición futura si el backfill lo justifica).

## 3. Arquitectura

Promover el `Selector` a **Cerebro de Régimen**. `RadarRegimen` queda **intacto** (sigue dando
`modo_mercado` + `factor_capital` a sus consumidores: short-shadow, panic global, HUD).

```
RadarRegimen (INTACTO) ─► modo_mercado + factor_capital ─► short-shadow, panic, HUD
Selector→Cerebro ───────► regimen_mercado (4 estados) + regimen_confianza ─► EscuadronGridOkx (CABLE NUEVO)
```

**Por qué no fusionar las dos computaciones de régimen:** sería más limpio pero toca consumidores
estables (`short-shadow` lee `modo_mercado=="BAJISTA"`). El menor blast-radius gana — mismo
criterio que apagar futuros sin borrarlos. La redundancia (dos `fetch_ohlcv` BTC diarios) es
costo aceptable por aislamiento.

## 4. Taxonomía (definición positiva, señales que el Selector ya calcula)

Prioridad de evaluación (de arriba abajo; el primero que matchea gana):

| Estado | Regla |
|---|---|
| **CAOS** | `atr > SELECTOR_ATR_VOL_HIGH` (vol manda) |
| **TENDENCIA_DOWN** | `desv < SELECTOR_BEAR_EMA_PCT` **y** `slope < 0` |
| **TENDENCIA_UP** | `desv > SELECTOR_BULL_EMA_PCT` **y** `slope > 0` **y** `adx ≥ REGIMEN_ADX_TREND` |
| **RANGO** | residuo positivo: `adx < REGIMEN_ADX_RANGO` **y** `\|desv\| ≤ REGIMEN_DESV_RANGO` (sin tendencia, cerca de la media, vol normal) |
| **(fallback)** | si nada matchea → **RANGO** con confianza baja (residuo) |

**Señal de RANGO — decisión v1:** usar ADX-bajo (ausencia de tendencia ≈ mean-reversion). **NO**
agregar Hurst/autocorrelación todavía. Se mide en el backfill: si el residuo etiqueta mal
(p.ej. llama "RANGO" a un goteo lento), recién ahí se introduce un estadístico MR (autocorr lag-1<0
/ Hurst<0.5). Measure-first.

## 5. Playbook (estado → comportamiento del grid)

```
RANGO          → grid sizing PLENO   (factor 1.0; su hábitat)
TENDENCIA_UP   → grid sizing NORMAL  (factor 0.7; acompaña, el hold gana)
TENDENCIA_DOWN → CONGELA entradas    (solo cosecha + earn)  ← equivale al BAJISTA actual
CAOS           → CONGELA todo
```

Lógica contraintuitiva validada: **RANGO ≥ TENDENCIA_UP** porque el grid cosecha de la oscilación,
no de la tendencia (en bull vertical el precio se escapa y cosecha poco; ahí ganaría el sleeve, diferido).

**Sizing por confianza:** `monto = base × factor_estado × map_confianza(conf) × ml_mult`,
con `map_confianza: [0,1] → [REGIMEN_CONF_MIN_MULT, REGIMEN_CONF_MAX_MULT]` (ej. [0.6, 1.2]),
todo dentro de los caps existentes (`okx_spot_max_cap`, mínimo $5).

## 6. Validación ANTES de gobernar capital (shadow → live, patrón probado)

Dial `REGIMEN_BRAIN_MODE ∈ {shadow, live}` (default **shadow**):

- **shadow (default):** el cerebro computa `regimen_mercado` + confianza y **loguea** qué estado
  llama y qué le diría al grid, pero el grid **sigue corriendo con el binario actual**
  (`modo_mercado`). Cero cambio de comportamiento al desplegar.
- **live:** el grid consume el 4-estados + confianza (gate + sizing). El freeze de
  TENDENCIA_DOWN/CAOS y el sizing por confianza pasan a regir.

**Backfill (offline):** `entrenar.py --regimen-backfill --dias N` replaya OHLCV BTC diario,
etiqueta cada día con `clasificar_regimen`, e imprime una auditoría: distribución de estados,
y para cada estado el retorno forward y la oscilación (proxy de cosecha) de los días siguientes.
Criterio de graduación a `live`: RANGO precede oscilación cosechable; TENDENCIA_DOWN/CAOS
preceden drawdown/whipsaw; TENDENCIA_UP precede subidas. Reusa el patrón de `--shadow-backfill`.

## 7. Componentes

- **`clasificar_regimen(inputs) -> (estado, razon)`** — función **pura**. `inputs` = dict con
  `desv, slope, adx, atr` (lo que `Selector._inputs` ya produce). Sin red, sin estado global.
- **`map_confianza(conf) -> factor`** — pura.
- **`Selector` (renombrado conceptualmente a Cerebro):** en `run()`, además de la lógica
  SPOT/FLAT actual (que se mantiene para no romper nada), computa `clasificar_regimen` + confianza
  y publica `regimen_mercado`/`regimen_confianza`/`regimen_razon` bajo `regime_lock`, con la misma
  histéresis (`_aplicar_histeresis` extendido o uno paralelo para el 4-estados).
- **`EscuadronGridOkx._gestionar`:** si `REGIMEN_BRAIN_MODE=="live"`, el gate de entrada usa
  `regimen_mercado` (congela en TENDENCIA_DOWN/CAOS) y el sizing multiplica por
  `factor_estado × map_confianza`. Si `=="shadow"`, comportamiento idéntico al actual.
- **Backfill harness** en `entrenar.py` (`clasificar_regimen` importado de `main`).

## 8. Estado global nuevo

```python
regimen_mercado   = "RANGO"     # 4 estados; default neutro
regimen_confianza = 0.5
regimen_razon     = "BOOT"
regimen_evaluado  = False
```
(bajo `regime_lock`, junto a `modo_mercado`/`modo_operacion`).

## 9. Config nueva (.env / CONFIG)

```
REGIMEN_BRAIN_MODE       = shadow            # shadow|live
REGIMEN_ADX_TREND        = 25.0              # ADX mínimo para TENDENCIA_UP fuerte
REGIMEN_ADX_RANGO        = 20.0              # ADX máximo para RANGO
REGIMEN_DESV_RANGO       = 0.05              # |desv| máximo para RANGO
REGIMEN_CONF_MIN_MULT    = 0.6               # sizing a confianza 0
REGIMEN_CONF_MAX_MULT    = 1.2               # sizing a confianza 1
REGIMEN_FACTOR_RANGO     = 1.0
REGIMEN_FACTOR_TEND_UP   = 0.7
```
`validar_config`: abortar si `REGIMEN_BRAIN_MODE` inválido; warning si `live` (gobierna capital).

## 10. Testing (`test_regimen.py`)

- `clasificar_regimen`: inputs sintéticos → uptrend claro→TENDENCIA_UP, rango→RANGO,
  vol alta→CAOS, downtrend→TENDENCIA_DOWN, bordes/fallback→RANGO.
- `map_confianza`: bordes (0→min, 1→max, monotonía).
- Integración: grid congela en TENDENCIA_DOWN/CAOS y opera en RANGO/TENDENCIA_UP cuando
  `REGIMEN_BRAIN_MODE=live`; modo `shadow` NO altera el grid (comportamiento idéntico al actual).
- `validar_config`: rechaza modo inválido.

## 11. Documentación (mismo turno)

- Bloque V1.8.0 en `bot.txt` (clase/estado/hilos/config).
- Nota V1.8.0 en `CLAUDE.md`.

## 12. Riesgo

Muy bajo. Default `shadow` = cero cambio de comportamiento. El grid solo cambia tras flip
explícito a `live`, justificado por la auditoría del backfill. `RadarRegimen`, short-shadow y
earn intactos.
