import main, time
db = main.DatabaseManager('quantum.db')
print('=== reporte (pnl_avg real por politica) ===')
for r in db.short_shadow_reporte():
    print(f"  {r['contexto']:8} {r['politica']:11} n={r['n']:3} pnl_avg={r['pnl_avg']:+.3f}%")
print('=== gate APAREADO (el que usaria el meta en vivo) ===')
for ctx in ['calmo', 'normal']:
    rows = db.short_shadow_rows_por_contexto(ctx)
    pol = main.seleccionar_politica_meta_apareado(rows, 30, 3.15e7, time.time())  # halflife 1 año
    print(f'  {ctx}: el meta elegiria -> {pol}')
