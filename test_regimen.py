"""Tests del Cerebro de Régimen 4-estados (V1.8.0) — funciones puras.
clasificar_regimen / map_confianza / regimen_factor_estado no tocan red ni estado global."""
import main


# =============================================================================
# clasificar_regimen — taxonomía de 4 estados (orden de prioridad)
# =============================================================================
def test_uptrend_fuerte_es_tendencia_up():
    est, _ = main.clasificar_regimen(dict(desv=0.10, slope=0.02, adx=30, atr=0.03))
    assert est == "TENDENCIA_UP"


def test_rango_adx_bajo_cerca_de_media():
    est, _ = main.clasificar_regimen(dict(desv=0.01, slope=0.0, adx=12, atr=0.02))
    assert est == "RANGO"


def test_downtrend_es_tendencia_down():
    est, _ = main.clasificar_regimen(dict(desv=-0.10, slope=-0.02, adx=30, atr=0.03))
    assert est == "TENDENCIA_DOWN"


def test_vol_alta_es_caos_aunque_haya_tendencia():
    # CAOS tiene prioridad: vol manda incluso sobre un uptrend con ADX fuerte
    est, _ = main.clasificar_regimen(dict(desv=0.10, slope=0.02, adx=30, atr=0.08))
    assert est == "CAOS"


def test_caos_gana_a_downtrend():
    est, _ = main.clasificar_regimen(dict(desv=-0.10, slope=-0.02, adx=30, atr=0.08))
    assert est == "CAOS"


def test_uptrend_debil_sin_adx_no_es_tendencia_up():
    # desv>bull y slope>0 pero ADX flojo → NO es TENDENCIA_UP (falta fuerza direccional)
    est, _ = main.clasificar_regimen(dict(desv=0.03, slope=0.01, adx=15, atr=0.03))
    assert est != "TENDENCIA_UP"


def test_ambiguo_cae_en_rango_por_fallback():
    # ni caos, ni down (desv no < -0.05), ni up (slope<0), ni rango positivo (adx≥20) → fallback RANGO
    est, _ = main.clasificar_regimen(dict(desv=0.03, slope=-0.001, adx=22, atr=0.03))
    assert est == "RANGO"


def test_estados_validos():
    for inp in [dict(desv=0.1, slope=0.02, adx=30, atr=0.03),
                dict(desv=0.0, slope=0.0, adx=10, atr=0.02),
                dict(desv=-0.1, slope=-0.02, adx=30, atr=0.03),
                dict(desv=0.1, slope=0.02, adx=30, atr=0.08)]:
        est, _ = main.clasificar_regimen(inp)
        assert est in main.REGIMENES_VALIDOS


# =============================================================================
# map_confianza — confianza [0,1] → multiplicador de sizing acotado
# =============================================================================
def test_map_confianza_bordes():
    assert abs(main.map_confianza(0.0) - CONFIG_MIN()) < 1e-9
    assert abs(main.map_confianza(1.0) - CONFIG_MAX()) < 1e-9


def test_map_confianza_monotona_y_clamp():
    assert main.map_confianza(0.3) < main.map_confianza(0.7)
    assert main.map_confianza(-5.0) == CONFIG_MIN()   # clamp inferior
    assert main.map_confianza(5.0) == CONFIG_MAX()    # clamp superior


def CONFIG_MIN():
    return main.CONFIG["regimen_conf_min_mult"]


def CONFIG_MAX():
    return main.CONFIG["regimen_conf_max_mult"]


# =============================================================================
# regimen_factor_estado — playbook (RANGO ≥ TENDENCIA_UP, congela el resto)
# =============================================================================
def test_factor_rango_pleno_y_mayor_o_igual_que_up():
    assert main.regimen_factor_estado("RANGO") >= main.regimen_factor_estado("TENDENCIA_UP")
    assert main.regimen_factor_estado("RANGO") > 0


def test_factor_congela_down_y_caos():
    assert main.regimen_factor_estado("TENDENCIA_DOWN") == 0.0
    assert main.regimen_factor_estado("CAOS") == 0.0


def test_regimen_opera_solo_rango_y_up():
    assert main.regimen_opera("RANGO") is True
    assert main.regimen_opera("TENDENCIA_UP") is True
    assert main.regimen_opera("TENDENCIA_DOWN") is False
    assert main.regimen_opera("CAOS") is False


# =============================================================================
# grid_regimen_decision — el cable: (mode, modo_mercado, estado, conf) → (opera, size_mult)
# shadow = comportamiento idéntico al actual (gate por modo_mercado, sin sizing de régimen)
# live = gate por estado + sizing factor_estado × map_confianza
# =============================================================================
def test_shadow_replica_comportamiento_actual():
    # shadow: opera si NO es BAJISTA, sizing de régimen neutro (1.0) — cero cambio
    opera, mult = main.grid_regimen_decision("shadow", "ALCISTA", "CAOS", 0.9)
    assert opera is True and mult == 1.0
    opera, mult = main.grid_regimen_decision("shadow", "BAJISTA", "RANGO", 0.9)
    assert opera is False and mult == 1.0


def test_live_rango_opera_sizing_pleno():
    opera, mult = main.grid_regimen_decision("live", "ALCISTA", "RANGO", 1.0)
    assert opera is True
    assert abs(mult - (1.0 * main.CONFIG["regimen_conf_max_mult"])) < 1e-9


