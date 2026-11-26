#!/usr/bin/env python3
"""ses: gestor de sesiones de Claude Code (solo stdlib)."""
import argparse
import hashlib
import collections
import contextlib
import io
import json
import os
import re
import shutil
import sys
import time
import unicodedata
from datetime import datetime

VERSION = "dev"

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PREFIX_RE = re.compile(r"^[0-9a-f]{4,36}$|^[0-9a-f-]{4,36}$")
FAKE_PREFIXES = ("<local-command-caveat>", "<local-command-stdout>", "<command-name>",
                 "<command-message>", "<command-args>")
ARTIFACT_KINDS = ("proyecto", "file-history", "session-env", "memory")


class SesError(Exception):
    pass


# ---------------------------------------------------------------- utilidades
def root():
    return os.environ.get("CLAUDE_HOME") or os.path.expanduser("~/.claude")


def activa_seg():
    try:
        return float(os.environ.get("SES_ACTIVA_MIN", "10")) * 60
    except ValueError:
        return 600.0


def use_color():
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def col(txt, code):
    return "\033[%sm%s\033[0m" % (code, txt) if use_color() else txt


def human(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return "%d B" % n if u == "B" else "%.1f %s" % (n, u)
        n /= 1024


def one_line(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: max(0, n - 3)] + "..."


def tilde(p):
    home = os.path.expanduser("~")
    if p == home:
        return "~"
    if p.startswith(home + os.sep):
        return "~" + p[len(home):]
    return p


def fmt_ts(epoch):
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def fmt_iso(ts):
    if not ts:
        return "?"
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, AttributeError):
        return str(ts)


def tree_size(p):
    total = 0
    if os.path.islink(p):
        return 0
    if os.path.isfile(p):
        return os.path.getsize(p)
    for d, _dirs, files in os.walk(p):
        for f in files:
            try:
                fp = os.path.join(d, f)
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
            except OSError:
                pass
    return total


# ---------------------------------------------------------------- seguridad
def allowed_roots():
    r = root()
    return [os.path.realpath(os.path.join(r, s))
            for s in ("projects", "file-history", "session-env", "ses-papelera")]


def check_safe(path, expected_name, memory=None):
    """Aborta (SesError) si la ruta no es segura de mover/borrar.

    memory: None (memory prohibido), "proj" (projects/<proy>/memory) o
    "trash" (artefactos/memory dentro de la papelera).
    """
    base = os.path.basename(path)
    if base == "memory" or expected_name == "memory":
        if memory is None or base != "memory" or expected_name != "memory":
            raise SesError("memory solo se mueve junto con la ultima sesion del proyecto: %s" % path)
        if memory == "proj":
            projs = os.path.realpath(os.path.join(root(), "projects"))
            if os.path.realpath(os.path.dirname(os.path.dirname(path))) != projs:
                raise SesError("memory fuera de projects/<proy>: %s" % path)
    if base != expected_name:
        raise SesError("nombre inesperado: %s" % path)
    parent = os.path.realpath(os.path.dirname(path))
    real = os.path.realpath(path)
    if real != os.path.join(parent, os.path.basename(path)) or os.path.islink(path):
        raise SesError("es un symlink: %s" % path)
    roots = allowed_roots()
    if memory == "proj":
        roots = roots[:1]
    elif memory == "trash":
        roots = roots[3:]
    for a in roots:
        if real.startswith(a + os.sep):
            return real
    raise SesError("fuera de las rutas permitidas: %s" % path)


def project_protected(pd, cwd=None):
    """La memoria del proyecto cuyo cwd es exactamente HOME esta protegida."""
    if os.environ.get("SES_PROTEGER_HOME", "1") == "0":
        return False
    home = os.path.expanduser("~")
    if cwd:
        return os.path.normpath(cwd) == os.path.normpath(home)
    return os.path.basename(pd) == re.sub(r"[^A-Za-z0-9]", "-", home)


def check_uuid(u):
    if not UUID_RE.match(u):
        raise SesError("uuid invalido: %s" % u)


# ---------------------------------------------------------------- parseo
def real_text(obj):
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "tool_result":
                    return None
                if b.get("type") == "text" and isinstance(b.get("text"), str):
                    parts.append(b["text"])
        text = "\n".join(parts)
    else:
        return None
    text = text.strip()
    if not text or text.startswith(FAKE_PREFIXES):
        return None
    return text


def parse_file(path):
    info = dict(cwd=None, first_ts=None, n_user=0, n_asst=0, corrupt=0, valid=0, real=0,
                first_real=[], last_real=[], custom=None, lastp=None)
    last = collections.deque(maxlen=3)
    with open(path, "rb") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                info["corrupt"] += 1
                continue
            if not isinstance(obj, dict):
                info["corrupt"] += 1
                continue
            info["valid"] += 1
            if info["cwd"] is None and isinstance(obj.get("cwd"), str):
                info["cwd"] = obj["cwd"]
            if info["first_ts"] is None and isinstance(obj.get("timestamp"), str):
                info["first_ts"] = obj["timestamp"]
            t = obj.get("type")
            if t == "user":
                info["n_user"] += 1
                txt = real_text(obj)
                if txt:
                    info["real"] += 1
                    line = one_line(txt, 200)
                    if len(info["first_real"]) < 3:
                        info["first_real"].append(line)
                    last.append(line)
            elif t == "assistant":
                info["n_asst"] += 1
            elif t == "custom-title" and isinstance(obj.get("customTitle"), str):
                info["custom"] = obj["customTitle"]
            elif t == "last-prompt" and isinstance(obj.get("lastPrompt"), str):
                info["lastp"] = obj["lastPrompt"]
    info["last_real"] = list(last)
    return info


def make_title(info):
    for c in (info["custom"], info["lastp"],
              info["first_real"][0] if info["first_real"] else None):
        if c and c.strip():
            return one_line(c, 60)
    return "(sin titulo)"


