from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CommandEntry:
    command: str
    usage: str
    purpose: str
    suggest_when: str
    section: str


_COMMANDS: tuple[CommandEntry, ...] = (
    CommandEntry("/help", "/help", "查看完整帮助", "用户问你有哪些命令、怎么用、功能总览", "核心入口"),
    CommandEntry("/settings", "/settings", "打开可视化设置中心", "最高管理员要配置机器人", "核心入口"),
    CommandEntry("/lm", "/lm", "查看永久记忆列表，支持翻页和删除", "用户想查看、删除永久记忆，或找不到某条记忆", "核心入口"),
    CommandEntry("/lm add", "/lm add <内容>", "新增一条永久记忆", "用户想显式通过命令写入永久记忆", "核心入口"),
    CommandEntry(
        "/lm replace",
        "/lm replace <#ID或关键词> => <新内容>",
        "修改已有永久记忆",
        "用户想显式通过命令修改永久记忆",
        "核心入口",
    ),
    CommandEntry("/addrule", "/addrule <自然语言>", "新增群规", "用户想显式通过命令新增群规", "核心入口"),
    CommandEntry("/rules", "/rules", "查看群规列表，支持翻页和删除", "用户想查看或删除群规", "核心入口"),
    CommandEntry("/av", "/av <番号/演员/关键词>", "搜索 AV 资源", "用户想搜片或查询 AV 详情", "核心入口"),
    CommandEntry("/voteban", "回复目标用户消息后发送 /voteban [举报理由]", "发起民主投票封禁，票数达标即封禁被回复用户；管理员可在投票消息上取消投票或直接封禁；与 AI 技能共用用户额度", "用户想集体投票封禁骚扰者", "核心入口"),
    CommandEntry("/report", "回复要举报的消息后发送 /report [补充说明]", "举报被漏判的消息：机器人立刻让审核模型复核，并把结果与原文转交管理员", "群友看到广告/骚扰消息没有被处理", "核心入口"),
    CommandEntry("/find", "/find <关键词>", "在本群保留期聊天记录里搜索消息", "群友想找回群里聊过的某条消息", "核心入口"),
    CommandEntry("/checkin", "/checkin", "每日签到得积分：连续第 N 天得 N 分（最高 10 分），断签从 1 分重来", "群友想签到，或问签到怎么用", "核心入口"),
    CommandEntry("/points", "/points", "查看可用积分、连续天数；广告质询时可花 2 分免除", "群友问自己有多少积分", "核心入口"),
    CommandEntry("/rank", "/rank 或 /rank week", "查看本群积分榜 Top10；加 week 只看本周获得的积分", "群友想看积分排名，或想知道本周谁最活跃", "核心入口"),
    CommandEntry("/me", "/me", "查看自己的积分、签到、违规与封禁状态", "群友想一次看清自己的积分和违规记录", "核心入口"),
    CommandEntry("/shop", "/shop", "打开积分商店：价目表、每件商品怎么用", "群友问积分能换什么、怎么用积分", "核心入口"),
    CommandEntry("/tag", "/tag <头衔文字>（加 30天 买 80 分的长租）", "用积分给自己挂群内自定义头衔：30 分 7 天、80 分 30 天", "群友想给自己挂一个群内头衔，或头衔到期了想续费", "核心入口"),
    CommandEntry("/top", "回复自己的一条消息后发送 /top", "花 20 分把自己的一条求助消息置顶 6 小时", "群友想让自己的求助被大家看见", "核心入口"),
    CommandEntry("/draw", "/draw", "花 5 分抽奖一次（每人每天最多 10 次）", "群友想用积分抽奖试试手气", "核心入口"),
    CommandEntry("/health", "/health", "查看本群今日审核命中、待完成质询、归档量与当前模型通道", "管理员想快速了解机器人当前运行状况", "群审核管理"),
    CommandEntry("/modstats", "/modstats [天数]", "审核质量报表：命中构成、边缘判定、误伤率（管理员）", "管理员想了解审核判得准不准、误伤多少", "管理工具"),
    CommandEntry("/cost", "/cost [天数]", "成本与健康报表：token 用量、缓存命中、超时与空响应（管理员）", "管理员想看这几天花了多少 token、缓存有没有生效", "管理工具"),
    CommandEntry("/warnings", "/warnings", "查看当前群警告/封禁名单", "管理员想查看审核处罚情况", "群审核管理"),
    CommandEntry("/clearwarnings", "回复用户后 /clearwarnings，或 /clearwarnings <用户ID>", "清空某用户的累计违规次数", "管理员要重置某用户的违规次数", "群审核管理"),
    CommandEntry("/ban", "回复用户后 /ban [原因]，或 /ban <用户ID> [原因]", "在当前群手动封禁；最高管理员可选择全局", "管理员要封禁某个用户", "群审核管理"),
    CommandEntry("/spam", "回复垃圾消息后 /spam [原因]，或 /spam <用户ID> [原因]", "封禁目标并加入全局封禁名单", "管理员确认某用户是垃圾广告或骚扰账号", "群审核管理"),
    CommandEntry("/unban", "回复用户后 /unban，或 /unban <用户ID>", "解除当前群封禁；最高管理员可选择全局", "管理员要解封某个用户", "群审核管理"),
    CommandEntry("/raidguard", "/raidguard on [分钟]|off|status，或 /raidguard <分钟>", "手动开启、限时开启或解除爆破锁定", "管理员需要立即阻止新成员加入", "群审核管理"),
    CommandEntry("/aiexempt", "回复目标用户消息后发送 /aiexempt", "豁免某用户的 AI 审核", "管理员想让某用户跳过审核", "群审核管理"),
    CommandEntry("/unaiexempt", "回复目标用户消息后发送 /unaiexempt，或 /unaiexempt <用户ID>", "取消某用户的 AI 审核豁免", "管理员想恢复某用户的审核", "群审核管理"),
    CommandEntry("/exemptlist", "/exemptlist（别名 /modlist）", "查看本群审核豁免与回复静默名单，支持翻页和一键取消", "管理员想查看哪些人被豁免审核或被静默回复，或直接点按钮移除", "群审核管理"),
    CommandEntry("/mute", "回复目标用户消息后发送 /mute", "忽略某用户后续消息回复", "管理员不想让 bot 再回复某用户", "群审核管理"),
    CommandEntry("/mute all", "/mute all", "全群仅审核不回复", "管理员希望 bot 暂时只做审核", "群审核管理"),
    CommandEntry("/unmute", "回复目标用户消息后发送 /unmute", "恢复某用户的消息回复", "管理员想重新允许 bot 回复某用户", "群审核管理"),
    CommandEntry("/unmute all", "/unmute all", "恢复本群正常回复", "管理员想退出全群静默回复模式", "群审核管理"),
    CommandEntry("/proactive", "/proactive on|off|status", "控制主动话题开关和状态", "管理员要控制 bot 主动开话题", "群审核管理"),
    CommandEntry("/mimic", "回复用户后 /mimic，或 /mimic status|off", "学习指定用户的说话风格并应用", "管理员想让 bot 模仿某个群友说话", "群审核管理"),
    CommandEntry("/compact", "/compact", "立即把本群临时对话历史压缩进背景摘要", "管理员想手动压缩上下文、清理临时对话历史", "群审核管理"),
    CommandEntry("/authgroup", "/authgroup <群ID> 或群内直接 /authgroup", "授权群组", "最高管理员要授权新群", "最高管理员命令"),
    CommandEntry("/unauthgroup", "/unauthgroup <群ID> 或群内直接 /unauthgroup", "撤销群组授权", "最高管理员要取消群授权", "最高管理员命令"),
    CommandEntry("/authlist", "/authlist", "查看授权群组列表", "最高管理员要查看所有已授权群", "最高管理员命令"),
    CommandEntry("/banlist", "/banlist", "查看封禁名单", "最高管理员要查看封禁了哪些人", "最高管理员命令"),
    CommandEntry("/authadmin", "回复用户后 /authadmin，或 /authadmin <群ID> <用户ID>", "授权群管理员", "最高管理员要给某人群管理权限", "最高管理员命令"),
    CommandEntry("/unauthadmin", "回复用户后 /unauthadmin，或 /unauthadmin <群ID> <用户ID>", "撤销群管理员权限", "最高管理员要取消某人群管理权限", "最高管理员命令"),
    CommandEntry("/adminlist", "/adminlist <群ID> 或群内直接 /adminlist", "查看群管理列表", "最高管理员要查看群管理列表", "最高管理员命令"),
    CommandEntry("/atreply", "/atreply 或 /atreply enable|disable", "控制仅 @ 才回复", "最高管理员要调整 bot 的 @ 回复模式", "最高管理员命令"),
    CommandEntry("/tts", "/tts 或 /tts enable|disable|always", "控制 TTS 状态", "最高管理员要查看或调整语音模式", "最高管理员命令"),
    CommandEntry("/av enable", "/av enable", "启用本群 AV 查询", "最高管理员要在当前群打开 AV 查询", "最高管理员命令"),
    CommandEntry("/av disable", "/av disable", "停用本群 AV 查询", "最高管理员要在当前群关闭 AV 查询", "最高管理员命令"),
)


