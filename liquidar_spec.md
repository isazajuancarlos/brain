# Spec — `liquidar.py` (liquidación total OKX → USDT)

- **Fecha:** 2026-06-05
- **Estado:** Aprobado (diseño), pendiente de plan de implementación
- **Sub-proyecto:** 1 de 3 (los otros: 2 = eliminar Binance/Futuros/ARB de `main.py`; 3 = verificación local). Este spec cubre SOLO el sub-proyecto 1.
- **Autor:** sesión de brainstorming 2026-06-05

## 1. Objetivo

Script standalone, de una sola corrida, que deja **toda la cuenta OKX en USDT**: rescata la cripto parqueada en Simple Earn, cancela órdenes abiertas, vende a market todo lo no-USDT, y limpia el estado de la DB de los activos liquidados. Análogo en forma a `entrenar.py` / `test_fut_keys.py` (auxiliar en la raíz, ejecución manual, NO parte del proceso del bot).

**Motivación:** reset a un estado limpio de solo-USDT antes de simplificar el bot a OKX spot + earn puro (sub-proyectos 2 y 3).

## 2. Alcance

**Dentro:**
- Solo **OKX**. Cuenta única (la configurada en `.env` como `OKX_*`).
- Liquida: ETH + alts (ADA, AVAX, LINK, SOL, SUI, UNI) y cualquier otro no-USDT que aparezca en el balance real o en Simple Earn.
- Cripto en Simple Earn → rescatada y vendida.

**Fuera:**
- **Binance**: se ignora por completo (ya está en USDT).
- **USDT en OKX Simple Earn**: NO se toca (queda rindiendo — decisión (a) del usuario).
- Cambios a `main.py` (eso es el sub-proyecto 2).

## 3. Precondiciones

- **El bot (`main.py`) debe estar APAGADO.** Dos instancias contra la misma cuenta = órdenes duplicadas / reconcile en conflicto. El script lo advierte explícitamente al arrancar.
- `.env` con `OKX_API_KEY` / `OKX_API_SECRET` / `OKX_PASSWORD` válidas (mismas que usa `TradingCoreOkx`).
- `quantum.db` accesible en el CWD (para la limpieza de DB).

## 4. Interfaz CLI

```
python liquidar.py                 # DRY-RUN (default): imprime el plan, NO toca nada
python liquidar.py --ejecutar      # ejecuta de verdad; pide confirmación tipeada "SI"
python liquidar.py --convert-dust  # además, convierte el polvo sub-mínimo vía OKX small-balance convert
python liquidar.py --no-clean-db   # ejecuta pero NO limpia la DB (default: SÍ limpia)
```

- **Default = dry-run.** Sin `--ejecutar` no se coloca ninguna orden, redención ni transfer.
- Con `--ejecutar`: imprime el plan, pide escribir `SI` por stdin, y solo entonces procede.
- Cada acción real se loguea a `logs/` (nuevo `logs/liquidar.log`) y a `caja_negra` de la DB.

## 5. Flujo detallado

Todo paso real está gateado por `--ejecutar`; en dry-run solo se imprime lo que haría.

1. **Init**: carga `.env`, construye cliente `ccxt.okx` (spot/trading), `load_markets()`. Verifica conectividad y permisos (lectura de balance).
2. **Snapshot inicial**: lee balance Trading + Funding + posiciones de Simple Earn (`private_get_finance_savings_balance` sin filtro de ccy → todas). Arma la lista de activos no-USDT a liquidar. Imprime tabla: ccy, qty en Trading, qty en Funding, qty en Earn, precio, valor USD estimado.
3. **Cancelar órdenes abiertas**: `fetch_open_orders()` en OKX spot y cancelar todas (las limit del Grid). Evita re-fills durante la liquidación.
4. **Rescatar cripto de Earn** (por cada ccy no-USDT con balance en savings):
   - `purchase-redempt` side=`redempt`, ccy, amt (redención flexible).
   - Poll del Funding balance del ccy hasta settlement (timeout 60s, intervalo 1.5s).
   - `asset/transfer` Funding(6) → Trading(18) por la qty rescatada.
   - Patrón idéntico a `GestorInventario._redempt`/`_transfer`/`_funding_balance_ccy`.
5. **Re-leer balances Trading** tras settlement de todos los rescates.
6. **Vender a market** cada activo no-USDT:
   - `sym = f"{ccy}/USDT"`. Redondear qty con `amount_to_precision(sym, qty)`.
   - Validar contra `minSz` / `minNotional` del market. Si pasa → `create_market_sell_order(sym, qty)`.
   - Si NO pasa el mínimo → marcar como **polvo** (paso 7).
