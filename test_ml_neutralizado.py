"""V1.8.3 — el sizing ML quedó NEUTRALIZADO por defecto (AUC walk-forward 0.495 = ruido).
Estos tests bloquean esa decisión: con el flag off, ml_predict_prob no opina (None) y
ml_size_multiplier devuelve 1.0 (sizing neutro). Revivible con ML_SIZING_ENABLED=true."""
import main


def test_predict_prob_none_cuando_off(monkeypatch):
    monkeypatch.setitem(main.CONFIG, "ml_sizing_enabled", False)
    assert main.ml_predict_prob("okx", "BTC/USDT") is None


def test_size_multiplier_neutro_cuando_off(monkeypatch):
    # sin importar features/modelo, off → mult=1.0 (no escala nada)
    monkeypatch.setitem(main.CONFIG, "ml_sizing_enabled", False)
    assert main.ml_size_multiplier("okx", "BTC/USDT") == 1.0
    assert main.ml_size_multiplier("okx", "ETH/USDT") == 1.0
