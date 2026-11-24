# lab.py — Banco de medición de features candidatas (spec)

**Fecha:** 2026-06-24
**Estado:** spec aprobado, pendiente de implementación (TDD)
**Origen:** investigación de bots open-source (Freqtrade, Hummingbot) + comerciales (Pionex, 3Commas,
KuCoin/Shrimpy). 5 hallazgos con encaje estructural/no-predictivo → este banco los mide, compara,
revisa y evalúa con el mismo rigor que ya usa el bot, antes de tocar runtime/capital.

## Principio

`lab.py` es **standalone, read-only de mercado, no toca bot/DB/capital** (idéntico envelope a
`screener_pares.py`, `liquidar.py`, `entrenar.py --*-sweep`). **Mide, no despliega.** Un hallazgo que
salga `DESPLEGAR` recién ahí habilita una discusión de runtime — y skew/vol entrarían por el canary
del ladder, no de golpe. Magnitudes esperadas modestas (~0.5–2%/año): el edge del bot es estructural.

## Arquitectura

Archivo nuevo `lab.py` en la raíz. **Importa** los primitivos probados de `entrenar.py`
(`_bars_15m`, `_regimen_estado_por_ts`, `sim_grid_ladder`, `_atr_mult`, constantes de universo) — NO
los duplica. Tres capas:

### 1. Núcleo evaluador (la parte "revisar + evaluar exhaustivamente")

Funciones **puras**, sin red, testeables por inyección:

- `lcb_apareado(deltas, z=1.645)` → media de δ (variante − baseline, por unidad pareada) y su
  **límite inferior de confianza** usando **n efectivo de Kish** (`(Σw)²/Σw²`, aquí w=1 → n real),
  replicando la lógica de `seleccionar_politica_meta_apareado` del short. `LCB = mean − z·se`.
  Significativo ⟺ `LCB > 0`.
- `veredicto(full, mitad1, mitad2, lcb, fee_taker=None, fee_maker=None)` → dict con:
  - `robusto` = `mitad1.net > 0 and mitad2.net > 0 and full.realiz > 0` (robustez por mitades)
  - `significativo` = `lcb > 0` (gate apareado) — `None` si el hallazgo no tiene baseline pareable
  - `fee_proof` = `fee_taker is None or fee_taker.realiz > 0` (sobrevive maker→taker)
  - `por_cosecha` = `full.realiz > 0 and full.realiz >= full.get('trab', 0)` (no es beta)
  - `veredicto` ∈ `{DESPLEGAR, MEDIR-MÁS, DESCARTAR}`:
    - `DESPLEGAR` ⟺ robusto ∧ por_cosecha ∧ fee_proof ∧ (significativo es True o None)
    - `DESCARTAR` ⟺ ¬robusto ∨ (significativo es False)
    - `MEDIR-MÁS` ⟺ resto (señal parcial, muestra fina, o no concluyente)
- `fmt_veredicto(nombre, d)` → línea de tabla homogénea.

El núcleo es **agnóstico del hallazgo**: cada adaptador le pasa resultados normalizados
`{net, realiz, trab}` (full/mitades) + una lista de δ pareados opcional.

### 2. Cinco adaptadores

Cada uno produce los inputs del evaluador y devuelve `(nombre, dict_veredicto, detalle)`.

| Función | Flag | Qué mide | Baseline |
|---|---|---|---|
| `eval_skew(dias, pares)` | `--skew` | inventory skew: rungs sesgados hacia `inventory_target_pct` | ladder simétrico SP2 |
| `eval_vol(dias, pares)` | `--vol` | spacing por volatilidad: barre el mapa `_atr_mult` | spacing fijo |
| `eval_protections(dias, pares)` | `--protections` | LowProfitPairs + MaxDrawdown global sobre portfolio multi-par | sin protección |
| `eval_rebalance(dias, pares)` | `--rebalance` | threshold 15% vs periódico vs HODL vs earn | HODL y earn-siempre |
| `eval_edge(dias, pares)` | `--edge` | capital por par ponderado por `net%` medido | equal-weight |

**Detalle de cada adaptador:**

- **skew** — extiende `sim_grid_ladder` con un parámetro nuevo `inventory_skew` (default `0.0` =
  comportamiento idéntico al actual; se agrega aditivamente como SP2 sumó `stop_pct`, preservando
  call-sites/tests existentes). Con skew>0, el `sell_px` y los `buy_px` se desplazan según el
  inventario colgado vs un `inventory_target_pct`: más inventario → ventas más cerca del mid (vende
  ansioso), compras más lejos (compra reticente). δ pareado por par = `net_skew − net_simétrico`.
  Corre sobre los pares ganadores de SP2 (LTC/ADA/BTC/SOL/XRP).
- **vol** — `atr_spacing` ya existe en `sim_grid_ladder`; este adaptador barre variantes del mapa
  `_atr_mult` (lo/hi/low_mult/high_mult) y compara contra fijo. δ pareado por par.
