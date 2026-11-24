"""Tests del universo de entrada OKX configurable (Subsistema C — 2026-06-15).

Cubre la construcción del universo de pares spot (PARES_OKX_SPOT / PARES_BASE_HOLD)
desde el env y su validación de formato. NO toca red ni dinero real.

El universo de entrada del Grid (nuevas compras) sale de PARES_OKX_SPOT; ese mismo set
alimenta datos/indicadores, suscripciones del feed y el HUD. Agregar un par ahí lo activa
de punta a punta. BTC/ETH (core) SIEMPRE están presentes aunque el env los omita.
"""
import importlib
import os

import main as _main_mod


def _reload(env):
    # Aísla del .env real: main.py llama load_dotenv() en el reload y repoblaría
    # OKX_PARES_SPOT desde el archivo (que puede tener pares extra como TRX). Sembrar
    # la clave presente-pero-vacía hace que load_dotenv(override=False) NO la pise →
    # el test prueba la lógica de default sin depender del .env del desarrollador.
    os.environ["OKX_PARES_SPOT"] = str(env.get("OKX_PARES_SPOT", ""))
    for k, v in env.items():
        os.environ[k] = str(v)
    importlib.reload(_main_mod)
    return _main_mod


def test_universo_default_btc_eth():
    # sin env → comportamiento idéntico al previo: solo BTC/ETH
    m = _reload({})
    assert m.PARES_OKX_SPOT == ["BTC/USDT", "ETH/USDT"]
    assert m.PARES_BASE_HOLD == {"BTC/USDT", "ETH/USDT"}


def test_universo_agrega_par_extra():
    # opt-in: SOL entra al universo de entrada Y como base-hold ACTIVE (no LIQUIDATE_ONLY)
    m = _reload({"OKX_PARES_SPOT": "BTC/USDT,ETH/USDT,SOL/USDT"})
    assert "SOL/USDT" in m.PARES_OKX_SPOT
    assert "SOL/USDT" in m.PARES_BASE_HOLD


def test_universo_core_siempre_presente():
    # aunque el env omita BTC/ETH, el core nunca se cae (footgun guard)
    m = _reload({"OKX_PARES_SPOT": "SOL/USDT"})
    assert "BTC/USDT" in m.PARES_OKX_SPOT
    assert "ETH/USDT" in m.PARES_OKX_SPOT
    assert "SOL/USDT" in m.PARES_OKX_SPOT


def test_universo_dedup_y_normaliza():
    # duplicados y minúsculas → set limpio, sin BTC repetido
    m = _reload({"OKX_PARES_SPOT": "btc/usdt, SOL/USDT , sol/usdt"})
    assert m.PARES_OKX_SPOT.count("BTC/USDT") == 1
    assert m.PARES_OKX_SPOT.count("SOL/USDT") == 1


def test_validar_pares_acepta_formato_correcto():
    m = _reload({})
    assert m._validar_pares_okx(["BTC/USDT", "SOL/USDT"]) == []


def test_validar_pares_rechaza_malformado():
    m = _reload({})
    assert m._validar_pares_okx(["SOLUSDT"]) != []        # sin slash
    assert m._validar_pares_okx(["SOL/USDC"]) != []        # quote != USDT
    assert m._validar_pares_okx(["/USDT"]) != []           # base vacía
