"""Tests del EntrenadorPeriodico — auto-llamada a entrenar.py (subproceso inyectable)."""
import importlib
import os
import main as _main_mod


def _reload(env=None):
    for k, v in (env or {}).items():
        os.environ[k] = str(v)
    importlib.reload(_main_mod)
    return _main_mod


def test_entrenador_ejecuta_retrain():
    m = _reload({"ENTRENAR_DIAS": "45", "ENTRENAR_AUTO_BACKFILL": "false"})
    db = m.DatabaseManager(":memory:")
    calls = []
    ent = m.EntrenadorPeriodico(db, runner=lambda cmd: calls.append(cmd))
    ent._ejecutar()
    assert len(calls) == 1
    assert any("entrenar.py" in str(c) for c in calls[0])
    assert "45" in calls[0]


def test_entrenador_backfill_opcional():
    m = _reload({"ENTRENAR_AUTO_BACKFILL": "true"})
    db = m.DatabaseManager(":memory:")
    calls = []
    ent = m.EntrenadorPeriodico(db, runner=lambda cmd: calls.append(cmd))
    ent._ejecutar()
    assert len(calls) == 2
    assert "--shadow-backfill" in calls[1]


def test_entrenador_falla_no_propaga():
    m = _reload({"ENTRENAR_AUTO_BACKFILL": "false"})
    db = m.DatabaseManager(":memory:")
    def boom(cmd):
        raise RuntimeError("subproc cayó")
    ent = m.EntrenadorPeriodico(db, runner=boom)
    ent._ejecutar()   # no debe levantar


# ------------------------------------------------ walk-forward temporal (entrenar.py)
def test_split_temporal_no_filtra_futuro():
    import numpy as np, entrenar
    X = np.arange(10).reshape(-1, 1).astype(float)
    y = np.zeros(10, dtype=int)
    ts = np.arange(10).astype(float)            # ya ordenado en el tiempo
    Xtr, Xte, ytr, yte = entrenar._split_temporal(X, y, ts, test_frac=0.2, embargo=0)
    assert Xtr.flatten().tolist() == [0, 1, 2, 3, 4, 5, 6, 7]   # pasado
    assert Xte.flatten().tolist() == [8, 9]                     # futuro (más reciente)


def test_split_temporal_ordena_por_tiempo():
    import numpy as np, entrenar
    X = np.arange(10).reshape(-1, 1).astype(float)
    y = np.zeros(10, dtype=int)
    ts = np.arange(10)[::-1].astype(float)      # X y ts en orden inverso
    Xtr, Xte, _, _ = entrenar._split_temporal(X, y, ts, test_frac=0.2, embargo=0)
    # test = ts mayor = X menor (0,1) tras ordenar ascendente
    assert sorted(Xte.flatten().tolist()) == [0, 1]
    assert set(Xtr.flatten()).isdisjoint(set(Xte.flatten()))    # sin leakage


def test_split_temporal_embargo_descarta_borde():
    import numpy as np, entrenar
    X = np.arange(10).reshape(-1, 1).astype(float)
    y = np.zeros(10, dtype=int)
    ts = np.arange(10).astype(float)
    Xtr, Xte, _, _ = entrenar._split_temporal(X, y, ts, test_frac=0.2, embargo=2)
    assert Xtr.flatten().tolist() == [0, 1, 2, 3, 4, 5]   # corte=8, embargo 2 → train hasta 6
    assert Xte.flatten().tolist() == [8, 9]


# ------------------------------------------------ Punto A: funding como feature del ML
def test_ml_features_identicas_off():
    import importlib, entrenar
    m = _reload({"ML_FUNDING_FEATURE": "false"})
    importlib.reload(entrenar)
    assert m.ml_feature_names() == entrenar.FEATURES_ORDER == ["rsi", "adx", "ema_dist", "macd", "vol_ratio"]


def test_ml_features_incluyen_funding_con_flag():
    import importlib, entrenar
    m = _reload({"ML_FUNDING_FEATURE": "true"})
    importlib.reload(entrenar)
    assert m.ml_feature_names() == entrenar.FEATURES_ORDER     # IDÉNTICAS (la regla del repo)
    assert m.ml_feature_names() == ["rsi", "adx", "ema_dist", "macd", "vol_ratio", "funding"]
    importlib.reload(entrenar)   # deja entrenar consistente para el resto
