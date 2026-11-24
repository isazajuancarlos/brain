# Ladder Grid SP2 — Spec de diseño (¿existe una config robustamente net-positiva?)

**Fecha:** 2026-06-22
**Estado:** diseño aprobado (brainstorm), pendiente de plan de implementación.
**Alcance de ESTE spec:** SP2 = **medición** (extensiones al sim + scan multi-par). NO toca bot/DB/capital. SP3 (sleeve live `EscuadronLadderOkx`) diferido y gateado por el resultado de SP2.

---

## 1. Motivación (resultado de SP1)

SP1 (`ladder_grid_spec.md` §11, `--ladder-sweep`) encontró la **primera señal real de la sesión**: el grid ladder round-trip cosecha RANGO **~10× el grid actual, neto de fees** (los fees NO lo matan), robusto en ambas mitades, los 3 pares. **PERO** el `net%` lo dragueó el **inventario varado**: rungs que compran en la bajada y quedan colgados cuando el rango se rompe. `flatten_on_exit` arregló BTC (net +4.1% robusto) pero NO ETH (−5.8%: declive secular −47%; hasta sus días RANGO sangran) ni TRX (flatten vende ganadores temprano).

**Causa raíz del fallo ETH:** en SP1 el gate fue el régimen de **BTC** (el cerebro clasifica BTC), así que el ladder se desplegó sobre ETH en días BTC-RANGO aunque ETH *propio* estuviera en declive.

**Pregunta de SP2:** ¿existe una configuración del ladder (gate + inventario + activo) que sea **robustamente net-positiva**? Si sí → SP3 (sleeve live). Si no → descartar, pero con evidencia.

## 2. Alcance / no-objetivos

- SP2 es **medición**: extiende `sim_grid_ladder` y agrega un scan multi-par. NO toca `main.py`/DB/capital.
- NO se construye el sleeve live (SP3) — diferido y gateado por SP2 positivo.
- NO se optimiza a muerte: objetivo = **decisión binaria** (¿existe config robusta?), no el ladder óptimo.
- YAGNI: tres palancas, una decisión.

## 3. Las tres palancas

### 3.1 Gate "lateral genuino" = régimen PROPIO del activo (la palanca central)
El fix más limpio y principista del fallo ETH: gatear el ladder por el **régimen 4-estados del activo mismo** (correr `main.clasificar_regimen` sobre las velas diarias del activo), exigiendo **RANGO**. Un activo en declive secular clasifica `TENDENCIA_DOWN` *en su propio marco* → el ladder se congela ahí solo, sin inventar nada. Reusa `clasificar_regimen` + `_regimen_estado_por_ts` **generalizado a cualquier símbolo** (hoy hardcodea `BTC/USDT`).

### 3.2 Stop a rungs colgados (acota el sangrado intra-rango)
`sim_grid_ladder` gana dos params:
- `stop_pct`: un rung *holding* underwater más de `stop_pct` se vende a mercado (corta la pérdida sin esperar al flatten de salida).
- `max_hold_bars`: un rung retenido más de N barras se corta (anti-inventario-zombi).
Ambos default `None`/0 = comportamiento SP1 (preserva tests). Acotan el m2m trabado *dentro* del rango, complementando `flatten_on_exit` (que solo actúa al salir).

### 3.3 Selección de activo vía scan multi-par
Correr el ladder (gate propio + stop) sobre un universo de pares (default: el set del screener / alts líquidos USDT), rankeado por net% robusto. La hipótesis: existen alts genuinamente laterales donde el ladder es **net-positivo en ambas mitades**. Conecta con `screener_pares.py` (ya rankea aptitud grid). Si **ninguno** lo logra robusto → descartar.

## 4. Componentes y aislamiento

| Unidad | Qué hace | Pura | Tests |
|---|---|---|---|
| `sim_grid_ladder(..., stop_pct, max_hold_bars)` | + corte de rungs colgados (underwater/zombi) | ✅ | stop corta underwater; max_hold corta zombi; default=SP1 sin cambio |
| `_regimen_estado_por_ts(exchange, dias, sym="BTC/USDT")` | generalizado: régimen propio de CUALQUIER símbolo | ❌ (red) | smoke |
| `ladder_scan(dias, pares)` | corre el ladder gate-propio+stop sobre universo, rankea por net% robusto ambas mitades, imprime | ❌ (red/IO) | smoke |
| reusados | `clasificar_regimen`, `_bars_15m`, `_atr_mult`, `sim_grid_intrabar` (baseline) | — | ya testeados |

CLI: `python entrenar.py --ladder-scan --dias 730 [--pares ...]`. Sin escritura a DB.

