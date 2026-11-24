# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## `bot.txt` es la spec autoritativa — léela primero

Todo el detalle técnico (config, clases, hilos, esquema BD, flujo de arranque, capas de riesgo) vive en `bot.txt` (~3500 líneas), **encabezado por el changelog completo en orden inverso** (V1.9.20 arriba). Este archivo se mantiene corto a propósito: es el mapa, no el territorio. Si los dos se contradicen, **`bot.txt` gana** y este archivo se corrige.

**Regla operacional:** todo cambio significativo (clase nueva, hilo nuevo, constante, schema BD) se refleja en `bot.txt` **en el mismo turno**. Lo que no está en `bot.txt` no está documentado.

## Estado (2026-08-03, V1.9.20)

> **DESACTIVADO EL 2026-08-18 por decisión de Juan.** `brain.service` está
> `stopped` + `disabled` (no rearranca en boot); el colector de microflujo,
> retirado. Motivo, medido esta sesión: ninguna estrategia sobrevive neta de
> fees —shadow chandelier +0,0135%/trade (t=0,16, ruido); microflujo
> DESCARTADO (IC≈0 < 2×fee); momentum/reversión/OBI pierden fuera de muestra;
> funding carry ~5% ≈ Earn sin batirlo— y la cuenta (2.965 USDT) es demasiado
> pequeña para cubrir el VPS a cualquier retorno seguro. **Todo el USDT
> (2.965,22) está en OKX Simple Earn**; cero órdenes, cero posiciones al apagar.
> Reactivar: `systemctl start brain` (el código queda intacto). Lo de abajo
> describe el bot cuando corría.

- **El bot corre en la VPS 24/7.** `/mnt/data/brain` (Linux) es el repo de **desarrollo**: `quantum.db` / `microflujo.db` / `logs/` locales son copias que pueden estar **stale**. Para datos vivos: el MCP de OKX (cuenta real) o traer la DB de la VPS.
- **Todo cambio a `main.py` es cambio de runtime → se despliega al VPS y se reinicia EN LA MISMA PASADA**, sin preguntar (autorización permanente de Juan, 2026-08-02). El repo local no es el runtime: un fix commiteado y no desplegado es un fix que no existe.

  ```bash
  ssh debian@192.99.42.108 'cp ~/brain/main.py ~/brain/main.py.bak-$(date +%Y%m%d-%H%M%S)'
  scp main.py debian@192.99.42.108:brain/main.py
  md5sum main.py; ssh debian@192.99.42.108 'md5sum ~/brain/main.py'   # deben coincidir
  ssh debian@192.99.42.108 'sudo systemctl restart brain'             # SIGTERM → graceful_shutdown
  ```

  El bot corre bajo `brain.service` (systemd, `enabled`, `WorkingDirectory=/home/debian/brain`); `sudo` no pide contraseña. **`systemctl restart`, nunca `touch KILL`**: SIGTERM entra por `graceful_shutdown` y deja los maker limits resting para que el reconcile los recupere, mientras que el kill-switch fuerza market-sell de las holdings del ladder. Tras reiniciar, verificar de verdad (directiva 24): `systemctl status brain`, `logs/errors.log` desde la marca de tiempo del arranque, y que el motor tocado dejó rastro en `quantum.db`.
- Git es **local** (sin remoto). `main` es la rama de integración.

