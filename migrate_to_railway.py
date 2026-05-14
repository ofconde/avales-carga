#!/usr/bin/env python3
"""
Migración única: exporta todos los registros de la DB local y los sube a Railway.
Uso: python3 migrate_to_railway.py
"""
import sqlite3
import json
import urllib.request
import sys
from pathlib import Path

RAILWAY_URL = "https://avales-carga-production.up.railway.app"
LOCAL_DB    = Path(__file__).parent / "carga.db"
BATCH_SIZE  = 200

COLS = [
    "num_exp", "fecha_instruccion", "fecha_aval_recibido", "fecha_tasacion_recibida",
    "fecha_carga", "titular", "provincia", "nombre_proyecto", "linea_programatica",
    "monto", "garantia", "tec_de_carga", "paso_carga_inicial", "se_solicito",
    "tecnico", "observaciones", "deleted_at", "cuit", "avalista", "estado_pei",
    "linea_manual", "fecha_respuesta_uep", "devolvio_legales",
    "motivo_devolucion_legales", "observaciones_carga",
]

def post_json(url, data):
    body = json.dumps(data).encode()
    req  = urllib.request.Request(url, data=body,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())

def main():
    conn = sqlite3.connect(str(LOCAL_DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT {','.join(COLS)} FROM expedientes ORDER BY id").fetchall()
    conn.close()

    records = [dict(r) for r in rows]
    total   = len(records)
    print(f"Exportando {total} registros a {RAILWAY_URL} ...")

    imported = skipped = 0
    for i in range(0, total, BATCH_SIZE):
        batch = records[i:i + BATCH_SIZE]
        try:
            res = post_json(f"{RAILWAY_URL}/api/expedientes/bulk", batch)
            imported += res.get("importados", 0)
            skipped  += res.get("omitidos", 0)
            print(f"  [{i+len(batch)}/{total}] importados={res.get('importados',0)}  omitidos={res.get('omitidos',0)}")
        except Exception as e:
            print(f"  ERROR en batch {i}: {e}", file=sys.stderr)

    print(f"\nListo. Total importados: {imported}  omitidos (ya existían): {skipped}")

if __name__ == "__main__":
    main()
