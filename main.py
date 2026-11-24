# =============================================================================
# QUANTUM UNIFIED BOT V1.0 — main.py
# Monobot: Binance Spot + Binance Futuros + OKX Spot + Funding Arb
# Ver bot.txt para spec completa. Actualizar bot.txt en cada cambio.
# =============================================================================

import os
import sys
import time
import json
import random
import hmac
import hashlib
import signal
import sqlite3
import uuid
import threading
import logging
import logging.handlers
import collections
from datetime import datetime, timedelta, timezone
from typing import Optional

import joblib
import requests
from dotenv import load_dotenv

import ccxt
try:
    import ccxt.pro as ccxtpro
except Exception:
    ccxtpro = None

# Parche ccxt (2026-06-11): load_markets() revienta con
# "'<' not supported between instances of 'NoneType' and 'str'" cuando OKX lista un
# instrumento con id=None (keysort ordena markets_by_id y falla al comparar None con str).
# Lo hacemos None-safe. Sin esto, get_balance / WS / MotorIndicadores OKX fallan de a ratos.
# No es bug nuestro (quirk del lado de OKX); afecta a todos los exchanges ccxt del bot.
# ccxt.Exchange es la clase base (sync+async la comparten) → un parche cubre todo.
def _keysort_none_safe(dictionary):
    return dict(sorted(dictionary.items(), key=lambda kv: (kv[0] is None, str(kv[0]))))


ccxt.Exchange.keysort = staticmethod(_keysort_none_safe)

import pandas as pd
import pandas_ta as ta
import numpy as np

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout

console = Console()


# =============================================================================
# 0. CONFIG — load .env into a single dict
# =============================================================================
load_dotenv()


def _env_bool(name, default=False):
    v = os.getenv(name, str(default)).strip().lower()
    return v in ("1", "true", "yes", "on")


def _env_float(name, default=0.0):
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


def _env_json(name, default):
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


CONFIG = {
    # Sistema / Riesgo común
    "kill_switch":            _env_bool("KILL_SWITCH", False),
    "max_daily_loss_pct":     float(os.getenv("MAX_DAILY_LOSS_PCT", 3.0)),
    "liq_buffer_pct":         float(os.getenv("LIQ_BUFFER_PCT", 5.0)),
    "sl_confirmation_min":    int(os.getenv("SL_CONFIRMATION_MINUTES", 3)),
    "wallet_bridge_min_usdt": float(os.getenv("WALLET_BRIDGE_MIN_USDT", 10.0)),
    "price_freshness_max_s":  float(os.getenv("PRICE_FRESHNESS_MAX_S", 10.0)),
    "hud_refresh_seconds":    float(os.getenv("HUD_REFRESH_SECONDS", 3.0)),
    # V1.3.1 HUD modo journalctl/systemd:
    #   "auto" (default) → tty si stdout es terminal, log si está pipeado (systemd/journalctl)
    #   "tty"  → siempre rich Live layout (clear+repaint)
    #   "log"  → snapshots plain-text greppables, sin escapes, prefijo "[HUD]"
    "hud_mode":               os.getenv("HUD_MODE", "auto").strip().lower(),
    "hud_log_interval_s":     float(os.getenv("HUD_LOG_INTERVAL_SECONDS", 60.0)),
    "db_name":                os.getenv("DB_NAME", "quantum.db"),
    "liquidate_only":         _env_bool("LIQUIDATE_ONLY", True),
    "entry_lock_timeout_ms":  int(os.getenv("ENTRY_LOCK_TIMEOUT_MS", 50)),

    # Pata Bajista V1.7.0 (EscuadronShortOkx) — futuros OKX direccional disciplinado
    "fut_enabled":            _env_bool("OKX_FUT_ENABLED", False),
    "short_max_equity_pct":   _env_float("SHORT_MAX_EQUITY_PCT", 0.18),
    "short_leverage":         int(_env_float("SHORT_LEVERAGE", 3)),
    "short_sl_pct":           _env_float("SHORT_SL_PCT", 0.04),
    "short_tp_pct":           _env_float("SHORT_TP_PCT", 0.03),
    "short_max_posiciones":   int(_env_float("SHORT_MAX_POSICIONES", 2)),
    "short_daily_loss_cap_usd": _env_float("SHORT_DAILY_LOSS_CAP_USD", 60.0),
    "short_canary_mode":      _env_bool("SHORT_CANARY_MODE", True),
    # Fase 2: gestor que gradúa la política ganadora del shadow a capital real.
    # shadow(default)=sin gestión dinámica; live=auto-selecciona; <nombre>=pin manual.
    "short_gestor_mode":      os.getenv("SHORT_GESTOR_MODE", "shadow"),
    "short_gestor_min_trades": int(_env_float("SHORT_GESTOR_MIN_TRADES", 30)),
    "short_gestor_margen_pct": _env_float("SHORT_GESTOR_MARGEN_PCT", 0.2),
    # Gate del meta: 'apareado' (t apareado + n efectivo + LCB, recomendado) | 'margen' (simple).
    "short_meta_gate":        os.getenv("SHORT_META_GATE", "apareado"),

    # Cerebro de Régimen 4-estados V1.8.0 — gobierna el Grid (cuándo opera/congela + sizing).
    # shadow(default)=computa y loguea pero el grid sigue con el binario modo_mercado (cero cambio);
    # live=el grid consume regimen_mercado (gate por estado + sizing por confianza).
    "regimen_brain_mode":     os.getenv("REGIMEN_BRAIN_MODE", "shadow").strip().lower(),
    "regimen_conf_min_mult":  _env_float("REGIMEN_CONF_MIN_MULT", 0.6),
    "regimen_conf_max_mult":  _env_float("REGIMEN_CONF_MAX_MULT", 1.2),
    "regimen_factor_rango":   _env_float("REGIMEN_FACTOR_RANGO", 1.0),
    "regimen_factor_tend_up": _env_float("REGIMEN_FACTOR_TEND_UP", 0.7),

    # Auditoría 6 bots OKX — features extraídos
    # Pánico Global BTC (okx_dca V12): bloquea compras agresivas tras caída BTC en 1h
    "panic_drop_pct":         float(os.getenv("PANIC_DROP_PCT", 3.0)),
    "panic_block_minutes":    int(os.getenv("PANIC_BLOCK_MINUTES", 30)),
    # DCA rebote confirmado (okx_dca V12): anti-cuchillo-cayendo, espera rebote desde local_bottom
    "dca_rebound_pct":        float(os.getenv("DCA_REBOUND_PCT", 1.0)),
    # Trailing drop adicional (okx_dca V12): retroceso desde local_top para soltar Ratchet
    "trailing_drop_pct":      float(os.getenv("TRAILING_DROP_PCT", 2.0)),
    # Anti-asfixia BE (okx_gemini V11.5): buffer mínimo sobre BE para fees
    "be_safety_buffer_pct":   float(os.getenv("BE_SAFETY_BUFFER_PCT", 0.2)),
    # Bóveda automática (okx_gemini V11.5): % de PnL realizado +OKX que se barre a Funding
    "vault_pnl_threshold":    float(os.getenv("VAULT_PNL_THRESHOLD_USDT", 0.10)),
    "vault_pnl_frac":         float(os.getenv("VAULT_PNL_FRAC", 1.0)),

    # Auditoría binance_antiguo + kucoin_antiguo (V1.2)
    # TP Parcial 50% (kucoin_antiguo V13): asegura 50% al primer tier, free ride en resto
    "tp_parcial_pct":         float(os.getenv("TP_PARCIAL_PCT", 1.5)),
    # Killswitch volatilidad per-pair (binance_antiguo V14): bloquea entrada si ATR irracional
    # V1.4.0: aplica solo a EscuadronGridBin/Okx (BUY maker) — MartilloEliteFut removido.
    "vol_spike_mult":         float(os.getenv("VOL_SPIKE_MULT", 3.0)),
    "vol_spike_window":       int(os.getenv("VOL_SPIKE_WINDOW", 50)),

    # V1.4.1 — Mejoras estrategias (5 mejoras opt-in según filosofía central).
    # Mejora #1: OBI gate opcional para Grid BUY maker.
    # Skip BUY si OBI ≤ MIN (presión vendedora institucional dominante en orderbook).
    # Reduce trampas: spoofing fakes ya están descartados por maker order (solo
    # llenamos si alguien VENDE contra nosotros); este gate evita comprar cuando
    # la presión vendedora indica que el precio podría seguir bajando.
    "grid_obi_gate_enabled":  _env_bool("GRID_OBI_GATE_ENABLED", False),
    "grid_obi_min":           float(os.getenv("GRID_OBI_MIN", -0.20)),
    # Mejora #2: Harvest tiers adaptativos según ATR%.
    # ATR alta (>4%) → tiers × 1.4 (más espacio para volatilidad)
    # ATR baja (<1%) → tiers × 0.75 (acerca tiers, captura ganancias menores)
    "harvest_atr_adaptive":   _env_bool("HARVEST_ATR_ADAPTIVE", True),
    "harvest_atr_high_pct":   float(os.getenv("HARVEST_ATR_HIGH_PCT", 4.0)),
    "harvest_atr_low_pct":    float(os.getenv("HARVEST_ATR_LOW_PCT", 1.0)),
    "harvest_high_mult":      float(os.getenv("HARVEST_HIGH_MULT", 1.4)),
    "harvest_low_mult":       float(os.getenv("HARVEST_LOW_MULT", 0.75)),
    # Mejora #3: Cooldown dinámico según ATR (per-symbol).
    # cd_eff = base_cd × clamp(0.5 + atr_pct × 0.3, 0.5, 2.0)
    "cooldown_atr_adaptive":  _env_bool("COOLDOWN_ATR_ADAPTIVE", True),
    # Mejora #4: Métricas yield/APY por motor (helper SQL, no flag).
    # Mejora #5: Dashboard spot vs HODL (usa OKX_BOT_GENESIS_*, no flag).
    "hodl_dashboard_enabled": _env_bool("HODL_DASHBOARD_ENABLED", True),
    # V1.4.0 REMOVIDOS: fut_ttl_hours/min_pnl_pct (TTL Guillotina FUT — OmniRiskFut removido),
    # fut_loss_cooldown_min (CentinelaFut removido).

    # V1.5.0 — ML como modulador de sizing en Grid BUY (sin SL forzado).
    # Filosofía: ML solo escala CUÁNTO comprar dentro del rango [MIN_MULT, MAX_MULT].
    # Los gates técnicos (ADX<20, close>EMA200, BAJISTA, panic, vol_spike, OBI) son
    # veto previo obligatorio — ML nunca decide solo apertura ni cierre.
    # Modelo: xgboost_oracle.joblib, features [rsi, adx, ema_dist, macd, vol_ratio].
    # Label entrenamiento: precio toca +1.2% antes de -2% en 24 velas 15m (horizonte 6h).
    # Dataset: entrenar.py (mining histórico OHLCV). Reentrenar con
    # `python entrenar.py --dias 90` y main.py hot-reloadea por mtime.
    # V1.8.3 (2026-06-21): NEUTRALIZADO por defecto (default True→False). Medido con datos
    # (144K muestras, 3 meses, walk-forward cronológico): AUC = 0.495 ± 0.020 = moneda al aire.
    # Las features locales NO predicen el label; BTC-lead tampoco salva (0.489 ± 0.066 en 5 folds,
    # el 0.551 de 1 fold fue suerte). El ml_size_multiplier dimensionaba sobre RUIDO → con el flag
    # off devuelve mult=1.0 (sizing neutro). Plumbing intacto y revivible (ML_SIZING_ENABLED=true)
    # SI alguna vez aparece señal real. Ver memoria ml-predictivo-muerto / bot.txt V1.8.3.
    "ml_sizing_enabled":      _env_bool("ML_SIZING_ENABLED", False),
    "ml_min_prob_floor":      float(os.getenv("ML_MIN_PROB_FLOOR", 0.40)),
    "ml_min_mult":            float(os.getenv("ML_MIN_MULT", 0.40)),
    "ml_max_mult":            float(os.getenv("ML_MAX_MULT", 1.60)),

    # Binance Spot
    "bin_api_key":            os.getenv("BINANCE_API_KEY", ""),
    "bin_api_secret":         os.getenv("BINANCE_API_SECRET", ""),
    "bin_spot_max_cap":       float(os.getenv("BIN_SPOT_MAX_CAP_USDT", 60.0)),
    "bin_global_max_cap":     float(os.getenv("BIN_GLOBAL_MAX_CAP_USDT", 250.0)),
    "bin_investment_pct":     float(os.getenv("BIN_INVESTMENT_PCT", 0.05)),
    "bin_moonbag_trigger":    float(os.getenv("BIN_MOONBAG_TRIGGER", 4.0)),
    "bin_cooldown_seconds":   int(os.getenv("BIN_COOLDOWN_SECONDS", 3600)),
    "bin_dca_max_count":      int(os.getenv("BIN_DCA_MAX_COUNT", 3)),
    "bin_wagon_trail_frac":   float(os.getenv("BIN_WAGON_TRAIL_FRAC", 0.40)),
    "bin_wagon_trail_min":    float(os.getenv("BIN_WAGON_TRAIL_MIN", 0.25)),

    # V1.3.4 — GestorInventarioBin (BIN Simple Earn Flexible cripto, espejo OKX).
    # Parquea cripto idle en Earn flexible (BTC/ETH/alts) para generar yield mientras
    # no se vende. Subscribe/redeem vía ccxt sapi (subscribe/redeem FAST = T+0).
    # No requiere Funding bucket (BIN tiene flujo Spot ↔ Earn directo, simpler que OKX).
    "bin_inv_incluye_alts":   _env_bool("BIN_INV_INCLUYE_ALTS", True),
    "bin_inv_yield_min_usd":  float(os.getenv("BIN_INV_YIELD_MIN_USD_CYCLE", 0.01)),
    "bin_inv_yield_init_conf":float(os.getenv("BIN_INV_YIELD_INITIAL_CONF", 0.30)),
    "bin_inv_yield_succ_step":int(os.getenv("BIN_INV_YIELD_SUCCESS_STEP", 10)),
    "bin_inv_yield_timeout":  int(os.getenv("BIN_INV_YIELD_FLUSH_TIMEOUT", 60)),
    "bin_inv_yield_freeze":   int(os.getenv("BIN_INV_YIELD_FREEZE_SECS", 86400)),

    # OKX Spot
    "okx_api_key":            os.getenv("OKX_API_KEY", ""),
    "okx_api_secret":         os.getenv("OKX_API_SECRET", ""),
    "okx_passphrase":         os.getenv("OKX_PASSPHRASE", ""),
    "okx_spot_max_cap":       float(os.getenv("OKX_SPOT_MAX_CAP_USDT", 500.0)),
    "okx_global_max_cap":     float(os.getenv("OKX_GLOBAL_MAX_CAP_USDT", 2500.0)),
    "okx_investment_pct":     float(os.getenv("OKX_INVESTMENT_PCT", 0.04)),
    "okx_moonbag_trigger":    float(os.getenv("OKX_MOONBAG_TRIGGER", 4.0)),
    "okx_cooldown_seconds":   int(os.getenv("OKX_COOLDOWN_SECONDS", 3600)),
    "okx_dca_max_count":      int(os.getenv("OKX_DCA_MAX_COUNT", 3)),
    "okx_savings_apr_ref":    float(os.getenv("OKX_SAVINGS_APR_REF", 0.05)),
    "okx_inv_yield_min_usd":  float(os.getenv("OKX_INV_YIELD_MIN_USD_CYCLE", 0.01)),
    "okx_inv_yield_init_conf":float(os.getenv("OKX_INV_YIELD_INITIAL_CONF", 0.30)),
    "okx_inv_yield_succ_step":int(os.getenv("OKX_INV_YIELD_SUCCESS_STEP", 10)),
    "okx_inv_yield_timeout":  int(os.getenv("OKX_INV_YIELD_FLUSH_TIMEOUT", 60)),
    "okx_inv_yield_freeze":   int(os.getenv("OKX_INV_YIELD_FREEZE_SECS", 86400)),
    # V1.3.3: extiende GestorInventario a alts del portfolio OKX (LIQUIDATE_ONLY).
    # true → parquea TODO el portfolio activo (BTC/ETH + alts) en OKX Earn cripto.
    # false → solo BTC/ETH (comportamiento V1.0). flush_to_spot SÍNCRONO pre-SELL
    # ya es agnóstico al asset; cuando OmniRisk dispara SELL en una alt parqueada,
    # primero redime y luego vende. Earn yield mientras esperan recovery o salida.
    "okx_inv_incluye_alts":   _env_bool("OKX_INV_INCLUYE_ALTS", True),
    "okx_genesis_ts":         float(os.getenv("OKX_BOT_GENESIS_TS", 0.0)),
    "okx_genesis_capital":    float(os.getenv("OKX_BOT_GENESIS_CAPITAL", 0.0)),
    "okx_genesis_btc":        float(os.getenv("OKX_BOT_GENESIS_BTC", 0.0)),
    "okx_genesis_eth":        float(os.getenv("OKX_BOT_GENESIS_ETH", 0.0)),

    # V1.8.2 — resiliencia anti-bot/Cloudflare (los 403 transitorios de OKX).
    # okx_retry: backoff exponencial + jitter ante 403/Cloudflare/DDoS/timeout.
    # okx_antibot_ua: UA de navegador + Accept-Language para que Cloudflare
    # challenguee menos. Defaults conservadores: la espera total acotada para no
    # colgar el hilo MotorIndicadores (intentos×max_s tope).
    "okx_retry_intentos":     int(os.getenv("OKX_RETRY_INTENTOS", 3)),
    "okx_retry_base_s":       float(os.getenv("OKX_RETRY_BASE_S", 1.0)),
    "okx_retry_max_s":        float(os.getenv("OKX_RETRY_MAX_S", 6.0)),
    "okx_antibot_ua":         _env_bool("OKX_ANTIBOT_UA", True),

    # Ladder Grid SP3 (EscuadronLadderOkx) — sleeve spot round-trip, canary. Default OFF.
    # Pares en el global PARES_LADDER (como PARES_OKX_SPOT). Envelope en _validar_ladder.
    "ladder_enabled":         _env_bool("LADDER_ENABLED", False),
    "ladder_canary_mode":     _env_bool("LADDER_CANARY_MODE", True),
    "ladder_max_capital":     _env_float("LADDER_MAX_CAPITAL", 100.0),
    "ladder_rungs":           int(os.getenv("LADDER_RUNGS", 4)),
    "ladder_spacing_pct":     _env_float("LADDER_SPACING_PCT", 1.5),
    "ladder_stop_pct":        _env_float("LADDER_STOP_PCT", 15.0),
    "ladder_daily_loss_cap":  _env_float("LADDER_DAILY_LOSS_CAP", 15.0),
    "ladder_tick_s":          _env_float("LADDER_TICK_S", 60.0),
}

# V1.3.1 — resuelve modo HUD efectivo. En systemd/journalctl stdout NO es TTY.
_hud_mode_cfg = CONFIG["hud_mode"]
if _hud_mode_cfg == "tty":
    HUD_LOG_MODE = False
elif _hud_mode_cfg == "log":
    HUD_LOG_MODE = True
else:
    HUD_LOG_MODE = not sys.stdout.isatty()
CONFIG["hud_log_mode"] = HUD_LOG_MODE


# =============================================================================
# 0b. RESILIENCIA ANTI-BOT / CLOUDFLARE (V1.8.2)
# -----------------------------------------------------------------------------
# OKX pone Cloudflare delante de su REST y de a ratos challenguea los requests
# del bot con 403 (storms cortos: 06-18, 06-21). Mitigación en dos capas:
#   (1) _aplicar_antibot: UA de navegador + Accept-Language + timeout holgado al
#       construir el cliente ccxt → Cloudflare challenguea menos a algo que
#       parece browser. Es la cuenta propia del usuario contra la API oficial:
#       endurecer un cliente legítimo contra falsos positivos del WAF, no evadir
#       control de acceso.
#   (2) okx_retry: envuelve los reads que fallaron (balance/earn/OHLCV) y
#       reintenta SOLO ante errores anti-bot/transitorios con backoff exponencial
#       + jitter (espaciar, no martillar el bloqueo). Errores reales (auth/lógica/
#       fondos) se re-lanzan en el acto. Si se agotan los intentos re-lanza la
#       última excepción → el `except ...: logger_errores.error(...)` de cada call
#       site sigue de piso fail-safe (devuelve el último valor cacheado).
# =============================================================================
_OKX_UA_NAVEGADOR = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Tipos ccxt reintetables (defensivo: getattr por si cambian entre versiones).
_CCXT_REINTETABLES = tuple(
    c for c in (
        getattr(ccxt, "NetworkError", None),        # base de los transitorios
        getattr(ccxt, "DDoSProtection", None),      # 403/429 Cloudflare
        getattr(ccxt, "RateLimitExceeded", None),
        getattr(ccxt, "RequestTimeout", None),
        getattr(ccxt, "ExchangeNotAvailable", None),
    ) if c is not None
)

_ANTIBOT_MARCADORES = (
    "403", "forbidden", "cloudflare", "just a moment", "ddos",
    "429", "too many requests", "rate limit", "temporarily unavailable",
    "service unavailable", "challenge",
)


def piso_monto(amt: float, decimales: int = 8) -> float:
    """Trunca (FLOOR) un monto a N decimales antes de pedírselo a OKX.

    round() redondea HACIA ARRIBA (16.5618266480 → 16.56182665) → pide más de lo
    disponible → OKX 58350/51008 "insufficient balance". Floor NUNCA sobre-solicita:
    es la dirección segura para TODO movimiento de saldo (transfer/purchase/redeem),
    porque solo deja polvo, nunca supera lo que hay. Unifica el fix disperso de
    V1.9.9' (que solo tocó GestorInventario._transfer; el propio comentario ya avisaba
    "mismo bug le pega a la tesorería" — nunca se aplicó ahí hasta este primitivo).
    Pura, sin red. amt≤0 → 0.0 (los montos de saldo nunca son negativos; defensivo)."""
    if amt <= 0:
        return 0.0
    factor = 10 ** decimales
    return int(amt * factor) / factor


def nuevo_clordid(prefijo: str = "lad") -> str:
    """clOrdId válido para OKX: alfanumérico, ≤32 chars. Se genera UNA vez por colocación y
    se reusa en TODOS los reintentos de okx_retry → OKX garantiza at-most-once (rechaza el
    duplicado) aunque el fallo sea un timeout ambiguo (la orden llegó pero no respondió) →
    elimina la doble-orden/doble-venta. Cada colocación nueva genera uno fresco (uuid4)."""
    return (prefijo + uuid.uuid4().hex)[:32]


# =============================================================================
# Núcleo okx-semantica: movimiento de saldo OKX (transfer/purchase/redeem/balance).
# Extraído de GestorTesoreria (USDT) y GestorInventario (ccy) que lo duplicaban con
# criterio inconsistente — donde vivía el round-up que arregló V1.9.12 (piso_monto).
# Funciones sobre el cliente ccxt crudo (ex); infra reusable sin alfa (directiva 9).
# Fail-safe: los movimientos devuelven bool; los balances devuelven 0.0 ante error.
# =============================================================================
def okx_saldo_earn(ex, ccy: str) -> float:
    """Simple Earn (savings) balance de ccy. 0.0 si no hay/error (fail-safe)."""
    try:
        res = ex.private_get_finance_savings_balance({"ccy": ccy})
        for r in res.get("data", []):
            if r.get("ccy") == ccy:
                return float(r.get("amt", 0))
    except Exception as e:
        logger_errores.error(f"OKX earn balance {ccy}: {e}")
    return 0.0


def okx_saldo_funding(ex, ccy: str) -> float:
    """Funding (asset) availBal de ccy. 0.0 si no hay/error (fail-safe)."""
    try:
        res = ex.private_get_asset_balances({"ccy": ccy})
        for r in res.get("data", []):
            if r.get("ccy") == ccy:
                return float(r.get("availBal", 0))
    except Exception as e:
        logger_errores.error(f"OKX funding balance {ccy}: {e}")
    return 0.0


def okx_transfer(ex, ccy: str, amt: float, src: str, dst: str, decimales: int = 8) -> bool:
    """Transferencia interna OKX (18=Trading, 6=Funding). Monto FLOOR (piso_monto) para
    no sobre-solicitar. `decimales` por-ccy (USDT=4 histórico, cripto=8)."""
    try:
        ex.private_post_asset_transfer({
            "ccy": ccy, "amt": str(piso_monto(amt, decimales)),
            "from": src, "to": dst, "type": "0",
        })
        return True
    except Exception as e:
        logger_errores.error(f"OKX transfer {ccy} {src}→{dst} {amt}: {e}")
        return False


def okx_earn_purchase(ex, ccy: str, amt: float, on_unsupported=None, decimales: int = 8) -> bool:
    """Deposita ccy en Simple Earn flexible. Monto FLOOR. Si OKX rechaza por ccy no soportado
    (58001/58002/51000) y se pasó `on_unsupported`, lo invoca con el ccy (cache de la clase)."""
    try:
        ex.private_post_finance_savings_purchase_redempt({
            "ccy": ccy, "amt": str(piso_monto(amt, decimales)), "side": "purchase", "rate": "0.01",
        })
        return True
    except Exception as e:
        if on_unsupported is not None and any(c in str(e).lower() for c in ("58001", "58002", "51000")):
            on_unsupported(ccy)
        logger_errores.error(f"OKX Earn purchase {ccy}: {e}")
        return False


def okx_earn_redeem(ex, ccy: str, amt: float, decimales: int = 8) -> bool:
    """Redime ccy de Simple Earn flexible → Funding. Monto FLOOR."""
    try:
        ex.private_post_finance_savings_purchase_redempt({
            "ccy": ccy, "amt": str(piso_monto(amt, decimales)), "side": "redempt", "rate": "0.01",
        })
        return True
    except Exception as e:
        logger_errores.error(f"OKX Earn redempt {ccy}: {e}")
        return False


def _es_error_antibot(exc) -> bool:
    """True si la excepción parece bloqueo anti-bot / transitorio de red (reintetable).
    Errores reales (auth, fondos, lógica) → False (no se reintentan a lo bobo)."""
    if _CCXT_REINTETABLES and isinstance(exc, _CCXT_REINTETABLES):
        return True
    msg = str(getattr(exc, "args", "") or exc).lower()
    return any(m in msg for m in _ANTIBOT_MARCADORES)


def okx_retry(fn, *args, intentos=None, base=None, max_s=None,
              _sleep=time.sleep, _rand=random.random, **kwargs):
    """Ejecuta fn(*args, **kwargs) con reintentos + backoff exponencial + jitter
    ante errores anti-bot/transitorios (403/Cloudflare/DDoS/timeout). Re-lanza al
    instante si el error NO es anti-bot. Si agota los intentos, re-lanza la última
    excepción (preserva el fail-safe del call site). _sleep/_rand inyectables para test."""
    intentos = CONFIG["okx_retry_intentos"] if intentos is None else intentos
    base = CONFIG["okx_retry_base_s"] if base is None else base
    max_s = CONFIG["okx_retry_max_s"] if max_s is None else max_s
    intentos = max(1, int(intentos))
    ultima = None
    for i in range(intentos):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            ultima = e
            if not _es_error_antibot(e):
                raise
            if i >= intentos - 1:
                break
            espera = min(max_s, base * (2 ** i)) + _rand() * base
            _sleep(espera)
    raise ultima


def _aplicar_antibot(ex):
    """Endurece un cliente ccxt contra los challenges de Cloudflare: UA de
    navegador + Accept-Language + timeout holgado. Defensivo: nunca rompe la
    construcción del exchange. Devuelve el mismo ex (encadenable)."""
    if not CONFIG["okx_antibot_ua"]:
        return ex
    try:
        ex.userAgent = _OKX_UA_NAVEGADOR
        ex.headers = {**(getattr(ex, "headers", None) or {}),
                      "Accept-Language": "en-US,en;q=0.9"}
        ex.timeout = max(int(getattr(ex, "timeout", 10000) or 10000), 15000)
    except Exception:
        pass
    return ex


def banner(title, lines, level="info"):
    """V1.3.1 — banner adaptativo. En tty: Panel rich con bordes y color.
    En log mode (journalctl): líneas plain con prefijo greppable."""
    if HUD_LOG_MODE:
        tag = {"error": "[CFG-ERROR]", "warn": "[CFG-WARN]", "info": "[BOOT]"}.get(level, "[BOOT]")
        if title:
            print(f"{tag} === {title} ===", flush=True)
        for ln in lines:
            print(f"{tag} {ln}", flush=True)
        return
    color = {"error": "red", "warn": "yellow", "info": "cyan"}.get(level, "cyan")
    body = "\n".join(lines)
    console.print(Panel.fit(body, title=f"[bold {color}]{title}[/]", border_style=color))


# =============================================================================
# 1. CONSTANTES — fees, vagones, harvest, régimen, selector, catalyst
# =============================================================================
BIN_MAKER_FEE = 0.0010
BIN_TAKER_FEE = 0.0010
BIN_SLIPPAGE  = 0.0015
OKX_MAKER_FEE = 0.0008
OKX_TAKER_FEE = 0.0010
OKX_SLIPPAGE  = 0.0015
# V1.4.0: FUT_MAKER_FEE/FUT_TAKER_FEE/WAGONS_FUT removidos (eran usados solo por
# directional FUT motors removidos en V1.4.0). ARB usa ARB_FEE_PERP de CONFIG.

WAGONS_NORMAL = [1.0, 2.5, 5.0, 8.5, 14.0, 22.0]    # altcoins LIQUIDATE-ONLY
WAGONS_MEME   = [2.0, 5.0, 10.0, 20.0, 40.0]         # memes LIQUIDATE-ONLY

HARVEST_TIERS    = [2.0, 4.0, 7.0, 11.0, 16.0]
HARVEST_FRAC     = 0.15
HARVEST_TTL      = 1800
HARVEST_COOLDOWN = 300

REGIME_K          = 3.0
REGIME_FLOOR      = 0.30
REGIME_ENTER_BEAR = -0.02
REGIME_EXIT_BEAR  =  0.02

SELECTOR_TICK_SECONDS      = 60
SELECTOR_HYSTERESIS_HOURS  = 24
SELECTOR_COOLDOWN_HOURS    = 6
SELECTOR_BULL_EMA_PCT      = +0.02
SELECTOR_BEAR_EMA_PCT      = -0.05
SELECTOR_EMA_SLOPE_DAYS    = 30
SELECTOR_FUNDING_MAX_LONG  = 0.0005
SELECTOR_FUNDING_MIN_SHORT = 0.0
SELECTOR_ADX_TREND_MIN     = 25.0
SELECTOR_ATR_VOL_HIGH      = 0.06
SELECTOR_FUNDING_PATOL     = 0.001

# Cerebro de Régimen 4-estados V1.8.0 — umbrales del clasificador (taxonomía positiva).
REGIMEN_TEND_UP   = "TENDENCIA_UP"
REGIMEN_RANGO     = "RANGO"
REGIMEN_TEND_DOWN = "TENDENCIA_DOWN"
REGIMEN_CAOS      = "CAOS"
REGIMENES_VALIDOS = {REGIMEN_TEND_UP, REGIMEN_RANGO, REGIMEN_TEND_DOWN, REGIMEN_CAOS}
REGIMEN_ADX_TREND = _env_float("REGIMEN_ADX_TREND", 25.0)   # ADX mínimo para TENDENCIA_UP fuerte
REGIMEN_ADX_RANGO = _env_float("REGIMEN_ADX_RANGO", 20.0)   # ADX máximo para RANGO
REGIMEN_DESV_RANGO = _env_float("REGIMEN_DESV_RANGO", 0.05)  # |desv| máximo para RANGO
REGIMEN_HYST_TICKS = int(_env_float("REGIMEN_HYST_TICKS", 3))  # ticks consecutivos para confirmar cambio

OKX_CUENTA_FUNDING = "6"
OKX_CUENTA_TRADING = "18"

PARES_BIN_SPOT = ["BTC/USDT", "ETH/USDT"]  # solo datos: pares que el régimen escanea en Binance

# --- Universo de entrada OKX (Subsistema C 2026-06-15) ---
# PARES_OKX_SPOT = universo de NUEVAS entradas del Grid; mismo set alimenta datos/indicadores,
# suscripciones del feed y HUD. Configurable vía env OKX_PARES_SPOT (CSV). El core BTC/ETH
# SIEMPRE está presente (no se puede sacar por env). Default sin env = solo BTC/ETH (idéntico
# al comportamiento previo). Agregar un par (ej. SOL/USDT) reabre entrada QUIRÚRGICAMENTE en ese
# alt como base-hold ACTIVE; el resto de alts sigue en LIQUIDATE_ONLY.
_PARES_OKX_CORE = ["BTC/USDT", "ETH/USDT"]

def _construir_universo_okx():
    raw = os.getenv("OKX_PARES_SPOT")
    universo = list(_PARES_OKX_CORE)
    if raw:
        for p in raw.split(","):
            p = p.strip().upper()
            if p and p not in universo:
                universo.append(p)
    return universo

def _validar_pares_okx(pares):
    """Valida formato XXX/USDT (sin import de re). Devuelve lista de errores."""
    errs = []
    for p in pares:
        parts = p.split("/")
        if len(parts) != 2 or parts[1] != "USDT" or not parts[0] or not parts[0].isalnum():
            errs.append(f"OKX_PARES_SPOT par inválido (formato XXX/USDT): {p}")
    return errs

PARES_OKX_SPOT = _construir_universo_okx()
PARES_BASE_HOLD = set(PARES_OKX_SPOT)


def _construir_pares_ladder():
    """Universo del sleeve ladder (SP3) desde LADDER_PARES. Default LTC/USDT (cosecha más limpia
    en SP2). Separado de PARES_OKX_SPOT: el ladder es un sleeve aislado, no el grid core."""
    raw = os.getenv("LADDER_PARES", "LTC/USDT")
    pares = [p.strip().upper() for p in raw.split(",") if p.strip()]
    return [p if "/" in p else f"{p}/USDT" for p in pares]


PARES_LADDER = _construir_pares_ladder()


def ladder_ccys_gestionados():
    """Ccys cuyo inventario spot gestiona EL LADDER — vacío si el sleeve no corre.

    Fuente ÚNICA de la exclusión ladder↔core. PARES_LADDER se puebla del env SIEMPRE
    (default LTC/USDT), pero _build_ladder devuelve None con LADDER_ENABLED=false: leer
    la lista sin mirar el flag afirmaba un dueño inexistente. Con el ladder apagado y
    LTC heredado en el portfolio, OmniRisk se inhibía (sin stops ni salidas) y
    GestorInventario lo sacaba de Earn → posición huérfana sin nadie que la gestione,
    y en la config por DEFECTO. La pregunta correcta no es «¿está en LADDER_PARES?»
    sino «¿hay un ladder vivo que lo gestione?» (directiva 20: sin dueño, no se
    inhibe al que sí puede)."""
    if not CONFIG["ladder_enabled"]:
        return set()
    return {p.split("/")[0] for p in PARES_LADDER}


