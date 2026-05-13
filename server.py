#!/usr/bin/env python3
import http.server
import json
import sqlite3
import os
import threading
from datetime import datetime
from pathlib import Path
import urllib.parse

BASE_DIR  = Path(__file__).parent
_DATA_DIR = Path('/data') if Path('/data').exists() else BASE_DIR
DB_PATH   = _DATA_DIR / 'carga.db'
CONFIG_PATH = BASE_DIR / 'config.json'
PUBLIC_DIR  = BASE_DIR / 'public'

_db_write_lock = threading.Lock()

def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS expedientes (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                num_exp                 TEXT NOT NULL,
                fecha_instruccion       TEXT,
                fecha_aval_recibido     TEXT,
                fecha_tasacion_recibida TEXT,
                fecha_carga             TEXT,
                titular                 TEXT,
                provincia               TEXT,
                nombre_proyecto         TEXT,
                linea_programatica      TEXT,
                monto                   TEXT,
                garantia                TEXT,
                tec_de_carga            TEXT,
                paso_carga_inicial      TEXT,
                se_solicito             TEXT,
                tecnico                 TEXT,
                observaciones           TEXT DEFAULT '',
                deleted_at              TEXT,
                created_at              TEXT DEFAULT (datetime('now','localtime')),
                updated_at              TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_num_exp ON expedientes(num_exp);
        """)

def get_config():
    with open(CONFIG_PATH, encoding='utf-8') as f:
        return json.load(f)

# ── HTTP Handler ──────────────────────────────────────────────────────────────

CONTENT_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.css':  'text/css',
    '.js':   'application/javascript',
}

class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} — {fmt % args}")

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def serve_file(self, path):
        ext   = Path(path).suffix
        ctype = CONTENT_TYPES.get(ext, 'application/octet-stream')
        try:
            with open(path, 'rb') as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_response(404); self.end_headers()

    def read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        qs     = urllib.parse.parse_qs(parsed.query)

        if path in ('/', '/index.html'):
            return self.serve_file(PUBLIC_DIR / 'index.html')
        if not path.startswith('/api/'):
            fp = PUBLIC_DIR / path.lstrip('/')
            if fp.exists() and fp.is_file():
                return self.serve_file(fp)

        if path == '/api/expedientes':            return self._list(qs)
        if path == '/api/expedientes/papelera':   return self._papelera()
        if path == '/api/config/provincias':      return self._provincias()
        if path == '/api/config/analistas':       return self._analistas()
        if path == '/api/config/garantias':       return self._garantias()
        if path == '/api/config/lineas':          return self._lineas()
        if path == '/api/config/tecnicos':        return self._tecnicos()

        if path.startswith('/api/expedientes/'):
            eid = path.split('/')[-1]
            if eid.isdigit():
                return self._get_one(eid)

        self.send_response(404); self.end_headers()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/api/expedientes':       return self._create()
        if path == '/api/expedientes/bulk':  return self._bulk()

        if path.startswith('/api/expedientes/') and path.endswith('/restaurar'):
            parts = path.split('/')
            if len(parts) == 5 and parts[3].isdigit():
                return self._restaurar(parts[3])

        self.send_response(404); self.end_headers()

    def do_PUT(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith('/api/expedientes/'):
            return self._update(path.split('/')[-1])
        self.send_response(404); self.end_headers()

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith('/api/expedientes/'):
            return self._delete(path.split('/')[-1])
        self.send_response(404); self.end_headers()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_query(self, qs):
        q  = "SELECT * FROM expedientes WHERE deleted_at IS NULL"
        p  = []
        search    = (qs.get('search')    or [None])[0]
        provincia = (qs.get('provincia') or [None])[0]
        garantia  = (qs.get('garantia')  or [None])[0]
        linea     = (qs.get('linea')     or [None])[0]
        tecnico   = (qs.get('tecnico')   or [None])[0]
        analista  = (qs.get('analista')  or [None])[0]
        status    = (qs.get('status')    or [None])[0]

        if search:
            q += " AND (num_exp LIKE ? OR titular LIKE ? OR nombre_proyecto LIKE ?)"
            s  = f"%{search}%"; p += [s, s, s]
        if provincia:
            q += " AND provincia = ?"; p.append(provincia)
        if garantia:
            q += " AND garantia = ?"; p.append(garantia)
        if linea:
            q += " AND linea_programatica = ?"; p.append(linea)
        if tecnico:
            q += " AND tecnico = ?"; p.append(tecnico)
        if analista:
            cfg = get_config()
            obj = next((a for a in cfg.get('analistas', []) if a['nombre'] == analista), None)
            if obj and obj.get('provincias'):
                ph  = ','.join('?' * len(obj['provincias']))
                q  += f" AND provincia IN ({ph})"
                p  += obj['provincias']
            else:
                return None, None

        NO_RECIBIDO = "(fecha_aval_recibido IS NULL OR fecha_aval_recibido = '')"
        if status == 'recibido':
            q += f" AND NOT {NO_RECIBIDO}"
        elif status == 'pendiente':
            q += f" AND {NO_RECIBIDO}"
        elif status == 'sin_instruccion':
            q += f" AND (fecha_instruccion IS NULL OR fecha_instruccion = '')"
        elif status == 'sin_tasacion':
            q += f" AND NOT {NO_RECIBIDO} AND (fecha_tasacion_recibida IS NULL OR fecha_tasacion_recibida = '')"

        return q, p

    def _list(self, qs):
        try:
            q, p = self._build_query(qs)
            if q is None:
                return self.send_json({'data': [], 'total': 0, 'limit': 200, 'offset': 0, 'stats': {}})

            limit  = int((qs.get('limit')  or [200])[0])
            offset = int((qs.get('offset') or [0])[0])

            NO_REC = "(fecha_aval_recibido IS NULL OR fecha_aval_recibido = '')"
            cq = q.replace("SELECT *", "SELECT COUNT(*)", 1)

            q_data = q + " ORDER BY CAST(SUBSTR(num_exp, -6) AS INTEGER) DESC LIMIT ? OFFSET ?"

            with get_db() as conn:
                total      = conn.execute(cq, p).fetchone()[0]
                recibidos  = conn.execute(cq + f" AND NOT {NO_REC}", p).fetchone()[0]
                sin_instr  = conn.execute(cq + " AND (fecha_instruccion IS NULL OR fecha_instruccion = '')", p).fetchone()[0]
                sin_tasac  = conn.execute(cq + f" AND NOT {NO_REC} AND (fecha_tasacion_recibida IS NULL OR fecha_tasacion_recibida = '')", p).fetchone()[0]
                rows       = conn.execute(q_data, p + [limit, offset]).fetchall()

            self.send_json({
                'data':   [dict(r) for r in rows],
                'total':  total,
                'limit':  limit,
                'offset': offset,
                'stats': {
                    'total':        total,
                    'recibidos':    recibidos,
                    'pendientes':   total - recibidos,
                    'sin_instruccion': sin_instr,
                    'sin_tasacion': sin_tasac,
                },
            })
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def _get_one(self, eid):
        try:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT * FROM expedientes WHERE id=? AND deleted_at IS NULL", (eid,)
                ).fetchone()
            if not row:
                self.send_response(404); self.end_headers(); return
            self.send_json(dict(row))
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def _create(self):
        try:
            d = self.read_body()
            with _db_write_lock:
                conn = get_db()
                try:
                    cur = conn.execute("""
                        INSERT INTO expedientes
                            (num_exp, fecha_instruccion, fecha_aval_recibido, fecha_tasacion_recibida,
                             fecha_carga, titular, provincia, nombre_proyecto, linea_programatica,
                             monto, garantia, tec_de_carga, paso_carga_inicial, se_solicito,
                             tecnico, observaciones)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        (d.get('num_exp') or '').strip().upper(),
                        d.get('fecha_instruccion') or None,
                        d.get('fecha_aval_recibido') or None,
                        d.get('fecha_tasacion_recibida') or None,
                        d.get('fecha_carga') or None,
                        d.get('titular', ''),
                        d.get('provincia', ''),
                        d.get('nombre_proyecto', ''),
                        d.get('linea_programatica', ''),
                        d.get('monto', ''),
                        d.get('garantia', ''),
                        d.get('tec_de_carga', ''),
                        d.get('paso_carga_inicial', ''),
                        d.get('se_solicito', ''),
                        d.get('tecnico', ''),
                        d.get('observaciones', ''),
                    ))
                    row = conn.execute("SELECT * FROM expedientes WHERE id=?", (cur.lastrowid,)).fetchone()
                    conn.commit()
                finally:
                    conn.close()
            self.send_json(dict(row))
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def _update(self, eid):
        try:
            d = self.read_body()
            with _db_write_lock:
                conn = get_db()
                try:
                    conn.execute("""
                        UPDATE expedientes SET
                            num_exp=?, fecha_instruccion=?, fecha_aval_recibido=?,
                            fecha_tasacion_recibida=?, fecha_carga=?, titular=?,
                            provincia=?, nombre_proyecto=?, linea_programatica=?,
                            monto=?, garantia=?, tec_de_carga=?, paso_carga_inicial=?,
                            se_solicito=?, tecnico=?, observaciones=?,
                            updated_at=datetime('now','localtime')
                        WHERE id=?
                    """, (
                        (d.get('num_exp') or '').strip().upper(),
                        d.get('fecha_instruccion') or None,
                        d.get('fecha_aval_recibido') or None,
                        d.get('fecha_tasacion_recibida') or None,
                        d.get('fecha_carga') or None,
                        d.get('titular', ''),
                        d.get('provincia', ''),
                        d.get('nombre_proyecto', ''),
                        d.get('linea_programatica', ''),
                        d.get('monto', ''),
                        d.get('garantia', ''),
                        d.get('tec_de_carga', ''),
                        d.get('paso_carga_inicial', ''),
                        d.get('se_solicito', ''),
                        d.get('tecnico', ''),
                        d.get('observaciones', ''),
                        eid,
                    ))
                    row = conn.execute("SELECT * FROM expedientes WHERE id=? AND deleted_at IS NULL", (eid,)).fetchone()
                    conn.commit()
                finally:
                    conn.close()
            if not row:
                self.send_response(404); self.end_headers(); return
            self.send_json(dict(row))
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def _delete(self, eid):
        try:
            with get_db() as conn:
                conn.execute(
                    "UPDATE expedientes SET deleted_at=datetime('now','localtime') WHERE id=?", (eid,)
                )
            self.send_json({'ok': True})
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def _restaurar(self, eid):
        try:
            with get_db() as conn:
                conn.execute("UPDATE expedientes SET deleted_at=NULL WHERE id=?", (eid,))
            self.send_json({'ok': True})
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def _papelera(self):
        try:
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT * FROM expedientes WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC"
                ).fetchall()
            self.send_json([dict(r) for r in rows])
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def _bulk(self):
        try:
            rows = self.read_body()
            if not isinstance(rows, list):
                return self.send_json({'error': 'Se esperaba un array'}, 400)

            ok = 0; skipped = 0; errors = []

            with _db_write_lock:
                conn = get_db()
                try:
                    existentes = set(
                        r[0] for r in conn.execute("SELECT num_exp FROM expedientes").fetchall()
                    )
                    for i, d in enumerate(rows):
                        exp = (d.get('num_exp') or '').strip().upper()
                        if not exp or exp in existentes:
                            skipped += 1; continue
                        try:
                            conn.execute("""
                                INSERT INTO expedientes
                                    (num_exp, fecha_instruccion, fecha_aval_recibido,
                                     fecha_tasacion_recibida, fecha_carga, titular, provincia,
                                     nombre_proyecto, linea_programatica, monto, garantia,
                                     tec_de_carga, paso_carga_inicial, se_solicito, tecnico, observaciones)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """, (
                                exp,
                                d.get('fecha_instruccion') or None,
                                d.get('fecha_aval_recibido') or None,
                                d.get('fecha_tasacion_recibida') or None,
                                d.get('fecha_carga') or None,
                                d.get('titular', ''),
                                d.get('provincia', ''),
                                d.get('nombre_proyecto', ''),
                                d.get('linea_programatica', ''),
                                d.get('monto', ''),
                                d.get('garantia', ''),
                                d.get('tec_de_carga', ''),
                                d.get('paso_carga_inicial', ''),
                                d.get('se_solicito', ''),
                                d.get('tecnico', ''),
                                d.get('observaciones', ''),
                            ))
                            existentes.add(exp)
                            ok += 1
                        except Exception as e:
                            errors.append({'row': i + 2, 'msg': str(e)})
                        if (ok + skipped) % 200 == 0:
                            conn.commit()
                    conn.commit()
                finally:
                    conn.close()

            self.send_json({'ok': True, 'importados': ok, 'omitidos': skipped, 'errores': errors})
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def _provincias(self):
        try:
            self.send_json(get_config().get('provincias', {}))
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def _analistas(self):
        try:
            self.send_json(get_config().get('analistas', []))
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def _garantias(self):
        try:
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT garantia FROM expedientes WHERE deleted_at IS NULL AND garantia != '' ORDER BY garantia"
                ).fetchall()
            self.send_json([r[0] for r in rows])
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def _lineas(self):
        try:
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT linea_programatica FROM expedientes WHERE deleted_at IS NULL AND linea_programatica != '' ORDER BY linea_programatica"
                ).fetchall()
            self.send_json([r[0] for r in rows])
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def _tecnicos(self):
        try:
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT tecnico FROM expedientes WHERE deleted_at IS NULL AND tecnico != '' ORDER BY tecnico"
                ).fetchall()
            self.send_json([r[0] for r in rows])
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

# ── Arranque ──────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 3001))
    server = http.server.ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print(f"\n  Sistema de Carga de Expedientes CFI")
    print(f"  Corriendo en http://localhost:{port}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Servidor detenido.")
