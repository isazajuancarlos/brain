"""OmniRiskOkx: guard de dust + exclusión de ccy del ladder. Evita el loop de errores
(2026-07-04) donde OmniRisk intentaba vender cada 1s un dust invendible de TRX
(qty 8.5e-7 < mínimo 1e-6) residuo del round-trip del ladder → martilleo a la API OKX.
Patrón puro + integración de test_short_okx.py / test_ladder_okx.py."""
import time
import main


# ---------- función pura: omnirisk_debe_gestionar ----------

def test_gestiona_posicion_normal():
    assert main.omnirisk_debe_gestionar("BTC/USDT", 0.01, 1e-6, {"TRX"}) is True


def test_no_gestiona_dust_bajo_minimo():
    # el caso real 2026-07-04: 8.508e-7 TRX < mínimo vendible 1e-6
    assert main.omnirisk_debe_gestionar("TRX/USDT", 8.508e-7, 1e-6, set()) is False


def test_no_gestiona_ccy_del_ladder_aunque_qty_normal():
    # el ladder gestiona su propio inventario; el core no debe tocar TRX
    assert main.omnirisk_debe_gestionar("TRX/USDT", 100.0, 1e-6, {"TRX"}) is False


def test_qty_igual_al_minimo_si_es_gestionable():
    # boundary: qty == min NO es dust (solo qty < min)
    assert main.omnirisk_debe_gestionar("XRP/USDT", 1e-6, 1e-6, set()) is True


# ---------- integración: OmniRiskOkx._gestionar no llama execute_trade ----------

class _FakeDB:
    def __init__(self, portfolio=()):
        self._portfolio = list(portfolio)

    def get_tp_state(self, venue, sym):
        return {"armed": False, "tier": 0, "top": 0.0, "floor_pct": 0.0,
                "moonbag": False, "partial": False}

    def save_tp_state(self, *a, **k):
        pass

    def get_active_portfolio(self, venue):
        return self._portfolio


class _RecCore:
    """core_okx mínimo que registra las llamadas a execute_trade."""
    def __init__(self, min_amt=1e-6):
        self.calls = []
        self.exchange_trade = type("Ex", (), {
            "markets": {"TRX/USDT": {"limits": {"amount": {"min": min_amt}}}}
        })()

    def execute_trade(self, sym, side, qty, strategy="", reason=""):
        self.calls.append((sym, side, qty, strategy))
        return None


def _omni(monkeypatch, ladder_pares=("TRX/USDT",), enabled=True, portfolio=()):
    monkeypatch.setattr(main, "PARES_LADDER", list(ladder_pares))
    monkeypatch.setitem(main.CONFIG, "ladder_enabled", enabled)
    # Precio fresco y bid enorme → pnl_pct >> okx_moonbag_trigger (4%) → SIN el guard,
    # el camino MOONBAG llama execute_trade. Así el test falla RED sin el fix (no es vacuo).
    monkeypatch.setitem(main.PRECIOS_RAM, "okx",
                        {"TRX/USDT": {"bid": 10.0, "ask": 10.0, "local_ts": time.time()}})
    core = _RecCore()
    return main.OmniRiskOkx(core, _FakeDB(portfolio)), core


def test_gestionar_dust_no_llama_execute_trade(monkeypatch):
    omni, core = _omni(monkeypatch, ladder_pares=())  # sin exclusión ladder → aísla el guard de dust
    # inv>0 y pnl enorme: sin el guard, MOONBAG venderia el dust
    omni._gestionar("TRX/USDT", 0.31699, 8.508e-7, 2.7e-7, "ACTIVE")
    assert core.calls == []


def test_gestionar_ccy_ladder_no_llama_execute_trade(monkeypatch):
    omni, core = _omni(monkeypatch, ladder_pares=("TRX/USDT",))
    # qty grande y vendible, pero TRX es ccy del ladder → el core no lo gestiona
    omni._gestionar("TRX/USDT", 0.31699, 100.0, 30.0, "ACTIVE")
    assert core.calls == []