## 5. Criterio de decisión (SP2 → SP3)

Habilita el sleeve live **solo si**, a fee MAKER, con gate de régimen PROPIO + stop:
- **≥1-2 pares** muestran `net%` **robustamente positivo** (ambas mitades), Y
- el net positivo NO es trivialmente "el activo subió" (comparar contra buy&hold del par: el ladder debe ganar por **cosecha**, no por beta — net positivo con `inv_trabado` chico y `realizado` dominante), Y
- no depende de fee taker→maker irreal.

Si ningún par logra net-positivo robusto por cosecha (no por beta) → **descartar** el ladder (se suma al linaje medir-y-descartar, pero fue el que más cerca estuvo).

## 6. Tests (TDD, patrón `test_ladder_grid.py`)

Extender `test_ladder_grid.py`:
- `stop_pct` corta un rung que cae underwater más del umbral (realiza la pérdida, libera el slot).
- `max_hold_bars` corta un rung retenido demasiado tiempo aunque no esté underwater al umbral.
- default (`stop_pct=None, max_hold_bars=None`) → idéntico a SP1 (preserva los 7 tests vigentes).
- el corte respeta capital/conservación (no vende más unidades que las del rung).

## 7. Decomposición / siguiente paso

- **SP2 (este spec):** extensiones al sim + scan multi-par + decisión. Termina en SP3-or-discard.
- **SP3 (diferido, gateado por SP2 positivo):** spec aparte del sleeve live `EscuadronLadderOkx` — **aislado, additivo, capital chico, sobre el/los par(es) que SP2 corone**, con gate de régimen propio + stop server-side, NO sobre BTC/ETH core. No se diseña hasta el verde de SP2.

## 8. Riesgos del propio experimento

- **Overfitting por selección de activo:** elegir el par ganador sobre 730d puede ser data-mining. Mitigación: exigir robustez en **ambas mitades** (ya es walk-forward parcial) y reportar varios pares, no cherry-pick de uno.
- **Régimen propio de alts:** `clasificar_regimen` se calibró en BTC; sus umbrales (desv/slope/atr) pueden no transferir a alts más volátiles. Documentar; si domina, sensibilizar umbrales por activo.
- **Stop demasiado agresivo:** un `stop_pct` chico convierte el ladder en una máquina de realizar pérdidas (whipsaw). Barrer stop_pct y observar el trade-off realiz vs trabado.
- **Beta disfrazada de cosecha:** un alt que sube hace ver bien al ladder por inventario, no por cosecha. El criterio §5 lo separa exigiendo realiz dominante + comparación a buy&hold.

---

## 9. RESULTADO SP2 (2026-06-22, `--ladder-scan` 730d, 8 pares) — GATE PASA, con caveats

**El gate régimen-propio + stop arregló el inventario varado** (trab=0.00% en todos): **5 de 8 pares** net-positivo ROBUSTO (ambas mitades) por cosecha:
- **LTC** (bh −34.8%) net **+3.66%** (1.41/2.25) · **ADA** (bh −56.8%) net **+1.01%** (0.40/0.61) → **ganaron mientras el activo se desplomaba** = cosecha INEQUÍVOCA, no beta (la evidencia más fuerte).
- **BTC** (bh +7.8%) net +3.60% (2.11/1.16) · **SOL** (bh −43%) net +1.74% (1.54/0.20) · **XRP** (bh +141%) net +9.51% (4.67/1.69, el outlier).
- ✗ ETH (−0.31%), TRX (−2.39%, uptrend fuerte), BNB (−2.28%, 20 stops/whipsaw) — no robustos. El gate régimen-propio bajó el RANGO de ETH a 24% (vs 48% gateando por BTC) → el filtro de declive secular FUNCIONÓ.

**Veredicto:** el gate PASA — primera estrategia de la sesión que lo logra. La señal es real (positivo en activos que CAYERON = cosecha genuina). **PERO caveats:** (1) magnitudes modestas (~0.5-1.8%/año; XRP ~4.75%); (2) la config ganadora por par es in-sample-selected (mejor de 8 sobre los mismos 730d; robustez-por-mitades lo mitiga, no lo elimina); (3) **el riesgo #1 no modelado = fills maker idealizados** (el sim asume llenado al tocar el rung; en real puede no llenar/parcial → infla round-trips); (4) 2ª mitad más floja.

**→ HABILITA SP3** (sleeve live aislado, capital chico, sobre los pares de cosecha más limpia: LTC/ADA/BTC) **CON CANARY** que mida el gap fill-real vs fill-idealizado antes de escalar. SP3 = spec aparte (toca runtime/capital → el primer cambio al bot del hilo ladder).