class Cache:
    def __init__(self):
        self.path = os.path.join(root(), "cache", "ses-cache.json")
        self.data = {}
        self.dirty = False
        try:
            with open(self.path) as f:
                d = json.load(f)
            if isinstance(d, dict):
                self.data = d
        except (OSError, ValueError):
            pass
        self.seen = set()

    def get(self, path, st):
        self.seen.add(path)
        e = self.data.get(path)
        if e and e.get("m") == st.st_mtime_ns and e.get("s") == st.st_size:
            return e["d"]
        d = parse_file(path)
        self.data[path] = {"m": st.st_mtime_ns, "s": st.st_size, "d": d}
        self.dirty = True
        return d

    def save(self, full_scan=False):
        if full_scan:
            stale = [k for k in self.data if k not in self.seen]
            for k in stale:
                del self.data[k]
                self.dirty = True
        if not self.dirty:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp%d" % os.getpid()
            with open(tmp, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
        except OSError:
            pass


def project_dirs():
    pr = os.path.join(root(), "projects")
    if not os.path.isdir(pr):
        return []
    out = []
    for n in sorted(os.listdir(pr)):
        p = os.path.join(pr, n)
        if os.path.isdir(p) and not os.path.islink(p):
            out.append(p)
    return out


def scan(parse=True):
    """Devuelve lista de sesiones (dicts)."""
    cache = Cache() if parse else None
    res = []
    for pd in project_dirs():
        for n in sorted(os.listdir(pd)):
            if not n.endswith(".jsonl"):
                continue
            u = n[:-6]
            p = os.path.join(pd, n)
            if not UUID_RE.match(u) or os.path.islink(p) or not os.path.isfile(p):
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            s = dict(uuid=u, path=p, proj_dir=pd, mtime=st.st_mtime, size=st.st_size)
            if parse:
                info = cache.get(p, st)
                s["info"] = info
                s["title"] = make_title(info)
                s["cwd"] = info["cwd"]
                s["proyecto"] = info["cwd"] or os.path.basename(pd)
                s["existe"] = os.path.isdir(info["cwd"]) if info["cwd"] else None
            res.append(s)
    if cache:
        cache.save(full_scan=True)
    return res


def is_active(mtime):
    return time.time() - mtime < activa_seg()


def resolve(prefix, pool, what="sesion"):
    p = prefix.lower()
    if not PREFIX_RE.match(p):
        raise SesError("id invalido '%s': hacen falta al menos 4 caracteres hexa" % prefix)
    ms = [s for s in pool if s["uuid"].startswith(p)]
    if not ms:
        raise SesError("no hay ninguna %s que empiece con '%s'" % (what, prefix))
    if len(ms) > 1:
        lines = "\n".join("  %s  %s" % (s["uuid"], s.get("title", "")) for s in ms)
        raise SesError("'%s' es ambiguo, coincide con %d:\n%s" % (prefix, len(ms), lines))
    return ms[0]


def artifacts_of(s):
    r = root()
    u = s["uuid"]
    cands = {"proyecto": os.path.join(s["proj_dir"], u),
             "file-history": os.path.join(r, "file-history", u),
             "session-env": os.path.join(r, "session-env", u)}
    return {k: v for k, v in cands.items() if os.path.lexists(v)}


# ---------------------------------------------------------------- salida en vivo
def term_width():
    if not sys.stdout.isatty():
        return None
    return shutil.get_terminal_size((100, 24)).columns


def fit(text):
    """Corta a lo ancho de la terminal (solo si es TTY)."""
    w = term_width()
    if w and len(text) > w - 1:
        return text[: max(0, w - 4)] + "..."
    return text


def plural(n, uno, varios):
    return "%d %s" % (n, uno if n == 1 else varios)


def row(kind, ident, size, rest=""):
    """Linea corta y alineada: tipo, id, tamano, detalle."""
    sz = human(size) if size is not None else ""
    return ("%-8s %-8s %9s  %s" % (kind, ident, sz, rest)).rstrip()


class Status:
    """Linea de estado que se reescribe con \r (solo TTY; si no, no hace nada)."""

    def __init__(self):
        self.on = sys.stdout.isatty()
        self.shown = False
        self.last = 0.0

    def update(self, text, force=False):
        if not self.on:
            return
        now = time.time()
        if not force and now - self.last < 0.1:
            return
        self.last = now
        w = term_width() or 80
        t = fit(text)
        sys.stdout.write("\r" + t + " " * max(0, w - 1 - len(t)) + "\r")
        sys.stdout.flush()
        self.shown = True

    def clear(self):
        if self.on and self.shown:
            w = term_width() or 80
            sys.stdout.write("\r" + " " * (w - 1) + "\r")
            sys.stdout.flush()
        self.shown = False


class Report:
    """Salida en vivo por secciones, contadores y resumen final."""
    LABELS = collections.OrderedDict([
        ("ses", ("sesion", "sesiones")), ("mem", ("memoria", "memorias")),
        ("flow", (".claude-flow", ".claude-flow")),
        ("hue", ("artefacto huerfano", "artefactos huerfanos")),
        ("proj", ("proyecto vacio", "proyectos vacios"))])

    def __init__(self, dry=False):
        self.dry = dry
        self.seen = set()
        self.n = collections.Counter()
        self.b = collections.Counter()
        self.activas = 0
        self.errores = 0

    def section(self, name):
        if name not in self.seen:
            self.seen.add(name)
            if len(self.seen) > 1:
                print(flush=True)
            print(col(name, "1"), flush=True)

    def line(self, text):
        print(fit(text), flush=True)

    def add(self, kind, size=0):
        self.n[kind] += 1
        self.b[kind] += size or 0

    def fail(self, ident, motivo, activa=False):
        if activa:
            self.activas += 1
        else:
            self.errores += 1
        sys.stdout.flush()
        print(col(fit("no se pudo  %s  %s" % (ident, motivo)), "31"), file=sys.stderr, flush=True)

    def moved(self):
        return sum(self.n.values())

    def summary(self):
        parts = []
        for k, (uno, varios) in self.LABELS.items():
            if not self.n[k]:
                continue
            t = plural(self.n[k], uno, varios)
            if k in ("ses", "flow"):
                t += " (%s)" % human(self.b[k])
            parts.append(t)
        total = sum(self.b.values())
        print(file=sys.stdout, flush=True)
        if parts:
            print("%s %s. Total: %s." % ("Se llevaria:" if self.dry else "Listo:", ", ".join(parts), human(total)),
                  flush=True)
        self.fails_line()
        if self.dry:
            print("Modo --dry-run: no se modifico nada.", flush=True)
        else:
            self.hint()

    def fails_line(self):
        fails = self.activas + self.errores
        if fails:
            why = []
            if self.activas:
                why.append("activas" if not self.errores else "%d activas" % self.activas)
            if self.errores:
                why.append("con error" if not self.activas else "%d con error" % self.errores)
            print("%s (%s)." % (plural(fails, "no se pudo", "no se pudieron"), ", ".join(why)), flush=True)

    def hint(self):
        if self.moved() and not self.dry:
            print("Todo quedo en la papelera. Para recuperar algo: ses restore <id>  (ses restore lista todo)",
                  flush=True)


# ---------------------------------------------------------------- papelera
def papelera():
    return os.path.join(root(), "ses-papelera")


def do_moves(moves):
    """moves: [(src, dst)]. Revierte si falla."""
    done = []
    try:
        for src, dst in moves:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            done.append((src, dst))
    except Exception as e:
        for src, dst in reversed(done):
            try:
                shutil.move(dst, src)
            except Exception:
                pass
        raise SesError("fallo al mover (%s); se revirtio lo movido" % e)


def trash_session(s, take_memory=False):
    """Manda sesion + artefactos a la papelera. Devuelve True si se llevo memory."""
    u = s["uuid"]
    check_uuid(u)
    check_safe(s["path"], u + ".jsonl")
    arts = artifacts_of(s)
    for k, v in arts.items():
        check_safe(v, u)
    took = False
    mem = os.path.join(s["proj_dir"], "memory")
    if take_memory and os.path.isdir(mem) and not os.path.islink(mem):
        check_safe(mem, "memory", memory="proj")
        arts["memory"] = mem
        took = True
    tdir = os.path.join(papelera(), u)
    if os.path.lexists(tdir):
        raise SesError("ya hay algo en la papelera con ese id: %s" % tdir)
    meta = dict(uuid=u, ruta_original=s["path"], artefactos=arts, titulo=s.get("title", ""),
                proyecto=s.get("proyecto", ""), incluye_memoria=took,
                borrada_en=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
    moves = [(s["path"], os.path.join(tdir, "sesion.jsonl"))]
    for k, v in arts.items():
        moves.append((v, os.path.join(tdir, "artefactos", k)))
    check_safe_dest(tdir)
    try:
        os.makedirs(tdir)
        with open(os.path.join(tdir, "meta.json"), "w") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        do_moves(moves)
    except Exception as e:
        cleanup_empty(tdir)
        raise e if isinstance(e, SesError) else SesError(str(e))
    return took


def cleanup_empty(d):
    """Saca meta.json y directorios vacios; nunca borra datos reales."""
    try:
        os.remove(os.path.join(d, "meta.json"))
    except OSError:
        pass
    for dp, _dn, _f in sorted(os.walk(d), key=lambda x: -len(x[0])):
        try:
            os.rmdir(dp)
        except OSError:
            pass


def check_safe_dest(path):
    real = os.path.realpath(path)
    pap = os.path.realpath(papelera())
    if not real.startswith(pap + os.sep):
        raise SesError("destino fuera de la papelera: %s" % path)


def trash_entries():
    pap = papelera()
    out = []
    if not os.path.isdir(pap):
        return out
    for n in sorted(os.listdir(pap)):
        d = os.path.join(pap, n)
        mp = os.path.join(d, "meta.json")
        if not UUID_RE.match(n) or not os.path.isdir(d) or os.path.islink(d):
            continue
        try:
            with open(mp) as f:
                m = json.load(f)
        except (OSError, ValueError):
            m = {}
        out.append(dict(uuid=n, dir=d, meta=m, title=m.get("titulo", ""),
                        proyecto=m.get("proyecto", ""), borrada=m.get("borrada_en", "?")))
    for sub in ("_huerfanos", "_flow"):
        hd = os.path.join(pap, sub)
        if not os.path.isdir(hd) or os.path.islink(hd):
            continue
        for n in sorted(os.listdir(hd)):
            d = os.path.join(hd, n)
            if not os.path.isdir(d) or os.path.islink(d):
                continue
            try:
                with open(os.path.join(d, "meta.json")) as f:
                    m = json.load(f)
            except (OSError, ValueError):
                m = {}
            orig = m.get("ruta_original", "")
            if sub == "_flow":
                tipo, title, proy = "flow", tilde(orig) or n, os.path.dirname(orig)
            else:
                tipo = m.get("tipo") or ("memoria" if "memoria" in m.get("motivo", "") else "huerfano")
                title = "%s: %s" % (tipo, m.get("nombre", n))
                proy = os.path.basename(os.path.dirname(orig))
            out.append(dict(uuid=hashlib.sha1((sub + n).encode()).hexdigest(), dir=d, meta=m,
                            huerfano=True, tipo=tipo, title=title, proyecto=proy,
                            borrada=m.get("borrada_en", "?")))
    return out


def restore_huerfano(e):
    m = e["meta"]
    src = os.path.join(e["dir"], "contenido")
    dst = m.get("ruta_original", "")
    if not dst or not os.path.isdir(src):
        raise SesError("meta.json o contenido invalido en %s" % e["dir"])
    check_safe(src, "contenido")
    parent = os.path.realpath(os.path.dirname(dst))
    projs = os.path.realpath(os.path.join(root(), "projects"))
    if e["tipo"] == "flow":
        frt = os.path.realpath(flow_root())
        if os.path.basename(dst) != ".claude-flow" or not (parent == frt or parent.startswith(frt + os.sep)):
            raise SesError("destino de .claude-flow invalido: %s" % dst)
    elif e["tipo"] == "memoria":
        if os.path.basename(dst) != "memory" or os.path.dirname(parent) != projs:
            raise SesError("destino de memory invalido: %s" % dst)
    else:
        check_uuid(os.path.basename(dst))
        if parent != projs and os.path.dirname(parent) != projs:
            raise SesError("destino fuera de projects: %s" % dst)
    if os.path.lexists(dst):
        raise SesError("el destino ya existe, no se toca nada: %s" % dst)
    do_moves([(src, dst)])
    cleanup_empty(e["dir"])


def restore_entry(e):
    if e.get("huerfano"):
        return restore_huerfano(e)
    u = e["uuid"]
    m = e["meta"]
    if not m or m.get("uuid") != u:
        raise SesError("meta.json ausente o invalido en %s" % e["dir"])
    moves = []
    src_j = os.path.join(e["dir"], "sesion.jsonl")
    if not os.path.isfile(src_j):
        raise SesError("falta sesion.jsonl en la papelera")
    pairs = [(src_j, m["ruta_original"], u + ".jsonl")]
    for k, orig in (m.get("artefactos") or {}).items():
        if k not in ARTIFACT_KINDS:
            raise SesError("artefacto desconocido: %s" % k)
        pairs.append((os.path.join(e["dir"], "artefactos", k), orig, "memory" if k == "memory" else u))
    for src, dst, name in pairs:
        is_mem = name == "memory"
        check_safe(src, os.path.basename(src), memory="trash" if is_mem else None)
        if os.path.basename(dst) != name:
            raise SesError("destino con nombre inesperado: %s" % dst)
        parent = os.path.realpath(os.path.dirname(dst))
        projs = os.path.realpath(os.path.join(root(), "projects"))
        if is_mem:
            if os.path.dirname(parent) != projs:
                raise SesError("destino de memory fuera de projects/<proy>: %s" % dst)
        elif not any((parent + os.sep).startswith(a + os.sep) for a in allowed_roots()[:3]):
            raise SesError("destino fuera de las rutas permitidas: %s" % dst)
        if os.path.lexists(dst):
            raise SesError("el destino ya existe, no se toca nada: %s" % dst)
        moves.append((src, dst))
    do_moves(moves)
    try:
        os.remove(os.path.join(e["dir"], "meta.json"))
        ad = os.path.join(e["dir"], "artefactos")
        if os.path.isdir(ad):
            os.rmdir(ad)
        os.rmdir(e["dir"])
    except OSError:
        pass


# ---------------------------------------------------------------- comandos
def parse_dias(tok):
    try:
        d = int(tok)
    except (TypeError, ValueError):
        raise UsageError("<dias> tiene que ser un entero mayor a 0")
    if d <= 0:
        raise UsageError("<dias> tiene que ser un entero mayor a 0")
    return d


def cmd_list(a):
    args = list(a.args)
    dias = None
    if args and args[0] == "all":
        args.pop(0)
    elif args and args[0] == "old":
        if len(args) < 2:
            raise UsageError("'list old' necesita los dias: ses list old <dias>")
        dias = parse_dias(args[1])
        del args[:2]
    if len(args) > 1:
        raise UsageError("sobran argumentos: %s" % " ".join(args[1:]))
    a.texto = args[0] if args else None
    ss = scan()
    if dias is not None:
        lim = time.time() - dias * 86400
        ss = [s for s in ss if s["mtime"] < lim]
    if a.orphans:
        ss = [s for s in ss if s["existe"] is False]
    if a.project:
        ss = [s for s in ss if a.project.lower() in s["proyecto"].lower()]
    if a.texto:
        t = a.texto.lower()
        ss = [s for s in ss if t in s["title"].lower() or t in s["proyecto"].lower() or t in s["uuid"]]
    ss.sort(key=(lambda s: -s["size"]) if a.sort == "size" else (lambda s: -s["mtime"]))
    if a.json:
        out = [dict(uuid=s["uuid"], fecha=fmt_ts(s["mtime"]), mtime=s["mtime"], tamano=s["size"],
                    mensajes=s["info"]["n_user"] + s["info"]["n_asst"], proyecto=s["proyecto"],
                    existe=s["existe"], activa=is_active(s["mtime"]), titulo=s["title"]) for s in ss]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    rows = []
    for s in ss:
        marks = ""
        if s["existe"] is False:
            marks += " " + col("[borrado]", "31")
        if is_active(s["mtime"]):
            marks += " " + col("[activa]", "33")
        proj = tilde(s["proyecto"])
        if len(proj) > 32:
            proj = "..." + proj[-29:]
        rows.append((s["uuid"][:8], fmt_ts(s["mtime"]), human(s["size"]),
                     str(s["info"]["n_user"] + s["info"]["n_asst"]), proj, s["title"] + marks))
    if rows:
        hdr = ("ID", "MODIFICADA", "TAMANO", "MSJ", "PROYECTO", "TITULO")
        w = [max(len(r[i]) for r in rows + [hdr]) for i in range(5)]
        fmt = lambda r: "  ".join(r[i].ljust(w[i]) if i != 3 and i != 2 else r[i].rjust(w[i])
                                  for i in range(5)) + "  " + r[5]
        print(col(fmt(hdr), "1"))
        for r in rows:
            print(fmt(r))
    else:
        print("No hay sesiones que coincidan.")
    print("\n%d sesiones, %s en total" % (len(ss), human(sum(s["size"] for s in ss))))
    return 0


_FOLD = {}


def _fold_char(c):
    f = _FOLD.get(c)
    if f is None:
        low = c.lower()
        base = [x for x in unicodedata.normalize("NFKD", low) if not unicodedata.combining(x)]
        f = base[0] if base and len(low) == 1 else (low[0] if low else c)
        _FOLD[c] = f
    return f


def fold(t):
    """Minusculas y sin tildes, letra por letra: el largo no cambia (sirve para ubicar el fragmento)."""
    if t.isascii():
        return t.lower()
    return "".join(_fold_char(c) for c in t)


def msg_text(obj):
    """Texto escrito por el usuario o por Claude (sin salidas de herramientas)."""
    t = obj.get("type")
    if t == "user":
        return real_text(obj)
    if t != "assistant":
        return None
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content
                         if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    return None


def session_text(path):
    """Todos los mensajes de la sesion en un solo texto."""
    parts = []
    with open(path, "rb") as f:
        for raw in f:
            if b'"user"' not in raw and b'"assistant"' not in raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if isinstance(obj, dict):
                txt = msg_text(obj)
                if txt and txt.strip():
                    parts.append(txt.strip())
    return "\n".join(parts)


def full_title(info):
    for c in (info["custom"], info["lastp"],
              info["first_real"][0] if info["first_real"] else None):
        if c and c.strip():
            return one_line(c, 200)
    return "(sin titulo)"


COMUNES = set("""
que con los las del por para una uno unos unas como pero mas sin sus son esta este esto estos estas ese esa eso
esos esas aca ahi alla hay muy tan todo toda todos todas otro otra otros otras cual cuales quien donde cuando
porque pues entre sobre hasta desde tambien solo ser fue era sea estar estoy esta estan tiene tengo tener hace
hacer hago puede puedo poder ver mira algo nada cada mismo misma bien asi ademas antes despues ahora luego aun
the and for with that this from are was were you your have has not but all can will its
""".split())


def query_words(texto):
    words = []
    for w in fold(texto).split():
        w = w.strip(".,;:!?\"'()[]{}")
        if len(w) >= 3 and w not in COMUNES and w not in words:
            words.append(w)
    return words


class Indice:
    """Texto de todas las sesiones en memoria, para buscar muchas veces sin releer los archivos."""

    def __init__(self, ss, progress=None):
        self.items = []
        for i, s in enumerate(ss, 1):
            if progress:
                progress("cargando sesiones... %d/%d" % (i, len(ss)))
            try:
                txt = session_text(s["path"])
            except OSError:
                continue
            extra = "%s\n%s" % (s["info"]["custom"] or "", s["proyecto"])
            title = full_title(s["info"])
            self.items.append(dict(s=s, title=title, ltitle=fold(title), text=txt, low=fold(txt),
                                   extra=fold(extra)))

    def buscar(self, words):
        """Sesiones donde aparece al menos una palabra: (item, {palabra: veces}), las mejores primero."""
        out = []
        for it in self.items:
            hits = {}
            for w in words:
                n = it["low"].count(w) + it["extra"].count(w)
                if n:
                    hits[w] = n
            if hits:
                en_titulo = sum(1 for w in hits if w in it["ltitle"])
                out.append((it, hits, en_titulo))
        out.sort(key=lambda r: (-len(r[1]), -r[2], -sum(r[1].values()), -r[0]["s"]["mtime"]))
        return [(it, h) for it, h, _ in out]

    @staticmethod
    def fragmento(it, words, ancho):
        """Pedazo del texto alrededor de la primera coincidencia: (texto, inicio, fin) de lo resaltado."""
        best = None
        rara = None
        for w in words:
            n = it["low"].count(w)
            if n and (rara is None or n < rara[0]):
                rara = (n, w)
        if rara:
            best = (it["low"].find(rara[1]), len(rara[1]))
        if best is None or ancho < 10:
            return "", 0, 0
        k, n = best
        a = max(0, k - ancho // 3)
        raw = it["text"][a:a + ancho * 2]
        pre = " ".join(raw[:k - a].split())
        if raw[:k - a][-1:].isspace():
            pre += " "
        if a > 0:
            pre = "..." + pre
        hit = " ".join(raw[k - a:k - a + n].split())
        post = raw[k - a + n:]
        post = (" " if post[:1].isspace() else "") + " ".join(post.split())
        line = (pre + hit + post)[:ancho]
        return line, len(pre), min(len(pre) + len(hit), len(line))


def cmd_search(a):
    if a.texto is None:
        return search_interactivo(a)
    words = query_words(a.texto)
    if not words:
        raise UsageError("pasa al menos una palabra de 3 letras o mas que no sea muy comun (que, para, con...)")
    st = Status()
    st.update("leyendo sesiones...", force=True)
    ss = scan()
    if a.project:
        ss = [s for s in ss if a.project.lower() in s["proyecto"].lower()]
    try:
        found = Indice(ss, st.update).buscar(words)
    finally:
        st.clear()
    if a.json:
        print(json.dumps([dict(uuid=it["s"]["uuid"], titulo=it["title"], fecha=fmt_ts(it["s"]["mtime"]),
                               mtime=it["s"]["mtime"], tamano=it["s"]["size"], proyecto=it["s"]["proyecto"],
                               coincidencias=h) for it, h in found], ensure_ascii=False, indent=2))
        return 0
    if not found:
        print("Ninguna sesion menciona: %s" % ", ".join(words))
        return 0
    for it, hits in found:
        s = it["s"]
        marks = " " + col("[activa]", "33") if is_active(s["mtime"]) else ""
        print(col(s["uuid"], "1") + marks)
        print("  titulo:     %s" % it["title"])
        print("  fecha:      %s" % fmt_ts(s["mtime"]))
        print("  tamano:     %s" % human(s["size"]))
        print("  proyecto:   %s" % tilde(s["proyecto"]))
        print("  coincide:   %s" % ", ".join("%s (%d)" % (w, n) for w, n in hits.items()))
        print()
    print("%s, %s en total" % (plural(len(found), "sesion", "sesiones"),
                               human(sum(it["s"]["size"] for it, _ in found))))
    return 0


# ---------------------------------------------------------------- buscador interactivo
def search_interactivo(a):
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise UsageError("el buscador interactivo necesita una terminal; si no, ses search \"texto\"")
    import curses
    st = Status()
    st.update("leyendo sesiones...", force=True)
    ss = scan()
    if a.project:
        ss = [s for s in ss if a.project.lower() in s["proyecto"].lower()]
    try:
        idx = Indice(ss, st.update)
    finally:
        st.clear()
    os.environ.setdefault("ESCDELAY", "25")
    elegido = curses.wrapper(lambda scr: Buscador(scr, idx, curses).run())
    if not elegido:
        return 0
    s = elegido["s"]
    cwd = s["cwd"]
    cmd = "cd %s && claude --resume %s" % (cwd or "<proyecto>", s["uuid"])
    if not cwd or not os.path.isdir(cwd):
        print("El proyecto de esa sesion ya no existe (%s).\nPara retomarla igual:\n  %s"
              % (tilde(s["proyecto"]), cmd), file=sys.stderr)
        return 1
    if not shutil.which("claude"):
        print("No encuentro 'claude' en el PATH. Para retomarla:\n  %s" % cmd, file=sys.stderr)
        return 1
    print("Retomando %s en %s" % (s["uuid"][:8], tilde(cwd)), flush=True)
    os.chdir(cwd)
    os.execvp("claude", ["claude", "--resume", s["uuid"]])


class Buscador:
    AYUDA = "arrows: move   Enter: resume   Tab: details   Ctrl-U: clear   Esc: quit"
    PROMPT = "search: "

    def __init__(self, scr, idx, curses):
        self.scr, self.idx, self.c = scr, idx, curses
        self.q = ""
        self.sel = 0
        self.top = 0
        self.res = []
        self.recientes = sorted(idx.items, key=lambda it: -it["s"]["mtime"])
        curses.curs_set(1)
        self.hl = curses.A_BOLD | curses.A_UNDERLINE
        self.dim = curses.A_DIM
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_YELLOW, -1)
            curses.init_pair(2, curses.COLOR_CYAN, -1)
            self.hl = curses.color_pair(1) | curses.A_BOLD
            self.dim = curses.color_pair(2)
        except curses.error:
            pass
        self.filtrar()

    def filtrar(self):
        self.words = query_words(self.q)
        if self.words:
            self.res = self.idx.buscar(self.words)
        else:
            self.res = [(it, {}) for it in self.recientes]
        self.sel = 0
        self.top = 0

    def put(self, y, x, txt, attr=0):
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and x < w - 1:
            try:
                self.scr.addnstr(y, x, txt, w - 1 - x, attr)
            except self.c.error:
                pass

    def dibujar(self):
        c, scr = self.c, self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        pl = len(self.PROMPT)
        self.put(0, 0, self.PROMPT, c.A_BOLD)
        self.put(0, pl, self.q)
        if self.words:
            info = plural(len(self.res), "session", "sessions")
        elif self.q.strip():
            info = "type at least 3 letters (very common words like 'que' or 'the' are ignored)"
        else:
            info = "%s, newest first" % plural(len(self.res), "session", "sessions")
        self.put(1, 0, info + "   " + self.AYUDA, self.dim)
        filas = max(1, (h - 3) // 2)
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + filas:
            self.top = self.sel - filas + 1
        y = 3
        for i in range(self.top, min(len(self.res), self.top + filas)):
            it, hits = self.res[i]
            s = it["s"]
            attr = c.A_REVERSE if i == self.sel else 0
            head = "%s  %s  %8s  %s" % (s["uuid"][:8], fmt_ts(s["mtime"]), human(s["size"]), it["title"])
            if is_active(s["mtime"]):
                head += "  [active]"
            self.put(y, 0, head.ljust(w - 1), attr)
            if self.words:
                frag, a, b = self.idx.fragmento(it, self.words, w - 6)
                if not frag:
                    frag, a, b = tilde(s["proyecto"]), 0, 0
                self.put(y + 1, 4, frag[:a], self.dim)
                self.put(y + 1, 4 + a, frag[a:b], self.hl)
                self.put(y + 1, 4 + b, frag[b:], self.dim)
            else:
                self.put(y + 1, 4, tilde(s["proyecto"]), self.dim)
            y += 2
        if not self.res and self.words:
            self.put(3, 0, "No session mentions: %s" % ", ".join(self.words))
        scr.move(0, min(pl + len(self.q), w - 1))
        scr.refresh()

    def detalle(self, it):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_ver(argparse.Namespace(id=it["s"]["uuid"]))
        lines = buf.getvalue().splitlines()
        top = 0
        self.c.curs_set(0)
        while True:
            self.scr.erase()
            h, _ = self.scr.getmaxyx()
            for k, ln in enumerate(lines[top:top + h - 1]):
                self.put(k, 0, ln)
            self.put(h - 1, 0, "arrows: scroll   any other key: back", self.dim)
            self.scr.refresh()
            ch = self.scr.getch()
            if ch == self.c.KEY_DOWN and top + h - 1 < len(lines):
                top += 1
            elif ch == self.c.KEY_UP and top > 0:
                top -= 1
            elif ch != self.c.KEY_RESIZE:
                break
        self.c.curs_set(1)

    def run(self):
        c = self.c
        while True:
            self.dibujar()
            try:
                ch = self.scr.get_wch()
            except c.error:
                continue
            n = len(self.res)
            if ch in ("\x1b",):
                return None
            if ch in ("\n", "\r", c.KEY_ENTER):
                if n:
                    return self.res[self.sel][0]
            elif ch == "\t":
                if n:
                    self.detalle(self.res[self.sel][0])
            elif ch == c.KEY_DOWN:
                self.sel = min(n - 1, self.sel + 1) if n else 0
            elif ch == c.KEY_UP:
                self.sel = max(0, self.sel - 1)
            elif ch == c.KEY_NPAGE:
                self.sel = min(n - 1, self.sel + 10) if n else 0
            elif ch == c.KEY_PPAGE:
                self.sel = max(0, self.sel - 10)
            elif ch in (c.KEY_BACKSPACE, "\x7f", "\b"):
                self.q = self.q[:-1]
                self.filtrar()
            elif ch == "\x15":
                self.q = ""
                self.filtrar()
            elif ch == "\x17":
                self.q = self.q.rstrip()
                self.q = self.q[:self.q.rfind(" ") + 1] if " " in self.q else ""
                self.filtrar()
            elif ch == "\x03":
                return None
            elif isinstance(ch, str) and ch.isprintable():
                self.q += ch
                self.filtrar()


def cmd_ver(a):
    s = resolve(a.id, scan())
    i = s["info"]
    arts = artifacts_of(s)
    total = s["size"] + sum(tree_size(v) for v in arts.values())
    print("uuid:            %s" % s["uuid"])
    print("proyecto:        %s" % (tilde(s["cwd"]) if s["cwd"] else "(desconocido: %s)" % os.path.basename(s["proj_dir"])))
    print("existe:          %s" % ("si" if s["existe"] else ("no" if s["existe"] is False else "?")))
    print("creada:          %s" % fmt_iso(i["first_ts"]))
    print("modificada:      %s" % fmt_ts(s["mtime"]))
    print("tamano total:    %s (sesion %s%s)" % (human(total), human(s["size"]),
          ", artefactos: " + ", ".join(arts) if arts else ""))
    print("mensajes:        %d user, %d assistant" % (i["n_user"], i["n_asst"]))
    print("lineas corruptas: %d" % i["corrupt"])
    print("titulo:          %s" % s["title"])
    first, last = i["first_real"], i["last_real"]
    shown = list(first)
    extra = i["real"] - len(first)
    if extra > 0:
        shown_last = last[-min(3, extra):]
    else:
        shown_last = []
    print("\nprimeros mensajes de usuario:")
    for m in first:
        print("  - " + one_line(m, 120))
    if shown_last:
        print("ultimos mensajes de usuario:")
        for m in shown_last:
            print("  - " + one_line(m, 120))
    cwd = s["cwd"] or "<proyecto>"
    print("\nretomar con:\n  cd %s && claude --resume %s" % (cwd, s["uuid"]))
    return 0


def session_row(s, rest=None):
    return row("sesion", s["uuid"][:8], s["size"],
               "%-24s %s" % (one_line(tilde(s["proyecto"]), 24),
                             one_line(rest if rest is not None else s["title"], 60)))


def trash_many(sessions, mem_filter=None, rep=None):
    """Manda a la papelera; memory del proyecto va con la ultima sesion del proyecto."""
    rep = rep or Report()
    rc = 0
    ok_list = []
    if sessions:
        rep.section("Sesiones")
    for s in sessions:
        if is_active(s["mtime"]):
            rep.fail(s["uuid"][:8], "activa, modificada hace menos de %g min" % (activa_seg() / 60), activa=True)
            rc = 1
        else:
            ok_list.append(s)
    groups = collections.OrderedDict()
    for s in ok_list:
        groups.setdefault(s["proj_dir"], []).append(s)
    for pd, grp in groups.items():
        try:
            all_j = {n for n in os.listdir(pd) if n.endswith(".jsonl")}
        except OSError:
            all_j = set()
        clears = all_j <= {x["uuid"] + ".jsonl" for x in grp}
        good = True
        for i, s in enumerate(grp):
            want = clears and good and i == len(grp) - 1
            prot = project_protected(pd, s.get("cwd"))
            if want and prot and os.path.isdir(os.path.join(pd, "memory")):
                protected_note = True
            else:
                protected_note = False
            if want and (prot or (mem_filter and not mem_filter(pd))):
                want = False
            memdir = os.path.join(pd, "memory")
            msz = tree_size(memdir) if want and os.path.isdir(memdir) else 0
            try:
                took = trash_session(s, take_memory=want)
            except SesError as e:
                rep.fail(s["uuid"][:8], e)
                rc = 1
                good = False
                continue
            rep.add("ses", s["size"])
            rep.line(session_row(s))
            if protected_note:
                rep.line("    memoria protegida (home): %s" % tilde(s["proyecto"]))
            if took:
                rep.add("mem", msz)
                rep.line(row("memoria", "", msz, "%-24s con la ultima sesion"
                             % one_line(tilde(s["proyecto"]), 24)))
        try:
            if os.path.isdir(pd) and not os.listdir(pd) and not project_protected(pd, grp[0].get("cwd")):
                check_proj_rmdir(pd)
                os.rmdir(pd)
        except (SesError, OSError):
            pass
    return rc


def cmd_borrar(a):
    if a.ids[0] == "old":
        if len(a.ids) != 2:
            raise UsageError("uso: ses delete old <dias> [--dry-run] [--project SUBSTR]")
        a.dias = parse_dias(a.ids[1])
        return cmd_old(a)
    if a.ids[0] == "junk":
        if len(a.ids) != 1 or a.project:
            raise UsageError("uso: ses delete junk [--dry-run]")
        return cmd_junk(a)
    if a.dry_run or a.project:
        raise UsageError("--dry-run y --project van con 'delete old' o 'delete junk'")
    ss = scan()
    sel = []
    rc = 0
    for i in a.ids:
        try:
            s = resolve(i, ss)
        except SesError as e:
            print("error: %s" % e, file=sys.stderr, flush=True)
            rc = 1
            continue
        if all(s["uuid"] != x["uuid"] for x in sel):
            sel.append(s)
    rep = Report()
    rc = trash_many(sel, rep=rep) or rc
    rep.fails_line()
    rep.hint()
    return rc


def cmd_restaurar(a):
    es = trash_entries()
    if not a.id:
        if not es:
            print("La papelera esta vacia.")
            return 0
        for e in es:
            kind = "[%s] " % e["tipo"] if e.get("huerfano") else ""
            print("%s  %s  %s  %s%s" % (e["uuid"][:8], e["borrada"], tilde(e["proyecto"]), kind,
                                        one_line(e["title"], 60)))
        print("\nPara recuperar algo: ses restore <id>")
        return 0
    e = resolve(a.id, es, "sesion en la papelera")
    restore_entry(e)
    print("Restaurada %s: %s" % (e["uuid"][:8], one_line(e["title"], 60)))
    if "memory" in (e["meta"].get("artefactos") or {}):
        print("tambien se devolvio la memoria del proyecto %s" % tilde(e["proyecto"]))
    return 0


def basura_find(ss):
    """-> lista de (tipo, motivo, objeto)."""
    found = []
    for s in ss:
        i = s["info"]
        if s["size"] == 0:
            found.append(("sesion", "archivo de 0 bytes", s))
        elif i["valid"] == 0:
            found.append(("sesion", "ninguna linea JSON valida", s))
        elif i["real"] == 0 and i["n_asst"] == 0:
            found.append(("sesion", "vacia (sin mensajes)", s))
    uuids = {s["uuid"] for s in ss}
    for pd in project_dirs():
        names = os.listdir(pd)
        for n in sorted(names):
            p = os.path.join(pd, n)
            if UUID_RE.match(n) and os.path.isdir(p) and not os.path.islink(p) and n not in uuids \
                    and not os.path.lexists(os.path.join(pd, n + ".jsonl")):
                found.append(("huerfano", "artefacto huerfano (no existe %s.jsonl)" % n[:8],
                              dict(uuid=n, path=p, proj_dir=pd, mtime=os.path.getmtime(p), name=n)))
        if not any(n.endswith(".jsonl") and os.path.isfile(os.path.join(pd, n)) for n in names):
            mem = os.path.join(pd, "memory")
            hasmem = os.path.isdir(mem) and bool(os.listdir(mem))
            if hasmem and project_protected(pd):
                continue
            found.append(("proyecto", "proyecto vacio" + (" (con memoria)" if hasmem else ""),
                          dict(path=pd, name=os.path.basename(pd))))
    return found


def to_huerfanos(path, label, expected, motivo, memory=None, tipo="huerfano"):
    check_safe(path, expected, memory)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = os.path.join(papelera(), "_huerfanos", "%s-%s" % (ts, label))
    check_safe_dest(d)
    if os.path.lexists(d):
        raise SesError("destino ya existe: %s" % d)
    meta = dict(nombre=label, tipo=tipo, ruta_original=path, motivo=motivo,
                borrada_en=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
    try:
        os.makedirs(d)
        with open(os.path.join(d, "meta.json"), "w") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        do_moves([(path, os.path.join(d, "contenido"))])
    except Exception as e:
        cleanup_empty(d)
        raise e if isinstance(e, SesError) else SesError(str(e))


def junk_project(o):
    pd = o["path"]
    if not os.path.isdir(pd):
        return "ya no existe"
    if any(n.endswith(".jsonl") for n in os.listdir(pd)):
        raise SesError("ahora tiene sesiones, no se toca")
    msg = "carpeta vacia borrada"
    mem = os.path.join(pd, "memory")
    if project_protected(pd) and os.path.isdir(mem):
        return "memoria protegida (home), no se toca"
    if os.path.isdir(mem) and not os.path.islink(mem) and os.listdir(mem):
        to_huerfanos(mem, o["name"], "memory", "memoria de proyecto vacio", memory="proj", tipo="memoria")
        msg = "memoria a la papelera (_huerfanos) y carpeta borrada"
    elif os.path.isdir(mem) and not os.path.islink(mem):
        os.rmdir(mem)  # memory/ vacia: no hay nada que guardar
    check_proj_rmdir(pd)
    os.rmdir(pd)
    return msg


def cmd_junk(a):
    ss = scan()
    found = basura_find(ss)
    if not found:
        print("No se encontro basura.")
        return 0
    rep = Report(a.dry_run)
    if a.dry_run:
        for kind, titulo in (("sesion", "Sesiones"), ("huerfano", "Artefactos huerfanos"),
                             ("proyecto", "Proyectos vacios")):
            for k, why, o in found:
                if k != kind:
                    continue
                rep.section(titulo)
                if k == "sesion":
                    rep.add("ses", o["size"])
                    rep.line(session_row(o, "%s  %s" % (why, o["title"])))
                elif k == "huerfano":
                    sz = tree_size(o["path"])
                    rep.add("hue", sz)
                    rep.line(row("huerfano", o["uuid"][:8], sz, why))
                else:
                    rep.add("proj")
                    rep.line(row("proyecto", "", None, "%s  %s" % (why, o["name"])))
        rep.summary()
        return 0
    rc = trash_many([o for k, _w, o in found if k == "sesion"], rep=rep)
    for kind, why, o in found:
        try:
            if kind == "huerfano":
                if is_active(o["mtime"]):
                    raise SesError("activo, no se toca")
                sz = tree_size(o["path"])
                to_huerfanos(o["path"], o["uuid"], o["uuid"], "artefacto huerfano")
                rep.section("Artefactos huerfanos")
                rep.add("hue", sz)
                rep.line(row("huerfano", o["uuid"][:8], sz, "a _huerfanos"))
            elif kind == "proyecto":
                msg = junk_project(o)
                rep.section("Proyectos vacios")
                if not msg.startswith(("memoria protegida", "ya no existe")):
                    rep.add("proj")
                rep.line(row("proyecto", "", None, "%s  %s" % (msg, o["name"])))
        except (SesError, OSError) as e:
            rep.fail(o.get("uuid", o.get("name"))[:30], e)
            rc = 1
    rep.summary()
    return rc


def check_proj_rmdir(p):
    real = os.path.realpath(p)
    pr = os.path.realpath(os.path.join(root(), "projects"))
    if os.path.islink(p) or os.path.dirname(real) != pr or os.path.basename(p) == "memory":
        raise SesError("ruta de proyecto no segura: %s" % p)
    if os.listdir(p):
        raise SesError("la carpeta no esta vacia, no se toca")


def flow_root():
    return os.environ.get("SES_FLOW_ROOT") or os.path.expanduser("~")


def find_flows(progress=None):
    rt = os.path.realpath(flow_root())
    if not os.path.isdir(rt):
        return []
    dev = os.stat(rt).st_dev
    pap = os.path.realpath(papelera())
    res = []
    for dp, dirnames, _f in os.walk(rt, followlinks=False, onerror=lambda e: None):
        if progress:
            progress("buscando .claude-flow... %d encontradas, revisando %s" % (len(res), tilde(dp)))
        keep = []
        for d in dirnames:
            p = os.path.join(dp, d)
            if os.path.islink(p):
                continue
            if d == ".claude-flow":
                res.append(p)
                continue
            if d in ("node_modules", ".git") or os.path.realpath(p) == pap:
                continue
            try:
                if os.stat(p).st_dev != dev:
                    continue
            except OSError:
                continue
            keep.append(d)
        dirnames[:] = keep
    out = []
    for i, p in enumerate(sorted(res)):
        if progress:
            progress("midiendo .claude-flow... %d/%d, %s" % (i + 1, len(res), tilde(p)))
        m = newest_mtime(p) or os.lstat(p).st_mtime
        out.append(dict(path=p, mtime=m, size=tree_size(p)))
    return out


def check_flow_safe(p):
    if os.path.basename(p) != ".claude-flow" or os.path.islink(p):
        raise SesError("no es una .claude-flow valida: %s" % p)
    real = os.path.realpath(p)
    if real != os.path.join(os.path.realpath(os.path.dirname(p)), ".claude-flow"):
        raise SesError("es un symlink: %s" % p)
    if not real.startswith(os.path.realpath(flow_root()) + os.sep):
        raise SesError("fuera de la raiz: %s" % p)


def trash_flows(flows, rep=None):
    rep = rep or Report()
    rc = 0
    base = os.path.join(papelera(), "_flow")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    n = 0
    if flows:
        rep.section(".claude-flow")
    for f in flows:
        try:
            check_flow_safe(f["path"])
            n += 1
            d = os.path.join(base, "%s-%d" % (ts, n))
            while os.path.lexists(d):
                n += 1
                d = os.path.join(base, "%s-%d" % (ts, n))
            check_safe_dest(d)
            os.makedirs(d)
            try:
                with open(os.path.join(d, "meta.json"), "w") as fh:
                    json.dump(dict(ruta_original=f["path"], tamano=f["size"],
                                   borrada_en=datetime.now().strftime("%Y-%m-%dT%H:%M:%S")),
                              fh, ensure_ascii=False, indent=2)
                do_moves([(f["path"], os.path.join(d, "contenido"))])
            except Exception as e:
                cleanup_empty(d)
                raise e if isinstance(e, SesError) else SesError(str(e))
            rep.add("flow", f["size"])
            rep.line(row("flow", "", f["size"], tilde(f["path"])))
        except SesError as e:
            rep.fail(tilde(f["path"]), e)
            rc = 1
    return rc


def newest_mtime(p):
    best = 0.0
    for d, _dn, files in os.walk(p):
        for f in files:
            try:
                best = max(best, os.lstat(os.path.join(d, f)).st_mtime)
            except OSError:
                pass
    return best


def cmd_old(a):
    lim = time.time() - a.dias * 86400
    st = Status()
    st.update("leyendo sesiones...", force=True)
    all_s = scan()
    ss = [s for s in all_s if s["mtime"] < lim]
    if a.project:
        ss = [s for s in ss if a.project.lower() in s["proyecto"].lower()]
    ss.sort(key=lambda s: s["mtime"])
    # memorias sin uso, a nivel proyecto
    by_proj = {}
    for s in all_s:
        by_proj.setdefault(s["proj_dir"], []).append(s)
    mems = []
    for pd in project_dirs():
        mem = os.path.join(pd, "memory")
        if not os.path.isdir(mem) or os.path.islink(mem) or not os.listdir(mem):
            continue
        sess = by_proj.get(pd, [])
        name = sess[0]["proyecto"] if sess else os.path.basename(pd)
        if a.project and a.project.lower() not in name.lower():
            continue
        if project_protected(pd, sess[0]["cwd"] if sess else None):
            st.clear()
            print("memoria protegida (home): %s" % tilde(name), flush=True)
            continue
        if newest_mtime(mem) >= lim:
            continue
        if any(s["mtime"] >= lim for s in sess):
            continue
        mems.append(dict(path=mem, pd=pd, name=name, folder=os.path.basename(pd), sessions=sess))
    try:
        flows = find_flows(st.update)
    finally:
        st.clear()
    if not ss and not mems and not flows:
        print("No hay sesiones ni memorias con mas de %d dias, ni carpetas .claude-flow." % a.dias)
        return 0
    rep = Report(a.dry_run)
    if a.dry_run:
        if ss:
            rep.section("Sesiones")
        for s in ss:
            rep.add("ses", s["size"])
            rep.line(session_row(s))
        if mems:
            rep.section("Memorias")
        for m in mems:
            msz = tree_size(m["path"])
            rep.add("mem", msz)
            rep.line(mem_row(m, msz))
        if flows:
            rep.section(".claude-flow")
        for f in flows:
            rep.add("flow", f["size"])
            rep.line(row("flow", "", f["size"], tilde(f["path"])))
        rep.summary()
        return 0
    ok_dirs = {m["pd"] for m in mems}
    rc = trash_many(ss, mem_filter=lambda pd: pd in ok_dirs, rep=rep)
    for m in mems:
        if m["sessions"]:
            continue
        try:
            msz = tree_size(m["path"])
            to_huerfanos(m["path"], m["folder"], "memory", "memoria sin uso", memory="proj", tipo="memoria")
            rep.section("Memorias")
            rep.add("mem", msz)
            rep.line(mem_row(m, msz))
            if os.path.isdir(m["pd"]) and not os.listdir(m["pd"]):
                check_proj_rmdir(m["pd"])
                os.rmdir(m["pd"])
        except (SesError, OSError) as e:
            rep.fail(m["folder"], e)
            rc = 1
    rc = trash_flows(flows, rep) or rc
    rep.summary()
    return rc


def mem_row(m, msz):
    nm = m["name"]
    ident = os.path.basename(nm.rstrip("/")) if nm.startswith("/") else nm[-8:]
    note = "viaja con la ultima sesion" if m["sessions"] else "sin sesiones"
    return row("memoria", one_line(ident, 8), msz, "%s  %s" % (tilde(nm) if nm.startswith("/") else nm, note))


def cmd_empty(a):
    pap = papelera()
    if not os.path.isdir(pap) or os.path.islink(pap):
        print("La papelera esta vacia.")
        return 0
    kids = sorted(os.listdir(pap))
    if not kids:
        print("La papelera esta vacia.")
        return 0
    nses = len([k for k in kids if k not in ("_huerfanos", "_flow")])
    nor = len(os.listdir(os.path.join(pap, "_huerfanos"))) if "_huerfanos" in kids else 0
    nfl = len(os.listdir(os.path.join(pap, "_flow"))) if "_flow" in kids else 0
    size = tree_size(pap)
    print("La papelera tiene %d sesiones, %d artefactos huerfanos y %d carpetas .claude-flow, %s en total."
          % (nses, nor, nfl, human(size)))
    if a.dry_run:
        print("Modo --dry-run: no se modifico nada (vaciar es irreversible).")
        return 0
    print("Borrando definitivamente (irreversible)...")
    check_pap = os.path.realpath(pap)
    if check_pap != os.path.join(os.path.realpath(root()), "ses-papelera"):
        raise SesError("la papelera no es segura (symlink?)")
    rc = 0
    for k in kids:
        p = os.path.join(pap, k)
        try:
            if os.path.islink(p) or os.path.isfile(p):
                os.unlink(p)
            else:
                shutil.rmtree(p)
        except OSError as e:
            print("no se pudo borrar %s: %s" % (k, e), file=sys.stderr)
            rc = 1
    print("Papelera vaciada." if not rc else "Papelera vaciada con errores.")
    return rc


# ---------------------------------------------------------------- CLI
class UsageError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


HELP = """\
ses: gestor de sesiones de Claude Code.

Las sesiones se mueven a una papelera (<raiz>/ses-papelera) y se pueden
restaurar hasta que hagas 'ses empty'. Raiz: $CLAUDE_HOME (por defecto ~/.claude).

Comandos:
  ses list [all] [texto] [--project SUBSTR] [--orphans] [--sort date|size] [--json]
  ses list old <dias> [texto] [...mismas opciones]
  ses search [--project SUBSTR]              buscador interactivo
  ses search "texto" [--project SUBSTR] [--json]
  ses show <id>
  ses delete <id> [<id>...]
  ses delete old <dias> [--dry-run] [--project SUBSTR]
  ses delete junk [--dry-run]
  ses restore [<id>]
  ses empty [--dry-run]

Notas:
  'delete old' ademas manda a la papelera TODAS las carpetas .claude-flow (las
  crea el plugin ruflo) bajo $HOME (o $SES_FLOW_ROOT), sin importar su antiguedad.
  --project NO filtra flows y 'delete junk' no las toca. restore las lista como [flow].
  'delete junk' se lleva todo lo roto o vacio, sin importar la edad.

Ejemplos:
  ses list all                   todas las sesiones
  ses list arduino --sort size   busca "arduino", las mas pesadas primero
  ses list old 30                sesiones sin uso hace mas de 30 dias
  ses list --orphans             sesiones cuyo proyecto ya no existe
  ses search                     buscador: tipeas y abajo salen las sesiones
  ses search "bug login"         sesiones que mencionan "bug" o "login"
  ses show 3f2a                  detalle de una sesion (id o prefijo)
  ses delete 3f2a 9c1d           a la papelera
  ses delete old 30 --dry-run    que se llevaria, sin tocar nada
  ses delete old 30              sesiones de mas de 30 dias a la papelera
  ses delete junk --dry-run      mira que basura hay, sin tocar nada
  ses delete junk                manda la basura a la papelera
  ses restore                    lista la papelera
  ses restore 3f2a               vuelve todo a su lugar
  ses empty                      borra la papelera para siempre (irreversible)

Si borrar deja a un proyecto sin sesiones, la carpeta memory/ del proyecto
viaja a la papelera con la ultima sesion, y restore la devuelve.
Salida: 0 ok, 1 error o rechazo parcial, 2 uso incorrecto.
Las sesiones modificadas hace menos de 10 min (SES_ACTIVA_MIN) no se tocan.
"""


def build():
    p = Parser(prog="ses", description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter,
               add_help=False)
    sp = p.add_subparsers(dest="cmd", parser_class=Parser)
    c = sp.add_parser("list", add_help=False)
    c.add_argument("args", nargs="*")
    c.add_argument("--project")
    c.add_argument("--orphans", action="store_true")
    c.add_argument("--sort", choices=("date", "size"), default="date")
    c.add_argument("--json", action="store_true")
    c = sp.add_parser("search", add_help=False)
    c.add_argument("texto", nargs="?")
    c.add_argument("--project")
    c.add_argument("--json", action="store_true")
    sp.add_parser("show", add_help=False).add_argument("id")
    c = sp.add_parser("delete", add_help=False)
    c.add_argument("ids", nargs="+")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--project")
    sp.add_parser("restore", add_help=False).add_argument("id", nargs="?")
    sp.add_parser("empty", add_help=False).add_argument("--dry-run", action="store_true")
    return p


COMMANDS = {"list": cmd_list, "search": cmd_search, "show": cmd_ver, "delete": cmd_borrar, "restore": cmd_restaurar,
            "empty": cmd_empty}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    p = build()
    try:
        if argv and argv[0] in ("--version", "-V"):
            print("ses %s" % VERSION)
            return 0
        if not argv or argv[0] in ("-h", "--help") or "--help" in argv or "-h" in argv:
            print(HELP)
            return 0 if argv else 2
        a = p.parse_args(argv)
        if not a.cmd:
            print(HELP)
            return 2
        return COMMANDS[a.cmd](a)
    except UsageError as e:
        print("ses: uso incorrecto: %s\nProba 'ses --help'." % e, file=sys.stderr)
        return 2
    except SesError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