| Módulo | Estado | Notas |
|---|---|---|
| OKX spot (`EscuadronGridOkx`) | **REAL** | BTC/USDT, ETH/USDT (`OKX_PARES_SPOT`); gateado por el Cerebro de Régimen |
| Earn cripto + USDT (OKX) | **ACTIVO** | `GestorInventario` (ccy) / `GestorTesoreria` (USDT) |
| Ladder sleeve (`EscuadronLadderOkx`) | **CANARY vivo** | round-trip spot aislado, cap ~$100, par en `LADDER_PARES` (el canary corrió sobre TRX); mide fill-real vs fill-ideal |
| Pata Bajista (`EscuadronShortOkx`) | **APAGADA** (`OKX_FUT_ENABLED=false`) | código intacto; apagada por evidencia, no por principio (V1.7.10) |
| `GestorSalidaShadow` | ON como **sensor** | paper puro, cero capital |
| ML `xgboost_oracle.joblib` | **NEUTRALIZADO** (`ML_SIZING_ENABLED=false`) | AUC walk-forward ≈0.50; plumbing revivible |
| Colector de microflujo | **RETIRADO** (2026-08-18) | alimentaba el experimento microflujo, cerrado en DESCARTAR; `microflujo.db` se conserva como dato histórico |
| Binance | **solo datos** | `FuenteBinance.exchange_read`: OHLCV BTC/ETH + funding BTC |
| `LIQUIDATE_ONLY=true` | por defecto | las alts heredadas se gestionan hasta liquidarse; ningún motor de entrada las toca |

## Comandos

```bash
# Entorno (Linux; el venv está gitignored y puede no existir tras un sync)
python3 -m venv venv && venv/bin/pip install -r requirements.txt

venv/bin/python -m pytest -q                    # suite completa: 381 tests, ~6s (verificado 2026-08-03)
venv/bin/python -m pytest test_ladder_okx.py -q # un archivo
venv/bin/python -m pytest test_regimen.py::test_histeresis_no_deja_sostenido_desde_none_tras_match -x -q   # un test
venv/bin/python -m pytest -k ladder -q          # por patrón

venv/bin/python main.py     # arranca el bot (necesita .env con claves OKX + Binance read)
touch KILL                  # parada de emergencia: KillSwitchMonitor cierra todo en <5s
```

No hay linter ni type-check en CI (no hay CI). `mypy --check-untyped-defs main.py` se corrió una vez a mano y cazó un bug real (V1.9.11); el resto de sus hallazgos es ruido de globals sin tipar.

**Herramientas standalone** (ninguna toca el bot vivo, la DB de producción ni capital):

```bash
venv/bin/python entrenar.py                       # mining OHLCV 60d + train del ML
venv/bin/python entrenar.py --solo-mining|--solo-train|--dias 30 --pares BTC,ETH
venv/bin/python entrenar.py --<experimento> --dias 730   # ver tabla de veredictos abajo
venv/bin/python lab.py --all --dias 730           # banco de features candidatas → tabla-veredicto
venv/bin/python lab.py --requote --refresh --cartera --dias 730   # SP4: re-quote del ladder
venv/bin/python lab.py --microflujo               # analiza microflujo.db (no entra en --all)
venv/bin/python screener_pares.py --top 25 --dias 365   # rankea pares OKX para el grid
venv/bin/python liquidar.py [--ejecutar]          # one-shot: todo OKX → USDT (dry-run por defecto)
venv/bin/python limpiar_fantasma.py [--ejecutar]  # borra filas fantasma de short_positions (bot APAGADO)
nohup venv/bin/python -u colector_microflujo.py & # daemon de microestructura → microflujo.db
```

Los flags de experimento de `entrenar.py`: `--shadow-backfill`, `--barrer-senal`, `--regimen-backfill`, `--sleeve-backfill`, `--grid-sweep`, `--grid-intrabar`, `--grid-rango`, `--ladder-sweep`, `--ladder-scan`, `--regimen-longflat`. Ninguno escribe en la DB salvo `--shadow-backfill`.

**`/security-review` NO corre aquí** (verificado 2026-08-02), por dos razones que no se arreglan solas: el comando arma su contexto con `git diff origin/HEAD...` y este repo **no tiene remoto**, así que `origin/HEAD` ni resuelve; y aunque lo tuviera, aquí no hay flujo de rama-protegida —todo se commitea directo sobre `main`— de modo que «los cambios pendientes de la rama actual» es vacío por construcción. **La unidad auditable de este proyecto es un rango de commits, no una rama.** Se recoge con base explícita y se despacha con la skill `revisar-seguridad`:

