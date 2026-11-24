# Spec — Pata Bajista (EscuadronShortOkx) · Quantum Bot V1.7.0

**Fecha:** 2026-06-11
**Estado:** aprobado para implementación (pendiente review del spec)
**Autor:** diseño colaborativo (sesión brainstorming)

---

## 1. Contexto y objetivo

El bot V1.6.0 es **OKX spot + earn puro**. En régimen ALCISTA, `EscuadronGridOkx` compra dips BTC/ETH; en BAJISTA el capital se sienta en Simple Earn sin generar más que yield. **El régimen está BAJISTA desde 2026-06-06 y la tendencia sigue** — hay upside direccional sin capturar.

**Objetivo:** agregar una **pata short apalancada y disciplinada** que opere SOLO en BAJISTA, simétrica al Grid spot (el Grid compra dips en alcista; esta pata shortea rallies en bajista). Reabre futuros OKX **conscientemente**, acotando el liquidation risk que motivó su eliminación en V1.6.0 mediante: leverage bajo, margen isolated y stop server-side persistente.

**Reversión consciente de V1.6.0:** este spec reintroduce futuros direccionales (prohibidos en CLAUDE.md "Lo que NO hay que hacer"). La justificación: el riesgo se acota por diseño (ver §4) y el VPS provee 24/7 mientras siga activo. CLAUDE.md y bot.txt se actualizan en consecuencia.

## 2. Investigación que fundamenta el diseño (top traders OKX)

Reverse-engineering de los 5 mejores lead traders OKX (API pública `copytrading/public-subpositions-history`):

| Trader | Arquetipo | Dir | Lev | Hold | Win | Payoff | Peor trade |
|---|---|---|---|---|---|---|---|
| Steady first | swing trend-short BTC | SHORT | 20x | 63h | 46% | 3.91 | **−107% (liquidó)** |
| Splendid-DDoS | scalper alta-frec | LONG | 20x | 2.2h | 76% | 0.51 | −96% |
| To the Moon Merchant | intraday balanceado | SHORT | **5-10x** | 22h | 53% | 1.38 | −77% |
| Amusing-PoW-AirBags | scalper táctico | LONG | 20x | 1.9h | 80% | 1.25 | −90% |

**Conclusiones extraídas:**
- Los que aprovechan el bear son **short-biased** (Steady first, To the Moon).
- **El arquetipo más sano es "To the Moon Merchant"**: leverage bajo, payoff positivo, holds de swing/intraday (no scalp → tolera operación no-perfecta-24/7).
- **Debilidad común de los 4** → corregible: (a) leverage 20x = liquidaciones; (b) sin stop duro = aguantan a −90/−107%; (c) sin filtro macro. Nuestra "mejora" ataca las tres.
- Ningún líder hace arb (confirma: copy = direccional puro).

## 3. Arquitectura

Nuevo hilo daemon en `main.py`: **`EscuadronShortOkx(threading.Thread)`**, espejo simétrico de `EscuadronGridOkx`. **Construcción fresca y mínima** — NO se resucita el código FUT viejo (SensorPosicionesFut/CircuitBreakerFut/WalletBridge eliminados en V1.6.0).

Reutiliza infra existente:
- `RadarRegimen` / `modo_mercado` (con `regime_lock`) → gate de entrada.
- `MotorIndicadores.get_indicators("okx", sym)` + `PRECIOS_RAM["okx"]` → señales (RSI/ADX/EMA200/MACD ya calculados).
- `GestorTesoreria.rescate_urgente(amt)` → saca margen del earn al disparar; devuelve al cerrar.
- `sys_caja_negra`, HUD (`RadarTelemetria`), `kill_switch` → logging, telemetría, KILL.
- Locks: nuevo `short_lock` por símbolo (patrón `get_entry_lock`).

**Exchange:** instancia ccxt OKX en modo swap (`options.defaultType='swap'`) — puede ser la misma `core_okx` con llamadas parametrizadas o un wrapper `core_short`. Decisión en plan de implementación; preferencia: wrapper dedicado `TradingCoreShortOkx` para aislar la lógica de derivados del spot.

**Estado en DB:** nuevas tablas `short_positions` (posiciones abiertas: symbol, side, entry_px, qty, lever, sl_order_id, open_ts), `short_realized_pnl` (cerradas), `short_cooldown_state`. Prefijo `short_` consistente con convención de módulos.

## 4. Riesgo y salidas — envelope CONSERVADOR (decisión del usuario)

| Parámetro | Valor | Razón |
|---|---|---|
| Máx equity en la pata | **18%** (`SHORT_MAX_EQUITY_PCT=0.18`) | resto sigue en earn |
| Leverage | **3x** (`SHORT_LEVERAGE=3`) | muy por debajo de los líderes (20x) |
| Margen | **ISOLATED** | capa la pérdida por posición; no arrastra la cuenta |
| Stop-loss | **~4% server-side** (`SHORT_SL_PCT=0.04`) | algo-order OKX adjunta al entry → persiste si el bot/VPS muere |
| Take-profit | tiers +2% / +4% (parcial, estilo harvest del Grid) | asegura ganancia en el rally que revierte |
| Posiciones simultáneas | máx 2 (1 por símbolo) | |
| Circuit breaker | cap de pérdida diaria + KILL → cierra todo a mercado | |

**Piso de seguridad (invariante del diseño):** con isolated + stop server-side funcionando, **el peor caso de toda la pata queda capado en el margen asignado (~$800 al equity actual)**, aunque el VPS explote. Si alguna de las dos NO funciona, el invariante se rompe → de ahí el canary (§6).

**Salidas:**
1. Stop-loss server-side (−4%) — siempre colgado en OKX.
2. TP tiers (cubre parcial en fuerza).
3. **Flip de régimen:** BAJISTA → ALCISTA ⇒ cierra todos los shorts a mercado.
4. KILL switch ⇒ cierra todo a mercado, cancela algo-orders.

