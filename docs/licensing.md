# 许可与署名（MIT）

本文件说明这个仓库的许可范围、署名要求，以及**哪些东西不由本仓库的 MIT 覆盖**。
它解释许可文本本身的含义，**不构成任何合规结论或法律保证**——某个具体部署是否符合
Telegram、模型供应商、数据保护等第三方条款，由部署者自行判断。

## 本仓库的 LICENSE 保持原样，且必须保持原样

- 仓库根目录 [`LICENSE`](../LICENSE) 是 MIT License 全文，其中第一段版权声明是
  `Copyright (c) 2025 Sanite&Ava`——这是上游作者对本项目原始代码的版权声明。
- 该文件与上游 [Hamster-Prime/Smart_Group_Bot](https://github.com/Hamster-Prime/Smart_Group_Bot)
  `main` 分支的 LICENSE 逐字一致：2026-10-06 校对时把上游那份取下来与本仓库这份做了整文件
  `diff`，无差异，SHA-256 同为 `5269fca5d0452f8534a054f380755a13cc277208796fd5fb4c4acbf9df63271c`。
- 本 fork **没有修改 LICENSE**，本次文档校对也**不会**修改它。原因有三条：
  1. MIT 的唯一条件是「上述版权声明与许可声明必须包含在软件的所有副本或实质性部分中」。
     改写或删除原版权声明会直接违反 MIT；
  2. MIT 没有任何「版权转让」条款，下游 fork 不能通过改文件的方式把原作者的版权变成自己的；
  3. 署名是来源信息，不是形式。声称自己是原始作者比留着一个不属于自己的版权声明糟糕得多
     （仓库里的 `tests/test_public_artifact_scan.py` 就专门为此钉了一条反向断言）。

## MIT 授予了什么

按 [`LICENSE`](../LICENSE) 的正文：

- **允许**：使用、复制、修改、合并、发布、分发、再许可（sublicense）、销售；
- **唯一条件**：在软件的所有副本或实质性部分中保留原版权声明与本许可声明；
- **不要求**：署名原作者之外的人、不公开源码、不使用相同许可证、不得用于商业。

「再许可」指你可以把自己写的改动按别的条款发布——但必须**另外**附带这份 MIT 声明，
不能因为自己那部分换了条款就把上游代码的条款也一并换掉。

## 「AS IS」意味着什么

[`LICENSE`](../LICENSE) 结尾三段是免责声明：软件按**现状**提供，**不附带任何形式的
担保**（明示或默示），作者与版权持有人**不承担任何**使用后果的责任。

实际影响：代码是公开交付的，没有 SLA、没有可用性承诺，也没有缺陷修复承诺。使用、部署
或依赖它造成的问题（包括数据损失），作者不承担赔偿责任。如果需要更强的责任边界（例如
SLA 或赔偿条款），那要靠单独的商务约定，开源许可本身不提供。

## fork 维护者与原作者的关系

- 本 fork 由 [@uxiner](https://github.com/uxiner) 维护，仓库是
  <https://github.com/uxiner/Smart_Group_Bot>；这些是写死在代码里的公开项目元数据
  （`bot/utils/project_info.py`：`PROJECT_REPOSITORY_URL` / `PROJECT_DEVELOPER` /
  `PROJECT_UPSTREAM_URL`）。
- **维护者只对自己写的那部分改动拥有版权**，并且以同样的 MIT 条款发布。
- 维护者**不是**原始版权持有人，**不主张**独占本项目的原始版权，也不代表原作者发言。
- 本仓库不添加任何额外许可限制（没有附加条款、没有 CLA、没有 source-available 限制）：
  fork 的改动同样以 MIT 发布，读者拿到的权利与上游 MIT 给的完全一致。

## MIT 不覆盖的东西

MIT 只覆盖本项目自身。下列内容各自按**它自己的**许可证或服务条款处理，MIT 既不
重新授权它们，也不新增任何限制：

- **直接与间接依赖**（aiogram、LiteLLM、SQLAlchemy、aiohttp、pydantic、cryptography
  等）：各自的上游许可证。本仓库只在 `pyproject.toml` 声明直接依赖，并在
  `requirements.lock` / `uv.lock` 锁定版本——**使用前请查阅对应版本的许可文本**。
- **随包分发的第三方前端资源**：`bot/web/static/lucide.LICENSE`（Lucide/Feather，ISC）
  ——第三方声明随文件一起保留，改动其使用范围时不要删掉它。
- **Telegram Bot API 与 Telegram 平台**：第三方服务，按 Telegram 的条款使用；机器人账号
  的创建、群管理员权限与平台合规由部署者负责。
- **模型 / TTS / 搜索等外部服务**：由各服务商的条款约束；本仓库不代理、也不改变它们的条款。
- **部署期数据**：数据库内容、群消息、用户数据、备份文件由部署者自己保管；本仓库只提供
  存取它的代码，不对数据的合法性作任何声明。
- **仓库内界面截图**（`docs/ui-night-crystal/`）：本项目自身界面的截图；如需对外使用，
  仍由使用者自行确认其中不含第三方受版权保护的内容。
- **上游 README 存档**（`docs/README.upstream.md`）里的徽章与外链图片：由 shields.io、
  star-history 等第三方服务生成，按其条款使用；该存档保留上游署名，不做改写。

## 分发时必须一起带走的东西

如果你分发这个软件（无论原样还是改动后）：

1. **保留** `LICENSE` 全文与其中的 `Copyright (c) 2025 Sanite&Ava`；
2. 在自己的文档或发行说明里**说明**改动来自本 fork（原仓库
   <https://github.com/uxiner/Smart_Group_Bot>）——这是 MIT 的「保留声明」义务之外的
   良好实践，也是让来源链不至于断掉的最低成本做法；
3. 不要把上游的 MIT 声明替换成你自己的、只声明你自己名字的版本。

## 相关文件

| 关注点 | 文件 |
| --- | --- |
| 许可正文（不可改动） | [`LICENSE`](../LICENSE) |
| 仓库内的公开项目元数据与上游归属 | `bot/utils/project_info.py` |
| 上游 README 存档（保留上游署名） | [`docs/README.upstream.md`](./README.upstream.md) |
| 公开产物的敏感信息扫描（禁止伪造作者署名） | `tests/test_public_artifact_scan.py` |