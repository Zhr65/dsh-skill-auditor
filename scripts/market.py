#!/usr/bin/env python3
"""market.py — 从可现实访问的插件/skill 市场找候选。

为什么单独一个文件：这是唯一会碰网络的部分，和"本地判定"职责不同；
拆开也让 console.py 不再膨胀（它已经 1750+ 行）。

数据源实测（2026-10-19，本机）：
  · DSH 插件目录  https://deepseek1024.com/api/v2/plugins   ✅ 通（13810 条）
  · SkillHub      https://lightmake.site/api/v1/search      ✅ 通（中文可搜）
  · GitHub API                                              ❌ 超时（需加速器）
  · ghfast 等镜像                                            ❌ 不通

**关于 stars 的重要口径**：DSH 目录里的 stars 是**上游源码仓库**的，
不是插件本身的热度（实测：dsh-runtime 101755 stars，自身 installCount 仅 73）。
所以排序**不用 star**，用 installCount + growth7d + 最近提交时间。
输出里也必须显式标注这一点，否则会误导用户。

失败时一律如实报错，不返回空列表假装"没有结果"。
"""
import datetime
import json
import os
import sys
import urllib.parse
import urllib.request

def _ua():
    """User-Agent 从仓库 _meta.json 读版本（单一真源）。
    审查 R3：原来这段用了 os 却没 import os，每次必抛 NameError 被
    except 吞掉，静默兜底成硬编码 0.9.0 —— 正是本项目反对的假绿灯。"""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        meta_path = os.path.join(os.path.dirname(here), "_meta.json")
        with open(meta_path, encoding="utf-8-sig") as fh:
            meta = json.load(fh)
        return "skill-auditor/" + str(meta.get("version") or "unknown")
    except Exception:
        return "skill-auditor/unknown"

UA = {'User-Agent': _ua()}
DSH_MARKET = "https://deepseek1024.com/api/v2/plugins"
SKILLHUB_SEARCH = "https://lightmake.site/api/v1/search?q=%s&limit=%d"
TIMEOUT = 20


def _fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _text(v):
    if isinstance(v, dict):
        return v.get("zh") or v.get("en") or ""
    return str(v or "")


def _days_since(iso):
    if not iso:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        return max(0, (now - dt).days)
    except Exception:
        return None


def score(entry):
    """复合分：真实安装量 + 近期增长 + 活跃度。**不含 star**。"""
    installs = _int(entry.get("installCount") if "installCount" in entry else entry.get("installs"))
    growth = _int(entry.get("growth7d"))
    days = _days_since(entry.get("pushedAt") or entry.get("updatedAt") or entry.get("updated_at"))
    fresh = 0.0 if days is None else max(0.0, (30 - days) / 30.0)
    return min(installs, 500) / 500.0 * 2.0 + min(growth, 2000) / 2000.0 + fresh * 1.5


def search_dsh(term, limit=8, scan=300):
    """搜 DSH 插件目录。返回 (meta, hits)。

    审查 P1 修正：原来只拉前 200/300 条（市场共 13810），被审计插件
    多半不在前 200，find_alternatives 的"同类"判定基于 0.7% 样本。
    现在优先用服务端 q= 定向搜索（实测支持），miss 时分页兜底扫全量。
    """
    t = (term or "").lower().strip()
    if not t:
        data = _fetch(DSH_MARKET + "?limit=%d" % scan)
        return {"total": data.get("total"), "generatedAt": data.get("generatedAt"),
                "source": data.get("source"), "scanned": len(data.get("plugins") or [])}, []

    items = []
    scanned = 0
    server_err = None
    meta_src = {}   # 最后一次成功响应的元数据（total/generatedAt/source）
    # ① 服务端定向搜索（快、全）
    try:
        data = _fetch(DSH_MARKET + "?q=%s&limit=%d" % (urllib.parse.quote(t), 100))
        items = data.get("plugins") or []
        scanned = len(items)
        meta_src = {"total": data.get("total"), "generatedAt": data.get("generatedAt"),
                    "source": data.get("source")}
    except Exception as e:
        # 诚实边界：服务端搜索失败必须保留错误信息，不能吞掉假装"没结果"
        server_err = str(e)[:80]
    # ② 服务端没给的，本地分页兜底（最多 5 页 × 200 = 1000 条，够覆盖头部）
    if len(items) < limit:
        seen = {p.get("name") for p in items}
        for page in range(1, 6):
            try:
                data = _fetch(DSH_MARKET + "?limit=200&page=%d" % page)
            except Exception:
                break
            batch = data.get("plugins") or []
            if not batch:
                break
            scanned += len(batch)
            meta_src = {"total": data.get("total"), "generatedAt": data.get("generatedAt"),
                        "source": data.get("source")}
            for p in batch:
                n = p.get("name")
                if n not in seen and (t in (n or "").lower()
                                      or t in _text(p.get("description")).lower()
                                      or t in (p.get("category") or "").lower()):
                    items.append(p)
                    seen.add(n)
            if len(items) >= limit * 3:
                break
    meta = {"total": meta_src.get("total"),
            "generatedAt": meta_src.get("generatedAt"),
            "source": meta_src.get("source"),
            "scanned": scanned,
            "server_error": server_err}
    # 全部通道失败（服务端 + 分页兜底都没拿到）→ 必须如实报，不假装"没结果"
    if not items and server_err and scanned == 0:
        return meta, []
    if not items:
        return meta, []
    hits = [p for p in items
            if t in (p.get("name") or "").lower()
            or t in _text(p.get("description")).lower()
            or t in (p.get("category") or "").lower()]
    return meta, sorted(hits, key=score, reverse=True)[:limit]


