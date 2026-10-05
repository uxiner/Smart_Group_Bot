# 设置中心 UI 重设计交付说明 — 夜蓝 × 冰青 × 薰衣草

- 分支：`feat/settings-night-crystal`
- 派单基线：`0094862e22ca0630fa8cbf846fc8e1d776d588ae`
- 范围：只动 `bot/web/static/{index.html,styles.css,app.js}`，新增 `tests/test_settings_night_crystal.py`、
  `tools/settings-ui-harness/`（本地预览 harness）、`docs/ui-night-crystal/`（真实截图与布局检查报告）。
- **没有**改后端 API、数据库、审核/回复/记忆逻辑、生产 config 或密钥。
- 没有 push，没有 SSH VPS，没有读取 `.env` 或生产数据库。

---

## 1. 设计 tokens

参考图只用来取色系与气质（深夜底、白、薰衣草紫 + 冰青蓝发色）。没有把人物贴进后台，
没有加粒子，没有霓虹光效。

### 平面层次（只有四层，靠平面和留白建立质感）

| token | 值 | 用途 |
| --- | --- | --- |
| `--bg` | `#0C1220` | 页面底 |
| `--surface-sidebar` | `#0F1726` | 侧栏 / 移动端横向导航条 |
| `--surface` | `#151F31` | 分组卡片、条目卡、表格 |
| `--surface-raised` | `#1B2940` | 输入框、下拉、内嵌面板、徽标 |
| `--surface-muted` | `#111A2A` | 井（折叠面板、空态、权限编辑器底） |
| `--surface-hover` | `#1F2E47` | hover 平面 |
| `--surface-active` | `#263954` | 开关关闭轨、轨道底 |

### 文字与边界

| token | 值 | 对 `--surface-raised` 实测对比度 |
| --- | --- | --- |
| `--text` | `#EEF3FC` | 13.12:1 |
| `--text-muted` | `#A8B5CC` | 7.98:1 |
| `--text-faint` | `#8795AD` | 4.82:1（原先的 `#7E8DA6` 只有 4.35:1，已上调） |
| `--border` | `#2A3A52` | — |
| `--border-subtle` | `#202C41` | 卡片内分隔线，比主边界更弱 |
| `--border-strong` | `#3B4E6C` | 控件 hover 边界 |

### 点缀

| token | 值 | 用途 |
| --- | --- | --- |
| `--primary` | `#71D9EF` | 冰青：主按钮、保存、开关打开态、焦点环 |
| `--primary-ink` | `#062430` | 冰青按钮上的深色文字（对 `--primary` 9.88:1） |
| `--accent-violet` | `#B8A4F4` | 淡薰衣草紫：**选中分类**（导航、Prompt 列表、群分组、折叠面板图标） |

语义色保持克制、彼此可辨，且**危险按钮永远不会伪装成主按钮**：
`--danger #F08C8C` + `--danger-ink #360F12`，`--warning #F0B662`，
`--success #62D2A6`，`--info #9DBCF2`。`.danger-button` 是实心红，
`.primary-button` 是冰青；`tests/test_settings_night_crystal.py` 会持续断言两者不共用颜色。

### 尺度

- 间距刻度：`--space-1..6` = 4 / 8 / 12 / 16 / 22 / 30px
- 圆角：`--radius-sm 8px` / `--radius 12px` / `--radius-lg 16px`
- 控件高度：`--control-height 44px`（所有可点区域统一，见 §4）
- 内容宽度：`--content-max 1180px`，`.content { width: min(var(--content-max), 100%); margin: 0 auto; }`
  → 1280px 以上不会无限横向拉长；≥1560px 时侧栏加宽到 264px 而不是拉伸正文。
- 字体：`-apple-system / BlinkMacSystemFont / Segoe UI / PingFang SC / …` 系统字体栈，
  **没有** `@font-face`、`@import`、CDN 字体或外链（`font-src 'self'` 不变）。
- 质感：只用一条 `inset 0 1px 0 rgb(255 255 255 / 4%)` 的发丝高光和极低透明投影，
  没有大面积渐变、没有满屏光效。

---

## 2. 改动清单

### `bot/web/static/index.html`（8 行）