```bash
python3 /mnt/data/conocimiento/diff_para_revisar.py /mnt/data/brain --base <commit> --salida /tmp/rev.diff
```

Sin `--base` el script se niega ruidosamente (base y rama serían ambas `main`), que es la conducta correcta: un informe «sin hallazgos» sobre cero líneas es el fallo silencioso que la directiva 25 existe para evitar.

## Arquitectura

**`main.py` (~5500 líneas) es el único archivo de código en runtime.** Todo plano en la raíz, sin subdirectorios; la única carpeta que el bot crea es `logs/`. Los demás `.py` son auxiliares standalone que se importan entre sí (`lab.py` importa primitivos de `entrenar.py`; `entrenar.py` reusa funciones de `main.py`).

**Hilos daemon** (los arranca `main()`; 15 con la config actual — 5 compartidos + 7 OKX + ladder + shadow + entrenador):

- *Compartidos:* `MotorIndicadores` (OHLCV+indicadores 15m a RAM), `RadarRegimen` (binario ALCISTA/BAJISTA por BTC vs EMA200d), `Selector` (**el Cerebro de Régimen**), `KillSwitchMonitor`, `RadarTelemetria` (HUD).
- *OKX spot/earn:* `WebSocketFeedOkx`, `OmniRiskOkx` (salidas/stops, tick 1s), `CentinelaAsincronoOkx`, `EscudoDCAOkx`, `EscuadronGridOkx`, `GestorTesoreria`, `GestorInventario`.
- *Opcionales:* `EscuadronLadderOkx` (canary), `GestorSalidaShadow`, `EscuadronShortOkx` (off), `EntrenadorPeriodico`.

**La cadena de gateo del Grid** — lo que hay que entender antes de tocar entradas:

```
MotorIndicadores → Selector._publicar_regimen (bajo regime_lock)
  → clasificar_regimen() ∈ {TENDENCIA_UP, RANGO, TENDENCIA_DOWN, CAOS} + confianza + histéresis
  → grid_regimen_decision(mode, modo_mercado_bin, estado, conf, evaluado) → (opera?, factor_clip)
  → EscuadronGridOkx dimensiona y postea
```

RANGO opera pleno, TENDENCIA_UP ×0.7, TENDENCIA_DOWN/CAOS congelan. `evaluado=False` (aún no hubo primera clasificación) ⇒ no opera — es el warmup guard de V1.8.6, cerró una ventana de ~60s post-boot en la que el Grid entraba sobre el default optimista. `RadarRegimen` es **otro** cerebro (binario, vestigial-pero-vivo) que alimenta shadow/panic/HUD; no confundirlos.

**El ladder está deliberadamente aislado del Grid core:** thread, capital, tablas (`ladder_*`) e inventario propios. `GestorInventario._syms_objetivo` **excluye** los ccy de `PARES_LADDER` y `omnirisk_debe_gestionar()` también — el acoplamiento tesorería↔ladder y OmniRisk↔ladder ya causó dos bugs en vivo (V1.9.9 Fix C, V1.9.10). Cualquier motor nuevo que toque balances debe repetir esa exclusión.

**Núcleo `okx-semantica`** (funciones puras a nivel de módulo, `main.py:280-465`) — la infra extraíble (directiva 9) y donde vive la clase de bug más cara del proyecto, *la semántica del exchange*:

- `piso_monto(amt, decimales)` — **FLOOR, nunca `round()`**: todo movimiento de saldo (transfer/purchase/redeem) se trunca. `round()` sube por encima del disponible → OKX 51008/58350. Reapareció 4 veces por estar reimplementado en 6 call-sites.
- `nuevo_clordid(prefijo)` + `_recuperar_por_clordid` — idempotencia at-most-once de las escrituras del ladder. Un timeout ambiguo bajo `okx_retry` duplicaba órdenes.
- `okx_saldo_earn/_funding`, `okx_transfer`, `okx_earn_purchase/_redeem` — los 5 helpers que `GestorTesoreria` (USDT, 4 dec) y `GestorInventario` (ccy, 8 dec) duplicaban; ahora delegan, las diferencias van como parámetro.
- `okx_retry` + `_es_error_antibot` + `_aplicar_antibot` — Cloudflare 403 en ráfagas: reintenta **solo** lo transitorio, re-lanza auth/fondos/lógica en el acto.

