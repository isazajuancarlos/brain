"""
liquidar.py — Liquidación total OKX → USDT (one-shot, standalone).

Ver liquidar_spec.md. El bot (main.py) DEBE estar apagado al correr esto:
dos instancias contra la misma cuenta = órdenes duplicadas / reconcile en conflicto.

Uso:
    python liquidar.py                 # DRY-RUN (default): imprime el plan, NO toca nada
    python liquidar.py --ejecutar      # ejecuta de verdad; pide confirmación tipeada "SI"
    python liquidar.py --convert-dust  # además convierte el polvo sub-mínimo
    python liquidar.py --no-clean-db   # ejecuta pero NO limpia quantum.db
"""
import argparse
import math
import time

OKX_CUENTA_FUNDING = "6"
OKX_CUENTA_TRADING = "18"
POLL_TIMEOUT_S = 60.0
POLL_INTERVAL_S = 1.5
TRANSFER_REINTENTOS = 5
TRANSFER_BACKOFF_S = 3.0


class LiquidadorOKX:
    def __init__(self, exchange, db_conn, *, ejecutar=False, convert_dust=False,
                 clean_db=True, min_usd=1.0, clock=time.monotonic, sleep=time.sleep,
                 poll_timeout=POLL_TIMEOUT_S, poll_interval=POLL_INTERVAL_S,
                 transfer_reintentos=TRANSFER_REINTENTOS, transfer_backoff=TRANSFER_BACKOFF_S):
        self.ex = exchange
        self.db = db_conn
        self.ejecutar = ejecutar
        self.convert_dust = convert_dust
        self.clean_db = clean_db
        self.min_usd = min_usd  # piso de valor: por debajo se trata como polvo
        self.clock = clock
        self.sleep = sleep
        self.poll_timeout = poll_timeout
        self.poll_interval = poll_interval
        self.transfer_reintentos = transfer_reintentos
        self.transfer_backoff = transfer_backoff
        self.log = []  # lista de strings; reporte final

    def _registrar(self, msg):
        self.log.append(msg)
        print(msg)

    # ------------------------------------------------------------------ snapshot
    def _earn_balances(self):
        res = self.ex.private_get_finance_savings_balance({})
        return {r["ccy"]: float(r["amt"]) for r in res.get("data", [])}

    def _funding_balances(self):
        res = self.ex.private_get_asset_balances({})
        return {r["ccy"]: float(r.get("availBal", 0)) for r in res.get("data", [])}

    def _precio(self, ccy):
        try:
            t = self.ex.fetch_ticker(f"{ccy}/USDT")
            return float(t.get("last") or 0.0)
        except Exception:
            return 0.0  # sin par/USDT o ticker falla → se tratará como polvo

    def snapshot(self):
        bal = self.ex.fetch_balance()
        # ccxt: bal['free'] es el mapa {ccy: monto_libre}. NO iterar bal.items()
        # directamente — incluye claves no-moneda (info/timestamp/datetime).
        trading = {c: float(q or 0) for c, q in (bal.get("free") or {}).items()}
        earn = self._earn_balances()
        funding = self._funding_balances()
        ccys = {c for c in set(trading) | set(earn) | set(funding) if c != "USDT"}
        plan = []
        for ccy in sorted(ccys):
            tr = trading.get(ccy, 0.0)
            er = earn.get(ccy, 0.0)
            fu = funding.get(ccy, 0.0)
            total = tr + er + fu
            if total <= 1e-9:
                continue
            precio = self._precio(ccy)
            plan.append({"ccy": ccy, "trading": tr, "earn": er, "funding": fu,
                         "total": total, "precio": precio,
                         "valor_usd": total * precio})
        return plan

    # --------------------------------------------------------------------- polvo
    def es_polvo(self, sym, qty, precio):
        if qty <= 0 or precio <= 0:
            return True
        m = self.ex.markets.get(sym, {})
        limits = m.get("limits", {})
        min_amt = (limits.get("amount", {}) or {}).get("min") or 0.0
        min_cost = (limits.get("cost", {}) or {}).get("min") or 0.0
        if qty < min_amt:
            return True
        # Piso de notional: max entre el mínimo del exchange (a veces ausente en OKX)
        # y nuestro min_usd. Atrapa el polvo aunque el market no reporte cost.min.
        floor_cost = max(min_cost, self.min_usd)
        if qty * precio < floor_cost:
            return True
        return False

    # ----------------------------------------------------------------- limpieza db
    def limpiar_db(self, symbols, ccys):
        cur = self.db.cursor()
        for sym in symbols:
            for tabla in ("okx_portfolio", "okx_tp_state", "okx_dca_state",
                          "okx_cooldown_state", "okx_grid_orders", "okx_pending_sells"):
                cur.execute(f"DELETE FROM {tabla} WHERE symbol=?", (sym,))
        for ccy in ccys:
            cur.execute("DELETE FROM okx_inventory_earn WHERE ccy=?", (ccy,))
        self.db.commit()

    # --------------------------------------------------------------- rescate earn
    def _funding_balance_ccy(self, ccy):
        res = self.ex.private_get_asset_balances({"ccy": ccy})
        for r in res.get("data", []):
            if r.get("ccy") == ccy:
                return float(r.get("availBal", 0))
        return 0.0

    def _redempt(self, ccy, amt):
        self.ex.private_post_finance_savings_purchase_redempt(
            {"ccy": ccy, "amt": str(round(amt, 8)), "side": "redempt", "rate": "0.01"})

    def _transfer(self, ccy, amt, src, dst):
        self.ex.private_post_asset_transfer(
            {"ccy": ccy, "amt": str(round(amt, 8)), "from": src, "to": dst, "type": "0"})

    def _esperar_funding(self, ccy, objetivo):
        """Poll hasta que el availBal de Funding alcance `objetivo` (o timeout)."""
        t0 = self.clock()
        while (self.clock() - t0) <= self.poll_timeout:
            self.sleep(self.poll_interval)
            if self._funding_balance_ccy(ccy) >= objetivo * 0.99:
                return True
        self._registrar(f"WARN: settlement {ccy} no llegó a Funding en {self.poll_timeout:.0f}s")
        return False

    def _barrer_funding_a_trading(self, ccy):
        """Transfiere TODO el availBal de Funding del ccy a Trading. Reintenta:
        OKX a veces muestra el availBal una fracción antes de que sea transferible
        (causa del 58350 Insufficient balance). Trunca a 6 decimales para no exceder."""
        for intento in range(self.transfer_reintentos):
            bal = self._funding_balance_ccy(ccy)
            mover = math.floor(bal * 1e6) / 1e6  # nunca pedir más que el disponible
            if mover <= 0:
                return 0.0
            try:
                self._transfer(ccy, mover, OKX_CUENTA_FUNDING, OKX_CUENTA_TRADING)
                self._registrar(f"transferido {mover:.6f} {ccy} Funding→Trading")
                return mover
            except Exception as e:
                self._registrar(f"transfer {ccy} intento {intento+1}/{self.transfer_reintentos} "
                                f"falló ({e}); reintento")
                self.sleep(self.transfer_backoff)
        self._registrar(f"WARN: {ccy} no se pudo transferir de Funding; queda para re-correr")
        return 0.0

    def consolidar_a_trading(self, ccy, earn_amt):
        """Idempotente: redime Earn (si hay) y barre TODO el Funding del ccy a Trading.
        Barrer Funding recupera además fondos varados de una corrida previa fallida."""
        if not self.ejecutar:
            if earn_amt > 1e-9:
                self._registrar(f"[dry-run] rescataría {earn_amt:.6f} {ccy} de Earn")
            return earn_amt
        if earn_amt > 1e-9:
            antes = self._funding_balance_ccy(ccy)
            self._redempt(ccy, earn_amt)
            self._esperar_funding(ccy, antes + earn_amt)
        return self._barrer_funding_a_trading(ccy)

    # ---------------------------------------------------------------------- venta
    def vender(self, ccy, qty, precio):
        sym = f"{ccy}/USDT"
        # Clasificar polvo ANTES de redondear: amount_to_precision de ccxt LANZA
        # InvalidOrder en cantidades sub-precisión (el polvo), no devuelve.
        if self.es_polvo(sym, qty, precio):
            self._registrar(f"polvo: {ccy} qty {qty} (~${qty*precio:.4f}) — no vendible")
            return {"ccy": ccy, "vendido": False, "polvo": True, "qty": qty}
        try:
            qty_r = float(self.ex.amount_to_precision(sym, qty))
        except Exception as e:
            self._registrar(f"polvo: {ccy} qty {qty} no redondeable ({e})")
            return {"ccy": ccy, "vendido": False, "polvo": True, "qty": qty}
        if self.es_polvo(sym, qty_r, precio):
            self._registrar(f"polvo: {ccy} qty {qty_r} (~${qty_r*precio:.4f}) — no vendible")
            return {"ccy": ccy, "vendido": False, "polvo": True, "qty": qty_r}
        if not self.ejecutar:
            self._registrar(f"[dry-run] vendería {qty_r} {ccy} (~${qty_r*precio:.2f})")
            return {"ccy": ccy, "vendido": True, "polvo": False, "qty": qty_r}
        order = self.ex.create_market_sell_order(sym, qty_r)
        self._registrar(f"vendido {qty_r} {ccy} @ ~{order.get('average')}")
        return {"ccy": ccy, "vendido": True, "polvo": False, "qty": qty_r, "order": order}

    # ------------------------------------------------------------- cancelar ordenes
    def cancelar_ordenes(self):
        abiertas = self.ex.fetch_open_orders()
        if not self.ejecutar:
            self._registrar(f"[dry-run] cancelaría {len(abiertas)} órdenes abiertas")
            return len(abiertas)
        n = 0
        for o in abiertas:
            try:
                self.ex.cancel_order(o["id"], o["symbol"])
                n += 1
            except Exception as e:
                self._registrar(f"WARN cancel {o.get('id')}: {e}")
        self._registrar(f"canceladas {n} órdenes abiertas")
        return n

    # --------------------------------------------------------------- orquestación
    def ejecutar_liquidacion(self):
        plan = self.snapshot()
        self._registrar(f"=== PLAN ({'EJECUTAR' if self.ejecutar else 'DRY-RUN'}) ===")
        for p in plan:
            self._registrar(f"  {p['ccy']}: total {p['total']:.6f} "
                            f"(trading {p['trading']:.6f} + earn {p['earn']:.6f} "
                            f"+ funding {p.get('funding', 0.0):.6f}) ~${p['valor_usd']:.2f}")
        if not plan:
            self._registrar("Nada que liquidar.")
            return

        # 1. cancelar órdenes
        self.cancelar_ordenes()
        # 2. consolidar a Trading: redime Earn + barre Funding (recupera fondos varados).
        #    Por-moneda con try/except para que un fallo no aborte el resto.
        for p in plan:
            try:
                self.consolidar_a_trading(p["ccy"], p["earn"])
            except Exception as e:
                self._registrar(f"WARN consolidación {p['ccy']}: {e}")
        # 3. re-leer trading y vender
        libres = self.ex.fetch_balance().get("free") or {}
        liquidados, polvo, ccys_ok = [], [], []
        for p in plan:
            ccy = p["ccy"]
            qty = float(libres.get(ccy, 0) or 0) if self.ejecutar else p["total"]
            if qty <= 1e-9:
                continue
            res = self.vender(ccy, qty, p["precio"])
            if res["vendido"]:
                liquidados.append(f"{ccy}/USDT")
                ccys_ok.append(ccy)
            elif res["polvo"]:
                polvo.append(ccy)
                if self.convert_dust:
                    self._convertir_polvo(ccy, res["qty"])
        # 4. limpiar DB
        if self.clean_db and self.ejecutar:
            self.limpiar_db(liquidados, ccys_ok)
        # 5. reporte
        self._registrar(f"=== FIN === vendidos: {liquidados or 'ninguno'} | "
                        f"polvo: {polvo or 'ninguno'}")

    def _convertir_polvo(self, ccy, qty):
        # OKX small-balance convert. Endpoint exacto a confirmar en runbook (Task 9).
        self._registrar(f"[convert-dust] {ccy} {qty} (endpoint pendiente de confirmar)")


