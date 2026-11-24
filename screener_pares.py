"""
=============================================================================
QUANTUM BOT — screener_pares.py
Detector de pares para el grid spot (V1.7.10+, etapa spot+grid+earn).

Escanea los pares USDT spot de OKX y los rankea por APTITUD PARA EL GRID:
  - correlación a BTC (diagnóstico: en cripto casi todos son ~0.8; baja corr = bonus)
  - volatilidad anual (potencial de cosecha)
  - liquidez (volumen 24h; gate de descarte)
  - retorno total + max drawdown (¿recupera o es falling knife?)
  - grid_pnl: backtest simple de la cosecha del grid (long-only, compra dips / vende +tp)
El grid gana por VOL + LIQUIDEZ + RECUPERACIÓN (no por decorrelación). El score lo refleja.

Standalone (como entrenar.py/liquidar.py). NO toca el bot ni la DB. Solo lee OHLCV público.

Uso:
    python screener_pares.py                  # top 25 pares OKX, 365d
    python screener_pares.py --top 40 --dias 180
    python screener_pares.py --min-liq 5      # liquidez mínima en M USD (default 1)
=============================================================================
"""
import argparse
import logging
import time

import ccxt
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("screener")

TIMEFRAME = "1d"   # diario: suficiente para corr/vol/trend/grid-proxy, rápido para muchos pares


# =============================================================================
# Métricas puras (testeables sin red)
# =============================================================================
def retornos(closes):
    """Retornos pct día a día. [100,110,99] -> [0.1, -0.1]."""
    return [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))
            if closes[i - 1]]


def corr_a_btc(rets, btc_rets):
    """Correlación de Pearson entre los retornos del par y los de BTC (alineados al tail común)."""
    n = min(len(rets), len(btc_rets))
    if n < 3:
        return 0.0
    a, b = np.array(rets[-n:]), np.array(btc_rets[-n:])
    if a.std() == 0 or b.std() == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def volatilidad_anual(rets):
    """Desvío estándar de los retornos diarios, anualizado (×√365). En %."""
    if len(rets) < 2:
        return 0.0
    return float(np.std(rets) * np.sqrt(365) * 100)


def max_drawdown(closes):
    """Peor caída desde un pico (negativo, en %). [100,120,60,90] -> -50."""
    if len(closes) < 2:
        return 0.0
    pico = closes[0]
    peor = 0.0
    for px in closes:
        pico = max(pico, px)
        if pico > 0:
            peor = min(peor, (px - pico) / pico)
    return round(peor * 100, 2)


def grid_sim(closes, dip=0.05, tp=0.05, sma=None):
    """Backtest SIMPLE de la cosecha del grid (long-only, proxy): compra un lote cada vez que el
    precio cae `dip` desde el pico de referencia; cosecha cada lote a +`tp`. Devuelve
    (realizado_pct, no_realizado_pct, n_harvests):
      - realizado = suma de los +tp cosechados.
      - no_realizado = mark-to-market del inventario que quedó SIN vender (negativo si underwater)
        → castiga al falling knife: un par que cosechó mucho pero quedó sentado en pérdida NO es bueno.
    `sma` (opcional) = gate de RÉGIMEN: solo compra si px > SMA(sma) → replica el grid real, que está
    CONGELADO en BAJISTA (no compra dips en bear). Sin esto el sim sobre-castiga a los majors. El NET
    (realizado + no_realizado) es la verdad. NO modela fees — proxy diario de oscilación cosechable."""
    inventario = []
    realizado = 0.0
    n = 0
    pico = closes[0] if closes else 0.0
    for i, px in enumerate(closes):
        pico = max(pico, px)
        regime_ok = True
        if sma:
            ventana = closes[max(0, i - sma):i]
            regime_ok = bool(ventana) and px > (sum(ventana) / len(ventana))
        if regime_ok and px <= pico * (1 - dip):
            inventario.append(px)
            pico = px            # nuevo piso de referencia para el próximo dip
        rest = []
        for compra in inventario:
            if px >= compra * (1 + tp):
                realizado += tp
                n += 1
            else:
                rest.append(compra)
        inventario = rest
    ultimo = closes[-1] if closes else 0.0
    no_realizado = sum((ultimo - c) / c for c in inventario if c)   # inventario trabado al final
    return round(realizado * 100, 2), round(no_realizado * 100, 2), n


def score_par(grid_pnl, liq, corr, min_liq=1_000_000.0):
    """Score de aptitud. `grid_pnl` = NET del grid (realizado + no_realizado). Iliquido
    (liq<min_liq) → −1 (descartado). Si no, NET (el motor honesto) + bonus chico por baja
    correlación a BTC (diversificación rara y valiosa)."""
    if liq < min_liq:
        return -1.0
    bonus_corr = max(0.0, 0.7 - abs(corr)) * 5.0   # corr<0.7 suma; ~0 suma 3.5
    return round(grid_pnl + bonus_corr, 2)


