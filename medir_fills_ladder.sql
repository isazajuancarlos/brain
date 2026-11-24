-- ============================================================================
-- MEDICIÓN: fill REAL vs IDEAL del canary ladder (riesgo #1 de SP3)
-- ----------------------------------------------------------------------------
-- El sim (sim_grid_ladder) asume IDEAL: una orden maker se LLENA en el instante
-- en que el precio TOCA el rung (fill-on-touch, 100%). La realidad: una limit
-- maker en el rung puede NO llenarse al tocar (cola, el precio rebota sin
-- atravesar). Esta query mide cuánto se aleja la realidad del ideal.
--
-- Tabla: ladder_fill_log(order_id, symbol, rung_idx, price,
--                        ts_colocado, ts_tocado, lleno, ts_lleno)
--
-- Uso (read-only, NO toca el bot):
--   sqlite3 "file:quantum.db?mode=ro" < medir_fills_ladder.sql
--
-- Lectura: la métrica clave es FILL_RATE_REAL = llenos / tocados.
--   Ideal = 100%. Graduación (config, no código): ≥~70% → fills creíbles, escalar.
--   <70% → el sim infla los round-trips; revisar antes de poner más capital.
-- ============================================================================

.mode column
.headers on

SELECT '== Resumen global (todos los símbolos) ==' AS seccion;
SELECT
  COUNT(*)                                             AS ordenes,
  SUM(ts_tocado IS NOT NULL)                           AS tocadas,
  SUM(lleno = 1)                                       AS llenas,
  ROUND(100.0 * SUM(lleno = 1)
        / NULLIF(SUM(ts_tocado IS NOT NULL), 0), 1)    AS fill_rate_real_pct,  -- vs ideal 100%
  ROUND(AVG(CASE WHEN lleno = 1 AND ts_lleno IS NOT NULL AND ts_tocado IS NOT NULL
                 THEN ts_lleno - ts_tocado END), 1)    AS latencia_avg_s,      -- demora toque→fill
  SUM(ts_tocado IS NOT NULL AND lleno = 0)             AS tocadas_sin_llenar   -- el gap vs el ideal
FROM ladder_fill_log;

SELECT '== Por símbolo / rung ==' AS seccion;
SELECT
  symbol,
  rung_idx,
  COUNT(*)                                             AS ordenes,
  SUM(ts_tocado IS NOT NULL)                           AS tocadas,
  SUM(lleno = 1)                                       AS llenas,
  ROUND(100.0 * SUM(lleno = 1)
        / NULLIF(SUM(ts_tocado IS NOT NULL), 0), 1)    AS fill_rate_real_pct
FROM ladder_fill_log
GROUP BY symbol, rung_idx
ORDER BY symbol, rung_idx;

SELECT '== Round-trips realizados (cosecha real) ==' AS seccion;
SELECT
  symbol,
  COUNT(*)                 AS vueltas,
  ROUND(SUM(pnl_neto), 4)  AS pnl_neto_total,
  ROUND(AVG(pnl_neto), 4)  AS pnl_neto_avg,
  SUM(pnl_neto > 0)        AS ganadoras,
  motivo
FROM ladder_realized
GROUP BY symbol, motivo
ORDER BY symbol;
