import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import ses  # noqa: E402

U_NORMAL = "aaaa1111-0000-4000-8000-000000000001"
U_AMBIG = "aaaa2222-0000-4000-8000-000000000002"
U_EMPTY = "bbbb1111-0000-4000-8000-000000000003"
U_BROKEN = "bbbb2222-0000-4000-8000-000000000004"
U_ZERO = "bbbb3333-0000-4000-8000-000000000005"
U_OLD = "cccc1111-0000-4000-8000-000000000006"
U_ACTIVE = "cccc2222-0000-4000-8000-000000000007"
U_ART = "dddd1111-0000-4000-8000-000000000008"
U_ORPH = "eeee1111-0000-4000-8000-000000000009"
U_SOLO = "ffff1111-0000-4000-8000-00000000000a"
U_SUB = "ffff2222-0000-4000-8000-00000000000b"


def line(**kw):
    return json.dumps(kw)


def conv(cwd, prompt="hola mundo", n=2):
    out = [line(type="user", cwd=cwd, timestamp="2026-01-01T10:00:00Z",
                message={"role": "user", "content": prompt})]
    for i in range(n):
        out.append(line(type="assistant", cwd=cwd, timestamp="2026-01-01T10:00:01Z",
                        message={"role": "assistant", "content": [{"type": "text", "text": "ok"}]}))
    return "\n".join(out) + "\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ses-test-")
        self.root = os.path.join(self.tmp, "claude")
        os.makedirs(os.path.join(self.root, "projects"))
        self.old_env = os.environ.get("CLAUDE_HOME")
        os.environ["CLAUDE_HOME"] = self.root
        os.environ["NO_COLOR"] = "1"
        # SIEMPRE una raiz temporal para .claude-flow: nunca recorrer el HOME real
        self.old_flow = os.environ.get("SES_FLOW_ROOT")
        os.environ["SES_FLOW_ROOT"] = os.path.join(self.tmp, "flowroot")
        os.makedirs(os.environ["SES_FLOW_ROOT"])
        self.assertTrue(ses.root().startswith(tempfile.gettempdir()))
        self.alive = os.path.join(self.tmp, "proyecto-vivo")
        os.makedirs(self.alive)
        self.dead = os.path.join(self.tmp, "proyecto-muerto")  # no existe
        self.pa = self.proj("-vivo")
        self.pd = self.proj("-muerto")

    def tearDown(self):
        if self.old_flow is None:
            os.environ.pop("SES_FLOW_ROOT", None)
        else:
            os.environ["SES_FLOW_ROOT"] = self.old_flow
        if self.old_env is None:
            os.environ.pop("CLAUDE_HOME", None)
        else:
            os.environ["CLAUDE_HOME"] = self.old_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def proj(self, name):
        p = os.path.join(self.root, "projects", name)
        os.makedirs(p, exist_ok=True)
        return p

    def mk(self, pdir, uuid, content, age_h=2):
        p = os.path.join(pdir, uuid + ".jsonl")
        with open(p, "w") as f:
            f.write(content)
        t = time.time() - age_h * 3600
        os.utime(p, (t, t))
        return p

    def run_ses(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ses.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def snapshot(self):
        res = set()
        for d, dirs, files in os.walk(self.root):
            for n in dirs + files:
                p = os.path.join(d, n)
                if os.sep + "cache" not in p:
                    res.add(p)
        return res

    def art(self, pdir, uuid):
        os.makedirs(os.path.join(pdir, uuid, "subagents"))
        with open(os.path.join(pdir, uuid, "subagents", "agent.jsonl"), "w") as f:
            f.write(conv(self.alive))
        for k in ("file-history", "session-env"):
            d = os.path.join(self.root, k, uuid)
            os.makedirs(d)
            with open(os.path.join(d, "x"), "w") as f:
                f.write("data")


class TestListAndResolve(Base):
    def setUp(self):
        super().setUp()
        self.mk(self.pa, U_NORMAL, conv(self.alive, "arreglar el bug del login"))
        self.mk(self.pa, U_AMBIG, conv(self.alive))
        self.mk(self.pd, U_OLD, conv(self.dead, "sesion vieja"), age_h=24 * 60)
        self.mk(self.pa, U_ACTIVE, conv(self.alive, "activa"), age_h=0)
        self.art(self.pa, U_NORMAL)

    def test_list(self):
        rc, out, _ = self.run_ses("list")
        self.assertEqual(rc, 0)
        self.assertIn("arreglar el bug del login", out)
        self.assertIn("[activa]", out)
        self.assertIn("[borrado]", out)
        self.assertIn("4 sesiones", out)
        # subagente no cuenta como sesion
        self.assertNotIn("agent", out)

    def test_filters_and_json(self):
        _, out, _ = self.run_ses("list", "--orphans")
        self.assertIn("sesion vieja", out)
        self.assertNotIn("login", out)
        _, out, _ = self.run_ses("list", "LOGIN")
        self.assertIn("1 sesiones", out)
        _, out, _ = self.run_ses("list", "--project", "muerto")
        self.assertIn("1 sesiones", out)
        rc, out, _ = self.run_ses("list", "--json", "--sort", "size")
        data = json.loads(out)
        self.assertEqual(len(data), 4)
        self.assertEqual(data[0]["mensajes"] + 0, data[0]["mensajes"])

    def test_ambiguous_prefix(self):
        before = self.snapshot()
        rc, _, err = self.run_ses("delete", "aaaa")
        self.assertEqual(rc, 1)
        self.assertIn(U_NORMAL, err)
        self.assertIn(U_AMBIG, err)
        self.assertEqual(before, self.snapshot())

    def test_no_match_and_show(self):
        rc, _, err = self.run_ses("show", "9999")
        self.assertEqual(rc, 1)
        rc, out, _ = self.run_ses("show", "aaaa1")
        self.assertEqual(rc, 0)
        self.assertIn("claude --resume " + U_NORMAL, out)
        self.assertIn("arreglar el bug", out)
        self.assertIn("2 assistant", out)

    def test_version(self):
        for flag in ("--version", "-V"):
            rc, out, _ = self.run_ses(flag)
            self.assertEqual(rc, 0)
            self.assertEqual(out.strip(), "ses " + ses.VERSION)

    def test_usage_error(self):
        rc, _, _ = self.run_ses("old", "abc")
        self.assertEqual(rc, 2)
        rc, _, _ = self.run_ses("old", "0")
        self.assertEqual(rc, 2)
        rc, _, _ = self.run_ses("nada")
        self.assertEqual(rc, 2)


class TestDeleteRestore(Base):
    def test_roundtrip_with_artifacts(self):
        self.mk(self.pa, U_ART, conv(self.alive, "con artefactos"))
        self.mk(self.pa, U_NORMAL, conv(self.alive))
        self.art(self.pa, U_ART)
        before = self.snapshot()
        rc, out, _ = self.run_ses("delete", "dddd")
        self.assertEqual(rc, 0)
        self.assertIn("dddd1111", out)
        self.assertEqual(out.count("Para recuperar algo"), 1)  # el aviso va una sola vez, al final
        self.assertIn("Todo quedo en la papelera", out.splitlines()[-1])
        t = os.path.join(self.root, "ses-papelera", U_ART)
        self.assertTrue(os.path.isfile(os.path.join(t, "sesion.jsonl")))
        for k in ("proyecto", "file-history", "session-env"):
            self.assertTrue(os.path.isdir(os.path.join(t, "artefactos", k)))
        meta = json.load(open(os.path.join(t, "meta.json")))
        self.assertEqual(meta["uuid"], U_ART)
        self.assertFalse(os.path.exists(os.path.join(self.pa, U_ART + ".jsonl")))
        rc, out, _ = self.run_ses("restore")
        self.assertIn("dddd1111", out)
        rc, out, _ = self.run_ses("restore", "dddd1111")
        self.assertEqual(rc, 0)
        self.assertEqual(before - {os.path.join(self.root, "ses-papelera")}, self.snapshot() - {os.path.join(self.root, "ses-papelera")})
        self.assertTrue(os.path.isfile(os.path.join(self.pa, U_ART, "subagents", "agent.jsonl")))

    def test_reject_active(self):
        self.mk(self.pa, U_ACTIVE, conv(self.alive), age_h=0)
        rc, out, err = self.run_ses("delete", U_ACTIVE)
        self.assertEqual(rc, 1)
        self.assertIn("no se pudo  " + U_ACTIVE[:8], err)
        self.assertIn("activa", err)
        self.assertIn("1 no se pudo (activas)", out)
        self.assertNotIn("Todo quedo en la papelera", out)  # no se movio nada
        self.assertTrue(os.path.exists(os.path.join(self.pa, U_ACTIVE + ".jsonl")))

    def test_partial_reject_continues(self):
        self.mk(self.pa, U_ACTIVE, conv(self.alive), age_h=0)
        self.mk(self.pa, U_NORMAL, conv(self.alive))
        rc, _, _ = self.run_ses("delete", U_ACTIVE, U_NORMAL)
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(os.path.join(self.pa, U_NORMAL + ".jsonl")))

    def test_restore_dest_exists(self):
        self.mk(self.pa, U_NORMAL, conv(self.alive))
        self.mk(self.pa, U_AMBIG, conv(self.alive))
        self.run_ses("delete", U_NORMAL)
        self.mk(self.pa, U_NORMAL, conv(self.alive, "nueva"))
        before = self.snapshot()
        rc, _, err = self.run_ses("restore", U_NORMAL)
        self.assertEqual(rc, 1)
        self.assertIn("ya existe", err)
        self.assertEqual(before, self.snapshot())

    def test_memory_goes_with_last_session(self):
        self.mk(self.pa, U_NORMAL, conv(self.alive))
        mem = os.path.join(self.pa, "memory")
        os.makedirs(mem)
        open(os.path.join(mem, "MEMORY.md"), "w").write("nota")
        rc, out, _ = self.run_ses("delete", U_NORMAL)
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"(?m)^memoria .*con la ultima sesion")
        t = os.path.join(self.root, "ses-papelera", U_NORMAL)
        self.assertTrue(os.path.isfile(os.path.join(t, "artefactos", "memory", "MEMORY.md")))
        self.assertTrue(json.load(open(os.path.join(t, "meta.json")))["incluye_memoria"])
        self.assertFalse(os.path.exists(self.pa))  # carpeta vacia -> rmdir
        rc, out, _ = self.run_ses("restore", U_NORMAL)
        self.assertEqual(rc, 0)
        self.assertEqual(open(os.path.join(mem, "MEMORY.md")).read(), "nota")
        self.assertTrue(os.path.isfile(os.path.join(self.pa, U_NORMAL + ".jsonl")))

    def test_memory_restore_refuses_if_exists(self):
        self.mk(self.pa, U_NORMAL, conv(self.alive))
        os.makedirs(os.path.join(self.pa, "memory"))
        open(os.path.join(self.pa, "memory", "a"), "w").write("x")
        self.run_ses("delete", U_NORMAL)
        os.makedirs(os.path.join(self.pa, "memory"))
        before = self.snapshot()
        rc, _, err = self.run_ses("restore", U_NORMAL)
        self.assertEqual(rc, 1)
        self.assertEqual(before, self.snapshot())

    def test_memory_untouched_if_other_sessions(self):
        self.mk(self.pa, U_NORMAL, conv(self.alive))
        self.mk(self.pa, U_AMBIG, conv(self.alive))
        mem = os.path.join(self.pa, "memory")
        os.makedirs(mem)
        open(os.path.join(mem, "MEMORY.md"), "w").write("nota")
        rc, out, _ = self.run_ses("delete", U_NORMAL)
        self.assertNotIn("memoria", out)
        self.assertTrue(os.path.isfile(os.path.join(mem, "MEMORY.md")))
        # borrar las dos juntas: memory viaja con la ultima
        rc, out, _ = self.run_ses("delete", U_AMBIG)
        self.assertRegex(out, r"(?m)^memoria .*con la ultima sesion")
        self.assertFalse(os.path.exists(mem))

    def test_memory_not_moved_if_last_rejected(self):
        self.mk(self.pa, U_NORMAL, conv(self.alive))
        self.mk(self.pa, U_ACTIVE, conv(self.alive), age_h=0)
        os.makedirs(os.path.join(self.pa, "memory"))
        self.run_ses("delete", U_NORMAL, U_ACTIVE)
        self.assertTrue(os.path.isdir(os.path.join(self.pa, "memory")))

    def test_check_safe_blocks(self):
        outside = os.path.join(self.tmp, "afuera")
        os.makedirs(outside)
        with self.assertRaises(ses.SesError):
            ses.check_safe(outside, "afuera")
        mem = os.path.join(self.pa, "memory")
        os.makedirs(mem)
        with self.assertRaises(ses.SesError):
            ses.check_safe(mem, "memory")
        link = os.path.join(self.pa, U_ART)
        os.symlink(outside, link)
        with self.assertRaises(ses.SesError):
            ses.check_safe(link, U_ART)


