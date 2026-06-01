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

VERSION   = "1.1.0"

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
        # Normalizar tec_de_carga: apodos → nombres completos canónicos
        for informal, canonico in _TECNICO_INFORMAL_MAP.items():
            conn.execute(
                "UPDATE expedientes SET tec_de_carga=? WHERE UPPER(TRIM(tec_de_carga))=UPPER(?)",
                (canonico, informal)
            )
        # Corregir provincia usando el código embebido en num_exp (fuente de verdad)
        # Ejemplo: 2025-CR-CO-001794 → CO (Córdoba), no CB (Chubut)
        rows_prov = conn.execute("SELECT id, num_exp, provincia FROM expedientes").fetchall()
        for r in rows_prov:
            correct = _prov_from_num_exp(r[1])
            if not correct:
                # Fallback: mapear nombre completo → código
                correct = _PROV_NOMBRE_A_CODIGO.get((r[2] or '').lower().strip(), '')
            if correct and r[2] != correct:
                conn.execute("UPDATE expedientes SET provincia=? WHERE id=?", (correct, r[0]))
        # Tabla de aprobados por director (agregada/actualizada via POST /api/aprobados-director)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS aprobados_director (
                mes         TEXT PRIMARY KEY,
                cantidad    INTEGER DEFAULT 0,
                monto_total REAL    DEFAULT 0
            )
        """)
        # Normalizar garantías: aliases y variantes (CASFOG Serie*, GARANTIZAR, CUYO AVAL, etc.)
        rows_gar = conn.execute("SELECT id, garantia FROM expedientes").fetchall()
        for r in rows_gar:
            new_g = _norm_garantia(r[1])
            if new_g != r[1]:
                conn.execute("UPDATE expedientes SET garantia=? WHERE id=?", (new_g, r[0]))

def get_config():
    with open(CONFIG_PATH, encoding='utf-8') as f:
        return json.load(f)

# ── Normalización de campos ────────────────────────────────────────────────────

# Mapa de nombres completos de provincia → código
_PROV_NOMBRE_A_CODIGO = {
    'buenos aires':               'BA',
    'buenos aires-gba':           'BA',
    'buenos aires (gba)':         'BA',
    'catamarca':                  'CA',
    'chaco':                      'CH',
    'chubut':                     'CB',
    'córdoba':                    'CO',
    'cordoba':                    'CO',
    'corrientes':                 'CT',
    'entre ríos':                 'ER',
    'entre rios':                 'ER',
    'formosa':                    'FO',
    'jujuy':                      'JU',
    'la pampa':                   'LP',
    'la rioja':                   'LR',
    'mendoza':                    'MZ',
    'misiones':                   'MI',
    'neuquén':                    'NQ',
    'neuquen':                    'NQ',
    'río negro':                  'RN',
    'rio negro':                  'RN',
    'salta':                      'SA',
    'san juan':                   'SJ',
    'san luis':                   'SL',
    'santa cruz':                 'SC',
    'santa fe':                   'SF',
    'santiago del estero':        'SE',
    'tierra del fuego':           'TF',
    'tucumán':                    'TU',
    'tucuman':                    'TU',
}

# Mapa PEI "Apellido, Nombre" → nombre canónico en Carga
_TECNICO_PEI_MAP = {
    'garcía, maría josé':   'MARIA JOSE GARCIA',
    'garcia, maria jose':   'MARIA JOSE GARCIA',
    'carrion, gabriela':    'GABRIELA CARRION',
    'lloveras, jimena':     'JIMENA LLOVERAS',
    'tello, mauro':         'MAURO TELLO',
    'conde, omar francisco':'OMAR CONDE',
}

# Mapa de apodos/nombres informales → nombre canónico para corregir históricos
_TECNICO_INFORMAL_MAP = {
    'majo':         'MARIA JOSE GARCIA',
    'gaby':         'GABRIELA CARRION',
    'jimena':       'JIMENA LLOVERAS',
    'mauro':        'MAURO TELLO',
    'omar':         'OMAR CONDE',
}

def _norm_tecnico_pei(nombre_pei):
    """Convierte 'García, María José' (formato PEI) al nombre canónico de Carga."""
    key = nombre_pei.strip().lower()
    return _TECNICO_PEI_MAP.get(key, nombre_pei.strip())

_CODIGOS_VALIDOS = set(_PROV_NOMBRE_A_CODIGO.values())

def _prov_from_num_exp(num_exp):
    """Extrae el código de provincia del número de expediente (YYYY-CR-XX-NNNNNN → XX)."""
    s = (num_exp or '').strip().upper()
    # Formato esperado: 2025-CR-CO-001794
    parts = s.split('-')
    if len(parts) >= 3:
        code = parts[2]
        if code in _CODIGOS_VALIDOS:
            return code
    return ''

def _norm_provincia(v, num_exp=None):
    """Normaliza provincia: prioriza el código extraído del num_exp (fuente de verdad),
    luego intenta mapear el valor ingresado manualmente."""
    # Fuente de verdad: código embebido en el número de expediente
    if num_exp:
        from_exp = _prov_from_num_exp(num_exp)
        if from_exp:
            return from_exp
    if not v:
        return v
    mapped = _PROV_NOMBRE_A_CODIGO.get(v.strip().lower())
    return mapped if mapped else v.strip()

_GARANTIA_ALIAS = {
    # GARANTIZAR: cualquier variante → GARANTIZAR SGR
    'GARANTIZAR':                 'GARANTIZAR SGR',
    # CUYO AVAL: variantes → CUYO AVAL SGR
    'CUYO AVAL':                  'CUYO AVAL SGR',
    'CUYO AVAL FAE MENDOZA':      'CUYO AVAL SGR',
    # Correcciones menores de capitalización ya manejadas por upper()
}

def _norm_garantia(v):
    """Normaliza garantía: uppercase + colapsa variantes CASFOG / aliases."""
    if not v:
        return v
    v = v.strip().upper()
    if v.startswith('CASFOG'):
        return 'CASFOG'
    return _GARANTIA_ALIAS.get(v, v)

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

        if path in ('/', '/carga', '/carga/'):
            return self.serve_file(PUBLIC_DIR / 'carga.html')
        if path in ('/informe-mensual', '/informe-mensual/'):
            return self.serve_file(PUBLIC_DIR / 'informe-mensual.html')
        if not path.startswith('/api/'):
            fp = PUBLIC_DIR / path.lstrip('/')
            if fp.exists() and fp.is_file():
                return self.serve_file(fp)

        # ── API expedientes (tablero general) ──────────────────────────────
        if path == '/api/version':                return self.send_json({"version": VERSION})
        if path == '/api/expedientes':            return self._list(qs)
        if path == '/api/expedientes/papelera':   return self._papelera()
        if path == '/api/admin/normalizar':       return self._normalizar_datos()
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
        if path == '/api/carga/stats_mensuales':  return self._carga_stats_mensuales(qs)
        if path == '/api/carga/anual':            return self._carga_anual(qs)
        if path == '/api/carga/meses':            return self._carga_meses()
        if path == '/api/carga/buscar':           return self._carga_buscar(qs)
        if path == '/api/aprobados-director':     return self._get_aprobados()

        # ── Backup ─────────────────────────────────────────────────────────
        if path == '/api/backup/db':              return self._backup_db(qs)
        if path == '/api/debug/buscar':           return self._debug_buscar(qs)

        self.send_response(404); self.end_headers()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/api/expedientes':            return self._create()
        if path == '/api/expedientes/bulk':       return self._bulk()
        if path == '/api/carga':                  return self._carga_crear()
        if path == '/api/config/tecnicos_carga':  return self._tecnicos_carga_post()
        if path == '/api/aprobados-director':     return self._post_aprobados()

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
        if path.startswith('/api/expedientes/') and path.endswith('/permanente'):
            return self._eliminar_permanente(path.split('/')[-2])
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

            q_data = q + " ORDER BY num_exp DESC LIMIT ? OFFSET ?"

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
                    num_exp_norm = (d.get('num_exp') or '').strip().upper()
                    # Verificar si ya existe antes de insertar
                    existing = conn.execute(
                        "SELECT * FROM expedientes WHERE num_exp=?", (num_exp_norm,)
                    ).fetchone()
                    if existing:
                        conn.close()
                        self.send_json({
                            'error': f'El expediente {num_exp_norm} ya existe en el sistema.',
                            'existing': dict(existing),
                            'already_exists': True
                        }, 409)
                        return
                    cur = conn.execute("""
                        INSERT INTO expedientes
                            (num_exp, fecha_instruccion, fecha_aval_recibido, fecha_tasacion_recibida,
                             fecha_carga, titular, provincia, nombre_proyecto, linea_programatica,
                             monto, garantia, tec_de_carga, paso_carga_inicial, se_solicito,
                             tecnico, observaciones)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        num_exp_norm,
                        d.get('fecha_instruccion') or None,
                        d.get('fecha_aval_recibido') or None,
                        d.get('fecha_tasacion_recibida') or None,
                        d.get('fecha_carga') or None,
                        d.get('titular', ''),
                        _norm_provincia(d.get('provincia', ''), d.get('num_exp')),
                        d.get('nombre_proyecto', ''),
                        d.get('linea_programatica', ''),
                        d.get('monto', ''),
                        _norm_garantia(d.get('garantia', '')),
                        d.get('tec_de_carga', ''),
                        d.get('paso_carga_inicial', ''),
                        d.get('se_solicito', ''),
                        d.get('tecnico', ''),
                        d.get('observaciones', ''),
                    ))
                    row = conn.execute("SELECT * FROM expedientes WHERE id=?", (cur.lastrowid,)).fetchone()
                    conn.commit()
                finally:
                    if conn:
                        try: conn.close()
                        except: pass
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
                        _norm_provincia(d.get('provincia', ''), d.get('num_exp')),
                        d.get('nombre_proyecto', ''),
                        d.get('linea_programatica', ''),
                        d.get('monto', ''),
                        _norm_garantia(d.get('garantia', '')),
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

    def _eliminar_permanente(self, eid):
        try:
            with get_db() as conn:
                conn.execute("DELETE FROM expedientes WHERE id=?", (eid,))
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
        """
        Bulk import con todas las columnas via INSERT OR REPLACE.
        Acepta registros activos y borrados (deleted_at).
        Idempotente: se puede correr más de una vez.
        """
        try:
            rows = self.read_body()
            if not isinstance(rows, list):
                return self.send_json({'error': 'Se esperaba un array'}, 400)

            ok = 0; errors = []

            with _db_write_lock:
                conn = get_db()
                try:
                    for i, d in enumerate(rows):
                        exp = (d.get('num_exp') or '').strip().upper()
                        if not exp:
                            continue
                        try:
                            conn.execute("""
                                INSERT OR REPLACE INTO expedientes
                                    (num_exp, fecha_instruccion, fecha_aval_recibido,
                                     fecha_tasacion_recibida, fecha_carga, titular, provincia,
                                     nombre_proyecto, linea_programatica, monto, garantia,
                                     tec_de_carga, paso_carga_inicial, se_solicito, tecnico,
                                     observaciones, deleted_at, cuit, avalista, estado_pei,
                                     linea_manual, fecha_respuesta_uep, devolvio_legales,
                                     motivo_devolucion_legales, observaciones_carga)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """, (
                                exp,
                                d.get('fecha_instruccion') or None,
                                d.get('fecha_aval_recibido') or None,
                                d.get('fecha_tasacion_recibida') or None,
                                d.get('fecha_carga') or None,
                                d.get('titular', ''),
                                _norm_provincia(d.get('provincia', ''), exp),
                                d.get('nombre_proyecto', ''),
                                d.get('linea_programatica', ''),
                                d.get('monto', ''),
                                d.get('garantia', ''),
                                d.get('tec_de_carga', ''),
                                d.get('paso_carga_inicial', ''),
                                d.get('se_solicito', ''),
                                d.get('tecnico', ''),
                                d.get('observaciones', ''),
                                d.get('deleted_at') or None,
                                d.get('cuit', ''),
                                d.get('avalista', ''),
                                d.get('estado_pei', ''),
                                d.get('linea_manual', ''),
                                d.get('fecha_respuesta_uep') or None,
                                d.get('devolvio_legales', ''),
                                d.get('motivo_devolucion_legales', ''),
                                d.get('observaciones_carga', ''),
                            ))
                            ok += 1
                        except Exception as e:
                            errors.append({'row': i + 1, 'msg': str(e)})
                        if ok % 200 == 0:
                            conn.commit()
                    conn.commit()
                finally:
                    conn.close()

            self.send_json({'ok': True, 'importados': ok, 'omitidos': 0, 'errores': errors})
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def _normalizar_datos(self):
        """Limpieza: normaliza garantia y provincia usando num_exp como fuente de verdad."""
        try:
            with _db_write_lock:
                conn = get_db()
                try:
                    rows = conn.execute("SELECT id, num_exp, garantia, provincia FROM expedientes").fetchall()
                    updated = 0
                    for r in rows:
                        new_g = _norm_garantia(r['garantia'])
                        new_p = _norm_provincia(r['provincia'], r['num_exp'])
                        if new_g != r['garantia'] or new_p != r['provincia']:
                            conn.execute(
                                "UPDATE expedientes SET garantia=?, provincia=? WHERE id=?",
                                (new_g, new_p, r['id'])
                            )
                            updated += 1
                    conn.commit()
                finally:
                    conn.close()
            self.send_json({'ok': True, 'actualizados': updated})
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

    def _debug_buscar(self, qs):
        """Busca incluyendo borrados — para diagnóstico."""
        try:
            q = (qs.get('q') or [None])[0]
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT id, num_exp, titular, fecha_carga, deleted_at FROM expedientes "
                    "WHERE num_exp LIKE ? OR titular LIKE ? OR cuit LIKE ?",
                    (f"%{q}%", f"%{q}%", f"%{q}%")
                ).fetchall()
            self.send_json({"total": len(rows), "data": [dict(r) for r in rows]})
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

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

            tec      = (qs.get('tec')      or [None])[0]
            tec_uep  = (qs.get('tec_uep')  or [None])[0]

            if search:
                # Búsqueda GLOBAL: sin filtro de mes ni provincia
                q = "SELECT * FROM expedientes WHERE deleted_at IS NULL"
                p = []
                s = f"%{search}%"
                q += " AND (num_exp LIKE ? OR titular LIKE ? OR cuit LIKE ?)"
                p += [s, s, s]
            else:
                if not mes:
                    from datetime import date
                    mes = date.today().strftime('%Y-%m')

                q = "SELECT * FROM expedientes WHERE deleted_at IS NULL AND substr(fecha_carga,1,7) = ?"
                p = [mes]

                if provincia: q += " AND provincia = ?";          p.append(provincia)
                if linea:     q += " AND (linea_programatica = ? OR linea_manual = ?)"; p += [linea, linea]
                if garantia:  q += " AND garantia = ?";           p.append(garantia)
                if paso:      q += " AND paso_carga_inicial = ?"; p.append(paso)
                if legales:   q += " AND devolvio_legales = ?";   p.append(legales)
                if tec:       q += " AND tec_de_carga = ?";       p.append(tec)
                if tec_uep:   q += " AND tecnico LIKE ?";         p.append(f"%{tec_uep}%")

            total_q  = q.replace("SELECT *", "SELECT COUNT(*)", 1)
            data_q   = q + " ORDER BY CAST(SUBSTR(num_exp, -6) AS INTEGER) DESC LIMIT ? OFFSET ?"

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
            mes      = (qs.get('mes')      or [None])[0]
            provincia = (qs.get('provincia') or [None])[0]
            if not mes:
                from datetime import date
                mes = date.today().strftime('%Y-%m')

            base = "FROM expedientes WHERE deleted_at IS NULL AND substr(fecha_carga,1,7) = ?"
            p    = [mes]

            if provincia:
                base += " AND provincia = ?"
                p.append(provincia)

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

    REGIONES = {
        'BA': 'Centro',     'CO': 'Centro',     'ER': 'Centro',     'SF': 'Centro',
        'CA': 'NOA',        'JU': 'NOA',        'SA': 'NOA',        'TU': 'NOA',
        'CT': 'NEA+SDE',    'FO': 'NEA+SDE',    'CH': 'NEA+SDE',    'MI': 'NEA+SDE',    'SE': 'NEA+SDE',
        'CB': 'Sur',        'NQ': 'Sur',        'RN': 'Sur',        'SC': 'Sur',        'TF': 'Sur',
        'LP': 'Cuyo+LP',    'LR': 'Cuyo+LP',    'MZ': 'Cuyo+LP',    'SJ': 'Cuyo+LP',    'SL': 'Cuyo+LP',
    }

    def _carga_stats_mensuales(self, qs):
        try:
            mes = (qs.get('mes') or [None])[0]
            if not mes:
                from datetime import date
                mes = date.today().strftime('%Y-%m')

            with get_db() as conn:
                rows = conn.execute("""
                    SELECT num_exp, monto, provincia,
                           CASE WHEN linea_manual != '' THEN linea_manual ELSE linea_programatica END as linea,
                           garantia
                    FROM expedientes
                    WHERE deleted_at IS NULL AND substr(fecha_carga,1,7) = ?
                """, (mes,)).fetchall()

            def is_usd(num_exp):
                return (num_exp or '').startswith('2002-')

            def prov_code_from_exp(num_exp):
                s = num_exp or ''
                return s[8:10] if len(s) >= 10 else ''

            def agg_zero():
                return {'total_ars': 0, 'monto_ars': 0.0, 'total_usd': 0, 'monto_usd': 0.0}

            def inc(d, key, monto, usd):
                if key not in d:
                    d[key] = agg_zero()
                if usd:
                    d[key]['total_usd'] += 1
                    d[key]['monto_usd'] += monto
                else:
                    d[key]['total_ars'] += 1
                    d[key]['monto_ars'] += monto

            resumen = agg_zero()
            regiones = {}; provincias = {}; lineas = {}; garantias = {}

            for r in rows:
                num_exp  = r[0] or ''
                monto    = parse_monto(r[1])
                prov     = r[2] or ''
                linea    = r[3] or ''
                garantia = r[4] or ''
                usd = is_usd(num_exp)

                code   = prov_code_from_exp(num_exp)
                region = self.REGIONES.get(code, prov or '(Sin región)')

                if usd:
                    resumen['total_usd'] += 1
                    resumen['monto_usd'] += monto
                else:
                    resumen['total_ars'] += 1
                    resumen['monto_ars'] += monto

                inc(regiones,  region,                       monto, usd)
                inc(provincias, prov or '(Sin provincia)',   monto, usd)
                inc(lineas,    linea or '(Sin línea)',       monto, usd)
                inc(garantias, garantia or '(Sin garantía)', monto, usd)

            def to_list(d, key_name):
                return sorted(
                    [{key_name: k, **v} for k, v in d.items()],
                    key=lambda x: -(x['total_ars'] + x['total_usd'])
                )

            self.send_json({
                'mes':           mes,
                'resumen':       resumen,
                'por_region':    to_list(regiones,   'region'),
                'por_provincia': to_list(provincias, 'provincia'),
                'por_linea':     to_list(lineas,     'linea'),
                'por_garantia':  to_list(garantias,  'garantia'),
            })
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    BACKUP_KEY = os.environ.get('BACKUP_SECRET', 'cfi-backup-2026')

    def _backup_db(self, qs):
        """Devuelve el archivo SQLite completo para backup externo."""
        key = (qs.get('key') or [''])[0]
        if key != self.BACKUP_KEY:
            self.send_response(403)
            self.end_headers()
            return
        try:
            import shutil, tempfile
            with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
                tmp_path = tmp.name
            # Backup seguro via sqlite3 API
            import sqlite3 as _sqlite3
            src = _sqlite3.connect(str(DB_PATH))
            dst = _sqlite3.connect(tmp_path)
            src.backup(dst)
            dst.close(); src.close()
            with open(tmp_path, 'rb') as f:
                data = f.read()
            os.unlink(tmp_path)
            fecha = datetime.now().strftime('%Y-%m-%d')
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Disposition', f'attachment; filename="carga_backup_{fecha}.db"')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
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

    # ── Aprobados por director ─────────────────────────────────────────────────

    def _get_aprobados(self):
        """GET /api/aprobados-director — devuelve [{mes, cantidad, monto_total}] ordenado por mes."""
        try:
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT mes, cantidad, monto_total FROM aprobados_director ORDER BY mes ASC"
                ).fetchall()
            self.send_json([{'mes': r[0], 'cantidad': r[1], 'monto_total': r[2]} for r in rows])
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def _post_aprobados(self):
        """POST /api/aprobados-director — recibe {key, data:[{mes, cantidad, monto_total}]},
        valida la key, borra y recarga la tabla. Devuelve {ok: true, meses: N}."""
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            return self.send_json({'error': 'JSON inválido'}, 400)

        if body.get('key') != self.BACKUP_KEY:
            self.send_response(403)
            self.end_headers()
            return

        data = body.get('data', [])
        if not isinstance(data, list):
            return self.send_json({'error': 'data debe ser una lista'}, 400)

        with _db_write_lock:
            try:
                with get_db() as conn:
                    conn.execute("DELETE FROM aprobados_director")
                    conn.executemany(
                        "INSERT INTO aprobados_director (mes, cantidad, monto_total) VALUES (?,?,?)",
                        [(r['mes'], r['cantidad'], r.get('monto_total', 0)) for r in data]
                    )
                self.send_json({'ok': True, 'meses': len(data)})
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
            'linea_programatica':r.get('linea_normalizada', '') or r.get('linea', ''),
            'tec_de_carga':      _norm_tecnico_pei(r.get('nombreRepresentanteComercial', '') or ''),
            'estado_pei':        r.get('estadoExpediente', ''),
            'fecha_instruccion': (r.get('fechaSolicitud') or '')[:10],
            'fecha_carga':       (r.get('fechaSolicitud') or '')[:10],
            '_pei': True,
        }

    # URL de PostgreSQL compartido (mismo que Dashboard/UEP — solo lectura para buscar)
    PG_URL = os.environ.get('DATABASE_URL', '').replace('postgres://', 'postgresql://', 1)

    def _buscar_en_pei(self, q_str):
        """Consulta directa a bandeja_pei_real en PostgreSQL. Rápido, sin HTTP."""
        if not self.PG_URL:
            return [], "DATABASE_URL no configurada"
        try:
            import psycopg2
            import psycopg2.extras
            patron = f"%{q_str}%"
            with psycopg2.connect(self.PG_URL) as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT "denominacionSolicitud", "razonSocial", "cuit",
                               "provincia", "importeSolicitado", "estadoExpediente",
                               "entidadesAvalantes", "tiposContragarantia", "fechaSolicitud",
                               "linea", "nombreRepresentanteComercial"
                        FROM bandeja_pei_real
                        WHERE "tipoDeLinea" = 'Crédito'
                          AND (
                            LOWER("razonSocial")              LIKE LOWER(%s)
                            OR LOWER("denominacionSolicitud") LIKE LOWER(%s)
                            OR LOWER(COALESCE("cuit",''))     LIKE LOWER(%s)
                          )
                        ORDER BY "denominacionSolicitud" DESC
                        LIMIT 30
                    """, (patron, patron, patron))
                    rows = cur.fetchall()
            result = []
            for r in rows:
                monto_raw = r.get('importeSolicitado')
                monto = f"$ {int(monto_raw):,}".replace(',', '.') if monto_raw else ''
                entidad = str(r.get('entidadesAvalantes') or '').strip()
                tipo    = str(r.get('tiposContragarantia') or '').strip()
                for pref in ['Garantia SGR / FPG — ', 'Garantia SGR / FPG',
                             'Garantía SGR / FPG — ', 'Garantía SGR / FPG', 'Garantia ']:
                    if tipo.startswith(pref):
                        tipo = tipo[len(pref):]
                        break
                result.append({
                    'num_exp':           r.get('denominacionSolicitud') or '',
                    'titular':           r.get('razonSocial') or '',
                    'cuit':              r.get('cuit') or '',
                    'provincia':         r.get('provincia') or '',
                    'monto':             monto,
                    'garantia':          entidad or tipo,
                    'estado_pei':        r.get('estadoExpediente') or '',
                    'linea_programatica':r.get('linea') or '',
                    'tec_de_carga':      _norm_tecnico_pei(r.get('nombreRepresentanteComercial') or ''),
                    'fecha_instruccion': (r.get('fechaSolicitud') or '')[:10],
                    'fecha_carga':       (r.get('fechaSolicitud') or '')[:10],
                    '_pei': True,
                })
            return result, None
        except Exception as e:
            return [], str(e)

    def _carga_buscar(self, qs):
        """
        Búsqueda rápida: DB local SQLite + bandeja_pei_real PostgreSQL directo.
        Sin llamadas HTTP intermedias — consulta SQL en < 1 segundo.
        """
        try:
            q_str = (qs.get('q') or [''])[0].strip()
            if len(q_str) < 2:
                return self.send_json([])

            # ── 1. Buscar en DB local (SQLite) ────────────────────
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

            # ── 2. Buscar en PEI (PostgreSQL directo) ─────────────
            pei_only, pei_error = self._buscar_en_pei(q_str)
            pei_only = [r for r in pei_only if r['num_exp'] not in local_nums]

            resultado = (local + pei_only)[:25]

            if not resultado:
                return self.send_json({
                    'results': [],
                    'sin_resultados': True,
                    'pei_error': pei_error,
                    'sugerencia': 'manual',
                })

            self.send_json(resultado)
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

# ── Sync de bajas desde PEI (retirados/cancelados) ───────────────────────────

PG_URL = os.environ.get('DATABASE_URL', '').replace('postgres://', 'postgresql://', 1)

def _sync_bajas_pei():
    """
    Consulta bandeja_pei_real en PostgreSQL y soft-deleteea en SQLite local
    los expedientes que PEI marcó como Retirado o Cancelado.
    Corre al inicio y cada 30 minutos en background.
    """
    if not PG_URL:
        return
    try:
        import psycopg2
        conn_pg = psycopg2.connect(PG_URL)
        cur = conn_pg.cursor()
        # Solo num_exp donde TODAS las filas son Retirado/Cancelado.
        # Si existe al menos una fila activa para el mismo num_exp, lo excluimos.
        cur.execute("""
            SELECT "denominacionSolicitud"
            FROM bandeja_pei_real
            WHERE "tipoDeLinea" = 'Crédito'
              AND (LOWER(COALESCE("estadoExpediente",'')) LIKE '%retir%'
                OR LOWER(COALESCE("estadoExpediente",'')) LIKE '%cancel%')
            EXCEPT
            SELECT "denominacionSolicitud"
            FROM bandeja_pei_real
            WHERE "tipoDeLinea" = 'Crédito'
              AND LOWER(COALESCE("estadoExpediente",'')) NOT LIKE '%retir%'
              AND LOWER(COALESCE("estadoExpediente",'')) NOT LIKE '%cancel%'
        """)
        retirados = {r[0] for r in cur.fetchall()}
        conn_pg.close()

        if not retirados:
            return

        with _db_write_lock:
            conn = get_db()
            try:
                # Buscar cuáles están activos en SQLite
                placeholders = ','.join('?' * len(retirados))
                activos = conn.execute(
                    f"SELECT id, num_exp FROM expedientes WHERE num_exp IN ({placeholders}) AND deleted_at IS NULL",
                    list(retirados)
                ).fetchall()

                if activos:
                    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    for row in activos:
                        conn.execute(
                            "UPDATE expedientes SET deleted_at=? WHERE id=?",
                            (ts, row['id'])
                        )
                    conn.commit()
                    print(f"[sync-bajas] Soft-delete de {len(activos)} expedientes retirados en PEI: "
                          f"{[r['num_exp'] for r in activos]}")
                else:
                    print(f"[sync-bajas] Sin expedientes activos que dar de baja (retirados PEI: {len(retirados)}).")
            finally:
                conn.close()
    except Exception as e:
        print(f"[sync-bajas] Error: {e}")


def _loop_sync_bajas():
    """Loop de fondo: sync de bajas cada 30 minutos."""
    import time
    _sync_bajas_pei()                    # primera corrida al arrancar
    while True:
        time.sleep(30 * 60)
        _sync_bajas_pei()


# ── Arranque ──────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    threading.Thread(target=_loop_sync_bajas, daemon=True).start()
    port = int(os.environ.get('PORT', 3001))
    server = http.server.ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print(f"\n  Sistema de Carga de Expedientes CFI")
    print(f"  Corriendo en http://localhost:{port}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Servidor detenido.")