def search_skillhub(term, limit=8):
    """搜 WorkBuddy SkillHub。返回列表（可能为空）。"""
    url = SKILLHUB_SEARCH % (urllib.parse.quote(term or ""), limit)
    data = _fetch(url)
    res = data.get("results") if isinstance(data, dict) else data
    return sorted(res or [], key=score, reverse=True)[:limit]


def render_dsh(term, limit=6, out=print):
    try:
        meta, hits = search_dsh(term, limit)
    except Exception as e:
        out("  [err] DSH 市场不可达：%s" % e)
        out("        （本机需要代理才能访问部分外网；失败时如实报，不编造候选）")
        return 0
    stale = "  ⚠️ 非实时快照" if meta.get("source") == "stale" else ""
    out("  DSH 插件市场：目录 %s 条，已扫 %s 条，数据生成于 %s%s"
        % (meta.get("total"), meta.get("scanned"),
           (meta.get("generatedAt") or "")[:19], stale))
    # 诚实边界：服务端搜索失败要如实说（审查教训：吞掉错误假装"没结果"是假绿灯）
    if meta.get("server_error") and not meta.get("scanned"):
        out("  [err] DSH 市场不可达：%s" % meta["server_error"])
        out("        （本机需要代理才能访问部分外网；失败时如实报，不编造候选）")
        return 0
    if not hits:
        out("  没有命中（换更短的关键词，或试 SkillHub）")
        return 0
    for p in hits:
        days = _days_since(p.get("pushedAt"))
        out("\n  ▸ %s  [%s]" % (p.get("name"), p.get("category") or "-"))
        out("    %s" % _text(p.get("description"))[:120])
        out("    安装=%s  7日增长=%s  最后提交=%s"
            % (p.get("installCount"), p.get("growth7d"),
               "%s天前" % days if days is not None else "?"))
        out("    ⓘ 上游项目 stars=%s（源码仓库热度，不是本插件安装量）" % p.get("stars"))
        if p.get("install"):
            out("    安装命令：%s" % p["install"])
    return len(hits)


def render_skillhub(term, limit=6, out=print):
    try:
        hits = search_skillhub(term, limit)
    except Exception as e:
        out("  [err] SkillHub 不可达：%s" % e)
        return 0
    if not hits:
        out("  SkillHub 没有命中")
        return 0
    for it in hits:
        dz = it.get("description_zh")
        if isinstance(dz, str) and dz.strip().startswith("{"):
            try:
                dz = json.loads(dz).get("answer", "")
            except Exception:
                pass
        out("\n  ▸ %s  [%s]" % (it.get("slug") or it.get("name"), it.get("category") or "-"))
        out("    %s" % str(dz or it.get("description") or "")[:120])
        out("    downloads=%s  installs=%s  stars=%s  版本=%s"
            % (it.get("downloads"), it.get("installs"), it.get("stars"), it.get("version")))
        if it.get("homepage"):
            out("    页面：%s" % it["homepage"])
    return len(hits)


HOW_TO_READ = [
    "怎么读这些数字（别只看 star）：",
    "  · 安装量   = 真正装过它的人，最硬",
    "  · 7日增长 = 近期热度，涨得快的常是 demo/教程",
    "  · 最后提交 = 还活着吗（半年没动的慎装）",
    "  · stars    = ⚠️ DSH 目录标的是上游源码仓库热度，不是插件热度",
    "  真正该看的：装进来 → scan → 建 golden → 跑基线，用数据比",
]


def main(argv):
    # 【CI 回归修复】Windows 控制台默认 GBK，输出中文/符号会 UnicodeEncodeError。
    # 主动重配 stdout/stderr 为 UTF-8（errors=replace 兜底），不依赖调用方设环境变量。
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if not argv:
        print("usage: market.py <关键词> [--market dsh|skillhub|both] [--limit N]")
        return 2
    term = argv[0]
    market = argv[argv.index("--market") + 1] if "--market" in argv else "both"
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else 6
    if market in ("dsh", "both"):
        print("\n=== DSH 插件市场 · 「%s」 ===" % term)
        render_dsh(term, limit)
    if market in ("skillhub", "both"):
        print("\n=== SkillHub（WorkBuddy skill）· 「%s」 ===" % term)
        render_skillhub(term, limit)
    print()
    for line in HOW_TO_READ:
        print(line)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
