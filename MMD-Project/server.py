#!/usr/bin/env python3
"""Local, dependency-free document register for e-file MMD."""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import json, mimetypes, os, re, secrets, sqlite3, threading, subprocess, urllib.parse, datetime
import socket
from email.parser import BytesParser
from email.policy import default

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
FILES = DATA / "documents"
DB = DATA / "efile.sqlite3"
SETTINGS = DATA / "settings.json"
MAX_BYTES = 5 * 1024 * 1024
ALLOWED = {".pdf", ".doc", ".docx", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
LOCK = threading.Lock()
DB_LOCK = threading.RLock()
MASTER_FIELDS = {"unit":"Unit", "category":"Category", "subcategory":"Sub-category", "documentType":"Document Type", "itemCategory":"Item Category", "department":"Department", "financialYear":"Financial Year"}
DEFAULT_MASTERS = {"unit":["Hospital"], "category":["C5-receipt&Qc"], "subcategory":["Goods Receipt"], "documentType":["Goods Receipt Note"], "itemCategory":["Consumable"], "department":["Cardiology"], "financialYear":["2026-2027"]}

def settings():
    try: cfg=json.loads(SETTINGS.read_text())
    except (OSError, ValueError): cfg={"database": {"backend":"sqlite"}, "files": {"backend":"local"}}
    if not isinstance(cfg,dict): cfg={"database": {"backend":"sqlite"}, "files": {"backend":"local"}}
    cfg.setdefault("masters", {k:list(v) for k,v in DEFAULT_MASTERS.items()})
    return cfg

def secret_key(): return "efile-mmd-postgres"

def save_settings(cfg):
    DATA.mkdir(parents=True,exist_ok=True)
    tmp=SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg,indent=2))
    os.replace(tmp,SETTINGS)

def sys_platform_is_mac():
    import sys
    return sys.platform == "darwin"

def smb_mount_path(cfg):
    if not sys_platform_is_mac(): return None
    share=str(cfg.get("share","")).strip()
    if not share: return None
    vols=Path("/Volumes")
    candidates=[vols/share]
    try: candidates += [p for p in vols.iterdir() if p.name.casefold()==share.casefold()]
    except OSError: pass
    return next((p for p in candidates if p.is_dir()),None)

def validate_postgres(cfg,password):
    try: import psycopg
    except ImportError as e: raise ValueError("Install the PostgreSQL extras first; see README.md.") from e
    try:
        with psycopg.connect(host=cfg["host"],port=cfg["port"],dbname=cfg["name"],user=cfg["user"],password=password,
                             sslmode=cfg.get("sslmode","prefer"),connect_timeout=5) as c: c.execute("SELECT 1")
    except Exception as e: raise ValueError(f"PostgreSQL connection failed: {type(e).__name__}: {e}")

def connect():
    backend=settings().get("database", {}).get("backend", "sqlite")
    if backend == "sqlite":
        c = sqlite3.connect(DB, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        return c
    try:
        import keyring
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as e:
        raise RuntimeError("PostgreSQL support needs the optional dependencies. Follow the network setup in README.md.") from e
    cfg=settings().get("database", {})
    password=keyring.get_password(secret_key(), cfg.get("user", ""))
    if password is None: raise RuntimeError("PostgreSQL password is not available in macOS Keychain. Re-enter it in Storage settings.")
    conn=psycopg.connect(host=cfg.get("host"), port=int(cfg.get("port",5432)), dbname=cfg.get("name"), user=cfg.get("user"), password=password,
                         sslmode=cfg.get("sslmode","prefer"), connect_timeout=5, row_factory=dict_row)
    return PgConnection(conn)

class PgRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int): return list(self.values())[key]
        return super().__getitem__(key)

class PgCursor:
    def __init__(self, cursor): self.cursor=cursor
    def fetchone(self):
        row=self.cursor.fetchone()
        return PgRow(row) if row is not None else None
    def fetchall(self): return [PgRow(row) for row in self.cursor.fetchall()]

class PgConnection:
    def __init__(self, conn): self.conn=conn
    def __enter__(self): self.conn.__enter__(); return self
    def __exit__(self, *args): return self.conn.__exit__(*args)
    def execute(self, sql, params=()): return PgCursor(self.conn.execute(sql.replace("?", "%s"), params))

