#!/usr/bin/env python3
"""
demo.py — 30 秒看懂「能力折旧表」，不碰你自己的任何数据。

跑起来就三步：
    python demo.py          # 在 demo/ 里造一个沙盒，跑完整流程
    python demo.py --clean  # 删掉沙盒

沙盒里会发生什么（全部在 demo/ 内，读完可以整个删掉）：
    6 个假 skill  →  scan 出真实指纹  →  建基线（全过）
    →  模拟模型换代  →  check 发现基线过期
    →  用新模型重跑（结果是我造的，标注 synthetic）
    →  report 出真实判定：2 个降级、1 个指纹漂移、3 个稳定
    →  recommend 出五档面板

为什么要有这个 demo：新用户装完第一眼看到的是「全是 unverified」——因为还没有 golden set
和基线。这个脚本用沙盒把「有判定之后长什么样」提前展示出来，省掉「先建 30 条用例才能看懂产品」的门槛。

⚠️ 判定是引擎**真算**出来的（regress.py 的阈值、漂移检测、众数归因都真的跑了）；
   唯一假的是每个用例的 passed 值 —— 它们代表「假设新模型在这些用例上的表现」，
   在 results.json 里带 "synthetic": true 标记。
"""
import os
import sys
import json
import shutil
import subprocess

# 【CI 回归修复】Windows 控制台默认 GBK，demo 会转发引擎输出（含 ⚠ 等符号），
# 直接 print 就 UnicodeEncodeError 崩掉。CI 的 windows job 跑 demo 时没设
# PYTHONIOENCODING，于是必红。
# 这里主动把 stdout/stderr 重配成 UTF-8（errors=replace 兜底），
# 不依赖调用方设环境变量 —— 用户本地直接跑也不会崩。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
SANDBOX = os.path.join(HERE, "demo")
ENGINE = os.path.join(SANDBOX, ".engine")
ASSETS = os.path.join(SANDBOX, "assets")
GOLDEN = os.path.join(ENGINE, "golden")

BASELINE_MODEL = "v-2026.08"
NEW_MODEL = "v-2026.10"

# 沙盒资产：名字 / 描述 / 正文行数 / 声明的工具（正文越长、工具越多，risk 越高）
FAKE_SKILLS = [
    ("pdf-extract", "从 PDF 里提取文本与表格，输出 markdown", 40, ["Bash", "Read"]),
    ("sql-review", "审查 SQL 的索引缺失、N+1、注入风险", 60, ["Read", "Grep"]),
    ("docx-writer", "生成排版规范的 Word 文档", 120, ["Bash", "Read", "Write"]),
    ("chart-maker", "把 CSV 数据画成柱状图/折线图", 80, ["Bash"]),
    ("email-draft", "写商务邮件，按收件人调整语气", 50, []),
    ("repo-scan", "扫描仓库结构、依赖与入口，出结构报告", 150, ["Bash", "Grep", "Read"]),
]

# 新模型下每个用例的结果。True=通过。假数据，但结构是真的。
NEW_MODEL_RESULTS = {
    "docx-writer": [True, False, False],   # 3→1，降 66.7pp
    "sql-review": [True, True, False],     # 3→2，降 33.3pp
    "pdf-extract": [True, True, True],
    "chart-maker": [True, True, True],
    "email-draft": [True, True, True],
}

# 失败用例的归因（四类里选，别扩写）
ATTRIBUTION = {
    "docx-writer": "output structure changed -> rewrite instructions",
    "sql-review": "tool call failed -> fix tool description",
}

C = {
    "dim": "\033[2m", "b": "\033[1m", "g": "\033[32m", "y": "\033[33m",
    "r": "\033[31m", "c": "\033[36m", "x": "\033[0m",
}


def color_on():
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        # 让旧版 conhost 也认 ANSI
        try:
            subprocess.run(["cmd", "/c", ""], capture_output=True)
        except Exception:
            return False
    return True


ON = color_on()


def c(text, key):
    return (C[key] + str(text) + C["x"]) if ON else str(text)


def glyph(ch, alt):
    """GBK 控制台打印不了 ▌─═ 这类字符；按当前编码降级。"""
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        ch.encode(enc)
        return ch
    except Exception:
        return alt


def hr(ch=None, n=78):
    ch = ch or glyph("─", "-")
    print(c(ch * n, "dim"))


def step(n, title):
    print()
    print(c(glyph("▌", "|") + " %d/7 " % n, "c") + c(title, "b"))


def run_engine(args, quiet=True):
    cmd = [sys.executable, os.path.join(ENGINE, "scripts", "console.py")] + args
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    p = subprocess.run(cmd, cwd=ENGINE, capture_output=True, env=env)
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    if p.returncode != 0 and not quiet:
        print(c("[engine exit %d]" % p.returncode, "r"))
        print(out)
        print(err)
    return p.returncode, out, err


