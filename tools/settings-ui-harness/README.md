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

# 3) 对比度 + 角色可见性 + 错误态
node $CHECKS/roles.mjs
GROUP_HARNESS=http://127.0.0.1:8793 node $CHECKS/roles.mjs
ERROR_HARNESS=http://127.0.0.1:8794 node $CHECKS/roles.mjs

# 4) 截图像素抽样（确认深色主题真的渲染出来了）
SHOTS=/tmp/dsh-ui-shots node $CHECKS/verify.mjs   # verify.mjs 会顺便截图
node $CHECKS/pixels.mjs /tmp/dsh-ui-shots/*.png

# 5) 保存按钮点击回归（改字段 → 真鼠标点击保存）
node $CHECKS/save-click-probe.mjs 8781
```

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