7. **Polvo** (saldo no vendible en el libro):
   - Por defecto: **reportar** (no forzar nada).
   - Con `--convert-dust`: usar la conversión de saldos chicos de OKX (small-balance convert) para barrerlo a un activo soportado / USDT.
8. **Limpieza de DB** (default ON, salvo `--no-clean-db`):
   - `DELETE` de `okx_portfolio` para los símbolos liquidados.
   - `DELETE` de `okx_inventory_earn` para las monedas rescatadas.
   - `DELETE` del estado por-símbolo de los liquidados: `okx_tp_state`, `okx_dca_state`, `okx_cooldown_state`, `okx_grid_orders`, `okx_pending_sells`.
   - NO toca filas/registros de USDT ni el resto de la DB.
9. **Reporte final**: tabla con holdings iniciales → rescatado → vendido → polvo residual → USDT final en Trading. Resumen de fees/slippage observados (de las respuestas de las órdenes).

## 6. Endpoints / helpers OKX (reutilizar patrones de `main.py`)

| Acción | Método ccxt OKX | Referencia en `main.py` |
|---|---|---|
| Balance Earn (savings) | `private_get_finance_savings_balance` | `GestorTesoreria._earn_balance` (4312) |
| Balance Funding por ccy | `private_get_asset_balances` | `_funding_balance_ccy` (4583) |
| Redención Earn | `private_post_finance_savings_purchase_redempt` (side=`redempt`) | `GestorInventario._redempt` (4618) |
| Transfer entre cuentas | `private_post_asset_transfer` (`from`/`to`, type=`0`) | `_transfer` (4593) |
| Venta market | `create_market_sell_order` + `amount_to_precision` | OKX core (2699) |
| Órdenes abiertas | `fetch_open_orders` / `cancel_order` | — |

Constantes: `OKX_CUENTA_FUNDING="6"`, `OKX_CUENTA_TRADING="18"` (main.py:320-321).

## 7. Casos borde y manejo de errores

- **Earn ya vacío / sin posiciones**: salta el rescate, sigue con venta de lo que haya en Trading.
- **Settlement no llega en 60s**: loguea warning, NO vende esa moneda en esta corrida (queda para re-correr). El script es **re-ejecutable**: una segunda corrida retoma lo que quedó.
- **qty bajo mínimo tras redondeo**: → polvo (paso 7).
- **Orden rechazada** (ej. mercado cerrado, ccy no soportado): loguea y continúa con las demás; reporta los fallos al final.
- **Idempotencia**: correr el script dos veces es seguro; siempre opera sobre el balance/earn real actual.
- **USDT**: nunca se vende ni se redime de Earn. Filtro explícito `ccy != "USDT"`.

## 8. Lo que el script NO hace (límites claros)

- No toca Binance.
- No redime ni mueve el USDT de OKX Earn.
- No modifica `main.py` ni la config del bot.
- No reintroduce ni elimina futuros/ARB (sub-proyecto 2).

## 9. Verificación

1. **Dry-run primero**: correr sin flags, revisar que el plan liste exactamente los 7 activos esperados (ETH, ADA, AVAX, LINK, SOL, SUI, UNI) con valores razonables.
2. **Ejecutar**: `--ejecutar`, confirmar `SI`.
3. **Post-check**: el balance OKX Trading queda solo con USDT (+ polvo residual reportado); `okx_portfolio` y `okx_inventory_earn` vacíos (salvo USDT); `fetch_open_orders` vacío.
4. **Re-correr en dry-run**: debe reportar "nada que liquidar".

## 10. Fuera de alcance (próximos sub-proyectos)

- **Sub-proyecto 2** — eliminar de `main.py`: todo Binance (`TradingCoreBin`, feeds/motores/gestores BIN, `refrescar_earn_usdt_bin`, config BIN), todo Futuros (helpers FUT del core, `SensorPosicionesFut`, `CircuitBreakerFut`, `_tabla_fut`, config FUT, `test_fut_keys.py`), todo ARB (`FundingScanner`, `ArbExecutor`, `WalletBridge`, tablas/config arb). Resultado: bot OKX spot + earn puro.
- **Sub-proyecto 3** — verificar adecuación a operación local-only (deps ya instaladas), actualizar `CLAUDE.md`/`bot.txt`.