def build_help_text() -> str:
    return (
        "<b>命令总览</b>\n\n"
        "<b>核心入口</b>\n"
        "/help：查看帮助\n"
        "/settings：最高管理员打开可视化设置中心\n"
        "/lm：永久记忆列表，支持翻页和删除\n"
        "/lm add &lt;内容&gt;：新增永久记忆\n"
        "/lm replace &lt;#ID或关键词&gt; =&gt; &lt;新内容&gt;：修改永久记忆\n"
        "/addrule &lt;自然语言&gt;：新增群规\n"
        "/rules：群规列表，支持翻页和删除\n"
        "/av &lt;番号/演员/关键词&gt;：搜索 JAVBUS + MADOUQU + DMM + FC2\n"
        "/voteban：回复用户消息后发起民主投票封禁\n"
        "/report：回复要举报的消息后转交管理员，机器人会先复核一遍\n"
        "/find &lt;关键词&gt;：在本群保留期聊天记录里搜索消息\n"
        "/checkin：每日签到得积分，连续第 N 天得 N 分（最高 10 分）；漏签一天重新从 1 分算\n"
        "/points：查看可用积分、连续签到天数（消息被判定广告时可花 2 积分免除质询）\n"
        "/rank：看本群积分榜（加 week 只看本周获得的积分）\n"
        "/me：看自己的积分与违规记录\n"
        "/shop：积分商店（自定义头衔、置顶求助、抽奖的价目表与用法）\n"
        "/tag &lt;头衔文字&gt;：用积分给自己挂群内头衔（30 分 7 天；加 30天 是 80 分 30 天）\n"
        "/top：回复自己的一条消息后发送，花 20 分把它置顶 6 小时\n"
        "/draw：花 5 分抽奖一次（每人每天最多 10 次）\n"
        "@admin：呼叫全部群管理员（可附说明或回复被举报消息）\n\n"
        "<b>语义入口</b>\n"
        "主模型会自动调用 skill 处理：永久记忆新增/查看/修改、群规新增/查看。\n"
        "删除统一走 /lm、/rules 这些命令页的内联按钮。\n\n"
        "<b>群审核管理（需已授权）</b>\n"
        "/warnings：查看警告/封禁名单\n"
        "/clearwarnings：回复用户或指定用户 ID，清空累计违规次数\n"
        "/ban / unban：群管理员手动本群封禁/解封；最高管理员可选择全局范围\n"
        "/spam：封禁垃圾用户、删除被回复消息并加入全局封禁名单\n"
        "/raidguard on [分钟]|off|status：手动控制爆破防护，数字单位为分钟\n"
        "/aiexempt：回复目标用户消息后豁免审核\n"
        "/unaiexempt：回复目标用户消息后取消豁免（也支持 /unaiexempt &lt;用户ID&gt;）\n"
        "/exemptlist（或 /modlist）：查看审核豁免与回复静默名单，可点按钮取消\n"
        "/mute：回复目标用户消息后忽略其后续回复\n"
        "/mute all：本群仅做审核，不再回复\n"
        "/unmute：回复目标用户消息后恢复其回复\n"
        "/unmute all：恢复本群正常回复\n"
        "/proactive on|off|status：主动话题开关/状态\n"
        "/mimic：回复用户后学习其说话风格（status 查看 / off 停止）\n"
        "/compact：立即压缩本群临时对话历史进背景摘要\n"
        "/health：本群今日审核命中、待完成质询、归档量与当前模型通道\n\n"
        "/modstats：审核质量报表（近 7 天命中数、边缘判定占比、被改判放行的误伤率，可加天数如 /modstats 30）\n"
        "/cost：成本与健康报表（token 用量、缓存命中率、超时/空响应，可加天数如 /cost 30）\n"
        "<b>最高管理员命令</b>\n"
        "/authgroup / unauthgroup / authlist\n"
        "/banlist：查看全局封禁名单\n"
        "/authadmin / unauthadmin / adminlist\n"
        "/atreply / atreply enable|disable\n"
        "/tts / tts enable|disable|always\n"
        "/av enable|disable"
    )


