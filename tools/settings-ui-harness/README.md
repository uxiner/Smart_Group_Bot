# 设置中心 UI 本地预览 harness

**这是开发工具，不是发布页面的一部分。**

- 生产镜像只 `COPY bot` 和 `COPY prompt`（见 `Dockerfile`），`tools/` 不会进镜像。
- `bot/web/static/` 里没有任何测试模式、演示模式或绕过鉴权的入口。
- 进程只绑定 `127.0.0.1`；传入非 loopback 地址会直接拒绝启动。
- 只读 `fixtures/settings.json`（合成数据），不读 `.env`、不连生产数据库、不请求 Telegram。
- `/settings-assets/*` 直接从 `bot/web/static/` 取真实文件，页面结构由真实的
  `bot/web/static/index.html` 生成（仅把 `telegram.org` 的 SDK 换成 loopback 桩，
  避免产生任何外网请求）。
- `tests/test_settings_night_crystal.py::ReleaseHygieneTests` 会持续检查上面这几条。

## 启动

```bash
python3 tools/settings-ui-harness/harness.py --port 8781
# 打开 http://127.0.0.1:8781/settings
```

可用开关：

| 开关 | 作用 |
| --- | --- |
| `--group-admin` | 以普通群管理员身份进入（`can_manage_global = false`），验证越权时页面只剩群组页 |
| `--fail-save` | 所有写操作返回 503，用于验证保存失败与草稿保留 |
| `--no-groups` | 群组列表返回 503，用于验证错误态 |

另外有两个只用于自动化的端点：`GET /harness/requests`（请求日志）和
`POST /harness/reset`（把内存状态复位回 fixture）。

Telegram 桩按官方文档（Bot API 8.0+）的真实字段形状实现：
`safeAreaInset` / `contentSafeAreaInset` 是 `{top, bottom, left, right}` 对象，
`viewportHeight` / `viewportStableHeight` 是数字；`__setInsets` / `__setViewport`
是给 `checks/safe-area.mjs` 用的驱动口（桩在 `tools/` 下，不随发布页出去）。

## 浏览器验收脚本（需要 Playwright + 本机 Chrome）

脚本从 `playwright` 解析该包，所以要在装过它的目录里跑（仓库里没有 `node_modules`，
`node_modules/` 也在 `.gitignore` 里）：

```bash
# 任意目录
mkdir -p /tmp/ui-verify && cd /tmp/ui-verify
npm init -y && npm i playwright        # channel: "chrome"，不会额外下载浏览器
CHECKS=<repo>/tools/settings-ui-harness/checks
```

```bash
# 终端 1：起 harness（四个实例各测一种形态）
cd <repo>
python3 tools/settings-ui-harness/harness.py --port 8781 &
python3 tools/settings-ui-harness/harness.py --port 8792 --fail-save &
python3 tools/settings-ui-harness/harness.py --port 8793 --group-admin &
python3 tools/settings-ui-harness/harness.py --port 8794 --no-groups &

# 终端 2
cd /tmp/ui-verify

# 1) 布局：360 / 390 / 430 / 1280 / 1680 × 全部页面 + 展开群卡片/分组/高级面板
node $CHECKS/verify.mjs

# 2) 交互：草稿、保存成功/失败、折叠态、对话框焦点与 ESC、抽屉焦点陷阱
node $CHECKS/interact.mjs                                   # 26 项
FAIL_HARNESS=http://127.0.0.1:8792 node $CHECKS/interact.mjs # 29 项（多 3 条保存失败路径）

# 3) Telegram 安全区：真实 SDK 形状 + 发事件 + 读计算样式
node $CHECKS/safe-area.mjs

# 4) 文案真伪：热生效承诺、方向指代、原文保留策略
node $CHECKS/copy-truth.mjs

# 5) 对比度 + 角色可见性 + 错误态
node $CHECKS/roles.mjs
GROUP_HARNESS=http://127.0.0.1:8793 node $CHECKS/roles.mjs
ERROR_HARNESS=http://127.0.0.1:8794 node $CHECKS/roles.mjs

# 6) 截图像素抽样（确认深色主题真的渲染出来了）
SHOTS=/tmp/dsh-ui-shots node $CHECKS/verify.mjs   # verify.mjs 会顺便截图
node $CHECKS/pixels.mjs /tmp/dsh-ui-shots/*.png

# 7) 保存按钮点击回归（改字段 → 真鼠标点击保存）
node $CHECKS/save-click-probe.mjs 8781
```

`safe-area.mjs` 用桩上的 `__setInsets(safe, content)` / `__setViewport(height, stable)`
驱动**官方真实形状**（`safeAreaInset` / `contentSafeAreaInset` 是
`{top,bottom,left,right}` 对象，`viewportHeight` / `viewportStableHeight` 是数字），
发 `safeAreaChanged` / `contentSafeAreaChanged` / `fullscreenChanged` / `viewportChanged`
事件，然后同时读两处：

- `inline`：`app.js` 实际写进 `--tg-safe-*` 的 px 字符串（逐边取 max 的结果）；
- `probe`：一个 `padding: var(--safe-top) var(--safe-right) …` 的真实元素，
  读回解析后的 px（`getPropertyValue()` 对未注册的自定义属性只会返回未求值的
  `max(…)` 字符串，所以必须走真实元素）。

再逐项验证：非对称 inset 生效、四个消费者（topbar / content 左右 / toast 底部）
真的吃到了值、inset 归零不留残留、viewport 高度跟随、无效高度回落而不塌陷、
旧 SDK（无这些字段）冷启动与热事件都不报错。

`verify.mjs` 会把截图写到 `/tmp/dsh-ui-shots`（可用 `SHOTS=` 改），并把逐条
检查结果写进 `$SHOTS/report.json`（仓库里提交的那份在 `docs/ui-night-crystal/layout-check-report.json`）。

## 重新生成 fixture

`fixtures/settings.json` 已经提交，harness 只用标准库即可运行。
只有在你改了后端默认配置、想刷新基线时才需要重新生成：

```bash
# 需要一个装了项目依赖的 Python 3.12 环境
LITELLM_MODE=PRODUCTION python3 tools/settings-ui-harness/make_fixture.py
```

生成器用项目自己的默认 `RuntimeConfig` 取运行时配置（不加载 `.env`、不连数据库），
群组、名单、群规等行全部是脚本里编出来的合成数据。
