# Ladder Grid — Spec de diseño (SP1: simulador fee-honesto)

**Fecha:** 2026-06-22
**Estado:** diseño aprobado (brainstorm), pendiente de plan de implementación.
**Alcance de ESTE spec:** Sub-Proyecto 1 = **medición**. NO toca bot/DB/capital. SP2 (sleeve live) diferido y gateado por el resultado de SP1.

---

## 1. Motivación (con datos)

V1.8.7 (`--grid-rango`) **refutó** la tesis "el grid cosecha bien la oscilación en RANGO": el grid actual captura **<3% de la oscilación disponible** (7-10 harvests en 2 años contra cientos de swings ≥2%). La causa NO es el gate de entrada (aflojarlo no cambió nada) sino la **arquitectura**: el grid entra una vez (`portfolio is None`), raspa 15%/tier, **nunca cierra del todo** → no re-entra → es un **acumulador, no un oscilador**.

El candidato de rediseño es un **grid ladder round-trip clásico**: comprar abajo, vender *las mismas unidades* arriba, repetir — cosechando cada swing. Antes de tocar el bot lo **medimos en simulador**, fiel al linaje de esta sesión (short-shadow, grid-sweep, grid-intrabar, grid-rango): la medición es barata, el capital no.

**El crux que mata o salva la idea: FEES.** Un ladder hace *muchos* trades. Un round-trip a fee maker ~0.16% sobre un swing de 2% se come ~8% del bruto. Si el edge no sobrevive fees realistas, se descarta en SP1 y nos ahorramos el rediseño.

## 2. Objetivo de SP1

Responder con datos: **¿un grid ladder round-trip captura la oscilación de RANGO de forma rentable, neto de fees realistas, y le gana al grid actual + buy&hold de forma robusta?**

- **Sí robusto** → habilita SP2 (diseño del sleeve live aislado).
- **No** → se descarta (linaje medir-y-descartar), spec archivado como evidencia.

## 3. No-objetivos (YAGNI)

- NO se diseña ni construye el sleeve live (`EscuadronLadderOkx`) en SP1.
- NO se toca `main.py`, `quantum.db`, ni capital real.
- NO se modela order-book / slippage fino (se aproxima con fees maker/taker honestos; el slippage de un maker-limit en par líquido es ~0).
- NO se barre el universo de pares (se usa BTC/ETH/TRX, los mismos del linaje).
- NO se optimiza a muerte: el objetivo es **decisión binaria** (¿hay edge?), no el ladder óptimo.

## 4. Mecánica del simulador `sim_grid_ladder` (pura, fiel a 15m)

Función **pura**, mismo patrón que `sim_grid_intrabar` (V1.8.5). Entrada: `bars` (list de dict `{close,high,low,atr_pct,...}`) + `operable` (gate de régimen por barra) + params. Salida: dict de métricas.

**Escalera:**
- Referencia móvil `ref` por **segmento RANGO** (se fija al entrar a un segmento operable; se re-centra al iniciar un segmento nuevo). Evita re-centrar dentro del segmento (un grid estático dentro del rango es lo correcto; el trend lo maneja el gate).
- `N` rungs de compra en `ref·(1 − k·S)`, k=1..N. `S` = spacing (fijo% **o** ATR-escalado vía `_atr_mult` reusado).
- Cada rung tiene capital fijo `invest_per_rung_pct · capital0` (capital-constrained: no compra si no hay cash).

**Round-trip:**
- Cuando el `low` de una barra toca un rung de compra armado → **fill maker** de ese rung; se coloca una **venta de esas mismas unidades** a `fill_px·(1 + S)`.
- Cuando el `high` de una barra toca esa venta → **fill maker**; `realizado += unidades·(venta − compra) − fees(2 lados)`; el rung de compra se **re-arma** a su nivel.
- **Intrabar pesimista:** dentro de una barra, ordenar high/low de forma conservadora (si la barra baja-y-sube, primero los buys del low, después los sells del high — pero un buy y su sell NO se cierran en la misma barra salvo que el rango intrabar cubra `2S`; documentar el supuesto).

**Gate de régimen:**
- `operable[i]=False` (TENDENCIA_DOWN/CAOS) → **no se arman compras nuevas**; las ventas abiertas siguen vivas (deja desarmar el inventario), pero **no se re-centra**. Política de flatten al salir del rango = fork a barrer (ver §5).

**Fees:**
- `fee_maker` por lado (default OKX spot maker ~0.0008). Round-trip = `2·fee_maker`. Param para barrer maker-only vs taker (~0.001).

**Cierre:** inventario abierto al final → m2m al último close (igual que `sim_grid_intrabar`).

**Salida (dict):** `net_pct`, `realizado_pct` (suma de round-trips netos), `n_roundtrips`, `n_buys`, `inv_abierto_pct` (m2m trabado), `cash_final`, `qty_final`.

## 5. Barridos y robustez (`ladder_sweep`)

Orquestador (impuro, imprime, no escribe DB). Sobre BTC/ETH/TRX, 730d, **aislando RANGO** (reusa `_regimen_estado_por_ts`):

1. **Spacing `S`:** {1%, 2%, 3%} fijo, + ATR-escalado.
2. **N rungs:** {3, 5, 8}.
3. **Fees:** maker (0.08%) vs taker (0.10%) — sensibilidad explícita.
4. **Flatten al salir de RANGO:** {sí, no} — fork de política.
5. **Robustez por mitades** (1ª/2ª) como en `grid_intrabar`.
6. **Baselines en la misma corrida:** grid actual (`sim_grid_intrabar`) y buy&hold, dentro de RANGO, para comparación directa.

