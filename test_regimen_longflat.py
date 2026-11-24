"""SP1 — long/flat por régimen (prepararse para el cambio de régimen). Funciones PURAS, sin red.
sim_longflat = holdear si el régimen de ayer es long, si no earn (causal). _onset_captura = qué pasa
cuando el alcista llega (lag de entrada + % del up-leg capturado)."""
import entrenar


def test_longflat_todo_long_sigue_precio():
    closes = [100, 110, 121]                      # +10%, +10%
    r = entrenar.sim_longflat(closes, ["TENDENCIA_UP"] * 3, {"TENDENCIA_UP"}, fee=0.0, earn_daily=0.0)
    assert abs(r["net_pct"] - 21.0) < 1e-6        # 1.1*1.1 = 1.21
    assert r["fin_cripto"] is True


def test_longflat_todo_flat_rinde_earn():
    closes = [100, 110, 121]
    r = entrenar.sim_longflat(closes, ["TENDENCIA_DOWN"] * 3, {"TENDENCIA_UP"}, fee=0.0, earn_daily=0.01)
    assert abs(r["net_pct"] - (1.01 ** 2 - 1) * 100) < 1e-6
    assert r["fin_cripto"] is False and r["n_switches"] == 0


def test_longflat_switch_paga_fee():
    # flat (day0 DOWN) → long (day1 UP, switch) → flat (day2 DOWN, switch) → 2 fees con precio plano
    closes = [100, 100, 100, 100]
    states = ["TENDENCIA_DOWN", "TENDENCIA_UP", "TENDENCIA_DOWN", "TENDENCIA_DOWN"]
    r = entrenar.sim_longflat(closes, states, {"TENDENCIA_UP"}, fee=0.01, earn_daily=0.0)
    assert r["n_switches"] == 2 and r["net_pct"] < 0      # entró y salió, dos fees, precio plano


def test_longflat_no_lookahead():
    # el precio salta +100% en day1 pero el régimen de AYER (day0) era flat → no lo captura
    r = entrenar.sim_longflat([100, 200], ["TENDENCIA_DOWN", "TENDENCIA_UP"], {"TENDENCIA_UP"},
                              fee=0.0, earn_daily=0.0)
    assert abs(r["net_pct"] - 0.0) < 1e-6


def test_longflat_max_drawdown():
    r = entrenar.sim_longflat([100, 80, 100], ["TENDENCIA_UP"] * 3, {"TENDENCIA_UP"},
                              fee=0.0, earn_daily=0.0)
    assert r["max_dd"] >= 19.9                            # cayó ~20% en el camino estando long


def test_onset_detecta_entrada_tardia_y_captura():
    # baja a 90 (piso día 2), régimen flat durante la caída, long desde el día 3 → entra DESPUÉS del piso
    closes = [100, 95, 90, 95, 110, 120]
    states = ["TENDENCIA_DOWN", "TENDENCIA_DOWN", "TENDENCIA_DOWN", "TENDENCIA_UP", "TENDENCIA_UP", "TENDENCIA_UP"]
    on = entrenar._onset_captura(closes, states, {"TENDENCIA_UP"}, lookback=5)
    assert len(on) >= 1
    assert on[0]["lag"] >= 1                              # entró tarde (después del piso)
    assert on[0]["captured_pct"] > 0                      # agarró parte de la subida