- `<meta name="color-scheme" content="dark">`（原 `light dark`）
- `<meta name="theme-color" content="#0C1220">`（原 `#f5f6f4`，Telegram 顶栏会跟着变色）
- 资源版本号 `20260903-unified-save-layout-per-model-request-params`
  → `20261005-night-crystal-unified-save-layout`（JS 与 CSS 同步，保留 `unified-save-layout` 子串）
- **CSP 逐字未改**，`viewport-fit=cover` 保留。

### `bot/web/static/styles.css`（53.7KB → 71.0KB，全量重写）

- `:root` 直接是深色，`color-scheme: dark`；**删除了** `@media (prefers-color-scheme: dark)`
  整块（旧结构是浅色 `:root` + 暗色覆盖，正是"半深半白"的来源），也删除了 `:root` 里
  指向未加载 `Inter` 的字体名。
- 层级重建：`title → 分类 → 分组 → 字段`，统一了 `h2/h3/h4`、字段网格
  （`.field-grid` 2 列 / `.three` 3 列 / `.single` 1 列）、控件高度、徽标、按钮高度。
- 加载 / 无权限 / 出错 / 空列表 / 保存中 / 已修改：共用同一套
  `.loading-state .empty-state .error-state .spinner .badge .save-state .group-save-state` 语言。
- 新增 `.advanced-panel`（分组级渐进披露）与 `.field-help`（字段级 `<details>`）组件。
- 滚动条改成暗色（`scrollbar-color` + `::-webkit-scrollbar`），避免系统控件露出白底。
- 焦点环从 `rgb(37 103 155 / 22%)`（几乎看不见）改成实线 `2px solid var(--focus-ring)`。
- `prefers-reduced-motion` 里额外停掉 spinner 旋转。
- 新增 `@media (forced-colors: active)` 兜底。

### `bot/web/static/app.js`（+315 / -约 210 行）

结构与业务逻辑**没有**改动接线，字段路径一个没动。新增/修改：

1. `syncTelegramSafeArea()`：把 `Telegram.WebApp.safeAreaInset / contentSafeAreaInset / viewport`
   写进 `--tg-safe-*` CSS 变量（`safeAreaChanged` / `contentSafeAreaChanged` / `viewportChanged` 订阅），
   CSS 侧 `env(safe-area-inset-*)` 与它取 `max()`。没有新增任何网络请求。
2. `helpDisclosure(help, label, open)`：`field()` 新增 `help` / `helpLabel` / `helpOpen` 选项；
   `toggle()` 增加第 5 个可选参数 `help`；`secretField()` 增加第 4 个 `help`。
   三者原签名保持兼容，所有既有调用点不变。
3. `advancedPanel(id, title, description, body, {open})` + `state.advancedOpen: new Map()`
   + `captureAdvancedDisclosureStates()`（在 `renderContent()` 和 `loadAll()` 重渲染前调用）
   + `content.addEventListener("toggle", …, true)`（捕获阶段，不依赖 `toggle` 是否冒泡）。
4. `revealAdvancedDisclosure(control)` / `revealFieldByPath(path)`：校验失败时逐级打开
   折叠层再 `reportValidity()` / 滚动过去，避免"报错指向一个从没打开过的面板"。
5. `captureContentScrollAnchor()` 的候选锚点加入 `[data-advanced-panel]`。
6. 文案重写（见 §3）。
7. **`updateChrome()` 的一处行为修复**（见 §6 blocker）。

### 渐进披露落点（8 个高级面板，默认收起，一键展开）

| 面板 id | 位置 | 内容 |
| --- | --- | --- |
| `bot.streaming` | Bot 行为 | 流式首段字符数、编辑间隔 |
| `bot.summary` | Bot 行为 | 15 个群摘要参数 + 开关 |
| `bot.context-budget` | Bot 行为 | 上下文预算 / 模型窗口上限 / 输出上限 |
| `bot.history-budget` | Bot 行为 | 群历史条数与 Token 预算、私聊预算、检索留档新鲜窗口 |
| `bot.memory-tuning` | Bot 行为 | 长期记忆的 8 个节奏与额度参数 |
| `tts.audio` | 媒体能力 | 采样率 / 比特率 / 情感 / 语速 / 音量 / 尾部静音 |
| `av.sources` | 媒体能力 | 4 个数据源地址 |
| `movie_info.imdb` | 外部服务 | IMDb Data Set / Revision / Asset + 4 个密钥 |