def _validar_ladder():
    """Envelope del sleeve ladder (SP3): devuelve lista de errores abort-worthy. [] si ladder OFF.
    Espejo de _validar_pares_okx (helper que retorna lista, NO sale del proceso)."""
    errs = []
    if not CONFIG["ladder_enabled"]:
        return errs
    # El cap se aplica POR SÍMBOLO (_comprometido(sym) en _tick), así que N pares con cap C
    # despliegan N·C. Validar C solo era correcto con un par: con 6 pares a 100 dejaba pasar $600
    # reales creyendo que eran 100. El envelope que importa es el TOTAL (directiva 23: la regla
    # tiene que medir la cantidad de la que habla).
    total_max = CONFIG["ladder_max_capital"] * max(1, len(PARES_LADDER))
    if total_max > 500.0:
        errs.append(f"LADDER_MAX_CAPITAL fuera del envelope de arranque: {len(PARES_LADDER)} par(es) "
                    f"× ${CONFIG['ladder_max_capital']:.0f} = ${total_max:.0f} totales (>500). "
                    f"El cap es POR PAR, no global.")
    if not PARES_LADDER:
        errs.append("LADDER_ENABLED=true pero LADDER_PARES vacío")
    errs += _validar_pares_okx(PARES_LADDER)
    return errs

# --- Pata Bajista V1.7.0 (EscuadronShortOkx) ---
PARES_SHORT_OKX = ["BTC/USDT:USDT", "ETH/USDT:USDT"]   # perps lineales OKX
K_CLOSE, K_EMA200, K_ADX, K_RSI, K_MACDH = "close", "EMA_200", "ADX_14", "RSI_14", "MACDh_12_26_9"
K_DMP, K_DMN = "DMP_14", "DMN_14"   # +DI / −DI (los computa df.ta.adx, ya en el dict de indicadores)
SHORT_RSI_MIN = _env_float("SHORT_RSI_MIN", 55.0)   # rebote (RSI elevado). Env-configurable tras el
                           # barredor 2026-06-16: RSI55→14 trades/año (insuficiente); RSI52→74 (mejor
                           # frecuencia + calidad). Bajalo a 52 para re-backfillear y medir.
SHORT_ADX_MIN = _env_float("SHORT_ADX_MIN", 20.0)   # hay tendencia, no chop
SHORT_DI_FILTER = _env_bool("SHORT_DI_FILTER", True)  # filtro direccional −DI ≥ +DI. El barredor
                           # confirmó que SIRVE (DI-off degrada pnl_avg y total). NO lo apagues.
SHORT_QTY_MINIMA = 0.001   # lote canary, en MONEDA (BTC/ETH). El wrapper lo convierte a
                           # contratos. BTC 0.001→0.1 contratos; ETH 0.001→0.01 (=minSz). Ambos válidos.
SHORT_COOLDOWN_S = 1800.0  # 30 min entre entradas por símbolo
SHORT_LOSS_COOLDOWN_S = 14400.0  # 4h sin re-shortear un símbolo tras cerrarlo EN PÉRDIDA (SL).
                                 # Anti-revenge-trade: el cooldown normal se keya al ABRIR, así que
                                 # tras un stop-out de posición longeva ya estaba vencido y re-entraba
                                 # al toque en el mismo rally. Este se keya al CERRAR-en-pérdida.
SHORT_MIN_MARGEN_USD = 5.0  # margen mínimo para abrir (evita órdenes sub-mínimas)
SHORT_MARGEN_BUFFER = 0.95  # usar solo 95% del free posteable: deja aire para el fee del
                            # taker al abrir + redondeo, así el IM real nunca supera el free.
SHORT_RECONCILE_GRACE_S = 90.0  # en runtime, no reconciliar un short abierto hace <90s: evita
                                # un falso-cierre si fetch_positions todavía no refleja el fill
                                # (lag de la API). Al boot el grace NO aplica (el cierre es real).
# OKX swap: el `amount` de ccxt es en CONTRATOS. ctVal = moneda por contrato (verificado
# vía /api/v5/public/instruments 2026-06-11). contratos = qty_moneda / ctVal.
SHORT_CTVAL = {"BTC/USDT:USDT": 0.01, "ETH/USDT:USDT": 0.1}

# --- Fase 1: Gestor de Salida SHADOW (multiverso de MEDICIÓN, riesgo cero) ---
# Corre políticas de salida en PAPER sobre entradas virtuales gatilladas por
# senal_short. NO abre órdenes ni toca capital: el OCO real sigue gobernando. Genera
# PnL contrafactual por política → dataset para decidir cuál gobierna capital real
# (Fase 2, diferida). Corre aunque OKX_FUT_ENABLED=false. Ver gestor_salida_shadow_spec.md.
SHORT_SHADOW_ENABLED = _env_bool("SHORT_SHADOW_ENABLED", True)
SHORT_SHADOW_CHANDELIER_K = _env_float("SHORT_SHADOW_CHANDELIER_K", 2.5)  # múltiplo de ATR del trailing
SHORT_SHADOW_ML_ADVERSO = _env_float("SHORT_SHADOW_ML_ADVERSO", 0.60)    # umbral ml_prob salida adversa
SHORT_SHADOW_MAX_HOLD_S = _env_float("SHORT_SHADOW_MAX_HOLD_S", 21600.0)  # 24 velas 15m = 6h
SHORT_SHADOW_TICK_S = 60.0
# --- Fase 3: meta-learner online (selección por contexto + recencia) ---
# El contexto = volatilidad del mar (ATR/close): distinta maniobra por tamaño de ola.
# El meta-learner pondera el feedback completo del shadow por recencia (half-life) y elige
# la política que mejor sobrevive en el contexto ACTUAL. Ver short_bajista / spec.
SHORT_META_HALFLIFE_S = _env_float("SHORT_META_HALFLIFE_S", 604800.0)  # 7d: peso por recencia
SHORT_META_VOL_LOW = _env_float("SHORT_META_VOL_LOW", 0.005)   # ATR/close < 0.5% = mar calmo
SHORT_META_VOL_HIGH = _env_float("SHORT_META_VOL_HIGH", 0.015)  # ATR/close > 1.5% = mar picado
SHORT_META_Z = _env_float("SHORT_META_Z", 1.65)   # z del gate apareado (1.65 = 95% una cola)
# Funding como dimensión de contexto del meta (Punto C). Opt-in: default off = solo volatilidad.
# El funding es señal NUEVA (no precio reprocesado) y backfillable → puede SUBIR edge, no solo medirlo.
SHORT_FUNDING_CONTEXT = _env_bool("SHORT_FUNDING_CONTEXT", False)
SHORT_FUNDING_THR = _env_float("SHORT_FUNDING_THR", 0.0005)   # ±0.05%/8h = funding "extremo"
# Punto A: funding como 6ª FEATURE del ML (xgboost). Opt-in (default off = modelo 5-feature actual).
# OJO: cambiar este flag INVALIDA el modelo vigente — hay que REENTRENAR (entrenar.py) en conjunto.
# La lista de features DEBE quedar idéntica en main.py (ml_feature_names) y entrenar.py (FEATURES_ORDER).
ML_FUNDING_FEATURE = _env_bool("ML_FUNDING_FEATURE", False)
ML_FEATURES_BASE = ["rsi", "adx", "ema_dist", "macd", "vol_ratio"]


def ml_feature_names():
    """Orden EXACTO de features del modelo. +funding si ML_FUNDING_FEATURE (idéntico a entrenar.py)."""
    return ML_FEATURES_BASE + (["funding"] if ML_FUNDING_FEATURE else [])


def _perp_de_spot(sym):
    """'BTC/USDT' → 'BTC/USDT:USDT' (perp lineal OKX). Para leer el funding del spot."""
    return sym if ":" in sym else f"{sym}:{sym.split('/')[1]}"
# --- Auto-entrenamiento periódico: main.py llama a entrenar.py de vez en cuando ---
# Mantiene fresco el 'mapa' (ML) y, opcional, el dataset del shadow (backfill histórico).
ENTRENAR_AUTO_ENABLED = _env_bool("ENTRENAR_AUTO_ENABLED", True)
ENTRENAR_INTERVALO_H = _env_float("ENTRENAR_INTERVALO_H", 24.0)   # cada cuántas horas re-entrena
ENTRENAR_DIAS = int(_env_float("ENTRENAR_DIAS", 90))              # ventana de histórico a minar
ENTRENAR_AUTO_BACKFILL = _env_bool("ENTRENAR_AUTO_BACKFILL", False)  # además backfillea el shadow


def _spot_de_perp(sym):
    """Mapea símbolo perp OKX → su spot para leer indicadores/precios/régimen.

    'BTC/USDT:USDT' -> 'BTC/USDT'. El perp comparte underlying con el spot, y tanto
    MotorIndicadores como WebSocketFeedOkx cachean los datos bajo el símbolo SPOT.
    Las ÓRDENES y el estado en DB usan el símbolo perp; los DATOS, el spot.
    """
    return sym.split(":")[0]


def senal_short(ind, rsi_min=None, adx_min=None, di_filter=None):
    """Rebote que se agota en un bear YA confirmado por el régimen diario.

    El gate de tendencia macro (BTC vs EMA200 DIARIA = BAJISTA) lo aplica `_gestionar`
    antes de llamar acá. Por eso NO re-filtramos tendencia en 15m: un rally que vale
    la pena shortear suele estar POR ENCIMA del EMA200 15m, así que exigir close<EMA200
    perdía el giro (para cuando el precio devolvía el rebote, el RSI ya había caído <50).
    Disparamos cuando el momentum del rebote se da vuelta (MACDh<0) con RSI aún elevado
    y tendencia presente (ADX). Simétrica al Grid spot que compra dips en alcista.

    Los umbrales (rsi_min/adx_min/di_filter) son override-ables para el BARREDOR offline
    (entrenar.py --barrer-senal); None = usa las constantes de producción."""
    rsi_min = SHORT_RSI_MIN if rsi_min is None else rsi_min
    adx_min = SHORT_ADX_MIN if adx_min is None else adx_min
    di_filter = SHORT_DI_FILTER if di_filter is None else di_filter
    if not ind:
        return False
    adx = float(ind.get(K_ADX, 0) or 0)
    rsi = float(ind.get(K_RSI, 0) or 0)
    macdh = float(ind.get(K_MACDH, 0) or 0)
    if not (adx > adx_min            # hay tendencia, no chop
            and rsi > rsi_min        # rebote (RSI elevado)
            and macdh < 0):          # momentum del rebote agotándose
        return False
    # Filtro direccional: ADX confirma fuerza pero NO dirección. Exigir −DI ≥ +DI evita
    # shortear un rally donde los toros todavía dominan (el cuadrante que stopeó al ETH).
    if di_filter:
        dmp = float(ind.get(K_DMP, 0) or 0)   # +DI
        dmn = float(ind.get(K_DMN, 0) or 0)   # −DI
        if dmn < dmp:
            return False
    return True


def calcular_size_short(equity, precio, ml_mult=1.0, disponible=None):
    """Qty del short respetando el envelope conservador. Canary → lote mínimo.

    `disponible` = USDT realmente posteable como margen (cuenta de trading). El earn
    NO cuenta hasta redimirse, así que el margen objetivo (equity*pct) se topea por el
    disponible real para no sobre-apalancar ni fallar por fondos insuficientes.
    """
    if CONFIG["short_canary_mode"]:
        return SHORT_QTY_MINIMA
    if precio <= 0:
        return 0.0
    # El ml_mult escala el margen DESEADO (no el notional final), para que el cap por
    # disponible sea el último paso y el IM real (= margen) nunca supere el free posteable.
    ml = max(0.5, min(2.0, ml_mult))
    margen = equity * CONFIG["short_max_equity_pct"] * ml
    if disponible is not None:
        margen = min(margen, disponible * SHORT_MARGEN_BUFFER)
    if margen < SHORT_MIN_MARGEN_USD:
        return 0.0
    notional = margen * CONFIG["short_leverage"]
    return notional / precio


# --- Fase 1 SHADOW: políticas de salida candidatas (puras) ---
# Firma común: politica(vp, ctx) -> (exit_px, motivo) | None.
#   vp  = {entry_px, mejor_px (mínimo alcanzado = más favorable al short), entry_ts, vivas}
#   ctx = {px (bid actual), ind (dict indicadores), ml_prob (o None)}
# Todas comparten un SL duro de catástrofe: nunca quedan desnudas (espejo del OCO
# server-side real, que es el piso bajo todo).

def _pol_sl_duro(vp, ctx):
    sl_px = vp["entry_px"] * (1 + CONFIG["short_sl_pct"])
    if ctx["px"] >= sl_px:                       # short: pierde si el precio sube
        return (sl_px, "sl-duro")
    return None


def pol_oco_fijo(vp, ctx):
    """Baseline = comportamiento actual: TP fijo abajo, SL fijo arriba."""
    tp_px = vp["entry_px"] * (1 - CONFIG["short_tp_pct"])
    if ctx["px"] <= tp_px:                        # short: gana si el precio baja
        return (tp_px, "tp-fijo")
    return _pol_sl_duro(vp, ctx)


def pol_chandelier(vp, ctx):
    """Trailing por ATR: cabalga la caída, sale si el precio retrocede k·ATR desde el
    mínimo alcanzado. Sin TP fijo → deja correr el cuchillo."""
    atr = float(ctx["ind"].get("ATRr_14", 0) or 0)
    if atr > 0:
        trail_px = vp["mejor_px"] + SHORT_SHADOW_CHANDELIER_K * atr
        if ctx["px"] >= trail_px:
            return (trail_px, "chandelier")
    return _pol_sl_duro(vp, ctx)


def pol_momentum(vp, ctx):
    """Salida anticipada: el rebote se reanuda hacia arriba (MACDh>0 ó +DI>−DI)."""
    ind = ctx["ind"]
    macdh = float(ind.get(K_MACDH, 0) or 0)
    dmp = float(ind.get(K_DMP, 0) or 0)
    dmn = float(ind.get(K_DMN, 0) or 0)
    if macdh > 0 or dmp > dmn:
        return (ctx["px"], "momentum-revierte")
    return _pol_sl_duro(vp, ctx)


def pol_ml_adverso(vp, ctx):
    """ML long invertido: prob alta de "sube +1.2% antes de bajar" = peligro para el short."""
    p = ctx.get("ml_prob")
    if p is not None and p >= SHORT_SHADOW_ML_ADVERSO:
        return (ctx["px"], "ml-adverso")
    return _pol_sl_duro(vp, ctx)


POLITICAS_SALIDA = {
    "oco_fijo": pol_oco_fijo,
    "chandelier": pol_chandelier,
    "momentum": pol_momentum,
    "ml_adverso": pol_ml_adverso,
}


def seleccionar_politica_live(resumen, mode, min_trades, margen_pct):
    """Fase 2: compuerta de graduación. Decide qué política gobierna las salidas
    REALES, a partir del PnL contrafactual medido por el shadow (short_shadow_resumen).
    El gobierno se GANA con datos. Pura y testeable.

    - mode "shadow" → None (sin gestión dinámica; el OCO server-side gobierna).
    - mode "<nombre>" (pin manual) → esa política.
    - mode "live" → gradúa la mejor que (a) tenga ≥min_trades muestras y (b) supere a
      `oco_fijo` por ≥margen_pct en pnl_avg. Si nadie califica → "oco_fijo" (= baseline,
      cero cambio). El piso SL server-side queda intacto bajo todas.
    """
    if mode == "shadow":
        return None
    if mode in POLITICAS_SALIDA:        # pin manual de una política concreta
        return mode
    # mode == "live": selección automática por evidencia
    base = next((r for r in resumen if r["politica"] == "oco_fijo"), None)
    base_avg = base["pnl_avg"] if base else 0.0
    candidatas = [r for r in resumen
                  if r["politica"] != "oco_fijo"
                  and r["n"] >= min_trades
                  and r["pnl_avg"] >= base_avg + margen_pct]
    if not candidatas:
        return "oco_fijo"
    return max(candidatas, key=lambda r: r["pnl_avg"])["politica"]


# Caché de funding en vivo por símbolo perp (poblada por MotorIndicadores cuando el contexto
# de funding está activo). El short y el shadow la leen vía get_funding.
FUNDING_RAM = {}
funding_lock = threading.Lock()


def set_funding(sym, rate, ts=None):
    with funding_lock:
        FUNDING_RAM[sym] = {"rate": float(rate), "ts": ts if ts is not None else time.time()}


def get_funding(sym):
    with funding_lock:
        e = FUNDING_RAM.get(sym)
    return e["rate"] if e else 0.0


def contexto_funding(rate, thr=None):
    """Bucket de funding = posicionamiento de la manada ('animales'). Punto C.
    neg = funding muy negativo (shorts amontonados, pagan → riesgo squeeze-up contra el short);
    pos = muy positivo (longs amontonados, rally inflado con leverage → favorable al short)."""
    if thr is None:
        thr = SHORT_FUNDING_THR
    r = float(rate or 0.0)
    if r < -thr:
        return "neg"
    if r > thr:
        return "pos"
    return "neutro"


def contexto_short(ind):
    """Contexto del meta. Default = solo volatilidad (contexto_vol). Si SHORT_FUNDING_CONTEXT,
    agrega la dimensión de funding (ind['funding']) → ej. 'calmo|neg'. Opt-in para no fragmentar
    la muestra hasta tener datos (el backfill ayuda)."""
    base = contexto_vol(ind)
    if not SHORT_FUNDING_CONTEXT:
        return base
    return f"{base}|{contexto_funding((ind or {}).get('funding', 0.0))}"


def contexto_vol(ind):
    """Fase 3: bucket de volatilidad del 'mar' = ATR/close. calmo/normal/picado.
    Sin datos → 'normal' (fail-safe neutro)."""
    close = float((ind or {}).get("close", 0) or 0)
    atr = float((ind or {}).get("ATRr_14", 0) or 0)
    if close <= 0 or atr <= 0:
        return "normal"
    r = atr / close
    if r < SHORT_META_VOL_LOW:
        return "calmo"
    if r > SHORT_META_VOL_HIGH:
        return "picado"
    return "normal"


def seleccionar_politica_meta(rows, min_trades, margen_pct, halflife_s, now):
    """Fase 3: meta-learner online. `rows` = trades-fantasma del shadow YA filtrados por el
    contexto actual (politica, pnl_pct, exit_ts). Pondera cada política por RECENCIA
    (half-life: lo viejo pesa menos → 'se adapta cuando el mar se mueve') y elige la que le
    gana a `oco_fijo` por ≥margen_pct. Si nadie califica → 'oco_fijo' (baseline). El feedback
    es COMPLETO (el shadow corre todas las políticas a la vez) → no hace falta explorar."""
    if not rows:
        return "oco_fijo"
    acc = {}   # politica -> [peso_total, pnl_ponderado, n_crudo]
    for r in rows:
        pol = r["politica"]
        ts = r.get("exit_ts")
        edad = max(0.0, now - (ts if ts is not None else now))
        w = 0.5 ** (edad / halflife_s) if halflife_s > 0 else 1.0
        a = acc.setdefault(pol, [0.0, 0.0, 0])
        a[0] += w
        a[1] += w * float(r["pnl_pct"])
        a[2] += 1

    def wavg(pol):
        a = acc.get(pol)
        return (a[1] / a[0]) if (a and a[0] > 0) else 0.0

    base = wavg("oco_fijo")
    candidatas = [(pol, wavg(pol)) for pol, a in acc.items()
                  if pol != "oco_fijo" and a[2] >= min_trades and wavg(pol) >= base + margen_pct]
    if not candidatas:
        return "oco_fijo"
    return max(candidatas, key=lambda t: t[1])[0]


def seleccionar_politica_meta_apareado(rows, min_trades, halflife_s, now, z=None):
    """Gate APAREADO (varianza/confianza) — el upgrade estadístico del meta. Aprovecha que el
    shadow corre todas las políticas sobre la MISMA ola: por cada trade virtual computa
    δ = pnl_política − pnl_oco_fijo (la varianza común del mercado se CANCELA en la resta).
    Pondera δ por recencia, exige muestra EFECTIVA (Kish) ≥ min_trades, y gradúa solo si la
    COTA INFERIOR de confianza (mean − z·SE) es > 0 → 'la ventaja es significativa, no suerte'.
    Elige la de mayor LCB; si ninguna → 'oco_fijo'. rows necesita symbol + entry_ts para aparear."""
    if z is None:
        z = SHORT_META_Z
    trades = {}
    for r in rows:
        trades.setdefault((r.get("symbol"), r.get("entry_ts")), {})[r["politica"]] = r
    mejor = ("oco_fijo", 0.0)   # (politica, lcb)
    for pol in POLITICAS_SALIDA:
        if pol == "oco_fijo":
            continue
        ws, ds = [], []
        for byp in trades.values():
            if pol in byp and "oco_fijo" in byp:
                ds.append(float(byp[pol]["pnl_pct"]) - float(byp["oco_fijo"]["pnl_pct"]))
                ts = byp[pol].get("exit_ts")
                edad = max(0.0, now - (ts if ts is not None else now))
                ws.append(0.5 ** (edad / halflife_s) if halflife_s > 0 else 1.0)
        if len(ds) < 2:
            continue
        sw = sum(ws)
        if sw <= 0:
            continue
        n_eff = (sw * sw) / sum(w * w for w in ws)
        if n_eff < min_trades:
            continue
        mean = sum(w * d for w, d in zip(ws, ds)) / sw
        var = sum(w * (d - mean) ** 2 for w, d in zip(ws, ds)) / sw
        se = (var / n_eff) ** 0.5
        lcb = mean - z * se if se > 0 else mean   # SE=0 (sin dispersión) → LCB = mean
        if lcb > 0 and lcb > mejor[1]:
            mejor = (pol, lcb)
    return mejor[0]

# Memes hardcoded fallback — env BIN_CATALOGO_MEMES + OKX_CATALOGO_MEMES los override.
# Solo se usa para clasificar altcoins LIQUIDATE-ONLY (stop sectorial -30% si DCA agotado).
CATALOGO_MEMES_DEFAULT = ["PEPE", "WIF", "BOME", "DOGE", "SHIB", "FLOKI", "TRUMP", "MEME"]
CATALOGO_MEMES_OKX = _env_json("OKX_CATALOGO_MEMES", CATALOGO_MEMES_DEFAULT)


def get_stop_threshold(venue, sym):
    """Stop sectorial: -30% para memes, None para BTC/ETH y altcoins normales.
    En LIQUIDATE-ONLY se aplica solo cuando dca_count >= dca_max_count."""
    if sym in PARES_BASE_HOLD:
        return None
    base = sym.split("/")[0]
    memes = CATALOGO_MEMES_OKX  # solo OKX opera; el venue es siempre "okx"
    if base in memes:
        return -30.0
    return None


def get_wagons(venue, sym):
    """Vagones por categoría. BTC/ETH no usan ratchet (Grid Harvest)."""
    base = sym.split("/")[0]
    memes = CATALOGO_MEMES_OKX  # solo OKX opera; el venue es siempre "okx"
    return WAGONS_MEME if base in memes else WAGONS_NORMAL


def is_vol_spike(venue, sym):
    """Killswitch volatilidad per-pair (auditoría binance_antiguo V14).
    True si ATR_actual > VOL_SPIKE_MULT × SMA(ATR_14, VOL_SPIKE_WINDOW).
    Fix V1.2.2 #6: fail-CLOSED durante warmup (indicadores_evaluado=False) — bloquea
    entradas hasta tener cache de indicadores. Tras warmup, fail-open si no hay datos
    para el sym específico (puede ser un sym nuevo en portfolio sin OHLCV aún)."""
    if not indicadores_evaluado:
        return True  # warmup: bloquea entradas hasta tener datos
    with cache_lock:
        entry = ohlcv_cache.get(venue, {}).get(sym)
    if not entry:
        return False  # post-warmup sin datos para este sym → fail-open
    df = entry["df"]
    if "ATRr_14" not in df.columns or len(df) < CONFIG["vol_spike_window"]:
        return False
    try:
        atr_now = float(df["ATRr_14"].iloc[-1])
        sma_atr = float(df["ATRr_14"].rolling(CONFIG["vol_spike_window"]).mean().iloc[-1])
        if sma_atr <= 0 or atr_now <= 0:
            return False
        return atr_now > sma_atr * CONFIG["vol_spike_mult"]
    except Exception:
        return False


def trailing_distance_mult(top_pct):
    """Trailing dinámico por distancia ganancia (auditoría okx_gemini V11.5).
    Cuanto más PnL acumulado, más estrecho el vagón — protege ganancias."""
    if top_pct >= 10.0:
        return 0.3
    if top_pct >= 5.0:
        return 0.4
    if top_pct >= 2.5:
        return 0.6
    return 1.0


# =============================================================================
# 2. ESTADO GLOBAL EN RAM + LOCKS
# =============================================================================
# Jerarquía de adquisición (de más interno a más externo):
#   regime_lock -> precios_lock -> cache_lock -> *_risk_lock -> entry_locks -> api_*_lock

PRECIOS_RAM   = {"binance": {}, "okx": {}}                  # {venue: {sym: {bid, ask, local_ts}}}
ohlcv_cache   = {"binance": {}, "okx": {}}                  # {venue: {sym: {df, ts}}}

# Estado publicado por el sleeve ladder en CADA tick, aunque no opere: es lo único
# que distingue "congelado por régimen propio" (sano) de "hilo muerto" (grave). Sin
# esto los dos casos se ven idénticos desde fuera — cero órdenes y errors.log limpio.
# {sym: {ts, regimen, motivo, armed, pending, holding, comprometido, cap, precio}}
ladder_estado_publicado: dict = {}

precios_lock  = threading.RLock()
cache_lock    = threading.RLock()
regime_lock   = threading.RLock()
bin_risk_lock = threading.RLock()
okx_risk_lock = threading.RLock()
tesoreria_lock= threading.RLock()
inv_yield_lock= threading.RLock()
coarse_lock   = threading.RLock()  # protege creación de entry_locks

# Mutex por símbolo para entradas spot (crea on-demand bajo coarse_lock)
entry_locks: dict = {}  # {(venue, sym): threading.Lock}

# Kill switch atómico
kill_switch = threading.Event()
if CONFIG["kill_switch"]:
    kill_switch.set()

# Régimen y modo (escritos solo por RadarRegimen y Selector respectivamente)
modo_mercado          = "ALCISTA"
factor_capital        = 1.0
regime_evaluado       = False
modo_operacion        = "SPOT"
modo_confianza        = 0.5
modo_razon            = "BOOT"
# V1.4.0: transition_force_close/target_side removidos (eran señales para OmniRiskFut
# para cerrar posiciones FUT cuando Selector cambiaba de FUT_LONG→FUT_SHORT. Sin
# directional FUT motors, estas señales no aplican).
selector_evaluado     = False

# Cerebro de Régimen 4-estados V1.8.0 (escrito solo por Selector._publicar_regimen, bajo regime_lock)
regimen_mercado       = REGIMEN_RANGO   # TENDENCIA_UP|RANGO|TENDENCIA_DOWN|CAOS; default neutro
regimen_confianza     = 0.5
regimen_razon         = "BOOT"
regimen_evaluado      = False

# Earn balances (OKX)
earn_balance_usdt_okx = 0.0
earn_balance_cripto_okx = 0.0
# Cache TTL para HUD: evita N llamadas/3s. Last refresh por venue.
_earn_usdt_cache_ts = {"binance": 0.0, "okx": 0.0}
_EARN_CACHE_TTL_S = 60.0

# Estado AIMD GestorInventario (fix V1.2.4 #2: faltaba declaración → NameError al loop).
# confidence ∈ [0.30, 1.00], success_streak suma hasta SUCCESS_STEP, freeze tras fallo 24h.
inv_yield_state = {
    "confidence":      float(os.getenv("OKX_INV_YIELD_INITIAL_CONF", 0.30)),
    "success_streak":  0,
    "flush_count_ok":  0,
    "flush_count_fail": 0,
    "freeze_until":    0.0,
}
earn_status_okx       = "INICIANDO..."

# V1.3.4: estado AIMD GestorInventarioBin (espejo del de OKX, independiente).
bin_inv_yield_state = {
    "confidence":      float(os.getenv("BIN_INV_YIELD_INITIAL_CONF", 0.30)),
    "success_streak":  0,
    "flush_count_ok":  0,
    "flush_count_fail": 0,
    "freeze_until":    0.0,
}
earn_status_bin       = "INICIANDO..."

# Modelo ML (cargado y hot-reloaded por _cargar_modelo)
ml_model = None
ml_model_mtime = 0.0
ml_lock = threading.Lock()

# Pánico Global BTC (auditoría okx_dca V12): bloquea entradas agresivas si BTC cae N% en 1h.
# Consumido por todos los motores de entrada spot/fut. Update por MotorIndicadores.
panico_global         = False
panico_global_until   = 0.0
btc_price_history     = collections.deque(maxlen=60)   # (ts, price) - 1h con tick 60s
panico_lock           = threading.RLock()

# Warmup MotorIndicadores (fix V1.2.2 #6): mientras False, is_vol_spike es fail-CLOSED
# (bloquea entradas en lugar de fail-open). Se setea True tras primera vuelta exitosa.
indicadores_evaluado  = False


def get_entry_lock(venue: str, sym: str) -> threading.Lock:
    """Devuelve (o crea) el mutex por símbolo para entradas spot."""
    key = (venue, sym)
    with coarse_lock:
        lock = entry_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            entry_locks[key] = lock
        return lock


def is_panico_global() -> bool:
    """True si Pánico Global BTC está activo (-3%/1h, ventana 30min). Re-evalúa expiry."""
    with panico_lock:
        if not panico_global:
            return False
        if time.time() >= panico_global_until:
            return False
        return True


# Seed de portfolio: avg_cost histórico (fix V1.2.6 PnL incorrecto en adopción).
# Leído una sola vez al import; sincronizar_*_arranque lo consulta para usar avg_cost
# real en vez de precio_actual (que daría PnL ~0% siempre). Formato JSON:
# {"okx": {"SYM/USDT": {"avg_cost", "qty", "inv"}}, "binance": {...}}
_portfolio_seed = {}
try:
    _seed_path = "portfolio_seed.json"
    if os.path.exists(_seed_path):
        with open(_seed_path, "r", encoding="utf-8") as _f:
            _portfolio_seed = json.load(_f)
except Exception as _e:
    print(f"WARN: portfolio_seed.json no se pudo cargar: {_e}")


def seed_avg_cost(venue, sym):
    """Devuelve avg_cost del seed para (venue, sym), o None si no está seedeado."""
    v = (_portfolio_seed.get(venue) or {}).get(sym) or {}
    avg = v.get("avg_cost")
    return float(avg) if avg else None


def refrescar_earn_usdt_okx(core_okx, force=False):
    """Lee USDT en OKX Simple Earn (cuenta 6 Funding savings) y actualiza global.
    Cache 60s salvo force=True. Falla silenciosa → mantiene último valor."""
    global earn_balance_usdt_okx
    now = time.time()
    if not force and (now - _earn_usdt_cache_ts["okx"]) < _EARN_CACHE_TTL_S:
        return earn_balance_usdt_okx
    try:
        res = okx_retry(core_okx.exchange_trade.private_get_finance_savings_balance, {"ccy": "USDT"})
        for r in (res.get("data") or []):
            if r.get("ccy") == "USDT":
                earn_balance_usdt_okx = float(r.get("amt", 0) or 0)
                _earn_usdt_cache_ts["okx"] = now
                return earn_balance_usdt_okx
    except Exception as e:
        logger_errores.error(f"OKX earn USDT read: {e}")
    return earn_balance_usdt_okx


def is_price_fresh(venue: str, sym: str, max_age: Optional[float] = None) -> bool:
    """True si el último tick WebSocket de (venue, sym) está dentro del límite."""
    max_age = max_age if max_age is not None else CONFIG["price_freshness_max_s"]
    with precios_lock:
        entry = PRECIOS_RAM.get(venue, {}).get(sym)
    if not entry:
        return False
    ts = entry.get("local_ts", 0.0)
    return (time.time() - ts) <= max_age


def calcular_obi_spot(exchange, sym, limit=20):
    """V1.4.1 Mejora #1: OBI orderbook spot vía REST público.
    Devuelve float ∈ [-1, 1] o None si error.
    +1.0 = todo bid (presión compradora extrema)
    -1.0 = todo ask (presión vendedora extrema)
    Solo se invoca cuando un motor está por entrar (no en cada tick) → REST overhead OK."""
    try:
        ob = exchange.fetch_order_book(sym, limit)
        bids = sum(float(b[1]) for b in (ob.get("bids") or []))
        asks = sum(float(a[1]) for a in (ob.get("asks") or []))
        total = bids + asks
        if total <= 0:
            return None
        return (bids - asks) / total
    except Exception:
        return None


def get_cooldown_dyn(venue, sym, base_seconds):
    """V1.4.1 Mejora #3: cooldown dinámico según ATR% del símbolo.
    Alta volatilidad → más cooldown (espacia entradas).
    Baja volatilidad → menos cooldown (más frecuencia en mercados estables).
    cd_eff = base × clamp(0.5 + atr_pct×0.3, 0.5, 2.0)
    Si COOLDOWN_ATR_ADAPTIVE=false → devuelve base sin modificar."""
    if not CONFIG.get("cooldown_atr_adaptive", True):
        return base_seconds
    try:
        ind = MotorIndicadores.get_indicators(venue, sym)
        if not ind:
            return base_seconds
        atr = float(ind.get("ATRr_14") or 0)
        close = float(ind.get("close") or 0)
        if atr <= 0 or close <= 0:
            return base_seconds
        # ATR_pct sobre escala diaria (15m × √96)
        atr_pct = (atr / close) * (96 ** 0.5) * 100
        factor = max(0.5, min(2.0, 0.5 + atr_pct * 0.3))
        return base_seconds * factor
    except Exception:
        return base_seconds


def get_harvest_tiers_dyn(venue, sym):
    """V1.4.1 Mejora #2: Harvest tiers adaptativos según ATR%.
    ATR alta → tiers más espaciados (volátil: dar margen).
    ATR baja → tiers más cercanos (capta ganancias chicas).
    Devuelve lista de tiers (porcentajes)."""
    if not CONFIG.get("harvest_atr_adaptive", True):
        return list(HARVEST_TIERS)
    try:
        ind = MotorIndicadores.get_indicators(venue, sym)
        if not ind:
            return list(HARVEST_TIERS)
        atr = float(ind.get("ATRr_14") or 0)
        close = float(ind.get("close") or 0)
        if atr <= 0 or close <= 0:
            return list(HARVEST_TIERS)
        atr_pct = (atr / close) * (96 ** 0.5) * 100
        if atr_pct >= CONFIG["harvest_atr_high_pct"]:
            mult = CONFIG["harvest_high_mult"]
        elif atr_pct <= CONFIG["harvest_atr_low_pct"]:
            mult = CONFIG["harvest_low_mult"]
        else:
            # Interpolación lineal entre low_pct y high_pct
            lo, hi = CONFIG["harvest_atr_low_pct"], CONFIG["harvest_atr_high_pct"]
            t = (atr_pct - lo) / max(1e-9, hi - lo)
            mult = CONFIG["harvest_low_mult"] + t * (CONFIG["harvest_high_mult"] - CONFIG["harvest_low_mult"])
        return [round(tier * mult, 2) for tier in HARVEST_TIERS]
    except Exception:
        return list(HARVEST_TIERS)


def _cargar_modelo():
    """Hot-reload del XGBoost si el mtime del joblib cambió. Idempotente."""
    global ml_model, ml_model_mtime
    path = "xgboost_oracle.joblib"
    try:
        if not os.path.exists(path):
            return
        mtime = os.path.getmtime(path)
        if mtime == ml_model_mtime:
            return
        with ml_lock:
            ml_model = joblib.load(path)
            ml_model_mtime = mtime
        logger_errores.info(f"Modelo ML cargado (mtime={mtime:.0f})")
    except Exception as e:
        logger_errores.error(f"ML load failed: {e}")


