#!/usr/bin/env python3
"""
Migración Railway → Railway (Carga)
Lee todos los expedientes del servicio ORIGEN y los escribe en el servicio DESTINO.

Uso:
    python3 migrate_railway_to_railway.py DEST_URL

Ejemplo:
    python3 migrate_railway_to_railway.py https://carga-nuevo-production.up.railway.app

ORIGEN siempre es https://carga.up.railway.app (producción actual).
DESTINO es el nuevo servicio vacío en miraculous-education.
"""
import json
import sys
import urllib.request

ORIGIN_URL = "https://carga.up.railway.app"
BATCH_SIZE = 200

ALL_COLS = [
    "num_exp", "fecha_instruccion", "fecha_aval_recibido", "fecha_tasacion_recibida",
    "fecha_carga", "titular", "provincia", "nombre_proyecto", "linea_programatica",
    "monto", "garantia", "tec_de_carga", "paso_carga_inicial", "se_solicito",
    "tecnico", "observaciones", "deleted_at", "cuit", "avalista", "estado_pei",
    "linea_manual", "fecha_respuesta_uep", "devolvio_legales",
    "motivo_devolucion_legales", "observaciones_carga",
]


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read())


def post_json(url, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def fetch_all_active(origin):
    """Fetches all active expedientes from origin (paginates if needed)."""
    records = []
    offset = 0
    limit = 500
    while True:
        url = f"{origin}/api/expedientes?limit={limit}&offset={offset}"
        resp = fetch_json(url)
        batch = resp.get("data", [])
        records.extend(batch)
        total = resp.get("total", 0)
        offset += len(batch)
        print(f"  Activos: {offset}/{total} obtenidos...")
        if offset >= total or not batch:
            break
    return records


def fetch_papelera(origin):
    """Fetches all soft-deleted expedientes from origin."""
    resp = fetch_json(f"{origin}/api/expedientes/papelera")
    if isinstance(resp, list):
        return resp
    return []


def slim(record):
    """Keeps only the columns accepted by the bulk endpoint."""
    return {k: record.get(k) for k in ALL_COLS}


def push_batch(dest, records):
    imported = 0
    errors = 0
    for i in range(0, len(records), BATCH_SIZE):
        batch = [slim(r) for r in records[i:i + BATCH_SIZE]]
        try:
            res = post_json(f"{dest}/api/expedientes/bulk", batch)
            imported += res.get("importados", 0)
            errs = res.get("errores", [])
            if errs:
                errors += len(errs)
                print(f"    Errores en batch {i}: {errs[:3]}")
            print(f"  [{i + len(batch)}/{len(records)}] importados={res.get('importados', 0)}")
        except Exception as e:
            print(f"  ERROR en batch {i}: {e}", file=sys.stderr)
    return imported, errors


def main():
    if len(sys.argv) < 2:
        print("Uso: python3 migrate_railway_to_railway.py DEST_URL")
        print("Ejemplo: python3 migrate_railway_to_railway.py https://carga-nuevo-production.up.railway.app")
        sys.exit(1)

    dest_url = sys.argv[1].rstrip("/")
    print(f"\nOrigen: {ORIGIN_URL}")
    print(f"Destino: {dest_url}")
    print()

    # Verificar que el destino responde
    try:
        fetch_json(f"{dest_url}/api/expedientes?limit=1")
        print("Destino responde OK.")
    except Exception as e:
        print(f"Error conectando al destino: {e}")
        sys.exit(1)

    # Obtener registros activos
    print("\n[1/2] Obteniendo expedientes activos del origen...")
    activos = fetch_all_active(ORIGIN_URL)
    print(f"  Total activos: {len(activos)}")

    # Obtener papelera (borrados)
    print("\n[2/2] Obteniendo papelera del origen...")
    papelera = fetch_papelera(ORIGIN_URL)
    print(f"  Total en papelera: {len(papelera)}")

    todos = activos + papelera
    print(f"\nTotal a migrar: {len(todos)} registros")
    print()

    # Enviar al destino
    print("Enviando al destino...")
    importados, errores = push_batch(dest_url, todos)

    print(f"\n✓ Migración completa.")
    print(f"  Importados: {importados}")
    print(f"  Errores:    {errores}")

    # Verificar en destino
    print("\nVerificando en destino...")
    resp = fetch_json(f"{dest_url}/api/expedientes?limit=1")
    total_dest = resp.get("total", 0)
    print(f"  Expedientes activos en destino: {total_dest}")
    print(f"  Expedientes activos en origen:  {len(activos)}")
    if total_dest == len(activos):
        print("  ✓ Conteos coinciden.")
    else:
        print(f"  ⚠ Diferencia: {abs(total_dest - len(activos))} registros.")


if __name__ == "__main__":
    main()