## 6. Criterio de decisión (gate honesto)

Se habilita SP2 **solo si**, neto de fees **maker realistas**:
- `realizado_pct` del ladder es **robustamente positivo** (ambas mitades, ≥2 de 3 pares), Y
- supera materialmente la cosecha del grid actual (`sim_grid_intrabar` realiz ~0.3-0.9%/2 años), Y
- no depende de fees taker→maker irrealistas (si solo funciona a 0% fee, se descarta).

Si el edge se evapora con fees realistas o no es robusto entre mitades/pares → **descartar**, archivar como evidencia. (El "comprar+raspar+aguantar" conservador actual preservó capital; no se reemplaza por algo que sangra en fees.)

## 7. Componentes y aislamiento

| Unidad | Qué hace | Pura | Tests |
|---|---|---|---|
| `sim_grid_ladder(bars, operable, ...)` | simula el ladder round-trip 15m, capital+fee constrained | ✅ | invariantes: round-trip vende lo comprado; fee reduce realiz; gate frena compras; capital finito; spacing más ancho = menos round-trips |
| `ladder_sweep(dias)` | orquesta barridos + baselines + mitades, imprime | ❌ (red/IO) | smoke (no en CI) |
| reusados | `_regimen_estado_por_ts`, `_bars_15m`, `_atr_mult`, `sim_grid_intrabar` (baseline) | — | ya testeados |

CLI: `python entrenar.py --ladder-sweep --dias 730`. Sin escritura a DB.

## 8. Tests (TDD, patrón `test_grid_*`)

`test_ladder_grid.py` — invariantes de `sim_grid_ladder` (PURO, sin red):
- round-trip cierra las mismas unidades que abrió (conservación).
- una venta a `+S` sobre su compra deja `realizado > 0` antes de fees, y `fee` lo reduce.
- `operable=False` no arma compras nuevas.
- capital finito: `cash_final ≥ 0`.
- spacing más ancho → `n_roundtrips` menor o igual (monotonía).
- un swing de `2S` redondo cosecha exactamente un round-trip por rung tocado.

## 9. Decomposición / siguiente paso

- **SP1 (este spec):** simulador + barridos + decisión. Termina en deploy-or-discard.
- **SP2 (diferido, gateado por SP1 positivo):** spec aparte del sleeve live `EscuadronLadderOkx` — **aislado, additivo, NO convierte el grid core** (el acumulador preservó capital; el ladder es otra filosofía, se suma como la Pata Bajista, con su propio envelope de capital y gate de régimen). No se diseña hasta tener el verde de SP1.

## 10. Riesgos del propio experimento

- **Fidelidad intrabar:** una barra 15m puede barrer varios rungs; el supuesto pesimista de ordenamiento debe documentarse y testearse (no inflar round-trips imposibles).
- **Re-centrado:** la política de `ref` por segmento es una aproximación; si domina los resultados, sensibilizar.
- **Fees:** el parámetro maker debe ser el real de la cuenta OKX, no optimista. Es el factor decisivo.

---

## 11. RESULTADO SP1 (2026-06-22, `--ladder-sweep` 730d) — señal real PERO condicional

**El mecanismo round-trip FUNCIONA y los fees NO lo matan** (refuta el crux temido):
- `realiz%` (cosecha neta de fees maker): BTC **+12.1%**, ETH **+10.7%**, TRX **+7.2%** (mejor combo 1%/8rungs) vs grid actual +0.87/+0.85/+0.50% → **~10× más cosecha**, robusto en ambas mitades, los 3 pares. A fee **taker** apenas baja (12.1→11.5) → fees NO son el asesino. **Primera señal positiva robusta de toda la sesión.** El grid actual SÍ deja oscilación cosechable en la mesa.

**PERO el `net%` revela el modo de falla clásico del grid: inventario varado.** Los rungs que compran en la bajada y quedan colgados cuando el rango se rompe sangran m2m más de lo cosechado:
- naive (flatten=F): BTC net −1.0%, ETH −7.2% (TRX +7.2% solo porque subió).
- con `flatten_on_exit=T` (vende colgados al salir de RANGO): **BTC → +4.1% net robusto ambas mitades** (arreglado); **ETH → sigue −5.8%** (en un −47% secular, hasta los días RANGO sangran; flatten solo *realiza* las pérdidas — no se puede gridear un cuchillo que cae); **TRX → +3.4%** (flatten estorba: vende ganadores temprano; y de todos modos buy&hold +178% lo aplasta).

**Verdad profunda (de manual):** el grid gana en lateral GENUINO y pierde/sub-rinde en tendencia (down = inventario varado sangra; up = vende temprano y se pierde el ride). El gate RANGO **diario** no es lo bastante fino para aislar "genuinamente lateral" — los días RANGO de ETH igual van net-abajo.

**VEREDICTO:** NO es deploy limpio (pierde en ETH-tipo, sub-rinde en TRX-tipo, naive net-negativo). NO es callejón sin salida (cosecha real; BTC net-positivo robusto con flatten). Es **lo primero que vale continuar**. La señal es real; convertirla en net-positivo robusto *cross-asset* exige SP2: gestión de inventario (flatten + stop a rungs colgados) + un gate de "lateral genuino" más fino que el RANGO diario + **selección de activo** (probablemente un sleeve aislado sobre un par genuinamente range-bound — conecta con `screener_pares.py` — NO sobre BTC/ETH core ni sobre un activo en declive secular). **SP2 condicionado a la decisión del usuario.**