def engine(args, label, ok=(0,)):
    """跑一步引擎；非预期退出码就停，而不是继续用一个坏状态往下演示。"""
    rc, out, err = run_engine(args, quiet=False)
    if rc not in ok:
        print(c("  步骤失败：%s（exit %d）" % (label, rc), "r"))
        sys.exit(1)
    return out


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


def rmtree_or_die(path):
    """删除沙盒；失败时给人话，而不是一个裸 PermissionError traceback。"""
    try:
        shutil.rmtree(path)
    except OSError as e:
        print(c("无法清理 %s：%s" % (path, e), "r"))
        print("  可能是目录被占用或权限不足；先关掉占用它的程序，或手动删除该目录后重试。")
        sys.exit(1)


def build_sandbox():
    if os.path.isdir(SANDBOX):
        rmtree_or_die(SANDBOX)
    os.makedirs(os.path.join(ENGINE, "scripts"))
    os.makedirs(GOLDEN)
    os.makedirs(ASSETS)

    src_scripts = os.path.join(HERE, "scripts")
    for f in os.listdir(src_scripts):
        if f.endswith(".py"):
            shutil.copy2(os.path.join(src_scripts, f), os.path.join(ENGINE, "scripts", f))
    tpl = os.path.join(HERE, "golden", "_template.json")
    if os.path.isfile(tpl):
        shutil.copy2(tpl, os.path.join(GOLDEN, "_template.json"))

    # 宿主表：只指向沙盒，绝不指向真实目录
    write_json(os.path.join(ENGINE, "hosts.json"), {
        "default_host": "demo",
        "hosts": {
            "demo": {
                "kind": "skill", "label": "skill",
                "root": ASSETS, "discover": "dirs", "manifests": ["SKILL.md"],
            }
        },
    })
    write_json(os.path.join(ENGINE, "model-version.json"), {
        "version": BASELINE_MODEL,
        "bumped_at": "2026-08-01T09:00:00+08:00",
        "note": "demo: 基线模型",
    })

    for name, desc, body_lines, tools in FAKE_SKILLS:
        d = os.path.join(ASSETS, name)
        os.makedirs(d)
        tools_yaml = "".join("\n  - %s" % t for t in tools)
        body = ["", "# %s" % name, "", desc, "", "## 步骤", ""]
        for i in range(1, body_lines + 1):
            body.append("%d. 第 %d 步：按输入处理，产出结构化结果。" % (i, i))
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: %s\ndescription: %s\n" % (name, desc))
            if tools:
                fh.write("allowed-tools:%s\n" % tools_yaml)
            fh.write("---\n" + "\n".join(body) + "\n")

        # 每个资产 3 条 golden（沙盒用，标 synthetic）
        cases = []
        for i in range(1, 4):
            cases.append({
                "id": "%s-d%d" % (name, i),
                "polarity": "positive",
                "type": "task",
                "input": "【demo】%s 的第 %d 个真实请求示例" % (name, i),
                "expected": "按 %s 的职责完成，输出可直接使用" % name,
                "judge": "结果是否完成请求且无需返工",
                "provenance": "demo-synthetic",
                "synthetic": True,
            })
        write_json(os.path.join(GOLDEN, "%s.json" % name), {
            "skill": name,
            "draft": True,
            "note": "demo 沙盒用例，synthetic=true；真实使用时换成你自己的 prompt",
            "cases": cases,
        })