- **protections** — overlay puro sobre un portfolio de N pares corridos con `sim_grid_ladder`:
  - `LowProfitPairs`: si el realiz de un par en una ventana < umbral → se lo "desactiva" (su PnL
    posterior se congela) por `stop_duration`. Mide nº de desactivaciones + Δ PnL portfolio.
  - `MaxDrawdown` global: si el equity agregado cae > `max_dd_pct` en una ventana → congela TODAS
    las entradas por `stop_duration`. Mide nº de disparos + Δ PnL.
  - No tiene "baseline pareable por unidad" clásico → `significativo=None`; se juzga por
    robusto + Δ-portfolio (drag o beneficio) en ambas mitades.
- **rebalance** — `sim_rebalance(closes_por_activo, modo, umbral_pct/periodo, fee, earn_daily)`
  **NUEVO, puro**: mantiene un target mix (default equal-weight sobre los activos + leg USDT a earn);
  - modo `umbral`: rebalancea cuando un peso se desvía > `umbral_pct` (default 15%);
  - modo `periodico`: cada `periodo` días;
  - compara vs HODL (sin rebalancear) y earn-siempre (100% USDT). Walk-forward por sub-períodos +
    mitades. Es el único hallazgo que es una **estrategia nueva** (portfolio-level), no un refinamiento
    del ladder.
- **edge** — toma los `net%` por par del scan SP2 (reusa `ladder_scan`-style por par) y construye un
  portfolio con pesos ∝ `max(0, net%)` (normalizado); compara net agregado vs equal-weight. δ = los
  dos portfolios sobre las mismas mitades.

### 3. CLI

```
python lab.py --all  --dias 730            # corre los 5 y emite la TABLA-VEREDICTO ranqueada
python lab.py --skew --dias 730            # un hallazgo solo
python lab.py --vol | --protections | --rebalance | --edge
  --pares BTC,ETH,LTC,ADA,...              # universo (default = ganadores SP2 + BTC/ETH para rebalance)
  --dias N                                 # histórico (default 730)
```

`--all` imprime al final una tabla ordenada por veredicto (DESPLEGAR > MEDIR-MÁS > DESCARTAR) y
dentro por edge, con un párrafo de cierre honesto (qué seguir, qué descartar) en el estilo de los
veredictos de `ladder_scan`/`regimen_longflat`.

## Tests — `test_lab.py` (TDD, sin red, barras sintéticas)

1. `lcb_apareado`: δ todos iguales y positivos → LCB>0; δ con alta varianza alrededor de 0 → LCB≤0;
   n=1 → se comporta defensivo (no significativo).
2. `veredicto`: las combinaciones robusto/significativo/cosecha/fee mapean al label correcto
   (tabla de verdad explícita: DESPLEGAR / MEDIR-MÁS / DESCARTAR).
3. `sim_rebalance`: (a) sin desvío (un solo activo o precios planos) → 0 trades; (b) monotonía: umbral
   más chico → ≥ trades; (c) fee>0 dragea vs fee=0; (d) earn_daily se acumula sobre el leg USDT.
4. **skew no-regresión**: `sim_grid_ladder(..., inventory_skew=0.0)` ≡ resultado actual bit-a-bit
   (invariante que protege el canary vivo).
5. protections: LowProfitPairs desactiva un par bajo-umbral; MaxDrawdown dispara cuando el equity cae
   > umbral; ambos no disparan en un caso limpio.
6. edge: expectativas iguales → pesos equal-weight; un par dominante → mayor peso a ese par.

`conftest.py` (si hace falta) no necesita cambios: `lab.py` no lee flags opt-in del bot.

## Lo que NO hace (YAGNI)

- No escribe en `quantum.db` ni en ninguna tabla. No coloca órdenes. No lee `.env` ni secrets.
- No modifica `main.py` (runtime) — un `DESPLEGAR` abre OTRA discusión/spec, no se auto-gradúa.
- No reintroduce nada predictivo (sigue muerto: ML-árbol, timing, ballenas).
- `sim_grid_ladder` solo se extiende **aditivamente** (`inventory_skew=0.0` default) — cero cambio
  de comportamiento para los call-sites/tests del ladder vivo.

## Criterio de éxito

`python lab.py --all` corre end-to-end sobre 730d reales y emite una tabla-veredicto reproducible de
los 5 hallazgos, cada uno etiquetado DESPLEGAR/MEDIR-MÁS/DESCARTAR con su evidencia (robusto,
significativo, fee-proof, por-cosecha). `pytest test_lab.py` verde. `entrenar.py` y el ladder vivo
intactos (sus tests siguen pasando).

## Doc

Al cerrar: nota en `bot.txt` + línea en `CLAUDE.md` (auxiliares) describiendo `lab.py`, y memoria con
el veredicto de cada hallazgo (medir-y-descartar o promover).
