"""Tests de la simulación pura del Sleeve Trend-Ride (Fase 1, medición).
simular_sleeve no toca red ni DB: entrada en TENDENCIA_UP, ride con chandelier, salidas
(chandelier / stop duro / flip de régimen). Barras sintéticas."""
import entrenar


def _bar(close, high, low, atr):
    return {"close": close, "high": high, "low": low, "atr": atr}


def test_chandelier_sale_con_ganancia():
    # sube hasta 130 (peak), retrocede k·ATR (k=2, atr=5 → trail=120) → sale en 120 con +20%
    bars = [_bar(100, 100, 100, 5), _bar(110, 112, 108, 5), _bar(125, 130, 120, 5)]
    estados = ["TENDENCIA_UP", "TENDENCIA_UP", "TENDENCIA_UP"]
    trades = entrenar.simular_sleeve(bars, estados, k=2.0, hard=0.10, fee=0.002)
    assert len(trades) == 1
    t = trades[0]
    assert t["motivo"] == "chandelier"
    assert t["pnl"] > 0.15            # ~0.198 tras fee
    assert t["entry"] == 100


def test_flip_de_regimen_sale_al_close():
    # el régimen deja de ser TENDENCIA_UP → sale al close de ese día (la tendencia terminó)
    bars = [_bar(100, 100, 100, 5), _bar(105, 106, 104, 5), _bar(103, 104, 102, 5)]
    estados = ["TENDENCIA_UP", "TENDENCIA_UP", "RANGO"]
    trades = entrenar.simular_sleeve(bars, estados, k=2.0, hard=0.10, fee=0.002)
    assert len(trades) == 1
    assert trades[0]["motivo"] == "flip"
    assert abs(trades[0]["exit"] - 103) < 1e-9


def test_stop_duro_cuando_trail_queda_debajo():
    # caída fuerte con ATR grande: trail (80) < hard_stop (90) → gobierna el stop duro, sale en 90
    bars = [_bar(100, 100, 100, 10), _bar(75, 100, 75, 10)]
    estados = ["TENDENCIA_UP", "TENDENCIA_UP"]
    trades = entrenar.simular_sleeve(bars, estados, k=2.0, hard=0.10, fee=0.002)
    assert len(trades) == 1
    assert trades[0]["motivo"] == "stop_duro"
    assert trades[0]["pnl"] < 0       # ~ -0.102


def test_no_entra_fuera_de_tendencia_up():
    bars = [_bar(100, 101, 99, 5), _bar(102, 103, 101, 5)]
    estados = ["RANGO", "CAOS"]
    assert entrenar.simular_sleeve(bars, estados, k=2.0, hard=0.10, fee=0.002) == []
