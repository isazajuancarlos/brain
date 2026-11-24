"""V1.8.5 — sim_grid_intrabar (opción ③ fiel): mecánica real del grid OKX a 15m. PURO, sin red.
Verifica las invariantes que deciden la pregunta de geometría (entrada/harvest/DCA/capital)."""
import entrenar


def _bar(close, high=None, low=None, rsi=50, adx=10, ema200=None, atr_pct=2.0, green=True):
    return {"close": close, "high": high if high is not None else close,
            "low": low if low is not None else close, "rsi": rsi, "adx": adx,
            "ema200": ema200 if ema200 is not None else close * 0.9,
            "atr_pct": atr_pct, "green": green}


def test_entra_con_adx_bajo_y_sobre_ema():
    bars = [_bar(100, adx=10, ema200=90)] + [_bar(100) for _ in range(5)]
    oper = [True] * len(bars)
    r = entrenar.sim_grid_intrabar(bars, oper, invest_pct=0.04, capital0=1000.0)
    assert r["n_entry"] == 1            # entró una vez (luego ya tiene pos)


def test_no_entra_si_adx_alto():
    bars = [_bar(100, adx=25, ema200=90) for _ in range(6)]
    r = entrenar.sim_grid_intrabar(bars, [True] * 6)
    assert r["n_entry"] == 0


def test_no_entra_bajo_ema():
    bars = [_bar(100, adx=10, ema200=110) for _ in range(6)]
    r = entrenar.sim_grid_intrabar(bars, [True] * 6)
    assert r["n_entry"] == 0


def test_no_entra_si_no_operable():
    bars = [_bar(100, adx=10, ema200=90) for _ in range(6)]
    r = entrenar.sim_grid_intrabar(bars, [False] * 6)
    assert r["n_entry"] == 0


def test_harvest_vende_al_tocar_tier():
    # entra a 100; luego un bar cuyo HIGH toca +2% (primer tier, atr_mult calmo no aplica: atr_pct=2 -> ~0.94)
    # usamos atr_pct alto para mult≈1.4 -> tier0 ≈ 2.8%; el high a +5% lo cubre
    bars = [_bar(100, adx=10, ema200=90, atr_pct=5.0)]
    bars += [_bar(100, high=105, low=100, adx=10, ema200=90, atr_pct=5.0)]
    bars += [_bar(105, adx=10, ema200=90, atr_pct=5.0) for _ in range(3)]
    r = entrenar.sim_grid_intrabar(bars, [True] * len(bars), cooldown_bars=1)
    assert r["n_harvest"] >= 1
    assert r["realizado_pct"] > 0      # cosechó ganancia


def test_tier_mult_mas_ancho_cosecha_mas_tarde():
    # mismo path; con tier_mult grande los tiers se alejan -> menos harvests
    bars = [_bar(100, adx=10, ema200=90, atr_pct=2.0)]
    bars += [_bar(100 + k * 0.5, high=100 + k * 0.5, low=100, adx=10, ema200=90, atr_pct=2.0)
             for k in range(1, 30)]   # sube gradual a +14.5%
    oper = [True] * len(bars)
    estrecho = entrenar.sim_grid_intrabar(bars, oper, tier_mult=0.6, cooldown_bars=1)
    ancho = entrenar.sim_grid_intrabar(bars, oper, tier_mult=2.0, cooldown_bars=1)
    assert estrecho["n_harvest"] >= ancho["n_harvest"]   # más apretado cosecha más seguido


def test_capital_finito_no_sobregira():
    # invest_pct alto + muchas señales: cash nunca negativo
    bars = [_bar(100, adx=10, ema200=90) for _ in range(50)]
    r = entrenar.sim_grid_intrabar(bars, [True] * 50, invest_pct=0.9, capital0=1000.0,
                                   cooldown_bars=1)
    assert r["cash_final"] >= -1e-6


def test_dca_agrega_en_perdida_con_rebote():
    # entra a 100; cae a 80 (pnl -20% -> L3) con low, luego rebota a 84 (bounce +5% desde 80)
    bars = [_bar(100, adx=10, ema200=90)]
    bars += [_bar(82, high=82, low=80, adx=10, ema200=90, green=True)]   # toca -20%, rebota a 82
    bars += [_bar(84, high=84, low=82, adx=10, ema200=90, green=True)]
    r = entrenar.sim_grid_intrabar(bars, [True] * len(bars), cooldown_bars=1)
    assert r["n_dca"] >= 1