**所有字段仍然可达**：`tests/test_settings_night_crystal.py` 逐个断言这些 `field()` /
`toggle()` / `secretField()` 的路径仍在源码里；浏览器验收也逐个打开后检查无溢出。
默认值、min/max/step/required 全部原样保留，没有改任何业务默认。

---

## 3. 文案：把施工备注换成用户说明

用 `tests/test_settings_night_crystal.py::UserFacingCopyTests` 里的小型 JS 注释剥离器
（保留字符串内容、只清掉 `//` 与 `/* */`）断言下列施工痕迹**不再出现在渲染字符串中**：

| 改前（渲染给用户看） | 改后 |
| --- | --- |
| `D3-40：控制粗体/斜体/代码块…读取侧 getattr(..., False) 恒真，等于没有关` | `开启后按下方「消息解析格式」渲染粗体、斜体、代码块等样式；关闭则统一按纯文本发送。` |
| `长期记忆（第 ④ 期）…D3-39：这一整块 11 个开关此前可 PUT…Mini App 完全无入口` | `长期记忆`：只讲"从对话里提炼跨天还有用的稳定事实" + `保存后立即生效，无需重启` |
| `⚠️ 不是长期记忆的总开关…第 ④ 期照样提炼照样写库` | hint 只留结论"只影响按当前问题从本群原始消息档案检索"，细节移进 `help`：`这不是长期记忆的总开关：…那部分由下一页的「启用长期记忆（总开关）」负责。` |
| `…普通成员不能独占全群额度（B-34）` | hint 留 `默认 30（每个作用域每天，0 表示不限）`，`（B-34）` 移进 `help` 并改写成用户话 |
| `关闭即回到旧行为` | `关闭后恢复为只审核普通成员。` |
| `默认 100，与改动前一致` | `默认 100；设为 0 表示不记录正文，只记录长度与内容哈希前缀` |
| `独立于 legacy 热历史压缩` | `原文与归档一条都不删，前台只读已发布的摘要，绝不等待摘要生成` |
| toast `旧权限配置已兼容修复，请检查后保存` | `已把旧版权限配置补全为当前字段，请检查后保存` |

`app.js` 里的 `// D3-42: …` 这类**源码注释**原样保留（那是给维护者看的，不会上屏）。

**没有**为了"变短"删掉任何约束：`需重启` 徽标、`保存后立即生效`、`留空保留当前密钥`、
`保存后清除`、`空白不会覆盖已保存的密钥`、`留空会保留已保存值`、以及所有单位
（秒/分钟/毫秒/小时/Token）都在回归里逐条断言仍然存在。

---

## 4. 移动优先与桌面

手机优先验证用 360 / 390 / 430px，桌面 1280 / 1680px。

- **触控目标 ≥44px**：`--control-height: 44px` 统一了
  `.icon-button .mini-icon-button .text-button .nav-button .prompt-button
  .group-quick-nav-button .advanced-panel > summary .group-settings-section > summary
  .group-card-toggle .field-help > summary .compact-check .toggle`。
  旧 CSS 里 `.mobile-nav .nav-button` 是 35px、`.field-help > summary` 曾经被我写成 28px，
  都已提到 44px。
- **顶栏不重叠、不出屏**：保留并强化了 520px / 420px / 360px 断点
  （`#save-button { min-width: 104px }`、360px 下 `min-width: 88px` + 标题截断 68px）。
  浏览器实测 `.topbar-actions` 右边界 ≤ 视口宽度，且与 `.topbar-title` 无重叠（每个视口 × 每个页面各 1 次断言）。
- **抽屉导航**：`≤820px` 侧栏变抽屉，ESC 关闭、焦点进入抽屉、背景 `inert`、关闭后焦点回到
  `#sidebar-toggle` —— 全部浏览器实测通过。
- **长文本 / 长 ID**：`.group-title-block` 桌面省略、≤620px 改为 `overflow-wrap: anywhere` 换行；
  `.field-hint`、`.notice`、`.toast`、`.page-head > div`、`.section-heading > div` 都带
  `overflow-wrap: anywhere`；`.bootstrap-row dd` 用等宽字体 + `anywhere`。