def ml_features_para(venue, sym):
    """V1.5.0 — extrae las 5 features que espera el modelo en el orden exacto
    de entrenamiento: [rsi, adx, ema_dist, macd, vol_ratio].
    Devuelve None si faltan indicadores o si no se puede calcular vol_ratio."""
    ind = MotorIndicadores.get_indicators(venue, sym)
    if not ind:
        return None
    try:
        close = float(ind.get("close") or 0)
        ema200 = float(ind.get("EMA_200") or 0)
        if close <= 0 or ema200 <= 0:
            return None
        ema_dist = (close - ema200) / ema200
        # vol_ratio = volumen actual / promedio últimos 20 (de ohlcv_cache)
        with cache_lock:
            entry = ohlcv_cache.get(venue, {}).get(sym)
        if not entry:
            return None
        df = entry["df"]
        if len(df) < 21:
            return None
        vol_now = float(df["volume"].iloc[-1])
        vol_mean = float(df["volume"].iloc[-21:-1].mean())
        vol_ratio = vol_now / vol_mean if vol_mean > 0 else 1.0
        feats = [
            float(ind.get("RSI_14") or 50),
            float(ind.get("ADX_14") or 20),
            ema_dist,
            float(ind.get("MACDh_12_26_9") or 0),
            vol_ratio,
        ]
        if ML_FUNDING_FEATURE:   # Punto A: 6ª feature = funding del perp (idéntico a entrenar.py)
            feats.append(get_funding(_perp_de_spot(sym)))
        return feats
    except Exception:
        return None


def ml_predict_prob(venue, sym):
    """V1.5.0 — devuelve prob (float ∈ [0,1]) o None si modelo/features no disponibles.
    Prob = probabilidad de que el precio toque +1.2% antes de -2% en 24 velas 15m."""
    if not CONFIG["ml_sizing_enabled"]:
        return None
    with ml_lock:
        model = ml_model
    if model is None:
        return None
    feats = ml_features_para(venue, sym)
    if feats is None:
        return None
    try:
        prob = float(model.predict_proba([feats])[0][1])
        return max(0.0, min(1.0, prob))
    except Exception:
        return None


def ml_size_multiplier(venue, sym):
    """V1.5.0 — modulador de tamaño basado en prob ML para Grid BUY.
    Devuelve mult ∈ [ML_MIN_MULT, ML_MAX_MULT] o 1.0 si modelo/features no disponibles.

    Filosofía: ML escala CUÁNTO comprar, nunca decide SI comprar. Los gates técnicos
    del motor llamador son veto previo obligatorio.

    Mapeo:
      prob < floor  → mult = MIN_MULT (reduce pero NUNCA skip)
      prob = floor  → mult = MIN_MULT
      prob = 1.0    → mult = MAX_MULT
      interpolación lineal entre ambos extremos.

    Dataset de entrenamiento: `entrenar.py` (mining histórico OHLCV). Las capturas
    live del bot operando se descartaron por sesgo de selección + volumen insuficiente
    + ruido operacional. Reentrenamiento periódico: `python entrenar.py --dias 90`."""
    prob = ml_predict_prob(venue, sym)
    if prob is None:
        return 1.0
    floor = CONFIG["ml_min_prob_floor"]
    if prob <= floor:
        return CONFIG["ml_min_mult"]
    slope = (CONFIG["ml_max_mult"] - CONFIG["ml_min_mult"]) / (1.0 - floor)
    mult = CONFIG["ml_min_mult"] + (prob - floor) * slope
    return max(CONFIG["ml_min_mult"], min(CONFIG["ml_max_mult"], mult))


# =============================================================================
# 3. LOGGING — errores, pares, indicadores, arb
# =============================================================================
os.makedirs("logs", exist_ok=True)

_error_fh = logging.handlers.RotatingFileHandler(
    "logs/errors.log", maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
)
_error_fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger_errores = logging.getLogger("quantum")
logger_errores.setLevel(logging.INFO)
logger_errores.addHandler(_error_fh)


