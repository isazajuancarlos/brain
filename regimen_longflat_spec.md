# Régimen Long/Flat — Spec de diseño (SP1: simulación del cambio de régimen / onset del bull)

**Fecha:** 2026-06-22
**Estado:** diseño aprobado (brainstorm), pendiente de revisión + build.
**Alcance:** SP1 = **medición/simulación**. NO toca bot/DB/capital. SP2 (gestor de posición live por régimen) diferido y gateado por SP1.

---

## 1. Motivación

Estamos **preparándonos para el cambio de régimen**: hoy el bot está bien posicionado en bear (todo USDT/earn = la mitad "earn en bajista" que funciona), pero la mitad "holdear en alcista" la ejecuta **mal** (el grid acumulador capturó solo +4.2% del run de +153% a $124k). La pregunta operativa: **cuando el alcista vuelva, ¿qué debería hacer el bot, y el long/flat por régimen lo captura de forma robusta?**

El prototipo dio **+23%** (long/flat operable) vs −6% buy&hold vs +11% earn en el ciclo del run a $124k — **pero es UNA ventana, UN activo** (un ciclo subida-sostenida+crash favorece al trend-timer). SP1 decide si es real o artefacto, **antes** de diseñar cualquier ejecución live.

## 2. Objetivo de SP1

Responder con datos: **¿el long/flat por régimen es una forma ROBUSTA de capturar el bull cuando llega (y esquivar el bear), o el +23% fue suerte de un ciclo?** Con foco explícito en el **onset del alcista** (qué pasa en la transición a operable).

- **Robusto** → habilita SP2 (gestor de posición live por régimen).
- **Artefacto** → documentar, NO construir live (el bot se queda en su modo actual, ya correcto para el bear).

## 3. No-objetivos (YAGNI)

- NO se construye el gestor live (SP2) en SP1.
- NO se toca `main.py`/DB/capital.
- NO se optimizan umbrales del régimen (se usa el cerebro tal cual, ya en `live`).
- NO HFT ni timing fino — es long/flat DIARIO (el régimen es diario).

## 4. La estrategia simulada

Long/flat sobre el core, **causal** (decide con el régimen de AYER, sin lookahead):
- Régimen ∈ `long_states` → 100% en el activo (holdear/cabalgar).
- Si no → 100% USDT rindiendo earn (~5% APR → daily).
- En cada transición: paga fee (maker/taker, barrido).
- `long_states` por defecto = **{TENDENCIA_UP, RANGO}** (operable, la variante ganadora); se compara contra {TENDENCIA_UP} solo.

## 5. Componentes (puros + harness)

| Unidad | Qué hace | Pura | Tests |
|---|---|---|---|
| `sim_longflat(closes, states, long_states, fee, earn_daily)` | camina diario causal; devuelve `{net_pct, n_switches, max_dd, pct_long, fin_cripto}` | ✅ | long sube con cripto / earn cuando flat; switch paga fee; sin lookahead; max_dd correcto |
| `_onset_captura(closes, states, long_states)` | por cada transición flat→long (onset de bull): lag desde el mínimo real, % del up-leg capturado | ✅ | captura un up-leg sintético; mide el lag |
| `regimen_longflat(dias, pares)` | corre sim_longflat sobre multi-activo × variantes × fees, **walk-forward por sub-períodos**, vs buy&hold + earn-only; imprime | ❌ (red/IO) | smoke |
| reusados | `_regimen_estado_por_ts` (régimen PROPIO por activo) | — | ya testeado |

CLI: `python entrenar.py --regimen-longflat --dias 730`. Sin DB.

## 6. Dimensiones de robustez (lo que falta para creerle al +23%)

1. **Walk-forward / sub-períodos:** parte la historia en N tramos (ej. mitades y tercios) → net% en CADA uno. ¿Gana en la mayoría o solo en el ciclo grande? (test anti-artefacto central).
2. **Multi-activo:** BTC, ETH + 2-3 alts líquidos, **régimen propio de cada uno**.
3. **Variantes de `long_states`:** {UP} vs {UP+RANGO operable}.
4. **Sensibilidad a fee:** maker 0.1% vs taker 0.2% (los switches cuestan).
5. **Onset del bull (foco del usuario):** por cada flip a operable, lag desde el piso real + % del up-leg capturado → "cuando el alcista llega, ¿qué tan rápido y cuánto agarra?".