def build_bot_commands() -> list[tuple[str, str]]:
    """The Telegram "/" command menu, derived from the same catalog as /help.

    Keeping one source means a new command shows up in the menu and in /help
    together instead of being remembered by hand.  Only single-token names are
    registrable: entries documenting an argument form ("/lm add", "/mute all")
    are skipped so the menu never advertises a name Telegram would reject.
    """
    seen: set[str] = set()
    commands: list[tuple[str, str]] = []
    for item in _COMMANDS:
        name = item.command.strip().lstrip("/")
        if not name or " " in name or name in seen:
            continue
        seen.add(name)
        commands.append((name, item.purpose[:250]))
    return commands


def build_command_guide_context() -> str:
    lines = [
        "[BOT_COMMAND_GUIDE]",
        "authoritative: yes",
        "Use this block when users ask what commands exist, how to use them, or when you should remind them of the correct /command form.",
        "If a user is trying to delete memories/rules, prefer reminding the corresponding explicit command entrypoint.",
        "When suggesting a command, mention a short usage example rather than only saying the command name.",
        "commands:",
    ]
    for item in _COMMANDS:
        lines.append(f"- command: {item.command}")
        lines.append(f"  section: {item.section}")
        lines.append(f"  usage: {item.usage}")
        lines.append(f"  purpose: {item.purpose}")
        lines.append(f"  suggest_when: {item.suggest_when}")
    return "\n".join(lines)