class LoggerCircular:
    """Registro circular en deque + flush a JSON lines en disco."""

    def __init__(self, filepath, max_entries=100):
        self.filepath = filepath
        self.max_entries = max_entries
        self.lock = threading.Lock()
        self._buffer = collections.deque(maxlen=max_entries)
        self._cargar()

    def _cargar(self):
        try:
            if os.path.exists(self.filepath):
                with open(self.filepath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            self._buffer.append(line)
        except Exception:
            pass

    def registrar(self, **kwargs):
        kwargs.setdefault("ts", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        entrada = json.dumps(kwargs, ensure_ascii=False, default=str)
        with self.lock:
            self._buffer.append(entrada)
            try:
                with open(self.filepath, "w", encoding="utf-8") as f:
                    f.write("\n".join(self._buffer))
            except Exception:
                pass


logger_pares       = LoggerCircular("logs/pares.log", max_entries=100)
logger_indicadores = LoggerCircular("logs/indicadores.log", max_entries=100)


# =============================================================================
# 4. VALIDAR_CONFIG — fast-fail antes de tocar exchanges/BD
# =============================================================================
def validar_config():
    errores = []
    warnings = []

    # Claves obligatorias
    if not CONFIG["bin_api_key"] or not CONFIG["bin_api_secret"]:
        errores.append("BINANCE_API_KEY / BINANCE_API_SECRET vacías")
    if not CONFIG["okx_api_key"] or not CONFIG["okx_api_secret"] or not CONFIG["okx_passphrase"]:
        errores.append("OKX_API_KEY / SECRET / PASSPHRASE vacías")

    # Caps coherentes (OKX)
    if CONFIG["okx_spot_max_cap"] > CONFIG["okx_global_max_cap"]:
        errores.append("OKX_SPOT_MAX_CAP > OKX_GLOBAL_MAX_CAP")

    # LIQUIDATE_ONLY
    if not CONFIG["liquidate_only"]:
        warnings.append(
            "LIQUIDATE_ONLY=false — re-abre motores de entrada en altcoins. "
            "V1 está diseñado para BTC/ETH only. Confirmar que querés esto."
        )

    # Universo de entrada OKX (Subsistema C): formato + warning si hay pares fuera del core
    errores += _validar_pares_okx(PARES_OKX_SPOT)
    extra_pares = [p for p in PARES_OKX_SPOT if p not in set(_PARES_OKX_CORE)]
    if extra_pares:
        warnings.append(
            f"OKX_PARES_SPOT incluye pares fuera de BTC/ETH: {extra_pares} — reabre entrada "
            "(Grid BUY) en esos alts como base-hold ACTIVE. Confirmá liquidez/datos en OKX y "
            "que querés meter plata nueva ahí. (Sin datos OKX el par simplemente no opera: "
            "el Grid retorna si no hay indicadores — fail-safe.)"
        )

    # Pata Bajista V1.7.0 — validación de envelope de riesgo (fail-fast en config peligrosa)
    if CONFIG["fut_enabled"]:
        if not (1 <= CONFIG["short_leverage"] <= 5):
            errores.append(f"SHORT_LEVERAGE fuera de rango seguro [1-5]: {CONFIG['short_leverage']}")
        if not (0.0 < CONFIG["short_max_equity_pct"] <= 0.30):
            errores.append(f"SHORT_MAX_EQUITY_PCT fuera de rango [0-0.30]: {CONFIG['short_max_equity_pct']}")
        if not (0.01 <= CONFIG["short_sl_pct"] <= 0.10):
            errores.append(f"SHORT_SL_PCT fuera de rango [0.01-0.10]: {CONFIG['short_sl_pct']}")
        if not (0.005 <= CONFIG["short_tp_pct"] <= 0.10):
            errores.append(f"SHORT_TP_PCT fuera de rango [0.005-0.10]: {CONFIG['short_tp_pct']}")
        warnings.append(
            "OKX_FUT_ENABLED=true — pata bajista (futuros direccional) ACTIVA. "
            "Verificá el canary (isolated + SL colgado) antes de operar full."
        )

    # Fase 2: modo del gestor de salida (aplica aunque fut esté off; default shadow=inocuo).
    modos_gestor = {"shadow", "live", "meta"} | set(POLITICAS_SALIDA.keys())
    if CONFIG["short_gestor_mode"] not in modos_gestor:
        errores.append(f"SHORT_GESTOR_MODE inválido: {CONFIG['short_gestor_mode']} "
                       f"(válidos: {sorted(modos_gestor)})")
    elif CONFIG["short_gestor_mode"] != "shadow":
        warnings.append(
            f"SHORT_GESTOR_MODE={CONFIG['short_gestor_mode']} — gestión dinámica de salidas "
            "SOBRE CAPITAL REAL activa. El OCO server-side sigue de piso (SL). "
            "Revisá que el shadow tenga datos suficientes antes de confiar en la política."
        )

    # Cerebro de Régimen V1.8.0 — modo shadow(default, inocuo) | live (gobierna el grid).
    if CONFIG["regimen_brain_mode"] not in ("shadow", "live"):
        errores.append(f"REGIMEN_BRAIN_MODE inválido: {CONFIG['regimen_brain_mode']} "
                       "(válidos: shadow, live)")
    elif CONFIG["regimen_brain_mode"] == "live":
        warnings.append(
            "REGIMEN_BRAIN_MODE=live — el Cerebro de Régimen 4-estados GOBIERNA el Grid "
            "(congela en TENDENCIA_DOWN/CAOS, sizing por confianza). Verificá que el backfill "
            "(entrenar.py --regimen-backfill) respalde la clasificación antes de operar."
        )

    # Ladder Grid SP3 — envelope del sleeve (abort-worthy) + warning si canary off.
    errores += _validar_ladder()
    if CONFIG["ladder_enabled"]:
        if not CONFIG["ladder_canary_mode"]:
            warnings.append(
                "LADDER_CANARY_MODE=false — sleeve ladder con capital real ESCALADO. "
                "Graduá solo con fill_rate validado por el canary."
            )
        else:
            warnings.append(
                f"LADDER_ENABLED=true [CANARY] — sleeve ladder spot sobre {PARES_LADDER}, "
                f"cap ${CONFIG['ladder_max_capital']:.0f}. Seguí el runbook canary_ladder.md."
            )

    if errores:
        banner("Config inválida — aborta arranque",
               [f"ERROR {e}" for e in errores], level="error")
        sys.exit(1)

    if warnings:
        banner("Warnings de configuración",
               [f"WARN {w}" for w in warnings], level="warn")


# =============================================================================
# 5. DATABASE MANAGER — quantum.db, write-through, RAM caches
# =============================================================================
class DatabaseManager:
    """SQLite WAL + caches RAM write-through. Prefijos por módulo en tablas."""

    def __init__(self, db_name=None):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(
            db_name or CONFIG["db_name"], check_same_thread=False, timeout=20
        )
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")

        # RAM caches por (venue, sym)
        self._cooldown_cache = {}
        self._tp_cache       = {}
        self._dca_cache      = {}
        self._portfolio_cache_valid = {"binance": False, "okx": False}
        self._portfolio_cache_data  = {"binance": [],    "okx": []}

        self._init_db()
        self._load_caches()

    def _exec(self, query, params=(), fetch=None, silent=False):
        """silent=True: no logea errores (útil para ALTER TABLE ADD COLUMN idempotente
        donde 'duplicate column name' es comportamiento esperado en re-arranques)."""
        with self.lock:
            try:
                cur = self.conn.execute(query, params)
                res = (
                    cur.fetchone() if fetch == "one"
                    else cur.fetchall() if fetch == "all"
                    else None
                )
                self.conn.commit()
                return res
            except Exception as e:
                if not silent:
                    logger_errores.error(f"DB exec: {e} | {query[:80]}")
                return None

    def _init_db(self):
        # sys_*
        self._exec("CREATE TABLE IF NOT EXISTS sys_caja_negra ("
                   "id INTEGER PRIMARY KEY AUTOINCREMENT, ts DATETIME, "
                   "modulo TEXT, symbol TEXT, mensaje TEXT)")
        self._exec("CREATE TABLE IF NOT EXISTS sys_bot_genesis ("
                   "id INTEGER PRIMARY KEY, ts REAL, "
                   "capital_bin REAL, capital_okx REAL, btc_price REAL, eth_price REAL)")

        # bin_*
        for prefix in ("bin", "okx"):
            self._exec(f"CREATE TABLE IF NOT EXISTS {prefix}_portfolio ("
                       f"symbol TEXT PRIMARY KEY, average_cost REAL, total_qty REAL, "
                       f"initial_investment REAL, mode TEXT DEFAULT 'ACTIVE')")
            self._exec(f"CREATE TABLE IF NOT EXISTS {prefix}_dca_state ("
                       f"symbol TEXT PRIMARY KEY, dca_armed INTEGER, dca_level REAL, "
                       f"local_bottom REAL, dca_count INTEGER DEFAULT 0)")
            self._exec(f"CREATE TABLE IF NOT EXISTS {prefix}_tp_state ("
                       f"symbol TEXT PRIMARY KEY, tp_armed INTEGER, tp_tier REAL, "
                       f"local_top REAL, moonbag_done INTEGER DEFAULT 0, floor_pct REAL DEFAULT 0.0, "
                       f"partial_done INTEGER DEFAULT 0)")
            # Migración idempotente (auditoría kucoin_antiguo V13: TP Parcial 50%)
            self._exec(
                f"ALTER TABLE {prefix}_tp_state ADD COLUMN partial_done INTEGER DEFAULT 0",
                silent=True  # idempotente: duplicate column en re-arranques no es error
            )
            self._exec(f"CREATE TABLE IF NOT EXISTS {prefix}_cooldown_state ("
                       f"symbol TEXT PRIMARY KEY, last_buy_time REAL)")
            self._exec(f"CREATE TABLE IF NOT EXISTS {prefix}_grid_orders ("
                       f"order_id TEXT PRIMARY KEY, symbol TEXT, timestamp REAL, "
                       f"reserved_usdt REAL DEFAULT 0.0, side TEXT DEFAULT 'BUY')")
            self._exec(f"CREATE TABLE IF NOT EXISTS {prefix}_pending_sells ("
                       f"order_id TEXT PRIMARY KEY, symbol TEXT, p_limit REAL, "
                       f"q_sell REAL, c_old REAL, is_moonbag INTEGER, timestamp REAL)")
            self._exec(f"CREATE TABLE IF NOT EXISTS {prefix}_realized_pnl ("
                       f"id INTEGER PRIMARY KEY AUTOINCREMENT, ts DATETIME, symbol TEXT, "
                       f"buy_price REAL, sell_price REAL, qty REAL, pnl_pct REAL, pnl_usdt REAL)")

        # OKX Earn cripto
        self._exec("CREATE TABLE IF NOT EXISTS okx_inventory_earn ("
                   "ccy TEXT PRIMARY KEY, symbol TEXT, qty REAL, "
                   "deposited_at REAL, last_flush_ms REAL DEFAULT 0.0, "
                   "fail_count INTEGER DEFAULT 0)")

        # V1.3.4: BIN Earn cripto (espejo OKX). productId cache en RAM, no DB.
        self._exec("CREATE TABLE IF NOT EXISTS bin_inventory_earn ("
                   "ccy TEXT PRIMARY KEY, symbol TEXT, qty REAL, "
                   "deposited_at REAL, last_flush_ms REAL DEFAULT 0.0, "
                   "fail_count INTEGER DEFAULT 0)")

        # fut_*
        self._exec("CREATE TABLE IF NOT EXISTS fut_tp_state ("
                   "symbol TEXT PRIMARY KEY, side TEXT, tp_armed INTEGER, tp_tier REAL, "
                   "local_top REAL, moonbag_done INTEGER DEFAULT 0, floor_pct REAL DEFAULT 0.0)")
        self._exec("CREATE TABLE IF NOT EXISTS fut_dca_state ("
                   "symbol TEXT PRIMARY KEY, side TEXT, dca_armed INTEGER, dca_level REAL, "
                   "local_extreme REAL, dca_count INTEGER DEFAULT 0)")
        self._exec("CREATE TABLE IF NOT EXISTS fut_cooldown_state ("
                   "symbol TEXT PRIMARY KEY, last_action_time REAL, "
                   "loss_cooldown_until REAL DEFAULT 0.0)")
        # Migración idempotente (auditoría binance_antiguo V14: cooldown post-pérdida)
        self._exec(
            "ALTER TABLE fut_cooldown_state ADD COLUMN loss_cooldown_until REAL DEFAULT 0.0",
            silent=True  # idempotente
        )
        # Auditoría binance_antiguo V14: TTL Guillotina Temporal — track entry_ts/entry_price
        # por posición. Insertado en _abrir_fut, limpiado en CentinelaFut cierre.
        self._exec("CREATE TABLE IF NOT EXISTS fut_position_meta ("
                   "symbol TEXT PRIMARY KEY, side TEXT, entry_ts REAL, entry_price REAL)")
        self._exec("CREATE TABLE IF NOT EXISTS fut_native_stops ("
                   "symbol TEXT PRIMARY KEY, side TEXT, order_id TEXT, "
                   "stop_price REAL, ts REAL)")
        self._exec("CREATE TABLE IF NOT EXISTS fut_realized_pnl ("
                   "id INTEGER PRIMARY KEY AUTOINCREMENT, ts DATETIME, symbol TEXT, "
                   "side TEXT, entry REAL, exit REAL, contracts REAL, notional REAL, "
                   "pnl_usdt REAL, motivo TEXT)")
        self._exec("CREATE TABLE IF NOT EXISTS fut_daily_anchor ("
                   "date TEXT PRIMARY KEY, balance_start REAL, realized_today REAL)")

        # ml_*
        # V1.5.0: schema alineado con entrenar.py (mining histórico es la única fuente
        # del dataset; las capturas live se descartaron por sesgo de selección + ruido).
        self._exec("CREATE TABLE IF NOT EXISTS ml_feature_log ("
                   "id TEXT PRIMARY KEY, venue TEXT, symbol TEXT, "
                   "rsi REAL, adx REAL, ema_dist REAL, macd REAL, vol_ratio REAL, "
                   "resultado INTEGER)")

        # Migraciones idempotentes (suprime error si la columna ya existe)
        for prefix in ("bin", "okx"):
            self._exec(
                f"ALTER TABLE {prefix}_portfolio ADD COLUMN mode TEXT DEFAULT 'ACTIVE'",
                silent=True  # idempotente
            )

        # short_* (Pata Bajista V1.7.0 — EscuadronShortOkx)
        self._exec("CREATE TABLE IF NOT EXISTS short_positions ("
                   "symbol TEXT PRIMARY KEY, entry_px REAL, qty REAL, lever INTEGER, "
                   "sl_order_id TEXT, open_ts REAL)")
        self._exec("CREATE TABLE IF NOT EXISTS short_realized_pnl ("
                   "id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, entry_px REAL, "
                   "exit_px REAL, pnl REAL, close_ts REAL)")
        self._exec("CREATE TABLE IF NOT EXISTS short_cooldown_state ("
                   "symbol TEXT PRIMARY KEY, last_open_ts REAL)")
        self._exec("CREATE TABLE IF NOT EXISTS short_loss_cooldown_state ("
                   "symbol TEXT PRIMARY KEY, last_loss_ts REAL)")
        # Fase 1: gestor de salida SHADOW (PnL contrafactual por política, paper)
        # Fase 3: columna `contexto` (bucket de volatilidad) para el meta-learner.
        self._exec("CREATE TABLE IF NOT EXISTS short_shadow_pnl ("
                   "id INTEGER PRIMARY KEY AUTOINCREMENT, politica TEXT, symbol TEXT, "
                   "entry_px REAL, exit_px REAL, pnl_pct REAL, motivo TEXT, "
                   "entry_ts REAL, exit_ts REAL, contexto TEXT)")
        # Migración defensiva: si la tabla ya existía sin `contexto` (V1.7.1), agrégala.
        _cols = [r[1] for r in (self._exec("PRAGMA table_info(short_shadow_pnl)", fetch="all") or [])]
        if "contexto" not in _cols:
            self._exec("ALTER TABLE short_shadow_pnl ADD COLUMN contexto TEXT")
        # Ladder Grid SP3 (EscuadronLadderOkx) — sleeve spot round-trip, estado + canary fill-log.
        self._exec("CREATE TABLE IF NOT EXISTS ladder_orders ("
                   "order_id TEXT PRIMARY KEY, symbol TEXT, side TEXT, rung_idx INTEGER, "
                   "price REAL, qty REAL, state TEXT, ts REAL)")
        self._exec("CREATE TABLE IF NOT EXISTS ladder_positions ("
                   "symbol TEXT, rung_idx INTEGER, qty REAL, avg_cost REAL, "
                   "sell_order_id TEXT, bought_ts REAL, PRIMARY KEY(symbol, rung_idx))")
        self._exec("CREATE TABLE IF NOT EXISTS ladder_realized ("
                   "id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, qty REAL, buy_px REAL, "
                   "sell_px REAL, pnl_neto REAL, motivo TEXT, ts REAL)")
        self._exec("CREATE TABLE IF NOT EXISTS ladder_fill_log ("
                   "order_id TEXT PRIMARY KEY, symbol TEXT, rung_idx INTEGER, price REAL, "
                   "ts_colocado REAL, ts_tocado REAL, lleno INTEGER DEFAULT 0, ts_lleno REAL)")

    # --- Pata Bajista V1.7.0: estado de posiciones short ---
    def short_abrir_posicion(self, symbol, entry_px, qty, lever, sl_order_id):
        self._exec("INSERT OR REPLACE INTO short_positions "
                   "(symbol, entry_px, qty, lever, sl_order_id, open_ts) VALUES (?,?,?,?,?,?)",
                   (symbol, entry_px, qty, lever, sl_order_id, time.time()))
        self._exec("INSERT OR REPLACE INTO short_cooldown_state (symbol, last_open_ts) "
                   "VALUES (?,?)", (symbol, time.time()))

    def short_get_posiciones(self):
        rows = self._exec("SELECT symbol, entry_px, qty, lever, sl_order_id, open_ts "
                          "FROM short_positions", fetch="all") or []
        return [dict(symbol=r[0], entry_px=r[1], qty=r[2], lever=r[3],
                     sl_order_id=r[4], open_ts=r[5]) for r in rows]

    def short_cerrar_posicion(self, symbol, exit_px, pnl):
        row = self._exec("SELECT entry_px FROM short_positions WHERE symbol=?",
                         (symbol,), fetch="one")
        entry = row[0] if row else 0.0
        self._exec("DELETE FROM short_positions WHERE symbol=?", (symbol,))
        self._exec("INSERT INTO short_realized_pnl (symbol, entry_px, exit_px, pnl, close_ts) "
                   "VALUES (?,?,?,?,?)", (symbol, entry, exit_px, pnl, time.time()))
        if pnl < 0:   # cierre en pérdida (SL) → arma el cooldown anti-revenge-trade
            self._exec("INSERT OR REPLACE INTO short_loss_cooldown_state "
                       "(symbol, last_loss_ts) VALUES (?,?)", (symbol, time.time()))

    def short_loss_cooldown(self, symbol):
        row = self._exec("SELECT last_loss_ts FROM short_loss_cooldown_state WHERE symbol=?",
                         (symbol,), fetch="one")
        return float(row[0]) if row else 0.0

    def short_pnl_hoy(self):
        import datetime
        medianoche = datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        row = self._exec("SELECT COALESCE(SUM(pnl),0) FROM short_realized_pnl "
                         "WHERE close_ts>=?", (medianoche,), fetch="one")
        return float(row[0]) if row else 0.0

    def short_cooldown(self, symbol):
        row = self._exec("SELECT last_open_ts FROM short_cooldown_state WHERE symbol=?",
                         (symbol,), fetch="one")
        return float(row[0]) if row else 0.0

    # --- Ladder Grid SP3 (EscuadronLadderOkx): órdenes, posiciones, realizados, fill-log canary ---
    def ladder_registrar_orden(self, order_id, symbol, side, rung_idx, price, qty):
        self._exec("INSERT OR REPLACE INTO ladder_orders "
                   "(order_id, symbol, side, rung_idx, price, qty, state, ts) VALUES (?,?,?,?,?,?,?,?)",
                   (order_id, symbol, side, rung_idx, price, qty, "open", time.time()))
        if side == "buy":   # fill-realism: solo el lado entrada (¿llena el maker al tocar?)
            self._exec("INSERT OR REPLACE INTO ladder_fill_log "
                       "(order_id, symbol, rung_idx, price, ts_colocado, ts_tocado, lleno, ts_lleno) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       (order_id, symbol, rung_idx, price, time.time(), None, 0, None))

    def ladder_marcar_tocado(self, order_id):
        self._exec("UPDATE ladder_fill_log SET ts_tocado=? WHERE order_id=? AND ts_tocado IS NULL",
                   (time.time(), order_id))

    def ladder_marcar_fill(self, order_id):
        self._exec("UPDATE ladder_orders SET state='filled' WHERE order_id=?", (order_id,))
        self._exec("UPDATE ladder_fill_log SET lleno=1, ts_lleno=? WHERE order_id=?",
                   (time.time(), order_id))

    def ladder_fill_resumen(self):
        col = self._exec("SELECT COUNT(*) FROM ladder_fill_log", fetch="one")
        toc = self._exec("SELECT COUNT(*) FROM ladder_fill_log WHERE ts_tocado IS NOT NULL", fetch="one")
        lle = self._exec("SELECT COALESCE(SUM(lleno),0) FROM ladder_fill_log", fetch="one")
        colocados, tocados, llenos = int(col[0]), int(toc[0]), int(lle[0])
        return {"colocados": colocados, "tocados": tocados, "llenos": llenos,
                "fill_rate": (llenos / tocados) if tocados else 0.0}

    def ladder_set_posicion(self, symbol, rung_idx, qty, avg_cost, sell_order_id):
        self._exec("INSERT OR REPLACE INTO ladder_positions "
                   "(symbol, rung_idx, qty, avg_cost, sell_order_id, bought_ts) VALUES (?,?,?,?,?,?)",
                   (symbol, rung_idx, qty, avg_cost, sell_order_id, time.time()))

    def ladder_actualizar_sell(self, symbol, rung_idx, sell_order_id):
        """Re-arma la sell de una posición existente sin tocar qty/avg_cost/bought_ts (a diferencia
        de ladder_set_posicion, que hace INSERT OR REPLACE y resetea bought_ts)."""
        self._exec("UPDATE ladder_positions SET sell_order_id=? WHERE symbol=? AND rung_idx=?",
                   (sell_order_id, symbol, rung_idx))

    def ladder_marcar_terminal(self, order_id, estado="cancelled"):
        """Saca la orden de 'open' sin tocar el fill-log (no hubo fill que registrar). Todo
        lo que consulta ladder_orders filtra por state='open', así que basta con moverla."""
        self._exec("UPDATE ladder_orders SET state=? WHERE order_id=?", (estado, order_id))

    def ladder_ajustar_posicion(self, symbol, rung_idx, qty, sell_order_id):
        """Deja la posición con la qty que REALMENTE queda y la salida indicada, sin tocar
        avg_cost ni bought_ts. Para el sell que se llenó a medias antes de cancelarse: el
        resto sigue en la cuenta y necesita salida nueva."""
        self._exec("UPDATE ladder_positions SET qty=?, sell_order_id=? WHERE symbol=? AND rung_idx=?",
                   (qty, sell_order_id, symbol, rung_idx))

    def ladder_get_posiciones(self):
        rows = self._exec("SELECT symbol, rung_idx, qty, avg_cost, sell_order_id, bought_ts "
                          "FROM ladder_positions", fetch="all") or []
        return [dict(symbol=r[0], rung_idx=r[1], qty=r[2], avg_cost=r[3],
                     sell_order_id=r[4], bought_ts=r[5]) for r in rows]

    def ladder_del_posicion(self, symbol, rung_idx):
        self._exec("DELETE FROM ladder_positions WHERE symbol=? AND rung_idx=?", (symbol, rung_idx))

    def ladder_registrar_realizado(self, symbol, qty, buy_px, sell_px, pnl_neto, motivo):
        self._exec("INSERT INTO ladder_realized (symbol, qty, buy_px, sell_px, pnl_neto, motivo, ts) "
                   "VALUES (?,?,?,?,?,?,?)", (symbol, qty, buy_px, sell_px, pnl_neto, motivo, time.time()))

    def ladder_pnl_hoy(self):
        import datetime
        medianoche = datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        row = self._exec("SELECT COALESCE(SUM(pnl_neto),0) FROM ladder_realized WHERE ts>=?",
                         (medianoche,), fetch="one")
        return float(row[0]) if row else 0.0

    # --- Fase 1: gestor de salida SHADOW (PnL contrafactual por política) ---
    def short_shadow_registrar(self, politica, symbol, entry_px, exit_px, pnl_pct,
                               motivo, entry_ts, exit_ts, contexto="normal"):
        self._exec("INSERT INTO short_shadow_pnl (politica, symbol, entry_px, exit_px, "
                   "pnl_pct, motivo, entry_ts, exit_ts, contexto) VALUES (?,?,?,?,?,?,?,?,?)",
                   (politica, symbol, entry_px, exit_px, pnl_pct, motivo, entry_ts, exit_ts, contexto))

    def short_shadow_resumen(self):
        rows = self._exec("SELECT politica, COUNT(*), COALESCE(SUM(pnl_pct),0), "
                          "COALESCE(AVG(pnl_pct),0), "
                          "SUM(CASE WHEN pnl_pct>0 THEN 1 ELSE 0 END) "
                          "FROM short_shadow_pnl GROUP BY politica", fetch="all") or []
        return [dict(politica=r[0], n=r[1], pnl_total=r[2], pnl_avg=r[3], wins=r[4])
                for r in rows]

    def short_shadow_rows_por_contexto(self, contexto):
        """Fase 3: filas del shadow del contexto dado (para el meta-learner). Incluye entry_ts +
        symbol para que el gate apareado pueda emparejar políticas del MISMO trade virtual."""
        rows = self._exec("SELECT politica, pnl_pct, exit_ts, entry_ts, symbol FROM short_shadow_pnl "
                          "WHERE contexto=?", (contexto,), fetch="all") or []
        return [dict(politica=r[0], pnl_pct=r[1], exit_ts=r[2], entry_ts=r[3], symbol=r[4])
                for r in rows]

    def short_shadow_reporte(self):
        """Readiness: por (contexto, política) → n, pnl_avg. Para ver cuándo el shadow está listo."""
        rows = self._exec("SELECT contexto, politica, COUNT(*), COALESCE(AVG(pnl_pct),0) "
                          "FROM short_shadow_pnl GROUP BY contexto, politica", fetch="all") or []
        return [dict(contexto=r[0], politica=r[1], n=r[2], pnl_avg=r[3]) for r in rows]

    def short_shadow_listo(self, min_trades):
        """Por contexto: ¿hay una política con muestra suficiente que supere al baseline?
        Devuelve {contexto: {listo, lider, margen, n}}. 'listo' = el líder le gana a oco_fijo."""
        por_ctx = {}
        for r in self.short_shadow_reporte():
            por_ctx.setdefault(r["contexto"], {})[r["politica"]] = r
        out = {}
        for ctx, pols in por_ctx.items():
            base = pols.get("oco_fijo", {}).get("pnl_avg", 0.0)
            cand = [(p, d) for p, d in pols.items()
                    if p != "oco_fijo" and d["n"] >= min_trades]
            if cand:
                lider, d = max(cand, key=lambda t: t[1]["pnl_avg"])
                out[ctx] = dict(listo=bool(d["pnl_avg"] > base), lider=lider,
                                margen=d["pnl_avg"] - base, n=d["n"])
            else:
                out[ctx] = dict(listo=False, lider=None, margen=0.0,
                                n=max((p["n"] for p in pols.values()), default=0))
        return out

    def _load_caches(self):
        # cooldown
        rows = self._exec(
            "SELECT 'binance' AS venue, symbol, last_buy_time FROM bin_cooldown_state "
            "UNION ALL "
            "SELECT 'okx', symbol, last_buy_time FROM okx_cooldown_state",
            fetch="all"
        ) or []
        for venue, sym, ts in rows:
            self._cooldown_cache[(venue, sym)] = float(ts)

        # tp
        for prefix, venue in (("bin", "binance"), ("okx", "okx")):
            tp = self._exec(
                f"SELECT symbol, tp_armed, tp_tier, local_top, moonbag_done, floor_pct, "
                f"COALESCE(partial_done, 0) FROM {prefix}_tp_state", fetch="all"
            ) or []
            for r in tp:
                self._tp_cache[(venue, r[0])] = {
                    "armed": bool(r[1]), "tier": int(r[2] or 0),
                    "top": float(r[3] or 0.0), "moonbag": bool(r[4] or 0),
                    "floor_pct": float(r[5] or 0.0),
                    "partial": bool(r[6] or 0),
                }
            dca = self._exec(
                f"SELECT symbol, dca_armed, dca_level, local_bottom, dca_count "
                f"FROM {prefix}_dca_state", fetch="all"
            ) or []
            for r in dca:
                self._dca_cache[(venue, r[0])] = {
                    "armed": bool(r[1]), "level": float(r[2] or 0.0),
                    "bottom": float(r[3] or float("inf")),
                    "count": int(r[4] or 0),
                }

    # ---- caja negra ----
    def log_caja_negra(self, modulo, symbol, mensaje):
        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._exec(
            "INSERT INTO sys_caja_negra (ts, modulo, symbol, mensaje) VALUES (?,?,?,?)",
            (ahora, modulo, symbol, mensaje)
        )

    def get_ultimos_caja_negra(self, n=5):
        return self._exec(
            "SELECT ts, modulo, symbol, mensaje FROM sys_caja_negra ORDER BY id DESC LIMIT ?",
            (n,), fetch="all"
        ) or []

    # ---- portfolio ----
    def get_active_portfolio(self, venue):
        if self._portfolio_cache_valid.get(venue):
            return self._portfolio_cache_data[venue]
        prefix = "bin" if venue == "binance" else "okx"
        rows = self._exec(
            f"SELECT symbol, average_cost, total_qty, initial_investment, mode "
            f"FROM {prefix}_portfolio", fetch="all"
        ) or []
        data = [
            (r[0], float(r[1]), float(r[2]), float(r[3] or 0.0), r[4] or "ACTIVE")
            for r in rows if float(r[2]) > 0
        ]
        self._portfolio_cache_data[venue] = data
        self._portfolio_cache_valid[venue] = True
        return data

    def update_portfolio(self, venue, sym, qty, cost, inv=None, mode=None):
        prefix = "bin" if venue == "binance" else "okx"
        if inv is not None or mode is not None:
            existing = self._exec(
                f"SELECT initial_investment, mode FROM {prefix}_portfolio WHERE symbol=?",
                (sym,), fetch="one"
            )
            inv_val = inv if inv is not None else (existing[0] if existing else 0.0)
            mode_val = mode if mode is not None else (existing[1] if existing else "ACTIVE")
            self._exec(
                f"INSERT INTO {prefix}_portfolio VALUES (?,?,?,?,?) "
                f"ON CONFLICT(symbol) DO UPDATE SET "
                f"average_cost=excluded.average_cost, total_qty=excluded.total_qty, "
                f"initial_investment=excluded.initial_investment, mode=excluded.mode",
                (sym, float(cost), float(qty), float(inv_val), mode_val)
            )
        else:
            self._exec(
                f"UPDATE {prefix}_portfolio SET average_cost=?, total_qty=? WHERE symbol=?",
                (float(cost), float(qty), sym)
            )
        self._portfolio_cache_valid[venue] = False

    def delete_portfolio(self, venue, sym):
        prefix = "bin" if venue == "binance" else "okx"
        self._exec(f"DELETE FROM {prefix}_portfolio WHERE symbol=?", (sym,))
        self._portfolio_cache_valid[venue] = False

    def set_portfolio_mode(self, venue, sym, mode):
        prefix = "bin" if venue == "binance" else "okx"
        self._exec(f"UPDATE {prefix}_portfolio SET mode=? WHERE symbol=?", (mode, sym))
        self._portfolio_cache_valid[venue] = False

    # ---- cooldown ----
    def get_cooldown(self, venue, sym):
        return self._cooldown_cache.get((venue, sym), 0.0)

    def set_cooldown(self, venue, sym, ts):
        prefix = "bin" if venue == "binance" else "okx"
        self._cooldown_cache[(venue, sym)] = float(ts)
        self._exec(
            f"INSERT INTO {prefix}_cooldown_state VALUES (?,?) "
            f"ON CONFLICT(symbol) DO UPDATE SET last_buy_time=excluded.last_buy_time",
            (sym, float(ts))
        )

    # ---- tp / dca caches ----
    def get_tp_state(self, venue, sym):
        return self._tp_cache.get(
            (venue, sym),
            {"armed": False, "tier": 0, "top": 0.0, "moonbag": False,
             "floor_pct": 0.0, "partial": False}
        )

    def save_tp_state(self, venue, sym, armed, tier, top, moonbag=False,
                       floor_pct=None, partial=None):
        prev = self._tp_cache.get((venue, sym), {})
        new_floor = float(floor_pct) if floor_pct is not None else prev.get("floor_pct", 0.0)
        new_partial = bool(partial) if partial is not None else prev.get("partial", False)
        self._tp_cache[(venue, sym)] = {
            "armed": bool(armed), "tier": int(tier), "top": float(top),
            "moonbag": bool(moonbag), "floor_pct": new_floor, "partial": new_partial,
        }
        prefix = "bin" if venue == "binance" else "okx"
        self._exec(
            f"INSERT INTO {prefix}_tp_state VALUES (?,?,?,?,?,?,?) "
            f"ON CONFLICT(symbol) DO UPDATE SET tp_armed=excluded.tp_armed, "
            f"tp_tier=excluded.tp_tier, local_top=excluded.local_top, "
            f"moonbag_done=excluded.moonbag_done, floor_pct=excluded.floor_pct, "
            f"partial_done=excluded.partial_done",
            (sym, int(armed), int(tier), float(top), int(moonbag), new_floor, int(new_partial))
        )

    def get_dca_state(self, venue, sym):
        return self._dca_cache.get(
            (venue, sym),
            {"armed": False, "level": 0.0, "bottom": float("inf"), "count": 0}
        )

    def save_dca_state(self, venue, sym, armed, level, bottom, count=None):
        prev_count = self._dca_cache.get((venue, sym), {}).get("count", 0)
        new_count = int(count) if count is not None else prev_count
        self._dca_cache[(venue, sym)] = {
            "armed": bool(armed), "level": float(level),
            "bottom": float(bottom), "count": new_count,
        }
        prefix = "bin" if venue == "binance" else "okx"
        self._exec(
            f"INSERT INTO {prefix}_dca_state VALUES (?,?,?,?,?) "
            f"ON CONFLICT(symbol) DO UPDATE SET dca_armed=excluded.dca_armed, "
            f"dca_level=excluded.dca_level, local_bottom=excluded.local_bottom, "
            f"dca_count=excluded.dca_count",
            (sym, int(armed), float(level), float(bottom), new_count)
        )

    # ---- ARB ----
    def vacuum(self):
        """Purga > 7d caja_negra + > 30d ml_feature_log + VACUUM."""
        try:
            cutoff_cn = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
            self._exec("DELETE FROM sys_caja_negra WHERE ts < ?", (cutoff_cn,))
            cutoff_ml = int((time.time() - 30 * 86400) * 1000)
            self._exec(
                "DELETE FROM ml_feature_log WHERE "
                "CAST(SUBSTR(id, INSTR(id, '_') + 1) AS INTEGER) < ?",
                (cutoff_ml,)
            )
            with self.lock:
                self.conn.isolation_level = None
                self.conn.execute("VACUUM")
                self.conn.isolation_level = ""
            self.log_caja_negra("SISTEMA", "DB", "VACUUM completado")
        except Exception as e:
            logger_errores.error(f"VACUUM: {e}")


# =============================================================================
# 6. TRADING CORE BASE — utilidades compartidas (heredan TradingCoreBin/Okx)
# =============================================================================
class TradingCoreBase:
    """Atributos y métodos comunes a los TradingCore por venue."""

    def __init__(self, venue: str, db: DatabaseManager):
        self.venue = venue
        self.db = db
        self.exchange_trade = None      # ccxt con keys (cada hijo lo construye)
        self.exchange_read = None       # ccxt público
        self.exchange_ws = None         # ccxtpro (puede ser None si no disponible)
        self.balance_cache = {"data": None, "ts": 0.0}
        self.balance_lock = threading.Lock()
        self.reserved_capital_global = 0.0
        self.reserved_capital_per_sym = {}

    def get_balance(self, force_refresh=False):
        """TTL 3s. Devuelve dict de balance free/used/total."""
        with self.balance_lock:
            if (not force_refresh and self.balance_cache["data"] and
                    (time.time() - self.balance_cache["ts"]) < 3.0):
                return self.balance_cache["data"]
            try:
                if self.exchange_trade is None:
                    return {}
                bal = okx_retry(self.exchange_trade.fetch_balance)
                self.balance_cache = {"data": bal, "ts": time.time()}
                return bal
            except Exception as e:
                logger_errores.error(f"{self.venue} get_balance: {e}")
                return self.balance_cache["data"] or {}

    def invalidate_balance(self):
        with self.balance_lock:
            self.balance_cache = {"data": None, "ts": 0.0}


# =============================================================================
# 7. MOTOR INDICADORES — compartido, calcula 15m ADX/RSI/MACD/EMA/ATR
# =============================================================================
class MotorIndicadores(threading.Thread):
    """Calcula indicadores 15m por (venue, sym) en ohlcv_cache. Sin REST inline."""

    CICLO_S = 60.0

    def __init__(self, core_bin, core_okx, db: DatabaseManager):
        super().__init__(name="MotorIndicadores", daemon=True)
        self.core_bin = core_bin
        self.core_okx = core_okx
        self.db = db

    def _scan_venue(self, venue: str, exchange, pares):
        for sym in pares:
            try:
                velas = okx_retry(exchange.fetch_ohlcv, sym, "15m", limit=250)
                if not velas:
                    continue
                df = pd.DataFrame(velas, columns=["ts", "open", "high", "low", "close", "volume"])
                for c in ["open", "high", "low", "close", "volume"]:
                    df[c] = pd.to_numeric(df[c], errors="coerce")

                df.ta.rsi(length=14, append=True)
                df.ta.adx(length=14, append=True)
                df.ta.macd(fast=12, slow=26, signal=9, append=True)
                df.ta.atr(length=14, append=True)
                df["EMA_200"] = ta.ema(df["close"], length=200)
                df = df.dropna().reset_index(drop=True)
                if df.empty:
                    continue

                with cache_lock:
                    ohlcv_cache[venue][sym] = {"df": df, "ts": time.time()}

                last = df.iloc[-1]
                logger_indicadores.registrar(
                    venue=venue, sym=sym,
                    close=round(float(last["close"]), 6),
                    rsi=round(float(last.get("RSI_14", 0)), 2),
                    adx=round(float(last.get("ADX_14", 0)), 2),
                    macd=round(float(last.get("MACDh_12_26_9", 0)), 6),
                    ema200=round(float(last["EMA_200"]), 6),
                )
            except Exception as e:
                logger_errores.error(f"MotorIndicadores {venue} {sym}: {e}")
            time.sleep(0.3)

    def _bootstrap_panico_btc(self):
        """Fix V1.2.2 #7: rellena btc_price_history con OHLCV 1m × 60 al arranque,
        evita el fail-open ~5 min iniciales. Si arrancamos durante un crash, lo detecta."""
        try:
            velas = self.core_bin.exchange_read.fetch_ohlcv("BTC/USDT", "1m", limit=60)
            if not velas:
                return
            with panico_lock:
                for v in velas:
                    ts_s = float(v[0]) / 1000.0
                    close = float(v[4])
                    btc_price_history.append((ts_s, close))
            logger_errores.info(f"Pánico BTC bootstrap: {len(velas)} velas 1m cargadas")
        except Exception as e:
            logger_errores.error(f"_bootstrap_panico_btc: {e}")

    def _actualizar_panico_btc(self):
        """Pánico Global BTC (auditoría okx_dca V12). Lee BTC bid de PRECIOS_RAM,
        mantiene historial 1h, si max-min/max ≥ PANIC_DROP_PCT activa gate panico_global."""
        global panico_global, panico_global_until
        try:
            entry = PRECIOS_RAM.get("okx", {}).get("BTC/USDT")
            if not entry or not is_price_fresh("okx", "BTC/USDT"):
                return
            now = time.time()
            btc_price_history.append((now, entry["bid"]))
            # Filtra > 1h
            cutoff = now - 3600
            while btc_price_history and btc_price_history[0][0] < cutoff:
                btc_price_history.popleft()
            if len(btc_price_history) < 5:
                return
            prices = [p for _, p in btc_price_history]
            max_p = max(prices)
            drop_pct = (max_p - entry["bid"]) / max_p * 100 if max_p > 0 else 0.0
            with panico_lock:
                prev = panico_global
                if drop_pct >= CONFIG["panic_drop_pct"]:
                    panico_global = True
                    panico_global_until = now + CONFIG["panic_block_minutes"] * 60
                elif now >= panico_global_until:
                    panico_global = False
                if panico_global != prev:
                    estado = "ON" if panico_global else "OFF"
                    self.db.log_caja_negra("PANICO_BTC", "BTC/USDT",
                                            f"{estado} drop {drop_pct:+.2f}%/1h "
                                            f"until {datetime.fromtimestamp(panico_global_until).strftime('%H:%M')}")
        except Exception as e:
            logger_errores.error(f"MotorIndicadores panico_btc: {e}")

    def _actualizar_funding(self):
        """Punto C: refresca FUNDING_RAM para los perps del short (BTC/ETH). Solo cuando el
        contexto de funding está activo. Una llamada por perp por ciclo (overhead bajo)."""
        for perp in PARES_SHORT_OKX:
            try:
                fr = self.core_okx.exchange_read.fetch_funding_rate(perp)
                set_funding(perp, float(fr.get("fundingRate", 0.0) or 0.0))
            except Exception as e:
                logger_errores.error(f"MotorIndicadores funding {perp}: {e}")

    def run(self):
        global indicadores_evaluado
        time.sleep(2.0)
        # Bootstrap pánico BTC (fix V1.2.2 #7) — ANTES del loop principal
        if self.core_bin and self.core_bin.exchange_read:
            self._bootstrap_panico_btc()
        while not kill_switch.is_set():
            try:
                # Incluye pares base + cualquier sym con posición abierta (port_syms)
                bin_port = [r[0] for r in self.db.get_active_portfolio("binance")]
                okx_port = [r[0] for r in self.db.get_active_portfolio("okx")]
                bin_pares = list(set(PARES_BIN_SPOT) | set(bin_port))
                okx_pares = list(set(PARES_OKX_SPOT) | set(okx_port))

                if self.core_bin and self.core_bin.exchange_read:
                    self._scan_venue("binance", self.core_bin.exchange_read, bin_pares)
                if self.core_okx and self.core_okx.exchange_read:
                    self._scan_venue("okx", self.core_okx.exchange_read, okx_pares)

                # Pánico Global BTC — depende de PRECIOS_RAM (WS)
                self._actualizar_panico_btc()

                # Funding de los perps (Punto C contexto / Punto A feature ML) — si algo lo usa.
                if (SHORT_FUNDING_CONTEXT or ML_FUNDING_FEATURE) and self.core_okx and self.core_okx.exchange_read:
                    self._actualizar_funding()

                # Fix V1.2.2 #6: marca warmup completo tras primera vuelta exitosa
                # → is_vol_spike pasa de fail-closed a operación normal
                indicadores_evaluado = True
            except Exception as e:
                logger_errores.error(f"MotorIndicadores loop: {e}")
            time.sleep(self.CICLO_S)

    @staticmethod
    def get_indicators(venue, sym, max_age_s=120.0):
        """Devuelve la última fila de indicadores; None si stale o ausente."""
        with cache_lock:
            entry = ohlcv_cache.get(venue, {}).get(sym)
        if not entry:
            return None
        if (time.time() - entry["ts"]) > max_age_s:
            return None
        return entry["df"].iloc[-1].to_dict()


# =============================================================================
# 7b. CEREBRO DE RÉGIMEN 4-estados V1.8.0 — clasificador puro + playbook
# =============================================================================
def clasificar_regimen(inp):
    """Taxonomía POSITIVA de 4 estados desde señales que el Selector ya calcula.
    `inp` = dict con desv (BTC vs EMA200d), slope (pendiente EMA200), adx, atr (norm).
    Orden de prioridad (el primero que matchea gana); fallback = RANGO (residuo).
    Devuelve (estado, razon). Pura: sin red, sin estado global."""
    desv = float(inp.get("desv", 0.0))
    slope = float(inp.get("slope", 0.0))
    adx = float(inp.get("adx", 0.0))
    atr = float(inp.get("atr", 0.0))

    if atr > SELECTOR_ATR_VOL_HIGH:
        return REGIMEN_CAOS, f"ATR {atr:.3f}>{SELECTOR_ATR_VOL_HIGH}"
    if desv < SELECTOR_BEAR_EMA_PCT and slope < 0:
        return REGIMEN_TEND_DOWN, f"desv {desv:+.3f} & slope<0"
    if desv > SELECTOR_BULL_EMA_PCT and slope > 0 and adx >= REGIMEN_ADX_TREND:
        return REGIMEN_TEND_UP, f"desv {desv:+.3f} & slope>0 & ADX {adx:.0f}"
    if adx < REGIMEN_ADX_RANGO and abs(desv) <= REGIMEN_DESV_RANGO:
        return REGIMEN_RANGO, f"ADX {adx:.0f}<{REGIMEN_ADX_RANGO:.0f} & |desv|≤{REGIMEN_DESV_RANGO}"
    return REGIMEN_RANGO, "fallback"


def regimen_propio(velas):
    """Régimen 4-clases del PROPIO activo (gate del sleeve ladder SP3): corre clasificar_regimen
    sobre velas diarias OHLCV [ts,o,h,l,c,v] del activo. Un alt en declive secular clasifica
    TENDENCIA_DOWN en su marco → el ladder se congela ahí solo. Pura. Fail-safe: datos
    insuficientes/NaN/error → CAOS (no opera). Espejo in-process de _regimen_estado_por_ts."""
    slope_d = SELECTOR_EMA_SLOPE_DAYS
    if not velas or len(velas) < 200 + slope_d + 14:
        return REGIMEN_CAOS
    try:
        df = pd.DataFrame(velas, columns=["ts", "open", "high", "low", "close", "volume"])
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df.ta.adx(length=14, append=True)
        df.ta.atr(length=14, append=True)
        df["EMA_200"] = ta.ema(df["close"], length=200)
        df = df.dropna().reset_index(drop=True)
        if len(df) <= slope_d:
            return REGIMEN_CAOS
        closes, ema = df["close"].tolist(), df["EMA_200"].tolist()
        i = len(df) - 1
        desv = (closes[i] - ema[i]) / ema[i] if ema[i] else 0.0
        slope = (ema[i] - ema[i - slope_d]) / ema[i - slope_d] if ema[i - slope_d] else 0.0
        atr = float(df["ATRr_14"].iloc[i]) / closes[i] if closes[i] else 0.0
        adx = float(df["ADX_14"].iloc[i])
        estado, _ = clasificar_regimen(dict(desv=desv, slope=slope, adx=adx, atr=atr))
        return estado
    except Exception:
        return REGIMEN_CAOS


LADDER_ESTADOS_TERMINALES = ("canceled", "cancelled", "expired", "rejected")


def ladder_accion_reconcile(status, filled):
    """Qué hacer con una orden 'open' de ladder_orders según lo que OKX dice de ella.

    'fill' → contabilizarla (usando la qty REALMENTE llena); 'terminal' → murió sin dejar
    nada, liberar el rung; 'esperar' → sigue viva o no se puede saber.

    El reconcile hacía `if status not in ("closed","filled"): continue`, así que una orden
    cancelada/expirada/rechazada quedaba state='open' PARA SIEMPRE: _comprometido seguía
    contando su nocional (comía del cap sin exposición real) y _estado la veía 'pending',
    de modo que el rung no se re-armaba nunca — y sin una línea en el log. El core sí
    maneja los tres estados (okx_grid_orders); el ladder no.

    Una terminal CON fill parcial no es terminal: esa cripto existe en la cuenta. Tratarla
    como muerta la volvería inventario huérfano que el ladder nunca vende (directiva 20: el
    dato que falta no se rellena, pero el que SÍ está no se tira). Un status ausente o
    desconocido es 'esperar', nunca una conclusión."""
    if status in ("closed", "filled"):
        return "fill"
    if status in LADDER_ESTADOS_TERMINALES:
        return "fill" if filled > 0 else "terminal"
    return "esperar"


def ladder_acciones(estado, precio, regimen, cfg):
    """Planner PURO del ladder (SP3): dado el estado de rungs + precio + régimen + cfg, devuelve
    las acciones del tick. Tipos: 'buy' (arma escalera en RANGO sin ref), 'stop' (holding underwater
    >stop_pct), 'flatten' (cierra holdings al salir de RANGO). Las 'sell'/'rearm' las maneja el
    reconcile (driven por fills). Espejo de la mecánica de sim_grid_ladder, sin I/O."""
    acc = []
    rungs = estado.get("rungs", [])
    if regimen != REGIMEN_RANGO:
        for r in rungs:                                   # fuera de RANGO: flatten, no arma
            if r.get("state") == "holding":
                acc.append({"tipo": "flatten", "rung_idx": r.get("rung_idx"), "qty": r.get("qty", 0.0)})
        return acc
    stop_pct = cfg["stop_pct"]                            # RANGO: stop a holdings underwater
    for r in rungs:
        if r.get("state") == "holding":
            buy_px = r.get("buy_px", 0.0)
            if buy_px > 0 and precio <= buy_px * (1 - stop_pct / 100.0):
                acc.append({"tipo": "stop", "rung_idx": r.get("rung_idx"), "qty": r.get("qty", 0.0)})
    if estado.get("ref") is None:                         # arma la escalera si no hay ref
        S, monto = cfg["spacing_pct"], cfg["monto_rung"]
        armados = [r for r in rungs if r.get("state") == "armed"]
        for k, r in enumerate(armados, 1):
            price = precio * (1 - k * S / 100.0)
            acc.append({"tipo": "buy", "rung_idx": r.get("rung_idx", k - 1),
                        "price": price, "qty": monto / price})
    return acc


def hud_linea_ladder(sym, est, ahora, tick_s):
    """Línea de HUD del sleeve ladder para UN par. Pura (formato, sin I/O).

    Existe porque un ladder congelado por su régimen propio no dejaba rastro alguno:
    ni orden, ni log, ni línea de HUD. Desde fuera era idéntico a un hilo muerto —la
    misma trampa que ya costó 11.5 h de canary mudo (ver el grito de SIN PRECIO en
    `_tick`), tapada allí solo para una de las causas—. Directiva 20: la ausencia de
    dato se grita.

    Precedencia deliberada: SIN ESTADO > STALE > congelado/opera. Un estado viejo NO
    puede leerse como "congelado y todo en orden": el hilo colgado es el diagnóstico
    grave y tiene que ganar.
    """
    if est is None:
        return (f"[HUD] LADDER {sym:<12} SIN ESTADO — el hilo ladder no ha publicado "
                f"(¿arrancó? ¿LADDER_ENABLED?)")
    edad = ahora - float(est.get("ts") or 0.0)
    umbral = max(3.0 * float(tick_s), 180.0)
    if edad > umbral:
        return (f"[HUD] LADDER {sym:<12} STALE {edad:.0f}s sin tick (ciclo {tick_s:.0f}s) "
                f"— el hilo puede estar colgado")
    motivo = est.get("motivo")
    estado = "OPERA" if not motivo else f"CONGELADO({motivo})"
    px = est.get("precio")
    px_s = f"{px:.6g}" if px else "-"
    return (f"[HUD] LADDER {sym:<12} {est.get('regimen', '?'):<14} {estado:<20} "
            f"rungs a{est.get('armed', 0)}/p{est.get('pending', 0)}/h{est.get('holding', 0)}  "
            f"comp={float(est.get('comprometido', 0.0)):.2f}/{float(est.get('cap', 0.0)):.2f}  "
            f"px={px_s}")


def map_confianza(conf):
    """Confianza [0,1] → multiplicador de sizing acotado [min,max]. Lineal + clamp."""
    lo = CONFIG["regimen_conf_min_mult"]
    hi = CONFIG["regimen_conf_max_mult"]
    c = max(0.0, min(1.0, float(conf)))
    return lo + c * (hi - lo)


def regimen_factor_estado(estado):
    """Playbook: factor de sizing del grid por estado. RANGO ≥ TENDENCIA_UP; congela el resto.
    El grid cosecha de la oscilación (RANGO = su hábitat), solo acompaña en TENDENCIA_UP."""
    if estado == REGIMEN_RANGO:
        return CONFIG["regimen_factor_rango"]
    if estado == REGIMEN_TEND_UP:
        return CONFIG["regimen_factor_tend_up"]
    return 0.0   # TENDENCIA_DOWN / CAOS → congela


def regimen_opera(estado):
    """True si el grid puede abrir entradas en este estado (RANGO o TENDENCIA_UP)."""
    return regimen_factor_estado(estado) > 0.0


def grid_regimen_decision(mode, modo_mercado_bin, estado, conf, evaluado=True):
    """El CABLE entre el Cerebro de Régimen y el Grid. Devuelve (opera, size_mult).
      - mode='shadow' (default): comportamiento IDÉNTICO al actual — opera si el binario
        modo_mercado no es BAJISTA, sin sizing de régimen (mult=1.0). Cero cambio.
      - mode='live': el 4-estados gobierna — opera solo en RANGO/TENDENCIA_UP (congela
        TENDENCIA_DOWN/CAOS) y el sizing = factor_estado × map_confianza.
    `evaluado` (V1.8.6): guard de WARMUP. El cerebro arranca en RANGO(0.50)=operable por defecto
    y el Selector recién clasifica ~60s post-boot. Sin este guard, el grid entraba en esa ventana
    sobre el default optimista ANTES de saber el régimen real (visto en vivo 2026-06-21: 2 órdenes
    BTC/ETH puestas antes de clasificar TENDENCIA_DOWN). evaluado=False → NO opera (espera al cerebro).
    Pura: el grid llama esto y aplica el resultado; no decide acá nada de mercado."""
    if not evaluado:
        return False, 0.0
    if mode == "live":
        return regimen_opera(estado), regimen_factor_estado(estado) * map_confianza(conf)
    return (modo_mercado_bin != "BAJISTA"), 1.0


# =============================================================================
# 8. RADAR RÉGIMEN — BTC vs EMA200d → modo_mercado + factor_capital
# =============================================================================
class RadarRegimen(threading.Thread):
    CICLO_S = 1800.0

    def __init__(self, core_bin, db: DatabaseManager):
        super().__init__(name="RadarRegimen", daemon=True)
        self.core_bin = core_bin
        self.db = db

    def _evaluar(self):
        global modo_mercado, factor_capital, regime_evaluado
        try:
            velas = self.core_bin.exchange_read.fetch_ohlcv("BTC/USDT", "1d", limit=300)
            if not velas or len(velas) < 200:
                return
            df = pd.DataFrame(velas, columns=["ts", "o", "h", "l", "c", "v"])
            df["c"] = pd.to_numeric(df["c"], errors="coerce")
            ema200 = ta.ema(df["c"], length=200).iloc[-1]
            close = df["c"].iloc[-1]
            desv = (close - ema200) / ema200

            with regime_lock:
                prev_modo = modo_mercado
                if desv < REGIME_ENTER_BEAR:
                    nuevo = "BAJISTA"
                elif desv > REGIME_EXIT_BEAR:
                    nuevo = "ALCISTA"
                else:
                    nuevo = modo_mercado  # histéresis

                modo_mercado = nuevo
                factor_capital = max(REGIME_FLOOR, min(1.0, 1.0 + REGIME_K * min(desv, 0.0)))
                regime_evaluado = True

            if prev_modo != modo_mercado:
                self.db.log_caja_negra(
                    "REGIMEN", "BTC",
                    f"{prev_modo} → {modo_mercado} (desv {desv:+.3f}, factor {factor_capital:.2f})"
                )
        except Exception as e:
            logger_errores.error(f"RadarRegimen: {e}")

    def run(self):
        time.sleep(5.0)
        while not kill_switch.is_set():
            self._evaluar()
            for _ in range(int(self.CICLO_S)):
                if kill_switch.is_set():
                    return
                time.sleep(1.0)


# =============================================================================
# 9. SELECTOR — V18.2 adaptativo, decide modo_operacion (afecta BIN_FUT)
# =============================================================================
class Selector(threading.Thread):
    TICK_S = SELECTOR_TICK_SECONDS

    def __init__(self, core_bin, db: DatabaseManager):
        super().__init__(name="Selector", daemon=True)
        self.core_bin = core_bin
        self.db = db
        self._last_change_ts = 0.0
        self._sostenido_desde: float | None = None
        self._sostenido_modo: str | None = None
        # Cerebro de Régimen 4-estados (histéresis liviana por confirmación sostenida)
        self._regimen_cand = None
        self._regimen_cand_count = 0

    def _publicar_regimen(self, inp):
        """V1.8.0 — clasifica el 4-estados y lo publica en regimen_mercado bajo regime_lock.
        Histéresis liviana: un estado distinto al vigente debe sostenerse REGIMEN_HYST_TICKS
        evaluaciones consecutivas antes de commitear (anti-flapping en los umbrales). El boot
        publica inmediato. NO toca la lógica SPOT/FLAT ni el grid: solo provee la señal
        (el grid la consume solo en REGIMEN_BRAIN_MODE=live)."""
        global regimen_mercado, regimen_confianza, regimen_razon, regimen_evaluado
        estado, razon = clasificar_regimen(inp)
        conf = self._calcular_confianza(inp)

        with regime_lock:
            actual = regimen_mercado
            # boot o sin cambio: publica directo
            if not regimen_evaluado or estado == actual:
                regimen_mercado = estado
                regimen_confianza = conf
                regimen_razon = razon
                if not regimen_evaluado:
                    regimen_evaluado = True
                    self.db.log_caja_negra("REGIMEN_BRAIN", "BTC",
                                            f"BOOT → {estado} (conf {conf:.2f}, {razon})")
                self._regimen_cand = None
                self._regimen_cand_count = 0
                return

            # cambio propuesto: requiere confirmación sostenida
            if self._regimen_cand != estado:
                self._regimen_cand = estado
                self._regimen_cand_count = 1
                return
            self._regimen_cand_count += 1
            if self._regimen_cand_count < REGIMEN_HYST_TICKS:
                return

            prev = regimen_mercado
            regimen_mercado = estado
            regimen_confianza = conf
            regimen_razon = razon
            self._regimen_cand = None
            self._regimen_cand_count = 0
        self.db.log_caja_negra("REGIMEN_BRAIN", "BTC",
                                f"{prev} → {estado} (conf {conf:.2f}, {razon}, mode={CONFIG['regimen_brain_mode']})")

    def _inputs(self):
        """Recolecta inputs: BTC EMA200d/ATR/ADX/slope, BTC/ETH ratio, funding, atr_15m."""
        try:
            velas = self.core_bin.exchange_read.fetch_ohlcv("BTC/USDT", "1d", limit=300)
            # Fix V1.2.4 #3: pandas_ta.adx() / .atr() requieren columnas 'high'/'low'/'close',
            # no abreviadas. Antes daba KeyError 'ADX_14' silencioso (no se generaba la col).
            df = pd.DataFrame(velas, columns=["ts", "open", "high", "low", "close", "volume"])
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df.ta.adx(length=14, append=True)
            df.ta.atr(length=14, append=True)
            ema200 = ta.ema(df["close"], length=200)
            close = df["close"].iloc[-1]
            desv = (close - ema200.iloc[-1]) / ema200.iloc[-1]
            slope = (ema200.iloc[-1] - ema200.iloc[-1 - SELECTOR_EMA_SLOPE_DAYS]) / ema200.iloc[-1 - SELECTOR_EMA_SLOPE_DAYS]
            adx = float(df["ADX_14"].iloc[-1] or 0)
            atr = float(df["ATRr_14"].iloc[-1] or 0) / close

            # ETH ratio
            eth = self.core_bin.exchange_read.fetch_ohlcv("ETH/USDT", "1d", limit=8)
            eth_close = [v[4] for v in eth]
            btc_close = [v[4] for v in velas[-8:]]
            ratio_now = btc_close[-1] / eth_close[-1]
            ratio_prev = btc_close[0] / eth_close[0]
            ratio_slope = (ratio_now - ratio_prev) / ratio_prev

            # Funding BTC perp (público, sin keys necesarias en CCXT)
            try:
                fr = self.core_bin.exchange_read.fetch_funding_rate("BTC/USDT:USDT")
                funding = float(fr.get("fundingRate", 0.0))
            except Exception:
                funding = 0.0

            return dict(desv=desv, slope=slope, adx=adx, atr=atr,
                        ratio_slope=ratio_slope, funding=funding)
        except Exception as e:
            logger_errores.error(f"Selector inputs: {e}")
            return None

    @staticmethod
    def _decidir_modo(inp):
        if inp["atr"] > SELECTOR_ATR_VOL_HIGH:
            return "FLAT", "ATR_HIGH_HARD"
        if abs(inp["funding"]) > SELECTOR_FUNDING_PATOL:
            return "FLAT", "FUNDING_PATOL_HARD"
        # V1.4.0: removido directional FUT. Selector solo distingue SPOT vs FLAT.
        # BEAR_TREND y BEAR_WEAK ambos → FLAT (no abrir spot en bajista).
        # BULL → SPOT (Grid+Harvest+DCA captura tendencia alcista).
        if inp["desv"] < -0.05 and inp["slope"] < 0:
            return "FLAT", "BEAR"
        if inp["desv"] > SELECTOR_BULL_EMA_PCT:
            return "SPOT", "BULL"
        return "FLAT", "INDECISA"

    @staticmethod
    def _calcular_confianza(inp):
        thr = SELECTOR_BEAR_EMA_PCT if inp["desv"] < 0 else SELECTOR_BULL_EMA_PCT
        margen = min(1.0, abs(inp["desv"] - thr) / 0.10)
        slope_n = min(1.0, abs(inp["slope"]) / 0.10)
        adx_n = min(1.0, inp["adx"] / 40.0)
        ratio_n = min(1.0, abs(inp["ratio_slope"]) / 0.10)
        atr_n = max(0.0, 1.0 - inp["atr"] / 0.06)
        return 0.30 * margen + 0.20 * slope_n + 0.20 * adx_n + 0.20 * ratio_n + 0.10 * atr_n

    def _aplicar_histeresis(self, modo_propuesto, razon, conf):
        """V1.4.0: simplificado. Solo modos SPOT/FLAT. force_close removido (era
        para cerrar posiciones FUT_LONG/FUT_SHORT en transiciones, ya no existen)."""
        global modo_operacion, modo_confianza, modo_razon, selector_evaluado
        ahora = time.time()
        hard_filter = razon.endswith("HARD")

        with regime_lock:
            actual = modo_operacion
            if not selector_evaluado:
                modo_operacion = modo_propuesto
                modo_confianza = conf
                modo_razon = razon
                selector_evaluado = True
                self._last_change_ts = ahora
                return

            if actual == modo_propuesto:
                self._sostenido_desde = None
                self._sostenido_modo = None    # V1.9.11: el par (modo,desde) se resetea junto;
                modo_confianza = conf           # si no, un _modo stale saltaba la re-init → float-None en 2423
                return

            # Hard filter → FLAT inmediato
            if hard_filter and modo_propuesto == "FLAT":
                self._commit(modo_propuesto, razon, conf, ahora)
                return

            # Cooldown
            if (ahora - self._last_change_ts) < SELECTOR_COOLDOWN_HOURS * 3600:
                return

            # Histéresis adaptativa
            hist_h = max(6.0, min(36.0, 36 - 30 * conf))
            if self._sostenido_modo != modo_propuesto:
                self._sostenido_modo = modo_propuesto
                self._sostenido_desde = ahora
            assert self._sostenido_desde is not None   # invariante: (_modo,_desde) van juntos (V1.9.11)
            sostenido_h = (ahora - self._sostenido_desde) / 3600.0
            if sostenido_h < hist_h:
                return

            self._commit(modo_propuesto, razon, conf, ahora)

    def _commit(self, modo, razon, conf, ts):
        global modo_operacion, modo_confianza, modo_razon
        prev = modo_operacion
        modo_operacion = modo
        modo_confianza = conf
        modo_razon = razon
        self._last_change_ts = ts
        self._sostenido_modo = None
        self._sostenido_desde = None
        self.db.log_caja_negra(
            "SELECTOR", "BTC",
            f"{prev} → {modo} (razón={razon}, conf={conf:.2f})"
        )

    def run(self):
        time.sleep(60.0)  # espera primer ciclo MotorIndicadores
        while not kill_switch.is_set():
            try:
                inp = self._inputs()
                if inp:
                    modo_prop, razon = self._decidir_modo(inp)
                    conf = self._calcular_confianza(inp)
                    self._aplicar_histeresis(modo_prop, razon, conf)
                    self._publicar_regimen(inp)   # V1.8.0 Cerebro de Régimen 4-estados
            except Exception as e:
                logger_errores.error(f"Selector loop: {e}")
            for _ in range(int(self.TICK_S)):
                if kill_switch.is_set():
                    return
                time.sleep(1.0)


# =============================================================================
# 10. KILL SWITCH MONITOR — universal, vigila archivo "KILL"
# =============================================================================
class KillSwitchMonitor(threading.Thread):
    TICK_S = 5.0

    def __init__(self, db: DatabaseManager):
        super().__init__(name="KillSwitchMonitor", daemon=True)
        self.db = db

    def run(self):
        while True:
            if kill_switch.is_set():
                # Ya activado; sigue vigilando para logging pero no hace nada
                time.sleep(self.TICK_S)
                continue
            if os.path.exists("KILL"):
                kill_switch.set()
                self.db.log_caja_negra("KILL_SWITCH", "GLOBAL",
                                        "Archivo KILL detectado — cierre forzado de TODAS las posiciones")
                logger_errores.warning("KILL_SWITCH activado")
                return
            time.sleep(self.TICK_S)


# =============================================================================
# 11. RADAR TELEMETRÍA — HUD multi-panel, detector único
# =============================================================================
class RadarTelemetria(threading.Thread):
    def __init__(self, core_okx, db: DatabaseManager):
        super().__init__(name="RadarTelemetria", daemon=True)
        self.core_okx = core_okx
        self.db = db
        self.tick_s = CONFIG["hud_refresh_seconds"]

    def _color_modo(self, modo):
        return {"SPOT": "green", "FLAT": "yellow"}.get(modo, "white")

    def _free_usdt(self, core):
        """USDT libre en Trading account (cache 3s)."""
        try:
            bal = core.get_balance()
            v = bal.get("USDT")
            if isinstance(v, dict):
                return float(v.get("free", 0) or 0)
            return float((bal.get("free", {}) or {}).get("USDT", 0) or 0)
        except Exception:
            return 0.0

    def _valor_cripto(self, venue):
        """Suma qty × bid de todas las posiciones spot del venue."""
        total = 0.0
        for sym, avg, qty, inv, mode in self.db.get_active_portfolio(venue):
            bid = (PRECIOS_RAM.get(venue, {}).get(sym, {}) or {}).get("bid") or avg
            total += qty * (bid or 0)
        return total

    def _earn_cripto_okx_valor(self):
        """Valor USDT del Earn cripto OKX (qty × precio actual)."""
        try:
            rows = self.db._exec(
                "SELECT ccy, qty FROM okx_inventory_earn", fetch="all"
            ) or []
        except Exception:
            return 0.0
        total = 0.0
        for ccy, qty in rows:
            sym = f"{ccy}/USDT"
            bid = (PRECIOS_RAM.get("okx", {}).get(sym, {}) or {}).get("bid") or 0
            total += float(qty or 0) * bid
        return total

    def _yield_metrics(self, ventana_dias=7):
        """Yield realizado OKX + earn estimado en los últimos N días."""
        try:
            since = (datetime.now() - timedelta(days=ventana_dias)).strftime("%Y-%m-%d %H:%M:%S")
            try:
                r = self.db._exec(
                    "SELECT COALESCE(SUM(pnl_usdt), 0.0) FROM okx_realized_pnl WHERE ts >= ?",
                    (since,), fetch="one"
                )
                spot_okx = float(r[0]) if r and r[0] is not None else 0.0
            except Exception:
                spot_okx = 0.0
            apy = CONFIG["okx_savings_apr_ref"]
            earn_total = earn_balance_usdt_okx + earn_balance_cripto_okx
            earn_est = earn_total * apy * (ventana_dias / 365.0)
            total = spot_okx + earn_est
            return {"spot_okx": spot_okx, "earn_est": earn_est,
                    "total": total, "dias": ventana_dias}
        except Exception:
            return {"spot_okx": 0.0, "earn_est": 0.0, "total": 0.0, "dias": ventana_dias}

    def _pata_short_margen_upnl(self):
        """(margen, uPnL) de las posiciones short de futuros, para el equity del HUD.

        Al abrir un short, el margen sale del USDT free → sin sumarlo el equity se
        sub-reporta. margen_i = qty*entry/lever ; uPnL_i = (entry-bid)*qty (short gana
        cuando el precio baja). Datos de la DB + precio spot (PRECIOS_RAM)."""
        if not CONFIG.get("fut_enabled"):
            return 0.0, 0.0
        margen = upnl = 0.0
        try:
            for p in self.db.short_get_posiciones():
                spot = _spot_de_perp(p["symbol"])
                bid = (PRECIOS_RAM.get("okx", {}).get(spot, {}) or {}).get("bid") or p["entry_px"]
                lev = p["lever"] or 1
                margen += p["qty"] * p["entry_px"] / lev
                upnl += (p["entry_px"] - bid) * p["qty"]
        except Exception:
            return 0.0, 0.0
        return margen, upnl

    def _capital_metrics(self):
        """CAPITAL_USUARIO / EQUITY / PROFIT derivados de DB (solo OKX).

        CAPITAL_USUARIO = Σ(inv_posiciones) + free_USDT + earn_USDT − Σ(realized) + margen_short
        EQUITY          = Σ(qty × bid) + free_USDT + earn_USDT + margen_short + uPnL_short
        (el margen short salió del free; sumarlo a ambos deja profit = uPnL de la pata)
        """
        try:
            inv_open = 0.0
            cripto_value = 0.0
            for sym, avg, qty, inv, mode in self.db.get_active_portfolio("okx"):
                inv_open += float(inv or 0)
                bid = (PRECIOS_RAM.get("okx", {}).get(sym, {}) or {}).get("bid") or avg
                cripto_value += float(qty or 0) * float(bid or 0)
            free_total = self._free_usdt(self.core_okx)
            earn_total = refrescar_earn_usdt_okx(self.core_okx)
            m_pata, u_pata = self._pata_short_margen_upnl()
            try:
                r = self.db._exec(
                    "SELECT COALESCE(SUM(pnl_usdt), 0.0) FROM okx_realized_pnl", fetch="one"
                )
                realized_total = float(r[0]) if r and r[0] is not None else 0.0
            except Exception:
                realized_total = 0.0
            capital_user = inv_open + free_total + earn_total - realized_total + m_pata
            equity = cripto_value + free_total + earn_total + m_pata + u_pata
            profit_total = equity - capital_user
            profit_pct = (profit_total / capital_user * 100) if capital_user > 0 else 0.0
            unrealized = equity - inv_open - free_total - earn_total - m_pata
            return {
                "capital_user": capital_user, "equity": equity,
                "profit_total": profit_total, "profit_pct": profit_pct,
                "unrealized": unrealized, "realized": realized_total,
                "free_total": free_total, "earn_total": earn_total,
                "cripto_value": cripto_value, "inv_open": inv_open,
                "short_val": m_pata + u_pata, "short_upnl": u_pata,
            }
        except Exception as e:
            logger_errores.error(f"_capital_metrics: {e}")
            return None

    def _hodl_dashboard(self, equity_actual):
        """Compara equity bot vs HODL hipotético desde genesis (OKX_BOT_GENESIS_*)."""
        if not CONFIG.get("hodl_dashboard_enabled", True):
            return None
        genesis_ts = CONFIG.get("okx_genesis_ts", 0.0)
        genesis_capital = CONFIG.get("okx_genesis_capital", 0.0)
        genesis_btc = CONFIG.get("okx_genesis_btc", 0.0)
        genesis_eth = CONFIG.get("okx_genesis_eth", 0.0)
        if genesis_ts <= 0 or genesis_capital <= 0:
            return None
        try:
            btc_bid = (PRECIOS_RAM.get("okx", {}).get("BTC/USDT", {}) or {}).get("bid") or 0
            eth_bid = (PRECIOS_RAM.get("okx", {}).get("ETH/USDT", {}) or {}).get("bid") or 0
            hodl_val = genesis_btc * btc_bid + genesis_eth * eth_bid
            cripto_genesis_val = hodl_val
            if genesis_capital > cripto_genesis_val:
                hodl_val += (genesis_capital - cripto_genesis_val)
            delta_usdt = equity_actual - hodl_val
            delta_pct = (delta_usdt / hodl_val * 100) if hodl_val > 0 else 0.0
            return {"bot": equity_actual, "hodl": hodl_val,
                    "delta_usdt": delta_usdt, "delta_pct": delta_pct}
        except Exception:
            return None

    def _panel_capital_okx(self):
        free = self._free_usdt(self.core_okx)
        earn_usdt = refrescar_earn_usdt_okx(self.core_okx)
        cripto = self._valor_cripto("okx")
        earn_cripto = self._earn_cripto_okx_valor()
        m_pata, u_pata = self._pata_short_margen_upnl()
        equity = free + earn_usdt + cripto + m_pata + u_pata
        short_line = ""
        if CONFIG.get("fut_enabled") and (m_pata or u_pata):
            short_line = (f"Short fut   [magenta]{m_pata + u_pata:>10.2f}[/] "
                          f"[dim](uPnL {u_pata:+.2f})[/]\n")
        body = (
            f"USDT free   [green]{free:>10.2f}[/]\n"
            f"USDT Earn   [cyan]{earn_usdt:>10.2f}[/]\n"
            f"Cripto val  [cyan]{cripto:>10.2f}[/]\n"
            f"  └ Earn    [dim]{earn_cripto:>10.2f}[/]\n"
            f"{short_line}"
            f"[dim]──────────────────[/]\n"
            f"[bold]EQUITY     {equity:>10.2f}[/]\n"
            f"[dim]{earn_status_okx[:28]}[/]"
        )
        return Panel(body, title="[bold blue]OKX[/]", border_style="blue", expand=True)

    def _tabla_spot(self, venue, title):
        t = Table(title=title, show_header=True, header_style="bold", expand=True)
        t.add_column("Sym", width=12)
        t.add_column("Modo", width=10)
        t.add_column("Qty", justify="right")
        t.add_column("Avg", justify="right")
        t.add_column("Bid", justify="right")
        t.add_column("Val$", justify="right")
        t.add_column("PnL%", justify="right")
        filas = list(self.db.get_active_portfolio(venue))
        if not filas:
            t.add_row("[dim]—[/]", "[dim]—[/]", "[dim]—[/]", "[dim]—[/]", "[dim]—[/]", "[dim]—[/]", "[dim]—[/]")
        for sym, avg, qty, inv, mode in filas:
            bid = (PRECIOS_RAM.get(venue, {}).get(sym, {}) or {}).get("bid")
            if bid and avg > 0:
                pnl_pct = (bid - avg) / avg * 100
                pnl_str = f"[{'green' if pnl_pct >= 0 else 'red'}]{pnl_pct:+.2f}%[/]"
                val = qty * bid
            else:
                pnl_str = "[dim]—[/]"
                val = qty * (avg or 0)
            t.add_row(
                sym,
                f"[{'cyan' if mode == 'ACTIVE' else 'yellow'}]{mode}[/]",
                f"{qty:.6f}", f"{avg:.4f}",
                f"{bid:.4f}" if bid else "[dim]—[/]",
                f"{val:.2f}", pnl_str,
            )
        return t

    def _snapshot_state(self):
        """Lee todo el estado bajo locks una vez; usado por _render() y _render_text()."""
        with regime_lock:
            mm = modo_mercado
            fc = factor_capital
            mo = modo_operacion
            mc = modo_confianza
            mr = modo_razon
            reg = regimen_mercado
            rc = regimen_confianza
        return {
            "mm": mm, "fc": fc, "mo": mo, "mc": mc, "mr": mr,
            "reg": reg, "rc": rc, "reg_mode": CONFIG["regimen_brain_mode"],
            "ks": "ACTIVO" if kill_switch.is_set() else "off",
            "liq": "ON" if CONFIG["liquidate_only"] else "off",
            "panic_on": is_panico_global(),
            "warmup": not indicadores_evaluado,
        }

    def _render_text(self):
        """Snapshot plain-text para journalctl/systemd. Prefijo [HUD] greppable."""
        s = self._snapshot_state()
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        wu = " warmup" if s["warmup"] else ""
        panic = "PANIC" if s["panic_on"] else "."
        out = []
        out.append(
            f"[HUD] {ts} Quantum OKX-only — régimen {s['mm']}({s['fc']:.2f}) "
            f"cerebro {s['reg']}({s['rc']:.2f},{s['reg_mode']}) "
            f"modo {s['mo']}({s['mc']:.2f},{s['mr']}) "
            f"liq={s['liq']} panic={panic} kill={s['ks']}{wu}"
        )
        # Capital OKX
        free_o = self._free_usdt(self.core_okx)
        earn_o = refrescar_earn_usdt_okx(self.core_okx)
        cripto_o = self._valor_cripto("okx")
        earn_c_o = self._earn_cripto_okx_valor()
        m_pata, u_pata = self._pata_short_margen_upnl()
        eq_o = free_o + earn_o + cripto_o + m_pata + u_pata
        short_str = f" short={m_pata + u_pata:.2f}(uPnL{u_pata:+.2f})" if CONFIG.get("fut_enabled") else ""
        out.append(
            f"[HUD] OKX free={free_o:.2f} earn={earn_o:.2f} cripto={cripto_o:.2f} "
            f"earn_cripto={earn_c_o:.2f}{short_str} EQ={eq_o:.2f}"
        )
        # Yield 7d
        ym = self._yield_metrics(ventana_dias=7)
        out.append(
            f"[HUD] YIELD 7d spot_okx={ym['spot_okx']:+.2f} "
            f"earn_est={ym['earn_est']:+.2f} TOT={ym['total']:+.2f}"
        )
        # CAPITAL/PROFIT
        cm = self._capital_metrics()
        if cm is not None:
            arrow = "▲" if cm["profit_total"] >= 0 else "▼"
            out.append(
                f"[HUD] CAPITAL user={cm['capital_user']:.2f} equity={cm['equity']:.2f} "
                f"profit={cm['profit_total']:+.2f}({cm['profit_pct']:+.2f}%){arrow} "
                f"unreal={cm['unrealized']:+.2f} real={cm['realized']:+.2f}"
            )
        # HODL
        hodl = self._hodl_dashboard(eq_o)
        if hodl is not None:
            symbol_delta = "▲" if hodl["delta_pct"] >= 0 else "▼"
            out.append(
                f"[HUD] HODL bot={hodl['bot']:.2f} hodl={hodl['hodl']:.2f} "
                f"Δ={hodl['delta_usdt']:+.2f}({hodl['delta_pct']:+.2f}%){symbol_delta}"
            )
        # ML (solo OKX)
        if CONFIG["ml_sizing_enabled"] and ml_model is not None:
            ml_parts = []
            for sym in PARES_OKX_SPOT:
                prob = ml_predict_prob("okx", sym)
                if prob is None:
                    continue
                mult = ml_size_multiplier("okx", sym)
                ml_parts.append(f"OKX/{sym.split('/')[0]} p={prob:.2f}×{mult:.2f}")
            if ml_parts:
                out.append("[HUD] ML  " + "  ".join(ml_parts))
        # SPOT OKX
        for sym, avg, qty, inv, mode in list(self.db.get_active_portfolio("okx")):
            bid = (PRECIOS_RAM.get("okx", {}).get(sym, {}) or {}).get("bid")
            if bid and avg > 0:
                pnl = (bid - avg) / avg * 100
                val = qty * bid
                out.append(
                    f"[HUD] SPOT OKX {sym:<12} {mode:<10} qty={qty:.6f} "
                    f"avg={avg:.4f} bid={bid:.4f} val={val:.2f} pnl={pnl:+.2f}%"
                )
            else:
                val = qty * (avg or 0)
                out.append(
                    f"[HUD] SPOT OKX {sym:<12} {mode:<10} qty={qty:.6f} "
                    f"avg={avg:.4f} bid=- val={val:.2f} pnl=-"
                )
        # LADDER — una línea por par SIEMPRE que el sleeve esté encendido, opere o no.
        # Si el sleeve está ON y aquí no sale nada, es que ni el bucle del HUD llegó.
        if CONFIG["ladder_enabled"]:
            ahora = time.time()
            tick_s = float(CONFIG["ladder_tick_s"])
            for sym in PARES_LADDER:
                out.append(hud_linea_ladder(sym, ladder_estado_publicado.get(sym),
                                            ahora, tick_s))
        # Caja negra (3 últimos eventos)
        try:
            cn = self.db.get_ultimos_caja_negra(n=3)
        except Exception:
            cn = []
        for r in cn:
            ts_e = (r[0] or "")[-8:]
            ev = r[1] or "-"
            sym_e = r[2] or "-"
            det = (r[3] or "")[:140]
            out.append(f"[HUD] EVT {ts_e} {ev} {sym_e}: {det}")
        print("\n".join(out), flush=True)

    def _panel_short(self):
        """Panel compacto de la pata bajista (posiciones short + pnl del día)."""
        rows = self.db.short_get_posiciones()
        lines = []
        for p in rows:
            sym = p["symbol"].replace("/USDT:USDT", "")
            bid = (PRECIOS_RAM.get("okx", {}).get(_spot_de_perp(p["symbol"]), {}) or {}).get("bid") or p["entry_px"]
            upnl = (p["entry_px"] - bid) * p["qty"]
            sign = "green" if upnl >= 0 else "red"
            lines.append(f"[red]SHORT[/] {sym} entry {p['entry_px']:.2f} "
                         f"qty {p['qty']:.4f} lev {p['lever']}x  uPnL [{sign}]{upnl:+.2f}[/]")
        pnl_hoy = self.db.short_pnl_hoy()
        sign_h = "green" if pnl_hoy >= 0 else "red"
        canary = " [yellow]CANARY[/]" if CONFIG["short_canary_mode"] else ""
        body = ("\n".join(lines) if lines else "[dim](sin shorts abiertos)[/]")
        body += f"\n[dim]PnL hoy:[/] [{sign_h}]{pnl_hoy:+.2f}[/]{canary}"
        return Panel(body, title="[bold red]PATA BAJISTA[/]", border_style="red")

    def _panel_ladder(self):
        """Panel del sleeve ladder. Reusa la MISMA función pura que el HUD de texto para
        que las dos vistas no puedan divergir (una mintiendo y la otra no)."""
        ahora, tick_s = time.time(), float(CONFIG["ladder_tick_s"])
        lines = []
        for sym in PARES_LADDER:
            txt = hud_linea_ladder(sym, ladder_estado_publicado.get(sym), ahora, tick_s)
            txt = txt.replace("[HUD] LADDER ", "")
            color = "red" if ("STALE" in txt or "SIN ESTADO" in txt) else \
                    ("yellow" if "CONGELADO" in txt else "green")
            lines.append(f"[{color}]{txt}[/]")
        body = "\n".join(lines) if lines else "[dim](sin pares)[/]"
        return Panel(body, title="[bold]LADDER (canary)[/]", border_style="magenta")

    def _render(self):
        s = self._snapshot_state()
        mm, fc, mo, mc, mr = s["mm"], s["fc"], s["mo"], s["mc"], s["mr"]
        reg, rc, reg_mode = s["reg"], s["rc"], s["reg_mode"]
        ks = s["ks"]
        liq = s["liq"]
        panic_on = s["panic_on"]
        panic_label = "[red]PANIC ON[/]" if panic_on else "[dim]panic off[/]"
        warmup = " [yellow](warmup)[/]" if s["warmup"] else ""

        header = Table.grid(expand=True)
        header.add_column(justify="left", ratio=1)
        header.add_column(justify="right", ratio=1)
        reg_color = "green" if regimen_opera(reg) else "red"
        reg_mode_tag = reg_mode if reg_mode == "live" else f"[dim]{reg_mode}[/]"
        header.add_row(
            f"[bold cyan]Quantum OKX[/]  Régimen [{('red' if mm == 'BAJISTA' else 'green')}]{mm}[/]"
            f" factor {fc:.2f}  Cerebro [{reg_color}]{reg}[/] ({rc:.2f}, {reg_mode_tag})"
            f"  Modo [{self._color_modo(mo)}]{mo}[/]"
            f" ({mc:.2f}, {mr})  Liquidación {liq}  {panic_label}{warmup}",
            f"KILL [{'red' if ks == 'ACTIVO' else 'dim'}]{ks}[/]  "
            f"{datetime.now().strftime('%H:%M:%S')}"
        )

        capital_panel = self._panel_capital_okx()
        spot_table = self._tabla_spot("okx", "SPOT OKX")

        try:
            ym = self._yield_metrics(ventana_dias=7)
            cm = self._capital_metrics()
            eq_total = (cm["equity"] if cm
                        else self._free_usdt(self.core_okx) + refrescar_earn_usdt_okx(self.core_okx)
                             + self._valor_cripto("okx") + sum(self._pata_short_margen_upnl()))
            hodl = self._hodl_dashboard(eq_total)
        except Exception:
            ym, cm, hodl = None, None, None

        yield_lines = []
        if ym:
            sign_total = "green" if ym["total"] >= 0 else "red"
            yield_lines.append(
                f"[dim]7d:[/] spot_okx [cyan]{ym['spot_okx']:+.2f}[/]  "
                f"earn_est [cyan]{ym['earn_est']:+.2f}[/]  "
                f"[bold {sign_total}]TOT {ym['total']:+.2f}[/]"
            )
        if cm:
            sign_c = "green" if cm["profit_total"] >= 0 else "red"
            arrow_c = "▲" if cm["profit_total"] >= 0 else "▼"
            yield_lines.append(
                f"[dim]vs Capital:[/] inj [cyan]{cm['capital_user']:.2f}[/]  "
                f"eq [cyan]{cm['equity']:.2f}[/]  "
                f"profit [{sign_c}]{cm['profit_total']:+.2f} ({cm['profit_pct']:+.2f}%) {arrow_c}[/]  "
                f"[dim]unreal {cm['unrealized']:+.2f}  real {cm['realized']:+.2f}[/]"
            )
        if hodl:
            sign = "green" if hodl["delta_pct"] >= 0 else "red"
            arrow = "▲" if hodl["delta_pct"] >= 0 else "▼"
            yield_lines.append(
                f"[dim]vs HODL:[/] bot [cyan]{hodl['bot']:.2f}[/]  hodl [dim]{hodl['hodl']:.2f}[/]  "
                f"Δ [{sign}]{hodl['delta_usdt']:+.2f} ({hodl['delta_pct']:+.2f}%) {arrow}[/]"
            )
        yield_panel = Panel(
            "\n".join(yield_lines) if yield_lines else "[dim](sin métricas)[/]",
            title="Performance", border_style="green"
        )

        cn = self.db.get_ultimos_caja_negra(n=8)
        cn_lines = "\n".join(
            f"[dim]{r[0][-8:]}[/] [{('red' if 'KILL' in r[1] or 'STOP' in r[1] else 'cyan')}]{r[1]}[/] "
            f"{r[2] or '-'}: {r[3][:90]}"
            for r in cn
        ) or "[dim](sin eventos)[/]"
        caja_panel = Panel(cn_lines, title="Caja Negra (últimos 8)", border_style="dim")

        layout = Layout()
        sections = [
            Layout(Panel(header, border_style="cyan"), size=3),
            Layout(capital_panel, size=10),
            Layout(spot_table, size=10),
            Layout(yield_panel, size=5),
            Layout(caja_panel),
        ]
        if CONFIG["ladder_enabled"]:
            try:
                sections.insert(4, Layout(self._panel_ladder(), size=2 + len(PARES_LADDER)))
            except Exception:
                pass
        if CONFIG["fut_enabled"]:
            try:
                sections.insert(4, Layout(self._panel_short(), size=6))
            except Exception:
                pass
        layout.split_column(*sections)
        console.clear()
        console.print(layout)

    def run(self):
        time.sleep(3.0)
        log_mode = CONFIG.get("hud_log_mode", False)
        tick = CONFIG["hud_log_interval_s"] if log_mode else self.tick_s
        while not kill_switch.is_set():
            try:
                if log_mode:
                    self._render_text()
                else:
                    self._render()
            except Exception as e:
                logger_errores.error(f"RadarTelemetria: {e}")
            time.sleep(tick)


# =============================================================================
# 12. MÓDULOS POR VENUE — PLACEHOLDERS (a completar en próximos turnos)
# =============================================================================
# Los siguientes módulos heredan TradingCoreBase y serán implementados en el
# orden definido en bot.txt. Por ahora, placeholders que permiten el arranque.

class FuenteBinance:
    """Fuente de datos Binance (solo lectura) para el cerebro de régimen/ML.
    NO opera trading: expone exchange_read (ccxt público) para OHLCV BTC/ETH +
    funding BTC, consumido por MotorIndicadores / RadarRegimen / Selector.
    Trading: exclusivamente OKX."""

    def __init__(self, db: DatabaseManager):
        self.db = db
        self.exchange_read = ccxt.binance({
            "enableRateLimit": True,
            "options": {"defaultType": "spot", "adjustForTimeDifference": True},
        })


class TradingCoreOkx(TradingCoreBase):
    """OKX_SPOT + GestorTesoreria + GestorInventario.
    Wireado por main(): tesoreria + inventario assignados antes de arrancar hilos."""

    def __init__(self, db: DatabaseManager):
        super().__init__("okx", db)
        self.exchange_trade = _aplicar_antibot(ccxt.okx({
            "apiKey": CONFIG["okx_api_key"],
            "secret": CONFIG["okx_api_secret"],
            "password": CONFIG["okx_passphrase"],
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }))
        self.exchange_read = _aplicar_antibot(ccxt.okx({
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }))
        self.api_trade_lock = threading.RLock()
        self.tesoreria = None   # wireado por main()
        self.inventario = None  # wireado por main()

    def sincronizar_okx_spot_arranque(self):
        """Adopta criptos existentes en OKX (Trading + Earn) al portfolio.
        Fix V1.2.5: suma qty Trading + Earn savings/staking para reflejar holding total.
        Persiste Earn cripto a okx_inventory_earn para que GestorInventario sepa qué hay."""
        ignorar = {"USDT", "USDC", "BUSD", "TUSD", "DAI"}
        try:
            # 1. Balance Trading (cuenta 18)
            bal = self.get_balance(force_refresh=True)
            free_qty_by_ccy = {}
            for ccy, info in (bal or {}).items():
                if not isinstance(info, dict):
                    continue
                q = float(info.get("free", 0) or 0) + float(info.get("used", 0) or 0)
                if q > 0:
                    free_qty_by_ccy[ccy] = q

            # 2. Earn cripto (Simple Earn savings flexible) — private_get_finance_savings_balance
            earn_qty_by_ccy = {}
            try:
                res = okx_retry(self.exchange_trade.private_get_finance_savings_balance, {}) or {}
                for r in (res.get("data") or []):
                    ccy = r.get("ccy")
                    qty = float(r.get("amt", 0) or 0)
                    if ccy and qty > 0:
                        earn_qty_by_ccy[ccy] = earn_qty_by_ccy.get(ccy, 0) + qty
            except Exception as e:
                logger_errores.error(f"OKX Earn savings balance: {e}")

            # 3. Persistir Earn cripto a tabla para GestorInventario
            for ccy, qty in earn_qty_by_ccy.items():
                if ccy in ignorar:
                    continue
                self.db._exec(
                    "INSERT OR REPLACE INTO okx_inventory_earn "
                    "(ccy, symbol, qty, deposited_at, last_flush_ms, fail_count) "
                    "VALUES (?,?,?,?,?,?)",
                    (ccy, f"{ccy}/USDT", qty, time.time(), 0.0, 0)
                )

            # 4. Adopción al portfolio (qty_total = trading + earn)
            adoptadas = 0
            todos_ccys = set(free_qty_by_ccy.keys()) | set(earn_qty_by_ccy.keys())
            for ccy in todos_ccys:
                if ccy in ignorar:
                    continue
                free_q = free_qty_by_ccy.get(ccy, 0)
                earn_q = earn_qty_by_ccy.get(ccy, 0)
                qty_total = free_q + earn_q
                if qty_total < 1e-9:
                    continue
                sym = f"{ccy}/USDT"
                existing = next((r for r in self.db.get_active_portfolio("okx") if r[0] == sym), None)
                seed_avg = seed_avg_cost("okx", sym)
                if existing and not seed_avg:
                    continue  # ya en DB y no hay seed → respeta
                try:
                    ticker = self.exchange_read.fetch_ticker(sym)
                    price = float(ticker.get("last") or 0)
                except Exception:
                    continue
                if price <= 0 or qty_total * price < 1.0:
                    continue
                avg = seed_avg if seed_avg and seed_avg > 0 else price
                inv = qty_total * avg
                mode = "ACTIVE" if sym in PARES_BASE_HOLD else "LIQUIDATE_ONLY"
                self.db.update_portfolio("okx", sym, qty_total, avg, inv=inv, mode=mode)
                origen = "seed" if seed_avg else "precio_actual"
                accion = "RESEED" if existing else "ADOPT"
                self.db.log_caja_negra(f"OKX_{accion}", sym,
                                        f"qty {qty_total:.6f} (T {free_q:.6f}+E {earn_q:.6f}) "
                                        f"avg {avg:.4f} ({origen}) ≈ {inv:.2f} USDT ({mode})")
                adoptadas += 1
            if adoptadas:
                logger_errores.info(f"OKX adopción inicial: {adoptadas} criptos importadas")
        except Exception as e:
            logger_errores.error(f"OKX sincronizar adopción: {e}")

        if CONFIG["liquidate_only"]:
            for sym, avg, qty, inv, mode in self.db.get_active_portfolio("okx"):
                if sym not in PARES_BASE_HOLD and mode != "LIQUIDATE_ONLY":
                    self.db.set_portfolio_mode("okx", sym, "LIQUIDATE_ONLY")
                    self.db.log_caja_negra("OKX_BOOT", sym, "→ LIQUIDATE_ONLY")

        # V1.2.7: lectura inicial Earn USDT OKX (sin esperar ciclo 3600s del GestorTesoreria)
        eb = refrescar_earn_usdt_okx(self, force=True)
        if eb > 0:
            self.db.log_caja_negra("OKX_EARN", "USDT", f"Simple Earn detectado: {eb:.2f} USDT")

    def reconciliar_portfolio(self):
        """Espejo de TradingCoreBin.reconciliar_portfolio para OKX.
        Excluye cripto parqueada en Earn (GestorInventario): suma free + earn_cripto antes
        del compare. Sin esto, durante parking activo el bot eliminaría posiciones válidas."""
        try:
            bal = self.get_balance()
            earn_qty_by_ccy = {}
            if self.inventario is not None:
                rows = self.db._exec(
                    "SELECT ccy, qty FROM okx_inventory_earn", fetch="all"
                ) or []
                earn_qty_by_ccy = {ccy: float(qty or 0) for ccy, qty in rows}
            for sym, avg, qty_db, inv, mode in self.db.get_active_portfolio("okx"):
                base = sym.split("/")[0]
                free = float(bal.get(base, {}).get("free", 0)) if isinstance(bal.get(base), dict) else 0.0
                qty_real = free + earn_qty_by_ccy.get(base, 0.0)
                if qty_db > 1e-9 and qty_real < 1e-9:
                    self.db.delete_portfolio("okx", sym)
                    self.db.log_caja_negra("OKX_RECONCIL", sym,
                                            f"qty_db {qty_db:.6f} → free+earn 0, delete fantasma")
                elif qty_db > qty_real * 1.01 and qty_real > 0:
                    new_inv = (inv or 0.0) * (qty_real / qty_db) if qty_db > 0 else 0.0
                    self.db.update_portfolio("okx", sym, qty_real, avg, inv=new_inv)
                    self.db.log_caja_negra("OKX_RECONCIL", sym,
                                            f"qty_db {qty_db:.6f} → free+earn {qty_real:.6f}")
        except Exception as e:
            logger_errores.error(f"reconciliar_portfolio OKX: {e}")

    # -------- Budget allocation --------
    def allocate_budget(self, sym, monto_usdt):
        with okx_risk_lock:
            bal = self.get_balance()
            free = float(bal.get("USDT", {}).get("free", 0)) \
                   if isinstance(bal.get("USDT"), dict) else 0.0
            sym_used = self.reserved_capital_per_sym.get(sym, 0.0)
            if sym_used + monto_usdt > CONFIG["okx_spot_max_cap"]:
                return False
            if self.reserved_capital_global + monto_usdt > CONFIG["okx_global_max_cap"]:
                return False
            # rescate_urgente OBLIGATORIO si liquidez insuficiente (mitigación race Tesorería)
            if monto_usdt > free * 0.98 and self.tesoreria is not None:
                deficit = monto_usdt - free * 0.98 + 1.0  # +1 USDT buffer
                rescatado = self.tesoreria.rescate_urgente(deficit)
                if rescatado <= 0:
                    return False
                bal = self.get_balance(force_refresh=True)
                free = float(bal.get("USDT", {}).get("free", 0)) \
                       if isinstance(bal.get("USDT"), dict) else 0.0
                if monto_usdt > free * 0.98:
                    return False
            self.reserved_capital_per_sym[sym] = sym_used + monto_usdt
            self.reserved_capital_global += monto_usdt
            return True

    def release_budget(self, sym, monto_usdt):
        with okx_risk_lock:
            self.reserved_capital_per_sym[sym] = max(
                0.0, self.reserved_capital_per_sym.get(sym, 0.0) - monto_usdt
            )
            self.reserved_capital_global = max(0.0, self.reserved_capital_global - monto_usdt)

    # -------- Execution helpers --------
    def execute_trade(self, sym, side, qty_or_inv, strategy="MANUAL", reason=""):
        """Market buy/sell. side='LONG' → buy; side='SELL' → sell.
        Para SELL: flush_to_spot SÍNCRONO antes (mitigación race con GestorInventario)."""
        if kill_switch.is_set() and side == "LONG":
            return None
        if not is_price_fresh("okx", sym):
            self.db.log_caja_negra("OKX_STALE", sym, f"Precio stale, {side} pospuesto")
            return None
        try:
            # flush_to_spot ANTES de SELL (fuera del api_trade_lock — puede tardar 5-60s)
            if side == "SELL" and self.inventario is not None:
                self.inventario.flush_to_spot(sym, float(qty_or_inv))

            with self.api_trade_lock:
                if side == "LONG":
                    if not self.allocate_budget(sym, float(qty_or_inv)):
                        return None
                    price = PRECIOS_RAM["okx"][sym]["ask"]
                    qty = float(qty_or_inv) / price
                    order = self.exchange_trade.create_market_buy_order(
                        sym, qty, params={"tdMode": "cash"}
                    )
                    fill_price = float(order.get("average") or order.get("price") or price)
                    fill_qty = float(order.get("filled") or order.get("amount") or qty)
                    self._post_buy(sym, fill_qty, fill_price, float(qty_or_inv), strategy)
                    self.db.log_caja_negra("OKX_BUY", sym,
                                            f"{strategy} {fill_qty:.6f}@{fill_price:.4f} ({reason})")
                    return order
                elif side == "SELL":
                    qty = float(qty_or_inv)
                    if qty <= 0:
                        return None
                    # Cierre defensivo (auditoría okx_gemini V11.5): tras flush_to_spot,
                    # reverifica balance real Trading. Evita -50011 en altcoins liquidándose.
                    # Fix V1.2.2 #5: get_balance() con cache. Tras flush_to_spot, invalidate
                    # primero para forzar refresh (Earn → Trading transfer puede no estar reflejado).
                    try:
                        base = sym.split("/")[0]
                        self.invalidate_balance()  # asegura ver el flush_to_spot recién hecho
                        bal = self.get_balance()
                        free = float(bal.get(base, {}).get("free", 0)) if isinstance(bal.get(base), dict) else 0.0
                        qty = min(qty, free)
                        qty = float(self.exchange_trade.amount_to_precision(sym, qty))
                    except Exception as _be:
                        logger_errores.error(f"OKX SELL saldo_real {sym}: {_be}")
                    if qty <= 0:
                        self.db.log_caja_negra("OKX_SELL", sym,
                                                f"{strategy} qty=0 saldo_real, skip ({reason})")
                        return None
                    order = self.exchange_trade.create_market_sell_order(
                        sym, qty, params={"tdMode": "cash"}
                    )
                    fill_price = float(order.get("average") or order.get("price")
                                       or PRECIOS_RAM["okx"][sym]["bid"])
                    fill_qty = float(order.get("filled") or order.get("amount") or qty)
                    self._post_sell(sym, fill_qty, fill_price, strategy)
                    self.db.log_caja_negra("OKX_SELL", sym,
                                            f"{strategy} {fill_qty:.6f}@{fill_price:.4f} ({reason})")
                    return order
        except Exception as e:
            logger_errores.error(f"OKX execute_trade {side} {sym}: {e}")
            if side == "LONG":
                self.release_budget(sym, float(qty_or_inv))
            return None

    def _post_buy(self, sym, qty, price, inv_usdt, strategy):
        existing = next((r for r in self.db.get_active_portfolio("okx") if r[0] == sym), None)
        if existing:
            _, avg_old, qty_old, inv_old, mode = existing
            qty_new = qty_old + qty
            avg_new = (avg_old * qty_old + price * qty) / qty_new if qty_new > 0 else price
            inv_new = (inv_old or 0.0) + inv_usdt if strategy == "ENTRY" else inv_old
            self.db.update_portfolio("okx", sym, qty_new, avg_new, inv=inv_new)
        else:
            mode = "ACTIVE" if sym in PARES_BASE_HOLD else "LIQUIDATE_ONLY"
            self.db.update_portfolio("okx", sym, qty, price, inv=inv_usdt, mode=mode)
        self.db.set_cooldown("okx", sym, time.time())
        self.release_budget(sym, inv_usdt)
        self.invalidate_balance()

    def _post_sell(self, sym, qty, price, strategy):
        existing = next((r for r in self.db.get_active_portfolio("okx") if r[0] == sym), None)
        if not existing:
            return
        _, avg_old, qty_old, inv_old, mode = existing
        qty_new = max(0.0, qty_old - qty)
        pnl_usdt = (price - avg_old) * qty
        pnl_pct = (price - avg_old) / avg_old * 100 if avg_old > 0 else 0.0
        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.db._exec(
            "INSERT INTO okx_realized_pnl (ts, symbol, buy_price, sell_price, qty, pnl_pct, pnl_usdt) "
            "VALUES (?,?,?,?,?,?,?)",
            (ahora, sym, avg_old, price, qty, pnl_pct, pnl_usdt)
        )
        if qty_new <= 1e-9:
            self.db.delete_portfolio("okx", sym)
            self.db.log_caja_negra("OKX_EXIT", sym,
                                    f"Salida total ({strategy}) PnL={pnl_usdt:+.2f} USDT")
        else:
            # TP_PARCIAL_50 reduce inv proporcionalmente (ver comentario en BIN _post_sell)
            if strategy == "MOONBAG":
                new_inv = 0.0
            elif strategy == "TP_PARCIAL_50" and qty_old > 0:
                new_inv = (inv_old or 0.0) * (qty_new / qty_old)
            else:
                new_inv = inv_old
            self.db.update_portfolio("okx", sym, qty_new, avg_old, inv=new_inv)
        self.invalidate_balance()

    def execute_limit_maker(self, sym, price, monto_usdt):
        if kill_switch.is_set():
            return None
        if not self.allocate_budget(sym, monto_usdt):
            return None
        try:
            with self.api_trade_lock:
                qty = monto_usdt / price
                order = self.exchange_trade.create_limit_buy_order(
                    sym, qty, price, params={"tdMode": "cash"}
                )
                oid = str(order.get("id"))
                self.db._exec(
                    "INSERT OR REPLACE INTO okx_grid_orders "
                    "(order_id, symbol, timestamp, reserved_usdt, side) VALUES (?,?,?,?,'BUY')",
                    (oid, sym, time.time(), float(monto_usdt))
                )
                return order
        except Exception as e:
            logger_errores.error(f"OKX limit_maker {sym}: {e}")
            self.release_budget(sym, monto_usdt)
            return None

    def execute_limit_sell_harvest(self, sym, price, qty):
        if kill_switch.is_set():
            return None
        if not is_price_fresh("okx", sym):
            return None
        # Cancela cualquier harvest activa para evitar doble venta
        existing = self.db._exec(
            "SELECT order_id FROM okx_grid_orders WHERE symbol=? AND side='SELL'",
            (sym,), fetch="all"
        ) or []
        for (oid,) in existing:
            try:
                self.exchange_trade.cancel_order(oid, sym)
            except Exception:
                pass
            self.db._exec("DELETE FROM okx_grid_orders WHERE order_id=?", (oid,))
        # flush_to_spot ANTES (cripto puede estar en Earn)
        if self.inventario is not None:
            self.inventario.flush_to_spot(sym, qty)
        try:
            with self.api_trade_lock:
                order = self.exchange_trade.create_limit_sell_order(
                    sym, qty, price, params={"tdMode": "cash"}
                )
                oid = str(order.get("id"))
                self.db._exec(
                    "INSERT OR REPLACE INTO okx_grid_orders "
                    "(order_id, symbol, timestamp, reserved_usdt, side) VALUES (?,?,?,?,'SELL')",
                    (oid, sym, time.time(), 0.0)
                )
                return order
        except Exception as e:
            logger_errores.error(f"OKX harvest_sell {sym}: {e}")
            return None


def simbolos_feed_okx(pares_spot, pares_ladder, port_syms):
    """Universo de símbolos que WebSocketFeedOkx debe alimentar a PRECIOS_RAM["okx"].

    Son TRES consumidores, no uno: el Grid (PARES_OKX_SPOT), OmniRisk sobre lo heredado
    (portfolio activo) y el sleeve ladder (PARES_LADDER). Omitir el tercero dejó el canary
    mudo el 2026-08-02: al mover LADDER_PARES de TRX (que estaba en OKX_PARES_SPOT, de ahí
    que nadie lo notara) a XRP+LTC, esos pares nunca entraron al feed → _precio() = None →
    el sleeve no posteó nada en 11.5 h. El feed es infraestructura COMPARTIDA; suscribir un
    precio no acopla motores (el Grid itera su propia lista, OmniRisk el portfolio)."""
    vistos, out = set(), []
    for s in list(pares_spot) + list(pares_ladder) + list(port_syms):
        if s not in vistos:
            vistos.add(s)
            out.append(s)
    return out


class WebSocketFeedOkx(threading.Thread):
    """OKX_SPOT — ccxtpro.okx watch_ticker por símbolo (OKX no soporta watch_tickers batch).
    Fallback REST 5s si ccxtpro no disponible. Event loop inmortal."""

    def __init__(self, core_okx, db: DatabaseManager):
        super().__init__(name="WSTickerFeedOkx", daemon=True)
        self.core = core_okx
        self.db = db

    def _port_syms(self):
        return [r[0] for r in self.db.get_active_portfolio("okx")]

    async def _async_loop(self):
        import asyncio
        if ccxtpro is None:
            while not kill_switch.is_set():
                try:
                    syms = simbolos_feed_okx(PARES_OKX_SPOT, PARES_LADDER, self._port_syms())
                    tickers = self.core.exchange_read.fetch_tickers(syms)
                    now = time.time()
                    with precios_lock:
                        for sym, t in tickers.items():
                            PRECIOS_RAM["okx"][sym] = {
                                "bid": float(t.get("bid") or 0),
                                "ask": float(t.get("ask") or 0),
                                "local_ts": now,
                            }
                except Exception as e:
                    logger_errores.error(f"WS OKX fallback: {e}")
                await asyncio.sleep(5.0)
            return

        ws = ccxtpro.okx({
            "apiKey": CONFIG["okx_api_key"],
            "secret": CONFIG["okx_api_secret"],
            "password": CONFIG["okx_passphrase"],
            "enableRateLimit": True,
        })
        # Fix V1.2.4 #1: ccxtpro.watch_ticker requiere markets cargados primero.
        # Sin esto, cada watch_ticker spamea "okx markets not loaded" cada 2s.
        try:
            await ws.load_markets()
        except Exception as e:
            logger_errores.error(f"WS OKX load_markets: {e}")

        async def watch_one(sym):
            while not kill_switch.is_set():
                try:
                    t = await ws.watch_ticker(sym)
                    bid = t.get("bid")
                    ask = t.get("ask")
                    if bid and ask:
                        with precios_lock:
                            PRECIOS_RAM["okx"][sym] = {
                                "bid": float(bid), "ask": float(ask),
                                "local_ts": time.time(),
                            }
                except Exception as e:
                    logger_errores.error(f"WS OKX {sym}: {e}")
                    await asyncio.sleep(2.0)

        try:
            syms = simbolos_feed_okx(PARES_OKX_SPOT, PARES_LADDER, self._port_syms())
            await asyncio.gather(*[watch_one(s) for s in syms])
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    def run(self):
        import asyncio
        while not kill_switch.is_set():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(self._async_loop())
            except Exception as e:
                logger_errores.error(f"WS OKX reinicio loop: {e}")
                time.sleep(5.0)


def omnirisk_debe_gestionar(sym, qty, min_amt, ladder_ccys):
    """False si la posición NO debe gestionarla el core OmniRisk: su ccy lo gestiona el
    ladder (EscuadronLadderOkx maneja su propio inventario) o es un dust invendible
    (qty < mínimo del mercado). Evita el loop de errores 2026-07-04 donde OmniRisk
    intentaba vender cada 1s un dust residual del round-trip del ladder (TRX 8.5e-7 <
    1e-6) → OKX lo rechazaba y se martillaba la API. Extensión de Fix C (V1.9.9) al core."""
    if sym.split("/")[0] in ladder_ccys:
        return False
    if qty < min_amt:
        return False
    return True


class OmniRiskOkx(threading.Thread):
    """OKX_SPOT — espejo OmniRiskBin con flush_to_spot pre-SELL (en execute_trade)."""

    TICK_S = 1.0

    def __init__(self, core_okx, db: DatabaseManager):
        super().__init__(name="OmniRiskOkx", daemon=True)
        self.core = core_okx
        self.db = db
        self._pending_sells_sym = set()
        self._ladder_ccys = ladder_ccys_gestionados()

    def _min_amount(self, sym):
        """Mínimo vendible del mercado (ccxt). Fallback conservador 1e-6 = solo filtra
        dust real, nunca posiciones legítimas."""
        try:
            m = (self.core.exchange_trade.markets or {}).get(sym, {})
            mn = ((m.get("limits") or {}).get("amount") or {}).get("min")
            if mn:
                return float(mn)
        except Exception:
            pass
        return 1e-6

    def _pnl_neto_pct(self, avg, bid):
        return (bid * (1 - OKX_TAKER_FEE) - avg * (1 + OKX_MAKER_FEE)) / (avg * (1 + OKX_MAKER_FEE)) * 100

    def _kill_close_all(self):
        for sym, avg, qty, inv, mode in self.db.get_active_portfolio("okx"):
            # Era la ÚNICA ruta de venta del core sin el guard. Lo que la frenaba sobre un
            # ccy del ladder no era una regla sino un accidente: el is_price_fresh de
            # execute_trade fallaba porque el par no estaba en el feed. Al suscribirlo
            # (simbolos_feed_okx) el accidente desapareció → doble liquidación contra
            # cerrar_todo_kill, PnL con cost basis inventado y fila fantasma. El guard
            # también filtra el dust invendible, que aquí se reintentaría cada 30s.
            if not omnirisk_debe_gestionar(sym, qty, self._min_amount(sym), self._ladder_ccys):
                continue
            try:
                self.core.execute_trade(sym, "SELL", qty, strategy="KILL_SWITCH",
                                          reason="KILL global")
            except Exception as e:
                logger_errores.error(f"OmniRiskOkx KILL {sym}: {e}")

    def _gestionar(self, sym, avg, qty, inv, mode):
        if not omnirisk_debe_gestionar(sym, qty, self._min_amount(sym), self._ladder_ccys):
            return
        if sym in self._pending_sells_sym:
            return
        bid_entry = PRECIOS_RAM.get("okx", {}).get(sym)
        if not bid_entry or not is_price_fresh("okx", sym):
            return
        bid = bid_entry["bid"]
        if bid <= 0 or avg <= 0:
            return
        pnl_pct = self._pnl_neto_pct(avg, bid)
        tp_state = self.db.get_tp_state("okx", sym)

        # 0. TP PARCIAL 50% (auditoría kucoin_antiguo V13) — solo BTC/ETH
        if (sym in PARES_BASE_HOLD and not tp_state.get("partial")
                and pnl_pct >= CONFIG["tp_parcial_pct"]):
            qty_half = qty * 0.5
            if qty_half > 0:
                self._pending_sells_sym.add(sym)
                try:
                    res = self.core.execute_trade(sym, "SELL", qty_half,
                                                    strategy="TP_PARCIAL_50",
                                                    reason=f"PnL {pnl_pct:+.2f}% asegura 50%")
                    if res:
                        self.db.save_tp_state("okx", sym, armed=True, tier=0,
                                              top=pnl_pct, moonbag=tp_state["moonbag"],
                                              floor_pct=CONFIG["be_safety_buffer_pct"],
                                              partial=True)
                finally:
                    self._pending_sells_sym.discard(sym)
            return

        # 1. MOONBAG
        if (not tp_state["moonbag"]) and pnl_pct >= CONFIG["okx_moonbag_trigger"] and inv > 0:
            qty_recover = min(qty, inv / bid) if bid > 0 else 0
            if qty_recover > 0:
                self._pending_sells_sym.add(sym)
                try:
                    res = self.core.execute_trade(sym, "SELL", qty_recover,
                                                    strategy="MOONBAG",
                                                    reason=f"PnL {pnl_pct:+.2f}%")
                    if res:
                        self.db.save_tp_state("okx", sym, armed=False, tier=0,
                                              top=bid, moonbag=True, floor_pct=0.0)
                finally:
                    self._pending_sells_sym.discard(sym)
            return

        # 2. BTC/ETH: stop aquí (Grid Harvest gestiona)
        if sym in PARES_BASE_HOLD:
            return

        # 3. Stop sectorial memes
        stop_thr = get_stop_threshold("okx", sym)
        dca_state = self.db.get_dca_state("okx", sym)
        if (stop_thr is not None
                and dca_state["count"] >= CONFIG["okx_dca_max_count"]
                and pnl_pct <= stop_thr):
            self._pending_sells_sym.add(sym)
            try:
                self.core.execute_trade(sym, "SELL", qty, strategy="STOP_SECTOR",
                                          reason=f"PnL {pnl_pct:+.2f}% ≤ {stop_thr}%")
            finally:
                self._pending_sells_sym.discard(sym)
            return

        # 4. Quantum Ratchet
        wagons = get_wagons("okx", sym)
        if not tp_state["armed"] and pnl_pct >= wagons[0]:
            self.db.save_tp_state("okx", sym, armed=True, tier=0,
                                  top=pnl_pct, moonbag=tp_state["moonbag"],
                                  floor_pct=0.0)
            return
        if not tp_state["armed"]:
            return

        top = max(tp_state["top"], pnl_pct)
        tier = tp_state["tier"]
        floor_pct = tp_state["floor_pct"]
        while tier + 1 < len(wagons) and top >= wagons[tier + 1]:
            tier += 1
            floor_pct = wagons[tier - 1] if tier > 0 else 0.0
        if top != tp_state["top"] or tier != tp_state["tier"]:
            self.db.save_tp_state("okx", sym, armed=True, tier=tier, top=top,
                                  moonbag=tp_state["moonbag"], floor_pct=floor_pct)

        ancho = (wagons[tier + 1] - wagons[tier]) if tier + 1 < len(wagons) else 5.0
        # OKX usa los mismos params de trailing que BIN (no expuestos por módulo en V1)
        dist_mult = trailing_distance_mult(top)
        holgura = max(CONFIG["bin_wagon_trail_min"], ancho * CONFIG["bin_wagon_trail_frac"] * dist_mult)
        if tier == 0:
            floor_pct = max(floor_pct, CONFIG["be_safety_buffer_pct"])
        drop_thr = top - CONFIG["trailing_drop_pct"]
        if pnl_pct <= floor_pct or pnl_pct <= top - holgura or pnl_pct <= drop_thr:
            self._pending_sells_sym.add(sym)
            try:
                self.core.execute_trade(sym, "SELL", qty, strategy="RATCHET_PROFIT",
                                          reason=f"top {top:.2f}% → pnl {pnl_pct:+.2f}% (floor {floor_pct:.2f}, mult {dist_mult:.1f})")
            finally:
                self._pending_sells_sym.discard(sym)

    def run(self):
        time.sleep(5.0)
        while True:
            if kill_switch.is_set():
                self._kill_close_all()
                time.sleep(30.0)
                continue
            try:
                for sym, avg, qty, inv, mode in self.db.get_active_portfolio("okx"):
                    self._gestionar(sym, avg, qty, inv, mode)
            except Exception as e:
                logger_errores.error(f"OmniRiskOkx loop: {e}")
            time.sleep(self.TICK_S)


class CentinelaAsincronoOkx(threading.Thread):
    """OKX_SPOT — espejo CentinelaAsincronoBin."""

    TICK_S = 2.0

    def __init__(self, core_okx, db: DatabaseManager):
        super().__init__(name="CentinelaOkx", daemon=True)
        self.core = core_okx
        self.db = db

    def _procesar_grid_orders(self):
        rows = self.db._exec(
            "SELECT order_id, symbol, timestamp, reserved_usdt, side FROM okx_grid_orders",
            fetch="all"
        ) or []
        for oid, sym, ts, reserved, side in rows:
            try:
                order = self.core.exchange_trade.fetch_order(oid, sym)
                status = order.get("status")
                if status == "closed":
                    filled = float(order.get("filled") or 0)
                    avg = float(order.get("average") or order.get("price") or 0)
                    if side == "BUY" and filled > 0 and avg > 0:
                        self.core._post_buy(sym, filled, avg, float(reserved or 0), "GRID")
                    elif side == "SELL" and filled > 0 and avg > 0:
                        self.core._post_sell(sym, filled, avg, "HARVEST")
                    self.db._exec("DELETE FROM okx_grid_orders WHERE order_id=?", (oid,))
                elif status in ("canceled", "expired", "rejected"):
                    if side == "BUY":
                        self.core.release_budget(sym, float(reserved or 0))
                    self.db._exec("DELETE FROM okx_grid_orders WHERE order_id=?", (oid,))
                elif side == "SELL" and (time.time() - float(ts)) > HARVEST_TTL:
                    try:
                        self.core.exchange_trade.cancel_order(oid, sym)
                    except Exception:
                        pass
                    self.db._exec("DELETE FROM okx_grid_orders WHERE order_id=?", (oid,))
                    self.db.log_caja_negra("OKX_HARVEST", sym, f"TTL cancel oid={oid}")
            except Exception as e:
                msg = str(e).lower()
                if "does not exist" in msg or "order not found" in msg or "51603" in msg:
                    if side == "BUY":
                        self.core.release_budget(sym, float(reserved or 0))
                    self.db._exec("DELETE FROM okx_grid_orders WHERE order_id=?", (oid,))
                else:
                    logger_errores.error(f"Centinela OKX grid {oid} {sym}: {e}")

    def _procesar_pending_sells(self):
        rows = self.db._exec(
            "SELECT order_id, symbol, p_limit, q_sell, c_old, is_moonbag, timestamp "
            "FROM okx_pending_sells", fetch="all"
        ) or []
        for oid, sym, p_limit, q_sell, c_old, is_moonbag, ts in rows:
            try:
                order = self.core.exchange_trade.fetch_order(oid, sym)
                status = order.get("status")
                if status == "closed":
                    self.db._exec("DELETE FROM okx_pending_sells WHERE order_id=?", (oid,))
                elif (time.time() - float(ts)) > 5.0 and status == "open":
                    try:
                        self.core.exchange_trade.cancel_order(oid, sym)
                    except Exception:
                        pass
                    self.db._exec("DELETE FROM okx_pending_sells WHERE order_id=?", (oid,))
            except Exception:
                self.db._exec("DELETE FROM okx_pending_sells WHERE order_id=?", (oid,))

    def run(self):
        time.sleep(7.0)
        ciclo = 0
        while not kill_switch.is_set():
            try:
                self._procesar_grid_orders()
                self._procesar_pending_sells()
                # Reconciliación portfolio↔balance cada 30 ciclos (~60s) — fix V1.2.2 #4
                ciclo += 1
                if ciclo % 30 == 0:
                    self.core.reconciliar_portfolio()
            except Exception as e:
                logger_errores.error(f"CentinelaOkx loop: {e}")
            time.sleep(self.TICK_S)


class EscudoDCAOkx(threading.Thread):
    """OKX_SPOT — DCA BTC/ETH only con rescate_urgente OBLIGATORIO si liquidez insuficiente."""

    TICK_S = 3.0

    def __init__(self, core_okx, db: DatabaseManager):
        super().__init__(name="EscudoDCAOkx", daemon=True)
        self.core = core_okx
        self.db = db

    def _pnl_neto_pct(self, avg, bid):
        return (bid * (1 - OKX_TAKER_FEE) - avg * (1 + OKX_MAKER_FEE)) / (avg * (1 + OKX_MAKER_FEE)) * 100

    def _m2m_global_pct(self):
        cap = CONFIG["okx_global_max_cap"]
        total = 0.0
        for sym, avg, qty, inv, mode in self.db.get_active_portfolio("okx"):
            bid = PRECIOS_RAM.get("okx", {}).get(sym, {}).get("bid", 0)
            total += qty * bid
        return (total / cap) * 100 if cap > 0 else 0.0

    def _gestionar(self, sym, avg, qty, inv, mode):
        if sym not in PARES_BASE_HOLD:
            return
        ind = MotorIndicadores.get_indicators("okx", sym)
        if not ind:
            return
        bid_entry = PRECIOS_RAM.get("okx", {}).get(sym)
        if not bid_entry or not is_price_fresh("okx", sym):
            return
        bid = bid_entry["bid"]
        pnl = self._pnl_neto_pct(avg, bid)
        if pnl >= -2.0:
            return

        dca = self.db.get_dca_state("okx", sym)
        if dca["count"] >= CONFIG["okx_dca_max_count"]:
            return

        if pnl <= -18.0:
            current_lv = 3
        elif pnl <= -10.0:
            current_lv = 2
        elif pnl <= -5.0:
            current_lv = 1
        else:
            return

        with regime_lock:
            mm = modo_mercado
        if mm == "BAJISTA" and current_lv < 3:
            return

        # V1.5.0: cooldown DCA adaptive por ATR (igual que Grid). Base 600s.
        last_buy = self.db.get_cooldown("okx", sym)
        cd_eff = get_cooldown_dyn("okx", sym, 600)
        if (time.time() - last_buy) < cd_eff:
            return

        lock = get_entry_lock("okx", sym)
        if not lock.acquire(timeout=CONFIG["entry_lock_timeout_ms"] / 1000.0):
            return
        try:
            base_inv = CONFIG["okx_investment_pct"] * float(
                self.core.get_balance().get("USDT", {}).get("free", 0)
            )
            base_inv = max(base_inv, 5.0)
            local_bottom = dca["bottom"] if dca["bottom"] < float("inf") else bid
            new_bottom = min(local_bottom, bid)
            self.db.save_dca_state("okx", sym, armed=True, level=current_lv,
                                    bottom=new_bottom, count=dca["count"])

            # Pánico Global BTC (auditoría okx_dca): no DCAr en flash-crash, solo L3.
            if is_panico_global() and current_lv < 3:
                return

            # Rebote confirmado (auditoría okx_dca V12): anti-cuchillo-cayendo en TODOS los niveles.
            bounce = (bid - new_bottom) / new_bottom * 100 if new_bottom > 0 else 0.0
            rebound_min = CONFIG["dca_rebound_pct"]
            disparado = False
            if current_lv == 1:
                rsi = float(ind.get("RSI_14", 0))
                vela_verde = ind.get("close", 0) > ind.get("open", 0)
                if bounce >= max(rebound_min, 1.5) and rsi >= 35 and vela_verde:
                    disparado = True
                    monto = base_inv
            elif current_lv == 2:
                if bounce >= rebound_min:
                    disparado = True
                    monto = base_inv
            elif current_lv == 3:
                if bounce >= rebound_min and self._m2m_global_pct() < 80.0:
                    disparado = True
                    monto = base_inv * 1.5

            if disparado:
                res = self.core.execute_trade(sym, "LONG", monto,
                                                strategy=f"DCA_L{current_lv}",
                                                reason=f"pnl {pnl:.2f}% bottom {new_bottom:.4f} bounce {bounce:.2f}%")
                if res:
                    self.db.save_dca_state("okx", sym, armed=True, level=current_lv,
                                            bottom=new_bottom, count=dca["count"] + 1)
        except Exception as e:
            logger_errores.error(f"EscudoDCAOkx {sym}: {e}")
        finally:
            lock.release()

    def run(self):
        time.sleep(20.0)
        while not kill_switch.is_set():
            try:
                for sym, avg, qty, inv, mode in self.db.get_active_portfolio("okx"):
                    self._gestionar(sym, avg, qty, inv, mode)
            except Exception as e:
                logger_errores.error(f"EscudoDCAOkx loop: {e}")
            time.sleep(self.TICK_S)


class TradingCoreShortOkx:
    """Wrapper OKX swap (linear perp) para la pata bajista V1.7.0.

    Aísla la lógica de derivados del spot: margen ISOLATED + stop-loss server-side
    adjunto a la orden de entrada (persiste aunque el bot/VPS se apague).
    """

    def __init__(self, exchange):
        self.ex = exchange  # ccxt.okx (options.defaultType='swap') o FakeOkxFut

    def _set_isolated_lev(self, symbol):
        self.ex.set_leverage(CONFIG["short_leverage"], symbol,
                             params={"mgnMode": "isolated", "posSide": "short"})

    def _inst_id(self, symbol):
        """'BTC/USDT:USDT' → 'BTC-USDT-SWAP' (instId nativo OKX para el endpoint order-algo)."""
        if hasattr(self.ex, "market_id"):
            try:
                return self.ex.market_id(symbol)
            except Exception:
                pass
        return symbol.split(":")[0].replace("/", "-") + "-SWAP"

    def _a_contratos(self, symbol, qty_moneda):
        """Convierte qty en moneda (BTC/ETH) → contratos OKX, cuantizado al lote."""
        ctval = SHORT_CTVAL.get(symbol, 1.0)
        contratos = qty_moneda / ctval if ctval > 0 else qty_moneda
        if hasattr(self.ex, "amount_to_precision"):
            contratos = float(self.ex.amount_to_precision(symbol, contratos))
        return contratos

    def abrir_short(self, symbol, precio, qty):
        self._set_isolated_lev(symbol)
        contratos = self._a_contratos(symbol, qty)       # qty viene en MONEDA
        sl_px = round(precio * (1 + CONFIG["short_sl_pct"]), 2)   # stop ARRIBA en short
        # 1) Abrir short a MARKET (taker → fill inmediato; la posición de la DB es real).
        #    SIN SL adjunto: OKX valida el slTriggerPx contra el lado del sell de apertura
        #    y rechaza un trigger por encima (error 51280). El SL va como orden separada.
        tp_px = round(precio * (1 - CONFIG["short_tp_pct"]), 2)   # TP abajo (ganancia del short)
        res = self.ex.create_order(symbol, "market", "sell", contratos, None,
                                   {"tdMode": "isolated", "posSide": "short"})
        # 2) Bracket TP+SL como UNA orden algo OCO server-side (endpoint nativo order-algo,
        #    ordType="oco"). OKX cancela una pata al dispararse la otra. PERSISTE aunque el
        #    bot/VPS se apague → el TP no se pierde. NO usar los params planos
        #    stopLossPrice+takeProfitPrice juntos: ccxt los traduce a un ordType contradictorio
        #    y OKX rechaza con 51000 "Parameter ordType error". En hedge mode el posSide ya
        #    implica cierre, así que NO se manda reduceOnly (daría conflicto de parámetros).
        try:
            sz = (self.ex.amount_to_precision(symbol, contratos)
                  if hasattr(self.ex, "amount_to_precision") else str(contratos))
            br = self.ex.private_post_trade_order_algo({
                "instId": self._inst_id(symbol), "tdMode": "isolated",
                "side": "buy", "posSide": "short", "ordType": "oco", "sz": sz,
                "tpTriggerPx": str(tp_px), "tpOrdPx": "-1", "tpTriggerPxType": "last",
                "slTriggerPx": str(sl_px), "slOrdPx": "-1", "slTriggerPxType": "last",
            })
            data0 = (br.get("data") or [{}])[0]
            if str(data0.get("sCode", "0")) not in ("0", ""):
                raise RuntimeError(f"OCO sCode {data0.get('sCode')}: {data0.get('sMsg')}")
            algo_id = data0.get("algoId") or br.get("id")
        except Exception as e:
            # FALLBACK CRÍTICO: si el bracket no se colgó, cerrar el short YA (nunca desnudo).
            logger_errores.error(f"bracket TP/SL no colgó en {symbol}, cierro el short: {e}")
            try:
                self.ex.create_order(symbol, "market", "buy", contratos, None,
                                     {"tdMode": "isolated", "posSide": "short", "reduceOnly": True})
            except Exception as e2:
                logger_errores.error(f"FALLO cerrar short tras bracket fallido {symbol}: {e2}")
            raise RuntimeError(f"bracket no colocado en {symbol}; short cerrado por fallback")
        qty_real = contratos * SHORT_CTVAL.get(symbol, 1.0)   # moneda realmente ejecutada
        return {"order_id": res.get("id"), "sl_order_id": algo_id,
                "entry_px": precio, "sl_px": sl_px, "tp_px": tp_px,
                "qty": qty_real, "contratos": contratos}

    def cancelar_algo(self, symbol, algo_id):
        """Cancela el bracket OCO (best-effort) al cerrar manual, para no dejar algos huérfanos
        que podrían disparar contra una posición futura del mismo símbolo."""
        if not algo_id:
            return
        try:
            self.ex.private_post_trade_cancel_algos(
                [{"algoId": str(algo_id), "instId": self._inst_id(symbol)}])
        except Exception as e:
            logger_errores.error(f"cancelar_algo {symbol} {algo_id}: {e}")

    def cerrar_short_market(self, symbol, qty):
        contratos = self._a_contratos(symbol, qty)       # qty (moneda guardada en DB) → contratos
        params = {"tdMode": "isolated", "posSide": "short", "reduceOnly": True}
        return self.ex.create_order(symbol, "market", "buy", contratos, None, params)

    def get_posiciones(self, symbols=None):
        return self.ex.fetch_positions(symbols)

    def usdt_disponible(self):
        bal = self.ex.fetch_balance()
        return float((bal.get("free") or {}).get("USDT", 0.0))


class EscuadronShortOkx(threading.Thread):
    """Pata bajista: shortea rallies BTC/ETH en régimen BAJISTA.

    Espejo simétrico de EscuadronGridOkx (que compra dips en alcista).
    Isolated + SL server-side. Solo opera con fut_enabled=true.
    """
    TICK_S = 60.0

    def __init__(self, core_short, db, tesoreria=None):
        super().__init__(name="EscuadronShortOkx", daemon=True)
        self.core = core_short
        self.db = db
        # GestorTesoreria: para redimir de earn → Trading y fondear el margen del short
        # al 18% objetivo (sin esto la pata abre con las migajas de free). Opcional (None
        # = degrada al comportamiento previo, usado en tests).
        self.tesoreria = tesoreria
        self._mejor_px = {}   # Fase 2: mínimo alcanzado por símbolo (para chandelier live)

    def _asegurar_margen(self, target_margen):
        """Si el free posteable no alcanza el margen objetivo, redime el déficit de earn
        (síncrono vía rescate_urgente). Solo el déficit, nunca más; rescate_urgente ya capa
        a lo que hay en earn. No-op sin tesorería o si el target es sub-mínimo."""
        if self.tesoreria is None or target_margen < SHORT_MIN_MARGEN_USD:
            return
        try:
            free = self.core.usdt_disponible()
        except Exception:
            return
        deficit = target_margen - free
        if deficit < 1.0:
            return
        try:
            self.tesoreria.rescate_urgente(deficit)
        except Exception as e:
            logger_errores.error(f"EscuadronShortOkx _asegurar_margen: {e}")

    def _equity(self):
        # USDT disponible (Trading) + earn USDT → capital total para el cap de la pata
        try:
            base = self.core.usdt_disponible()
        except Exception:
            base = 0.0
        return base + float(globals().get("earn_balance_usdt_okx", 0.0) or 0.0)

    def _gestionar(self, sym):
        # sym = perp ("BTC/USDT:USDT") para órdenes/DB; spot para datos/régimen/locks.
        spot = _spot_de_perp(sym)
        with regime_lock:
            mm = modo_mercado
        if mm != "BAJISTA":
            return
        if self.db.short_pnl_hoy() <= -CONFIG["short_daily_loss_cap_usd"]:
            return   # circuit breaker: cap de pérdida diaria alcanzado
        if is_panico_global() or is_vol_spike("okx", spot):
            return
        posiciones = self.db.short_get_posiciones()
        if any(p["symbol"] == sym for p in posiciones):
            return
        if len(posiciones) >= CONFIG["short_max_posiciones"]:
            return
        if CONFIG["short_canary_mode"] and len(posiciones) >= 1:
            return
        if (time.time() - self.db.short_cooldown(sym)) < SHORT_COOLDOWN_S:
            return
        if (time.time() - self.db.short_loss_cooldown(sym)) < SHORT_LOSS_COOLDOWN_S:
            return   # acaba de stopear en pérdida → no re-shortear el mismo rally (anti-revenge)
        ind = MotorIndicadores.get_indicators("okx", spot)
        if not senal_short(ind):
            return
        px = PRECIOS_RAM.get("okx", {}).get(spot)
        if not px or not is_price_fresh("okx", spot):
            return
        precio = px["bid"]
        lock = get_entry_lock("okx", spot)
        if not lock.acquire(timeout=CONFIG["entry_lock_timeout_ms"] / 1000.0):
            return
        try:
            ml = ml_size_multiplier("okx", spot)
            # Fondear margen ANTES de dimensionar: redime de earn el déficit hasta el 18%
            # objetivo. Recién acá (post-señal+gates) para no sacar earn si no vamos a abrir.
            target_margen = self._equity() * CONFIG["short_max_equity_pct"] * max(0.5, min(2.0, ml))
            self._asegurar_margen(target_margen)
            qty = calcular_size_short(self._equity(), precio, ml,
                                      disponible=self.core.usdt_disponible())
            if qty <= 0:
                return
            res = self.core.abrir_short(sym, precio, qty)
            self.db.short_abrir_posicion(sym, res["entry_px"], res.get("qty", qty),
                                         CONFIG["short_leverage"], res["sl_order_id"])
            self.db.log_caja_negra("SHORT_OPEN", sym,
                f"qty {res.get('qty', qty):.6f} @ {res['entry_px']:.2f} "
                f"SL {res['sl_px']:.2f} TP {res['tp_px']:.2f} lev {CONFIG['short_leverage']}x")
        except Exception as e:
            logger_errores.error(f"EscuadronShortOkx abrir {sym}: {e}")
        finally:
            lock.release()

    def _precio(self, sym):
        px = PRECIOS_RAM.get("okx", {}).get(_spot_de_perp(sym))
        return px["bid"] if px else 0.0

    def _cerrar(self, pos, motivo):
        sym = pos["symbol"]
        try:
            self.core.cerrar_short_market(sym, pos["qty"])
        except Exception as e:
            logger_errores.error(f"EscuadronShortOkx cerrar {sym}: {e}")
            return
        self.core.cancelar_algo(sym, pos.get("sl_order_id"))   # limpia el OCO huérfano en OKX
        px = self._precio(sym) or pos["entry_px"]
        pnl = (pos["entry_px"] - px) * pos["qty"]   # short: gana si el precio baja
        self.db.short_cerrar_posicion(sym, px, pnl)
        self.db.log_caja_negra("SHORT_CLOSE", sym, f"{motivo} pnl ${pnl:.2f} @ {px:.2f}")

    def _gestionar_salidas(self):
        if not regime_evaluado:
            return   # boot: no actuar sobre el régimen default ALCISTA (evita flip-close espurio
                     # del short antes de que RadarRegimen reevalúe). Patrón igual a GestorTesoreria.
        with regime_lock:
            mm = modo_mercado
        tp_pct = CONFIG["short_tp_pct"] * 100
        mode = CONFIG["short_gestor_mode"]
        # Selección de la política que gobierna las salidas. El OCO server-side (SL) sigue de
        # PISO bajo todo; esto solo hace salidas MÁS tempranas. shadow→None (sin gestión);
        # live/pin→estática (Fase 2); meta→per-símbolo por contexto (Fase 3).
        politica_estatica = None
        if mode == "live" or mode in POLITICAS_SALIDA:
            politica_estatica = seleccionar_politica_live(
                self.db.short_shadow_resumen(), mode,
                CONFIG["short_gestor_min_trades"], CONFIG["short_gestor_margen_pct"])
        for pos in self.db.short_get_posiciones():
            sym = pos["symbol"]
            # 1) flip de régimen → cerrar a mercado (el SL server-side ya cubre el downside)
            if mm != "BAJISTA":
                self._cerrar(pos, "flip-regimen")
                self._mejor_px.pop(sym, None)
                continue
            px = self._precio(sym)
            if px <= 0:
                continue
            # 2) gestión dinámica (Fase 2/3): resuelve la política y deja que decida la salida.
            if mode != "shadow":
                spot = _spot_de_perp(sym)
                ind = MotorIndicadores.get_indicators("okx", spot) or {}
                ind = {**ind, "funding": get_funding(sym)}   # sym = perp; para el contexto de funding
                if mode == "meta":
                    filas_ctx = self.db.short_shadow_rows_por_contexto(contexto_short(ind))
                    if CONFIG["short_meta_gate"] == "apareado":
                        politica = seleccionar_politica_meta_apareado(
                            filas_ctx, CONFIG["short_gestor_min_trades"],
                            SHORT_META_HALFLIFE_S, time.time())
                    else:
                        politica = seleccionar_politica_meta(
                            filas_ctx, CONFIG["short_gestor_min_trades"],
                            CONFIG["short_gestor_margen_pct"], SHORT_META_HALFLIFE_S, time.time())
                else:
                    politica = politica_estatica
                if politica and politica != "oco_fijo":
                    self._mejor_px[sym] = min(self._mejor_px.get(sym, pos["entry_px"]), px)
                    vp = {"entry_px": pos["entry_px"], "mejor_px": self._mejor_px[sym],
                          "entry_ts": pos.get("open_ts", 0.0)}
                    try:
                        ml_prob = ml_predict_prob("okx", spot)
                    except Exception:
                        ml_prob = None
                    res = POLITICAS_SALIDA[politica](vp, {"px": px, "ind": ind, "ml_prob": ml_prob})
                    if res is not None:
                        self._cerrar(pos, f"gestor-{politica}-{res[1]}")
                        self._mejor_px.pop(sym, None)
                        continue
            # 3) TP backstop in-process: el TP primario vive server-side (OCO en OKX); esto es
            #    red de seguridad y cubre posiciones legacy sin bracket. Cierra todo al +TP%.
            gain_pct = (pos["entry_px"] - px) / pos["entry_px"] * 100
            if gain_pct >= tp_pct:
                self._cerrar(pos, f"tp-backstop-{tp_pct:.0f}%")

    def cerrar_todo_kill(self):
        """KILL: cierra todas las posiciones short a mercado (reduceOnly)."""
        for pos in self.db.short_get_posiciones():
            self._cerrar(pos, "KILL")

    def reconciliar_shorts(self, contexto="boot"):
        """Sincroniza la DB con OKX. Si un short de la DB ya no existe en OKX (lo cerró el TP/SL
        server-side OCO), lo limpia de la DB + registra el PnL estimado. Corre al BOOT (cierres
        con el bot apagado) Y en cada TICK del loop (cierres server-side con el bot CORRIENDO —
        sin esto la fila quedaba fantasma hasta el próximo reinicio: PnL sin registrar + slot
        ocupado). En runtime aplica un grace para no falso-cerrar un short recién abierto que
        fetch_positions todavía no refleja; al boot el cierre ya es real (no hay grace)."""
        db_pos = self.db.short_get_posiciones()
        if not db_pos:
            return
        try:
            okx = self.core.get_posiciones(PARES_SHORT_OKX)
        except Exception as e:
            logger_errores.error(f"reconciliar_shorts: {e}")
            return
        vivos = set()
        for p in okx or []:
            try:
                sz = float(p.get("contracts") or (p.get("info", {}) or {}).get("pos") or 0)
            except Exception:
                sz = 0.0
            s = p.get("symbol")
            if s and sz != 0:
                vivos.add(s)
        for pos in db_pos:
            if pos["symbol"] in vivos:
                continue
            if contexto == "runtime" and \
               (time.time() - (pos.get("open_ts") or 0)) < SHORT_RECONCILE_GRACE_S:
                continue   # recién abierto: el lag de fetch_positions no es un cierre real
            px = self._precio(pos["symbol"]) or pos["entry_px"]
            pnl = (pos["entry_px"] - px) * pos["qty"]
            self.db.short_cerrar_posicion(pos["symbol"], px, pnl)
            self.db.log_caja_negra("SHORT_RECONCILE", pos["symbol"],
                f"cerrado server-side OCO ({contexto}); pnl est ${pnl:.2f} @ {px:.2f}")

    def _tick(self):
        # Un ciclo del loop. Reconcilia PRIMERO (detecta OCO disparado server-side con el bot
        # vivo y limpia la fila antes de que _gestionar_salidas intente cerrar algo inexistente),
        # luego salidas, luego entradas.
        self.reconciliar_shorts("runtime")
        self._gestionar_salidas()
        for sym in PARES_SHORT_OKX:
            self._gestionar(sym)

    def run(self):
        if not CONFIG["fut_enabled"]:
            return
        time.sleep(30.0)
        self.reconciliar_shorts("boot")  # sync DB ↔ OKX: limpia shorts cerrados con el bot apagado
        while not kill_switch.is_set():
            try:
                self._tick()
            except Exception as e:
                logger_errores.error(f"EscuadronShortOkx loop: {e}")
            for _ in range(int(self.TICK_S)):
                if kill_switch.is_set():
                    break
                time.sleep(1.0)
        # KILL detectado → cierre forzado de todas las posiciones short a mercado.
        # (En apagado ordenado Ctrl+C los shorts se PRESERVAN: el SL server-side los cubre.)
        if kill_switch.is_set():
            try:
                self.cerrar_todo_kill()
            except Exception as e:
                logger_errores.error(f"EscuadronShortOkx KILL close: {e}")


class GestorSalidaShadow(threading.Thread):
    """Fase 1 del 'surfista completo': multiverso de MEDICIÓN. Corre las políticas de
    salida candidatas en PAPER (cero capital, cero órdenes) sobre entradas virtuales
    gatilladas por senal_short en régimen BAJISTA. Registra el PnL contrafactual de
    cada política → dataset para decidir cuál gobierna capital real (Fase 2, diferida).
    Corre aunque OKX_FUT_ENABLED=false (decoupla medición de despliegue).
    Ver gestor_salida_shadow_spec.md."""

    TICK_S = SHORT_SHADOW_TICK_S

    def __init__(self, db, pares=None, ml_fn=None, senal_fn=None):
        super().__init__(name="GestorSalidaShadow", daemon=True)
        self.db = db
        self.pares = pares or PARES_SHORT_OKX
        self.ml_fn = ml_fn or ml_predict_prob
        self.senal_fn = senal_fn or senal_short   # override-able para el barredor offline
        self._abiertas = {}   # symbol -> vp {entry_px, mejor_px, entry_ts, vivas}

    def _registrar(self, politica, sym, vp, exit_px, motivo, now):
        pnl_pct = (vp["entry_px"] - exit_px) / vp["entry_px"] * 100.0   # short
        self.db.short_shadow_registrar(politica, sym, vp["entry_px"], exit_px,
                                       pnl_pct, motivo, vp["entry_ts"], now,
                                       contexto=vp.get("contexto", "normal"))

    def _procesar(self, sym, px, ind, ml_prob, regime_ok=True, now=None, permitir_entrada=True):
        """Un tick para un símbolo. Testeable por inyección (sin exchange). `permitir_entrada`
        False = solo gestiona virtuales ya abiertas (lo usa el backfill intrabar para no re-abrir
        dentro de la misma barra al replayear high/low/close)."""
        now = now if now is not None else time.time()
        vp = self._abiertas.get(sym)
        if vp is None:
            if not permitir_entrada:
                return
            # Entrada virtual: solo régimen BAJISTA + señal (sin gates de capital:
            # queremos máximas muestras limpias para rankear SALIDAS). Una por símbolo.
            if regime_ok and self.senal_fn(ind):
                self._abiertas[sym] = {"entry_px": px, "mejor_px": px, "entry_ts": now,
                                       "contexto": contexto_short(ind),  # mar (+ funding si opt-in)
                                       "vivas": set(POLITICAS_SALIDA.keys())}
            return
        vp["mejor_px"] = min(vp["mejor_px"], px)   # short: el mínimo es lo más favorable
        ctx = {"px": px, "ind": ind, "ml_prob": ml_prob}
        for nombre in list(vp["vivas"]):
            res = POLITICAS_SALIDA[nombre](vp, ctx)
            if res is not None:
                exit_px, motivo = res
                self._registrar(nombre, sym, vp, exit_px, motivo, now)
                vp["vivas"].discard(nombre)
        # max-hold: cierra a mercado las políticas que sigan vivas
        if (now - vp["entry_ts"]) >= SHORT_SHADOW_MAX_HOLD_S:
            for nombre in list(vp["vivas"]):
                self._registrar(nombre, sym, vp, px, "max-hold", now)
                vp["vivas"].discard(nombre)
        if not vp["vivas"]:
            del self._abiertas[sym]   # todas cerraron → el símbolo puede re-entrar

    def _tick(self):
        with regime_lock:
            regime_ok = (modo_mercado == "BAJISTA")
        for sym in self.pares:
            spot = _spot_de_perp(sym)
            ind = MotorIndicadores.get_indicators("okx", spot)
            px_entry = PRECIOS_RAM.get("okx", {}).get(spot)
            if not ind or not px_entry or not is_price_fresh("okx", spot):
                continue
            ind = {**ind, "funding": get_funding(sym)}   # sym = perp; funding live (0 si no hay)
            try:
                ml_prob = self.ml_fn("okx", spot)
            except Exception:
                ml_prob = None
            self._procesar(sym, px_entry["bid"], ind, ml_prob, regime_ok=regime_ok)

    def run(self):
        if not SHORT_SHADOW_ENABLED:
            return
        time.sleep(35.0)   # deja calentar indicadores/precios
        while not kill_switch.is_set():
            try:
                self._tick()
            except Exception as e:
                logger_errores.error(f"GestorSalidaShadow loop: {e}")
            for _ in range(int(self.TICK_S)):
                if kill_switch.is_set():
                    return
                time.sleep(1.0)


def backfill_shadow_bars(db, perp_sym, bars, regime_flags, ml_fn=None, senal_fn=None):
    """Replay OFFLINE del shadow sobre barras históricas — siembra el dataset sin esperar al
    futuro (misma lógica que el ML: minar el pasado en vez de aguardar trades en vivo, que en
    alcista nunca llegan). Reutiliza GestorSalidaShadow._procesar: por cada barra abre/avanza/
    registra las políticas igual que en vivo. `bars` = dicts con 'close', 'ts' (segundos) y los
    indicadores (ADX_14/RSI_14/MACDh_12_26_9/DMP_14/DMN_14/ATRr_14). `regime_flags` = bool BAJISTA
    por barra. ml_prob=None (ml_adverso sólo SL en backfill; su edge se mide en vivo).
    INTRABAR: si la barra trae high/low, una virtual abierta se gestiona replayeando
    high (adverso) → low (favorable) → close, en ese orden PESIMISTA (cuenta el SL antes que el TP
    si ambos se tocan en la misma barra; y mejor_px toma el LOW real). La ENTRADA se decide solo al
    close (1 por barra, como en vivo → no re-abre intrabar). Sin high/low → close-to-close (compat).
    `senal_fn` override-able para el barredor de señal (mide variantes de entrada offline)."""
    g = GestorSalidaShadow(db, ml_fn=ml_fn, senal_fn=senal_fn)
    n = 0
    for bar, bajista in zip(bars, regime_flags):
        close = float(bar["close"])
        now = float(bar["ts"])
        if perp_sym in g._abiertas:
            for p in (float(bar.get("high", close)), float(bar.get("low", close)), close):
                g._procesar(perp_sym, p, bar, None, regime_ok=bool(bajista),
                            now=now, permitir_entrada=False)
        else:
            g._procesar(perp_sym, close, bar, None, regime_ok=bool(bajista), now=now)
        n += 1
    return n


class EntrenadorPeriodico(threading.Thread):
    """Re-entrena el ML (y opcional backfillea el shadow) cada ENTRENAR_INTERVALO_H llamando a
    entrenar.py como SUBPROCESO aislado: si falla no tumba el bot; el modelo se hot-reloadea por
    mtime. Mantiene fresco el 'mapa' sin corridas manuales (las actualizaciones deben ser
    constantes, no esporádicas)."""

    def __init__(self, db, runner=None):
        super().__init__(name="EntrenadorPeriodico", daemon=True)
        self.db = db
        self._runner = runner or self._subprocess_run

    @staticmethod
    def _subprocess_run(cmd):
        import subprocess
        return subprocess.run(cmd, capture_output=True, text=True, timeout=7200)

    @staticmethod
    def _comando(backfill=False):
        if backfill:
            return [sys.executable, "entrenar.py", "--shadow-backfill", "--dias", str(ENTRENAR_DIAS)]
        return [sys.executable, "entrenar.py", "--dias", str(ENTRENAR_DIAS)]

    def _ejecutar(self):
        try:
            self._runner(self._comando(backfill=False))
            self.db.log_caja_negra("ENTRENAR", "ML", f"retrain auto ({ENTRENAR_DIAS}d)")
            if ENTRENAR_AUTO_BACKFILL:
                self._runner(self._comando(backfill=True))
                self.db.log_caja_negra("ENTRENAR", "SHADOW", "backfill auto")
        except Exception as e:
            logger_errores.error(f"EntrenadorPeriodico: {e}")

    def run(self):
        if not ENTRENAR_AUTO_ENABLED:
            return
        time.sleep(120.0)   # deja arrancar el bot antes del primer entrenamiento
        while not kill_switch.is_set():
            self._ejecutar()
            for _ in range(int(ENTRENAR_INTERVALO_H * 3600)):
                if kill_switch.is_set():
                    return
                time.sleep(1.0)


class EscuadronGridOkx(threading.Thread):
    """OKX_SPOT — modo dual BTC/ETH (BUY maker + Grid Harvest)."""

    TICK_S = 60.0

    def __init__(self, core_okx, db: DatabaseManager):
        super().__init__(name="EscuadronGridOkx", daemon=True)
        self.core = core_okx
        self.db = db
        self._last_harvest_ts = {}

    def _has_grid_harvest(self, sym):
        row = self.db._exec(
            "SELECT order_id FROM okx_grid_orders WHERE symbol=? AND side='SELL' LIMIT 1",
            (sym,), fetch="one"
        )
        return row is not None

    def _has_grid_buy(self, sym):
        row = self.db._exec(
            "SELECT order_id FROM okx_grid_orders WHERE symbol=? AND side='BUY' LIMIT 1",
            (sym,), fetch="one"
        )
        return row is not None

    def _gestionar(self, sym):
        ind = MotorIndicadores.get_indicators("okx", sym)
        if not ind:
            return
        portfolio = next(
            (r for r in self.db.get_active_portfolio("okx") if r[0] == sym), None
        )
        bid_entry = PRECIOS_RAM.get("okx", {}).get(sym)
        if not bid_entry or not is_price_fresh("okx", sym):
            return
        bid = bid_entry["bid"]
        close = float(ind.get("close", 0))
        adx = float(ind.get("ADX_14", 0))
        ema200 = float(ind.get("EMA_200", 0))

        with regime_lock:
            mm = modo_mercado
            reg_estado = regimen_mercado
            reg_conf = regimen_confianza
            reg_eval = regimen_evaluado
        # V1.8.0 Cerebro de Régimen: shadow=idéntico al binario actual; live=4-estados gobierna.
        # V1.8.6: reg_eval = guard de warmup (no entrar sobre el default RANGO de boot).
        opera, reg_mult = grid_regimen_decision(
            CONFIG["regimen_brain_mode"], mm, reg_estado, reg_conf, reg_eval)

        lock = get_entry_lock("okx", sym)
        if not lock.acquire(timeout=CONFIG["entry_lock_timeout_ms"] / 1000.0):
            return
        try:
            if portfolio is None:
                if not opera:
                    return
                if is_panico_global():
                    return
                if is_vol_spike("okx", sym):
                    return
                if self._has_grid_buy(sym):
                    return
                if adx >= 20 or close <= ema200:
                    return
                # V1.4.1 Mejora #3: cooldown dinámico
                last_buy = self.db.get_cooldown("okx", sym)
                cd_eff = get_cooldown_dyn("okx", sym, CONFIG["okx_cooldown_seconds"])
                if (time.time() - last_buy) < cd_eff:
                    return
                # V1.4.1 Mejora #1: OBI gate opcional
                if CONFIG["grid_obi_gate_enabled"]:
                    obi = calcular_obi_spot(self.core.exchange_read, sym)
                    if obi is not None and obi <= CONFIG["grid_obi_min"]:
                        self.db.log_caja_negra("OKX_GRID_OBI_SKIP", sym,
                                                f"OBI={obi:+.3f} ≤ {CONFIG['grid_obi_min']:+.2f}")
                        return
                bal = self.core.get_balance()
                free = float(bal.get("USDT", {}).get("free", 0))
                monto_base = max(5.0, CONFIG["okx_investment_pct"] * free)
                # V1.5.0: ML modula sizing dentro de [MIN_MULT, MAX_MULT]. mult=1.0 si modelo offline.
                ml_mult = ml_size_multiplier("okx", sym)
                # V1.8.0: en live, el régimen modula además por estado×confianza (1.0 en shadow).
                monto = max(5.0, monto_base * ml_mult * reg_mult)
                if monto < 5.0:
                    return
                if abs(ml_mult - 1.0) > 0.05 or abs(reg_mult - 1.0) > 0.05:
                    self.db.log_caja_negra("OKX_ML_SIZING", sym,
                        f"Tamaño: base ${monto_base:.2f} × ml {ml_mult:.2f} × reg {reg_mult:.2f}"
                        f" ({reg_estado}) = ${monto:.2f}")
                price = close * 0.996
                self.core.execute_limit_maker(sym, price, monto)
            else:
                _, avg_cost, qty, inv, mode = portfolio
                if self._has_grid_harvest(sym):
                    return
                last_h = self._last_harvest_ts.get(sym, 0.0)
                if (time.time() - last_h) < HARVEST_COOLDOWN:
                    return
                if bid <= avg_cost:
                    return
                gain_pct = (bid - avg_cost) / avg_cost * 100
                # V1.4.1 Mejora #2: tiers adaptativos por ATR
                tiers = get_harvest_tiers_dyn("okx", sym)
                next_tier = next((t for t in tiers if t > gain_pct), None)
                if next_tier is None:
                    return
                target_price = avg_cost * (1 + next_tier / 100.0)
                qty_sell = qty * HARVEST_FRAC
                if qty_sell * target_price < 5.0:
                    return
                res = self.core.execute_limit_sell_harvest(sym, target_price, qty_sell)
                if res:
                    self._last_harvest_ts[sym] = time.time()
                    self.db.log_caja_negra("OKX_HARVEST", sym,
                                            f"tier {next_tier}% @ {target_price:.4f} qty {qty_sell:.6f}")
        finally:
            lock.release()

    def run(self):
        time.sleep(30.0)
        while not kill_switch.is_set():
            try:
                for sym in PARES_OKX_SPOT:
                    self._gestionar(sym)
            except Exception as e:
                logger_errores.error(f"EscuadronGridOkx loop: {e}")
            for _ in range(int(self.TICK_S)):
                if kill_switch.is_set():
                    return
                time.sleep(1.0)


class EscuadronLadderOkx(threading.Thread):
    """SP3 — sleeve spot grid ladder round-trip, aislado del grid core. Canary.
    Compra maker en escalera bajo `ref`, vende round-trip a +spacing, stopea colgados, flatten al
    salir de RANGO (régimen PROPIO del activo). Orden por el cliente ccxt crudo (NO execute_limit_maker,
    que está acoplado al grid: okx_grid_orders + allocate_budget) → bookkeeping propio en ladder_*."""

    def __init__(self, core_okx, db: DatabaseManager):
        super().__init__(name="EscuadronLadderOkx", daemon=True)
        self.core = core_okx
        self.db = db
        self.pares = list(PARES_LADDER)
        self._ref = {}          # sym -> ref price del segmento RANGO (None = sin centrar)
        self._reg_cache = {}    # sym -> (estado, ts) régimen propio cacheado (TTL 1h)
        self._mudo_desde = {}   # sym -> ts del último grito por falta de precio (throttle 15m)

    # --- helpers de orden (cliente crudo + okx_retry; bookkeeping ladder_*) ---
    def _flush(self, sym, qty):
        """Trae la cripto de Earn a spot antes de vender (GestorInventario barre alts a Earn con
        OKX_INV_INCLUYE_ALTS=true → sin esto el sell del ladder fallaría). Espejo del grid.
        Además consolida cualquier saldo VARADO en funding → trading: el barrido a Earn deja la
        cripto del ladder en funding tras un purchase fallido (visto 2026-07-02, TRX: 16.56 TRX
        varados en funding, Earn en 0 → flush_to_spot no-op → el sell no llenaba, OKX 51008).
        Self-healing e idempotente: si no hay nada en funding, no-op."""
        inv = getattr(self.core, "inventario", None)
        if inv is None:
            return
        try:
            inv.flush_to_spot(sym, qty)
        except Exception as e:
            logger_errores.error(f"Ladder flush {sym}: {e}")
        fbal = getattr(inv, "_funding_balance_ccy", None)
        transf = getattr(inv, "_transfer", None)
        if fbal is None or transf is None:
            return
        try:
            ccy = sym.split("/")[0]
            f = fbal(ccy)
            if f > 1e-9:
                transf(ccy, f, OKX_CUENTA_FUNDING, OKX_CUENTA_TRADING)
                self.core.invalidate_balance()
        except Exception as e:
            logger_errores.error(f"Ladder consolidar funding {sym}: {e}")

    def _qty_vendible(self, sym, qty):
        """El qty registrado es el BRUTO de la orden; el fee del buy se cobró en la cripto
        (feeCcy=base) → el holding real es qty−fee. Vender el bruto → OKX 51008 (visto 2026-07-02).
        Capa al free spot REAL. Lectura desconocida/0 → intenta el bruto (sin regresión: el exchange
        lo rechaza igual que antes, fail-safe)."""
        ccy = sym.split("/")[0]
        try:
            free = float(self.core.get_balance(force_refresh=True).get(ccy, {}).get("free", 0.0) or 0.0)
        except Exception:
            return qty
        if free <= 0 or free >= qty:
            return qty
        vend = free
        ex = getattr(self.core, "exchange_trade", None)
        if ex is not None and hasattr(ex, "amount_to_precision"):
            try:
                vend = float(ex.amount_to_precision(sym, vend))
            except Exception:
                pass
        return vend

    def _recuperar_por_clordid(self, sym, cid):
        """Tras un create fallido, ¿la orden llegó igual a OKX? (timeout ambiguo: aterrizó pero
        no respondió). Consulta por clOrdId — es un READ, idempotente y seguro de reintentar.
        Devuelve el order dict si existe, None si nunca aterrizó. Fail-safe: error/no-soportado
        → None (= conducta previa al fix, sin regresión)."""
        try:
            with self.core.api_trade_lock:
                od = okx_retry(self.core.exchange_trade.fetch_order, None, sym,
                               params={"clOrdId": cid})
            return od if od and od.get("id") else None
        except Exception:
            return None

    def _buy_maker(self, sym, rung_idx, price, qty):
        cid = nuevo_clordid("lad")
        try:
            with self.core.api_trade_lock:
                order = okx_retry(self.core.exchange_trade.create_limit_buy_order,
                                  sym, qty, price, params={"tdMode": "cash", "clOrdId": cid})
        except Exception as e:
            order = self._recuperar_por_clordid(sym, cid)   # ¿aterrizó pese al fallo?
            if not order:
                logger_errores.error(f"Ladder buy {sym} rung{rung_idx}: {e}")
                return None
        try:
            oid = str(order.get("id"))
            self.db.ladder_registrar_orden(oid, sym, "buy", rung_idx, price, qty)
            return oid
        except Exception as e:
            logger_errores.error(f"Ladder buy registrar {sym} rung{rung_idx}: {e}")
            return None

    def _sell_limit(self, sym, rung_idx, price, qty):
        self._flush(sym, qty)
        qty = self._qty_vendible(sym, qty)
        if qty <= 0:
            return None
        cid = nuevo_clordid("lad")
        try:
            with self.core.api_trade_lock:
                order = okx_retry(self.core.exchange_trade.create_limit_sell_order,
                                  sym, qty, price, params={"tdMode": "cash", "clOrdId": cid})
        except Exception as e:
            order = self._recuperar_por_clordid(sym, cid)
            if not order:
                logger_errores.error(f"Ladder sell {sym} rung{rung_idx}: {e}")
                return None
        try:
            oid = str(order.get("id"))
            self.db.ladder_registrar_orden(oid, sym, "sell", rung_idx, price, qty)
            return oid
        except Exception as e:
            logger_errores.error(f"Ladder sell registrar {sym} rung{rung_idx}: {e}")
            return None

    def _cerrar_market(self, sym, rung_idx, qty, buy_px, motivo):
        self._flush(sym, qty)
        qty = self._qty_vendible(sym, qty)
        if qty <= 0:
            return
        cid = nuevo_clordid("lad")
        try:
            with self.core.api_trade_lock:
                order = okx_retry(self.core.exchange_trade.create_market_sell_order,
                                  sym, qty, params={"tdMode": "cash", "clOrdId": cid})
        except Exception as e:
            order = self._recuperar_por_clordid(sym, cid)   # ¿la venta a mercado aterrizó igual?
            if not order:
                logger_errores.error(f"Ladder cerrar {sym} rung{rung_idx}: {e}")
                return
        try:
            px = float(order.get("average") or order.get("price") or 0.0)
            pnl = (px - buy_px) * qty if buy_px else 0.0
            self.db.ladder_registrar_realizado(sym, qty, buy_px, px, pnl, motivo)
            self.db.ladder_del_posicion(sym, rung_idx)
        except Exception as e:
            logger_errores.error(f"Ladder cerrar registrar {sym} rung{rung_idx}: {e}")

    def _ejecutar_accion(self, sym, a):
        t = a["tipo"]
        if t == "buy":
            self._buy_maker(sym, a["rung_idx"], a["price"], a["qty"])
        elif t == "sell":
            self._sell_limit(sym, a["rung_idx"], a["price"], a["qty"])
        elif t in ("stop", "flatten"):
            self._cerrar_market(sym, a["rung_idx"], a.get("qty", 0.0), a.get("buy_px", 0.0), t)

    def reconciliar_ladder(self, contexto="tick"):
        """OKX = fuente de verdad. Por cada orden 'open' en ladder_orders consulta su estado:
        buy lleno → marca fill (fill-log), crea posición y coloca el sell round-trip a +spacing;
        sell lleno → cierra el round-trip (registra realizado, libera el rung). Corre en boot+tick."""
        spacing = CONFIG["ladder_spacing_pct"]
        rows = self.db._exec("SELECT order_id, symbol, side, rung_idx, price, qty "
                             "FROM ladder_orders WHERE state='open'", fetch="all") or []
        for oid, sym, side, rung_idx, price, qty in rows:
            try:
                with self.core.api_trade_lock:
                    od = okx_retry(self.core.exchange_trade.fetch_order, oid, sym)
            except Exception as e:
                logger_errores.error(f"Ladder reconcile fetch {oid}: {e}")
                continue
            od = od or {}
            lleno = float(od.get("filled") or 0.0)
            accion = ladder_accion_reconcile(od.get("status"), lleno)
            if accion == "esperar":
                continue
            if accion == "terminal":
                self.db.ladder_marcar_terminal(oid)
                logger_errores.error(
                    f"Ladder {sym} rung{rung_idx}: {side} {oid} TERMINAL en OKX "
                    f"(status={od.get('status')}) sin fill — rung liberado.")
                if side == "sell":
                    # La posición sigue retenida. Sin limpiar el sell_order_id muerto,
                    # _rearmar_ventas_huerfanas la salta (solo mira los vacíos) y la cripto
                    # queda sin salida para siempre: sería cambiar un silencio por otro.
                    self.db.ladder_actualizar_sell(sym, rung_idx, "")
                continue
            qty_real = lleno if lleno > 0 else qty                    # 'fill' total o parcial
            if side == "buy":
                self.db.ladder_marcar_tocado(oid)
                self.db.ladder_marcar_fill(oid)                       # state→filled + fill-log
                if qty_real < qty - 1e-12:
                    logger_errores.error(
                        f"Ladder {sym} rung{rung_idx}: buy {oid} cancelada con fill PARCIAL "
                        f"{qty_real:.8f}/{qty:.8f} — se contabiliza lo realmente comprado.")
                sell_px = price * (1 + spacing / 100.0)
                sell_oid = self._sell_limit(sym, rung_idx, sell_px, qty_real)
                self.db.ladder_set_posicion(sym, rung_idx, qty_real, price, sell_oid or "")
            else:                                                     # sell lleno = round-trip cerrado
                pos = next((p for p in self.db.ladder_get_posiciones()
                            if p["symbol"] == sym and p["rung_idx"] == rung_idx), None)
                buy_px = pos["avg_cost"] if pos else 0.0
                fill_px = float(od.get("average") or price)
                pnl = (fill_px - buy_px) * qty_real if buy_px else 0.0
                self.db.ladder_registrar_realizado(sym, qty_real, buy_px, fill_px, pnl, "round-trip")
                restante = (pos["qty"] - qty_real) if pos else 0.0
                if restante > 1e-12:
                    # Vendió una parte y la cancelaron: el resto sigue en la cuenta y necesita
                    # salida nueva. Con sell_order_id="" lo re-arma _rearmar_ventas_huerfanas.
                    self.db.ladder_ajustar_posicion(sym, rung_idx, restante, "")
                    logger_errores.error(
                        f"Ladder {sym} rung{rung_idx}: sell {oid} cancelada con fill PARCIAL "
                        f"{qty_real:.8f} — quedan {restante:.8f}, se re-arma la salida.")
                else:
                    self.db.ladder_del_posicion(sym, rung_idx)
                self.db._exec("UPDATE ladder_orders SET state='filled' WHERE order_id=?", (oid,))
        self._rearmar_ventas_huerfanas()

    def _rearmar_ventas_huerfanas(self):
        """Si la sell round-trip falló al colocarse en el fill (visto en vivo 2026-06-30: OKX 51008,
        el TRX recién comprado/redimido de Earn no estaba disponible en ese instante), la posición
        queda con sell_order_id vacío y el loop de arriba NO la revisita (solo mira state='open') →
        holding sin salida. Reintenta la sell por cada posición huérfana; idempotente por tick (al
        siguiente tick el balance ya liquidó). El OCO/round-trip se cierra por el reconcile normal."""
        spacing = CONFIG["ladder_spacing_pct"]
        for p in self.db.ladder_get_posiciones():
            if p.get("sell_order_id"):
                continue
            sell_px = p["avg_cost"] * (1 + spacing / 100.0)
            sell_oid = self._sell_limit(p["symbol"], p["rung_idx"], sell_px, p["qty"])
            if sell_oid:
                self.db.ladder_actualizar_sell(p["symbol"], p["rung_idx"], sell_oid)

    # --- fuentes live (testeables por monkeypatch) ---
    def _precio(self, sym):
        e = PRECIOS_RAM.get("okx", {}).get(sym)
        if not e or not is_price_fresh("okx", sym):
            return None
        return e.get("bid")

    def _regimen(self, sym):
        now = time.time()
        c = self._reg_cache.get(sym)
        if c and now - c[1] < 3600:
            return c[0]
        try:
            with self.core.api_trade_lock:
                velas = okx_retry(self.core.exchange_trade.fetch_ohlcv, sym, "1d", limit=300)
            est = regimen_propio(velas)
        except Exception as e:
            logger_errores.error(f"Ladder regimen {sym}: {e}")
            est = REGIMEN_CAOS
        self._reg_cache[sym] = (est, now)
        return est

    # --- estado/contabilidad para el planner ---
    def _estado(self, sym, n):
        pos = {p["rung_idx"]: p for p in self.db.ladder_get_posiciones() if p["symbol"] == sym}
        pend = {r[0] for r in (self.db._exec(
            "SELECT rung_idx FROM ladder_orders WHERE symbol=? AND side='buy' AND state='open'",
            (sym,), fetch="all") or [])}
        rungs = []
        for k in range(n):
            if k in pos:
                rungs.append({"state": "holding", "buy_px": pos[k]["avg_cost"],
                              "qty": pos[k]["qty"], "rung_idx": k})
            elif k in pend:
                rungs.append({"state": "pending", "rung_idx": k})
            else:
                rungs.append({"state": "armed", "buy_px": 0.0, "rung_idx": k})
        return {"rungs": rungs, "ref": self._ref.get(sym)}

    def _comprometido(self, sym):
        pos = sum(p["qty"] * p["avg_cost"] for p in self.db.ladder_get_posiciones()
                  if p["symbol"] == sym)
        ob = self.db._exec("SELECT COALESCE(SUM(price*qty),0) FROM ladder_orders "
                           "WHERE symbol=? AND side='buy' AND state='open'", (sym,), fetch="one")
        return pos + float(ob[0] if ob else 0.0)

    def _publicar(self, sym, regimen, motivo, precio, n, cap):
        """Deja el estado del par en RAM para el HUD. Se llama en CADA tick, opere o no:
        el silencio es justo lo que no se puede distinguir de un hilo muerto (directiva 20).
        Nunca levanta — un fallo de telemetría no puede tumbar el sleeve."""
        try:
            rungs = self._estado(sym, n)["rungs"]
            ladder_estado_publicado[sym] = {
                "ts": time.time(), "regimen": regimen, "motivo": motivo,
                "armed": sum(1 for r in rungs if r["state"] == "armed"),
                "pending": sum(1 for r in rungs if r["state"] == "pending"),
                "holding": sum(1 for r in rungs if r["state"] == "holding"),
                "comprometido": self._comprometido(sym), "cap": cap, "precio": precio,
            }
        except Exception as e:
            logger_errores.error(f"Ladder publicar estado {sym}: {e}")

    def _tick(self):
        try:
            self.reconciliar_ladder("tick")
        except Exception as e:
            logger_errores.error(f"Ladder reconcile tick: {e}")
        cap = CONFIG["ladder_max_capital"]
        n = max(1, CONFIG["ladder_rungs"])
        cfg = {"rungs": n, "spacing_pct": CONFIG["ladder_spacing_pct"],
               "stop_pct": CONFIG["ladder_stop_pct"], "monto_rung": max(5.0, cap / n)}
        loss_cap = abs(CONFIG["ladder_daily_loss_cap"])
        # Mismo predicado de siempre (loss_cap OR pánico OR kill), pero NOMBRADO: el HUD
        # tiene que poder decir POR QUÉ está congelado, no solo que lo está.
        if kill_switch.is_set():
            motivo_global = "kill"
        elif is_panico_global():
            motivo_global = "panico"
        elif self.db.ladder_pnl_hoy() <= -loss_cap:
            motivo_global = "loss_cap"
        else:
            motivo_global = None
        congelado = motivo_global is not None
        for sym in self.pares:
            precio = self._precio(sym)
            if not precio:
                self._publicar(sym, "?", "sin_precio", None, n, cap)
                # Directiva 20: no operar es correcto, callarse NO. Un `continue` mudo aquí
                # escondió 11.5 h de canary muerto (el par no estaba suscrito en el feed y
                # errors.log salió limpio). Throttle 15m para avisar sin martillar el log.
                ahora = time.time()
                if ahora - self._mudo_desde.get(sym, 0.0) > 900.0:
                    self._mudo_desde[sym] = ahora
                    logger_errores.error(
                        f"Ladder {sym}: SIN PRECIO en PRECIOS_RAM['okx'] — el sleeve no puede "
                        f"operar. ¿El par está en el universo del feed (simbolos_feed_okx)?")
                continue
            self._mudo_desde.pop(sym, None)
            regimen = self._regimen(sym)
            # Fuera de RANGO el planner solo aplana, nunca arma: ese es el motivo dominante.
            # El congelamiento global (pánico/kill/loss_cap) solo muerde DENTRO de RANGO.
            self._publicar(sym, regimen,
                           "regimen" if regimen != REGIMEN_RANGO else motivo_global,
                           precio, n, cap)
            if regimen != REGIMEN_RANGO:
                self._ref[sym] = None
            # fill-realism: marca 'tocado' los buys abiertos cuyo nivel alcanzó el precio
            for oid, rprice in (self.db._exec(
                    "SELECT order_id, price FROM ladder_orders "
                    "WHERE symbol=? AND side='buy' AND state='open'", (sym,), fetch="all") or []):
                if precio <= rprice:
                    self.db.ladder_marcar_tocado(oid)
            acciones = ladder_acciones(self._estado(sym, n), precio, regimen, cfg)
            if congelado:
                acciones = [a for a in acciones if a["tipo"] != "buy"]   # congela compras, deja salidas
            comprometido, armo_buy = self._comprometido(sym), False
            for a in acciones:
                if a["tipo"] == "buy":
                    costo = a["price"] * a["qty"]
                    if comprometido + costo > cap + 1e-6:
                        continue
                    comprometido += costo
                    armo_buy = True
                self._ejecutar_accion(sym, a)
            if armo_buy and self._ref.get(sym) is None:
                self._ref[sym] = precio

    def cerrar_todo_kill(self):
        """KILL: cancela las órdenes resting y market-sell TODAS las holdings del ladder
        (contrato del kill-switch: cierre forzado). En Ctrl+C ordenado NO se llama (los maker
        limits quedan resting y el reconcile los recupera al rearrancar)."""
        for sym in self.pares:
            for (oid,) in (self.db._exec("SELECT order_id FROM ladder_orders "
                           "WHERE symbol=? AND state='open'", (sym,), fetch="all") or []):
                try:
                    with self.core.api_trade_lock:
                        self.core.exchange_trade.cancel_order(oid, sym)
                except Exception:
                    pass
                self.db._exec("UPDATE ladder_orders SET state='cancelled' WHERE order_id=?", (oid,))
            for p in [x for x in self.db.ladder_get_posiciones() if x["symbol"] == sym]:
                self._cerrar_market(sym, p["rung_idx"], p["qty"], p["avg_cost"], "KILL")

    def run(self):
        time.sleep(30.0)
        try:
            self.reconciliar_ladder("boot")
        except Exception as e:
            logger_errores.error(f"Ladder reconcile boot: {e}")
        while not kill_switch.is_set():
            try:
                self._tick()
            except Exception as e:
                logger_errores.error(f"EscuadronLadderOkx loop: {e}")
            for _ in range(int(CONFIG["ladder_tick_s"])):
                if kill_switch.is_set():
                    break
                time.sleep(1.0)
        # KILL detectado → cierre forzado de holdings + cancela resting (como EscuadronShortOkx).
        if kill_switch.is_set():
            try:
                self.cerrar_todo_kill()
            except Exception as e:
                logger_errores.error(f"EscuadronLadderOkx KILL close: {e}")


def _build_ladder(core_okx, db):
    """Construye el sleeve ladder si LADDER_ENABLED; None si off. Testeable sin arrancar main()."""
    if not CONFIG["ladder_enabled"]:
        return None
    return EscuadronLadderOkx(core_okx, db)


class GestorTesoreria(threading.Thread):
    """OKX Simple Earn USDT. Target = okx_spot_max_cap × factor_capital.
    Banda muerta 5%. Savings debita de Funding (transfer interno requerido).
    rescate_urgente(amt) SÍNCRONO para entradas (LONG, DCA L3, Grid BUY).
    KILL → standby."""

    CICLO_S = 3600.0

    def __init__(self, core_okx, db: DatabaseManager):
        super().__init__(name="GestorTesoreria", daemon=True)
        self.core = core_okx
        self.db = db
        self._last_target = 0.0
        self._lock = tesoreria_lock
        # Bóveda automática (auditoría okx_gemini V11.5): barre PnL realizado positivo a Funding.
        # Lee okx_realized_pnl posterior a este timestamp en cada ciclo.
        self._last_vault_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # -------- API helpers (OKX) — delegan al núcleo okx-semantica (ccy=USDT, 4 dec) --------
    def _earn_balance(self):
        return okx_saldo_earn(self.core.exchange_trade, "USDT")

    def _funding_balance(self):
        return okx_saldo_funding(self.core.exchange_trade, "USDT")

    def _transferir(self, amt, src_cuenta, dst_cuenta):
        return okx_transfer(self.core.exchange_trade, "USDT", amt, src_cuenta, dst_cuenta, decimales=4)

    def _purchase(self, amt):
        return okx_earn_purchase(self.core.exchange_trade, "USDT", amt, decimales=4)

    def _redempt(self, amt):
        return okx_earn_redeem(self.core.exchange_trade, "USDT", amt, decimales=4)

    # -------- Rescate síncrono (mitigación race) --------
    def rescate_urgente(self, amt_usdt):
        """Rescata amt_usdt de Earn → Funding → Trading. Síncrono.
        Devuelve cantidad efectivamente disponible en Trading."""
        global earn_balance_usdt_okx
        with self._lock:
            earn_now = self._earn_balance()
            if earn_now < 1.0:
                return 0.0
            need = min(earn_now, amt_usdt + 1.0)  # +1 buffer
            if not self._redempt(need):
                return 0.0
            # Poll hasta 30s a Funding
            t0 = time.time()
            funding = 0.0
            while (time.time() - t0) < 30:
                time.sleep(1.5)
                funding = self._funding_balance()
                if funding >= need * 0.99:
                    break
            if funding < 1.0:
                return 0.0
            transferir = min(funding, need)
            if not self._transferir(transferir, OKX_CUENTA_FUNDING, OKX_CUENTA_TRADING):
                return 0.0
            self.core.invalidate_balance()
            earn_balance_usdt_okx = max(0.0, earn_balance_usdt_okx - transferir)
            self.db.log_caja_negra("OKX_EARN", "USDT",
                                    f"rescate_urgente {transferir:.2f} USDT → Trading")
            return transferir

    # -------- Bóveda automática (auditoría okx_gemini V11.5) --------
    def _barrer_pnl_a_funding(self):
        """Lee okx_realized_pnl post último sweep, suma PnL>0, transfiere fracción a Funding.
        NO va a Earn — preserva liquidez Funding para rescate inmediato si re-entra.
        Sólo avanza _last_vault_ts tras un transfer exitoso o liquidez insuficiente
        (no antes del threshold check — acumula small wins entre ciclos).
        Adquiere tesoreria_lock para serializar con rescate_urgente y _ciclo."""
        try:
            with self._lock:
                rows = self.db._exec(
                    "SELECT ts, pnl_usdt FROM okx_realized_pnl WHERE ts > ? ORDER BY ts",
                    (self._last_vault_ts,), fetch="all"
                ) or []
                if not rows:
                    return
                pnl_pos = sum(p for _, p in rows if p and p > 0)
                if pnl_pos < CONFIG["vault_pnl_threshold"]:
                    return  # acumula para próximo ciclo (NO avanza _last_vault_ts)
                sweep = pnl_pos * CONFIG["vault_pnl_frac"]
                bal = self.core.get_balance(force_refresh=True)
                free = float(bal.get("USDT", {}).get("free", 0)) if isinstance(bal.get("USDT"), dict) else 0.0
                if free < sweep + 5.0:
                    # Sin liquidez para barrer ahora: avanza cursor igual para no acumular
                    # indefinidamente (los pnl ya están realizados en saldo, solo no se mueve a Funding).
                    self._last_vault_ts = rows[-1][0]
                    return
                if self._transferir(sweep, OKX_CUENTA_TRADING, OKX_CUENTA_FUNDING):
                    self._last_vault_ts = rows[-1][0]
                    self.core.invalidate_balance()
                    self.db.log_caja_negra("OKX_VAULT", "USDT",
                                            f"bóveda +{sweep:.2f} USDT (PnL realizado {pnl_pos:.2f})")
        except Exception as e:
            logger_errores.error(f"GestorTesoreria bóveda: {e}")

    # -------- Ciclo principal --------
    def _ciclo(self):
        global earn_balance_usdt_okx, earn_status_okx, factor_capital, regime_evaluado
        if kill_switch.is_set():
            earn_status_okx = "STANDBY (KILL)"
            return
        # Espera primer cálculo de régimen
        if not regime_evaluado:
            earn_status_okx = "WAIT_REGIMEN"
            return

        # Bóveda: barre PnL realizado positivo a Funding (antes del cálculo de target,
        # para que el saldo a parquear refleje el flujo neto).
        self._barrer_pnl_a_funding()

        with self._lock:
            with regime_lock:
                fc = factor_capital
            target = CONFIG["okx_spot_max_cap"] * fc
            # Banda muerta 5%
            if abs(target - self._last_target) / max(self._last_target, 1.0) < 0.05 and self._last_target > 0:
                target = self._last_target
            else:
                self._last_target = target

            bal = self.core.get_balance(force_refresh=True)
            free = float(bal.get("USDT", {}).get("free", 0)) \
                   if isinstance(bal.get("USDT"), dict) else 0.0
            earn_now = self._earn_balance()
            earn_balance_usdt_okx = earn_now

            # Depósito: free > target + 10
            if free > target + 10:
                deposit_amt = free - target
                if self._transferir(deposit_amt, OKX_CUENTA_TRADING, OKX_CUENTA_FUNDING):
                    time.sleep(1.0)
                    if self._purchase(deposit_amt):
                        self.core.invalidate_balance()
                        earn_balance_usdt_okx = earn_now + deposit_amt
                        earn_status_okx = f"DEPÓSITO {deposit_amt:.2f}"
                        self.db.log_caja_negra("OKX_EARN", "USDT",
                                                f"depósito {deposit_amt:.2f} → Earn")
                        return
                    else:
                        # Rollback transfer
                        self._transferir(deposit_amt, OKX_CUENTA_FUNDING, OKX_CUENTA_TRADING)

            # Rescate: free < target × 0.5
            if free < target * 0.5 and earn_now > 10.0:
                rescate = min(earn_now, target - free + 10)
                if self._redempt(rescate):
                    time.sleep(2.0)
                    funding = self._funding_balance()
                    if funding > 1.0:
                        self._transferir(min(funding, rescate), OKX_CUENTA_FUNDING, OKX_CUENTA_TRADING)
                        self.core.invalidate_balance()
                        earn_balance_usdt_okx = max(0.0, earn_now - rescate)
                        earn_status_okx = f"RESCATE {rescate:.2f}"
                        self.db.log_caja_negra("OKX_EARN", "USDT",
                                                f"rescate {rescate:.2f} → Trading")
                        return

            earn_status_okx = f"EQ target={target:.0f} free={free:.0f} earn={earn_now:.0f}"

    def run(self):
        time.sleep(60.0)  # espera RadarRegimen
        while not kill_switch.is_set():
            try:
                self._ciclo()
            except Exception as e:
                logger_errores.error(f"GestorTesoreria: {e}")
            for _ in range(int(self.CICLO_S)):
                if kill_switch.is_set():
                    return
                time.sleep(1.0)


class GestorInventario(threading.Thread):
    """OKX Earn cripto. V1 SOLO BTC y ETH (altcoins LIQUIDATE-ONLY no se parquean).
    AIMD confidence ∈ [0.30, 1.00]: +0.05 cada SUCCESS_STEP, /2 + freeze 24h en fallo.
    flush_to_spot(sym, qty) SÍNCRONO antes de cualquier SELL.
    INV_YIELD_UNSUPPORTED bajo inv_yield_lock (mitigación auditoría).
    Reconciliación self-healing cada 24h: adopta qty huérfana, barre Funding."""

    CICLO_S = 3600.0

    def __init__(self, core_okx, db: DatabaseManager):
        super().__init__(name="GestorInventario", daemon=True)
        self.core = core_okx
        self.db = db
        self.unsupported = set()  # ccys que OKX rechaza (cache RAM)
        self._cycles_since_reconcile = 0

    def _syms_objetivo(self):
        """V1.3.3: PARES_OKX_SPOT ∪ portfolio OKX activo (LIQUIDATE_ONLY).
        Si OKX_INV_INCLUYE_ALTS=false → solo PARES_OKX_SPOT (comportamiento V1.0).
        Asegura que las alts del portfolio sean considerados para Earn cripto."""
        if not CONFIG["okx_inv_incluye_alts"]:
            base = set(PARES_OKX_SPOT)
        else:
            try:
                activos = {sym for sym, *_ in self.db.get_active_portfolio("okx")}
            except Exception:
                activos = set()
            base = set(PARES_OKX_SPOT) | activos
        # El ladder gestiona su PROPIO inventario spot (compra/vende round-trip). El barrido a Earn
        # NO debe tocar sus ccy: un purchase fallido dejaba la cripto varada en funding y rompía el
        # sell (visto 2026-07-02, TRX). Excluir los pares del ladder.
        ladder_ccys = ladder_ccys_gestionados()
        return [s for s in base if s.split("/")[0] not in ladder_ccys]

    def _is_unsupported(self, ccy):
        with inv_yield_lock:
            return ccy in self.unsupported

    def _mark_unsupported(self, ccy):
        with inv_yield_lock:
            self.unsupported.add(ccy)

    def _confidence(self):
        with inv_yield_lock:
            return inv_yield_state["confidence"]

    def _bump_success(self):
        with inv_yield_lock:
            inv_yield_state["success_streak"] += 1
            inv_yield_state["flush_count_ok"] += 1
            if inv_yield_state["success_streak"] >= CONFIG["okx_inv_yield_succ_step"]:
                inv_yield_state["success_streak"] = 0
                inv_yield_state["confidence"] = min(
                    1.0, inv_yield_state["confidence"] + 0.05
                )

    def _bump_fail(self):
        with inv_yield_lock:
            inv_yield_state["flush_count_fail"] += 1
            inv_yield_state["confidence"] = max(0.30, inv_yield_state["confidence"] / 2.0)
            inv_yield_state["freeze_until"] = time.time() + CONFIG["okx_inv_yield_freeze"]
            inv_yield_state["success_streak"] = 0

    def _is_frozen(self):
        with inv_yield_lock:
            return time.time() < inv_yield_state["freeze_until"]

    # -------- API helpers — delegan al núcleo okx-semantica (ccy genérico, 8 dec) --------
    def _earn_balance_ccy(self, ccy):
        return okx_saldo_earn(self.core.exchange_trade, ccy)

    def _funding_balance_ccy(self, ccy):
        return okx_saldo_funding(self.core.exchange_trade, ccy)

    def _transfer(self, ccy, amt, src, dst):
        return okx_transfer(self.core.exchange_trade, ccy, amt, src, dst)

    def _purchase(self, ccy, amt):
        # on_unsupported: OKX rechaza ccy no elegible para Earn → cachea para no reintentar.
        return okx_earn_purchase(self.core.exchange_trade, ccy, amt, on_unsupported=self._mark_unsupported)

    def _redempt(self, ccy, amt):
        return okx_earn_redeem(self.core.exchange_trade, ccy, amt)

    # -------- Flush síncrono (llamado por OmniRiskOkx + execute_trade SELL) --------
    def flush_to_spot(self, sym, qty_needed):
        """Rescata qty_needed de Earn → Funding → Trading. Síncrono, timeout 60s."""
        ccy = sym.split("/")[0]
        row = self.db._exec(
            "SELECT qty FROM okx_inventory_earn WHERE ccy=?", (ccy,), fetch="one"
        )
        if not row or float(row[0] or 0) < 1e-9:
            return True  # nada parqueado, OK
        parqueado = float(row[0])
        rescatar = min(parqueado, qty_needed * 1.001)  # +0.1% buffer

        if not self._redempt(ccy, rescatar):
            self._bump_fail()
            return False

        timeout = CONFIG["okx_inv_yield_timeout"]
        t0 = time.time()
        funding = 0.0
        while (time.time() - t0) < timeout:
            time.sleep(2.0)
            funding = self._funding_balance_ccy(ccy)
            if funding >= rescatar * 0.99:
                break
        if funding < rescatar * 0.5:
            # Falló el redempt
            self._bump_fail()
            self.db._exec(
                "UPDATE okx_inventory_earn SET fail_count=fail_count+1 WHERE ccy=?", (ccy,)
            )
            return False

        if not self._transfer(ccy, funding, OKX_CUENTA_FUNDING, OKX_CUENTA_TRADING):
            self._bump_fail()
            return False

        # Actualiza inventario
        nueva_qty = max(0.0, parqueado - rescatar)
        self.db._exec(
            "UPDATE okx_inventory_earn SET qty=?, last_flush_ms=? WHERE ccy=?",
            (nueva_qty, time.time() * 1000, ccy)
        )
        self.core.invalidate_balance()
        self._bump_success()
        self.db.log_caja_negra("OKX_EARN", ccy,
                                f"flush_to_spot {rescatar:.6f} {ccy}")
        return True

    # -------- Reconciliación self-healing --------
    def _reconcile(self):
        """Cada 24h: sincroniza DB con Earn real + adopta qty huérfana + barre Funding.
        V1.3.3: itera PARES_OKX_SPOT ∪ portfolio activo (incluye alts en LIQUIDATE_ONLY)."""
        for sym in self._syms_objetivo():
            ccy = sym.split("/")[0]
            if self._is_unsupported(ccy):
                continue
            try:
                earn_real = self._earn_balance_ccy(ccy)
                row = self.db._exec(
                    "SELECT qty FROM okx_inventory_earn WHERE ccy=?", (ccy,), fetch="one"
                )
                db_qty = float(row[0]) if row else 0.0

                # Adopta qty huérfana
                if earn_real > db_qty + 1e-6:
                    if row:
                        self.db._exec(
                            "UPDATE okx_inventory_earn SET qty=? WHERE ccy=?",
                            (earn_real, ccy)
                        )
                    else:
                        self.db._exec(
                            "INSERT INTO okx_inventory_earn (ccy, symbol, qty, "
                            "deposited_at, last_flush_ms, fail_count) VALUES (?,?,?,?,?,?)",
                            (ccy, sym, earn_real, time.time(), 0.0, 0)
                        )
                    self.db.log_caja_negra("OKX_EARN", ccy,
                                            f"reconcile adoptó {earn_real:.6f} (DB tenía {db_qty:.6f})")
                elif earn_real < db_qty - 1e-6:
                    # Earn lo perdió o se rescató externamente
                    self.db._exec(
                        "UPDATE okx_inventory_earn SET qty=? WHERE ccy=?", (earn_real, ccy)
                    )

                # Barre Funding (cripto atrapada por reversas fallidas)
                funding = self._funding_balance_ccy(ccy)
                price = PRECIOS_RAM.get("okx", {}).get(sym, {}).get("bid", 0)
                if funding * price > 0.5:  # ignora dust < $0.50
                    self._transfer(ccy, funding, OKX_CUENTA_FUNDING, OKX_CUENTA_TRADING)
                    self.db.log_caja_negra("OKX_EARN", ccy,
                                            f"barrió Funding {funding:.6f} → Trading")
            except Exception as e:
                logger_errores.error(f"reconcile {ccy}: {e}")

    # -------- Ciclo principal --------
    def _ciclo(self):
        global earn_balance_cripto_okx, regime_evaluado
        if kill_switch.is_set():
            return
        if self._is_frozen():
            return
        if not regime_evaluado:
            return

        conf = self._confidence()
        with regime_lock:
            fc = factor_capital
            mm = modo_mercado

        # Fracción base por símbolo (de informe.txt)
        regime_mult = 0.4 + 0.6 * (1 - fc)  # ALCISTA 0.4 / BAJISTA pleno 0.82

        total_earn_value = 0.0
        # V1.3.3: incluye alts en LIQUIDATE_ONLY (parquea cripto idle → Earn cripto yield)
        for sym in self._syms_objetivo():
            ccy = sym.split("/")[0]
            if self._is_unsupported(ccy):
                continue
            try:
                portfolio = next(
                    (r for r in self.db.get_active_portfolio("okx") if r[0] == sym), None
                )
                if not portfolio:
                    continue
                _, avg, qty_total, inv, mode = portfolio
                tp_state = self.db.get_tp_state("okx", sym)
                has_harvest = self.db._exec(
                    "SELECT 1 FROM okx_grid_orders WHERE symbol=? AND side='SELL' LIMIT 1",
                    (sym,), fetch="one"
                )
                has_pending_sell = self.db._exec(
                    "SELECT 1 FROM okx_pending_sells WHERE symbol=? LIMIT 1",
                    (sym,), fetch="one"
                )

                # Base por posición
                if has_pending_sell:
                    base = 0.0
                elif tp_state["moonbag"] and not has_harvest:
                    base = 1.0
                elif not has_harvest:
                    base = 0.85  # 15% buffer para harvest sells
                else:
                    base = 0.0

                # cost_benefit: PARES_BASE_HOLD siempre = 1
                cost_benefit = 1.0

                qty_target_park = qty_total * base * cost_benefit * regime_mult * conf
                row = self.db._exec(
                    "SELECT qty FROM okx_inventory_earn WHERE ccy=?", (ccy,), fetch="one"
                )
                qty_parqueado = float(row[0]) if row else 0.0
                delta = qty_target_park - qty_parqueado
                price = PRECIOS_RAM.get("okx", {}).get(sym, {}).get("bid", 0)

                # Filtro USD: yield_14d > MIN_USD_CYCLE
                yield_14d = max(qty_target_park, qty_parqueado) * price * CONFIG["okx_savings_apr_ref"] * 14 / 365
                if yield_14d < CONFIG["okx_inv_yield_min_usd"]:
                    continue
                # Banda muerta $5
                if abs(delta * price) < 5.0:
                    total_earn_value += qty_parqueado * price
                    continue

                if delta > 0:
                    # Deposita
                    if self._transfer(ccy, delta, OKX_CUENTA_TRADING, OKX_CUENTA_FUNDING):
                        time.sleep(1.0)
                        if self._purchase(ccy, delta):
                            new_qty = qty_parqueado + delta
                            self.db._exec(
                                "INSERT INTO okx_inventory_earn (ccy, symbol, qty, "
                                "deposited_at, last_flush_ms, fail_count) VALUES (?,?,?,?,?,?) "
                                "ON CONFLICT(ccy) DO UPDATE SET qty=excluded.qty, "
                                "deposited_at=excluded.deposited_at",
                                (ccy, sym, new_qty, time.time(), 0.0, 0)
                            )
                            self._bump_success()
                            self.db.log_caja_negra("OKX_EARN", ccy,
                                                    f"depósito {delta:.6f} ({qty_target_park:.6f} target)")
                            total_earn_value += new_qty * price
                            continue
                        else:
                            self._transfer(ccy, delta, OKX_CUENTA_FUNDING, OKX_CUENTA_TRADING)
                            self._bump_fail()
                elif delta < 0 and qty_parqueado > 0:
                    # Redime el exceso
                    rescatar = min(qty_parqueado, abs(delta))
                    if self._redempt(ccy, rescatar):
                        time.sleep(2.0)
                        f = self._funding_balance_ccy(ccy)
                        if f > 0:
                            self._transfer(ccy, f, OKX_CUENTA_FUNDING, OKX_CUENTA_TRADING)
                        new_qty = max(0.0, qty_parqueado - rescatar)
                        self.db._exec(
                            "UPDATE okx_inventory_earn SET qty=?, last_flush_ms=? WHERE ccy=?",
                            (new_qty, time.time() * 1000, ccy)
                        )
                        self._bump_success()
                        total_earn_value += new_qty * price
                        continue  # V1.5.0 fix: evita double-count (línea sumaba qty pre-redeem)

                # Sin acción este ciclo (delta ~0, transfer falló, o nada parqueado)
                total_earn_value += qty_parqueado * price
            except Exception as e:
                logger_errores.error(f"GestorInventario {ccy}: {e}")

        earn_balance_cripto_okx = total_earn_value

    def run(self):
        time.sleep(75.0)
        # Reconcilia al arranque
        try:
            self._reconcile()
        except Exception as e:
            logger_errores.error(f"reconcile boot: {e}")
        while not kill_switch.is_set():
            try:
                self._ciclo()
                self._cycles_since_reconcile += 1
                if self._cycles_since_reconcile >= 24:
                    self._reconcile()
                    self._cycles_since_reconcile = 0
            except Exception as e:
                logger_errores.error(f"GestorInventario loop: {e}")
            for _ in range(int(self.CICLO_S)):
                if kill_switch.is_set():
                    return
                time.sleep(1.0)


# =============================================================================
# 13. GRACEFUL SHUTDOWN
# =============================================================================
_shutdown_requested = False
# Ref a TradingCore OKX para que graceful_shutdown pueda cancelar órdenes.
# Se setea en main() antes de arrancar los hilos.
core_okx_ref = None
# Ref a la pata bajista (EscuadronShortOkx). None si fut_enabled=false. Para HUD/KILL.
esc_short_ref = None


def graceful_shutdown(signum=None, frame=None):
    """V1.5.0 — apagado ordenado.
    Cancela órdenes abiertas (libera reservas y evita fills a precio stale durante downtime),
    preserva posiciones spot/perp (al reboot Centinela las reconcilia),
    cierra DB con WAL checkpoint."""
    global _shutdown_requested
    if _shutdown_requested:
        return
    _shutdown_requested = True
    console.print("\n[yellow]→ Apagado en curso. Cancelando órdenes abiertas...[/]")

    # 1. Cancelar grid_orders + pending_sells OKX
    try:
        if 'core_okx_ref' in globals() and core_okx_ref is not None:
            _cancelar_ordenes_venue(core_okx_ref, "okx",
                                     "okx_grid_orders", "okx_pending_sells")
    except Exception as e:
        logger_errores.error(f"shutdown OKX cancel: {e}")

    # 3. WAL checkpoint + cerrar DB
    try:
        if 'db' in globals():
            db.vacuum()
    except Exception:
        pass

    console.print("[green]✓ Apagado completado. Las posiciones quedan abiertas y se "
                  "reconcilian al próximo arranque.[/]")
    sys.exit(0)


def _cancelar_ordenes_venue(core, venue, tabla_grid, tabla_pending):
    """Helper shutdown: cancela órdenes abiertas y libera capital reservado.
    Best-effort: si falla cancelando alguna, sigue con las demás."""
    cancelled = 0
    failed = 0
    # Grid orders (BUY + SELL harvest)
    try:
        rows = db._exec(
            f"SELECT order_id, symbol, reserved_usdt, side FROM {tabla_grid}",
            fetch="all"
        ) or []
        for oid, sym, reserved, side in rows:
            try:
                core.exchange_trade.cancel_order(oid, sym)
                cancelled += 1
            except Exception:
                failed += 1
            if side == "BUY":
                try:
                    core.release_budget(sym, float(reserved or 0))
                except Exception:
                    pass
            db._exec(f"DELETE FROM {tabla_grid} WHERE order_id=?", (oid,))
    except Exception as e:
        logger_errores.error(f"shutdown {venue} grid: {e}")
    # Pending sells (TP market que aún no llenó)
    try:
        rows = db._exec(
            f"SELECT order_id, symbol FROM {tabla_pending}",
            fetch="all"
        ) or []
        for oid, sym in rows:
            try:
                core.exchange_trade.cancel_order(oid, sym)
                cancelled += 1
            except Exception:
                failed += 1
            db._exec(f"DELETE FROM {tabla_pending} WHERE order_id=?", (oid,))
    except Exception as e:
        logger_errores.error(f"shutdown {venue} pending: {e}")
    if cancelled or failed:
        msg = f"{cancelled} canceladas, {failed} fallaron (probable ya cerradas)"
        try:
            db.log_caja_negra(f"{venue.upper()}_SHUTDOWN", "ALL", msg)
        except Exception:
            pass
        console.print(f"[dim]  {venue}: {msg}[/]")


signal.signal(signal.SIGINT, graceful_shutdown)
try:
    signal.signal(signal.SIGTERM, graceful_shutdown)
except (AttributeError, ValueError):
    # SIGTERM no soportado en Windows main thread o ya registrado
    pass


# =============================================================================
# 14. MAIN
# =============================================================================
def main():
    if HUD_LOG_MODE:
        hud_info = f"HUD modo log (journalctl) — refresca cada {CONFIG['hud_log_interval_s']:.0f}s"
    else:
        hud_info = f"HUD modo TTY (rich layout) — refresca cada {CONFIG['hud_refresh_seconds']:.1f}s"
    banner(
        "Quantum Bot V1.7.0 (OKX-only + Pata Bajista)",
        [
            "Spot + Earn (OKX). Pata bajista shortea rallies en BAJISTA (si OKX_FUT_ENABLED).",
            "Binance solo data (régimen/ML). Consultá bot.txt para la spec completa.",
            hud_info,
        ],
        level="info"
    )

    # 1. Validar config (fast-fail)
    validar_config()

    # 2. DatabaseManager
    global db
    db = DatabaseManager()

    # V1.4.0: ML desactivado. Único consumidor era MartilloEliteFut (removido).
    # Se mantiene la carga por si V2 reintroduce un motor ML con SL configurable.
    _cargar_modelo()

    # 4. TradingCore OKX + fuente de datos Binance (solo lectura, para el régimen/ML)
    global core_okx_ref
    fuente_bin = FuenteBinance(db)   # solo exchange_read: OHLCV BTC/ETH + funding BTC
    core_okx = TradingCoreOkx(db)
    core_okx_ref = core_okx

    # 5. Sincronización de arranque (solo OKX; Binance es data-only)
    core_okx.sincronizar_okx_spot_arranque()

    # 6. Hilos COMPARTIDOS (cerebro de régimen lee datos de Binance vía fuente_bin)
    motor_ind = MotorIndicadores(fuente_bin, core_okx, db)
    radar_reg = RadarRegimen(fuente_bin, db)
    selector  = Selector(fuente_bin, db)
    kill_mon  = KillSwitchMonitor(db)
    hud       = RadarTelemetria(core_okx, db)

    motor_ind.start()
    radar_reg.start()
    selector.start()
    kill_mon.start()
    hud.start()

    # 7. Hilos OKX_SPOT + EARN (único venue de trading)
    # Tesoreria + Inventario se wirean en core_okx ANTES de arrancar para que
    # execute_trade y allocate_budget puedan usarlos (rescate_urgente, flush_to_spot).
    tesoreria = GestorTesoreria(core_okx, db)
    inventario = GestorInventario(core_okx, db)
    core_okx.tesoreria = tesoreria
    core_okx.inventario = inventario
    hilos_okx = [
        WebSocketFeedOkx(core_okx, db),
        OmniRiskOkx(core_okx, db),
        CentinelaAsincronoOkx(core_okx, db),
        EscudoDCAOkx(core_okx, db),
        EscuadronGridOkx(core_okx, db),
        tesoreria,
        inventario,
    ]

    for h in hilos_okx:
        h.start()

    # 8. Pata Bajista V1.7.0 — futuros direccional disciplinado (solo si fut_enabled).
    #    Exchange swap dedicado (defaultType='swap'); margen isolated + SL server-side.
    global esc_short_ref
    hilos_short = []
    if CONFIG["fut_enabled"]:
        ex_swap = _aplicar_antibot(ccxt.okx({
            "apiKey": os.getenv("OKX_API_KEY"),
            "secret": os.getenv("OKX_API_SECRET"),
            "password": os.getenv("OKX_PASSPHRASE"),
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        }))
        core_short = TradingCoreShortOkx(ex_swap)
        esc_short = EscuadronShortOkx(core_short, db, tesoreria)
        esc_short_ref = esc_short
        esc_short.start()
        hilos_short = [esc_short]
        modo_canary = " [CANARY: lote mínimo, verificá isolated+SL]" if CONFIG["short_canary_mode"] else ""
        console.print(f"[red]⚡ Pata bajista ACTIVA — futuros {CONFIG['short_leverage']}x "
                      f"isolated, SL {CONFIG['short_sl_pct']*100:.0f}%{modo_canary}[/]")

    # 9. Gestor de Salida SHADOW (Fase 1) — multiverso de MEDICIÓN, riesgo cero.
    #    Mide políticas de salida en paper aunque la Pata Bajista esté apagada.
    hilos_shadow = []
    if SHORT_SHADOW_ENABLED:
        shadow = GestorSalidaShadow(db)
        shadow.start()
        hilos_shadow = [shadow]
        console.print(f"[cyan]◎ Gestor de salida SHADOW activo — midiendo "
                      f"{len(POLITICAS_SALIDA)} políticas en paper (cero capital).[/]")

    # 9b. Ladder Grid SP3 (EscuadronLadderOkx) — sleeve spot round-trip aislado, canary. Default off.
    hilos_ladder = []
    esc_ladder = _build_ladder(core_okx, db)
    if esc_ladder is not None:
        esc_ladder.start()
        hilos_ladder = [esc_ladder]
        cy = " [CANARY]" if CONFIG["ladder_canary_mode"] else ""
        console.print(f"[green]▤ Ladder sleeve ACTIVO — {','.join(PARES_LADDER)} spot, "
                      f"cap ${CONFIG['ladder_max_capital']:.0f}{cy} — mide fill-real vs fill-ideal[/]")

    # 10. Entrenador periódico — re-entrena el ML (y opcional backfill shadow) vía subproceso.
    hilos_entrenar = []
    if ENTRENAR_AUTO_ENABLED:
        entrenador = EntrenadorPeriodico(db)
        entrenador.start()
        hilos_entrenar = [entrenador]
        bf = " + backfill shadow" if ENTRENAR_AUTO_BACKFILL else ""
        console.print(f"[cyan]◎ Entrenador periódico activo — cada {ENTRENAR_INTERVALO_H:.0f}h "
                      f"({ENTRENAR_DIAS}d){bf}.[/]")

    total = (5 + len(hilos_okx) + len(hilos_short) + len(hilos_shadow)
             + len(hilos_ladder) + len(hilos_entrenar))
    db.log_caja_negra("BOOT", "QUANTUM",
                       f"Quantum OKX-only arrancado con {total} hilos daemon activos")
    console.print(
        f"[green]✓ Bot operativo — {total} hilos daemon corriendo.[/]\n"
        f"[dim]  Ctrl+C para apagado ordenado. `touch KILL` para parada de emergencia.[/]"
    )

    # Main loop — espera kill o señal
    try:
        while not kill_switch.is_set():
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass

    graceful_shutdown()


if __name__ == "__main__":
    main()