## 5. Lógica de entrada — "shortear rallies en bajista"

Simétrica al Grid spot (que compra dips: `ADX<20 and close>EMA200` en alcista). Para `sym in [BTC-USDT-SWAP, ETH-USDT-SWAP]`:

```
GATE:   régimen == BAJISTA            (regime_lock)
        no is_panico_global()
        no is_vol_spike("okx", sym)
        no hay short abierto en sym    (short_positions)
        cooldown transcurrido          (short_cooldown_state)

SEÑAL (rally que se agota dentro de downtrend):
        close < EMA200_15m             (downtrend intacto)
        ADX_14 > 20                    (hay tendencia, no chop)
        RSI_15m > 50                   (rebote local hacia resistencia)
        MACD histograma girando a la baja  (momentum del rebote agotándose)

→ abrir SHORT maker (limit ligeramente por encima del precio, vender en fuerza)
   con SL adjunto a −4% (attachAlgoOrds / params stopLoss de ccxt)
   sizing = min(SHORT_MAX_EQUITY_PCT*equity disponible) a 3x, modulado por ML opcional
```

Umbrales exactos (RSI, tiers, cooldown) son **tunables**; se fijan valores iniciales en el plan y se ajustan tras observar el canary.

**Instrumentos:** SOLO `BTC-USDT-SWAP` y `ETH-USDT-SWAP`. Nada de alts (el cerebro de régimen solo sigue BTC/ETH).

## 6. Operación REAL + gate de canary (decisión del usuario)

El hilo vivo **opera dinero real desde el arranque** (no hay modo paper en producción). El modo fake existe ÚNICAMENTE como harness de tests.

**Camino a real (misma sesión de build, no es esperar):**
1. Construir módulo (opera real).
2. Tests contra `FakeOkxFut` (no disparan órdenes reales).
3. **CANARY:** 1 short real al tamaño MÍNIMO de OKX. Verificación manual obligatoria:
   - posición quedó en **ISOLATED** (no cross),
   - el **stop-loss aparece colgado** en OKX (algo-order activa),
   - `set_leverage(3)` aplicó.
4. Canary OK ⇒ habilitar full envelope conservador, real, 24/7.

Flag de arranque: `SHORT_CANARY_MODE=true` (default) fuerza tamaño mínimo + 1 posición hasta verificación; se pasa a `false` para operación plena.

## 7. Configuración (.env)

Re-agregar (mínimo):
```
OKX_FUT_ENABLED=true
SHORT_MAX_EQUITY_PCT=0.18
SHORT_LEVERAGE=3
SHORT_SL_PCT=0.04
SHORT_TP_TIERS=2,4
SHORT_MAX_POSICIONES=2
SHORT_DAILY_LOSS_CAP_USD=60   # ~2 stop-outs sobre el pata de ~$800; tunable con el capital
SHORT_CANARY_MODE=true
```
Keys: las mismas OKX existentes (requieren permiso de **Trade** sobre derivados — verificar en el canary). Cuenta OKX debe estar en modo de margen que habilite swaps (Single-currency margin o superior). `validar_config` chequea presencia y rangos al boot.

## 8. Testing (TDD, como liquidar.py)

`FakeOkxFut`: exchange de derivados en memoria que replica estructura ccxt swap (fetch_positions, create_order con attachAlgoOrds, set_leverage, fetch_balance). Tests:
- entrada dispara solo en BAJISTA + señal completa;
- entrada NO dispara en ALCISTA / panic / vol-spike / con short existente / en cooldown;
- el SL se adjunta a −4% en isolated;
- flip de régimen cierra shorts;
- KILL cierra todo y cancela algos;
- circuit breaker corta tras cap diario;
- sizing respeta SHORT_MAX_EQUITY_PCT;
- canary mode fuerza tamaño mínimo / 1 posición.
Real money NUNCA se toca en tests.

## 9. Documentación

- `CLAUDE.md`: nueva sección "Pata Bajista V1.7.0"; revierte el "no reintroducir Futuros" con el racional disciplinado; bump a V1.7.0; actualiza tabla de alcance y conteo de hilos (12 → 13).
- `bot.txt`: documentar `EscuadronShortOkx`, esquema `short_*`, config, flujo de canary (regla operacional: cambio significativo ⇒ bot.txt en el mismo turno).
- Memoria del proyecto: actualizar con la reapertura de futuros y su racional.

## 10. Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Stop no se coloca → pérdida ilimitada | Canary verifica; entry y SL en la misma llamada (attachAlgoOrds); si SL falla, abortar/cerrar entry |
| Margen queda en cross → arrastra cuenta | Canary verifica isolated; `set_leverage` con `mgnMode=isolated` antes de operar |
| VPS se apaga con short abierto | SL server-side persiste; no scalping (holds de swing toleran gaps) |
| Bug de sizing → posición sobredimensionada | Tests de sizing; canary a tamaño mínimo; cap duro SHORT_MAX_EQUITY_PCT |
| Régimen oscila (whipsaw) | cooldown; ADX>20 gate evita chop; flip-exit a mercado |
| Escala chica ($800 hoy) | aceptado por el usuario; estrategia correcta que escala con capital |
| Dos instancias / conflicto con spot | mismo proceso, hilo daemon; reusa kill_switch y locks |

## 11. Fuera de alcance (YAGNI)

- Alts (solo BTC/ETH).
- Mirror/copy de líderes (estrategia original, no replicación).
- Pata long apalancada en alcista (el Grid spot ya cubre alcista).
- Re-introducir ARB (sigue descartado: no copiable, capacidad limitada).
- Cross-exchange.
