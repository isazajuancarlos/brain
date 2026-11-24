"""V1.8.4 — grid_sim_neto (opción ③): cosecha del grid con gate de régimen + fees. PURO, sin red."""
import entrenar


def test_dip_y_cosecha_una_vez():
    # 100 -> 95 (dip 5%, compra) -> 99.75 (+5% sobre 95, cosecha)
    closes = [100.0, 95.0, 99.75]
    real, trabado, n = entrenar.grid_sim_neto(closes, [True, True, True], dip=0.05, tp=0.05, fee=0.0)
    assert n == 1
    assert real == 5.0          # tp 5% sin fee
    assert trabado == 0.0       # nada trabado


def test_fee_descuenta_la_cosecha():
    closes = [100.0, 95.0, 99.75]
    real, _, n = entrenar.grid_sim_neto(closes, [True, True, True], dip=0.05, tp=0.05, fee=0.002)
    assert n == 1
    assert abs(real - 4.8) < 1e-9   # (0.05 - 0.002)*100


def test_gate_regimen_bloquea_compra():
    # mismo dip pero la barra del dip NO es operable -> no compra -> no cosecha
    closes = [100.0, 95.0, 99.75]
    real, trabado, n = entrenar.grid_sim_neto(closes, [True, False, True], dip=0.05, tp=0.05, fee=0.0)
    assert n == 0
    assert real == 0.0


def test_inventario_trabado_castiga():
    # compra en el dip y nunca recupera -> queda underwater (trabado negativo)
    closes = [100.0, 95.0, 90.0]
    real, trabado, n = entrenar.grid_sim_neto(closes, [True, True, True], dip=0.05, tp=0.05, fee=0.0)
    assert n == 0
    assert real == 0.0
    assert trabado < 0.0


def test_monotono_sube_no_compra():
    closes = [100.0, 101.0, 102.0, 103.0]
    real, trabado, n = entrenar.grid_sim_neto(closes, [True] * 4, dip=0.05, tp=0.05, fee=0.0)
    assert (real, trabado, n) == (0.0, 0.0, 0)


def test_cap_inventario_limita_compras():
    # cae en escalera 4 veces (cada -5% desde el pico nuevo); con max_inv=2 solo compra 2 lotes
    closes = [100.0, 95.0, 90.0, 85.0, 80.0]
    real, trabado, n = entrenar.grid_sim_neto(closes, [True] * 5, dip=0.05, tp=0.05,
                                              fee=0.0, max_inv=2)
    assert n == 0                       # nunca recupera, no cosecha
    # trabado = solo 2 lotes (95 y 90) marcados a 80, NO 4 lotes
    esperado = ((80 - 95) / 95 + (80 - 90) / 90) * 100
    assert abs(trabado - round(esperado, 2)) < 0.05