# =============================================================================
# V1.9.16 — "dueño del ccy" se decide por LADDER_ENABLED, no solo por LADDER_PARES.
#
# Dos agujeros con la MISMA causa: `ladder_ccys` se calculaba de PARES_LADDER sin
# preguntar si el ladder existe. Y PARES_LADDER se puebla del env SIEMPRE (default
# LTC/USDT), mientras que _build_ladder devuelve None con LADDER_ENABLED=false.
#
#  A) ladder APAGADO + LTC heredado → OmniRisk se inhibía (sin stops ni salidas),
#     GestorInventario lo excluía de Earn, y no había hilo de ladder que lo tomara:
#     posición HUÉRFANA sin dueño, en la configuración por DEFECTO.
#  B) ladder ENCENDIDO → _kill_close_all iteraba el portfolio SIN el guard (única
#     ruta de venta del core que no lo consultaba). Lo que la frenaba no era una
#     regla sino un accidente: el `is_price_fresh` de execute_trade fallaba porque
#     el par del ladder no estaba en el feed. Al suscribirlo (simbolos_feed_okx,
#     V1.9.15) el accidente desapareció y la venta procede: doble liquidación
#     contra cerrar_todo_kill, cost basis inventado en okx_realized_pnl y fila
#     fantasma en ladder_positions → el bucle 51008 que V1.9.8/9.9 costó dos rondas.
# =============================================================================

def test_ladder_ccys_vacio_si_el_ladder_esta_apagado(monkeypatch):
    monkeypatch.setattr(main, "PARES_LADDER", ["LTC/USDT"])
    monkeypatch.setitem(main.CONFIG, "ladder_enabled", False)
    assert main.ladder_ccys_gestionados() == set()


def test_ladder_ccys_pobla_si_el_ladder_esta_encendido(monkeypatch):
    monkeypatch.setattr(main, "PARES_LADDER", ["XRP/USDT", "LTC/USDT"])
    monkeypatch.setitem(main.CONFIG, "ladder_enabled", True)
    assert main.ladder_ccys_gestionados() == {"XRP", "LTC"}


def test_omnirisk_SI_gestiona_el_ccy_si_el_ladder_esta_apagado(monkeypatch):
    """(A) El agujero de la huérfana. Con el ladder apagado nadie más la gestiona,
    así que el core DEBE hacerlo: qty vendible y pnl enorme → MOONBAG vende."""
    omni, core = _omni(monkeypatch, ladder_pares=("TRX/USDT",), enabled=False)
    omni._gestionar("TRX/USDT", 0.31699, 100.0, 30.0, "ACTIVE")
    assert core.calls != []          # discrimina contra el test de arriba (ladder ON → [])


def test_kill_no_vende_el_inventario_del_ladder(monkeypatch):
    """(B) En KILL el ladder cierra lo suyo por cerrar_todo_kill; el core no debe
    duplicar la venta sobre una fila adoptada al portfolio con cost basis inventado."""
    omni, core = _omni(monkeypatch, ladder_pares=("TRX/USDT",), enabled=True,
                       portfolio=[("TRX/USDT", 0.31699, 100.0, 30.0, "LIQUIDATE_ONLY"),
                                  ("BTC/USDT", 60000.0, 0.01, 600.0, "ACTIVE")])
    omni._kill_close_all()
    vendidos = [c[0] for c in core.calls]
    assert "TRX/USDT" not in vendidos     # del ladder → lo cierra el ladder
    assert "BTC/USDT" in vendidos         # el resto del core SÍ se liquida


def test_kill_SI_vende_el_ccy_si_el_ladder_esta_apagado(monkeypatch):
    """Discriminación: sin hilo de ladder, excluirlo dejaría capital vivo tras un
    KILL. El guard no puede inhibir la liquidación de lo que nadie más cierra."""
    omni, core = _omni(monkeypatch, ladder_pares=("TRX/USDT",), enabled=False,
                       portfolio=[("TRX/USDT", 0.31699, 100.0, 30.0, "LIQUIDATE_ONLY")])
    omni._kill_close_all()
    assert [c[0] for c in core.calls] == ["TRX/USDT"]


def test_kill_no_martillea_con_dust_invendible(monkeypatch):
    """El guard trae gratis el filtro de dust: 8.5e-7 TRX < mínimo → market-sell
    rechazado con 51008 en cada pasada de KILL (cada 30s). No se intenta."""
    omni, core = _omni(monkeypatch, ladder_pares=(), enabled=False,
                       portfolio=[("TRX/USDT", 0.31699, 8.508e-7, 2.7e-7, "ACTIVE")])
    omni._kill_close_all()
    assert core.calls == []