def local_connect():
    c=sqlite3.connect(DB, timeout=15); c.row_factory=sqlite3.Row; c.execute("PRAGMA foreign_keys=ON"); return c

def initialize():
    FILES.mkdir(parents=True, exist_ok=True)
    with local_connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, name TEXT NOT NULL, stored_name TEXT NOT NULL UNIQUE,
          size INTEGER NOT NULL, media_type TEXT NOT NULL, uploaded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          storage_path TEXT, unit TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '', subcategory TEXT NOT NULL DEFAULT '',
          document_type TEXT NOT NULL DEFAULT '', item_category TEXT NOT NULL DEFAULT '', department TEXT NOT NULL DEFAULT '', financial_year TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS tags(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE);
        CREATE TABLE IF NOT EXISTS document_tags(document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
          tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE, PRIMARY KEY(document_id,tag_id));
        CREATE INDEX IF NOT EXISTS idx_doc_tags_tag ON document_tags(tag_id);
        CREATE TABLE IF NOT EXISTS app_logs(id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          level TEXT NOT NULL, action TEXT NOT NULL, message TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_app_logs_time ON app_logs(occurred_at DESC);
        """)
        cols={r[1] for r in c.execute("PRAGMA table_info(documents)").fetchall()}
        for column in ("storage_path TEXT", "unit TEXT NOT NULL DEFAULT ''", "category TEXT NOT NULL DEFAULT ''", "subcategory TEXT NOT NULL DEFAULT ''", "document_type TEXT NOT NULL DEFAULT ''", "item_category TEXT NOT NULL DEFAULT ''", "department TEXT NOT NULL DEFAULT ''", "financial_year TEXT NOT NULL DEFAULT ''"):
            name=column.split()[0]
            if name not in cols: c.execute(f"ALTER TABLE documents ADD COLUMN {column}")
    if settings().get("database", {}).get("backend") == "postgres":
        try: ensure_postgres_schema()
        except Exception as e:
            # Keep the local UI available so the user can fix the PostgreSQL settings.
            write_log("ERROR","PostgreSQL startup",f"{type(e).__name__}: {e}")
            print("PostgreSQL is not ready; the app will stay open so Storage settings can be corrected.",flush=True)

def write_log(level, action, message):
    # Keep a bounded local history; never log document contents or tag values.
    with local_connect() as c:
        c.execute("INSERT INTO app_logs(level,action,message) VALUES(?,?,?)", (level[:12], action[:40], message[:500]))
        c.execute("DELETE FROM app_logs WHERE id NOT IN (SELECT id FROM app_logs ORDER BY id DESC LIMIT 500)")

def ensure_postgres_schema():
    with connect() as c:
        for sql in [
            "CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, name TEXT NOT NULL, stored_name TEXT NOT NULL UNIQUE, size BIGINT NOT NULL, media_type TEXT NOT NULL, uploaded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, storage_path TEXT, unit TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '', subcategory TEXT NOT NULL DEFAULT '', document_type TEXT NOT NULL DEFAULT '', item_category TEXT NOT NULL DEFAULT '', department TEXT NOT NULL DEFAULT '', financial_year TEXT NOT NULL DEFAULT '')",
            "CREATE TABLE IF NOT EXISTS tags(id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE)",
            "CREATE TABLE IF NOT EXISTS document_tags(document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE, tag_id BIGINT NOT NULL REFERENCES tags(id) ON DELETE CASCADE, PRIMARY KEY(document_id,tag_id))",
            "CREATE TABLE IF NOT EXISTS app_logs(id BIGSERIAL PRIMARY KEY, occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, level TEXT NOT NULL, action TEXT NOT NULL, message TEXT NOT NULL)",
            "CREATE INDEX IF NOT EXISTS idx_doc_tags_tag ON document_tags(tag_id)",
            "CREATE INDEX IF NOT EXISTS idx_app_logs_time ON app_logs(occurred_at DESC)",
        ]: c.execute(sql)
        for column in ("unit", "category", "subcategory", "document_type", "item_category", "department", "financial_year"):
            c.execute(f"ALTER TABLE documents ADD COLUMN IF NOT EXISTS {column} TEXT NOT NULL DEFAULT ''")
    # Copy existing local records once by stable document ID. Attachments remain in their current locations.
    with local_connect() as local:
        docs=local.execute("SELECT id,name,stored_name,size,media_type,uploaded_at,storage_path,unit,category,subcategory,document_type,item_category,department,financial_year FROM documents").fetchall()
        tags=local.execute("SELECT id,name FROM tags").fetchall()
        links=local.execute("SELECT document_id,tag_id FROM document_tags").fetchall()
    with connect() as remote:
        for d in docs:
            remote.execute("INSERT INTO documents(id,name,stored_name,size,media_type,uploaded_at,storage_path,unit,category,subcategory,document_type,item_category,department,financial_year) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                           (d["id"],d["name"],d["stored_name"],d["size"],d["media_type"],d["uploaded_at"],d["storage_path"],d["unit"],d["category"],d["subcategory"],d["document_type"],d["item_category"],d["department"],d["financial_year"]))
        for t in tags:
            remote.execute("INSERT INTO tags(name) VALUES(?) ON CONFLICT(name) DO NOTHING", (t["name"],))
        for link in links:
            tag=remote.execute("SELECT id FROM tags WHERE name=?", (local.execute("SELECT name FROM tags WHERE id=?",(link["tag_id"],)).fetchone()[0],)).fetchone()
            if tag: remote.execute("INSERT INTO document_tags(document_id,tag_id) VALUES(?,?) ON CONFLICT DO NOTHING", (link["document_id"],tag["id"]))

def copy_postgres_to_local():
    with connect() as remote:
        docs=remote.execute("SELECT id,name,stored_name,size,media_type,uploaded_at,storage_path,unit,category,subcategory,document_type,item_category,department,financial_year FROM documents").fetchall()
        tags=remote.execute("SELECT id,name FROM tags").fetchall()
        links=remote.execute("SELECT document_id,tag_id FROM document_tags").fetchall()
    tag_names={r["id"]:r["name"] for r in tags}
    with local_connect() as local:
        for d in docs:
            local.execute("INSERT OR IGNORE INTO documents(id,name,stored_name,size,media_type,uploaded_at,storage_path,unit,category,subcategory,document_type,item_category,department,financial_year) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (d["id"],d["name"],d["stored_name"],d["size"],d["media_type"],d["uploaded_at"].isoformat() if hasattr(d["uploaded_at"],"isoformat") else d["uploaded_at"],d["storage_path"],d["unit"],d["category"],d["subcategory"],d["document_type"],d["item_category"],d["department"],d["financial_year"]))
        for t in tags: local.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)",(t["name"],))
        for link in links:
            name=tag_names.get(link["tag_id"])
            if name:
                local_tag=local.execute("SELECT id FROM tags WHERE name=? COLLATE NOCASE",(name,)).fetchone()
                if local_tag: local.execute("INSERT OR IGNORE INTO document_tags(document_id,tag_id) VALUES(?,?)",(link["document_id"],local_tag["id"]))

def clean_name(name):
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r"[^\w.()\- ]", "_", name, flags=re.UNICODE).strip(" .")
    return name[:180] or "document"

def request_origin_matches(handler):
    origin=handler.headers.get("Origin")
    if origin is None: return True
    parsed=urlparse(origin)
    return parsed.scheme in ("http","https") and parsed.netloc.casefold()==handler.headers.get("Host","").casefold()

def safe_tags(raw):
    try: values = json.loads(raw)
    except Exception: raise ValueError("Tags must be a valid list.")
    if not isinstance(values, list) or len(values) > 20: raise ValueError("Add up to 20 tags.")
    result, seen = [], set()
    for item in values:
        if not isinstance(item, str): raise ValueError("Each tag must be text.")
        tag = re.sub(r"\s+", " ", item).strip()
        if not tag or len(tag) > 40: raise ValueError("Tags must contain 1–40 characters.")
        if tag.casefold() not in seen: result.append(tag); seen.add(tag.casefold())
    return result

class Handler(SimpleHTTPRequestHandler):
    server_version = "EFileLocal/1.0"
    def __init__(self, *args, **kwargs): super().__init__(*args, directory=str(ROOT), **kwargs)
    def log_message(self, fmt, *args): pass
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Content-Security-Policy", "default-src 'self' blob:; img-src 'self' blob: data:; frame-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'")
        super().end_headers()
    def send_json(self, value, code=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff"); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        try: return self._do_get()
        except Exception as e:
            if urlparse(self.path).path.startswith("/api/"):
                write_log("ERROR","API read",f"{type(e).__name__}: {e}")
                return self.send_json({"error":"Storage connection failed. Check Storage settings and Activity logs."},503)
            raise
    def _do_get(self):
        p = urlparse(self.path)
        if p.path == "/api/masters":
            cfg=settings(); return self.send_json({"masters":cfg.get("masters",DEFAULT_MASTERS)})
        if p.path == "/api/storage/settings":
            cfg=settings()
            db=cfg.get("database",{"backend":"sqlite"}); fs=cfg.get("files",{"backend":"local"})
            return self.send_json({"database":{"backend":db.get("backend","sqlite"),"host":db.get("host",""),"port":db.get("port",5432),"name":db.get("name",""),"user":db.get("user",""),"sslmode":db.get("sslmode","prefer")},
                                  "files":{"backend":fs.get("backend","local"),"host":fs.get("host",""),"share":fs.get("share",""),"folder":fs.get("folder","e-file MMD/documents")},
                                  "smbMounted":bool(smb_mount_path(fs)) if fs.get("backend")=="smb" else False})
        if p.path == "/api/storage":
            with connect() as c:
                row = c.execute("SELECT count(*), coalesce(sum(size),0) FROM documents").fetchone()
            return self.send_json({"documents": int(row[0]), "usedBytes": int(row[1] or 0), "backend":settings().get("files",{}).get("backend","local")})
        if p.path == "/api/logs":
            with local_connect() as c:
                rows = c.execute("SELECT occurred_at,level,action,message FROM app_logs ORDER BY id DESC LIMIT 200").fetchall()
            return self.send_json({"logs": [dict(timestamp=r[0], level=r[1], action=r[2], message=r[3]) for r in rows]})
        if p.path == "/api/documents":
            q = [x.strip() for x in parse_qs(p.query).get("tag", []) if x.strip()]
            with connect() as c:
                rows = c.execute("SELECT * FROM documents ORDER BY uploaded_at DESC").fetchall()
                rel = c.execute("SELECT dt.document_id,t.name FROM document_tags dt JOIN tags t ON t.id=dt.tag_id ORDER BY lower(t.name)").fetchall()
                tags = [r[0] for r in c.execute("SELECT name FROM tags ORDER BY lower(name)").fetchall()]
                by_doc={}
                for r in rel: by_doc.setdefault(r["document_id"],[]).append(r["name"])
                docs = [dict(id=r["id"], name=r["name"], size=r["size"], type=r["media_type"], uploadedAt=r["uploaded_at"].isoformat() if hasattr(r["uploaded_at"],"isoformat") else r["uploaded_at"], tags=by_doc.get(r["id"],[]), unit=r["unit"], category=r["category"], subcategory=r["subcategory"], documentType=r["document_type"], itemCategory=r["item_category"], department=r["department"], financialYear=r["financial_year"]) for r in rows]
                if q:
                    wanted={x.casefold() for x in q}; docs=[d for d in docs if wanted.issubset({x.casefold() for x in d["tags"]})]
            return self.send_json({"documents": docs, "tags": tags})
        if p.path.startswith("/api/file/"):
            docid = p.path.rsplit("/", 1)[-1]
            with connect() as c: row = c.execute("SELECT * FROM documents WHERE id=?", (docid,)).fetchone()
            if not row: return self.send_error(404)
            path = Path(row["storage_path"]) if row["storage_path"] else FILES / row["stored_name"]
            if not path.is_file(): return self.send_error(404)
            data = path.read_bytes(); mime = row["media_type"]
            disposition = "inline" if mime == "application/pdf" or mime.startswith("image/") else "attachment"
            self.send_response(200); self.send_header("Content-Type", mime); self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f"{disposition}; filename*=UTF-8''{__import__('urllib.parse').parse.quote(row['name'])}")
            self.send_header("X-Content-Type-Options", "nosniff"); self.end_headers(); self.wfile.write(data); return
        if p.path == "/": self.path = "/index.html"
        return super().do_GET()
    def do_POST(self):
        endpoint = urlparse(self.path).path
        if not request_origin_matches(self):
            write_log("ERROR","request", "Request rejected because the origin did not match this local app.")
            return self.send_json({"error":"Request origin rejected."},403)
        if endpoint == "/api/logs":
            if not request_origin_matches(self):
                return self.send_json({"error":"Request origin rejected."}, 403)
            try:
                data = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", "0")), 8192)))
                action = str(data.get("action", "client"))[:40]
                message = str(data.get("message", "Client-side failure"))[:500]
                write_log("ERROR", action, message)
                return self.send_json({"saved": True}, 201)
            except Exception: return self.send_json({"error":"Could not save diagnostic log."}, 400)
        if endpoint == "/api/masters":
            try:
                body=json.loads(self.rfile.read(min(int(self.headers.get("Content-Length","0")),1048576)))
                if not isinstance(body,dict): raise ValueError("Master values must be grouped by classification.")
                raw=body.get("masters",{})
                if not isinstance(raw,dict): raise ValueError("Master values must be grouped by classification.")
                masters={}
                for field in MASTER_FIELDS:
                    values=raw.get(field,[])
                    if not isinstance(values,list) or len(values)>500: raise ValueError(f"Enter up to 500 values for {MASTER_FIELDS[field]}.")
                    clean=[]; seen=set()
                    for value in values:
                        value=re.sub(r"\s+"," ",str(value)).strip()
                        if not value or len(value)>80: raise ValueError(f"Each {MASTER_FIELDS[field]} value must contain 1–80 characters.")
                        if value.casefold() not in seen: clean.append(value); seen.add(value.casefold())
                    masters[field]=clean
                cfg=settings(); cfg["masters"]=masters; save_settings(cfg)
                return self.send_json({"saved":True,"masters":masters})
            except ValueError as e: return self.send_json({"error":str(e)},400)
            except Exception as e:
                write_log("ERROR","Classification masters",f"{type(e).__name__}: {e}")
                return self.send_json({"error":"Could not save classification masters."},500)
        if endpoint == "/api/storage/smb/connect":
            try:
                body=json.loads(self.rfile.read(min(int(self.headers.get("Content-Length","0")),8192)))
                host=str(body.get("host","")).strip(); share=str(body.get("share","")).strip()
                if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}",host) or not re.fullmatch(r"[A-Za-z0-9._$ -]{1,80}",share): raise ValueError("Enter a valid SMB server IP/host and share name.")
                cfg=settings(); cfg.setdefault("files",{}).update({"backend":"smb","host":host,"share":share,"folder":"e-file MMD/documents"})
                save_settings(cfg)
                if not sys_platform_is_mac(): return self.send_json({"error":"SMB connection launch is supported on macOS. Mount the share in Finder, then retry."}, 400)
                subprocess.Popen(["open",f"smb://{host}/{urllib.parse.quote(share)}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return self.send_json({"started":True,"mounted":bool(smb_mount_path(cfg["files"]))}, 202)
            except ValueError as e: return self.send_json({"error":str(e)},400)
            except Exception as e:
                write_log("ERROR","SMB connect",f"{type(e).__name__}: {e}");return self.send_json({"error":"Could not open the SMB connection in Finder."},500)
        if endpoint == "/api/storage/settings":
            try:
                body=json.loads(self.rfile.read(min(int(self.headers.get("Content-Length","0")),16384)))
                db=body.get("database",{}); fs=body.get("files",{})
                database={"backend":db.get("backend","sqlite")}; files={"backend":fs.get("backend","local")}
                if database["backend"] not in ("sqlite","postgres"): raise ValueError("Choose SQLite or PostgreSQL.")
                if files["backend"] not in ("local","smb"): raise ValueError("Choose local files or an SMB share.")
                if database["backend"]=="postgres":
                    database.update({"host":str(db.get("host","")).strip(),"port":int(db.get("port",5432)),"name":str(db.get("name","")).strip(),"user":str(db.get("user","")).strip(),"sslmode":str(db.get("sslmode","prefer"))})
                    password=str(db.get("password",""))
                    if not all(database[k] for k in ("host","name","user")) or not (1<=database["port"]<=65535): raise ValueError("Enter PostgreSQL host, port, database, and username.")
                    previous=settings().get("database",{})
                    if previous.get("backend")=="postgres" and any(str(previous.get(k,""))!=str(database.get(k,"")) for k in ("host","port","name","user")):
                        raise ValueError("Switch to SQLite first before changing the PostgreSQL server. This safely copies records back to local storage before you reconnect.")
                    validate_postgres(database,password)
                    try:
                        import keyring
                        keyring.set_password(secret_key(),database["user"],password)
                    except ImportError: raise ValueError("Install the network extras before configuring PostgreSQL; see README.md.")
                    except Exception as e: raise ValueError("Could not store the database password in macOS Keychain: "+str(e))
                if files["backend"]=="smb":
                    files.update({"host":str(fs.get("host","")).strip(),"share":str(fs.get("share","")).strip(),"folder":"e-file MMD/documents"})
                    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}",files["host"]) or not re.fullmatch(r"[A-Za-z0-9._$ -]{1,80}",files["share"]): raise ValueError("Enter a valid SMB server IP/host and share name.")
                    if not smb_mount_path(files): raise ValueError("The SMB share is not mounted yet. Connect in Finder, sign in, then retry.")
                old=settings().get("database",{}).get("backend","sqlite")
                if old=="postgres" and database["backend"]=="sqlite":
                    try: copy_postgres_to_local()
                    except Exception as e: write_log("ERROR","PostgreSQL fallback",f"Could not copy remote records; using the last local copy. {type(e).__name__}: {e}")
                updated=settings(); updated.update({"database":database,"files":files}); save_settings(updated)
                if database["backend"]=="postgres": ensure_postgres_schema()
                return self.send_json({"saved":True})
            except ValueError as e: return self.send_json({"error":str(e)},400)
            except Exception as e:
                write_log("ERROR","Storage settings",f"{type(e).__name__}: {e}");return self.send_json({"error":"Storage settings could not be saved. See Activity logs."},500)
        if endpoint != "/api/documents": return self.send_error(404)
        action = "upload"
        if not request_origin_matches(self):
            write_log("ERROR", action, "Upload rejected because the request origin did not match the local app.")
            return self.send_json({"error":"Request origin rejected."}, 403)
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BYTES + 100_000:
            message = "Upload exceeds the 5 MB limit."; write_log("ERROR", action, message)
            return self.send_json({"error":message}, 413)
        body = self.rfile.read(length)
        try:
            message = BytesParser(policy=default).parsebytes((f"Content-Type: {self.headers.get('Content-Type')}\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
            fields = {}
            for part in message.iter_parts():
                key = part.get_param("name", header="content-disposition")
                if key == "file": fields["file"] = (part.get_filename() or "document", part.get_payload(decode=True) or b"", part.get_content_type())
                elif key == "tags": fields["tags"] = (part.get_payload(decode=True) or b"").decode("utf-8")
                elif key == "metadata": fields["metadata"] = (part.get_payload(decode=True) or b"").decode("utf-8")
            if "file" not in fields: raise ValueError("Choose a document to upload.")
            original, content, claimed_type = fields["file"]
            name = clean_name(original); ext = Path(name).suffix.lower()
            if ext not in ALLOWED: raise ValueError("Choose a PDF, DOC, DOCX, or supported image file.")
            if not content: raise ValueError("The selected file is empty.")
            if len(content) > MAX_BYTES:
                message = "The document must be 5 MB or smaller."; write_log("ERROR", action, message)
                return self.send_json({"error":message}, 413)
            tags = safe_tags(fields.get("tags", "[]"))
            metadata=json.loads(fields.get("metadata", "{}"))
            if not isinstance(metadata,dict): raise ValueError("Document classification must be an object.")
            metadata={key:re.sub(r"\s+"," ",str(metadata.get(key,"")).strip())[:80] for key in ("unit","category","subcategory","documentType","itemCategory","department","financialYear")}
            # Use a canonical content type based on extension; do not trust browser-supplied MIME.
            media = {".pdf":"application/pdf", ".doc":"application/msword", ".docx":"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                     ".png":"image/png", ".jpg":"image/jpeg", ".jpeg":"image/jpeg", ".gif":"image/gif", ".webp":"image/webp", ".bmp":"image/bmp", ".tif":"image/tiff", ".tiff":"image/tiff"}[ext]
            if ext == ".pdf" and not content.startswith(b"%PDF-"): raise ValueError("This file does not appear to be a valid PDF.")
            if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}:
                signatures={".png":content.startswith(b"\x89PNG\r\n\x1a\n"),".jpg":content.startswith(b"\xff\xd8\xff"),".jpeg":content.startswith(b"\xff\xd8\xff"),".gif":content.startswith((b"GIF87a",b"GIF89a")),".webp":content.startswith(b"RIFF") and content[8:12]==b"WEBP",".bmp":content.startswith(b"BM"),".tif":content.startswith((b"II*\x00",b"MM\x00*")),".tiff":content.startswith((b"II*\x00",b"MM\x00*"))}
                if not signatures[ext]: raise ValueError("The file content does not match its image extension.")
            docid = secrets.token_urlsafe(18); stored = docid + ext
            file_cfg=settings().get("files",{"backend":"local"})
            if file_cfg.get("backend")=="smb":
                mount=smb_mount_path(file_cfg)
                if not mount: raise ValueError("SMB share is disconnected. Reconnect it in Finder, then retry.")
                destdir=mount / file_cfg.get("folder","e-file MMD/documents")
            else: destdir=FILES
            destdir.mkdir(parents=True,exist_ok=True)
            with LOCK:
                dest = destdir / stored
                with open(dest, "xb") as f: f.write(content)
                try:
                    with connect() as c:
                        c.execute("INSERT INTO documents(id,name,stored_name,size,media_type,storage_path,unit,category,subcategory,document_type,item_category,department,financial_year) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (docid,name,stored,len(content),media,str(dest) if file_cfg.get("backend")=="smb" else None,metadata["unit"],metadata["category"],metadata["subcategory"],metadata["documentType"],metadata["itemCategory"],metadata["department"],metadata["financialYear"]))
                        for tag in tags:
                            found=c.execute("SELECT id FROM tags WHERE lower(name)=lower(?)", (tag,)).fetchone()
                            if found: tid=found["id"]
                            else:
                                c.execute("INSERT INTO tags(name) VALUES(?)", (tag,))
                                tid=c.execute("SELECT id FROM tags WHERE lower(name)=lower(?)", (tag,)).fetchone()["id"]
                            c.execute("INSERT INTO document_tags(document_id,tag_id) VALUES(?,?)", (docid,tid))
                except Exception:
                    dest.unlink(missing_ok=True); raise
            return self.send_json({"id":docid}, 201)
        except ValueError as e:
            write_log("ERROR", action, str(e)); return self.send_json({"error":str(e)}, 400)
        except Exception as e:
            write_log("ERROR", action, f"{type(e).__name__}: {e}")
            return self.send_json({"error":"Could not save this document. Check local storage and try again."}, 500)

if __name__ == "__main__":
    initialize()
    import argparse, webbrowser
    parser=argparse.ArgumentParser(description="Run the e-file MMD document register")
    parser.add_argument("--port", type=int, default=8765); parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--network", action="store_true", help="Allow other devices on the same trusted network to connect")
    args=parser.parse_args()
    bind_host="0.0.0.0" if args.network else "127.0.0.1"
    try: server=ThreadingHTTPServer((bind_host,args.port),Handler)
    except OSError as first_error:
        if args.port != 8765: raise SystemExit(f"Could not start on port {args.port}: {first_error}")
        for candidate in range(args.port + 1, args.port + 21):
            try: server=ThreadingHTTPServer((bind_host,candidate),Handler); break
            except OSError: continue
        else: raise SystemExit(f"Could not find an available local port near {args.port}: {first_error}")
    url=f"http://127.0.0.1:{server.server_port}"
    network_url=None
    if args.network:
        try:
            probe=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); probe.connect(("192.0.2.1",80)); lan_ip=probe.getsockname()[0]; probe.close()
            network_url=f"http://{lan_ip}:{server.server_port}"
        except OSError: network_url=f"http://<this-Mac-IP>:{server.server_port}"
    if server.server_port != args.port:
        print(f"Port {args.port} is already in use. This app is using {server.server_port}; close the older app window if you were using it.",flush=True)
    access=(f"Only this Mac: {url}" if not args.network else f"This Mac: {url}\nSame-network users: {network_url}\nNetwork access is enabled; use only on a trusted private network.")
    print(f"e-file MMD is ready.\n{access}\nDocuments stay in {DATA}\nPress Ctrl-C to stop.", flush=True)
    if not args.no_open: threading.Timer(.5,lambda:webbrowser.open(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nStopped.")
    finally: server.server_close()
