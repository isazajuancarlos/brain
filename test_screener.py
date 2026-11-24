"""Tests del screener de pares — funciones puras (métricas + grid sim + score).
La parte de red (descubrir_pares / mining) es glue, no se testea acá."""
import screener_pares as s


def test_retornos():
    assert s.retornos([100, 110, 99]) == [0.1, -0.1]
    assert s.retornos([100]) == []


def test_corr_a_btc_identica_y_opuesta():
    a = [0.01, -0.02, 0.03, -0.01]
    assert abs(s.corr_a_btc(a, a) - 1.0) < 1e-9
    assert abs(s.corr_a_btc(a, [-x for x in a]) + 1.0) < 1e-9


def test_volatilidad_anual_cero_si_constante():
    assert s.volatilidad_anual([0.0, 0.0, 0.0]) == 0.0


def test_max_drawdown():
    assert abs(s.max_drawdown([100, 120, 60, 90]) - (-50.0)) < 1e-6   # 120→60 = -50%
    assert s.max_drawdown([100, 110, 120]) == 0.0                     # solo sube → 0


def test_grid_sim_cosecha_en_oscilacion():
    real, unreal, n = s.grid_sim([100, 95, 100], dip=0.05, tp=0.05)
    assert n == 1 and abs(real - 5.0) < 1e-6 and unreal == 0.0


def test_grid_sim_alza_monotona_no_compra():
    real, unreal, n = s.grid_sim([100, 110, 120], dip=0.05, tp=0.05)
    assert n == 0 and real == 0.0 and unreal == 0.0


def test_grid_sim_baja_monotona_inventario_trabado():
    # compra los dips pero NUNCA cosecha (falling knife) → 0 harvest e inventario UNDERWATER
    real, unreal, n = s.grid_sim([100, 90, 80], dip=0.05, tp=0.05)
    assert n == 0 and real == 0.0 and unreal < 0   # el inventario trabado pesa NEGATIVO


def test_grid_sim_gate_regimen_no_compra_en_downtrend():
    # gate SMA = grid frozen en bear: en downtrend px < SMA → no compra → no queda trabado
    closes = [100, 98, 96, 94, 92, 90, 88, 86, 84, 82]
    _, trabado_gate, _ = s.grid_sim(closes, dip=0.05, tp=0.05, sma=3)
    _, trabado_sin, _ = s.grid_sim(closes, dip=0.05, tp=0.05, sma=None)
    assert trabado_gate == 0.0 and trabado_sin < 0   # el gate evita el inventario trabado del bear


def test_score_descarta_iliquido():
    assert s.score_par(grid_pnl=10.0, liq=500_000, corr=0.8, min_liq=1_000_000) < 0     # iliquido
    assert s.score_par(grid_pnl=10.0, liq=5_000_000, corr=0.8, min_liq=1_000_000) >= 10.0
