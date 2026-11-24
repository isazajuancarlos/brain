# Re-quote del ladder (SP4) — spec de MEDICIÓN

**Estado:** medición standalone. NO toca `main.py`, ni la DB del bot, ni capital.
**Linaje:** SP1 (V1.8.8) halló cosecha · SP2 (V1.8.9) arregló el inventario varado · SP3 (V1.9.0)
puso el sleeve canary en vivo. Esto es **SP4**: por qué el canary no junta muestras.

## 1. El síntoma (dato en vivo, no hipótesis)

El canary del ladder lleva **~1 mes sin un solo fill nuevo**:

| rung | precio | colocado | tocado | lleno |
|---|---|---|---|---|
| 0 | 0.3154364 | 2026-06-26 | 2026-06-30 | ✓ (round-trip +0.2965 USDT, +1.50%) |
| 1 | 0.3106328 | 2026-06-26 | — | ✗ |
| 2 | 0.3058292 | 2026-06-26 | — | ✗ |
| 3 | 0.3010256 | 2026-06-26 | — | ✗ |
| 0' | 0.3155447 | 2026-07-03 | — | ✗ |

Cuatro órdenes colocadas hace 36 días que el mercado **nunca tocó**. El deliverable de SP3 es medir
fill-real vs fill-ideal, y con n=1 no se puede decidir nada. **La graduación está bloqueada por falta
de muestras, no por un resultado malo.**

## 2. La causa (verificada en backtest, no inferida del caso)

`sim_grid_ladder` —y el `EscuadronLadderOkx` que lo implementa— re-centra la escalera **solo en la
transición no-operable → operable**:

```python
if op and not prev_op:      # entrenar.py:1117
    ...  r["buy_px"] = px * (1 - k * S * buy_mult / 100.0)
```

Dentro de un segmento operable largo, la referencia queda **congelada en el precio del día que
empezó el RANGO**. Si el precio deriva hacia arriba, los cinco rungs quedan colgados abajo y la
escalera entera es inalcanzable. Peor: tras cada round-trip el rung se re-arma **en su precio
viejo**, no en uno relativo al mercado de hoy.

Sonda sobre 365d, 15m, RANGO-only (`_regimen_estado_por_ts` por símbolo):

| par | segmentos RANGO | barras operables | barras con el precio por encima de TODA la escalera | deriva vs ref (mediana / p90 / max) |
|---|---|---|---|---|
| LTC/USDT | 3 | 4 773 | **75.5%** | +1.24% / +6.52% / +14.61% |
| BTC/USDT | 7 | 10 437 | **64.2%** | −0.20% / +4.10% / +8.58% |

Parte de eso es normal —una escalera de compra vive debajo del precio por construcción—, pero con
spacing 2% la escalera abarca de −2% a −10%: con el precio +6% por encima de la referencia hace
falta un desplome del 8% para tocar el primer rung. Eso no es esperar un dip, es estar fuera del
mercado.

**Encuadre (directiva 18):** esto no es un fallo del gate de régimen (SP2 lo arregló y funciona),
ni de la geometría (SP1 la barrió). Es que la escalera **no sigue al mercado**. Ningún market maker
real deja una cotización quieta: Hummingbot re-cotiza cada `order_refresh_time` y salta el re-quote
si el precio se movió menos de `order_refresh_tolerance`. `lab.py` tanda 1 midió el
`inventory_skew` de Hummingbot pero **nunca midió su primitivo central, el refresh** — así que es
una feature genuinamente no medida, no una ya descartada.

## 3. Candidatas

### F1 · Re-quote por deriva (`recenter_drift_pct`)
Mientras el segmento es operable, si `|px/ref − 1| ≥ drift%` → re-centrar la escalera sobre `px` y
mover `ref = px`. Es `order_refresh_tolerance` invertido: re-cotizar cuando el precio SE MOVIÓ.

### F2 · Re-quote por tiempo (`refresh_bars`)
Re-centrar cada N barras mientras es operable, haya deriva o no. Es `order_refresh_time` puro.

### F3 · Cartera de ladders (multi-par)
SP2 escaneó 8 pares **independientes**; nunca se midió la cartera. Con el gate por régimen propio
cada par se congela en un momento distinto, así que N escaleras con capital/N deberían tener menos
tiempo muerto que una sola con el capital entero. Es la vía directa a destrabar las muestras del
canary, y es el prerrequisito que `lab.py` tanda 1 ya le puso a `protections`
(«útil cuando el ladder gradúe a multi-par»).

## 4. Diseño (y la trampa que evita)

Ambos knobs son **aditivos**, `None` por defecto ⟹ conducta bit-a-bit idéntica a la de hoy. Es la
misma disciplina que `inventory_skew=0.0`: hay un canary con plata real corriendo esta lógica y una
regresión silenciosa en el simulador contaminaría la decisión de graduarlo.

