"""
limpiar_fantasma.py — Limpia filas FANTASMA de short_positions (one-shot, standalone).

Contexto: con OKX_FUT_ENABLED=false el hilo EscuadronShortOkx no corre, así que
reconciliar_shorts() nunca barre las filas de short_positions de shorts que OKX ya
cerró por OCO. Quedan como "posiciones abiertas" fantasma: ensucian el HUD pero NO
hay capital en riesgo (en OKX no existe la posición).

Seguridad (dos guardas):
  1) ABORTA si el bot está corriendo (la DB está lockeada por el proceso vivo).
     Escribir la DB con el bot abierto = race + riesgo de corromper el WAL.
  2) Re-verifica contra OKX EN VIVO: solo borra filas cuyo símbolo NO tenga una
     posición swap real (pos != 0). Nunca borra un short realmente abierto.

El bot (main.py) DEBE estar APAGADO al correr esto.

Uso:
    python limpiar_fantasma.py              # DRY-RUN (default): muestra qué borraría, NO toca nada
    python limpiar_fantasma.py --ejecutar   # borra de verdad; pide confirmación tipeada "SI"
    python limpiar_fantasma.py --sin-okx    # NO consulta OKX (asume todas fantasma; usar solo si no hay claves)
"""
import argparse
import sqlite3
import sys


def _instid_swap(symbol):
    """'BTC/USDT:USDT' -> 'BTC-USDT-SWAP' (instId de OKX swap)."""
    base = symbol.split("/")[0].strip()
    return f"{base}-USDT-SWAP"


def filas_fantasma(db_rows, instids_vivos):
    """Función pura. Dada la lista de filas (symbol, ...) y el set de instIds con
    posición real en OKX, devuelve (fantasmas, reales).
    Una fila es fantasma si su instId NO está entre los vivos."""
    fantasmas, reales = [], []
    for row in db_rows:
        symbol = row[0]
        if _instid_swap(symbol) in instids_vivos:
            reales.append(row)
        else:
            fantasmas.append(row)
    return fantasmas, reales


def _proceso_main_vivo():
    """True si hay un proceso `python main.py` corriendo (excluye este script).
    Robusto en local (Linux): el chequeo por write-lock de SQLite da falso negativo
    porque en WAL el lock está libre entre ticks del bot."""
    import os
    yo = os.getpid()
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit() or int(pid) == yo:
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    args = fh.read().split(b"\0")
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            if any(a.endswith(b"main.py") or a.endswith(b"/main.py") for a in args):
                return True
    except FileNotFoundError:
        pass  # no /proc (no Linux) → caemos al chequeo de lock
    return False


def _bot_corriendo(conn):
    """True si el bot (main.py) está vivo. Dos señales:
    (1) proceso main.py corriendo (robusto), (2) write-lock tomado ahora mismo."""
    if _proceso_main_vivo():
        return True
    try:
        conn.execute("PRAGMA busy_timeout = 800;")
        conn.execute("BEGIN IMMEDIATE;")  # toma write-lock; falla si el bot lo tiene
        conn.execute("ROLLBACK;")
        return False
    except sqlite3.OperationalError:
        return True


def _instids_vivos_okx():
    """Consulta OKX las posiciones swap reales (pos != 0). Devuelve set de instIds."""
    import os
    import ccxt
    from dotenv import load_dotenv
    load_dotenv()
    ex = ccxt.okx({
        "apiKey": os.getenv("OKX_API_KEY"),
        "secret": os.getenv("OKX_API_SECRET"),
        "password": os.getenv("OKX_PASSPHRASE"),
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })
    res = ex.private_get_account_positions({"instType": "SWAP"})
    vivos = set()
    for p in res.get("data", []):
        try:
            if float(p.get("pos") or 0) != 0:
                vivos.add(p.get("instId"))
        except (TypeError, ValueError):
            continue
    return vivos


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Limpia filas fantasma de short_positions")
    p.add_argument("--ejecutar", action="store_true", help="borra de verdad (default: dry-run)")
    p.add_argument("--sin-okx", action="store_true",
                   help="no consulta OKX; trata TODAS las filas como fantasma (usar solo sin claves)")
    return p.parse_args(argv)


def main():
    args = parse_args()
    print("ATENCIÓN: el bot (main.py) debe estar APAGADO. No corras dos instancias.\n")

    conn = sqlite3.connect("quantum.db")

    if _bot_corriendo(conn):
        print("ABORTADO: la DB está lockeada — el bot parece estar CORRIENDO.")
        print("Apagá el bot (Ctrl+C, o 'touch KILL') y reintentá.")
        sys.exit(1)

    rows = conn.execute(
        "SELECT symbol, entry_px, qty, lever, open_ts FROM short_positions"
    ).fetchall()
    if not rows:
        print("short_positions está vacía. Nada que limpiar.")
        return

    if args.sin_okx:
        print("Modo --sin-okx: NO se consulta OKX, se asumen TODAS fantasma.\n")
        fantasmas, reales = rows, []
    else:
        try:
            vivos = _instids_vivos_okx()
        except Exception as e:  # noqa: BLE001 — fail-safe: sin verificación no se borra
            print(f"ABORTADO: no pude consultar OKX ({e}).")
            print("Reintentá, o usá --sin-okx si estás seguro de que no hay shorts abiertos.")
            sys.exit(1)
        print(f"Posiciones swap reales en OKX: {sorted(vivos) or '(ninguna)'}\n")
        fantasmas, reales = filas_fantasma(rows, vivos)

    for r in reales:
        print(f"  REAL (NO se toca): {r[0]} entry={r[1]} qty={r[2]} lev={r[3]}")
    for r in fantasmas:
        print(f"  FANTASMA: {r[0]} entry={r[1]} qty={r[2]} lev={r[3]} open_ts={r[4]}")

    if not fantasmas:
        print("\nNo hay filas fantasma. Nada que borrar.")
        return

    if not args.ejecutar:
        print(f"\nDRY-RUN: borraría {len(fantasmas)} fila(s) fantasma. "
              "Corré con --ejecutar para hacerlo de verdad.")
        return

    resp = input(f"\nVas a BORRAR {len(fantasmas)} fila(s) fantasma de short_positions. "
                 "Escribí 'SI' para continuar: ")
    if resp.strip() != "SI":
        print("Abortado.")
        return

    cur = conn.cursor()
    for r in fantasmas:
        cur.execute("DELETE FROM short_positions WHERE symbol = ?", (r[0],))
    conn.commit()
    print(f"Listo: {len(fantasmas)} fila(s) borrada(s). short_positions limpia.")


if __name__ == "__main__":
    main()
