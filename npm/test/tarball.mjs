/**
 * tarball.mjs — 「别人装了这个包」的端到端验证（不进 npm 包）
 *
 * 关键点：仓库里能跑 ≠ 装进去能跑。这个脚本走一遍真实路径：
 *   npm pack → 解到临时目录 → npm install <tgz> → import 安装后的包
 *   → 调一个工具 → 断言引擎随包落地、输出正常
 *
 * prepack 只在**发布方**跑；消费者拿到的是 tarball 里已有的 engine/。
 * 所以这里断言的是 tarball 内容，不是工作区内容。
 *
 * 用法：node test/tarball.mjs
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const NPM_ROOT = path.resolve(HERE, '..')

/**
 * Node 22 在 Windows 上禁止直接 spawn `npm.cmd`（CVE-2024-27980 的 EINVAL）。
 * 官方发行版自带 npm-cli.js，用 node 直接跑它 —— 不经过 shell，路径带空格也不用转义。
 */
const NPM_CLI = path.join(path.dirname(process.execPath), 'node_modules', 'npm', 'bin', 'npm-cli.js')
function runNpm(args, cwd) {
  const opts = { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }
  return fs.existsSync(NPM_CLI)
    ? execFileSync(process.execPath, [NPM_CLI, ...args], opts)
    : execFileSync(process.platform === 'win32' ? 'npm.cmd' : 'npm', args, { ...opts, shell: true })
}

const failures = []
const ok = (cond, label, detail) => {
  console.log(`  ${cond ? '✓' : '✗'} ${label}${!cond && detail ? `\n      ${String(detail).slice(0, 400)}` : ''}`)
  if (!cond) failures.push(label)
}

const work = fs.mkdtempSync(path.join(os.tmpdir(), 'skill-auditor-tar-'))

