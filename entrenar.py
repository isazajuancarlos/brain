"""
=============================================================================
QUANTUM UNIFIED BOT V1.5.0 — entrenar.py
Pipeline ML todo-en-uno para generar xgboost_oracle.joblib

Mining (OHLCV 15m × N pares × N días) + entrenamiento XGBoost en un solo script.
Las features y label son IDÉNTICAS a las que consume el bot vía ml_size_multiplier
(usado en EscuadronGridBin/Okx para modular sizing de Grid BUY).

Uso:
    python entrenar.py                       # mining 60d + train (ambos)
    python entrenar.py --solo-mining         # solo extrae features histórico
    python entrenar.py --solo-train          # solo entrena desde DB existente
    python entrenar.py --dias 30             # window custom
    python entrenar.py --pares BTC,ETH       # solo BTC/USDT y ETH/USDT
    python entrenar.py --db otra.db          # DB alternativa

Salida: xgboost_oracle.joblib en CWD (mismo directorio que main.py lee).
        main.py lo hot-reloadea automáticamente (mtime check, sin reiniciar bot).

Consistencia con main.py V1.5.0:
- Features: [rsi, adx, ema_dist, macd, vol_ratio] — mismo orden que ml_features_para
- Label:    1 si TP +1.2% antes que SL -2.0% en HORIZONTE_VELAS=24 (6h con 15m)
- DB:       quantum.db tabla ml_feature_log (schema 9 columnas V1.5.0)

V1.5.0 — el dataset es exclusivamente histórico (mining OHLCV). Las capturas
live del bot operando se descartaron por sesgo de selección (solo registran
entries que pasaron todos los gates → distribución no representativa),
volumen insuficiente (~80 samples/mes vs 170k del mining en una corrida),
y ruido operacional (depende del uptime del bot y del labeler).
Reentrenar periódicamente con `--dias 90` mantiene el modelo actualizado.
=============================================================================
"""

import os
import sys
import time
import sqlite3
import argparse
import logging
from datetime import datetime, timedelta

import ccxt
import pandas as pd
import pandas_ta as ta
import numpy as np
from dotenv import load_dotenv

load_dotenv()

# =============================================================================
# CONSTANTES — alineadas con MartilloEliteFut (main.py línea ~4045)
# =============================================================================
DB_NAME_DEFAULT  = os.getenv("DB_NAME", "quantum.db")
MODEL_PATH       = "xgboost_oracle.joblib"

OBJETIVO_PROFIT  = 0.012   # +1.2% (gatillo neto post-fees Binance 0.2% round-trip)
STOP_LOSS        = -0.020  # -2.0% respiración
HORIZONTE_VELAS  = 24      # 6h con 15m
TIMEFRAME        = "15m"

# Punto A: funding como 6ª feature (opt-in, DEBE coincidir con main.ml_feature_names()).
ML_FUNDING_FEATURE = os.getenv("ML_FUNDING_FEATURE", "false").strip().lower() in ("1", "true", "yes", "on")
FEATURES_ORDER = ["rsi", "adx", "ema_dist", "macd", "vol_ratio"] + (["funding"] if ML_FUNDING_FEATURE else [])

PARES_DEFAULT = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "LINK/USDT", "SUI/USDT",
    "AVAX/USDT", "ADA/USDT", "XRP/USDT", "MATIC/USDT", "DOT/USDT",
    "UNI/USDT", "LTC/USDT", "NEAR/USDT", "APT/USDT", "ARB/USDT",
    "INJ/USDT", "OP/USDT", "FIL/USDT", "ATOM/USDT", "STX/USDT",
    "TIA/USDT", "SEI/USDT", "GALA/USDT", "FTM/USDT", "RNDR/USDT",
    "LDO/USDT", "AAVE/USDT", "SNX/USDT", "IMX/USDT", "MKR/USDT",
]

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("entrenar")