- **无横向溢出**：`body { overflow-x: clip }`，只有 `.mobile-nav` 和 `.prompt-list` 两条
  有意的横向滚动条。`verify.mjs` 对 5 个视口 × 10 个页面 ×（展开群卡片 / 展开每个分组 /
  展开所有高级面板）逐个断言 `documentElement.scrollWidth <= innerWidth + 1`，
  并排除"处于有意滚动容器内"的元素后再报越界元素。
- **桌面 1280+**：`.content` 锁在 1180px 居中；≥1560px 只加宽侧栏（264px），正文不再拉长。

---

## 5. Telegram / 无障碍

- `viewport-fit=cover` + `env(safe-area-inset-*)`，并叠加 `Telegram.WebApp.safeAreaInset` /
  `contentSafeAreaInset` / `viewport`（`--tg-safe-*`、`--tg-viewport-height`）。
- 深色是唯一主题：`:root` 即深色 + `color-scheme: dark` + `theme-color #0C1220`，
  样式表是渲染阻塞资源，页面不会先闪白底。旧的浅色默认与 `prefers-color-scheme: dark`
  覆盖块都已删除，不会出现半深半白。
- 可见键盘焦点：全局 `:focus-visible { outline: 2px solid var(--focus-ring) }`（冰青），
  浏览器实测 `outlineWidth 2px / style solid / color rgb(113,217,239)`。
- `prefers-reduced-motion: reduce`：过渡/动画降到 0.01ms，spinner 停止旋转。
- 对话框：`<dialog>` 打开时焦点进入对话框，ESC 关闭（浏览器实测）。
- 对比度：正文 / 辅助 / 最弱文字 / 主色 / 选中色 / 三个语义色在四种底色上全部 ≥4.5:1
  （源级断言 + 浏览器实测抽样，最低 4.82:1）。
- 系统字体，无外链资源；CSP 一字未动。

---

## 6. ⚠️ 交付中发现并修复的既有阻断级缺陷

**症状**：在任意字段里改完值后，用真实鼠标点"保存全部"——**点了没有任何反应**，
顶栏仍然显示"1 项更改待保存"，没有任何 toast，也没有发出任何请求。

**根因**：`updateChrome()` 每次都执行 `saveButton.innerHTML = …`。
从输入框移开鼠标点保存时，blur 会先触发 `change` → `updatePathControl()` → `updateChrome()`，
于是按钮的子节点（`<svg>` + `<span>保存全部</span>`）在 **mousedown 已经按下、mouseup 还没发生**
时被整体替换。mousedown 的目标节点被移除，浏览器因此不派发 `click`，保存函数根本没被调用。

**这是既有缺陷，不是本次改动引入的**。我在派单基线 `0094862` 上单独建了一个 worktree
（`git worktree add /tmp/dsh-base 0094862`），用同一份 harness 和同一段脚本复现，行为完全一致：

```
### BASELINE 0094862 (port 8791)
dirty? 1 项更改待保存
toasts: []                      ← 点击被吞掉
save-state: 1 项更改待保存

### 本分支 (port 8781)
dirty? 1 项更改待保存
toasts: [ '已保存全部 1 项更改' ]
save-state: 全部已保存 · 修订 13
```

**修复**（只改 `updateChrome()` 一处，不碰保存语义、不碰并发保护、不碰草稿）：

```js
const savePhase = state.saving ? "saving" : "idle";
if (saveButton.dataset.savePhase !== savePhase) {
  saveButton.dataset.savePhase = savePhase;
  saveButton.innerHTML = state.saving ? … : `${icon("save")}<span>保存全部</span>`;
}
```

标签只在"保存中 ↔ 空闲"真正翻转时才重绘，按钮 DOM 在按键期间保持稳定。
`saveButton.disabled` / `aria-label` / `title` 仍然每次都更新。
回归见 `SaveButtonRegressionTests::test_pressing_save_after_typing_actually_saves`，
浏览器复现脚本见 `tools/settings-ui-harness/checks/save-click-probe.mjs`。

> 这条超出"纯视觉改造"的范围，但它直接违反"保存是现有显式动作"这条硬边界，
> 所以修了。如果不希望这个分支带上它，可以单独 revert 这一个 hunk。

---

## 7. 测试

### 环境说明（如实记录）

