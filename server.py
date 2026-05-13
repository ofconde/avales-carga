#!/usr/bin/env python3
import http.server
import json
import sqlite3
import os
import threading
from datetime import datetime
from pathlib import Path
import urllib.parse
import urllib.request

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
        # Campos del Tablero de Carga (agregados en etapa 2)
        existing = [r[1] for r in conn.execute("PRAGMA table_info(expedientes)").fetchall()]
        new_cols = [
            ("cuit",                       "TEXT DEFAULT ''"),
            ("avalista",                   "TEXT DEFAULT ''"),
            ("estado_pei",                 "TEXT DEFAULT ''"),
            ("linea_manual",               "TEXT DEFAULT ''"),
            ("fecha_respuesta_uep",        "TEXT"),
            ("devolvio_legales",           "TEXT DEFAULT ''"),
            ("motivo_devolucion_legales",  "TEXT DEFAULT ''"),
            ("observaciones_carga",        "TEXT DEFAULT ''"),
        ]
        for col, typedef in new_cols:
            if col not in existing:
                conn.execute(f"ALTER TABLE expedientes ADD COLUMN {col} {typedef}")
        # Normalizar paso_carga_inicial → 'SI' / 'NO' / ''
        conn.execute("""
            UPDATE expedientes SET paso_carga_inicial = 'SI'
            WHERE UPPER(TRIM(paso_carga_inicial)) IN ('SI','SÍ','S','YES','1','TRUE')
              AND paso_carga_inicial NOT IN ('SI','')
        """)
        conn.execute("""
            UPDATE expedientes SET paso_carga_inicial = 'NO'
            WHERE UPPER(TRIM(paso_carga_inicial)) IN ('NO','N','NOT','0','FALSE')
              AND paso_carga_inicial NOT IN ('NO','')
        """)

def get_config():
    with open(CONFIG_PATH, encoding='utf-8') as f:
        return json.load(f)

# ── Helpers de monto ──────────────────────────────────────────────────────────
import re as _re

def parse_monto(s):
    """Convierte "$ 4.699.552" → 4699552.0 (formato argentino)."""
    if not s:
        return 0.0
    s = _re.sub(r'[^\d.,]', '', str(s))
    if not s:
        return 0.0
    if ',' in s:
        s = s.replace('.', '').replace(',', '.')
    else:
        s = s.replace('.', '')
    try:
        return float(s)
    except Exception:
        return 0.0