class TestJunkOldEmpty(Base):
    def setUp(self):
        super().setUp()
        self.mk(self.pa, U_NORMAL, conv(self.alive))
        self.mk(self.pa, U_EMPTY, line(type="summary", summary="x") + "\n")
        self.mk(self.pa, U_BROKEN, "esto no es json\n{roto\n")
        self.mk(self.pa, U_ZERO, "")
        self.mk(self.pa, U_ACTIVE, "", age_h=0)
        self.mk(self.pd, U_OLD, conv(self.dead), age_h=24 * 90)
        os.makedirs(os.path.join(self.pa, U_ORPH))
        open(os.path.join(self.pa, U_ORPH, "f"), "w").write("x")
        t = time.time() - 7200
        os.utime(os.path.join(self.pa, U_ORPH), (t, t))
        self.pe = self.proj("-vacio")
        self.pm = self.proj("-solo-memoria")
        os.makedirs(os.path.join(self.pm, "memory"))
        open(os.path.join(self.pm, "memory", "m.md"), "w").write("recuerdo")
        self.pmv = self.proj("-memoria-vacia")
        os.makedirs(os.path.join(self.pmv, "memory"))
        self.pcm = self.proj("-con-sesion-y-memoria")
        self.mk(self.pcm, U_SOLO, conv(self.alive))
        os.makedirs(os.path.join(self.pcm, "memory"))
        open(os.path.join(self.pcm, "memory", "m.md"), "w").write("x")

    def test_dry_run_changes_nothing(self):
        before = self.snapshot()
        rc, out, _ = self.run_ses("junk", "--dry-run")
        self.assertEqual(rc, 0)
        for w in ("0 bytes", "JSON valida", "vacia", "huerfano", "proyecto vacio"):
            self.assertIn(w, out)
        self.assertEqual(before, self.snapshot())
        self.run_ses("old", "30", "--dry-run")
        self.assertEqual(before, self.snapshot())
        self.run_ses("delete", "--help")
        self.assertEqual(before, self.snapshot())

    def test_junk_acts(self):
        rc, out, _ = self.run_ses("junk")
        self.assertEqual(rc, 1)  # la activa de 0 bytes se rechaza
        pap = os.path.join(self.root, "ses-papelera")
        for u in (U_EMPTY, U_BROKEN, U_ZERO):
            self.assertTrue(os.path.isdir(os.path.join(pap, u)), u)
            self.assertFalse(os.path.exists(os.path.join(self.pa, u + ".jsonl")))
        self.assertTrue(os.path.exists(os.path.join(self.pa, U_ACTIVE + ".jsonl")))
        self.assertTrue(os.path.exists(os.path.join(self.pa, U_NORMAL + ".jsonl")))
        self.assertFalse(os.path.exists(os.path.join(self.pa, U_ORPH)))
        hu = os.listdir(os.path.join(pap, "_huerfanos"))
        self.assertTrue(any(h.endswith(U_ORPH) for h in hu))
        self.assertFalse(os.path.exists(self.pe))
        # proyecto con memory/ vacia: se borra todo
        self.assertFalse(os.path.exists(self.pmv))
        # proyecto con solo memory: memory a _huerfanos, carpeta borrada
        self.assertFalse(os.path.exists(self.pm))
        self.assertTrue(any(h.endswith("-solo-memoria") for h in hu))
        # proyecto con sesion + memory intacto
        self.assertTrue(os.path.isfile(os.path.join(self.pcm, "memory", "m.md")))

    def test_old(self):
        rc, out, _ = self.run_ses("old", "30")
        self.assertEqual(rc, 0)
        self.assertIn("cccc1111", out)
        self.assertTrue(os.path.isdir(os.path.join(self.root, "ses-papelera", U_OLD)))
        self.assertFalse(os.path.exists(os.path.join(self.pd, U_OLD + ".jsonl")))
        self.assertTrue(os.path.exists(os.path.join(self.pa, U_NORMAL + ".jsonl")))

    def test_old_project_filter(self):
        rc, out, _ = self.run_ses("old", "30", "--project", "zzz")
        self.assertNotIn("cccc1111", out)
        self.assertTrue(os.path.exists(os.path.join(self.pd, U_OLD + ".jsonl")))

    def test_empty(self):
        self.run_ses("delete", U_NORMAL)
        pap = os.path.join(self.root, "ses-papelera")
        rc, out, _ = self.run_ses("empty", "--dry-run")
        self.assertTrue(os.path.isdir(os.path.join(pap, U_NORMAL)))
        rc, out, _ = self.run_ses("empty")
        self.assertEqual(rc, 0)
        self.assertEqual(os.listdir(pap), [])
        # sesiones vivas intactas
        self.assertTrue(os.path.exists(os.path.join(self.pcm, U_SOLO + ".jsonl")))


