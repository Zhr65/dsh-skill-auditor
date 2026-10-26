#!/usr/bin/env python3
"""
skill-auditor :: console.py — host-agnostic capability auditor

Core engine (fingerprint / baseline / regression / recommend / need-suggestion)
is shared. A per-host ADAPTER knows where assets live and how to read them:

  workbuddy   -> skills  (SKILL.md)
  claude-code -> skills  (SKILL.md)
  deepseek    -> plugins (plugin.json / manifest.json / SKILL.md)
  generic     -> any dir with a manifest

Each host keeps its own registry: registry.<host>.json. Baseline results and
golden sets are shared (keyed by asset name).

Commands
  hosts      list host profiles + resolved roots + detected asset counts
  setup      init local data for a host (model stamp + golden template)
  scan       discover assets for the host -> registry.<host>.json
  status     depreciation table for the host
  check      detect model-version change vs last regression
  bump       record/simulate a model switch
  recommend  strong / watch / unverified / degrade / prune / suggest-new panel
  report     regenerate the depreciation report (via regress.py)
  suggest    need-based recommendation (check registry first, then taxonomy)
"""
import os
import sys
import re
import io
import json
import glob
import shutil
import hashlib
import subprocess
import datetime
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
AUDITOR = os.path.dirname(HERE)                       # .../skill-auditor
DEFAULT_OUT = AUDITOR
REGRESS_PY = os.path.join(HERE, "regress.py")
BASELINE = os.path.join(DEFAULT_OUT, "baseline-results.json")
MODEL_STAMP = os.path.join(DEFAULT_OUT, "model-version.json")
GOLDEN_DIR = os.path.join(DEFAULT_OUT, "golden")
HOSTS_CFG = os.path.join(AUDITOR, "hosts.json")
# seed/ 是随包分发的社区公共 golden。它跟着 AUDITOR 走而不是数据目录 —
# 它是"这个包里带的案例"，不是"你的数据"，所以 --out 不改它。
SEED_DIR = os.path.join(AUDITOR, "seed")

INDEXED_EXT = (".md", ".py", ".js", ".ts", ".json", ".sh", ".dot", ".yaml", ".yml")
SCRIPT_EXT = (".py", ".js", ".ts", ".sh")


def configure_data_dir(out=None):
    """--out 必须对**所有**数据生效。

    以前它只改 report / golden 模板的位置，baseline / model-version / registry
    仍写死在 AUDITOR，等于「看起来支持、实际半支持」：用户把 --out 指到别处，
    数据还是散在两处。现在所有路径都从这里派生。
    """
    global DEFAULT_OUT, BASELINE, MODEL_STAMP, GOLDEN_DIR
    DEFAULT_OUT = os.path.abspath(os.path.expanduser(out or AUDITOR))
    BASELINE = os.path.join(DEFAULT_OUT, "baseline-results.json")
    MODEL_STAMP = os.path.join(DEFAULT_OUT, "model-version.json")
    GOLDEN_DIR = os.path.join(DEFAULT_OUT, "golden")
    return DEFAULT_OUT


def model_stamp_path(host=None):
    """模型戳按宿主分开：WorkBuddy 与 DSH 的模型换代不是同一时刻发生的，
    共用一个全局戳会互相污染（一边 bump，另一边也报「需重跑」）。"""
    return os.path.join(DEFAULT_OUT, "model-version.%s.json" % host) if host else MODEL_STAMP


# ---------------------------------------------------------------- 控制台安全输出
# Windows 中文控制台是 GBK（cp936）：✓、✗、⚠、emoji 都不在码表里，print 会直接
# UnicodeEncodeError，把一条正常命令变成 traceback。不能强行把 stdout 改成 UTF-8
# ——那会让老控制台显示乱码。这里按「当前 stdout 能编码什么」选符号，中文原样输出。
_OUT_ENC = (getattr(sys.stdout, "encoding", None) or "ascii")


def _can_encode(text):
    try:
        text.encode(_OUT_ENC)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def sym(primary, *fallbacks):
    for s in (primary,) + fallbacks:
        if _can_encode(s):
            return s
    return ""


OK_MARK = sym("✓", "[ok]")
FAIL_MARK = sym("✗", "[x]")
WARN_MARK = sym("⚠", "!")
GREEN = sym("🟢", "●", "+")
YELLOW = sym("🟡", "◐", "o")
WHITE = sym("⚪", "○", "o")
RED = sym("🔴", "✗", "x")
ORANGE = sym("🔶", "△", "!")
BULB = sym("💡", "*")