**Estado compartido:** `quantum.db` (SQLite WAL, prefijo de tabla por módulo: `okx_*`, `short_*`, `ladder_*`, `sys_*`, `ml_feature_log`) + globals en RAM (`PRECIOS_RAM`, `FUNDING_RAM`, `REGIMEN_*` bajo `regime_lock`). `microflujo.db` es una base **aparte** del colector.

**Invariantes que cruzan archivos** (romperlos no da error de compilación, da datos falsos):

- Las features del ML deben ser **idénticas** en `main.py` y `entrenar.py`: `[rsi, adx, ema_dist, macd, vol_ratio]` (+`funding` si `ML_FUNDING_FEATURE=true`) y el label (TP +1.2% antes que SL −2% en 24 velas de 15m). La identidad está forzada por test: `main.ml_feature_names() == entrenar.FEATURES_ORDER`. Cambiar `ML_FUNDING_FEATURE` invalida el modelo vigente → re-mine limpio + reentrenar.
- `xgboost_oracle.joblib` se **hot-recarga por mtime**: reentrenar no exige reiniciar el bot.
- `portfolio_seed.json` aporta el `avg_cost` histórico que lee `sincronizar_okx_spot_arranque`; sin él, las posiciones heredadas arrancan sin costo base.

**Superficie testeable:** la lógica pura vive a nivel de módulo (`clasificar_regimen`, `regimen_propio`, `ladder_acciones`, `senal_short`, `calcular_size_short`, `pol_*`, `seleccionar_politica_*`, `omnirisk_debe_gestionar`, `piso_monto`…) y los hilos la consumen. Los tests de ejecución corren contra fakes (`FakeOkxSpot`, `FakeOkxFut`) — **sin red y sin capital**. Un cambio nuevo se diseña para caer en una función pura o en un fake; si no cabe en ninguno, probablemente esté en la capa equivocada.

## Cómo se decide aquí: medir primero, descartar casi todo

Este proyecto tiene un linaje explícito: **ninguna estrategia llega a capital sin una medición honesta que la sobreviva** (robustez por mitades + gate apareado LCB>0 + neta de fees + que gane *por cosecha*, no por beta). La mayoría muere en la medición, y eso se documenta para no re-litigarlo. Antes de proponer una idea, revisá si ya está en esta tabla:

| Idea | Veredicto | Dónde |
|---|---|---|
| ML predictivo (xgboost, BTC-lead, funding feature) | **muerto** — AUC ≈0.50 walk-forward | V1.8.3 |
| Geometría del grid (dip×tp, ancho de tiers, invest_pct) | **negativo** — invest_pct es dial de riesgo, no edge | V1.8.4-8.5 |
| "El grid cosecha RANGO" | **refutado** — captura <3% de la oscilación; el cuello es arquitectónico, no el gate | V1.8.7 |
| Grid spot **en vivo** | **0 órdenes en 59 días** — el gate lo congeló 59/59 días (BTC TENDENCIA_DOWN). No está roto; tampoco se puede afirmar que funcione | medido 2026-08-03 |
| Sleeve trend-ride (TENDENCIA_UP) | medido, **no desplegado** (BTC pierde; ETH n=9-17) | V1.8.1 |
| Long/flat por régimen (earn-bajista + hold-alcista) | **refutado** walk-forward (1/8 robusto) | `--regimen-longflat` |
| Pata Bajista (short real) | **apagada con datos** (edge no significativo, net-neg tras fees) | V1.7.10 |
| Catálogo de bots comerciales (skew, vol-spacing, rebalance, edge-positioning, infinity-grid, funding-farm) | **todo descartado** (rigor o envelope) | `lab.py`, V1.9.2-9.3 |
| Microestructura gratis (OI+funding) | inconcluso-negativo; el +0.35% era artefacto de ventanas solapadas | V1.9.4 |
| **Ladder round-trip** | **la única señal positiva** — ~10× la cosecha del grid, fee-proof; en canary | V1.8.8-9.0 |
| Re-quote del ladder (deriva / tiempo / cartera) | **descartado** — funciona donde la escalera está varada (LTC +3.6pp) pero empeora donde ya cosecha (BTC, XRP); LCB≤0 | V1.9.15, `lab.py --requote` |
| Microflujo (CVD/OBI del colector) | **DESCARTADO** (2026-08-18) — IC-OBI +0.00–0.006%/op, IC-CVD +0.00–0.002%/op sobre 54d/77k barras (n=5132 vent. indep.), muy por debajo del umbral operable ~0.16% (2×fee). Cerrado el último experimento abierto | `lab.py --microflujo`; colector retirado del VPS |
| ARB, opciones OKX, reescritura en Rust | descartados por diseño/envelope | ver abajo |

