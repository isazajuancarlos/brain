"""V1.8.7 — sonda RANGO: ¿el grid cosecha bien la oscilación en RANGO, o el gate de entrada
(ADX<20 + cooldown) deja plata? Funciones PURAS, sin red.
contar_oscilaciones mide la oscilación cruda cosechable disponible (independiente del grid);
el barrido de gate (entry_adx × cooldown) lo cubre sim_grid_intrabar (ya testeado)."""
import entrenar


# =============================================================================
# contar_oscilaciones — up-swings no-solapados de ≥u% desde un valle local, solo en mask=True
# =============================================================================
def test_sawtooth_cuenta_swings():
    # 100 -> 105 -> 100 -> 105 -> 100 -> 105: TRES up-swings de ≥4% (cada 100->105)
    closes = [100, 105, 100, 105, 100, 105]
    mask = [True] * len(closes)
    out = entrenar.contar_oscilaciones(closes, mask, umbrales=(4.0,))
    assert out[4.0] == 3


def test_monotono_arriba_cuenta_no_solapado():
    # sube ≥2% por paso (margen claro, sin filo de float): cuenta un swing por paso
    closes = [100, 103, 106, 110, 113, 117]   # 5 pasos de ≥2%
    out = entrenar.contar_oscilaciones(closes, [True] * 6, umbrales=(2.0,))
    assert out[2.0] == 5


def test_plano_no_cuenta():
    out = entrenar.contar_oscilaciones([100] * 10, [True] * 10, umbrales=(2.0,))
    assert out[2.0] == 0


def test_solo_baja_no_cuenta():
    closes = [100, 98, 96, 94]
    out = entrenar.contar_oscilaciones(closes, [True] * 4, umbrales=(2.0,))
    assert out[2.0] == 0


def test_mask_false_no_cuenta():
    closes = [100, 105, 100, 105]
    out = entrenar.contar_oscilaciones(closes, [False] * 4, umbrales=(4.0,))
    assert out[4.0] == 0


def test_hueco_reinicia_el_valle():
    # valle a 100, hueco non-mask, luego 105: el swing NO debe contar a través del hueco
    closes = [100, 99, 200, 105]
    mask =   [True, False, False, True]
    out = entrenar.contar_oscilaciones(closes, mask, umbrales=(4.0,))
    assert out[4.0] == 0          # el 105 arranca valle nuevo en su segmento, no sube ≥4% desde ahí


def test_valle_se_actualiza_a_la_baja():
    # 100 -> 95 (valle baja) -> 100: +5.3% desde 95 → cuenta con umbral 5
    closes = [100, 95, 100]
    out = entrenar.contar_oscilaciones(closes, [True] * 3, umbrales=(5.0,))
    assert out[5.0] == 1


def test_multiumbral_independiente():
    # un swing de +5%: cuenta para ≥2% y ≥4% pero no para ≥7%
    closes = [100, 105]
    out = entrenar.contar_oscilaciones(closes, [True, True], umbrales=(2.0, 4.0, 7.0))
    assert out[2.0] == 1 and out[4.0] == 1 and out[7.0] == 0


# =============================================================================
# gate sweep — sim_grid_intrabar con entry_adx/cooldown más sueltos captura ≥ entradas en RANGO
# (invariante de monotonía: aflojar el gate no REDUCE las entradas)
# =============================================================================
def _bar(close, adx=10, ema200=None):
    return {"close": close, "high": close, "low": close, "rsi": 50, "adx": adx,
            "ema200": ema200 if ema200 is not None else close * 0.9, "atr_pct": 2.0, "green": True}


def test_gate_mas_suelto_no_reduce_entradas():
    # barras con ADX=22 (sobre el real 20): el gate real no entra, uno suelto (adx 999) sí
    bars = [_bar(100, adx=22) for _ in range(10)]
    oper = [True] * 10
    real = entrenar.sim_grid_intrabar(bars, oper, entry_adx=20.0, cooldown_bars=4)
    suelto = entrenar.sim_grid_intrabar(bars, oper, entry_adx=999.0, cooldown_bars=1)
    assert real["n_entry"] == 0
    assert suelto["n_entry"] >= real["n_entry"]
    assert suelto["n_entry"] >= 1