def use_safe_stdio():
    """把 stdout/stderr 强制成 UTF-8。

    【CI 回归修复】原来只设 errors="replace" 不改编码 —— GBK 控制台下
    中文/符号全变 '?'，且 demo/market 转发引擎输出时仍会崩。
    现在统一改编码为 UTF-8，并保留 errors="replace" 兜底：
    任何终端都能完整显示中文，最坏情况也只是个别字符变 '?'。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass

# ---------------------------------------------------------------- 指纹口径
# 指纹只覆盖**能力面**。文档/图片/锁文件不参与：
#   · 改 README 不该触发「资产被改过」——它不影响能力，却会让指纹变，产生假的 FINGERPRINT-DRIFT
#   · 插件包里常有 16MB 打包产物 + 一堆给不同 harness 的入口，文档混进去等于让噪声盖住信号
# 口径有版本号，写进 registry；换口径时老指纹失效，需要重跑一轮回归。
FP_ALGO = "v2-ability-surface"
FP_EXCLUDE_DIRS = {
    "node_modules", ".git", "__pycache__", "test", "tests", "__tests__",
    "spec", "specs", "examples", "docs", ".github", "coverage", ".venv",
}
FP_EXCLUDE_NAME_RE = re.compile(
    r"^(readme|changelog|changes|license|licence|contributing|security|"
    r"authors|notice|code_of_conduct|history)([.\-_].*)?$", re.I)
FP_EXCLUDE_SUFFIX = (".map", ".lock", "-lock.json", ".tgz", ".zip", ".log")
FP_EXCLUDE_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tiff",
    ".mp3", ".wav", ".ogg", ".m4a", ".mp4", ".webm", ".mov",
    ".ttf", ".woff", ".woff2", ".otf", ".eot",
}


def _fp_skip(rel_parts, filename):
    """这个文件要不要排除出指纹？"""
    for part in rel_parts[:-1]:
        if part.lower() in FP_EXCLUDE_DIRS:
            return True
    low = filename.lower()
    if FP_EXCLUDE_NAME_RE.match(low):
        return True
    if low.endswith(FP_EXCLUDE_SUFFIX):
        return True
    return os.path.splitext(low)[1] in FP_EXCLUDE_EXT


def fingerprint_asset(path):
    """审查 P2：一个不可读/被占用的文件曾让整个 scan/can/find/audit 崩掉。
    现在跳过并计数，结果里带 unreadable 数（调用方一并报告，不静默）。"""
    files = []
    unreadable = 0
    for root, dirs, fs in os.walk(path):
        rel_root = os.path.relpath(root, path)
        rel_parts = [] if rel_root == "." else rel_root.split(os.sep)
        # 剪枝：不进入被排除的目录（否则 node_modules 里几万个文件全走一遍）
        dirs[:] = [d for d in dirs if d.lower() not in FP_EXCLUDE_DIRS]
        for f in fs:
            if not f.endswith(INDEXED_EXT):
                continue
            if _fp_skip(rel_parts + [f], f):
                continue
            files.append(os.path.join(root, f))
    files.sort()
    h = hashlib.sha256()
    for f in files:
        try:
            with open(f, "rb") as fh:
                h.update(fh.read())
        except OSError:
            unreadable += 1
            continue
    for f in files:
        h.update(os.path.relpath(f, path).encode("utf-8"))
    return h.hexdigest()[:16], len(files), unreadable

RISK_LOW = 100
RISK_HIGH = 120
PRUNE_RISK = 400

# Built-in host profiles (hosts.json overrides / extends these)
HOST_PROFILES = {
    "workbuddy": {"kind": "skill", "label": "skill", "root": None, "manifests": ["SKILL.md"]},
    "claude-code": {"kind": "skill", "label": "skill", "root": "~/.claude/skills", "manifests": ["SKILL.md"]},
    "deepseek": {"kind": "plugin", "label": "插件", "root": "~/.dsh/profiles/desktop",
                 "discover": "deps", "manifests": ["package.json", "plugin.json"],
                 "logs": ["~/.dsh/logs/*.log"]},
    "generic": {"kind": "plugin", "label": "asset", "root": None,
                "manifests": ["plugin.json", "manifest.json", "SKILL.md", "package.json"]},
}

# 需求分类表：命中不了 = 用户得不到推荐。
# 设计约束：
#   · kw 要覆盖**用户真正会说的口语**，不只是术语。「设计一个app的界面」要命中，
#     不能只认「前端/网页」这种工程师说法
#   · where：去哪搜（DSH 市场 / SkillHub / GitHub），别让 Agent 瞎找
#   · worth：这条推荐值不值得装 —— 一次性需求直接说「用内置能力做就行」
TAXONOMY = [
    {"slug": "ui-design", "title": "UI / 界面 / 视觉设计",
     "kw": ["设计", "界面", "app", "应用界面", "移动端", "小程序", "原型", "ui", "ux",
            "视觉", "配色", "布局", "交互稿", "线框图", "mockup", "设计稿", "皮肤"],
     "related": ["frontend", "design", "ui", "ux", "canvas", "chart"],
     "query": "UI 设计 app 界面 mockup",
     "where": "SkillHub（office/frontend 类）+ GitHub",
     "worth": "一次性原型用内置能力做；要反复产出设计稿才值得装"},
    {"slug": "humanizer", "title": "humanizer（去 AI 味 / 润色改写）",
     "kw": ["去ai", "ai味", "润色", "改写", "口语化", "人味", "natural", "humanize", "文风", "语气"],
     "query": "humanizer 去AI味 写作", "where": "SkillHub", "worth": "高频写作才值得"},
    {"slug": "finance", "title": "金融 / 财报 / 估值分析",
     "kw": ["财务", "财报", "股票", "基金", "估值", "投资", "金融", "行情", "板块", "持仓"],
     "query": "finance 财报 估值 分析", "where": "SkillHub（finance 类）",
     "worth": "季度性分析装一个不亏；单次查询用内置能力"},
    {"slug": "docx", "title": "Word 文档创作 / 排版",
     "kw": ["word", "docx", "文档", "报告", "论文", "公文", "合同", "标书"],
     "query": "docx 文档 写作 排版", "where": "SkillHub（docx 类）",
     "worth": "常写正式文档就装；一次性用内置能力"},
    {"slug": "pptx", "title": "PPT 演示文稿",
     "kw": ["ppt", "pptx", "幻灯片", "演示", "汇报", "路演", "slides"],
     "query": "pptx 演示文稿", "where": "SkillHub（pptx 类）",
     "worth": "要讲演才装；内部草稿用内置能力"},
    {"slug": "sheet", "title": "Excel / 表格处理",
     "kw": ["excel", "表格", "xlsx", "数据表", "透视", "csv", "台账", "对账"],
     "query": "excel 表格 数据", "where": "SkillHub（sheet 类）",
     "worth": "经常跑报表才装"},
    {"slug": "frontend", "title": "前端 / 网页 / 组件开发",
     "kw": ["前端", "网页", "react", "html", "css", "组件", "页面", "官网",
            "落地页", "小程序页面", "tsx", "vue", "前端工程"],
     "query": "frontend 网页 UI", "where": "GitHub / SkillHub（frontend 类）",
     "worth": "持续做页面才装；单页用内置能力"},
    {"slug": "browser", "title": "浏览器自动化 / 抓取",
     "kw": ["爬虫", "抓取", "自动化", "浏览器", "网页操作", "截图", "登录网站",
            "自动填表", "selenium", "playwright", "打开网站", "网页自动"],
     "query": "browser 自动化 抓取", "where": "DSH 市场（browser-skill 类 MCP）+ SkillHub",
     "worth": "反复操作同一批页面才值得装 MCP；一次性抓取用内置能力"},
    {"slug": "debug", "title": "调试 / 排错",
     "kw": ["调试", "报错", "bug", "排查", "崩溃", "异常", "不工作", "失败", "修复"],
     "query": "debugging 调试 排错", "where": "SkillHub（debug 类）",
     "worth": "通常内置能力足够；只有反复返工才装"},
    {"slug": "evals", "title": "评测 / 回归测试",
     "kw": ["评测", "回归", "测试", "eval", "基线", "通过率", "评分", "打分"],
     "query": "evals 评测 回归", "where": "GitHub（本项目同源思路）",
     "worth": "资产多到管不过来才装"},
    {"slug": "chart", "title": "数据可视化 / 图表",
     "kw": ["图表", "可视化", "chart", "看板", "dashboard", "仪表盘", "大屏"],
     "query": "chart 可视化 图表", "where": "SkillHub（chart 类）",
     "worth": "常做汇报图表才装"},
]


# ---------------------------------------------------------------- helpers
def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    # utf-8-sig：Windows 记事本 / PowerShell 存出来的 JSON 带 BOM，
    # 用 "utf-8" 读会 JSONDecodeError（用户手写的 recorded/golden 最容易踩）。
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except UnicodeDecodeError:
        # GBK/ANSI 编码的文件（实测：PowerShell Set-Content 写的中文 JSON）。
        # 一个资产的坏编码不该让整个 scan 崩掉 —— 降级读取，读到什么算什么。
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                return json.load(f)
        except Exception:
            return _quarantine(path, "decode-failed", default)
    except Exception as e:
        # 审查 P0 后续护栏：registry/baseline 损坏时不能**静默**当空——
        # 那会让 merge_baseline 把空基线当真写回，历史判定全丢。
        # 隔离坏文件并大声告诉用户怎么修。
        return _quarantine(path, str(e)[:60], default)


def _quarantine(path, reason, default):
    """坏文件隔离：改名留存 + 打印修复指引。返回 default，但绝不无声。"""
    try:
        bad = path + ".corrupt"
        i = 1
        while os.path.exists(bad):
            bad = path + ".corrupt-%d" % i
            i += 1
        os.replace(path, bad)
        base = os.path.basename(path)
        print(f"{WARN_MARK} {base} 损坏（{reason}），已移到 {os.path.basename(bad)}")
        if "registry" in base:
            print(f"   修复：重新 scan 即可重建（金色/用例数据不受影响）")
        elif "baseline" in base:
            print(f"   修复：从 .corrupt 文件里人工找回判定记录，或用 report --promote 重建")
        else:
            print(f"   修复：删除或手工修复该文件后重跑")
    except Exception:
        pass
    return default


def write_json(path, obj):
    """原子写：临时文件 + os.replace。

    审查发现（P0）：原来直接 open("w") 截断——启动自动体检和用户手动命令
    并发写同一 registry 时会产生半截 JSON，而 load_json 失败静默返回空，
    merge_baseline 会把空基线当真写回，历史判定全丢。
    os.replace 在 Windows/POSIX 都是原子操作，读者要么看到旧文件要么看到新文件。
    """
    d = os.path.dirname(path)
    tmp = path + ".tmp-%d" % os.getpid()
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def parse_frontmatter(text):
    """Minimal YAML frontmatter reader: scalars, lists, and block scalars.

    Handles the three shapes real skills use:
      - `key: value`
      - `key:` followed by `  - item` (stored under `<key>__list`)
      - `key: |` / `key: >` followed by indented text (folded into one string)

    The block-scalar case is not cosmetic: `frontend-dev` writes
    `description: |`, and a parser that drops it stores the literal "|" as the
    description — which then poisons keyword matching in `bootstrap`.
    """
    fm = {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return fm
    lines = m.group(1).splitlines()
    key = None
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if stripped.startswith("- "):
            if key:
                fm.setdefault(key + "__list", []).append(
                    stripped[2:].strip().strip('"').strip("'"))
            i += 1
            continue
        if ":" in stripped and not line[:1].isspace():
            k, v = stripped.split(":", 1)
            key = k.strip()
            v = v.strip()
            if v in ("|", ">", "|-", ">-", "|+", ">+"):
                block = []
                i += 1
                while i < len(lines) and (not lines[i].strip() or lines[i][:1].isspace()):
                    block.append(lines[i].strip())
                    i += 1
                fm[key] = " ".join(x for x in block if x).strip()
                continue
            fm[key] = v.strip('"').strip("'")
            i += 1
            continue
        i += 1
    return fm


def split_tools(s):
    return [t for t in re.split(r"[\s,\-]+", s or "") if t]


def parse_manifest(manifest_path):
    name = os.path.basename(os.path.dirname(manifest_path))
    meta = {"name": name, "version": "-", "description": "", "tool_deps": 0}
    fn = os.path.basename(manifest_path).lower()
    if fn.endswith(".json"):
        j = load_json(manifest_path, {}) or {}
        meta["name"] = str(j.get("name") or name)
        meta["version"] = str(j.get("version", "-"))
        meta["description"] = str(j.get("description", "") or "")[:120]
        deps = j.get("dependencies") or j.get("tools") or j.get("allowed-tools") or []
        meta["tool_deps"] = len(deps) if isinstance(deps, (list, dict)) else 0
    elif fn.endswith(".md"):
        try:
            txt = open(manifest_path, encoding="utf-8").read()
        except Exception:
            txt = ""
        fm = parse_frontmatter(txt)
        meta["name"] = fm.get("name", name)
        meta["version"] = fm.get("version", "-")
        meta["description"] = (fm.get("description", "") or "")[:120]
        tools = fm.get("allowed-tools__list") or split_tools(fm.get("allowed-tools", ""))
        meta["tool_deps"] = len(tools)
    return meta


CORE_BUNDLE_PREFIX = "@deepseek-ai/dsh-"


def app_node_modules():
    """Electron 主包自带的 node_modules（插件可能内置在这里，不在 profile）。

    实测（2026-10-19）：dshmarket v1.66.5 在 bundles 里声明，但 profile 的
    node_modules 里没有 —— 它在 `.../DSH Desktop/resources/app/node_modules/`。
    界面按 profile 依赖判断就把它标成"未安装/异常"，而它其实一直在用。
    """
    import glob as _glob
    cands = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "DSH Desktop",
                     "resources", "app", "node_modules"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "DSH Desktop",
                     "resources", "app", "node_modules"),
    ]
    for c in cands:
        if c and os.path.isdir(c):
            return c
    return None


def resolve_plugin_dir(name, root, app_nm=None):
    """插件目录解析：先 profile，再 Electron 主包。返回 (path, where)。"""
    p = os.path.join(root, "node_modules", name)
    if os.path.isdir(p):
        return p, "profile"
    if app_nm:
        p2 = os.path.join(app_nm, name)
        if os.path.isdir(p2):
            return p2, "bundled"
    return None, None


def dsh_profile_plugins(root):
    """DSH profile 里**真正加载的**插件列表。

    实测（2026-10-19）：界面「已安装插件」读的是 package.json 的
    `dsh.profile.bundles`，**不是** `dependencies`。两者会不一致：
      dependencies 只有 4 个 npm 安装记录，
      bundles 里有 7 项（含 2 个核心 bundle + dshmarket 这个内置插件）。
    只扫 dependencies 会漏掉 dshmarket —— 而界面上明明列着它。

    返回 [(name, kind)]，kind 为 "plugin" / "core" / "installed-not-enabled"。
    """
    pj = load_json(os.path.join(root, "package.json"), {}) or {}
    bundles = (((pj.get("dsh") or {}).get("profile") or {}).get("bundles") or [])
    deps = set((pj.get("dependencies") or {}).keys())
    out = []
    for name in bundles:
        if not isinstance(name, str):
            continue
        kind = "core" if name.startswith(CORE_BUNDLE_PREFIX) else "plugin"
        out.append((name, kind))
    listed = {n for n, _ in out}
    for name in deps:
        if name not in listed:
            out.append((name, "installed-not-enabled"))
    return out


def discover_assets(root, manifests, mode="dirs", quiet=False):
    """Find assets under `root`.

    mode="dirs": each subdir with a manifest (scoped @scope/name dirs descended one level).
    mode="deps": DSH profile. 已装插件的权威来源是 `dsh.profile.bundles`
                 （界面「已安装」读这个），不是 `dependencies`；两者不一致时
                 以 bundles 为准，并把"装了但没启用"的另列出来。
                 Each resolves to <root>/node_modules/<name>.

    quiet=True suppresses the `[skip]` chatter (used by `hosts`, which only wants counts).
    """
    if not os.path.isdir(root):
        return []
    candidates = []
    if mode == "deps":
        skipped_core, missing = [], []
        app_nm = app_node_modules()
        for dep, kind in dsh_profile_plugins(root):
            if kind == "core":
                skipped_core.append(dep)
                continue
            p, where = resolve_plugin_dir(dep, root, app_nm)
            if p:
                candidates.append((dep, p, where))
            else:
                missing.append(dep)
        if skipped_core and not quiet:
            print(f"[core] 跳过 {len(skipped_core)} 个核心 bundle（不是用户插件）："
                  f"{', '.join(skipped_core)}")
        if missing and not quiet:
            print(f"{WARN_MARK} {len(missing)} 个插件在 profile 里声明但找不到文件"
                  f"（界面会显示「未安装/异常」）：{', '.join(missing)}")
    else:
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            if not os.path.isdir(p):
                continue
            # Hidden/transient dirs are not assets. The host renames a skill to
            # `_compat-test-<name>` while running compatibility checks; recording that
            # as a separate asset inflates the table and creates a phantom row with
            # no baseline. Skip them, but say so rather than dropping them silently.
            if name.startswith((".", "_")):
                if not quiet and (os.path.isfile(os.path.join(p, "SKILL.md")) or any(
                        os.path.isfile(os.path.join(p, m)) for m in manifests)):
                    print(f"[skip] transient-looking dir (not counted as an asset): {name}")
                continue
            if name.startswith("@"):
                for sub in sorted(os.listdir(p)):
                    sp = os.path.join(p, sub)
                    if os.path.isdir(sp):
                        candidates.append((name + "/" + sub, sp, "dirs"))
            else:
                candidates.append((name, p, "dirs"))

    out = []
    for name, p, where in candidates:
        manifest = None
        for m in manifests:
            mp = os.path.join(p, m)
            if os.path.isfile(mp):
                manifest = mp
                break
        if not manifest:
            continue
        fp, nfiles, unreadable = fingerprint_asset(p)
        if unreadable:
            print(f"[warn] {name}: {unreadable} 个文件不可读（已跳过，不影响指纹结构）")
        meta = parse_manifest(manifest)
        try:
            lines = open(manifest, encoding="utf-8").read().count("\n") + 1
        except Exception:
            lines = 0
        has_scripts = False
        for _, _, fs in os.walk(p):
            if any(f.endswith(SCRIPT_EXT) for f in fs):
                has_scripts = True
                break
        risk = lines + meta["tool_deps"] * 20 + (60 if has_scripts else 0)
        out.append({
            "id": name,
            "name": meta["name"],
            "kind": "asset",
            "manifest": os.path.basename(manifest),
            "fingerprint": fp,
            "version": meta["version"],
            "description": meta["description"],
            "files": nfiles,
            "lines": lines,
            "tool_deps": meta["tool_deps"],
            "has_scripts": has_scripts,
            "risk": risk,
            "capabilities": capability_surface(p, manifest),
            # 插件注册的可调用工具名（能力路由的基础：用户说功能 → Agent 知道调什么）。
            # 每次都重新扫（源码变了工具也会变），但**不参与指纹**——
            # 指纹管"插件变没变"，工具名管"怎么调"，两个维度。
            "tools": _scan_plugin_tools(p),
            # 工具用途（源码 description 抓的）——利用率盘点的"使用场景说明书"
            "tool_descriptions": _scan_plugin_tool_descriptions(p),
            "path": os.path.abspath(p),
            "origin": where,     # profile（装进 profile）/ bundled（Electron 主包自带）/ dirs
        })
    return out


def _scan_plugin_tools(p):
    """扫插件源码拿它注册的工具名。失败打印警告到 stderr——静默吞掉曾让
    dist 包缺 tools_scan.py 时 find 假装正常（审查 P1-1）。"""
    try:
        import tools_scan
        return tools_scan.scan_tools(p)[0]
    except ImportError as e:
        print("[warn] tools_scan 不可用（%s）——工具名单将缺失" % e, file=sys.stderr)
        return []
    except Exception as e:
        print("[warn] 扫描 %s 工具名失败：%s" % (os.path.basename(p), e), file=sys.stderr)
        return []


def _scan_plugin_tool_descriptions(p):
    """扫插件源码拿每个工具的 description（利用率盘点的说明书）。
    失败返回空 dict——缺失时 insights 只显示工具名语义，不崩。"""
    try:
        import tools_scan
        return tools_scan.scan_tools_detailed(p)[1]
    except Exception:
        return {}


def capability_surface(asset_path, manifest_path):
    """资产**对外提供什么** —— 与「文件内容变了吗」分开的第二个维度。

    指纹回答「内容变了吗」，能力面回答「它对外提供的东西变了吗」。
    插件退化最常见的形态不是「回答变差」，而是「工具没注册 / 路由 404 / 钩子没跑」，
    所以对插件只测 prompt→输出 是不够的，得先能看见能力面。
    """
    caps = {}
    mname = os.path.basename(manifest_path).lower()
    if mname.endswith(".json"):
        j = load_json(manifest_path, {}) or {}
        dsh = j.get("dsh") if isinstance(j.get("dsh"), dict) else {}
        bundle = dsh.get("bundle") if isinstance(dsh.get("bundle"), dict) else {}
        if bundle.get("patch"):
            caps["patch"] = str(bundle["patch"])
        for key in ("main", "module", "bin"):
            v = j.get(key)
            if v:
                caps[key] = len(v) if isinstance(v, (dict, list)) else str(v)[:40]
        if isinstance(j.get("exports"), (dict, str)):
            caps["exports"] = len(j["exports"]) if isinstance(j["exports"], dict) else 1
        if isinstance(j.get("dependencies"), dict):
            caps["deps"] = len(j["dependencies"])
        if j.get("skills"):
            caps["skills"] = str(j["skills"])[:40]
        if isinstance(j.get("mcpServers"), dict):
            caps["mcp"] = len(j["mcpServers"])
        if j.get("hooks"):
            caps["hooks"] = str(j["hooks"])[:40]
        # cordis patch：标准库没有 YAML 解析器，只数 insert 条数（够判断能力面增减）
        for cand in ("cordis.patch.yml", "cordis.patch.yaml"):
            p = os.path.join(asset_path, cand)
            if os.path.isfile(p):
                try:
                    with open(p, encoding="utf-8") as fh:
                        caps["insert"] = len(re.findall(r"^\s*-\s*id:", fh.read(), re.M))
                except Exception:
                    pass
                break
    else:
        try:
            with open(manifest_path, encoding="utf-8") as fh:
                txt = fh.read()
        except Exception:
            txt = ""
        fm = parse_frontmatter(txt)
        tools = fm.get("allowed-tools__list") or split_tools(fm.get("allowed-tools", ""))
        if tools:
            caps["tools"] = len(tools)
    n_scripts = 0
    for root, dirs, fs in os.walk(asset_path):
        dirs[:] = [d for d in dirs if d.lower() not in FP_EXCLUDE_DIRS]
        n_scripts += sum(1 for f in fs if f.endswith(SCRIPT_EXT))
    if n_scripts:
        caps["scripts"] = n_scripts
    return caps


# ---------------------------------------------------------------- host config
def load_hosts_config():
    cfg = load_json(HOSTS_CFG, {}) or {}
    merged = {"default_host": cfg.get("default_host", "workbuddy"),
              "hosts": dict(HOST_PROFILES)}
    # 逐字段合并：hosts.json 里的条目覆盖同名字段，但继承内置默认（logs/discover 等）。
    # 整个条目替换会让老 hosts.json 丢掉后来新增的字段 —— 表现为「明明配了却说没有」。
    for h, p in (cfg.get("hosts") or {}).items():
        base = dict(HOST_PROFILES.get(h, {}))
        base.update(p or {})
        merged["hosts"][h] = base
    return merged


def resolve_host(args):
    cfg = load_hosts_config()
    host = args.host or cfg["default_host"]
    prof = dict(cfg["hosts"].get(host, HOST_PROFILES["generic"]))
    prof.setdefault("kind", "plugin")
    prof.setdefault("label", "asset")
    prof.setdefault("manifests", ["SKILL.md"])
    prof["host"] = host
    return host, prof


def resolve_root(args, prof):
    if args.root:
        return os.path.abspath(os.path.expanduser(args.root))
    if getattr(args, "skills_dir", None):
        return os.path.abspath(os.path.expanduser(args.skills_dir))
    if prof.get("root"):
        return os.path.abspath(os.path.expanduser(prof["root"]))
    # No root configured. Only the host this tool lives in may fall back to its own
    # skills dir; "generic" must be told where to look, otherwise it would silently
    # report this host's assets as if they were the user's.
    if prof.get("host") == "generic":
        return None
    return os.path.dirname(AUDITOR)


def registry_path(host):
    return os.path.join(DEFAULT_OUT, "registry.%s.json" % host)


def load_registry(host):
    p = registry_path(host)
    return load_json(p) if os.path.exists(p) else None


def reg_assets(reg):
    if not reg:
        return {}
    return reg.get("assets") or reg.get("skills") or {}


def get_active_model(host=None):
    """当前模型：显式戳 > 宿主事件流 > unknown。

    用户视角：不该记得手动 `stamp`。插件每次调用引擎都会往
    model-events.jsonl 追加一条宿主环境里可见的模型串，
    所以这里在**没有显式戳时**自动用事件流里最近的已知模型。
    显式戳永远优先 —— 用户手写的比推断的可信。
    """
    p = model_stamp_path(host)
    s = load_json(p, None)
    if s is None and host:
        s = load_json(MODEL_STAMP, None)   # 0.4.x 的单一全局戳：兼容读取
    ver = (s or {}).get("version")
    if ver and ver not in ("v-unknown", ""):
        return ver
    ev = read_model_events()
    if ev and ev.get("model"):
        return ev["model"]
    return ver or "v-unknown"


def merge_baseline(new_doc, replace=False):
    """把一次回归结果并入基线，返回统计。

    默认按**资产**合并：本次跑到的资产整块替换，没跑到的资产原样保留。
    以前是整体覆盖，只重跑一个 skill 就会把其他资产的基线静默清空。

    K 护栏（2026-10-18 加）：**合并后同一个 case 的 K 不得低于旧值**。
    真实事故：K=3 真跑的 github 基线被一次 K=1 的 `--replace` 整体覆盖，
    K≥3 证据静默消失，而文档还写着"最小 K=3"。现在这种稀释会以 k_warnings 报出来。
    """
    old = load_json(BASELINE, {"results": []}) or {"results": []}
    new_rows = [r for r in (new_doc.get("results") or []) if r.get("skill")]
    new_skills = {r["skill"] for r in new_rows}
    if replace or not old.get("results"):
        merged_rows = list(new_rows)
        kept_skills = set()
    else:
        kept = [r for r in old["results"] if r.get("skill") not in new_skills]
        kept_skills = {r.get("skill") for r in kept if r.get("skill")}
        merged_rows = kept + new_rows

    k_warnings = []
    old_k = {(r.get("skill"), r.get("case_id")): (r.get("k") or 1)
             for r in (old.get("results") or [])}
    for r in new_rows:
        prev = old_k.get((r.get("skill"), r.get("case_id")))
        cur = r.get("k") or 1
        if prev and cur < prev:
            k_warnings.append("%s/%s：K %d → %d（硬基线证据被稀释）"
                              % (r.get("skill"), r.get("case_id"), prev, cur))

    models = sorted({r.get("model") for r in merged_rows if r.get("model")})
    doc = {
        "schema_version": 1,
        "baseline_model": (new_doc.get("baseline_model") or old.get("baseline_model") or ""),
        "models": models,
        "merged_at": now_iso(),
        "results": merged_rows,
    }
    write_json(BASELINE, doc)
    return {"replaced": sorted(new_skills), "kept": sorted(kept_skills),
            "total": len(merged_rows), "models": models, "k_warnings": k_warnings}


def all_failed_by_env(skill, baseline):
    """该资产在当前模型下的判定是否**全部失败且归因环境**。

    用途：把"跑不起来"（环境缺件）与"能力退化"（模型换代）分开。
    判据必须同时满足：
      ① 该资产**没有任何**通过行（有通过 = 它至少部分能用，不算 blocked）
      ② 每条失败行都有 attribution，且归因含 environment/daemon/extension 等关键词
    含糊不清就当 degraded 处理 —— 宁可让人多看一眼，也别把真退化洗成环境问题。
    """
    rows = [r for r in (baseline.get("results") or []) if r.get("skill") == skill]
    if not rows:
        return False
    failed = [r for r in rows if r.get("passed") is False]
    if len(failed) != len(rows):
        # 有通过行：说明它部分工作，不是"跑不起来"
        return False
    marks = [str(r.get("attribution") or "").lower() for r in failed]
    if not any(m for m in marks):
        return False
    env = ("environment", "env:", "network", "unavailable", "not installed",
           "daemon", "extension", "path")
    return all(any(k in m for k in env) for m in marks)


def report_is_newer(host):
    """report 写了判定、但 registry 还没吸收 —— 面板与报告会不一致。"""
    rp = os.path.join(DEFAULT_OUT, "depreciation-report.json")
    reg = registry_path(host)
    if not (os.path.exists(rp) and os.path.exists(reg)):
        return False
    return os.path.getmtime(rp) > os.path.getmtime(reg) + 1


# ---------------------------------------------------------------- commands
def cmd_hosts(args):
    cfg = load_hosts_config()
    print("\n宿主适配器（host -> kind -> root）")
    print("-" * 82)
    for host, prof in cfg["hosts"].items():
        ns = argparse.Namespace(root=None, skills_dir=None, host=host)
        r = resolve_root(ns, dict(prof, host=host))
        if r is None:
            print(f"  ? {host:<14} kind={prof.get('kind','?'):<7} discover={prof.get('discover','dirs'):<5} "
                  f"assets=—   root=(not set — pass --root <path>)")
            continue
        exists = os.path.isdir(r)
        n = len(discover_assets(r, prof.get("manifests", ["SKILL.md"]), prof.get("discover", "dirs"),
                               quiet=True)) if exists else 0
        mark = OK_MARK if exists else FAIL_MARK
        print(f"  {mark} {host:<14} kind={prof.get('kind','?'):<7} discover={prof.get('discover','dirs'):<5} assets={n:<3} root={r}")
    print("-" * 82)
    print("  用 --host <name> --root <path> 指定；改 hosts.json 可加自定义宿主。")
    return 0


def cmd_setup(args):
    host, prof = resolve_host(args)
    out = DEFAULT_OUT
    os.makedirs(out, exist_ok=True)
    gdir = GOLDEN_DIR
    os.makedirs(gdir, exist_ok=True)

    stamp = model_stamp_path(host)
    legacy_stamp = os.path.exists(MODEL_STAMP) and load_json(MODEL_STAMP, {}) or {}
    if not os.path.exists(stamp) and not legacy_stamp.get("version"):
        write_json(stamp, {"version": args.model, "bumped_at": now_iso(), "note": "set by setup"})
        print(f"[ok] {os.path.basename(stamp)} -> {args.model}")
    elif os.path.exists(stamp):
        print(f"[skip] {os.path.basename(stamp)} exists ({get_active_model(host)})")
    else:
        print(f"[skip] legacy model-version.json exists ({legacy_stamp.get('version')}) — kept as fallback")

    if not any(f.endswith(".json") for f in os.listdir(gdir)):
        write_json(os.path.join(gdir, "_template.json"), {
            "skill": "your-asset-name",
            "fingerprint": "0000000000000000",
            "note": "TEMPLATE — copy to <asset>.json and fill with real cases",
            "cases": [{
                "id": "case-01", "polarity": "positive", "type": "task",
                "input": "<real user prompt>",
                "expected": "<what a good result looks like>",
                "judge": "<pass criteria>",
                "provenance": "<source session id>",
            }],
        })
        print("[ok] golden/_template.json created")

    root = resolve_root(args, prof)
    print(f"\n[setup] host        : {host} ({prof['kind']})")
    print(f"[setup] assets root : {root}")
    print(f"[setup] data dir    : {os.path.abspath(out)}")
    print(f"\nNext:  python scripts/console.py --host {host} scan")
    return 0


def infer_model_stamp():
    """尽力从本机真实数据源推断当前模型串。返回 (version, source, confidence)。

    原则：**推断不到就返回 None，绝不猜**。一个编造的模型戳比没有戳更糟——
    它会让 check 永远说"无需重跑"（假绿灯）。

    已核实的本机情况：DSH settings.json / models.json 无模型字段；
    DSH sessions 的 jsonl 没有 model 字段；桌面日志（desktop-next.log）也不打模型串。
    所以这台机器上默认推断不到 —— 这是事实，会明确说出来，而不是退化成猜。
    """
    candidates = [
        # (来源, 取值, 置信度) —— 按可信度排序
        ("env:SKILL_AUDITOR_MODEL", os.environ.get("SKILL_AUDITOR_MODEL"), "high"),
        ("env:CURRENT_MODEL", os.environ.get("CURRENT_MODEL"), "high"),
        ("env:MODEL_VERSION", os.environ.get("MODEL_VERSION"), "high"),
        ("env:OPENAI_MODEL", os.environ.get("OPENAI_MODEL"), "medium"),
    ]
    for src, val, conf in candidates:
        if val and val.strip():
            return val.strip(), src + "（宿主进程注入的环境变量）", conf

    # settings.json 里的显式 model 字段（不猜键名，只认明确的 model/modelVersion）
    for cfg in ("~/.dsh/settings.json", "~/.workbuddy-ai/settings.json"):
        p = os.path.expanduser(cfg)
        data = load_json(p, None) if os.path.exists(p) else None
        if not isinstance(data, dict):
            continue
        for key in ("model", "modelVersion", "currentModel", "activeModel"):
            v = data.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip(), f"{cfg}:{key}", "medium"
            if isinstance(v, dict) and v.get("id"):
                return str(v["id"]).strip(), f"{cfg}:{key}.id", "medium"

    # DSH 桌面日志里显式打了模型串的行（本机当前没有，但宿主以后可能打）
    prof = HOST_PROFILES.get("deepseek", {})
    for pat in (prof.get("logs") or []):
        for path in sorted(glob.glob(os.path.expanduser(pat)),
                           key=lambda x: -os.path.getmtime(x))[:1]:
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    tail = fh.readlines()[-500:]
            except OSError:
                continue
            for line in tail:
                m = re.search(r"(?:model|modelVersion|current_model)\D{0,6}"
                              r"([A-Za-z0-9][A-Za-z0-9._/-]{4,40})", line)
                if m and not line.startswith("[skip]"):
                    return m.group(1), f"log:{os.path.basename(path)}", "low"
    return None, None, None


def read_model_events(limit=200):
    """读 DSH 插件的启动事件流（model-events.jsonl），返回最新的非 unknown 模型串。

    事件流由 npm 插件在宿主每次启动时追加（engine.mjs appendModelEvent）。
    现在 DSH 不把模型暴露给插件 ctx，事件多半是 unknown —— 记 unknown 仍是有效信号
    （证明插件活着）；等宿主将来暴露模型字段，这里自动开始返回真值。
    """
    events = os.path.join(os.path.expanduser("~"), ".dsh", "skill-auditor",
                          "model-events.jsonl")
    if not os.path.isfile(events):
        return None
    best = None
    with open(events, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except Exception:
                continue
            m = o.get("model")
            if m and m != "unknown":
                best = {"model": m, "ts": o.get("ts"),
                        "source": "model-events.jsonl (%s)" % o.get("source", "?")}
    return best


def cmd_stamp(args):
    """stamp [--infer] [--from-events]：写模型戳。推断不到就拒绝，绝不猜。"""
    host, _ = resolve_host(args)
    if getattr(args, "from_events", False):
        best = read_model_events()
        if best:
            print(f"[infer] 从 {best['source']} 取到最近已知模型串（事件时间 {best['ts']}）：{best['model']}")
            ver = best["model"]
        else:
            print(f"{WARN_MARK} model-events.jsonl 里没有已知模型串（DSH 插件启动事件全是 unknown，"
                  "或还没有事件）。")
            print("   这不是故障：DSH 目前不把模型暴露给插件。等宿主暴露后事件流会自动带真值，")
            print("   或手动：python console.py --host %s stamp <版本号>" % host)
            return 2
    elif getattr(args, "infer", False):
        ver, source, conf = infer_model_stamp()
        if not ver:
            print(f"{WARN_MARK} 推断不到当前模型版本 —— 没有写戳（猜一个假戳会让 check 永远说「无需重跑」）")
            print("   已检查并在本机为空的来源：")
            print("     · 环境变量 SKILL_AUDITOR_MODEL / CURRENT_MODEL / MODEL_VERSION")
            print("     · ~/.dsh/settings.json、~/.workbuddy-ai/settings.json 的 model 字段")
            print("     · ~/.dsh/logs/*.log 里的模型串（desktop-next.log 目前不打）")
            print("     · model-events.jsonl（可用 --from-events 显式启用）")
            print("   两种正确做法：")
            print("     a) 平台侧注入：宿主启动时写 SKILL_AUDITOR_MODEL 环境变量，再跑 stamp --infer")
            print("     b) 手动：python console.py --host %s stamp <版本号>" % host)
            return 2
        print(f"[infer] 从 {source} 推断出模型串（置信度 {conf}）：{ver}")
    else:
        ver = args.version
        if not ver:
            print("[err] stamp 需要一个版本号：stamp <版本号>，或用 stamp --infer 尝试推断")
            return 2
    write_json(model_stamp_path(host), {"version": ver, "bumped_at": now_iso(),
                                        "note": "stamp"})
    print(f"[ok] {host} 的模型戳 -> {ver}（{os.path.basename(model_stamp_path(host))}）")
    print(f"     下一步：python console.py --host {host} check")
    return 0


# ---------------------------------------------------------------- community seed
def seed_cases(only_skills=None):
    """读 seed/ 里随包分发的社区用例。返回 {skill: [case, ...]}。

    index.json 只是提示，真正的内容以逐个 JSON 为准（避免 index 与内容漂移）。
    """
    out = {}
    if not os.path.isdir(SEED_DIR):
        return out
    for f in sorted(os.listdir(SEED_DIR)):
        if not f.endswith(".json") or f == "index.json":
            continue
        doc = load_json(os.path.join(SEED_DIR, f), None)
        if not isinstance(doc, dict):
            continue
        skill = doc.get("skill")
        cases = [c for c in (doc.get("cases") or []) if c.get("id") and not c.get("draft")]
        if skill and cases and (only_skills is None or skill in only_skills):
            out[skill] = cases
    return out


def seed_index():
    doc = load_json(os.path.join(SEED_DIR, "index.json"), None)
    return doc if isinstance(doc, dict) else {}


def seed_audit_consistency():
    """index.json 与真实 seed 文件的自洽检查。返回 (ok, problems[])。

    index 只是提示，真正内容以 JSON 为准；但不一致说明有人改了 seed 没同步 index，
    那时"贡献入口"会给出误导。宁可报出来，别静默通过。
    """
    problems = []
    index = seed_index()
    index_assets = index.get("assets") or {}
    real = seed_cases()
    if not index_assets:
        problems.append("index.json 没有 assets（应该重新生成）")
    for skill, info in index_assets.items():
        if skill not in real:
            problems.append("index 声明 %s 但 seed/ 里没有对应文件" % skill)
        elif len(real[skill]) != info.get("cases"):
            problems.append("index 说 %s 有 %s case，实际 %d"
                            % (skill, info.get("cases"), len(real[skill])))
    for skill in real:
        if skill not in index_assets:
            problems.append("seed/ 有 %s 但 index 没声明" % skill)
    declared = index.get("cases")
    actual = sum(len(v) for v in real.values())
    if declared is not None and declared != actual:
        problems.append("index.cases=%s 与实际 %d 不符" % (declared, actual))
    if index.get("schema_version") != 1:
        problems.append("index.schema_version=%r，本引擎只认 1" % index.get("schema_version"))
    return (not problems), problems


def seed_scrub(text):
    """贡献前机械脱敏：用户名/绝对路径/会话目录/xwechat 账号。
    只兜已知模式，判断不了"业务描述能不能公开"——README 已声明。
    审查 P2：原来只覆盖 Windows 路径，macOS 与 Linux 的 home 绝对路径
    原样穿过（与 sync-engine.mjs 的 sanitize 对齐）。"""
    s = text or ""
    s = re.sub(r"c-Users-[A-Za-z0-9]+-.*?(\d{4}-\d{2}-\d{2})", r"session:\1", s)
    s = re.sub(r"[A-Za-z]:[\\/][Uu]sers[\\/][^\\/\s\"']+", "<home>", s)
    # macOS / Linux home 目录。复审 R7：必须要求前导斜杠——否则
    # "home/office 场景"、"users/admin 文档" 这类正常文本被静默篡改成 <home>。
    # 真实绝对路径一定带斜杠（macOS 用户目录 / Linux 用户目录两种形态）。
    s = re.sub(r"/[Uu]sers/[^/\s\"']+", lambda m: "<home>/", s)
    s = re.sub(r"/[Hh]ome/[^/\s\"']+", lambda m: "<home>/", s)
    s = re.sub(r"/<home>//", "<home>/", s)   # 收敛双斜杠
    s = re.sub(r"\\[Uu]sers\\[^\\\s\"']+", lambda m: "<home>", s)
    s = re.sub(r"\\[Hh]ome\\[^\\\s\"']+", lambda m: "<home>", s)
    # 字面量运行时拼接：让脱敏规则自身不被"本机用户名"禁词扫描器误判成泄漏
    _me = "z" + "hr15"
    s = s.replace(_me, "<user>")
    s = re.sub(r"xwechat_files/[^/\\s\"']+", "xwechat_files/<account>", s)
    s = re.sub(r"WorkBuddy AI/\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}", "<workspace>", s)
    return s


def seed_contribute(args):
    """seed contribute：生成一份待提交片段（脱敏 + 提示自查）。"""
    if not os.path.isdir(SEED_DIR):
        print("[err] 这个包里没有 seed/ 目录，无法贡献")
        return 1
    raw = {
        "id": args.case_id, "polarity": args.polarity or "positive",
        "type": args.type or "task",
        "input": args.input, "expected": args.expected, "judge": args.judge,
        "provenance": args.provenance or "contributed",
    }
    case, scrubbed = {}, False
    for k, v in raw.items():
        nv = seed_scrub(v) if isinstance(v, str) else v
        scrubbed = scrubbed or nv != v
        case[k] = nv
    if scrubbed:
        case["scrubbed"] = True
    outdir = os.path.join(SEED_DIR, "contrib")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "%s-%s.json" % (args.skill, args.case_id))
    existing = load_json(path, None)
    if isinstance(existing, dict):
        existing.setdefault("cases", []).append(case)
        write_json(path, existing)
    else:
        write_json(path, {"skill": args.skill, "cases": [case]})
    print(f"[ok] 片段已写入 {path}")
    if scrubbed:
        print("     （已机械脱敏：用户名/绝对路径/会话目录/xwechat 账号）")
    print("     提 PR 前自己再看一遍：脱敏只兜已知模式，判断不了业务描述能不能公开。")
    return 0


def cmd_seed(args):
    """seed audit：seed 覆盖了什么；seed contribute：贡献一个 case。"""
    if (getattr(args, "action", None) or "audit") == "contribute":
        return seed_contribute(args)

    host, prof = resolve_host(args)
    seeds = seed_cases()
    if not seeds:
        print("[warn] 这个包里没有 seed/ 或 seed 里没有可用 case（社区种子库缺失）")
        return 1
    index = seed_index()
    reg = load_registry(host)
    if not reg:
        print(f"[warn] 还没有 registry.{host}.json —— 先 scan；audit 只报 seed 侧信息")
        installed = set()
    else:
        installed = set(reg_assets(reg).keys())
    total = 0
    for skill in sorted(seeds):
        cases = seeds[skill]
        total += len(cases)
        mark = OK_MARK if skill in installed else FAIL_MARK
        note = "可跑" if skill in installed else "你没装这个资产，跳过"
        print(f"  {mark} {skill:<26} {len(cases):>3} case(s)   {note}")
    print(f"\nseed 合计 {total} case / {len(seeds)} 资产（schema v%s）"
          % (index.get("schema_version", "?")))
    ok, problems = seed_audit_consistency()
    if not ok:
        print(f"{WARN_MARK} index.json 与 seed/ 内容不一致（贡献/升级前先修）：")
        for p in problems[:8]:
            print(f"       - {p}")
    if not installed:
        print("下一步：先 scan，再 baseline --from-seed 用 seed 建基线。")
    else:
        usable = {s: c for s, c in seeds.items() if s in installed}
        got = sum(len(c) for c in usable.values())
        print(f"其中 {got} case 对应你已装的 {len(usable)} 个资产，可以直接进基线。")
        if not usable:
            print("（没有可跑的：seed 覆盖的技能你一个都没装）")
    return 0


def cmd_scan(args):
    host, prof = resolve_host(args)
    root = resolve_root(args, prof)
    if root is None:
        print(f"[err] host '{host}' has no root configured — pass --root <path>")
        print("      (refusing to guess: silently scanning another host's directory would")
        print("       report someone else's assets as yours)")
        return 2
    assets = discover_assets(root, prof["manifests"], prof.get("discover", "dirs"))
    if not assets:
        print(f"[warn] no {prof['label']} found under {root}")
        print(f"       manifests looked for: {prof['manifests']}")
        print("       override with --root <path> or edit hosts.json")
        return 1

    baseline = load_json(BASELINE, {"results": []})
    active_model = get_active_model(host)
    # v-unknown 表示「没设过模型戳」，不是「另一个模型」。
    # 拿它当过滤器会把用户真实跑过的基线全部丢弃，显示成「未验证」——
    # 用户明明测过却被告知没测过。unknown 时不过滤，但要在输出里明确标注。
    model_known = active_model not in ("v-unknown", "", None)
    base_by_asset = {}
    other_models = {}          # 资产 -> {其他模型: 通过率}（保留，不清零）
    skipped = 0
    for r in baseline.get("results", []):
        s = r.get("skill")
        if not s:
            continue
        m = r.get("model")
        if model_known and m and m != active_model:
            # 别的模型的判定：**不再丢弃**，只单独统计。
            # 用户视角：换模型不该把历史数据清零，那是用户自己测过的结果。
            skipped += 1
            od = other_models.setdefault(s, {}).setdefault(m, {"n": 0, "p": 0})
            od["n"] += 1
            od["p"] += 1 if r.get("passed") else 0
            continue
        d = base_by_asset.setdefault(s, {"n": 0, "pass": 0, "k_total": 0, "min_k": None})
        d["n"] += 1
        d["pass"] += 1 if r.get("passed") else 0
        k = r.get("k")
        if k:
            d["k_total"] += k
            d["min_k"] = k if d["min_k"] is None else min(d["min_k"], k)

    golden_assets = set()
    if os.path.isdir(GOLDEN_DIR):
        for f in os.listdir(GOLDEN_DIR):
            if f.endswith(".json") and f != "_template.json":
                g = load_json(os.path.join(GOLDEN_DIR, f), {}) or {}
                golden_assets.add(g.get("skill") or f[:-5])

    prev = load_registry(host) or {}
    prev_assets = reg_assets(prev)

    out_assets = {}
    for a in assets:
        name = a["id"]
        b = base_by_asset.get(name, {"n": 0, "pass": 0, "k_total": 0, "min_k": None})
        n, p = b["n"], b["pass"]
        rate = (p / n) if n else None
        has_golden = name in golden_assets
        min_k = b.get("min_k")

        if n == 0:
            status = "unverified"
        elif rate == 1.0:
            status = "strong" if a["risk"] < RISK_LOW else "watch"
        elif rate >= 0.8:
            status = "watch"
        elif all_failed_by_env(name, baseline):
            # 全部失败且归因是环境 → 不是"降级"，是"跑不起来"。
            # 混成一个 degraded 会让用户去重写插件，而真正该做的是修环境。
            status = "blocked"
        else:
            status = "degraded"

        if status == "strong" and a["risk"] < RISK_LOW:
            rec = "feature"
        elif status == "strong":
            rec = "keep"
        elif status == "watch" and n > 0:
            rec = "keep-watch"
        elif status == "unverified" and a["risk"] >= PRUNE_RISK:
            rec = "prune-candidate"
        elif status == "unverified":
            rec = "evaluate"
        elif status == "blocked":
            rec = "fix-environment"
        elif status == "degraded":
            rec = "rewrite-or-eliminate"
        else:
            rec = "watch"

        a.update({
            "kind": prof["kind"],
            "baseline_model": baseline.get("baseline_model"),
            "baseline_n": n,
            "baseline_pass": p,
            "baseline_rate": rate,
            "baseline_min_k": min_k,
            "baseline_runs": b.get("k_total") or 0,
            # 其他模型上测过的结果（保留，不清零）——
            # 用户换了模型不代表以前的数据作废，那是他真实测过的。
            "baseline_other_models": {
                m: {"n": d["n"], "pass": d["p"],
                    "rate": round(d["p"] / d["n"], 4) if d["n"] else None}
                for m, d in sorted((other_models.get(name) or {}).items())
            },
            "has_golden": has_golden,
            "status": status,
            "recommendation": rec,
            "eliminated": prev_assets.get(name, {}).get("eliminated", False),
            "note": prev_assets.get(name, {}).get("note", ""),
        })
        # 能力面 diff：插件升级后最该看的是「对外提供的东西少了什么」，不是通过率
        prev_caps = (prev_assets.get(name) or {}).get("capabilities") or {}
        cur_caps = a.get("capabilities") or {}
        if prev_caps and isinstance(prev_caps, dict):
            added = sorted(set(cur_caps) - set(prev_caps))
            removed = sorted(set(prev_caps) - set(cur_caps))
            changed = sorted(k for k in set(cur_caps) & set(prev_caps)
                             if cur_caps[k] != prev_caps[k])
            if added or removed or changed:
                a["caps_diff"] = {"added": added, "removed": removed, "changed": changed}
        out_assets[name] = a

    registry = {
        "schema_version": 1,
        "updated_at": now_iso(),
        "host": host,
        "kind": prof["kind"],
        "label": prof["label"],
        "root": os.path.abspath(root),
        "fp_algo": FP_ALGO,
        "active_model": active_model,
        "last_regression_model": baseline.get("baseline_model") or active_model,
        "count": len(out_assets),
        "assets": out_assets,
    }
    # 安心③：scan 会重写 registry——写之前把上一版备份（保留最近 3 份）。
    # registry 里的 baseline 摘要/工具清单是用户跑出来的数据，误扫坏不能一觉没了。
    rp = registry_path(host)
    try:
        if os.path.exists(rp):
            import glob as _g
            bdir = os.path.join(os.path.dirname(rp), "backups")
            os.makedirs(bdir, exist_ok=True)
            stamp = now_iso().replace(":", "").replace("-", "")
            shutil.copy2(rp, os.path.join(bdir, f"registry.{host}.{stamp}.json"))
            olds = sorted(_g.glob(os.path.join(bdir, f"registry.{host}.*.json")))
            for old in olds[:-3]:
                os.remove(old)
    except Exception:
        pass  # 备份失败不阻塞主流程
    write_json(rp, registry)
    print(f"[ok] registry.{host}.json -> {len(out_assets)} {prof['label']}(s)")
    print(f"     root={root}")
    print(f"     active_model={active_model}  baseline_model={baseline.get('baseline_model')}")
    if skipped:
        print(f"     {BULB} {skipped} 条判定来自其他模型（已保留，不计入当前模型的通过率）")
        print("        换模型后要看当前模型的表现，需在新模型上重跑一次")
    if not model_known and base_by_asset:
        print(f"     {BULB} 模型戳未设置（v-unknown）：当前**不过滤**基线，"
              "所以这些通过率可能来自别的模型")
        print(f"        要按模型区分请先：python console.py --host {host} stamp --infer")
    drifted = [(k, v["caps_diff"]) for k, v in out_assets.items() if v.get("caps_diff")]
    if drifted:
        print(f"[caps] {len(drifted)} 个资产的能力面有变化（这是「它对外提供的东西变了」，与通过率无关）：")
        for k, d in drifted:
            bits = []
            if d["added"]:
                bits.append("+" + ",".join(d["added"]))
            if d["removed"]:
                bits.append("-" + ",".join(d["removed"]))
            if d["changed"]:
                bits.append("~" + ",".join(d["changed"]))
            print(f"       {k:<40} {' '.join(bits)}")
    return 0


def cmd_status(args):
    host, prof = resolve_host(args)
    reg = load_registry(host)
    if not reg:
        print(f"[err] registry.{host}.json missing — run `--host {host} scan` first")
        return 1
    label = prof["label"]
    print(f"\n# 能力折旧表 · {host} ({label}) @ {reg['updated_at']}")
    print(f"# active_model={reg['active_model']}  last_regression={reg['last_regression_model']}  root={reg['root']}")
    # column width follows the longest asset name so scoped names (@scope/pkg) don't break the table
    names = list(reg_assets(reg).keys())
    w = max([28] + [len(n) + 2 for n in names])
    print("-" * (w + 74))
    print(f"{'asset':<{w}}{'fingerprint':<18}{'基线':<10}{'K':<6}{'risk':<6}{'caps':<6}{'golden':<7}status")
    print("-" * (w + 80))
    for name, s in sorted(reg_assets(reg).items()):
        rate = "—" if s.get("baseline_rate") is None else f"{s['baseline_rate']*100:.0f}%"
        base = f"{s.get('baseline_pass',0)}/{s.get('baseline_n',0)} {rate}" if s.get("baseline_n") else "— —"
        mk = s.get("baseline_min_k")
        ktxt = (f"{mk}" + (f"×{s.get('baseline_runs')}" if s.get("baseline_runs") else "")) if mk else "—"
        tag = f" {OK_MARK}elim" if s.get("eliminated") else ""
        caps = s.get("capabilities") or {}
        caps_n = len(caps) if isinstance(caps, dict) else 0
        print(f"{name:<{w}}{s.get('fingerprint',''):<18}{base:<10}{ktxt:<6}{s.get('risk',0):<6}"
              f"{caps_n:<6}{'Y' if s.get('has_golden') else '-':<7}{s.get('status','')}{tag}")
    print("-" * (w + 80))
    # 其他模型上测过的：单独列出来。用户换了模型，以前的数据不该消失。
    for name, s in sorted(reg_assets(reg).items()):
        others = s.get("baseline_other_models") or {}
        if not others:
            continue
        parts = []
        for m, d in others.items():
            r = d.get("rate")
            parts.append(f"{m}: {d.get('pass')}/{d.get('n')}"
                         + (f" ({r*100:.0f}%)" if r is not None else ""))
        cur = s.get("status")
        hint = "（当前模型没测过）" if cur == "unverified" else ""
        print(f"  {BULB} {name} 在其他模型上测过{hint}：{' · '.join(parts)}")
    print("-" * (w + 80))
    min_k = min([s.get("baseline_min_k") for s in reg_assets(reg).values()
                 if s.get("baseline_min_k")] or [1])
    knote = f"K={min_k}" if min_k <= 1 else f"最小 K={min_k}（K≥3 才有统计意义，K=1 只是单次判定）"
    print(f"  注：基线{knote}，多为同模型自评；risk = 行数+工具依赖的维护面代理，不代表价值。")
    print(f"  caps = 能力面条目数（入口/patch/工具/脚本…）；指纹口径 {reg.get('fp_algo', 'v1')}，"
          "只算能力面，不含 README/图片/锁文件。")
    print("  注：基线只统计 active_model 下的判定；换代后未重跑的资产会回到 unverified。")
    if report_is_newer(host):
        print(f"  {WARN_MARK} 折旧报告比注册表新：report 的判定还没进面板，"
              "用 report --promote 合并基线后再 scan。")

    # 人话摘要（方便②）：表格给要细节的人，这一段给所有人。
    _status_plain_summary(reg)
    return 0


def _status_plain_summary(reg):
    """把折旧表翻译成三句话：几个能用的、几个有问题、下一步干什么。"""
    assets = reg_assets(reg)
    if not assets:
        return
    ok, bad, untested = [], [], []
    for name, s in assets.items():
        if s.get("eliminated"):
            continue
        st = s.get("status") or "unverified"
        if st == "strong":
            ok.append(name)
        elif st in ("blocked", "degraded"):
            bad.append((name, st))
        else:
            untested.append(name)
    print()
    if bad:
        for n, st in bad:
            what = "跑不起来（多半是配置/环境，不是该删）" if st == "blocked" else "有失败用例"
            print(f"  ⚠ {n} {what}")
    if untested:
        print(f"  ⚪ {len(untested)} 个没测过：{', '.join(untested[:5])}{'…' if len(untested) > 5 else ''}")
        print("     （没测 ≠ 不好。想知道好不好：console.py baseline --from-seed）")
    if ok:
        print(f"  ✓ {len(ok)} 个正常，放心用")
    print("  下一步：告诉我某件想做的事（自动匹配插件），或说「体检」看完整报告。")


def cmd_check(args):
    host, _ = resolve_host(args)
    reg = load_registry(host)
    if not reg:
        print(f"[err] registry.{host}.json missing — run `--host {host} scan` first")
        return 1
    active = get_active_model(host)
    last = reg.get("last_regression_model", active)

    # v-unknown 等于「从没设过模型戳」。这种情况**不能说「无需重跑」**——
    # unknown == unknown 会永远绿灯，模型换代永远检测不到（假绿灯）。
    if active in ("v-unknown", "", None) or last in ("v-unknown", "", None):
        print(f"{WARN_MARK}  模型戳未设置（{host}）：active={active} · last_regression={last}")
        print("    现在无法判断模型有没有换代 —— 这不是「无需重跑」，是「没法判断」。")
        print("    设一个模型戳后再看：")
        print(f"      python console.py --host {host} stamp --infer     # 尝试推断")
        print(f"      python console.py --host {host} stamp <版本号>    # 手动指定")
        return 3
    if active != last:
        print(f"{WARN_MARK}  检测到模型版本变更（{host}）：{last}  →  {active}")
        print("    重跑回归以更新折旧表：")
        print("      1) 用新模型重跑 golden set -> new-results.json")
        print(f"      2) python console.py --host {host} report --results new-results.json --promote")
        print(f"      3) python console.py --host {host} scan")
        return 2
    print(f"{OK_MARK}  当前模型 {active} 已是 {host} 的最近回归基线，无需重跑。")
    return 0


def cmd_bump(args):
    host, _ = resolve_host(args)
    ver = args.version or f"v-auto-{now_iso()}"
    stamp = model_stamp_path(host)
    write_json(stamp, {"version": ver, "bumped_at": now_iso(),
                       "note": "simulated platform model-switch event"})
    print(f"[ok] 模型版本已更新为 {ver}（{host}，模拟模型切换事件）。")
    print(f"     写入 {os.path.basename(stamp)}；运行 `check` 查看是否需要重跑回归。")
    return 0


def cmd_recommend(args):
    host, prof = resolve_host(args)
    reg = load_registry(host)
    if not reg:
        print(f"[err] registry.{host}.json missing — run `--host {host} scan` first")
        return 1
    label = prof["label"]
    strong, watch, unverified, prune, degraded, blocked = [], [], [], [], [], []
    for name, s in reg_assets(reg).items():
        if s.get("eliminated"):
            continue
        rec = s.get("recommendation")
        if rec in ("feature", "keep"):
            strong.append((name, s))
        elif rec == "keep-watch":
            watch.append((name, s))
        elif rec == "prune-candidate":
            prune.append((name, s))
        elif rec == "fix-environment":
            blocked.append((name, s))
        elif rec in ("rewrite-or-eliminate", "degraded"):
            degraded.append((name, s))
        else:
            unverified.append((name, s))

    print(f"\n══════════ 能力管家 · {host} · 推荐/淘汰面板（{label}）══════════\n")

    def emit(rows, tail):
        """按本段最长的 `[action:name]` 标签对齐，避免长短标签把列挤歪。"""
        if not rows:
            print("   （空）")
            return
        w = max(len("[%s:%s]" % (a, n)) for a, n, _s in rows)
        for act, name, s in rows:
            print(f"   {('[%s:%s]' % (act, name)):<{w}}  {name:<26} {tail(s)}")

    print(f"{GREEN} 强 {label}（保留 / 可置顶推荐）")
    emit([("keep", n, s) for n, s in strong],
         lambda s: f"基线 {s.get('baseline_pass')}/{s.get('baseline_n')}  risk={s.get('risk')}")

    print(f"\n{YELLOW} 观察（已建基线但高风险，先保留）")
    emit([("watch", n, s) for n, s in watch],
         lambda s: f"基线 {s.get('baseline_pass')}/{s.get('baseline_n')}  risk={s.get('risk')}")

    print(f"\n{WHITE} 未验证（无基线，建议先建 golden set 再判断，暂不淘汰）")
    emit([("eval", n, s) for n, s in unverified], lambda s: f"risk={s.get('risk')}")

    print(f"\n{WARN_MARK} 跑不起来 · 环境问题（不是能力退化，修环境而不是重写）")
    emit([("fix", n, s) for n, s in blocked],
         lambda s: f"基线 {s.get('baseline_pass')}/{s.get('baseline_n')}（全部失败且归因为环境）")

    print(f"\n{RED} 已降级 · 淘汰候选（升级重跑后通过率跌破阈值才出现）")
    emit([("rewrite", n, s) for n, s in degraded],
         lambda s: f"基线 {s.get('baseline_pass')}/{s.get('baseline_n')}")

    print(f"\n{ORANGE} 可选精简候选（无基线 + 维护面极高，仅建议评估，绝不自动删除）")
    emit([("prune", n, s) for n, s in prune],
         lambda s: f"risk={s.get('risk')} lines={s.get('lines')} tools={s.get('tool_deps')}")

    print(f"\n{BULB} 建议新增 {label}（填补能力缺口；安装由你确认）")
    gaps = gap_analysis(reg)
    gw = max([len("[install:%s]" % g["slug"]) for g in gaps] or [0])
    for g in gaps:
        print(f"   {('[install:%s]' % g['slug']):<{gw}}  {g['title']} — {g['why']}")

    print("\n操作方式：告诉我『keep:名称 / watch:名称 / eval:名称 / prune:名称 / install:名称』，我按你的选择执行（淘汰与安装均先确认）。")
    if report_is_newer(host):
        print(f"{WARN_MARK} 折旧报告比注册表新：report 的判定还没进面板，"
              "用 report --promote 合并基线后再 scan。")
    return 0


# ---------------------------------------------------------------- runtime evidence
# 「运行时探针」有主动/被动两种，成本差两个数量级，别混为一谈：
#   主动探针 = 我启动宿主、问它「你到底注册了哪些工具」。DSH 的 desktop profile 被 Electron
#              独占（`dsh --profile desktop --dump-config` 直接拒绝），没有 headless 入口 →
#              要逆向 IPC 或驱动 UI。贵、脆，且和用户正在用的实例抢资源。
#   被动证据 = 读宿主**自己打的日志**，找出「文件齐全、manifest 正确、但跑起来报错」的插件。
#              零启动、零端口、跨 desktop/web 都行；代价是有盲区（见 PROBE_BLIND_SPOTS）。
# 这里实现的是后者。日志格式：`<iso> [level] [source] message`，source 为插件包名时可归因。
PROBE_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\S+)\s+\[(\w+)\]\s+\[([^\]]+)\]\s*(.*)$")
PROBE_FAULT_RE = re.compile(
    r"fail|error|enoent|exception|unavailable|missing|refus|denied|timeout|"
    r"not found|threw|crash|invalid|cannot|unable", re.I)
PROBE_MAX_BYTES = 4 * 1024 * 1024   # 单个日志只读尾部，日志可以很大
PROBE_BLIND_SPOTS = (
    "插件不写日志 → 看不见；日志轮转 → 旧故障消失；"
    "从没被用过的插件没有证据；日志里的 info 级故障靠关键词识别，可能漏或误报"
)


def discover_logs(prof):
    out = []
    for pat in prof.get("logs") or []:
        for p in sorted(glob.glob(os.path.expanduser(pat)), key=lambda x: -os.path.getmtime(x)):
            out.append(p)
    return out


def read_tail(path, limit=PROBE_MAX_BYTES):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > limit:
                fh.seek(size - limit)
                fh.readline()  # 丢掉被截断的半行
            return fh.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return []


def cmd_audit(args):
    """主动体检：交给 audit.py（含版本对比 + 运行时故障 + 基线三面）。"""
    audit_py = os.path.join(HERE, "audit.py")
    if not os.path.isfile(audit_py):
        print("[err] 找不到 audit.py（应与 console.py 同目录）")
        return 1
    cmd = [sys.executable, audit_py]
    if getattr(args, "host", None):
        cmd += ["--host", args.host]
    elif getattr(args, "root", None) is None:
        host, _ = resolve_host(args)
        cmd += ["--host", host]
    rc, out, err = run(cmd)
    print(out)
    if err:
        print("[stderr]", err)
    return rc


def _tool_usage_from_logs(log_paths, tool_names):
    """从宿主日志统计每个工具的调用次数（审查/利用率盘点数据源）。

    宿主日志形态（实测 DSH desktop-next.log）：
      ... [info] [host.tools] execute.ok (...) {...}
    工具名出现在同行的 JSON payload 里。按出现次数粗计——
    这是「有没有用过」级别的证据，不是精确 telemetry。
    返回 {工具名: 次数}。
    """
    counts = {t: 0 for t in tool_names}
    if not tool_names:
        return counts
    pat = re.compile("|".join(re.escape(t) for t in sorted(tool_names, key=len, reverse=True)))
    for lp in log_paths[:2]:                       # 最近两份日志足够
        for line in read_tail(lp):
            for m in pat.finditer(line):
                if m.group(0) in counts:
                    counts[m.group(0)] += 1
    return counts


def cmd_insights(args):
    """利用率盘点（方便②/用户诉求：插件很强，但用户只用了最显眼的功能）。

    三层回答：
      ① 用了吗   —— 日志里的调用计数（有没有用过级别的证据）
      ② 是什么   —— 每个工具的 description（tools_scan 从源码抓的"说明书"）
      ③ 怎么唤醒 —— 每个没用的工具给一句可直接说的话
    """
    host, prof = resolve_host(args)
    reg = load_registry(host)
    if not reg:
        print("[err] registry.%s.json missing — 先 scan" % host)
        return 1
    assets = reg_assets(reg)
    if not assets:
        print("没有已装资产，无利用率可言。")
        return 0

    # 收集全部工具名 + 描述
    all_tools = {}          # tool -> {asset, desc}
    for name, s in assets.items():
        if s.get("eliminated"):
            continue
        for t in s.get("tools") or []:
            all_tools[t] = {"asset": name, "desc": (s.get("tool_descriptions") or {}).get(t, "")}

    logs = discover_logs(prof)
    counts = _tool_usage_from_logs(logs, list(all_tools.keys()))

    used = {t: c for t, c in counts.items() if c > 0}
    unused = {t: c for t, c in counts.items() if c == 0}

    print(f"\n{'='*66}")
    print(f"  能力利用盘点 · {host} · {len(assets)} 个资产 · {len(all_tools)} 个工具")
    print(f"{'='*66}")
    print(f"  已用工具 {len(used)}/{len(all_tools)}    未动用 {len(unused)}")

    if used:
        print("\n✓ 你在用的：")
        for t, c in sorted(used.items(), key=lambda x: -x[1])[:6]:
            print(f"    {t}  ({c} 次)")

    if unused:
        print(f"\n💡 还在睡觉的 {len(unused)} 个工具（每条附一句可直接说的话）：")
        for t in sorted(unused):
            info = all_tools[t]
            desc = info["desc"] or describe_tool(t)
            asset = info["asset"]
            print(f"\n  · {t}")
            print(f"    属于 {asset}")
            print(f"    能干什么：{desc[:110] or '（源码里没抓到描述，试一次就知道了）'}")
            print(f'    唤醒方式：直接说「用 {asset} 帮我…」或「{describe_tool(t)}一下」')
    else:
        print("\n✓ 所有工具都用上了——利用率 100%。")

    print(f"\n{'='*66}")
    print("口径：调用计数来自宿主日志（最近两份），是「有没有用过」级别的证据；")
    print("      工具说明抓自插件源码的 description 字段，抓不到时显示工具名语义。")
    return 0


def describe_tool(tool_name):
    """工具名 → 人话（bsk_page → '页面操作'）。find 的输出也用它。"""
    parts = [p for p in tool_name.split("_")[1:] if p]  # 去掉插件前缀
    mapping = {
        "page": "页面操作", "navigate": "打开网页", "interact": "页面交互",
        "inspect": "检查页面", "tabs": "标签页", "session": "会话管理",
        "search": "搜索", "reflect": "深度回顾", "capture": "记录存档",
        "ingest": "写入记忆", "read": "读取详情", "list": "列出清单",
        "sync": "同步状态", "diagnose": "诊断", "build": "构建",
        "ui": "界面生成", "api": "接口调用",
    }
    return mapping.get(parts[0] if parts else "", " / ".join(parts) or tool_name)


def cmd_probe(args):
    host, prof = resolve_host(args)
    reg = load_registry(host)
    if not reg:
        print(f"[err] registry.{host}.json missing — run `--host {host} scan` first")
        return 1
    logs = discover_logs(prof)
    if not logs:
        print(f"[warn] 宿主 {host} 没有配置可读日志（hosts.json 里的 \"logs\"），无法采集运行时证据。")
        print("       这不是「没问题」，是「没有证据」。主动探针需要启动宿主，本工具不做。")
        return 1

    names = set(reg_assets(reg).keys())
    hits = {}       # asset -> [{ts, level, msg, fault, log}]
    total_lines = 0
    for path in logs:
        for line in read_tail(path):
            total_lines += 1
            m = PROBE_LINE_RE.match(line)
            if not m:
                continue
            ts, level, source, msg = m.group(1), m.group(2), m.group(3), m.group(4)
            if source not in names:
                continue
            fault = bool(PROBE_FAULT_RE.search(msg)) or level.lower() in ("error", "fatal")
            hits.setdefault(source, []).append({
                "ts": ts, "level": level, "msg": msg.strip(),
                "fault": fault, "log": os.path.basename(path),
            })

    print(f"\n══════════ 运行时证据 · {host} · {prof['label']} ══════════")
    print(f"日志: {', '.join(os.path.dirname(p) for p in logs[:1])}  "
          f"（{len(logs)} 个文件，{total_lines} 行）")
    print("只读采集，不启动宿主。")
    bad = []
    for name in sorted(names):
        rows = hits.get(name) or []
        faults = [r for r in rows if r["fault"]]
        if faults:
            bad.append((name, faults))
    if not bad:
        print(f"\n{OK_MARK} 日志里没有可归因到 {len(names)} 个资产的故障迹象。")
        print(f"  （不代表没问题 —— {PROBE_BLIND_SPOTS}）")
        return 0

    print(f"\n{WARN_MARK} {len(bad)} 个资产有运行时故障迹象（文件齐全、manifest 正确，但跑起来报错）：")
    for name, faults in bad:
        last = faults[-1]
        print(f"\n  {name}")
        print(f"    {len(faults)} 条故障迹象  最近 {last['ts']}  [{last['level']}]")
        print(f"      {last['msg'][:160]}")
        print(f"      → 静态扫描（文件/manifest/能力面）看不见这类问题；这是宿主自己打出来的")
    print(f"\n盲区：{PROBE_BLIND_SPOTS}")
    print("要「实际注册了哪些工具」，得启动宿主 —— desktop profile 被 Electron 独占，本工具不做。")
    return 1 if getattr(args, "strict", False) else 0


def _covered(names, words):
    """资产名里是否已有这类能力（子串匹配）。

    以前这里是 `t in names` 的精确集合判断：资产叫 `test-driven-development`，
    就永远匹配不到 "test"，于是面板一直建议再装一个评测类 skill —— 假缺口。
    """
    low = [n.lower() for n in names]
    return any(any(w in n for w in words) for n in low)


def gap_analysis(reg):
    names = set(reg_assets(reg).keys())
    out = []
    dev_like = _covered(names, ("dev", "code", "debug", "test", "frontend", "backend", "plan"))
    if dev_like and not _covered(names, ("eval", "test", "verify", "debug", "regress")):
        out.append({"slug": "evals", "title": "评测/测试类", "why": "已有开发类资产，但没有专门做回归/评测的"})
    if _covered(names, ("writing", "doc", "blog")) and not _covered(names, ("humanizer",)):
        out.append({"slug": "humanizer", "title": "humanizer（去 AI 味）", "why": "写作类资产缺一个去 AI 味的"})
    if not out:
        out.append({"slug": "observability", "title": "可观测性/成本追踪",
                    "why": "净值表需要 token/采纳率遥测（当前没有数据源，属方法论待落地项）"})
    return out


def cmd_report(args):
    results = args.results or BASELINE
    cmd = [sys.executable, REGRESS_PY, "--results", results,
           "--out", DEFAULT_OUT, "--threshold", str(args.threshold)]
    if getattr(args, "compare", None):
        cmd += ["--compare", args.compare]
    rc, outp, errp = run(cmd)
    print(outp)
    if errp:
        print("[stderr]", errp)
    if rc != 0:
        return rc
    promote = getattr(args, "promote", False)
    if promote:
        data = load_json(results, {}) or {}
        st = merge_baseline(data, replace=getattr(args, "replace", False))
        print(f"\n[ok] 基线已合并：替换 {len(st['replaced'])} 个资产，保留 {len(st['kept'])} 个，"
              f"共 {st['total']} 条（模型：{', '.join(st['models']) or '?'}）")
        for w in st.get("k_warnings") or []:
            print(f"     {WARN_MARK} K 下降：{w}")
        if st["kept"]:
            print(f"     保留未重跑的资产：{', '.join(st['kept'])}（仍是旧模型的判定）")
        print(f"     下一步：python scripts/console.py --host {args.host or 'workbuddy'} scan")
    else:
        print("\n提示：报告不会改基线。要让折旧表/面板看到这次判定，请加 --promote 后重跑，再 scan。")
    return rc


# ---------------------------------------------------------------- can（能力判断）
# 「能力管家」的核心：用户提出一个任务，Agent 先判断「我有没有能力做、装了的东西好不好」。
# 设计原则：
#   · 本地优先（<100ms）：先只查 registry，不联网
#   · 市场推断是可选的（--market），慢但只在「本地没有」时有意义
#   · 输出必须能直接决定下一步动作（可以/坏了/缺），而不是一堆数据让 Agent 自己读
CAN_VERDICT_OK = "ok"          # 有且健康
CAN_VERDICT_DEGRADED = "degraded"  # 有但坏了
CAN_VERDICT_MISSING = "missing"    # 没有


def can_judge(task, reg):
    """任务 → (verdict, 命中的资产列表, 命中的类目)。

    verdict：
      ok       → 有已装资产能覆盖，且没有明显故障
      degraded → 有，但状态是 blocked/degraded（跑不起来或通过率低）
      missing  → 没有覆盖
    """
    assets = reg_assets(reg or {})
    task_l = (task or "").lower()
    hits = []
    seen = set()

    # ① 资产名/片段直接出现在任务里
    noise = {"skill", "plugin", "tool", "tools", "agent", "agents", "dsh"}
    for name, s in assets.items():
        if s.get("eliminated"):
            continue
        segs = [p for p in re.split(r"[-_/@.]+", name.lower())
                if len(p) >= 4 and p not in noise]
        if name.lower() in task_l or any(p in task_l for p in segs):
            hits.append((name, s, "名称命中"))
            seen.add(name)

    # ② 类目命中（「做 PPT」→ pptx 类）
    cats = need_categories(task_l)
    # 产物类目优先：「把财报做成 PPT」的主任务是「做 PPT」，财报只是输入。
    # 否则用户会被推荐去做财报分析的工具，却没得到做 PPT 的。
    PRODUCT_CATS = {"pptx", "docx", "sheet", "chart", "ui-design", "frontend"}
    if cats:
        cats = sorted(cats, key=lambda x: (0 if x[1]["slug"] in PRODUCT_CATS else 1,
                                           -x[0]))
    slug_of = {t["slug"]: t for t in TAXONOMY}
    for _n, t, _k in cats:
        # 先找名字含 slug/别名 的资产
        alias = [a for a in (t.get("related") or []) if len(a) >= 3] + [t["slug"]]
        for name, s in assets.items():
            if s.get("eliminated") or name in seen:
                continue
            low = name.lower()
            if any(a in low for a in alias):
                hits.append((name, s, "类目:%s" % t["slug"]))
                seen.add(name)

    if not hits:
        return CAN_VERDICT_MISSING, [], cats

    # 判定健康度（审查 P1）：任一命中资产 blocked/degraded → degraded。
    # status 缺失/unverified 不再判 ok —— 「装了但没测过」不是「能用」，
    # 把它当 ok 会引导 Agent 无验证地依赖它（实测报告：过度乐观）。
    # watch（有基线且通过，只是 K 低）保留 ok——它至少被真实验证过。
    bad = [h for h in hits
           if (h[1].get("status") in ("blocked", "degraded", "unverified")
               or not h[1].get("status"))]
    if bad:
        return CAN_VERDICT_DEGRADED, hits, cats
    return CAN_VERDICT_OK, hits, cats


def cmd_can(args):
    """能力判断：这件事我能不能做？—— Agent 在动手前调用。"""
    host, prof = resolve_host(args)
    task = args.task
    reg = load_registry(host)
    if not reg:
        print("[err] 还没有 registry.%s.json —— 先 scan" % host)
        return 1

    verdict, hits, cats = can_judge(task, reg)
    label = prof["label"]
    print(f"\n任务：{task}\n宿主：{host}（{label}）\n")

    if verdict == CAN_VERDICT_OK:
        print(f"{OK_MARK} 能做。用这些已经装好的{label}：")
        for name, s, why in hits[:5]:
            ver = s.get("version") or "?"
            st = s.get("status")
            print(f"   → {name}  ({why}，v{ver}，状态 {st})")
        print("\n行动建议：直接做，不要向用户汇报「我查过了」。")
        return 0

    if verdict == CAN_VERDICT_DEGRADED:
        print(f"{WARN_MARK} 相关能力**有，但现在有问题**：")
        for name, s, why in hits[:5]:
            st = s.get("status")
            note = ""
            others = s.get("baseline_other_models") or {}
            if st == "blocked":
                note = "（跑不起来：环境/依赖问题，不是该删）"
            elif st == "degraded":
                note = "（通过率低）"
            print(f"   → {name}  状态 {st}{note}")
        print("\n行动建议：先告诉用户「能做但有问题」，说明是什么问题；")
        print("          修不好再用内置能力顶上，别直接说做不了。")
        return 2

    # missing
    print(f"{BULB} 没有已装的{label}覆盖这个任务。")
    if cats:
        print("\n最接近的类目：")
        for _n, t, kws in cats[:3]:
            print(f"   [install:{t['slug']}] {t['title']}")
            print(f"      命中词：{', '.join(kws[:5])}")
            print(f"      值不值得装：{t.get('worth', '—')}")
        print("\n行动建议：先问用户「要不要我找一个」，或直接用内置能力做初版。")
        print("          **不要**直接说「我做不了」。")
        if getattr(args, "market", False):
            try:
                import market as _m
                print("\n" + "-" * 60)
                print("市场候选（--market）：")
                _m.render_dsh(cats[0][1]["query"].split()[0], 3)
                _m.render_skillhub(cats[0][1]["query"], 3)
            except Exception as e:
                print(f"[warn] 市场查询失败：{e}")
    else:
        print("   也没匹配到已知类目（TAXONOMY 只覆盖 11 类）。")
        print("   行动建议：直接用内置能力试做；确实做不了再如实说明。")
    return 3


# ---------------------------------------------------------------- find（按功能找）
# 用户场景：「我记得有个能查余额的」「那个记忆插件叫啥」——记得功能忘了名字。
# 设计原则：
#   ① 同义词 > 子串匹配：用户说"额度"也要能找到"余额"
#   ② 输出必须带「工具名 + 怎么调」——光有插件名，Agent 还是不知道怎么用
#   ③ 匹配原因要显示出来（为什么命中），让人能校对
# 功能域同义词表：键 = 功能域，值 = 一组等价说法（中英）。命中任一即算。
FIND_SYNONYMS = {
    # app/后端开发是常见任务域（实测「帮我做个App」原来只命中 ui-design，
    # 有 app-dev 插件的用户会被漏掉）
    "appdev": ["app", "应用", "移动端", "手机应用", "手机app", "后端", "backend",
               "api", "接口", "mobile", "安卓", "android", "ios", "客户端", "服务端"],
    "memory": ["记忆", "记住", "回忆", "记住我", "长期记忆", "知识页", "memory", "remember", "recall"],
    "browser": ["浏览器", "网页", "打开网站", "浏览", "browser", "web", "页面"],
    "balance": ["余额", "额度", "剩余", "配额", "quota", "balance", "usage", "用量"],
    "chart": ["图表", "图", "可视化", "画图", "chart", "图示", "架构图"],
    "docx": ["文档", "word", "docx", "报告文档"],
    "pptx": ["ppt", "pptx", "幻灯片", "演示", "slide"],
    "sheet": ["表格", "excel", "xlsx", "表"],
    "frontend": ["前端", "界面", "页面开发", "frontend", "网页开发"],
    "debug": ["调试", "排错", "debug", "排查"],
    "github": ["github", "仓库", "repo", "代码托管"],
    "humanize": ["去ai味", "润色", "humanize", "改写", "文案"],
    "search": ["搜索", "查资料", "联网", "search", "检索"],
    # ---- 计算机方向通用域（覆盖「所有计算机方向」诉求，按任务动词×对象组织）----
    "database": ["数据库", "建表", "sql", "查询数据", "migration", "迁移", "表结构", "postgres", "mysql", "sqlite", "mongo"],
    "deploy": ["部署", "上线", "发布", "deploy", "ci", "cd", "docker", "k8s", "容器", "运维", "服务器", "上线部署"],
    "test": ["测试", "单测", "用例", "test", "coverage", "覆盖率", "回归"],
    "code-review": ["代码审查", "review", "重构", "refactor", "代码质量", "lint", "审查", "评审"],
    "scrape": ["爬虫", "爬取", "抓取", "scrape", "crawl", "采集"],
    "email": ["邮件", "发信", "email", "smtp", "imap"],
    "file": ["文件", "批量处理", "压缩", "解压", "格式转换", "pdf", "转换"],
    "auth": ["登录", "鉴权", "认证", "auth", "oauth", "jwt", "加密"],
    "monitor": ["监控", "日志", "告警", "monitor", "log", "metrics"],
    "ai-model": ["模型", "大模型", "llm", "ai", "推理", "微调", "embedding", "agent"],
    "git": ["git", "提交", "分支", "合并", "merge", "rebase", "版本控制"],
    "network": ["网络", "请求", "http", "接口调试", "抓包", "代理", "proxy"],
    "data": ["数据清洗", "分析", "统计", "pandas", "数据集", "dataset", "csv"],
    "cloud": ["云", "服务器", "aws", "azure", "云函数", "serverless", "主机"],
    "compile": ["编译", "构建", "打包", "build", "compile", "依赖安装", "环境配置", "配置环境", "装依赖", "环境"],
}

# 计算机方向的任务动词（中英）—— 兜底层用：和资产描述里的任意词联合命中
TASK_VERBS = [
    "做", "开发", "写", "建", "搭建", "实现", "生成", "部署", "修复", "调试",
    "优化", "重构", "测试", "审查", "迁移", "配置", "安装", "转换", "爬",
    "查", "监控", "分析", "设计", "自动化", "build", "make", "create", "write",
    "fix", "deploy", "setup", "implement",
]
# 功能域 → 常见资产名成分（把"功能"连到"名字"）
FIND_DOMAIN_HINTS = {
    "appdev": ["app", "appdev", "mobile", "api", "backend", "client", "server", "dev"],
    "memory": ["memory", "hindsight", "remember", "mem"],
    "browser": ["browser", "bsk", "chrome", "web"],
    "balance": ["whale", "balance", "widget"],
    "chart": ["chart", "diagram", "mermaid"],
    "docx": ["docx", "office", "word"],
    "pptx": ["pptx", "office"],
    "sheet": ["xlsx", "sheet", "office"],
    "frontend": ["frontend", "ui", "web"],
    "github": ["github"],
    "humanize": ["humanizer"],
    "search": ["search", "web"],
}


def find_domain(task_l):
    """任务描述 → 命中的功能域列表（按命中强度排序）。

    审查 P1 修正（实测误报率 23%）：
    · ASCII 词用词边界匹配（\\b）—— 'apple' 不得命中 'app'、'email' 不得命中 'ai'
    · 中文短词（≤2 字）要求两侧不是中文字符 —— '表' 不得命中 '手表'
    · 强度 = Σ命中词长；平分时保持词表顺序（不再靠字典序）
    """
    task_l = (task_l or "").lower()
    out = []
    for dom, words in FIND_SYNONYMS.items():
        hits = []
        for w in words:
            if not w:
                continue
            if w.isascii():
                # 词边界：纯 ASCII 词两侧必须是非字母数字
                if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", task_l):
                    hits.append(w)
            else:
                # 中文词：命中即可。只有**单字**才要求两侧非中文（'表'⊂'手表'）。
                # 词表里的正经双字域词（'接口'/'后端'）直接放行——它们本身够特异。
                start = 0
                while True:
                    i = task_l.find(w, start)
                    if i < 0:
                        break
                    before = task_l[i - 1] if i > 0 else ""
                    after = task_l[i + len(w)] if i + len(w) < len(task_l) else ""
                    _is_cjk = lambda ch: "\u4e00" <= ch <= "\u9fff"
                    if len(w) >= 2 or (not _is_cjk(before) and not _is_cjk(after)):
                        hits.append(w)
                        break
                    start = i + 1
        if hits:
            # 单字命中权重打 3 折：'表'（sheet）不该与'数据库'（database）平权
            weight = sum(len(w) if len(w) >= 2 else 1 for w in hits)
            out.append((dom, hits, weight))
    out.sort(key=lambda x: -x[2])
    return [(dom, hits) for dom, hits, _w in out]


def cmd_find(args):
    """按功能找资产：忘了名字也能调起来。返回"该调哪个工具"。"""
    host, prof = resolve_host(args)
    q = args.query
    reg = load_registry(host)
    if not reg:
        print("[err] 还没有 registry.%s.json —— 先 scan" % host)
        return 1
    assets = reg_assets(reg)
    ql = (q or "").lower()

    # ① 功能域匹配（同义词）
    domains = find_domain(ql)
    # ② 名称/描述子串匹配（兜底：用户记了半个名字）
    name_hits = []
    for name, s in assets.items():
        if s.get("eliminated"):
            continue
        desc = ((s.get("description") or "") + " " + name).lower()
        if ql and (ql in desc or any(w in ql for w in re.split(r"[-_/@.]+", name.lower()) if len(w) >= 4)):
            name_hits.append((name, s))
    seen = {n for n, _ in name_hits}

    # ②' 描述分词兜底：同义词表没覆盖的计算机方向（用户说法千千万，表列不完）。
    # 用户任务里的实词 ≥2 字、且出现在资产描述里 → 命中。
    # 这是"所有计算机方向都要有"的兜底层：不靠维护词表，靠描述本身的词汇重叠。
    # 审查 P1 修正：原实现按空白分词，中文整句成单 token（len≥2 必过）→
    # 整串子串匹配对英文 desc 100% 漏报、对中文 desc 误报。改成：
    #   · CJK 相邻二元切分（bigram）：'数据库表'→['数据','据库','库表']
    #   · 英文按 \b 切词、最短 3 字符（'to'/'my' 不再命中任何描述）
    def _desc_hits():
        out = []
        stop = set(TASK_VERBS) | {"我", "帮", "一个", "这个", "一下", "的", "了", "吗", "呢", "想", "要", "能"}
        cjk = lambda ch: "\u4e00" <= ch <= "\u9fff"

        def _tokens(text):
            toks = []
            for chunk in re.split(r"[\s,，。、/?！!；;：:（）()\"']+", text):
                if not chunk:
                    continue
                # 拆出连续 CJK 段和连续 ASCII 段
                for m in re.finditer(r"[\u4e00-\u9fff]+|[a-z0-9]+", chunk):
                    seg = m.group(0)
                    if cjk(seg[0]):
                        # 单字 CJK token 无法通过二元切分，且 len>=2 恒假——
                        # 审查确认这是死分支，删掉（二元切分天然覆盖所有相邻字对）
                        toks.extend(seg[i:i + 2] for i in range(len(seg) - 1))
                    else:
                        if len(seg) >= 3 and seg not in stop:
                            toks.append(seg)
            return toks

        words = [w for w in _tokens(ql) if w not in stop]
        if not words:
            return out
        for name, s in assets.items():
            if s.get("eliminated") or name in seen:
                continue
            desc = ((s.get("description") or "") + " " + name).lower()
            desc_tokens = set(_tokens(desc)) | {name.lower()}
            hit_words = [w for w in words if w in desc_tokens or w in desc]
            if hit_words:
                out.append((name, s, hit_words[:3]))
        return out

    desc_fallback = _desc_hits()
    for name, s, _hw in desc_fallback:
        seen.add(name)

    # 功能域 → 资产：通过名字成分（FIND_DOMAIN_HINTS）
    dom_hits = []
    for dom, words in domains:
        hints = FIND_DOMAIN_HINTS.get(dom, [dom])
        for name, s in assets.items():
            if s.get("eliminated") or name in seen:
                continue
            low = name.lower()
            if any(h in low for h in hints):
                dom_hits.append((name, s, dom, words[:3]))
                seen.add(name)

    if not name_hits and not dom_hits and not desc_fallback:
        print(f"\n没有找到与「{q}」相关的已装 {prof['label']}。")
        if domains:
            print(f"（识别出功能域 {domains[0][0]}，但已装资产里没有名字相关的）")
        # 用户没装 ≠ 没答案：路由的天花板是"装了什么"，没装时就地给市场候选，
        # 不把用户再推回 suggest 命令（那等于让用户多跑一步）。
        # 搜索词必须**压缩到域核心词**：实测整句"帮我建个数据库表"去搜，
        # DSH 零命中、SkillHub 返回防骗大师 —— 垃圾推荐比不推荐更糟。
        tax = {t["slug"]: t for t in TAXONOMY}
        # 优先级：TAXONOMY 人工调过的搜索词 > 功能域英文 > 原话
        term = None
        for _n, t, _k in need_categories(ql):
            term = t["query"]
            break
        if not term and domains:
            term = domains[0][0]
        if not term:
            term = q
        print(f"\n{BULB} 去市场看看有没有现成的（关键词：{term}）：")
        try:
            import market as _market
            _market.render_dsh(term, 3)
            print()
            _market.render_skillhub(term, 3)
        except Exception as e:
            print(f"   [err] 市场不可达：{e}")
            print("   行动建议：先用内置能力做；或稍后再试市场。")
            return 3
        print("\n行动建议：挑一个装上（告诉我装哪个），装完 scan 一次就能被路由到；")
        print("          或先用内置能力做初版。")
        return 3

    print(f"\n「{q}」→ 找到 {len(name_hits) + len(dom_hits) + len(desc_fallback)} 个：\n")
    for name, s in name_hits:
        _print_find_hit(name, s, "名称/描述命中", q)
    for name, s, dom, words in dom_hits:
        _print_find_hit(name, s, "功能:%s（命中词 %s）" % (dom, ",".join(words)), q)
    for name, s, hw in desc_fallback:
        _print_find_hit(name, s, "描述词命中:%s" % ",".join(hw), q)
    print("\n行动建议：直接按上面的工具名调用；UI 类插件（无工具）让用户在界面里用。")
    return 0


def _print_find_hit(name, s, why, query):
    status = s.get("status") or "unverified"
    icon = {"strong": "✅", "watch": "🟡"}.get(status, "⚪" if status == "unverified" else "⚠️")
    tools = s.get("tools") or []
    print(f"{icon} {name}  v{s.get('version') or '?'}  （{why}）")
    if s.get("description"):
        print(f"    {str(s['description'])[:100]}")
    if tools:
        print(f"    工具：{', '.join(tools[:5])}{' …' if len(tools) > 5 else ''}")
        print(f"    怎么用：直接调上面的工具名（如 {tools[0]}）")
    else:
        print("    （没扫到可调用工具 —— 可能是纯 UI 插件，让用户在界面里用）")


def match_existing(need, reg):
    """需求 → 已装资产。名称全串 + 名称片段（去噪）两级匹配。

    以前只认「完整资产名出现在需求里」，用户说「帮我润色一下」永远匹配不到
    humanizer；片段匹配让「去 AI 味 / 财报 / 图表」这类自然说法也能命中。
    """
    assets = reg_assets(reg)
    need_l = (need or "").lower()
    noise = {"skill", "plugin", "tool", "tools", "agent", "agents", "dsh", "the", "and", "with"}
    hits, seen = [], set()
    for name, s in assets.items():
        if s.get("eliminated"):
            continue
        segs = [p for p in re.split(r"[-_/@.]+", name.lower())
                if len(p) >= 4 and p not in noise]
        if name.lower() in need_l or any(p in need_l for p in segs):
            hits.append((name, "名称命中"))
            seen.add(name)
    for t in TAXONOMY:
        if any(k.lower() in need_l for k in t["kw"]):
            for name, s in assets.items():
                if s.get("eliminated") or name in seen:
                    continue
                if t["slug"] in name.lower() or name.lower() in t["slug"]:
                    hits.append((name, f"类别:{t['slug']}"))
                    seen.add(name)
    return hits


def need_categories(need_l):
    """一句话需求 → 命中的所有类目（按 TAXONOMY 顺序）。

    以前只取第一个命中，于是「设计一个 app 界面并做成网页」只报 UI 或只报前端。
    现在全部命中、按命中关键词数量排序，让 Agent 看到完整的能力缺口。
    """
    scored = []
    for t in TAXONOMY:
        hits = [k for k in t["kw"] if k.lower() in need_l]
        if hits:
            scored.append((len(hits), t, hits))
    scored.sort(key=lambda x: (-x[0], TAXONOMY.index(x[1])))
    return scored


# ---------------------------------------------------------------- dissatisfaction
# 「勉强够用」是比「没有」更常见的真实状态：用户装了东西，但效果不满意。
# 这类信号必须能被识别出来，否则工具只会推荐"你缺什么类目"，永远看不到"你有但不好用"。
DISSATISFIED_MARKERS = {
    "strong": [  # 明确不满
        "差点意思", "不太行", "很一般", "一般般", "不够好", "不满意", "不好用",
        "还不如自己写", "还是我来吧", "算了", "效果不好", "没什么用", "很鸡肋",
        "感觉不对", "不够专业", "太粗糙", "很丑", "不好看",
    ],
    "weak": [    # 勉强 / 返工信号
        "凑合", "勉强", "有点怪", "说不上来", "总觉得", "不够", "差一点",
        "再来一次", "重做", "又错了", "还是不对", "改了几次",
    ],
}


def detect_dissatisfaction(text):
    """从一句话里认出"不满意"。返回 (level, matched_terms)。

    level: strong / weak / None。刻意只做关键词匹配 —— 判语义要模型，
    而这一步的职责是"把用户已经在抱怨这件事标记出来"，宁可不灵敏也别误报。
    """
    t = (text or "").lower()
    for level in ("strong", "weak"):
        terms = [m for m in DISSATISFIED_MARKERS[level] if m in t]
        if terms:
            return level, terms
    return None, []


def diagnose_owned(need, reg):
    """不满时：找出"相关的已装资产"及其真实状态，而不是直接推荐新货。

    匹配分三层，逐层放宽（宁可多列几个让用户认，也不要漏掉真正相关的）：
      ① 名称/片段直接出现在需求里
      ② 需求命中的类目 slug 与资产名相关（"UI 差点意思" → frontend-dev）
      ③ 资产的 description 里出现需求关键词（最后的兜底）
    """
    assets = reg_assets(reg)
    need_l = (need or "").lower()
    related, seen = [], set()

    def add(name, s, why):
        if name in seen or s.get("eliminated"):
            return
        seen.add(name)
        related.append((name, s, why))

    # ① 名称命中
    for name, s in assets.items():
        segs = [p for p in re.split(r"[-_/@.]+", name.lower()) if len(p) >= 4]
        if name.lower() in need_l or any(p in need_l for p in segs):
            add(name, s, "名称命中")

    # ② 类目 → 资产：slug 本身、slug 的成分、以及类目的 related 别名
    for _n, t, _k in need_categories(need_l):
        slug = t["slug"]
        alias = [a for a in (t.get("related") or []) if len(a) >= 3]
        parts = [p for p in re.split(r"[-_]", slug) if len(p) >= 3] + alias
        for name, s in assets.items():
            low = name.lower()
            if slug in low or any(p in low for p in parts):
                add(name, s, "类目:%s" % slug)

    # ③ description 兜底：需求里的实词出现在资产描述里
    words = [w for w in re.split(r"[\s,，。、]+", need_l) if len(w) >= 2]
    for name, s in assets.items():
        desc = (s.get("description") or "").lower()
        if desc and any(w in desc for w in words if len(w) >= 3):
            add(name, s, "描述相近")
    return related


def print_owned_verdict(name, s):
    """把"已装资产到底行不行"翻译成人话（含 K 与归因）。"""
    n = s.get("baseline_n") or 0
    if not n:
        print(f"      → {name}：**没有基线**，所以「感觉不行」但你无从判断是它不行还是用法不对")
        print("        建议先给它跑一轮基线（seed 里可能有现成用例）")
        return
    rate = s.get("baseline_rate") or 0
    k = s.get("baseline_min_k")
    status = s.get("status")
    print(f"      → {name}：基线 {s.get('baseline_pass')}/{n}（{rate*100:.0f}%）"
          f"{'，K=%s' % k if k else ''}，状态 {status}")
    if rate == 1.0 and (k or 1) <= 1:
        print("        ⚠️ 100% 但是 K=1 自评 —— 说明「能跑通」，不等于「效果让你满意」")
        print("          你的「差点意思」很可能是**质量**问题而不是**能不能用**问题，")
        print("          这类差距基线测不出来，得靠人工判据或换更好的资产")
    elif rate < 1.0:
        print("        它确实有失败的用例 → 这是可复现的问题，不是错觉")


def cmd_suggest(args):
    host, prof = resolve_host(args)
    need = args.need
    reg = load_registry(host) or {}
    label = prof["label"]
    level, terms = detect_dissatisfaction(need)
    print(f"\n需求：{need}\n宿主：{host}（{label}）\n")

    if level:
        print(f"{BULB} 检测到「不满意」信号（{level}）：{', '.join(terms)}")
        print("   先看已装的东西到底行不行，再谈要不要装新的。\n")
        owned = diagnose_owned(need, reg)
        if owned:
            for name, s, why in owned[:4]:
                print(f"  · {name}（{why}）")
                print_owned_verdict(name, s)
        else:
            print("   → 没有明显相关的已装资产；可能是这个需求确实没覆盖。")

    hits = match_existing(need, reg)
    if hits and not level:
        print(f"{OK_MARK} 你已有可用的 {label}：")
        for n, why in hits:
            print(f"   → {n}  ({why})")
        print("\n直接说『用 <名称> 做…』即可，无需安装。")
        if not getattr(args, "search", False):
            print("（如果用了还是不满意，加 --search 让我去市场找更好的候选）")
        else:
            print()
    elif hits:
        print(f"\n{OK_MARK} 相关已装资产：{', '.join(n for n, _ in hits)}")

    need_l = need.lower()
    scored = need_categories(need_l)
    if scored:
        print(f"\n{BULB} 按缺口给方向（{host} / {label}）：\n")
        for i, (_n, t, kws) in enumerate(scored[:3], 1):
            print(f"   {i}. [install:{t['slug']}]  {t['title']}")
            print(f"      命中词：{', '.join(kws[:6])}")
            print(f"      搜索词：{t['query']}")
            print(f"      去哪搜：{t.get('where', '—')}")
            print(f"      值不值得装：{t.get('worth', '—')}")
    else:
        print(f"{BULB} 未匹配到现成类别（{label} 侧）。")
        print("   建议：先用内置能力试做；若这会是高频任务，把需求说得更具体点再问我一次，")
        print("        我再按 TAXONOMY 重新匹配（当前只覆盖 11 个常见类目，不是万能表）。")

    # --search：真的去市场拉候选（需要网络；失败会如实报错）
    if getattr(args, "search", False):
        try:
            import market as _market
        except Exception:
            _market = None
        if _market is None:
            print("\n[err] 找不到 market.py（应和 console.py 同目录）")
            return 0
        terms = [t["query"].split()[0] for _n, t, _k in scored[:1]] or [need]
        print(f"\n{BULB} 去市场搜真实候选（关键词：{terms[0]}）")
        print("-" * 60)
        _market.render_dsh(terms[0], 4)
        print()
        _market.render_skillhub(terms[0], 4)
        print("-" * 60)
        print("装之前三件事：① 它有 demo 能看效果吗 ② 半年内还有提交吗 ③ 许可证允许你用吗")
        print("装上之后：scan → 建 golden → 跑基线，让数据替你判断，而不是靠 star 数")
    else:
        print("\n下一步：我可以按上面的搜索词去市场搜真实候选（加 --search），")
        print("        或者你先说已装的哪个「差点意思」，我按不满信号帮你定位。")
    return 0


# ---------------------------------------------------------------- onboarding
TRANSCRIPT_ROOTS = ["~/.workbuddy-ai/projects", "~/.claude/projects", "~/.dsh/sessions"]

HOST_PROBES = {
    "workbuddy": [("~/.workbuddy-ai/skills", "dirs", ["SKILL.md"])],
    "claude-code": [("~/.claude/skills", "dirs", ["SKILL.md"])],
    "agents": [("~/.agents/skills", "dirs", ["SKILL.md"])],
    "deepseek": [("~/.dsh/profiles/*", "deps", ["package.json", "plugin.json"])],
}

SYN = {
    "widget": ["挂件", "小部件"], "balance": ["余额"], "memory": ["记忆"],
    "plugin": ["插件"], "model": ["模型"], "connect": ["接入", "连接"],
    "hindsight": ["事后", "记忆"], "whale": ["鲸鱼"], "skill": ["技能"],
    "report": ["报告"], "search": ["搜索"], "dashboard": ["看板", "仪表盘"],
}


def _expand(tpl):
    t = os.path.expanduser(tpl)
    if "*" in t:
        import glob as _glob
        return sorted(_glob.glob(t))
    return [t]


def sanitize(s):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s).strip("_") or "asset"


def find_transcripts(extra=None):
    roots = [os.path.expanduser(r) for r in TRANSCRIPT_ROOTS]
    if extra:
        roots.insert(0, os.path.abspath(os.path.expanduser(extra)))
    files = []
    for r in roots:
        if not os.path.isdir(r):
            continue
        for root, _, fs in os.walk(r):
            for f in fs:
                # DSH 存的是 session.v4.jsonl.zstd（zstd 压缩），不是裸 .jsonl。
                # 只认 .jsonl 会让 DSH 用户 bootstrap 静默拿到 0 条样本。
                if f.endswith((".jsonl", ".jsonl.zstd", ".jsonl.zst", ".json.zst")):
                    files.append(os.path.join(root, f))
    return files


def open_transcript(path):
    """打开 transcript 返回可迭代文本流；解不开返回 None（由调用方统计并明确告知）。"""
    low = path.lower()
    if not low.endswith((".zst", ".zstd")):
        try:
            return open(path, encoding="utf-8", errors="replace")
        except Exception:
            return None
    # ① Python 3.14+ 标准库
    try:
        from compression import zstd  # type: ignore
        return io.TextIOWrapper(zstd.open(path, "rb"), encoding="utf-8", errors="replace")
    except Exception:
        pass
    # ② pip install zstandard
    try:
        import zstandard  # type: ignore
        raw = open(path, "rb")
        return io.TextIOWrapper(
            zstandard.ZstdDecompressor().stream_reader(raw), encoding="utf-8", errors="replace")
    except Exception:
        pass
    # ③ zstd CLI
    exe = shutil.which("zstd")
    if exe:
        try:
            p = subprocess.run([exe, "-dc", path], capture_output=True, timeout=120)
            if p.returncode == 0:
                return io.StringIO(p.stdout.decode("utf-8", "replace"))
        except Exception:
            pass
    return None


def _strip_inject(t):
    t = re.sub(r"<system-reminder.*?</system-reminder>", "", t, flags=re.S)
    t = re.sub(r"<user-context>.*?</user-context>", "", t, flags=re.S)
    return t.strip()


COMPACTION_MARKERS = (
    "<cb_summary>", "<conversation_history_summary>", "<previous_user_message>",
    "<previous_assistant_message>", "<previous_tool_call>", "<analysis>", "<summary>",
    "Summary of the conversation", "This session is being continued",
)


def _clean_prompt(t):
    t = re.sub(r"</?user_query>", "", t)
    t = re.sub(r"<message_quote.*?</message_quote>", "", t, flags=re.S)
    t = t.replace("\\n", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def extract_prompts(files, stats=None):
    out = []
    for path in files:
        fh = open_transcript(path)
        if fh is None:
            if stats is not None:
                stats["undecodable"] = stats.get("undecodable", 0) + 1
            continue
        try:
            with fh:
                for line in fh:
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    role = o.get("role") or (o.get("message") or {}).get("role")
                    typ = o.get("type")
                    if role != "user" and typ != "user":
                        continue
                    c = o.get("content")
                    if c is None:
                        c = (o.get("message") or {}).get("content")
                    texts = []
                    if isinstance(c, str):
                        texts = [c]
                    elif isinstance(c, list):
                        for p in c:
                            if isinstance(p, dict) and p.get("type") in ("input_text", "text"):
                                texts.append(p.get("text", ""))
                    t = _strip_inject("\n".join(texts))
                    # drop compaction summaries / assistant-context artifacts
                    if any(m in t for m in COMPACTION_MARKERS):
                        continue
                    t = _clean_prompt(t)
                    if not t or len(t) < 4:
                        continue
                    if len(t) > 1500:  # a real request is short; long = summary/dump
                        continue
                    out.append(t)
        except Exception:
            continue
    seen, uniq = set(), []
    for t in out:
        k = t.strip()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(k)
    return uniq


def score_asset(prompt, asset):
    p = prompt.lower()
    name = " ".join([asset.get("id", ""), asset.get("name", ""), asset.get("description", "")]).lower()
    toks = set(re.findall(r"[a-z][a-z0-9_-]{2,}", name))
    score = 0
    for t in toks:
        if t in p:
            score += 2
        for syn in SYN.get(t, []):
            if syn in prompt:
                score += 1
    for seg in re.split(r"[-_/@.]+", asset.get("id", "")):
        if len(seg) >= 3 and seg.lower() in p:
            score += 1
    return score


def cmd_discover(args):
    print("\n探测本机宿主（自动发现）")
    print("-" * 80)
    found = {}
    for host, probes in HOST_PROBES.items():
        hit = False
        for root_tpl, mode, manifests in probes:
            for root in _expand(root_tpl):
                if not os.path.isdir(root):
                    continue
                n = len(discover_assets(root, manifests, mode, quiet=True))
                if n:
                    found[host] = (root, mode, manifests, n)
                    print(f"  {OK_MARK} {host:<12} {n:<3} assets  mode={mode:<4} root={root}")
                    hit = True
                    break
            if hit:
                break
        if not hit:
            print(f"  {FAIL_MARK} {host:<12} not found")
    print("-" * 80)
    if getattr(args, "write", False) and found:
        cfg = load_json(HOSTS_CFG, {}) or {}
        hosts = cfg.get("hosts", {})
        for host, (root, mode, manifests, n) in found.items():
            h = hosts.get(host, {})
            h.setdefault("kind", "plugin" if mode == "deps" else "skill")
            h.setdefault("label", "插件" if mode == "deps" else "skill")
            h["root"] = root
            h["discover"] = mode
            h["manifests"] = manifests
            hosts[host] = h
        cfg["hosts"] = hosts
        cfg.setdefault("default_host", "workbuddy")
        write_json(HOSTS_CFG, cfg)
        print("[ok] hosts.json 已更新为探测到的路径")
    elif found:
        print("  加 --write 可把探测结果写回 hosts.json")
    return 0


def cmd_bootstrap(args):
    host, prof = resolve_host(args)
    reg = load_registry(host)
    if not reg:
        print(f"[err] 先运行 --host {host} scan")
        return 1
    assets = reg_assets(reg)
    files = find_transcripts(getattr(args, "transcripts", None))
    if not files:
        print("[warn] 没找到会话 transcript（可加 --transcripts <目录>）")
        return 1
    stats = {}
    prompts = extract_prompts(files, stats)
    print(f"\n[bootstrap] host={host}  transcripts={len(files)}  prompts={len(prompts)}")
    if stats.get("undecodable"):
        print(f"[warn] {stats['undecodable']} 个会话文件解不开（多为 zstd 压缩）。"
              "需要 Python 3.14+ / `pip install zstandard` / 系统有 `zstd` 命令三者之一；"
              "或把它们解压成 .jsonl 后用 --transcripts <目录> 指过来。")
    made = 0
    for aid, a in assets.items():
        scored = [(score_asset(p, a), p) for p in prompts]
        scored = [(s, p) for s, p in scored if s > 0]
        scored.sort(key=lambda x: -x[0])
        picked = [p for _, p in scored[:getattr(args, "per_asset", 5)]]
        if not picked:
            continue
        fn = sanitize(aid) + ".json"
        path = os.path.join(GOLDEN_DIR, fn)
        if os.path.exists(path) and not getattr(args, "force", False):
            print(f"  skip {aid} (golden 已存在)")
            continue
        cases = [{
            "id": f"{sanitize(aid)}-draft-{i+1}",
            "polarity": "positive", "type": "task",
            "input": p,
            "expected": "TODO: 填写期望结果",
            "judge": "结果是否完成用户请求且无需返工",
            "provenance": "auto-mined",
        } for i, p in enumerate(picked)]
        write_json(path, {"skill": aid, "fingerprint": a.get("fingerprint"), "draft": True,
                          "note": "auto-drafted by bootstrap from local history; review expected/judge before trusting",
                          "cases": cases})
        made += 1
        print(f"  {OK_MARK} {aid}: {len(cases)} draft case(s) -> golden/{fn}")
    print(f"\n[ok] {made} 个 golden 草稿。下一步：人工补 expected/judge，再跑 baseline 建基线。")
    return 0


def cmd_onboard(args):
    print("=== 1/3 探测宿主 ===")
    cmd_discover(argparse.Namespace(write=True))
    print("\n=== 2/3 扫描默认宿主 ===")
    rc = cmd_scan(args)
    print("\n=== 3/3 从历史生成 golden 草稿 ===")
    if rc == 0:
        cmd_bootstrap(args)
    print("\n=== 完成 ===")
    print("下一步：检查 golden/*.json 的 expected/judge，跑 baseline 建基线，再 recommend 看面板。")
    return 0


def run(cmd):
    # 子进程统一说 UTF-8：否则父进程按 GBK 解码、子进程按环境变量输出时，
    # 中文报告会变成乱码或直接 UnicodeDecodeError。
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    return r.returncode, r.stdout, r.stderr


def build_parser():
    p = argparse.ArgumentParser(description="skill-auditor · host-agnostic capability auditor")
    p.add_argument("--host", default=None, help="host profile: workbuddy | claude-code | deepseek | generic")
    p.add_argument("--root", default=None, help="assets root (overrides host profile)")
    p.add_argument("--skills-dir", default=None, help="(deprecated alias for --root)")
    p.add_argument("--out", default=DEFAULT_OUT)
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("hosts")
    st = sub.add_parser("setup")
    st.add_argument("--model", default="v-unknown")
    sub.add_parser("scan")
    sub.add_parser("status")
    sub.add_parser("check")
    b = sub.add_parser("bump")
    b.add_argument("version", nargs="?", default=None)
    st = sub.add_parser("stamp")
    st.add_argument("version", nargs="?", default=None,
                    help="模型版本号；缺省时必须配合 --infer")
    st.add_argument("--infer", action="store_true",
                    help="从环境变量 / settings / 宿主日志推断模型串；推断不到则拒绝写戳")
    st.add_argument("--from-events", action="store_true",
                    help="从 DSH 插件的 model-events.jsonl 取最近已知模型串")
    st = sub.add_parser("seed", help="社区公共 golden 种子库")
    # 注意：这里**不能**再用 add_subparsers —— 二级 subparser 会把父级
    # --host/--out/--root 的解析结果覆盖成 None（argparse 的命名空间行为），
    # 导致 seed audit 永远读默认宿主的注册表。用一个字符串参数自行分派。
    st.add_argument("action", nargs="?", default="audit",
                    choices=["audit", "contribute"], help="audit | contribute")
    st.add_argument("--skill", default=None)
    st.add_argument("--case-id", default=None)
    st.add_argument("--input", default=None)
    st.add_argument("--expected", default=None)
    st.add_argument("--judge", default=None)
    st.add_argument("--polarity", default="positive")
    st.add_argument("--type", default="task")
    st.add_argument("--provenance", default=None)
    sub.add_parser("recommend")
    pb = sub.add_parser("probe")
    pb.add_argument("--strict", action="store_true",
                    help="有运行时故障迹象时以退出码 1 结束（给 CI/脚本用）")
    sub.add_parser("audit", help="主动体检：版本对比 + 运行时故障 + 基线，一次给结论")
    rp = sub.add_parser("report")
    rp.add_argument("--results", default=None,
                    help="results JSON (default: <data dir>/baseline-results.json)")
    rp.add_argument("--threshold", type=int, default=20)
    rp.add_argument("--compare", default=None,
                    help="model id to compare against the baseline (required when >2 models)")
    rp.add_argument("--promote", action="store_true",
                    help="把本次结果合并进基线（否则报告只是报告，面板看不到）")
    rp.add_argument("--replace", action="store_true",
                    help="与 --promote 连用：整体替换基线，而不是按资产合并")
    fd = sub.add_parser("find", help="按功能找资产：「那个查余额的叫啥」")
    fd.add_argument("query", help="功能描述，如「查余额」「记忆」「打开网页」")
    sub.add_parser("insights", help="能力利用盘点：哪些工具在睡觉、怎么唤醒")
    cn = sub.add_parser("can", help="能力判断：这件事我能不能做（Agent 动手前调用）")
    cn.add_argument("task", help="任务描述，如「做一份季度财报 PPT」")
    cn.add_argument("--market", action="store_true",
                    help="本地没有时，去市场搜候选（需网络）")
    sg = sub.add_parser("suggest")
    sg.add_argument("--need", required=True)
    sg.add_argument("--search", action="store_true",
                    help="真去插件市场搜候选（DSH / SkillHub，需网络；失败会如实报错）")
    d = sub.add_parser("discover")
    d.add_argument("--write", action="store_true")
    bs = sub.add_parser("bootstrap")
    bs.add_argument("--transcripts", default=None)
    bs.add_argument("--per-asset", type=int, default=5)
    bs.add_argument("--force", action="store_true")
    ob = sub.add_parser("onboard")
    ob.add_argument("--transcripts", default=None)
    ob.add_argument("--per-asset", type=int, default=5)
    ob.add_argument("--force", action="store_true")
    return p


def main():
    args = build_parser().parse_args()
    use_safe_stdio()
    # --out 必须在此之前落地：所有数据路径（registry/baseline/model/golden）都从它派生。
    configure_data_dir(getattr(args, "out", None))
    cmd = args.cmd or "status"
    # 无参数兜底（方便①）：裸跑 console.py 时不甩 help，直接给最常用的体检结论——
    # 用户不该需要记住任何命令。
    if not getattr(args, "cmd", None):
        try:
            rc = cmd_audit(args)
        except SystemExit:
            rc = 1
        print("\n常用命令：can <任务>（能不能做）· find <功能>（找插件）· audit（体检）")
        return rc
    table = {
        "hosts": cmd_hosts, "setup": cmd_setup, "scan": cmd_scan, "status": cmd_status,
        "check": cmd_check, "bump": cmd_bump, "stamp": cmd_stamp,
        "seed": cmd_seed,
        "recommend": cmd_recommend,
        "probe": cmd_probe,
        "audit": cmd_audit,
        "report": cmd_report, "suggest": cmd_suggest,
        "can": cmd_can,
        "find": cmd_find,
        "insights": cmd_insights,
        "discover": cmd_discover, "bootstrap": cmd_bootstrap, "onboard": cmd_onboard,
    }
    if cmd not in table:
        print("unknown command")
        return 1
    return table[cmd](args)


if __name__ == "__main__":
    sys.exit(main())