Dos lecciones metodológicas que se pagaron caro: **ventanas de forward solapadas inflan el LCB** (V1.9.4 se cazó a sí mismo un falso positivo) y **un walk-forward honesto baja la AUC pero deja de mentir** (V1.7.5).

## Lo que NO hay que hacer

- **No crear módulos en subdirectorios.** `main.py` es el único archivo de código en runtime.
- **No tocar `bot.txt` sin actualizar el código correspondiente** (ni al revés).
- **No commitear** `.env`, `*.db`, `*.joblib` (ya están en `.gitignore`).
- **No reintroducir trading en Binance.** Es solo fuente de datos. El único venue de trading es OKX.
- **No reintroducir ARB** (no copiable, edge de capacidad limitada) ni **opciones OKX** (coin-margined, europeas, settle en cripto → reintroduce riesgo coin-margined; descartado 2026-06-15). Opciones USDT-collateralized serían otro venue = otro proyecto.
- **Futuros solo dentro del envelope de la Pata Bajista** (leverage ≤5x, isolated, stop server-side, gate de régimen). `validar_config()` aborta con `SHORT_LEVERAGE>5` o `SHORT_MAX_EQUITY_PCT>0.30`. Nada de futuros direccionales fuera de ahí.
- **No re-habilitar entrada en alts** (`LIQUIDATE_ONLY=false`) — para sumar un par se usa `OKX_PARES_SPOT`, que lo hace quirúrgicamente.
- **No correr dos instancias contra la misma cuenta OKX** (órdenes duplicadas / reconcile en conflicto).
- No usar `round()` sobre un monto que se le pide a OKX. `piso_monto()`.

## Dónde está lo demás

Specs y planes por subsistema (`*_spec.md` / `*_plan.md`): `ladder_grid_sp{1,2,3}`, `cerebro_regimen`, `short_bajista`, `gestor_salida_shadow`, `sleeve`, `regimen_longflat`, `lab_features`, `liquidar`, `eliminar_bin_fut_arb`. Runbooks operativos: `canary_ladder.md`, `canary_short.md`, `colector_microflujo.md` (incluye el deploy systemd del colector). Cada uno documenta el *porqué* de un subsistema mucho mejor que el código.

## Convenciones

- **Todo en español**: docs, comentarios, identificadores internos (`caja_negra`, `GestorInventario`, `senal_short`). Los mensajes de commit también.
- **TDD para los fixes de runtime**: cada bug de los V1.9.x salió con su test en rojo primero, y los tests nombran el escenario real que reprodujeron.
- **Fail-safe explícito**: ante dato ausente o error, la conducta es *no operar* (`ml_predict_prob` → `None` → sizing neutro; par sin datos OKX → no opera; `evaluado=False` → no entra).