def main():
    if "--clean" in sys.argv:
        if os.path.isdir(SANDBOX):
            rmtree_or_die(SANDBOX)
            print("[ok] 已删除 %s" % SANDBOX)
        else:
            print("[ok] 沙盒本来就不存在")
        return 0

    print()
    print(c("能力折旧表 · 30 秒演示", "b"))
    print(c("沙盒目录：%s" % SANDBOX, "dim"))
    print(c("不会读写你的 skills 目录、注册表或基线。", "dim"))

    step(1, "造 6 个假 skill（正文长度/工具依赖不同 → risk 不同）")
    build_sandbox()
    for name, _d, lines, tools in FAKE_SKILLS:
        print("   %-14s %3d 行  tools=%s" % (name, lines, ",".join(tools) or "-"))

    step(2, "scan：发现资产、算能力指纹")
    out = engine(["--host", "demo", "scan"], "scan")
    print("   " + c(out.strip().splitlines()[0] if out.strip() else "(no output)", "g"))

    step(3, "建基线：用 %s 跑一遍 golden，全过" % BASELINE_MODEL)
    with open(os.path.join(ENGINE, "registry.demo.json"), encoding="utf-8") as fh:
        reg = json.load(fh)
    assets = reg.get("assets") or reg.get("skills") or {}
    fp_of = {name: s["fingerprint"] for name, s in assets.items()}

    base_rows = []
    for name, _d, _l, _t in FAKE_SKILLS:
        for i in range(3):
            base_rows.append({
                "fingerprint": fp_of[name], "skill": name, "model": BASELINE_MODEL,
                "case_id": "%s-d%d" % (name, i + 1), "passed": True,
                "synthetic": True,
            })
    write_json(os.path.join(ENGINE, "baseline-results.json"),
               {"baseline_model": BASELINE_MODEL, "results": base_rows})
    engine(["--host", "demo", "scan"], "scan（建基线后重扫）")
    print("   %s  %d 条用例全过 → 6 个资产都有基线" % (c(glyph("✓", "OK"), "g"), len(base_rows)))

    step(4, "模型换代了：%s → %s" % (BASELINE_MODEL, NEW_MODEL))
    engine(["--host", "demo", "bump", NEW_MODEL], "bump")
    chk = engine(["--host", "demo", "check"], "check", ok=(0, 2))
    for line in chk.strip().splitlines():
        print("   " + c(line, "y" if glyph("⚠", "!") in line else "dim"))
    print("   " + c("→ 工具不会替你宣布「退化」，先重跑再说。", "dim"))

    step(5, "用新模型重跑 golden，出折旧报告")
    new_rows = []
    for name, _d, _l, _t in FAKE_SKILLS:
        if name not in NEW_MODEL_RESULTS:
            continue  # repo-scan 走「指纹漂移」分支
        for i, passed in enumerate(NEW_MODEL_RESULTS[name]):
            row = {
                "fingerprint": fp_of[name], "skill": name, "model": NEW_MODEL,
                "case_id": "%s-d%d" % (name, i + 1), "passed": passed,
                "synthetic": True,
            }
            if not passed and name in ATTRIBUTION:
                row["attribution"] = ATTRIBUTION[name]
            new_rows.append(row)
    # repo-scan 被改过：指纹变了，所以它在新模型下的行带着一个全新的指纹
    for i in range(3):
        new_rows.append({
            "fingerprint": "drift0000demo001", "skill": "repo-scan", "model": NEW_MODEL,
            "case_id": "repo-scan-d%d" % (i + 1), "passed": True, "synthetic": True,
        })
    results = {"baseline_model": BASELINE_MODEL, "synthetic": True,
               "note": "passed 值由 demo 构造；判定由 regress.py 真算", "results": base_rows + new_rows}
    results_path = os.path.join(SANDBOX, "results.json")
    write_json(results_path, results)

    rep = engine(["--host", "demo", "report", "--results", results_path,
                  "--compare", NEW_MODEL], "report")
    for line in rep.strip().splitlines():
        if line.startswith("[ok]"):
            continue
        print("   " + line)
    print()
    print("   " + c("repo-scan 出现两行不是重复：", "y")
          + "旧指纹在这一轮里消失了、新指纹又没有基线 —— 两行说的是同一件事：")
    print("   " + c("你改了 skill，不是模型退化。", "y"))

    step(6, "把新结果落成新基线（真实流程就是这样），再看折旧表")
    write_json(os.path.join(ENGINE, "baseline-results.json"),
               {"baseline_model": NEW_MODEL, "results": new_rows})
    engine(["--host", "demo", "scan"], "scan（落新基线）")
    st = engine(["--host", "demo", "status"], "status")
    for line in st.strip().splitlines():
        print("   " + line)
    print("   " + c("→ 基线换成新模型后，docx-writer 1/3、sql-review 2/3 才落进表里。", "dim"))

    step(7, "推荐/淘汰面板（recommend）")
    rec = engine(["--host", "demo", "recommend"], "recommend")
    for line in rec.strip().splitlines():
        print("   " + line)

    hr("═")
    print(c("看懂了什么", "b"))
    print("   · docx-writer 降 66.7pp → DEGRADATION，归因「输出结构变了 → 重写指令」")
    print("   · sql-review  降 33.3pp → DEGRADATION，归因「工具调用报错 → 修工具描述」")
    print("   · repo-scan   指纹变了   → FINGERPRINT-DRIFT（是你改了 skill，别怪模型）")
    print("   · 其余 3 个    通过率不变 → 保留")
    print("   · recommend 的「已降级」档只有在**新结果落成基线并重扫之后**才亮 —— 这是有意的：")
    print("     判定和推荐读同一份注册表，不允许两处结论打架。")
    print()
    print(c("产物", "b"))
    print("   %s" % os.path.join(ENGINE, "depreciation-report.md"))
    print("   %s" % os.path.join(ENGINE, "depreciation-report.json"))
    print("   %s" % results_path)
    print()
    print(c("换成你自己的资产", "b"))
    print("   python scripts/console.py --host workbuddy status          # 先看清单")
    print("   python scripts/console.py --host workbuddy bootstrap       # 从会话记录起草 golden")
    print("   python scripts/console.py --host workbuddy bump <当前模型>  # 记模型戳")
    print()
    print(c("清掉沙盒：python demo.py --clean", "dim"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
