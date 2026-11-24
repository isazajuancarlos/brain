"""Colector de MICROFLUJO de OKX (Etapa 1 del experimento del 'santo grial' — ver lab_features_spec.md
tanda 3 y la memoria lab-features).

Daemon standalone que graba la microestructura PÚBLICA de OKX (todo el mercado, no nuestros trades) a
una DB propia `microflujo.db`, agregada a barras de 1 minuto. Mide hacia adelante (forward) la única
señal no-OHLCV que nos queda: CVD (flujo agresivo de TODO el mercado) + OBI + OI + funding + liquidaciones.

AISLAMIENTO (no negociable):
- DB PROPIA (microflujo.db), NUNCA quantum.db → cero riesgo de racear al bot vivo.
- Solo lectura/grabado: canales PÚBLICOS del ws (sin API keys, sin auth), CERO órdenes, CERO capital.
- Proceso separado: si se cae, el bot ni se entera; si el bot se cae, esto sigue.
- Tolerante a huecos: reconecta y sigue; un gap no rompe nada.

Uso:
    python colector_microflujo.py                 # corre el colector (BTC/ETH SWAP) hasta Ctrl+C
    python colector_microflujo.py --pares BTC-USDT-SWAP
    python colector_microflujo.py --resumen       # imprime filas/rango de microflujo.db y corta

NO toca el bot, quantum.db, .env ni capital. El análisis posterior será `lab.py --microflujo`.
"""
import sqlite3
import time

DB_MICRO = "microflujo.db"
PARES_DEFAULT = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]


# =============================================================================
# NÚCLEO PURO (testeable, sin red)
# =============================================================================
def obi(bids, asks):
    """Order Book Imbalance en [-1, 1]: (vol_bids − vol_asks)/(vol_bids + vol_asks).
    bids/asks = listas de [precio, size]. Vacío → 0.0."""
    bv = sum(s for _, s in bids)
    av = sum(s for _, s in asks)
    tot = bv + av
    return (bv - av) / tot if tot > 0 else 0.0


class AgregadorMinuto:
    """Acumula eventos de microflujo en una barra de 1 minuto y la emite al cruzar el minuto.
    Eventos (dicts con 'tipo' y 'ts' en ms):
      trade  {side: buy|sell, px, sz}   → OHLC, vol, cvd_delta (firmado), n_buys/n_sells
      book   {bids, asks}               → OBI (promedio del minuto + último)
      oi     {oi}                       → último OI del minuto
      funding{rate}                     → último funding del minuto
      liq    {notional}                 → conteo + notional acumulado de liquidaciones
    procesar(ev) devuelve la barra cerrada (dict) si el evento abrió un minuto nuevo, si no None."""

    def __init__(self, symbol):
        self.symbol = symbol
        self.min_actual = None
        self.cvd_cum = 0.0
        self._reset_acum()

    def _reset_acum(self):
        self.open = self.high = self.low = self.close = None
        self.vol = 0.0
        self.cvd_delta = 0.0
        self.n_buys = self.n_sells = 0
        self.obi_sum = 0.0
        self.obi_n = 0
        self.obi_last = None
        self.oi = None
        self.funding = None
        self.n_liqs = 0
        self.liq_notional = 0.0

    @staticmethod
    def _min(ts_ms):
        return ts_ms // 60000

    def procesar(self, ev):
        m = self._min(ev["ts"])
        cerrada = None
        if self.min_actual is None:
            self.min_actual = m
        elif m > self.min_actual:
            cerrada = self.cerrar()
            self.min_actual = m
            self._reset_acum()
        elif m < self.min_actual:
            return None                      # evento de un minuto pasado → descartar
        self._aplicar(ev)
        return cerrada

    def _aplicar(self, ev):
        t = ev["tipo"]
        if t == "trade":
            px, sz = float(ev["px"]), float(ev["sz"])
            if self.open is None:
                self.open = self.high = self.low = px
            self.high = max(self.high, px)
            self.low = min(self.low, px)
            self.close = px
            self.vol += sz
            if ev["side"] == "buy":
                self.cvd_delta += sz
                self.n_buys += 1
            else:
                self.cvd_delta -= sz
                self.n_sells += 1
        elif t == "book":
            o = obi(ev["bids"], ev["asks"])
            self.obi_sum += o
            self.obi_n += 1
            self.obi_last = o
        elif t == "oi":
            self.oi = float(ev["oi"])
        elif t == "funding":
            self.funding = float(ev["rate"])
        elif t == "liq":
            self.n_liqs += 1
            self.liq_notional += float(ev["notional"])

    def cerrar(self):
        """Cierra la barra del minuto actual y devuelve el dict. Acumula el CVD corrido."""
        self.cvd_cum += self.cvd_delta
        return {
            "ts_min": self.min_actual, "symbol": self.symbol,
            "open": self.open, "high": self.high, "low": self.low, "close": self.close,
            "vol": round(self.vol, 8), "cvd_delta": round(self.cvd_delta, 8),
            "cvd_cum": round(self.cvd_cum, 8), "n_buys": self.n_buys, "n_sells": self.n_sells,
            "obi_avg": (self.obi_sum / self.obi_n) if self.obi_n else None,
            "obi_last": self.obi_last, "oi": self.oi, "funding": self.funding,
            "n_liqs": self.n_liqs, "liq_notional": round(self.liq_notional, 2),
        }