- 本机系统 `python3` 是 **3.9.6**，项目要求 ≥3.12，默认环境**没有** pytest / aiogram / litellm，
  所以第一次运行时 `tests/test_settings_web.py` 直接 `ModuleNotFoundError: No module named 'aiogram'`。
- 用 `~/.local/bin/python3.12` 建了一个 venv 装齐依赖（`cryptography` 需要 `<44`，
  因为这台 x86_64 macOS 上没有对应 wheel 又没有 Rust 工具链）：

  ```bash
  python3.12 -m venv /tmp/dsh-nightcrystal-venv
  /tmp/dsh-nightcrystal-venv/bin/pip install "aiogram>=3.29" … "cryptography<44" pytest pytest-asyncio
  ```

- 前端回归是纯源码断言，系统 Python 3.9 就能跑，不需要上面这个 venv。
- **没有在 VPS 上跑过任何东西**，全量 suite 由父代理在隔离容器里跑。

### 基线（改动前，先跑一遍留底）

```
$ LITELLM_MODE=PRODUCTION /tmp/dsh-nightcrystal-venv/bin/python -m pytest \
      tests/test_settings_web.py tests/test_settings_api_helpers.py -q
.....................................                                    [100%]
37 passed in 35.06s

$ python3 tests/test_settings_frontend.py
Ran 42 tests in 0.028s
OK
```

### 改动后

```
$ python3 tests/test_settings_frontend.py
Ran 42 tests in 0.034s
OK

$ python3 tests/test_settings_night_crystal.py
Ran 28 tests in 0.086s
OK

$ node --check bot/web/static/app.js
OK

$ LITELLM_MODE=PRODUCTION /tmp/dsh-nightcrystal-venv/bin/python -m pytest \
      tests/test_settings_web.py tests/test_settings_api_helpers.py -q
…（见下）
```

**现有测试一条都没有被删改，也没有被放松。**
`tests/test_settings_frontend.py` 的 42 条断言全部保持原样通过——
它们检查的都是字段路径、save/revision 保护、dirty tracking、secret 语义、
分角色可见性、即时操作确认与交互锁、折叠状态捕获、校验定位等**行为契约**，
没有一条是"断言施工备注文案"，因此不需要按产品语义改写。新增的
`tests/test_settings_night_crystal.py`（28 条）是**视觉契约 / 布局 / 交互 / 文案 / 发布卫生**的增量回归。

### 唯一一条按产品语义改写的既有测试

`tests/test_p1_forkfeatures_d3_39_40_42_config_surface.py::
D3_39MemorySwitchUiTests::test_memory_recall_enabled_is_not_presented_as_the_master_switch`
在全量跑的时候失败了一次。原因与 §3 同一件事：原断言用正则只抓 `toggle()` 的**第 3 个参数**
（hint），要求字面量 `"不是"` 出现在里面；重设计把"这不是长期记忆的总开关"这句话移到了
**第 5 个参数**（可访问的 `help`/`<details>`），因为 60 多字的施工期解释压在字段下方
正是这次要消除的视觉噪声。

改后的断言**没有放宽语义**，它要求：

1. label 里不能出现"总开关"（不变）；
2. `"不是"` 与 `"总开关"` 必须出现在 hint **或** help 里——即"用户必须在界面上被明确告知
   这个开关不是总开关"这条产品要求仍然被强制。

负向对照确认它不是空断言：把 help 里的那句话删掉后，断言即失败（已实测）。

### 全量 suite（本地 python3.12 venv，非生产依赖锁定）

```
$ LITELLM_MODE=PRODUCTION /tmp/dsh-nightcrystal-venv/bin/python -m pytest tests/ -q
1 failed, 3680 passed, 50 warnings, 710 subtests passed in 1273.08s (0:21:13)
   FAILED tests/test_p1_forkfeatures_d3_39_40_42_config_surface.py::
          D3_39MemorySwitchUiTests::test_memory_recall_enabled_is_not_presented_as_the_master_switch
```

这一条失败是本次文案重写引起的（见上面"唯一一条按产品语义改写的既有测试"）。
按产品语义改完该断言后重跑全量：

```
$ LITELLM_MODE=PRODUCTION /tmp/dsh-nightcrystal-venv/bin/python -m pytest tests/ -q
3681 passed, 50 warnings, 710 subtests passed in 1259.30s (0:20:59)
```

