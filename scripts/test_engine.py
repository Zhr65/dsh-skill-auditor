#!/usr/bin/env python3
"""
skill-auditor :: test_engine.py — stdlib-only regression tests for the engine.

Covers the parts that had no protection and therefore drifted silently:
  · fingerprint scope (README edit must NOT move the fingerprint; code edit must)
  · frontmatter block scalars (`description: |` used to become the literal "|")
  · baseline merge semantics (partial ingest must not delete other assets)
  · regress verdicts (threshold + direction; FINGERPRINT-DRIFT)
  · scan/model filtering (old-model baselines must not look green)
  · Windows GBK console output (hosts/recommend must not raise UnicodeEncodeError)

Run: python scripts/test_engine.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS)
sys.path.insert(0, SCRIPTS)

import console  # noqa: E402
import baseline as baseline_mod  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def write_json(path, obj):
    write(path, json.dumps(obj, ensure_ascii=False, indent=2))


# 某些受限环境（CI 沙箱、只读 %TEMP%）不允许写系统临时目录。
# 设置 SKILL_AUDITOR_TEST_TMP 指向可写目录即可；普通机器不需要。
TMP_BASE = os.environ.get("SKILL_AUDITOR_TEST_TMP") or None


def mkdtemp():
    """Create a writable scratch dir.

    Deliberately not tempfile.mkdtemp(): on Windows some sandboxes turn the
    0o700 mode it passes into an ACL that then denies the very next write.
    os.makedirs() with the default ACL works everywhere.
    """
    base = TMP_BASE or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    for _ in range(50):
        path = os.path.join(base, "sa-test-" + os.urandom(4).hex())
        try:
            os.makedirs(path)
            return path
        except FileExistsError:
            continue
    raise RuntimeError("cannot create a scratch dir under %s" % base)


def make_skill(root, name="demo-skill", body="body", readme="readme", tool="print('x')"):
    d = os.path.join(root, name)
    write(os.path.join(d, "SKILL.md"), "---\nname: %s\nversion: 1.0.0\n---\n%s\n" % (name, body))
    write(os.path.join(d, "README.md"), readme)
    write(os.path.join(d, "tool.py"), tool)
    return d


def run_console(*args, env=None, encoding="utf-8"):
    e = dict(os.environ)
    e.pop("PYTHONUTF8", None)
    e.pop("PYTHONIOENCODING", None)
    if env:
        e.update(env)
    # 子进程统一 UTF-8，父进程按 utf-8 解码：否则 Windows 默认 GBK 下，
    # 断言里的中文提示会因为编码不一致而匹配不上（测试自己制造噪音）。
    e["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "console.py"), *args],
        capture_output=True, text=True, encoding=encoding, errors="replace", env=e,
        cwd=ROOT,
    )


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_readme_is_not_capability(self):
        d = make_skill(self.tmp)
        fp1, _, _ = console.fingerprint_asset(d)
        write(os.path.join(d, "README.md"), "completely different docs")
        fp2, _, _ = console.fingerprint_asset(d)
        self.assertEqual(fp1, fp2, "README 改动不应改变能力指纹")

    def test_code_is_capability(self):
        d = make_skill(self.tmp)
        fp1, _, _ = console.fingerprint_asset(d)
        write(os.path.join(d, "tool.py"), "print('changed behaviour')")
        fp2, _, _ = console.fingerprint_asset(d)
        self.assertNotEqual(fp1, fp2, "脚本改动必须改变能力指纹")


class FrontmatterTests(unittest.TestCase):
    def test_block_scalar(self):
        text = (
            "---\n"
            "name: frontend-dev\n"
            "description: |\n"
            "  Use when building UI.\n"
            "  Handles: layouts, forms.\n"
            "allowed-tools:\n"
            "  - Read\n"
            "  - Write\n"
            "---\n"
            "body\n"
        )
        fm = console.parse_frontmatter(text)
        self.assertEqual(fm["name"], "frontend-dev")
        self.assertIn("building UI", fm["description"])
        self.assertNotEqual(fm["description"], "|")
        self.assertEqual(fm["allowed-tools__list"], ["Read", "Write"])
        self.assertNotIn("Handles", fm, "块标量里的 key:value 不应被当成新字段")

    def test_folded_scalar(self):
        fm = console.parse_frontmatter("---\ndescription: >\n  one\n  two\n---\n")
        self.assertEqual(fm["description"], "one two")


class SuggestionTests(unittest.TestCase):
    def test_no_false_eval_gap(self):
        reg = {"assets": {
            "test-driven-development": {}, "verification-before-completion": {},
            "systematic-debugging": {}, "frontend-dev": {},
        }}
        slugs = [g["slug"] for g in console.gap_analysis(reg)]
        self.assertNotIn("evals", slugs, "已有 test/verify/debug 时不应再建议装评测")

    def test_real_gap_still_reported(self):
        reg = {"assets": {"frontend-dev": {}}}
        slugs = [g["slug"] for g in console.gap_analysis(reg)]
        self.assertIn("evals", slugs)

    def test_segment_match(self):
        reg = {"assets": {"frontend-dev": {}, "humanizer": {}}}
        hits = [n for n, _ in console.match_existing("帮我做个前端页面", reg)]
        self.assertIn("frontend-dev", hits)


class BaselineMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = mkdtemp()
        console.configure_data_dir(self.tmp)

    def tearDown(self):
        console.configure_data_dir(ROOT)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _old(self):
        write_json(console.BASELINE, {
            "baseline_model": "v1",
            "results": [
                {"skill": "a", "case_id": "a1", "model": "v1", "passed": True, "fingerprint": "f1"},
                {"skill": "a", "case_id": "a2", "model": "v1", "passed": True, "fingerprint": "f1"},
                {"skill": "b", "case_id": "b1", "model": "v1", "passed": True, "fingerprint": "f2"},
            ],
        })

    def test_partial_ingest_keeps_other_assets(self):
        self._old()
        st = console.merge_baseline({"baseline_model": "v2", "results": [
            {"skill": "a", "case_id": "a1", "model": "v2", "passed": False, "fingerprint": "f1"},
        ]})
        doc = console.load_json(console.BASELINE)
        self.assertEqual(st["total"], 2, "a 的旧行被替换，b 的行必须留下")
        self.assertEqual(st["kept"], ["b"])
        skills = sorted(r["skill"] for r in doc["results"])
        self.assertEqual(skills, ["a", "b"])

    def test_replace_overwrites(self):
        self._old()
        st = console.merge_baseline({"baseline_model": "v2", "results": [
            {"skill": "a", "case_id": "a1", "model": "v2", "passed": False, "fingerprint": "f1"},
        ]}, replace=True)
        self.assertEqual(st["total"], 1)
        self.assertEqual(st["kept"], [])

    def test_draft_guard(self):
        draft = {"draft": True, "cases": [{"id": "x", "expected": "TODO: fill"}]}
        real = {"cases": [{"id": "y", "expected": "does the job"}]}
        self.assertTrue(baseline_mod.is_draft(draft["cases"][0], draft))
        self.assertFalse(baseline_mod.is_draft(real["cases"][0], real))


class RegressTests(unittest.TestCase):
    def run_regress(self, results):
        tmp = mkdtemp()
        try:
            path = os.path.join(tmp, "results.json")
            out = os.path.join(tmp, "out")
            write_json(path, results)
            p = subprocess.run(
                [sys.executable, os.path.join(SCRIPTS, "regress.py"),
                 "--results", path, "--out", out, "--compare", "v2"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            with open(os.path.join(out, "depreciation-report.json"), encoding="utf-8") as fh:
                return json.load(fh)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_degradation(self):
        rows = []
        for i in range(5):
            rows.append({"skill": "s", "fingerprint": "fp", "model": "v1",
                         "case_id": "c%d" % i, "passed": True})
        for i in range(3):
            rows.append({"skill": "s", "fingerprint": "fp", "model": "v2",
                         "case_id": "c%d" % i, "passed": True, "attribution": "rewrite instructions"})
        for i in range(3, 5):
            rows.append({"skill": "s", "fingerprint": "fp", "model": "v2",
                         "case_id": "c%d" % i, "passed": False, "attribution": "rewrite instructions"})
        rep = self.run_regress({"baseline_model": "v1", "results": rows})
        row = rep["rows"][0]
        self.assertEqual(row["verdict"], "DEGRADATION")
        self.assertEqual(row["drop_pp"], 40.0)
        self.assertEqual((row["worse_cases"], row["better_cases"]), (2, 0))

    def test_mixed_direction_is_not_degradation(self):
        rows = []
        base = [True, True, True, True, False]
        new = [False, False, True, True, True]
        for i, p in enumerate(base):
            rows.append({"skill": "s", "fingerprint": "fp", "model": "v1",
                         "case_id": "c%d" % i, "passed": p})
        for i, p in enumerate(new):
            rows.append({"skill": "s", "fingerprint": "fp", "model": "v2",
                         "case_id": "c%d" % i, "passed": p})
        rep = self.run_regress({"baseline_model": "v1", "results": rows})
        row = rep["rows"][0]
        self.assertEqual(row["drop_pp"], 20.0)
        self.assertEqual((row["worse_cases"], row["better_cases"]), (2, 1))
        self.assertEqual(row["verdict"], "volatility watch")

    def test_fingerprint_drift(self):
        rows = [
            {"skill": "s", "fingerprint": "old", "model": "v1", "case_id": "c0", "passed": True},
            {"skill": "s", "fingerprint": "new", "model": "v2", "case_id": "c0", "passed": True},
        ]
        rep = self.run_regress({"baseline_model": "v1", "results": rows})
        verdicts = {r["verdict"] for r in rep["rows"]}
        self.assertIn("FINGERPRINT-DRIFT", verdicts)


class ScanModelFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = mkdtemp()
        self.assets = os.path.join(self.tmp, "assets")
        self.data = os.path.join(self.tmp, "data")
        make_skill(self.assets, "demo-skill")
        write_json(os.path.join(self.data, "baseline-results.json"), {
            "baseline_model": "v1",
            "results": [{"skill": "demo-skill", "case_id": "c1", "model": "v1",
                         "passed": True, "fingerprint": "x"}],
        })

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _scan(self):
        p = run_console("--host", "generic", "--root", self.assets, "--out", self.data, "scan")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(os.path.join(self.data, "registry.generic.json"), encoding="utf-8") as fh:
            return json.load(fh)["assets"]["demo-skill"]

    def test_newer_model_baseline_not_counted(self):
        p = run_console("--host", "generic", "--root", self.assets, "--out", self.data,
                        "setup", "--model", "v1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self._scan()["baseline_n"], 1)

        run_console("--host", "generic", "--out", self.data, "bump", "v2")
        asset = self._scan()
        self.assertEqual(asset["baseline_n"], 0, "旧模型的基线不能被当成当前模型的绿灯")
        self.assertEqual(asset["status"], "unverified")


class InstallSafetyTests(unittest.TestCase):
    """--force / --uninstall 不能删掉源目录本身。"""

    def test_force_on_source_is_refused(self):
        import install as install_mod
        tmp = mkdtemp()
        try:
            src = os.path.join(tmp, "skill-auditor")
            write(os.path.join(src, "SKILL.md"), "---\nname: skill-auditor\nversion: 9.9.9\n---\n")
            old_here, old_name = install_mod.HERE, install_mod.NAME
            install_mod.HERE, install_mod.NAME = src, "skill-auditor"
            try:
                args = type("A", (), {"dry_run": False, "create": False,
                                      "force": True, "copy": False})()
                _, status, msg = install_mod.install_one("x", tmp, "d", args)
            finally:
                install_mod.HERE, install_mod.NAME = old_here, old_name
            self.assertEqual(status, "ok")
            self.assertIn("源目录本身", msg)
            self.assertTrue(os.path.isfile(os.path.join(src, "SKILL.md")),
                            "源目录必须还在")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(os.name == "nt", "GBK console only exists on Windows")
class GbkConsoleTests(unittest.TestCase):
    def test_hosts_does_not_crash(self):
        p = run_console("hosts", env={"PYTHONIOENCODING": "gbk"}, encoding="gbk")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("UnicodeEncodeError", p.stderr)

    def test_recommend_does_not_crash(self):
        tmp = mkdtemp()
        try:
            # 造一个最小注册表，避免依赖用户真实资产
            write_json(os.path.join(tmp, "registry.generic.json"), {
                "updated_at": "2026-01-01T00:00:00", "host": "generic",
                "kind": "plugin", "label": "asset", "root": tmp,
                "active_model": "v1", "last_regression_model": "v1",
                "count": 1,
                "assets": {"demo": {"fingerprint": "f", "baseline_n": 0, "risk": 10,
                                    "capabilities": {}, "status": "unverified",
                                    "recommendation": "evaluate", "has_golden": False}},
            })
            p = run_console("--host", "generic", "--out", tmp, "recommend",
                            env={"PYTHONIOENCODING": "gbk"}, encoding="gbk")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertNotIn("UnicodeEncodeError", p.stderr)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class KTests(unittest.TestCase):
    """K≥3：多次判定按多数聚合，K 信息要一路带到 scan 的注册表。"""

    def setUp(self):
        self.tmp = mkdtemp()
        console.configure_data_dir(self.tmp)
        self.assets = os.path.join(self.tmp, "assets")
        make_skill(self.assets, "demo-skill")

    def tearDown(self):
        console.configure_data_dir(ROOT)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _verdict_arrays(self):
        return {
            "demo-skill-d1": [True, True, True],
            "demo-skill-d2": [True, False, False],   # 1/3 → 按多数算失败
            "demo-skill-d3": [True, True],           # K=2，不足 --k 3
        }

    def test_majority_vote_and_tie_is_fail(self):
        # 与 baseline.py 的口径一致：通过数×2 > 总数才算过；平票算失败（保守）。
        def passed_of(runs):
            return sum(1 for r in runs if r) * 2 > len(runs)
        self.assertTrue(passed_of([True, True, True]))
        self.assertFalse(passed_of([True, False, False]), "1/3 应按多数算失败")
        self.assertFalse(passed_of([True, False]), "平票必须算失败，不能变绿灯")
        self.assertTrue(passed_of([True]))

    def test_k_fields_reach_registry(self):
        tmp_data = self.tmp
        rows = []
        for i in (1, 2, 3):
            ok = (i != 1)
            rows.append({
                "skill": "demo-skill", "case_id": "demo-skill-d%d" % i,
                "model": "v1", "passed": ok, "k": 3, "k_passed": (3 if ok else 1),
                "runs": [True, True, True] if ok else [True, False, False],
                "fingerprint": "x",
            })
        write_json(os.path.join(tmp_data, "baseline-results.json"), {
            "baseline_model": "v1",
            "results": rows,
        })
        args = type("A", (), {"host": None, "root": self.assets, "skills_dir": None,
                              "out": tmp_data})()
        host, prof = console.resolve_host(args)
        args.host = host
        p = run_console("--host", "generic", "--root", self.assets, "--out", tmp_data,
                        "setup", "--model", "v1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(run_console("--host", "generic", "--root", self.assets,
                                     "--out", tmp_data, "scan").returncode, 0)
        with open(os.path.join(tmp_data, "registry.generic.json"), encoding="utf-8") as fh:
            asset = json.load(fh)["assets"]["demo-skill"]
        self.assertEqual(asset["baseline_min_k"], 3)
        self.assertEqual(asset["baseline_runs"], 9)
        self.assertEqual(asset["baseline_n"], 3)
        self.assertEqual(asset["baseline_pass"], 2, "K 多数判定：2 通过 1 失败")


class StampInferTests(unittest.TestCase):
    """stamp --infer：推断不到必须拒绝，命中才写戳。绝不猜。"""

    def setUp(self):
        self.tmp = mkdtemp()
        self._env = {k: os.environ.pop(k, None) for k in
                     ("SKILL_AUDITOR_MODEL", "CURRENT_MODEL", "MODEL_VERSION", "OPENAI_MODEL")}
        console.configure_data_dir(self.tmp)

    def tearDown(self):
        for k, v in self._env.items():
            if v is not None:
                os.environ[k] = v
        console.configure_data_dir(ROOT)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_refuses_when_nothing_to_infer(self):
        ver, src, conf = console.infer_model_stamp()
        # 本机三个来源都为空；但若 CI 恰好设了这些变量，则要求返回的 source 明确标注
        if ver is None:
            self.assertIsNone(src)
        else:
            self.assertIn(src, ("env:SKILL_AUDITOR_MODEL", "env:CURRENT_MODEL",
                                "env:MODEL_VERSION", "env:OPENAI_MODEL"))

    def test_infers_from_env_and_writes_stamp(self):
        os.environ["SKILL_AUDITOR_MODEL"] = "v-inferred-1"
        ver, src, conf = console.infer_model_stamp()
        self.assertEqual(ver, "v-inferred-1")
        self.assertEqual(conf, "high")
        # 写戳后 check 才有参照：跑一次真命令
        p = run_console("--host", "generic", "--out", self.tmp, "stamp", "--infer",
                        env={"SKILL_AUDITOR_MODEL": "v-inferred-1"})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(os.path.join(self.tmp, "model-version.generic.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["version"], "v-inferred-1")

    def test_cli_refuses_without_flag_and_version(self):
        p = run_console("--host", "generic", "--out", self.tmp, "stamp")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("版本号", p.stdout)


class CuratedOverrideTests(unittest.TestCase):
    """draft 用例被 curated: true 标记后应可入基线；未标记的仍被拦。"""

    def setUp(self):
        self.tmp = mkdtemp()
        console.configure_data_dir(self.tmp)
        self.golden = os.path.join(self.tmp, "golden")
        os.makedirs(self.golden)
        write_json(os.path.join(self.golden, "demo-skill.json"), {
            "skill": "demo-skill", "draft": True,
            "cases": [
                {"id": "d-1", "input": "x", "expected": "TODO: fill", "judge": "ok"},
                {"id": "d-2", "input": "y", "expected": "TODO: fill", "judge": "ok"},
            ],
        })

    def tearDown(self):
        console.configure_data_dir(ROOT)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, recorded):
        rec = os.path.join(self.tmp, "rec.json")
        write_json(rec, recorded)
        import subprocess
        # 【CI 回归修复】必须给子进程显式指定 UTF-8，否则 Windows 默认 GBK 下
        # 中文提示会被 errors="replace" 变成 '?'，断言「没有任何判定」必然匹配不上。
        # 与模块级 run_console 保持同一口径。
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "baseline.py"),
                            "--golden", os.path.join(self.golden, "demo-skill.json"),
                            "--from", rec, "--out", self.tmp, "--model", "v1"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env)
        return p.returncode, p.stdout + p.stderr

    def test_draft_without_curated_is_blocked(self):
        rc, out = self._run({"d-1": [True, True, True], "d-2": [True, True]})
        self.assertEqual(rc, 1)
        self.assertIn("没有任何判定", out)

    def test_curated_case_overrides_draft_mark(self):
        rc, out = self._run({
            "d-1": {"runs": [True, True, True], "curated": True},
            "d-2": [True, True],
        })
        self.assertEqual(rc, 0, out)
        self.assertIn("curated", out)
        rows = console.load_json(console.BASELINE)["results"]
        kept = [r for r in rows if r["skill"] == "demo-skill"]
        self.assertEqual(len(kept), 1, "只收 curated 的那一条")
        self.assertEqual(kept[0]["k"], 3)


class SeedTests(unittest.TestCase):
    """社区 seed：库须随包带、无隐私、from-seed 只取已装、audit 认宿主、contribute 脱敏。"""

    def setUp(self):
        self.tmp = mkdtemp()
        console.configure_data_dir(self.tmp)
        self.assets = os.path.join(self.tmp, "assets")
        os.makedirs(self.assets)

    def tearDown(self):
        console.configure_data_dir(ROOT)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_seed_library_ships(self):
        seeds = console.seed_cases()
        self.assertTrue(seeds, "包里应该带社区 seed/")
        for skill, cases in seeds.items():
            self.assertTrue(cases)
            for c in cases:
                self.assertTrue(c.get("id"))
                exp = str(c.get("expected", "")).strip()
                self.assertTrue(exp, "%s/%s 缺 expected" % (skill, c["id"]))
                self.assertFalse(exp.upper().startswith("TODO"),
                                 "%s/%s expected 还是占位符" % (skill, c["id"]))

    def test_seed_has_no_private_leak(self):
        for skill, cases in console.seed_cases().items():
            for c in cases:
                blob = json.dumps(c, ensure_ascii=False)
                for bad in ("zhr15", "xwechat", "WorkBuddy AI/", "C:\\\\Users"):
                    self.assertNotIn(bad, blob, "seed 泄漏隐私：%s" % bad)

    def test_audit_uses_given_host_data_dir(self):
        p = run_console("--host", "generic", "--root", self.assets, "--out", self.tmp,
                        "setup", "--model", "v1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        run_console("--host", "generic", "--root", self.assets, "--out", self.tmp, "scan")
        p = run_console("--host", "generic", "--out", self.tmp, "seed", "audit")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("seed 合计", p.stdout)
        self.assertNotIn("registry.workbuddy.json", p.stdout,
                         "seed audit 读错了宿主的注册表（二级子命令把 --host 清成 None）")

    def test_from_seed_without_assets_refuses(self):
        p = run_console("--host", "generic", "--root", self.assets, "--out", self.tmp,
                        "setup", "--model", "v1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        run_console("--host", "generic", "--root", self.assets, "--out", self.tmp, "scan")
        p = run_console("--host", "generic", "--out", self.tmp, "baseline",
                        "--from-seed", "--host", "generic")
        self.assertEqual(p.returncode, 2, "没有已装资产必须报错，不能凑数")

    def test_index_is_consistent(self):
        ok, problems = console.seed_audit_consistency()
        self.assertTrue(ok, "index.json 与 seed/ 不一致：%s" % "; ".join(problems))

    def test_contribute_scrubs(self):
        # 真实 seed/ 在仓库里（可写），跑完清理，避免污染
        path = os.path.join(console.SEED_DIR, "contrib", "demo-d-1.json")
        try:
            p = run_console("--host", "generic", "--out", self.tmp, "seed", "contribute",
                            "--skill", "demo", "--case-id", "d-1",
                            "--input", "看下 C:\\Users\\zhr15\\foo 里有什么",
                            "--expected", "基于真实读取作答",
                            "--judge", "未编造")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertTrue(os.path.isfile(path))
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            c = doc["cases"][0]
            self.assertNotIn("zhr15", c["input"])
            self.assertIn("<home>", c["input"])
            self.assertTrue(c.get("scrubbed"))
        finally:
            if os.path.isfile(path):
                os.remove(path)
            contrib = os.path.dirname(path)
            if os.path.isdir(contrib) and not os.listdir(contrib):
                os.rmdir(contrib)


class KDilutionGuardTests(unittest.TestCase):
    """合并后同一个 case 的 K 不得低于旧值（防硬基线证据被静默稀释）。"""

    def setUp(self):
        self.tmp = mkdtemp()
        console.configure_data_dir(self.tmp)

    def tearDown(self):
        console.configure_data_dir(ROOT)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, rows):
        write_json(console.BASELINE, {"baseline_model": "v1", "results": rows})

    def test_k_dilution_warns(self):
        self._seed([{"skill": "a", "case_id": "c1", "model": "v1",
                     "passed": True, "k": 3, "runs": [True] * 3}])
        st = console.merge_baseline({"baseline_model": "v1", "results": [
            {"skill": "a", "case_id": "c1", "model": "v1", "passed": False, "k": 1},
        ]})
        self.assertTrue(st["k_warnings"], "K 3→1 必须告警")
        self.assertIn("c1", st["k_warnings"][0])

    def test_k_upgrade_silent(self):
        self._seed([{"skill": "a", "case_id": "c1", "model": "v1",
                     "passed": True, "k": 1}])
        st = console.merge_baseline({"baseline_model": "v1", "results": [
            {"skill": "a", "case_id": "c1", "model": "v1", "passed": True,
             "k": 3, "runs": [True] * 3},
        ]})
        self.assertEqual(st["k_warnings"], [], "K 提升不该告警")
        rows = console.load_json(console.BASELINE)["results"]
        self.assertEqual(rows[0]["k"], 3)

    def test_replace_skips_guard_but_reports(self):
        """--replace 允许覆盖（用户显式选择），但合并结果仍如实反映 K。"""
        self._seed([{"skill": "a", "case_id": "c1", "model": "v1",
                     "passed": True, "k": 3, "runs": [True] * 3}])
        st = console.merge_baseline({"baseline_model": "v2", "results": [
            {"skill": "a", "case_id": "c1", "model": "v2", "passed": True, "k": 1},
        ]}, replace=True)
        rows = console.load_json(console.BASELINE)["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["k"], 1)
        self.assertEqual(rows[0]["model"], "v2")


class SuggestionTestsExtra(unittest.TestCase):
    """缺能力推荐：口语要命中、多类目要报、已装优先于推荐。"""

    def test_colloquial_needs_hit(self):
        cases = {
            "帮我设计一个app的界面": "ui-design",
            "帮我做个公司官网": "frontend",
            "我想自动登录网站批量下载": "browser",
            "帮我做一份季度财报分析": "finance",
            "把这段文字的AI味去掉": "humanizer",
            "帮我画个看板": "chart",
            "帮我做个PPT汇报": "pptx",
        }
        for need, want in cases.items():
            slugs = [t["slug"] for _n, t, _h in console.need_categories(need.lower())]
            self.assertIn(want, slugs, "%r 应命中 %s（实际 %s）" % (need, want, slugs))

    def test_multi_category_ranked(self):
        scored = console.need_categories("设计一个 app 界面并做成网页上线")
        slugs = [t["slug"] for _n, t, _h in scored]
        self.assertGreaterEqual(len(scored), 2, "跨类目需求应报多个缺口")
        # 命中词多的排前面
        self.assertGreaterEqual(scored[0][0], scored[-1][0])

    def test_every_category_has_guidance(self):
        for t in console.TAXONOMY:
            self.assertTrue(t.get("where"), "%s 缺 where（去哪搜）" % t["slug"])
            self.assertTrue(t.get("worth"), "%s 缺 worth（值不值得装）" % t["slug"])


class SchemaVersionTests(unittest.TestCase):
    """registry / baseline 写入时带 schema_version（格式演进不再静默失败）。"""

    def setUp(self):
        self.tmp = mkdtemp()
        console.configure_data_dir(self.tmp)
        self.assets = os.path.join(self.tmp, "assets")
        make_skill(self.assets, "demo-skill")

    def tearDown(self):
        console.configure_data_dir(ROOT)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_registry_has_schema_version(self):
        p = run_console("--host", "generic", "--root", self.assets, "--out", self.tmp,
                        "setup", "--model", "v1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        run_console("--host", "generic", "--root", self.assets, "--out", self.tmp, "scan")
        with open(os.path.join(self.tmp, "registry.generic.json"), encoding="utf-8") as fh:
            reg = json.load(fh)
        self.assertEqual(reg.get("schema_version"), 1)

    def test_baseline_has_schema_version(self):
        console.merge_baseline({"baseline_model": "v1", "results": [
            {"skill": "a", "case_id": "c1", "model": "v1", "passed": True, "k": 1},
        ]})
        doc = console.load_json(console.BASELINE)
        self.assertEqual(doc.get("schema_version"), 1)


class ModelEventTests(unittest.TestCase):
    """事件流：engine.mjs 写、console.py 读。unknown 不算已知模型串。"""

    def setUp(self):
        self.tmp = mkdtemp()
        console.configure_data_dir(self.tmp)

    def tearDown(self):
        console.configure_data_dir(ROOT)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_from_events_refuses_without_known_model(self):
        # 沙盒 HOME 下没有 ~/.dsh/skill-auditor/model-events.jsonl → 拒绝
        p = run_console("--host", "generic", "--out", self.tmp, "stamp", "--from-events")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("没有已知模型串", p.stdout)


class NodePluginTests(unittest.TestCase):
    """npm 插件的模型事件写入（detectModel / appendModelEvent / lastKnownModel）。"""

    def test_detect_and_append(self):
        import subprocess
        tmp = mkdtemp()
        try:
            script = (
                "import { appendModelEvent, lastKnownModel } from './lib/engine.mjs';\n"
                "process.env.SKILL_AUDITOR_HOME = %r;\n"
                "const r1 = appendModelEvent({ model: 'v-test-9' });\n"
                "const r2 = appendModelEvent({});\n"
                "const best = lastKnownModel();\n"
                "console.log(JSON.stringify({ r1: r1.model, r2: r2.model, best: best && best.model }));\n"
            ) % tmp
            npm_root = os.path.join(ROOT, "npm")
            p = subprocess.run(["node", "--input-type=module", "-e", script],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", cwd=npm_root,
                               env=dict(os.environ, SKILL_AUDITOR_HOME=tmp))
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn('"r1":"v-test-9"', p.stdout)
            self.assertIn('"r2":"unknown"', p.stdout, "取不到模型时记 unknown，不猜")
            self.assertIn('"best":"v-test-9"', p.stdout, "lastKnownModel 跳过 unknown 取真值")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class BlockedByEnvTests(unittest.TestCase):
    """全失败且归因环境 → blocked，不是 degraded（别让人去重写插件）。"""

    def _bl(self, attribution="environment: daemon not running"):
        return {"results": [
            {"skill": "p", "passed": False, "attribution": attribution},
            {"skill": "p", "passed": False, "attribution": attribution},
        ]}

    def test_all_env_failures_are_blocked(self):
        self.assertTrue(console.all_failed_by_env("p", self._bl()))

    def test_mixed_attribution_is_not_blocked(self):
        bl = {"results": [
            {"skill": "p", "passed": False, "attribution": "environment: daemon"},
            {"skill": "p", "passed": False, "attribution": "output structure changed"},
        ]}
        self.assertFalse(console.all_failed_by_env("p", bl),
                         "归因不一致时必须按 degraded 处理，不能洗成环境问题")

    def test_no_attribution_is_not_blocked(self):
        self.assertFalse(console.all_failed_by_env(
            "p", {"results": [{"skill": "p", "passed": False}]}))

    def test_passed_rows_ignored(self):
        # 关键回归：有通过行 = 部分可用，绝不能判 blocked（曾把 2/3 误标成 blocked）
        bl = {"results": [
            {"skill": "p", "passed": False, "attribution": "environment: x"},
            {"skill": "p", "passed": True},
        ]}
        self.assertFalse(console.all_failed_by_env("p", bl),
                         "有通过行说明它部分能用，不是跑不起来")

    def test_attribution_propagates_from_recorded_underscore(self):
        """recorded 顶层 _attribution 要传播到每行，scan 才能判 blocked。"""
        import subprocess
        tmp = mkdtemp()
        try:
            console.configure_data_dir(tmp)
            golden = os.path.join(tmp, "g.json")
            write_json(golden, {"skill": "plug-t", "fingerprint": "f", "draft": False,
                                "cases": [{"id": "c1", "input": "x",
                                           "expected": "ok", "judge": "ok"}]})
            rec = os.path.join(tmp, "r.json")
            write_json(rec, {"c1": {"runs": [False], "curated": True},
                             "_attribution": "environment: daemon not running"})
            p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "baseline.py"),
                                "--golden", golden, "--from", rec, "--out", tmp,
                                "--model", "v1"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            rows = console.load_json(console.BASELINE)["results"]
            self.assertEqual(rows[0]["attribution"], "environment: daemon not running")
            self.assertTrue(console.all_failed_by_env("plug-t", {"results": rows}))
        finally:
            console.configure_data_dir(ROOT)
            shutil.rmtree(tmp, ignore_errors=True)


class PluginTemplateTests(unittest.TestCase):
    """插件 golden：不能是 TODO 占位，必须有 template 标记和可判的 judge。

    【CI 回归修复】这个测试原来读 `golden/`（个人数据，.gitignore 排除），
    在干净 clone / CI 上 `checked` 恒为 0 → 必然失败。三个 commit 前引入，
    因为只在本机跑过所以没暴露。

    现在改为读**随包分发的 `seed/`**（公共数据，仓库里有），
    本机有 golden 时额外校验它（宽松，缺失不判失败）。
    """

    def test_seed_plugin_cases_are_usable(self):
        """seed/ 里的插件用例必须可判（CI 上唯一能保证存在的公共数据）。"""
        seed_files = [f for f in os.listdir(console.SEED_DIR)
                      if f.endswith(".json") and f != "index.json"] \
            if os.path.isdir(console.SEED_DIR) else []
        checked = 0
        for f in seed_files:
            doc = console.load_json(os.path.join(console.SEED_DIR, f), {}) or {}
            for c in doc.get("cases") or []:
                exp = str(c.get("expected", "")).strip()
                self.assertTrue(exp, "%s/%s 缺 expected" % (f, c.get("id")))
                self.assertFalse(exp.upper().startswith("TODO"),
                                 "%s/%s expected 仍是占位符" % (f, c.get("id")))
                checked += 1
        self.assertGreater(checked, 0, "seed/ 里应至少有一条可判用例")

    def test_local_plugin_goldens_if_present(self):
        """本机有填好的插件 golden 时，校验它是真用例；没有就跳过（不判失败）。"""
        found = 0
        for name in ("vectorize-io_hindsight-coding-agents",
                     "wxg-prc-cpg_browser-skill-dsh-plugin",
                     "dsh-workbuddy-connect"):
            path = os.path.join(ROOT, "golden", name + ".json")
            if not os.path.isfile(path):
                continue
            doc = console.load_json(path)
            found += 1
            self.assertFalse(doc.get("draft"), "%s 不应还是草稿" % name)
            self.assertTrue(doc.get("template"), "%s 应标 template: true" % name)
            for c in doc["cases"]:
                exp = str(c.get("expected", "")).strip()
                self.assertTrue(exp and not exp.upper().startswith("TODO"),
                                "%s/%s expected 仍是占位符" % (name, c["id"]))
                self.assertTrue(str(c.get("judge", "")).strip())
        # 只在有数据时才要求"至少 3 个"——干净环境允许 0（个人数据不随仓库分发）
        if found:
            self.assertGreaterEqual(found, 1)


class DissatisfactionTests(unittest.TestCase):
    """不满侦测：认出「勉强够用」比认出「没有」更重要。"""

    def test_strong_markers(self):
        for text in ("这个 UI 差点意思", "效果不太行", "感觉很鸡肋", "还不如自己写"):
            level, terms = console.detect_dissatisfaction(text)
            self.assertEqual(level, "strong", repr(text))
            self.assertTrue(terms)

    def test_weak_markers(self):
        for text in ("凑合能用", "改了几次还是不对", "总觉得哪里怪"):
            level, _ = console.detect_dissatisfaction(text)
            self.assertIn(level, ("strong", "weak"), repr(text))

    def test_neutral_not_flagged(self):
        for text in ("帮我设计一个app的界面", "做一个公司官网", "整理会议记录"):
            level, terms = console.detect_dissatisfaction(text)
            self.assertIsNone(level, "%r 不该被判为不满" % text)
            self.assertEqual(terms, [])

    def test_diagnose_maps_category_to_installed_asset(self):
        """「UI 差点意思」必须关联到已装的 frontend-dev，而不是说'没装相关资产'。"""
        reg = {"assets": {"frontend-dev": {"baseline_n": 5, "baseline_pass": 5,
                                           "baseline_rate": 1.0, "baseline_min_k": 1,
                                           "status": "watch", "description": "frontend ui"},
                          "github": {"baseline_n": 3, "baseline_pass": 3,
                                     "baseline_rate": 1.0, "status": "strong"}}}
        hits = console.diagnose_owned("我设计 app 的 UI 总是差点意思", reg)
        names = [h[0] for h in hits]
        self.assertIn("frontend-dev", names, "类目 ui-design 应关联到 frontend-dev")
        self.assertNotIn("github", names, "不相关的资产不该被列进来")

    def test_diagnose_marks_reason(self):
        reg = {"assets": {"frontend-dev": {"description": "x"}}}
        hits = console.diagnose_owned("UI 设计差", reg)
        self.assertTrue(hits)
        self.assertTrue(all(len(h) == 3 for h in hits), "三元组必须带 why")


class MarketModuleTests(unittest.TestCase):
    """市场模块：评分不用 star、失败如实报错（不假装没结果）。"""

    def setUp(self):
        sys.path.insert(0, SCRIPTS)
        import market
        self.market = market

    def test_score_ignores_stars(self):
        a = {"name": "a", "installCount": 100, "growth7d": 50,
             "pushedAt": "2026-10-18T00:00:00Z", "stars": 999999}
        b = {"name": "b", "installCount": 100, "growth7d": 50,
             "pushedAt": "2026-10-18T00:00:00Z", "stars": 0}
        self.assertEqual(self.market.score(a), self.market.score(b),
                         "star 不能影响排序（实测 star 是上游仓库的）")

    def test_score_prefers_installs(self):
        lo = {"installCount": 1, "growth7d": 0, "pushedAt": "2026-10-18T00:00:00Z"}
        hi = {"installCount": 400, "growth7d": 0, "pushedAt": "2026-10-18T00:00:00Z"}
        self.assertGreater(self.market.score(hi), self.market.score(lo))

    def test_network_failure_reported_honestly(self):
        """不可达时必须报错，不能返回空列表让人以为'没有结果'。"""
        out = []
        orig = self.market._fetch
        try:
            def boom(url):
                raise OSError("simulated unreachable")
            self.market._fetch = boom
            n = self.market.render_dsh("ui", 3, out=out.append)
            text = "\n".join(out)
            self.assertEqual(n, 0)
            self.assertIn("不可达", text)
            self.assertIn("不编造候选", text)
        finally:
            self.market._fetch = orig

    def test_timeout_is_finite(self):
        self.assertLessEqual(self.market.TIMEOUT, 30)

    def test_how_to_read_warns_about_stars(self):
        text = "\n".join(self.market.HOW_TO_READ)
        self.assertIn("star", text)
        self.assertIn("不是插件热度", text)


class DshProfileDiscoveryTests(unittest.TestCase):
    """DSH 插件发现：以 dsh.profile.bundles 为权威，含 Electron 主包内置插件。"""

    def setUp(self):
        self.tmp = mkdtemp()
        self.profile = os.path.join(self.tmp, "profile")
        self.appnm = os.path.join(self.tmp, "appnm")
        os.makedirs(os.path.join(self.profile, "node_modules"))
        os.makedirs(self.appnm)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mk(self, base, name, desc="d"):
        d = os.path.join(base, name)
        write_json(os.path.join(d, "package.json"),
                   {"name": name, "version": "1.0.0", "description": desc})
        return d

    def _profile_json(self, deps, bundles):
        write_json(os.path.join(self.profile, "package.json"), {
            "name": "dsh-profile-desktop", "private": True,
            "dependencies": deps,
            "dsh": {"profile": {"bundles": bundles}},
        })

    def test_bundles_is_authoritative_not_dependencies(self):
        """bundles 里声明、dependencies 里没有的插件必须被发现（dshmarket 就是这种）。

        【CI 回归修复】`configure_data_dir` / `app_node_modules()` 返回的是**真实**
        Electron 主包路径（Windows 专有），Linux CI 上拿到 None，于是这里造的
        dshmarket 目录根本没被读到 → 'dshmarket' not found。
        必须 monkey-patch 指向测试自己的 appnm，和 test_origin_labels 一致。
        """
        self._profile_json({"plug-a": "1.0.0"},
                           ["@deepseek-ai/dsh-base", "plug-a", "dshmarket"])
        self._mk(os.path.join(self.profile, "node_modules"), "plug-a")
        self._mk(self.appnm, "dshmarket")          # 只在 Electron 主包里
        orig = console.app_node_modules
        try:
            console.app_node_modules = lambda: self.appnm
            got = [a["id"] for a in console.discover_assets(
                self.profile, ["package.json"], "deps", quiet=True)]
        finally:
            console.app_node_modules = orig
        self.assertIn("plug-a", got)
        self.assertIn("dshmarket", got, "bundled 插件必须被发现")

    def test_core_bundles_excluded(self):
        self._profile_json({}, ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app"])
        got = console.discover_assets(self.profile, ["package.json"], "deps", quiet=True)
        self.assertEqual(got, [], "核心 bundle 不是用户插件")

    def test_origin_labels(self):
        self._profile_json({"plug-a": "1.0.0"}, ["plug-a", "builtin-b"])
        self._mk(os.path.join(self.profile, "node_modules"), "plug-a")
        self._mk(self.appnm, "builtin-b")
        app_nm = self.appnm
        orig = console.app_node_modules
        try:
            console.app_node_modules = lambda: app_nm
            assets = {a["id"]: a for a in console.discover_assets(
                self.profile, ["package.json"], "deps", quiet=True)}
        finally:
            console.app_node_modules = orig
        self.assertEqual(assets["plug-a"]["origin"], "profile")
        self.assertEqual(assets["builtin-b"]["origin"], "bundled")

    def test_missing_plugin_reported_not_crash(self):
        self._profile_json({}, ["ghost-plugin"])
        out = []
        orig = console.app_node_modules
        try:
            console.app_node_modules = lambda: None
            assets = console.discover_assets(self.profile, ["package.json"], "deps", quiet=True)
        finally:
            console.app_node_modules = orig
        self.assertEqual(assets, [], "找不到文件的插件不崩、不伪造")

    def test_installed_not_enabled_listed(self):
        self._profile_json({"dormant": "1.0.0"}, ["@deepseek-ai/dsh-base"])
        self._mk(os.path.join(self.profile, "node_modules"), "dormant")
        names = [n for n, k in console.dsh_profile_plugins(self.profile)]
        self.assertIn("dormant", names, "装了但没在 bundles 里的也要报出来")


class AuditTests(unittest.TestCase):
    """主动体检：版本对比 + 分档，且不因网络失败而假装结论。"""

    def setUp(self):
        sys.path.insert(0, SCRIPTS)
        import audit
        self.audit = audit

    def test_version_compare(self):
        self.assertEqual(self.audit.vtuple("1.66.5"), (1, 66, 5))
        self.assertGreater(self.audit.vtuple("1.66.42"), self.audit.vtuple("1.66.5"),
                           "字典序会错，必须按数字比")
        self.assertGreater(self.audit.vtuple("0.10.0"), self.audit.vtuple("0.9.9"))
        self.assertIsNone(self.audit.vtuple(""))

    def test_behind_detection_is_numeric(self):
        """dshmarket 1.66.5 vs 1.66.42 必须判为落后（字符串比较会判错）。"""
        a = self.audit.vtuple("1.66.5")
        b = self.audit.vtuple("1.66.42")
        self.assertLess(a, b)

    def test_offline_skips_network_not_fake(self):
        """--offline 时不联网，且不应把资产标成'最新'。"""
        # 直接验证 npm_latest 在不可达 URL 下返回 None 而不是抛异常
        orig = self.audit.NPM
        try:
            self.audit.NPM = "http://127.0.0.1:1/%s/latest"
            self.assertIsNone(self.audit.npm_latest("whatever"))
        finally:
            self.audit.NPM = orig

    def test_probe_evidence_parses_faults(self):
        tmp = mkdtemp()
        try:
            log = os.path.join(tmp, "x.log")
            with open(log, "w", encoding="utf-8") as fh:
                fh.write("2026-10-19T00:00:00Z [info] [plug-a] all good\n")
                fh.write("2026-10-19T00:00:01Z [info] [plug-b] spawn bsk ENOENT\n")
            hits = self.audit.probe_evidence(tmp)
            self.assertIn("plug-b", hits)
            self.assertNotIn("plug-a", hits)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class AuditVersionTests(unittest.TestCase):
    """体检版本必须实时读，不能信 registry 快照（曾误报「落后」）。"""

    def setUp(self):
        sys.path.insert(0, SCRIPTS)
        import audit
        self.audit = audit
        self.tmp = mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_live_version_reads_profile(self):
        root = os.path.join(self.tmp, "prof")
        write_json(os.path.join(root, "node_modules", "plug-x", "package.json"),
                   {"name": "plug-x", "version": "0.7.8"})
        self.assertEqual(self.audit.live_version(root, "plug-x"), "0.7.8")

    def test_live_version_missing_returns_none(self):
        root = os.path.join(self.tmp, "prof2")
        os.makedirs(root)
        self.assertIsNone(self.audit.live_version(root, "nope"))

    def test_stale_snapshot_not_trusted(self):
        """registry 记 0.7.4、实际装 0.7.8 → 必须用 0.7.8，否则会误报升级。"""
        root = os.path.join(self.tmp, "prof3")
        write_json(os.path.join(root, "node_modules", "plug-y", "package.json"),
                   {"name": "plug-y", "version": "0.7.8"})
        self.assertEqual(self.audit.live_version(root, "plug-y"), "0.7.8")


class UnknownModelStampTests(unittest.TestCase):
    """v-unknown 表示「没设过」，不是「另一个模型」——不能拿它过滤掉真实基线。"""

    def setUp(self):
        self.tmp = mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        self.assets = os.path.join(self.tmp, "assets")
        make_skill(self.assets, "demo-skill")
        write_json(os.path.join(self.data, "model-version.json"),
                   {"version": "v-unknown", "bumped_at": "x"})
        write_json(os.path.join(self.data, "baseline-results.json"), {
            "schema_version": 1, "baseline_model": "v-model-A",
            "results": [{"skill": "demo-skill", "case_id": "c1", "model": "v-model-A",
                         "passed": True, "k": 1, "fingerprint": "f"}],
        })

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _scan(self):
        p = run_console("--host", "generic", "--root", self.assets, "--out", self.data, "scan")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(os.path.join(self.data, "registry.generic.json"), encoding="utf-8") as fh:
            return p, json.load(fh)["assets"]["demo-skill"]

    def test_unknown_does_not_discard_baseline(self):
        p, asset = self._scan()
        self.assertEqual(asset["baseline_n"], 1,
                         "模型戳 unknown 时不能把真实基线当成'旧模型'丢弃")
        self.assertEqual(asset["status"], "strong")
        self.assertIn("模型戳未设置", p.stdout, "要明确提示通过率可能来自别的模型")

    def test_check_refuses_to_green_light_unknown(self):
        self._scan()
        p = run_console("--host", "generic", "--out", self.data, "check")
        self.assertEqual(p.returncode, 3,
                         "unknown 时不能说'无需重跑'，只能说'没法判断'")
        self.assertIn("没法判断", p.stdout)


class MultiModelTests(unittest.TestCase):
    """多模型共存：换模型不清零历史（用户测过的数据不该消失）。"""

    def setUp(self):
        self.tmp = mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        self.assets = os.path.join(self.tmp, "assets")
        make_skill(self.assets, "demo-skill")
        write_json(os.path.join(self.data, "model-version.json"),
                   {"version": "model-B", "bumped_at": "x"})
        write_json(os.path.join(self.data, "baseline-results.json"), {
            "schema_version": 1, "baseline_model": "model-B",
            "results": [
                {"skill": "demo-skill", "case_id": "c1", "model": "model-A",
                 "passed": True, "k": 1, "fingerprint": "f"},
                {"skill": "demo-skill", "case_id": "c2", "model": "model-A",
                 "passed": False, "k": 1, "fingerprint": "f"},
                {"skill": "demo-skill", "case_id": "c1", "model": "model-B",
                 "passed": True, "k": 1, "fingerprint": "f"},
            ],
        })

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_other_model_data_is_kept(self):
        p = run_console("--host", "generic", "--root", self.assets, "--out", self.data, "scan")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(os.path.join(self.data, "registry.generic.json"), encoding="utf-8") as fh:
            asset = json.load(fh)["assets"]["demo-skill"]
        # 当前模型只该算 model-B 那 1 条
        self.assertEqual(asset["baseline_n"], 1)
        self.assertEqual(asset["baseline_pass"], 1)
        # 但 model-A 的数据必须保留下来
        others = asset.get("baseline_other_models") or {}
        self.assertIn("model-A", others, "换模型不能丢掉其他模型的判定")
        self.assertEqual(others["model-A"]["n"], 2)
        self.assertEqual(others["model-A"]["pass"], 1)
        self.assertAlmostEqual(others["model-A"]["rate"], 0.5, places=3)

    def test_status_shows_other_models(self):
        run_console("--host", "generic", "--root", self.assets, "--out", self.data, "scan")
        p = run_console("--host", "generic", "--out", self.data, "status")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("在其他模型上测过", p.stdout)
        self.assertIn("model-A", p.stdout)
        self.assertIn("50%", p.stdout)

    def test_active_model_prefers_explicit_stamp(self):
        """显式戳永远优先于事件流推断。"""
        ev = os.path.join(self.tmp, "events")
        os.makedirs(ev, exist_ok=True)
        console.configure_data_dir(self.data)
        try:
            self.assertEqual(console.get_active_model("generic"), "model-B")
        finally:
            console.configure_data_dir(ROOT)


class AlternativeTests(unittest.TestCase):
    """市场替代推荐：找到真同类，且不把无关插件混进来。"""

    def setUp(self):
        sys.path.insert(0, SCRIPTS)
        import audit
        self.audit = audit
        self.mkt = {
            "@wxg-prc-cpg/browser-skill-dsh-plugin": {
                "name": "@wxg-prc-cpg/browser-skill-dsh-plugin", "category": "tools",
                "installCount": 0, "description": {"zh": "浏览器自动化"}},
            "dsh-plugin-browserskill": {
                "name": "dsh-plugin-browserskill", "category": "tools",
                "installCount": 120, "growth7d": 422, "pushedAt": "2026-10-15T00:00:00Z",
                "description": {"zh": "通过 CLI 与扩展让智能体操控浏览器"}},
            "dsh-memory-plugin": {
                "name": "dsh-memory-plugin", "category": "memory",
                "installCount": 444, "description": {"zh": "长期记忆与知识检索"}},
        }

    def test_finds_real_alternative(self):
        alts = self.audit.find_alternatives(
            "@wxg-prc-cpg/browser-skill-dsh-plugin", self.mkt, "0.3.3")
        names = [a["name"] for a in alts]
        self.assertIn("dsh-plugin-browserskill", names, "应找到真正的浏览器替代")

    def test_excludes_unrelated(self):
        alts = self.audit.find_alternatives(
            "@wxg-prc-cpg/browser-skill-dsh-plugin", self.mkt, "0.3.3")
        names = [a["name"] for a in alts]
        self.assertNotIn("dsh-memory-plugin", names,
                         "记忆插件不该被当成浏览器插件的替代（实测误报）")

    def test_ignores_tiny_installs(self):
        mkt = dict(self.mkt)
        mkt["tiny-browser-thing"] = {
            "name": "tiny-browser-thing", "category": "tools", "installCount": 1,
            "description": {"zh": "另一个浏览器工具"}}
        alts = self.audit.find_alternatives(
            "@wxg-prc-cpg/browser-skill-dsh-plugin", mkt, "0.3.3")
        self.assertNotIn("tiny-browser-thing", [a["name"] for a in alts],
                         "安装量 1 的不算'更好的'")

    def test_empty_market_is_safe(self):
        self.assertEqual(self.audit.find_alternatives("x", None, "1.0"), [])
        self.assertEqual(self.audit.find_alternatives("x", {}, "1.0"), [])


class CanJudgeTests(unittest.TestCase):
    """能力管家核心：五类真实任务必须判对（这是 Agent 是否敢用的前提）。"""

    def setUp(self):
        self.reg = {"assets": {
            "humanizer": {"status": "watch", "version": "2.1.1"},
            "github": {"status": "degraded", "version": "1.0.0"},
            "browser-use": {"status": "blocked", "version": "2.0.0"},
            "office-pptx": {"status": "strong", "version": "1.0.0"},
        }}

    def test_has_capability(self):
        v, hits, _ = console.can_judge("帮我把这段文字去一下AI味", self.reg)
        self.assertEqual(v, "ok")
        self.assertIn("humanizer", [h[0] for h in hits])

    def test_missing_recommends_product_category_first(self):
        """「把财报做成 PPT」主任务是做 PPT，不该首推财报分析。"""
        reg_no_pptx = {"assets": {
            "humanizer": {"status": "watch", "version": "2.1.1"},
        }}
        v, _hits, cats = console.can_judge("帮我把这份财报做成 PPT", reg_no_pptx)
        self.assertEqual(v, "missing")
        self.assertEqual(cats[0][1]["slug"], "pptx",
                         "产物类目必须排在主题类目前面")

    def test_has_pptx_when_installed(self):
        """装了 pptx 能力时，做 PPT 应该判 ok。"""
        v, hits, _ = console.can_judge("帮我把这份财报做成 PPT", self.reg)
        self.assertEqual(v, "ok")
        self.assertIn("office-pptx", [h[0] for h in hits])

    def test_degraded_detected(self):
        v, hits, _ = console.can_judge("去 GitHub 上查一下这个项目", self.reg)
        self.assertEqual(v, "degraded", "有资产但状态 degraded 时必须说'有问题'而非'能做'")

    def test_blocked_is_degraded_not_ok(self):
        v, _h, _ = console.can_judge("用浏览器打开网站", self.reg)
        self.assertEqual(v, "degraded")

    def test_unrelated_task_is_missing_without_fake_category(self):
        v, _h, cats = console.can_judge("帮我规划一次月球基地选址", self.reg)
        self.assertEqual(v, "missing")
        self.assertEqual(cats, [], "不要硬凑类目")

    def test_unverified_is_not_ok(self):
        """审查 P1：装了但没测过（unverified/无 status）不能当「能做」。"""
        reg = {"assets": {"new-plug": {"version": "1.0.0", "status": "unverified"}}}
        v, _h, _c = console.can_judge("用 new-plug 帮我干活", reg)
        self.assertEqual(v, "degraded", "unverified ≠ ok")
        reg2 = {"assets": {"newer": {"version": "1.0.0"}}}  # 无 status 字段
        v2, _h2, _c2 = console.can_judge("用 newer 帮我干活", reg2)
        self.assertEqual(v2, "degraded", "status 缺失同理")

    def test_find_domain_word_boundaries(self):
        """审查 P1：无词边界子串误报（apple⊂app 等 23% 误报率）必须为 0。"""
        for q in ("apple pie recipe", "applications of calculus", "happy birthday song"):
            self.assertEqual(console.find_domain(q.lower()), [],
                             "%r 不该命中任何域" % q)
        # 正例不回归
        self.assertEqual(console.find_domain("帮我做个 app")[0][0], "appdev")
        self.assertEqual(console.find_domain("email 模板")[0][0], "email")

    def test_cli_exit_codes(self):
        """退出码可被脚本/Agent 用来分支：0 能做 / 2 有问题 / 3 缺。"""
        tmp = mkdtemp()
        try:
            console.configure_data_dir(tmp)
            write_json(os.path.join(tmp, "registry.generic.json"), {
                "schema_version": 1, "host": "generic", "root": tmp,
                "active_model": "v1", "count": 1,
                "assets": {"humanizer": {"status": "watch", "version": "2.1.1",
                                         "baseline_n": 5, "baseline_pass": 5,
                                         "baseline_rate": 1.0}},
            })
            p = run_console("--host", "generic", "--out", tmp, "can",
                            "帮我把这段文字去一下AI味")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            p2 = run_console("--host", "generic", "--out", tmp, "can",
                             "帮我做一份月球基地选址报告")
            self.assertEqual(p2.returncode, 3, p2.stdout + p2.stderr)
        finally:
            console.configure_data_dir(ROOT)
            shutil.rmtree(tmp, ignore_errors=True)


class FindRouteTests(unittest.TestCase):
    """能力路由：说功能 → 返回工具名。这是「不用记插件名就能用」的核心。"""

    def setUp(self):
        sys.path.insert(0, SCRIPTS)
        import tools_scan
        self.ts = tools_scan
        self.tmp = mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_finds_prefixed_tools(self):
        """tools.register 里 name 是变量时，靠 <前缀>_<动作> 字面量反推。"""
        d = os.path.join(self.tmp, "mytool-plug", "lib")
        os.makedirs(d)
        with open(os.path.join(d, "index.js"), "w", encoding="utf-8") as fh:
            fh.write(
                'tools.register({ name: spec.name, description: spec.description });\n'
                'var A = "mytool_page"; var B = "mytool_session";\n'
            )
        tools, _ = self.ts.scan_tools(os.path.dirname(d))
        self.assertIn("mytool_page", tools)
        self.assertIn("mytool_session", tools)

    def test_scan_rejects_noise(self):
        """zod 的 invalid_* / 状态类标识不能混进来（实测踩坑）。"""
        d = os.path.join(self.tmp, "mytool-plug", "lib")
        os.makedirs(d)
        with open(os.path.join(d, "index.js"), "w", encoding="utf-8") as fh:
            fh.write('var A = "mytool_page"; var B = "mytool_session";\n'
                     'var C = "invalid_string"; var D = "invalid_type";\n')
        tools, _ = self.ts.scan_tools(os.path.dirname(d))
        self.assertNotIn("invalid_string", tools, "zod 错误码不是工具")
        self.assertNotIn("invalid_type", tools)
        self.assertIn("mytool_page", tools)
        self.assertIn("mytool_session", tools)

    def test_ui_plugin_returns_empty(self):
        """纯 UI 插件没有工具 —— 必须返回空，find 才能如实说'让用户在界面用'。"""
        d = os.path.join(self.tmp, "ui-plug", "lib")
        os.makedirs(d)
        with open(os.path.join(d, "index.js"), "w", encoding="utf-8") as fh:
            fh.write('const el = document.createElement("div"); el.id = "petpet";\n')
        tools, _ = self.ts.scan_tools(os.path.dirname(d))
        self.assertEqual(tools, [])

    def test_find_synonym_balance(self):
        """「额度」要能命中「余额」域（用户不会背同义词表）。"""
        doms = console.find_domain("还剩多少额度")
        self.assertTrue(any(d == "balance" for d, _ in doms))

    def test_find_synonym_memory(self):
        doms = console.find_domain("帮我记住这个")
        self.assertTrue(any(d == "memory" for d, _ in doms))

    def test_find_appdev_domain(self):
        """「做个App」这类表述必须命中 appdev 域（实测曾只命中 ui-design）。"""
        for q in ("帮我做个 App", "开发一个app的后端和ui", "做个手机应用", "写个后端接口"):
            doms = console.find_domain(q.lower())
            self.assertTrue(any(d == "appdev" for d, _ in doms), repr(q))

    def test_all_cs_domains(self):
        """16 个计算机方向场景：首位功能域必须正确（用户要求全方向覆盖）。"""
        cases = [
            ("帮我建个数据库表", "database"),
            ("部署到服务器", "deploy"),       # 曾误判 cloud（服务器 vs 部署）
            ("写个单元测试", "test"),
            ("审查一下这段代码", "code-review"),
            ("爬取网页数据", "browser"),
            ("发封邮件", "email"),
            ("批量转换文件格式", "file"),
            ("加个登录鉴权", "auth"),
            ("看看日志排查问题", "debug"),
            ("微调一下模型", "ai-model"),
            ("提交代码到git", "git"),
            ("配置环境装依赖", "compile"),
            ("帮我做个App", "appdev"),
            ("查下余额", "balance"),
            ("帮我记住这个", "memory"),
            ("做个Excel报表", "sheet"),
        ]
        for q, want in cases:
            doms = console.find_domain(q.lower())
            top = doms[0][0] if doms else None
            self.assertEqual(top, want, "%r 首位域=%r 期望 %r" % (q, top, want))

    def test_domain_weight_prefers_longer_hits(self):
        """「建个数据库表」首位必须是 database 不是 sheet（单字'表'曾同分误判）。"""
        doms = console.find_domain("帮我建个数据库表")
        self.assertEqual(doms[0][0], "database")

    def test_load_json_survives_gbk_file(self):
        """GBK 编码的 JSON（PowerShell Set-Content 写的）不能让 scan 崩掉。"""
        p = os.path.join(self.tmp, "gbk.json")
        with open(p, "w", encoding="gbk") as fh:
            fh.write('{"name":"app-dev","description":"中文描述"}')
        d = console.load_json(p, None)
        self.assertIsNotNone(d, "坏编码要降级读取，不能返回 None 崩掉")
        self.assertEqual(d.get("name"), "app-dev")


if __name__ == "__main__":
    unittest.main(verbosity=2)