# =============================================================================
# Red (descubrimiento + mining) — glue, no unit-tested
# =============================================================================
def descubrir_pares(exchange, top_n=25, quote="USDT"):
    """Pares spot activos cotizados en `quote`, ordenados por volumen 24h (los más líquidos)."""
    mercados = exchange.load_markets()
    cands = [m["symbol"] for m in mercados.values()
             if m.get("spot") and m.get("active") and m.get("quote") == quote
             and m.get("base") != quote]
    try:
        tickers = exchange.fetch_tickers(cands)
    except Exception:
        tickers = {}
    def vol(s):
        t = tickers.get(s) or {}
        return float(t.get("quoteVolume") or 0)
    return sorted(cands, key=vol, reverse=True)[:top_n]


def _ohlcv(exchange, sym, dias):
    """Cierres + volumen-quote medio (USD). Devuelve (closes, liq) o (None, 0) si falla."""
    try:
        velas = exchange.fetch_ohlcv(sym, TIMEFRAME, limit=min(dias, 365))
        if not velas or len(velas) < 30:
            return None, 0.0
        closes = [float(v[4]) for v in velas]
        # volumen-quote ≈ volumen_base × precio (mediana, robusto a spikes)
        qv = np.median([float(v[5]) * float(v[4]) for v in velas])
        return closes, float(qv)
    except Exception as e:
        log.warning(f"  {sym}: {type(e).__name__}")
        return None, 0.0


def screener(dias=365, top_n=25, min_liq_m=1.0):
    min_liq = min_liq_m * 1_000_000
    ex = ccxt.okx({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    log.info(f"Descubriendo pares USDT spot de OKX (top {top_n} por volumen)...")
    pares = descubrir_pares(ex, top_n)
    btc_closes, _ = _ohlcv(ex, "BTC/USDT", dias)
    if not btc_closes:
        log.error("No se pudo bajar BTC/USDT — abortando.")
        return
    btc_rets = retornos(btc_closes)

    filas = []
    for sym in pares:
        closes, liq = _ohlcv(ex, sym, dias)
        if not closes:
            continue
        rets = retornos(closes)
        corr = corr_a_btc(rets, btc_rets)
        real, trabado, gn = grid_sim(closes, sma=50)   # sma=50: replica el grid frozen en BAJISTA
        net = round(real + trabado, 2)
        filas.append({
            "sym": sym, "corr": corr, "vol": volatilidad_anual(rets), "liq": liq,
            "ret": round((closes[-1] / closes[0] - 1) * 100, 1), "dd": max_drawdown(closes),
            "real": real, "trabado": trabado, "net": net, "gn": gn,
            "score": score_par(net, liq, corr, min_liq),
        })
        time.sleep(0.05)

    filas.sort(key=lambda r: r["score"], reverse=True)
    print(f"\n{'par':12}{'score':>7}{'NET%':>8}{'cosech%':>8}{'trabado%':>9}{'harv':>5}"
          f"{'corrBTC':>8}{'vol%':>6}{'maxDD%':>8}{'liq$M':>8}")
    print("-" * 87)
    for r in filas:
        marca = " ⛔" if r["score"] < 0 else ""
        print(f"{r['sym']:12}{r['score']:>7.1f}{r['net']:>8.1f}{r['real']:>8.1f}"
              f"{r['trabado']:>9.1f}{r['gn']:>5}{r['corr']:>8.2f}{r['vol']:>6.0f}"
              f"{r['dd']:>8.1f}{r['liq']/1e6:>8.1f}{marca}")
    print(f"\nLeyenda: NET% = cosech% (harvests) + trabado% (mark-to-market del inventario sin vender,")
    print(f"  NEGATIVO si quedó underwater → castiga al falling knife). score = NET + bonus_baja_corr.")
    print(f"  Iliquido <{min_liq_m}M USD → ⛔. El grid gana por VOL+LIQUIDEZ+RECUPERACIÓN, no por")
    print(f"  decorrelación (corrBTC ~0.8 = normal en cripto). Proxy diario sin fees/régimen.")


def main():
    p = argparse.ArgumentParser(description="Screener de pares para el grid spot (OKX)")
    p.add_argument("--top", type=int, default=25, help="cuántos pares (por volumen) escanear")
    p.add_argument("--dias", type=int, default=365, help="días de histórico diario")
    p.add_argument("--min-liq", type=float, default=1.0, help="liquidez mínima en M USD (gate)")
    args = p.parse_args()
    screener(dias=args.dias, top_n=args.top, min_liq_m=args.min_liq)


if __name__ == "__main__":
    main()