# =============================================================================
# MINING — extrae features + label desde OHLCV histórico de Binance
# =============================================================================
def _crear_tabla(conn):
    """Idempotente. Schema alineado con main.py V1.5.0:
    9 columnas (id, venue, symbol, rsi, adx, ema_dist, macd, vol_ratio, resultado).
    ALTER añade `venue` a DBs viejas de pre-V1.5.0 que no la tenían."""
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ml_feature_log (
            id TEXT PRIMARY KEY,
            venue TEXT,
            symbol TEXT,
            rsi REAL,
            adx REAL,
            ema_dist REAL,
            macd REAL,
            vol_ratio REAL,
            resultado INTEGER
        )
    """)
    # Migración idempotente para DBs anteriores que no tenían `venue`.
    try:
        conn.execute("ALTER TABLE ml_feature_log ADD COLUMN venue TEXT")
    except sqlite3.OperationalError:
        pass  # columna ya existe
    # Punto A: columna funding (NULL en filas viejas → se descartan en el dropna del train).
    try:
        conn.execute("ALTER TABLE ml_feature_log ADD COLUMN funding REAL")
    except sqlite3.OperationalError:
        pass
    conn.commit()


def _fetch_paginado(exchange, sym, dias):
    """Pagina fetch_ohlcv 1000 velas para superar el límite single-call."""
    inicio_ms = int((datetime.now() - timedelta(days=dias)).timestamp() * 1000)
    velas = []
    desde_ms = inicio_ms
    while True:
        batch = exchange.fetch_ohlcv(sym, TIMEFRAME, since=desde_ms, limit=1000)
        if not batch:
            break
        velas.extend(batch)
        desde_ms = batch[-1][0] + 1
        if len(batch) < 1000:
            break
        time.sleep(0.2)
    return velas


def _calcular_features(df):
    """Computa los 5 indicadores. Mismo cálculo que MotorIndicadores._scan_venue."""
    df.ta.rsi(length=14, append=True)
    df.ta.adx(length=14, append=True)
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    df["EMA_200"] = ta.ema(df["close"], length=200)
    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def _label_lookahead(df, i):
    """Mira HORIZONTE_VELAS adelante: 1 si TP antes que SL, 0 si SL primero, None si neutral."""
    entrada = df.iloc[i]["close"]
    for j in range(1, HORIZONTE_VELAS + 1):
        if i + j >= len(df):
            return None
        hi = df.iloc[i + j]["high"]
        lo = df.iloc[i + j]["low"]
        if (lo - entrada) / entrada <= STOP_LOSS:
            return 0
        if (hi - entrada) / entrada >= OBJETIVO_PROFIT:
            return 1
    return None


def minar(db_path, pares, dias):
    """Mining principal. Extrae features + label, escribe a ml_feature_log."""
    log.info(f"Mining: {len(pares)} pares × {dias}d ({TIMEFRAME}) → {db_path}")
    exchange = ccxt.binance({
        "enableRateLimit": True,
        "options": {"defaultType": "spot", "adjustForTimeDifference": True},
    })

    conn = sqlite3.connect(db_path)
    _crear_tabla(conn)

    total_nuevos = 0
    for sym in pares:
        log.info(f"  → {sym}")
        try:
            velas = _fetch_paginado(exchange, sym, dias)
            if not velas:
                log.warning(f"     {sym}: 0 velas, skip")
                continue
            df = pd.DataFrame(velas, columns=["ts", "open", "high", "low", "close", "volume"])
            df.drop_duplicates(subset=["ts"], keep="last", inplace=True)
            for c in ["open", "high", "low", "close", "volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df = _calcular_features(df)
            if df.empty:
                continue

            # Punto A: historial de funding del perp (solo si el feature está on; defensivo).
            f_ts, f_rates = [], []
            if ML_FUNDING_FEATURE:
                perp = f"{sym}:{sym.split('/')[1]}"
                pts = _funding_historico(exchange, perp, dias)
                f_ts, f_rates = ([p[0] for p in pts], [p[1] for p in pts]) if pts else ([], [])

            nuevos = 0
            for i in range(len(df) - HORIZONTE_VELAS):
                fila = df.iloc[i]
                resultado = _label_lookahead(df, i)
                if resultado is None:
                    continue

                ema200 = float(fila["EMA_200"])
                entrada = float(fila["close"])
                ema_dist = (entrada - ema200) / ema200 if ema200 > 0 else 0.0
                if i >= 20:
                    vol_media = df["volume"].iloc[i - 20:i].mean()
                    vol_ratio = entrada and (fila["volume"] / vol_media if vol_media > 0 else 1.0)
                else:
                    vol_ratio = 1.0
                funding = _funding_en(f_ts, f_rates, int(fila["ts"])) if ML_FUNDING_FEATURE else None

                # ID encode timestamp para idempotencia.
                fid = f"binance_{sym}_{int(fila['ts'])}"
                conn.execute(
                    "INSERT OR IGNORE INTO ml_feature_log "
                    "(id, venue, symbol, rsi, adx, ema_dist, macd, vol_ratio, resultado, funding) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (fid, "binance", sym,
                     float(fila["RSI_14"]),
                     float(fila.get("ADX_14", 20)),
                     ema_dist,
                     float(fila.get("MACDh_12_26_9", 0)),
                     float(vol_ratio),
                     int(resultado),
                     funding)
                )
                if conn.execute("SELECT changes()").fetchone()[0] > 0:
                    nuevos += 1
            conn.commit()
            log.info(f"     {sym}: +{nuevos} muestras nuevas")
            total_nuevos += nuevos
        except Exception as e:
            log.warning(f"     {sym}: error {type(e).__name__}: {e}")
        time.sleep(0.5)

    conn.close()
    log.info(f"Mining completo: +{total_nuevos} muestras totales")
    return total_nuevos


# =============================================================================
# TRAIN — XGBoost con las features ya en DB
# =============================================================================
def _split_temporal(X, y, ts, test_frac=0.20, embargo=HORIZONTE_VELAS):
    """Walk-forward: ordena por tiempo y separa el test como el tramo MÁS RECIENTE (no aleatorio).
    El split aleatorio de train_test_split FILTRA el futuro en serie temporal (velas de mañana en
    train) → infla la métrica. `embargo` descarta las velas de train que se solapan con el horizonte
    del label del test (24 velas mirando adelante) para evitar leakage en el borde. La métrica honesta
    sale más baja que la del split aleatorio — pero es la real out-of-sample."""
    order = np.argsort(ts, kind="stable")
    Xs, ys = X[order], y[order]
    corte = int(len(ys) * (1 - test_frac))
    tr_fin = max(0, corte - embargo)
    return Xs[:tr_fin], Xs[corte:], ys[:tr_fin], ys[corte:]


def entrenar(db_path, modelo_path=MODEL_PATH):
    """Entrena XGBClassifier desde ml_feature_log, persiste a modelo_path."""
    try:
        from xgboost import XGBClassifier
        from sklearn.metrics import accuracy_score, classification_report, roc_auc_score
        import joblib
    except ImportError as e:
        log.error(f"Falta dependencia: {e}. Ejecutá: pip install -r requirements.txt")
        return False

    log.info(f"Train: leyendo {db_path}")
    conn = sqlite3.connect(db_path)
    # `id` (= venue_sym_TS) trae el timestamp para ordenar cronológicamente (walk-forward).
    # Columnas dinámicas: FEATURES_ORDER incluye 'funding' si ML_FUNDING_FEATURE (idéntico a main.py).
    cols = ", ".join(FEATURES_ORDER)
    rows = conn.execute(
        f"SELECT {cols}, resultado, id FROM ml_feature_log WHERE resultado IS NOT NULL"
    ).fetchall()
    conn.close()

    if not rows:
        log.error("ml_feature_log vacío. Corré primero el mining: python entrenar.py --solo-mining")
        return False
    if len(rows) < 500:
        log.warning(f"Solo {len(rows)} muestras — alto riesgo de underfitting. Recomendado >2000.")

    df = pd.DataFrame(rows, columns=FEATURES_ORDER + ["resultado", "id"])
    df = df.dropna(subset=FEATURES_ORDER + ["resultado"])

    def _ts_de_id(s):
        try:
            return int(str(s).rsplit("_", 1)[1])   # 'binance_BTC/USDT_1700000000000' → ts
        except Exception:
            return 0

    ts = df["id"].map(_ts_de_id).values
    X = df[FEATURES_ORDER].values
    y = df["resultado"].values.astype(int)

    pos = int(y.sum())
    neg = len(y) - pos
    scale_pos_weight = (neg / pos) if pos > 0 else 1.0
    log.info(f"Dataset: {len(y)} muestras | +{pos} / -{neg} | balance scale_pos_weight={scale_pos_weight:.2f}")

    # Walk-forward: test = tramo MÁS RECIENTE (no aleatorio). Evita el leakage temporal que
    # inflaba la métrica; la accuracy/AUC honesta sale más baja pero es la real out-of-sample.
    X_tr, X_te, y_tr, y_te = _split_temporal(X, y, ts, test_frac=0.20)
    log.info(f"Split temporal (walk-forward): train={len(y_tr)} | test={len(y_te)} (más reciente)")

    model = XGBClassifier(
        n_estimators=300,
        max_depth=5,
        learning_rate=0.05,
        subsample=0.85,
        colsample_bytree=0.85,
        scale_pos_weight=scale_pos_weight,
        objective="binary:logistic",
        eval_metric="logloss",
        random_state=42,
        n_jobs=-1,
    )
    log.info("Entrenando XGBClassifier...")
    model.fit(X_tr, y_tr)

    y_pred = model.predict(X_te)
    y_proba = model.predict_proba(X_te)[:, 1]
    acc = accuracy_score(y_te, y_pred)
    auc = roc_auc_score(y_te, y_proba)
    log.info(f"Test accuracy: {acc:.3f}  |  ROC-AUC: {auc:.3f}")
    log.info("Classification report (test):\n" + classification_report(y_te, y_pred, digits=3))

    importancias = dict(zip(FEATURES_ORDER, model.feature_importances_))
    log.info("Feature importances: " +
             ", ".join(f"{k}={v:.3f}" for k, v in sorted(importancias.items(), key=lambda x: -x[1])))

    joblib.dump(model, modelo_path)
    log.info(f"✅ Modelo persistido: {modelo_path} ({os.path.getsize(modelo_path) / 1024:.1f} KB)")
    log.info(f"   main.py lo hot-reloadea automáticamente (ml_model_mtime check)")
    return True


# =============================================================================
# SHADOW BACKFILL — replay histórico del gestor de salida (Fase 1/3 del short)
# Minar el pasado en vez de esperar trades en vivo (que en alcista nunca llegan).
# =============================================================================
REGIME_ENTER_BEAR = -0.02   # idéntico a main.RadarRegimen (BTC vs EMA200d)
PARES_SHORT_BACKFILL = {"BTC/USDT": "BTC/USDT:USDT", "ETH/USDT": "ETH/USDT:USDT"}


def _regimen_btc_diario(exchange):
    """Mapa día→bool BAJISTA (BTC < EMA200d·(1+ENTER_BEAR)). Mismo criterio que RadarRegimen."""
    velas = exchange.fetch_ohlcv("BTC/USDT", "1d", limit=400)
    df = pd.DataFrame(velas, columns=["ts", "o", "h", "l", "c", "v"])
    df["c"] = pd.to_numeric(df["c"], errors="coerce")
    df["ema200"] = ta.ema(df["c"], length=200)
    df = df.dropna()
    reg = {}
    for _, r in df.iterrows():
        desv = (r["c"] - r["ema200"]) / r["ema200"] if r["ema200"] > 0 else 0.0
        reg[int(r["ts"]) // 86400000] = bool(desv < REGIME_ENTER_BEAR)
    return reg


def _bars_con_indicadores(df):
    """rsi/adx(+DI)/macd/ema200/atr por barra → lista de dicts con las claves que usa el shadow."""
    df.ta.rsi(length=14, append=True)
    df.ta.adx(length=14, append=True)       # ADX_14 + DMP_14/DMN_14
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    df.ta.atr(length=14, append=True)       # ATRr_14
    df["EMA_200"] = ta.ema(df["close"], length=200)
    df = df.dropna().reset_index(drop=True)
    bars = []
    for _, r in df.iterrows():
        bars.append({
            "ts": float(r["ts"]) / 1000.0, "ts_ms": int(r["ts"]), "close": float(r["close"]),
            "high": float(r["high"]), "low": float(r["low"]),   # intrabar (toques TP/SL reales)
            "RSI_14": float(r.get("RSI_14", 0) or 0), "ADX_14": float(r.get("ADX_14", 0) or 0),
            "DMP_14": float(r.get("DMP_14", 0) or 0), "DMN_14": float(r.get("DMN_14", 0) or 0),
            "MACDh_12_26_9": float(r.get("MACDh_12_26_9", 0) or 0),
            "ATRr_14": float(r.get("ATRr_14", 0) or 0), "EMA_200": float(r.get("EMA_200", 0) or 0),
        })
    return bars


def _funding_historico(exchange, perp, dias):
    """Punto C: historial de funding del perp (8h). Defensivo: si no está disponible → []
    (cada barra cae a funding 0 = neutro, sin romper el backfill)."""
    try:
        since = int((datetime.now() - timedelta(days=dias)).timestamp() * 1000)
        hist = exchange.fetch_funding_rate_history(perp, since=since, limit=1000)
        return sorted((int(h["timestamp"]), float(h["fundingRate"]))
                      for h in hist if h.get("timestamp") is not None)
    except Exception as e:
        log.warning(f"  funding {perp}: no disponible ({type(e).__name__}) → neutro")
        return []


def _funding_en(ts_list, rates, ts_ms):
    """Último funding con ts ≤ ts_ms (el vigente en esa barra)."""
    import bisect
    if not ts_list:
        return 0.0
    i = bisect.bisect_right(ts_list, ts_ms) - 1
    return rates[i] if i >= 0 else 0.0


def backfill_shadow(db_path, dias):
    """Siembra short_shadow_pnl replayeando OHLCV histórico por las políticas de salida del short."""
    import main   # lazy: no cargar el bot en el path ML normal
    log.info(f"Shadow backfill: {dias}d → {db_path}")
    exchange = ccxt.binance({"enableRateLimit": True,
                             "options": {"defaultType": "spot", "adjustForTimeDifference": True}})
    reg = _regimen_btc_diario(exchange)
    db = main.DatabaseManager(db_path)
    # idempotencia: limpia el histórico (entry_ts viejo), preserva las filas en vivo (~now).
    db._exec("DELETE FROM short_shadow_pnl WHERE entry_ts < ?", (time.time() - 86400,))
    total = 0
    for spot, perp in PARES_SHORT_BACKFILL.items():
        try:
            velas = _fetch_paginado(exchange, spot, dias)
            if not velas:
                continue
            df = pd.DataFrame(velas, columns=["ts", "open", "high", "low", "close", "volume"])
            df.drop_duplicates(subset=["ts"], keep="last", inplace=True)
            for c in ["open", "high", "low", "close", "volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            bars = _bars_con_indicadores(df)
            if main.SHORT_FUNDING_CONTEXT:   # Punto C: anexa el funding vigente a cada barra
                pts = _funding_historico(exchange, perp, dias)
                ts_list, rates = ([p[0] for p in pts], [p[1] for p in pts]) if pts else ([], [])
                for b in bars:
                    b["funding"] = _funding_en(ts_list, rates, b["ts_ms"])
            flags = [reg.get(b["ts_ms"] // 86400000, False) for b in bars]
            n = main.backfill_shadow_bars(db, perp, bars, flags)
            log.info(f"  {spot}: {n} barras, BAJISTA en {sum(flags)}")
            total += n
        except Exception as e:
            log.warning(f"  {spot}: error {type(e).__name__}: {e}")
    log.info(f"Backfill completo (+{total} barras). Readiness: {db.short_shadow_listo(30)}")
    return total


def regimen_backfill(dias, horizonte=5):
    """Audita el Cerebro de Régimen 4-estados (V1.8.0) sobre histórico BTC diario: etiqueta cada
    día con main.clasificar_regimen (mismas señales que Selector._inputs: desv/slope/adx/atr) y
    mide qué pasó DESPUÉS de cada estado (retorno forward + oscilación cosechable a `horizonte`
    días). Criterio de graduación a live: RANGO precede oscilación cosechable; TENDENCIA_DOWN/CAOS
    preceden drawdown/whipsaw; TENDENCIA_UP precede subidas. NO escribe DB — solo imprime auditoría."""
    import main
    from collections import defaultdict
    log.info(f"Regimen backfill (auditoría): {dias}d, horizonte {horizonte}d")
    exchange = ccxt.binance({"enableRateLimit": True,
                             "options": {"defaultType": "spot", "adjustForTimeDifference": True}})
    # daily directo (no _fetch_paginado, que es 15m). Warmup: EMA200(200)+slope(30)+horizonte.
    lim = min(1000, dias + 260)
    velas = exchange.fetch_ohlcv("BTC/USDT", "1d", limit=lim)
    df = pd.DataFrame(velas, columns=["ts", "open", "high", "low", "close", "volume"])
    df.drop_duplicates(subset=["ts"], keep="last", inplace=True)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df.ta.adx(length=14, append=True)
    df.ta.atr(length=14, append=True)
    df["EMA_200"] = ta.ema(df["close"], length=200)
    df = df.dropna().reset_index(drop=True)

    slope_d = main.SELECTOR_EMA_SLOPE_DAYS
    closes = df["close"].tolist()
    ema = df["EMA_200"].tolist()
    adx = df["ADX_14"].tolist()
    atrr = df["ATRr_14"].tolist()
    agg = defaultdict(list)
    for i in range(slope_d, len(df) - horizonte):
        desv = (closes[i] - ema[i]) / ema[i] if ema[i] else 0.0
        slope = (ema[i] - ema[i - slope_d]) / ema[i - slope_d] if ema[i - slope_d] else 0.0
        atr = atrr[i] / closes[i] if closes[i] else 0.0
        estado, _ = main.clasificar_regimen(dict(desv=desv, slope=slope, adx=adx[i], atr=atr))
        fwd = closes[i + horizonte] / closes[i] - 1.0                     # retorno forward
        osc = sum(abs(closes[j + 1] / closes[j] - 1.0)                    # oscilación (round-trips)
                  for j in range(i, i + horizonte) if closes[j])
        agg[estado].append((fwd, osc))

    total = sum(len(v) for v in agg.values())
    print(f"\n=== Auditoría Cerebro de Régimen ({total} días etiquetados, horizonte {horizonte}d) ===")
    print(f"{'estado':16}{'n':>5}{'%dist':>7}{'fwd_ret%':>10}{'oscil%':>9}{'win%':>7}")
    print("-" * 54)
    for est in ["TENDENCIA_UP", "RANGO", "TENDENCIA_DOWN", "CAOS"]:
        v = agg.get(est, [])
        if not v:
            print(f"{est:16}{0:>5}")
            continue
        n = len(v)
        avg_fwd = sum(f for f, _ in v) / n * 100
        avg_osc = sum(o for _, o in v) / n * 100
        win = sum(1 for f, _ in v if f > 0) / n * 100
        print(f"{est:16}{n:>5}{n/total*100:>6.0f}%{avg_fwd:>10.2f}{avg_osc:>9.2f}{win:>6.0f}%")
    print("\nLectura: RANGO debería tener oscil% alta (cosecha) y fwd_ret ~0; TENDENCIA_UP fwd_ret>0;")
    print("  TENDENCIA_DOWN fwd_ret<0; CAOS oscil% alta + fwd_ret negativo/volátil (whipsaw).")
    print("  Si el mapeo se sostiene → graduar a REGIMEN_BRAIN_MODE=live con evidencia.")
    return dict(agg)


# =============================================================================
# SLEEVE TREND-RIDE — Fase 1: medición (¿comprar en TENDENCIA_UP y cabalgar con chandelier
# gana plata neto de fees?). NO toca bot/DB/capital. Decide si la Fase 2 (ejecución) se justifica.
# =============================================================================
def simular_sleeve(bars, estados, k=3.0, hard=0.10, fee=0.002):
    """Simulación PURA buy-and-ride del sleeve. `bars` = lista de dicts {close, high, low, atr}
    (atr en price units = ATRr_14); `estados` = lista paralela de estados de régimen.
    Entra al close cuando estado==TENDENCIA_UP sin posición; cabalga con chandelier (peak−k·atr),
    stop duro (entry·(1−hard)) y flip de régimen. Pesimista intrabar: el `low` dispara los stops.
    Devuelve trades [{entry, exit, pnl, motivo, hold}] con pnl neto de `fee` (round-trip)."""
    trades = []
    pos = None
    for i, b in enumerate(bars):
        est = estados[i]
        if pos is None:
            if est == "TENDENCIA_UP":
                pos = {"entry": b["close"], "peak": b["close"], "i": i}
            continue
        pos["peak"] = max(pos["peak"], b["high"])
        atr = b["atr"]
        trail = pos["peak"] - k * atr if atr > 0 else float("-inf")
        hard_stop = pos["entry"] * (1 - hard)
        stop = max(trail, hard_stop)
        if b["low"] <= stop:
            motivo = "chandelier" if trail >= hard_stop else "stop_duro"
            exit_px = stop
        elif est != "TENDENCIA_UP":
            motivo, exit_px = "flip", b["close"]
        else:
            continue
        pnl = (exit_px - pos["entry"]) / pos["entry"] - fee
        trades.append({"entry": pos["entry"], "exit": exit_px, "pnl": pnl,
                       "motivo": motivo, "hold": i - pos["i"]})
        pos = None
    return trades


def sleeve_backfill(dias):
    """Mide el sleeve sobre histórico BTC/ETH diario: etiqueta régimen, simula buy-and-ride con
    chandelier, barre k ∈ {2,3,4}. Referencias: buy&hold del período y % tiempo en TENDENCIA_UP.
    NO escribe DB. Criterio Fase 2: net-positivo tras fees Y mejor que no operar."""
    import main
    log.info(f"Sleeve backfill (medición): {dias}d")
    exchange = ccxt.binance({"enableRateLimit": True,
                             "options": {"defaultType": "spot", "adjustForTimeDifference": True}})
    slope_d = main.SELECTOR_EMA_SLOPE_DAYS
    for spot in ["BTC/USDT", "ETH/USDT"]:
        velas = exchange.fetch_ohlcv(spot, "1d", limit=min(1000, dias + 260))
        df = pd.DataFrame(velas, columns=["ts", "open", "high", "low", "close", "volume"])
        df.drop_duplicates(subset=["ts"], keep="last", inplace=True)
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df.ta.adx(length=14, append=True)
        df.ta.atr(length=14, append=True)
        df["EMA_200"] = ta.ema(df["close"], length=200)
        df = df.dropna().reset_index(drop=True)
        closes, highs, lows = df["close"].tolist(), df["high"].tolist(), df["low"].tolist()
        ema, adx, atrr = df["EMA_200"].tolist(), df["ADX_14"].tolist(), df["ATRr_14"].tolist()
        bars, estados = [], []
        for i in range(slope_d, len(df)):
            desv = (closes[i] - ema[i]) / ema[i] if ema[i] else 0.0
            slope = (ema[i] - ema[i - slope_d]) / ema[i - slope_d] if ema[i - slope_d] else 0.0
            atr_norm = atrr[i] / closes[i] if closes[i] else 0.0
            est, _ = main.clasificar_regimen(dict(desv=desv, slope=slope, adx=adx[i], atr=atr_norm))
            bars.append({"close": closes[i], "high": highs[i], "low": lows[i], "atr": atrr[i]})
            estados.append(est)
        bh = (closes[-1] / closes[slope_d] - 1.0) * 100 if closes[slope_d] else 0.0
        pct_up = sum(1 for e in estados if e == "TENDENCIA_UP") / len(estados) * 100 if estados else 0.0

        print(f"\n=== Sleeve {spot} ({len(estados)} días, {pct_up:.0f}% en TENDENCIA_UP, "
              f"buy&hold {bh:+.0f}%) ===")
        print(f"{'k':>4}{'trades':>8}{'sumPnL%':>10}{'avg%':>8}{'win%':>7}{'holdMed':>9}{'ch/fl/st':>12}")
        print("-" * 60)
        for k in [2.0, 3.0, 4.0]:
            tr = simular_sleeve(bars, estados, k=k)
            n = len(tr)
            if not n:
                print(f"{k:>4.0f}{0:>8}")
                continue
            tot = sum(t["pnl"] for t in tr) * 100
            avg = tot / n
            win = sum(1 for t in tr if t["pnl"] > 0) / n * 100
            hold = sorted(t["hold"] for t in tr)[n // 2]
            mch = sum(1 for t in tr if t["motivo"] == "chandelier")
            mfl = sum(1 for t in tr if t["motivo"] == "flip")
            mst = sum(1 for t in tr if t["motivo"] == "stop_duro")
            print(f"{k:>4.0f}{n:>8}{tot:>10.1f}{avg:>8.2f}{win:>6.0f}%{hold:>9}{f'{mch}/{mfl}/{mst}':>12}")
    print("\nsumPnL% = suma de retornos por trade (no compuesto, por lote). Criterio Fase 2:")
    print("  net-positivo tras fees Y que supere a NO operar (earn). Si no → sleeve apagado, sin capital.")


# =============================================================================
# BARREDOR DE SEÑAL — mide variantes de senal_short offline (trades vs PnL baseline)
# El cuello de botella del short es la ENTRADA (la señal casi no dispara), no la salida.
# Este barre umbrales y reporta frecuencia + rentabilidad del baseline (oco_fijo), sin tocar plata.
# =============================================================================
VARIANTES_SENAL = [
    {"nombre": "actual (RSI55, DI on)", "rsi_min": 55, "di_filter": True},
    {"nombre": "RSI52, DI on",          "rsi_min": 52, "di_filter": True},
    {"nombre": "RSI50, DI on",          "rsi_min": 50, "di_filter": True},
    {"nombre": "RSI55, DI off",         "rsi_min": 55, "di_filter": False},
    {"nombre": "RSI52, DI off",         "rsi_min": 52, "di_filter": False},
    {"nombre": "RSI50, DI off",         "rsi_min": 50, "di_filter": False},
]


def barrer_senal(db_path, dias):
    """Para cada variante de señal: replaya el histórico y reporta nº de trades + PnL del baseline
    (oco_fijo). Así se decide CON DATOS si aflojar la señal sube frecuencia sin degradar calidad."""
    import main
    exchange = ccxt.binance({"enableRateLimit": True,
                             "options": {"defaultType": "spot", "adjustForTimeDifference": True}})
    reg = _regimen_btc_diario(exchange)
    # Minar una sola vez las barras (caras de bajar); reusar en cada variante.
    datos = {}
    for spot, perp in PARES_SHORT_BACKFILL.items():
        velas = _fetch_paginado(exchange, spot, dias)
        if not velas:
            continue
        df = pd.DataFrame(velas, columns=["ts", "open", "high", "low", "close", "volume"])
        df.drop_duplicates(subset=["ts"], keep="last", inplace=True)
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        bars = _bars_con_indicadores(df)
        flags = [reg.get(b["ts_ms"] // 86400000, False) for b in bars]
        datos[perp] = (bars, flags)

    log.info(f"Barredor de señal ({dias}d, {len(datos)} símbolos):")
    print(f"\n{'variante':24} {'trades':>7} {'pnl_avg':>9} {'total%':>9} {'winrate':>8}")
    print("-" * 62)
    for v in VARIANTES_SENAL:
        sf = lambda ind, v=v: main.senal_short(ind, rsi_min=v["rsi_min"], di_filter=v["di_filter"])
        mem = main.DatabaseManager(":memory:")
        for perp, (bars, flags) in datos.items():
            main.backfill_shadow_bars(mem, perp, bars, flags, senal_fn=sf)
        rows = mem._exec("SELECT pnl_pct FROM short_shadow_pnl WHERE politica='oco_fijo'",
                         fetch="all") or []
        pnls = [r[0] for r in rows]
        n = len(pnls)
        avg = (sum(pnls) / n) if n else 0.0
        tot = sum(pnls)
        wr = (sum(1 for p in pnls if p > 0) / n * 100) if n else 0.0
        print(f"{v['nombre']:24} {n:>7} {avg:>+8.2f}% {tot:>+8.1f}% {wr:>6.0f}%")
    print()


# =============================================================================
# GRID SWEEP (V1.8.4) — opción ③: optimizar geometría del grid por régimen.
# El árbol predictivo está muerto (AUC 0.50, ver bot.txt V1.8.3); la inteligencia
# que SÍ paga es no-predictiva: ¿qué geometría (dip de entrada × tp de cosecha)
# maximiza la cosecha NETA de fees, condicionada al régimen del cerebro? Mide, no
# despliega (mismo patrón regimen/sleeve backfill). NO toca bot/DB/capital.
# =============================================================================
GRID_PARES = ["BTC/USDT", "ETH/USDT", "TRX/USDT"]
GRID_DIPS = [0.03, 0.05, 0.08]
GRID_TPS = [0.03, 0.05, 0.08]


def grid_sim_neto(closes, operable, dip, tp, fee=0.002, max_inv=3):
    """grid_sim con gate de régimen por barra (operable[i] bool) + fees por cosecha + CAP de
    inventario (capital finito). PURA. Compra un lote al caer `dip` desde el pico (solo si
    operable[i] y el inventario no llegó a `max_inv` lotes — fiel al grid real que capa el DCA a
    okx_dca_max_count=3); cosecha a +`tp` cobrando `tp - fee` (round-trip ~0.2% spot). Devuelve
    (realizado_neto%, trabado%, n_cosechas). `trabado` = mark-to-market de los ≤max_inv lotes sin
    vender al final (castiga al falling-knife, ahora ACOTADO). NET = realizado_neto + trabado."""
    inv, real, n = [], 0.0, 0
    pico = closes[0] if closes else 0.0
    for i, px in enumerate(closes):
        pico = max(pico, px)
        if operable[i] and px <= pico * (1 - dip) and len(inv) < max_inv:
            inv.append(px)
            pico = px
        rest = []
        for c in inv:
            if px >= c * (1 + tp):
                real += (tp - fee)
                n += 1
            else:
                rest.append(c)
        inv = rest
    ult = closes[-1] if closes else 0.0
    trabado = sum((ult - c) / c for c in inv if c)
    return round(real * 100, 2), round(trabado * 100, 2), n


def _regimen_estado_por_ts(exchange, dias, sym="BTC/USDT"):
    """Etiqueta el régimen diario de `sym` (mismas señales que el cerebro live) y devuelve
    dict ts_ms -> estado 4-clases ('TENDENCIA_UP'/'RANGO'/'TENDENCIA_DOWN'/'CAOS').
    Base de _regimen_operable_por_ts. SP2: con sym≠BTC da el régimen PROPIO del activo → un alt en
    declive secular clasifica TENDENCIA_DOWN en su marco y el ladder se congela ahí solo."""
    import main
    lim = min(1000, dias + 260)
    velas = exchange.fetch_ohlcv(sym, "1d", limit=lim)
    df = pd.DataFrame(velas, columns=["ts", "open", "high", "low", "close", "volume"])
    df.drop_duplicates(subset=["ts"], keep="last", inplace=True)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df.ta.adx(length=14, append=True)
    df.ta.atr(length=14, append=True)
    df["EMA_200"] = ta.ema(df["close"], length=200)
    df = df.dropna().reset_index(drop=True)
    slope_d = main.SELECTOR_EMA_SLOPE_DAYS
    closes, ema = df["close"].tolist(), df["EMA_200"].tolist()
    adx, atrr, ts = df["ADX_14"].tolist(), df["ATRr_14"].tolist(), df["ts"].tolist()
    out = {}
    for i in range(slope_d, len(df)):
        desv = (closes[i] - ema[i]) / ema[i] if ema[i] else 0.0
        slope = (ema[i] - ema[i - slope_d]) / ema[i - slope_d] if ema[i - slope_d] else 0.0
        atr = atrr[i] / closes[i] if closes[i] else 0.0
        estado, _ = main.clasificar_regimen(dict(desv=desv, slope=slope, adx=adx[i], atr=atr))
        out[int(ts[i])] = estado
    return out


def _regimen_operable_por_ts(exchange, dias):
    """dict ts_ms -> operable(bool). operable = RANGO o TENDENCIA_UP (el playbook del cerebro:
    DOWN/CAOS congelan). Deriva de _regimen_estado_por_ts."""
    import main
    return {ts: main.regimen_opera(est) for ts, est in _regimen_estado_por_ts(exchange, dias).items()}


def grid_sweep(dias, fee=0.002):
    """Opción ③ — barre geometría dip×tp del grid por régimen sobre BTC/ETH/TRX, neto de fees.
    Compara el gate del cerebro (RANGO+TENDENCIA_UP operan) vs sin-gate, y reporta la mejor
    geometría por par vs buy&hold. NO escribe DB — solo imprime. Proxy diario de oscilación."""
    log.info(f"Grid sweep (opción ③): {dias}d, fee_roundtrip={fee:.3f}")
    exchange = ccxt.binance({"enableRateLimit": True,
                             "options": {"defaultType": "spot", "adjustForTimeDifference": True}})
    op_map = _regimen_operable_por_ts(exchange, dias)
    pct_op = 100.0 * sum(1 for v in op_map.values() if v) / max(1, len(op_map))
    print(f"\nDías con régimen operable (RANGO+TEND_UP): {pct_op:.0f}% de {len(op_map)}")

    for sym in GRID_PARES:
        velas = exchange.fetch_ohlcv(sym, "1d", limit=min(1000, dias + 260))
        d = pd.DataFrame(velas, columns=["ts", "o", "h", "l", "close", "v"]).drop_duplicates("ts")
        d = d.sort_values("ts")
        d = d[d["ts"].astype(int).isin(op_map)]   # solo barras con régimen conocido
        if len(d) < 30:
            print(f"\n{sym}: datos insuficientes")
            continue
        closes = pd.to_numeric(d["close"], errors="coerce").tolist()
        oper = [op_map[int(t)] for t in d["ts"].tolist()]
        siempre = [True] * len(closes)
        bh = (closes[-1] / closes[0] - 1.0) * 100
        print(f"\n=== {sym} ({len(closes)}d, buy&hold {bh:+.1f}%) — NET% (cosecha_neta+trabado) ===")
        print(f"{'dip\\tp':>8}" + "".join(f"{tp*100:>9.0f}%" for tp in GRID_TPS) + "   (gate cerebro)")
        mejor = (-1e9, None)
        for dip in GRID_DIPS:
            fila = f"{dip*100:>7.0f}%"
            for tp in GRID_TPS:
                r, t, n = grid_sim_neto(closes, oper, dip, tp, fee)
                net = r + t
                fila += f"{net:>9.1f} "
                if net > mejor[0]:
                    mejor = (net, (dip, tp, r, t, n))
            print(fila)
        dip, tp, r, t, n = mejor[1]
        # mismo (dip,tp) ganador pero SIN gate de régimen, para aislar el aporte del cerebro
        r2, t2, n2 = grid_sim_neto(closes, siempre, dip, tp, fee)
        print(f"  mejor gate-cerebro: dip {dip*100:.0f}% tp {tp*100:.0f}% → NET {mejor[0]:+.1f}% "
              f"(cosechó {r:+.1f}% en {n}, trabado {t:+.1f}%)")
        print(f"  misma geo SIN gate régimen:            NET {r2+t2:+.1f}% "
              f"(cosechó {r2:+.1f}% en {n2}, trabado {t2:+.1f}%)  ← cuánto aporta congelar el bear")
    print("\nLectura: NET = cosecha neta de fees + inventario trabado (mark-to-market). Si la mejor")
    print("  geometría supera a buy&hold Y el gate-cerebro mejora al sin-gate → tuning por régimen paga.")
    print("  Proxy diario (no intrabar): subestima la cosecha real del grid 15m; sirve para RANKEAR geo.")


# =============================================================================
# GRID INTRABAR (V1.8.5) — opción ③ FIEL: simulador 15m del DCA+ratchet real, capital-constrained.
# El proxy diario (V1.8.4) modeló mal la ENTRADA (creyó "compra dips"; el grid real entra con
# ADX<20 & close>EMA200 & cooldown, y cosecha scale-out por tiers [2,4,7,11,16]%). Esto calca la
# mecánica real a 15m para validar/refutar el lead "tiers demasiado apretados" ANTES de capital.
# Abstracciones declaradas: DCA con rebote aproximado (bottom corrido + bounce); gate de régimen =
# operable diario del cerebro mapeado a la barra 15m; sin moonbag/stop-sectorial (BTC/ETH no los usan).
# NO toca bot/DB/capital.
# =============================================================================
GRID_BASE_TIERS = [2.0, 4.0, 7.0, 11.0, 16.0]   # == main.HARVEST_TIERS
GRID_HARVEST_FRAC = 0.15                          # == main.HARVEST_FRAC


def _atr_mult(atr_pct, lo=1.0, hi=4.0, low_mult=0.75, high_mult=1.4):
    """Replica get_harvest_tiers_dyn: tiers ×0.75 (calmo) a ×1.4 (volátil), interp lineal."""
    if atr_pct >= hi:
        return high_mult
    if atr_pct <= lo:
        return low_mult
    t = (atr_pct - lo) / max(1e-9, hi - lo)
    return low_mult + t * (high_mult - low_mult)


def sim_grid_intrabar(bars, operable, tier_mult=1.0, capital0=1000.0, invest_pct=0.04,
                      dca_max=3, harvest_frac=GRID_HARVEST_FRAC, fee=0.001, cooldown_bars=4,
                      harvest_cd_bars=1, entry_adx=20.0, dca_rebound=1.0):
    """Simulador intrabar 15m FIEL del grid OKX. PURO. bars: list de dict
    {close,high,low,rsi,adx,ema200,atr_pct,green}. operable[i]: gate régimen (RANGO+UP).
    Entrada: no-pos & operable & adx<entry_adx & close>ema200 & cooldown → compra invest_pct×cash.
    DCA: pos en pérdida (pnl_low ≤ -5/-10/-18 → L1/L2/L3), rebote≥dca_rebound desde el bottom corrido,
      cap dca_max, en bear solo L3. Harvest: vende harvest_frac de qty al tocar (high) cada tier
      [2,4,7,11,16]×atr_mult×tier_mult no cosechado, cooldown. Capital finito. Devuelve dict."""
    cash = capital0
    lots = []                      # {qty, cost}
    realizado = 0.0
    last_buy = -10**9
    last_harv = -10**9
    harvested = set()
    dca_count = 0
    bottom = float("inf")
    n_entry = n_dca = n_harv = 0

    def qtot():
        return sum(l["qty"] for l in lots)

    def avg():
        q = qtot()
        return sum(l["qty"] * l["cost"] for l in lots) / q if q > 0 else 0.0

    def reducir(qsell):
        q = qtot()
        if q <= 0:
            return
        f = min(1.0, qsell / q)
        for l in lots:
            l["qty"] *= (1.0 - f)

    for i, b in enumerate(bars):
        px, hi, lo = b["close"], b["high"], b["low"]
        q = qtot()
        if q <= 1e-12:
            lots = []
            if (operable[i] and b["adx"] < entry_adx and px > b["ema200"]
                    and (i - last_buy) >= cooldown_bars):
                monto = max(5.0, invest_pct * cash)
                if 5.0 <= monto <= cash:
                    qy = monto / px
                    cash -= monto + monto * fee
                    lots = [{"qty": qy, "cost": px}]
                    last_buy = i
                    n_entry += 1
                    harvested = set(); dca_count = 0; bottom = float("inf")
            continue
        a = avg()
        # ---- HARVEST scale-out (usa el high para alcanzar el tier) ----
        tiers = [t * tier_mult * _atr_mult(b["atr_pct"]) for t in GRID_BASE_TIERS]
        if (i - last_harv) >= harvest_cd_bars and hi > a:
            for ti, t in enumerate(tiers):
                if ti in harvested:
                    continue
                target = a * (1 + t / 100.0)
                if hi >= target:
                    qsell = qtot() * harvest_frac
                    if qsell * target >= 5.0:
                        proceeds = qsell * target
                        realizado += (proceeds - qsell * a) - proceeds * fee
                        cash += proceeds - proceeds * fee
                        reducir(qsell)
                        harvested.add(ti); last_harv = i; n_harv += 1
                    break
        # ---- DCA add (usa el low: peor pnl intrabar) ----
        bottom = min(bottom, lo)
        if dca_count < dca_max:
            pnl_low = (lo - a) / a * 100
            lv = 3 if pnl_low <= -18 else 2 if pnl_low <= -10 else 1 if pnl_low <= -5 else 0
            bear = not operable[i]
            if lv > 0 and not (bear and lv < 3):
                bounce = (px - bottom) / bottom * 100 if bottom > 0 else 0.0
                reb_ok = bounce >= (max(dca_rebound, 1.5) if lv == 1 else dca_rebound)
                if lv == 1:
                    reb_ok = reb_ok and b["rsi"] >= 35 and b["green"]
                if reb_ok:
                    monto = max(5.0, invest_pct * cash) * (1.5 if lv == 3 else 1.0)
                    if 5.0 <= monto <= cash:
                        qy = monto / px
                        cash -= monto + monto * fee
                        lots.append({"qty": qy, "cost": px})
                        dca_count += 1; n_dca += 1
                        harvested = set()      # avg cambió → re-arma tiers
    # cierre: m2m del inventario final al último close
    ult = bars[-1]["close"] if bars else 0.0
    m2m = sum(l["qty"] * (ult - l["cost"]) for l in lots)
    equity = cash + sum(l["qty"] * ult for l in lots)
    return {
        "net_pct": round((equity - capital0) / capital0 * 100, 2),
        "realizado_pct": round(realizado / capital0 * 100, 2),
        "m2m_trabado_pct": round(m2m / capital0 * 100, 2),
        "n_entry": n_entry, "n_dca": n_dca, "n_harvest": n_harv,
        "cash_final": round(cash, 2), "qty_final": round(qtot(), 8),
    }


def _bars_15m(exchange, sym, dias):
    """Fetch 15m paginado + indicadores idénticos a MotorIndicadores. Devuelve list de dict + ts."""
    velas = _fetch_paginado(exchange, sym, dias)
    df = pd.DataFrame(velas, columns=["ts", "open", "high", "low", "close", "volume"])
    df.drop_duplicates(subset=["ts"], keep="last", inplace=True)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df.ta.rsi(length=14, append=True)
    df.ta.adx(length=14, append=True)
    df.ta.atr(length=14, append=True)
    df["EMA_200"] = ta.ema(df["close"], length=200)
    df = df.dropna().reset_index(drop=True)
    bars, ts = [], []
    for _, r in df.iterrows():
        atr_pct = (float(r["ATRr_14"]) / float(r["close"])) * (96 ** 0.5) * 100 if r["close"] else 0.0
        bars.append({"close": float(r["close"]), "high": float(r["high"]), "low": float(r["low"]),
                     "rsi": float(r.get("RSI_14", 50)), "adx": float(r.get("ADX_14", 20)),
                     "ema200": float(r["EMA_200"]), "atr_pct": atr_pct,
                     "green": float(r["close"]) > float(r["open"])})
        ts.append(int(r["ts"]))
    return bars, ts


def grid_intrabar(dias, tier_mults=(0.6, 0.8, 1.0, 1.5, 2.0)):
    """Opción ③ FIEL — barre el ancho de los harvest tiers (tier_mult) sobre el grid real 15m,
    capital-constrained, gate de régimen del cerebro. Reporta net% por mult vs buy&hold. NO escribe DB."""
    log.info(f"Grid intrabar (opción ③ fiel): {dias}d, 15m, sweep tier_mult")
    exchange = ccxt.binance({"enableRateLimit": True,
                             "options": {"defaultType": "spot", "adjustForTimeDifference": True}})
    op_map = _regimen_operable_por_ts(exchange, dias)   # operable por DÍA (ts_ms diario)
    def operable_de(ts_ms):
        day = (ts_ms // 86400000) * 86400000            # floor al día UTC
        return op_map.get(day, True)                    # sin etiqueta (warmup) → no bloquea

    for sym in GRID_PARES:
        bars, ts = _bars_15m(exchange, sym, dias)
        if len(bars) < 300:
            print(f"\n{sym}: datos 15m insuficientes ({len(bars)})")
            continue
        oper = [operable_de(t) for t in ts]
        bh = (bars[-1]["close"] / bars[0]["close"] - 1.0) * 100
        pct_op = 100.0 * sum(oper) / len(oper)
        print(f"\n=== {sym} ({len(bars)} barras 15m, {pct_op:.0f}% operable, buy&hold {bh:+.1f}%) ===")
        print(f"{'tier_mult':>10}{'tiers ej.':>16}{'net%':>9}{'realiz%':>9}{'trabado%':>10}"
              f"{'entry':>7}{'dca':>5}{'harv':>6}")
        for tm in tier_mults:
            r = sim_grid_intrabar(bars, oper, tier_mult=tm)
            ej = "/".join(str(round(t * tm, 1)) for t in GRID_BASE_TIERS[:3]) + ".."
            print(f"{tm:>10.1f}{ej:>16}{r['net_pct']:>9.2f}{r['realizado_pct']:>9.2f}"
                  f"{r['m2m_trabado_pct']:>10.2f}{r['n_entry']:>7}{r['n_dca']:>5}{r['n_harvest']:>6}")
    print("\ntier_mult 1.0 = tiers reales [2,4,7,11,16]%. >1 = cosecha MÁS ANCHA (deja correr),")
    print("  <1 = más apretada (toma antes). net% incluye inventario trabado m2m. Si un mult>1 supera")
    print("  consistentemente a 1.0 (y a buy&hold) en los 3 pares → el lead del proxy se confirma.")

    # ---- Palanca real: DEPLOYMENT (invest_pct), con robustez por mitades ----
    print(f"\n{'='*72}\nPALANCA DEPLOYMENT — sweep invest_pct @ tier_mult=1.0 (net% total | 1ª mitad | 2ª mitad)\n{'='*72}")
    for sym in GRID_PARES:
        bars, ts = _bars_15m(exchange, sym, dias)
        if len(bars) < 300:
            continue
        oper = [operable_de(t) for t in ts]
        mid = len(bars) // 2
        bh_total = (bars[-1]["close"] / bars[0]["close"] - 1.0) * 100
        print(f"\n{sym} (buy&hold total {bh_total:+.1f}%):")
        print(f"{'invest_pct':>11}{'net_total%':>12}{'net_1ª%':>10}{'net_2ª%':>10}{'harv':>6}")
        for ip in (0.02, 0.04, 0.08, 0.15):
            full = sim_grid_intrabar(bars, oper, invest_pct=ip)
            h1 = sim_grid_intrabar(bars[:mid], oper[:mid], invest_pct=ip)
            h2 = sim_grid_intrabar(bars[mid:], oper[mid:], invest_pct=ip)
            print(f"{ip:>11.2f}{full['net_pct']:>12.2f}{h1['net_pct']:>10.2f}"
                  f"{h2['net_pct']:>10.2f}{full['n_harvest']:>6}")
    print("\nLectura: si subir invest_pct mejora el net en AMBAS mitades → deployment paga (palanca real).")
    print("  Si solo amplifica (mejor en la mitad alcista, peor en la bajista) → es dial de RIESGO, no edge.")


# =============================================================================
# GRID RANGO PROBE (V1.8.7) — ¿el grid cosecha BIEN la oscilación en RANGO, o el gate de
# entrada (ADX<20 + cooldown) es demasiado restrictivo y deja oscilación cosechable sin tocar?
# Es la última tesis central que seguía en "fe de diseño": el grid se midió solo en un año bear
# donde RANGO fue raro (~1 entrada/año) → nunca se aisló su hábitat. Esto:
#   (1) aísla RANGO específicamente (no RANGO+UP);
#   (2) mide la oscilación CRUDA cosechable disponible ahí (contar_oscilaciones, model-free);
#   (3) barre el gate de entrada (entry_adx × cooldown) DENTRO de RANGO, con robustez por mitades,
#       para ver si aflojarlo captura más PnL realizado de forma robusta (=edge) o solo más
#       whipsaw/trabado (=el gate ya está bien). NO toca bot/DB/capital.
# =============================================================================
def contar_oscilaciones(closes, mask, umbrales=(2.0, 4.0, 7.0)):
    """Cuenta up-swings NO-solapados de ≥u% desde un valle local, SOLO en barras mask=True
    (segmentos contiguos; un hueco non-mask reinicia el valle). PURO, model-free: mide la
    oscilación cosechable cruda presente, independiente de la mecánica del grid."""
    out = {u: 0 for u in umbrales}
    trough = {u: None for u in umbrales}
    for i, px in enumerate(closes):
        if not mask[i]:
            for u in umbrales:
                trough[u] = None
            continue
        for u in umbrales:
            if trough[u] is None or px < trough[u]:
                trough[u] = px
            elif px >= trough[u] * (1 + u / 100.0):
                out[u] += 1
                trough[u] = px
    return out


def grid_rango_probe(dias):
    """V1.8.7 — sonda exhaustiva del hábitat del grid (RANGO). Aísla RANGO, mide la oscilación
    cruda disponible y barre el gate de entrada dentro de RANGO con robustez por mitades.
    Responde con datos: ¿el grid cosecha bien RANGO o el gate lo sub-despliega? NO escribe DB."""
    log.info(f"Grid RANGO probe (V1.8.7): {dias}d, 15m, aislando RANGO + barrido de gate")
    exchange = ccxt.binance({"enableRateLimit": True,
                             "options": {"defaultType": "spot", "adjustForTimeDifference": True}})
    est_map = _regimen_estado_por_ts(exchange, dias)
    def estado_de(ts_ms):
        day = (ts_ms // 86400000) * 86400000
        return est_map.get(day, None)
    dist = {}
    for e in est_map.values():
        dist[e] = dist.get(e, 0) + 1
    tot = max(1, len(est_map))
    print(f"\nDistribución de régimen diario BTC ({len(est_map)} días):")
    for e in ("TENDENCIA_UP", "RANGO", "TENDENCIA_DOWN", "CAOS"):
        print(f"  {e:>16}: {dist.get(e,0):>4}d ({100.0*dist.get(e,0)/tot:>4.1f}%)")

    UMBR = (2.0, 4.0, 7.0)
    # barrido de gate: (entry_adx, cooldown_bars). real = (20, 4). El resto afloja progresivamente.
    GATES = [(20.0, 4), (20.0, 2), (20.0, 1), (25.0, 4), (30.0, 4), (40.0, 2), (999.0, 1)]

    for sym in GRID_PARES:
        bars, ts = _bars_15m(exchange, sym, dias)
        if len(bars) < 300:
            print(f"\n{sym}: datos 15m insuficientes ({len(bars)})")
            continue
        rango_mask = [estado_de(t) == "RANGO" for t in ts]
        oper_mask = [estado_de(t) in ("RANGO", "TENDENCIA_UP") for t in ts]
        n_rango = sum(rango_mask)
        pct_rango = 100.0 * n_rango / len(bars)
        closes = [b["close"] for b in bars]
        osc = contar_oscilaciones(closes, rango_mask, UMBR)
        bh = (bars[-1]["close"] / bars[0]["close"] - 1.0) * 100
        print(f"\n=== {sym} ({len(bars)} barras 15m, {pct_rango:.0f}% en RANGO, buy&hold {bh:+.1f}%) ===")
        print(f"  Oscilación CRUDA cosechable en RANGO: "
              + "  ".join(f"≥{u:.0f}%: {osc[u]}" for u in UMBR)
              + "   (techo disponible, model-free)")
        if n_rango < 50:
            print(f"  ⚠ solo {n_rango} barras en RANGO — muestra fina, leer con cautela")
        mid = len(bars) // 2
        print(f"  {'gate(adx/cd)':>13}{'net%':>8}{'realiz%':>9}{'trab%':>8}{'entry':>6}{'harv':>6}"
              f"{'  | 1ª/2ª realiz%':>18}")
        for adx_g, cd in GATES:
            # SOLO RANGO: el grid solo puede operar en barras RANGO (gate más estricto que el live).
            r = sim_grid_intrabar(bars, rango_mask, entry_adx=adx_g, cooldown_bars=cd)
            r1 = sim_grid_intrabar(bars[:mid], rango_mask[:mid], entry_adx=adx_g, cooldown_bars=cd)
            r2 = sim_grid_intrabar(bars[mid:], rango_mask[mid:], entry_adx=adx_g, cooldown_bars=cd)
            etiq = f"{adx_g:.0f}/{cd}" + (" real" if (adx_g, cd) == (20.0, 4) else "")
            print(f"  {etiq:>13}{r['net_pct']:>8.2f}{r['realizado_pct']:>9.2f}"
                  f"{r['m2m_trabado_pct']:>8.2f}{r['n_entry']:>6}{r['n_harvest']:>6}"
                  f"{r1['realizado_pct']:>9.2f}/{r2['realizado_pct']:<8.2f}")
        # contraste: mismo grid pero operando RANGO+UP (el gate live real) vs RANGO-puro
        live = sim_grid_intrabar(bars, oper_mask, entry_adx=20.0, cooldown_bars=4)
        print(f"  {'(ref) RANGO+UP gate live':>25}: net {live['net_pct']:+.2f}% "
              f"realiz {live['realizado_pct']:+.2f}% entry {live['n_entry']} harv {live['n_harvest']}")

    print("\nLECTURA:")
    print("  • Si la oscilación CRUDA en RANGO es alta pero el gate real (20/4) cosecha pocos harvests")
    print("    → el gate deja plata: hay swing cosechable que no toca.")
    print("  • Si aflojar el gate (adx↑, cd↓) sube realiz% en AMBAS mitades → edge real, vale aflojar.")
    print("  • Si solo sube 'trab%'/whipsaw sin mejorar realiz% robusto → el gate ya está bien (no tocar).")
    print("  • net% incluye inventario trabado m2m; realiz% es la cosecha efectiva (lo que importa).")


# =============================================================================
# LADDER GRID (SP1, ladder_grid_spec.md) — el rediseño candidato: grid round-trip CLÁSICO.
# El grid actual ACUMULA (entra 1 vez, raspa 15%/tier, nunca cierra → toca <3% de la oscilación,
# V1.8.7). Un ladder OSCILA: compra abajo, vende las MISMAS unidades arriba (+spacing), re-arma,
# repite → cosecha cada swing. ESTO ES SOLO LA MEDICIÓN (SP1): mide si captura RANGO neto de fees y
# le gana al grid actual + buy&hold, robusto. El crux = FEES (muchos trades). NO toca bot/DB/capital.
# Si el edge sobrevive fees maker realistas → habilita SP2 (sleeve live aislado). Si no → descartar.
# =============================================================================
def sim_grid_ladder(bars, operable, spacing_pct=2.0, n_rungs=5, capital0=1000.0,
                    invest_per_rung_pct=0.04, fee=0.0008, atr_spacing=False,
                    flatten_on_exit=False, stop_pct=None, max_hold_bars=None,
                    inventory_skew=0.0, recenter_drift_pct=None, refresh_bars=None):
    """Simulador PURO del grid ladder round-trip, fiel a 15m, capital + fee constrained.
    bars: list de dict {close,high,low,atr_pct,...} (de _bars_15m). operable[i]: gate régimen.
    Escalera: ref se fija al ENTRAR a un segmento operable (re-centra los rungs ARMADOS); N rungs de
    compra en ref·(1−k·S); cada rung armado que toca su buy_px (low) compra invest_per_rung·capital0
    y coloca venta de esas unidades a buy_px·(1+S); cuando el high toca la venta → round-trip y re-arma.
    Conservador: un lote recién comprado NO vende en la misma barra (ventas se procesan antes que compras).
    En non-operable no arma compras nuevas; flatten_on_exit vende holdings a mercado al close.
    SP2: stop_pct (un holding underwater >stop_pct% desde su buy se corta a mercado) y max_hold_bars (un
    holding retenido ≥N barras se corta) acotan el inventario varado DENTRO del rango. Defaults None = SP1.
    SP4 (ladder_requote_spec.md): recenter_drift_pct / refresh_bars re-cotizan la escalera DENTRO del
    segmento operable (por deriva del precio o por tiempo) en vez de dejarla anclada al precio del día
    que empezó el RANGO. Ambos None ⟹ conducta bit-a-bit idéntica a SP2 (hay un canary vivo)."""
    if not bars:
        return {"net_pct": 0.0, "realizado_pct": 0.0, "inv_trabado_pct": 0.0, "n_roundtrips": 0,
                "n_buys": 0, "n_stops": 0, "cash_final": round(capital0, 2), "qty_final": 0.0}
    cash = capital0
    rungs = [{"buy_px": 0.0, "state": "armed", "qty": 0.0, "sell_px": 0.0, "bought_i": -1}
             for _ in range(n_rungs)]
    realizado = 0.0
    n_rt = n_buy = n_stop = 0
    prev_op = False

    def S_of(b):
        return spacing_pct * (_atr_mult(b["atr_pct"]) if atr_spacing else 1.0)

    def _skew_factor():
        # inventory skew (Hummingbot PMM): más inventario colgado → ventas más cerca del mid
        # (sell_mult<1) y compras más lejos (buy_mult>1). inventory_skew=0 ⟹ (1,1) = sin cambio.
        if inventory_skew <= 0.0:
            return 1.0, 1.0
        held = sum(1 for r in rungs if r["state"] == "holding")
        frac = held / n_rungs if n_rungs else 0.0
        adj = inventory_skew * frac
        return max(0.1, 1.0 - adj), 1.0 + adj

    def _vender(r, px_venta, rearm=True):
        # rearm=True (harvest/flatten): vuelve a 'armed' (re-compra en el próximo dip). rearm=False
        # (stop/max_hold): queda 'stopped' → NO re-compra en el mismo segmento (no caza el cuchillo);
        # se re-arma recién en el próximo re-centrado. Sin esto, el stop se anula re-comprando el mismo bar.
        nonlocal cash, realizado
        proceeds = r["qty"] * px_venta
        cash += proceeds * (1 - fee)
        realizado += proceeds * (1 - fee) - r["qty"] * r["buy_px"] * (1 + fee)
        r["state"] = "armed" if rearm else "stopped"
        r["qty"] = 0.0; r["sell_px"] = 0.0; r["bought_i"] = -1

    def _recentrar(px, S, incluir_stopped=True):
        # Reposiciona los rungs SIN inventario sobre el precio actual. Un 'holding' nunca se toca:
        # tiene su venta round-trip apuntada y moverla sería cambiar el trato a mitad de camino.
        _, buy_mult = _skew_factor()
        for k, r in enumerate(rungs, 1):
            if r["state"] == "armed" or (r["state"] == "stopped" and incluir_stopped):
                r["state"] = "armed"
                r["buy_px"] = px * (1 - k * S * buy_mult / 100.0)

    ref = 0.0            # precio del último centrado (SP4)
    ultimo_refresh = -1

    for i, b in enumerate(bars):
        op = operable[i]
        px, hi, lo = b["close"], b["high"], b["low"]
        S = S_of(b)
        # --- re-centrar rungs sin posición (armed/stopped) al entrar a un segmento operable ---
        if op and not prev_op:
            _recentrar(px, S)
            ref, ultimo_refresh = px, i
        # --- SP4: re-cotizar DENTRO del segmento (por deriva o por tiempo) ---
        elif op and ref > 0.0:
            deriva = abs(px / ref - 1.0) * 100.0
            por_deriva = recenter_drift_pct is not None and deriva >= recenter_drift_pct
            por_tiempo = refresh_bars is not None and (i - ultimo_refresh) >= refresh_bars
            if por_deriva or por_tiempo:
                # Un rung 'stopped' solo vuelve al ruedo si el re-centrado va hacia ARRIBA: hacia
                # abajo sería re-comprar lo que el stop de SP2 acaba de cortar (cazar el cuchillo).
                _recentrar(px, S, incluir_stopped=(px > ref))
                ref, ultimo_refresh = px, i
        # --- VENTAS primero (holdings cuyo sell_px ≤ high); los lotes recién comprados esperan ---
        for r in rungs:
            if r["state"] == "holding" and r["sell_px"] > 0 and hi >= r["sell_px"]:
                _vender(r, r["sell_px"]); n_rt += 1
        # --- SP2: stop a rungs colgados (underwater por low, o zombi por tiempo). Harvest tiene
        # prioridad arriba (target > buy); el stop vive abajo (buy·(1−stop)) → no se solapan. ---
        for r in rungs:
            if r["state"] != "holding":
                continue
            if stop_pct is not None and lo <= r["buy_px"] * (1 - stop_pct / 100.0):
                _vender(r, r["buy_px"] * (1 - stop_pct / 100.0), rearm=False); n_stop += 1
            elif max_hold_bars is not None and r["bought_i"] >= 0 and (i - r["bought_i"]) >= max_hold_bars:
                _vender(r, px, rearm=False); n_stop += 1
        if not op:
            if flatten_on_exit:
                for r in rungs:
                    if r["state"] == "holding":
                        _vender(r, px)
            prev_op = op
            continue
        # --- COMPRAS (rungs armados cuyo buy_px tocó el low) ---
        for r in rungs:
            if r["state"] == "armed" and r["buy_px"] > 0 and lo <= r["buy_px"]:
                monto = invest_per_rung_pct * capital0
                if 5.0 <= monto <= cash:
                    qty = monto / r["buy_px"]
                    cash -= monto * (1 + fee)
                    sell_mult, _ = _skew_factor()
                    r["state"] = "holding"; r["qty"] = qty
                    r["sell_px"] = r["buy_px"] * (1 + S * sell_mult / 100.0); r["bought_i"] = i
                    n_buy += 1
        prev_op = op

    ult = bars[-1]["close"]
    hold = [r for r in rungs if r["state"] == "holding"]
    m2m = sum(r["qty"] * (ult - r["buy_px"]) for r in hold)
    equity = cash + sum(r["qty"] * ult for r in hold)
    return {
        "net_pct": round((equity - capital0) / capital0 * 100, 2),
        "realizado_pct": round(realizado / capital0 * 100, 2),
        "inv_trabado_pct": round(m2m / capital0 * 100, 2),
        "n_roundtrips": n_rt, "n_buys": n_buy, "n_stops": n_stop,
        "cash_final": round(cash, 2), "qty_final": round(sum(r["qty"] for r in hold), 8),
    }


LADDER_SPACINGS = [(1.0, False), (2.0, False), (3.0, False), (2.0, True)]   # (spacing_pct, atr)
LADDER_NRUNGS = [3, 5, 8]


def ladder_sweep(dias):
    """SP1 — mide el grid ladder round-trip en RANGO vs grid actual + buy&hold, neto de fees.
    Barre spacing × n_rungs a fee maker; muestra sensibilidad a fee taker del mejor combo; robustez
    por mitades. NO escribe DB. Decide si el rediseño tiene edge antes de tocar capital."""
    log.info(f"Ladder sweep (SP1): {dias}d, 15m, RANGO-only, fee maker vs taker")
    exchange = ccxt.binance({"enableRateLimit": True,
                             "options": {"defaultType": "spot", "adjustForTimeDifference": True}})
    est_map = _regimen_estado_por_ts(exchange, dias)
    def es_rango(ts_ms):
        return est_map.get((ts_ms // 86400000) * 86400000, None) == "RANGO"

    FEE_MAKER, FEE_TAKER = 0.0008, 0.0010
    for sym in GRID_PARES:
        bars, ts = _bars_15m(exchange, sym, dias)
        if len(bars) < 300:
            print(f"\n{sym}: datos 15m insuficientes ({len(bars)})")
            continue
        rango = [es_rango(t) for t in ts]
        n_rango = sum(rango)
        bh = (bars[-1]["close"] / bars[0]["close"] - 1.0) * 100
        # baseline: grid ACTUAL en RANGO (mismo gate)
        base = sim_grid_intrabar(bars, rango)
        print(f"\n=== {sym} ({len(bars)} barras 15m, {100.0*n_rango/len(bars):.0f}% RANGO, "
              f"buy&hold {bh:+.1f}%) ===")
        print(f"  baseline grid ACTUAL (RANGO): realiz {base['realizado_pct']:+.2f}%  "
              f"net {base['net_pct']:+.2f}%  (harv {base['n_harvest']}, entry {base['n_entry']})")
        if n_rango < 50:
            print(f"  ⚠ solo {n_rango} barras RANGO — muestra fina")
        mid = len(bars) // 2
        print(f"  LADDER @ fee maker {FEE_MAKER*100:.2f}%:")
        print(f"  {'spacing':>9}{'rungs':>6}{'net%':>8}{'realiz%':>9}{'trab%':>8}"
              f"{'rt':>5}{'buys':>6}{'  realiz 1ª/2ª':>16}")
        mejor = (-1e9, None)
        for sp, atr in LADDER_SPACINGS:
            for nr in LADDER_NRUNGS:
                r = sim_grid_ladder(bars, rango, spacing_pct=sp, n_rungs=nr, atr_spacing=atr, fee=FEE_MAKER)
                r1 = sim_grid_ladder(bars[:mid], rango[:mid], spacing_pct=sp, n_rungs=nr, atr_spacing=atr, fee=FEE_MAKER)
                r2 = sim_grid_ladder(bars[mid:], rango[mid:], spacing_pct=sp, n_rungs=nr, atr_spacing=atr, fee=FEE_MAKER)
                lbl = (f"atr×{sp:.0f}" if atr else f"{sp:.0f}%")
                print(f"  {lbl:>9}{nr:>6}{r['net_pct']:>8.2f}{r['realizado_pct']:>9.2f}"
                      f"{r['inv_trabado_pct']:>8.2f}{r['n_roundtrips']:>5}{r['n_buys']:>6}"
                      f"{r1['realizado_pct']:>7.2f}/{r2['realizado_pct']:<8.2f}")
                if r['realizado_pct'] > mejor[0]:
                    mejor = (r['realizado_pct'], (sp, atr, nr, r1['realizado_pct'], r2['realizado_pct']))
        sp, atr, nr, m1, m2 = mejor[1]
        tk = sim_grid_ladder(bars, rango, spacing_pct=sp, n_rungs=nr, atr_spacing=atr, fee=FEE_TAKER)
        lbl = (f"atr×{sp:.0f}" if atr else f"{sp:.0f}%")
        print(f"  → mejor realiz: spacing {lbl} rungs {nr} = {mejor[0]:+.2f}% (1ª {m1:+.2f}/2ª {m2:+.2f}); "
              f"a fee taker {FEE_TAKER*100:.2f}%: realiz {tk['realizado_pct']:+.2f}%")

    print("\nGATE DE DECISIÓN (SP1 → SP2):")
    print("  Habilita el sleeve live SOLO si, a fee MAKER, el realiz% del ladder es robustamente")
    print("  positivo (ambas mitades, ≥2 de 3 pares) Y supera materialmente al grid actual Y NO")
    print("  depende de fee taker→maker irreal. Si se evapora con fees o no es robusto → descartar.")


# =============================================================================
# LADDER SCAN (SP2, ladder_grid_sp2_spec.md) — ¿existe una config del ladder robustamente net-positiva?
# SP1 halló cosecha real pero net dragueado por inventario varado (falla ETH = se gateó por BTC pese al
# declive propio de ETH). SP2 ataca con: (1) gate por el régimen PROPIO del activo (un declive secular
# = TENDENCIA_DOWN en su marco → congela solo), (2) stop a rungs colgados, (3) selección de activo
# (scan multi-par). Decisión binaria: ≥1-2 pares net+ robusto POR COSECHA (no beta) → SP3 (sleeve live).
# NO toca bot/DB/capital.
# =============================================================================
LADDER_UNIVERSE = ["BTC/USDT", "ETH/USDT", "TRX/USDT", "SOL/USDT", "BNB/USDT",
                   "XRP/USDT", "LTC/USDT", "ADA/USDT"]
LADDER_SCAN_SPACINGS = [1.0, 2.0]
LADDER_SCAN_RUNGS = [5, 8]
LADDER_SCAN_STOPS = [8.0, 15.0]


def ladder_scan(dias, pares=None):
    """SP2 — corre el ladder (gate régimen PROPIO del activo + stop a colgados + flatten) sobre un
    universo de pares, busca config net-positiva robusta (ambas mitades) POR COSECHA (no beta).
    Decide si existe terreno para SP3 (sleeve live). NO escribe DB."""
    universo = pares or LADDER_UNIVERSE
    log.info(f"Ladder scan (SP2): {dias}d, {len(universo)} pares, gate régimen-propio + stop")
    exchange = ccxt.binance({"enableRateLimit": True,
                             "options": {"defaultType": "spot", "adjustForTimeDifference": True}})
    FEE = 0.0008
    ganadores = []
    for sym in universo:
        try:
            bars, ts = _bars_15m(exchange, sym, dias)
            est = _regimen_estado_por_ts(exchange, dias, sym=sym)   # régimen PROPIO del activo
        except Exception as e:
            print(f"\n{sym}: error de datos ({e})"); continue
        if len(bars) < 300:
            print(f"\n{sym}: datos 15m insuficientes ({len(bars)})"); continue
        def es_rango(t): return est.get((t // 86400000) * 86400000, None) == "RANGO"
        rango = [es_rango(t) for t in ts]
        pct_r = 100.0 * sum(rango) / len(bars)
        bh = (bars[-1]["close"] / bars[0]["close"] - 1.0) * 100
        mid = len(bars) // 2
        # mejor config: prioriza robusto (net ambas mitades >0) y luego net total
        mejor = None   # (robusto, net, dict)
        for sp in LADDER_SCAN_SPACINGS:
            for nr in LADDER_SCAN_RUNGS:
                for st in LADDER_SCAN_STOPS:
                    kw = dict(spacing_pct=sp, n_rungs=nr, stop_pct=st, flatten_on_exit=True, fee=FEE)
                    r = sim_grid_ladder(bars, rango, **kw)
                    r1 = sim_grid_ladder(bars[:mid], rango[:mid], **kw)
                    r2 = sim_grid_ladder(bars[mid:], rango[mid:], **kw)
                    robusto = (r1["net_pct"] > 0 and r2["net_pct"] > 0 and r["realizado_pct"] > 0)
                    cand = (robusto, r["net_pct"], {"sp": sp, "nr": nr, "st": st, "net": r["net_pct"],
                            "realiz": r["realizado_pct"], "trab": r["inv_trabado_pct"],
                            "n1": r1["net_pct"], "n2": r2["net_pct"], "rt": r["n_roundtrips"],
                            "stops": r["n_stops"]})
                    if mejor is None or cand[:2] > mejor[:2]:
                        mejor = cand
        rob, _, m = mejor
        # "por cosecha no beta": net+ Y realiz domina el net (no es solo inventario que apreció)
        cosecha = m["realiz"] > 0 and m["realiz"] >= m["trab"]
        flag = "ROBUSTO✓" if (rob and cosecha) else ("net+ pero beta" if rob else "no robusto✗")
        print(f"\n=== {sym} (bh {bh:+.1f}%, {pct_r:.0f}% RANGO-propio) ===")
        print(f"  mejor: spacing {m['sp']:.0f}% rungs {m['nr']} stop {m['st']:.0f}% → "
              f"net {m['net']:+.2f}% (1ª {m['n1']:+.2f}/2ª {m['n2']:+.2f}) "
              f"realiz {m['realiz']:+.2f}% trab {m['trab']:+.2f}% rt {m['rt']} stops {m['stops']}  [{flag}]")
        if rob and cosecha:
            ganadores.append((sym, m))

    print(f"\n{'='*72}\nVEREDICTO SP2 → SP3:")
    if ganadores:
        print(f"  {len(ganadores)} par(es) con config net-positiva ROBUSTA por cosecha:")
        for sym, m in ganadores:
            print(f"    {sym}: net {m['net']:+.2f}% (1ª {m['n1']:+.2f}/2ª {m['n2']:+.2f}), "
                  f"realiz {m['realiz']:+.2f}% > trab {m['trab']:+.2f}%")
        print("  → HABILITA SP3 (sleeve live aislado) sobre el/los par(es) ganador(es).")
    else:
        print("  NINGÚN par logró net-positivo robusto por cosecha → DESCARTAR el ladder")
        print("  (se suma al linaje medir-y-descartar; fue el que más cerca estuvo).")
    print("  Recordá: robusto = net>0 en AMBAS mitades; cosecha = realiz domina (no beta del activo).")


# =============================================================================
# RÉGIMEN LONG/FLAT (regimen_longflat_spec.md) — prepararse para el cambio de régimen.
# Earn-en-bajista + hold-en-alcista: holdea el activo cuando el régimen PROPIO es operable (UP+RANGO),
# si no está en USDT rindiendo earn. CAUSAL (decide con el régimen de ayer). Mide robustez (walk-forward
# multi-activo) y el ONSET del bull (lag de entrada + % del up-leg capturado). NO toca bot/DB/capital.
# Prototipo dio +23% vs buy&hold -6% vs earn +11% en el ciclo del run a $124k — esto decide si es real.
# =============================================================================
LONGFLAT_UNIVERSE = ["BTC/USDT", "ETH/USDT", "TRX/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "LTC/USDT", "ADA/USDT"]
EARN_DAILY_DEFAULT = 0.05 / 365.0     # ~5% APR Simple Earn USDT


def sim_longflat(closes, states, long_states, fee=0.001, earn_daily=EARN_DAILY_DEFAULT):
    """PURO. Long/flat por régimen, causal: día i va long si states[i-1] ∈ long_states, si no earn.
    Cada switch paga fee. Devuelve {net_pct, n_switches, max_dd, pct_long, fin_cripto}."""
    if len(closes) < 2:
        return {"net_pct": 0.0, "n_switches": 0, "max_dd": 0.0, "pct_long": 0.0, "fin_cripto": False}
    eq, pos, switches, dias_long, peak, max_dd = 1.0, False, 0, 0, 1.0, 0.0
    for i in range(1, len(closes)):
        want = states[i - 1] in long_states
        if want != pos:
            eq *= (1 - fee); switches += 1; pos = want
        if pos:
            eq *= closes[i] / closes[i - 1] if closes[i - 1] else 1.0; dias_long += 1
        else:
            eq *= (1 + earn_daily)
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak if peak > 0 else 0.0)
    return {"net_pct": round((eq - 1) * 100, 2), "n_switches": switches,
            "max_dd": round(max_dd * 100, 2), "pct_long": round(100 * dias_long / (len(closes) - 1), 1),
            "fin_cripto": pos}


def _onset_captura(closes, states, long_states, lookback=20):
    """PURO. Por cada transición flat→long (onset del bull), mide: lag desde el mínimo de la ventana
    previa (qué tan tarde entra tras el piso) y captured_pct (lo que agarró en el período long) vs
    full_pct (piso→máximo del período). Lista de dicts. Responde '¿qué pasa cuando el alcista llega?'."""
    onsets, pos, entry_i = [], False, None

    def _cerrar(end_i):
        seg = closes[entry_i:end_i + 1]
        if entry_i is None or len(seg) < 2:
            return
        a = max(0, entry_i - lookback)
        prev = closes[a:entry_i + 1]
        lo = min(prev); lag = entry_i - (a + prev.index(lo))
        hi = max(seg)
        full = (hi / lo - 1) * 100 if lo else 0.0
        captured = (closes[end_i] / closes[entry_i] - 1) * 100 if closes[entry_i] else 0.0
        onsets.append({"lag": lag, "full_pct": round(full, 1), "captured_pct": round(captured, 1)})

    for i in range(1, len(closes)):
        want = states[i - 1] in long_states
        if want and not pos:
            entry_i, pos = i, True
        elif not want and pos:
            _cerrar(i); pos = False
    if pos:
        _cerrar(len(closes) - 1)
    return onsets


def regimen_longflat(dias, pares=None):
    """SP1 — simula earn-bajista+hold-alcista (long/flat por régimen propio) sobre multi-activo,
    walk-forward por sub-períodos, vs buy&hold + earn-siempre, + análisis del onset del bull.
    Decide si el +23% del prototipo es robusto o artefacto de un ciclo. NO escribe DB."""
    universo = pares or LONGFLAT_UNIVERSE
    log.info(f"Régimen long/flat (SP1): {dias}d, {len(universo)} activos, walk-forward + onset")
    exchange = ccxt.binance({"enableRateLimit": True,
                             "options": {"defaultType": "spot", "adjustForTimeDifference": True}})
    OPERABLE = {"TENDENCIA_UP", "RANGO"}
    UP = {"TENDENCIA_UP"}
    ganadores = 0
    evaluados = 0
    for sym in universo:
        try:
            est = _regimen_estado_por_ts(exchange, dias, sym=sym)
            velas = exchange.fetch_ohlcv(sym, "1d", limit=min(1000, dias + 260))
        except Exception as e:
            print(f"\n{sym}: error de datos ({e})"); continue
        rows = sorted((int(v[0]), v[4]) for v in velas if int(v[0]) in est)
        if len(rows) < 120:
            print(f"\n{sym}: datos insuficientes ({len(rows)})"); continue
        closes = [c for _, c in rows]
        states = [est[t] for t, _ in rows]
        n = len(closes)
        bh = (closes[-1] / closes[0] - 1) * 100
        earn = ((1 + EARN_DAILY_DEFAULT) ** (n - 1) - 1) * 100
        op = sim_longflat(closes, states, OPERABLE)
        up = sim_longflat(closes, states, UP)
        # walk-forward: tercios (la variante operable)
        t = n // 3
        tercios = [sim_longflat(closes[a:b], states[a:b], OPERABLE)["net_pct"]
                   for a, b in [(0, t), (t, 2 * t), (2 * t, n)]]
        on = _onset_captura(closes, states, OPERABLE)
        lag_med = sorted(o["lag"] for o in on)[len(on) // 2] if on else 0
        cap_med = sorted(o["captured_pct"] for o in on)[len(on) // 2] if on else 0.0
        # gana = operable supera a buy&hold Y a earn en el total Y en ≥2 de 3 tercios positivos
        gana = op["net_pct"] > bh and op["net_pct"] > earn
        evaluados += 1
        if gana and sum(1 for x in tercios if x > 0) >= 2:
            ganadores += 1
        flag = "GANA✓" if gana else "no"
        print(f"\n=== {sym} ({n}d, buy&hold {bh:+.0f}%, earn {earn:+.0f}%) ===")
        print(f"  long/flat OPERABLE(UP+RANGO): net {op['net_pct']:+.0f}%  dd {op['max_dd']:.0f}%  "
              f"sw {op['n_switches']}  %long {op['pct_long']:.0f}  fin {'CRIPTO' if op['fin_cripto'] else 'USDT'}  [{flag}]")
        print(f"  long/flat solo UP:           net {up['net_pct']:+.0f}%  dd {up['max_dd']:.0f}%  sw {up['n_switches']}")
        print(f"  walk-forward (tercios, operable): {tercios[0]:+.0f}% / {tercios[1]:+.0f}% / {tercios[2]:+.0f}%")
        print(f"  onset del bull: {len(on)} entradas, lag mediano {lag_med}d, captura mediana {cap_med:+.0f}% del up-leg")

    print(f"\n{'='*72}\nVEREDICTO SP1:")
    print(f"  {ganadores}/{evaluados} activos: long/flat operable le gana a buy&hold Y a earn, "
          f"robusto (≥2/3 tercios positivos).")
    if ganadores >= max(2, evaluados // 2):
        print("  → SEÑAL ROBUSTA: el +23% no fue un solo ciclo. Habilita SP2 (gestor de posición live).")
    else:
        print("  → NO robusto cross-asset/tiempo: el +23% fue mayormente el ciclo de BTC. NO ir a SP2;")
        print("    el bot se queda como está (earn-en-bear correcto). El onset igual informa qué esperar.")


# =============================================================================
# CLI
# =============================================================================
def main():
    p = argparse.ArgumentParser(description="Pipeline ML Quantum Unified Bot")
    p.add_argument("--solo-mining", action="store_true", help="Solo extrae features, no entrena")
    p.add_argument("--solo-train", action="store_true", help="Solo entrena desde DB existente")
    p.add_argument("--shadow-backfill", action="store_true",
                   help="Replay histórico del gestor de salida del short (siembra short_shadow_pnl)")
    p.add_argument("--barrer-senal", action="store_true",
                   help="Barre variantes de senal_short offline: trades vs PnL baseline (no escribe DB)")
    p.add_argument("--regimen-backfill", action="store_true",
                   help="Audita el Cerebro de Régimen 4-estados sobre histórico BTC diario (no escribe DB)")
    p.add_argument("--grid-sweep", action="store_true",
                   help="Opción ③: barre geometría dip×tp del grid por régimen, neto de fees (no escribe DB)")
    p.add_argument("--grid-intrabar", action="store_true",
                   help="Opción ③ FIEL: simulador 15m del DCA+ratchet real, barre ancho de tiers (no escribe DB)")
    p.add_argument("--grid-rango", action="store_true",
                   help="Sonda RANGO (V1.8.7): aísla RANGO, mide oscilación cruda + barre gate de entrada (no escribe DB)")
    p.add_argument("--ladder-sweep", action="store_true",
                   help="SP1: mide el grid ladder round-trip en RANGO vs grid actual + buy&hold, neto de fees (no escribe DB)")
    p.add_argument("--ladder-scan", action="store_true",
                   help="SP2: scan multi-par del ladder con gate régimen-propio + stop; ¿existe config net+ robusta? (no escribe DB)")
    p.add_argument("--regimen-longflat", action="store_true",
                   help="SP1: simula earn-bajista+hold-alcista (long/flat por régimen), walk-forward + onset del bull (no escribe DB)")
    p.add_argument("--sleeve-backfill", action="store_true",
                   help="Mide el Sleeve Trend-Ride (Fase 1): buy-and-ride en TENDENCIA_UP, barre k (no escribe DB)")
    p.add_argument("--horizonte", type=int, default=5, help="Días forward para la auditoría de régimen (default 5)")
    p.add_argument("--dias", type=int, default=60, help="Días de histórico a minar (default 60)")
    p.add_argument("--pares", type=str, default=None,
                   help="Pares custom CSV, ej: 'BTC,ETH' (sin /USDT). Default: 30 pares.")
    p.add_argument("--db", type=str, default=DB_NAME_DEFAULT, help=f"Path SQLite (default {DB_NAME_DEFAULT})")
    args = p.parse_args()

    if args.solo_mining and args.solo_train:
        p.error("--solo-mining y --solo-train son mutuamente excluyentes")

    pares = PARES_DEFAULT
    if args.pares:
        pares = [f"{x.strip().upper()}/USDT" for x in args.pares.split(",") if x.strip()]

    log.info(f"DB: {args.db} | pares: {len(pares)} | días: {args.dias}")

    if args.shadow_backfill:
        backfill_shadow(args.db, args.dias)
        return

    if args.barrer_senal:
        barrer_senal(args.db, args.dias)
        return

    if args.regimen_backfill:
        regimen_backfill(args.dias, args.horizonte)
        return

    if args.sleeve_backfill:
        sleeve_backfill(args.dias)
        return

    if args.grid_sweep:
        grid_sweep(args.dias)
        return

    if args.grid_intrabar:
        grid_intrabar(args.dias)
        return

    if args.grid_rango:
        grid_rango_probe(args.dias)
        return

    if args.ladder_sweep:
        ladder_sweep(args.dias)
        return

    if args.ladder_scan:
        pares = None
        if args.pares:
            pares = [f"{x.strip().upper()}/USDT" for x in args.pares.split(",") if x.strip()]
        ladder_scan(args.dias, pares)
        return

    if args.regimen_longflat:
        pares = None
        if args.pares:
            pares = [f"{x.strip().upper()}/USDT" for x in args.pares.split(",") if x.strip()]
        regimen_longflat(args.dias, pares)
        return

    if not args.solo_train:
        nuevos = minar(args.db, pares, args.dias)
        if nuevos == 0 and not args.solo_mining:
            log.warning("Mining no añadió muestras. ¿Ya estaba al día?")

    if not args.solo_mining:
        ok = entrenar(args.db)
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