class TestOldMemory(Base):
    def mem(self, pdir, age_days, files=("m.md",)):
        d = os.path.join(pdir, "memory")
        os.makedirs(d, exist_ok=True)
        for f in files:
            p = os.path.join(d, f)
            open(p, "w").write("x")
            t = time.time() - age_days * 86400
            os.utime(p, (t, t))
        return d

    def test_old_memory_no_sessions(self):
        p = self.proj("-sin-sesiones")
        mem = self.mem(p, 60)
        rc, out, _ = self.run_ses("old", "30", "--dry-run")
        self.assertIn("memoria", out)
        self.assertTrue(os.path.isdir(mem))
        rc, out, _ = self.run_ses("old", "30")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(p))
        rc, out, _ = self.run_ses("restore")
        self.assertIn("[memoria]", out)
        short = [l for l in out.splitlines() if "[memoria]" in l][0].split()[0]
        rc, out, _ = self.run_ses("restore", short)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(os.path.join(mem, "m.md")))
        # restaurar de nuevo: ya no esta en la papelera
        rc, _, _ = self.run_ses("restore", short)
        self.assertEqual(rc, 1)

    def test_orphan_memory_restore_refuses_if_exists(self):
        p = self.proj("-sin-sesiones")
        mem = self.mem(p, 60)
        self.run_ses("old", "30")
        self.mem(p, 0)
        rc, out, _ = self.run_ses("restore")
        short = [l for l in out.splitlines() if "[memoria]" in l][0].split()[0]
        rc, _, err = self.run_ses("restore", short)
        self.assertEqual(rc, 1)

    def test_old_memory_with_old_sessions_goes_with_last(self):
        self.mk(self.pa, U_OLD, conv(self.alive), age_h=24 * 90)
        mem = self.mem(self.pa, 60)
        rc, out, _ = self.run_ses("old", "30")
        self.assertRegex(out, r"(?m)^memoria .*con la ultima sesion")
        t = os.path.join(self.root, "ses-papelera", U_OLD, "artefactos", "memory", "m.md")
        self.assertTrue(os.path.isfile(t))

    def test_old_memory_with_recent_session_stays(self):
        self.mk(self.pa, U_OLD, conv(self.alive), age_h=24 * 90)
        self.mk(self.pa, U_NORMAL, conv(self.alive), age_h=2)
        mem = self.mem(self.pa, 60)
        self.run_ses("old", "30")
        self.assertTrue(os.path.isfile(os.path.join(mem, "m.md")))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "ses-papelera", U_OLD)))

    def test_old_memory_with_recent_file_stays(self):
        self.mk(self.pa, U_OLD, conv(self.alive), age_h=24 * 90)
        mem = self.mem(self.pa, 60)
        self.mem(self.pa, 1, files=("nuevo.md",))
        rc, out, _ = self.run_ses("old", "30")
        self.assertTrue(os.path.isfile(os.path.join(mem, "nuevo.md")))
        self.assertTrue(os.path.isfile(os.path.join(mem, "m.md")))
        # y no se movio ninguna memoria sin sesiones
        self.assertFalse(os.path.exists(os.path.join(self.root, "ses-papelera", "_huerfanos")))

    def test_home_protected(self):
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = home
        try:
            ph = self.proj(ses_enc(home))
            self.mk(ph, U_OLD, conv(home), age_h=24 * 90)
            mem = self.mem(ph, 60)
            rc, out, _ = self.run_ses("old", "30")
            self.assertIn("memoria protegida (home)", out)
            self.assertTrue(os.path.isfile(os.path.join(mem, "m.md")))
            self.assertTrue(os.path.isdir(ph))
            self.assertTrue(os.path.isdir(os.path.join(self.root, "ses-papelera", U_OLD)))
            # delete tambien
            self.mk(ph, U_NORMAL, conv(home))
            rc, out, _ = self.run_ses("delete", U_NORMAL)
            self.assertIn("memoria protegida (home)", out)
            self.assertTrue(os.path.isfile(os.path.join(mem, "m.md")))
            # y junk con proyecto sin sesiones
            rc, out, _ = self.run_ses("junk")
            self.assertTrue(os.path.isfile(os.path.join(mem, "m.md")))
            # desactivable
            os.environ["SES_PROTEGER_HOME"] = "0"
            self.assertFalse(ses.project_protected(ph, home))
        finally:
            os.environ.pop("SES_PROTEGER_HOME", None)
            if old_home is not None:
                os.environ["HOME"] = old_home


