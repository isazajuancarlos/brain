# Runbook — Colector de Microflujo (Etapa 1 del "santo grial")

Daemon standalone que graba la microestructura **pública** de OKX (todo el mercado, NO nuestros trades)
a `microflujo.db`, agregada a barras de 1 minuto. Captura la única señal no-OHLCV que nos queda: **CVD**
(flujo agresivo market-wide) + OBI + OI + funding + liquidaciones. Es **forward-collection** = la prueba
más limpia posible (cero lookahead, cero sobreajuste).

## Aislamiento (no negociable)
- DB **propia** `microflujo.db` — NUNCA `quantum.db`. No racea al bot.
- Canales **públicos** del ws (sin API keys, sin auth). **Cero órdenes, cero capital.**
- Proceso **separado** del bot. Si se cae, el bot ni se entera; si el bot se cae, esto sigue.
- Tolerante a huecos: reconecta y sigue (ccxt.pro maneja ping/pong + reconexión).

## 0. Requisitos
- `ccxt` ≥ 4.5 (incluye `ccxt.pro`) — ya instalado, sin dep nueva.
- No necesita `.env` ni claves (todo público).

## 1. Arrancar (persistente — debe sobrevivir semanas)
El colector corre hasta Ctrl+C; para juntar datos necesita correr **semanas**. Lanzarlo **persistente**
(que sobreviva al cierre de la terminal):

```bash
cd /mnt/data/brain
nohup python -u colector_microflujo.py > logs/microflujo.log 2>&1 &
echo $! > microflujo.pid          # guarda el PID para pararlo después
```

Banner esperado: `▤ Colector microflujo ACTIVO — BTC-USDT-SWAP, ETH-USDT-SWAP → microflujo.db (público, sin capital)`

- Solo BTC/ETH SWAP por default (los líquidos). Otros: `--pares BTC-USDT-SWAP,SOL-USDT-SWAP`.

## 2. Verificar que junta datos
```bash
python colector_microflujo.py --resumen     # filas por símbolo + rango temporal cubierto
tail -f logs/microflujo.log                  # reconexiones / errores
```
Se escribe **1 fila por minuto por símbolo** (1440/día/símbolo). Pocos MB/semana.

## 3. Parar
```bash
kill $(cat microflujo.pid)        # vuelca las barras en curso y cierra limpio
```
En Ctrl+C / kill las barras del minuto en curso se **flushean** antes de salir.

## 4. Cómo medir (Etapa 3 — el analizador YA está construido)
El analizador `lab.py --microflujo` **ya existe** (TDD, 8 tests en `test_lab.py`); solo espera datos.
Cuando haya **muestra suficiente** (idealmente ≥4–8 semanas, capturando al menos una transición de
régimen RANGO↔tendencia) se corre **en tu máquina** (no en la VPS):

```bash
# traé la DB de la VPS primero (ver sección 6), luego:
python lab.py --microflujo                          # auto-descubre símbolos de microflujo.db, k=15m
python lab.py --microflujo --mf-k 60                 # horizonte forward 60m
python lab.py --microflujo --mf-db ./microflujo.db --pares BTC-USDT-SWAP
```
- señales: divergencia **CVD** (`cvd_divergence`: precio↑ + CVD↓ = debilidad → −1; precio↓ + CVD↑ =
  dip absorbido → +1) y **OBI** (`obi_signal`: book imbalance direccional).
- test: IC pareado señal·forward a `k` minutos, **con ventanas NO solapadas** (`_deltas_microflujo`
  muestrea a paso k → δ independientes; la lección de la tanda 3: los windows solapados inflan el LCB),
  + LCB apareado + robustez por mitades + chequeo fee (`veredicto`, el mismo rigor del banco).
- gate: IC > fees (~0.16%), robusto, significativo en muestras **independientes**.
- pregunta: ¿CVD/OBI predicen el forward **más allá del precio**? (la pata no-OHLCV que faltaba)
- guard: con muestra fina imprime "muestra fina" y devuelve `[]` (no crashea, no veredicto falso).

