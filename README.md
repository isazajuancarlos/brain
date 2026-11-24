# brain — quantitative crypto trading bot (OKX)

> **Status: retired.** Published as a clean, self-contained source snapshot for
> reference. It carries **no credentials and no real account data** — every
> config value is an example (`.env.example`, example portfolio seed).

`brain` is a single-process, event-driven spot trading bot for OKX, built around
a **regime-aware ladder engine**, a **microstructure data collector**, an
**XGBoost signal oracle**, and a **risk layer that fails safe by construction**.
It was developed test-first (381-test suite) with a strict telemetry discipline:
*the absence of a datum is shouted, never filled with silence.*

---

## Design principles

- **Fail safe for capital.** Reconciliation only ever adjusts positions *down*
  (adopts what the exchange reports, never over-deploys). An unknown state parks
  a position in `LIQUIDATE-ONLY` rather than guessing.
- **No silent freeze.** Every strategy sleeve publishes its state on *each tick,
  whether it trades or not* — a frozen-but-healthy sleeve must be distinguishable
  from a hung one. This came from real incidents where a sleeve stopped posting
  orders and left no order, no log, no HUD line; the fix was telemetry that
  grades `NO-STATE > STALE > frozen/active` so a stale state can never read as
  "frozen and fine."
- **Test-first.** Each behavioral change ships with tests that are *red before
  the fix*, including the wiring (a correct helper nobody calls leaves the same
  silence). Suite: 381 tests.

## Architecture

```
market data ──▶ microflow collector ──▶ SQLite (OHLC, CVD, OBI, OI, funding, liqs)
                                             │
                        feature build ◀──────┘
                             │
                      XGBoost oracle ──▶ entry signals
                             │
   regime detector ──▶ strategy sleeves ──▶ OKX (ccxt / ccxt.pro)
                             │                     ▲
                        risk layer ────────────────┘ (caps, locks, kill-switch)
                             │
                        HUD / telemetry (per-tick state)
```

### Strategy engine (sleeves)
Independent sleeves, each gated by the per-symbol **regime** (`RANGO`,
`TENDENCIA_DOWN`, …) so a sleeve only acts where its edge exists:

- **Ladder** — range-bound grid that re-arms rungs off a stable `avg_cost`;
  tracks *actually filled* quantity, not requested, and re-posts resting sells.
- **Martillo / Grid / Caos** — entry engines for different volatility regimes.
- **Short bajista** — the bearish leg, for down-trending regimes.
- **OmniRisk** — exit management: Moonbag, Quantum Ratchet, sector stops.
- **EscudoDCA** — defensive averaging, scoped to exclude liquidate-only alts.

### Market-data pipeline
`colector_microflujo` (a systemd service) ingests per-minute microstructure:
OHLC, CVD (cumulative + delta), order-book imbalance, open interest, funding and
liquidation notional — the feature base shared across venues.

### Signal oracle (ML)
XGBoost model over `RSI_14, ADX_14, ema_dist, MACDh_12_26_9, vol_ratio`, labeled
on whether price hits `entry × 1.012` (TP) before `entry × 0.98` (SL) within the
horizon. Training/offline research live in `entrenar.py` and `lab.py`.

### Risk layer
Per-venue capital caps, race-safe budget allocation behind locks, a balance
cache with short TTL, and a `LIQUIDATE-ONLY` mode that manages exits on
pre-existing positions without ever adding capital to them.

## Repository layout

| Area | Files |
|---|---|
| Runtime | `main.py`, `comando.py` |
| Data collector | `colector_microflujo.py`, `colector-microflujo.service`, `colector_microflujo.md` |
| ML / research | `entrenar.py`, `lab.py`, `screener_pares.py` |
| Ops | `liquidar.py`, `limpiar_fantasma.py`, `portfolio_seed.json` (example) |
| Tests | `test_*.py`, `conftest.py` |
| Specs / design notes | `*_spec.md`, `*_plan.md`, `bot.txt` (technical reference & changelog) |

## Configuration

Copy `.env.example` to `.env` and fill your own values. The bot reads OKX API
credentials (`OKX_API_KEY`, `OKX_SECRET_KEY`, `OKX_PASSPHRASE`) **only** from the
environment — none are stored in the repo. `portfolio_seed.json` ships with
example positions; replace them with your own (or leave empty) before running.

## Disclaimer

Reference code, retired and unmaintained. Not financial advice and not a
solicitation. Trading crypto assets carries risk of loss. Run at your own risk.

---

Part of the work of **Xiliux** — Juan Carlos Isaza (Medellín, Colombia).