## 7. Métricas de modo de falla (trend-timing engaña acá)

- **n_switches** (whipsaw: muchos = comprar caro/vender barato + fees).
- **max drawdown** de la estrategia (exposición a crash en V: vender el piso y perderse el rebote).
- **peor sub-período** (¿hay tramos donde sangra?).

## 8. Gate de decisión (SP1 → SP2)

Habilita SP2 **solo si**, neto de fees realistas:
- Le gana a **AMBOS** baselines (buy&hold Y earn-siempre) en **≥mayoría de sub-períodos Y ≥2 de los activos**, Y
- el onset captura una fracción material del up-leg (no entra tan tarde que se pierde el run), Y
- sin whipsaw catastrófico ni drawdown peor que buy&hold de forma sistemática.

Si solo gana en el ciclo BTC del +23% → **artefacto de un ciclo** → documentar y no ir a SP2. El bot se queda como está (correcto para el bear actual).

## 9. Decomposición

- **SP1 (este spec):** simulación de robustez + onset → decisión SP2-or-stop.
- **SP2 (diferido, gateado):** spec aparte del **gestor de posición live por régimen** sobre el core — cuando operable: ir long BTC/ETH de verdad; flip a DOWN: vender a USDT/earn. Toca runtime/capital → el cambio grande. No se diseña hasta el verde de SP1.

## 10. Riesgos del propio experimento

- **Overfit a un ciclo:** mitigado por walk-forward + multi-activo (exigir robustez en ambos).
- **Régimen propio de alts:** `clasificar_regimen` calibrado en BTC; umbrales pueden no transferir — documentar, observar.
- **Earn APR supuesto** (~5%): sensibilizar (el resultado no debería depender de un APR optimista).
- **V-crashes raros:** una sola ventana puede no contenerlos; el max_dd + peor-sub-período los expone si están.

---

## 11. RESULTADO SP1 (2026-06-22, `--regimen-longflat` 730d, 8 activos) — NO ROBUSTO, no ir a SP2

**El +23% NO sobrevivió walk-forward ni multi-activo. 1/8 activos pasó el gate** (y ese 1 es beta, no skill):
- **BTC** operable +22% total PERO tercios **+49% / −5% / −16%** → el edge es UN tramo (el run a $124k), no robustez; sangra en los otros dos tercios.
- **Cross-asset pierde:** ETH −23%, SOL −15%, BNB −31%, LTC −31% (operable). El único "ganador" robusto = TRX +204%, pero TRX buy&hold +189% → **beta del activo que mooneó, no timing**. Whipsaw brutal (SOL 50 sw, LTC 52).
- **Onset malo (la pregunta del usuario):** cuando el alcista llega, el régimen entra **4-16 días tarde**, captura mediana **≈0% (mayoría negativa)** por entrada. El régimen NO cronometra el inicio del bull; el número de BTC vino de *quedarse* long en un run sostenido (72% días long), no de buen timing.
- **Ni variante ganadora consistente:** BTC favorece operable, ETH/BNB favorecen solo-UP → el ranking se da vuelta por activo = ruido.

**INSIGHT:** el cerebro de régimen es bueno DEFENSIVAMENTE (esquivar bear sostenido, congelar grid — validado V1.8.0) pero MALO OFENSIVAMENTE para cronometrar entradas al bull. "Prepararse para el bull" vía régime-switching no funciona; lo único que captura un bull es holdear a través (= comerse el bear). No free lunch.

**DECISIÓN:** NO ir a SP2 (gestor de posición live). El bot se queda como está — la mitad defensiva (earn en bear) es la validada y es donde está hoy. Herramienta `--regimen-longflat` queda reusable. Linaje medir-y-descartar.