def fmt_monto_json(n):
    try:
        n = float(n)
        return f"$ {int(n):,}".replace(',', '.')
    except Exception:
        return "$ 0"

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
        if path in ('/carga', '/carga/'):
            return self.serve_file(PUBLIC_DIR / 'carga.html')
        if not path.startswith('/api/'):
            fp = PUBLIC_DIR / path.lstrip('/')
            if fp.exists() and fp.is_file():
                return self.serve_file(fp)

        # ── API expedientes (tablero general) ──────────────────────────────
        if path == '/api/expedientes':            return self._list(qs)
        if path == '/api/expedientes/papelera':   return self._papelera()
        if path == '/api/config/provincias':      return self._provincias()
        if path == '/api/config/analistas':       return self._analistas()
        if path == '/api/config/garantias':        return self._garantias()
        if path == '/api/config/lineas':           return self._lineas()
        if path == '/api/config/tecnicos':         return self._tecnicos()
        if path == '/api/config/tecnicos_carga':   return self._tecnicos_carga_get()

        if path.startswith('/api/expedientes/'):
            eid = path.split('/')[-1]
            if eid.isdigit():
                return self._get_one(eid)

        # ── API Tablero de Carga ────────────────────────────────────────────
        if path == '/api/carga/mes':              return self._carga_mes(qs)
        if path == '/api/carga/stats':            return self._carga_stats(qs)
        if path == '/api/carga/anual':            return self._carga_anual(qs)
        if path == '/api/carga/meses':            return self._carga_meses()
        if path == '/api/carga/buscar':           return self._carga_buscar(qs)

        self.send_response(404); self.end_headers()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/api/expedientes':            return self._create()
        if path == '/api/expedientes/bulk':       return self._bulk()
        if path == '/api/carga':                  return self._carga_crear()
        if path == '/api/config/tecnicos_carga':  return self._tecnicos_carga_post()

        if path.startswith('/api/expedientes/') and path.endswith('/restaurar'):
            parts = path.split('/')
            if len(parts) == 5 and parts[3].isdigit():
                return self._restaurar(parts[3])

        self.send_response(404); self.end_headers()

    def do_PUT(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith('/api/expedientes/'):
            return self._update(path.split('/')[-1])
        if path.startswith('/api/carga/'):
            eid = path.split('/')[-1]
            if eid.isdigit():
                return self._carga_update(eid)
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

    def _tecnicos_carga_get(self):
        try:
            cfg = get_config()
            self.send_json(cfg.get('tecnicos_carga', []))
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def _tecnicos_carga_post(self):
        try:
            d = self.read_body()
            action = d.get('action')
            cfg = get_config()
            lista = cfg.get('tecnicos_carga', [])
            if action == 'add':
                nombre = (d.get('nombre') or '').strip().upper()
                if not nombre: return self.send_json({'error': 'nombre requerido'}, 400)
                if nombre not in lista:
                    lista.append(nombre)
                    lista.sort()
            elif action == 'remove':
                nombre = (d.get('nombre') or '').strip().upper()
                lista = [x for x in lista if x != nombre]
            elif action == 'update':
                old = (d.get('old') or '').strip().upper()
                new = (d.get('new') or '').strip().upper()
                lista = [new if x == old else x for x in lista]
                lista.sort()
            cfg['tecnicos_carga'] = lista
            with open(str(CONFIG_PATH), 'w') as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            self.send_json(lista)
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

# ── Tablero de Carga — API ────────────────────────────────────────────────────

    CARGA_FIELDS = [
        'cuit', 'tec_de_carga', 'estado_pei', 'linea_manual',
        'fecha_respuesta_uep', 'devolvio_legales',
        'motivo_devolucion_legales', 'observaciones_carga',
        'paso_carga_inicial', 'se_solicito', 'tecnico',
        'fecha_carga', 'titular', 'provincia', 'monto', 'garantia', 'linea_programatica',
    ]

    def _carga_mes(self, qs):
        try:
            mes       = (qs.get('mes')      or [None])[0]
            provincia = (qs.get('provincia')or [None])[0]
            linea     = (qs.get('linea')    or [None])[0]
            garantia  = (qs.get('garantia') or [None])[0]
            paso      = (qs.get('paso')     or [None])[0]
            legales   = (qs.get('legales')  or [None])[0]
            search    = (qs.get('search')   or [None])[0]
            limit     = int((qs.get('limit') or [200])[0])
            offset    = int((qs.get('offset')or [0])[0])

            if not mes:
                from datetime import date
                mes = date.today().strftime('%Y-%m')

            q = "SELECT * FROM expedientes WHERE deleted_at IS NULL AND substr(fecha_carga,1,7) = ?"
            p = [mes]

            tec      = (qs.get('tec')      or [None])[0]
            tec_uep  = (qs.get('tec_uep')  or [None])[0]

            if provincia: q += " AND provincia = ?";          p.append(provincia)
            if linea:     q += " AND (linea_programatica = ? OR linea_manual = ?)"; p += [linea, linea]
            if garantia:  q += " AND garantia = ?";           p.append(garantia)
            if paso:      q += " AND paso_carga_inicial = ?"; p.append(paso)
            if legales:   q += " AND devolvio_legales = ?";   p.append(legales)
            if tec:       q += " AND tec_de_carga = ?";       p.append(tec)
            if tec_uep:   q += " AND tecnico LIKE ?";         p.append(f"%{tec_uep}%")
            if search:
                q += " AND (num_exp LIKE ? OR titular LIKE ? OR cuit LIKE ?)"
                s = f"%{search}%"; p += [s, s, s]

            total_q  = q.replace("SELECT *", "SELECT COUNT(*)", 1)
            data_q   = q + " ORDER BY fecha_carga DESC, id DESC LIMIT ? OFFSET ?"

            with get_db() as conn:
                total = conn.execute(total_q, p).fetchone()[0]
                rows  = conn.execute(data_q, p + [limit, offset]).fetchall()

            self.send_json({
                'mes':    mes,
                'data':   [dict(r) for r in rows],
                'total':  total,
                'limit':  limit,
                'offset': offset,
            })
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def _carga_stats(self, qs):
        try:
            mes = (qs.get('mes') or [None])[0]
            if not mes:
                from datetime import date
                mes = date.today().strftime('%Y-%m')

            base = "FROM expedientes WHERE deleted_at IS NULL AND substr(fecha_carga,1,7) = ?"
            p    = [mes]

            def agg(conn, group_col):
                rows = conn.execute(f"""
                    SELECT COALESCE(NULLIF({group_col},''), '(Sin datos)') as grupo,
                           COUNT(*) as total
                    {base} GROUP BY grupo ORDER BY total DESC
                """, p).fetchall()
                return [{'grupo': r[0], 'total': r[1]} for r in rows]

            with get_db() as conn:
                total    = conn.execute(f"SELECT COUNT(*) {base}", p).fetchone()[0]
                montos   = [parse_monto(r[0]) for r in conn.execute(
                    f"SELECT monto {base}", p).fetchall()]
                monto_total = sum(montos)
                promedio    = round(monto_total / len(montos), 0) if montos else 0

                paso_si  = conn.execute(f"SELECT COUNT(*) {base} AND paso_carga_inicial='SI'", p).fetchone()[0]
                paso_no  = conn.execute(f"SELECT COUNT(*) {base} AND paso_carga_inicial='NO'", p).fetchone()[0]
                leg_si   = conn.execute(f"SELECT COUNT(*) {base} AND devolvio_legales='SI'", p).fetchone()[0]
                pend_uep = conn.execute(f"SELECT COUNT(*) {base} AND tecnico != '' AND (fecha_respuesta_uep IS NULL OR fecha_respuesta_uep='')", p).fetchone()[0]

                by_prov    = agg(conn, 'provincia')
                by_linea   = agg(conn, "CASE WHEN linea_manual != '' THEN linea_manual ELSE linea_programatica END")
                by_garantia= agg(conn, 'garantia')
                by_tec     = agg(conn, 'tec_de_carga')

                # Stats detalladas por técnico de carga
                tec_detail = conn.execute(f"""
                    SELECT COALESCE(NULLIF(tec_de_carga,''),'(Sin asignar)') as tec,
                           COUNT(*) as total,
                           SUM(CASE WHEN paso_carga_inicial='SI' THEN 1 ELSE 0 END) as paso_si,
                           SUM(CASE WHEN paso_carga_inicial='NO' THEN 1 ELSE 0 END) as paso_no,
                           SUM(CASE WHEN devolvio_legales='SI' THEN 1 ELSE 0 END) as legales_si,
                           monto
                    {base} GROUP BY tec ORDER BY total DESC
                """, p).fetchall()
                by_tec_detail = []
                for row in tec_detail:
                    tec_name = row[0]
                    # sum montos for this tech
                    montos_tec = [parse_monto(r[0]) for r in conn.execute(
                        f"SELECT monto {base} AND COALESCE(NULLIF(tec_de_carga,''),'(Sin asignar)')=?",
                        p + [tec_name]
                    ).fetchall()]
                    mt = sum(montos_tec)
                    by_tec_detail.append({
                        'tec': row[0], 'total': row[1],
                        'paso_si': row[2], 'paso_no': row[3], 'legales_si': row[4],
                        'monto_total': mt,
                        'promedio': round(mt/row[1], 0) if row[1] else 0,
                    })

                # Montos por provincia
                prov_montos = conn.execute(f"""
                    SELECT COALESCE(NULLIF(provincia,''),'(Sin datos)') as prov,
                           monto {base} ORDER BY prov
                """, p).fetchall()
                prov_m = {}
                for prov, m in prov_montos:
                    prov_m.setdefault(prov, 0)
                    prov_m[prov] += parse_monto(m)
                by_prov_monto = [{'grupo': k, 'monto': v} for k, v in sorted(prov_m.items(), key=lambda x: -x[1])]

                # Motivos más frecuentes (se_solicito)
                motivos = conn.execute(f"""
                    SELECT se_solicito, COUNT(*) as n {base}
                    AND se_solicito != '' AND paso_carga_inicial = 'NO'
                    GROUP BY se_solicito ORDER BY n DESC LIMIT 10
                """, p).fetchall()
                motivos_leg = conn.execute(f"""
                    SELECT motivo_devolucion_legales, COUNT(*) as n {base}
                    AND motivo_devolucion_legales != '' AND devolvio_legales = 'SI'
                    GROUP BY motivo_devolucion_legales ORDER BY n DESC LIMIT 10
                """, p).fetchall()

            self.send_json({
                'mes':          mes,
                'total':        total,
                'monto_total':  monto_total,
                'promedio':     promedio,
                'paso_si':      paso_si,
                'paso_no':      paso_no,
                'legales_si':   leg_si,
                'pend_uep':     pend_uep,
                'by_provincia': by_prov,
                'by_linea':     by_linea,
                'by_garantia':  by_garantia,
                'by_tecnico':   by_tec,
                'by_tec_detail': by_tec_detail,
                'by_prov_monto': by_prov_monto,
                'motivos_carga': [{'motivo': r[0], 'n': r[1]} for r in motivos],
                'motivos_legales': [{'motivo': r[0], 'n': r[1]} for r in motivos_leg],
            })
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def _carga_meses(self):
        try:
            with get_db() as conn:
                rows = conn.execute("""
                    SELECT substr(fecha_carga,1,7) as mes, COUNT(*) as total
                    FROM expedientes WHERE deleted_at IS NULL
                      AND fecha_carga IS NOT NULL AND fecha_carga != ''
                    GROUP BY mes ORDER BY mes DESC
                """).fetchall()
            self.send_json([{'mes': r[0], 'total': r[1]} for r in rows])
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    PEI_API     = os.environ.get('PEI_API_URL',  'https://recepcionavales.cfi.org.ar')
    PEI_API_KEY = os.environ.get('PEI_API_KEY',  'cfi-avales-sync-2026')

    def _pei_to_row(self, r):
        monto_raw = r.get('importeSolicitado')
        monto = f"$ {int(monto_raw):,}".replace(',', '.') if monto_raw else ''
        return {
            'id':                None,
            'num_exp':           r.get('expediente', ''),
            'titular':           r.get('razonSocial', ''),
            'cuit':              r.get('cuit', ''),
            'provincia':         r.get('provincia', ''),
            'monto':             monto,
            'garantia':          r.get('garantia_display', ''),
            'linea_programatica':r.get('linea_normalizada', ''),
            'estado_pei':        r.get('estadoExpediente', ''),
            'fecha_instruccion': (r.get('fechaSolicitud') or '')[:10],
            'fecha_carga':       (r.get('fechaSolicitud') or '')[:10],
            '_pei': True,
        }

    def _carga_buscar(self, qs):
        try:
            q_str = (qs.get('q') or [''])[0].strip()
            if len(q_str) < 2:
                return self.send_json([])

            # Buscar en DB local primero
            s = f"%{q_str}%"
            with get_db() as conn:
                rows = conn.execute("""
                    SELECT * FROM expedientes
                    WHERE deleted_at IS NULL
                      AND (num_exp LIKE ? OR titular LIKE ? OR cuit LIKE ?)
                    ORDER BY fecha_carga DESC LIMIT 20
                """, (s, s, s)).fetchall()
            local = [dict(r) for r in rows]
            local_nums = {r['num_exp'] for r in local}

            # Buscar en PEI para traer los que no están en DB local
            pei_only = []
            try:
                url = f"{self.PEI_API}/api/avales?search={urllib.parse.quote(q_str)}&limit=20"
                req = urllib.request.Request(url, headers={
                    'Accept': 'application/json',
                    'X-API-Key': self.PEI_API_KEY,
                })
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())
                for r in data.get('data', []):
                    num = r.get('expediente', '')
                    if num and num not in local_nums:
                        pei_only.append(self._pei_to_row(r))
            except Exception:
                pass

            self.send_json((local + pei_only)[:25])
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def _carga_anual(self, qs):
        try:
            from datetime import date
            year = (qs.get('year') or [str(date.today().year)])[0]
            with get_db() as conn:
                rows = conn.execute("""
                    SELECT substr(fecha_carga,1,7) as mes, monto
                    FROM expedientes
                    WHERE deleted_at IS NULL AND substr(fecha_carga,1,4) = ?
                      AND fecha_carga IS NOT NULL AND fecha_carga != ''
                    ORDER BY mes
                """, (year,)).fetchall()

            meses_data = {}
            for mes, monto in rows:
                if mes not in meses_data:
                    meses_data[mes] = {'mes': mes, 'total': 0, 'monto_total': 0}
                meses_data[mes]['total'] += 1
                meses_data[mes]['monto_total'] += parse_monto(monto)

            lista = sorted(meses_data.values(), key=lambda x: x['mes'])
            total_anual = sum(m['total'] for m in lista)
            monto_anual = sum(m['monto_total'] for m in lista)

            self.send_json({
                'year':        year,
                'total_anual': total_anual,
                'monto_anual': monto_anual,
                'por_mes':     lista,
            })
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def _carga_crear(self):
        try:
            from datetime import date as _date
            d = self.read_body()
            num_exp = (d.get('num_exp') or '').strip().upper()
            if not num_exp:
                return self.send_json({'error': 'num_exp es obligatorio'}, 400)
            if not d.get('fecha_carga'):
                d['fecha_carga'] = str(_date.today())
            fields = ['num_exp', 'titular', 'provincia', 'monto', 'garantia',
                      'linea_programatica', 'fecha_carga'] + self.CARGA_FIELDS
            seen = set(); unique_fields = []
            for f in fields:
                if f not in seen: seen.add(f); unique_fields.append(f)
            cols = [f for f in unique_fields if f in d or f == 'num_exp']
            vals = []
            for f in cols:
                if f == 'num_exp':
                    vals.append(num_exp)
                else:
                    vals.append(d.get(f) or None)
            placeholders = ','.join('?' * len(cols))
            col_names = ','.join(cols)
            with get_db() as conn:
                cur = conn.execute(
                    f"INSERT INTO expedientes ({col_names}) VALUES ({placeholders})",
                    vals
                )
                row = conn.execute("SELECT * FROM expedientes WHERE id=?", (cur.lastrowid,)).fetchone()
            self.send_json(dict(row), 201)
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def _carga_update(self, eid):
        try:
            d = self.read_body()
            campos = {k: d.get(k) for k in self.CARGA_FIELDS if k in d}
            if not campos:
                return self.send_json({'error': 'Sin campos para actualizar'}, 400)
            set_clause = ', '.join(f"{k}=?" for k in campos)
            vals = list(campos.values()) + [eid]
            with get_db() as conn:
                conn.execute(
                    f"UPDATE expedientes SET {set_clause}, updated_at=datetime('now','localtime') WHERE id=?",
                    vals
                )
                row = conn.execute("SELECT * FROM expedientes WHERE id=?", (eid,)).fetchone()
            if not row:
                self.send_response(404); self.end_headers(); return
            self.send_json(dict(row))
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