`tests/test_p1_forkfeatures_d3_39_40_42_config_surface.py` 单文件复跑：`17 passed, 33 subtests passed in 0.87s`。

### 真实浏览器验收（Playwright + 本机 Google Chrome，只监听 loopback，只用合成数据）

```
### verify.mjs — 5 视口 × 10 页面 × 展开群卡片/分组/高级面板
255 checks passed, 0 failed
### interact.mjs — 草稿、保存成功/失败、折叠态、对话框、抽屉、reduced-motion、焦点环
29 interaction checks passed, 0 failed
### roles.mjs — 对比度
11 role/state/contrast checks passed, 0 failed
### roles.mjs（--group-admin 会话）
15 role/state/contrast checks passed, 0 failed
### roles.mjs（--no-groups 错误态）
14 role/state/contrast checks passed, 0 failed
```

逐条通过项举例（完整清单见 `docs/ui-night-crystal/layout-check-report.json`）：

```
PASS  [mobile-360/bot] no horizontal scroll (scrollWidth=360 vw=360)
PASS  [mobile-390/groups] group card 0 expanded: no horizontal scroll (390)
PASS  [mobile-390/groups] group 0 section 0 open: no horizontal scroll (390)
PASS  [mobile-430/bot] all touch targets >= 44px (0 below)
PASS  [mobile-390/bot] topbar actions stay on screen (save right=374 vw=390)
PASS  [mobile-390/bot] topbar title and actions do not overlap
PASS  [desktop-1280/overview] no uncaught JS errors (0)
PASS  a failed save opens the advanced panel that owns the invalid field
PASS  the invalid field is visible after the failed save
PASS  the invalid edit is not discarded
PASS  the draft survives a failed save
PASS  the save button stays enabled after a failed save
PASS  draft survives switching tabs
PASS  advanced panel stays open after a tab round trip
PASS  the group-admin save state renders
PASS  a group admin lands on the groups page
PASS  only the groups page is reachable (["groups"])
PASS  no request is made to the global settings API for a group admin
PASS  a failed group list renders the error state
PASS  focus moves into the confirm dialog
PASS  Escape closes the confirm dialog / closes the drawer
PASS  focus returns to the drawer toggle
PASS  transitions are disabled under prefers-reduced-motion (1e-05s)
PASS  keyboard focus draws a >=2px outline ({"width":"2px","style":"solid","color":"rgb(113, 217, 239)"})
PASS  field hint contrast 7.98:1 / input placeholder contrast 4.82:1 / …（全部 ≥4.5）
```

### 截图（真实浏览器渲染，非画布伪造）

| 路径 | 视口 | 页面 |
| --- | --- | --- |
| `docs/ui-night-crystal/mobile-390-bot-behavior.png` | 390×844 @2x | Bot 行为（含流式/摘要/预算/历史/记忆细调折叠层） |
| `docs/ui-night-crystal/mobile-390-groups.png` | 390×844 @2x | 群组设置（群卡片已展开 + 首个分组已展开） |
| `docs/ui-night-crystal/mobile-390-models.png` | 390×844 @2x | 模型路由（供应商 / 角色 / 回退链） |
| `docs/ui-night-crystal/desktop-1280-overview.png` | 1280×900 | 运行概览 |

像素抽样（`checks/pixels.mjs`，把 PNG 画进 canvas 逐像素统计）证明深色主题真的渲染了：

```
mobile-390-bot-behavior.png  dark 93.9%  light 3.6%  cyan 0.35%  base=rgb(12,18,32)
mobile-390-groups.png       dark 94.9%  light 2.8%  violet 0.18% base=rgb(12,19,33)
mobile-390-models.png       dark 94.9%  light 2.8%            base=rgb(12,18,32)
desktop-1280-overview.png   dark 97.2%  light 1.2%  violet 0.07% base=rgb(15,23,38)
```

浏览器里读回来的**计算样式**（不是 CSS 源码，而是实际渲染值）：

