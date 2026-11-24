# Canary — primer despliegue real de la Pata Bajista (V1.7.0)

El canary es la verificación **con plata real, a tamaño mínimo**, de que el envelope
conservador funciona de verdad en OKX. Su único objetivo: confirmar **isolated + stop
server-side** antes de soltar el bot a operar con el sizing pleno.

> **Invariante que protege:** con isolated + SL colgado, el peor caso de toda la pata
> queda capado en el margen asignado (~$800 al equity actual), aunque el VPS explote.
> Si alguna de las dos NO funciona, el invariante se rompe. Por eso el canary es obligatorio.

---

## Paso 0 — Verificar el lote mínimo real (antes de arrancar)

`SHORT_QTY_MINIMA` en `main.py` es el tamaño del canary (en la unidad de `amount` que
usa ccxt para el swap OKX — contratos). Confirmá el mínimo real y ajustalo si hace falta:

```bash
python -c "
import ccxt, os
ex = ccxt.okx({'apiKey':os.getenv('OKX_API_KEY'),'secret':os.getenv('OKX_API_SECRET'),
               'password':os.getenv('OKX_PASSPHRASE'),'options':{'defaultType':'swap'}})
ex.load_markets()
for s in ['BTC/USDT:USDT','ETH/USDT:USDT']:
    m = ex.market(s)
    print(s, 'min amount:', m['limits']['amount']['min'], '| ctVal:', m.get('contractSize'))
"
```

Si el mínimo es mayor que `SHORT_QTY_MINIMA`, actualizá la constante en `main.py`.

## Paso 1 — Config

En `.env`:
```
OKX_FUT_ENABLED=true
SHORT_CANARY_MODE=true     # fuerza lote mínimo + máx 1 posición
SHORT_LEVERAGE=3
SHORT_SL_PCT=0.04
SHORT_MAX_EQUITY_PCT=0.18
```

Confirmá en OKX que la API key tiene permiso **Trade** sobre derivados y que la cuenta
está en un modo de margen que habilite swaps (Single-currency margin o superior).

## Paso 2 — Arrancar limpio

```bash
# Si el bot está corriendo, paralo y reiniciá limpio (mismo proceso, hilo daemon nuevo)
python main.py
```

Verás en el arranque: `⚡ Pata bajista ACTIVA — futuros 3x isolated, SL 4% [CANARY: ...]`.
Esperá a que el régimen sea **BAJISTA** y la señal dispare 1 short mínimo (puede tardar:
necesita ADX>20 + RSI>50 + MACDh girando a negativo — la tendencia la da el régimen diario).

### Pre-requisito de cuenta (Finding #5 de la revisión): MODO DE POSICIÓN
La pata abre con `posSide="short"`, que requiere la cuenta de derivados en modo
**Long/Short (hedge)**. Si está en modo **Net**, OKX **rechaza** la orden. Verificá/cambiá
en OKX: Trade → Settings → Position mode → **Long/Short**. (Si preferís Net mode, hay que
ajustar `TradingCoreShortOkx` para no mandar `posSide`.)

## Paso 3 — Verificación manual en OKX (web/app) — LOS 4 OBLIGATORIOS

Cuando se abra la posición (verás `SHORT_OPEN` en la Caja Negra / panel PATA BAJISTA):

- [ ] **Margen ISOLATED** (no cross) en la posición BTC/ETH-USDT-SWAP.
- [ ] **Stop-loss colgado** — debe existir una algo-order / TP-SL activa a ~+4% del entry.
- [ ] **Leverage = 3x**.
- [ ] **TAMAÑO correcto** (Finding #4): OKX swap se ordena en **contratos** (BTC ctVal 0.01).
      Confirmá que el notional de la posición es el esperado (~`SHORT_QTY_MINIMA` × precio en
      el canary). Si la posición sale **~100x más chica/grande** de lo esperado, el `amount`
      está en la unidad equivocada → hay que convertir coin↔contratos en `abrir_short` antes
      de pasar a full. NO pases a full hasta que el tamaño sea el correcto.

## Paso 4 — Decisión

- **Los 3 OK** → en `.env`: `SHORT_CANARY_MODE=false`, reiniciar. La pata opera el
  envelope conservador pleno (18% equity, hasta 2 posiciones).
- **Algo falla** → `touch KILL` (cierra todo a mercado en <5s). Revisá `logs/errors.log`
  y `sys_caja_negra`. Corregí (permisos, modo de margen, params de la algo-order) y
  reintentá el canary. NO pases a full hasta que los 3 estén verdes.

## Notas

- En **Ctrl+C** (apagado ordenado) los shorts se **preservan** (el SL los cubre); se
  reconcilian al próximo arranque. Solo `touch KILL` los cierra a mercado.
- El circuit breaker corta nuevas entradas si la pérdida del día supera
  `SHORT_DAILY_LOSS_CAP_USD` (default $60).