# =============================================================================
# PERSISTENCIA (SQLite propia, WAL)
# =============================================================================
def _conn(db=DB_MICRO):
    c = sqlite3.connect(db, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS flujo_1m (
        ts_min INTEGER, symbol TEXT, open REAL, high REAL, low REAL, close REAL,
        vol REAL, cvd_delta REAL, cvd_cum REAL, n_buys INTEGER, n_sells INTEGER,
        obi_avg REAL, obi_last REAL, oi REAL, funding REAL, n_liqs INTEGER, liq_notional REAL,
        PRIMARY KEY (ts_min, symbol))""")
    return c


def guardar_barra(conn, b):
    conn.execute("""INSERT OR REPLACE INTO flujo_1m VALUES
        (:ts_min,:symbol,:open,:high,:low,:close,:vol,:cvd_delta,:cvd_cum,:n_buys,:n_sells,
         :obi_avg,:obi_last,:oi,:funding,:n_liqs,:liq_notional)""", b)
    conn.commit()


def resumen(db=DB_MICRO):
    c = _conn(db)
    for row in c.execute("""SELECT symbol, COUNT(*) n, MIN(ts_min) a, MAX(ts_min) b
                            FROM flujo_1m GROUP BY symbol"""):
        sym, n, a, bb = row
        if a is None:
            print(f"  {sym}: vacío"); continue
        dias = (bb - a) / 1440.0
        ta = time.strftime("%Y-%m-%d %H:%M", time.gmtime(a * 60))
        tb = time.strftime("%Y-%m-%d %H:%M", time.gmtime(bb * 60))
        print(f"  {sym}: {n} barras 1m, {ta}..{tb} UTC = {dias:.1f}d cubiertos")
    c.close()


# =============================================================================
# CAPA DE RED (ccxt.pro, async) — NO unit-testeada (integración)
# =============================================================================
async def _correr(pares):
    import asyncio
    import ccxt.pro as ccxtpro

    ex = ccxtpro.okx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    conn = _conn()
    aggs = {p: AgregadorMinuto(p) for p in pares}
    ccxt_sym = {p: p.replace("-", "/").replace("/USDT/SWAP", "/USDT:USDT") for p in pares}
    print(f"▤ Colector microflujo ACTIVO — {', '.join(pares)} → {DB_MICRO} (público, sin capital)")

    def manejar(p, ev):
        b = aggs[p].procesar(ev)
        if b is not None:
            guardar_barra(conn, b)

    async def loop_trades(p):
        sym = ccxt_sym[p]
        while True:
            try:
                trades = await ex.watch_trades(sym)
                for t in trades:
                    manejar(p, {"tipo": "trade", "ts": t["timestamp"], "side": t["side"],
                                "px": t["price"], "sz": t["amount"]})
            except Exception as e:
                print(f"  trades {p}: {type(e).__name__} → reintento"); await asyncio.sleep(2)

    async def loop_book(p):
        sym = ccxt_sym[p]
        while True:
            try:
                ob = await ex.watch_order_book(sym, limit=5)
                bids = [[float(x[0]), float(x[1])] for x in ob["bids"][:5]]
                asks = [[float(x[0]), float(x[1])] for x in ob["asks"][:5]]
                ts = ob.get("timestamp") or int(time.time() * 1000)
                manejar(p, {"tipo": "book", "ts": ts, "bids": bids, "asks": asks})
            except Exception as e:
                print(f"  book {p}: {type(e).__name__} → reintento"); await asyncio.sleep(2)

    async def poll_oi_funding(p):
        # OI y funding cambian lento → REST cada 60s (más simple/robusto que ws dedicado)
        while True:
            try:
                sym = ccxt_sym[p]
                ts = int(time.time() * 1000)
                try:
                    oi = await ex.fetch_open_interest(sym)
                    val = oi.get("openInterestValue") or oi.get("openInterestAmount")
                    if val:
                        manejar(p, {"tipo": "oi", "ts": ts, "oi": val})
                except Exception:
                    pass
                try:
                    fr = await ex.fetch_funding_rate(sym)
                    if fr.get("fundingRate") is not None:
                        manejar(p, {"tipo": "funding", "ts": ts, "rate": fr["fundingRate"]})
                except Exception:
                    pass
            except Exception as e:
                print(f"  oi/funding {p}: {type(e).__name__}")
            await asyncio.sleep(60)

    tareas = []
    for p in pares:
        tareas += [loop_trades(p), loop_book(p), poll_oi_funding(p)]
    try:
        await asyncio.gather(*tareas)
    finally:
        # flush de las barras en curso al salir (Ctrl+C)
        for p, ag in aggs.items():
            if ag.min_actual is not None and ag.close is not None:
                guardar_barra(conn, ag.cerrar())
        await ex.close()
        conn.close()
        print("▤ Colector detenido — barras en curso volcadas.")


def main():
    import argparse
    import asyncio
    p = argparse.ArgumentParser(description="Colector de microflujo OKX (público, no toca bot/capital)")
    p.add_argument("--pares", type=str, default=None, help="CSV ej. BTC-USDT-SWAP,ETH-USDT-SWAP")
    p.add_argument("--resumen", action="store_true", help="imprime estado de microflujo.db y corta")
    args = p.parse_args()
    if args.resumen:
        resumen(); return
    pares = [x.strip() for x in args.pares.split(",")] if args.pares else PARES_DEFAULT
    try:
        asyncio.run(_correr(pares))
    except KeyboardInterrupt:
        print("\n▤ Ctrl+C — cerrando colector.")


if __name__ == "__main__":
    main()
