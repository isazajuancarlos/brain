# Sleeve Trend-Ride — Spec (V1.8.x)

**Fecha:** 2026-06-19
**Estado:** Fase 1 (medición) en implementación. Fase 2 (ejecución) condicionada a la evidencia.
**Relacionado:** `cerebro_regimen_spec.md` (lo consume), `gestor_salida_shadow_spec.md` (chandelier, patrón medir→live)

---

## 1. Idea y bandera roja

El grid se **pierde el bull vertical**: su gate de entrada es `ADX<20`, así que en una tendencia
fuerte (ADX≥25) no compra. El sleeve trend-ride compraría BTC/ETH en TENDENCIA_UP (donde el grid
abstiene) y **cabalgaría** con un trailing stop (chandelier), capturando lo que el grid deja pasar.

**Bandera roja (datos propios):** el backfill del cerebro (756d) mostró **TENDENCIA_UP → forward 5d
= −0.29%**. La señal de TENDENCIA_UP, tal como está (desv>2% & slope>0 & ADX≥25), precede retornos
planos/levemente negativos a 5 días — comprar ahí puede ser **comprar el techo local**. Esto NO mata
la idea (5d es corto para una tendencia; quizá el horizonte/entrada deben cambiar), pero **obliga a
medir antes de arriesgar capital.** Mismo criterio con que se mató el short y los ratio-pairs.

## 2. Decisión: dos fases

### Fase 1 — Banco de medición (ESTE deliverable, sin capital)
Backfill que cuantifica: *¿comprar en TENDENCIA_UP y cabalgar con chandelier gana plata neto de
fees, y con qué parámetros?* No toca bot, DB ni capital. Reusa el labeling de régimen
(`clasificar_regimen`) + simulación buy-and-ride con salida chandelier (concepto de `pol_chandelier`).

- **Entrada:** día con estado == TENDENCIA_UP y sin posición abierta → compra al close.
- **Cabalgar:** trackea `peak` (high intrabar diario). Trailing = `peak − k·ATR` (ATRr_14, price units).
- **Salidas (lo que dispare primero, pesimista con low intrabar):**
  - chandelier: `low ≤ peak − k·ATR` → sale al trail.
  - stop duro: `low ≤ entry·(1−hard)` → catástrofe.
  - flip de régimen: estado deja de ser TENDENCIA_UP → sale al close (la tendencia terminó).
- **PnL por trade:** `(exit−entry)/entry − fees_roundtrip` (≈0.2% spot OKX).
- **Barrido:** k ∈ {2,3,4}, hard ∈ {opcional}. Reporta n trades, retorno total/avg, win%, hold medio,
  desglose chandelier vs flip vs stop. **Referencias:** buy&hold del período y % tiempo en TENDENCIA_UP.
- CLI: `entrenar.py --sleeve-backfill --dias N`.

**Criterio de graduación a Fase 2:** el sleeve debe (a) ser net-positivo tras fees y (b) batir a no
hacerlo (cash/earn) en el período. Si no → se reporta y queda apagado (como el short shadow).

### Fase 2 — Ejecución en vivo (SOLO si la Fase 1 lo respalda)
`EscuadronSleeveOkx` (hilo daemon, default off `SLEEVE_ENABLED=false`, opt-in como el short canary):
- Opera solo con `REGIMEN_BRAIN_MODE=live` y `regimen_mercado==TENDENCIA_UP`. Pares: BTC/ETH (los
  que lideran tendencias; NO TRX, baja beta).
- **Entrada:** TENDENCIA_UP + confirmación del par (close>EMA200 15m, ADX≥umbral, opcional breakout),
  clip dimensionado desde cap dedicado (`SLEEVE_MAX_EQUITY_PCT`, ej. 10%), fondeo vía
  `GestorTesoreria.rescate_urgente`. Spot puro: **sin leverage, sin liquidación.**
- **Salida:** chandelier trailing + flip de régimen (sale de TENDENCIA_UP) + stop duro. Market sell.
- **Estado:** `sleeve_positions` (symbol, entry_px, qty, peak_px, open_ts) + `sleeve_realized_pnl`.
- **Entanglement con el grid (la complejidad real):** el sleeve compra el mismo spot BTC/ETH que el
  grid (balance OKX fungible + `okx_portfolio` compartido). Fase 2 debe llevar **contabilidad lógica
  separada** (`sleeve_positions`) y garantizar que el grid solo cosecha su qty y el sleeve vende la
  suya, sin sobre-vender el balance total. Se diseña en detalle SI llegamos a Fase 2.
- **validar_config:** envelope (cap ≤ límite), warning si enabled.

## 3. Scope / no-objetivos

- Fase 1 NO toca el bot ni la DB (standalone en `entrenar.py`, como los otros backfills).
- NO reabre futuros: el sleeve es **spot puro** (la diferencia con el short es justamente esa).
- Fase 2 NO se construye hasta que la Fase 1 muestre edge. YAGNI: no resolver el entanglement
  grid/sleeve hasta saber que el sleeve sirve.

## 3b. RESULTADO Fase 1 (2026-06-19, backfill 730d BTC/ETH)

Medición corrida (`entrenar.py --sleeve-backfill --dias 730`):
- **BTC:** PIERDE en todo k (sumPnL −23/−24/−6% para k=2/3/4, win 24-31%). La bandera roja
  (TENDENCIA_UP fwd −0.29%) se materializó: la mayoría de salidas son `flip` (el "uptrend" se
  dio vuelta = comprar el techo). Buy&hold BTC fue −12%; el sleeve no lo mejora.
- **ETH:** GANA, fuerte con k=4 (sumPnL +37/+40/+52% para k=2/3/4, avg +5.8%, win 56%) mientras
  buy&hold ETH fue −54%. Captura de momentum real con stop ancho.

**Decisión: Fase 2 NO se construye todavía.** Razones: (1) BTC pierde → no es edge general de
trend-ride, es ETH-específico/período-específico; (2) n=9-17 es demasiado fino (el short se mató
con n=61 + gate apareado); el +52% de ETH puede ser 1-2 ganadores; (3) sumPnL es por-lote, no
retorno compuesto. El sleeve queda **medido y apagado**, sin capital (patrón short V1.7.10 /
ratio-pairs). ETH se DIFIERE con una pregunta: ¿el edge k=4 es robusto o suerte? → profundización
de medición (estabilidad sub-período, distribución de trades, más historia/pares, gate estadístico)
ANTES de cualquier capital. **La medición evitó que BTC sangrara en vivo — para esto era.**

## 4. Testing (Fase 1)

`test_sleeve.py`: la simulación pura de un trade (entrada→ride→salida) con barras sintéticas
(una tendencia que sube y luego retrocede k·ATR → sale por chandelier con ganancia; una que flipea
de régimen → sale al close; una que gapea abajo → stop duro). Funciones puras, sin red.
