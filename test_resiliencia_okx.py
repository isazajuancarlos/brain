"""Tests de la capa de resiliencia anti-bot/Cloudflare (V1.8.2).

Cubren okx_retry (backoff exponencial + jitter, reintento selectivo) y
_es_error_antibot (clasificación reintetable vs no). Sin red, sin capital:
fn inyectada + _sleep/_rand inyectados → deterministas y sin dormir de verdad.
"""
import ccxt
import pytest

import main


# --------------------------------------------------------------------------
# _es_error_antibot — clasifica qué excepción es reintetable
# --------------------------------------------------------------------------
def test_antibot_detecta_403_por_mensaje():
    assert main._es_error_antibot(ccxt.ExchangeError("okx GET ... 403 Forbidden"))
    assert main._es_error_antibot(Exception("Just a moment... Cloudflare challenge"))
    assert main._es_error_antibot(Exception("Too Many Requests 429"))


def test_antibot_detecta_por_tipo_ccxt():
    assert main._es_error_antibot(ccxt.DDoSProtection("x"))
    assert main._es_error_antibot(ccxt.RequestTimeout("timeout"))
    assert main._es_error_antibot(ccxt.ExchangeNotAvailable("down"))


def test_antibot_ignora_errores_reales():
    # auth/lógica NO son reintetables: reintentar a lo bobo solo gasta y enmascara
    assert not main._es_error_antibot(ccxt.AuthenticationError("invalid api key"))
    assert not main._es_error_antibot(ValueError("bug de logica"))
    assert not main._es_error_antibot(ccxt.InsufficientFunds("no balance"))


# --------------------------------------------------------------------------
# okx_retry — backoff + reintento selectivo
# --------------------------------------------------------------------------
def test_exito_primer_intento_no_duerme():
    n = {"c": 0}
    def fn():
        n["c"] += 1
        return "ok"
    dormidas = []
    r = main.okx_retry(fn, _sleep=dormidas.append, _rand=lambda: 0.0)
    assert r == "ok"
    assert n["c"] == 1
    assert dormidas == []


def test_reintenta_ante_403_y_se_recupera():
    n = {"c": 0}
    def fn():
        n["c"] += 1
        if n["c"] < 3:
            raise ccxt.ExchangeError("okx GET /account/balance 403 Forbidden Cloudflare")
        return "recuperado"
    dormidas = []
    r = main.okx_retry(fn, intentos=4, base=1.0, max_s=8.0,
                       _sleep=dormidas.append, _rand=lambda: 0.0)
    assert r == "recuperado"
    assert n["c"] == 3
    assert len(dormidas) == 2  # durmió antes del 2º y 3er intento


def test_pasa_args_y_kwargs():
    def fn(a, b, c=0):
        return a + b + c
    assert main.okx_retry(fn, 1, 2, c=3, _sleep=lambda s: None, _rand=lambda: 0.0) == 6


def test_error_no_antibot_se_relanza_sin_reintentar():
    n = {"c": 0}
    def fn():
        n["c"] += 1
        raise ccxt.AuthenticationError("invalid api key")
    dormidas = []
    with pytest.raises(ccxt.AuthenticationError):
        main.okx_retry(fn, intentos=4, _sleep=dormidas.append, _rand=lambda: 0.0)
    assert n["c"] == 1
    assert dormidas == []


def test_agota_intentos_relanza_ultima():
    def fn():
        raise ccxt.DDoSProtection("403 cloudflare")
    dormidas = []
    with pytest.raises(ccxt.DDoSProtection):
        main.okx_retry(fn, intentos=3, base=1.0, max_s=8.0,
                       _sleep=dormidas.append, _rand=lambda: 0.0)
    assert len(dormidas) == 2  # intentos-1 dormidas, luego re-lanza


def test_backoff_exponencial_acotado_por_max_s():
    def fn():
        raise ccxt.DDoSProtection("403")
    dormidas = []
    with pytest.raises(ccxt.DDoSProtection):
        main.okx_retry(fn, intentos=5, base=1.0, max_s=3.0,
                       _sleep=dormidas.append, _rand=lambda: 0.0)
    # min(max_s, base*2^i): 1, 2, 3(cap), 3(cap)
    assert dormidas == [1.0, 2.0, 3.0, 3.0]


def test_jitter_se_suma_a_la_espera():
    def fn():
        raise ccxt.DDoSProtection("403")
    dormidas = []
    with pytest.raises(ccxt.DDoSProtection):
        main.okx_retry(fn, intentos=2, base=2.0, max_s=100.0,
                       _sleep=dormidas.append, _rand=lambda: 0.5)
    # i=0: min(100, 2*1)=2 + 0.5*2 = 3.0
    assert dormidas == [3.0]