class TestFlow(Base):
    def setUp(self):
        super().setUp()
        self.fr = os.path.join(self.tmp, "fhome")
        os.makedirs(self.fr)
        os.environ["SES_FLOW_ROOT"] = self.fr

    def flow(self, rel, age_days=0, files=("a.json",)):
        d = os.path.join(self.fr, rel, ".claude-flow")
        os.makedirs(d, exist_ok=True)
        for f in files:
            p = os.path.join(d, f)
            with open(p, "w") as fh:
                fh.write("datos")
            t = time.time() - age_days * 86400
            os.utime(p, (t, t))
        return d

    def test_find_prune_nested_symlink(self):
        a = self.flow("proyA")
        os.makedirs(os.path.join(a, ".claude-flow"))  # anidada: no cuenta
        self.flow("proyB/node_modules/x")
        self.flow("proyC/.git/y")
        real = os.path.join(self.tmp, "real-flow")
        os.makedirs(os.path.join(real, ".claude-flow"))
        os.symlink(real, os.path.join(self.fr, "enlace"))
        os.makedirs(os.path.join(self.fr, "proyD"))
        os.symlink(os.path.join(real, ".claude-flow"), os.path.join(self.fr, "proyD", ".claude-flow"))
        self.assertEqual([f["path"] for f in ses.find_flows()], [a])

    def test_dry_run_act_and_restore(self):
        a = self.flow("proyA", age_days=1)
        rc, out, _ = self.run_ses("old", "30", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("Se llevaria:", out)
        self.assertIn("1 .claude-flow", out)
        self.assertIn(".claude-flow\nflow ", out)
        self.assertIn("Modo --dry-run: no se modifico nada.", out)
        self.assertNotIn("\r", out)
        self.assertNotIn("Todo quedo", out)
        self.assertTrue(os.path.isdir(a))
        self.assertFalse(os.path.exists(os.path.join(self.root, "ses-papelera")))
        rc, out, _ = self.run_ses("old", "30")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(a))
        self.assertIn("Listo:", out)
        self.assertEqual(out.count("Para recuperar algo"), 1)
        self.assertNotIn("\r", out)
        rc, out, _ = self.run_ses("restore")
        self.assertIn("[flow]", out)
        short = [l for l in out.splitlines() if "[flow]" in l][0].split()[0]
        os.makedirs(a)  # destino existe: se niega
        rc, _, _ = self.run_ses("restore", short)
        self.assertEqual(rc, 1)
        os.rmdir(a)
        rc, _, _ = self.run_ses("restore", short)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(os.path.join(a, "a.json")))

    def test_find_progress_tty_only(self):
        self.flow("proyA")
        msgs = []
        ses.find_flows(msgs.append)
        self.assertTrue(any("buscando .claude-flow... 0 encontradas, revisando" in m for m in msgs))
        self.assertTrue(any(m.startswith("midiendo") for m in msgs))

    def test_status_line_only_on_tty(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            st = ses.Status()
            st.update("hola", force=True)
            st.clear()
        self.assertEqual(buf.getvalue(), "")  # no TTY: sin \r

        class Tty(io.StringIO):
            def isatty(self):
                return True
        tty = Tty()
        with contextlib.redirect_stdout(tty):
            st = ses.Status()
            st.update("x" * 500, force=True)
            st.clear()
        v = tty.getvalue()
        self.assertTrue(v.startswith("\r") and v.endswith("\r"))
        self.assertLess(max(len(x) for x in v.split("\r")), 200)

    def test_old_takes_all_flows_regardless_of_age(self):
        old = self.flow("viejo", age_days=40)
        new = self.flow("nuevo", age_days=0)
        rc, out, _ = self.run_ses("old", "30", "--dry-run", "--project", "zzz")
        self.assertIn("viejo", out)
        self.assertIn("nuevo", out)
        self.assertTrue(os.path.isdir(old))
        self.assertTrue(os.path.isdir(new))
        self.run_ses("old", "30", "--project", "zzz")  # --project no filtra flows
        self.assertFalse(os.path.exists(old))
        self.assertFalse(os.path.exists(new))

    def test_junk_ignores_flow_and_empty_removes(self):
        a = self.flow("proyA")
        self.run_ses("junk")
        self.assertTrue(os.path.isdir(a))
        self.run_ses("old", "30")
        rc, _, _ = self.run_ses("empty")
        self.assertEqual(rc, 0)
        self.assertEqual(os.listdir(os.path.join(self.root, "ses-papelera")), [])

    def test_safety(self):
        with self.assertRaises(ses.SesError):
            ses.check_flow_safe(os.path.join(self.fr, "otra"))
        outside = os.path.join(self.tmp, "fuera", ".claude-flow")
        os.makedirs(outside)
        with self.assertRaises(ses.SesError):
            ses.check_flow_safe(outside)


def ses_enc(path):
    import re
    return re.sub(r"[^A-Za-z0-9]", "-", path)


if __name__ == "__main__":
    unittest.main()