```
body                       background rgb(12,18,32)   color rgb(238,243,252)   color-scheme dark
.sidebar                   background rgb(15,23,38)
.settings-section          background rgb(21,31,49)   border rgb(42,58,82)    radius 16px
.field input               background rgb(27,41,64)   border rgb(42,58,82)    radius 10px  min-height 44px
.nav-button.active         background rgba(184,164,244,.14)  color rgb(203,187,250)  border rgba(184,164,244,.26)
#save-button               background rgb(113,217,239) color rgb(6,36,48)      min-width 126px  min-height 44px
.secondary-button          background rgb(27,41,64)   border rgb(42,58,82)
.badge.warning             background rgba(240,182,98,.13)  color rgb(240,182,98)
.field-hint                color rgb(168,181,204)  font-size 12px
.page-head h2              font-size 21px  font-weight 700  color rgb(238,243,252)
.section-heading h3        font-size 15px  font-weight 680
.advanced-panel            background rgb(17,26,42)   border rgb(32,44,65)
.advanced-panel > summary  min-height 44px  cursor pointer
.toggle-track (checked)    background rgb(113,217,239) border rgb(113,217,239)
```

### 本地预览怎么起

```bash
python3 tools/settings-ui-harness/harness.py --port 8781
# 打开 http://127.0.0.1:8781/settings
```

harness 只绑 `127.0.0.1`、只读 `tools/settings-ui-harness/fixtures/settings.json`（合成数据）、
不读 `.env`、不连数据库、不请求 Telegram（把 `telegram.org` 的 SDK 换成本地桩）。
详见 `tools/settings-ui-harness/README.md`。

---

## 8. 功能与安全边界（未变动的证据）

- 后端 `bot/` 下的任何文件都**没有**改动（`git diff --stat` 只有 3 个静态资源文件）。
- 字段路径、`save`/`revision` 并发保护、dirty tracking、secret masking / 清空语义、
  分角色可见性、即时操作确认与交互锁、取消/失败恢复语义：全部保留，
  `tests/test_settings_frontend.py` 的 42 条 + `tests/test_settings_web.py` 的回归未改一行。
- 保存仍是显式动作：`saveButton.addEventListener("click", saveAllChanges)` 不变，
  没有 `setInterval` / 自动保存；`switchTab()` 里没有 `apiFetch` / `persist*`（新增断言），
  切 tab、加载、主题切换都不会写配置。
- 群管理员越权：浏览器实测群管理员会话落地在"群组设置"、侧栏只有 1 个入口、
  **完全没有向 `/api/v1/settings` 发起过请求**。视觉改造没有扩大任何权限。
- 没有 demo 模式、没有绕过 Telegram initData / 鉴权的入口、没有硬编码 session 或密钥。
- CSP 逐字未改；没有新增外链字体、CDN 或任何新的网络请求。
- 生产镜像不包含 harness（`Dockerfile` 只 `COPY bot` / `COPY prompt`），
  `tests/test_settings_night_crystal.py::ReleaseHygieneTests` 会持续断言这一点，
  同时断言 `bot/web/static/*` 里不出现 `harness` / `fixture` / `mock` / `localhost` / `127.0.0.1`。
- 参考图 `img_cbd5c9d533e5.png` **没有**进入仓库。

---

## 9. 未验证 / 留给父代理的项

1. **真机 Telegram WebView 未验证。** 本机只有桌面 Chrome，safe-area / viewport 事件
   是用桩模拟的；`syncTelegramSafeArea()` 在 iOS/Android 客户端上的真实行为需要真机确认。
2. **父代理的隔离容器全量 suite。** 本机 `python3.12` venv 里
   `cryptography` 被迫降到 43.0.3（这台机器没有 44+ 的 wheel 也没有 Rust），
   跟生产 `requirements.lock` 不完全一致；`edge-tts` 等依赖也只按 `>=` 装了最新版。
   **VPS 隔离容器里的全量结果是准的。**
3. **独立视觉验收。** 截图是本机 Chrome 渲染的合成数据；真实 Telegram 客户端与真实群名的
   渲染需要父代理再确认一遍。
4. **深色是唯一主题。** 如果将来要支持 Telegram 的浅色主题，
   需要把整套 token 在浅色下再写一遍——不能只翻转 `--bg`。
5. §6 的保存按钮修复属于既有缺陷，跨出了"纯视觉改造"的范围；
   如果希望它单独成一个 commit / 单独回滚，告诉我即可。