## 5. Si pasa / si no
- **Verde** → no un bot predictivo nuevo (el timing muere) — sino **alimentar el Cerebro de Régimen
  4-estados** con un input de microestructura → grid/ladder operan/congelan más afilado. Monetiza sin
  romper el envelope.
- **Rojo** → se mata, documentado (linaje medir-y-descartar). El colector queda reusable.

## 6. Deploy 24/7 en VPS (systemd — recomendado sobre nohup)
El colector necesita correr **semanas sin huecos**, así que la PC que se apaga de noche no sirve
(huecos + sesgo de horario). En una VPS ultraeconómica (1 vCPU / 512MB–1GB basta; la ubicación
es **irrelevante** porque agregamos a 1m, no es HFT) se corre como servicio systemd: arranca solo
al boot y se revive si se cae.

**El colector NO necesita `.env`, claves, `quantum.db` ni capital** — solo el archivo + `ccxt`.

```bash
# --- En la VPS (Ubuntu/Debian fresco, como root/sudo) ---
adduser --system --group --home /opt/microflujo microflujo
apt update && apt install -y python3 python3-venv

# Subir SOLO estos 2 archivos desde tu máquina (NADA de secrets):
#   scp colector_microflujo.py colector-microflujo.service usuario@VPS:/tmp/
install -o microflujo -g microflujo /tmp/colector_microflujo.py /opt/microflujo/

# venv + única dependencia
cd /opt/microflujo
sudo -u microflujo python3 -m venv venv
sudo -u microflujo venv/bin/pip install --upgrade pip ccxt

# Instalar y arrancar el servicio
cp /tmp/colector-microflujo.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now colector-microflujo
systemctl status colector-microflujo          # debe decir "active (running)"
```

Verificación y operación:
```bash
journalctl -u colector-microflujo -f                              # logs en vivo (banner + reconexiones)
sudo -u microflujo /opt/microflujo/venv/bin/python \
     /opt/microflujo/colector_microflujo.py --resumen             # filas/rango cubierto
systemctl stop colector-microflujo                                # apaga LIMPIO (flushea barra en curso)
```

Detalles de la unidad (`colector-microflujo.service`):
- `Restart=always` + `RestartSec=10` → se revive solo ante caída de red/ws/OOM. Un restart pierde a lo
  sumo la barra de 1m en curso por símbolo (el colector tolera huecos: ccxt.pro reconecta).
- `KillSignal=SIGINT` → el `stop` manda SIGINT (no SIGTERM) para gatillar el `finally` que vuelca las
  barras en curso, igual que Ctrl+C. `TimeoutStopSec=20` le da margen al flush.
- Endurecido (`ProtectSystem=strict`, `ProtectHome`, `ReadWritePaths=/opt/microflujo`): proceso público
  sin auth que solo escribe su propia DB — no puede tocar el resto del FS.
- Si cambiás el dir de `/opt/microflujo`, ajustá `WorkingDirectory`, `ExecStart` y `ReadWritePaths` en
  el `.service` (y `User`/`Group` si usás otro usuario).

**Recordá traer la DB para medir** (Etapa 3 corre en tu máquina con `lab.py --microflujo`):
```bash
scp usuario@VPS:/opt/microflujo/microflujo.db ./microflujo.db   # con el servicio detenido o WAL-checkpointed
```

## Notas
- El esquema `flujo_1m`: ts_min, symbol, OHLC, vol, cvd_delta, cvd_cum, n_buys, n_sells, obi_avg,
  obi_last, oi, funding, n_liqs, liq_notional. PK (ts_min, symbol) → reinicios no duplican.
- `microflujo.db` es **gitignored** (como `quantum.db`).
- Núcleo puro (`obi`, `AgregadorMinuto`) testeado en `test_colector_microflujo.py` (10, sin red).
- Diseño/plan: conversación de la sesión + memoria [[lab-features]] (tanda 3).