# ============================================================================ CLI
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Liquidación total OKX → USDT")
    p.add_argument("--ejecutar", action="store_true", help="ejecuta de verdad (default: dry-run)")
    p.add_argument("--convert-dust", action="store_true", help="convierte polvo sub-mínimo")
    p.add_argument("--no-clean-db", action="store_true", help="no limpia quantum.db")
    p.add_argument("--min-usd", type=float, default=1.0,
                   help="piso de valor en USD; por debajo se trata como polvo (default 1.0)")
    return p.parse_args(argv)


def construir_okx():
    import os
    import ccxt
    from dotenv import load_dotenv
    load_dotenv()
    ex = ccxt.okx({
        "apiKey": os.getenv("OKX_API_KEY"),
        "secret": os.getenv("OKX_API_SECRET"),
        "password": os.getenv("OKX_PASSPHRASE"),
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })
    ex.load_markets()
    return ex


def main():
    import sqlite3
    args = parse_args()
    print("ATENCIÓN: el bot (main.py) debe estar APAGADO. No corras dos instancias.")
    ex = construir_okx()
    conn = sqlite3.connect("quantum.db")
    liq = LiquidadorOKX(ex, conn, ejecutar=args.ejecutar,
                        convert_dust=args.convert_dust, clean_db=not args.no_clean_db,
                        min_usd=args.min_usd)
    if args.ejecutar:
        resp = input("Vas a LIQUIDAR todo OKX a USDT. Escribí 'SI' para continuar: ")
        if resp.strip() != "SI":
            print("Abortado.")
            return
    liq.ejecutar_liquidacion()


if __name__ == "__main__":
    main()