try {
  console.log('\n[1] npm pack')
  const packOut = runNpm(['pack', '--json'], NPM_ROOT)
  // prepack（sync-engine）会往 stdout 打日志，JSON 数组是最后一段；用换行+`[` 定位起点
  const start = Math.max(packOut.lastIndexOf('\n[\n'), packOut.lastIndexOf('\n['))
  if (start < 0) throw new Error('npm pack --json 没输出 JSON：\n' + packOut.slice(0, 500))
  const info = JSON.parse(packOut.slice(start + 1))[0]
  const tgz = path.join(NPM_ROOT, info.filename)
  ok(fs.existsSync(tgz), `产出 ${info.filename}`, tgz)
  ok(info.version === JSON.parse(fs.readFileSync(path.join(NPM_ROOT, 'package.json'), 'utf8')).version,
    `版本一致 ${info.version}`)

  const paths = info.files.map((f) => f.path)
  for (const must of [
    'lib/index.js',
    'lib/engine.mjs',
    'engine/scripts/console.py',
    'engine/scripts/regress.py',
    'engine/golden/_template.json',
    'cordis.patch.yml',
    'plugin.json',
    'skill/SKILL.md',
    'LICENSE',
  ]) {
    ok(paths.includes(must), `tarball 含 ${must}`)
  }
  ok(!paths.some((p) => /^engine\/registry\.|^engine\/baseline-results|^engine\/model-version/.test(p)),
    'tarball 不含用户数据')
  ok(!paths.some((p) => p.startsWith('test/') || p.startsWith('scripts/')), 'tarball 不含构建脚本与测试')

  const yml = fs.readFileSync(path.join(NPM_ROOT, 'cordis.patch.yml'), 'utf8')
  ok(/dsh-skill-auditor/.test(yml), 'cordis.patch.yml 指向包名 dsh-skill-auditor')
  const pkg = JSON.parse(fs.readFileSync(path.join(NPM_ROOT, 'package.json'), 'utf8'))
  ok(pkg.dsh?.bundle?.patch === './cordis.patch.yml', 'package.json 声明 dsh.bundle.patch')
  ok(/^\d+\.\d+\.\d+$/.test(pkg.version), `版本是稳定版（无 prerelease）：${pkg.version}`)

  console.log('\n[2] npm install <tgz>（模拟消费者）')
  fs.writeFileSync(path.join(work, 'package.json'), JSON.stringify({ name: 'consumer', private: true }))
  runNpm(['install', tgz, '--no-audit', '--no-fund'], work)
  const installed = path.join(work, 'node_modules', 'dsh-skill-auditor')
  ok(fs.existsSync(path.join(installed, 'lib', 'index.js')), '安装后 lib/index.js 存在')
  ok(fs.existsSync(path.join(installed, 'engine', 'scripts', 'console.py')), '安装后引擎随包落地')

  console.log('\n[3] 从安装位置真跑')
  process.env.DSH_HOME = path.join(work, 'dsh-home')
  // 【CI 回归修复】status 默认跑 --host deepseek，该宿主 root 是
  // ~/.dsh/profiles/desktop —— CI 上为空，scan 扫不到资产就 exit 1，
  // 「首次调用自动建了注册表」必然失败。这里造一个假 profile 指过去，
  // 与 smoke.mjs 同一套做法，测试不再依赖真实环境。
  const fakeProfile = path.join(work, 'fake-profile')
  fs.mkdirSync(path.join(fakeProfile, 'node_modules', 'demo-plugin'), { recursive: true })
  fs.writeFileSync(path.join(fakeProfile, 'package.json'), JSON.stringify({
    name: 'fake-profile', private: true,
    dependencies: { 'demo-plugin': '1.0.0' },
    dsh: { profile: { bundles: ['demo-plugin'] } },
  }, null, 2))
  fs.writeFileSync(path.join(fakeProfile, 'node_modules', 'demo-plugin', 'package.json'),
    JSON.stringify({
      name: 'demo-plugin', version: '1.0.0',
      description: 'tarball-test fixture plugin',
      dsh: { bundle: { patch: './cordis.patch.yml' } },
    }, null, 2))
  const tarDataDir = path.join(work, 'dsh-home', 'skill-auditor')
  fs.mkdirSync(tarDataDir, { recursive: true })
  fs.writeFileSync(path.join(tarDataDir, 'hosts.json'), JSON.stringify({
    default_host: 'deepseek',
    hosts: {
      deepseek: { kind: 'plugin', label: '插件', root: fakeProfile, discover: 'deps',
                  manifests: ['package.json', 'plugin.json'], logs: [] },
    },
  }, null, 2))
  const entry = new URL('file://' + path.join(installed, 'lib', 'index.js').replace(/\\/g, '/')).href
  const mod = await import(entry)
  const reg = []
  const ctx = {
    logger: { warn: (m) => console.log('  [warn] ' + m) },
    inject: (deps, cb) => cb(ctx),
    tools: { register: (t) => reg.push(t) },
  }
  mod.apply(ctx)
  ok(reg.length === 9, `从安装位置注册 9 个工具（实际 ${reg.length}）`)

  const status = reg.find((t) => t.name === 'skill_audit_status')
  const out = await status.execute({ host: 'deepseek' }, {})
  ok(/能力折旧表|插件/.test(out), '安装后的引擎能出折旧表', out)
  ok(!/Traceback|ENOENT|Cannot find module/.test(out), '安装后无缺文件/缺解释器报错', out)

  const dataDir = path.join(work, 'dsh-home', 'skill-auditor')
  ok(fs.existsSync(path.join(dataDir, 'scripts', 'console.py')), '引擎复制到了 DSH_HOME')
  ok(fs.existsSync(path.join(dataDir, 'registry.deepseek.json')), '首次调用自动建了注册表')
} catch (err) {
  console.error('\n[FAIL] 异常：', err && err.message ? err.message : err)
  failures.push('exception')
} finally {
  try {
    const stray = fs.readdirSync(NPM_ROOT).filter((f) => f.endsWith('.tgz'))
    for (const f of stray) fs.rmSync(path.join(NPM_ROOT, f), { force: true })
  } catch {}
  fs.rmSync(work, { recursive: true, force: true })
}

console.log('')
if (failures.length) {
  console.log(`[FAIL] ${failures.length} 项未通过：\n  - ${failures.join('\n  - ')}`)
  process.exit(1)
}
console.log('[PASS] 打包 → 安装 → 运行 全链路通过')