Reglas del re-centrado, por estado del rung:

| estado | qué le pasa | por qué |
|---|---|---|
| `armed` | se reposiciona sobre `px` | es el objetivo del arreglo |
| `holding` | **intacto** | tiene inventario con su venta round-trip apuntada; moverla sería cambiar el trato a mitad de camino |
| `stopped` | se re-arma **solo si el re-centrado es hacia ARRIBA** | un re-centrado hacia abajo re-compraría lo que el stop de SP2 acaba de cortar = cazar el cuchillo, justo lo que SP2 existe para impedir |

Esa tercera fila es la decisión de diseño que importa: la versión ingenua (re-armar siempre) anula
una salvaguarda ya pagada. Va con test que discrimina.

**Lo que el simulador NO puede medir:** re-cotizar más seguido empeora el supuesto de fill ideal
(una orden recién colocada que el precio roza al instante es la que menos probabilidad tiene de
llenarse en maker real). El simulador llena cuando `low ≤ buy_px`; el mundo, no. Un veredicto
positivo aquí **no** gradúa nada por sí solo: manda al canary a medirlo, que es el riesgo #1 abierto
desde SP2.

## 5. Gate de decisión

El del banco (`lab.veredicto`), sin excepciones: robusto por mitades + gate apareado LCB>0 +
fee-proof + por cosecha (no por beta). Además, específico de aquí:

- **δ vs baseline por par**, no niveles absolutos (la varianza común se cancela).
- Si sube `n_roundtrips` pero `net_pct` no ⟹ es churn, no cosecha → DESCARTAR.
- Si sube `inv_trabado_pct` ⟹ el re-quote está comprando más arriba y varando inventario → DESCARTAR.

## 6. Resultado (730d, 5 pares, fee maker 0.08%, RANGO por régimen propio)

`python lab.py --requote --refresh --cartera --dias 730`

| variante | edge medio | rob | sig | fee | cos | veredicto |
|---|---|---|---|---|---|---|
| requote-drift5 | +1.06% | ✓ | **✗** | ✓ | ✓ | DESCARTAR |
| cartera-ladders | +0.80% | ✓ | **✗** | ✓ | ✓ | DESCARTAR |
| requote-cada1d | +0.50% | ✓ | **✗** | ✓ | ✓ | DESCARTAR |
| requote-drift3 | +0.42% | ✓ | **✗** | ✓ | ✓ | DESCARTAR |
| requote-cada4d | +0.35% | ✓ | **✗** | ✓ | ✓ | DESCARTAR |
| requote-drift1.5 | +0.05% | ✓ | **✗** | ✓ | ✓ | DESCARTAR |

**El mecanismo funciona exactamente como predijo la hipótesis, y aun así no pasa.** δnet por par
(drift 5%): LTC **+3.55** · SOL **+2.99** · ADA +0.61 · BTC −0.88 · XRP −1.00. Donde la escalera
estaba varada, el re-quote multiplica la cosecha (LTC 20→82 round-trips, net +1.46%→+5.01%; SOL
10→70) **sin añadir inventario varado** (δtrab = +0.00 en las 30 corridas: la regla de no tocar los
`holding` y de no re-armar `stopped` hacia abajo hizo su trabajo). Pero donde ya estaba bien
colocada, lo empeora (BTC 86 rt, XRP 72 rt, ambos negativos en las tres variantes). Con esa
dispersión y n=5, el LCB no despega.

**F3 cartera:** net +2.26% vs par mediano solo +1.46%. Pareado por 8 ventanas trimestrales
(no por mitades: la tanda 1 ya declaró n=2 demasiado fino) → δ medio +0.112%, **LCB −0.006**. Falla
por un pelo, y el gate existe justamente para que «casi» no despliegue.

**Lo que sí queda, y no es un edge:** la cartera de 5 produce **195 round-trips contra 20** del par
mediano. El cuello del canary de SP3 es **muestral**, y se ataca con más pares, no con más retorno.
Es un argumento operativo y se reporta como tal.

**Hipótesis pre-registrada (no se persigue ahora):** el δ correlaciona negativo con lo activa que ya
estaba la escalera — `rho(round-trips base, δnet)` = −0.50 / −0.60 / −0.50 en las tres variantes.
Con n=5 eso **no** es significativo (|rho| crítico ≈0.90 a p<0.05), y condicionar la feature a los
pares que ganaron es el sobreajuste in-sample que hundió a `edge-positioning`. Si se retoma: se
declara **antes** el umbral de «escalera hambrienta» y se prueba en pares que no estén en esta tabla.