def test_live_tend_up_opera_sizing_menor():
    opera, mult = main.grid_regimen_decision("live", "ALCISTA", "TENDENCIA_UP", 0.0)
    assert opera is True
    assert abs(mult - (main.CONFIG["regimen_factor_tend_up"] * main.CONFIG["regimen_conf_min_mult"])) < 1e-9


def test_live_congela_down_y_caos():
    assert main.grid_regimen_decision("live", "ALCISTA", "TENDENCIA_DOWN", 0.9)[0] is False
    assert main.grid_regimen_decision("live", "ALCISTA", "CAOS", 0.9)[0] is False


def test_live_ignora_modo_mercado_binario():
    # en live manda el 4-estados, no el binario: RANGO opera aunque modo_mercado sea BAJISTA
    opera, _ = main.grid_regimen_decision("live", "BAJISTA", "RANGO", 0.5)
    assert opera is True


def test_warmup_no_opera_antes_de_evaluar():
    # V1.8.6: sin clasificar (evaluado=False) NO opera, ni en live ni en shadow, aunque el
    # default sea operable (RANGO/ALCISTA). Cierra la ventana de boot.
    assert main.grid_regimen_decision("live", "ALCISTA", "RANGO", 0.9, evaluado=False) == (False, 0.0)
    assert main.grid_regimen_decision("shadow", "ALCISTA", "RANGO", 0.9, evaluado=False) == (False, 0.0)


def test_evaluado_true_no_cambia_comportamiento():
    # con evaluado=True (default) el cable se comporta igual que antes
    assert main.grid_regimen_decision("live", "ALCISTA", "RANGO", 0.5, evaluado=True)[0] is True
    assert main.grid_regimen_decision("live", "ALCISTA", "RANGO", 0.5)[0] is True  # default


# =============================================================================
# _publicar_regimen — boot inmediato + histéresis por confirmación sostenida
# =============================================================================
class _FakeDB:
    def log_caja_negra(self, *a, **k):
        pass


def test_publicar_regimen_boot_inmediato_y_histeresis():
    # reset de globals (este test los manipula a propósito)
    main.regimen_evaluado = False
    main.regimen_mercado = "RANGO"
    sel = main.Selector(core_bin=None, db=_FakeDB())
    rango = dict(desv=0.0, slope=0.0, adx=10, atr=0.02, ratio_slope=0.0, funding=0.0)
    caos = dict(desv=0.0, slope=0.0, adx=10, atr=0.09, ratio_slope=0.0, funding=0.0)

    sel._publicar_regimen(rango)                 # boot → publica inmediato
    assert main.regimen_mercado == "RANGO" and main.regimen_evaluado is True

    for _ in range(main.REGIMEN_HYST_TICKS - 1):  # cambio requiere N confirmaciones sostenidas
        sel._publicar_regimen(caos)
        assert main.regimen_mercado == "RANGO"    # todavía no commitea
    sel._publicar_regimen(caos)
    assert main.regimen_mercado == "CAOS"         # confirmado → commitea


def test_publicar_regimen_cambio_intermitente_no_commitea():
    main.regimen_evaluado = False
    main.regimen_mercado = "RANGO"
    sel = main.Selector(core_bin=None, db=_FakeDB())
    rango = dict(desv=0.0, slope=0.0, adx=10, atr=0.02, ratio_slope=0.0, funding=0.0)
    caos = dict(desv=0.0, slope=0.0, adx=10, atr=0.09, ratio_slope=0.0, funding=0.0)
    sel._publicar_regimen(rango)                  # boot RANGO
    sel._publicar_regimen(caos)                   # candidato CAOS (1)
    sel._publicar_regimen(rango)                  # vuelve a RANGO → resetea el candidato
    sel._publicar_regimen(caos)                   # candidato CAOS otra vez (1, no 2)
    assert main.regimen_mercado == "RANGO"        # el flapping no graduó


def test_histeresis_no_deja_sostenido_desde_none_tras_match():
    """Regresión (V1.9.11, cazado por mypy): _aplicar_histeresis reseteaba _sostenido_desde=None
    en la rama actual==propuesto SIN resetear _sostenido_modo. Una oscilación
    propuesto→actual→propuesto dejaba _sostenido_modo stale → el `!=` de 2420 se saltaba la
    re-inicialización → `ahora - self._sostenido_desde` = float - None → TypeError (atrapado por el
    try del run(), pero saltaba histéresis + _publicar_regimen ese tick)."""
    main.selector_evaluado = True
    main.modo_operacion = "SPOT"
    main.modo_confianza = 0.5
    main.modo_razon = ""
    sel = main.Selector(core_bin=None, db=_FakeDB())
    sel._aplicar_histeresis("FLAT", "regla", 0.5)   # A: sostiene FLAT sin commitear (_modo=FLAT,_desde=t)
    sel._aplicar_histeresis("SPOT", "regla", 0.5)   # B: actual==propuesto → resetea _desde (antes dejaba _modo=FLAT)
    sel._aplicar_histeresis("FLAT", "regla", 0.5)   # C: FLAT==_modo stale → antes del fix: TypeError float-None
    assert sel._sostenido_desde is not None          # se re-inicializó bien, no quedó en None