# Sections whose commands exist for group operators.  "核心入口" entries are the
# member-facing ones (/help, /av, /voteban, ...) and must keep working untouched.
_MANAGEMENT_SECTIONS = frozenset({"群审核管理", "最高管理员命令"})
_MEMBER_SECTIONS = frozenset({"核心入口"})

_BARE_COMMAND_RE = re.compile(r"[@（(\s]")
_ALIAS_RE = re.compile(r"（别名\s*([^）]+)）")


def _declared_aliases(entry: CommandEntry) -> frozenset[str]:
    """Extra command names an entry documents, e.g. ``（别名 /modlist）``.

    The alias is documented in the *usage* text (``/exemptlist`` is the command,
    ``/exemptlist（别名 /modlist）`` the usage), so both fields are scanned.
    """
    names: set[str] = set()
    for group in _ALIAS_RE.findall(f"{entry.command} {entry.usage}"):
        for part in re.split(r"[/\s、,，]+", group):
            name = part.strip().lower()
            if name:
                names.add(name)
    return frozenset(names)


def bare_command(text: str) -> str:
    """The plain command name in ``text`` (``/mute all`` -> ``mute``).

    Handles the group form ``/mute@xatongxue_bot`` and the catalog's documented
    argument forms (``/lm replace <...>``, ``/exemptlist（别名 /modlist）``).
    """
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return ""
    token = stripped.split(maxsplit=1)[0][1:]
    # Cut anything that is not part of the name: "@bot", "（别名 /modlist）", "(...)".
    token = _BARE_COMMAND_RE.split(token, maxsplit=1)[0]
    return token.strip().lower()


def management_command_names() -> frozenset[str]:
    """Command names reserved for operators, for group-side command cleanup.

    Derived from the catalog instead of a hand-kept list so a new operator
    command is covered as soon as it is documented.  A name that is also
    member-facing in another section is dropped: ``/av enable`` must not drag
    the member command ``/av`` into the cleanup set.
    """
    member_names = {
        bare_command(item.command)
        for item in _COMMANDS
        if item.section in _MEMBER_SECTIONS
    }
    names: set[str] = set()
    for item in _COMMANDS:
        if item.section not in _MANAGEMENT_SECTIONS:
            continue
        names.add(bare_command(item.command))
        names.update(_declared_aliases(item))
    return frozenset(name for name in names - member_names if name)
