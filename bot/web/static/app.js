(() => {
  "use strict";

  const tg = window.Telegram?.WebApp;
  const app = document.getElementById("app");
  const content = document.getElementById("content");
  const desktopNav = document.getElementById("desktop-nav");
  const mobileNav = document.getElementById("mobile-nav");
  const pageTitle = document.getElementById("page-title");
  const pageSubtitle = document.getElementById("page-subtitle");
  const saveButton = document.getElementById("save-button");
  const reloadButton = document.getElementById("reload-button");
  const saveState = document.getElementById("save-state");
  const sidebarStatus = document.getElementById("sidebar-status");
  const sidebar = document.querySelector(".sidebar");
  const sidebarToggle = document.getElementById("sidebar-toggle");
  const sidebarClose = document.getElementById("sidebar-close");
  const topbarActions = document.querySelector(".topbar-actions");
  const mobileSidebarMedia = window.matchMedia("(max-width: 820px)");
  const toastRegion = document.getElementById("toast-region");
  const confirmDialog = document.getElementById("confirm-dialog");
  const confirmTitle = document.getElementById("confirm-title");
  const confirmMessage = document.getElementById("confirm-message");

  const ALL_NAV_ITEMS = [
    { id: "overview", label: "概览", icon: "layout-dashboard", subtitle: "运行状态与启动参数", group: "总览" },
    { id: "models", label: "模型", icon: "boxes", subtitle: "供应商、角色与回退链", group: "AI 配置" },
    { id: "prompts", label: "Prompts", icon: "file-code-2", subtitle: "模型系统提示词", group: "AI 配置" },
    { id: "bot", label: "Bot 行为", icon: "bot", subtitle: "消息、上下文与主动发言", group: "Bot 能力" },
    { id: "safety", label: "审核验证", icon: "shield-check", subtitle: "内容审核与入群验证", group: "Bot 能力" },
    { id: "media", label: "媒体能力", icon: "audio-waveform", subtitle: "语音、音乐、AV 与贴纸", group: "Bot 能力" },
    { id: "integrations", label: "外部服务", icon: "plug", subtitle: "影片信息服务接入", group: "Bot 能力" },
    { id: "operations", label: "运营参数", icon: "coins", subtitle: "积分、活跃、私聊与提醒", group: "Bot 能力" },
    { id: "onboarding", label: "新群接入", icon: "user-plus", subtitle: "审批接入申请与待审队列", group: "群组管理" },
    { id: "groups", label: "群组设置", icon: "users", subtitle: "逐群行为、群规与自动化", group: "群组管理" },
    { id: "access", label: "权限封禁", icon: "shield-ban", subtitle: "群授权、管理员与全局名单", group: "群组管理" },
    { id: "history", label: "变更历史", icon: "history", subtitle: "配置版本、差异与回滚", group: "系统" },
    { id: "logging", label: "日志", icon: "scroll-text", subtitle: "运行日志与文件轮转", group: "系统" },
  ];

  const GROUP_SECTION_NAV = [
    { key: "reply-media", label: "回复与媒体", icon: "message-circle" },
    { key: "model-api", label: "模型 API", icon: "server" },
    { key: "onboarding", label: "入群欢迎", icon: "user-plus" },
    { key: "permissions", label: "成员权限", icon: "key-round" },
    { key: "safety", label: "安全防护", icon: "shield-check" },
    // 回放是**逐群**能力（后端 @any_admin + 按群隔离），所以它挂在群卡片里，
    // 而不是全局「审核验证」页：群管理员本来就只进得来群组页。
    // 它不编辑任何群设置，所以 GROUP_SECTION_SETTING_KEYS 里**没有**这一项——
    // 那个表只用来判断分节"有没有待保存"，空数组即恒为 false。
    { key: "replay", label: "审核回放", icon: "flask-conical" },
    { key: "management", label: "管理投票", icon: "badge-check" },
    { key: "proactive-style", label: "主动与风格", icon: "sparkles" },
    { key: "automation", label: "关键词与定时", icon: "timer" },
    { key: "rules-memory", label: "群规与记忆", icon: "book-open-check" },
    { key: "member-lists", label: "成员名单", icon: "contact-round" },
    // 「运营任务」是**只读**聚合视图（后端 @any_admin + 逐群隔离），和回放一样不编辑
    // 任何群设置，所以同样不进 GROUP_SECTION_SETTING_KEYS：空数组即恒无「待保存」。
    // key 不能叫 operations —— 那是全局「运营参数」页的 tab id。
    { key: "operations-status", label: "运营任务", icon: "activity" },
  ];

  const GROUP_SECTION_SETTING_KEYS = {
    "reply-media": ["mute_all_replies", "at_reply_mode", "av_enabled", "tts_mode"],
    "model-api": [
      "api_model_query_enabled", "api_model_query_base_url",
      "api_model_query_http_timeout_sec", "api_model_query_check_timeout_sec",
      "api_model_query_api_key_configured",
    ],
    onboarding: [
      "join_verification_enabled", "join_verification_provider", "welcome_message",
      "welcome_disable_link_preview", "welcome_buttons",
    ],
    permissions: ["default_permissions"],
    safety: [
      "patrol_enabled", "raid_guard_enabled", "raid_guard_pin_message", "raid_guard_join_threshold",
      "raid_guard_window_seconds", "raid_guard_lockdown_seconds",
      "raid_guard_lookback_seconds", "raid_guard_challenge_timeout_seconds",
    ],
    management: [
      "call_admin_enabled", "call_admin_pin_message", "call_admin_targets", "vote_ban_enabled",
      "vote_ban_pin_message",
      "vote_ban_threshold", "vote_ban_duration_seconds", "vote_ban_trigger_limit",
      "vote_ban_trigger_window_seconds",
    ],
    "proactive-style": ["proactive_enabled", "proactive_task_brief", "mimic_target_user_id", "mimic_target_user_name", "mimic_profile_text"],
  };

  const RESOURCE_TYPE_META = {
    "keyword-replies": { property: "keyword_replies", category: "automation", response: "keyword_reply", label: "关键词回复" },
    "scheduled-messages": { property: "scheduled_messages", category: "automation", response: "scheduled_message", label: "定时消息" },
    rules: { property: "rules", category: "rules-memory", response: "rule", label: "群规" },
    memories: { property: "memories", category: "rules-memory", response: "memory", label: "永久记忆" },
    warnings: { property: "warnings", category: "member-lists", response: "", label: "警告记录" },
    bans: { property: "bans", category: "member-lists", response: "ban", label: "群内封禁" },
    "moderation-exemptions": { property: "exemptions", category: "member-lists", response: "exemption", label: "审核豁免" },
    "reply-mutes": { property: "reply_mutes", category: "member-lists", response: "reply_mute", label: "回复静默" },
  };

  // 运营任务状态面板：**只读**。后端是逐群鉴权的聚合端点，这里只负责展示
  // 「已实现的执行来源」与「可证明的证据」；外部 cron 的登记情况本页读不到，
  // 一律显示「外部计划未接入」，绝不用占位日期或 now+间隔伪造下一次执行。
  const OPERATIONS_SECTION_KEY = "operations-status";
  const OPERATIONS_TASK_META = {
    scheduled_messages: { label: "定时消息", icon: "timer" },
    group_summary: { label: "群摘要", icon: "notebook-text" },
    checkin_reminder: { label: "签到提醒", icon: "calendar-check" },
    activity_award: { label: "活跃奖励", icon: "award" },
    weekly_report: { label: "周报", icon: "file-bar-chart" },
    shop_expire: { label: "商店到期", icon: "shopping-bag" },
  };
  const OPERATIONS_SOURCE_LABEL = {
    in_process: "程序内常驻任务",
    external_cron_only: "外部 cron（登记情况本页不可见）",
  };
  const OPERATIONS_TRIGGER_LABEL = {
    wall_clock: "墙上时钟",
    event_driven: "事件驱动",
    external_cron: "外部计划",
  };
  // 后端的 reason 是稳定标识；用户看到的是这句中文。
  const OPERATIONS_REASON_LABEL = {
    not_recorded: "未记录",
    no_delivery_evidence: "没有投递证据",
    delivery_time_not_recorded: "有投递记录，但没有投递时刻",
    placeholder_only: "只有占位记录，没有投递证据",
    no_success_ledger: "没有成功台账（处理完即删行）",
    failure_time_not_recorded: "有失败记录，但没有失败时刻",
    current_backoff_state_not_full_history: "这是当前退避状态，不是完整失败史",
    retry_scheduled_backoff_not_completed: "已安排退避重试，尚未完成",
    event_driven: "事件驱动，没有固定时刻",
    runtime_not_observed: "未观测到本轮的运行时刻",
    no_persisted_clock: "没有可持久化的时钟锚",
    due_now_pending_delivery: "已到期，等待投递",
    planned_by_scheduler: "已按调度口径推算",
    disabled: "已停用",
    not_configured: "未配置",
    invalid_schedule_time: "时间格式无效",
    external_schedule_unknown: "外部计划未接入",
  };
  // last_failure.count 的单位随任务不同而不同，必须照抄后端标注，绝不默认「失败 N 次」。
  const OPERATIONS_COUNT_MEANING_LABEL = {
    scheduled_messages_pending_retry_occurrences: "待重试记录",
    group_summary_cumulative_failures: "累计失败次数",
  };
  // 重试队列的三个状态由后端判定（前端不本地算时间）：任何取值都只说明「已安排、尚未完成」。
  const OPERATIONS_RETRY_STATE_LABEL = {
    retry_due: "已到期，等待执行",
    retry_in_flight: "已有记录在处理中（持租约），尚未完成",
  };
  // B13b：执行来源/计划的稳定标识 → 中文（不认识的取值一律原样显示，不猜）。
  const OPERATIONS_POLICY_SOURCE_LABEL = {
    internal: "机器人进程内执行",
    external: "外部入口（宿主 cron）",
  };
  const OPERATIONS_WEEKDAY_LABEL = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
  const OPERATIONS_RUN_STATUS_LABEL = {
    accepted: "已受理",
    running: "执行中",
    succeeded: "已完成",
    partial: "部分完成",
    failed: "失败",
    cancelled: "已取消",
    unknown: "结果未知",
    no_work: "没有可做的事",
  };
  const OPERATIONS_ITEM_STATUS_LABEL = {
    succeeded: "已完成",
    skipped: "未选中、未执行",
    failed: "失败",
    unknown: "结果未知（不重试）",
    cancelled: "已取消",
  };
  const OPERATIONS_RUN_REASON_LABEL = {
    guard_blocked: "写边界复核未通过（授权被撤 / 配置已改 / 已取消）",
    cancel_requested: "已请求取消",
    source_changed: "源数据在确认后变了",
    not_selected: "不在本次确认选中的目标里",
    already_delivered: "此前已经投递过",
    unknown_result: "外部结果未知",
    token_invalid: "令牌无效",
    token_expired: "令牌过期",
    actor_mismatch: "令牌不是这个账号的",
    group_mismatch: "令牌不是本群的",
    task_mismatch: "令牌不是这个任务的",
    not_due: "还没到执行时刻",
    no_work: "没有可做的事",
    plan_changed: "方案在确认后变了",
  };
  // 每个任务的参数输入：只有这几类，且都不预填「看起来已启用」的值。
  const OPERATIONS_PARAM_FIELDS = {
    scheduled_messages: [{ name: "entry_ids", label: "定时消息 ID（逗号分隔）", type: "text", placeholder: "例如 12,13" }],
    group_summary: [{ name: "action", label: "动作", type: "select", options: [["generate", "生成新摘要"], ["retry", "重试失败队列"]] }],
    checkin_reminder: [{ name: "slot", label: "时段（可选，0–23，留空=当天已到点且生效的时段）", type: "text", placeholder: "例如 15" }],
    activity_award: [{ name: "week_start", label: "结算周（周一，只允许上一个完整周）", type: "text", placeholder: "YYYY-MM-DD" }],
    weekly_report: [{ name: "days", label: "统计天数", type: "text", placeholder: "7" }],
    shop_expire: [{ name: "limit", label: "本批最多处理条数", type: "text", placeholder: "50" }],
  };
  const OPERATIONS_TERMINAL_RUN_STATUS = new Set(["succeeded", "partial", "failed", "cancelled", "no_work"]);

  const ROLE_META = {
    main: { label: "主模型", icon: "message-square", parent: "", embed: false, deadlineDefault: 120 },
    vision: { label: "视觉模型", icon: "scan-eye", parent: "继承主模型", embed: false, deadlineDefault: 90 },
    decision: { label: "决策模型", icon: "route", parent: "继承主模型", embed: false, deadlineDefault: 35 },
    moderation: { label: "审核模型", icon: "shield-alert", parent: "继承决策模型", embed: false, deadlineDefault: 35 },
    compress: { label: "压缩模型", icon: "minimize-2", parent: "继承主模型", embed: false, deadlineDefault: 90 },
    embed: { label: "向量模型", icon: "binary", parent: "继承主模型", embed: true, deadlineDefault: 60 },
    // D3-42：skill 角色后端完整支持（rc 用 `models.skill or main` 建 settings.bot.skill_model，
    // __main__ 传给 llm.reconfigure(skill=...)），但 ROLE_META 里缺席 → 模型页、供应商校验、
    // 回退重命名里全都看不到它，运维只能手改 DB payload。deadlineDefault 对齐 llm.py 的 120 秒。
    skill: { label: "技能模型", icon: "sparkles", parent: "继承主模型", embed: false, deadlineDefault: 120 },
  };

  const PROMPT_META = {
    decision: "回复决策",
    moderation: "内容审核",
    casual: "日常对话",
    manage_intent: "管理意图",
    compress: "上下文压缩",
    skill_tools: "技能工具",
    sticker_decision: "贴纸决策",
    reply_mode: "回复模式",
    persona: "角色人格",
    proactive_topic: "主动话题",
    style_distill: "风格提炼",
    av_synopsis: "AV 题材概述",
  };

  const GROUP_EDITABLE_KEYS = new Set([
    "av_enabled",
    "mute_all_replies",
    "at_reply_mode",
    "join_verification_enabled",
    "join_verification_provider",
    "welcome_message",
    "welcome_disable_link_preview",
    "welcome_buttons",
    "default_permissions",
    "patrol_enabled",
    "raid_guard_enabled",
    "raid_guard_pin_message",
    "raid_guard_join_threshold",
    "raid_guard_window_seconds",
    "raid_guard_lockdown_seconds",
    "raid_guard_lookback_seconds",
    "raid_guard_challenge_timeout_seconds",
    "call_admin_enabled",
    "call_admin_pin_message",
    "call_admin_targets",
    "vote_ban_enabled",
    "vote_ban_pin_message",
    "vote_ban_threshold",
    "vote_ban_duration_seconds",
    "vote_ban_trigger_limit",
    "vote_ban_trigger_window_seconds",
    "tts_mode",
    "proactive_enabled",
    "proactive_task_brief",
    "mimic_target_user_id",
    "mimic_target_user_name",
    "mimic_profile_text",
    "api_model_query_enabled",
    "api_model_query_base_url",
    "api_model_query_http_timeout_sec",
    "api_model_query_check_timeout_sec",
  ]);

  const RAID_GUARD_GROUP_INT_FIELDS = [
    { key: "raid_guard_join_threshold", label: "触发阈值（人数）", min: 2, max: 1000 },
    { key: "raid_guard_window_seconds", label: "检测窗口（秒）", min: 5, max: 3600 },
    { key: "raid_guard_lockdown_seconds", label: "锁定时长（秒）", min: 60, max: 86400 },
    { key: "raid_guard_lookback_seconds", label: "追溯窗口（秒）", min: 0, max: 86400 },
    { key: "raid_guard_challenge_timeout_seconds", label: "质询超时（秒）", min: 60, max: 86400 },
  ];

  const VOTE_BAN_GROUP_INT_FIELDS = [
    { key: "vote_ban_threshold", label: "封禁票数阈值", min: 2, max: 1000 },
    { key: "vote_ban_duration_seconds", label: "投票有效期（秒）", min: 60, max: 86400 },
    { key: "vote_ban_trigger_limit", label: "单用户触发上限", min: 1, max: 1000 },
    { key: "vote_ban_trigger_window_seconds", label: "触发统计窗口（秒）", min: 60, max: 604800 },
  ];

  const TEMPLATE_BUTTON_STYLES = ["primary", "success", "danger"];

  const AUTO_DELETE_CATEGORY_META = [
    { key: "reply", label: "普通 AI 回复" },
    { key: "management", label: "命令与管理提示" },
    { key: "moderation", label: "审核通知" },
    { key: "media", label: "语音、音乐与贴纸" },
    { key: "proactive", label: "主动话题" },
    { key: "keyword", label: "关键词回复" },
    { key: "scheduled", label: "定时消息" },
    { key: "welcome", label: "入群欢迎" },
    { key: "call_admin", label: "呼叫管理员" },
    { key: "vote", label: "民主投票" },
  ];

  const PERMISSION_FIELD_FALLBACK = [
    ["can_send_messages", "发送文字消息"],
    ["can_send_audios", "发送音频"],
    ["can_send_documents", "发送文件"],
    ["can_send_photos", "发送图片"],
    ["can_send_videos", "发送视频"],
    ["can_send_video_notes", "发送视频消息"],
    ["can_send_voice_notes", "发送语音消息"],
    ["can_send_polls", "发送投票"],
    ["can_send_other_messages", "发送贴纸/动画/游戏"],
    ["can_add_web_page_previews", "添加链接预览"],
    ["can_react_to_messages", "添加消息反应"],
    ["can_edit_tag", "编辑成员标签"],
    ["can_change_info", "修改群组信息"],
    ["can_invite_users", "邀请用户"],
    ["can_pin_messages", "置顶消息"],
    ["can_manage_topics", "管理话题"],
  ].map(([key, label]) => ({ key, label }));

  const state = {
    onboardingPreset: "",
    applicantOnly: false,
    selectedPreset: new Map(),
    presetPreview: new Map(),
    session: null,
    activeTab: "overview",
    document: null,
    config: null,
    baseline: null,
    configuredSecrets: new Set(),
    secretChanges: {},
    groups: [],
    groupBaselines: new Map(),
    groupsError: "",
    loading: true,
    saving: false,
    reloadingGroups: false,
    accessMutating: false,
    immediateMutations: 0,
    groupSaving: new Set(),
    groupResources: new Map(),
    groupResourceLoads: new Map(),
    groupResourceMutationEpochs: new Map(),
    groupPermissionLoads: new Map(),
    groupTelegramAdmins: new Map(),
    groupTemplateButtonDrafts: new Map(),
    groupApiModelQuerySecretChanges: new Map(),
    groupCardOpen: new Map(),
    groupSectionOpen: new Map(),
    // 只读「运营任务」面板：逐群各存各的，迟到的响应靠 groupOperationsLoads 里的
    // 令牌自己走掉（A 群的响应绝不会画到 B 群的卡片上）。
    groupOperations: new Map(),
    groupOperationsLoads: new Map(),
    // B13b：按群执行来源 / 周期计划（读 + 预览令牌 + 审计分页）。
    groupPolicy: new Map(),
    groupPolicyLoads: new Map(),
    // B13b：六类任务的工作流（预览草稿 / 令牌 / 当前 run / 轮询令牌）逐群各存各的；
    // 令牌是 Symbol，换群或权限变化后旧响应一律自己走掉。
    groupOperationsWork: new Map(),
    groupOperationsPoll: new Map(),
    advancedOpen: new Map(),
    resourceFormDrafts: new Map(),
    resourceFormBaselines: new Map(),
    pendingResourceCreates: new Map(),
    pendingResourceDeletes: new Map(),
    resourceDraftSequence: 0,
    permissionFields: PERMISSION_FIELD_FALLBACK,
    access: null,
    accessLoadToken: null,
    accessAdminGroup: null,
    accessSearches: {
      "global-bans": "",
      "global-exemptions": "",
    },
    promptKey: "decision",
    listPages: new Map(),
    groupSearch: "",
    // 保存前差异预览（全局配置）。默认走预览；skipPreview 是管理员显式关掉的
    // 逃生口，只在当前页面生命周期内有效，刷新即恢复。
    savePreview: null,
    skipSavePreview: false,
    replay: {
      groupId: null,
      loading: false,
      loadedGroupId: null,
      // 逐群面板可能同时展开多张卡片：靠它让迟到的响应自己走掉。
      loadToken: 0,
      // 详情/轮询另有自己的令牌：换群之后，A 群的 in-flight 响应绝不能画到 B 群上。
      detailToken: 0,
      running: false,
      showExpired: false,
      samples: [],
      ttlDays: 0,
      rules: [],
      useBaselinePrompt: true,
      prompt: "",
      runs: [],
      runId: null,
      detail: null,
      detailLoading: false,
      // 后台执行的回放**不在一个请求里跑完**：登记之后由轮询盯着。tracked 活在
      // state 里，所以离开页面再回来不会丢；pollTimer/pollToken 只在视图还在时存在。
      tracked: null,
      pollTimer: null,
      pollToken: 0,
      polling: false,
      lastBounds: null,
      lastCapacity: null,
      // idempotency_key 绑定「群 + 候选」：失败重试复用同一个 key，换群必须换 key。
      pendingKey: "",
      pendingKeyGroupId: "",
      pendingCandidateJson: "",
    },
    configHistory: {
      loading: false,
      entries: [],
      // 真分页：page 是服务端的翻页契约（limit / before / has_more / next_before）。
      page: null,
      before: null,
      limit: 20,
      expanded: null,
      detail: null,
      detailLoading: false,
      against: null,
      compareRevision: null,
      compare: null,
      compareLoading: false,
      restore: null,
      restoring: false,
      conflict: false,
    },
  };

  const LIST_PAGE_SIZE = 8;
  const RESOURCE_DRAFT_FORM_SELECTOR = [
    "[data-entry-edit-form]",
    "[data-rule-edit-form]",
    "[data-memory-edit-form]",
    '[data-resource-form][data-save-scope="deferred"]',
  ].join(", ");

  function paginate(items, key) {
    const total = items.length;
    const pages = Math.max(1, Math.ceil(total / LIST_PAGE_SIZE));
    const current = Math.min(Math.max(1, state.listPages.get(key) || 1), pages);
    const start = (current - 1) * LIST_PAGE_SIZE;
    return { slice: items.slice(start, start + LIST_PAGE_SIZE), current, pages, total };
  }

  function pagerMarkup(key, current, pages, total) {
    if (pages <= 1) return "";
    return `
      <div class="list-pager">
        <button class="mini-icon-button" type="button" data-action="list-page" data-list-key="${attr(key)}" data-page="${current - 1}"${current <= 1 ? " disabled" : ""} aria-label="上一页" title="上一页">${icon("chevron-left")}</button>
        <span>${current} / ${pages} 页 · 共 ${total} 条</span>
        <button class="mini-icon-button" type="button" data-action="list-page" data-list-key="${attr(key)}" data-page="${current + 1}"${current >= pages ? " disabled" : ""} aria-label="下一页" title="下一页">${icon("chevron-right")}</button>
      </div>`;
  }

  function paginatedRows(items, key, renderRow, emptyText = "暂无记录") {
    if (!items.length) return `<p class="field-hint">${escapeHtml(emptyText)}</p>`;
    const page = paginate(items, key);
    return page.slice.map(renderRow).join("") + pagerMarkup(key, page.current, page.pages, page.total);
  }

  // ---- 新群接入 / 按群成本 ----------------------------------------
  // 申请人（还没有任何服务权限）也能进来，但只看到申请页：看不到全局
  // settings，也看不到任何别的群。
  const ONBOARDING_PRESETS = [
    { id: "moderation_only", title: "审核型", hint: "静默审核与处置，不主动发言" },
    { id: "companion", title: "陪伴型", hint: "可回复，默认只在被 @ 时应答" },
    { id: "community_ops", title: "社区运营型", hint: "可回复 + 运营功能说明" },
  ];

  const COST_PURPOSE_LABELS = {
    safety: "安全审核",
    optional: "可选功能",
    background_optional: "后台可选",
  };

  const REQUEST_STATUS_LABELS = {
    pending: "待审理",
    approved: "已开通",
    rejected: "已拒绝",
    cancelled: "已撤回",
  };

  function formatMicro(amount, currency) {
    if (amount == null) return "金额未知（缺少单价）";
    const unit = String(currency || "").trim();
    if (!unit) return "金额未知（缺少币种）";
    return `${(Number(amount) / 1_000_000).toFixed(6)} ${unit}`;
  }

  function formatTokens(value) {
    if (value == null) return "未知";
    return Number(value).toLocaleString("zh-CN");
  }

  // 金额列：**已计价部分**与未知分开写。未知绝不显示成 0 元 —— 一次"缺价"
  // 的调用和一次"确认免费"的调用，在界面上必须是两种东西。
  function costRowAmountLabel(row) {
    const amounts = Array.isArray(row?.amounts) ? row.amounts : [];
    const parts = [];
    for (const entry of amounts) {
      const currency = String(entry?.currency || "");
      const version = Number(entry?.price_version || 0);
      const priced = Number(entry?.priced_attempts || 0);
      const unknown = Number(entry?.unknown_attempts || 0);
      if (currency && (priced > 0 || Number(entry?.amount_micro || 0) > 0)) {
        parts.push(`${formatMicro(entry.amount_micro, currency)}（单价 v${version}）`);
      }
      if (unknown > 0) {
        const why = entry?.has_price_entry
          ? (Number(entry?.unknown_usage_attempts || 0) > 0 ? "用量缺失或坏值" : "用到的用量段没有单价")
          : "发送时没有该模型的价格条目";
        parts.push(currency
          ? `${currency} 有 ${unknown} 次金额未知（${why}）`
          : `${unknown} 次金额未知（${why}，不折算任何货币）`);
      }
    }
    const unpriced = Number(row?.unpriced_attempts || 0);
    if (unpriced > 0) {
      parts.push(`${unpriced} 次未计价（早于单价冻结，无法回溯）`);
    }
    if (!parts.length) return Number(row?.attempts || 0) > 0 ? "未计价" : "—";
    return parts.join("；");
  }

  // 按币种的金额总览。金额按**发出时冻结**的单价版本计算：货币之间不相加
  // （没有汇率），未知尝试也不被当成 0 —— 不完整就明说"这是已计价部分"。
  function costAmountsView(payload) {
    const currencies = Array.isArray(payload?.currencies) ? payload.currencies : [];
    const complete = payload?.amount_complete === true;
    const items = [];
    for (const bucket of currencies) {
      const currency = String(bucket?.currency || "");
      const unknown = Number(bucket?.unknown_attempts || 0);
      const legacy = Number(bucket?.legacy_attempts || 0);
      if (currency) {
        const versions = (Array.isArray(bucket?.price_versions) ? bucket.price_versions : [])
          .filter(item => Number(item?.priced_attempts || 0) > 0)
          .map(item => `v${Number(item?.price_version || 0)} ${formatMicro(item.amount_micro, currency)}`);
        items.push(`
          <li data-currency="${attr(currency)}">
            <strong>${escapeHtml(currency)}</strong>
            已计价 ${escapeHtml(formatMicro(bucket?.amount_micro, currency))}
            · ${Number(bucket?.priced_attempts || 0)} 次
            ${versions.length ? `（单价版本：${escapeHtml(versions.join("；"))}）` : ""}
            ${unknown ? `<span class="cost-amount-unknown">另有 ${unknown} 次金额未知</span>` : ""}
            ${legacy ? `<span class="cost-amount-unknown">${legacy} 次早于单价冻结，无法回溯计价</span>` : ""}
            ${bucket?.complete ? "" : `<span class="cost-amount-unknown">该币种金额不完整</span>`}
          </li>`);
      } else {
        items.push(`
          <li data-currency="">
            <strong>币种未知</strong>
            ${unknown + legacy} 次未计价（其中旧数据 ${legacy} 次）
            <span class="cost-amount-unknown">不折算成任何货币</span>
          </li>`);
      }
    }
    return `
      <div class="cost-amounts" data-role="cost-amounts"
           data-amount-complete="${complete ? "true" : "false"}">
        <h4>金额（估算）</h4>
        ${items.length
          ? `<ul class="cost-amount-list">${items.join("")}</ul>`
          : `<p class="field-hint">所选窗口内没有可计价的金额。</p>`}
        <p class="field-hint">
          ${complete
            ? "该窗口内每一次尝试都有价格与用量，金额是完整的估算值。"
            : "⚠ 金额不完整：有尝试缺价格或缺用量，上面是**已计价部分**，不是本群总成本。"}
          金额按发出时冻结的单价版本计算，改价不追改历史；这是估算，不是供应商结算账单。
        </p>
      </div>`;
  }

  function applicantSessionView(session) {
    return `
      <section class="onboarding-view" data-view="onboarding">
        <header class="panel">
          <h2>接入新群</h2>
          <p class="panel-hint">提交后由最高管理员审理。提交申请<strong>不等于</strong>开通。</p>
        </header>
        <section class="panel">
          <label class="field-label" for="onboarding-group-id">群 ID</label>
          <input class="text-input" id="onboarding-group-id" type="text" inputmode="numeric"
                 placeholder="-1001234567890" autocomplete="off">
          <p class="field-error" data-role="onboarding-error" hidden></p>
          <div class="preset-grid" role="radiogroup" aria-label="首次预设">
            ${ONBOARDING_PRESETS.map(item => `
              <button class="preset-card" type="button" role="radio" aria-checked="false"
                      data-action="onboarding-preset" data-preset="${attr(item.id)}">
                <strong>${escapeHtml(item.title)}</strong>
                <span>${escapeHtml(item.hint)}</span>
              </button>`).join("")}
          </div>
          <div class="button-row">
            <button class="secondary-button" type="button" data-action="onboarding-check">
              ${icon("stethoscope")}检查就绪情况
            </button>
            <button class="primary-button" type="button" data-action="onboarding-submit">
              ${icon("send")}提交接入申请
            </button>
          </div>
          <div class="readiness-report" data-role="onboarding-check" hidden></div>
        </section>
        <section class="panel">
          <h3>我的申请</h3>
          <div data-role="onboarding-list"></div>
        </section>
        ${state.session?.can_manage_global ? `
        <section class="panel" data-role="onboarding-queue-panel">
          <header class="panel-header">
            <h3>待审批队列</h3>
            <button class="secondary-button" type="button" data-action="onboarding-queue-refresh">
              ${icon("refresh-cw")}刷新
            </button>
          </header>
          <p class="panel-hint">
            只有最高管理员看得到全局队列；申请人自己的列表在上一节。
            批准<strong>不等于</strong>机器人已在群里 —— 以结算后的真实读回为准。
          </p>
          <div class="queue-live" data-role="onboarding-queue-status" role="status"
               aria-live="polite"></div>
          <div data-role="onboarding-queue" aria-label="待审批申请列表"></div>
        </section>` : ""}
      </section>`;
  }

  function costPanelView(group) {
    return `
      <section class="panel cost-panel" data-group-id="${attr(group.id)}">
        <header class="panel-header">
          <h3>${icon("coins")}本群用量与预算</h3>
          <button class="secondary-button" type="button" data-action="cost-reload" data-group-id="${attr(group.id)}">
            ${icon("refresh-cw")}刷新
          </button>
        </header>
        <div data-role="cost-freshness"></div>
        <div data-role="cost-summary"></div>
        <div data-role="cost-budget"></div>
      </section>`;
  }

  async function loadOnboardingSession() {
    const result = await apiFetch("/api/v1/onboarding/session");
    return result.session;
  }

  async function loadOnboardingRequests(session) {
    const result = await apiFetch("/api/v1/onboarding/requests?limit=20");
    const list = content.querySelector('[data-role="onboarding-list"]');
    if (!list) return;
    const rows = Array.isArray(result.requests) ? result.requests : [];
    if (!rows.length) {
      list.innerHTML = `<p class="empty-state">还没有提交过申请。</p>`;
      return;
    }
    list.innerHTML = rows.map(row => `
      <div class="request-row" data-request-id="${attr(row.request_id)}">
        <div>
          <strong>群 ${escapeHtml(String(row.group_id))}</strong>
          <span class="badge">${escapeHtml(REQUEST_STATUS_LABELS[row.status] || row.status)}</span>
        </div>
        <div class="request-meta">
          <span>${escapeHtml(String(row.created_at || ""))}</span>
          ${row.status === "pending" ? `
            <button class="secondary-button" type="button" data-action="onboarding-cancel"
                    data-request-id="${attr(row.request_id)}">撤回</button>` : ""}
        </div>
      </div>`).join("");
  }

  // ---- 超管审批队列 ----------------------------------------------------
  //
  // 安全边界：只有 can_manage_global 才会去调这一节；普通申请人/群管理员根本
  // 不会发这个请求 —— 不靠"先 403 再隐藏"来证明安全。后端仍然按会话身份判权，
  // 前端这个判断只是"不去问不需要问的东西"。

  const QUEUE_PAGE_SIZE = 50;

  function queueState() {
    if (!(state.decisionDrafts instanceof Map)) state.decisionDrafts = new Map();
    if (!(state.decisionErrors instanceof Map)) state.decisionErrors = new Map();
    if (!(state.decisionsInFlight instanceof Map)) state.decisionsInFlight = new Map();
    // 「POST 已经被接受，但还没从服务器读回终结状态」的申请。
    // POST 成功只是**写被接受**，不等于这次决策生效了；在读回确认之前，
    // 这张卡片不能再决策，也不能显示成功。
    if (!(state.decisionsPendingConfirmation instanceof Map)) {
      state.decisionsPendingConfirmation = new Map();
    }
    if (!Array.isArray(state.pendingQueue)) state.pendingQueue = [];
    if (typeof state.queueTotal !== "number") state.queueTotal = 0;
    if (typeof state.queueOffset !== "number") state.queueOffset = 0;
    if (typeof state.queueSeq !== "number") state.queueSeq = 0;
    if (typeof state.queueStale !== "boolean") state.queueStale = false;
    if (typeof state.queueFailureMessage !== "string") state.queueFailureMessage = "";
    return state;
  }

  /** 这张卡片的决策按钮是否必须禁用。
   *
   * ``renderPendingQueue`` 与 ``setDecisionBusy`` 必须共用**同一个**谓词 ——
   * 否则"退出 in-flight"时的解锁会把渲染阶段因为"列表已过期"或"已提交待确认"
   * 而设的 disabled 直接抹掉：界面文案写着"决策已禁用"，按钮却是可点的。 */
  function decisionBlocked(requestId) {
    queueState();
    return state.decisionsInFlight.has(requestId)
      || state.queueStale
      || state.decisionsPendingConfirmation.has(requestId);
  }

  function queueDraftFor(requestId) {
    queueState();
    return state.decisionDrafts.get(requestId) || "";
  }

  function rememberQueueDraft(requestId, value) {
    queueState();
    // 草稿按 request_id 隔离：切申请/切页不会把 A 的理由写到 B。
    state.decisionDrafts.set(requestId, value);
  }

  function forgetQueueDraft(requestId) {
    queueState();
    state.decisionDrafts.delete(requestId);
  }

  function queueErrorFor(requestId) {
    queueState();
    return state.decisionErrors.get(requestId) || "";
  }

  function setQueueError(requestId, message) {
    queueState();
    if (message) state.decisionErrors.set(requestId, message);
    else state.decisionErrors.delete(requestId);
  }

  /** 拉一次待审批队列，并**显式**返回这次读取的结果。
   *
   * 返回 ``{ok: true, rows, total, offset}`` 或
   * ``{ok: false, reason: "absent" | "superseded" | "failed", error}``。
   *
   * 为什么必须显式返回：调用方（决策后的核对）要能区分"真的读到了服务器状态"
   * 和"读取失败/这次结果已被更新的请求淘汰"。以前这条函数把失败吞在内部、
   * catch 之后只 return，调用方的 try/catch 永远接不到失败，于是"读失败"被
   * 当成"核对通过"，理由草稿被清掉、还弹了成功提示。
   */
  async function loadOnboardingQueue({ append = false } = {}) {
    // 只给最高管理员拉全局队列。角色不是超管时**一个请求都不发**。
    if (!state.session?.can_manage_global) return { ok: false, reason: "absent" };
    const box = content.querySelector('[data-role="onboarding-queue"]');
    const status = content.querySelector('[data-role="onboarding-queue-status"]');
    // 面板不在当前视图里：这**不是**一次成功读取，绝不能当作核对通过。
    if (!box) return { ok: false, reason: "absent" };
    queueState();
    const offset = append ? state.queueOffset : 0;
    // 递增序号：异步响应回来时如果不是最新一次，就整段丢弃 ——
    // 否则"旧的失败/旧的列表"会覆盖用户已经切过去的视图。
    const seq = ++state.queueSeq;
    if (status && !append) status.textContent = "正在读取待审批申请…";
    let result;
    try {
      result = await apiFetch(
        `/api/v1/onboarding/requests?status=pending&limit=${QUEUE_PAGE_SIZE}&offset=${offset}`,
      );
    } catch (error) {
      if (seq !== state.queueSeq) return { ok: false, reason: "superseded" };
      // 读失败**不等于**"没有待审批"：把上一次已知的列表保留在原处（连带用户
      // 正在写的理由），只把它标记成过期、禁用决策按钮，并明说读取失败。
      // 直接清空 DOM 会把草稿从界面上抹掉，用户以为自己的字丢了。
      state.queueStale = true;
      state.queueFailureMessage = error.message || "未知错误";
      if (state.pendingQueue.length) {
        renderPendingQueue(state.pendingQueue, status, {
          failure: state.queueFailureMessage,
        });
      } else if (status) {
        status.textContent =
          `队列读取失败：${error.message || "未知错误"}。可以点刷新重试。`;
      }
      return { ok: false, reason: "failed", error };
    }
    if (seq !== state.queueSeq) return { ok: false, reason: "superseded" };
    state.queueStale = false;
    state.queueFailureMessage = "";
    const rows = Array.isArray(result.requests) ? result.requests : [];
    state.queueTotal = Number.isFinite(Number(result.total))
      ? Number(result.total)
      : rows.length;
    state.pendingQueue = append ? state.pendingQueue.concat(rows) : rows;
    state.queueOffset = state.pendingQueue.length;
    renderPendingQueue(state.pendingQueue, status);
    return {
      ok: true,
      rows: state.pendingQueue.slice(),
      total: state.queueTotal,
      offset,
    };
  }

  function renderQueueStatus(status) {
    if (!status) return;
    queueState();
    const shown = state.pendingQueue.length;
    const total = state.queueTotal;
    const truncated = shown < total;
    status.textContent = truncated
      ? `已显示 ${shown} / ${total} 条待审批（还有 ${total - shown} 条未加载）。`
      : `队列已刷新：${shown} 条待审批，共 ${total} 条。`;
  }

  /** 读失败时用的状态文案：明确说"这是上一次读到的旧数据、决策已禁用"。 */
  function renderQueueFailureStatus(status, message) {
    if (!status) return;
    queueState();
    status.textContent =
      `队列读取失败：${message}。下面是上一次读到的 ${state.pendingQueue.length} 条`
      + `（可能已过期，决策已禁用）。请点「刷新」重试，不要凭这份旧数据决策。`;
  }

  function renderPendingQueue(rows, status, { failure = "" } = {}) {
    const box = content.querySelector('[data-role="onboarding-queue"]');
    if (!box) return;
    queueState();
    if (!rows.length) {
      box.innerHTML = `<p class="empty-state">${
        failure ? "读取失败，且此前没有读到过任何待审批申请。" : "当前没有待审批的申请。"
      }</p>`;
      if (failure) renderQueueFailureStatus(status, failure);
      else renderQueueStatus(status);
      return;
    }
    box.innerHTML = rows.map(row => {
      const requestId = String(row.request_id || "");
      const pending = String(row.status || "") === "pending";
      const draft = queueDraftFor(requestId);
      const error = queueErrorFor(requestId);
      const busy = state.decisionsInFlight.has(requestId);
      const awaitingConfirmation = state.decisionsPendingConfirmation.has(requestId);
      // 列表过期、本次请求在飞、或者上一次决策还没读回确认 → 都禁止决策
      // （防止凭旧数据重复提交）。但**不禁用输入框**：用户正在写的理由必须
      // 还能继续编辑、也不会被重绘抹掉。
      const blocksDecision = decisionBlocked(requestId);
      const disabled = blocksDecision ? " disabled" : "";
      const inputDisabled = busy ? " disabled" : "";
      return `
      <article class="request-card" data-request-id="${attr(requestId)}">
        <header class="request-head">
          <strong class="request-group">群 ${escapeHtml(String(row.group_id ?? ""))}</strong>
          <span class="badge">${escapeHtml(
            REQUEST_STATUS_LABELS[row.status] || String(row.status || ""),
          )}</span>
        </header>
        <dl class="request-facts">
          <dt>申请人</dt><dd>${escapeHtml(String(row.applicant_id ?? ""))}</dd>
          <dt>申请时间</dt><dd>${escapeHtml(String(row.created_at || ""))}</dd>
        </dl>
        ${row.reason
          ? `<p class="request-reason"><span class="request-reason-label">审核理由</span>${
              escapeHtml(String(row.reason))
            }</p>`
          : ""}
        ${pending ? `
        <label class="field-label" for="decision-reason-${attr(requestId)}">
          审核理由（可选）
        </label>
        <input class="text-input" id="decision-reason-${attr(requestId)}" type="text"
               maxlength="200" autocomplete="off"
               data-role="decision-reason" data-request-id="${attr(requestId)}"
               value="${attr(draft)}" placeholder="写给申请人看的简短说明"${inputDisabled}>
        <p class="field-error" data-role="decision-error" data-request-id="${attr(requestId)}"
           role="alert" ${error ? "" : "hidden"}>${escapeHtml(error)}</p>
        <p class="panel-hint" data-role="decision-pending-confirmation"
           data-request-id="${attr(requestId)}"
           ${awaitingConfirmation ? "" : "hidden"}>操作已提交，状态<strong>尚未确认</strong>：
          请点「刷新」核对服务器上的结果，不要再重复决策。</p>
        <div class="button-row">
          <button class="primary-button" type="button" data-action="onboarding-approve"
                  data-request-id="${attr(requestId)}"${disabled}>${icon("check")}批准</button>
          <button class="secondary-button" type="button" data-action="onboarding-reject"
                  data-request-id="${attr(requestId)}"${disabled}>${icon("x")}拒绝</button>
        </div>` : `
        <p class="panel-hint">这条申请已经终结，不能再决策。</p>`}
      </article>`;
    }).join("");
    // 超过一页时给出真实的"加载更多"入口，而不是假装这就是全部。
    if (state.queueOffset < state.queueTotal) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "secondary-button";
      more.dataset.action = "onboarding-queue-more";
      more.textContent = `加载更多（还有 ${state.queueTotal - state.queueOffset} 条）`;
      box.appendChild(more);
    }
    if (failure) renderQueueFailureStatus(status, failure);
    else renderQueueStatus(status);
  }

  function decisionError(requestId, message) {
    // 错误状态存进 state，重绘之后仍然在（以前只写 DOM，重建卡片就丢了）。
    setQueueError(requestId, message);
    const box = content.querySelector(
      `[data-role="decision-error"][data-request-id="${cssEscape(requestId)}"]`,
    );
    if (!box) return;
    box.hidden = !message;
    box.textContent = message || "";
  }

  function decisionOutcomeText(payload) {
    // 语义要分开说清楚，别把"重放一条旧批准"讲成"刚刚新授了权"。
    const parts = [];
    if (payload.already_decided) parts.push("这条申请此前已决策过");
    if (payload.group_authorized === false && payload.already_decided) {
      parts.push("本次没有新增群授权");
    }
    if (payload.applicant_granted === false && payload.already_decided) {
      parts.push("本次没有新增申请人授权");
    }
    const current = payload.current || {};
    parts.push(
      `授权状态：群授权 ${current.authorized ? "在" : "不在"}；`
      + `机器人是否已在群 ${current.bot_present ? "是" : "否"}`
      + `（已授权 ≠ 已加入）；`
      + `申请人仍是管理员 ${current.applicant_admin ? "是" : "否"}`,
    );
    return parts.join("；");
  }

  async function decideOnboardingRequest(requestId, decision) {
    queueState();
    // 锁按 **requestId** 一把：批准与拒绝共用同一条锁，卡片重绘也绕不过去
    // （重绘时会按这个锁禁用输入与按钮），所以不会产生第二次 POST。
    if (state.decisionsInFlight.has(requestId)) return;
    // 上一次决策已经提交、但还没从服务器读回终结状态：**不再发第二次 POST**。
    // 重复点批准/拒绝会重复产生后台动作，用户该做的是刷新核对。
    if (state.decisionsPendingConfirmation.has(requestId)) {
      decisionError(
        requestId,
        "这条申请上一次的决策已经提交，但状态还没确认：请点「刷新」核对服务器上"
        + "的结果，在此之前不要再重复决策。审核理由已保留。",
      );
      return;
    }
    const input = content.querySelector(
      `[data-role="decision-reason"][data-request-id="${cssEscape(requestId)}"]`,
    );
    const reason = String(input?.value || "").trim();
    rememberQueueDraft(requestId, reason);
    state.decisionsInFlight.set(requestId, decision);
    setQueueError(requestId, "");
    setDecisionBusy(requestId, true);
    try {
      const payload = await apiFetch(
        `/api/v1/onboarding/requests/${encodeURIComponent(requestId)}/${decision}`,
        { method: "POST", body: JSON.stringify(reason ? { reason } : {}) },
      );
      // POST 返回成功只说明**写被服务端接受了**，不等于这次决策已经生效并落库。
      // 先记成"已提交、待确认"，等真的从服务器读回终结状态再清草稿、再报成功
      // （以前在 POST 之后立刻清草稿 + 弹成功，紧接着 GET 失败，理由就没了）。
      state.decisionsPendingConfirmation.set(requestId, {
        decision,
        reason,
        at: Date.now(),
        // 记住服务端这次的应答：确认之后要用它说明"授权/加入"的真实区别，
        // 光凭字符串 decision 拼不出这个语义。
        payload,
      });
      setQueueError(requestId, "");
      const suffix = decision === "approve" ? decisionOutcomeText(payload) : "";
      const confirmation = await refreshQueueAfterDecision(requestId, { decision });
      if (confirmation.confirmed) {
        forgetQueueDraft(requestId);
        state.decisionsPendingConfirmation.delete(requestId);
        setQueueError(requestId, "");
        showToast(
          `${decision === "approve" ? "已批准" : "已拒绝"}，并已从服务器读回确认。${suffix}`,
          "success",
        );
      } else {
        // 读回失败、没翻到终结状态、或这次结果已被更新的请求淘汰：**不能**报成功。
        decisionError(
          requestId,
          `操作已提交（服务端返回成功），但状态尚未确认：${confirmation.message}。`
          + "审核理由已保留；请点「刷新」核对结果，在此之前不要重复决策。",
        );
        showToast("已提交，但状态未确认", "warning");
      }
    } catch (error) {
      const detail = error?.details?.reason || error?.code || "";
      decisionError(
        requestId,
        `${error.message || "决策失败"}${detail ? `（${detail}）` : ""}`
        + `，理由已保留，可重试。`,
      );
      // 状态可能已被别的会话改掉：读回一次，但**保留**刚写的错误（错误存在
      // state 里，重绘会把它渲染回来，不会再被建卡片冲掉）。
      state.decisionsInFlight.delete(requestId);
      await refreshQueueAfterDecision(requestId, { keepErrors: true });
      return;
    } finally {
      // 只有"确认完成"（成功或已明确失败）之后才解锁。
      if (state.decisionsInFlight.get(requestId) === decision) {
        state.decisionsInFlight.delete(requestId);
      }
      setDecisionBusy(requestId, false);
    }
  }

  function setDecisionBusy(requestId, busy) {
    const nodes = content.querySelectorAll(
      `[data-request-id="${cssEscape(requestId)}"]`,
    );
    nodes.forEach(node => {
      if (node.matches("button")) {
        // busy=false 时**不能**无条件把 disabled 设回 false：列表过期、或这次
        // 决策还等着读回确认时，按钮必须保持禁用 —— 否则界面文案写着"决策已
        // 禁用"，按钮却是可点的（证据：approve/reject.disabled 都是 false）。
        node.disabled = busy ? true : decisionBlocked(requestId);
      } else if (node.matches("input")) {
        node.disabled = Boolean(busy);
      }
    });
    if (!busy) {
      const note = content.querySelector(
        `[data-role="decision-pending-confirmation"][data-request-id="${cssEscape(requestId)}"]`,
      );
      if (note) note.hidden = !state.decisionsPendingConfirmation.has(requestId);
    }
  }

  // 核对"这次决策在服务器上是否真的终结"时最多翻几页（每页 QUEUE_PAGE_SIZE）。
  const QUEUE_CONFIRM_MAX_PAGES = 4;

  /** 用**真实列表接口**分页定位一条申请，返回它在服务器上的状态。
   *
   * 后端只有 ``GET /api/v1/onboarding/requests?status&limit&offset``（没有单条
   * GET 接口），所以只能翻页找；**翻不完就老实说"没确认"**，不编造一个不存在
   * 的单条读取接口。
   * 返回 ``{ok, found, row, total, exhausted}``；``ok:false`` = 这次读取本身失败。
   */
  async function findRequestStatus(
    requestId, status, { maxPages = QUEUE_CONFIRM_MAX_PAGES } = {},
  ) {
    const limit = QUEUE_PAGE_SIZE;
    for (let page = 0; page < maxPages; page += 1) {
      const offset = page * limit;
      const query =
        `limit=${limit}&offset=${offset}${status ? `&status=${encodeURIComponent(status)}` : ""}`;
      let result;
      try {
        result = await apiFetch(`/api/v1/onboarding/requests?${query}`);
      } catch (error) {
        return { ok: false, found: false, error };
      }
      const rows = Array.isArray(result.requests) ? result.requests : [];
      const total = Number.isFinite(Number(result.total))
        ? Number(result.total)
        : rows.length;
      const row = rows.find(item => String(item.request_id || "") === requestId);
      if (row) return { ok: true, found: true, row, total, exhausted: true };
      if (!rows.length || offset + rows.length >= total) {
        return { ok: true, found: false, total, exhausted: true };
      }
    }
    return { ok: true, found: false, total: null, exhausted: false };
  }

  /** 决策提交之后核对服务器上的**真实**结果。
   *
   * 返回 ``{confirmed, terminalStatus, message}``。
   * "确认"的定义：服务器上这条申请**已经不在待审批队列里**，并且在**期望的**
   * 终结状态列表里能查到它。仅仅"本地这一次列表里少了这一条"不算 —— 可能只是
   * 分页截断，也可能被别的会话改成了别的结果。
   * GET 失败、列表没翻完、面板不在、结果被更新的请求淘汰：一律**未确认**。
   */
  async function refreshQueueAfterDecision(
    requestId, { decision = "", keepErrors = false } = {},
  ) {
    // 真实读回：不靠"本地把卡片删掉"假装成功。
    const savedError = keepErrors ? queueErrorFor(requestId) : "";
    const restore = () => { if (savedError) setQueueError(requestId, savedError); };
    const queueOutcome = await loadOnboardingQueue();
    if (!queueOutcome.ok) {
      restore();
      return {
        confirmed: false,
        terminalStatus: "",
        message: queueOutcome.reason === "superseded"
          ? "队列视图已被另一次刷新取代，这次读回结果不构成确认"
          : queueOutcome.reason === "absent"
            ? "当前不在接入队列视图里，读不到服务器状态"
            : `读取失败：${queueOutcome.error?.message || "未知错误"}`,
      };
    }
    // 申请人自己的列表也顺手刷新一次；它失败**不影响**确认判定（确认只认服务器
    // 上的终结状态）。
    loadOnboardingRequests(state.session).catch(() => {});
    const stillPending = await findRequestStatus(requestId, "pending");
    if (!stillPending.ok) {
      restore();
      return {
        confirmed: false,
        terminalStatus: "",
        message: `读取失败：${stillPending.error?.message || "未知错误"}`,
      };
    }
    if (stillPending.found) {
      restore();
      return {
        confirmed: false,
        terminalStatus: "pending",
        message: "服务器上这条申请仍然是待审批状态",
      };
    }
    if (!stillPending.exhausted) {
      restore();
      return {
        confirmed: false,
        terminalStatus: "",
        message: "待审批列表还没翻完，无法确认这条申请的最终状态",
      };
    }
    if (!decision) {
      restore();
      return { confirmed: false, terminalStatus: "", message: "没有可核对的期望状态" };
    }
    const expected = decision === "approve" ? "approved" : decision === "reject" ? "rejected" : "";
    const label = REQUEST_STATUS_LABELS[expected] || expected;
    const terminal = await findRequestStatus(requestId, expected);
    if (!terminal.ok) {
      restore();
      return {
        confirmed: false,
        terminalStatus: "",
        message: `读取失败：${terminal.error?.message || "未知错误"}`,
      };
    }
    if (terminal.found) {
      restore();
      return { confirmed: true, terminalStatus: expected, message: "" };
    }
    restore();
    return {
      confirmed: false,
      terminalStatus: "",
      message: !terminal.exhausted
        ? `「${label}」列表还没翻完，无法确认`
        : `它既不在待审批队列里，也不在「${label}」列表里（可能已被其他会话改成别的结果）`,
    };
  }

  /** 把"已提交但还没读回确认"的决策追认到底。
   *
   * 用户离开面板再回来、或手动点「刷新」时都要能收口；但**只有**真的从服务器
   * 读回期望的终结状态才算确认 —— 读失败、列表没翻完就继续挂着"未确认"，
   * 理由也继续留在输入框里，绝不假装成功，也不重发决策。
   */
  async function resolvePendingConfirmations() {
    const pendingIds = [...state.decisionsPendingConfirmation.keys()];
    if (!pendingIds.length) return;
    for (const requestId of pendingIds) {
      const entry = state.decisionsPendingConfirmation.get(requestId);
      const confirmation = await refreshQueueAfterDecision(requestId, {
        decision: entry?.decision || "",
      });
      if (!confirmation.confirmed) continue;
      forgetQueueDraft(requestId);
      setQueueError(requestId, "");
      state.decisionsPendingConfirmation.delete(requestId);
      const suffix = entry?.decision === "approve" && entry?.payload
        ? decisionOutcomeText(entry.payload)
        : "";
      showToast(
        `${entry?.decision === "approve" ? "已批准" : "已拒绝"}`
        + `${confirmation.terminalStatus ? `（服务器状态：${confirmation.terminalStatus}）` : ""}`
        + `，并已从服务器读回确认。${suffix}`,
        "success",
      );
    }
    renderPendingQueue(state.pendingQueue, content.querySelector('[data-role="onboarding-queue-status"]'));
  }


  function renderReadinessReport(payload) {
    const box = content.querySelector('[data-role="onboarding-check"]');
    if (!box) return;
    box.hidden = false;
    // 契约：check 返回 { chat_type, bot_present, ..., readiness: { capabilities } }。
    // 这里保留对扁平结构的回退，但**绝不**把"没有条目"画成一片空白 —— 空白
    // 会被读成"检查通过了"。拿不到条目必须自己说出来。
    const readiness = payload?.readiness || payload;
    const capabilities = Array.isArray(readiness?.capabilities) ? readiness.capabilities : [];
    const selected = Array.isArray(readiness?.selected_features) ? readiness.selected_features : [];
    const blockers = Array.isArray(readiness?.blockers) ? readiness.blockers : [];
    const missingPermissions = readiness?.missing_bot_permissions || payload?.missing_bot_permissions || [];
    box.innerHTML = `
      <h4>就绪情况（配置检查，未做远端连通性测试）</h4>
      ${capabilities.length
        ? `<ul class="readiness-list">
        ${capabilities.map(item => `
          <li class="readiness-item state-${escapeHtml(item.state)}">
            <span class="readiness-state">${escapeHtml(item.state)}</span>
            <span class="readiness-capability">${escapeHtml(item.capability)}</span>
            <span class="readiness-detail">${escapeHtml(item.detail || "")}</span>
          </li>`).join("")}
      </ul>`
        : `<p class="readiness-empty">这次响应里没有可显示的能力条目，因此无法判断是否就绪。请重新检查；在拿到条目之前不要把它当成"已就绪"。</p>`}
      ${selected.length
        ? `<p class="readiness-note">本次检查的功能：${escapeHtml(selected.join("、"))}</p>`
        : ""}
      ${blockers.length
        ? `<p class="field-error">阻断项：${escapeHtml(blockers.join("、"))}</p>`
        : ""}
      ${missingPermissions.length
        ? `<p class="field-error">机器人缺少权限：${escapeHtml(missingPermissions.join("、"))}</p>`
        : ""}
      <p class="readiness-note">这只是检查：不会应用预设，也不会授权本群。</p>`;
  }

  function onboardingError(message) {
    const box = content.querySelector('[data-role="onboarding-error"]');
    if (!box) return;
    box.hidden = !message;
    box.textContent = message || "";
  }

  async function submitOnboardingRequest(presetId) {
    const input = content.querySelector("#onboarding-group-id");
    const raw = String(input?.value || "").trim();
    onboardingError("");
    if (!raw) {
      onboardingError("请填写群 ID。");
      return;
    }
    const groupId = Number(raw);
    if (!Number.isFinite(groupId) || groupId >= 0) {
      onboardingError("群 ID 必须是以 - 开头的群标识。");
      return;
    }
    const button = content.querySelector('[data-action="onboarding-submit"]');
    // 响应慢时按钮必须防重复：重复提交只会得到同一条 pending，
    // 但两次点击意味着两次 Telegram 核验。
    if (button?.dataset.busy === "1") return;
    if (button) button.dataset.busy = "1";
    try {
      await apiFetch("/api/v1/onboarding/requests", {
        method: "POST",
        body: JSON.stringify({ group_id: groupId, preset_id: presetId || null }),
      });
      await loadOnboardingRequests(state.session);
      showToast("申请已提交，等待最高管理员审理。", "success");
    } catch (error) {
      onboardingError(error.message || "提交失败，请重试。");
    } finally {
      if (button) delete button.dataset.busy;
    }
  }

  async function checkOnboardingReadiness(presetId) {
    const input = content.querySelector("#onboarding-group-id");
    const raw = String(input?.value || "").trim();
    onboardingError("");
    const groupId = Number(raw);
    if (!raw || !Number.isFinite(groupId) || groupId >= 0) {
      onboardingError("请先填写有效的群 ID。");
      return;
    }
    try {
      const payload = await apiFetch("/api/v1/onboarding/check", {
        method: "POST",
        body: JSON.stringify({ group_id: groupId, preset_id: presetId || null }),
      });
      renderReadinessReport(payload);
    } catch (error) {
      onboardingError(error.message || "检查失败，请重试。");
    }
  }

  function navItems() {
    return state.session?.can_manage_global
      ? ALL_NAV_ITEMS
      : ALL_NAV_ITEMS.filter(item => item.id === "groups");
  }

  class ApiError extends Error {
    constructor(message, status, body) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.body = body;
    }
  }

  function clone(value) {
    return value == null ? value : JSON.parse(JSON.stringify(value));
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function attr(value) {
    return escapeHtml(value);
  }

  // 用于把值塞进 `[attr="..."]` 选择器：引号/反斜杠会破坏选择器，必须转义。
  // （`CSS.escape` 在旧 WebView 上不一定有，所以自己来。）
  function cssEscape(value) {
    return String(value ?? "").replace(/[\\"]/g, "\\$&");
  }

  function getPath(object, path) {
    return String(path).split(".").reduce((current, key) => current?.[key], object);
  }

  function setPath(object, path, value) {
    const parts = String(path).split(".");
    const last = parts.pop();
    let current = object;
    for (const part of parts) {
      if (current[part] == null || typeof current[part] !== "object") current[part] = {};
      current = current[part];
    }
    current[last] = value;
  }

  function sameValue(left, right) {
    return JSON.stringify(left) === JSON.stringify(right);
  }

  function configDirty() {
    if (!state.config || !state.baseline) return false;
    return !sameValue(state.config, state.baseline) || Object.keys(state.secretChanges).length > 0;
  }

  function groupDirty(group) {
    const buttonDraft = state.groupTemplateButtonDrafts.get(String(group.id));
    return Boolean(buttonDraft?.error)
      || state.groupApiModelQuerySecretChanges.has(String(group.id))
      || !sameValue(group.settings, state.groupBaselines.get(String(group.id)));
  }

  function anyGroupDirty() {
    return state.groups.some(groupDirty)
      || [...state.groupTemplateButtonDrafts.values()].some(draft => Boolean(draft?.error));
  }

  function anyResourceFormDirty() {
    return state.resourceFormDrafts.size > 0
      || state.pendingResourceCreates.size > 0
      || state.pendingResourceDeletes.size > 0;
  }

  function resourceDraftType(key) {
    const parts = String(key).split(":");
    if (parts[0] === "entry" || parts[0] === "create") return parts[2] || "";
    if (parts[0] === "rule") return "rules";
    if (parts[0] === "memory") return "memories";
    return "";
  }

  function resourceDraftGroupId(key) {
    return String(key).split(":")[1] || "";
  }

  function groupResourceDraftDirty(groupId, category = "") {
    const target = String(groupId);
    const matches = operation => String(operation.groupId) === target
      && (!category || RESOURCE_TYPE_META[operation.type]?.category === category);
    return [...state.resourceFormDrafts.keys()].some(key => (
      resourceDraftGroupId(key) === target
      && (!category || RESOURCE_TYPE_META[resourceDraftType(key)]?.category === category)
    ))
      || [...state.pendingResourceCreates.values()].some(matches)
      || [...state.pendingResourceDeletes.values()].some(matches);
  }

  function unsavedChangeCount() {
    let count = configDirty() ? 1 : 0;
    count += state.groups.filter(groupDirty).length;
    const resourceKeys = new Set();
    for (const key of state.resourceFormDrafts.keys()) {
      const parts = String(key).split(":");
      if (parts[0] === "entry") resourceKeys.add(resourceOperationKey(parts[1], parts[2], parts.slice(3).join(":")));
      else if (parts[0] === "rule") resourceKeys.add(resourceOperationKey(parts[1], "rules", parts.slice(2).join(":")));
      else if (parts[0] === "memory") resourceKeys.add(resourceOperationKey(parts[1], "memories", parts.slice(2).join(":")));
      else resourceKeys.add(key);
    }
    for (const key of state.pendingResourceCreates.keys()) resourceKeys.add(key);
    for (const key of state.pendingResourceDeletes.keys()) resourceKeys.add(key);
    return count + resourceKeys.size;
  }

  function hasUnsavedChanges() {
    return configDirty() || anyGroupDirty() || anyResourceFormDirty();
  }

  function restartChanges() {
    const paths = state.document?.restart_required_paths || [];
    return paths.filter(path => !sameValue(getPath(state.config, path), getPath(state.baseline, path)));
  }

  function icon(name, className = "") {
    return `<i data-lucide="${attr(name)}"${className ? ` class="${attr(className)}"` : ""}></i>`;
  }

  function refreshIcons() {
    if (window.lucide?.createIcons) window.lucide.createIcons({ attrs: { "aria-hidden": "true" } });
  }

  function telegramInit() {
    if (!tg) return;
    try {
      tg.ready();
      tg.expand();
      tg.disableVerticalSwipes?.();
    } catch (_) {
      // Older Telegram clients do not expose every WebApp method.
    }
    syncTelegramSafeArea();
    if (tg.onEvent) {
      // Bot API 8.0+ exposes safeAreaInset / contentSafeAreaInset as objects and
      // fires these when they change (rotation, fullscreen, Telegram chrome).
      tg.onEvent("safeAreaChanged", syncTelegramSafeArea);
      tg.onEvent("contentSafeAreaChanged", syncTelegramSafeArea);
      tg.onEvent("fullscreenChanged", syncTelegramSafeArea);
      tg.onEvent("viewportChanged", syncTelegramSafeArea);
    }
  }

  // Bot API 8.0 shapes, per the official WebApp docs:
  //   safeAreaInset        : { top, bottom, left, right } in CSS pixels
  //   contentSafeAreaInset : { top, bottom, left, right } in CSS pixels
  //   viewportHeight / viewportStableHeight : numbers (pixels)
  // env(safe-area-inset-*) only covers standalone webviews, so mirror the SDK
  // numbers into custom properties. Every edge is rewritten on every sync, so
  // an inset that drops back to 0 (leaving fullscreen, rotating to portrait)
  // can never leave a stale value behind.
  const SAFE_AREA_EDGES = ["top", "bottom", "left", "right"];

  // Older SDKs omit the objects entirely and a misbehaving client can hand us
  // NaN or a negative number. Anything that is not a finite positive number is
  // treated as "no inset" so the layout falls back to env()/0 instead of
  // emitting an invalid calc().
  function telegramPixel(value) {
    return typeof value === "number" && Number.isFinite(value) && value > 0
      ? Math.round(value)
      : 0;
  }

  function syncTelegramSafeArea() {
    const root = document.documentElement;
    for (const edge of SAFE_AREA_EDGES) {
      // contentSafeAreaInset additionally excludes Telegram's own chrome, so it
      // is normally the larger of the two. Take the max per edge so neither
      // source can be dropped (the old code read the object as a scalar, so
      // every edge silently stayed 0 and left/right were never distinguished).
      const device = telegramPixel(tg?.safeAreaInset?.[edge]);
      const content = telegramPixel(tg?.contentSafeAreaInset?.[edge]);
      root.style.setProperty(`--tg-safe-${edge}`, `${Math.max(device, content)}px`);
    }
    // viewportStableHeight does not jitter while the Mini App is dragged or the
    // keyboard animates, so prefer it when the client provides it.
    const stable = telegramPixel(tg?.viewportStableHeight);
    const height = telegramPixel(tg?.viewportHeight) || stable;
    // An unusable height removes the property instead of writing 0px: the CSS
    // falls back to 100dvh, and min(100dvh, 0px) would collapse the page.
    if (height) root.style.setProperty("--tg-viewport-height", `${height}px`);
    else root.style.removeProperty("--tg-viewport-height");
    if (stable) root.style.setProperty("--tg-viewport-stable-height", `${stable}px`);
    else root.style.removeProperty("--tg-viewport-stable-height");
  }

  function authHeaders(hasBody = false) {
    const headers = { Authorization: `tma ${tg?.initData || ""}` };
    if (hasBody) headers["Content-Type"] = "application/json";
    return headers;
  }

  function extractError(body, fallback) {
    const structuredError = body?.error && typeof body.error === "object" ? body.error : null;
    if (structuredError?.message) {
      const details = structuredError.details;
      if (typeof details === "string" && details.trim()) return `${structuredError.message}：${details}`;
      if (Array.isArray(details) && details.length) {
        const rendered = details.map(item => item?.message || item?.msg || String(item)).filter(Boolean).join("；");
        if (rendered) return `${structuredError.message}：${rendered}`;
      }
      return structuredError.message;
    }
    const detail = body?.detail ?? body?.error ?? body?.message;
    if (Array.isArray(detail)) {
      return detail
        .map(item => item?.msg || item?.message || String(item))
        .filter(Boolean)
        .join("；");
    }
    if (typeof detail === "string" && detail.trim()) return detail;
    return fallback;
  }

  async function apiFetch(url, options = {}) {
    const hasBody = options.body != null;
    const response = await fetch(url, {
      ...options,
      headers: { ...authHeaders(hasBody), ...(options.headers || {}) },
    });
    const text = await response.text();
    let body = null;
    if (text) {
      try {
        body = JSON.parse(text);
      } catch (_) {
        body = { message: text };
      }
    }
    if (!response.ok) {
      throw new ApiError(extractError(body, `请求失败 (${response.status})`), response.status, body);
    }
    return body;
  }

  function showToast(message, type = "success", duration = 3600) {
    const toast = document.createElement("div");
    const iconName = type === "error" ? "circle-x" : type === "warning" ? "triangle-alert" : "circle-check";
    toast.className = `toast ${type}`;
    toast.setAttribute("role", type === "error" ? "alert" : "status");
    toast.innerHTML = `${icon(iconName)}<span>${escapeHtml(message)}</span>`;
    toastRegion.appendChild(toast);
    refreshIcons();
    window.setTimeout(() => toast.remove(), duration);
  }

  function askConfirmation(title, message) {
    if (!confirmDialog?.showModal) return Promise.resolve(window.confirm(message));
    confirmTitle.textContent = title;
    confirmMessage.textContent = message;
    confirmDialog.returnValue = "cancel";
    confirmDialog.showModal();
    refreshIcons();
    return new Promise(resolve => {
      confirmDialog.addEventListener("close", () => resolve(confirmDialog.returnValue === "confirm"), { once: true });
    });
  }

  function navMarkup(item) {
    return `
      <button class="nav-button${state.activeTab === item.id ? " active" : ""}" type="button" data-nav="${item.id}" aria-current="${state.activeTab === item.id ? "page" : "false"}">
        ${icon(item.icon)}<span>${escapeHtml(item.label)}</span>
      </button>`;
  }

  function desktopNavMarkup(items) {
    let lastGroup = "";
    return items.map(item => {
      const heading = item.group !== lastGroup
        ? `<span class="nav-group-label">${escapeHtml(item.group)}</span>`
        : "";
      lastGroup = item.group;
      return `${heading}${navMarkup(item)}`;
    }).join("");
  }

  function updateNavigation() {
    const items = navItems();
    const mobileScrollLeft = mobileNav.scrollLeft;
    desktopNav.innerHTML = desktopNavMarkup(items);
    mobileNav.innerHTML = items.map(navMarkup).join("");
    mobileNav.scrollLeft = mobileScrollLeft;
    const active = items.find(item => item.id === state.activeTab) || items[0];
    pageTitle.textContent = active.label;
    pageSubtitle.textContent = active.subtitle;
  }

  function syncWorkspaceInteractionLock() {
    const locked = state.loading
      || state.saving
      || state.reloadingGroups
      || state.accessMutating
      || state.immediateMutations > 0;
    const drawerOpen = mobileSidebarMedia.matches && app.classList.contains("sidebar-open");
    // Pure sibling PATCH saves keep text editing available during the ACK.
    // Navigation/save stay locked; mixed config/create/delete saves retain the
    // established whole-workspace lock and cannot race their snapshots.
    const patchEditing = state.saving && state.resourcePatchEditing
      && !state.loading && !state.reloadingGroups && !state.accessMutating
      && state.immediateMutations === 0;
    if (patchEditing) {
      content.inert = drawerOpen;
    } else {
      content.inert = locked || drawerOpen;
    }
    desktopNav.inert = locked;
    mobileNav.inert = locked || drawerOpen;
    topbarActions.inert = locked || drawerOpen;
    sidebarToggle.disabled = locked;
  }

  function updateChrome() {
    updateNavigation();
    const dirty = hasUnsavedChanges();
    const dirtyCount = unsavedChangeCount();
    const restart = restartChanges();
    saveButton.hidden = !state.session;
    saveButton.disabled = !dirty
      || state.saving
      || state.loading
      || state.reloadingGroups
      || state.accessMutating
      || state.immediateMutations > 0
      || state.groupPermissionLoads.size > 0;
    reloadButton.disabled = state.saving
      || state.loading
      || state.reloadingGroups
      || state.accessMutating
      || state.immediateMutations > 0
      || state.groupSaving.size > 0;
    // Only rewrite the button's markup when the label actually changes. A
    // blur-driven `change` event also lands here (that is how updatePathControl
    // refreshes the header while the pointer is already down on 保存全部);
    // replacing the button's children mid-press removes the mousedown target and
    // the browser never delivers the click, so the save silently did nothing.
    const savePhase = state.saving ? "saving" : "idle";
    if (saveButton.dataset.savePhase !== savePhase) {
      saveButton.dataset.savePhase = savePhase;
      saveButton.innerHTML = state.saving
        ? `<span class="spinner spinner-compact"></span><span>保存中</span>`
        : `${icon("save")}<span>保存全部</span>`;
    }
    saveButton.setAttribute("aria-label", dirtyCount ? `保存全部 ${dirtyCount} 项更改` : "保存全部");
    saveButton.title = dirtyCount ? `保存全部 ${dirtyCount} 项更改` : "所有更改均已保存";
    saveState.hidden = !state.session;
    saveState.className = `save-state${dirty ? " dirty" : ""}`;
    saveState.textContent = state.saving
      ? "正在保存全部更改"
      : state.reloadingGroups
        ? "正在刷新群组数据"
        : state.accessMutating
          ? "正在更新访问权限"
          : state.immediateMutations > 0
            ? "正在执行即时操作"
            : dirty
              ? `${dirtyCount} 项更改待保存${restart.length ? " · 含重启项" : ""}`
              : state.session?.can_manage_global
                ? `全部已保存 · 修订 ${state.document?.revision ?? "-"}`
                : "全部已保存";
    const masterKey = state.document?.bootstrap?.master_key_configured;
    sidebarStatus.innerHTML = state.session?.can_manage_global ? `
      <div class="status-line"><span class="status-dot"></span><span>运行时配置 · r${escapeHtml(state.document?.revision ?? "-")}</span></div>
      <div class="status-line"><span class="status-dot${masterKey ? "" : " warning"}"></span><span>密钥加密 ${masterKey ? "已就绪" : "未配置"}</span></div>`
      : `<div class="status-line"><span class="status-dot"></span><span>群管理员模式</span></div>`;
    syncWorkspaceInteractionLock();
    refreshIcons();
  }

  function rewardEditor(path, options = {}) {
    const rows = Array.isArray(getPath(state.config, path)) ? getPath(state.config, path) : [];
    const totalWeight = rows.reduce((sum, row) => sum + (Number(row?.weight) || 0), 0);
    const expectation = totalWeight
      ? rows.reduce((sum, row) => sum + (Number(row?.payout) || 0) * (Number(row?.weight) || 0), 0) / totalWeight
      : 0;
    const body = rows.map((row, index) => `
      <div class="reward-row" data-reward-index="${index}">
        <label class="reward-cell">
          <span>中奖积分</span>
          <input type="number" min="0" max="1000000" step="1" required inputmode="numeric"
                 data-reward-path="${attr(path)}" data-reward-index="${index}" data-reward-key="payout"
                 value="${attr(row?.payout ?? 0)}">
        </label>
        <label class="reward-cell">
          <span>权重</span>
          <input type="number" min="1" max="1000000" step="1" required inputmode="numeric"
                 data-reward-path="${attr(path)}" data-reward-index="${index}" data-reward-key="weight"
                 value="${attr(row?.weight ?? 1)}">
        </label>
        <label class="reward-cell reward-cell-wide">
          <span>标签</span>
          <input type="text" maxlength="32" data-reward-path="${attr(path)}" data-reward-index="${index}" data-reward-key="label"
                 value="${attr(row?.label ?? "")}" placeholder="给用户看的名字">
        </label>
        <button class="icon-button danger" type="button" data-reward-remove="${attr(path)}" data-reward-index="${index}"
                aria-label="删除这一档"${rows.length <= 1 ? " disabled" : ""}>${icon("trash-2")}</button>
      </div>`).join("");
    return `
      <div class="reward-editor" data-reward-editor="${attr(path)}">
        <div class="reward-editor-head">
          <strong>${escapeHtml(options.label || "奖池档位")}</strong>
          <span class="badge info">总权重 ${escapeHtml(totalWeight)}</span>
          <span class="badge info">期望 ${escapeHtml(expectation.toFixed(2))} 分/次</span>
          <button class="secondary-button" type="button" data-reward-add="${attr(path)}">${icon("plus")}加一档</button>
        </div>
        ${body}
        ${options.hint ? `<span class="field-hint">${escapeHtml(options.hint)}</span>` : ""}
      </div>`;
  }

  function readRewardEditor(input) {
    const path = input.dataset.rewardPath;
    const rows = Array.isArray(getPath(state.config, path)) ? getPath(state.config, path).map(row => ({ ...row })) : [];
    const index = Number(input.dataset.rewardIndex);
    const key = input.dataset.rewardKey;
    if (!rows[index] || !key) return rows;
    rows[index][key] = key === "label" ? input.value : Number(input.value || 0);
    return rows;
  }

  function pageHead(title, description, actions = "") {
    return `
      <div class="page-head">
        <div><h2>${escapeHtml(title)}</h2><p>${escapeHtml(description)}</p></div>
        ${actions ? `<div class="page-actions">${actions}</div>` : ""}
      </div>`;
  }

  function sectionHead(title, description = "", action = "") {
    return `
      <div class="section-heading">
        <div><h3>${escapeHtml(title)}</h3>${description ? `<p>${escapeHtml(description)}</p>` : ""}</div>
        ${action}
      </div>`;
  }

  // 字段级 help/details：一句话结论留在 hint 里，其余说明折叠起来，
  // 这样"高级项"不再靠一长段施工备注顶着。
  function helpDisclosure(help, label = "展开说明") {
    if (!help) return "";
    return `
      <details class="field-help">
        <summary>${icon("chevron-down")}<span>${escapeHtml(label)}</span></summary>
        <div class="field-help-body">${escapeHtml(help)}</div>
      </details>`;
  }

  // 分组级渐进披露：把"大部分人用不到"的字段收进可展开的面板，
  // 但任何字段都没有被删除——展开后与原先完全一致。
  function advancedPanel(id, title, description, body, { open = false } = {}) {
    const expanded = state.advancedOpen.has(id) ? state.advancedOpen.get(id) : open;
    return `
      <details class="advanced-panel" data-advanced-panel="${attr(id)}"${expanded ? " open" : ""}>
        <summary>
          <span class="advanced-panel-icon">${icon("sliders-horizontal")}</span>
          <span class="advanced-panel-copy">
            <strong>${escapeHtml(title)}</strong>
            ${description ? `<small>${escapeHtml(description)}</small>` : ""}
          </span>
          ${icon("chevron-down", "advanced-panel-chevron")}
        </summary>
        <div class="advanced-body">${body}</div>
      </details>`;
  }

  function captureAdvancedDisclosureStates() {
    content.querySelectorAll("[data-advanced-panel]").forEach(panel => {
      state.advancedOpen.set(panel.dataset.advancedPanel, panel.open);
    });
  }

  // 校验报错必须落在"看得见"的字段上：先把折叠层逐级打开再 reportValidity。
  function revealAdvancedDisclosure(control) {
    let revealed = false;
    let node = control?.parentElement || null;
    while (node && node !== content) {
      if (node.matches?.("details.advanced-panel") && !node.open) {
        node.open = true;
        if (node.dataset.advancedPanel) state.advancedOpen.set(node.dataset.advancedPanel, true);
        revealed = true;
      }
      node = node.parentElement;
    }
    return revealed;
  }

  // 按配置路径把字段所在的折叠层全部打开并滚到它身上，
  // 这样"这个字段需要填数字"不会指向一个用户根本没打开过的面板。
  function revealFieldByPath(path) {
    if (!path) return;
    const control = content.querySelector(`[data-path="${CSS.escape(path)}"]`);
    if (!control) return;
    if (revealAdvancedDisclosure(control)) {
      control.focus({ preventScroll: true });
      control.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }

  function field(path, label, options = {}) {
    const value = getPath(state.config, path);
    const id = `field-${path.replaceAll(".", "-")}`;
    const full = options.full ? " full" : "";
    const kind = options.kind || (options.type === "number" ? "number" : "string");
    const restart = (state.document?.restart_required_paths || []).includes(path);
    const hint = options.hint || "";
    const labelRight = restart ? `<span class="badge warning">${icon("rotate-ccw")}需重启</span>` : "";
    const common = `id="${id}" data-path="${attr(path)}" data-kind="${attr(kind)}"${options.required ? " required" : ""}${options.disabled ? " disabled" : ""}`;
    let control;
    if (options.type === "select") {
      control = `<select ${common}>${(options.options || []).map(item => {
        const option = typeof item === "string" ? { value: item, label: item } : item;
        return `<option value="${attr(option.value)}"${String(value ?? "") === String(option.value) ? " selected" : ""}>${escapeHtml(option.label)}</option>`;
      }).join("")}</select>`;
    } else if (options.type === "textarea") {
      let textValue = value ?? "";
      if (kind === "json-object") {
        try {
          textValue = JSON.stringify(value && typeof value === "object" ? value : {}, null, 2);
        } catch (_error) {
          textValue = "{}";
        }
      }
      control = `<textarea ${common}${options.maxlength ? ` maxlength="${options.maxlength}"` : ""}${options.rows ? ` rows="${options.rows}"` : ""}>${escapeHtml(textValue)}</textarea>`;
    } else {
      let displayValue;
      if (kind === "array") displayValue = Array.isArray(value) ? value.join(", ") : "";
      else if (kind === "json-object") {
        try {
          displayValue = JSON.stringify(value && typeof value === "object" ? value : {}, null, 2);
        } catch (_error) {
          displayValue = "{}";
        }
      } else displayValue = value ?? "";
      const inputType = options.type === "url" ? "text" : (options.type || "text");
      control = `<input ${common} type="${attr(inputType)}" value="${attr(displayValue)}"${options.type === "url" ? " inputmode=\"url\"" : ""}${options.min != null ? ` min="${options.min}"` : ""}${options.max != null ? ` max="${options.max}"` : ""}${options.step != null ? ` step="${options.step}"` : ""}${options.maxlength ? ` maxlength="${options.maxlength}"` : ""}${options.placeholder ? ` placeholder="${attr(options.placeholder)}"` : ""}${options.list ? ` list="${attr(options.list)}"` : ""} autocomplete="off">`;
    }
    return `
      <div class="field${full}">
        <div class="field-label-row"><label class="field-label" for="${id}">${escapeHtml(label)}</label>${labelRight}</div>
        ${control}
        ${hint ? `<span class="field-hint">${escapeHtml(hint)}</span>` : ""}
        ${opaqueMarkerHint(value)}
        ${helpDisclosure(options.help, options.helpLabel)}
      </div>`;
  }

  // 带服务端脱敏标记的字段：标记**不是**真实值，界面必须说清楚这一点，否则
  // 管理员会把 `<redacted:current>` 当成一句可以照抄的示例字符串。
  function opaqueMarkerHint(value) {
    if (!hasOpaqueMarker(value)) return "";
    const status = redactionMarkerStatus(value);
    const label = status ? secretStatusLabel(status) : "含隐藏凭据";
    return `<span class="field-hint opaque-marker-hint" data-opaque-marker>${icon("key-round")}${escapeHtml(label)} · ${escapeHtml(OPAQUE_MARKER_HINT)}</span>`;
  }

  function toggle(path, label, hint = "", full = false, help = "") {
    const value = Boolean(getPath(state.config, path));
    const id = `field-${path.replaceAll(".", "-")}`;
    const restart = (state.document?.restart_required_paths || []).includes(path);
    return `
      <div class="toggle-field${full ? " full" : ""}">
        <div class="toggle-copy">
          <strong>${escapeHtml(label)}${restart ? ` <span class="badge warning">需重启</span>` : ""}</strong>
          ${hint ? `<small>${escapeHtml(hint)}</small>` : ""}
          ${helpDisclosure(help)}
        </div>
        <label class="toggle" for="${id}">
          <input id="${id}" type="checkbox" data-path="${attr(path)}" data-kind="boolean" aria-label="${attr(label)}"${value ? " checked" : ""}>
          <span class="toggle-track"></span>
        </label>
      </div>`;
  }

  function secretField(path, label, hint = "", help = "") {
    const change = state.secretChanges[path];
    const configured = state.configuredSecrets.has(path);
    const clearing = change?.action === "clear";
    const replacing = change?.action === "replace";
    const status = clearing
      ? `<span class="badge danger">${icon("trash-2")}保存后清除</span>`
      : replacing
        ? `<span class="badge warning">${icon("pencil")}等待替换</span>`
        : configured
          ? `<span class="badge success">${icon("key-round")}已配置</span>`
          : `<span class="badge">未配置</span>`;
    const action = clearing
      ? `<button class="mini-icon-button" type="button" data-action="secret-undo" data-secret-path="${attr(path)}" aria-label="撤销清除" title="撤销清除">${icon("undo-2")}</button>`
      : (configured || replacing)
        ? `<button class="mini-icon-button danger" type="button" data-action="secret-clear" data-secret-path="${attr(path)}" aria-label="清除密钥" title="清除密钥">${icon("trash-2")}</button>`
        : "";
    return `
      <div class="field">
        <label class="field-label">${escapeHtml(label)}</label>
        <div class="secret-control">
          <input type="password" data-secret-input="${attr(path)}" value="${attr(replacing ? change.value || "" : "")}" placeholder="${configured ? "留空保留当前密钥" : "输入新密钥"}"${clearing ? " disabled" : ""} autocomplete="new-password">
          <div class="secret-status">${status}${action}</div>
        </div>
        ${hint ? `<span class="field-hint">${escapeHtml(hint)}</span>` : ""}
        ${helpDisclosure(help)}
      </div>`;
  }

  // D3-42：`models.skill` 允许是 `null`（后端 `models.skill or main` = 继承主模型）。
  // 渲染循环与供应商校验都直接读 `state.config.models[role].provider`，所以先把
  // 缺失/null 的角色补成一个"留空 = 继承"的空壳，UI 才不会炸、校验也不会误报。
  function normalizeModelRoles() {
    const models = state.config?.models;
    if (!models) return;
    const template = models.main || {};
    for (const roleName of Object.keys(ROLE_META)) {
      const role = models[roleName];
      if (!role || typeof role !== "object") {
        // 从**主模型**整份克隆再把 provider/model 留空：`null` 在后端等价于
        // `models.skill or main`，即继承主模型的 temperature / max_tokens /
        // timeout / 回退链。若这里造一个"空壳"（fallbacks: []、各自的
        // total_deadline），保存回去就会**悄悄丢掉主模型的回退链**。
        models[roleName] = {
          ...JSON.parse(JSON.stringify(template)),
          provider: "",
          model: "",
        };
        if (models[roleName].total_deadline_sec == null) {
          models[roleName].total_deadline_sec = ROLE_META[roleName].deadlineDefault;
        }
      } else {
        if (role.provider == null) role.provider = "";
        if (role.model == null) role.model = "";
        if (role.request_params == null) role.request_params = {};
        if (role.fallbacks == null) role.fallbacks = [];
      }
    }
  }

  function providerOptions(selected, allowInherit = true, inheritLabel = "继承主模型") {
    const providers = state.config.models.providers || [];
    const options = [];
    if (allowInherit) options.push({ value: "", label: inheritLabel });
    for (const provider of providers) options.push({ value: provider.name, label: provider.name });
    return options.map(item => `<option value="${attr(item.value)}"${String(selected ?? "") === item.value ? " selected" : ""}>${escapeHtml(item.label)}</option>`).join("");
  }

  function renderOverview() {
    const bootstrap = state.document.bootstrap || {};
    const restartPaths = state.document.restart_required_paths || [];
    const pathLabels = {
      "bot.parse_mode": "消息解析格式",
    };
    return `
      ${pageHead("运行概览", "数据库中的配置会立即应用；启动参数保持只读。")}
      <div class="section-stack">
        <section class="settings-section">
          <div class="overview-grid">
            <div class="metric"><span>配置修订</span><strong>r${escapeHtml(state.document.revision)}</strong></div>
            <div class="metric"><span>模型供应商</span><strong>${state.config.models.providers.length}</strong></div>
            <div class="metric"><span>已配置密钥</span><strong>${state.configuredSecrets.size}</strong></div>
            <div class="metric"><span>管理群组</span><strong>${state.groupsError ? "-" : state.groups.length}</strong></div>
          </div>
        </section>

        ${!bootstrap.master_key_configured ? `
          <div class="notice">${icon("triangle-alert")}<span>CONFIG_MASTER_KEY 尚未配置。普通设置仍可保存，但新增或替换密钥会被拒绝。</span></div>` : ""}

        <section class="settings-section">
          ${sectionHead("启动参数", "这些项目由部署环境提供，修改后需要重启进程。")}
          <dl class="bootstrap-table">
            <div class="bootstrap-row"><dt>Mini App 公网地址</dt><dd>${escapeHtml(bootstrap.public_base_url || "未配置")}</dd></div>
            <div class="bootstrap-row"><dt>监听地址</dt><dd>${escapeHtml(bootstrap.listen_host || "-")}:${escapeHtml(bootstrap.listen_port ?? "-")}</dd></div>
            <div class="bootstrap-row"><dt>数据库</dt><dd>${escapeHtml(bootstrap.database_url || "-")}</dd></div>
            <div class="bootstrap-row"><dt>配置加密主密钥</dt><dd>${bootstrap.master_key_configured ? `<span class="badge success">已配置</span>` : `<span class="badge warning">未配置</span>`}</dd></div>
          </dl>
        </section>

        <section class="settings-section">
          ${sectionHead("重启项", "其余运行时设置保存后无需重启。")}
          ${(state.document.restart_pending || []).length ? `
            <div class="notice warning">${icon("triangle-alert")}<span>刚刚保存的改动里有这些字段要重启才生效：<strong>${escapeHtml((state.document.restart_pending || []).join("、"))}</strong></span></div>` : ""}
          <div class="item-list">
            ${restartPaths.map(path => `
              <div class="notice info${(state.document.restart_pending || []).includes(path) ? " warning" : ""}">${icon("rotate-ccw")}<span><strong>${escapeHtml(pathLabels[path] || path)}</strong><br>${escapeHtml(path)}</span></div>`).join("") || `<div class="empty-state empty-state-compact">${icon("circle-check")}<p>当前没有重启项</p></div>`}
          </div>
        </section>
      </div>`;
  }

  function renderProviderCard(provider, index) {
    const secretPath = `providers.${String(provider.name || "").trim().toLowerCase()}.api_key`;
    return `
      <article class="item-card">
        <div class="item-card-header">
          <div class="item-card-title">${icon("server")}<strong>${escapeHtml(provider.name || `供应商 ${index + 1}`)}</strong></div>
          <div class="item-actions">
            <button class="mini-icon-button danger" type="button" data-action="remove-provider" data-index="${index}" aria-label="删除供应商" title="删除供应商">${icon("trash-2")}</button>
          </div>
        </div>
        <div class="item-card-body">
          <div class="field-grid">
            <div class="field">
              <label class="field-label" for="provider-name-${index}">配置名称</label>
              <input id="provider-name-${index}" type="text" value="${attr(provider.name)}" data-path="models.providers.${index}.name" data-kind="string" data-provider-index="${index}" data-provider-old-name="${attr(provider.name)}" maxlength="64" pattern="[A-Za-z0-9_-]+" required autocomplete="off">
              <span class="field-hint">模型角色通过此名称引用</span>
            </div>
            <div class="field">
              <label class="field-label" for="provider-type-${index}">协议供应商</label>
              <input id="provider-type-${index}" type="text" value="${attr(provider.provider)}" data-path="models.providers.${index}.provider" data-kind="lower-string" list="provider-types" maxlength="64" required autocomplete="off">
            </div>
            <div class="field full">
              <label class="field-label" for="provider-base-${index}">API Base URL</label>
              <input id="provider-base-${index}" type="text" inputmode="url" value="${attr(provider.api_base)}" data-path="models.providers.${index}.api_base" data-kind="string" placeholder="使用供应商默认地址" autocomplete="off">
              ${opaqueMarkerHint(provider.api_base)}
            </div>
            <div class="field">
              <label class="field-label" for="provider-endpoint-${index}">聊天接口</label>
              <select id="provider-endpoint-${index}" data-path="models.providers.${index}.chat_endpoint" data-kind="string">
                <option value="auto"${provider.chat_endpoint === "auto" ? " selected" : ""}>自动识别</option>
                <option value="chat_completions"${provider.chat_endpoint === "chat_completions" ? " selected" : ""}>Chat Completions</option>
                <option value="responses"${provider.chat_endpoint === "responses" ? " selected" : ""}>Responses</option>
              </select>
            </div>
            ${secretField(secretPath, "API Key", "空白不会覆盖已保存的密钥")}
            ${toggle(`models.providers.${index}.stream`, "启用流式请求", "供应商连接使用流式返回")}
          </div>
        </div>
      </article>`;
  }

  function renderFallback(roleName, item, index) {
    return `
      <div class="fallback-row">
        <div class="field">
          <label class="field-label" for="fallback-provider-${roleName}-${index}">供应商</label>
          <select id="fallback-provider-${roleName}-${index}" data-path="models.${roleName}.fallbacks.${index}.provider" data-kind="string" required>
            ${providerOptions(item.provider, false)}
          </select>
        </div>
        <div class="field">
          <label class="field-label" for="fallback-model-${roleName}-${index}">模型</label>
          <input id="fallback-model-${roleName}-${index}" type="text" value="${attr(item.model)}" data-path="models.${roleName}.fallbacks.${index}.model" data-kind="string" maxlength="255" placeholder="留空沿用当前模型" autocomplete="off">
        </div>
        <button class="mini-icon-button danger" type="button" data-action="remove-fallback" data-role="${roleName}" data-index="${index}" aria-label="删除回退模型" title="删除回退模型">${icon("trash-2")}</button>
        ${field(`models.${roleName}.fallbacks.${index}.request_params`, "请求参数 JSON", {
          type: "textarea",
          kind: "json-object",
          rows: 5,
          full: true,
          maxlength: 20000,
          hint: "只对这个回退模型生效；不填写则不发送额外参数。",
        })}
      </div>`;
  }

  function renderRoleCard(roleName) {
    const meta = ROLE_META[roleName];
    const role = state.config.models[roleName];
    const allowInherit = roleName !== "main";
    return `
      <article class="item-card">
        <div class="item-card-header">
          <div class="item-card-title">${icon(meta.icon)}<strong>${escapeHtml(meta.label)}</strong></div>
          ${allowInherit ? `<span class="badge info">支持继承</span>` : `<span class="badge success">主路由</span>`}
        </div>
        <div class="item-card-body">
          <div class="field-grid three">
            <div class="field">
              <label class="field-label" for="role-provider-${roleName}">供应商</label>
              <select id="role-provider-${roleName}" data-path="models.${roleName}.provider" data-kind="string"${allowInherit ? "" : " required"}>
                ${providerOptions(role.provider, allowInherit, meta.parent)}
              </select>
            </div>
            <div class="field">
              <label class="field-label" for="role-model-${roleName}">模型名称</label>
              <input id="role-model-${roleName}" type="text" value="${attr(role.model)}" data-path="models.${roleName}.model" data-kind="string" maxlength="255"${roleName === "main" || meta.embed ? " required" : ""} placeholder="${allowInherit && !meta.embed ? "留空继承上级模型" : "模型标识"}" autocomplete="off">
            </div>
            ${field(`models.${roleName}.timeout_sec`, "超时（秒）", { type: "number", min: 1, max: 600, step: 0.1, required: true })}
            ${field(`models.${roleName}.total_deadline_sec`, "总时限（秒）", { type: "number", min: 0, max: 3600, step: 0.1, required: true, hint: `含重试与回退链的整体预算，0 使用内置默认（${meta.deadlineDefault} 秒）` })}
            ${meta.embed ? "" : field(`models.${roleName}.temperature`, "Temperature", { type: "number", min: 0, max: 2, step: 0.05, required: true })}
            ${meta.embed ? "" : field(`models.${roleName}.max_tokens`, "最大输出 Token", { type: "number", min: 1, max: 2000000, step: 1, required: true })}
            ${field(`models.${roleName}.request_params`, "主模型请求参数 JSON", {
              type: "textarea",
              kind: "json-object",
              rows: 7,
              full: true,
              maxlength: 20000,
              hint: "只对这个角色的主模型生效，例如 {\"thinking\":{\"type\":\"enabled\"},\"reasoning_effort\":\"low\"}。",
            })}
          </div>
          <div class="subsection">
            <div class="subsection-head">
              <strong>回退链</strong>
              <button class="text-button" type="button" data-action="add-fallback" data-role="${roleName}">${icon("plus")}添加回退</button>
            </div>
            <div class="fallback-list">
              ${(role.fallbacks || []).map((item, index) => renderFallback(roleName, item, index)).join("") || `<span class="field-hint">未设置回退模型</span>`}
            </div>
          </div>
        </div>
      </article>`;
  }

  function parseJsonObjectValue(rawValue, label = "请求参数") {
    const textValue = String(rawValue ?? "").trim();
    if (!textValue) return {};
    let parsed;
    try {
      parsed = JSON.parse(textValue);
    } catch (_error) {
      throw new Error(`${label}必须是有效 JSON`);
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error(`${label}必须是 JSON 对象`);
    }
    return parsed;
  }

  function renderModels() {
    return `
      ${pageHead("模型路由", "管理 API 供应商、任务角色、各模型请求参数和失败回退顺序。")}
      <datalist id="provider-types">
        <option value="gemini"></option><option value="openai"></option><option value="anthropic"></option><option value="openrouter"></option>
        <option value="openai_compatible"></option><option value="minimax"></option><option value="deepseek"></option><option value="moonshot"></option>
        <option value="dashscope"></option><option value="volcengine"></option><option value="xai"></option><option value="mistral"></option><option value="groq"></option>
      </datalist>
      <div class="section-stack">
        <section class="settings-section">
          ${sectionHead("供应商", "配置名称必须唯一，且只能使用字母、数字、下划线和连字符。", `<button class="secondary-button" type="button" data-action="add-provider">${icon("plus")}添加供应商</button>`)}
          <div class="item-list">${state.config.models.providers.map(renderProviderCard).join("")}</div>
        </section>
        <section class="settings-section">
          ${sectionHead("模型角色", "空白的非主模型字段会沿用上级模型。")}
          <div class="item-list">${Object.keys(ROLE_META).map(renderRoleCard).join("")}</div>
        </section>
        <section class="settings-section">
          ${sectionHead("重试策略", "所有模型角色共用。")}
          <div class="field-grid three">
            ${field("models.retry_attempts", "尝试次数", { type: "number", min: 1, max: 10, step: 1, required: true })}
            ${field("models.retry_backoff_sec", "退避时间（秒）", { type: "number", min: 0, max: 60, step: 0.1, required: true })}
            ${field("models.retry_timeout_multiplier", "超时倍率", { type: "number", min: 1, max: 10, step: 0.05, required: true })}
          </div>
        </section>
        ${state.session?.can_manage_global
          ? `<section class="settings-section" data-role="price-catalog">${priceCatalogBody()}</section>`
          : ""}
      </div>`;
  }

  function renderBot() {
    const deleteCategories = new Set(state.config.bot.auto_delete_categories || []);
    const categorySeconds = state.config.bot.auto_delete_category_seconds || {};
    const categoryModes = state.config.bot.auto_delete_category_mode || {};
    const categoryRow = (value, label) => {
      const enabled = deleteCategories.has(value);
      const seconds = categorySeconds[value];
      const mode = categoryModes[value] === "button" ? "button" : "timer";
      return `
      <div class="delete-category-row">
        <label class="choice-row">
          <input type="checkbox" data-auto-delete-category="${value}"${enabled ? " checked" : ""}>
          <span>${escapeHtml(label)}</span>
        </label>
        <select class="delete-category-mode" data-auto-delete-mode="${value}" aria-label="${attr(label)}清理方式" title="二选一：自动删除 = 到点由 bot 删掉本类消息；删除按钮 = 不定时删除，每条消息下方附一个「删除消息」按钮（本行的秒数在按钮模式下不生效）"${enabled ? "" : " disabled"}>
          <option value="timer"${mode === "timer" ? " selected" : ""}>自动删除</option>
          <option value="button"${mode === "button" ? " selected" : ""}>删除按钮</option>
        </select>
        <input class="delete-category-seconds" type="number" min="0" max="604800" step="1" data-auto-delete-seconds="${value}" value="${attr(seconds == null ? "" : seconds)}" placeholder="默认" aria-label="${attr(label)}自动删除秒数" title="留空使用上方全局秒数"${enabled && mode === "timer" ? "" : " disabled"}>
      </div>`;
    };
    return `
      ${pageHead("Bot 行为", "调整消息处理、上下文预算与主动发言节奏。多数设置保存后立即生效；标记「需重启」的设置除外。")}
      <datalist id="parse-modes"><option value="HTML"></option><option value="Markdown"></option><option value="MarkdownV2"></option></datalist>
      <div class="section-stack">
        <section class="settings-section">
          ${sectionHead("消息处理", "控制 Bot 如何接收、生成与发送消息。")}
          <div class="field-grid three">
            ${field("bot.parse_mode", "消息解析格式", { maxlength: 32, list: "parse-modes", placeholder: "留空发送纯文本" })}
            ${field("bot.inbound_debounce_seconds", "入站合并窗口（秒）", { type: "number", min: 0, max: 60, step: 0.1, required: true, hint: "短时间内的连续消息会合并成一轮处理" })}
            ${field("bot.reply_batch_timeout_seconds", "单次回复总时限（秒）", { type: "number", min: 5, max: 120, step: 1, required: true, hint: "覆盖决策、工具、生成和投递的总预算" })}
            ${field("bot.auto_delete_seconds", "自动删除（秒）", { type: "number", min: 0, max: 604800, step: 1, required: true, hint: "0 表示不自动删除；作为下方各类别的默认秒数" })}
            ${toggle("bot.disable_link_preview", "关闭 AI 回复链接预览", "同时作用于 AI 自动回复等所有 Bot 生成内容")}
            ${toggle("bot.enable_typing", "显示输入状态", "生成回复时发送 typing 状态")}
            ${toggle("bot.enable_streaming", "流式编辑消息", "生成期间持续更新同一条 Telegram 消息")}
            ${toggle("bot.enable_rich_messages", "启用富文本排版", "开启后按上方「消息解析格式」渲染粗体、斜体、代码块等样式；关闭则统一按纯文本发送。")}
            ${advancedPanel("bot.streaming", "流式输出细调", "只在需要调整发送节奏时改动", `
              <div class="field-grid">
                ${field("bot.stream_chunk_size", "流式首段字符数", { type: "number", min: 8, max: 4096, step: 1, required: true, hint: "首个片段的最小长度，太小会频繁编辑同一条消息" })}
                ${field("bot.stream_edit_interval_sec", "编辑间隔（秒）", { type: "number", min: 0.3, max: 30, step: 0.1, required: true, hint: "两次编辑之间的最小间隔，太小可能触发 Telegram 限流" })}
              </div>`)}
          </div>
          <div class="choice-grid full-width-control">
            ${AUTO_DELETE_CATEGORY_META.map(({ key, label }) => categoryRow(key, label)).join("")}
          </div>
          <p class="field-hint">勾选的类别按所选方式清理，两种方式互斥：「自动删除」到点由 Bot 删除该类消息，秒数留空则使用上方全局秒数；「删除按钮」不定时删除，而是在每条消息下方附一个管理员可用的内联删除按钮（此模式下本行的秒数不生效）。</p>
        </section>
        <section class="settings-section">
          ${sectionHead("上下文与长期记忆", "常规对话只保留最近消息；更早的原文按群归档并按需召回。摘要与召回不替代原文，原文按「原文保留天数」过期清理。")}
          <div class="field-grid three">
            ${field("bot.memory_recent_messages", "近期消息窗口", { type: "number", min: 50, max: 2000, step: 1, required: true, hint: "默认 500；每个群分别维护" })}
            ${field("bot.memory_retention_days", "原文保留天数", { type: "number", min: 1, max: 365, step: 1, required: true, hint: "默认 7 天，过期后按群清理" })}
            ${field("bot.memory_archive_max_messages_per_group", "每群归档硬上限", { type: "number", min: 1000, max: 1000000, step: 1000, required: true, hint: "防止高流量群在保留期内无限增长" })}
            ${field("bot.memory_recall_max_results", "召回索引候选数", { type: "number", min: 1, max: 20, step: 1, required: true })}
            ${toggle("bot.memory_recall_enabled", "启用原始档案召回", "只影响「按当前问题从本群原始消息档案里检索相关消息」。", false, "这不是长期记忆的总开关：它只管按问题检索本群原始消息，不控制稳定事实（口味、身份、关系、约定等）的提炼、注入与写入，那部分由同页下方的「启用长期记忆（总开关）」负责。关掉它，长期记忆仍会照常提炼与写入。")}
            ${toggle("bot.memory_automatic_compaction", "兼容旧自动压缩", "默认关闭；开启后仍只压缩近期窗口，不会额外删除已归档的原文。", false, "这是给仍在使用旧版压缩行为的群保留的兼容开关，新部署建议保持关闭：归档与召回已经能覆盖长对话，不需要额外压缩。")}
            ${advancedPanel("bot.summary", "群摘要（后台）", "把更早的内容压成摘要，默认关闭", `
              <div class="field-grid three">
                ${toggle("bot.group_summary_enabled", "启用后台群摘要", "开启后：近期原文 + 旧内容的后台摘要。原始消息与归档一条都不删，前台只读已发布的摘要，绝不等待摘要生成。")}
                ${field("bot.group_summary_recent_raw_messages", "摘要模式：近期原文条数", { type: "number", min: 20, max: 10000, step: 1, required: true, hint: "默认 200；只读最近 N 条原文，其余由摘要承载" })}
                ${field("bot.group_summary_max_tokens", "摘要长度上限 Token", { type: "number", min: 256, max: 32768, step: 256, required: true, hint: "默认 4096；超出按此裁断并标注" })}
                ${field("bot.group_summary_batch_max_messages", "单批最大条数", { type: "number", min: 10, max: 2000, step: 10, required: true, hint: "默认 200" })}
                ${field("bot.group_summary_batch_max_input_tokens", "单批最大输入 Token", { type: "number", min: 1024, max: 1000000, step: 1024, required: true, hint: "默认 16384；实际还受模型与业务预算约束" })}
                ${field("bot.group_summary_global_concurrency", "摘要全局并发", { type: "number", min: 1, max: 8, step: 1, required: true, hint: "默认 2。与普通回复共享容量，普通回复始终保留至少 2 个名额" })}
                ${field("bot.group_summary_per_group_concurrency", "摘要每群并发", { type: "number", min: 1, max: 1, step: 1, required: true, hint: "固定为 1，不随用户数放开" })}
                ${field("bot.group_summary_deadline_seconds", "摘要执行硬超时（秒）", { type: "number", min: 1, max: 120, step: 1, required: true, hint: "默认 15；整轮模型调用、重试与回退合计不超过它" })}
                ${field("bot.group_summary_queue_wait_seconds", "摘要排队最长等待（秒）", { type: "number", min: 1, max: 600, step: 1, required: true, hint: "默认 30；过期就跳过并退避，排队时间不计入模型时限" })}
                ${field("bot.group_summary_min_refresh_seconds", "同群最小刷新间隔（秒）", { type: "number", min: 0, max: 86400, step: 1, required: true, hint: "默认 60" })}
                ${field("bot.group_summary_failure_backoff_seconds", "失败退避起点（秒）", { type: "number", min: 1, max: 86400, step: 1, required: true, hint: "默认 60" })}
                ${field("bot.group_summary_failure_backoff_max_seconds", "失败退避上限（秒）", { type: "number", min: 1, max: 86400, step: 1, required: true, hint: "默认 3600" })}
                ${field("bot.group_summary_pending_capacity", "摘要待处理群队列容量", { type: "number", min: 1, max: 100000, step: 1, required: true, hint: "默认 1000。容量不等于可支持的群数量；队满时本次跳过并计数" })}
                ${field("bot.group_summary_trigger_messages", "触发阈值：未摘要旧消息条数", { type: "number", min: 1, max: 100000, step: 1, required: true, hint: "默认 200：近期窗口之外累积到这么多就触发" })}
                ${field("bot.group_summary_trigger_budget_ratio", "触发阈值：输入预算占用比", { type: "number", min: 0.1, max: 1, step: 0.05, required: true, hint: "默认 0.85：占用逼近有效输入预算的 85% 且存在未摘要旧消息时触发" })}
              </div>`)}
            ${advancedPanel("bot.context-budget", "上下文预算", "Token 预算与模型窗口上限，默认值即可满足绝大多数场景", `
              <div class="field-grid three">
                ${field("bot.decision_context_items", "决策上下文条数", { type: "number", min: 0, max: 20, step: 1, required: true })}
                ${field("bot.context_budget_tokens", "每轮业务总预算 Token", { type: "number", min: 1024, max: 16000000, step: 1024, required: true, hint: "默认 278528（272Ki，推荐值）。覆盖人设、工具定义、记忆、历史、本轮、工具结果与输出预留；与模型真实窗口取更小的一个" })}
                ${field("bot.context_reserve_tokens", "输出/工具预留 Token", { type: "number", min: 1024, max: 8000000, step: 1024, required: true, hint: "默认 32768（32Ki），必须小于业务总预算；输出需求更大时按该角色 max_tokens 进一步收紧" })}
                ${field("bot.context_window_mode", "上下文上限模式", { type: "select", kind: "string", required: true, options: [{ value: "auto", label: "自动发现模型窗口（默认）" }, { value: "fixed", label: "固定模型侧上限（兼容旧配置）" }], hint: "每轮业务预算固定为 272Ki（输入上限 245760，含人设、工具定义、记忆、历史、本轮、工具结果与输出预留）。自动模式会再查询模型真实窗口，只在模型更小时进一步收紧；固定模式不查元数据，直接用右侧数值。" })}
                ${field("bot.max_context_tokens", "固定上限 Token（仅固定模式生效）", { type: "number", min: 1024, max: 2000000, step: 1, required: true, hint: "默认 278528（272K）。自动模式下它只在查不到任何模型元数据时作为保守降级值" })}
                ${field("bot.max_output_tokens", "全局最大输出 Token", { type: "number", min: 256, max: 2000000, step: 1, required: true })}
              </div>`)}
            ${advancedPanel("bot.history-budget", "历史与检索留档", "历史读取量与检索结果的新鲜窗口", `
              <div class="field-grid three">
                ${field("bot.group_history_max_messages", "群历史单次读取条数", { type: "number", min: 1, max: 20000, step: 1, required: true, hint: "默认 1000。归档按页从新到旧读取，条数或预算先到即停" })}
                ${field("bot.group_history_token_budget", "群聊历史 Token 预算", { type: "number", min: 1024, max: 2000000, step: 1, required: true, hint: "默认 278528（272K）。仅在没有模型元数据时作为保守降级值" })}
                ${field("bot.group_history_reserve_tokens", "群聊固定部分余量", { type: "number", min: 1024, max: 1000000, step: 1, required: true, hint: "默认 32768。留给系统提示词、人设、本轮消息、召回与回复预留；装配历史 + 余量必须 ≤ 生效窗口" })}
                ${field("bot.private_chat_history_token_budget", "私聊历史 Token 预算", { type: "number", min: 1024, max: 2000000, step: 1, required: true, hint: "默认 278528（272K）。仅在没有模型元数据时作为保守降级值" })}
                ${field("bot.private_chat_history_retention_days", "私聊历史保留天数", { type: "number", min: 1, max: 365, step: 1, required: true, hint: "默认 30 天；过期内容由后台巡检清理" })}
                ${field("bot.search_record_retention_days", "检索留档保留天数", { type: "number", min: 1, max: 365, step: 1, required: true, hint: "默认 30 天；检索结果留档由后台巡检按此清理" })}
                ${field("bot.search_freshness_price_hours", "价格类新鲜窗口（小时）", { type: "number", min: 1, max: 8760, step: 1, required: true, hint: "默认 24；超出窗口的价格留档注入时会标注可能已过期" })}
                ${field("bot.search_freshness_news_hours", "新闻类新鲜窗口（小时）", { type: "number", min: 1, max: 8760, step: 1, required: true, hint: "默认 48；超出窗口的新闻留档会标注可能已过期" })}
                ${field("bot.search_freshness_fact_hours", "事实类新鲜窗口（小时）", { type: "number", min: 1, max: 8760, step: 1, required: true, hint: "默认 168（7 天）；型号、参数这类事实变化很慢" })}
              </div>`)}
          </div>
        </section>
        <section class="settings-section">
          ${sectionHead("长期记忆", "从对话里提炼「跨天还有用」的稳定事实（口味、身份、关系、约定等）。本组开关保存后立即生效，无需重启。")}
          <div class="field-grid three">
            ${toggle("bot.memory_facts_enabled", "启用长期记忆（总开关）", "关掉 = 不提炼、不注入、不写入（群聊注入、私聊召回与 /memory 命令都会先检查它）。", false, "关闭只停用长期事实，不清空记录。群组页的「永久记忆」是另一套群级背景信息；长期事实用群内 /memory forget <编号> 删单条，或 /memory off 关闭本人记忆并标记删除本人事实。总开关关闭时，这两条命令也不可用。")}
            ${toggle("bot.memory_extract_enabled", "启用后台提炼", "从群聊/私聊原文里被动提炼事实；关掉后不再有新的提炼，已有事实仍会被召回注入")}
            ${toggle("bot.memory_tool_enabled", "启用模型主动记忆（remember 工具）", "模型可以主动写一条关于当前说话人本人的稳定事实；总开关关闭时本项无效。", false, "关掉后模型只能读、不能写：工具仍可被调用，但不会落库。")}
            ${advancedPanel("bot.memory-tuning", "长期记忆细调", "提炼节奏与额度限制，默认值即可满足绝大多数场景", `
              <div class="field-grid three">
                ${field("bot.memory_extract_interval_minutes", "提炼巡检间隔（分钟）", { type: "number", min: 5, max: 1440, step: 1, required: true, hint: "默认 30" })}
                ${field("bot.memory_extract_min_messages", "触发提炼所需最少新消息", { type: "number", min: 5, max: 500, step: 1, required: true, hint: "默认 20；未达到就跳过这一轮，等待位置保持不变" })}
                ${field("bot.memory_extract_daily_cap", "每天最多提炼次数", { type: "number", min: 0, max: 500, step: 1, required: true, hint: "默认 48；0 表示不限。用于控制成本" })}
                ${field("bot.memory_extract_batch_max", "单批最大条数", { type: "number", min: 20, max: 1000, step: 10, required: true, hint: "默认 200；超出 Token 预算时从最旧一端整条丢弃" })}
                ${field("bot.memory_tool_daily_cap", "remember 工具每日上限", { type: "number", min: 0, max: 200, step: 1, required: true, hint: "默认 30（每个作用域每天，0 表示不限）", help: "群作用域另有一道「每个成员每天」的限制，普通成员不能独占全群额度。调高上限前请先确认成本可接受。" })}
                ${field("bot.memory_recall_limit", "注入条数上限", { type: "number", min: 1, max: 20, step: 1, required: true, hint: "默认 8；每轮注入的事实条数与字符数都不超过它" })}
                ${field("bot.memory_event_ttl_days", "有期限事实的保留天数", { type: "number", min: 1, max: 365, step: 1, required: true, hint: "默认 30；到期的事实不再注入并标记删除" })}
                ${field("bot.memory_deleted_retention_days", "已删除事实保留天数", { type: "number", min: 1, max: 365, step: 1, required: true, hint: "默认 30；被 /memory off 或被新事实替代的记录，保留这么多天后才物理清理" })}
              </div>`)}
          </div>
        </section>
        <section class="settings-section">
          ${sectionHead("主动发言", "群组页可逐群开关并设置任务简述；这里配置全局节奏。")}
          <div class="field-grid three">
            ${toggle("bot.proactive_default_enabled", "新群默认启用", "作为群级设置的默认策略")}
            ${field("bot.proactive_idle_minutes", "空闲触发（分钟）", { type: "number", min: 180, max: 43200, step: 1, required: true })}
            ${field("bot.proactive_jitter_minutes", "随机抖动（分钟）", { type: "number", min: 0, max: 1440, step: 1, required: true })}
            ${field("bot.proactive_check_interval_seconds", "检查间隔（秒）", { type: "number", min: 15, max: 3600, step: 1, required: true })}
            ${field("bot.proactive_quiet_hours_start", "安静时段开始", { type: "number", min: 0, max: 23, step: 1, required: true })}
            ${field("bot.proactive_quiet_hours_end", "安静时段结束", { type: "number", min: 0, max: 23, step: 1, required: true })}
            ${field("bot.proactive_retry_minutes", "失败重试（分钟）", { type: "number", min: 5, max: 1440, step: 1, required: true })}
          </div>
        </section>
      </div>`;
  }

  function renderSafety() {
    const provider = state.config?.verification?.provider || "turnstile";
    return `
      ${pageHead("审核与验证", "管理违规判定、ban 规则低置信度质询和新成员验证。")}
      <div class="section-stack">
        <section class="settings-section">
          ${sectionHead("内容审核")}
          <div class="field-grid three">
            ${toggle("moderation.enabled", "启用内容审核", "对群消息执行审核规则")}
            ${field("moderation.warn_threshold", "ban 规则累计阈值", { type: "number", min: 1, max: 100, step: 1, required: true, hint: "仅 action=ban 的规则会累计并在达标后封禁" })}
            ${field("moderation.high_confidence_threshold", "高置信度阈值", { type: "number", min: 0, max: 1, step: 0.01, required: true, hint: "低于阈值时，warn/delete 保持原动作；只有 ban 规则进入真人质询" })}
            ${field("moderation.challenge_timeout_seconds", "ban 质询超时（秒）", { type: "number", min: 60, max: 86400, step: 1, required: true })}
            ${toggle("moderation.bot_screening_enabled", "审核其他 bot 消息", "访客模式等 bot 消息先审核，累计干净消息达标后加入白名单。", false, "白名单由 Bot 自行累积，不影响人工添加的豁免名单。")}
            ${field("moderation.bot_screening_message_count", "bot 白名单所需干净消息数", { type: "number", min: 1, max: 100, step: 1, required: true })}
            ${toggle("moderation.nsfw_image_guard_enabled", "群内色情图片处置", "默认关闭，需要在此显式打开。", false, "复用图片描述时的视觉判定：命中后删图 + 群内 @警告（2 分钟后自动删除）+ 发起质询。只处理图片（贴纸不受影响），带 /av 的图片由识图流程负责。")}
            ${toggle("moderation.punish_quoted_author_enabled", "处罚被引用的原作者", "默认关闭，需要在此显式打开。", false, "引用/转发内容命中封禁规则且置信度足够高时，连同被引用消息的原作者一起处置（删除其消息 + 记违规 + 发起质询）。原作者是管理员、群主、豁免用户，或属于警示式引用时会跳过。关闭后只处理转发者。")}
            ${field("moderation.quoted_author_max_age_seconds", "引用追溯上限（秒）", { type: "number", min: 0, max: 31536000, step: 1, required: true, hint: "被引用消息超过该时长不再追溯原作者（只记日志），默认 604800 秒 = 7 天" })}
            ${toggle("moderation.admin_moderation_enabled", "管理员也走审核", "默认关闭，需要在此显式打开。", false, "开启后，除最高管理员外的管理员与群主不再整段跳过：照常判定，命中后只删消息 + 群内 @警示 + 记违规，不质询、不封禁、不禁言、不累计警告。最高管理员与手动豁免名单始终完全跳过。关闭后恢复为只审核普通成员。")}
            ${toggle("moderation.admin_alert_super_admin_enabled", "管理员违规私聊证据", "管理员命中违规时私聊最高管理员完整证据。", false, "证据包含对象、时间、命中规则、置信度、判定理由、送审原文与已执行动作，带图时附上图片。尽力发送，不会刷屏；普通成员违规不发送私聊。")}
            ${toggle("moderation.log_channel_enabled", "证据投递到频道", "开启后每条命中单独发一条证据卡（带「人工放行 / 确认封禁」按钮）；关闭后回到私聊最高管理员的老路径（含聚合）。")}
            ${field("moderation.log_channel_id", "证据频道 ID", { type: "number", min: -1009999999999, max: 0, step: 1, required: true, hint: "0 = 未配置：不会向任何频道投递，命中证据按原路径私聊最高管理员。填 -100 开头的频道 id 才会启用频道投递。", help: "Telegram 频道 chat id 恒为 -100 开头的负数。填 0（或留空按 0 处理）表示未配置；此时「证据投递到频道」开关打开也没有投递目标，行为与关闭时一致——这是安全默认，公开部署不会误投到任何人的私人频道。", helpLabel: "为什么默认是 0" })}
            ${field("moderation.review_handover_mention", "交接对象", { maxlength: 33, placeholder: "@your_bot", hint: "留空 = 不 @ 任何人，只发交接文案。必须是合法 Telegram 用户名（@ 开头，字母开头，5-32 位字母数字下划线）。", help: "审核规则需要调整时，证据卡末尾会 @ 这个账号并附一条 mention 实体。留空时不会出现任何 @；页面上也不会用「对空串做 rfind」之类的写法凭空造出一个假 mention。", helpLabel: "交接 mention 的行为" })}
          </div>
        </section>
        <section class="settings-section">
          ${sectionHead("入群验证默认值", "未单独配置的群组使用这里的开关和验证服务；群组管理员可在群组页覆盖。")}
          <div class="field-grid three">
            ${toggle("verification.enabled", "默认启用新成员验证", "通过当前选择的人机验证服务后恢复群权限")}
            ${field("verification.timeout_seconds", "验证超时（秒）", { type: "number", min: 60, max: 86400, step: 1, required: true })}
            ${field("verification.check_interval_seconds", "状态检查间隔（秒）", { type: "number", min: 5, max: 3600, step: 0.1, required: true })}
            ${field("verification.provider", "默认验证服务", { type: "select", options: [
              { value: "turnstile", label: "Cloudflare Turnstile" },
              { value: "hcaptcha", label: "hCaptcha" },
              { value: "turnstile_hcaptcha", label: "Turnstile + hCaptcha（双重验证）" },
            ] })}
            ${field("verification.turnstile_site_key", "Turnstile Site Key", { maxlength: 255 })}
            ${secretField("verification.turnstile_secret_key", "Turnstile Secret Key", "必须填写 Cloudflare 控制台中的 Secret Key，不能重复 Site Key；空白不会覆盖已保存值")}
            ${field("verification.hcaptcha_site_key", "hCaptcha Site Key", { maxlength: 255 })}
            ${secretField("verification.hcaptcha_secret_key", "hCaptcha Secret Key", "空白不会覆盖已保存值")}
          </div>
          <div class="notice info">${icon("refresh-cw")}<span>默认使用 ${provider === "turnstile_hcaptcha" ? "Turnstile + hCaptcha 双重验证（需依次通过两项，且两套密钥都必须配置）" : provider === "hcaptcha" ? "hCaptcha" : "Cloudflare Turnstile"}；两套密钥可同时保留并随时切换，群组可单独选择服务。</span></div>
        </section>
        <section class="settings-section">
          ${sectionHead("自动巡检", "按时批量复查已知成员的名字和简介是否违反群规；违规者禁言并发起真人质询，通过恢复权限，超时移出群聊（不封禁）。群组页可逐群覆盖开关和手动触发。")}
          <div class="field-grid three">
            ${toggle("patrol.enabled", "默认启用自动巡检", "需要已配置真人验证服务；群组管理员可在群组页覆盖")}
            ${field("patrol.schedule_time", "每日巡检时间", { maxlength: 5, required: true, placeholder: "04:30", hint: "24 小时制 HH:MM（Asia/Shanghai）" })}
            ${field("patrol.batch_size", "分批大小", { type: "number", min: 10, max: 5000, step: 1, required: true, hint: "每批检查的成员数，默认 500" })}
            ${field("patrol.batch_pause_seconds", "批间停顿（秒）", { type: "number", min: 0, max: 600, step: 0.5, required: true })}
            ${field("patrol.challenge_timeout_seconds", "质询超时（秒）", { type: "number", min: 60, max: 86400, step: 1, required: true, hint: "超时未完成质询将被移出群聊（可重新加入）" })}
            ${field("patrol.check_interval_seconds", "调度检查间隔（秒）", { type: "number", min: 15, max: 3600, step: 1, required: true })}
            ${toggle("patrol.fetch_bio", "巡检时抓取简介", "逐个调用 getChat 获取简介，更全面但更慢")}
          </div>
        </section>
        <section class="settings-section">
          ${sectionHead("呼叫管理员", "群成员发送 @admin 时 @ 群管理员，用于举报或紧急呼叫；群组页可逐群覆盖开关并选择要 @ 的管理员（默认全部）。")}
          <div class="field-grid three">
            ${toggle("call_admin.enabled", "默认启用呼叫管理员", "群组管理员可在群组页覆盖")}
            ${toggle("call_admin.pin_message", "呼叫管理员通知自动置顶", "默认关闭；管理员可标记已处理并取消置顶，群组管理员可在群组页覆盖")}
            ${field("call_admin.cooldown_seconds", "呼叫冷却（秒）", { type: "number", min: 0, max: 86400, step: 1, required: true, hint: "同群两次 @admin 之间的最小间隔，防刷屏" })}
          </div>
        </section>
        <section class="settings-section">
          ${sectionHead("民主投票封禁", "群成员可回复消息发送 /voteban，或明确要求 Bot 调用技能发起投票；两个入口共享持久化的单用户次数额度。票数达标即在本群封禁，管理员与最高管理员不可被投票。群组页可逐群覆盖各项。")}
          <div class="field-grid three">
            ${toggle("vote_ban.enabled", "默认启用民主投票封禁", "群组管理员可在群组页覆盖")}
            ${toggle("vote_ban.pin_message", "民主投票活动自动置顶", "默认开启；投票完成、管理员终止或超时后取消置顶，群组管理员可在群组页覆盖")}
            ${field("vote_ban.vote_threshold", "封禁票数阈值", { type: "number", min: 2, max: 1000, step: 1, required: true, hint: "含发起人自动投出的第一票" })}
            ${field("vote_ban.duration_seconds", "投票有效期（秒）", { type: "number", min: 60, max: 86400, step: 1, required: true, hint: "超时未达票数的投票自动失效" })}
            ${field("vote_ban.trigger_limit", "单用户触发上限", { type: "number", min: 1, max: 1000, step: 1, required: true, hint: "命令和 AI 技能共用同一额度" })}
            ${field("vote_ban.trigger_window_seconds", "触发统计窗口（秒）", { type: "number", min: 60, max: 604800, step: 1, required: true, hint: "默认 3600 秒内最多触发 3 次；重启后额度仍保留" })}
          </div>
        </section>
        <section class="settings-section">
          ${sectionHead("爆破防护", "短时间内大量成员加入时自动锁群：锁定期内任何新加入的成员都被移出（不封禁），并只发送一条防护提示；同时追溯锁定前进群的成员，统一 @ 要求真人质询，超时未通过将被移出（可重新加入）。群组页可逐群覆盖开关和各项阈值。")}
          <div class="field-grid three">
            ${toggle("raid_guard.enabled", "默认启用爆破防护", "需要已配置真人验证服务用于追溯质询；群组管理员可在群组页覆盖")}
            ${toggle("raid_guard.pin_message", "爆破防护活动自动置顶", "默认开启；自动或手动结束防护时取消置顶，群组管理员可在群组页覆盖")}
            ${field("raid_guard.join_threshold", "触发阈值（人数）", { type: "number", min: 2, max: 1000, step: 1, required: true, hint: "检测窗口内加入人数达到该值即触发锁定" })}
            ${field("raid_guard.window_seconds", "检测窗口（秒）", { type: "number", min: 5, max: 3600, step: 1, required: true })}
            ${field("raid_guard.lockdown_seconds", "锁定时长（秒）", { type: "number", min: 60, max: 86400, step: 1, required: true, hint: "锁定期内的新加入会刷新锁定时间" })}
            ${field("raid_guard.lookback_seconds", "追溯窗口（秒）", { type: "number", min: 0, max: 86400, step: 1, required: true, hint: "触发前该时间段内进群的成员将被要求真人质询" })}
            ${field("raid_guard.challenge_timeout_seconds", "质询超时（秒）", { type: "number", min: 60, max: 86400, step: 1, required: true, hint: "超时未完成质询将被移出群聊（可重新加入）" })}
          </div>
        </section>
        ${renderReplayPanel()}
      </div>`;
  }

  function renderOperations() {
    // 提示**始终**在页面上，不只在"有未保存的改动"时出现：这一页同时含热字段与
    // 冷字段，只在 dirty 时提示等于在保存后立刻把"这些要重启"的话收走。
    const pendingRestart = state.document?.restart_pending || [];
    const restartNotice = `<div class="notice info">${icon("rotate-ccw")}<span><strong>三种生效时机</strong>：一般业务项保存后立即生效（业务动作开始时现取）；带「需重启」标记的资源容量已经保存，但要重启进程才真正生效；提醒时段只改命令/CLI 接受哪些 --slot，还需要你同步修改外部 cron 的时间表。${pendingRestart.length ? `<br>当前等待重启生效：${escapeHtml(pendingRestart.join("、"))}` : ""}</span></div>`;
    return `
      ${pageHead("运营参数", "积分、活跃激励、私聊额度、签到提醒与对外文案。全部由最高管理员编辑。一般业务项保存后立即生效；带「需重启」标记的资源容量要重启进程才生效；提醒时段还需要同步修改外部 cron。")}
      ${restartNotice}
      <div class="section-stack">
        <section class="settings-section">
          ${sectionHead("签到与积分", "签到每日奖励封顶、积分榜、审核质询免除价。免除价不能设为 0——0 等于免费绕过质询。")}
          <div class="field-grid three">
            ${field("economy.checkin_daily_point_cap", "签到每日封顶（分）", { type: "number", min: 1, max: 100, step: 1, required: true, hint: "连续第 N 天得 N 分，到该值后封顶" })}
            ${field("economy.checkin_rank_limit", "积分榜名次", { type: "number", min: 1, max: 100, step: 1, required: true })}
            ${field("economy.checkin_violation_window_days", "违规统计窗口（天）", { type: "number", min: 1, max: 365, step: 1, required: true, hint: "个人档案里「近 N 天被审核命中」" })}
            ${field("economy.challenge_skip_cost", "免除质询价（分）", { type: "number", min: 1, max: 1000, step: 1, required: true, hint: "下界固定为 1，不提供 0（免费绕过）" })}
          </div>
        </section>

        <section class="settings-section">
          ${sectionHead("头衔与置顶", "价格单位是积分。头衔长度上限 16 字是 Telegram 原生协议，不能放开；防冒充词表固定在代码里，不在这里配置。")}
          <div class="field-grid three">
            ${field("economy.tag_price_7d", "头衔 7 天价（分）", { type: "number", min: 0, max: 100000, step: 1, required: true })}
            ${field("economy.tag_days_7d", "头衔 7 天时长（天）", { type: "number", min: 1, max: 3650, step: 1, required: true })}
            ${field("economy.tag_price_30d", "头衔 30 天价（分）", { type: "number", min: 0, max: 100000, step: 1, required: true })}
            ${field("economy.tag_days_30d", "头衔 30 天时长（天）", { type: "number", min: 1, max: 3650, step: 1, required: true })}
            ${field("economy.pin_price", "置顶价（分）", { type: "number", min: 0, max: 100000, step: 1, required: true })}
            ${field("economy.pin_hours", "置顶时长（小时）", { type: "number", min: 1, max: 720, step: 1, required: true })}
            ${field("economy.tag_max_length", "头衔字数上限", { type: "number", min: 1, max: 16, step: 1, required: true, hint: "上界固定为 16（Telegram 原生协议，不能放开）；防冒充词表固定在代码里，不在这里配置", help: "头衔长度是 Telegram 的原生规则，不是运营旋钮。防冒充管理员的词表（管理员/群主/admin/官方…）同样固定在源码里，配置不能删掉它——否则部署者可以让任何人买一个「官方」头衔。", helpLabel: "为什么这两项不给自由配" })}
          </div>
          ${advancedPanel("economy.expiry", "到期权益清理", "扫描间隔、单轮预算、批量与失败重推", `
            <div class="field-grid three">
              ${field("economy.expiry_check_seconds", "扫描间隔（秒）", { type: "number", min: 30, max: 86400, step: 1, required: true })}
              ${field("economy.expiry_pass_deadline_seconds", "单轮预算（秒）", { type: "number", min: 10, max: 3600, step: 1, required: true, hint: "一轮扫太久会挤占事件循环" })}
              ${field("economy.expiry_batch_limit", "单轮批量（条）", { type: "number", min: 1, max: 5000, step: 1, required: true })}
              ${field("economy.expiry_retry_seconds", "失败重推间隔（秒）", { type: "number", min: 30, max: 86400, step: 1, required: true, hint: "撤不下来时把到期时间往后推这么多，下次扫描再试" })}
            </div>`)}
        </section>

        <section class="settings-section">
          ${sectionHead("抽奖奖池", "每行 = 中奖积分 / 权重 / 标签。权重是相对值，总权重由各行相加得到；期望值会在下方实时算给你看。")}
          <div id="lottery-editor">${rewardEditor("economy.lottery_prizes", { label: "奖池档位", hint: "至少保留一档；总权重由各档相加自动得到，不需要另外填写。保存时后端会校验权重之和大于 0。" })}</div>
          ${advancedPanel("economy.lottery-table", "奖池明细", "默认 7 档、总权重 10000，期望值 6.00 分/次", `
            <div class="field-grid three">
              ${field("economy.lottery_price", "抽奖价（分）", { type: "number", min: 1, max: 100000, step: 1, required: true })}
              ${field("economy.lottery_daily_limit", "每日次数上限", { type: "number", min: 1, max: 1000, step: 1, required: true })}
            </div>
            <div id="lottery-editor-advanced"></div>`)}
        </section>

        <section class="settings-section">
          ${sectionHead("活跃激励", "每周榜单的奖励向量：下标 0 = 第 1 名。榜单长度与周奖励总额都由这个向量派生，不会出现两处数字互相矛盾。得分公式（发言 + 活跃天数×2 + 被回复次数）不随本页变化。")}
          <div class="field-grid three">
            ${field("activity.min_message_text_length", "有效发言最小长度（字符）", { type: "number", min: 1, max: 100, step: 1, required: true })}
            ${field("activity.max_daily_messages", "每天计入上限（条）", { type: "number", min: 1, max: 500, step: 1, required: true, hint: "写入时就封顶，防刷屏" })}
            ${field("activity.min_active_days", "参与门槛 · 活跃天数", { type: "number", min: 1, max: 365, step: 1, required: true })}
            ${field("activity.min_weekly_messages", "参与门槛 · 发言条数", { type: "number", min: 1, max: 100000, step: 1, required: true })}
            ${field("activity.weekly_reward_points", "每周奖励向量（分）", { kind: "array", full: true, hint: "逗号分隔，从第 1 名开始。默认 25, 12, 12, 4, 4, 4, 4, 4, 4, 4（10 名、合计 77）", help: "向量长度 = 榜单名次数，向量求和 = 本周奖励总额。两者都从这里派生，不再单独存一份，避免互相矛盾。", helpLabel: "为什么不再单独填「总额」" })}
          </div>
        </section>

        <section class="settings-section">
          ${sectionHead("私聊额度", "只对最高管理员开放。普通成员与群管理员是两套独立档位，各有单人上限与全局上限，互不占用。")}
          <div class="field-grid three">
            ${field("private_chat.per_user_daily_limit", "普通成员 · 每人每天（条）", { type: "number", min: 1, max: 100000, step: 1, required: true })}
            ${field("private_chat.admin_per_user_daily_limit", "群管理员 · 每人每天（条）", { type: "number", min: 1, max: 100000, step: 1, required: true })}
            ${field("private_chat.global_daily_limit", "普通成员 · 全网每天（条）", { type: "number", min: 1, max: 10000000, step: 1, required: true })}
            ${field("private_chat.admin_global_daily_limit", "群管理员 · 全网每天（条）", { type: "number", min: 1, max: 10000000, step: 1, required: true })}
            ${field("private_chat.input_max_chars", "单条输入上限（字符）", { type: "number", min: 1, max: 4096, step: 1, required: true })}
            ${field("private_chat.reply_max_chars", "单条回复分片（字符）", { type: "number", min: 512, max: 4096, step: 1, required: true, hint: "上界 4096 是 Telegram 协议硬上限，不能放开" })}
            ${field("private_chat.vision_budget_seconds", "识图预算（秒）", { type: "number", min: 5, max: 120, step: 0.5, required: true })}
            ${field("private_chat.memory_turns", "内存兜底轮数", { type: "number", min: 1, max: 200, step: 1, required: true, hint: "只在读库失败时兜底；真正的历史按 token 预算从库里读" })}
            ${field("private_chat.access_ttl_seconds", "成员准入缓存（秒）", { type: "number", min: 1, max: 60, step: 1, required: true, hint: "上界 60：调大会放大越权窗口，所以只能收紧", help: "缓存里除了档位还带着「已确认他在哪些授权群里」。被踢出群之后这个集合要尽快过期，所以 TTL 的上界被钉在今天的 60 秒——这不是保守，是不允许通过调大配置延长越权窗口。", helpLabel: "为什么只能调小" })}
            ${field("private_chat.search_daily_limit", "私聊检索保险丝（次/天）", { type: "number", min: 1, max: 100000, step: 1, required: true, hint: "热改不会重置当天已消耗的次数；午夜滚动" })}
            ${field("private_chat.voice_max_segments", "单条语音最多段数", { type: "number", min: 1, max: 20, step: 1, required: true })}
          </div>
        </section>

        <section class="settings-section">
          ${sectionHead("签到提醒", "时段只约束命令/CLI 接受哪些 --slot；真正几点发由外部 cron 决定。改这里不会、也无法自动更新 cron 的时间表——保存后需要你同步修改部署侧的 crontab。")}
          <div class="field-grid three">
            ${field("checkin_reminder.slots", "提醒时段（本地小时）", { kind: "array", hint: "逗号分隔的整点小时，默认 9, 12, 15, 18。必须 0-23 且去重排序。", help: "这里改的是**命令行白名单与文案**，不是调度表。真正几点发由外部 cron 决定（bot/tools/checkin_reminder.py 的 docstring 里有样例 crontab）；保存后需要你同步修改部署侧的 crontab，否则新时段不会真的被触发。", helpLabel: "与 cron 的关系" })}
            ${field("checkin_reminder.slot_greetings", "时段问候语", { kind: "json-object", full: true, rows: 4, hint: "形如 {\"9\": \"早上好\"}；键必须落在上面的时段里，值不能含 HTML 标签。" })}
            ${field("checkin_reminder.auto_delete_seconds", "发出后自动删除（秒）", { type: "number", min: 0, max: 86400, step: 1, required: true, hint: "0 = 不自动删除" })}
            ${field("checkin_reminder.roster_max_names", "名单最多昵称数", { type: "number", min: 1, max: 200, step: 1, required: true })}
            ${field("checkin_reminder.stale_grace_seconds", "空占位宽限（秒）", { type: "number", min: 60, max: 86400, step: 1, required: true, hint: "必须远大于一次「claim → 读名单 → 发消息」的正常耗时" })}
          </div>
        </section>

        <section class="settings-section">
          ${sectionHead("对外文案", "只改用户看得见的字。callback_data（checkin:v1）、深链前缀（shop_）是协议标识，不可配。人设提示词在 Prompts 页单独编辑，这里不会另造一份人格。")}
          <div class="field-grid three">
            ${field("display.bot_display_name", "显示名", { maxlength: 32, required: true, hint: "用于联网检索时剥掉问句里的称呼" })}
            ${field("display.private_voice_title", "私聊语音条标题", { maxlength: 64, required: true })}
            ${field("display.checkin_button_text", "签到按钮文字", { maxlength: 64, required: true })}
            ${field("display.shop_button_text", "商店按钮文字", { maxlength: 64, required: true })}
            ${field("display.search_query_prefixes", "检索唤醒前缀", { kind: "array", hint: "逗号分隔；显示名会自动并入一起剥离" })}
            ${field("display.private_not_member_notice", "私聊 · 非成员提示", { type: "textarea", rows: 2, maxlength: 2000, full: true })}
            ${field("display.private_limit_notice", "私聊 · 本人超限提示", { type: "textarea", rows: 2, maxlength: 2000, full: true })}
            ${field("display.private_global_limit_notice", "私聊 · 全局超限提示", { type: "textarea", rows: 2, maxlength: 2000, full: true })}
            ${field("display.private_media_unsupported_notice", "私聊 · 不支持的媒体提示", { type: "textarea", rows: 2, maxlength: 2000, full: true })}
          </div>
        </section>

        <section class="settings-section">
          ${sectionHead("高级资源", "进程级预算。标量保存后下一次动作生效；带「需重启」标记的是绑在模块级闸门上的并发容量，改完必须重启。默认全部等于今天真实生效的值。")}
          ${advancedPanel("resources.all", "展开高级资源", "并发闸门、超时、批量、维护间隔", `
            <div class="field-grid three">
              ${field("resources.llm_request_capacity", "LLM 总容量", { type: "number", min: 2, max: 64, step: 1, required: true })}
              ${field("resources.llm_request_noncritical_capacity", "LLM 非关键容量", { type: "number", min: 1, max: 64, step: 1, required: true, hint: "必须 < 总容量：至少留 1 个关键名额" })}
              ${field("resources.llm_request_normal_capacity", "LLM 普通容量", { type: "number", min: 1, max: 64, step: 1, required: true })}
              ${field("resources.llm_request_background_capacity", "LLM 背景容量", { type: "number", min: 1, max: 64, step: 1, required: true, hint: "必须 ≤ 普通容量 − 2" })}
              ${field("resources.llm_tokenizer_concurrency", "分词线程数", { type: "number", min: 1, max: 8, step: 1, required: true })}
              ${field("resources.llm_stage_deadlines", "LLM 阶段预算（秒）", { kind: "json-object", full: true, rows: 9, hint: "形如 {\"decision\": 35, \"moderation\": 35, ...}。角色上显式设置的总预算仍然优先。" })}
              ${field("resources.telegram_critical_admission_timeout_seconds", "关键请求准入等待（秒）", { type: "number", min: 0.1, max: 60, step: 0.1, required: true, hint: "热生效：每次请求现取" })}
              ${field("resources.telegram_high_admission_timeout_seconds", "高优请求准入等待（秒）", { type: "number", min: 0.1, max: 60, step: 0.1, required: true })}
              ${field("resources.telegram_normal_admission_timeout_seconds", "普通请求准入等待（秒）", { type: "number", min: 0.1, max: 60, step: 0.1, required: true })}
              ${field("resources.telegram_privileged_timeout_seconds", "特权请求 HTTP 上限（秒）", { type: "number", min: 0.1, max: 60, step: 0.1, required: true })}
              ${field("resources.telegram_total_capacity", "Telegram 总容量", { type: "number", min: 8, max: 512, step: 1, required: true })}
              ${field("resources.telegram_noncritical_capacity", "Telegram 非关键容量", { type: "number", min: 4, max: 512, step: 1, required: true })}
              ${field("resources.telegram_normal_capacity", "Telegram 普通容量", { type: "number", min: 2, max: 512, step: 1, required: true })}
              ${field("resources.pending_reply_execution_capacity", "群待回复并发", { type: "number", min: 1, max: 32, step: 1, required: true })}
              ${field("resources.pending_reply_timeout_seconds", "群待回复预算（秒）", { type: "number", min: 5, max: 120, step: 0.5, required: true, hint: "群级设置优先，这里是兜底默认值" })}
              ${field("resources.tts_synthesis_concurrency", "TTS 合成并发", { type: "number", min: 1, max: 8, step: 1, required: true })}
              ${field("resources.tts_transcode_concurrency", "TTS 转码并发", { type: "number", min: 1, max: 8, step: 1, required: true })}
              ${field("resources.tts_private_concurrency", "私聊 TTS 并发", { type: "number", min: 1, max: 8, step: 1, required: true })}
              ${field("resources.tts_max_segments_per_message", "单条最多合成段数", { type: "number", min: 1, max: 20, step: 1, required: true })}
              ${field("resources.tts_transcode_timeout_seconds", "转码超时（秒）", { type: "number", min: 5, max: 300, step: 1, required: true })}
              ${field("resources.tts_max_http_timeout_seconds", "TTS http 上限（秒）", { type: "number", min: 5, max: 600, step: 1, required: true })}
              ${field("resources.av_query_concurrency", "AV 查询并发", { type: "number", min: 1, max: 8, step: 1, required: true })}
              ${field("resources.av_query_deadline_seconds", "AV 查询预算（秒）", { type: "number", min: 5, max: 300, step: 0.5, required: true })}
              ${field("resources.av_query_admission_timeout_seconds", "AV 准入等待（秒）", { type: "number", min: 0.1, max: 60, step: 0.1, required: true })}
              ${field("resources.av_star_name_cache_max", "AV 名字缓存条数", { type: "number", min: 64, max: 8192, step: 1, required: true })}
              ${field("resources.admin_alert_window_seconds", "管理员告警窗口（秒）", { type: "number", min: 10, max: 86400, step: 1, required: true })}
              ${field("resources.admin_alert_aggregate_after", "管理员告警合并阈值", { type: "number", min: 2, max: 1000, step: 1, required: true })}
              ${field("resources.admin_alert_state_limit", "管理员告警状态上限", { type: "number", min: 16, max: 65536, step: 1, required: true })}
              ${field("resources.admin_alert_text_limit", "管理员告警截断字数", { type: "number", min: 100, max: 4000, step: 1, required: true })}
              ${field("resources.moderation_throttle_burst", "送审整形 burst", { type: "number", min: 1, max: 64, step: 1, required: true, hint: "不改变默认行为；改它不会关掉任何本地规则" })}
              ${field("resources.moderation_throttle_spacing_seconds", "送审整形间隔（秒）", { type: "number", min: 0.1, max: 120, step: 0.1, required: true })}
              ${field("resources.moderation_throttle_max_wait_seconds", "送审整形单条最多等（秒）", { type: "number", min: 0.1, max: 120, step: 0.1, required: true })}
              ${field("resources.moderation_throttle_max_waiters", "送审整形等待者上限", { type: "number", min: 1, max: 256, step: 1, required: true })}
              ${field("resources.decision_history_token_budget", "判定历史 token 预算", { type: "number", min: 512, max: 1000000, step: 1, required: true })}
              ${field("resources.decision_history_max_messages", "判定历史条数上限", { type: "number", min: 1, max: 1000, step: 1, required: true })}
              ${field("resources.memory_max_facts_per_extraction", "单次提炼最多事实数", { type: "number", min: 1, max: 20, step: 1, required: true })}
              ${field("resources.memory_extract_input_token_limit", "提炼输入上限（token）", { type: "number", min: 512, max: 200000, step: 1, required: true })}
              ${field("resources.memory_extract_scope_limit", "每轮作用域上限", { type: "number", min: 1, max: 200, step: 1, required: true })}
              ${field("resources.memory_candidate_row_limit", "召回候选行上限", { type: "number", min: 10, max: 5000, step: 1, required: true })}
              ${field("resources.memory_private_group_fanout", "私聊参考群数上限", { type: "number", min: 1, max: 20, step: 1, required: true })}
              ${field("resources.memory_tool_subject_daily_cap", "单人 remember 每日上限", { type: "number", min: 1, max: 200, step: 1, required: true })}
              ${field("resources.memory_maintenance_interval_seconds", "记忆维护间隔（秒）", { type: "number", min: 300, max: 86400, step: 1, required: true })}
              ${field("resources.search_prune_interval_seconds", "检索留档清理间隔（秒）", { type: "number", min: 300, max: 86400, step: 1, required: true })}
              ${field("resources.search_record_recall_limit", "检索留档召回条数", { type: "number", min: 1, max: 50, step: 1, required: true })}
              ${field("resources.archive_batch_size", "向量归档批大小", { type: "number", min: 1, max: 512, step: 1, required: true })}
              ${field("resources.archive_backfill_per_pass", "向量回填每轮条数", { type: "number", min: 1, max: 5000, step: 1, required: true })}
              ${field("resources.archive_scan_limit", "向量扫描上限", { type: "number", min: 100, max: 100000, step: 1, required: true })}
              ${field("resources.archive_candidate_limit", "向量候选上限", { type: "number", min: 1, max: 5000, step: 1, required: true })}
              ${field("resources.archive_query_timeout_seconds", "向量查询超时（秒）", { type: "number", min: 0.5, max: 120, step: 0.5, required: true })}
              ${field("resources.archive_maintenance_interval_seconds", "向量维护间隔（秒）", { type: "number", min: 0.5, max: 600, step: 0.5, required: true })}
              ${field("resources.archive_indexing_lease_seconds", "索引租约（秒）", { type: "number", min: 5, max: 3600, step: 1, required: true, hint: "必须不短于查询超时，否则会重复写" })}
              ${field("resources.telegram_send_chat_parallel", "同会话并发发送", { type: "number", min: 1, max: 32, step: 1, required: true, hint: "每次发送现建信号量，热生效" })}
            </div>`)}
          ${advancedPanel("admin_ops.all", "管理端与群内操作节奏", "分页、名单通知、特权批处理、视觉输入上限、出站发送", `
            <div class="field-grid three">
              ${field("admin_ops.list_page_size", "列表分页（行/页）", { type: "number", min: 1, max: 50, step: 1, required: true, hint: "管理端所有列表共用一个页大小" })}
              ${field("admin_ops.roster_notice_auto_delete_seconds", "名单通知自动删除（秒）", { type: "number", min: 0, max: 3600, step: 1, required: true, hint: "0 = 不自动删除" })}
              ${field("admin_ops.privileged_group_concurrency", "特权批处理并发群数", { type: "number", min: 1, max: 16, step: 1, required: true })}
              ${field("admin_ops.privileged_group_deadline_seconds", "单群截止（秒）", { type: "number", min: 5, max: 120, step: 0.5, required: true })}
              ${field("admin_ops.privileged_job_deadline_seconds", "特权任务总截止（秒）", { type: "number", min: 30, max: 900, step: 1, required: true, hint: "必须严格大于单群截止，否则内层还没跑完外层就掐断" })}
              ${field("group_ops.vision_image_max_bytes", "视觉下载体积上限（字节）", { type: "number", min: 262144, max: 20971520, step: 1024, required: true, hint: "默认 5 MiB；这是内存安全线，只可收紧，0 不表示无限" })}
              ${field("group_ops.vision_download_timeout_seconds", "视觉下载超时（秒）", { type: "number", min: 5, max: 60, step: 0.5, required: true, hint: "超时即取消下载并放弃本次判定" })}
              ${field("group_ops.vision_text_max_chars", "视觉描述注入上限（字符）", { type: "number", min: 100, max: 4096, step: 1, required: true })}
              ${field("group_ops.reply_targets_max_chars", "回复候选注入上限（字符）", { type: "number", min: 512, max: 4000, step: 1, required: true, hint: "上界 4000 = 现值 = 预算上限，不可放大" })}
              ${field("group_ops.nsfw_warning_auto_delete_seconds", "NSFW 告警自动删除（秒）", { type: "number", min: 0, max: 86400, step: 1, required: true, hint: "0 = 不自动删除；只影响展示节奏，不影响判定与封禁" })}
              ${field("group_ops.activity_debounce_seconds", "活跃度写库去抖（秒）", { type: "number", min: 0, max: 10, step: 0.1, required: true })}
              ${field("group_ops.activity_max_attempts", "活跃度写库重试次数", { type: "number", min: 1, max: 20, step: 1, required: true, hint: "调过小会放大写放大" })}
              ${field("telegram_send.send_total_deadline_seconds", "单次发送总预算（秒）", { type: "number", min: 5, max: 300, step: 0.5, required: true, hint: "含重试分片；必须大于单条 HTTP 超时（30s）" })}
              ${field("telegram_send.stream_max_incremental_edits", "流式最大增量编辑", { type: "number", min: 1, max: 50, step: 1, required: true, hint: "受 Telegram 编辑限流约束" })}
              ${field("telegram_send.stream_max_pacing_seconds", "流式节流总时长（秒）", { type: "number", min: 1, max: 60, step: 0.5, required: true })}
              ${field("telegram_send.typing_send_timeout_seconds", "typing 发送超时（秒）", { type: "number", min: 0.5, max: 10, step: 0.1, required: true, hint: "超时静默降级，不影响正文" })}
            </div>`)}
          ${advancedPanel("webhook_probe.all", "webhook 探针与 durable inbox 生命周期", "健康巡检节奏、留档保留与重试上限", `
            <div class="field-grid three">
              ${field("resources.webhook_watch_interval_seconds", "webhook 健康巡检间隔（秒）", { type: "number", min: 5, max: 300, step: 1, required: true })}
              ${field("resources.webhook_failure_threshold", "连续失败降级阈值", { type: "number", min: 1, max: 10, step: 1, required: true, hint: "达到该次数就降级为轮询" })}
              ${field("resources.webhook_probe_timeout_seconds", "探针超时（秒）", { type: "number", min: 1, max: 30, step: 0.5, required: true })}
              ${field("resources.webhook_probe_attempts", "探针重试次数", { type: "number", min: 1, max: 10, step: 1, required: true })}
              ${field("resources.webhook_inbox_retention_seconds", "inbox 留档保留（秒）", { type: "number", min: 3600, max: 7776000, step: 3600, required: true, hint: "下界 1 小时，且不得短于去重窗口 60 分钟" })}
              ${field("resources.webhook_inbox_dlq_retention_seconds", "死信保留（秒）", { type: "number", min: 3600, max: 31536000, step: 3600, required: true, hint: "必须长于正常保留期" })}
              ${field("resources.webhook_inbox_max_attempts", "重试上限（次）", { type: "number", min: 1, max: 50, step: 1, required: true, hint: "超过即入死信" })}
              ${field("resources.webhook_inbox_retry_base_seconds", "重试退避基数（秒）", { type: "number", min: 0.1, max: 600, step: 0.1, required: true, hint: "不能大于退避上限" })}
              ${field("resources.member_identity_lookup_timeout_seconds", "身份查询超时（秒）", { type: "number", min: 1, max: 15, step: 0.5, required: true, hint: "超时按「查不到」处理，不阻塞页面" })}
              ${field("resources.member_identity_lookup_concurrency", "身份查询并发", { type: "number", min: 1, max: 32, step: 1, required: true })}
              ${field("resources.member_identity_cache_max_entries", "身份缓存条数上限", { type: "number", min: 64, max: 65536, step: 64, required: true, hint: "缓存 TTL 属鉴权邻接，固定不可配" })}
              ${field("resources.verification_recovery_retry_seconds", "验证恢复重试（秒）", { type: "number", min: 60, max: 604800, step: 60, required: true, hint: "只调巡检节奏，终态判定与去重不变" })}
              ${field("resources.verification_operator_action_retry_seconds", "操作员动作重试（秒）", { type: "number", min: 60, max: 604800, step: 60, required: true })}
              ${field("resources.verification_unreachable_group_retry_seconds", "不可达群重试（秒）", { type: "number", min: 60, max: 604800, step: 60, required: true })}
              ${field("resources.verification_sweep_deadline_seconds", "单轮 sweep 截止（秒）", { type: "number", min: 30, max: 900, step: 5, required: true, hint: "超预算中止本轮，不丢状态" })}
            </div>`)}
        </section>

        <section class="settings-section">
          ${sectionHead("webhook 与轮询", "webhook 服务的 worker 数、队列容量、连接池上限都在服务启动时构造，因此这一段全部需要重启。租约与端到端预算有关联约束：租约必须不短于最大的端到端预算，否则仍在执行的 update 会被恢复循环交给第二个 worker 重复处理。", "")}
          <div class="field-grid three">
            ${field("resources.webhook_max_concurrent_updates", "webhook 总并发", { type: "number", min: 1, max: 256, step: 1, required: true })}
            ${field("resources.webhook_critical_concurrent_updates", "管理员交互车道并发", { type: "number", min: 1, max: 256, step: 1, required: true, hint: "独立车道：入群洪峰不会堵住 /ban、/unban" })}
            ${field("resources.webhook_security_concurrent_updates", "入群风控车道并发", { type: "number", min: 1, max: 256, step: 1, required: true })}
            ${field("resources.webhook_auth_concurrent_updates", "鉴权车道并发", { type: "number", min: 1, max: 256, step: 1, required: true })}
            ${field("resources.webhook_critical_queue_capacity", "管理员交互队列容量", { type: "number", min: 1, max: 100000, step: 1, required: true, hint: "不能小于该车道并发" })}
            ${field("resources.webhook_security_queue_capacity", "入群风控队列容量", { type: "number", min: 1, max: 100000, step: 1, required: true, hint: "不能小于该车道并发" })}
            ${field("resources.webhook_auth_queue_capacity", "鉴权队列容量", { type: "number", min: 1, max: 100000, step: 1, required: true, hint: "不能小于该车道并发" })}
            ${field("resources.webhook_update_timeout_seconds", "普通 update 端到端（秒）", { type: "number", min: 5, max: 1800, step: 1, required: true, hint: "必须盖住防抖 + 同步审核/索引 + 群回复 worker（群级可配，上界 120s）" })}
            ${field("resources.webhook_critical_update_timeout_seconds", "管理员交互端到端（秒）", { type: "number", min: 1, max: 1800, step: 1, required: true })}
            ${field("resources.webhook_security_update_timeout_seconds", "入群风控端到端（秒）", { type: "number", min: 1, max: 1800, step: 1, required: true })}
            ${field("resources.webhook_auth_update_timeout_seconds", "鉴权端到端（秒）", { type: "number", min: 1, max: 1800, step: 1, required: true })}
            ${field("resources.webhook_http_response_timeout_seconds", "HTTP 响应超时（秒）", { type: "number", min: 5, max: 3600, step: 1, required: true, hint: "必须不小于最大的端到端预算" })}
            ${field("resources.webhook_inbox_lease_seconds", "durable inbox 租约（秒）", { type: "number", min: 5, max: 3600, step: 1, required: true, hint: "必须不短于最大的端到端预算与最大重试退避，否则会重复处理", help: "租约到期后 durable inbox 的恢复循环会认为这条 update 没人管，于是交给第二个 worker。如果租约比一次处理实际花的时间短，管理员的 /ban、/unban 回调可能被执行两次。保存时后端会强校验这条关联。", helpLabel: "为什么租约不能配小" })}
            ${field("resources.webhook_inbox_recovery_batch", "恢复每轮批量", { type: "number", min: 1, max: 5000, step: 1, required: true, hint: "热生效" })}
            ${field("resources.webhook_inbox_retry_max_seconds", "最大重试退避（秒）", { type: "number", min: 1, max: 3600, step: 1, required: true, hint: "热生效；不能超过租约" })}
            ${field("resources.webhook_inbox_cleanup_interval_seconds", "留档清理间隔（秒）", { type: "number", min: 5, max: 3600, step: 1, required: true, hint: "热生效" })}
            ${field("resources.webhook_inbox_cleanup_batch", "留档清理批量", { type: "number", min: 1, max: 10000, step: 1, required: true, hint: "热生效" })}
            ${field("resources.polling_timeout_seconds", "轮询长轮询超时（秒）", { type: "number", min: 1, max: 300, step: 1, required: true, hint: "仅在未启用 webhook 时使用" })}
            ${field("resources.polling_http_timeout_seconds", "轮询 HTTP 超时（秒）", { type: "number", min: 1, max: 600, step: 1, required: true })}
            ${field("resources.polling_request_timeout_seconds", "轮询请求超时（秒）", { type: "number", min: 1, max: 600, step: 0.5, required: true })}
          </div>
        </section>
      </div>`;
  }

  function renderMedia() {
    return `
      ${pageHead("媒体能力", "配置语音合成、音乐检索、AV 检索和贴纸回退。")}
      <div class="section-stack">
        <section class="settings-section">
          ${sectionHead("豆包 TTS")}
          <div class="field-grid three">
            ${toggle("tts.enabled", "启用语音合成", "群级语音模式在群组页设置")}
            ${field("tts.http_timeout_sec", "请求超时（秒）", { type: "number", min: 1, max: 300, step: 0.1, required: true })}
            ${field("tts.max_text_length", "最大文本长度", { type: "number", min: 1, max: 10000, step: 1, required: true })}
            ${field("tts.api_base", "API Base URL", { type: "url", maxlength: 1000, full: true })}
            ${field("tts.app_id", "App ID", { maxlength: 255 })}
            ${secretField("tts.app_key", "App Key", "空白不会覆盖已保存的密钥")}
            ${secretField("tts.access_key", "Access Key", "空白不会覆盖已保存的密钥")}
            ${field("tts.resource_id", "Resource ID", { maxlength: 255 })}
            ${field("tts.model", "模型", { maxlength: 255, placeholder: "使用服务默认模型" })}
            ${field("tts.speaker", "音色", { maxlength: 255, placeholder: "使用服务默认音色" })}
            ${field("tts.audio_format", "音频格式", { maxlength: 64 })}
            ${advancedPanel("tts.audio", "音频参数", "音质与语调节，保留默认即可", `
              <div class="field-grid three">
                ${field("tts.sample_rate", "采样率", { type: "number", min: 8000, max: 192000, step: 1, required: true })}
                ${field("tts.bit_rate", "比特率", { type: "number", min: 8000, max: 512000, step: 1, required: true })}
                ${field("tts.emotion", "情感", { maxlength: 64, placeholder: "不指定" })}
                ${field("tts.emotion_scale", "情感强度", { type: "number", min: 1, max: 5, step: 1, required: true })}
                ${field("tts.speech_rate", "语速调整", { type: "number", min: -100, max: 100, step: 1, required: true })}
                ${field("tts.loudness_rate", "音量调整", { type: "number", min: -100, max: 100, step: 1, required: true })}
                ${field("tts.silence_duration_ms", "尾部静音（毫秒）", { type: "number", min: 0, max: 10000, step: 1, required: true })}
              </div>`)}
          </div>
        </section>
        <section class="settings-section">
          ${sectionHead("音乐检索")}
          <div class="field-grid three">
            ${toggle("music.enabled", "启用音乐检索", "允许音乐搜索与发送")}
            ${field("music.http_timeout_sec", "请求超时（秒）", { type: "number", min: 1, max: 300, step: 0.1, required: true })}
            ${field("music.default_source", "默认音源", { maxlength: 64 })}
            ${field("music.base_url", "API 地址", { type: "url", maxlength: 1000, full: true })}
            ${field("music.stable_sources", "稳定音源", { kind: "array", full: true, hint: "多个音源使用逗号分隔" })}
          </div>
        </section>
        <section class="settings-section">
          ${sectionHead("AV 检索")}
          <div class="field-grid three">
            ${toggle("av.enabled", "启用 AV 检索", "群级开关可进一步限制")}
            ${field("av.http_timeout_sec", "请求超时（秒）", { type: "number", min: 1, max: 300, step: 0.1, required: true })}
            ${field("av.max_results", "最大结果数", { type: "number", min: 1, max: 100, step: 1, required: true })}
            ${field("av.dm_sample_count", "私聊样例图张数（0=关闭）", { type: "number", min: 0, max: 5, step: 1, required: true })}
            ${field("av.inline_seed_count", "私聊内联下载地址条数（0=关闭该块）", { type: "number", min: 0, max: 5, step: 1, required: true, hint: "硬上限 5 条；群内文案不受影响" })}
            ${toggle("av.ai_synopsis_enabled", "私聊 AI 题材概述（默认关）", "开启后每次私聊查询多一次模型调用，输出会标注「AI 概述，非官方剧情」")}
            ${advancedPanel("av.sources", "数据源地址", "使用内置地址即可，只有自建镜像才需要改", `
              <div class="field-grid three">
                ${field("av.javbus_base_url", "JavBus 地址", { type: "url", maxlength: 1000 })}
                ${field("av.madouqu_base_url", "Madouqu 地址", { type: "url", maxlength: 1000 })}
                ${field("av.dmm_base_url", "DMM 地址", { type: "url", maxlength: 1000 })}
                ${field("av.fc2_base_url", "FC2 地址", { type: "url", maxlength: 1000 })}
              </div>`)}
          </div>
        </section>
        <section class="settings-section">
          ${sectionHead("贴纸回退")}
          <div class="field-grid single">
            ${field("stickers.fallback_file_ids", "Telegram Sticker File IDs", { kind: "array", full: true, hint: "多个 File ID 使用逗号分隔" })}
          </div>
        </section>
      </div>`;
  }

  function renderIntegrations() {
    return `
      ${pageHead("外部服务", "管理第三方业务服务的连接与超时。")}
      <div class="section-stack">
        <section class="settings-section">
          ${sectionHead("影片信息（IMDb / TMDB）", "按需查询影片搜索结果、详情与实时评分。仅在已取得适用于 Bot 用途的供应商授权后启用；IMDb 官方实时 API 需要 AWS Data Exchange 商业订阅。")}
          <div class="field-grid three">
            ${toggle("movie_info.enabled", "启用影片信息查询", "配置至少一个可用数据源后供影片信息技能调用")}
            ${field("movie_info.http_timeout_sec", "单请求超时（秒）", { type: "number", min: 1, max: 6, step: 0.1, required: true, hint: "最多 6 秒，以保证 ID 映射的两段请求不超过技能总预算" })}
            ${field("movie_info.max_results", "最大结果数", { type: "number", min: 1, max: 20, step: 1, required: true })}
            ${field("movie_info.default_language", "默认语言", { maxlength: 6, placeholder: "zh-CN", required: true })}
            ${field("movie_info.default_region", "默认地区", { maxlength: 2, placeholder: "CN", required: true })}
            ${secretField("movie_info.tmdb_read_access_token", "TMDB Read Access Token", "使用请求头鉴权；空白不会覆盖已保存的密钥")}
            ${advancedPanel("movie_info.imdb", "IMDb 接入", "需要 AWS Data Exchange 商业订阅；只查 TMDB 时可全部留空", `
              <div class="field-grid three">
                ${field("movie_info.imdb_data_set_id", "IMDb Data Set ID", { maxlength: 255 })}
                ${field("movie_info.imdb_revision_id", "IMDb Revision ID", { maxlength: 255 })}
                ${field("movie_info.imdb_asset_id", "IMDb Asset ID", { maxlength: 255 })}
                ${secretField("movie_info.imdb_api_key", "IMDb API Key", "空白不会覆盖已保存的密钥")}
                ${secretField("movie_info.imdb_aws_access_key_id", "IMDb AWS Access Key ID", "空白不会覆盖已保存的密钥")}
                ${secretField("movie_info.imdb_aws_secret_access_key", "IMDb AWS Secret Access Key", "空白不会覆盖已保存的密钥")}
                ${secretField("movie_info.imdb_aws_session_token", "IMDb AWS Session Token", "临时凭据可留空；空白不会覆盖已保存的密钥")}
              </div>`)}
          </div>
        </section>
      </div>`;
  }

  function renderLogging() {
    const levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];
    return `
      ${pageHead("日志", "日志级别与文件轮转策略在保存后立即生效。")}
      <div class="section-stack">
        <section class="settings-section">
          ${sectionHead("日志级别")}
          <div class="field-grid three">
            ${field("logging.level", "应用日志级别", { type: "select", options: levels })}
            ${field("logging.third_party_level", "第三方日志级别", { type: "select", options: levels })}
            ${field("logging.color", "终端颜色", { type: "select", options: [
              { value: "on", label: "开启" }, { value: "off", label: "关闭" }, { value: "auto", label: "自动" },
            ] })}
          </div>
        </section>
        <section class="settings-section">
          ${sectionHead("文件输出")}
          <div class="field-grid three">
            ${toggle("logging.to_file", "写入日志文件", "同时保留标准输出")}
            ${field("logging.file_path", "文件路径", { maxlength: 1000 })}
            ${field("logging.file_max_bytes", "单文件最大字节", { type: "number", min: 1024, max: 10737418240, step: 1, required: true })}
            ${field("logging.file_backup_count", "保留文件数", { type: "number", min: 1, max: 100, step: 1, required: true })}
            ${field("logging.message_preview_chars", "入口日志正文预览字数", { type: "number", min: 0, max: 1000, step: 1, required: true, hint: "默认 100；设为 0 表示不记录正文，只记录长度与内容哈希前缀" })}
          </div>
        </section>
      </div>`;
  }

  // ==========================================================================
  // 共享呈现：时间标量 + 脱敏差异表（保存预览 / 历史对比 / 回滚预览共用）
  // ==========================================================================
  function formatMoment(value) {
    const text = String(value ?? "").trim();
    if (!text) return "—";
    return text.replace("T", " ").replace(/\.\d+$/, "").replace(/[+-]\d{2}:\d{2}$/, "");
  }

  function renderScalar(value) {
    if (value === undefined || value === null || value === "") return "—";
    if (typeof value === "boolean") return value ? "是" : "否";
    const text = typeof value === "string" ? value : JSON.stringify(value);
    return text.length > 160 ? `${text.slice(0, 160)}…` : text;
  }

  const SECRET_STATUS_LABELS = {
    configured: "已配置",
    unchanged: "已保留当前值",
    cleared: "未配置",
    empty: "未配置",
    // ``current``：这一版的值**从来没有落进历史**（自由 JSON / URL 内嵌凭据）。
    // 回滚取当前值，所以这一格永远是"已配置（不回滚此字段）"，不是"没配"。
    current: "已配置（不回滚此字段）",
    "url-userinfo": "地址里的账号密码已隐藏（不回滚此字段）",
    "url-query": "地址里的密钥参数已隐藏（不回滚此字段）",
  };

  function secretStatusLabel(status) {
    const key = String(status || "").trim();
    return SECRET_STATUS_LABELS[key] || (key ? `未知状态（${key}）` : "状态未知");
  }

  // ``GET /api/v1/settings`` 现在会把自由 JSON 里的凭据和 URL 内嵌凭据换成标记
  // （``<redacted:current>`` / ``<redacted:url-userinfo>`` / ``<redacted:url-query>``）。
  // 这三个都是**服务端脱敏标记**，不是真实值：原样回传是无损的，但界面绝不能把它
  // 当成一个可以照着填的字符串，也绝不能因为改了别的字段就把它抹成空串。
  const OPAQUE_MARKERS = ["url-userinfo", "url-query"];
  const OPAQUE_MARKER_HINT = "含服务端脱敏标记：这些值被隐藏过，原样保存即可保持不变；把它们改写成别的内容等于换掉这里原本的凭据。";

  function redactionMarkerStatus(value) {
    if (typeof value !== "string") return "";
    const whole = /^<redacted:([^>]*)>$/.exec(value.trim());
    if (whole) return whole[1];
    // URL 里的两段是**就地**替换：整串仍是一个 URL，只在中间带标记。
    const embedded = OPAQUE_MARKERS.find(kind => value.includes(`<redacted:${kind}>`));
    return embedded || "";
  }

  function hasOpaqueMarker(value) {
    if (typeof value === "string") {
      return Boolean(redactionMarkerStatus(value)) || value.includes("<redacted:");
    }
    if (Array.isArray(value)) return value.some(hasOpaqueMarker);
    if (value && typeof value === "object") return Object.values(value).some(hasOpaqueMarker);
    return false;
  }

  const DIFF_CHANGE_META = {
    added: { label: "新增", badge: "success", icon: "plus" },
    removed: { label: "移除", badge: "danger", icon: "minus" },
    modified: { label: "修改", badge: "warning", icon: "pencil" },
  };

  function diffCounts(entries) {
    const counts = { added: 0, modified: 0, removed: 0, secret: 0 };
    (Array.isArray(entries) ? entries : []).forEach(entry => {
      if (entry?.secret) counts.secret += 1;
      if (counts[entry?.change] != null) counts[entry.change] += 1;
    });
    return counts;
  }

  // 密钥路径**只渲染状态**：历史快照里根本没有值，客户端也绝不能凭空造一个出来。
  function diffValueMarkup(entry, side) {
    const value = side === "old" ? entry.old : entry.new;
    if (entry.secret) {
      const status = value && typeof value === "object" ? value.status : null;
      if (!status) return `<span class="diff-absent">该侧没有这个密钥槽位</span>`;
      return `<span class="badge">${icon("key-round")}${escapeHtml(secretStatusLabel(status))}</span>`;
    }
    return `<code class="diff-value">${escapeHtml(renderScalar(value))}</code>`;
  }

  function diffEntriesMarkup(entries, emptyText = "两份配置之间没有差异。") {
    if (!Array.isArray(entries) || !entries.length) {
      return `<p class="field-hint">${escapeHtml(emptyText)}</p>`;
    }
    return `<div class="diff-list">${entries.map(entry => {
      const meta = DIFF_CHANGE_META[entry.change] || { label: entry.change || "变更", badge: "", icon: "pencil" };
      return `
        <div class="diff-row${entry.secret ? " is-secret" : ""}">
          <div class="diff-row-head">
            ${icon(meta.icon)}
            <code class="diff-path">${escapeHtml(entry.path || "")}</code>
            <span class="badge ${meta.badge}">${escapeHtml(meta.label)}</span>
            ${entry.secret ? `<span class="badge info">${icon("key-round")}密钥状态</span>` : ""}
            ${entry.truncated ? `<span class="badge info">已截断</span>` : ""}
          </div>
          <div class="diff-values">
            <span class="diff-side"><small>原</small>${diffValueMarkup(entry, "old")}</span>
            <span class="diff-arrow" aria-hidden="true">→</span>
            <span class="diff-side"><small>新</small>${diffValueMarkup(entry, "new")}</span>
          </div>
        </div>`;
    }).join("")}</div>`;
  }

  function redactedValueMarkup(value) {
    const text = typeof value === "string" ? value : "";
    const marker = /^<redacted:([^>]*)>$/.exec(text);
    if (marker) return `<span class="badge">${icon("key-round")}${escapeHtml(secretStatusLabel(marker[1]))}</span>`;
    return `<code class="diff-value">${escapeHtml(renderScalar(value))}</code>`;
  }

  function flattenLeaves(value, prefix = "", out = []) {
    if (Array.isArray(value)) {
      if (!value.length) out.push({ path: prefix, value });
      else value.forEach((item, index) => flattenLeaves(item, `${prefix}[${index}]`, out));
      return out;
    }
    if (value && typeof value === "object") {
      const keys = Object.keys(value);
      if (!keys.length) out.push({ path: prefix, value });
      else keys.forEach(key => flattenLeaves(value[key], prefix ? `${prefix}.${key}` : key, out));
      return out;
    }
    out.push({ path: prefix, value });
    return out;
  }

  // --------------------------------------------------------------------------
  // 保存前差异预览（只读；后端 preview() 一个字节都不写）
  // --------------------------------------------------------------------------
  function savePreviewRequest() {
    return {
      revision: Number(state.document?.revision || 0),
      config: stripSecrets(state.config),
      secret_changes: clone(state.secretChanges),
    };
  }

  async function previewConfigChanges() {
    return apiFetch("/api/v1/settings/diff", {
      method: "POST",
      body: JSON.stringify(savePreviewRequest()),
    });
  }

  function savePreviewMarkup() {
    const preview = state.savePreview;
    if (!preview) return "";
    if (preview.phase === "loading") {
      return `
        <section class="settings-section save-preview" data-save-preview aria-busy="true">
          ${sectionHead("保存前差异预览", "正在计算这次保存会改动哪些字段。只读预览，不会写库。")}
          <div class="loading-state inline"><span class="spinner"></span><p>正在计算差异</p></div>
        </section>`;
    }
    if (preview.phase === "error") {
      return `
        <section class="settings-section save-preview" data-save-preview>
          ${sectionHead("保存前差异预览", "预览没有成功，尚未发起保存。")}
          <div class="notice danger">${icon("shield-alert")}<span>${escapeHtml(preview.message || "差异预览失败。")}</span></div>
          <div class="toolbar-row">
            <button class="secondary-button" type="button" data-action="save-preview-refresh">${icon("refresh-cw")}重新计算</button>
            <button class="secondary-button" type="button" data-action="save-preview-cancel">${icon("x")}放弃这次保存</button>
          </div>
        </section>`;
    }
    const entries = Array.isArray(preview.diff) ? preview.diff : [];
    const counts = diffCounts(entries);
    const restart = Array.isArray(preview.restartPending) ? preview.restartPending : [];
    const secretPaths = Array.isArray(preview.changedSecretPaths) ? preview.changedSecretPaths : [];
    const summary = String(preview.summary || "");
    const noChange = entries.length === 0 && !secretPaths.length;
    const snapshotBytes = Number(preview.snapshotBytes);
    const snapshotLimit = Number(preview.snapshotLimitBytes);
    const hasSnapshot = Number.isFinite(snapshotBytes) && snapshotBytes > 0
      && Number.isFinite(snapshotLimit) && snapshotLimit > 0;
    const usagePercent = hasSnapshot ? Math.min(100, Math.round((snapshotBytes / snapshotLimit) * 100)) : 0;
    const banner = preview.phase === "conflict"
      ? `<div class="notice danger">${icon("shield-alert")}<span><strong>配置已被其他会话更新，这次保存没有写进去。</strong>页面上的草稿原样保留。请先重新加载最新配置，再决定要不要重做这些改动。</span>
          <button class="secondary-button" type="button" data-action="reload-config-after-conflict">${icon("refresh-cw")}重新加载配置</button></div>`
      : preview.stale
        ? `<div class="notice">${icon("triangle-alert")}<span><strong>你看到的已经不是最新版。</strong>服务端当前修订是 r${escapeHtml(preview.revision ?? "-")}，这份草稿基于 r${escapeHtml(preview.expectedRevision ?? "-")}。继续保存会覆盖其他会话刚写入的改动。</span></div>`
        : noChange
          ? `<div class="notice info" data-save-preview-nochange>${icon("circle-check")}<span><strong>服务端算出来是「无变化」。</strong>这份草稿与服务端当前的 r${escapeHtml(preview.revision ?? "-")} 逐字段一致，没有任何字段会被改写——这不是错误，也没有预览失败。</span></div>`
          : `<div class="notice info">${icon("scan-search")}<span>下面就是保存后会写入的内容。<strong>现在还没有写入任何东西</strong>——看完再点「确认保存这些改动」。</span></div>`;
    return `
      <section class="settings-section save-preview" data-save-preview data-preview-empty="${noChange ? "true" : "false"}">
        ${sectionHead(
          "保存前差异预览",
          `基于 r${escapeHtml(preview.expectedRevision ?? "-")} 的草稿 → 服务端当前 r${escapeHtml(preview.revision ?? "-")}`,
          `<button class="mini-icon-button" type="button" data-action="save-preview-cancel" aria-label="放弃这次保存" title="放弃这次保存">${icon("x")}</button>`,
        )}
        ${banner}
        ${summary ? `<p class="field-hint" data-save-preview-summary>${icon("info")}服务端摘要：${escapeHtml(summary)}</p>` : ""}
        <div class="stat-row">
          <span class="badge success">${icon("plus")}新增 ${counts.added}</span>
          <span class="badge warning">${icon("pencil")}修改 ${counts.modified}</span>
          <span class="badge danger">${icon("minus")}移除 ${counts.removed}</span>
          ${counts.secret ? `<span class="badge info">${icon("key-round")}涉及密钥 ${counts.secret}</span>` : ""}
          ${restart.length ? `<span class="badge warning">${icon("rotate-ccw")}需重启 ${restart.length}</span>` : ""}
        </div>
        ${hasSnapshot
          ? `<div class="stat-row" data-snapshot-usage>
              <span class="badge info">${icon("database")}快照占用 ${(snapshotBytes / 1024).toFixed(1)} KiB / 上限 ${(snapshotLimit / 1024).toFixed(0)} KiB（${usagePercent}%）</span>
            </div>`
          : ""}
        ${restart.length ? `<p class="field-hint">保存后需要重启才生效：${restart.map(item => `<code>${escapeHtml(item)}</code>`).join("、")}</p>` : ""}
        ${secretPaths.length ? `<p class="field-hint">这次显式改动的密钥槽位（只有路径）：${secretPaths.map(item => `<code>${escapeHtml(item)}</code>`).join("、")}</p>` : ""}
        ${diffEntriesMarkup(entries, noChange ? "服务端逐字段比对下来没有任何差异。" : "这次保存没有任何字段差异。")}
        <div class="toolbar-row save-preview-actions">
          <label class="compact-check">
            <input type="checkbox" data-preview-skip${state.skipSavePreview ? " checked" : ""}>
            <span>本次不再预览差异（仅本页有效）</span>
          </label>
          <button class="secondary-button" type="button" data-action="save-preview-refresh">${icon("refresh-cw")}重新计算</button>
          <button class="secondary-button" type="button" data-action="save-preview-cancel">${icon("x")}放弃这次保存</button>
          ${state.skipSavePreview
            ? `<span class="field-hint">预览已关闭：再点顶栏「保存全部」就会直接保存。</span>`
            : noChange
              ? `<span class="field-hint" data-save-preview-nothing-to-save>没有任何字段会被改写，所以这里没有「确认保存」按钮——想推进修订号请到顶栏走正常保存流程。</span>`
              : `<button class="primary-button" type="button" data-action="save-preview-confirm">${icon("save")}确认保存这些改动</button>`}
        </div>
      </section>`;
  }

  // 预览**只算不写**：它把差异面板摆出来就返回 true。真正的写入必须由管理员
  // 再点一次面板里的「确认保存这些改动」才发生——所以这里不弹模态确认框。
  // 弹框会把面板上的差异、重算、跳过预览全部挡在模态层后面，等于没有这些控件。
  async function runSavePreview() {
    state.savePreview = { phase: "loading" };
    renderContent();
    content.querySelector("[data-save-preview]")?.scrollIntoView({ block: "start", behavior: "smooth" });
    try {
      const preview = await previewConfigChanges();
      state.savePreview = {
        phase: "ready",
        revision: preview.revision,
        expectedRevision: preview.expected_revision,
        stale: Boolean(preview.stale),
        // **无变化是一份合法答案**：diff 会是空数组，summary 会说「无变化」。
        // 空数组不是错误，界面不能把它渲染成失败。
        summary: String(preview.summary || ""),
        diff: Array.isArray(preview.diff) ? preview.diff : [],
        restartPending: Array.isArray(preview.restart_pending) ? preview.restart_pending : [],
        changedSecretPaths: Array.isArray(preview.changed_secret_paths) ? preview.changed_secret_paths : [],
        snapshotBytes: preview.snapshot_bytes,
        snapshotLimitBytes: preview.snapshot_limit_bytes,
      };
      renderContent();
      content.querySelector("[data-save-preview]")?.scrollIntoView({ block: "start", behavior: "smooth" });
      return true;
    } catch (error) {
      state.savePreview = { phase: "error", message: error.message || "差异预览失败。" };
      renderContent();
      showToast(state.savePreview.message, "error", 7000);
      return false;
    }
  }

  // --------------------------------------------------------------------------
  // Part A · 审核回放（dry-run）
  // --------------------------------------------------------------------------
  const REPLAY_RULE_TYPES = [
    { value: "keyword", label: "关键词" },
    { value: "regex", label: "正则" },
    { value: "llm", label: "模型语义" },
  ];
  const REPLAY_RULE_ACTIONS = [
    { value: "warn", label: "警告" },
    { value: "delete", label: "删除" },
    { value: "ban", label: "封禁" },
  ];
  const REPLAY_SCAN_SCOPES = [
    { value: "message", label: "仅正文" },
    { value: "message+quote", label: "正文 + 引文" },
    { value: "message+vision", label: "正文 + 图片描述" },
    { value: "message+quote+vision", label: "正文 + 引文 + 图片描述" },
  ];
  const REPLAY_MAX_RULES = 200;
  const REPLAY_PATTERN_MAX_CHARS = 1000;
  const REPLAY_SOURCE_KIND_LABELS = {
    violation_release: "违规记录 · 人工放行",
    violation_confirmed: "违规记录 · 已确认",
    manual: "手工录入",
  };
  const REPLAY_LABEL_SOURCE_LABELS = {
    admin_release: "管理员人工放行",
    admin_ban: "管理员确认封禁",
    high_confidence_hit: "高置信度命中",
    manual: "手工标注",
    manual_confirm: "人工确认金标",
  };
  // 机器侧信号：导入时**建议**的标签。它不是答案键——任何统计都不读它，
  // 界面上必须与已确认金标视觉区分开。
  const REPLAY_SUGGESTION_BASIS_LABELS = {
    admin_release: "来源：被管理员放行的违规记录",
    admin_ban: "来源：被管理员确认封禁的记录",
    high_confidence_hit: "来源：导入时的高置信度命中",
    manual: "来源：手工录入",
  };
  const REPLAY_GOLD_LABELS = {
    violation: "违规",
    benign: "正常",
  };
  const REPLAY_SIDE_ERROR_LABELS = {
    model_error: "模型调用失败",
    model_timeout: "模型调用超时",
    unparseable_output: "模型输出不可解析",
    empty_response: "模型没有返回内容",
    invalid_verdict_field: "模型输出缺少合法判定字段",
    inconclusive_rules: "样本触发了模型语义判断，但没有足够信息下结论",
    missing_confidence: "模型没有给出置信度",
  };
  const REPLAY_RUN_ERROR_LABELS = {
    deadline_exceeded: "超过单次回放的墙钟预算，已提前停止（已算出的结果保留）",
    inline_deadline_exceeded: "超过单次回放的墙钟预算，已提前停止",
    cancelled_by_operator: "管理员取消了这一轮",
    worker_lost: "执行器中断（worker 丢失），结果可能不完整",
    run_failed: "执行失败",
    lease_lost: "执行租约丢失，可能已被另一个执行器接手",
  };
  const REPLAY_STATUS_META = {
    queued: { label: "排队中", badge: "info" },
    running: { label: "执行中", badge: "info" },
    completed: { label: "已完成", badge: "success" },
    cancelled: { label: "已取消", badge: "warning" },
    failed: { label: "失败", badge: "danger" },
    interrupted: { label: "执行器中断", badge: "danger" },
  };
  // 终态：到了这里就不再轮询。
  const REPLAY_TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled", "interrupted"]);
  // 轮询间隔（毫秒）。回放在后台跑，WebView 只是隔一会儿问一次状态。
  const REPLAY_POLL_INTERVAL_MS = 2000;
  const REPLAY_IDEMPOTENCY_LABELS = {
    replay_candidate_invalid: "候选结构无效：只能是 {rules, prompt?}，且 rules 必须是数组。",
    replay_rule_invalid: "候选规则无效：检查类型、处置动作、扫描范围与规则内容长度。",
    replay_prompt_invalid: "候选提示词未通过与线上相同的校验。",
    replay_prompt_forbidden: "候选审核提示词是全局配置，只有超级管理员可以提交。",
    replay_idempotency_key_required: "缺少 idempotency_key。",
    replay_idempotency_key_invalid: "idempotency_key 过长。",
    replay_idempotency_conflict: "同一个 idempotency_key 已经登记过另一份候选或另一个群。",
    replay_sample_empty: "样本内容不能为空。",
    replay_empty_sample_set: "没有可用于回放的样本。",
    replay_already_running: "回放正在执行中。",
    replay_cancelled: "回放已取消，不能执行。",
    replay_not_cancellable: "回放已经结束，无法取消。",
    replay_not_queued: "只有排队中的回放可以启动。",
    replay_busy: "回放队列已满，请稍后再试（同一份候选可以直接重试，不会重复开一轮）。",
    replay_run_not_found: "回放不存在。",
    replay_sample_not_found: "样本不存在。",
    replay_execute_invalid: "execute 必须是布尔值。",
    replay_gold_label_required: "必须提供 gold_label 字段。",
    replay_gold_label_invalid: "标注只能是 violation 或 benign。",
    unknown_replay_label_request: "请求里带了不允许的字段。",
    unknown_replay_run_request: "请求里带了不允许的字段。",
    unknown_replay_sample_request: "请求里带了不允许的字段。",
    unknown_replay_import_request: "请求里带了不允许的字段。",
    replay_import_kinds_invalid: "导入类型无效。",
    replay_limit_invalid: "导入条数无效。",
    invalid_sample_id: "样本 ID 无效。",
    invalid_run_id: "回放 ID 无效。",
    group_access_denied: "你没有这个群的管理权限。",
  };

  function replaySideErrorLabel(code) {
    const key = String(code || "").trim();
    if (!key || key === "none") return "";
    return REPLAY_SIDE_ERROR_LABELS[key] || key;
  }

  function replayRunErrorLabel(code) {
    const key = String(code || "").trim();
    if (!key) return "";
    if (REPLAY_RUN_ERROR_LABELS[key]) return REPLAY_RUN_ERROR_LABELS[key];
    if (key.startsWith("replay_failed:")) return `执行失败（${key.slice("replay_failed:".length)}）`;
    return key;
  }

  function replayErrorCode(body) {
    const structured = body?.error && typeof body.error === "object" ? body.error : null;
    return String(structured?.code || "");
  }

  function replayErrorDetails(body) {
    const structured = body?.error && typeof body.error === "object" ? body.error : null;
    const details = structured?.details;
    return details && typeof details === "object" ? details : null;
  }

  // 饱和时后端会带上容量占用（429 replay_busy）。把它原样说给管理员，
  // 比一句"队列已满"有用得多。
  function replayCapacitySummary(capacity) {
    const cap = capacity && typeof capacity === "object" ? capacity : null;
    if (!cap) return "";
    const parts = [];
    if (cap.global_running != null) parts.push(`执行中 ${cap.global_running}/${cap.global_limit ?? "?"}`);
    if (cap.global_queued != null) parts.push(`排队 ${cap.global_queued}/${cap.global_queue_limit ?? "?"}`);
    if (cap.group_running != null) parts.push(`本群执行中 ${cap.group_running}`);
    if (cap.group_queued != null) parts.push(`本群排队 ${cap.group_queued}`);
    return parts.join(" · ");
  }

  function replayGroups() {
    const allowed = new Set((state.session?.group_ids || []).map(String));
    const superAdmin = Boolean(state.session?.can_manage_global);
    return state.groups.filter(group => superAdmin || allowed.has(String(group.id)));
  }

  function replayGroupLabel(group) {
    return group.title ? `${group.title}（${group.id}）` : `群 ${group.id}`;
  }

  function replayActiveGroup() {
    const groups = replayGroups();
    if (!groups.length) return null;
    return groups.find(group => String(group.id) === String(state.replay.groupId)) || groups[0];
  }

  // 回放面板有两种呈现：
  //   * 逐群版本（群卡片里的「审核回放」分节）——**只有那一个群**，不提供群选择器，
  //     群管理员由此拿到自己群的入口，同时不可能指到别的群；
  //   * 跨群版本（最高管理员的「审核验证」页）——一个群选择器。
  // 动作一律按**触发它的那个面板**取作用域（closest("[data-replay-scope]")），
  // 绝不复用一个全局的"当前群"，否则两处渲染会互相串数据。
  function replayScopeOf(node) {
    const holder = node?.closest?.("[data-replay-scope]");
    if (holder?.dataset.replayScope === "group") {
      const group = replayGroups().find(item => String(item.id) === String(holder.dataset.replayGroupId));
      if (group) return group;
    }
    return replayActiveGroup();
  }

  function replayScopeGroup(group) {
    if (!group) return replayActiveGroup();
    return replayGroups().find(item => String(item.id) === String(group.id)) || group;
  }

  function replayDataIsFor(group) {
    return Boolean(group) && state.replay.loadedGroupId === String(group.id);
  }

  // 客户端先按**后端同一套校验口径**收敛一次：构造不出后端会拒绝的候选，
  // 才不会把 400 变成一次"点了才知道错"的往返。
  function buildReplayCandidate() {
    const rules = (state.replay.rules || []).map((rule, index) => {
      const position = index + 1;
      const ruleType = String(rule.rule_type || "keyword");
      const action = String(rule.action || "warn");
      const scanScope = String(rule.scan_scope || "message");
      const pattern = String(rule.pattern ?? "").trim();
      if (!REPLAY_RULE_TYPES.some(item => item.value === ruleType)) {
        throw new Error(`第 ${position} 条候选规则的类型无效。`);
      }
      if (!REPLAY_RULE_ACTIONS.some(item => item.value === action)) {
        throw new Error(`第 ${position} 条候选规则的处置动作无效。`);
      }
      if (!REPLAY_SCAN_SCOPES.some(item => item.value === scanScope)) {
        throw new Error(`第 ${position} 条候选规则的扫描范围无效。`);
      }
      if (!pattern) throw new Error(`第 ${position} 条候选规则的群规内容为空。`);
      if (pattern.length > REPLAY_PATTERN_MAX_CHARS) {
        throw new Error(`第 ${position} 条候选规则的群规内容超过 ${REPLAY_PATTERN_MAX_CHARS} 字符。`);
      }
      // id 由后端按位置从 1 起编号，这里不传，免得和后端编出来的对不上。
      return { rule_type: ruleType, pattern, action, enabled: Boolean(rule.enabled), scan_scope: scanScope };
    });
    const candidate = { rules };
    if (!state.replay.useBaselinePrompt) {
      // 角色矩阵：**全局**审核提示词只交给超管。群管理员在这里提交会被后端
      // 403 replay_prompt_forbidden，所以客户端就先挡住，不去做一次注定失败的往返。
      if (!state.session?.can_manage_global) {
        throw new Error("候选审核提示词属于全局配置，只有超级管理员可以提交或查看；请保持“沿用当前生效的审核提示词”。");
      }
      const prompt = String(state.replay.prompt ?? "");
      if (!prompt.trim()) throw new Error("候选提示词不能为空；不改提示词就保持“沿用当前生效的提示词”。");
      if (!prompt.includes("{rules_json}")) throw new Error("候选提示词必须包含 {rules_json} 占位符。");
      // 与后端同一个 .format() 口径：JSON 示例要写成双花括号 {{ }}，
      // 否则 Python 会在格式化时抛错。用剥掉合法占位符之后剩下的花括号近似。
      const rest = prompt.replace(/\{\{[\s\S]*?\}\}/g, "").replaceAll("{rules_json}", "");
      if (/[{}]/.test(rest)) {
        throw new Error("候选提示词含有无效花括号；JSON 示例需使用双花括号 {{ }}。");
      }
      candidate.prompt = prompt;
    }
    return candidate;
  }

  function replayCandidatePreview() {
    try {
      return { candidate: buildReplayCandidate(), error: "" };
    } catch (error) {
      return { candidate: null, error: error.message || "候选尚未成形。" };
    }
  }

  // 输入框里改一个字就重渲染 = 每敲一个字符丢一次焦点。这里只就地更新
  // JSON 预览、校验提示和运行按钮的可用状态，不动 DOM 结构。
  function refreshReplayCandidateSurface() {
    const { candidate, error } = replayCandidatePreview();
    const jsonNode = content.querySelector("[data-replay-json]");
    if (jsonNode) jsonNode.textContent = error ? `// 候选尚未成形：${error}` : JSON.stringify(candidate, null, 2);
    const errorNode = content.querySelector("[data-replay-candidate-error]");
    if (errorNode) {
      errorNode.innerHTML = `${icon("circle-alert")}${escapeHtml(error)}`;
      errorNode.hidden = !error;
    }
    // 复制 / 导出也是"候选成立"才允许做的事：候选刚从无效变成有效时，
    // 不重渲染就没人把它们从 disabled 里放出来。
    content.querySelectorAll('[data-action="replay-copy-candidate"], [data-action="replay-export-candidate"]')
      .forEach(button => { button.disabled = Boolean(error); });
    // 面板可能同时存在多份（每张群卡片一份），所以逐个按各自的作用域刷新，
    // 不能拿某一个"当前群"去统一决定所有按钮的可用性。
    content.querySelectorAll("[data-replay-panel]").forEach(panel => {
      const group = replayScopeOf(panel);
      const runButton = panel.querySelector("[data-action='replay-run']");
      if (!runButton) return;
      const blocker = replayRunBlocker(group, replayDataIsFor(group));
      runButton.disabled = Boolean(blocker) || state.replay.running;
      runButton.title = error || blocker || "运行一次离线对照";
    });
  }

  function replayActiveSampleCount() {
    return (state.replay.samples || []).filter(sample => !sample.expired).length;
  }

  function replayRunBlocker(group, loaded) {
    if (!group) return "先选择一个你有权限的群组。";
    if (state.replay.loading) return "正在加载样本与回放记录。";
    if (!loaded) return "先点「加载本群样本与回放记录」，再导入样本或发起回放。";
    if (!replayActiveSampleCount()) return "没有可用样本：先导入或手工录入样本（过留存期的样本不参与回放）。";
    const { error } = replayCandidatePreview();
    if (error) return error;
    return "";
  }

  // 机器建议与人工金标必须**看起来是两件事**：机器导入只是"机器认为该标成什么"，
  // 它不是答案键，也不能被当成已确认的金标渲染成绿色/红色徽章。
  function replaySuggestionMarkup(sample) {
    const provenance = sample.provenance || {};
    const suggested = provenance.suggested_label ? String(provenance.suggested_label) : "";
    if (!suggested) return "";
    const suggestedText = REPLAY_GOLD_LABELS[suggested] || suggested;
    const basis = REPLAY_SUGGESTION_BASIS_LABELS[provenance.suggested_label_basis] || String(provenance.suggested_label_basis || "");
    const confirmed = Boolean(provenance.suggested_label_confirmed);
    const sameAsGold = String(sample.gold_label || "") === suggested;
    return `
      <div class="replay-suggestion" data-replay-suggestion>
        <span class="badge suggestion">${icon("sparkles")}机器建议：${escapeHtml(suggestedText)}</span>
        ${basis ? `<span class="field-hint">${escapeHtml(basis)}</span>` : ""}
        ${confirmed ? `<span class="field-hint">导入时机器已有较高置信度，但这仍然只是机器判断。</span>` : ""}
        <span class="field-hint">机器建议<strong>不会</strong>自动成为金标：误伤 / 漏放只统计人工确认过的标签。</span>
        ${sameAsGold
          ? `<span class="badge success">${icon("check")}与当前金标一致</span>`
          : `<button class="secondary-button" type="button" data-action="replay-label" data-sample-id="${attr(sample.id)}" data-gold-label="${attr(suggested)}"${state.replay.running ? " disabled" : ""}>${icon("check")}采纳为金标</button>`}
      </div>`;
  }

  function replayLabelControls(sample) {
    const busy = state.replay.running ? " disabled" : "";
    const labelled = Boolean(sample.gold_label);
    const source = sample.label_source ? REPLAY_LABEL_SOURCE_LABELS[sample.label_source] || sample.label_source : "";
    const reviewed = sample.label_reviewed_at
      ? `${formatMoment(sample.label_reviewed_at)}`
      : "";
    const gold = sample.gold_label ? REPLAY_GOLD_LABELS[sample.gold_label] || sample.gold_label : "";
    return `
      <div class="replay-label-control" data-replay-label-control data-sample-id="${attr(sample.id)}">
        <div class="replay-label-head">
          <strong>人工金标</strong>
          ${labelled
            ? `<span class="badge ${sample.gold_label === "violation" ? "danger" : "success"}" data-gold-badge>${escapeHtml(gold)}</span>`
            : `<span class="badge" data-gold-badge>未确认（不参与误伤率）</span>`}
          ${source ? `<span class="field-hint">${escapeHtml(source)}</span>` : ""}
          ${reviewed ? `<span class="field-hint">确认于 ${escapeHtml(reviewed)}${sample.label_reviewed_by ? ` · 用户 ${escapeHtml(sample.label_reviewed_by)}` : ""}</span>` : ""}
        </div>
        <div class="toolbar-row">
          <button class="secondary-button" type="button" data-action="replay-label" data-sample-id="${attr(sample.id)}" data-gold-label="violation"${busy}>${icon("shield-alert")}标为违规</button>
          <button class="secondary-button" type="button" data-action="replay-label" data-sample-id="${attr(sample.id)}" data-gold-label="benign"${busy}>${icon("shield-check")}标为正常</button>
          <button class="secondary-button" type="button" data-action="replay-label" data-sample-id="${attr(sample.id)}" data-gold-label=""${busy}${labelled ? "" : " disabled"}>${icon("eraser")}清除金标</button>
        </div>
      </div>`;
  }

  function replaySampleRow(sample) {
    const labelState = String(sample.label_state || (sample.gold_label ? "labelled" : "unlabelled"));
    const disabled = state.replay.running ? " disabled" : "";
    return `
      <div class="replay-sample${sample.expired ? " is-expired" : ""}" data-replay-sample="${attr(sample.id)}" data-label-state="${attr(labelState)}">
        <div class="replay-sample-head">
          <span class="badge info">${escapeHtml(REPLAY_SOURCE_KIND_LABELS[sample.source_kind] || sample.source_kind || "未知来源")}</span>
          ${sample.expired
            ? `<span class="badge danger">${icon("clock-alert")}已过留存期</span>`
            : `<span class="badge">留存至 ${escapeHtml(formatMoment(sample.expires_at))}</span>`}
          <button class="mini-icon-button danger" type="button" data-action="replay-delete-sample" data-sample-id="${attr(sample.id)}"
                  aria-label="删除这条回放样本" title="删除这条回放样本"${disabled}>${icon("trash-2")}</button>
        </div>
        <p class="replay-sample-text">${escapeHtml(sample.redacted_text || "")}</p>
        ${sample.redacted_context ? `<p class="field-hint">上下文：${escapeHtml(sample.redacted_context)}</p>` : ""}
        ${replaySuggestionMarkup(sample)}
        ${replayLabelControls(sample)}
        <span class="field-hint">已脱敏（Telegram id / @用户名 / 电话号码在写入时已抹掉）· 录入于 ${escapeHtml(formatMoment(sample.created_at))}</span>
      </div>`;
  }

  function replayCandidateRuleRow(rule, index) {
    const disabled = state.replay.running ? " disabled" : "";
    const optionsFor = (list, name) => list.map(item =>
      `<option value="${attr(item.value)}"${rule[name] === item.value ? " selected" : ""}>${escapeHtml(item.label)}</option>`).join("");
    return `
      <div class="replay-rule" data-replay-rule="${index}">
        <div class="replay-rule-head">
          <span class="replay-rule-index">${index + 1}</span>
          <button class="mini-icon-button danger" type="button" data-action="replay-remove-rule" data-rule-index="${index}"
                  aria-label="删除第 ${index + 1} 条候选规则" title="删除第 ${index + 1} 条候选规则"${disabled}>${icon("trash-2")}</button>
        </div>
        <div class="replay-rule-grid">
          <select data-replay-rule-field="rule_type" data-rule-index="${index}" aria-label="第 ${index + 1} 条规则的类型"${disabled}>${optionsFor(REPLAY_RULE_TYPES, "rule_type")}</select>
          <select data-replay-rule-field="action" data-rule-index="${index}" aria-label="第 ${index + 1} 条规则的处置动作"${disabled}>${optionsFor(REPLAY_RULE_ACTIONS, "action")}</select>
          <select data-replay-rule-field="scan_scope" data-rule-index="${index}" aria-label="第 ${index + 1} 条规则的扫描范围"${disabled}>${optionsFor(REPLAY_SCAN_SCOPES, "scan_scope")}</select>
          <label class="compact-check">
            <input type="checkbox" data-replay-rule-field="enabled" data-rule-index="${index}"${rule.enabled ? " checked" : ""}${disabled}>
            <span>启用</span>
          </label>
          <input type="text" data-replay-rule-field="pattern" data-rule-index="${index}" maxlength="${REPLAY_PATTERN_MAX_CHARS}"
                 value="${attr(rule.pattern || "")}" placeholder="群规内容（关键词 / 正则 / 交给模型判断的语义描述）"
                 aria-label="第 ${index + 1} 条规则的群规内容"${disabled}>
        </div>
      </div>`;
  }

  function replaySideMarkup(side, label) {
    const verdict = String(side?.verdict || "");
    const errorText = replaySideErrorLabel(side?.error);
    // 契约：**conclusive === false 的行必然带一个非空 error**。所以"判不出来"
    // 这一档永远由 error 说话，绝不退回按字面 verdict 渲染成"未命中"这种通过态——
    // 那会把一次失败读成一次干净的通过。
    const conclusive = side?.conclusive !== false;
    let badge;
    if (!conclusive || errorText || verdict === "error") {
      badge = `<span class="badge warning" data-replay-inconclusive>${icon("help-circle")}判不出来</span>`;
    } else if (verdict === "violation") {
      badge = `<span class="badge danger">${icon("shield-alert")}命中违规</span>`;
    } else {
      badge = `<span class="badge success">${icon("shield-check")}未命中</span>`;
    }
    const facts = [];
    if (side.match_source) facts.push(`来源 ${side.match_source}`);
    if (side.confidence != null) facts.push(`置信度 ${Number(side.confidence).toFixed(2)}`);
    if (side.latency_ms != null) facts.push(`${side.latency_ms} ms`);
    const tokens = side.usage || {};
    const promptTokens = Number(tokens.prompt_tokens || 0);
    const outputTokens = Number(tokens.output_tokens || 0);
    if (promptTokens || outputTokens) facts.push(`token ${promptTokens}/${outputTokens}`);
    return `
      <div class="replay-side${conclusive ? "" : " is-inconclusive"}">
        <div class="replay-side-head"><strong>${escapeHtml(label)}</strong>${badge}</div>
        <span class="field-hint">${escapeHtml(facts.join(" · ") || "无加信息")}</span>
        ${!conclusive
          ? `<p class="replay-side-error" data-replay-inconclusive-reason>${icon("circle-alert")}${escapeHtml(errorText || "这一条没能下结论，不计入通过也不计入违规")}</p>`
          : ""}
        ${errorText && conclusive
          ? `<p class="replay-side-error">${icon("circle-alert")}${escapeHtml(errorText)}</p>`
          : ""}
      </div>`;
  }

  function replayResultRow(result) {
    const sample = result.sample || {};
    const gold = sample.gold_label ? REPLAY_GOLD_LABELS[sample.gold_label] || sample.gold_label : "";
    return `
      <div class="replay-result${result.changed ? " is-changed" : ""}">
        <div class="replay-result-head">
          <span class="badge info">样本 ${escapeHtml(sample.id ?? result.sample_id)}</span>
          ${gold ? `<span class="badge ${sample.gold_label === "violation" ? "danger" : "success"}">金标：${escapeHtml(gold)}</span>` : `<span class="badge">无金标</span>`}
          ${result.changed ? `<span class="badge warning">${icon("git-compare-arrows")}判定变化</span>` : `<span class="badge">判定一致</span>`}
        </div>
        <p class="replay-sample-text">${escapeHtml(sample.redacted_text || "")}</p>
        <div class="replay-sides">
          ${replaySideMarkup(result.baseline, "基线（当前生效）")}
          ${replaySideMarkup(result.candidate, "候选（本次提案）")}
        </div>
      </div>`;
  }

  function replayErrorBreakdown(breakdown) {
    const parts = Object.entries(breakdown || {})
      .filter(([, count]) => Number(count || 0) > 0)
      .map(([code, count]) => `${replaySideErrorLabel(code) || code} ×${count}`);
    return parts.join("，");
  }

  function replayStatsMarkup(run) {
    const stats = run.stats || {};
    const total = Number(stats.total || 0);
    const labeledTotal = Number(stats.labeled_total || 0);
    const labeledScored = Number(stats.labeled_scored || 0);
    const unlabeled = Math.max(0, total - labeledTotal);
    const usage = stats.token_usage || {};
    const rate = value => (value == null ? "—" : `${(Number(value) * 100).toFixed(1)}%`);
    const cell = (label, value, hint = "", badge = "") => `
      <div class="stat">
        <span class="stat-label">${escapeHtml(label)}</span>
        <span class="stat-value">${escapeHtml(String(value))}</span>
        ${badge ? `<span class="badge ${badge.badge}">${escapeHtml(badge.label)}</span>` : ""}
        ${hint ? `<span class="field-hint">${escapeHtml(hint)}</span>` : ""}
      </div>`;
    const baselineBreakdown = replayErrorBreakdown(stats.baseline_error_breakdown);
    const candidateBreakdown = replayErrorBreakdown(stats.candidate_error_breakdown);
    const baselineInconclusiveBreakdown = replayErrorBreakdown(stats.baseline_inconclusive_breakdown);
    const candidateInconclusiveBreakdown = replayErrorBreakdown(stats.candidate_inconclusive_breakdown);
    const totalInconclusive = Number(stats.baseline_inconclusive || 0) + Number(stats.candidate_inconclusive || 0);
    const totalError = Number(stats.baseline_error || 0) + Number(stats.candidate_error || 0);
    return `
      <div class="stats-block" data-replay-stats>
        <div class="stats-group">
          ${cell("本次评估条数", total, "本轮实际评估的条数（以它为准，不是样本池大小）")}
          ${cell("判定变化", Number(stats.changed_count ?? run.changed_count ?? 0), "基线与候选结论不同的样本数")}
          ${cell("带人工金标", labeledTotal, `未确认金标 ${unlabeled} 条不参与误伤 / 漏放`, "info")}
          ${Number(run.results_purged || 0) ? cell("结果行已因样本被删而消失", Number(run.results_purged || 0), "这条报告覆盖的不再是它当初算出的全部样本", "danger") : ""}
        </div>
        <div class="stats-group">
          <div class="stats-group-head">
            <strong>确认命中（conclusive）</strong>
            <span class="badge info">只统计下了结论的行</span>
          </div>
          ${cell("基线确认命中违规", Number(stats.baseline_confirmed_violation || 0), `基线确认未命中 ${Number(stats.baseline_confirmed_benign || 0)}`)}
          ${cell("候选确认命中违规", Number(stats.candidate_confirmed_violation || 0), `候选确认未命中 ${Number(stats.candidate_confirmed_benign || 0)}`)}
          ${cell("原始命中（含不确定）", `基线 ${Number(stats.baseline_violation || 0)} / 候选 ${Number(stats.candidate_violation || 0)}`, "其中不含任何「判不出来」的行；不要把它当成确认命中")}
        </div>
        <div class="stats-group">
          <div class="stats-group-head">
            <strong>判不出来 / 判定失败</strong>
            <span class="badge warning">${totalInconclusive + totalError} 条</span>
          </div>
          ${cell("基线不确定", Number(stats.baseline_inconclusive || 0), baselineInconclusiveBreakdown || "既不算命中也不算通过", "warning")}
          ${cell("候选不确定", Number(stats.candidate_inconclusive || 0), candidateInconclusiveBreakdown || "既不算命中也不算通过", "warning")}
          ${cell("基线判定失败", Number(stats.baseline_error || 0), baselineBreakdown || "没有失败记录", "danger")}
          ${cell("候选判定失败", Number(stats.candidate_error || 0), candidateBreakdown || "没有失败记录", "danger")}
        </div>
        <div class="stats-group stats-highlight">
          <div class="stats-group-head">
            <strong>误伤 / 漏放</strong>
            <span class="badge info">只统计人工确认过金标的样本</span>
          </div>
          ${cell("带人工金标样本", labeledTotal, `未确认金标 ${unlabeled} 条不参与误伤率`, "info")}
          ${cell("实际计入评分", labeledScored, `候选判不出来的有金标样本 ${Math.max(0, labeledTotal - labeledScored)} 条既不算对也不算错`)}
          ${cell("误伤（候选确认判违规、金标正常）", Number(stats.false_positive || 0), "", "danger")}
          ${cell("漏放（候选确认判正常、金标违规）", Number(stats.false_negative || 0), "", "danger")}
          ${cell("误伤率", rate(stats.false_positive_rate), `分母 = 实际计入评分 ${labeledScored} 条`)}
          ${cell("漏放率", rate(stats.false_negative_rate), `分母 = 实际计入评分 ${labeledScored} 条`)}
          ${cell("命中正确 / 未命中正确", `${Number(stats.true_positive || 0)} / ${Number(stats.true_negative || 0)}`, "同口径只对带金标样本计数")}
          ${cell("准确率", rate(stats.accuracy), labeledTotal ? `覆盖 ${labeledTotal}/${total} 条样本` : "没有任何带金标的样本，这个数不该被读成全量准确率")}
        </div>
        <div class="stats-group">
          ${cell("模型调用次数", Number(stats.model_call_count || 0), "回放会真的调用审核模型")}
          ${cell("token 用量（输入/输出）", `${Number(usage.prompt_tokens || 0)} / ${Number(usage.output_tokens || 0)}`, "仓库未配置模型单价，只报用量")}
          ${cell("时延 平均 / P50 / P95", `${stats.latency_ms_avg ?? "—"} / ${stats.latency_ms_p50 ?? "—"} / ${stats.latency_ms_p95 ?? "—"}`, "毫秒，两侧合并口径")}
          ${cell("预计花费", "未配置单价", "本仓库没有模型价格表，这里只报调用次数与 token 用量，不编金额", "info")}
        </div>
      </div>`;
  }

  // 保真度：一份回放是**脱敏副本 + 没有原始上下文**，明确不等价于生产回放。
  // 不把这段显示出来，一份回放报告就会被当成"照着线上跑了一遍"。
  function replayFidelityMarkup(run) {
    const fidelity = run?.fidelity && typeof run.fidelity === "object" ? run.fidelity : null;
    if (!fidelity) return "";
    const digest = String(fidelity.model_digest || "");
    const executed = String(fidelity.executed_model_digest || "");
    const item = (label, value, hint = "") => `
      <div class="stat">
        <span class="stat-label">${escapeHtml(label)}</span>
        <span class="stat-value">${escapeHtml(String(value))}</span>
        ${hint ? `<span class="field-hint">${escapeHtml(hint)}</span>` : ""}
      </div>`;
    return `
      <div class="stats-block replay-fidelity" data-replay-fidelity>
        <div class="stats-group">
          <div class="stats-group-head">
            <strong>这份回放在哪些地方不等于线上</strong>
            <span class="badge danger">不等价于生产回放</span>
          </div>
          ${item("模型绑定摘要", digest || "未知（登记那一刻没有冻结模型绑定）")}
          ${item("实际执行时摘要", executed || "未知", fidelity.model_binding_drift ? "与登记时不一致：这一轮的执行不是按登记时的绑定跑的" : "")}
          ${item("样本已脱敏", fidelity.redaction_applied ? "是" : "否", "Telegram id / @用户名 / 电话号码在写入样本时就被抹掉了")}
          ${item("文本被脱敏改动过的样本", Number(fidelity.samples_redaction_changed || 0), "掩码可能正好落在群规要抓的词上")}
          ${item("含原始上下文", fidelity.context_absent ? "否" : "是", "群里前文、被引用的话、图片原文都不在样本里")}
          ${fidelity.notes ? `<p class="field-hint">${escapeHtml(fidelity.notes)}</p>` : ""}
        </div>
      </div>`;
  }

  // 执行的界：告诉管理员"全评完了"和"提前停了"的区别。界值来自后端，
  // **不在客户端写死**（条数上限与墙钟预算都由服务端配置决定）。
  function replayBoundsMarkup(bounds, run) {
    if (!bounds || typeof bounds !== "object") return "";
    const chip = (label, value, badge = "") => `<span class="badge ${badge}">${escapeHtml(label)} ${escapeHtml(String(value))}</span>`;
    const status = String(run?.status || "");
    return `
      <div class="stat-row replay-bounds" data-replay-bounds>
        ${chip("样本池", bounds.sample_pool)}
        ${chip("单次上限", bounds.sample_limit)}
        ${bounds.deadline_seconds != null ? chip("墙钟预算（秒）", Number(bounds.deadline_seconds)) : ""}
        ${chip("实际评估", bounds.evaluated, bounds.stopped_early ? "warning" : "")}
        ${bounds.truncated ? `<span class="badge warning">${icon("triangle-alert")}撞到了条数上限</span>` : ""}
        ${bounds.deadline_exceeded ? `<span class="badge warning">${icon("triangle-alert")}用完了墙钟预算</span>` : ""}
        ${bounds.cancelled ? `<span class="badge warning">${icon("ban")}被取消</span>` : ""}
        ${bounds.interrupted ? `<span class="badge danger">${icon("circle-x")}执行器中断</span>` : ""}
        ${bounds.complete
          ? `<span class="badge success">${icon("check")}评完了全部可用样本</span>`
          : `<span class="badge warning">${icon("triangle-alert")}${status === "queued" || status === "running" ? "尚未结束，现在看到的是部分结果" : "提前停止，下面只是部分结果"}</span>`}
      </div>`;
  }

  function replayRunStatusBadge(run) {
    const meta = REPLAY_STATUS_META[run.status] || { label: run.status || "未知", badge: "" };
    const pollHint = REPLAY_TERMINAL_STATUSES.has(String(run.status))
      ? ""
      : ` · ${icon("loader")}页面每 ${Math.round(REPLAY_POLL_INTERVAL_MS / 1000)} 秒自动问一次后端状态`;
    return `<span class="badge ${meta.badge}" data-replay-status="${attr(run.status)}">${escapeHtml(meta.label)}${pollHint}</span>`;
  }

  function replayRunIsTracked(runId) {
    return state.replay.tracked != null && String(state.replay.tracked.runId) === String(runId);
  }

  // queued 的回放**可以真的启动**：先登记（execute:false）再点这里是完整路径，
  // 所以按钮必须真的调 POST /runs/{id}/execute，而不是让那一轮永远停在排队中。
  function replayRunActions(run) {
    const status = String(run.status || "");
    const busy = state.replay.running ? " disabled" : "";
    if (status === "queued") {
      return `<button class="primary-button" type="button" data-action="replay-execute-run" data-run-id="${attr(run.id)}"${busy}>${icon("play")}启动这一轮</button>`;
    }
    if (status === "running") {
      return `<button class="danger-button" type="button" data-action="replay-cancel-run" data-run-id="${attr(run.id)}"${busy}>${icon("ban")}取消</button>`;
    }
    if (replayRunIsTracked(run.id) && !REPLAY_TERMINAL_STATUSES.has(status)) {
      return `<button class="secondary-button" type="button" data-action="replay-stop-tracking" data-run-id="${attr(run.id)}"${busy}>${icon("eye-off")}停止跟进</button>`;
    }
    return "";
  }

  function replayRunRow(run) {
    const active = String(state.replay.runId) === String(run.id);
    const finished = run.status === "completed";
    const terminal = REPLAY_TERMINAL_STATUSES.has(String(run.status));
    const runError = replayRunErrorLabel(run.error);
    return `
      <div class="replay-run status-${attr(run.status)}${active ? " is-active" : ""}" data-replay-run="${attr(run.id)}" data-run-status="${attr(run.status)}">
        <div class="replay-run-head">
          <strong>回放 #${escapeHtml(run.id)}</strong>
          ${replayRunStatusBadge(run)}
          ${finished ? "" : `<span class="badge ${run.status === "failed" || run.status === "interrupted" ? "danger" : "warning"}">${terminal ? "结果不完整" : "尚未结束"}</span>`}
          ${replayRunIsTracked(run.id) ? `<span class="badge info" data-replay-tracked>页面正在跟进</span>` : ""}
          ${active ? `<span class="badge info">当前查看</span>` : ""}
          <button class="mini-icon-button" type="button" data-action="replay-open-run" data-run-id="${attr(run.id)}" aria-label="查看回放 ${attr(run.id)} 的结果" title="查看结果">${icon("chevron-right")}</button>
        </div>
        <span class="field-hint">基线 ${escapeHtml(run.baseline_version || "-")} → 候选 ${escapeHtml(run.candidate_version || "-")} · 创建于 ${escapeHtml(formatMoment(run.created_at))}</span>
        ${runError ? `<p class="replay-side-error">${icon("circle-alert")}${escapeHtml(runError)}</p>` : ""}
        ${run.status === "running" && run.cancel_requested ? `<span class="badge warning">已请求取消，等待执行器停下来</span>` : ""}
        ${Number(run.changed_count || 0) ? `<span class="field-hint">判定变化 ${escapeHtml(run.changed_count)} 条</span>` : ""}
        <div class="toolbar-row">${replayRunActions(run)}</div>
      </div>`;
  }

  function replayResultMarkup() {
    const detail = state.replay.detail;
    if (!detail) return `<p class="field-hint">选择一条回放记录查看逐条结果。</p>`;
    const run = detail.run || {};
    const results = Array.isArray(detail.results) ? detail.results : [];
    const status = String(run.status || "");
    const finished = status === "completed";
    const terminal = REPLAY_TERMINAL_STATUSES.has(status);
    const runError = replayRunErrorLabel(run.error);
    const bounds = detail.bounds || state.replay.lastBounds || null;
    // 失败 / 中断 / 被取消 / 超时：**都不能**当成一次完成的对照。
    const banner = finished
      ? ""
      : `<div class="notice ${status === "failed" || status === "interrupted" ? "danger" : ""}" data-replay-partial-banner>
          ${icon(status === "failed" || status === "interrupted" ? "shield-x" : "triangle-alert")}
          <span><strong>${terminal
            ? (status === "cancelled" ? "这是一次被取消的回放" : status === "interrupted" ? "这是一次被中断的回放" : status === "failed" ? "这是一次失败的回放" : "这是一次提前停止的回放")
            : "这一轮还在进行中"}，下面的结果只是部分结果。</strong>${runError ? escapeHtml(runError) : ""}${terminal ? "部分结果不能当作一次完整对照来读。" : "页面会继续自动刷新，结束后再看结论。"}</span>
        </div>`;
    const snapshot = detail.baseline_snapshot && typeof detail.baseline_snapshot === "object" ? detail.baseline_snapshot : null;
    const promptVisible = snapshot ? snapshot.prompt_visible !== false : null;
    const promptDigest = snapshot ? String(snapshot.prompt_digest || "") : "";
    return `
      <div class="replay-detail" data-replay-detail data-run-id="${attr(run.id)}" data-run-status="${attr(status)}">
        ${banner}
        <div class="stat-row">
          ${replayRunStatusBadge(run)}
          <span class="badge">基线 ${escapeHtml(run.baseline_version || "-")}</span>
          <span class="badge">候选 ${escapeHtml(run.candidate_version || "-")}</span>
          <span class="badge">实际评估 ${escapeHtml(detail.result_count ?? results.length)} 条</span>
          <span class="badge">开始 ${escapeHtml(formatMoment(run.started_at))}</span>
          <span class="badge">结束 ${escapeHtml(formatMoment(run.finished_at))}</span>
          <button class="secondary-button" type="button" data-action="replay-copy-run-candidate" data-run-id="${attr(run.id)}">${icon("copy")}复制这次候选 JSON</button>
        </div>
        <div class="toolbar-row">${replayRunActions(run)}</div>
        ${replayBoundsMarkup(bounds, run)}
        <div class="stat-row">
          <span class="badge info">${icon("lock")}基线规则 ${escapeHtml((snapshot?.rules || []).length)} 条</span>
          ${promptVisible === false
            ? `<span class="badge" data-replay-prompt-withheld>${icon("eye-off")}审核提示词不可见（仅超级管理员可见，摘要 ${escapeHtml(promptDigest || "—")}）</span>`
            : snapshot
              ? `<span class="badge">${icon("eye")}基线审核提示词对你可见</span>`
              : ""}
          <span class="badge">候选规则 ${escapeHtml((Array.isArray(detail.candidate_rules) ? detail.candidate_rules : []).length)} 条</span>
          ${detail.candidate_prompt_visible
            ? `<span class="badge">${icon("eye")}候选提示词对你可见</span>`
            : `<span class="badge" data-replay-prompt-withheld>${icon("eye-off")}候选提示词不可见（仅超级管理员可见）</span>`}
          ${replayCapacitySummary(detail.capacity) ? `<span class="badge info" data-replay-capacity>${escapeHtml(replayCapacitySummary(detail.capacity))}</span>` : ""}
        </div>
        ${replayFidelityMarkup(run)}
        ${replayStatsMarkup(run)}
        ${paginatedRows(results, `replay:results:${run.id}`, replayResultRow, "这次回放没有留下结果行。")}
      </div>`;
  }

  // 界值文案：条数上限 / 墙钟预算**都来自后端返回的 bounds**，客户端不写死数字。
  // （曾经在这里写死"25 条 / 120 秒"，服务端改成后台有界回放 200 条 / 1800 秒后，
  // 界面就在说一个已经不存在的上限。）
  function replayBoundsHint() {
    const bounds = state.replay.lastBounds;
    if (!bounds || typeof bounds !== "object") {
      return "回放已登记到后台队列，由服务端执行；页面每 "
        + `${Math.round(REPLAY_POLL_INTERVAL_MS / 1000)} 秒自动问一次进度，可以随时取消。`;
    }
    const parts = [
      `样本池 ${bounds.sample_pool} 条`,
      `单次上限 ${bounds.sample_limit} 条`,
    ];
    if (bounds.deadline_seconds != null) parts.push(`墙钟预算 ${Math.round(Number(bounds.deadline_seconds))} 秒`);
    return `${parts.join("、")}。回放在后台执行，超出会标记为提前停止并只保留部分结果；实际条数以结果页的「本次评估条数」为准。`;
  }

  function renderReplayPanel(group) {
    // group 为 null = 跨群版本（最高管理员的「审核验证」页）；
    // 给了 group = 逐群版本（群卡片里的「审核回放」分节）。
    const scoped = Boolean(group);
    const viewGroup = replayScopeGroup(group);
    const loaded = replayDataIsFor(viewGroup);
    const blocker = replayRunBlocker(viewGroup, loaded);
    const candidate = replayCandidatePreview();
    const running = state.replay.running;
    const scopeAttr = scoped
      ? `data-replay-scope="group" data-replay-group-id="${attr(viewGroup ? viewGroup.id : "")}"`
      : `data-replay-scope="picker"`;
    const panelId = scoped ? `replay-${attr(viewGroup ? viewGroup.id : "")}` : "replay";
    const header = scoped
      ? `<div class="replay-scope-line">
          ${icon("users")}
          <span><strong>本群</strong>：${escapeHtml(viewGroup ? replayGroupLabel(viewGroup) : "—")}。样本与回放严格按群隔离，这里读不到也碰不到任何别的群。</span>
        </div>`
      : `<div class="field-grid single">
          <div class="field">
            <div class="field-label-row"><label class="field-label" for="replay-group">目标群组</label></div>
            <select id="replay-group" data-replay-group>
              ${replayGroups().map(item => `<option value="${attr(item.id)}"${String(item.id) === String(viewGroup ? viewGroup.id : "") ? " selected" : ""}>${escapeHtml(replayGroupLabel(item))}</option>`).join("")}
            </select>
            <span class="field-hint">样本严格按群隔离：只会读取你有权限的群自己的记录。</span>
          </div>
        </div>`;
    return `
      <section class="replay-panel${scoped ? " replay-panel-scoped" : " replay-panel-global"}" ${scopeAttr} data-replay-panel aria-busy="${running ? "true" : "false"}">
        ${scoped ? "" : `${sectionHead(
          "审核回放（dry-run）",
          "用已脱敏的历史发言，把「当前生效的群规 + 提示词」和一份候选提案各跑一遍，看差异再决定要不要改线上。",
        )}`}
        <div class="notice danger">
          ${icon("shield-alert")}
          <span><strong>这是只读的离线对照工具。</strong>回放只写回放记录，不会发布任何群规或提示词，不会封禁、禁言、删除或扣分，也没有「一键生效」。最强的一步也只是把候选 JSON 复制 / 导出，由人带到别处去。</span>
        </div>
        ${viewGroup ? header : `<div class="notice">${icon("info")}<span>你还没有任何已授权的群，回放需要先在「权限封禁」里授权一个群。</span></div>`}
        ${state.replay.loading ? `<div class="loading-state inline"><span class="spinner"></span><p>正在加载样本与回放记录</p></div>` : ""}
        ${viewGroup ? `
          <div class="toolbar-row">
            <button class="secondary-button" type="button" data-action="replay-load"${running || state.replay.loading ? " disabled" : ""}>
              ${state.replay.loading
                ? `<span class="spinner spinner-compact"></span><span>加载中…</span>`
                : `${icon("refresh-cw")}${loaded ? "重新加载本群数据" : "加载本群样本与回放记录"}`}
            </button>
            <span class="field-hint">${loaded ? "已加载本群的样本与回放记录。" : "本页只显示本群的数据；换群会在各自的卡片里各看各的。"}</span>
          </div>
          ${advancedPanel(`${panelId}-samples`, "样本库", "脱敏后的历史发言。样本带留存期，过期后不参与新的回放。", renderReplaySamplesPanel(loaded))}
          ${advancedPanel(`${panelId}-candidate`, "候选规则与提示词", "编辑一份能被后端接受的 {rules, prompt?} 提案；留空的提示词表示沿用当前生效的提示词。", renderReplayCandidatePanel(candidate))}
          ${advancedPanel(`${panelId}-runs`, "回放记录与结果", "每一轮回放的基线 / 候选逐条对照。", renderReplayRunsPanel(loaded))}
          <div class="toolbar-row replay-launcher">
            <button class="primary-button" type="button" data-action="replay-run"${running || blocker ? " disabled" : ""} aria-busy="${running ? "true" : "false"}">
              ${running ? `<span class="spinner spinner-compact"></span><span>正在登记…</span>` : `${icon("flask-conical")}<span>开始回放（dry-run）</span>`}
            </button>
            <button class="secondary-button" type="button" data-action="replay-refresh"${running || state.replay.loading ? " disabled" : ""}>${icon("refresh-cw")}刷新</button>
            <span class="field-hint">${running ? "正在把这一轮登记到后台队列。" : escapeHtml(blocker || replayBoundsHint())}</span>
          </div>` : ""}
      </section>`;
  }

  function renderReplaySamplesPanel(loaded) {
    const samples = loaded ? (state.replay.samples || []) : [];
    const active = loaded ? replayActiveSampleCount() : 0;
    const disabled = state.replay.running ? " disabled" : "";
    const kinds = Object.entries(REPLAY_SOURCE_KIND_LABELS)
      .filter(([value]) => value !== "manual")
      .map(([value, label]) => `
        <label class="compact-check">
          <input type="checkbox" data-replay-import-kind value="${attr(value)}" checked${disabled}>
          <span>${escapeHtml(label)}</span>
        </label>`).join("");
    return `
      <div class="replay-samples">
        <div class="stat-row">
          <span class="badge info">可回放 ${active} 条</span>
          <span class="badge">共 ${samples.length} 条</span>
          ${state.replay.ttlDays ? `<span class="badge">留存 ${escapeHtml(state.replay.ttlDays)} 天</span>` : ""}
          <label class="compact-check">
            <input type="checkbox" data-replay-show-expired${state.replay.showExpired ? " checked" : ""}>
            <span>显示已过留存期的样本</span>
          </label>
        </div>
        <div class="notice info">
          ${icon("lock")}
          <span>样本是<strong>别人发言的脱敏副本</strong>：Telegram id、@用户名和电话号码在写入时就被抹掉了，留存期 ${escapeHtml(state.replay.ttlDays || 30)} 天。样本<strong>不带</strong>任何群标题、message_id 或用户标识。</span>
        </div>
        <div class="toolbar-row">
          <button class="secondary-button" type="button" data-action="replay-import"${disabled}>${icon("download")}从违规记录导入</button>
          <span class="field-hint">${kinds ? "来源：" : ""}</span>
          ${kinds}
          <label class="compact-check">
            <span>最多</span>
            <input class="replay-limit" type="number" min="1" max="200" step="1" value="50" data-replay-import-limit${disabled}>
          </label>
        </div>
        <form class="replay-sample-form" data-replay-form="sample">
          <div class="replay-sample-form-grid">
            <label class="field">
              <span class="field-label">样本正文</span>
              <textarea rows="3" maxlength="1200" required data-replay-sample-field="text" placeholder="要拿来对照的发言内容"${disabled}></textarea>
            </label>
            <label class="field">
              <span class="field-label">上下文（可选）</span>
              <textarea rows="2" maxlength="1200" data-replay-sample-field="context" placeholder="同一段对话里的前文，帮助判断语境"${disabled}></textarea>
            </label>
            <label class="field">
              <span class="field-label">金标（可选）</span>
              <select data-replay-sample-field="gold_label"${disabled}>
                <option value="">不标注（不参与误伤率）</option>
                <option value="violation">违规</option>
                <option value="benign">正常</option>
              </select>
              <span class="field-hint">标了金标，这条样本才会进入误伤 / 漏放统计。</span>
            </label>
          </div>
          <div class="toolbar-row">
            <button class="secondary-button" type="submit"${disabled}>${icon("plus")}加入样本库</button>
            <span class="field-hint">金标只能由人来标；不标就是"还不知道正确答案"，不会硬算成通过。</span>
          </div>
        </form>
        <div class="item-list">
          ${samples.length
            ? samples.map(replaySampleRow).join("")
            : `<p class="field-hint">${loaded ? "还没有样本。先从违规记录导入，或手工录入一两条。" : "尚未加载本群样本。点上面的「加载本群样本与回放记录」再看。"}</p>`}
        </div>
      </div>`;
  }

  function renderReplayCandidatePanel(candidate) {
    const disabled = state.replay.running ? " disabled" : "";
    const rules = state.replay.rules || [];
    const baselinePrompt = String(getPath(state.config, "prompts.moderation") || "");
    // 角色矩阵：审核提示词是**全局**口径，界面不能假装群管理员也能写它。
    const canManagePrompt = Boolean(state.session?.can_manage_global);
    return `
      <div class="replay-candidate">
        <div class="notice info">
          ${icon("info")}
          <span>候选只会以 <code>{rules, prompt?}</code> 的结构提交：规则类型限 <code>keyword / regex / llm</code>，处置限 <code>warn / delete / ban</code>，单条内容上限 ${REPLAY_PATTERN_MAX_CHARS} 字符，最多 ${REPLAY_MAX_RULES} 条。不勾"沿用"时，候选提示词必须包含 <code>{rules_json}</code> 占位符并能通过与线上同一个校验器。</span>
        </div>
        <div class="toolbar-row">
          <button class="secondary-button" type="button" data-action="replay-add-rule"${disabled}>${icon("plus")}加一条候选规则</button>
          <button class="secondary-button" type="button" data-action="replay-copy-candidate"${candidate.error ? " disabled" : ""}>${icon("copy")}复制候选 JSON</button>
          <button class="secondary-button" type="button" data-action="replay-export-candidate"${candidate.error ? " disabled" : ""}>${icon("file-down")}导出候选 JSON</button>
          <span class="field-hint">${rules.length ? `当前 ${rules.length} 条候选规则。` : "候选规则为空 = 这一轮假设「一条群规都不启用」。"}</span>
        </div>
        <div class="replay-rules">
          ${rules.length
            ? rules.map(replayCandidateRuleRow).join("")
            : `<p class="field-hint">还没有候选规则。只改提示词的话，可以直接展开下面那段提示词编辑。</p>`}
        </div>
        ${canManagePrompt
          ? `<label class="compact-check replay-prompt-toggle">
              <input type="checkbox" data-replay-use-baseline-prompt${state.replay.useBaselinePrompt ? " checked" : ""}${disabled}>
              <span>沿用当前生效的审核提示词（推荐）</span>
            </label>
            ${state.replay.useBaselinePrompt ? "" : `
              <label class="field full">
                <span class="field-label">候选提示词</span>
                <textarea rows="12" data-replay-prompt>${escapeHtml(state.replay.prompt || baselinePrompt)}</textarea>
                <span class="field-hint">必须包含 <code>{rules_json}</code>；JSON 示例写成 <code>{{ ... }}</code> 双花括号。</span>
              </label>`}`
          : `<div class="notice" data-replay-prompt-forbidden>
              ${icon("eye-off")}
              <span><strong>候选审核提示词对你不可见。</strong>审核提示词是<strong>全局</strong>口径、只有超级管理员能看能改；服务端对你提交的提示词也会直接 403 拒绝。规则对照照常可用，这里固定沿用当前生效的提示词。</span>
            </div>`}
        <pre class="replay-json" data-replay-json>${escapeHtml(candidate.error ? `// 候选尚未成形：${candidate.error}` : JSON.stringify(candidate.candidate, null, 2))}</pre>
        ${candidate.error ? `<p class="replay-side-error" data-replay-candidate-error>${icon("circle-alert")}${escapeHtml(candidate.error)}</p>` : ""}
      </div>`;
  }

  function renderReplayRunsPanel(loaded) {
    const runs = loaded ? (state.replay.runs || []) : [];
    return `
      <div class="replay-runs">
        ${paginatedRows(runs, "replay:runs", replayRunRow, loaded ? "还没有回放记录。" : "尚未加载本群的回放记录。")}
        ${state.replay.detailLoading ? `<div class="loading-state inline"><span class="spinner"></span><p>正在加载回放结果</p></div>` : ""}
        ${loaded ? replayResultMarkup() : ""}
      </div>`;
  }

  async function loadReplayWorkspace({ force = false, group = null } = {}) {
    const target = group || replayActiveGroup();
    if (!target || state.replay.running) return;
    if (!force && state.replay.loadedGroupId === String(target.id)) return;
    // 逐群面板可能同时展开好几张卡片：A 群的请求晚于 B 群的响应返回时，
    // 迟到的那个必须自己走掉，否则 A 的样本会落到 B 的卡片上。
    const token = ++state.replay.loadToken;
    state.replay.loading = true;
    renderContent();
    try {
      const query = state.replay.showExpired ? "?include_expired=1" : "";
      const [samples, runs] = await Promise.all([
        apiFetch(`/api/v1/groups/${encodeURIComponent(target.id)}/moderation-replay/samples${query}`),
        apiFetch(`/api/v1/groups/${encodeURIComponent(target.id)}/moderation-replay/runs`),
      ]);
      if (token !== state.replay.loadToken) return;
      state.replay.groupId = target.id;
      state.replay.samples = Array.isArray(samples?.samples) ? samples.samples : [];
      state.replay.ttlDays = Number(samples?.ttl_days || 0);
      state.replay.runs = Array.isArray(runs?.runs) ? runs.runs : [];
      state.replay.lastCapacity = runs?.capacity || state.replay.lastCapacity;
      state.replay.loadedGroupId = String(target.id);
      // 正在跟进的 run 必须属于**当前这个群**才会留下；换群就丢掉它，
      // 否则 A 群的轮询结果会被画到 B 群的卡片上。
      if (state.replay.tracked
        && String(state.replay.tracked.groupId) !== String(target.id)) {
        state.replay.tracked = null;
      }
      if (state.replay.runId
        && !state.replay.runs.some(run => String(run.id) === String(state.replay.runId))) {
        state.replay.runId = null;
        state.replay.detail = null;
      }
      if (state.replay.detail
        && String(state.replay.detail.group_id ?? state.replay.detail.run?.group_id ?? target.id) !== String(target.id)) {
        state.replay.detail = null;
      }
      // 列表已经知道这一轮结束了，详情就必须跟上：否则 run 在 400ms 内失败，
      // 轮询会按"它还活着"停掉，而详情永远停在"执行中"。
      const listed = state.replay.detail?.run
        ? state.replay.runs.find(item => String(item.id) === String(state.replay.detail.run.id))
        : null;
      if (listed && String(listed.status) !== String(state.replay.detail.run.status)) {
        const previous = String(state.replay.detail.run.status);
        state.replay.detail = { ...state.replay.detail, run: listed };
        if (!REPLAY_TERMINAL_STATUSES.has(previous) && REPLAY_TERMINAL_STATUSES.has(String(listed.status))) {
          replayFinishToast(listed);
        }
      }
    } catch (error) {
      if (token !== state.replay.loadToken) return;
      state.replay.samples = [];
      state.replay.runs = [];
      state.replay.loadedGroupId = null;
      showToast(REPLAY_IDEMPOTENCY_LABELS[replayErrorCode(error.body)] || error.message || "回放数据加载失败", "error", 6000);
    } finally {
      if (token === state.replay.loadToken) {
        state.replay.loading = false;
        renderContent();
        syncReplayPolling();
      }
    }
  }

  // 换群：样本、详情、跟进中的 run 全部按群重置，避免 A 群的数据落到 B 群。
  function resetReplayScope(group) {
    stopReplayPolling();
    state.replay.detailToken += 1;
    state.replay.detail = null;
    state.replay.runId = null;
    state.replay.lastBounds = null;
    if (group != null) {
      state.replay.groupId = group.id;
      // idempotency_key 绑定「群 + 候选」：换群必须换 key，否则同一个 key 配
      // 另一群的候选会被后端判成 409 replay_idempotency_conflict。
      state.replay.pendingKey = "";
      state.replay.pendingCandidateJson = "";
      state.replay.pendingKeyGroupId = "";
    }
  }

  async function fetchReplayRunDetail(groupId, runId) {
    return apiFetch(
      `/api/v1/groups/${encodeURIComponent(groupId)}/moderation-replay/runs/${encodeURIComponent(runId)}`,
    );
  }

  // 把一次 run 响应并进本地状态：runs 列表 + 详情共用同一份口径。
  // 作用域再确认一次：响应里的 group_id 不是当前已加载的群，就整份丢掉。
  function applyReplayRunPayload(payload, runId) {
    if (!payload || typeof payload !== "object") return;
    const run = payload.run && typeof payload.run === "object" ? payload.run : null;
    if (!run) return;
    const loaded = state.replay.loadedGroupId;
    if (loaded != null && String(run.group_id) !== String(loaded)) return;
    if (payload.bounds) state.replay.lastBounds = payload.bounds;
    if (payload.capacity) state.replay.lastCapacity = payload.capacity;
    const index = state.replay.runs.findIndex(item => String(item.id) === String(run.id));
    if (index >= 0) state.replay.runs.splice(index, 1, run);
    else state.replay.runs.unshift(run);
    if (String(run.id) === String(runId)) state.replay.detail = payload;
  }

  async function openReplayRun(runId, group) {
    const target = group || replayActiveGroup();
    if (!target) return;
    const token = ++state.replay.detailToken;
    state.replay.runId = runId;
    state.replay.detailLoading = true;
    renderContent();
    try {
      const result = await fetchReplayRunDetail(target.id, runId);
      // 迟到的响应自己走掉：A 群的详情绝不能画到 B 群的卡片上。
      if (token !== state.replay.detailToken) return;
      applyReplayRunPayload(result, runId);
      state.listPages.set(`replay:results:${runId}`, 1);
    } catch (error) {
      if (token !== state.replay.detailToken) return;
      state.replay.detail = null;
      showToast(REPLAY_IDEMPOTENCY_LABELS[replayErrorCode(error.body)] || error.message || "回放结果加载失败", "error", 6000);
    } finally {
      if (token === state.replay.detailToken) {
        state.replay.detailLoading = false;
        renderContent();
      }
    }
  }

  // --------------------------------------------------------------------------
  // 轮询：回放在后台跑，页面只是隔一会儿问一次状态。
  //
  // 三纪律：
  //   1. 只在**视图还在**时轮询（视图被重建就没有轮询了），离开页面不漏定时器；
  //   2. 每轮带一个 pollToken，换群 / 取消跟进之后旧的那一轮自己走掉；
  //   3. tracked 存在 state 里，所以离开页面再回来这一轮还在，进度不会丢。
  // --------------------------------------------------------------------------
  // 轮询时页面会被整块重渲染。如果每 2 秒都无条件重渲染，管理员手指底下的按钮
  // 会在点击落下的瞬间被换掉（真实的"点空了"）。这里给 run 状态算一个签名，
  // 只有真正变了才重渲染。
  function replayPayloadSignature(payload) {
    const run = payload?.run || {};
    return [
      run.id,
      run.status,
      run.error,
      run.evaluated,
      run.changed_count,
      run.results_purged,
      run.cancel_requested,
      run.attempt,
      run.finished_at,
      payload?.result_count,
    ].join("|");
  }

  function replayTrackedRun() {
    const tracked = state.replay.tracked;
    if (!tracked) return null;
    return (state.replay.runs || []).find(run => String(run.id) === String(tracked.runId)) || null;
  }

  function replayPanelMounted(groupId) {
    const scoped = content.querySelector(
      `[data-replay-panel][data-replay-group-id="${CSS.escape(String(groupId))}"]`,
    );
    if (scoped) return true;
    const picker = content.querySelector('[data-replay-panel][data-replay-scope="picker"]');
    return Boolean(picker) && String(state.replay.groupId) === String(groupId);
  }

  function replayShouldPoll() {
    const tracked = state.replay.tracked;
    if (!tracked || tracked.runId == null) return false;
    const run = replayTrackedRun();
    // 列表里这一轮已经结束（或已经不在当前群的列表里）就不再轮询。
    if (!run) return false;
    if (REPLAY_TERMINAL_STATUSES.has(String(run.status))) return false;
    return replayPanelMounted(tracked.groupId);
  }

  function stopReplayPolling() {
    state.replay.pollToken += 1;
    if (state.replay.pollTimer != null) {
      window.clearTimeout(state.replay.pollTimer);
      state.replay.pollTimer = null;
    }
    state.replay.polling = false;
  }

  function syncReplayPolling() {
    // 每次重渲染都要重新判断一次：视图被拆掉（离开这一页）就必须把在飞的定时器
    // 收掉，否则就是一个没人看的循环在后台一直问后端。早退在这里是错的——
    // 早退会让"页面已经不在了"这件事永远没机会被看见。
    if (!replayShouldPoll()) {
      stopReplayPolling();
      return;
    }
    if (state.replay.polling) return;
    state.replay.polling = true;
    scheduleReplayPoll(0);
  }

  function scheduleReplayPoll(delay = REPLAY_POLL_INTERVAL_MS) {
    if (!state.replay.polling) return;
    if (state.replay.pollTimer != null) window.clearTimeout(state.replay.pollTimer);
    const token = ++state.replay.pollToken;
    state.replay.pollTimer = window.setTimeout(async () => {
      state.replay.pollTimer = null;
      if (!state.replay.polling || token !== state.replay.pollToken) return;
      await pollReplayRun();
    }, delay);
  }

  function replayFinishToast(run) {
    const status = String(run?.status || "");
    const label = run?.id == null ? "" : `回放 #${run.id} `;
    if (status === "completed") {
      showToast(`${label}完成`, "success", 4000);
      return;
    }
    if (status === "cancelled") {
      showToast(`${label}已取消，下面只有部分结果`, "warning", 7000);
      return;
    }
    if (status === "interrupted") {
      showToast(`${label}执行器中断，结果不完整`, "error", 8000);
      return;
    }
    if (status === "failed") {
      showToast(`${label}失败：${replayRunErrorLabel(run.error) || "未知原因"}`, "error", 8000);
      return;
    }
    showToast(`${label}已登记为 ${status || "未知状态"}，页面会继续跟进`, "warning", 5000);
  }

  async function pollReplayRun() {
    const tracked = state.replay.tracked;
    if (!tracked || tracked.runId == null) {
      stopReplayPolling();
      return;
    }
    const token = state.replay.pollToken;
    const groupId = tracked.groupId;
    const runId = tracked.runId;
    const wasTerminal = (() => {
      const run = replayTrackedRun();
      return run ? REPLAY_TERMINAL_STATUSES.has(String(run.status)) : false;
    })();
    const before = replayPayloadSignature(state.replay.detail);
    try {
      const result = await fetchReplayRunDetail(groupId, runId);
      // 换群 / 取消跟进之后，这一轮的响应必须自己走掉。
      if (token !== state.replay.pollToken || !state.replay.polling) return;
      if (!state.replay.tracked || String(state.replay.tracked.runId) !== String(runId)
        || String(state.replay.tracked.groupId) !== String(groupId)) return;
      applyReplayRunPayload(result, runId);
      const run = result?.run || {};
      const terminal = REPLAY_TERMINAL_STATUSES.has(String(run.status || ""));
      if (terminal) {
        stopReplayPolling();
        if (!wasTerminal) replayFinishToast(run);
      }
      // 只有状态真的变了才重渲染：没变的那一轮不该把人手指底下的按钮换掉。
      if (replayPayloadSignature(result) !== before) renderContent();
    } catch (error) {
      if (token !== state.replay.pollToken || !state.replay.polling) return;
      // 轮询失败不该把这一轮判死：提示一次，下一轮继续问。
      showToast(REPLAY_IDEMPOTENCY_LABELS[replayErrorCode(error.body)] || error.message || "回放进度查询失败，将自动重试", "warning", 5000);
    } finally {
      if (state.replay.polling && token === state.replay.pollToken) {
        if (!REPLAY_TERMINAL_STATUSES.has(String(replayTrackedRun()?.status || ""))) {
          scheduleReplayPoll();
        } else {
          stopReplayPolling();
        }
      }
    }
  }

  function startReplayTracking(groupId, runId) {
    stopReplayPolling();
    state.replay.tracked = { groupId, runId };
  }

  function replayIdempotencyKey(groupId, candidateJson) {
    // key 绑定「群 + 候选」：同群同候选的重试复用同一个 key（后端因此认成重放，
    // 不会重复开一轮）；换群或改候选必须换 key，否则会撞上 409。
    const scope = String(groupId);
    if (!state.replay.pendingKey
      || state.replay.pendingKeyGroupId !== scope
      || state.replay.pendingCandidateJson !== candidateJson) {
      state.replay.pendingKey = `ui-${scope}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
      state.replay.pendingKeyGroupId = scope;
      state.replay.pendingCandidateJson = candidateJson;
    }
    return state.replay.pendingKey;
  }

  async function startReplayRun(node) {
    const group = replayScopeOf(node);
    if (!group || state.replay.running) return;
    const blocker = replayRunBlocker(group, replayDataIsFor(group));
    if (blocker) {
      showToast(blocker, "warning", 5000);
      return;
    }
    let candidate;
    try {
      candidate = buildReplayCandidate();
    } catch (error) {
      showToast(error.message || "候选尚未成形", "error", 5000);
      return;
    }
    const candidateJson = JSON.stringify(candidate);
    const idempotencyKey = replayIdempotencyKey(group.id, candidateJson);
    state.replay.running = true;
    renderContent();
    let payload = null;
    try {
      // 登记即返回：后端把这一轮丢进后台队列，run.status 是 queued / running。
      // 界面**不再**阻塞在这一次请求上等它跑完。
      payload = await apiFetch(`/api/v1/groups/${encodeURIComponent(group.id)}/moderation-replay/runs`, {
        method: "POST",
        body: JSON.stringify({ candidate, idempotency_key: idempotencyKey, execute: true }),
      });
    } catch (error) {
      const details = replayErrorDetails(error.body);
      const capacity = replayCapacitySummary(details?.capacity || state.replay.lastCapacity);
      const code = replayErrorCode(error.body);
      showToast(
        `${REPLAY_IDEMPOTENCY_LABELS[code] || error.message || "回放登记失败"}${capacity ? `（${capacity}）` : ""}`
        + (code === "replay_busy" || error.status >= 500
          ? "。可以直接再点一次：同一份候选会复用同一个 idempotency_key，不会重复开一轮。"
          : ""),
        "error",
        8000,
      );
      // 失败**不清 key**：原地重试必须复用同一个 key，否则一次网络抖动就会
      // 悄悄登记出两条几乎一样的回放。
      return;
    } finally {
      state.replay.running = false;
      renderContent();
    }
    const run = payload?.run && typeof payload.run === "object" ? payload.run : {};
    // 登记成功 = 这一轮已经有归属了，key 可以作废。
    state.replay.pendingKey = "";
    state.replay.pendingKeyGroupId = "";
    state.replay.pendingCandidateJson = "";
    state.replay.runId = run.id ?? null;
    applyReplayRunPayload(payload, run.id);
    startReplayTracking(group.id, run.id);
    showToast(
      `回放 #${run.id ?? "?"} 已登记（${REPLAY_STATUS_META[run.status]?.label || run.status || "排队中"}），页面会自动跟进进度`,
      "info",
      4000,
    );
    await loadReplayWorkspace({ force: true, group });
    // 列表刷新可能把这一轮重新读回来；接着开始轮询。
    if (replayShouldPoll()) {
      state.replay.polling = true;
      scheduleReplayPoll(0);
    }
  }

  async function cancelReplayRun(node) {
    const runId = node?.dataset?.runId ?? state.replay.tracked?.runId;
    const group = replayScopeOf(node);
    if (group == null || runId == null || state.replay.running) return;
    state.replay.running = true;
    renderContent();
    try {
      // 取消是**真的**调用后端；后端把 run 标成 cancelled / cancel_requested，
      // 界面继续轮询去**看到**它变成 cancelled，而不是自己假装取消了。
      const result = await apiFetch(
        `/api/v1/groups/${encodeURIComponent(group.id)}/moderation-replay/runs/${encodeURIComponent(runId)}/cancel`,
        { method: "POST" },
      );
      applyReplayRunPayload(result, runId);
      const run = result?.run || {};
      showToast(
        String(run.status) === "cancelled"
          ? `回放 #${runId} 已取消，下面只有部分结果`
          : `已请求取消回放 #${runId}，执行器停下来后状态会变成「已取消」`,
        "warning",
        5000,
      );
    } catch (error) {
      const code = replayErrorCode(error.body);
      showToast(REPLAY_IDEMPOTENCY_LABELS[code] || error.message || "取消失败", "error", 7000);
    } finally {
      state.replay.running = false;
      renderContent();
      // 不管取消请求成没成，只要这一轮还活着就继续盯着它的真实状态。
      if (replayShouldPoll()) {
        state.replay.polling = true;
        scheduleReplayPoll(0);
      } else {
        stopReplayPolling();
        await loadReplayWorkspace({ force: true, group });
      }
    }
  }

  async function executeQueuedReplayRun(node) {
    const runId = node?.dataset?.runId ?? state.replay.tracked?.runId;
    const group = replayScopeOf(node);
    if (group == null || runId == null || state.replay.running) return;
    state.replay.running = true;
    renderContent();
    try {
      const result = await apiFetch(
        `/api/v1/groups/${encodeURIComponent(group.id)}/moderation-replay/runs/${encodeURIComponent(runId)}/execute`,
        { method: "POST" },
      );
      applyReplayRunPayload(result, runId);
      startReplayTracking(group.id, runId);
      const accepted = result?.accepted !== false;
      showToast(
        accepted
          ? `回放 #${runId} 已排进后台队列`
          : `回放 #${runId} 已登记，但队列当前没有空位；稍后可以再点一次启动`,
        accepted ? "info" : "warning",
        5000,
      );
    } catch (error) {
      const code = replayErrorCode(error.body);
      showToast(REPLAY_IDEMPOTENCY_LABELS[code] || error.message || "启动失败", "error", 7000);
      if (code === "replay_not_queued" || code === "replay_run_not_found") {
        await loadReplayWorkspace({ force: true, group });
      }
    } finally {
      state.replay.running = false;
      renderContent();
      if (replayShouldPoll()) {
        state.replay.polling = true;
        scheduleReplayPoll(0);
      }
    }
  }

  function forgetReplayRun(node) {
    const runId = node?.dataset?.runId ?? state.replay.tracked?.runId;
    if (runId == null) return;
    if (state.replay.tracked && String(state.replay.tracked.runId) === String(runId)) {
      stopReplayPolling();
      state.replay.tracked = null;
    }
    if (String(state.replay.runId) === String(runId)) {
      state.replay.runId = null;
      state.replay.detail = null;
    }
    renderContent();
  }

  // 人工金标：确认 / 修正 / 清除。这是**唯一**能给样本打金标的入口，
  // 机器导入的建议永远只是建议。
  async function setReplaySampleLabel(node) {
    const sampleId = node?.dataset?.sampleId;
    const group = replayScopeOf(node);
    if (group == null || sampleId == null || state.replay.running) return;
    const raw = String(node.dataset.goldLabel ?? "");
    const goldLabel = raw.trim();
    const body = { gold_label: goldLabel === "" ? null : goldLabel };
    if (!await askConfirmation(
      goldLabel === "" ? "清除人工金标" : goldLabel === "violation" ? "把样本标为违规" : "把样本标为正常",
      goldLabel === ""
        ? "清除之后这条样本不再参与误伤 / 漏放统计（机器建议仍然保留在 provenance 里，只是不参与任何统计）。"
        : `只有人工确认过的金标才会进入误伤 / 漏放统计；确认人、确认时间与变更记录都会留下。`,
    )) return;
    state.replay.running = true;
    renderContent();
    try {
      const result = await apiFetch(
        `/api/v1/groups/${encodeURIComponent(group.id)}/moderation-replay/samples/${encodeURIComponent(sampleId)}/label`,
        { method: "PATCH", body: JSON.stringify(body) },
      );
      const sample = result?.sample;
      const index = state.replay.samples.findIndex(item => String(item.id) === String(sampleId));
      if (sample && index >= 0) state.replay.samples.splice(index, 1, sample);
      const history = Array.isArray(result?.label_history) ? result.label_history.length : 0;
      showToast(
        goldLabel === "" ? "已清除金标" : `金标已更新为「${REPLAY_GOLD_LABELS[goldLabel] || goldLabel}」（留痕 ${history} 条）`,
        "success",
        4000,
      );
    } catch (error) {
      const code = replayErrorCode(error.body);
      showToast(REPLAY_IDEMPOTENCY_LABELS[code] || error.message || "标注失败", "error", 7000);
    } finally {
      state.replay.running = false;
      renderContent();
      syncReplayPolling();
    }
  }

  async function importReplaySamples(node) {
    const group = replayScopeOf(node);
    if (!group || state.replay.running) return;
    const scope = node?.closest("[data-replay-scope]") || content;
    const kinds = [...scope.querySelectorAll("[data-replay-import-kind]:checked")].map(box => box.value);
    const limitInput = scope.querySelector("[data-replay-import-limit]");
    const limit = Number(limitInput?.value || 50);
    if (!kinds.length) {
      showToast("至少勾选一种来源", "warning", 4000);
      return;
    }
    if (!await askConfirmation(
      "从违规记录导入样本",
      `将把该群最近的违规记录脱敏后复制成回放样本（最多 ${limit} 条）。只复制脱敏正文，不带任何 Telegram 标识。`,
    )) return;
    state.replay.loading = true;
    renderContent();
    try {
      const result = await apiFetch(`/api/v1/groups/${encodeURIComponent(group.id)}/moderation-replay/samples/import`, {
        method: "POST",
        body: JSON.stringify({ kinds, limit: Number.isFinite(limit) ? limit : 50 }),
      });
      showToast(`已导入 ${Number(result?.imported || 0)} 条，跳过 ${Number(result?.skipped || 0)} 条`, "success", 5000);
      await loadReplayWorkspace({ force: true, group });
    } catch (error) {
      showToast(REPLAY_IDEMPOTENCY_LABELS[replayErrorCode(error.body)] || error.message || "导入失败", "error", 6000);
    } finally {
      state.replay.loading = false;
      renderContent();
    }
  }

  async function createReplaySample(form) {
    const group = replayScopeOf(form);
    if (!group || state.replay.running) return;
    const read = name => String(form.querySelector(`[data-replay-sample-field="${name}"]`)?.value ?? "");
    const text = read("text").trim();
    if (!text) {
      showToast("样本内容不能为空", "error", 4000);
      return;
    }
    const goldLabel = read("gold_label");
    form.setAttribute("aria-busy", "true");
    const submit = form.querySelector('[type="submit"]');
    if (submit) submit.disabled = true;
    try {
      await apiFetch(`/api/v1/groups/${encodeURIComponent(group.id)}/moderation-replay/samples`, {
        method: "POST",
        body: JSON.stringify({
          text,
          context: read("context"),
          ...(goldLabel ? { gold_label: goldLabel } : {}),
        }),
      });
      showToast("样本已加入", "success", 3000);
      await loadReplayWorkspace({ force: true, group });
    } catch (error) {
      showToast(REPLAY_IDEMPOTENCY_LABELS[replayErrorCode(error.body)] || error.message || "新增样本失败", "error", 6000);
    } finally {
      form.removeAttribute("aria-busy");
      if (submit) submit.disabled = false;
      renderContent();
    }
  }

  async function deleteReplaySample(node) {
    const sampleId = node?.dataset?.sampleId;
    const group = replayScopeOf(node);
    if (!group || sampleId == null || state.replay.running) return;
    if (!await askConfirmation("删除回放样本", `确认删除样本 ${sampleId}？已产生的回放结果会保留（结果行只存判定，不存正文）。`)) return;
    try {
      await apiFetch(`/api/v1/groups/${encodeURIComponent(group.id)}/moderation-replay/samples/${encodeURIComponent(sampleId)}`, { method: "DELETE" });
      showToast("样本已删除", "success", 3000);
      await loadReplayWorkspace({ force: true, group });
    } catch (error) {
      showToast(REPLAY_IDEMPOTENCY_LABELS[replayErrorCode(error.body)] || error.message || "删除失败", "error", 6000);
    }
  }

  function legacyCopyText(text) {
    const holder = document.createElement("textarea");
    holder.value = text;
    holder.setAttribute("readonly", "");
    holder.style.position = "fixed";
    holder.style.opacity = "0";
    document.body.appendChild(holder);
    holder.select();
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch (_) {
      copied = false;
    }
    holder.remove();
    return copied;
  }

  async function copyCandidateJson(candidate) {
    const text = JSON.stringify(candidate, null, 2);
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else if (!legacyCopyText(text)) {
        throw new Error("clipboard unavailable");
      }
      showToast("候选 JSON 已复制到剪贴板", "success", 4000);
      return;
    } catch (_) {
      // 沙箱 / 非安全上下文里 Clipboard API 会被拒绝，退回 execCommand。
      const copied = legacyCopyText(text);
      showToast(
        copied
          ? "候选 JSON 已复制到剪贴板"
          : "浏览器拒绝了剪贴板访问，请手动复制上面的候选 JSON",
        copied ? "success" : "warning",
        6000,
      );
    }
  }

  function exportCandidateJson(candidate) {
    const blob = new Blob([`${JSON.stringify(candidate, null, 2)}\n`], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "moderation-candidate.json";
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 2000);
    showToast("候选 JSON 已导出；它只是文件，不会写进线上配置", "success", 5000);
  }

  // --------------------------------------------------------------------------
  // Part B · 配置变更历史（仅最高管理员）
  // --------------------------------------------------------------------------
  const HISTORY_OPERATION_LABELS = {
    save: "保存",
    restore: "回滚",
    create: "创建",
    update: "更新",
    bootstrap: "初始化",
  };

  function historyRevisionOptions(selected) {
    return (state.configHistory.entries || []).map(entry =>
      `<option value="${attr(entry.revision)}"${String(entry.revision) === String(selected) ? " selected" : ""}>r${escapeHtml(entry.revision)}${entry.restore_source_revision ? `（回滚自 r${escapeHtml(entry.restore_source_revision)}）` : ""}</option>`).join("");
  }

  function historyConflictMarkup() {
    if (!state.configHistory.conflict) return "";
    return `
      <div class="notice danger" data-history-conflict>
        ${icon("shield-alert")}
        <span><strong>配置已被其他会话更新，这次操作没有写进去。</strong>页面上的草稿原样保留。</span>
        <button class="secondary-button" type="button" data-action="reload-config-after-conflict">${icon("refresh-cw")}重新加载配置</button>
      </div>`;
  }

  function historyRow(entry) {
    const revision = Number(entry.revision);
    const expanded = state.configHistory.expanded === revision;
    const operation = HISTORY_OPERATION_LABELS[entry.operation] || entry.operation || "变更";
    const pending = Array.isArray(entry.restart_pending) ? entry.restart_pending : [];
    const isCurrent = revision === Number(state.document?.revision || 0);
    return `
      <div class="history-row${expanded ? " is-expanded" : ""}">
        <div class="history-row-head">
          <button class="history-toggle" type="button" data-action="history-toggle" data-revision="${attr(revision)}" aria-expanded="${expanded ? "true" : "false"}" aria-label="展开修订 ${attr(revision)}">
            ${icon(expanded ? "chevron-down" : "chevron-right")}<strong>r${escapeHtml(revision)}</strong>
          </button>
          <span class="badge ${isCurrent ? "info" : ""}">${escapeHtml(operation)}</span>
          ${isCurrent ? `<span class="badge success">${icon("check")}当前生效</span>` : ""}
          ${entry.restore_source_revision ? `<span class="badge">回滚自 r${escapeHtml(entry.restore_source_revision)}</span>` : ""}
          ${pending.length ? `<span class="badge warning">${icon("rotate-ccw")}待重启 ${pending.length}</span>` : ""}
          ${isCurrent ? "" : `<button class="mini-icon-button" type="button" data-action="history-restore" data-revision="${attr(revision)}" aria-label="回滚到修订 ${attr(revision)}" title="回滚到这一版"${state.configHistory.restoring ? " disabled" : ""}>${icon("history")}</button>`}
        </div>
        <p class="field-hint history-summary">${escapeHtml(entry.summary || "（无摘要）")}</p>
        <span class="field-hint">父版本 r${escapeHtml(entry.parent_revision ?? "-")} · 由用户 ${escapeHtml(entry.updated_by ?? "-")} 于 ${escapeHtml(formatMoment(entry.updated_at))} 记录</span>
        ${expanded ? historyDetailMarkup(state.configHistory.detail) : ""}
      </div>`;
  }

  function historyDetailMarkup(detail) {
    if (state.configHistory.detailLoading && !detail) {
      return `<div class="loading-state inline"><span class="spinner"></span><p>正在加载该版本的脱敏快照</p></div>`;
    }
    if (!detail) return `<p class="field-hint">没有可显示的内容。</p>`;
    const leaves = flattenLeaves(detail.config || {});
    const secrets = Array.isArray(detail.secrets) ? detail.secrets : [];
    return `
      <div class="history-detail">
        <div class="notice info">
          ${icon("lock")}
          <span>下面是 r${escapeHtml(detail.revision)} 的<strong>脱敏快照</strong>：密钥位置只保留"配没配"的状态标记，<strong>没有任何密钥值</strong>。</span>
        </div>
        ${secrets.length ? `
          <div class="stat-row">
            <strong>密钥槽位（只有状态）</strong>
            ${secrets.map(item => `<span class="badge">${icon("key-round")}<code>${escapeHtml(item.path)}</code>：${escapeHtml(secretStatusLabel(item.status))}</span>`).join("")}
          </div>` : ""}
        <details class="field-help">
          <summary>${icon("chevron-down")}<span>展开脱敏配置（${leaves.length} 个字段）</span></summary>
          <div class="history-payload">
            ${leaves.map(leaf => `
              <div class="history-leaf">
                <code class="diff-path">${escapeHtml(leaf.path)}</code>
                ${redactedValueMarkup(leaf.value)}
              </div>`).join("")}
          </div>
        </details>
      </div>`;
  }

  function historyCompareMarkup() {
    const history = state.configHistory;
    const current = Number(state.document?.revision || 0);
    const against = history.against ?? current;
    const compareRevision = history.compareRevision;
    return `
      <div class="history-compare">
        <div class="field-grid three">
          <div class="field">
            <div class="field-label-row"><label class="field-label" for="history-against">对比基准</label></div>
            <select id="history-against" data-history-against>${historyRevisionOptions(against)}</select>
          </div>
          <div class="field">
            <div class="field-label-row"><label class="field-label" for="history-compare">目标版本</label></div>
            <select id="history-compare" data-history-compare>${historyRevisionOptions(compareRevision)}</select>
          </div>
          <div class="field">
            <div class="field-label-row"><span class="field-label">操作</span></div>
            <div class="toolbar-row">
              <button class="secondary-button" type="button" data-action="history-compare"${history.compareLoading ? " disabled" : ""}>
                ${history.compareLoading ? `<span class="spinner spinner-compact"></span><span>对比中…</span>` : `${icon("git-compare-arrows")}对比两版`}
              </button>
            </div>
          </div>
        </div>
        ${history.compare
          ? `<div class="history-compare-result">
              <div class="stat-row">
                <span class="badge info">r${escapeHtml(history.compare.against)} → r${escapeHtml(history.compare.revision)}</span>
                ${Object.entries(diffCounts(history.compare.diff)).filter(([key]) => key !== "secret").map(([key, value]) =>
                  `<span class="badge ${DIFF_CHANGE_META[key]?.badge || ""}">${escapeHtml(DIFF_CHANGE_META[key]?.label || key)} ${value}</span>`).join("")}
              </div>
              ${diffEntriesMarkup(history.compare.diff, "这两个版本之间没有差异。")}
            </div>`
          : `<p class="field-hint">选两版点"对比两版"，这里会列出脱敏后的逐字段差异。</p>`}
      </div>`;
  }

  function historyRestoreMarkup() {
    const restore = state.configHistory.restore;
    if (!restore) return "";
    if (restore.loading) {
      return `<div class="loading-state inline"><span class="spinner"></span><p>正在计算「当前配置 → 回滚候选」的差异</p></div>`;
    }
    if (restore.error) {
      return `<div class="notice danger" data-history-restore-error>${icon("shield-alert")}<span><strong>这一版现在不能安全回滚。</strong>${escapeHtml(restore.error)}</span></div>`;
    }
    const counts = diffCounts(restore.diff);
    const restart = Array.isArray(restore.restartPending) ? restore.restartPending : [];
    const retained = Array.isArray(restore.retainedSecrets) ? restore.retainedSecrets : [];
    const stale = !restore.ok || restore.currentRevision !== historyCurrentRevision();
    return `
      <div class="history-restore" data-history-restore data-restore-ok="${restore.ok ? "true" : "false"}">
        <div class="stat-row">
          <span class="badge warning">${icon("history")}回滚目标：历史版本 r${escapeHtml(restore.revision)} 的内容</span>
          <span class="badge info" data-history-restore-current>当前生效：r${escapeHtml(restore.currentRevision)}</span>
          <span class="badge success">${icon("plus")}新增 ${counts.added}</span>
          <span class="badge warning">${icon("pencil")}修改 ${counts.modified}</span>
          <span class="badge danger">${icon("minus")}移除 ${counts.removed}</span>
          ${restart.length ? `<span class="badge warning">${icon("rotate-ccw")}回滚后需重启 ${restart.length}</span>` : ""}
        </div>
        <div class="notice danger">
          ${icon("key-round")}
          <span><strong>密钥保持当前值。</strong>回滚只还原非密钥配置：它不会写入、清空或替换任何密钥，历史快照里也从来没有密钥值。密钥槽位的变化只会以"已配置 / 未配置"的状态显示。</span>
        </div>
        ${retained.length ? `
          <div class="notice" data-retained-secrets>
            ${icon("info")}
            <span>有 ${retained.length} 条密钥仍在密钥表里、但回滚后的配置不再引用它们（典型场景：回滚到供应商更少的旧版本）。它们<strong>没有被删除</strong>，只是运行时不再使用：${retained.map(item => `<code>${escapeHtml(item)}</code>`).join("、")}</span>
          </div>` : ""}
        ${stale ? `
          <div class="notice danger" data-history-restore-stale>
            ${icon("triangle-alert")}
            <span><strong>这份预览已经过期。</strong>预览算的是 r${escapeHtml(restore.currentRevision)} 之后的配置，而服务端现在的生效版本已经不是它了。请重新点一次回滚预览。</span>
          </div>` : ""}
        ${restore.summary ? `<p class="field-hint" data-history-restore-summary>${icon("info")}服务端摘要：${escapeHtml(restore.summary)}</p>` : ""}
        ${restart.length ? `<p class="field-hint">回滚后需要重启才生效：${restart.map(item => `<code>${escapeHtml(item)}</code>`).join("、")}</p>` : ""}
        ${diffEntriesMarkup(restore.diff, "回滚到这一版不会改变任何非密钥字段。")}
        <div class="toolbar-row">
          <button class="danger-button" type="button" data-action="history-restore-confirm"${state.configHistory.restoring || stale ? " disabled" : ""} aria-busy="${state.configHistory.restoring ? "true" : "false"}">
            ${state.configHistory.restoring ? `<span class="spinner spinner-compact"></span><span>回滚中…</span>` : `${icon("history")}确认把当前配置回滚为 r${escapeHtml(restore.revision)} 的内容`}
          </button>
          <button class="secondary-button" type="button" data-action="history-restore-cancel"${state.configHistory.restoring ? " disabled" : ""}>${icon("x")}取消</button>
          <button class="secondary-button" type="button" data-action="history-restore-retry">${icon("refresh-cw")}重新预览</button>
        </div>
        <p class="field-hint">回滚会写入一个<strong>新的修订</strong>（版本号继续往前加），历史里会出现一条 restore 记录；它<strong>不会</strong>把版本号退回 r${escapeHtml(restore.revision)}。</p>
      </div>`;
  }

  // 历史分页的页大小：服务端硬上限 100，越界一律 400 且**不夹取**，所以这里
  // 永远只发合法值。
  const HISTORY_PAGE_SIZE_OPTIONS = [10, 20, 50];
  const HISTORY_PAGE_LIMIT_MAX = 100;

  function historyCurrentRevision() {
    return Number(state.document?.revision || 0);
  }

  function historyCanPage() {
    return state.configHistory.page?.has_more
      && state.configHistory.page?.next_before != null;
  }

  function renderHistory() {
    const history = state.configHistory;
    const entries = history.entries || [];
    const page = history.page || null;
    const current = historyCurrentRevision();
    return `
      ${pageHead("配置变更历史", "每一版运行时配置的元数据、脱敏快照与逐字段差异。密钥在这里永远只有状态，没有值。")}
      <div class="section-stack">
        <section class="settings-section">
          ${sectionHead(
            "版本列表",
            "历史列表只保留「改了哪些字段」的元数据；展开某一版才会拉取它的脱敏快照。",
            `<button class="secondary-button" type="button" data-action="history-refresh"${history.loading ? " disabled" : ""}>
              ${history.loading ? `<span class="spinner spinner-compact"></span><span>加载中…</span>` : `${icon("refresh-cw")}刷新`}
            </button>`,
          )}
          ${historyConflictMarkup()}
          <div class="stat-row">
            <span class="badge info">${icon("git-branch")}本页 ${entries.length} 个版本</span>
            <span class="badge">当前生效 r${escapeHtml(current)}</span>
            ${page?.before != null ? `<span class="badge warning" data-history-before>翻到 r${escapeHtml(page.before)} 之前（更早）</span>` : `<span class="badge">最新一页</span>`}
            ${page?.has_more ? `<span class="badge info">还有更早的版本</span>` : `<span class="badge">已经是最早一页</span>`}
            ${(state.document?.restart_pending || []).length ? `<span class="badge warning">${icon("rotate-ccw")}待重启 ${escapeHtml((state.document.restart_pending || []).length)}</span>` : ""}
          </div>
          <div class="toolbar-row history-paging">
            <label class="field">
              <span class="field-label">每页条数</span>
              <select data-history-limit aria-label="历史每页条数">
                ${HISTORY_PAGE_SIZE_OPTIONS.map(size => `<option value="${size}"${Number(history.limit) === size ? " selected" : ""}>${size}</option>`).join("")}
              </select>
            </label>
            <button class="secondary-button" type="button" data-action="history-older"${historyCanPage() && !history.loading ? "" : " disabled"}>
              ${icon("chevron-left")}更早的一页
            </button>
            <button class="secondary-button" type="button" data-action="history-newest"${history.before != null && !history.loading ? "" : " disabled"}>
              ${icon("chevron-right")}回到最新
            </button>
            <span class="field-hint" data-history-page-note>${
              page
                ? `每页 ${escapeHtml(page.limit ?? history.limit)} 条 · 本页返回 ${escapeHtml(page.count ?? entries.length)} 条${page.has_more ? ` · 下一页起点 r${escapeHtml(page.next_before)}` : ""}`
                : "分页参数由服务端返回：翻页用它的 next_before，越界的页大小会被 400 拒绝而不是悄悄夹到上限。"
            }</span>
          </div>
          ${history.loading && !entries.length
            ? `<div class="loading-state inline"><span class="spinner"></span><p>正在加载历史</p></div>`
            // 这一段**不再**套客户端分页：翻页已经是服务端的事（limit/before/next_before）。
            // 两层分页叠在一起会把"最新一页"切成最旧的 8 条显示出来，看着像回退了。
            : entries.length
              ? entries.map(historyRow).join("")
              : `<p class="field-hint">${escapeHtml(history.before != null ? "这一页没有历史版本。" : "还没有配置变更记录。")}</p>`}
        </section>
        <section class="settings-section">
          ${sectionHead("版本对比", "任选两版，查看脱敏后的逐字段差异。")}
          ${entries.length ? historyCompareMarkup() : `<p class="field-hint">至少有两个版本才能对比。</p>`}
        </section>
        <section class="settings-section">
          ${sectionHead("回滚", "先看**当前配置 → 真正的回滚候选**的差异，再确认。回滚永远不动密钥。")}
          ${historyRestoreMarkup()}
          ${!history.restore ? `<p class="field-hint">在上面的版本列表里点历史版本右侧的回滚按钮，这里会先显示它将要应用的差异。</p>` : ""}
        </section>
      </div>`;
  }

  async function loadConfigHistory({ before = null } = {}) {
    // 角色矩阵：全局配置历史是**全局**资源，群管理员的导航里根本没有这一页，
    // 这里再加一道断言：没有全局权限就不发这个请求。
    if (!state.session?.can_manage_global) return;
    const history = state.configHistory;
    if (history.loading) return;
    const limit = Math.max(1, Math.min(HISTORY_PAGE_LIMIT_MAX, Number(history.limit) || 20));
    history.loading = true;
    renderContent();
    try {
      const params = new URLSearchParams({ limit: String(limit) });
      if (before != null) params.set("before", String(before));
      const result = await apiFetch(`/api/v1/settings/history?${params.toString()}`);
      history.before = before;
      history.entries = Array.isArray(result.history) ? result.history : [];
      history.page = result.page && typeof result.page === "object" ? result.page : null;
      history.conflict = false;
      const current = historyCurrentRevision();
      const visible = new Set(history.entries.map(entry => Number(entry.revision)));
      if (history.against == null || !visible.has(history.against)) {
        history.against = current;
      }
      if (history.compareRevision == null || history.compareRevision === history.against) {
        history.compareRevision = history.entries
          .map(entry => Number(entry.revision))
          .filter(revision => revision !== current)
          .sort((left, right) => right - left)[0] ?? current;
      }
      if (history.expanded != null && !visible.has(history.expanded)) {
        history.expanded = null;
        history.detail = null;
      }
    } catch (error) {
      showToast(error.message || "历史加载失败", "error", 6000);
    } finally {
      history.loading = false;
      renderContent();
    }
  }

  function loadOlderHistoryPage() {
    const history = state.configHistory;
    const nextBefore = history.page?.next_before;
    if (nextBefore == null) return;
    loadConfigHistory({ before: Number(nextBefore) });
  }

  function loadNewestHistoryPage() {
    loadConfigHistory({ before: null });
  }

  async function toggleHistoryRevision(revision) {
    const history = state.configHistory;
    if (history.expanded === revision) {
      history.expanded = null;
      history.detail = null;
      renderContent();
      return;
    }
    history.expanded = revision;
    history.detailLoading = true;
    renderContent();
    try {
      history.detail = await apiFetch(`/api/v1/settings/history/${encodeURIComponent(revision)}`);
    } catch (error) {
      history.detail = null;
      showToast(error.message || "该版本加载失败", "error", 6000);
    } finally {
      history.detailLoading = false;
      renderContent();
    }
  }

  async function compareHistoryRevisions() {
    const history = state.configHistory;
    const against = Number(history.against);
    const revision = Number(history.compareRevision);
    if (!against || !revision || against === revision) {
      showToast("请选择两个不同的版本", "warning", 4000);
      return;
    }
    history.compareLoading = true;
    renderContent();
    try {
      history.compare = await apiFetch(
        `/api/v1/settings/history/${encodeURIComponent(revision)}/diff?against=${encodeURIComponent(against)}`,
      );
    } catch (error) {
      history.compare = null;
      showToast(error.message || "版本对比失败", "error", 6000);
    } finally {
      history.compareLoading = false;
      renderContent();
    }
  }

  // 回滚预览走的是**当前配置 → 真正的回滚候选**，不是两个历史版本之间差异。
  // 这份预览由服务端和真正的 restore 共用同一条合并逻辑，所以"预览说会这样、
  // 点下去却不是这样"不会发生。
  async function prepareHistoryRestore(revision) {
    const history = state.configHistory;
    if (!state.session?.can_manage_global) return;
    if (hasUnsavedChanges()) {
      showToast("有未保存的改动时不能回滚：请先保存或重新加载，避免草稿被回滚结果覆盖", "warning", 7000);
      return;
    }
    const current = historyCurrentRevision();
    if (revision === current) {
      showToast("这一版已经是当前生效的配置", "warning", 4000);
      return;
    }
    history.restore = { revision, currentRevision: current, loading: true, ok: false, diff: [], error: "" };
    renderContent();
    try {
      const preview = await apiFetch(`/api/v1/settings/history/${encodeURIComponent(revision)}/restore-preview`);
      // 预览期间可能有人又保存了一次；这时「预览基于的当前版本」已经对不上了，
      // 必须重算，不能拿一份基于旧基线的差异去确认。
      const live = historyCurrentRevision();
      history.restore = {
        revision: Number(preview.revision ?? revision),
        currentRevision: Number(preview.current_revision ?? live),
        loading: false,
        ok: Number(preview.current_revision ?? live) === live,
        diff: Array.isArray(preview.diff) ? preview.diff : [],
        summary: String(preview.summary || ""),
        snapshotBytes: preview.snapshot_bytes,
        restartPending: Array.isArray(preview.restart_pending) ? preview.restart_pending : [],
        retainedSecrets: Array.isArray(preview.retained_secrets) ? preview.retained_secrets : [],
        error: "",
      };
    } catch (error) {
      const code = replayErrorCode(error.body);
      history.restore = {
        revision,
        currentRevision: current,
        loading: false,
        ok: false,
        diff: [],
        // history_revision_not_restorable 的 message 只含**路径名**，可以逐字给管理员看。
        error: `${error.message || "无法计算回滚差异。"}${code ? `（${code}）` : ""}`,
      };
    }
    renderContent();
    content.querySelector("[data-history-restore]")?.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  async function confirmHistoryRestore() {
    const history = state.configHistory;
    const restore = history.restore;
    if (!restore || history.restoring) return;
    if (!restore.ok || restore.currentRevision !== historyCurrentRevision()) {
      showToast("预览已经过期（配置在这期间变过）：重新点一次回滚预览，确认过差异再执行。", "warning", 7000);
      history.restore = null;
      renderContent();
      return;
    }
    // 预览之后管理员可能改了配置：那份差异已经对不上现在的草稿了，确认前必须
    // 再问一次，否则回滚会把他预览之后写的字悄悄抹掉。
    if (hasUnsavedChanges()) {
      history.restore = null;
      renderContent();
      showToast("预览之后你又改了配置：差异已经对不上，草稿原样保留。请重新点一次回滚预览。", "warning", 8000);
      return;
    }
    const counts = diffCounts(restore.diff);
    const total = counts.added + counts.modified + counts.removed;
    if (!await askConfirmation(
      "确认回滚配置",
      `将把**当前生效的 r${restore.currentRevision}** 回滚成**历史版本 r${restore.revision} 的内容**`
      + (total ? `（新增 ${counts.added}、修改 ${counts.modified}、移除 ${counts.removed}）。` : "（非密钥字段没有差异）。")
      + "\n密钥保持当前值，不会被写入、清空或替换。"
      + "\n回滚会保存为一个**新的修订**（版本号继续往前加），不会把版本号退回 r"
      + `${restore.revision}。`
      + "\n确认回滚？",
    )) return;
    history.restoring = true;
    renderContent();
    try {
      const document_ = await apiFetch(`/api/v1/settings/history/${encodeURIComponent(restore.revision)}/restore`, {
        method: "POST",
        body: JSON.stringify({ revision: Number(state.document?.revision || 0) }),
      });
      history.conflict = false;
      history.restore = null;
      applySettingsDocument(document_);
      state.configHistory.expanded = null;
      state.configHistory.detail = null;
      state.configHistory.compare = null;
      state.configHistory.against = historyCurrentRevision();
      // 成功文案必须分清**目标历史版本**与**新生成的修订**：新 r 不是回滚目标。
      const sourceRevision = document_?.restore?.source_revision ?? restore.revision;
      const retained = Array.isArray(document_?.restore?.retained_secrets) ? document_.restore.retained_secrets : [];
      showToast(
        `已把配置内容回滚到历史版本 r${sourceRevision}，并保存为新修订 r${document_.revision}`
        + (retained.length ? `；${retained.length} 条密钥仍保留在密钥表里（运行时不再使用）` : ""),
        "success",
        8000,
      );
      await loadConfigHistory();
      return;
    } catch (error) {
      const code = replayErrorCode(error.body);
      if (error instanceof ApiError && error.status === 409) {
        // 乐观锁失败：**草稿一个字都不动**。这里只作废那份基于旧版本的预览。
        history.conflict = true;
        history.restore = null;
        showToast("配置已被其他会话更新，回滚没有执行。页面上的草稿原样保留。", "error", 9000);
      } else {
        showToast(error.message || code || "回滚失败", "error", 7000);
      }
    } finally {
      history.restoring = false;
      renderContent();
    }
  }

  function promptDirty(key) {
    return getPath(state.config, `prompts.${key}`) !== getPath(state.baseline, `prompts.${key}`);
  }

  function renderPrompts() {
    const key = state.promptKey;
    const value = state.config.prompts[key] || "";
    return `
      ${pageHead("系统 Prompts", "编辑各模型任务使用的系统提示词；空值会使用项目内置默认。")}
      <div class="prompt-layout">
        <nav class="prompt-list" aria-label="Prompt 列表">
          ${Object.entries(PROMPT_META).map(([promptKey, label]) => `
            <button class="prompt-button${key === promptKey ? " active" : ""}" type="button" data-action="select-prompt" data-prompt-key="${promptKey}">
              <span>${escapeHtml(label)}</span>${promptDirty(promptKey) ? `<span class="prompt-dot" aria-label="已修改"></span>` : ""}
            </button>`).join("")}
        </nav>
        <section class="prompt-editor">
          <div class="prompt-editor-head">
            <div><h3>${escapeHtml(PROMPT_META[key])}</h3><span class="prompt-meta" id="prompt-meta">${value.length.toLocaleString("zh-CN")} 字符</span></div>
            <button class="mini-icon-button danger" type="button" data-action="clear-prompt" data-prompt-key="${key}" aria-label="清空并使用内置默认" title="清空并使用内置默认">${icon("eraser")}</button>
          </div>
          <textarea data-prompt-input="${key}" spellcheck="false">${escapeHtml(value)}</textarea>
        </section>
      </div>`;
  }

  function normalizeGroupSettings(settings = {}) {
    return {
      av_enabled: settings.av_enabled ?? false,
      mute_all_replies: settings.mute_all_replies ?? false,
      at_reply_mode: settings.at_reply_mode ?? false,
      join_verification_enabled: settings.join_verification_enabled == null ? null : Boolean(settings.join_verification_enabled),
      join_verification_provider: ["turnstile", "hcaptcha", "turnstile_hcaptcha"].includes(settings.join_verification_provider)
        ? settings.join_verification_provider
        : null,
      welcome_message: String(settings.welcome_message || ""),
      welcome_disable_link_preview: settings.welcome_disable_link_preview !== false,
      welcome_buttons: normalizeTemplateButtons(settings.welcome_buttons),
      default_permissions: normalizeDefaultPermissions(settings.default_permissions),
      patrol_enabled: settings.patrol_enabled == null ? null : Boolean(settings.patrol_enabled),
      raid_guard_enabled: settings.raid_guard_enabled == null ? null : Boolean(settings.raid_guard_enabled),
      raid_guard_pin_message: settings.raid_guard_pin_message == null ? null : Boolean(settings.raid_guard_pin_message),
      ...Object.fromEntries(RAID_GUARD_GROUP_INT_FIELDS.map(({ key }) => [
        key,
        settings[key] == null ? null : Number(settings[key]),
      ])),
      call_admin_enabled: settings.call_admin_enabled == null ? null : Boolean(settings.call_admin_enabled),
      call_admin_pin_message: settings.call_admin_pin_message == null ? null : Boolean(settings.call_admin_pin_message),
      call_admin_targets: Array.isArray(settings.call_admin_targets)
        ? settings.call_admin_targets.map(Number).filter(value => Number.isFinite(value) && value > 0).sort((a, b) => a - b)
        : [],
      vote_ban_enabled: settings.vote_ban_enabled == null ? null : Boolean(settings.vote_ban_enabled),
      vote_ban_pin_message: settings.vote_ban_pin_message == null ? null : Boolean(settings.vote_ban_pin_message),
      ...Object.fromEntries(VOTE_BAN_GROUP_INT_FIELDS.map(({ key }) => [
        key,
        settings[key] == null ? null : Number(settings[key]),
      ])),
      tts_mode: ["off", "on", "always"].includes(settings.tts_mode) ? settings.tts_mode : "off",
      proactive_enabled: settings.proactive_enabled == null ? null : Boolean(settings.proactive_enabled),
      proactive_task_brief: String(settings.proactive_task_brief || ""),
      mimic_target_user_id: Number(settings.mimic_target_user_id || 0),
      mimic_target_user_name: String(settings.mimic_target_user_name || ""),
      mimic_profile_text: String(settings.mimic_profile_text || ""),
      mimic_sample_count: Number(settings.mimic_sample_count || 0),
      mimic_distilled_at_count: Number(settings.mimic_distilled_at_count || 0),
      api_model_query_enabled: Boolean(settings.api_model_query_enabled),
      api_model_query_base_url: String(settings.api_model_query_base_url || ""),
      api_model_query_http_timeout_sec: Number(settings.api_model_query_http_timeout_sec ?? 15),
      api_model_query_check_timeout_sec: Number(settings.api_model_query_check_timeout_sec ?? 45),
      api_model_query_api_key_configured: Boolean(settings.api_model_query_api_key_configured),
    };
  }

  function normalizeDefaultPermissions(config) {
    if (!config || typeof config !== "object" || !config.base || typeof config.base !== "object") return null;
    const fields = state.permissionFields?.length ? state.permissionFields : PERMISSION_FIELD_FALLBACK;
    const base = Object.fromEntries(fields.map(({ key }) => [key, config.base[key] === true]));
    const windows = Array.isArray(config.windows) ? config.windows.map((window, index) => ({
      id: String(window?.id || `window_${index + 1}`),
      name: String(window?.name || `时段 ${index + 1}`),
      enabled: window?.enabled !== false,
      start: String(window?.start || "23:00"),
      end: String(window?.end || "07:00"),
      days: Array.isArray(window?.days)
        ? [...new Set(window.days.map(Number).filter(day => Number.isInteger(day) && day >= 0 && day <= 6))].sort()
        : [0, 1, 2, 3, 4, 5, 6],
      priority: Number.isInteger(Number(window?.priority)) ? Number(window.priority) : 0,
      overrides: Object.fromEntries(Object.entries(window?.overrides || {})
        .filter(([key, value]) => fields.some(field => field.key === key) && typeof value === "boolean")),
    })) : [];
    return {
      version: 1,
      timezone: String(config.timezone || "Asia/Shanghai"),
      schedule_enabled: config.schedule_enabled === true,
      base,
      windows,
    };
  }

  function normalizeTemplateButtons(buttons) {
    if (!Array.isArray(buttons)) return [];
    return buttons.slice(0, 12).map((button, index) => {
      const style = String(button?.style || "").trim().toLowerCase();
      return {
        text: String(button?.text || ""),
        action: ["url", "copy", "share", "dismiss"].includes(button?.action)
          ? button.action
          : "url",
        value: String(button?.value || ""),
        row: Number.isInteger(Number(button?.row)) ? Number(button.row) : index,
        ...(TEMPLATE_BUTTON_STYLES.includes(style) ? { style } : {}),
      };
    }).filter(button => button.text);
  }

  function escapeTemplateButtonPart(value) {
    return String(value ?? "")
      .replaceAll("\\", "\\\\")
      .replaceAll("|", "\\|")
      .replaceAll("\r", "\\r")
      .replaceAll("\n", "\\n");
  }

  function splitTemplateButtonLine(line) {
    const parts = [""];
    const source = String(line ?? "");
    for (let index = 0; index < source.length; index += 1) {
      const char = source[index];
      const next = source[index + 1];
      if (char === "\\" && ["\\", "|", "n", "r"].includes(next)) {
        parts[parts.length - 1] += next === "n" ? "\n" : next === "r" ? "\r" : next;
        index += 1;
      } else if (char === "|") {
        parts.push("");
      } else {
        parts[parts.length - 1] += char;
      }
    }
    return parts;
  }

  function templateButtonsToText(buttons) {
    return normalizeTemplateButtons(buttons).map(button => {
      const parts = [button.text, button.action, button.value, Number(button.row) + 1];
      if (button.style) parts.push(button.style);
      return parts.map(escapeTemplateButtonPart).join(" | ");
    }).join("\n");
  }

  function parseTemplateButtonsText(raw) {
    const lines = String(raw || "").split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    if (lines.length > 12) throw new Error("内联按钮最多 12 个");
    return lines.map((line, index) => {
      const parts = splitTemplateButtonLine(line);
      if (parts.length > 5) {
        throw new Error(`第 ${index + 1} 行包含未转义的“|”，请写成 \\|`);
      }
      while (parts.length < 5) parts.push("");
      const normalizedParts = parts.map(part => part.trim());
      const text = normalizedParts[0] || "";
      const action = (normalizedParts[1] || "url").toLowerCase();
      const value = normalizedParts[2] || "";
      const rowValue = normalizedParts[3] || String(index + 1);
      const style = (normalizedParts[4] || "").toLowerCase();
      const row = Number(rowValue) - 1;
      if (!text) throw new Error(`第 ${index + 1} 行缺少按钮名称`);
      if (text.length > 64) throw new Error(`第 ${index + 1} 行按钮名称不能超过 64 字`);
      if (!["url", "copy", "share", "dismiss"].includes(action)) {
        throw new Error(`第 ${index + 1} 行操作无效，请使用 url/copy/share/dismiss`);
      }
      if (action === "url" && !/^(https?:\/\/|tg:\/\/)/i.test(value)) {
        throw new Error(`第 ${index + 1} 行需要有效的 http(s):// 或 tg:// 链接`);
      }
      if (["copy", "share"].includes(action) && !value) {
        throw new Error(`第 ${index + 1} 行需要填写复制/分享内容`);
      }
      if (["copy", "share"].includes(action) && value.length > 256) {
        throw new Error(`第 ${index + 1} 行复制/分享内容不能超过 256 字`);
      }
      if (action === "url" && value.length > 2048) {
        throw new Error(`第 ${index + 1} 行链接不能超过 2048 字`);
      }
      if (!Number.isInteger(row) || row < 0 || row > 7) {
        throw new Error(`第 ${index + 1} 行的行号需为 1-8`);
      }
      if (style && !TEMPLATE_BUTTON_STYLES.includes(style)) {
        throw new Error(`第 ${index + 1} 行颜色无效，请使用 primary/success/danger`);
      }
      return {
        text,
        action,
        value: action === "dismiss" ? "" : value,
        row,
        ...(style ? { style } : {}),
      };
    });
  }

  function templateButtonStyleLegend() {
    return `<span class="template-button-style-legend" aria-label="按钮颜色选项">
      <span><i class="template-button-style-swatch primary"></i><code>primary</code> 蓝</span>
      <span><i class="template-button-style-swatch success"></i><code>success</code> 绿</span>
      <span><i class="template-button-style-swatch danger"></i><code>danger</code> 红</span>
    </span>`;
  }

  function groupTemplateButtonsText(group) {
    const draft = state.groupTemplateButtonDrafts.get(String(group.id));
    return draft ? draft.raw : templateButtonsToText(group.settings.welcome_buttons);
  }

  function groupToggle(group, key, label, hint = "", disabled = false) {
    const id = `group-${group.id}-${key}`;
    return `
      <div class="toggle-field">
        <div class="toggle-copy"><strong>${escapeHtml(label)}</strong>${hint ? `<small>${escapeHtml(hint)}</small>` : ""}</div>
        <label class="toggle" for="${attr(id)}">
          <input id="${attr(id)}" type="checkbox" data-group-id="${attr(group.id)}" data-group-key="${key}" data-kind="boolean" aria-label="${attr(label)}"${group.settings[key] ? " checked" : ""}${disabled ? " disabled" : ""}>
          <span class="toggle-track"></span>
        </label>
      </div>`;
  }

  function groupApiModelQuerySecretChange(groupId) {
    return state.groupApiModelQuerySecretChanges.get(String(groupId));
  }

  function groupApiModelQuerySecretConfigured(group) {
    const change = groupApiModelQuerySecretChange(group.id);
    if (change?.action === "replace") return Boolean(String(change.value || "").trim());
    if (change?.action === "clear") return false;
    return Boolean(group.settings.api_model_query_api_key_configured);
  }

  function renderGroupApiModelQuerySecretField(group, saving) {
    const change = groupApiModelQuerySecretChange(group.id);
    const configured = Boolean(group.settings.api_model_query_api_key_configured);
    const replacing = change?.action === "replace";
    const clearing = change?.action === "clear";
    const status = clearing
      ? `<span class="badge warning">等待清除</span>`
      : replacing
        ? `<span class="badge warning">等待替换</span>`
        : configured
          ? `<span class="badge success">已配置</span>`
          : `<span class="badge">未配置</span>`;
    const action = clearing || replacing
      ? `<button class="mini-icon-button" type="button" data-action="group-api-model-query-secret-undo" data-group-id="${attr(group.id)}" aria-label="${clearing ? "撤销清除" : "撤销替换"}" title="${clearing ? "撤销清除" : "撤销替换"}"${saving ? " disabled" : ""}>${icon("undo-2")}</button>`
      : configured
        ? `<button class="mini-icon-button danger" type="button" data-action="group-api-model-query-secret-clear" data-group-id="${attr(group.id)}" aria-label="清除 API Key" title="清除 API Key"${saving ? " disabled" : ""}>${icon("trash-2")}</button>`
        : "";
    const placeholder = replacing
      ? "已暂存新密钥；再次输入可替换待保存值"
      : configured
        ? "留空保留当前密钥"
        : "输入 API Key";
    return `
      <div class="field full group-api-model-query-secret">
        <label class="field-label" for="group-${attr(group.id)}-api-model-query-api-key">API Key</label>
        <div class="secret-control">
          <input id="group-${attr(group.id)}-api-model-query-api-key" type="password" data-group-api-model-query-secret-input data-group-id="${attr(group.id)}" value="" maxlength="1024" placeholder="${attr(placeholder)}" autocomplete="new-password"${clearing || saving ? " disabled" : ""}>
          <div class="secret-status">${status}${action}</div>
        </div>
        <span class="field-hint">密钥不会回显；留空会保留已保存值，清除需与关闭此功能一并保存。</span>
      </div>`;
  }

  function renderCallAdminTargets(group, saving) {
    const key = String(group.id);
    const admins = state.groupTelegramAdmins.get(key);
    if (!admins) {
      return `<button class="secondary-button" type="button" data-action="load-call-admin-targets" data-group-id="${attr(group.id)}"${saving ? " disabled" : ""}>${icon("users")}<span>加载管理员列表</span></button>
        <span class="field-hint">默认全部勾选；仅勾选部分时只 @ 勾选的管理员</span>`;
    }
    if (admins.loading) {
      return `<div class="loading-inline"><span class="spinner spinner-small"></span><span>正在加载</span></div>`;
    }
    if (admins.error) {
      return `<div class="notice danger">${icon("circle-x")}<span>${escapeHtml(admins.error)}</span></div>`;
    }
    if (!admins.items.length) {
      return `<span class="field-hint">未找到可选管理员（bot 需要在群内且能读取管理员列表）</span>`;
    }
    const selected = new Set(group.settings.call_admin_targets || []);
    const selectAll = selected.size === 0;
    return `
      <div class="call-admin-target-list">
        ${admins.items.map(admin => `
        <label class="compact-check">
          <input type="checkbox" data-call-admin-target="${attr(admin.user_id)}" data-group-id="${attr(group.id)}"${selectAll || selected.has(Number(admin.user_id)) ? " checked" : ""}${saving ? " disabled" : ""}>
          <span>${escapeHtml(admin.display_name || admin.user_id)}</span>
        </label>`).join("")}
      </div>
      <span class="field-hint">全部勾选＝@所有管理员（默认，新管理员自动包含）；仅勾选部分时只 @ 勾选的人</span>`;
  }

  function renderGroupPermissions(group, saving) {
    const loading = state.groupPermissionLoads.has(String(group.id));
    const config = group.settings.default_permissions;
    if (!config) {
      return `
        <div class="field group-permissions-editor">
          <div class="field-label-row"><span class="field-label">群默认用户权限与定时模式</span></div>
          <p class="field-hint">尚未接管此群的默认权限。先读取 Telegram 当前值，再按群配置完整权限和夜间时段。</p>
          <button class="secondary-button" type="button" data-action="load-group-permissions" data-group-id="${attr(group.id)}"${saving || loading ? " disabled" : ""}>${loading ? `<span class="spinner spinner-small"></span><span>正在读取</span>` : `${icon("download")}<span>读取当前群权限</span>`}</button>
        </div>`;
    }
    const weekdays = ["一", "二", "三", "四", "五", "六", "日"];
    const fields = state.permissionFields?.length ? state.permissionFields : PERMISSION_FIELD_FALLBACK;
    return `
      <div class="field group-permissions-editor">
        <div class="permission-editor-head">
          <div>
            <span class="field-label">群默认用户权限与定时模式</span>
            <span class="field-hint">基础权限始终完整保存；活动时段只覆盖选中的权限，结束后自动恢复基础权限。</span>
          </div>
          <button class="secondary-button" type="button" data-action="add-permission-window" data-group-id="${attr(group.id)}"${saving ? " disabled" : ""}>${icon("plus")}<span>新增时段</span></button>
        </div>
        <div class="permission-config-row">
          <label class="compact-check permission-schedule-toggle">
            <input type="checkbox" data-permission-control data-permission-config="schedule_enabled" data-group-id="${attr(group.id)}"${config.schedule_enabled ? " checked" : ""}${saving ? " disabled" : ""}>
            <span>启用定时权限</span>
          </label>
          <label class="permission-timezone">时区
            <input type="text" data-permission-control data-permission-config="timezone" data-group-id="${attr(group.id)}" value="${attr(config.timezone)}" maxlength="64" required${saving ? " disabled" : ""}>
          </label>
        </div>
        <h5>基础默认权限</h5>
        <div class="permission-field-grid">
          ${fields.map(({ key, label }) => `
            <label class="compact-check">
              <input type="checkbox" data-permission-control data-permission-base="${attr(key)}" data-group-id="${attr(group.id)}"${config.base[key] ? " checked" : ""}${saving ? " disabled" : ""}>
              <span>${escapeHtml(label)}</span>
            </label>`).join("")}
        </div>
        <div class="permission-window-list">
          ${config.windows.map((window, index) => `
            <section class="permission-window-card">
              <div class="permission-window-head">
                <strong>${escapeHtml(window.name || `时段 ${index + 1}`)}</strong>
                <button class="mini-icon-button danger" type="button" data-action="remove-permission-window" data-group-id="${attr(group.id)}" data-window-index="${index}" aria-label="删除时段" title="删除时段"${saving ? " disabled" : ""}>${icon("trash-2")}</button>
              </div>
              <div class="permission-window-fields">
                <label>名称<input type="text" data-permission-control data-permission-window-field="name" data-window-index="${index}" data-group-id="${attr(group.id)}" value="${attr(window.name)}" maxlength="80" required${saving ? " disabled" : ""}></label>
                <label>开始<input type="time" data-permission-control data-permission-window-field="start" data-window-index="${index}" data-group-id="${attr(group.id)}" value="${attr(window.start)}" required${saving ? " disabled" : ""}></label>
                <label>结束<input type="time" data-permission-control data-permission-window-field="end" data-window-index="${index}" data-group-id="${attr(group.id)}" value="${attr(window.end)}" required${saving ? " disabled" : ""}></label>
                <label>优先级<input type="number" data-permission-control data-permission-window-field="priority" data-window-kind="number" data-window-index="${index}" data-group-id="${attr(group.id)}" value="${attr(window.priority)}" min="-1000" max="1000" step="1" required${saving ? " disabled" : ""}></label>
                <label class="compact-check"><input type="checkbox" data-permission-control data-permission-window-field="enabled" data-window-kind="boolean" data-window-index="${index}" data-group-id="${attr(group.id)}"${window.enabled ? " checked" : ""}${saving ? " disabled" : ""}><span>启用时段</span></label>
              </div>
              <div class="permission-weekdays" aria-label="生效星期">
                ${weekdays.map((label, day) => `<label class="compact-check"><input type="checkbox" data-permission-control data-permission-window-day="${day}" data-window-index="${index}" data-group-id="${attr(group.id)}"${window.days.includes(day) ? " checked" : ""}${saving ? " disabled" : ""}><span>周${label}</span></label>`).join("")}
              </div>
              <div class="permission-override-grid">
                ${fields.map(({ key, label }) => {
                  const value = Object.hasOwn(window.overrides, key) ? String(window.overrides[key]) : "";
                  return `<label>${escapeHtml(label)}<select data-permission-control data-permission-window-override="${attr(key)}" data-window-index="${index}" data-group-id="${attr(group.id)}"${saving ? " disabled" : ""}><option value=""${value === "" ? " selected" : ""}>不覆盖</option><option value="true"${value === "true" ? " selected" : ""}>允许</option><option value="false"${value === "false" ? " selected" : ""}>禁止</option></select></label>`;
                }).join("")}
              </div>
            </section>`).join("") || `<p class="field-hint">暂无定时时段；可新增“23:00–07:00 禁止发送图片”等夜间模式。</p>`}
        </div>
      </div>`;
  }

  function groupCardIsOpen(group) {
    const key = String(group.id);
    return state.groupCardOpen.has(key) ? state.groupCardOpen.get(key) : state.groups.length === 1;
  }

  function groupSectionStateKey(groupId, sectionKey) {
    return `${String(groupId)}:${String(sectionKey)}`;
  }

  function groupSectionIsOpen(group, sectionKey, defaultOpen = false) {
    const key = groupSectionStateKey(group.id, sectionKey);
    return state.groupSectionOpen.has(key) ? state.groupSectionOpen.get(key) : defaultOpen;
  }

  function groupSectionSettingKeys(sectionKey) {
    return GROUP_SECTION_SETTING_KEYS[sectionKey] || [];
  }

  function groupSectionDirty(group, settingKeys = []) {
    const baseline = state.groupBaselines.get(String(group.id)) || {};
    if (settingKeys.includes("welcome_buttons") && state.groupTemplateButtonDrafts.get(String(group.id))?.error) {
      return true;
    }
    if (
      settingKeys.includes("api_model_query_api_key_configured")
      && state.groupApiModelQuerySecretChanges.has(String(group.id))
    ) return true;
    return settingKeys.some(key => !sameValue(group.settings[key], baseline[key]));
  }

  function updateRenderedGroupSectionDirty(section, dirty, label = "已修改") {
    if (!section) return;
    section.classList.toggle("dirty", dirty);
    const quickButton = section.closest("[data-group-card]")?.querySelector(
      `[data-action="jump-group-section"][data-group-section="${CSS.escape(section.dataset.groupSection || "")}"]`,
    );
    quickButton?.classList.toggle("dirty", dirty);
    if (quickButton) {
      let indicator = quickButton.querySelector("small");
      if (dirty && !indicator) {
        indicator = document.createElement("small");
        indicator.textContent = "待保存";
        quickButton.append(indicator);
      } else if (!dirty && indicator) {
        indicator.remove();
      }
      quickButton.setAttribute("aria-label", `${quickButton.querySelector("span")?.textContent || "分类"}${dirty ? "，有待保存更改" : ""}`);
    }
    const meta = section.querySelector(".group-section-meta");
    let badge = meta?.querySelector("[data-group-section-dirty]");
    if (dirty && !badge && meta) {
      badge = document.createElement("span");
      badge.className = "badge warning";
      badge.dataset.groupSectionDirty = "";
      const chevron = meta.querySelector(".group-section-chevron");
      if (chevron) chevron.before(badge);
      else meta.append(badge);
    }
    if (badge) badge.textContent = label;
    if (!dirty && badge) badge.remove();
  }

  function renderGroupSettingsSection(group, {
    key,
    title,
    description,
    iconName,
    itemLabel,
    settingKeys = [],
    content: sectionContent,
    defaultOpen = false,
    additionalDirty = false,
    dirtyLabel = "已修改",
  }) {
    const sectionId = `group-${group.id}-section-${key}`;
    const dirty = additionalDirty || groupSectionDirty(group, settingKeys);
    return `
      <details class="group-settings-section${dirty ? " dirty" : ""}" data-group-settings-section data-group-id="${attr(group.id)}" data-group-section="${attr(key)}" data-group-setting-keys="${attr(settingKeys.join(","))}"${groupSectionIsOpen(group, key, defaultOpen) ? " open" : ""}>
        <summary id="${attr(sectionId)}-summary" aria-controls="${attr(sectionId)}-body">
          <span class="group-section-icon">${icon(iconName)}</span>
          <span class="group-section-copy">
            <strong>${escapeHtml(title)}</strong>
            <small>${escapeHtml(description)}</small>
          </span>
          <span class="group-section-meta">
            <span class="group-section-count">${escapeHtml(itemLabel)}</span>
            ${dirty ? `<span class="badge warning" data-group-section-dirty>${escapeHtml(dirtyLabel)}</span>` : ""}
            ${icon("chevron-down", "group-section-chevron")}
          </span>
        </summary>
        <div id="${attr(sectionId)}-body" class="group-settings-section-body" role="region" aria-labelledby="${attr(sectionId)}-summary">
          ${sectionContent}
        </div>
      </details>`;
  }

  function renderGroupCard(group) {
    const dirty = groupDirty(group) || groupResourceDraftDirty(group.id);
    const saving = state.saving || state.groupSaving.has(String(group.id));
    const searchText = `${group.title || ""} ${group.id}`.toLowerCase();
    const cardOpen = groupCardIsOpen(group);
    const cardId = `group-${group.id}-settings`;
    return `
      <article class="item-card group-card" data-group-card data-group-id="${attr(group.id)}" data-search="${attr(searchText)}">
        <div class="item-card-header">
          <button class="group-card-toggle" id="${attr(cardId)}-toggle" type="button" data-action="toggle-group-card" data-group-id="${attr(group.id)}" aria-expanded="${cardOpen}" aria-controls="${attr(cardId)}-body">
            <span class="item-card-title">
              ${icon("users")}
              <span class="group-title-block"><strong>${escapeHtml(group.title || "未命名群组")}</strong><small>${escapeHtml(group.id)}</small></span>
            </span>
            ${icon("chevron-down", "group-card-chevron")}
          </button>
          <div class="item-actions">
            ${dirty ? `<span class="group-save-state" role="status">有未保存更改</span>` : ""}
          </div>
        </div>
        <div class="item-card-body" id="${attr(cardId)}-body" role="region" aria-labelledby="${attr(cardId)}-toggle" data-group-card-body data-group-body-rendered="${cardOpen}"${cardOpen ? "" : " hidden"}>
          ${cardOpen ? `
          <div class="group-settings-toolbar">
            <div><strong>快速定位</strong><small>直接进入需要的功能，修改后统一点击页面右上角“保存全部”</small></div>
            <div class="group-settings-toolbar-actions">
              <button class="text-button" type="button" data-action="set-group-sections" data-group-id="${attr(group.id)}" data-open="true">全部展开</button>
              <button class="text-button" type="button" data-action="set-group-sections" data-group-id="${attr(group.id)}" data-open="false">全部收起</button>
            </div>
          </div>
          <nav class="group-quick-nav" aria-label="${attr(group.title || group.id)} 功能分类">
            ${GROUP_SECTION_NAV.map(item => `
              <button class="group-quick-nav-button${groupResourceDraftDirty(group.id, item.key) || groupSectionDirty(group, groupSectionSettingKeys(item.key)) ? " dirty" : ""}" type="button" data-action="jump-group-section" data-group-id="${attr(group.id)}" data-group-section="${attr(item.key)}">
                ${icon(item.icon)}<span>${escapeHtml(item.label)}</span>${groupResourceDraftDirty(group.id, item.key) || groupSectionDirty(group, groupSectionSettingKeys(item.key)) ? `<small>待保存</small>` : ""}
              </button>`).join("")}
          </nav>
          <div class="group-settings-sections">
            ${renderGroupSettingsSection(group, {
              key: "reply-media",
              title: "回复与媒体",
              description: "回复触发、内容检索与语音输出",
              iconName: "message-circle",
              itemLabel: "4 项",
              settingKeys: ["mute_all_replies", "at_reply_mode", "av_enabled", "tts_mode"],
              defaultOpen: true,
              content: `
                <div class="group-settings-grid">
                  ${groupToggle(group, "mute_all_replies", "暂停全部回复", "临时停止本群中的所有 Bot 回复", saving)}
                  ${groupToggle(group, "at_reply_mode", "仅 @ 时回复", "只在明确提及 Bot 时响应", saving)}
                  ${groupToggle(group, "av_enabled", "允许 AV 检索", "允许本群使用 AV 内容检索能力", saving)}
                  <div class="field">
                    <label class="field-label" for="group-${attr(group.id)}-tts">语音模式</label>
                    <select id="group-${attr(group.id)}-tts" data-group-id="${attr(group.id)}" data-group-key="tts_mode" data-kind="string"${saving ? " disabled" : ""}>
                      <option value="off"${group.settings.tts_mode === "off" ? " selected" : ""}>关闭</option>
                      <option value="on"${group.settings.tts_mode === "on" ? " selected" : ""}>允许按需语音</option>
                      <option value="always"${group.settings.tts_mode === "always" ? " selected" : ""}>始终发送语音</option>
                    </select>
                  </div>
                </div>`,
            })}
            ${renderGroupSettingsSection(group, {
              key: "model-api",
              title: "模型 API",
              description: "逐群拉取可用模型并按所选模型发起连接测试",
              iconName: "server",
              itemLabel: "5 项",
              settingKeys: GROUP_SECTION_SETTING_KEYS["model-api"],
              content: `
                <div class="group-settings-grid">
                  <div class="notice info group-api-model-query-note">
                    ${icon("info")}
                    <span>兼容 OpenAI Chat Completions API；仅允许公网 HTTPS，Base URL 填写到服务根地址或包含 <code>/v1</code> 均可。</span>
                  </div>
                  ${groupToggle(group, "api_model_query_enabled", "启用模型 API", "允许本群查询模型列表并测试指定模型", saving)}
                  <div class="field full">
                    <label class="field-label" for="group-${attr(group.id)}-api-model-query-base-url">Base URL</label>
                    <input id="group-${attr(group.id)}-api-model-query-base-url" type="url" data-group-id="${attr(group.id)}" data-group-key="api_model_query_base_url" data-kind="string" value="${attr(group.settings.api_model_query_base_url)}" maxlength="1000" placeholder="https://api.example.com 或 https://api.example.com/v1"${saving ? " disabled" : ""}>
                    <span class="field-hint">开启时必填；不能包含账号、查询参数或 URL 片段。</span>
                  </div>
                  ${renderGroupApiModelQuerySecretField(group, saving)}
                  <div class="field">
                    <label class="field-label" for="group-${attr(group.id)}-api-model-query-http-timeout">单请求超时（秒）</label>
                    <input id="group-${attr(group.id)}-api-model-query-http-timeout" type="number" min="1" max="300" step="0.1" required data-group-id="${attr(group.id)}" data-group-key="api_model_query_http_timeout_sec" data-kind="number" value="${attr(group.settings.api_model_query_http_timeout_sec)}"${saving ? " disabled" : ""}>
                  </div>
                  <div class="field">
                    <label class="field-label" for="group-${attr(group.id)}-api-model-query-check-timeout">模型测试总超时（秒）</label>
                    <input id="group-${attr(group.id)}-api-model-query-check-timeout" type="number" min="1" max="600" step="0.1" required data-group-id="${attr(group.id)}" data-group-key="api_model_query_check_timeout_sec" data-kind="number" value="${attr(group.settings.api_model_query_check_timeout_sec)}"${saving ? " disabled" : ""}>
                  </div>
                </div>`,
            })}
            ${renderGroupSettingsSection(group, {
              key: "onboarding",
              title: "入群与欢迎",
              description: "新成员验证、欢迎语与内联按钮",
              iconName: "users",
              itemLabel: "5 项",
              settingKeys: [
                "join_verification_enabled", "join_verification_provider", "welcome_message",
                "welcome_disable_link_preview", "welcome_buttons",
              ],
              content: `
                <div class="group-settings-grid">
                  <div class="field">
                    <label class="field-label" for="group-${attr(group.id)}-join-verification-enabled">入群验证</label>
                    <select id="group-${attr(group.id)}-join-verification-enabled" data-group-id="${attr(group.id)}" data-group-key="join_verification_enabled" data-kind="nullable-boolean"${saving ? " disabled" : ""}>
                      <option value=""${group.settings.join_verification_enabled == null ? " selected" : ""}>继承全局默认</option>
                      <option value="true"${group.settings.join_verification_enabled === true ? " selected" : ""}>开启</option>
                      <option value="false"${group.settings.join_verification_enabled === false ? " selected" : ""}>关闭</option>
                    </select>
                    <span class="field-hint">验证服务可在右侧单独选择</span>
                  </div>
                  <div class="field">
                    <label class="field-label" for="group-${attr(group.id)}-join-verification-provider">入群验证服务</label>
                    <select id="group-${attr(group.id)}-join-verification-provider" data-group-id="${attr(group.id)}" data-group-key="join_verification_provider" data-kind="nullable-string"${saving ? " disabled" : ""}>
                      <option value=""${group.settings.join_verification_provider == null ? " selected" : ""}>继承全局默认</option>
                      <option value="turnstile"${group.settings.join_verification_provider === "turnstile" ? " selected" : ""}>Cloudflare Turnstile</option>
                      <option value="hcaptcha"${group.settings.join_verification_provider === "hcaptcha" ? " selected" : ""}>hCaptcha</option>
                      <option value="turnstile_hcaptcha"${group.settings.join_verification_provider === "turnstile_hcaptcha" ? " selected" : ""}>Turnstile + hCaptcha（双重验证）</option>
                    </select>
                  </div>
                  <div class="field welcome-message">
                    <label class="field-label" for="group-${attr(group.id)}-welcome">入群欢迎语</label>
                    <textarea id="group-${attr(group.id)}-welcome" data-group-id="${attr(group.id)}" data-group-key="welcome_message" data-kind="string" maxlength="4000" placeholder="留空不发送欢迎语；支持换行和 Markdown；{name} 为名称，{mention} 为可点击提及"${saving ? " disabled" : ""}>${escapeHtml(group.settings.welcome_message)}</textarea>
                    <span class="field-hint">开启入群验证时在验证通过后发送；支持 **粗体**、*斜体*、[文字](链接) 等 Markdown</span>
                  </div>
                  ${groupToggle(group, "welcome_disable_link_preview", "关闭欢迎语链接预览", "开启后不生成网页链接预览卡片", saving)}
                  <div class="field welcome-buttons">
                    <label class="field-label" for="group-${attr(group.id)}-welcome-buttons">欢迎语内联按钮</label>
                    <textarea id="group-${attr(group.id)}-welcome-buttons" data-template-buttons data-group-id="${attr(group.id)}" data-group-key="welcome_buttons" maxlength="30000" placeholder="每行：按钮名 | 操作 | 内容 | 行号 | 颜色（可选）&#10;官网 | url | https://example.com | 1 | primary&#10;复制群规 | copy | 群规文本 | 1 | success&#10;管理员删除 | dismiss | | 2 | danger"${saving ? " disabled" : ""}>${escapeHtml(groupTemplateButtonsText(group))}</textarea>
                    <span class="field-hint">操作：url 跳转、copy 复制、share 分享、dismiss 管理员删除；相同行号横向排列。内容中的 |、换行和反斜杠分别写成 \\|、\\n、\\\\。${templateButtonStyleLegend()}</span>
                  </div>
                </div>`,
            })}
            ${renderGroupSettingsSection(group, {
              key: "permissions",
              title: "成员权限与时段",
              description: "默认群权限与按时段覆盖的夜间模式",
              iconName: "key-round",
              itemLabel: "权限编辑器",
              settingKeys: ["default_permissions"],
              content: `<div class="group-settings-grid">${renderGroupPermissions(group, saving)}</div>`,
            })}
            ${renderGroupSettingsSection(group, {
              key: "safety",
              title: "巡检与爆破防护",
              description: "成员巡检、入群爆破检测与锁群参数",
              iconName: "shield-check",
              itemLabel: "8 项",
              settingKeys: ["patrol_enabled", "raid_guard_enabled", "raid_guard_pin_message", ...RAID_GUARD_GROUP_INT_FIELDS.map(({ key }) => key)],
              content: `
                <div class="group-settings-grid">
                  <div class="field">
                    <label class="field-label" for="group-${attr(group.id)}-patrol-enabled">自动巡检</label>
                    <select id="group-${attr(group.id)}-patrol-enabled" data-group-id="${attr(group.id)}" data-group-key="patrol_enabled" data-kind="nullable-boolean"${saving ? " disabled" : ""}>
                      <option value=""${group.settings.patrol_enabled == null ? " selected" : ""}>继承全局默认</option>
                      <option value="true"${group.settings.patrol_enabled === true ? " selected" : ""}>开启</option>
                      <option value="false"${group.settings.patrol_enabled === false ? " selected" : ""}>关闭</option>
                    </select>
                    <button class="secondary-button patrol-trigger" type="button" data-action="trigger-patrol" data-group-id="${attr(group.id)}"${saving ? " disabled" : ""}>
                      ${icon("radar")}<span>立即巡检</span>
                    </button>
                    <span class="field-hint">巡检时间等参数在「审核验证」页配置</span>
                  </div>
                  <div class="field">
                    <label class="field-label" for="group-${attr(group.id)}-raid-guard-enabled">爆破防护</label>
                    <select id="group-${attr(group.id)}-raid-guard-enabled" data-group-id="${attr(group.id)}" data-group-key="raid_guard_enabled" data-kind="nullable-boolean"${saving ? " disabled" : ""}>
                      <option value=""${group.settings.raid_guard_enabled == null ? " selected" : ""}>继承全局默认</option>
                      <option value="true"${group.settings.raid_guard_enabled === true ? " selected" : ""}>开启</option>
                      <option value="false"${group.settings.raid_guard_enabled === false ? " selected" : ""}>关闭</option>
                    </select>
                    <span class="field-hint">锁群阈值与超时可在下方逐群覆盖</span>
                  </div>
                  <div class="field">
                    <label class="field-label" for="group-${attr(group.id)}-raid-guard-pin-message">爆破防护活动自动置顶</label>
                    <select id="group-${attr(group.id)}-raid-guard-pin-message" data-group-id="${attr(group.id)}" data-group-key="raid_guard_pin_message" data-kind="nullable-boolean"${saving ? " disabled" : ""}>
                      <option value=""${group.settings.raid_guard_pin_message == null ? " selected" : ""}>继承全局默认</option>
                      <option value="true"${group.settings.raid_guard_pin_message === true ? " selected" : ""}>开启</option>
                      <option value="false"${group.settings.raid_guard_pin_message === false ? " selected" : ""}>关闭</option>
                    </select>
                    <span class="field-hint">触发防护时置顶通知，自动或手动结束防护时取消置顶</span>
                  </div>
                  ${RAID_GUARD_GROUP_INT_FIELDS.map(({ key, label, min, max }) => `
                  <div class="field">
                    <label class="field-label" for="group-${attr(group.id)}-${key.replaceAll("_", "-")}">${escapeHtml(label)}</label>
                    <input id="group-${attr(group.id)}-${key.replaceAll("_", "-")}" type="number" min="${min}" max="${max}" step="1" data-group-id="${attr(group.id)}" data-group-key="${key}" data-kind="nullable-int" value="${attr(group.settings[key] == null ? "" : group.settings[key])}" placeholder="继承全局默认"${saving ? " disabled" : ""}>
                  </div>`).join("")}
                </div>`,
            })}
            ${renderGroupSettingsSection(group, {
              key: "replay",
              title: "审核回放（dry-run）",
              description: "拿本群的脱敏历史发言，把当前生效的群规与一份候选提案各跑一遍",
              iconName: "flask-conical",
              itemLabel: "只读对照",
              // 没有可编辑的群设置：留空数组，分节就永远不会被标成"待保存"。
              settingKeys: [],
              content: renderReplayPanel(group),
            })}
            ${renderGroupSettingsSection(group, {
              key: "management",
              title: "管理员与投票",
              description: "呼叫管理员与民主投票封禁策略",
              iconName: "users",
              itemLabel: "9 项",
              settingKeys: ["call_admin_enabled", "call_admin_pin_message", "call_admin_targets", "vote_ban_enabled", "vote_ban_pin_message", ...VOTE_BAN_GROUP_INT_FIELDS.map(({ key }) => key)],
              content: `
                <div class="group-settings-grid">
                  <div class="field">
                    <label class="field-label" for="group-${attr(group.id)}-call-admin-enabled">呼叫管理员（@admin）</label>
                    <select id="group-${attr(group.id)}-call-admin-enabled" data-group-id="${attr(group.id)}" data-group-key="call_admin_enabled" data-kind="nullable-boolean"${saving ? " disabled" : ""}>
                      <option value=""${group.settings.call_admin_enabled == null ? " selected" : ""}>继承全局默认</option>
                      <option value="true"${group.settings.call_admin_enabled === true ? " selected" : ""}>开启</option>
                      <option value="false"${group.settings.call_admin_enabled === false ? " selected" : ""}>关闭</option>
                    </select>
                    <span class="field-hint">群成员发送 @admin 时 @ 下方勾选的管理员；可回复消息举报</span>
                  </div>
                  <div class="field">
                    <label class="field-label" for="group-${attr(group.id)}-call-admin-pin-message">呼叫管理员通知自动置顶</label>
                    <select id="group-${attr(group.id)}-call-admin-pin-message" data-group-id="${attr(group.id)}" data-group-key="call_admin_pin_message" data-kind="nullable-boolean"${saving ? " disabled" : ""}>
                      <option value=""${group.settings.call_admin_pin_message == null ? " selected" : ""}>继承全局默认</option>
                      <option value="true"${group.settings.call_admin_pin_message === true ? " selected" : ""}>开启</option>
                      <option value="false"${group.settings.call_admin_pin_message === false ? " selected" : ""}>关闭</option>
                    </select>
                    <span class="field-hint">开启后置顶呼叫通知；管理员可标记已处理并取消置顶</span>
                  </div>
                  <div class="field">
                    <label class="field-label" for="group-${attr(group.id)}-vote-ban-enabled">民主投票封禁</label>
                    <select id="group-${attr(group.id)}-vote-ban-enabled" data-group-id="${attr(group.id)}" data-group-key="vote_ban_enabled" data-kind="nullable-boolean"${saving ? " disabled" : ""}>
                      <option value=""${group.settings.vote_ban_enabled == null ? " selected" : ""}>继承全局默认</option>
                      <option value="true"${group.settings.vote_ban_enabled === true ? " selected" : ""}>开启</option>
                      <option value="false"${group.settings.vote_ban_enabled === false ? " selected" : ""}>关闭</option>
                    </select>
                    <span class="field-hint">命令和 Bot 技能共享下方单用户额度；票数达标即封禁被回复用户</span>
                  </div>
                  <div class="field">
                    <label class="field-label" for="group-${attr(group.id)}-vote-ban-pin-message">民主投票活动自动置顶</label>
                    <select id="group-${attr(group.id)}-vote-ban-pin-message" data-group-id="${attr(group.id)}" data-group-key="vote_ban_pin_message" data-kind="nullable-boolean"${saving ? " disabled" : ""}>
                      <option value=""${group.settings.vote_ban_pin_message == null ? " selected" : ""}>继承全局默认</option>
                      <option value="true"${group.settings.vote_ban_pin_message === true ? " selected" : ""}>开启</option>
                      <option value="false"${group.settings.vote_ban_pin_message === false ? " selected" : ""}>关闭</option>
                    </select>
                    <span class="field-hint">投票完成、管理员中止或直接封禁、票数不足超时后取消置顶</span>
                  </div>
                  <div class="field call-admin-targets">
                    <label class="field-label">呼叫目标管理员</label>
                    ${renderCallAdminTargets(group, saving)}
                  </div>
                  ${VOTE_BAN_GROUP_INT_FIELDS.map(({ key, label, min, max }) => `
                  <div class="field">
                    <label class="field-label" for="group-${attr(group.id)}-${key.replaceAll("_", "-")}">${escapeHtml(label)}</label>
                    <input id="group-${attr(group.id)}-${key.replaceAll("_", "-")}" type="number" min="${min}" max="${max}" step="1" data-group-id="${attr(group.id)}" data-group-key="${key}" data-kind="nullable-int" value="${attr(group.settings[key] == null ? "" : group.settings[key])}" placeholder="继承全局默认"${saving ? " disabled" : ""}>
                  </div>`).join("")}
                </div>`,
            })}
            ${renderGroupSettingsSection(group, {
              key: "proactive-style",
              title: "主动发言与风格",
              description: "主动话题任务与群内说话风格学习",
              iconName: "message-circle",
              itemLabel: "5 项",
              settingKeys: ["proactive_enabled", "proactive_task_brief", "mimic_target_user_id", "mimic_target_user_name", "mimic_profile_text"],
              content: `
                <div class="group-settings-grid">
                  <div class="field">
                    <label class="field-label" for="group-${attr(group.id)}-proactive">主动发言</label>
                    <select id="group-${attr(group.id)}-proactive" data-group-id="${attr(group.id)}" data-group-key="proactive_enabled" data-kind="nullable-boolean"${saving ? " disabled" : ""}>
                      <option value=""${group.settings.proactive_enabled == null ? " selected" : ""}>继承全局默认</option>
                      <option value="true"${group.settings.proactive_enabled === true ? " selected" : ""}>开启</option>
                      <option value="false"${group.settings.proactive_enabled === false ? " selected" : ""}>关闭</option>
                    </select>
                  </div>
                  <div class="field proactive-brief">
                    <label class="field-label" for="group-${attr(group.id)}-brief">主动任务简述</label>
                    <textarea id="group-${attr(group.id)}-brief" data-group-id="${attr(group.id)}" data-group-key="proactive_task_brief" data-kind="string" maxlength="240" placeholder="留空使用默认主动话题策略"${saving ? " disabled" : ""}>${escapeHtml(group.settings.proactive_task_brief)}</textarea>
                  </div>
                  <div class="field">
                    <label class="field-label" for="group-${attr(group.id)}-mimic-id">风格目标用户 ID</label>
                    <input id="group-${attr(group.id)}-mimic-id" type="number" min="0" step="1" data-group-id="${attr(group.id)}" data-group-key="mimic_target_user_id" data-kind="nonnegative-int" value="${attr(group.settings.mimic_target_user_id)}"${saving ? " disabled" : ""}>
                    <span class="field-hint">更换或清除 ID 会重置已有名称、画像、计数和样本</span>
                  </div>
                  <div class="field">
                    <label class="field-label" for="group-${attr(group.id)}-mimic-name">风格目标名称</label>
                    <input id="group-${attr(group.id)}-mimic-name" type="text" maxlength="80" data-group-id="${attr(group.id)}" data-group-key="mimic_target_user_name" data-kind="string" value="${attr(group.settings.mimic_target_user_name)}"${saving ? " disabled" : ""}>
                  </div>
                  <div class="field mimic-profile">
                    <label class="field-label" for="group-${attr(group.id)}-mimic-profile">说话风格画像</label>
                    <textarea id="group-${attr(group.id)}-mimic-profile" data-group-id="${attr(group.id)}" data-group-key="mimic_profile_text" data-kind="string" maxlength="1200" placeholder="可手动编辑，也会由采样自动更新"${saving ? " disabled" : ""}>${escapeHtml(group.settings.mimic_profile_text)}</textarea>
                    <span class="field-hint">已采样 ${escapeHtml(group.settings.mimic_sample_count)} 条，最近蒸馏点 ${escapeHtml(group.settings.mimic_distilled_at_count)} 条</span>
                  </div>
                </div>`,
            })}
            ${renderGroupSettingsSection(group, {
              key: "automation",
              title: "关键词回复与定时消息",
              description: "固定回复、消息计划与发送方式",
              iconName: "timer",
              itemLabel: resourceCategoryItemLabel(group.id, "automation"),
              additionalDirty: groupResourceDraftDirty(group.id, "automation"),
              dirtyLabel: "待保存",
              content: renderGroupResourcePanel(group, "automation"),
            })}
            ${renderGroupSettingsSection(group, {
              key: "rules-memory",
              title: "群规与永久记忆",
              description: "内容治理规则与长期上下文",
              iconName: "book-open-check",
              itemLabel: resourceCategoryItemLabel(group.id, "rules-memory"),
              additionalDirty: groupResourceDraftDirty(group.id, "rules-memory"),
              dirtyLabel: "待保存",
              content: renderGroupResourcePanel(group, "rules-memory"),
            })}
            ${renderGroupSettingsSection(group, {
              key: "memory-transparency", title: "记忆透明（只读）",
              description: "本群长期事实、联网检索留档及派生摘要；不含私聊或来源原文",
              iconName: "brain", defaultOpen: false,
              content: renderMemoryTransparency(group),
            })}
            ${renderGroupSettingsSection(group, {
              key: "member-lists",
              title: "成员记录与名单",
              description: "警告、封禁、审核豁免与回复静默",
              iconName: "contact-round",
              itemLabel: resourceCategoryItemLabel(group.id, "member-lists"),
              content: renderGroupResourcePanel(group, "member-lists"),
            })}
            ${renderGroupSettingsSection(group, {
              key: OPERATIONS_SECTION_KEY,
              title: "运营任务",
              description: "执行来源、周期计划与六类任务的预览/确认/结果",
              iconName: "activity",
              itemLabel: operationsItemLabel(group.id),
              content: renderOperationsStatusPanel(group),
            })}
          </div>` : ""}
        </div>
      </article>`;
  }

  function operationsEntry(groupId) {
    return state.groupOperations.get(String(groupId)) || null;
  }

  function operationsStamp(value) {
    if (!value) return "";
    // 后端给的是带 +08:00 偏移的 ISO 串；这里只做展示裁剪，不重新解释时区。
    const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
    if (!match) return String(value);
    return `${match[1]}-${match[2]}-${match[3]} ${match[4]}:${match[5]}`;
  }

  function operationsReason(reason) {
    if (!reason) return "";
    return OPERATIONS_REASON_LABEL[reason] || `未识别的原因（${reason}）`;
  }

  function operationsItemLabel(groupId) {
    const entry = operationsEntry(groupId);
    if (!entry || !entry.loaded) {
      if (entry?.loading) return "加载中";
      return entry?.error ? "加载失败" : "未加载";
    }
    return entry.stale ? "已过期" : "已加载";
  }

  function operationsTaskRow(task) {
    const meta = OPERATIONS_TASK_META[task.task_id]
      || { label: task.title || task.task_id, icon: "activity" };
    const source = task.source || {};
    const sourceLabel = OPERATIONS_SOURCE_LABEL[source.implemented] || "未知来源";
    const runner = source.runner_task_name ? `（${source.runner_task_name}）` : "";
    const nextRun = task.next_run || {};
    const success = task.last_success || {};
    const failure = task.last_failure || {};
    const failureCount = (failure.count === null || failure.count === undefined)
      ? null
      : Number(failure.count);
    // count 的单位由后端 count_meaning 标注：定时消息是「待重试记录数」，摘要才是失败次数。
    const countUnit = OPERATIONS_COUNT_MEANING_LABEL[failure.count_meaning]
      || (failure.count_meaning ? `记录（${failure.count_meaning}）` : "");
    const nextText = nextRun.at
      ? operationsStamp(nextRun.at)
      : (operationsReason(nextRun.reason) || "未记录");
    const successText = success.at
      ? operationsStamp(success.at)
      : (operationsReason(success.reason) || "未记录");
    const failureParts = [];
    if (failureCount) failureParts.push(`${countUnit || "记录"} ${failureCount}`);
    if (failure.at) failureParts.push(`最近 ${operationsStamp(failure.at)}`);
    if (failure.backoff_until) failureParts.push(`退避至 ${operationsStamp(failure.backoff_until)}`);
    const failureText = failureParts.length
      ? failureParts.join("，")
      : (failure.reason ? operationsReason(failure.reason) : "未记录");
    // 「已安排、尚未完成」的待重试：只有定时消息任务、且后端把 count 明确标注成
    // scheduled_messages_pending_retry_occurrences 时才成立 —— 其它任务的 count 是失败次数
    // （如摘要的累计失败次数），按失败次数推断「待重试 N 条」就是虚构排队事实。
    // 子项各自按字段语义有值才展示；整块不成立时连「待重试」这一行都不渲染。
    const retryConfirmed = task.task_id === "scheduled_messages"
      && failure.count_meaning === "scheduled_messages_pending_retry_occurrences";
    const retryScheduledText = OPERATIONS_RETRY_STATE_LABEL[failure.retry_state]
      || (failure.pending_retry_at
        ? `已安排 ${operationsStamp(failure.pending_retry_at)}，尚未重试`
        : "");
    const retryParts = retryConfirmed
      ? [
        retryScheduledText,
        failureCount ? `待重试记录 ${failureCount} 条` : "",
        (failure.attempts_max !== null && failure.attempts_max !== undefined)
          ? `最多已尝试 ${failure.attempts_max} 次`
          : "",
      ].filter(Boolean)
      : [];
    // 计划到期与未来计划是两个维度：当前到期待处理时不许拿未来时间冒充「下一次执行」，
    // 未来计划（details.earliest_planned_at）与到期待处理条数各自独立展示。
    const details = task.details || {};
    const earliestPlanned = details.earliest_planned_at || null;
    const dueNowCount = Number(details.due_now_pending_count) || 0;
    return `
      <li class="operations-task" data-operations-task="${attr(task.task_id)}">
        <div class="operations-task-head">
          <span class="operations-task-icon">${icon(meta.icon)}</span>
          <strong>${escapeHtml(meta.label)}</strong>
          <span class="badge info">${escapeHtml(OPERATIONS_TRIGGER_LABEL[task.trigger] || task.trigger || "未知触发")}</span>
        </div>
        <dl class="operations-task-facts">
          <div><dt>执行来源</dt><dd>${escapeHtml(sourceLabel)}${escapeHtml(runner)}</dd></div>
          <div><dt>下一次执行</dt><dd>${escapeHtml(nextText)}</dd></div>
          ${earliestPlanned && earliestPlanned !== nextRun.at
            ? `<div><dt>后续计划</dt><dd>${escapeHtml(operationsStamp(earliestPlanned))}</dd></div>`
            : ""}
          ${dueNowCount > 0
            ? `<div><dt>到期待处理</dt><dd>${escapeHtml(`${dueNowCount} 条配置已到执行时刻，等待投递`)}</dd></div>`
            : ""}
          <div><dt>最近成功</dt><dd>${escapeHtml(successText)}</dd></div>
          <div><dt>最近失败</dt><dd>${escapeHtml(failureText)}</dd></div>
          ${retryParts.length ? `<div><dt>待重试</dt><dd>${escapeHtml(retryParts.join("，"))}</dd></div>` : ""}
        </dl>
        ${(task.unknowns || []).length
          ? `<ul class="operations-task-unknowns">${task.unknowns.map(item => `<li>${escapeHtml(item.field)}：${escapeHtml(operationsReason(item.reason))}</li>`).join("")}</ul>`
          : ""}
      </li>`;
  }

  function renderOperationsStatusPanel(group) {
    const entry = operationsEntry(group.id);
    // in-flight（首次加载或已加载后刷新）统一算 busy：按钮禁用 + aria-busy=true。
    const busy = Boolean(entry?.loading || entry?.refreshing);
    const hasData = Boolean(entry?.loaded);
    // 首次读取失败：稳定显示错误 + 显式重试，绝不隐式重试。
    const failedFirstLoad = Boolean(entry?.error) && !hasData && !busy;
    const buttonLabel = hasData
      ? "刷新运营任务状态"
      : (entry?.attempted ? "重试读取" : "加载运营任务状态");
    const refreshButton = `
      <button class="secondary-button" type="button" data-action="load-group-operations" data-group-id="${attr(group.id)}"${busy ? " disabled" : ""} aria-busy="${busy ? "true" : "false"}">
        ${busy
          ? `<span class="spinner spinner-compact"></span><span>加载中…</span>`
          : `${icon("refresh-cw")}${buttonLabel}`}
      </button>`;
    // 渲染优先级：读取中 > 首次失败 > 未读取 > 有数据（可带「上次刷新失败」）。
    // 旧实现先匹配「未加载」，把错误分支整个遮住了（父代理独立验收 R1/R2）。
    let body;
    if (busy && !hasData) {
      body = `<div class="loading-inline"><span class="spinner spinner-small"></span><span>正在读取本群运营任务状态</span></div>`;
    } else if (failedFirstLoad) {
      body = `<div class="notice danger">${icon("shield-alert")}<span>${escapeHtml(entry.error)}</span></div>
        <p class="field-hint">首次读取失败后不会自动重试；需要时请用下面的按钮显式重试一次。</p>`;
    } else if (!hasData) {
      body = `<div class="resource-empty-state">${icon("activity")}<p><strong>尚未加载运营任务状态</strong></p><p>展开本分节会读取一次；数据严格按群隔离。</p></div>`;
    } else {
      const tasks = entry.data?.tasks || [];
      body = `
        ${entry.error ? `<div class="notice warning">${icon("info")}<span>上次刷新失败：${escapeHtml(entry.error)}。下面是上一次成功读取的数据，可能已经过期。</span></div>` : ""}
        ${tasks.length
          ? `<ul class="operations-task-list">${tasks.map(operationsTaskRow).join("")}</ul>`
          : `<div class="resource-empty-state">${icon("activity")}<p>后端没有返回任何任务条目。</p></div>`}
        <p class="field-hint">读取时刻：${escapeHtml(operationsStamp(entry.data?.generated_at) || "—")}（${escapeHtml(entry.data?.timezone || "Asia/Shanghai")}）</p>`;
    }
    return `
      <section class="group-resource-panel operations-panel" data-operations-panel data-operations-group-id="${attr(group.id)}">
        <div class="notice info">${icon("info")}<span><strong>上面这段是只读状态。</strong>只展示已实现的执行来源与可证明的证据；没有记录就显示「未记录」，读不到的外部计划不会用占位时间代替。下面的来源与计划、任务执行都需要先预览再确认。</span></div>
        ${body}
        <div class="resource-panel-toolbar">
          ${refreshButton}
          <span class="field-hint">状态读取不会补跑、不会发送、不会改任何设置。</span>
        </div>
        ${renderPolicyPanel(group)}
        ${renderOperationsWorkPanel(group)}
      </section>`;
  }

  // ------------------------------------------------ B13b：执行来源与周期计划

  function policyEntry(groupId) {
    return state.groupPolicy.get(String(groupId)) || null;
  }

  function operationsWork(groupId) {
    return state.groupOperationsWork.get(String(groupId)) || null;
  }

  function operationsWorkPatch(groupId, patch) {
    const key = String(groupId);
    state.groupOperationsWork.set(key, { ...(operationsWork(key) || {}), ...patch });
  }

  function clearOperationsSensitive(groupId) {
    // 权限变化 / 群不存在：清掉这个群的策略、工作流与轮询令牌，绝不把上一个人的
    // 预览令牌、run 结果留在界面上。
    const key = String(groupId);
    state.groupPolicy.delete(key);
    state.groupPolicyLoads.delete(key);
    state.groupOperationsWork.delete(key);
    state.groupOperationsPoll.delete(key);
  }

  function policyTaskLabel(task) {
    const meta = OPERATIONS_TASK_META[task.task_id];
    return meta ? meta.label : (task.title || task.task_id);
  }

  function policySourceText(task) {
    return OPERATIONS_POLICY_SOURCE_LABEL[task.source] || `未识别的来源（${task.source}）`;
  }

  function policyPlanText(task) {
    if (!task.plan_supported) return "这个任务没有周期计划。";
    if (!task.plan_enabled) return "周期计划：关闭（只在你手动确认之后才执行）。";
    const day = OPERATIONS_WEEKDAY_LABEL[Number(task.plan_weekday)] || `星期${task.plan_weekday}`;
    const minute = Number(task.plan_minute);
    const clock = `${String(Math.floor(minute / 60)).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`;
    const next = task.anchor_at ? operationsStamp(task.anchor_at) : "未记录";
    return `周期计划：每周${day} ${clock}（Asia/Shanghai），下一次 ${next}`;
  }

  function operationsRunStatusText(run) {
    const label = OPERATIONS_RUN_STATUS_LABEL[run.status] || `未识别状态（${run.status}）`;
    const reason = run.reason ? operationsRunReasonText(run.reason) : "";
    return reason ? `${label} · ${reason}` : label;
  }

  function operationsRunReasonText(reason) {
    return OPERATIONS_RUN_REASON_LABEL[reason]
      || OPERATIONS_REASON_LABEL[reason]
      || `未识别的原因（${reason}）`;
  }

  async function loadGroupPolicy(groupId) {
    const key = String(groupId);
    const previous = policyEntry(key) || null;
    if (previous?.loading) return;
    const token = Symbol("group-policy");
    state.groupPolicyLoads.set(key, token);
    state.groupPolicy.set(key, { ...(previous || {}), attempted: true, loading: true, error: "" });
    renderContent();
    try {
      const body = await apiFetch(`/api/v1/groups/${encodeURIComponent(key)}/operations/policy`);
      if (state.groupPolicyLoads.get(key) !== token) return;
      state.groupPolicy.set(key, {
        ...(previous || {}),
        attempted: true,
        loading: false,
        loaded: true,
        error: "",
        data: body || null,
      });
    } catch (error) {
      if (state.groupPolicyLoads.get(key) !== token) return;
      if (error?.status === 401 || error?.status === 403 || error?.status === 404) {
        clearOperationsSensitive(key);
      } else {
        state.groupPolicy.set(key, {
          ...(previous || {}),
          attempted: true,
          loading: false,
          error: error?.message || "执行来源读取失败",
        });
      }
    }
    renderContent();
  }

  async function loadPolicyAudit(groupId, cursor) {
    const key = String(groupId);
    const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
    const body = await apiFetch(`/api/v1/groups/${encodeURIComponent(key)}/operations/policy/audit${query}`);
    const previous = policyEntry(key) || {};
    const items = cursor ? [...(previous.audit?.items || []), ...(body?.items || [])] : (body?.items || []);
    state.groupPolicy.set(key, {
      ...previous,
      audit: { items, next_cursor: body?.next_cursor ?? null, has_more: Boolean(body?.has_more) },
    });
    renderContent();
  }

  async function previewPolicy(groupId, payload) {
    const key = String(groupId);
    try {
      const body = await apiFetch(`/api/v1/groups/${encodeURIComponent(key)}/operations/policy/preview`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      const preview = body?.preview || null;
      state.groupPolicy.set(key, {
        ...(policyEntry(key) || {}),
        preview,
        previewToken: preview?.token || "",
        previewError: "",
      });
    } catch (error) {
      if (error?.status === 401 || error?.status === 403 || error?.status === 404) {
        clearOperationsSensitive(key);
        showToast("权限或群状态已变化，已清空本群的来源与计划数据", "error", 6000);
        renderContent();
        return;
      }
      state.groupPolicy.set(key, {
        ...(policyEntry(key) || {}),
        preview: null,
        previewToken: "",
        previewError: error?.message || "预览失败",
      });
    }
    renderContent();
  }

  async function confirmPolicy(groupId) {
    const key = String(groupId);
    const entry = policyEntry(key) || {};
    const token = entry.previewToken;
    if (!token) return;
    try {
      const body = await apiFetch(`/api/v1/groups/${encodeURIComponent(key)}/operations/policy/confirm`, {
        method: "POST",
        body: JSON.stringify({ token }),
      });
      state.groupPolicy.set(key, {
        ...entry,
        preview: null,
        previewToken: "",
        previewError: "",
        confirmed: body || null,
        confirmedAt: Date.now(),
      });
      showToast(body?.detail || "执行来源已更新", "success", 4800);
      await loadGroupPolicy(groupId);
      return;
    } catch (error) {
      if (error?.status === 409 || error?.status === 400) {
        // 版本冲突 / 参数不再合法：如实说明，并保留草稿让用户重新预览。
        state.groupPolicy.set(key, {
          ...entry,
          previewError: `${error.message || "确认失败"}（需要重新预览）`,
          previewToken: "",
        });
      } else if (error?.status === 401 || error?.status === 403 || error?.status === 404) {
        clearOperationsSensitive(key);
        showToast("权限或群状态已变化，已清空本群的来源与计划数据", "error", 6000);
      } else {
        // 结果不确定：绝不显示成功，也不自动重试。
        state.groupPolicy.set(key, {
          ...entry,
          previewError: `这次确认的结果无法确认：${error.message || "网络或服务异常"}。可能已经生效，请点「重新查看」核对后再决定。`,
        });
      }
    }
    renderContent();
  }

  function readOperationsParams(panel, taskId) {
    const fields = OPERATIONS_PARAM_FIELDS[taskId] || [];
    const params = {};
    fields.forEach(field => {
      const input = panel?.querySelector(`[data-operations-param="${field.name}"]`);
      const raw = input ? String(input.value ?? "").trim() : "";
      if (!raw) return;
      if (field.name === "entry_ids") {
        params.entry_ids = raw.split(/[\s,，]+/).filter(Boolean).map(value => Number(value)).filter(value => Number.isInteger(value) && value > 0);
        if (!params.entry_ids.length) delete params.entry_ids;
        return;
      }
      if (field.name === "action") { params.action = raw; return; }
      const number = Number(raw);
      if (Number.isInteger(number)) params[field.name] = number;
    });
    return params;
  }

  async function previewOperationsRun(groupId, panel) {
    const key = String(groupId);
    const work = operationsWork(key) || {};
    const taskId = work.taskId;
    if (!taskId) return;
    const params = readOperationsParams(panel, taskId);
    try {
      const body = await apiFetch(`/api/v1/groups/${encodeURIComponent(key)}/operations/preview`, {
        method: "POST",
        body: JSON.stringify({ task_id: taskId, params }),
      });
      const preview = body?.preview || null;
      operationsWorkPatch(key, {
        params,
        preview,
        previewToken: preview?.token || "",
        previewError: "",
        run: null,
        runKey: "",
        notice: "",
      });
    } catch (error) {
      if (error?.status === 401 || error?.status === 403 || error?.status === 404) {
        clearOperationsSensitive(key);
        showToast("权限或群状态已变化，已清空本群的运营数据", "error", 6000);
        renderContent();
        return;
      }
      operationsWorkPatch(key, {
        params,
        preview: null,
        previewToken: "",
        previewError: error?.message || "预览失败",
      });
    }
    renderContent();
  }

  async function confirmOperationsRun(groupId) {
    const key = String(groupId);
    const work = operationsWork(key) || {};
    const token = work.previewToken;
    if (!token) return;
    try {
      const body = await apiFetch(`/api/v1/groups/${encodeURIComponent(key)}/operations/runs`, {
        method: "POST",
        body: JSON.stringify({ task_id: work.taskId, token }),
      });
      const run = body?.run || null;
      operationsWorkPatch(key, {
        preview: null,
        previewToken: "",
        previewError: "",
        run,
        runKey: run?.run_key || "",
        notice: run?.duplicate ? "这次确认是重复提交，已返回同一次执行记录。" : "已受理，下面显示真实状态。",
      });
      renderContent();
      if (run?.run_key) pollOperationsRun(groupId, run.run_key);
      return;
    } catch (error) {
      if (error?.status === 409 || error?.status === 400) {
        operationsWorkPatch(key, {
          previewError: `${error.message || "确认失败"}（目标或授权变了，需要重新预览）`,
          previewToken: "",
        });
      } else if (error?.status === 401 || error?.status === 403 || error?.status === 404) {
        clearOperationsSensitive(key);
        showToast("权限或群状态已变化，已清空本群的运营数据", "error", 6000);
      } else {
        // ack 未知：既不假装成功，也绝不自动重试（可能已经执行）。
        operationsWorkPatch(key, {
          notice: "",
          previewError: `这次确认的结果无法确认：${error.message || "网络或服务异常"}。可能已经受理，请点「对账」核对，不要直接重复提交。`,
        });
      }
    }
    renderContent();
  }

  async function loadOperationsRun(groupId, runKey) {
    const key = String(groupId);
    const body = await apiFetch(`/api/v1/groups/${encodeURIComponent(key)}/operations/runs/${encodeURIComponent(runKey)}`);
    operationsWorkPatch(key, { run: body?.run || null, runKey: body?.run?.run_key || runKey });
    renderContent();
  }

  async function pollOperationsRun(groupId, runKey) {
    const key = String(groupId);
    const token = Symbol("operations-poll");
    state.groupOperationsPoll.set(key, token);
    operationsWorkPatch(key, { polling: true });
    renderContent();
    for (let attempt = 0; attempt < 60; attempt += 1) {
      if (state.groupOperationsPoll.get(key) !== token) return;
      const current = operationsWork(key);
      if (!current || current.runKey !== runKey) return;
      try {
        await loadOperationsRun(groupId, runKey);
      } catch (error) {
        operationsWorkPatch(key, { polling: false, error: error?.message || "读取执行记录失败" });
        renderContent();
        return;
      }
      if (state.groupOperationsPoll.get(key) !== token) return;
      const run = operationsWork(key)?.run;
      if (run && OPERATIONS_TERMINAL_RUN_STATUS.has(String(run.status))) {
        operationsWorkPatch(key, { polling: false });
        renderContent();
        return;
      }
      await new Promise(resolve => window.setTimeout(resolve, 2000));
    }
    operationsWorkPatch(key, { polling: false });
    renderContent();
  }

  function stopOperationsPolling(groupId) {
    state.groupOperationsPoll.delete(String(groupId));
    operationsWorkPatch(groupId, { polling: false });
    renderContent();
  }

  async function operationsRunAction(groupId, runKey, action) {
    const key = String(groupId);
    try {
      const body = await apiFetch(
        `/api/v1/groups/${encodeURIComponent(key)}/operations/runs/${encodeURIComponent(runKey)}/${action}`,
        { method: "POST", body: JSON.stringify({}) },
      );
      const run = body?.run || null;
      operationsWorkPatch(key, { run, runKey: run?.run_key || runKey, notice: "" });
      renderContent();
      if (action === "retry" && run?.run_key) pollOperationsRun(groupId, run.run_key);
      return;
    } catch (error) {
      operationsWorkPatch(key, {
        notice: "",
        error: `${action === "cancel" ? "取消" : action === "retry" ? "重试" : "对账"}失败：${error?.message || "服务异常"}`,
      });
    }
    renderContent();
  }

  function operationsRetryAllowed(run) {
    if (!run || !run.finished) return false;
    const items = run.items || [];
    if (items.some(item => String(item.status) === "unknown")) return false;
    if (String(run.status) === "unknown") return false;
    return items.some(item => item.resendable === true);
  }

  function renderPolicyPreview(group, entry) {
    const preview = entry.preview;
    if (!preview) return "";
    const effects = (preview.effects || []).map(text => `<li>${escapeHtml(text)}</li>`).join("");
    const proposed = preview.proposed?.source
      ? `来源：${escapeHtml(OPERATIONS_POLICY_SOURCE_LABEL[preview.proposed.source] || preview.proposed.source)}`
      : preview.proposed?.plan_enabled
        ? `周期计划：${escapeHtml(policyPlanText({ ...preview.proposed, task_id: preview.task_id, plan_supported: true, anchor_at: preview.next_anchor_at }))}`
        : "周期计划：关闭";
    return `
      <div class="notice info" data-operations-policy-preview>
        ${icon("info")}
        <div>
          <p><strong>预览（还没有写入）</strong>：${escapeHtml(policyTaskLabel(preview))}</p>
          <p>${proposed}</p>
          <ul>${effects}</ul>
          <p class="field-hint">${escapeHtml(preview.detail || "")}｜时区 ${escapeHtml(preview.timezone || "Asia/Shanghai")}｜外部宿主 cron 的登记情况未知</p>
        </div>
      </div>
      <div class="resource-panel-toolbar">
        <button class="primary-button" type="button" data-action="operations-confirm-policy" data-group-id="${attr(group.id)}">${icon("check")}确认这次变更</button>
        <button class="secondary-button" type="button" data-action="operations-discard-policy" data-group-id="${attr(group.id)}">取消预览</button>
      </div>`;
  }

  function renderPolicyPanel(group) {
    const entry = policyEntry(group.id);
    const busy = Boolean(entry?.loading);
    const data = entry?.data;
    const tasks = data?.tasks || [];
    const rows = tasks.map(task => {
      const switchable = Boolean(task.source_switchable);
      const options = (task.source_options || []).map(source => `
        <button class="secondary-button" type="button" data-action="operations-preview-source" data-group-id="${attr(group.id)}" data-task-id="${attr(task.task_id)}" data-source="${attr(source)}"${source === task.source || busy ? " disabled" : ""}>
          ${escapeHtml(OPERATIONS_POLICY_SOURCE_LABEL[source] || source)}
        </button>`).join(" ");
      const planControls = task.plan_supported
        ? `
          <div class="operations-plan-controls">
            <label>周内日
              <select data-operations-plan-weekday data-task-id="${attr(task.task_id)}"${busy ? " disabled" : ""}>
                ${OPERATIONS_WEEKDAY_LABEL.map((label, index) => `<option value="${index}"${index === task.plan_weekday ? " selected" : ""}>${label}</option>`).join("")}
              </select>
            </label>
            <label>时刻（Asia/Shanghai）
              <input type="time" data-operations-plan-minute data-task-id="${attr(task.task_id)}"${busy ? " disabled" : ""} value="${task.plan_enabled && task.plan_minute !== null && task.plan_minute !== undefined ? `${String(Math.floor(Number(task.plan_minute) / 60)).padStart(2, "0")}:${String(Number(task.plan_minute) % 60).padStart(2, "0")}` : ""}">
            </label>
            <button class="secondary-button" type="button" data-action="operations-preview-plan" data-group-id="${attr(group.id)}" data-task-id="${attr(task.task_id)}" data-enabled="true"${busy ? " disabled" : ""}>${icon("calendar-clock")}预览开启计划</button>
            <button class="secondary-button" type="button" data-action="operations-preview-plan" data-group-id="${attr(group.id)}" data-task-id="${attr(task.task_id)}" data-enabled="false"${busy ? " disabled" : ""}>预览关闭计划</button>
          </div>`
        : "";
      return `
        <li class="operations-policy-task" data-operations-policy-task="${attr(task.task_id)}">
          <div class="operations-task-head">
            <strong>${escapeHtml(policyTaskLabel(task))}</strong>
            <span class="badge info">${escapeHtml(policySourceText(task))}</span>
          </div>
          <p class="field-hint">${escapeHtml(task.source_switchable ? "可以切换执行来源（只影响未来，不补历史）。" : "只有一种执行来源，不能切换。")}${task.runner_task_name ? `｜进程内任务：${escapeHtml(task.runner_task_name)}` : ""}</p>
          ${switchable ? `<div class="resource-panel-toolbar">${options}<span class="field-hint">当前：${escapeHtml(policySourceText(task))}</span></div>` : ""}
          <p class="field-hint">${escapeHtml(policyPlanText(task))}</p>
          ${planControls}
        </li>`;
    }).join("");
    const audit = entry?.audit;
    const auditItems = (audit?.items || []).map(item => `<li>${escapeHtml(operationsStamp(item.created_at) || "—")}｜${escapeHtml(OPERATIONS_TASK_META[item.task_id]?.label || item.task_id)}｜${escapeHtml(item.detail || item.action)}</li>`).join("");
    let body;
    if (busy && !data) {
      body = `<div class="loading-inline"><span class="spinner spinner-small"></span><span>正在读取执行来源与周期计划</span></div>`;
    } else if (entry?.error && !data) {
      body = `<div class="notice danger">${icon("shield-alert")}<span>${escapeHtml(entry.error)}</span></div>`;
    } else if (!data) {
      body = `<div class="resource-empty-state">${icon("sliders-horizontal")}<p><strong>尚未读取执行来源</strong></p><p>展开后读取一次；来源与周期计划都按群隔离。</p></div>`;
    } else {
      body = `${rows ? `<ul class="operations-task-list">${rows}</ul>` : ""}
        ${entry.previewError ? `<div class="notice warning">${icon("triangle-alert")}<span>${escapeHtml(entry.previewError)}</span></div>` : ""}
        ${renderPolicyPreview(group, entry)}
        <div class="resource-panel-toolbar">
          <button class="secondary-button" type="button" data-action="operations-load-audit" data-group-id="${attr(group.id)}">${icon("history")}读取变更审计</button>
          ${audit ? `<button class="secondary-button" type="button" data-action="operations-load-audit-more" data-group-id="${attr(group.id)}" data-cursor="${attr(audit.next_cursor || "")}"${audit.has_more ? "" : " disabled"}>加载更多审计</button>` : ""}
        </div>
        ${auditItems ? `<ul class="operations-audit-list">${auditItems}</ul>` : (audit ? `<p class="field-hint">没有更早的变更记录。</p>` : "")}`;
    }
    return `
      <section class="group-resource-panel operations-policy-panel" data-operations-policy-panel data-operations-group-id="${attr(group.id)}">
        <div class="notice info">${icon("info")}<span><strong>执行来源与周期计划需要两次确认。</strong>切换只影响未来（不补发已过去的时段）；程序不读宿主 crontab，外部 cron 的登记情况未知。</span></div>
        ${body}
        <div class="resource-panel-toolbar">
          <button class="secondary-button" type="button" data-action="operations-load-policy" data-group-id="${attr(group.id)}"${busy ? " disabled" : ""}>${icon("refresh-cw")}${data ? "刷新来源与计划" : "读取来源与计划"}</button>
        </div>
      </section>`;
  }

  function renderOperationsWorkPanel(group) {
    const work = operationsWork(group.id) || {};
    const taskId = work.taskId || "";
    const catalogue = OPERATIONS_TASK_META;
    const chooser = Object.keys(catalogue).map(id => `
      <button class="secondary-button${id === taskId ? " active" : ""}" type="button" data-action="operations-select-task" data-group-id="${attr(group.id)}" data-task-id="${attr(id)}">${escapeHtml(catalogue[id].label)}</button>`).join(" ");
    const fields = (OPERATIONS_PARAM_FIELDS[taskId] || []).map(field => {
      if (field.type === "select") {
        return `<label>${escapeHtml(field.label)}
          <select data-operations-param="${attr(field.name)}">${field.options.map(([value, label]) => `<option value="${attr(value)}">${escapeHtml(label)}</option>`).join("")}</select>
        </label>`;
      }
      return `<label>${escapeHtml(field.label)}
        <input type="text" data-operations-param="${attr(field.name)}" placeholder="${attr(field.placeholder || "")}" value="${attr(work.params?.[field.name] ?? "")}">
      </label>`;
    }).join("");
    const preview = work.preview;
    const effects = (preview?.effects || []).map(text => `<li>${escapeHtml(text)}</li>`).join("");
    const targets = (preview?.targets || []).map(item => `<li>${escapeHtml(item.label || item.item_key)}${item.detail ? `｜${escapeHtml(item.detail)}` : ""}</li>`).join("");
    const run = work.run;
    const items = (run?.items || []).map(item => `
      <li data-operations-item="${attr(item.item_key)}">
        <strong>${escapeHtml(item.label || item.target?.label || item.item_key)}</strong>
        <span class="badge">${escapeHtml(OPERATIONS_ITEM_STATUS_LABEL[item.status] || item.status)}</span>
        ${item.reason ? `<span class="field-hint">${escapeHtml(operationsRunReasonText(item.reason))}</span>` : ""}
        ${item.detail ? `<p class="field-hint">${escapeHtml(item.detail)}</p>` : ""}
      </li>`).join("");
    let runBlock = "";
    if (run) {
      const retryAllowed = operationsRetryAllowed(run);
      runBlock = `
        <div class="operations-run" data-operations-run="${attr(run.run_key)}">
          <p><strong>执行记录</strong>：${escapeHtml(operationsRunStatusText(run))}｜${escapeHtml(run.items_succeeded || 0)} 成功 / ${escapeHtml(run.items_failed || 0)} 失败 / ${escapeHtml(run.items_unknown || 0)} 未知</p>
          ${run.detail ? `<p class="field-hint">${escapeHtml(run.detail)}</p>` : ""}
          ${items ? `<ul class="operations-run-items">${items}</ul>` : ""}
          <div class="resource-panel-toolbar">
            <button class="secondary-button" type="button" data-action="operations-refresh-run" data-group-id="${attr(group.id)}" data-run-key="${attr(run.run_key)}">${icon("refresh-cw")}刷新状态</button>
            ${work.polling
              ? `<button class="secondary-button" type="button" data-action="operations-stop-poll" data-group-id="${attr(group.id)}">停止轮询</button>`
              : (run.finished ? "" : `<button class="secondary-button" type="button" data-action="operations-start-poll" data-group-id="${attr(group.id)}" data-run-key="${attr(run.run_key)}">继续轮询</button>`)}
            ${run.finished ? "" : `<button class="secondary-button" type="button" data-action="operations-cancel-run" data-group-id="${attr(group.id)}" data-run-key="${attr(run.run_key)}">请求取消</button>`}
            <button class="secondary-button" type="button" data-action="operations-reconcile-run" data-group-id="${attr(group.id)}" data-run-key="${attr(run.run_key)}">对账</button>
            ${retryAllowed
              ? `<button class="secondary-button" type="button" data-action="operations-retry-run" data-group-id="${attr(group.id)}" data-run-key="${attr(run.run_key)}">仅安全重试</button>`
              : `<button class="secondary-button" type="button" disabled title="有未知结果或没有可安全重试的条目">不提供重试</button>`}
          </div>
        </div>`;
    }
    return `
      <section class="group-resource-panel operations-work-panel" data-operations-work-panel data-operations-group-id="${attr(group.id)}">
        <div class="resource-panel-toolbar">${chooser}</div>
        ${taskId ? `
          <div class="operations-params">${fields}</div>
          <div class="resource-panel-toolbar">
            <button class="secondary-button" type="button" data-action="operations-preview-run" data-group-id="${attr(group.id)}">${icon("eye")}预览（只读，不写库）</button>
          </div>
          ${work.previewError ? `<div class="notice warning">${icon("triangle-alert")}<span>${escapeHtml(work.previewError)}</span></div>` : ""}
          ${preview ? `
            <div class="notice info" data-operations-run-preview>
              <div>
                <p><strong>${escapeHtml(preview.task_title || taskId)}</strong>：${escapeHtml(preview.detail || "")}</p>
                ${effects ? `<ul>${effects}</ul>` : ""}
                ${targets ? `<p class="field-hint">将处理 ${(preview.targets || []).length} 项：</p><ul>${targets}</ul>` : `<p class="field-hint">这次预览没有选中任何目标。</p>`}
                <p class="field-hint">成本：${escapeHtml(JSON.stringify(preview.cost || {}))}</p>
              </div>
            </div>
            <div class="resource-panel-toolbar">
              <button class="primary-button" type="button" data-action="operations-confirm-run" data-group-id="${attr(group.id)}"${preview.confirmable === false ? " disabled" : ""}>${icon("play")}确认并执行</button>
            </div>` : ""}
          ${work.notice ? `<div class="notice success">${icon("circle-check")}<span>${escapeHtml(work.notice)}</span></div>` : ""}
          ${work.error ? `<div class="notice danger">${icon("shield-alert")}<span>${escapeHtml(work.error)}</span></div>` : ""}
          ${runBlock}
        ` : `<p class="field-hint">选择上面任意一类任务：预览只读，确认之后才会真的执行，并显示真实的按项结果。</p>`}
      </section>`;
  }

  async function handleOperationsAction(action, button) {
    const groupId = button.dataset.groupId;
    const key = String(groupId);
    if (action === "operations-load-policy") { await loadGroupPolicy(groupId); return; }
    if (action === "operations-preview-source") {
      await previewPolicy(groupId, { task_id: button.dataset.taskId, mode: "source", source: button.dataset.source });
      return;
    }
    if (action === "operations-preview-plan") {
      const panel = button.closest("[data-operations-policy-panel]");
      const taskId = button.dataset.taskId;
      const weekday = panel?.querySelector(`[data-operations-plan-weekday][data-task-id="${CSS.escape(taskId)}"]`)?.value ?? "";
      const rawMinute = panel?.querySelector(`[data-operations-plan-minute][data-task-id="${CSS.escape(taskId)}"]`)?.value ?? "";
      const enabled = button.dataset.enabled === "true";
      const payload = { task_id: taskId, mode: "plan", enabled };
      if (enabled) {
        if (weekday === "" || rawMinute === "") {
          showToast("开启周期计划必须明确选择周内日与时刻", "warning", 6000);
          return;
        }
        const [hour, minute] = String(rawMinute).split(":");
        payload.weekday = Number(weekday);
        payload.minute = Number(hour) * 60 + Number(minute);
      }
      await previewPolicy(groupId, payload);
      return;
    }
    if (action === "operations-confirm-policy") { await confirmPolicy(groupId); return; }
    if (action === "operations-discard-policy") {
      state.groupPolicy.set(key, { ...(policyEntry(key) || {}), preview: null, previewToken: "", previewError: "" });
      renderContent();
      return;
    }
    if (action === "operations-load-audit") { await loadPolicyAudit(groupId, null); return; }
    if (action === "operations-load-audit-more") { await loadPolicyAudit(groupId, button.dataset.cursor || null); return; }
    if (action === "operations-select-task") {
      operationsWorkPatch(key, { taskId: button.dataset.taskId, preview: null, previewToken: "", previewError: "", params: {}, error: "" });
      renderContent();
      return;
    }
    if (action === "operations-preview-run") {
      await previewOperationsRun(groupId, button.closest("[data-operations-work-panel]"));
      return;
    }
    if (action === "operations-confirm-run") { await confirmOperationsRun(groupId); return; }
    if (action === "operations-refresh-run") {
      try { await loadOperationsRun(groupId, button.dataset.runKey); }
      catch (error) { operationsWorkPatch(key, { error: error?.message || "读取执行记录失败" }); renderContent(); }
      return;
    }
    if (action === "operations-start-poll") { await pollOperationsRun(groupId, button.dataset.runKey); return; }
    if (action === "operations-stop-poll") { stopOperationsPolling(groupId); return; }
    if (action === "operations-cancel-run") { await operationsRunAction(groupId, button.dataset.runKey, "cancel"); return; }
    if (action === "operations-retry-run") { await operationsRunAction(groupId, button.dataset.runKey, "retry"); return; }
    if (action === "operations-reconcile-run") { await operationsRunAction(groupId, button.dataset.runKey, "reconcile"); return; }
  }

  function resourceOperationKey(groupId, type, id) {
    return `${String(groupId)}:${String(type)}:${String(id)}`;
  }

  function resourceEditDraftKey(groupId, type, id) {
    if (type === "rules") return `rule:${groupId}:${id}`;
    if (type === "memories") return `memory:${groupId}:${id}`;
    return `entry:${groupId}:${type}:${id}`;
  }

  function resourceArray(groupId, type) {
    const resource = state.groupResources.get(String(groupId));
    const property = RESOURCE_TYPE_META[type]?.property;
    return resource && property && Array.isArray(resource[property]) ? resource[property] : [];
  }

  function upsertResourceRow(groupId, type, document) {
    if (!document) return;
    const list = resourceArray(groupId, type);
    const identity = document.id ?? document.user_id;
    const index = list.findIndex(item => String(item.id ?? item.user_id) === String(identity));
    if (index >= 0) list.splice(index, 1, document);
    else list.unshift(document);
  }

  function applyImmediateResourceCreate(groupId, type, result) {
    const responseKey = RESOURCE_TYPE_META[type]?.response;
    const document = responseKey ? result?.[responseKey] : null;
    upsertResourceRow(groupId, type, document);
    if (type === "bans" && document) {
      const warning = resourceArray(groupId, "warnings")
        .find(item => String(item.user_id) === String(document.user_id));
      if (warning) warning.is_banned = true;
    }
    bumpGroupResourceMutationEpoch(groupId);
  }

  function applyImmediateResourceDelete(groupId, type, id) {
    const list = resourceArray(groupId, type);
    const index = list.findIndex(item => String(item.id ?? item.user_id) === String(id));
    if (index >= 0) list.splice(index, 1);
    if (type === "bans") {
      const warningList = resourceArray(groupId, "warnings");
      const warningIndex = warningList.findIndex(item => String(item.user_id) === String(id));
      if (warningIndex >= 0) warningList.splice(warningIndex, 1);
    }
    bumpGroupResourceMutationEpoch(groupId);
  }

  function bumpGroupResourceMutationEpoch(groupId) {
    const key = String(groupId);
    state.groupResourceMutationEpochs.set(key, (state.groupResourceMutationEpochs.get(key) || 0) + 1);
  }

  function resourcePendingDelete(groupId, type, id) {
    return state.pendingResourceDeletes.has(resourceOperationKey(groupId, type, id));
  }

  function resourcePendingCreate(groupId, type, id) {
    return state.pendingResourceCreates.has(resourceOperationKey(groupId, type, id));
  }

  function resourceCategoryItemLabel(groupId, category) {
    const resource = state.groupResources.get(String(groupId));
    if (!resource) return "按需加载";
    if (resource.loading && !resource.loaded) return "正在加载";
    if (resource.error && !resource.loaded) return "加载失败";
    const total = Object.entries(RESOURCE_TYPE_META)
      .filter(([, meta]) => meta.category === category)
      .reduce((sum, [, meta]) => sum + (Array.isArray(resource[meta.property]) ? resource[meta.property].length : 0), 0);
    return `${total} 条`;
  }

  function resourceEntryState(groupId, type, id) {
    if (resourcePendingCreate(groupId, type, id)) return `<span class="badge info" data-resource-form-state>待新增</span>`;
    if (state.resourceFormDrafts.has(resourceEditDraftKey(groupId, type, id))) {
      return `<span class="badge warning" data-resource-form-state>已修改</span>`;
    }
    return `<span class="badge resource-clean-state" data-resource-form-state>已保存</span>`;
  }

  function pendingDeletionRow(group, type, item, label) {
    return `
      <div class="resource-row pending-delete" data-pending-resource-delete="${attr(resourceOperationKey(group.id, type, item.id ?? item.user_id))}">
        <span>${escapeHtml(label)}<small>将在点击“保存全部”后删除</small></span>
        <button class="text-button" type="button" data-action="undo-delete-group-resource" data-group-id="${attr(group.id)}" data-resource-type="${attr(type)}" data-resource-id="${attr(item.id ?? item.user_id)}">${icon("undo-2")}撤销</button>
      </div>`;
  }

  function renderKeywordReplyRow(group, item) {
    if (resourcePendingDelete(group.id, "keyword-replies", item.id)) {
      return pendingDeletionRow(group, "keyword-replies", item, `关键词回复：${item.keyword}`);
    }
    const pendingCreate = resourcePendingCreate(group.id, "keyword-replies", item.id);
    return `
      <form class="rule-resource-form entry-edit-form${pendingCreate ? " pending-create" : ""}" data-entry-edit-form="keyword-replies" data-group-id="${attr(group.id)}" data-entry-id="${attr(item.id)}">
        <div class="resource-form-head"><strong>${escapeHtml(item.keyword || "新关键词")}</strong>${resourceEntryState(group.id, "keyword-replies", item.id)}</div>
        <select name="match_type" aria-label="匹配方式">
          <option value="contains"${item.match_type === "contains" ? " selected" : ""}>包含</option>
          <option value="exact"${item.match_type === "exact" ? " selected" : ""}>完全匹配</option>
          <option value="regex"${item.match_type === "regex" ? " selected" : ""}>正则</option>
        </select>
        <input name="keyword" maxlength="255" value="${attr(item.keyword)}" aria-label="关键词" required>
        <textarea name="reply_text" maxlength="4000" rows="3" aria-label="回复内容" required>${escapeHtml(item.reply_text)}</textarea>
        <textarea name="buttons_text" maxlength="30000" rows="2" aria-label="内联按钮" placeholder="按钮名 | url/copy/share/dismiss | 内容 | 行号 | primary/success/danger（可选）；内容中 | 写成 \\|">${escapeHtml(templateButtonsToText(item.buttons))}</textarea>
        <label class="compact-check"><input name="pin_message" type="checkbox"${item.pin_message ? " checked" : ""}><span>置顶</span></label>
        <label class="compact-check"><input name="auto_delete" type="checkbox"${item.auto_delete ? " checked" : ""}><span>自动删除</span></label>
        <label class="compact-check"><input name="disable_link_preview" type="checkbox"${item.disable_link_preview !== false ? " checked" : ""}><span>关闭链接预览</span></label>
        <label class="compact-check"><input name="enabled" type="checkbox"${item.enabled !== false ? " checked" : ""}><span>启用</span></label>
        <button class="mini-icon-button danger" type="button" data-action="delete-group-resource" data-group-id="${attr(group.id)}" data-resource-type="keyword-replies" data-resource-id="${attr(item.id)}" aria-label="${pendingCreate ? "移除待新增项" : "标记删除"}" title="${pendingCreate ? "移除待新增项" : "标记删除"}">${icon("trash-2")}</button>
      </form>`;
  }

  function renderScheduledMessageRow(group, item) {
    if (resourcePendingDelete(group.id, "scheduled-messages", item.id)) {
      return pendingDeletionRow(group, "scheduled-messages", item, `定时消息：${item.text}`);
    }
    const pendingCreate = resourcePendingCreate(group.id, "scheduled-messages", item.id);
    return `
      <form class="rule-resource-form entry-edit-form${pendingCreate ? " pending-create" : ""}" data-entry-edit-form="scheduled-messages" data-group-id="${attr(group.id)}" data-entry-id="${attr(item.id)}">
        <div class="resource-form-head"><strong>${item.schedule_type === "interval" ? "固定间隔" : "每天定时"}</strong>${resourceEntryState(group.id, "scheduled-messages", item.id)}</div>
        <textarea name="text" maxlength="4000" rows="3" aria-label="消息内容" required>${escapeHtml(item.text)}</textarea>
        <textarea name="buttons_text" maxlength="30000" rows="2" aria-label="内联按钮" placeholder="按钮名 | url/copy/share/dismiss | 内容 | 行号；内容中 | 写成 \\|">${escapeHtml(templateButtonsToText(item.buttons))}</textarea>
        <select name="schedule_type" aria-label="定时方式">
          <option value="daily"${item.schedule_type === "daily" ? " selected" : ""}>每天定时</option>
          <option value="interval"${item.schedule_type === "interval" ? " selected" : ""}>固定间隔</option>
        </select>
        <input name="schedule_time" type="time" value="${attr(item.schedule_time || "09:00")}" aria-label="发送时间" title="每天定时的发送时间">
        <input name="interval_minutes" type="number" min="5" max="10080" step="1" value="${attr(item.interval_minutes || 60)}" aria-label="间隔分钟" title="固定间隔的分钟数" required>
        <label class="compact-check"><input name="pin_message" type="checkbox"${item.pin_message ? " checked" : ""}><span>置顶</span></label>
        <label class="compact-check"><input name="unpin_previous" type="checkbox"${item.unpin_previous ? " checked" : ""}><span>取消上次置顶</span></label>
        <label class="compact-check"><input name="auto_delete" type="checkbox"${item.auto_delete ? " checked" : ""}><span>自动删除</span></label>
        <label class="compact-check"><input name="disable_link_preview" type="checkbox"${item.disable_link_preview !== false ? " checked" : ""}><span>关闭链接预览</span></label>
        <label class="compact-check"><input name="enabled" type="checkbox"${item.enabled !== false ? " checked" : ""}><span>启用</span></label>
        <button class="mini-icon-button danger" type="button" data-action="delete-group-resource" data-group-id="${attr(group.id)}" data-resource-type="scheduled-messages" data-resource-id="${attr(item.id)}" aria-label="${pendingCreate ? "移除待新增项" : "标记删除"}" title="${pendingCreate ? "移除待新增项" : "标记删除"}">${icon("trash-2")}</button>
      </form>`;
  }

  const RULE_SCAN_SCOPES = ["message", "message+quote", "message+vision", "message+quote+vision"];

  function ruleScanScope(rule) {
    const value = String(rule?.scan_scope || "message");
    return RULE_SCAN_SCOPES.includes(value) ? value : "message";
  }

  function renderRuleRow(group, rule) {
    if (resourcePendingDelete(group.id, "rules", rule.id)) {
      return pendingDeletionRow(group, "rules", rule, `群规：${rule.pattern}`);
    }
    const pendingCreate = resourcePendingCreate(group.id, "rules", rule.id);
    return `
      <form class="rule-resource-form${pendingCreate ? " pending-create" : ""}" data-rule-edit-form data-group-id="${attr(group.id)}" data-rule-id="${attr(rule.id)}">
        <div class="resource-form-head"><strong>群规</strong>${resourceEntryState(group.id, "rules", rule.id)}</div>
        <select name="rule_type" aria-label="群规类型">
          <option value="keyword"${rule.rule_type === "keyword" ? " selected" : ""}>关键词</option>
          <option value="regex"${rule.rule_type === "regex" ? " selected" : ""}>正则</option>
          <option value="llm"${rule.rule_type === "llm" ? " selected" : ""}>语义</option>
        </select>
        <textarea name="pattern" maxlength="1000" rows="2" aria-label="群规内容" required>${escapeHtml(rule.pattern)}</textarea>
        <select name="scan_scope" aria-label="扫描范围" title="正则/关键词规则扫描哪些内容；语义规则始终看完整文本">
          <option value="message"${ruleScanScope(rule) === "message" ? " selected" : ""}>仅本人正文</option>
          <option value="message+quote"${ruleScanScope(rule) === "message+quote" ? " selected" : ""}>正文+引用</option>
          <option value="message+vision"${ruleScanScope(rule) === "message+vision" ? " selected" : ""}>正文+图片描述</option>
          <option value="message+quote+vision"${ruleScanScope(rule) === "message+quote+vision" ? " selected" : ""}>正文+引用+图片描述</option>
        </select>
        <select name="action" aria-label="命中动作">
          <option value="warn"${rule.action === "warn" ? " selected" : ""}>警告</option>
          <option value="delete"${rule.action === "delete" ? " selected" : ""}>删消息</option>
          <option value="ban"${rule.action === "ban" ? " selected" : ""}>封禁</option>
        </select>
        <label class="compact-check"><input name="enabled" type="checkbox"${rule.enabled !== false ? " checked" : ""}><span>启用</span></label>
        <button class="mini-icon-button danger" type="button" data-action="delete-group-resource" data-group-id="${attr(group.id)}" data-resource-type="rules" data-resource-id="${attr(rule.id)}" aria-label="${pendingCreate ? "移除待新增项" : "标记删除"}" title="${pendingCreate ? "移除待新增项" : "标记删除"}">${icon("trash-2")}</button>
      </form>`;
  }

  function renderMemoryRow(group, item) {
    if (resourcePendingDelete(group.id, "memories", item.id)) {
      return pendingDeletionRow(group, "memories", item, `永久记忆：${item.content}`);
    }
    const pendingCreate = resourcePendingCreate(group.id, "memories", item.id);
    return `
      <form class="rule-resource-form memory-edit-form${pendingCreate ? " pending-create" : ""}" data-memory-edit-form data-group-id="${attr(group.id)}" data-memory-id="${attr(item.id)}">
        <div class="resource-form-head"><strong>永久记忆</strong>${resourceEntryState(group.id, "memories", item.id)}</div>
        <textarea name="content" maxlength="4000" rows="3" aria-label="永久记忆内容" required>${escapeHtml(item.content)}</textarea>
        <button class="mini-icon-button danger" type="button" data-action="delete-group-resource" data-group-id="${attr(group.id)}" data-resource-type="memories" data-resource-id="${attr(item.id)}" aria-label="${pendingCreate ? "移除待新增项" : "标记删除"}" title="${pendingCreate ? "移除待新增项" : "标记删除"}">${icon("trash-2")}</button>
      </form>`;
  }

  function resourcePanelShell(group, category, contentMarkup) {
    const resource = state.groupResources.get(String(group.id));
    const refreshing = Boolean(resource?.refreshing);
    return `
      <div class="group-resource-panel" data-group-resource-panel="${attr(group.id)}" data-resource-category="${attr(category)}">
        <div class="resource-panel-toolbar">
          <span class="field-hint">${category === "member-lists" ? "此处是即时管理操作，按钮会明确标注动作。" : "新增、编辑和删除会先成为草稿，最后统一保存。"}</span>
          <button class="secondary-button" type="button" data-action="load-group-resources" data-group-id="${attr(group.id)}"${refreshing ? " disabled" : ""}>${refreshing ? `<span class="spinner spinner-small"></span>` : icon(resource?.loaded ? "refresh-cw" : "download")}${resource?.loaded ? "重新查看" : "加载数据"}</button>
        </div>
        ${resource?.loaded && resource.error ? `<div class="notice warning resource-refresh-notice">${icon("triangle-alert")}<span>${escapeHtml(resource.error)}；已保留其余已加载数据。</span></div>` : ""}
        ${resource?.serverView && category !== "member-lists" ? `<details class="notice" data-resource-server-view><summary>服务器重新查看结果（只读；草稿保留，可能已生效，请勿盲目重提）</summary><pre>${escapeHtml(JSON.stringify(Object.fromEntries(Object.entries(resource.serverView).filter(([key]) => category === "automation" ? ["keyword_replies", "scheduled_messages"].includes(key) : ["rules", "memories"].includes(key))), null, 2))}</pre></details>` : ""}
        ${contentMarkup}
      </div>`;
  }

  const memoryTransparency = new Map();
  const memoryLabels = { facts: "长期事实", searches: "联网检索留档", summary: "群摘要" };
  function memoryReadState(groupId, section) {
    const key = `${groupId}:${section}`;
    // ``filters`` is the **applied** query (it produced ``data``/``next_cursor``);
    // ``draft`` is what is currently typed in the form and never rides a cursor.
    if (!memoryTransparency.has(key)) memoryTransparency.set(key, { attempted: false, loading: false, data: null, error: "", filters: {}, draft: null, applied: null, queued: false, requestCount: 0 });
    return memoryTransparency.get(key);
  }
  function memoryFilterValues(form) {
    return Object.fromEntries([...new FormData(form)].filter(([, value]) => value !== ""));
  }
  function memoryFiltersEqual(left, right) {
    const a = left || {}, b = right || {}, keys = new Set([...Object.keys(a), ...Object.keys(b)]);
    for (const key of keys) if (String(a[key] ?? "") !== String(b[key] ?? "")) return false;
    return true;
  }
  function memoryDraftPending(read) {
    return Boolean(read.draft) && !memoryFiltersEqual(read.draft, read.filters);
  }
  function memorySafeLink(value) {
    try { const url = new URL(value); return ["http:","https:"].includes(url.protocol) && !url.username && !url.password ? value : null; } catch (_) { return null; }
  }
  // C3：每条事实的编辑草稿与"受控展开原文"状态，按 group:fact 隔离；
  // 换群/撤权/迟到回包都不会把 A 群的原文或草稿显示到 B 群。
  const memoryFactEdits = new Map();
  const memoryFactSources = new Map();
  const memorySourceLabels = { ok: "来源原文（本群受控展开，已按敏感规则脱敏）", deleted: "来源已被确认删除，不提供原文", missing: "没有可展开的来源归档", not_visible: "这条事实当前不对外可见", unverifiable: "来源当前无法验证，未提供原文" };
  function memoryFactKey(groupId, factId) {
    return `${groupId}:${factId}`;
  }
  function memoryFactEditState(groupId, factId) {
    const key = memoryFactKey(groupId, factId);
    if (!memoryFactEdits.has(key)) memoryFactEdits.set(key, { open: false, text: null, revision: null, saving: false, error: "", conflict: "", conflictRevision: null });
    return memoryFactEdits.get(key);
  }
  function memoryFactSourceState(groupId, factId) {
    const key = memoryFactKey(groupId, factId);
    if (!memoryFactSources.has(key)) memoryFactSources.set(key, { open: false, loading: false, error: "", data: null, request: 0 });
    return memoryFactSources.get(key);
  }
  function clearMemoryFactExtras(groupId) {
    for (const key of [...memoryFactEdits.keys()]) if (key.startsWith(`${groupId}:`)) memoryFactEdits.delete(key);
    for (const key of [...memoryFactSources.keys()]) if (key.startsWith(`${groupId}:`)) memoryFactSources.delete(key);
  }
  function clearMemoryTransparencyGroup(groupId, { global = false } = {}) {
    if (global) memoryTransparency.clear();
    for (const part of Object.keys(memoryLabels)) {
      const old = memoryReadState(groupId, part);
      old.data = null; old.applied = null; old.token = null; old.loading = false; old.queued = false; old.error = "管理权限已失效，已清理旧展示";
    }
    clearMemoryFactExtras(groupId);
  }
  function memoryFactItem(groupId, factId) {
    const items = memoryReadState(groupId, "facts").data?.items;
    if (!Array.isArray(items)) return null;
    return items.find(item => String(item.id) === String(factId)) || null;
  }
  function renderMemoryFact(group, item) {
    const edit = memoryFactEditState(group.id, item.id);
    const source = memoryFactSourceState(group.id, item.id);
    const sourceState = { verified: "归档已验证", unavailable: "来源不可用", unverifiable: "证据不可验证" }[item.source_state] || "来源未知";
    const sourceLink = item.source_url && memorySafeLink(item.source_url) ? `<a href="${attr(item.source_url)}" target="_blank" rel="noopener noreferrer">已验证来源消息</a>` : "";
    const corrected = item.corrected ? ` · 人工纠错${item.corrected_at ? `（${escapeHtml(item.corrected_at)}）` : ""}` : "";
    const editForm = edit.open ? `<form data-memory-edit="${attr(item.id)}" data-group-id="${attr(group.id)}"><label>修改这条事实的正文<textarea name="text" maxlength="200" aria-label="新的正文">${escapeHtml(edit.text ?? item.fact_text)}</textarea></label><p class="field-hint">当前修订 ${escapeHtml(item.revision)}；保存后旧版本保留为“已被替换”，聊天原文与来源关系不变。</p>${edit.error ? `<p role="alert" class="notice warning">${escapeHtml(edit.error)}${edit.conflict ? `（冲突：${escapeHtml(edit.conflict)}${edit.conflictRevision != null ? ` · 当前修订 ${escapeHtml(edit.conflictRevision)}` : ""}）` : ""}</p>` : ""}<button type="submit" class="text-button"${edit.saving ? " disabled" : ""}>确认修改</button> <button type="button" class="text-button" data-action="memory-fact-edit-cancel" data-group-id="${attr(group.id)}" data-fact-id="${attr(item.id)}">取消</button></form>` : "";
    const sourcePanel = source.open ? `<div class="memory-read-source" data-memory-fact-source="${attr(item.id)}">${source.loading ? '<p role="status">正在读取原文…</p>' : source.error ? `<p role="alert" class="notice warning">${escapeHtml(source.error)}</p>` : source.data ? `<p>${escapeHtml(memorySourceLabels[source.data.status] || "来源状态未知")}${source.data.truncated ? "（已截断）" : ""}</p>${source.data.available ? `<p class="memory-read-text">${escapeHtml(source.data.text)}</p>` : ""}${source.data.available && source.data.url && memorySafeLink(source.data.url) ? `<p><a href="${attr(source.data.url)}" target="_blank" rel="noopener noreferrer">在 Telegram 中打开来源消息</a></p>` : ""}` : ""}</div>` : "";
    return `<article class="notice memory-read-fact"><p class="memory-read-text">${escapeHtml(item.fact_text)}</p><small>#${escapeHtml(item.id)} · 修订 ${escapeHtml(item.revision)} · 对象 ${escapeHtml(item.subject_user_id)} · ${escapeHtml(item.category)} · 置信度 ${escapeHtml(item.confidence)} · 确认 ${escapeHtml(item.confirm_count)} 次${corrected}<br>首次 ${escapeHtml(item.first_seen_at || "未知")} · 最后 ${escapeHtml(item.last_confirmed_at || "未知")}<br>出处 ${escapeHtml(item.source_kind)} · ${sourceLink ? `${sourceLink} · ` : ""}${escapeHtml(sourceState)}</small><p class="memory-read-actions"><button type="button" class="text-button" data-action="memory-fact-edit" data-group-id="${attr(group.id)}" data-fact-id="${attr(item.id)}">编辑</button> <button type="button" class="text-button" data-action="memory-fact-source" data-group-id="${attr(group.id)}" data-fact-id="${attr(item.id)}">${source.open ? "收起原文" : "展开原文"}</button></p>${editForm}${sourcePanel}</article>`;
  }
  function toggleMemoryFactEdit(groupId, factId) {
    const edit = memoryFactEditState(groupId, factId);
    if (edit.open) { edit.open = false; renderContent(); return; }
    const item = memoryFactItem(groupId, factId);
    if (!item) { edit.error = "列表已变化，请先刷新后再修改。"; edit.open = true; renderContent(); return; }
    edit.open = true; edit.error = ""; edit.conflict = ""; edit.conflictRevision = null;
    edit.revision = Number.isInteger(item.revision) ? item.revision : null;
    edit.text = typeof item.fact_text === "string" ? item.fact_text : "";
    renderContent();
  }
  async function loadMemoryFactSource(groupId, factId) {
    const source = memoryFactSourceState(groupId, factId);
    if (source.open) {
      // 收起：读取还在路上也要立刻清掉正文，并让迟到回包作废（绝不重现已收起的文本）。
      source.open = false; source.loading = false; source.data = null; source.error = ""; source.request += 1;
      renderContent(); return;
    }
    const token = source.request + 1; source.request = token;
    source.open = true; source.loading = true; source.error = ""; source.data = null; renderContent();
    try {
      const result = await apiFetch(`/api/v1/groups/${encodeURIComponent(groupId)}/memory-transparency/facts/${encodeURIComponent(factId)}/source`);
      if (source.request !== token) return;
      if (String(result?.group_id) !== String(groupId) || String(result?.fact_id) !== String(factId) || !["ok","deleted","missing","not_visible","unverifiable"].includes(result?.status)) throw new Error("来源回包不可验证");
      source.data = result;
    } catch (error) {
      if (source.request !== token) return;
      source.data = null;
      if (error instanceof ApiError && [401,403,404].includes(error.status)) {
        memoryFactSources.delete(memoryFactKey(groupId, factId));
        clearMemoryTransparencyGroup(groupId, { global: error.status === 401 });
        return;
      }
      source.error = error.message || "来源读取失败";
    } finally {
      if (source.request === token) { source.loading = false; renderContent(); }
    }
  }
  async function saveMemoryFactCorrection(groupId, factId) {
    const edit = memoryFactEditState(groupId, factId);
    if (edit.saving) return;
    const text = String(edit.text ?? "").trim();
    if (!text) { edit.error = "新内容不能是空的。"; renderContent(); return; }
    if (!Number.isInteger(edit.revision)) { edit.error = "缺少修订号，请先刷新列表再修改。"; renderContent(); return; }
    edit.saving = true; edit.error = ""; edit.conflict = ""; edit.conflictRevision = null; renderContent();
    try {
      const result = await apiFetch(`/api/v1/groups/${encodeURIComponent(groupId)}/memory-transparency/facts/${encodeURIComponent(factId)}/correction`, { method: "POST", body: JSON.stringify({ text, expected_revision: edit.revision }) });
      if (result?.status === "corrected" && result.confirmed === true) {
        edit.open = false; edit.text = null; edit.saving = false;
        memoryFactSources.delete(memoryFactKey(groupId, factId));
        // 按实际返回的新稳定 ID / 修订刷新列表（不本地伪造一行）。
        await loadMemoryTransparency(groupId, "facts");
        return;
      }
      if (result?.status === "unchanged") { edit.error = result.message || "新内容和当前内容一致，没有改动。"; }
      else throw new Error("纠错回包不可验证");
    } catch (error) {
      const envelope = error instanceof ApiError ? error.body?.error || {} : {};
      const details = envelope.details && typeof envelope.details === "object" ? envelope.details : {};
      if (error instanceof ApiError && error.status === 409) {
        edit.conflict = String(details.conflict || "conflict");
        edit.conflictRevision = Number.isInteger(details.previous_revision) ? details.previous_revision : null;
        edit.error = envelope.message || error.message || "这条事实已经变化，请重看后再确认。";
      } else if (error instanceof ApiError && error.status === 503) {
        // 未确认绝不承诺"没有效果"，也绝不自动重试：保留草稿等人工显式重看。
        edit.error = envelope.message || error.message || "未能确认这次修改是否已生效，请显式重看后再决定。";
      } else if (error instanceof ApiError && [401,403].includes(error.status)) {
        memoryFactEdits.delete(memoryFactKey(groupId, factId));
        clearMemoryTransparencyGroup(groupId, { global: error.status === 401 });
        return;
      } else edit.error = envelope.message || error.message || "保存失败";
    } finally {
      edit.saving = false; renderContent();
    }
  }
  function renderMemoryTransparency(group) {
    return Object.entries(memoryLabels).map(([section, label]) => {
      const read = memoryReadState(group.id, section);
      // Keep the unsubmitted draft on screen; only the applied filters drive requests.
      const filters = read.draft ?? read.filters;
      const pending = section !== "summary" && memoryDraftPending(read);
      const options = section === "facts" ? ["identity", "preference", "relationship", "event", "taboo", "skill", "other"] : ["price", "news", "fact", "unknown"];
      const filterName = section === "facts" ? "category" : "kind";
      let body = "";
      if (section === "summary" && read.data) {
        const data = read.data, doc = data.document;
        const labels = { valid: "有效派生摘要（低信任资料，不代表当前事实）", invalid: "摘要已失效，正文不展示", empty: "暂无摘要", disabled: "摘要已禁用" };
        body = `<p>${escapeHtml(labels[data.status] || "状态未知")}</p>${doc ? `<p>版本 ${escapeHtml(doc.version)} · 生成 ${escapeHtml(doc.generated_at || "未知")} · 覆盖行 ${escapeHtml(doc.covered_from_id)}—${escapeHtml(doc.covered_through_id)} · ${escapeHtml(doc.covered_count)} 条 · 来源版本 ${escapeHtml(doc.source_revision)} · 范围 ${escapeHtml(doc.covered_from_key || "未知")}—${escapeHtml(doc.covered_through_key || "未知")} · ${doc.source_truncated ? "源已截断" : "覆盖范围资料"}</p>${data.status === "valid" ? `<p class="memory-read-text">${escapeHtml(doc.summary)}</p>` : ""}` : ""}`;
      } else if (read.data) {
        body = read.data.items.map(item => section === "facts"
          ? renderMemoryFact(group, item)
          : `<article class="notice memory-read-search"><p class="memory-read-text">${escapeHtml(item.query)}</p><p class="memory-read-text">${escapeHtml(item.digest)}</p><small>${escapeHtml(item.created_at || "时间未知")} · ${escapeHtml(item.kind)} · ${escapeHtml(item.outcome)} · ${escapeHtml({fresh:"窗口内资料（非实时现价）",stale:"陈旧，可能已过期，非现价",unknown:"时效未知"}[item.freshness] || "时效未知")} · 窗口 ${escapeHtml(item.freshness_hours ?? "未知")} 小时</small><p class="memory-read-sources">${(item.sources || []).filter(source => memorySafeLink(source.url)).map(source => `<a href="${attr(source.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(source.title || source.url)}</a>`).join(" · ")}</p></article>`).join("") || '<p>暂无可展示记录。</p>';
        if (section === "searches") body += `<p class="field-hint">生效留存 ${escapeHtml(read.data.retention_days)} 天；已知时间按留存筛选；未知时间单独标为未知。</p>`;
      }
      return `<details class="memory-read-section" data-memory-read-section="${section}" data-group-id="${attr(group.id)}"${read.open ? " open" : ""}><summary>${label}</summary>
        ${section !== "summary" ? `<form data-memory-filter="${section}" data-group-id="${attr(group.id)}"><label>类型<select name="${filterName}" aria-label="${label}类型"><option value="">全部</option>${options.map(value => `<option value="${value}"${filters[filterName] === value ? " selected" : ""}>${value}</option>`).join("")}</select></label>${section === "facts" ? `<label>对象 ID（0 为群整体）<input name="subject" type="text" inputmode="numeric" value="${attr(filters.subject || "")}" aria-label="事实对象 ID"></label>` : ""}<button type="submit" class="text-button">筛选</button></form><p class="field-hint" data-memory-draft-hint${pending ? "" : " hidden"}>筛选草稿尚未提交：列表与“下一页”仍按上一次点“筛选”的条件读取，点“筛选”后才会生效。</p>` : ""}
        <button type="button" class="text-button" data-action="memory-read-refresh" data-memory-section="${section}" data-group-id="${attr(group.id)}"${read.loading ? " disabled" : ""}>刷新</button>
        ${read.loading ? '<p role="status">正在读取…</p>' : ""}${read.error ? `<p role="alert" class="notice warning">${escapeHtml(read.error)}${read.data ? "；旧资料已保留但已陈旧，不代表当前状态。" : ""}</p>` : ""}
        ${!read.attempted ? '<p>展开后按需读取，仅本群数据。</p>' : body}
        ${read.data?.has_more ? `<button type="button" class="text-button" data-action="memory-read-more" data-memory-section="${section}" data-group-id="${attr(group.id)}"${read.loading ? " disabled" : ""}>下一页</button>` : ""}
      </details>`;
    }).join("");
  }
  async function loadMemoryTransparency(groupId, section, more = false) {
    const read = memoryReadState(groupId, section);
    if (read.loading) { if (more !== true) read.queued = true; return; }
    const requested = { ...read.filters };
    // A cursor may only be reused with the exact filters that produced it.
    const cursor = more && read.data?.next_cursor && memoryFiltersEqual(read.applied || {}, requested) ? read.data.next_cursor : null;
    const query = new URLSearchParams(requested);
    const token = Symbol("memory-read"); read.token = token;
    read.loading = true; read.attempted = true; read.requestCount += 1;
    read.error = ""; renderContent();
    if (section !== "summary") query.set("limit", "20");
    if (cursor) query.set("cursor", cursor);
    try {
      const result = await apiFetch(`/api/v1/groups/${encodeURIComponent(groupId)}/memory-transparency/${section}?${query}`);
      if (read.token !== token) return;
      if (result?.ok !== true || String(result.group_id) !== String(groupId) || result.section !== section || (section !== "summary" && (!Array.isArray(result.items) || typeof result.has_more !== "boolean"))) throw new Error("记忆读取回包不可验证");
      if (section === "summary" && (!['valid','invalid','empty','disabled'].includes(result.status)
        || (['valid','invalid'].includes(result.status) && (!result.document || !Number.isInteger(result.document.version)))
        || (result.status === 'valid' && typeof result.document.summary !== 'string'))) throw new Error("摘要读取回包不可验证");
      read.data = result;
      read.applied = requested;
    } catch (error) {
      if (read.token !== token) return;
      if (error instanceof ApiError && [401,403,404].includes(error.status)) {
        clearMemoryTransparencyGroup(groupId, { global: error.status === 401 });
        // The clear nulls the read token, so the finally below cannot render; do it
        // here so withdrawn rows/原文 disappear immediately instead of staying on screen.
        renderContent();
      } else read.error = error.message || "读取失败";
    } finally {
      if (read.token === token) {
        read.loading = false; renderContent();
        // A request that arrived while this one was in flight is honoured once, with
        // the filters it was asked for — never merged into the finished query.
        if (read.queued) { read.queued = false; await loadMemoryTransparency(groupId, section); }
      }
    }
  }
  content.addEventListener("toggle", event => {
    const details = event.target;
    if (!details.isConnected || !details.matches?.("[data-memory-read-section]")) return;
    const read = memoryReadState(details.dataset.groupId, details.dataset.memoryReadSection);
    read.open = details.open;
    if (read.open && !read.attempted) loadMemoryTransparency(details.dataset.groupId, details.dataset.memoryReadSection);
  }, true);
  content.addEventListener("click", event => {
    const factAction = event.target.closest('[data-action="memory-fact-edit"], [data-action="memory-fact-edit-cancel"], [data-action="memory-fact-source"]');
    if (factAction) {
      const groupId = factAction.dataset.groupId, factId = factAction.dataset.factId;
      if (!groupId || !factId) return;
      if (factAction.dataset.action === "memory-fact-edit") toggleMemoryFactEdit(groupId, factId);
      else if (factAction.dataset.action === "memory-fact-source") loadMemoryFactSource(groupId, factId);
      else { memoryFactEditState(groupId, factId).open = false; renderContent(); }
      return;
    }
    const button = event.target.closest('[data-action="memory-read-refresh"], [data-action="memory-read-more"]');
    if (!button) return;
    loadMemoryTransparency(button.dataset.groupId, button.dataset.memorySection, button.dataset.action === "memory-read-more");
  });

  function renderGroupResourcePanel(group, category) {
    const resource = state.groupResources.get(String(group.id));
    if (!resource) {
      return resourcePanelShell(group, category, `
        <div class="resource-empty-state">${icon(category === "automation" ? "timer" : category === "rules-memory" ? "book-open-check" : "contact-round")}
          <div><strong>尚未加载此群数据</strong><span>点击右上角“加载数据”后即可管理。</span></div>
        </div>`);
    }
    if (resource.loading && !resource.loaded) {
      return resourcePanelShell(group, category, `<div class="loading-inline resource-loading"><span class="spinner spinner-small"></span><span>正在加载群管理数据</span></div>`);
    }
    if (resource.error && !resource.loaded) {
      return resourcePanelShell(group, category, `<div class="notice danger">${icon("circle-x")}<span>${escapeHtml(resource.error)}</span></div>`);
    }
    if (category === "automation") return resourcePanelShell(group, category, renderAutomationResources(group, resource));
    if (category === "rules-memory") return resourcePanelShell(group, category, renderRulesMemoryResources(group, resource));
    return resourcePanelShell(group, category, renderMemberListResources(group, resource));
  }

  function resourceCardHead(iconName, title, description, count) {
    return `<div class="resource-card-head"><span class="resource-card-icon">${icon(iconName)}</span><div><h4>${escapeHtml(title)}</h4><p>${escapeHtml(description)}</p></div><span class="badge info">${count} 条</span></div>`;
  }

  function renderAutomationResources(group, resource) {
    const listKey = type => `group:${group.id}:${type}`;
    return `<div class="resource-category-grid">
      <section class="resource-category-card accent-blue">
        ${resourceCardHead("message-square-text", "关键词回复", "命中关键词后直接发送固定内容", resource.keyword_replies.length)}
        <form class="inline-resource-form template-resource-form resource-create-form" data-resource-form="keyword-replies" data-save-scope="deferred" data-group-id="${attr(group.id)}">
          <div class="resource-create-head"><strong>新建关键词回复</strong><span>填写后加入草稿列表</span></div>
          <select name="match_type" aria-label="匹配方式"><option value="contains">包含</option><option value="exact">完全匹配</option><option value="regex">正则</option></select>
          <input name="keyword" maxlength="255" placeholder="关键词" aria-label="关键词" required>
          <textarea name="reply_text" maxlength="4000" rows="3" placeholder="回复内容（支持换行和 Markdown）" aria-label="回复内容" required></textarea>
          <textarea name="buttons_text" maxlength="30000" rows="2" placeholder="可选内联按钮：按钮名 | url/copy/share/dismiss | 内容 | 行号 | primary/success/danger（可选）；| 写成 \\|" aria-label="内联按钮"></textarea>
          <label class="compact-check"><input name="pin_message" type="checkbox"><span>置顶</span></label>
          <label class="compact-check"><input name="auto_delete" type="checkbox" checked><span>自动删除</span></label>
          <label class="compact-check"><input name="disable_link_preview" type="checkbox" checked><span>关闭链接预览</span></label>
          <button class="secondary-button resource-stage-button" type="submit">${icon("plus")}加入待保存列表</button>
        </form>
        <p class="field-hint">命中后跳过 AI；内容支持 Markdown。${templateButtonStyleLegend()}</p>
        <div class="resource-entry-list">${paginatedRows(resource.keyword_replies, listKey("keyword-replies"), item => renderKeywordReplyRow(group, item), "暂无关键词回复")}</div>
      </section>
      <section class="resource-category-card accent-violet">
        ${resourceCardHead("calendar-clock", "定时消息", "按每日时间或固定间隔自动发送", resource.scheduled_messages.length)}
        <form class="inline-resource-form template-resource-form resource-create-form" data-resource-form="scheduled-messages" data-save-scope="deferred" data-group-id="${attr(group.id)}">
          <div class="resource-create-head"><strong>新建定时消息</strong><span>填写后加入草稿列表</span></div>
          <textarea name="text" maxlength="4000" rows="3" placeholder="消息内容（支持换行和 Markdown）" aria-label="定时消息内容" required></textarea>
          <textarea name="buttons_text" maxlength="30000" rows="2" placeholder="可选内联按钮：按钮名 | url/copy/share/dismiss | 内容 | 行号；| 写成 \\|" aria-label="内联按钮"></textarea>
          <select name="schedule_type" aria-label="定时方式"><option value="daily">每天定时</option><option value="interval">固定间隔</option></select>
          <input name="schedule_time" type="time" value="09:00" aria-label="发送时间" title="每天定时的发送时间">
          <input name="interval_minutes" type="number" min="5" max="10080" step="1" value="60" aria-label="间隔分钟" title="固定间隔的分钟数" required>
          <label class="compact-check"><input name="pin_message" type="checkbox"><span>置顶</span></label>
          <label class="compact-check"><input name="auto_delete" type="checkbox"><span>自动删除</span></label>
          <label class="compact-check"><input name="disable_link_preview" type="checkbox" checked><span>关闭链接预览</span></label>
          <button class="secondary-button resource-stage-button" type="submit">${icon("plus")}加入待保存列表</button>
        </form>
        <p class="field-hint">每天定时按 Asia/Shanghai 时间触发；固定间隔最短 5 分钟。</p>
        <div class="resource-entry-list">${paginatedRows(resource.scheduled_messages, listKey("scheduled-messages"), item => renderScheduledMessageRow(group, item), "暂无定时消息")}</div>
      </section>
    </div>`;
  }

  function renderRulesMemoryResources(group, resource) {
    const listKey = type => `group:${group.id}:${type}`;
    return `<div class="resource-category-grid two-column">
      <section class="resource-category-card accent-orange">
        ${resourceCardHead("shield-alert", "群规", "关键词、正则或语义规则及命中动作", resource.rules.length)}
        <form class="inline-resource-form multiline-resource-form resource-create-form" data-resource-form="rules" data-save-scope="deferred" data-group-id="${attr(group.id)}">
          <div class="resource-create-head"><strong>新建群规</strong><span>填写后加入草稿列表</span></div>
          <select name="rule_type" aria-label="群规类型"><option value="keyword">关键词</option><option value="regex">正则</option><option value="llm">语义</option></select>
          <textarea name="pattern" maxlength="1000" rows="2" placeholder="规则内容" aria-label="群规内容" required></textarea>
          <select name="scan_scope" aria-label="扫描范围" title="正则/关键词规则扫描哪些内容；语义规则始终看完整文本">
            <option value="message">仅本人正文</option>
            <option value="message+quote">正文+引用</option>
            <option value="message+vision">正文+图片描述</option>
            <option value="message+quote+vision">正文+引用+图片描述</option>
          </select>
          <select name="action" aria-label="命中动作"><option value="warn">警告</option><option value="delete">删消息</option><option value="ban">封禁</option></select>
          <button class="secondary-button resource-stage-button" type="submit">${icon("plus")}加入待保存列表</button>
        </form>
        <div class="resource-entry-list">${paginatedRows(resource.rules, listKey("rules"), rule => renderRuleRow(group, rule), "暂无群规")}</div>
      </section>
      <section class="resource-category-card accent-green">
        ${resourceCardHead("brain", "永久记忆", "长期提供给 AI 的群组背景信息", resource.memories.length)}
        <form class="inline-resource-form multiline-resource-form resource-create-form" data-resource-form="memories" data-save-scope="deferred" data-group-id="${attr(group.id)}">
          <div class="resource-create-head"><strong>新建永久记忆</strong><span>填写后加入草稿列表</span></div>
          <textarea name="content" maxlength="4000" rows="2" placeholder="新增永久记忆" aria-label="永久记忆内容" required></textarea>
          <button class="secondary-button resource-stage-button" type="submit">${icon("plus")}加入待保存列表</button>
        </form>
        <div class="resource-entry-list">${paginatedRows(resource.memories, listKey("memories"), item => renderMemoryRow(group, item), "暂无永久记忆")}</div>
      </section>
    </div>`;
  }

  function renderMemberListResources(group, resource) {
    const listKey = type => `group:${group.id}:${type}`;
    const userLabel = item => {
      const name = String(item.display_name || item.username || `用户 ${item.user_id}`);
      const username = String(item.username || "").replace(/^@/, "");
      return `${name}${username && name !== `@${username}` ? ` · @${username}` : ""} · ${item.user_id}`;
    };
    const actionLabel = type => ({ warnings: "清零警告", bans: "立即解封", "moderation-exemptions": "取消豁免", "reply-mutes": "取消静默" })[type] || "移除";
    const itemRows = (items, type, text) => paginatedRows(items, listKey(type), item => `
      <div class="resource-row">
        <span>${escapeHtml(text(item))}</span>
        ${(type !== "warnings" || !item.is_banned) ? `<button class="text-button danger immediate-action" type="button" data-action="delete-group-resource" data-group-id="${attr(group.id)}" data-resource-type="${attr(type)}" data-resource-id="${attr(item.id ?? item.user_id)}">${actionLabel(type)}</button>` : ""}
      </div>`, "暂无记录");
    return `<div class="member-resource-grid">
      <section class="resource-category-card compact accent-orange">
        ${resourceCardHead("triangle-alert", "警告记录", "查看累计警告并清零", resource.warnings.length)}
        ${itemRows(resource.warnings, "warnings", item => `${userLabel(item)} · ${item.count} 次${item.is_banned ? " · 已封禁" : ""}`)}
      </section>
      <section class="resource-category-card compact accent-red">
        ${resourceCardHead("user-x", "群内封禁", "立即执行 Telegram 封禁或解封", resource.bans.length)}
        ${userIdForm(group.id, "bans", "用户 ID", "立即封禁")}
        ${itemRows(resource.bans, "bans", item => `${userLabel(item)} · 已封禁`)}
      </section>
      <section class="resource-category-card compact accent-blue">
        ${resourceCardHead("shield-check", "AI 审核豁免", "跳过 AI 审核限制", resource.exemptions.length)}
        ${userIdForm(group.id, "moderation-exemptions", "用户 ID", "立即豁免")}
        ${itemRows(resource.exemptions, "moderation-exemptions", item => userLabel(item))}
      </section>
      <section class="resource-category-card compact accent-violet">
        ${resourceCardHead("message-square-off", "回复静默名单", "Bot 不再回复名单中的成员", resource.reply_mutes.length)}
        ${userIdForm(group.id, "reply-mutes", "用户 ID", "立即静默")}
        ${itemRows(resource.reply_mutes, "reply-mutes", item => userLabel(item))}
      </section>
    </div>`;
  }

  function userIdForm(groupId, type, placeholder, actionLabel) {
    return `<form class="inline-resource-form immediate-resource-form" data-resource-form="${type}" data-group-id="${attr(groupId)}">
      <input name="user_id" type="number" min="1" step="1" placeholder="${attr(placeholder)}" aria-label="${attr(placeholder)}" required>
      <button class="secondary-button danger-outline" type="submit">${icon("zap")} ${escapeHtml(actionLabel)}</button>
    </form>`;
  }

  function renderGroups() {
    if (state.groupsError) {
      return `
        ${pageHead("群组设置", "逐群覆盖全局行为。", `<button class="secondary-button" type="button" data-action="reload-groups">${icon("refresh-cw")}重试</button>`)}
        <div class="error-state">${icon("circle-x")}<p>${escapeHtml(state.groupsError)}</p></div>`;
    }
    return `
      ${pageHead("群组设置", "按群管理回复、模型 API、入群、权限、关键词回复、定时消息、群规和成员名单；所有配置统一保存。", `<button class="secondary-button" type="button" data-action="reload-groups"${state.groupSaving.size || state.reloadingGroups ? " disabled" : ""}>${state.reloadingGroups ? `<span class="spinner spinner-small"></span>刷新中` : `${icon("refresh-cw")}刷新群组`}</button>`)}
      <div class="group-toolbar">
        <div class="search-wrap">${icon("search")}<input id="group-search" class="search-input" type="search" placeholder="搜索群名或群 ID" aria-label="搜索群名或群 ID" autocomplete="off" value="${attr(state.groupSearch)}"></div>
        <span class="badge info">${state.groups.length} 个群组</span>
      </div>
      <div class="group-list">
        ${state.groups.map(renderGroupCard).join("") || `<div class="empty-state">${icon("users")}<p>暂无可管理群组</p></div>`}
      </div>
      ${state.groups.map(renderGroupCostSection).join("")}`;
  }

  function renderGroupCostSection(group) {
    const preview = state.presetPreview?.get(String(group.id));
    return `
      <section class="panel preset-section" data-cost-group="${attr(group.id)}">
        <header class="panel-header">
          <h3>${icon("wand-sparkles")}首次预设</h3>
          <span class="badge">先预览，再显式应用</span>
        </header>
        <div class="preset-grid" role="radiogroup" aria-label="选择预设">
          ${ONBOARDING_PRESETS.map(item => `
            <button class="preset-card${state.selectedPreset?.get(String(group.id)) === item.id ? " is-selected" : ""}"
                    type="button" role="radio"
                    aria-checked="${state.selectedPreset?.get(String(group.id)) === item.id ? "true" : "false"}"
                    data-action="select-preset" data-group-id="${attr(group.id)}" data-preset="${attr(item.id)}">
              <strong>${escapeHtml(item.title)}</strong>
              <span>${escapeHtml(item.hint)}</span>
            </button>`).join("")}
        </div>
        ${preview ? `
          <div class="preset-preview">
            <h4>预览：${escapeHtml(preview.title)}</h4>
            <ul>${(preview.patch || []).map(item => `
              <li><code>${escapeHtml(item.key)}</code> → ${escapeHtml(JSON.stringify(item.value))}<span class="preset-effect">${escapeHtml(item.effect)}</span></li>`).join("")}</ul>
            <ul class="preset-risks">${(preview.risks || []).map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
            ${(preview.pending_notes || []).length ? `
              <p class="field-hint">待准备：${escapeHtml(preview.pending_notes.join("；"))}</p>` : ""}
          </div>` : ""}
        <div class="button-row">
          <button class="secondary-button" type="button" data-action="preset-preview" data-group-id="${attr(group.id)}">
            ${icon("eye")}预览
          </button>
          <button class="primary-button" type="button" data-action="preset-apply" data-group-id="${attr(group.id)}"
                  ${preview ? "" : " disabled"}>
            ${icon("check")}应用预设
          </button>
        </div>
        ${costPanelView(group)}
      </section>`;
  }

  function accessRemoveLabel(type) {
    return ({
      "authorized-groups": "取消授权",
      admins: "移除管理员",
      "global-bans": "立即解封",
      "global-exemptions": "取消豁免",
    })[type] || "移除";
  }

  function accessRows(items, type, label, idKey = "user_id", emptyText) {
    return paginatedRows(items, `access:${type}`, item => {
      const resourceId = type === "admins"
        ? `${item.group_id}:${item.user_id}`
        : item[idKey];
      return `
      <div class="resource-row"><span>${escapeHtml(label(item))}</span>
      <button class="text-button danger immediate-action" type="button" data-action="delete-access" data-access-type="${type}" data-access-id="${attr(resourceId)}">${accessRemoveLabel(type)}</button></div>`;
    }, emptyText);
  }

  function filteredGlobalAccessItems(type) {
    const property = type === "global-bans" ? "global_bans" : "global_exemptions";
    const items = state.access?.[property] || [];
    const query = String(state.accessSearches[type] || "").trim().toLowerCase();
    if (!query) return items;
    return items.filter(item => {
      const searchable = type === "global-bans"
        ? `${item.user_id} ${item.reason || ""} ${item.source || ""}`
        : String(item.user_id);
      return searchable.toLowerCase().includes(query);
    });
  }

  function globalAccessRows(type) {
    const query = String(state.accessSearches[type] || "").trim();
    const items = filteredGlobalAccessItems(type);
    if (type === "global-bans") {
      return accessRows(
        items,
        type,
        item => `${item.user_id} · ${item.reason || "手动封禁"}`,
        "user_id",
        query ? "未找到匹配的全局封禁记录" : "暂无全局封禁记录",
      );
    }
    return accessRows(
      items,
      type,
      item => item.created_by
        ? `${item.user_id} · 操作人 ${item.created_by}`
        : `${item.user_id}`,
      "user_id",
      query ? "未找到匹配的全局豁免记录" : "暂无全局豁免记录",
    );
  }

  function globalAccessSearch(type, placeholder, ariaLabel) {
    const total = type === "global-bans"
      ? (state.access?.global_bans || []).length
      : (state.access?.global_exemptions || []).length;
    const matched = filteredGlobalAccessItems(type).length;
    return `
      <div class="access-list-toolbar">
        <div class="search-wrap">${icon("search")}<input class="search-input" type="search" data-access-search="${type}" placeholder="${attr(placeholder)}" aria-label="${attr(ariaLabel)}" autocomplete="off" value="${attr(state.accessSearches[type] || "")}"></div>
        <span class="badge info" data-access-search-count="${type}">${matched === total ? `${total} 条` : `${matched} / ${total} 条`}</span>
      </div>
      <div data-access-list="${type}">${globalAccessRows(type)}</div>`;
  }

  function refreshGlobalAccessList(type) {
    const list = content.querySelector(`[data-access-list="${CSS.escape(type)}"]`);
    if (list) list.innerHTML = globalAccessRows(type);
    const count = content.querySelector(`[data-access-search-count="${CSS.escape(type)}"]`);
    if (count) {
      const total = type === "global-bans"
        ? (state.access?.global_bans || []).length
        : (state.access?.global_exemptions || []).length;
      const matched = filteredGlobalAccessItems(type).length;
      count.textContent = matched === total ? `${total} 条` : `${matched} / ${total} 条`;
    }
    refreshIcons();
  }

  function renderAccess() {
    const access = state.access;
    if (!access) return `
      ${pageHead("权限与封禁", "管理授权群、群管理员和全局名单。")}
      <button class="secondary-button" type="button" data-action="load-access">${icon("refresh-cw")}加载数据</button>`;
    if (access.loading) return `<div class="loading-state"><span class="spinner"></span><p>正在加载权限数据</p></div>`;
    if (access.error) return `${pageHead("权限与封禁", "管理授权群、群管理员和全局名单。", `<button class="secondary-button" type="button" data-action="load-access">${icon("refresh-cw")}重试</button>`)}<div class="notice danger">${icon("circle-x")}<span>${escapeHtml(access.error)}</span></div>`;
    const authGroups = access.authorized_groups || [];
    const adminGroupId = authGroups.some(group => String(group.group_id) === String(state.accessAdminGroup))
      ? String(state.accessAdminGroup)
      : String(authGroups[0]?.group_id ?? "");
    const groupAdmins = access.admins.filter(admin => String(admin.group_id) === adminGroupId);
    return `
      ${pageHead("权限与封禁", "管理授权群、群管理员和全局名单。", `<button class="secondary-button" type="button" data-action="load-access">${icon("refresh-cw")}刷新</button>`)}
      <div class="resource-grid access-grid">
        <section class="settings-section">
         ${sectionHead("授权群组")}
         <form class="inline-resource-form" data-access-form="authorized-groups">
            <input name="group_id" type="number" step="1" placeholder="群 ID" aria-label="群 ID" required>
            <input name="title" maxlength="255" placeholder="群名称（可选）" aria-label="群名称（可选）">
            <button class="secondary-button" type="submit">${icon("shield-check")}立即授权</button>
          </form>
          ${accessRows(access.authorized_groups, "authorized-groups", item => `${item.title || "未命名群组"} · ${item.group_id}`, "group_id")}
        </section>
        <section class="settings-section">
         ${sectionHead("群管理员", "先选择群组，下方仅显示并管理该群的管理员。")}
         <form class="inline-resource-form" data-access-form="admins">
            <select name="group_id" data-access-admin-filter aria-label="选择授权群组" required>${authGroups.map(item => `<option value="${attr(item.group_id)}"${String(item.group_id) === adminGroupId ? " selected" : ""}>${escapeHtml(item.title || item.group_id)}</option>`).join("")}</select>
            <input name="user_id" type="number" step="1" placeholder="用户 ID" aria-label="管理员用户 ID" required>
            <button class="secondary-button" type="submit">${icon("user-check")}立即添加</button>
          </form>
          ${accessRows(groupAdmins, "admins", item => item.display_name ? `${item.display_name} · ${item.user_id}` : `${item.user_id}`, "user_id", "该群暂无管理员")}
        </section>
        <section class="settings-section">
         ${sectionHead("全局封禁")}
         <form class="inline-resource-form" data-access-form="global-bans">
            <input name="user_id" type="number" step="1" placeholder="用户 ID" aria-label="全局封禁用户 ID" required>
            <input name="reason" maxlength="1000" placeholder="封禁原因" aria-label="封禁原因">
            <button class="secondary-button danger-outline" type="submit">${icon("ban")}立即封禁</button>
          </form>
          ${globalAccessSearch("global-bans", "搜索用户 ID 或封禁原因", "搜索全局封禁")}
        </section>
        <section class="settings-section">
         ${sectionHead("全局资料筛查豁免", "解封后自动加入；取消后，该用户的昵称、用户名和简介将恢复筛查。")}
          ${globalAccessSearch("global-exemptions", "搜索用户 ID", "搜索全局资料筛查豁免")}
        </section>
      </div>`;
  }

  function resourceDraftFormKey(form) {
    const groupId = String(form.dataset.groupId || "");
    if (!groupId) return "";
    if (form.dataset.entryEditForm) {
      return `entry:${groupId}:${form.dataset.entryEditForm}:${form.dataset.entryId}`;
    }
    if (form.matches("[data-rule-edit-form]")) {
      return `rule:${groupId}:${form.dataset.ruleId}`;
    }
    if (form.matches("[data-memory-edit-form]")) {
      return `memory:${groupId}:${form.dataset.memoryId}`;
    }
    if (form.dataset.resourceForm) {
      return `create:${groupId}:${form.dataset.resourceForm}`;
    }
    return "";
  }

  function resourceFormSnapshot(form) {
    const snapshot = {};
    for (const control of form.elements) {
      if (!control.name) continue;
      snapshot[control.name] = control.type === "checkbox"
        ? { type: "checkbox", value: control.checked, disabled: control.disabled }
        : { type: control.type || control.tagName.toLowerCase(), value: control.value, disabled: control.disabled };
    }
    return snapshot;
  }

  function applyResourceFormSnapshot(form, snapshot) {
    for (const control of form.elements) {
      if (!control.name || !Object.hasOwn(snapshot || {}, control.name)) continue;
      const stored = snapshot[control.name];
      if (control.type === "checkbox") control.checked = Boolean(stored.value);
      else control.value = String(stored.value ?? "");
      if (stored.disabled != null) control.disabled = Boolean(stored.disabled);
    }
  }

  function syncScheduledFormFields(form) {
    const type = form.dataset.entryEditForm || form.dataset.resourceForm;
    if (type !== "scheduled-messages") return;
    const scheduleType = form.elements.schedule_type;
    const scheduleTime = form.elements.schedule_time;
    const intervalMinutes = form.elements.interval_minutes;
    if (!scheduleType || !scheduleTime || !intervalMinutes) return;
    const daily = scheduleType.value === "daily";
    scheduleTime.disabled = !daily;
    scheduleTime.required = daily;
    intervalMinutes.disabled = daily;
    intervalMinutes.required = !daily;
    scheduleTime.title = daily ? "每天定时必须填写发送时间" : "固定间隔模式不使用发送时间";
    intervalMinutes.title = daily ? "每天定时模式不使用间隔分钟" : "固定间隔必须填写分钟数";
  }

  function captureResourceFormDraft(form) {
    const key = resourceDraftFormKey(form);
    if (!key) return;
    const current = resourceFormSnapshot(form);
    const baseline = state.resourceFormBaselines.get(key);
    if (baseline && sameValue(current, baseline) && !state.resourcePatchInFlight?.has(key)) state.resourceFormDrafts.delete(key);
    else if (baseline) state.resourceFormDrafts.set(key, current);
    else state.resourceFormBaselines.set(key, current);
    const type = form.dataset.entryEditForm
      || form.dataset.resourceForm
      || (form.matches("[data-rule-edit-form]") ? "rules" : "memories");
    const pendingCreate = state.pendingResourceCreates.has(resourceOperationKey(
      form.dataset.groupId,
      type,
      form.dataset.entryId || form.dataset.ruleId || form.dataset.memoryId,
    ));
    const marker = form.querySelector("[data-resource-form-state]");
    const dirty = state.resourceFormDrafts.has(key);
    if (marker) {
      marker.className = `badge ${pendingCreate ? "info" : dirty ? "warning" : "resource-clean-state"}`;
      marker.textContent = pendingCreate ? "待新增" : dirty ? "已修改" : "已保存";
    }
    form.classList.toggle("is-dirty", pendingCreate || dirty);
    updateRenderedGroupSectionDirty(
      form.closest("[data-group-settings-section]"),
      groupResourceDraftDirty(form.dataset.groupId, RESOURCE_TYPE_META[type]?.category),
      "待保存",
    );
    updateRenderedGroupDirty(form.dataset.groupId);
    updateChrome();
  }

  function captureResourceFormDrafts() {
    content.querySelectorAll(RESOURCE_DRAFT_FORM_SELECTOR).forEach(captureResourceFormDraft);
  }

  function restoreResourceFormDrafts() {
    content.querySelectorAll(RESOURCE_DRAFT_FORM_SELECTOR).forEach(form => {
      const key = resourceDraftFormKey(form);
      if (!key) return;
      syncScheduledFormFields(form);
      state.resourceFormBaselines.set(key, resourceFormSnapshot(form));
      const draft = state.resourceFormDrafts.get(key);
      if (draft) applyResourceFormSnapshot(form, draft);
      syncScheduledFormFields(form);
    });
  }

  function clearResourceFormDrafts({ clearOperations = true } = {}) {
    state.resourceFormDrafts.clear();
    state.resourceFormBaselines.clear();
    if (clearOperations) {
      for (const operation of state.pendingResourceCreates.values()) {
        const list = resourceArray(operation.groupId, operation.type);
        const index = list.findIndex(item => String(item.id) === String(operation.id));
        if (index >= 0) list.splice(index, 1);
      }
      state.pendingResourceCreates.clear();
      state.pendingResourceDeletes.clear();
    }
  }

  function discardGroupResourceDrafts(groupId) {
    const target = String(groupId);
    state.groupApiModelQuerySecretChanges.delete(target);
    for (const key of [...state.resourceFormDrafts.keys()]) {
      if (resourceDraftGroupId(key) === target) state.resourceFormDrafts.delete(key);
    }
    for (const key of [...state.resourceFormBaselines.keys()]) {
      if (resourceDraftGroupId(key) === target) state.resourceFormBaselines.delete(key);
    }
    for (const [key, operation] of [...state.pendingResourceCreates.entries()]) {
      if (String(operation.groupId) === target) {
        const list = resourceArray(operation.groupId, operation.type);
        const index = list.findIndex(item => String(item.id) === String(operation.id));
        if (index >= 0) list.splice(index, 1);
        state.pendingResourceCreates.delete(key);
      }
    }
    for (const [key, operation] of [...state.pendingResourceDeletes.entries()]) {
      if (String(operation.groupId) === target) state.pendingResourceDeletes.delete(key);
    }
  }

  function restoreGroupTemplateButtonValidity() {
    content.querySelectorAll("[data-template-buttons]").forEach(control => {
      const draft = state.groupTemplateButtonDrafts.get(String(control.dataset.groupId));
      control.setCustomValidity(draft?.error || "");
    });
  }

  function setGroupCardDisclosure(card, open) {
    if (!card) return;
    const toggle = card.querySelector('[data-action="toggle-group-card"]');
    const body = card.querySelector("[data-group-card-body]");
    const groupId = String(toggle?.dataset.groupId || "");
    if (!toggle || !body || !groupId) return;
    state.groupCardOpen.set(groupId, open);
    toggle.setAttribute("aria-expanded", String(open));
    body.hidden = !open;
    if (open && body.dataset.groupBodyRendered !== "true") {
      renderContent();
      return;
    }
  }

  function setGroupSectionsDisclosure(card, open) {
    if (!card) return;
    card.querySelectorAll("[data-group-settings-section]").forEach(section => {
      section.open = open;
      state.groupSectionOpen.set(
        groupSectionStateKey(section.dataset.groupId, section.dataset.groupSection),
        open,
      );
    });
  }

  function revealGroupSection(groupId, sectionKey) {
    const groupKey = String(groupId);
    state.groupCardOpen.set(groupKey, true);
    state.groupSectionOpen.set(groupSectionStateKey(groupKey, sectionKey), true);
    let toggle = content.querySelector(
      `[data-action="toggle-group-card"][data-group-id="${CSS.escape(String(groupId))}"]`,
    );
    let card = toggle?.closest("[data-group-card]");
    let body = card?.querySelector("[data-group-card-body]");
    if (!card || body?.dataset.groupBodyRendered !== "true") {
      renderContent();
      toggle = content.querySelector(
        `[data-action="toggle-group-card"][data-group-id="${CSS.escape(groupKey)}"]`,
      );
      card = toggle?.closest("[data-group-card]");
      body = card?.querySelector("[data-group-card-body]");
    }
    if (toggle) toggle.setAttribute("aria-expanded", "true");
    if (body) body.hidden = false;
    const section = [...(card?.querySelectorAll("[data-group-settings-section]") || [])]
      .find(item => item.dataset.groupSection === String(sectionKey));
    if (!section) return;
    section.open = true;
  }

  function revealGroupControl(control) {
    const card = control.closest("[data-group-card]");
    setGroupCardDisclosure(card, true);
    const section = control.closest("[data-group-settings-section]");
    if (!section) return;
    section.open = true;
    state.groupSectionOpen.set(
      groupSectionStateKey(section.dataset.groupId, section.dataset.groupSection),
      true,
    );
  }

  function captureGroupDisclosureStates() {
    content.querySelectorAll('[data-action="toggle-group-card"]').forEach(toggle => {
      state.groupCardOpen.set(
        String(toggle.dataset.groupId),
        toggle.getAttribute("aria-expanded") === "true",
      );
    });
    content.querySelectorAll("[data-group-settings-section]").forEach(section => {
      state.groupSectionOpen.set(
        groupSectionStateKey(section.dataset.groupId, section.dataset.groupSection),
        section.open,
      );
    });
  }

  function applyGroupSearchFilter() {
    const query = state.groupSearch.trim().toLowerCase();
    content.querySelectorAll("[data-group-card]").forEach(card => {
      card.hidden = Boolean(query) && !card.dataset.search.includes(query);
    });
  }

  function captureContentScrollAnchor() {
    const stickyBottom = Math.max(
      document.querySelector(".topbar")?.getBoundingClientRect().bottom || 0,
      mobileNav.offsetParent ? mobileNav.getBoundingClientRect().bottom : 0,
    );
    const candidates = [...content.querySelectorAll(
      "[data-group-settings-section], [data-resource-category], [data-group-card], [data-advanced-panel]",
    )].map(element => ({ element, rect: element.getBoundingClientRect() }))
      .filter(item => item.rect.bottom > stickyBottom + 8)
      .sort((left, right) => Math.abs(left.rect.top - stickyBottom) - Math.abs(right.rect.top - stickyBottom));
    const target = candidates[0];
    if (!target) return null;
    const element = target.element;
    let selector = "";
    if (element.matches("[data-group-settings-section]")) {
      selector = `[data-group-settings-section][data-group-id="${CSS.escape(element.dataset.groupId)}"][data-group-section="${CSS.escape(element.dataset.groupSection)}"]`;
    } else if (element.matches("[data-resource-category]")) {
      selector = `[data-group-resource-panel="${CSS.escape(element.dataset.groupResourcePanel)}"][data-resource-category="${CSS.escape(element.dataset.resourceCategory)}"]`;
    } else if (element.matches("[data-group-card]")) {
      selector = `[data-group-card][data-group-id="${CSS.escape(element.dataset.groupId)}"]`;
    } else if (element.matches("[data-advanced-panel]")) {
      selector = `[data-advanced-panel="${CSS.escape(element.dataset.advancedPanel)}"]`;
    }
    return selector ? { selector, top: target.rect.top } : null;
  }

  function renderContent({ resetScroll = false } = {}) {
    if (!state.config) return;
    content.querySelectorAll("[data-memory-filter]").forEach(form => {
      // Capture the live form as a draft only; the applied query changes on submit.
      memoryReadState(form.dataset.groupId, form.dataset.memoryFilter).draft = memoryFilterValues(form);
    });
    content.querySelectorAll("[data-memory-edit]").forEach(form => {
      // 纠错草稿同样只存草稿：409/未确认/兄弟分节重绘都不吞掉用户输入。
      const field = form.querySelector('[name="text"]');
      if (field) memoryFactEditState(form.dataset.groupId, form.dataset.memoryEdit).text = field.value;
    });
    captureGroupDisclosureStates();
    captureAdvancedDisclosureStates();
    captureResourceFormDrafts();
    const renderers = {
      overview: renderOverview,
      models: renderModels,
      bot: renderBot,
      safety: renderSafety,
      media: renderMedia,
      operations: renderOperations,
      integrations: renderIntegrations,
      logging: renderLogging,
      prompts: renderPrompts,
      groups: renderGroups,
      access: renderAccess,
      history: renderHistory,
      // 超管的"入群接入"页 = 接入表单 + 我的申请 + 待审批队列。以前这里没注册，
      // `renderers[tab] || renderGroups` 会让超管点进去只看到群列表。
      onboarding: () => applicantSessionView(state.session),
    };
    const renderer = renderers[state.activeTab] || renderGroups;
    // Replacing innerHTML collapses the document height and lets the browser
    // clamp the scroll position; restore it so partial refreshes (saving a
    // group, loading resources) do not jump the page back to the top.
    const scrollX = window.scrollX;
    const scrollY = window.scrollY;
    const scrollAnchor = resetScroll ? null : captureContentScrollAnchor();
    const previousHeight = content.offsetHeight;
    if (previousHeight > 0) content.style.minHeight = `${previousHeight}px`;
    content.innerHTML = renderer();
    // 保存前差异预览是一个跨 tab 的浮层：它必须出现在任意页面的页头正下方，
    // 而不是绑死在某一个 renderer 里。
    const previewMarkup = savePreviewMarkup();
    if (previewMarkup) {
      const holder = document.createElement("div");
      holder.innerHTML = previewMarkup;
      const node = holder.firstElementChild;
      const pageHeadNode = content.querySelector(".page-head");
      if (node && pageHeadNode) pageHeadNode.after(node);
      else if (node) content.prepend(node);
    }
    if (state.activeTab === "onboarding") {
      // 与 switchTab 的 safety/history 同一个套路：进入时才拉，避免每次
      // render 都打一次全局队列。
      loadOnboardingRequests(state.session).catch(() => {});
      // 队列读成功后再追认"已提交但未确认"的决策（离开面板再回来也要能收口）。
      loadOnboardingQueue()
        .then((outcome) => (outcome?.ok ? resolvePendingConfirmations() : null))
        .catch(() => {});
    }
    if (state.activeTab === "models" && state.session?.can_manage_global) {
      // 价格目录只有超管能看到；普通本群管理员/申请人**连请求都不发**。
      // quiet：进入页面不弹"已读取"提示，但读失败仍会把卡片画成"状态未知"。
      reloadPriceCatalog({ quiet: true }).catch(() => {});
    }
    restoreResourceFormDrafts();
    restoreGroupTemplateButtonValidity();
    applyGroupSearchFilter();
    refreshIcons();
    if (resetScroll) {
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    } else {
      window.scrollTo(scrollX, scrollY);
      const nextAnchor = scrollAnchor ? content.querySelector(scrollAnchor.selector) : null;
      if (nextAnchor) {
        const delta = nextAnchor.getBoundingClientRect().top - scrollAnchor.top;
        if (Math.abs(delta) > 0.5) window.scrollBy({ top: delta, left: 0, behavior: "auto" });
      }
    }
    window.requestAnimationFrame(() => { content.style.minHeight = ""; });
    // 视图刚被重建过：回放面板还在就接着跟进，页面已经走掉就把定时器收干净。
    syncReplayPolling();
  }

  function render() {
    updateChrome();
    renderContent();
    app.setAttribute("aria-busy", "false");
  }

  function renderFatal(title, message) {
    state.loading = false;
    app.setAttribute("aria-busy", "false");
    syncWorkspaceInteractionLock();
    content.innerHTML = `
      <div class="error-state">
        ${icon("shield-x")}
        <h2>${escapeHtml(title)}</h2>
        <p>${escapeHtml(message)}</p>
        <button class="secondary-button retry-button" type="button" data-action="retry-load">${icon("refresh-cw")}重试</button>
      </div>`;
    saveButton.disabled = true;
    reloadButton.disabled = false;
    refreshIcons();
  }

  function applySettingsDocument(document) {
    state.document = document;
    state.config = clone(document.config);
    normalizeModelRoles();
    if (state.config?.bot?.auto_delete_seconds == null && state.config?.bot?.auto_delete_minutes != null) {
      state.config.bot.auto_delete_seconds = Number(state.config.bot.auto_delete_minutes || 0) * 60;
    }
    if (state.config?.bot) delete state.config.bot.auto_delete_minutes;
    if (state.config?.bot && (typeof state.config.bot.auto_delete_category_seconds !== "object" || state.config.bot.auto_delete_category_seconds == null)) {
      state.config.bot.auto_delete_category_seconds = {};
    }
    if (state.config?.bot && state.config.bot.disable_link_preview == null) {
      state.config.bot.disable_link_preview = true;
    }
    if (state.config?.verification) {
      state.config.verification.provider ||= "turnstile";
      state.config.verification.hcaptcha_site_key ||= "";
      state.config.verification.hcaptcha_secret_key ||= "";
    }
    state.baseline = clone(document.config);
    state.baseline = clone(state.config);
    state.configuredSecrets = new Set(document.configured_secrets || []);
    state.secretChanges = {};
  }

  function applyGroupsDocument(document) {
    const groups = Array.isArray(document?.groups) ? document.groups : [];
    state.permissionFields = Array.isArray(document?.permission_fields) && document.permission_fields.length
      ? document.permission_fields.map(item => ({ key: String(item.key), label: String(item.label || item.key) }))
      : PERMISSION_FIELD_FALLBACK;
    state.groups = groups.map(group => ({
      id: group.id,
      title: String(group.title || ""),
      revision: String(group.revision || ""),
      settings: normalizeGroupSettings(group.settings),
    }));
    state.groupBaselines = new Map(state.groups.map(group => [String(group.id), clone(group.settings)]));
    for (const group of state.groups) {
      const draft = state.groupTemplateButtonDrafts.get(String(group.id));
      if (Array.isArray(draft?.buttons)) {
        group.settings.welcome_buttons = clone(draft.buttons);
      }
    }
    state.groupsError = "";
  }

  async function loadAll({ keepTab = true } = {}) {
    captureGroupDisclosureStates();
    captureAdvancedDisclosureStates();
    captureResourceFormDrafts();
    state.groupResources.clear();
    state.groupResourceLoads.clear();
    state.groupPermissionLoads.clear();
    state.groupTelegramAdmins.clear();
    state.access = null;
    state.accessLoadToken = null;
    state.loading = true;
    updateChrome();
    if (!tg?.initData) {
      renderFatal("无法验证管理员身份", "请从 Telegram 中的管理员 Mini App 入口打开此页面。");
      return;
    }
    if (!keepTab) state.activeTab = "overview";
    content.innerHTML = `<div class="loading-state"><span class="spinner"></span><p>正在加载设置</p></div>`;
    try {
      let sessionResult;
      try {
        sessionResult = await apiFetch("/api/v1/session");
      } catch (sessionError) {
        // 没有任何已授权群的普通用户在 /api/v1/session 上是 403（既有合同，
        // 不为了新页面放宽）。这时退到**仅申请**入口：能提交申请、看自己的
        // 申请，但看不到全局 settings、也看不到任何群配置。
        if (!(sessionError instanceof ApiError && sessionError.status === 403)) throw sessionError;
        sessionResult = { session: await loadOnboardingSession() };
        state.applicantOnly = !sessionResult.session?.can_manage_global
          && !(sessionResult.session?.group_ids || []).length;
        if (state.applicantOnly) {
          state.loading = false;
          state.session = sessionResult.session;
          content.innerHTML = applicantSessionView(state.session);
          await loadOnboardingRequests(state.session);
          render();
          return;
        }
      }
      state.session = sessionResult.session;
      if (!state.session?.can_manage_global) state.activeTab = "groups";
      const requests = [apiFetch("/api/v1/groups")];
      if (state.session?.can_manage_global) requests.unshift(apiFetch("/api/v1/settings"));
      const results = await Promise.allSettled(requests);
      const settingsResult = state.session?.can_manage_global ? results[0] : null;
      const groupsResult = state.session?.can_manage_global ? results[1] : results[0];
      if (settingsResult?.status === "rejected") throw settingsResult.reason;
      if (settingsResult?.status === "fulfilled") applySettingsDocument(settingsResult.value);
      else {
        state.document = { revision: 0, bootstrap: {}, restart_required_paths: [], restart_pending: [] };
        state.config = {};
        state.baseline = {};
      }
      if (groupsResult.status === "fulfilled") {
        applyGroupsDocument(groupsResult.value);
      } else {
        state.groups = [];
        state.groupBaselines = new Map();
        state.groupsError = groupsResult.reason?.message || "群组列表加载失败";
      }
      state.loading = false;
      render();
    } catch (error) {
      const auth = error instanceof ApiError && (error.status === 401 || error.status === 403);
      renderFatal(auth ? "没有管理权限" : "设置加载失败", error.message || "无法连接到 Mini App 服务");
    }
  }

  function costPanelRoot(groupId) {
    return content.querySelector(`[data-cost-group="${CSS.escape(String(groupId))}"]`);
  }

  // ---- 模型价格目录（仅超管） ------------------------------------------
  //
  // 价格目录是**版本化只增**的历史：改价 = 新版本，历史账不追改。所以保存不是
  // "覆盖当前这一版"，而是"拿我读到的那个版本去换下一版"——服务端只接受
  // expected_version 与持久最新版本一致的写入，旧草稿必须 409。否则两个会话都
  // 读到 v3：A 把 input 存成 33 得到 v4，B 拿着旧 v3 草稿只改 output 存成 v5，
  // 就把 A 刚写的 input 悄悄还原了 —— 这是真实的保护丢失。
  //
  // 这张卡片刻意**不**走"设置总保存"（那是 /settings 的另一条链路）：它有自己
  // 的草稿、自己的保存按钮、自己的读回确认。预算编辑器在上面翻过的两次车都要
  // 避开：提交快照与可编辑草稿必须是**两份深复制**（共用同一个对象时，用户在
  // ACK 期间打字会把"我提交的内容"改成新值，回读时判成假冲突）；PUT 200
  // **不是**确认，必须逐字段 + 版本读回核对。
  //
  // 读失败 = **未知**，不是"空目录"：空目录会被存成"所有模型都没有价格"，而
  // 没有价格会让金额护栏 fail-closed 挡住真实调用。

  const PRICE_LIMIT_FIELDS = ["input", "output", "cache_read", "cache_write"];
  const PRICE_FIELD_LABELS = {
    provider_key: "配置槽",
    model: "模型",
    currency: "币种",
    input: "输入价",
    output: "输出价",
    cache_read: "缓存读价",
    cache_write: "缓存写价",
  };

  function priceState() {
    if (!state.priceCatalog || typeof state.priceCatalog !== "object") state.priceCatalog = {};
    const price = state.priceCatalog;
    if (!Array.isArray(price.rows)) price.rows = [];
    //: 最后一次**从服务器读到并采用**的行（重基时的三方合并基线）。
    if (!Array.isArray(price.baseRows)) price.baseRows = [];
    if (typeof price.version !== "number") price.version = null;
    if (typeof price.dirty !== "boolean") price.dirty = false;
    if (!Number.isSafeInteger(price.edits)) price.edits = 0;
    if (typeof price.loaded !== "boolean") price.loaded = false;
    if (typeof price.loading !== "boolean") price.loading = false;
    if (typeof price.saving !== "boolean") price.saving = false;
    if (typeof price.readError !== "string") price.readError = "";
    if (typeof price.error !== "string") price.error = "";
    if (typeof price.status !== "string") price.status = "";
    if (price.submitted === undefined) price.submitted = null;
    if (price.conflict === undefined) price.conflict = null;
    return price;
  }

  function priceTextOf(row, field) {
    return String(row?.[field] ?? "").trim();
  }

  /** 只包含"服务器会保存的字段"的可比较形态：整行全空的新行丢掉，配置槽小写、
   * 币种大写、小数去掉尾随 0 —— 否则 `1.50` 与服务器回的 `1.5` 会被判成不同，
   * 每次保存都造一个假冲突。 */
  function normalizeDecimalText(text) {
    const raw = String(text ?? "").trim();
    if (raw === "") return "";
    if (!/^\d+(?:\.\d+)?$/.test(raw)) return raw;
    const [whole, fraction = ""] = raw.split(".");
    const trimmed = fraction.replace(/0+$/, "");
    const normalizedWhole = whole.replace(/^0+(?=\d)/, "");
    return trimmed ? `${normalizedWhole}.${trimmed}` : normalizedWhole;
  }

  function priceComparableRows(rows) {
    const out = [];
    for (const row of Array.isArray(rows) ? rows : []) {
      const providerKey = priceTextOf(row, "provider_key");
      const model = priceTextOf(row, "model");
      const currency = priceTextOf(row, "currency");
      const prices = PRICE_LIMIT_FIELDS.map(field => normalizeDecimalText(priceTextOf(row, field)));
      if (!providerKey && !model && !currency && prices.every(value => !value)) continue;
      const comparable = { provider_key: providerKey.toLowerCase(), model, currency: currency.toUpperCase() };
      PRICE_LIMIT_FIELDS.forEach((field, index) => { comparable[field] = prices[index]; });
      out.push(comparable);
    }
    return out;
  }

  function priceRowIdentity(row) {
    return `${priceTextOf(row, "provider_key").toLowerCase()}\u0000${priceTextOf(row, "model")}`;
  }

  /** 行在文案里的可读标签（身份 = 配置槽 + 模型）。 */
  function priceRowLabel(row) {
    const provider = priceTextOf(row, "provider_key");
    const model = priceTextOf(row, "model");
    if (provider && model) return `${provider}/${model}`;
    return provider || model || "（未命名行）";
  }

  function priceDiffLines(mineRows, theirsRows) {
    const mine = priceComparableRows(mineRows);
    const theirs = priceComparableRows(theirsRows);
    const index = rows => new Map(rows.map(row => [priceRowIdentity(row), row]));
    const left = index(mine);
    const right = index(theirs);
    const lines = [];
    const label = row => `${row.provider_key || "（空配置槽）"} / ${row.model || "（空模型）"}`;
    for (const [identity, row] of left) {
      const other = right.get(identity);
      if (!other) {
        lines.push(`${label(row)}：只在你的草稿里，服务器上没有这一项`);
        continue;
      }
      for (const field of ["currency", ...PRICE_LIMIT_FIELDS]) {
        if (row[field] !== other[field]) {
          lines.push(
            `${label(row)} · ${PRICE_FIELD_LABELS[field]}：你的=${row[field] || "（空 = 未知）"}`
            + `，服务器=${other[field] || "（空 = 未知）"}`,
          );
        }
      }
    }
    for (const [identity, row] of right) {
      if (!left.has(identity)) lines.push(`${label(row)}：服务器上有、你的草稿里没有`);
    }
    return lines;
  }

  function priceRowsEqual(left, right) {
    return priceDiffLines(left, right).length === 0;
  }

  function priceCloneRows(rows) {
    return (Array.isArray(rows) ? rows : []).map(row => ({
      provider_key: String(row?.provider_key ?? ""),
      model: String(row?.model ?? ""),
      currency: String(row?.currency ?? ""),
      input: String(row?.input ?? ""),
      output: String(row?.output ?? ""),
      cache_read: String(row?.cache_read ?? ""),
      cache_write: String(row?.cache_write ?? ""),
    }));
  }

  //: 重基（三方合并）时要逐字段比对的字段清单。
  const PRICE_MERGE_FIELDS = ["provider_key", "model", "currency", ...PRICE_LIMIT_FIELDS];

  /**
   * 把我的草稿**重基**到服务器当前版本上（三方合并：基线 / 我的草稿 / 服务器）。
   *
   * 规则：
   * * 只有我这一轮真的改过的字段（`mine[f] !== base[f]`）才顶掉服务器值；
   *   我没碰过的字段一律采用服务器当前值。
   * * **行的增删也要看基线**：基线里有、我草稿里没有 = 我**本地删了**这一行 →
   *   保留删除（用户选的就是"保留我的改动"），绝不静默复活；基线里有、服务器上
   *   没有 = 别人删了这条原有行 → 以服务器为准，不替别人复活；两边都没有才是我
   *   新增的行（保留）、或服务器新增的行（保留）。
   * * `provider_key`/`model` 改动 = 旧身份被删 + 新身份新增（身份就是这两者的组合），
   *   所以重基后不会同时留下旧身份与新身份两行。
   *
   * 为什么不能简单"保留我的草稿、只换版本号再保存"：那样会把别人在这期间改过的、
   * 我根本没碰过的字段按我记忆里的旧值一起写回去 —— 那不是重基，是整份覆盖，
   * 别人的改动会被悄悄还原（本轮真实浏览器实测到的正是这个：我只改了输入价，
   * 保存后别人改过的输出价被打回了旧值）。
   *
   * 返回 `{ rows, keptDeletions, serverDeletions }`：后两者是"我删的行没有被复活"
   * 与"服务器删掉的行没有被加回来"的行标签，用于把结果**说清楚**，而不是让用户
   * 自己猜为什么少了一行。
   */
  function mergePriceRows(baseRows, mineRows, serverRows) {
    const base = new Map((baseRows || []).map(row => [priceRowIdentity(row), row]));
    const mine = new Map((mineRows || []).map(row => [priceRowIdentity(row), row]));
    const server = Array.isArray(serverRows) ? serverRows : [];
    const rows = [];
    const keptDeletions = [];
    const serverDeletions = [];
    for (const serverRow of server) {
      const key = priceRowIdentity(serverRow);
      const myRow = mine.get(key);
      if (!myRow) {
        if (base.has(key)) {
          // 基线里有、我草稿里没有 = 我本地删掉了它：保住我的删除
          keptDeletions.push(priceRowLabel(base.get(key)));
          continue;
        }
        // 基线里也没有 = 别人新增的行：保留服务器值
        rows.push(priceCloneRows([serverRow])[0]);
        continue;
      }
      const baseRow = base.get(key) || {};
      const row = {};
      for (const field of PRICE_MERGE_FIELDS) {
        const changedByMe = String(myRow[field] ?? "") !== String(baseRow[field] ?? "");
        row[field] = String((changedByMe ? myRow[field] : serverRow[field]) ?? "");
      }
      rows.push(row);
    }
    const serverKeys = new Set(server.map(row => priceRowIdentity(row)));
    for (const myRow of mineRows || []) {
      const key = priceRowIdentity(myRow);
      if (serverKeys.has(key)) continue;
      if (base.has(key)) {
        // 服务器删掉了这条原有行（我这边没删）：以服务器为准，不复活
        serverDeletions.push(priceRowLabel(myRow));
        continue;
      }
      // 我真正新增的行
      rows.push(priceCloneRows([myRow])[0]);
    }
    return { rows, keptDeletions, serverDeletions };
  }

  function priceRowFromEntry(entry) {
    const text = value => (value == null ? "" : String(value));
    return {
      provider_key: text(entry?.provider_key),
      model: text(entry?.model),
      currency: text(entry?.currency),
      input: text(entry?.input_per_million),
      output: text(entry?.output_per_million),
      cache_read: text(entry?.cache_read_per_million),
      cache_write: text(entry?.cache_write_per_million),
    };
  }

  /** 服务器目录哪里有毛病（空串 = 可信）。半坏回包按**读失败**处理：把"我读不
   * 出来"画成"没有价格"，管理员会以为目录是空的，然后真的存一份空目录上去。
   * 价格字段只认 `null` 或字符串 —— 真实 document() 把 Decimal 输出成字符串，
   * 回一个数字说明合同已经变了，此时宁可未知也不要猜。 */
  function priceCatalogProblem(document) {
    const catalog = document?.catalog;
    if (!catalog || typeof catalog !== "object") return "响应里没有 catalog 对象";
    if (!Number.isSafeInteger(catalog.version) || catalog.version < 1) {
      return "catalog.version 必须是 ≥1 的整数";
    }
    if (!Array.isArray(catalog.entries)) return "catalog.entries 不是数组";
    const priceKeys = [
      "input_per_million",
      "output_per_million",
      "cache_read_per_million",
      "cache_write_per_million",
    ];
    for (const entry of catalog.entries) {
      if (!entry || typeof entry !== "object") return "entries 里有不是对象的项";
      for (const key of ["provider_key", "model", "currency"]) {
        if (typeof entry[key] !== "string" || !entry[key].trim()) {
          return `entries 里有一项的 ${key} 不是非空字符串`;
        }
      }
      for (const key of priceKeys) {
        if (entry[key] !== null && typeof entry[key] !== "string") {
          return `entries 里有一项的 ${key} 既不是字符串也不是 null`;
        }
      }
    }
    return "";
  }

  async function fetchPriceCatalog() {
    const document = await apiFetch("/api/v1/prices");
    const problem = priceCatalogProblem(document);
    if (problem) {
      return { ok: false, error: { message: `服务器返回的价格目录不可信（${problem}）` } };
    }
    return { ok: true, catalog: document.catalog };
  }

  /** 价格文本：空 = 未知（提交 null），0 = 明确免费，其余必须是十进制字符串。
   *
   * **刻意不用** parseFloat/parseInt：`parseInt("1.9")` 是 1、`parseFloat("1.9abc")`
   * 是 1.9 —— 那等于替管理员改价。小数按十进制字符串原样交给后端（后端用
   * Decimal，不截断）。 */
  function parsePriceText(text) {
    const raw = String(text ?? "").trim();
    if (raw === "") return { ok: true, value: null };
    if (!/^\d+(?:\.\d+)?$/.test(raw)) {
      return { ok: false, message: `价格只能是空（未知）、0（免费）或十进制数：${raw}` };
    }
    return { ok: true, value: raw };
  }

  function collectPriceDraft() {
    const price = priceState();
    const entries = [];
    const seen = new Set();
    for (const [index, row] of price.rows.entries()) {
      const where = `第 ${index + 1} 行`;
      const providerKey = priceTextOf(row, "provider_key");
      const model = priceTextOf(row, "model");
      const currency = priceTextOf(row, "currency");
      const prices = PRICE_LIMIT_FIELDS.map(field => priceTextOf(row, field));
      // 整行全空 = 刚点"添加一行"还没填，直接忽略
      if (!providerKey && !model && !currency && prices.every(value => !value)) continue;
      if (!providerKey) return { ok: false, message: `${where}：配置槽不能为空（它是供应商配置槽，不是 API Key）。` };
      if (!model) return { ok: false, message: `${where}：模型不能为空。` };
      if (!/^[A-Za-z]{3}$/.test(currency)) {
        return { ok: false, message: `${where}：币种必须是三个字母的代码，例如 USD。` };
      }
      const identity = `${providerKey.toLowerCase()}\u0000${model}`;
      if (seen.has(identity)) {
        return { ok: false, message: `${where}：同一个配置槽 + 模型只能有一行（重复：${providerKey} / ${model}）。` };
      }
      seen.add(identity);
      const entry = { provider_key: providerKey, model, currency: currency.toUpperCase() };
      for (const field of PRICE_LIMIT_FIELDS) {
        const parsed = parsePriceText(row[field]);
        if (!parsed.ok) return { ok: false, message: `${where}：${parsed.message}` };
        entry[`${field}_per_million`] = parsed.value;
      }
      entries.push(entry);
    }
    return { ok: true, entries, rows: priceCloneRows(price.rows) };
  }

  /** 服务器回读 → 与本地状态对账。**唯一**的确认判定：保存回读、重新读取、
   * 进入模型页、切页重绘都走它 —— 一个入口拒旧回读而另一个入口认成功，正是
   * 预算编辑器上一轮抓到的口径不一致。 */
  function reconcilePriceWithServer(catalog) {
    const price = priceState();
    const serverRows = priceCloneRows((catalog.entries || []).map(priceRowFromEntry));
    const serverVersion = Number(catalog.version);
    const outcome = {
      adopted: false,
      confirmed: false,
      conflict: false,
      keptEdits: false,
      revisionAdvanced: false,
      ignoredStale: false,
      submittedVersion: null,
    };
    const submitted = price.submitted || null;
    if (!submitted) {
      if (!price.loaded || !price.dirty) {
        price.rows = serverRows;
        price.baseRows = priceCloneRows(serverRows);
        price.version = serverVersion;
        price.dirty = false;
        outcome.adopted = true;
        return outcome;
      }
      if (serverVersion === price.version) {
        // 版本没动 = 这是我自己的未保存编辑，不是"别人改过"
        outcome.keptEdits = true;
        return outcome;
      }
      if (priceRowsEqual(price.rows, serverRows)) {
        price.rows = serverRows;
        price.baseRows = priceCloneRows(serverRows);
        price.version = serverVersion;
        price.dirty = false;
        outcome.adopted = true;
        outcome.confirmed = true;
        outcome.revisionAdvanced = true;
        price.status = `服务器上的价格目录已经和你的草稿一致（revision=${serverVersion}）。`;
        return outcome;
      }
      price.conflict = {
        mineVersion: price.version,
        serverVersion,
        serverRows: priceCloneRows(serverRows),
        mineRows: priceCloneRows(price.rows),
        diffs: priceDiffLines(price.rows, serverRows),
        title: null,
        note: null,
      };
      outcome.conflict = true;
      return outcome;
    }
    outcome.submittedVersion = Number(submitted.version);
    if (serverVersion < Number(submitted.version)) {
      // 乱序回读：什么都没证明，保持待确认
      outcome.ignoredStale = true;
      return outcome;
    }
    if (!priceRowsEqual(submitted.rows, serverRows)) {
      price.conflict = {
        mineVersion: Number(submitted.version),
        serverVersion,
        serverRows: priceCloneRows(serverRows),
        mineRows: priceCloneRows(price.rows),
        diffs: priceDiffLines(price.rows, serverRows),
        title: "你的提交没有被确认：服务器上的价格目录和你提交的内容不一样",
        note: "版本往前走了只说明别的东西写了，并不证明你那次写入生效。",
      };
      outcome.conflict = true;
      return outcome;
    }
    outcome.confirmed = true;
    outcome.revisionAdvanced = serverVersion !== Number(submitted.version);
    price.submitted = null;
    price.version = serverVersion;
    if (Number(price.edits || 0) !== Number(submitted.edits || 0)) {
      // ACK 期间用户又改了：保留新编辑，并明确它是**没有保存**的。
      // 关键：确认之后基线必须推进到**本次已确认的服务器值**（== 提交快照）。否则基线
      // 还停在更早的版本，用户"把值改回旧基线"的那种编辑会被误判成"我没改过"，下一次
      // 重基时被服务器值顶掉 —— 用户明明改过却悄悄丢了。
      price.baseRows = priceCloneRows(serverRows);
      price.dirty = true;
      outcome.keptEdits = true;
      price.status = `上一次提交（revision=${submitted.version}）已经核对确认；你之后又改了内容，这些新修改还没有保存。`;
    } else {
      price.rows = serverRows;
      price.baseRows = priceCloneRows(serverRows);
      price.dirty = false;
      price.status = `已回读确认（revision=${serverVersion}）。`;
    }
    return outcome;
  }

  async function savePriceCatalog() {
    const price = priceState();
    if (!state.session?.can_manage_global) return;
    if (price.saving) return;
    if (price.conflict) {
      price.error = "还有未解决的版本冲突：请先选择「用服务器值」还是「重基我的草稿」。";
      renderPriceCatalog();
      return;
    }
    if (!price.loaded) {
      price.error = "还没有成功读到过价格目录，不能保存 —— 把未知状态当空目录写上去，会让所有模型都变成没有价格。";
      renderPriceCatalog();
      return;
    }
    if (price.submitted) {
      const pendingVersion = Number(price.submitted.version);
      const fresh = await fetchPriceCatalog().catch(error => ({ ok: false, error }));
      if (!fresh.ok) {
        price.error = `上一次提交（revision=${pendingVersion}）还没能确认：价格目录回读失败（${fresh.error?.message || "未知错误"}）。没有发出新的写入。`;
        renderPriceCatalog();
        showToast("上一次提交仍未确认，没有发出新的写入", "warning");
        return;
      }
      const outcome = reconcilePriceWithServer(fresh.catalog);
      if (outcome.conflict || outcome.ignoredStale || !outcome.confirmed) {
        price.error = outcome.ignoredStale
          ? `这次读到的目录（revision=${Number(fresh.catalog.version)}）比那次写入还旧，不构成确认。没有发出新的写入。`
          : "上一次提交还没有被确认，没有发出新的写入。";
        renderPriceCatalog();
        showToast("上一次提交尚未确认", "warning");
        return;
      }
      if (!price.dirty) {
        price.error = "";
        renderPriceCatalog();
        showToast(`上一次提交已核对确认（revision=${price.version}），没有重复写入。`, "success");
        return;
      }
      price.status = `上一次提交（revision=${pendingVersion}）已经核对确认；现在保存你之后的新修改。`;
    }
    const collected = collectPriceDraft();
    if (!collected.ok) {
      price.error = collected.message;
      renderPriceCatalog();
      return;
    }
    // 提交快照：**深复制**，且在 PUT 之前取好 —— ACK 期间用户接着打字不能改到它
    const sentRows = priceCloneRows(collected.rows);
    const sentEdits = Number(price.edits || 0);
    const expectedVersion = price.version;
    price.saving = true;
    price.error = "";
    price.status = "正在保存…";
    renderPriceCatalog();
    let ack = null;
    try {
      ack = await apiFetch("/api/v1/prices", {
        method: "PUT",
        body: JSON.stringify({ expected_version: expectedVersion, entries: collected.entries }),
      });
    } catch (error) {
      price.saving = false;
      price.status = "";
      if (Number(error?.status) === 409) {
        await handlePriceConflict(sentRows, expectedVersion, error);
        return;
      }
      price.error = `保存失败（${error?.message || "未知错误"}）：服务端没有接受这次写入。你的草稿还在，可以重试。`;
      renderPriceCatalog();
      showToast("价格目录保存失败", "error");
      return;
    }
    const ackVersion = Number(ack?.catalog?.version);
    price.submitted = {
      version: Number.isSafeInteger(ackVersion) ? ackVersion : Number(expectedVersion) + 1,
      rows: priceCloneRows(sentRows),
      edits: sentEdits,
    };
    // PUT 200 **不是**确认：必须回读逐字段核对
    const fresh = await fetchPriceCatalog().catch(error => ({ ok: false, error }));
    if (!fresh.ok) {
      price.saving = false;
      price.status = "";
      price.error = `保存请求已被服务端接受（服务端版本 revision=${price.submitted.version}），但价格目录回读失败：${fresh.error?.message || "未知错误"}。**无法确认**这次保存的结果；你的草稿与待确认状态都已保留，稍后点「重新读取」核对，不要重复保存。`;
      renderPriceCatalog();
      showToast("已提交，但未能读回确认", "warning");
      return;
    }
    price.saving = false;
    const outcome = reconcilePriceWithServer(fresh.catalog);
    if (outcome.conflict) {
      showToast("已提交，但服务器现值与提交内容不同，未按成功处理", "warning");
    } else if (outcome.confirmed) {
      if (outcome.keptEdits) {
        showToast("已提交的内容已回读确认；你之后的新修改还没有保存", "info");
      } else {
        price.error = "";
        showToast(
          outcome.revisionAdvanced
            ? `当前服务器上的价格目录与你提交的内容一致（revision=${price.version}）。`
            : `价格目录已保存并回读核验（revision=${price.version}）。`,
          "success",
        );
      }
    } else {
      price.error = `回读到的目录比这次写入还旧（revision=${Number(fresh.catalog.version)}），没有按成功处理。`;
      showToast("回读结果比写入更旧，未能确认", "warning");
    }
    renderPriceCatalog();
  }

  async function handlePriceConflict(sentRows, expectedVersion, error) {
    const price = priceState();
    price.rows = priceCloneRows(price.rows);
    price.dirty = true;
    price.submitted = null;
    const fresh = await fetchPriceCatalog().catch(fetchError => ({ ok: false, error: fetchError }));
    if (!fresh.ok) {
      price.conflict = {
        mineVersion: expectedVersion,
        serverVersion: "（未取到）",
        serverRows: null,
        mineRows: priceCloneRows(price.rows),
        diffs: [],
        title: "价格目录已被其他会话改过（409），而且服务器最新版本也没读回来",
        note: "你的草稿已保留；请点「重新读取」再决定。",
      };
      price.error = `保存被拒（${error?.message || "版本冲突"}），而且服务器最新目录也没读回来：${fresh.error?.message || "未知错误"}。你的草稿已保留。`;
      renderPriceCatalog();
      showToast("版本冲突，且没能读到服务器最新版本", "error");
      return;
    }
    const serverRows = priceCloneRows((fresh.catalog.entries || []).map(priceRowFromEntry));
    price.version = Number(fresh.catalog.version);
    price.conflict = {
      mineVersion: expectedVersion,
      serverVersion: Number(fresh.catalog.version),
      serverRows,
      mineRows: priceCloneRows(price.rows),
      diffs: priceDiffLines(price.rows, serverRows),
      title: null,
      note: null,
    };
    price.error = `配置已被其他会话更新（409）：你编辑的是 revision=${expectedVersion}，服务器现在是 revision=${price.conflict.serverVersion}。你的草稿没有被覆盖，也没有自动重试。`;
    renderPriceCatalog();
    showToast("价格目录版本冲突", "warning");
  }

  async function reloadPriceCatalog({ quiet = false } = {}) {
    const price = priceState();
    if (!state.session?.can_manage_global) return { ok: false, reason: "forbidden" };
    if (price.loading || price.saving) return { ok: false, reason: "busy" };
    price.loading = true;
    renderPriceCatalog();
    const fresh = await fetchPriceCatalog().catch(error => ({ ok: false, error }));
    price.loading = false;
    if (!fresh.ok) {
      // 读失败 = 未知：不画可保存表单，也不把空目录当成"目录是空的"
      price.loaded = false;
      price.readError = fresh.error?.message || "读取失败";
      price.error = `价格目录读取失败：${price.readError}。${price.rows.length ? "你的草稿已保留；" : ""}读不到就不能保存。`;
      renderPriceCatalog();
      if (!quiet) showToast("价格目录读取失败，草稿已保留", "error");
      return { ok: false, error: fresh.error };
    }
    price.readError = "";
    price.loaded = true;
    const outcome = reconcilePriceWithServer(fresh.catalog);
    price.error = "";
    renderPriceCatalog();
    if (quiet) return { ok: true, outcome };
    if (outcome.conflict) {
      showToast("服务器上的价格目录和你的草稿不同，请选择用哪一份。", "warning");
    } else if (outcome.confirmed) {
      showToast(
        outcome.keptEdits
          ? "上一次提交已核对确认；你之后的新修改还没有保存。"
          : `已读取并核对服务器目录（revision=${price.version}）。`,
        outcome.keptEdits ? "info" : "success",
      );
    } else if (outcome.ignoredStale) {
      showToast("读到的目录比那次写入还旧，不构成确认。", "warning");
    } else {
      showToast("已读取服务器最新价格目录。", "info");
    }
    return { ok: true, outcome };
  }

  function patchPriceDraft(mutate, { rerender = false, dirty = false } = {}) {
    const price = priceState();
    if (dirty) {
      price.dirty = true;
      price.edits = Number(price.edits || 0) + 1;
    }
    if (typeof mutate === "function") mutate(price.rows);
    if (rerender) renderPriceCatalog();
  }

  function priceRowMarkup(row, index) {
    const value = field => attr(row?.[field] ?? "");
    const cell = (field, label, extra = {}) => `
              <label class="price-field">
                <span class="price-field-label">${escapeHtml(label)}</span>
                <input class="text-input" type="text" autocomplete="off" spellcheck="false"
                       data-price-row="${index}" data-price-field="${field}"
                       ${extra.inputmode ? `inputmode="${extra.inputmode}"` : ""}
                       value="${value(field)}"
                       placeholder="${attr(extra.placeholder || "")}"
                       aria-label="${attr(`${label}（第 ${index + 1} 行）`)}">
              </label>`;
    return `
          <div class="price-row" data-price-index="${index}">
            <div class="price-row-head">
              <span class="price-row-index">第 ${index + 1} 行</span>
              <button class="secondary-button" type="button" data-action="price-row-remove"
                      data-index="${index}"
                      aria-label="${attr(`移除第 ${index + 1} 行`)}">${icon("trash-2")}移除</button>
            </div>
            <div class="price-row-grid">
              ${cell("provider_key", "配置槽", { placeholder: "openai_compatible" })}
              ${cell("model", "模型", { placeholder: "gpt-4o-mini" })}
              ${cell("currency", "币种", { placeholder: "USD" })}
              ${cell("input", "输入价 / 百万 token", { inputmode: "decimal" })}
              ${cell("output", "输出价 / 百万 token", { inputmode: "decimal" })}
              ${cell("cache_read", "缓存读价 / 百万 token", { inputmode: "decimal" })}
              ${cell("cache_write", "缓存写价 / 百万 token", { inputmode: "decimal" })}
            </div>
          </div>`;
  }

  function renderPriceConflict(price) {
    const conflict = price.conflict;
    if (!conflict) return "";
    const mineLabel = "我的草稿";
    const headline = conflict.title
      || `配置已被其他会话更新（你编辑的是 revision=${escapeHtml(String(conflict.mineVersion))}，服务器现在是 revision=${escapeHtml(String(conflict.serverVersion))}）。`;
    const diffs = (conflict.diffs || []).length
      ? `<ul class="price-diff-list">${conflict.diffs.map(line => `<li>${escapeHtml(line)}</li>`).join("")}</ul>`
      : `<p class="field-hint">${conflict.serverRows ? "逐字段比下来没有差异（可能只是版本号变了）。" : "还没读到服务器上的值，无法列差异。"}</p>`;
    return `
        <div class="budget-conflict" data-role="price-conflict" role="alert">
          <p><strong>${escapeHtml(headline)}</strong></p>
          ${conflict.note ? `<p class="field-hint">${escapeHtml(conflict.note)}</p>` : ""}
          ${diffs}
          <p class="field-hint"><strong>你的草稿没有被覆盖，也没有自动重试。</strong></p>
          <p class="field-hint">重基 = 只把<strong>你在本轮改过</strong>的字段顶到服务器当前版本上，其余字段采用服务器值；改完仍需你自己点保存。</p>
          <div class="button-row">
            <button class="secondary-button" type="button" data-action="price-conflict-use-server">放弃${mineLabel}，用服务器值</button>
            <button class="secondary-button" type="button" data-action="price-conflict-apply-mine">保留我的改动并重基到 revision=${escapeHtml(String(conflict.serverVersion))}</button>
          </div>
        </div>`;
  }

  function priceCatalogBody() {
    const price = priceState();
    const head = sectionHead(
      "模型价格目录",
      "版本化只增：改价 = 新版本，历史账不追改。单位是「每百万 token」；空单价 = 未知，0 = 明确免费。",
      price.loaded
        ? `<button class="primary-button" type="button" data-action="price-save"${price.saving ? " disabled" : ""}>${icon("save")}保存新版本</button>`
        : "",
    );
    if (!price.loaded) {
      return `
        ${head}
        <p class="budget-state is-unknown" data-role="price-unknown">状态未知：${price.readError ? `无法读取价格目录（${escapeHtml(price.readError)}）` : "还没有读到价格目录"}。读到之前不能保存 —— 把"读不出来"当成"空目录"存上去，会让所有模型都变成没有价格。</p>
        ${price.rows.length ? `<p class="field-hint">你的草稿已保留（${price.rows.length} 行），读到服务器版本后可以继续。</p>` : ""}
        <div class="button-row">
          <button class="secondary-button" type="button" data-action="price-reload">${icon("refresh-cw")}重新读取</button>
        </div>`;
    }
    const rows = price.rows.length
      ? price.rows.map(priceRowMarkup).join("")
      : `<p class="field-hint" data-role="price-empty">目录里还没有任何条目。点「添加一行」开始录入；保存后会生成一个新版本。</p>`;
    return `
        ${head}
        <p class="field-hint" data-role="price-version">当前配置版本 revision=${escapeHtml(String(price.version))}${price.dirty ? "，你有未保存的修改" : ""}</p>
        <p class="field-hint" data-role="price-status" aria-live="polite">${escapeHtml(price.status || "")}</p>
        ${price.error ? `<p class="field-error" data-role="price-error" role="alert">${escapeHtml(price.error)}</p>` : ""}
        ${renderPriceConflict(price)}
        <div class="price-rows" data-role="price-rows">${rows}</div>
        <div class="button-row">
          <button class="secondary-button" type="button" data-action="price-row-add">${icon("plus")}添加一行</button>
          <button class="primary-button" type="button" data-action="price-save"${price.saving ? " disabled" : ""}>${icon("save")}保存新版本</button>
          <button class="secondary-button" type="button" data-action="price-reload">${icon("refresh-cw")}重新读取</button>
        </div>`;
  }

  /** 只替换这一张卡片，不整页重绘（整页重绘会抢走输入焦点）。 */
  function renderPriceCatalog() {
    if (!state.session?.can_manage_global) return;
    const holder = content.querySelector('[data-role="price-catalog"]');
    if (!holder) return;
    holder.innerHTML = priceCatalogBody();
    refreshIcons();
  }

  // ---- 按群预算编辑（仅超管） ------------------------------------------
  //
  // 只有超管看到可保存的表单；普通本群管理员仍是只读。读取失败时**不**渲染
  // 可保存表单 —— 把"读不出来"画成"未启用、可以存"是最危险的错。

  function budgetState() {
    if (!(state.budgetDrafts instanceof Map)) state.budgetDrafts = new Map();
    if (!(state.budgetErrors instanceof Map)) state.budgetErrors = new Map();
    if (!(state.budgetSaving instanceof Set)) state.budgetSaving = new Set();
    // 409/服务器 revision 前进时的"我的草稿 vs 服务器值"对比。没有它，界面只能
    // 要么静默覆盖用户正在编辑的内容，要么自动套用新 revision 覆盖别人的改动。
    if (!(state.budgetConflicts instanceof Map)) state.budgetConflicts = new Map();
    // money_covers **不落库、也不回读**（服务端只拿它做本次保存的校验范围）。
    // 这里只记"本机最近一次成功提交"，界面上必须如实这么标注，不能伪称是
    // 从数据库恢复出来的。
    if (!(state.budgetCoversRecent instanceof Map)) state.budgetCoversRecent = new Map();
    return state;
  }

  /** 严格非负整数：空白 = 不限制（返回 null），其余必须是纯数字。
   *
   * 刻意**不用** parseInt —— 它会把 "12abc" 悄悄读成 12、"1.9" 读成 1，
   * 等于替用户改了额度。 */
  function parseStrictLimit(raw) {
    const text = String(raw ?? "").trim();
    if (text === "") return { ok: true, value: null };
    if (!/^\d+$/.test(text)) return { ok: false, value: null };
    const value = Number(text);
    if (!Number.isSafeInteger(value)) return { ok: false, value: null };
    return { ok: true, value };
  }

  function parseCurrency(raw) {
    const text = String(raw ?? "").trim().toUpperCase();
    // 空 = 不指定币种。真实后端只在**填了金额上限**时才要求币种
    // （`save_budget`：money is not None and not currency → BudgetError）；
    // 只设次数/token 上限的群本来就没有币种，原来这里一律拒会让用户
    // 根本存不下不涉及金额的额度。要求由 collectBudgetDraft 单独把关。
    if (text === "") return { ok: true, value: "" };
    if (!/^[A-Z]{3}$/.test(text)) return { ok: false, value: "" };
    return { ok: true, value: text };
  }

  function budgetDraftFor(groupId) {
    budgetState();
    return state.budgetDrafts.get(String(groupId)) || null;
  }

  function rememberBudgetDraft(groupId, draft) {
    budgetState();
    // 草稿按群隔离：切群不会把 A 的编辑写进 B。
    state.budgetDrafts.set(String(groupId), draft);
  }

  function forgetBudgetDraft(groupId) {
    budgetState();
    state.budgetDrafts.delete(String(groupId));
  }

  function budgetErrorFor(groupId) {
    budgetState();
    return state.budgetErrors.get(String(groupId)) || "";
  }

  function setBudgetError(groupId, message) {
    budgetState();
    if (message) state.budgetErrors.set(String(groupId), message);
    else state.budgetErrors.delete(String(groupId));
  }

  function setBudgetConflict(groupId, conflict) {
    budgetState();
    state.budgetConflicts.set(String(groupId), conflict);
  }

  function clearBudgetConflict(groupId) {
    budgetState();
    state.budgetConflicts.delete(String(groupId));
  }

  /** 单独读一次预算。
   *
   * 从 ``loadGroupCostPanel`` 里抽出来是因为"保存之后是否真的生效"必须能**单独**
   * 判定：原来保存走 ``Promise.allSettled``，预算 GET 失败被 allSettled 吞掉，
   * 于是"读回失败"照样弹「预算已保存」—— 一句没证据的成功。 */
  async function fetchGroupBudget(groupId) {
    try {
      const document = await apiFetch(`/api/v1/groups/${encodeURIComponent(groupId)}/budget`);
      const budget = document?.budget || {};
      // 半截文档（缺 limits / revision / enabled / 限额键）会被后面的读取当成
      // "所有额度都不限制"，那是这条链上最危险的一种误读。宁可当成**读失败**：
      // 未确认状态保持，也不拿它去覆盖任何东西。
      const problem = budgetSnapshotProblem(groupId, budget);
      if (problem) {
        return {
          ok: false,
          incomplete: true,
          error: { message: `服务器返回的预算文档不可信（${problem}）` },
        };
      }
      return { ok: true, budget };
    } catch (error) {
      return { ok: false, error };
    }
  }

  /** 把**服务端真实的**预算文档转成草稿基线。
   *
   * 真实 GET /api/v1/groups/{id}/budget 的形状是
   * ``{budget: {group_id, enabled, protecting, revision, currency, warn_ratio,
   *  pause_ratio, limits: {daily_attempt_limit, daily_token_limit,
   *  daily_money_micro_limit, operation_limits}, today, budget_date}}`` ——
   * 三个限额与 operation_limits **嵌在 limits 里**。
   *
   * 曾经在顶层读 budget.daily_attempt_limit：真实 API 上永远是 undefined，于是
   * "重新读取"把界面上所有额度都显示成空白；管理员只改一个 token 再保存，就把
   * 服务器上已有的 attempts/tokens/功能次数**静默清成 null/{}** —— 一次点击
   * 抹掉整群额度保护。所以这里必须读 limits，且**字段缺失与 0 要分得开**。
   *
   * money_covers 是独立的一件事：服务端把它当作"本次保存的金额校验范围"，
   * **不落库、也不回读**。所以这里只回填"本机最近一次成功提交"，并且调用方
   * 必须在界面上说清楚它不来自数据库。 */
  function budgetDraftFromServer(groupId, budget) {
    const limits = budget && typeof budget.limits === "object" && budget.limits
      ? budget.limits
      : {};
    const limitText = value => (value == null ? "" : String(value));
    const operations = Object.entries(limits.operation_limits || {})
      .map(([key, value]) => ({ key: String(key), value: limitText(value) }));
    const recentCovers = groupId ? state.budgetCoversRecent?.get(String(groupId)) : null;
    // 真实 API 不回 money_covers；万一将来回了数组就按数组读，绝不按对象读
    // （按对象读会把 `[{provider_key,model}]` 读成 "0"/"[object Object]"）。
    const returnedCovers = Array.isArray(budget?.money_covers)
      ? budget.money_covers
        .filter(item => item && typeof item === "object")
        .map(item => ({
          key: String(item.provider_key ?? ""),
          value: String(item.model ?? ""),
        }))
      : null;
    return {
      revision: Number(budget?.revision ?? 0),
      // dirty = 用户在本机改过、还没被服务器确认。只有 clean 的草稿才允许被
      // 服务器的新 revision 静默替换。
      dirty: false,
      values: {
        enabled: Boolean(budget?.enabled),
        currency: String(budget?.currency || ""),
        attempt: limitText(limits.daily_attempt_limit),
        token: limitText(limits.daily_token_limit),
        money: limitText(limits.daily_money_micro_limit),
        operations,
        covers: (returnedCovers || (Array.isArray(recentCovers) ? recentCovers : []))
          .map(row => ({ key: String(row.key), value: String(row.value) })),
      },
    };
  }

  /** 键值行渲染。``numeric`` 决定第二格是不是"次数"。
   *
   * 金额覆盖的第二格是**模型名**（真实 API 收 ``{provider_key, model}``），不是
   * 数字：给它加 ``inputmode="numeric"`` 会调出数字键盘、手机上根本打不出模型名。
   */
  function kvRowsMarkup(kind, rows, hint, { numeric = true, valueHint = "次数" } = {}) {
    return `
      <div class="kv-rows" data-kv="${kind}">
        ${rows.map((row, index) => `
          <div class="kv-row">
            <input class="text-input" type="text" data-kv-key="${kind}" data-index="${index}"
                   value="${attr(row.key)}" placeholder="${attr(hint)}" autocomplete="off"
                   aria-label="${attr(hint)} ${numeric ? "键" : ""}第 ${index + 1} 行">
            <input class="text-input" type="text" ${numeric ? 'inputmode="numeric"' : ""}
                   data-kv-value="${kind}" data-index="${index}" value="${attr(row.value)}"
                   placeholder="${attr(valueHint)}" autocomplete="off"
                   aria-label="${attr(valueHint)} 第 ${index + 1} 行">
            <button class="secondary-button" type="button" data-action="budget-kv-remove"
                    data-kv="${kind}" data-index="${index}"
                    aria-label="移除第 ${index + 1} 行 ${attr(row.key || hint)}">${icon("trash-2")}移除</button>
          </div>`).join("")}
        <button class="secondary-button" type="button" data-action="budget-kv-add"
                data-kv="${kind}">${icon("plus")}添加一项</button>
      </div>`;
  }

  function renderBudgetEditor(groupId, draft) {
    const conflict = state.budgetConflicts?.get(String(groupId)) || null;
    return `
      <div class="budget-editor" data-group-id="${attr(groupId)}">
        <h4>编辑预算（仅最高管理员）</h4>
        <p class="field-hint">
          空白 = 不限制；<strong>0 = 不允许</strong>；只能填非负整数。
          金额单位是<strong>微货币</strong>（1,000,000 微 = 1 个货币单位），
          币种填 ISO 三字母代码。<strong>只改你想改的字段即可，其余额度会原样保留。</strong>
        </p>
        <label class="checkbox-row">
          <input type="checkbox" data-budget-field="enabled" ${draft.values.enabled ? "checked" : ""}>
          <span>启用本群预算护栏</span>
        </label>
        <label class="field-label" for="budget-currency-${attr(groupId)}">币种（ISO 三字母）</label>
        <input class="text-input" id="budget-currency-${attr(groupId)}" type="text"
               maxlength="3" autocomplete="off" data-budget-field="currency"
               value="${attr(draft.values.currency)}" placeholder="USD">
        <label class="field-label" for="budget-attempt-${attr(groupId)}">每日尝试次数上限</label>
        <input class="text-input" id="budget-attempt-${attr(groupId)}" type="text"
               inputmode="numeric" autocomplete="off" data-budget-field="attempt"
               value="${attr(draft.values.attempt)}" placeholder="空白 = 不限制">
        <label class="field-label" for="budget-token-${attr(groupId)}">每日 token 上限</label>
        <input class="text-input" id="budget-token-${attr(groupId)}" type="text"
               inputmode="numeric" autocomplete="off" data-budget-field="token"
               value="${attr(draft.values.token)}" placeholder="空白 = 不限制">
        <label class="field-label" for="budget-money-${attr(groupId)}">每日金额上限（微货币）</label>
        <input class="text-input" id="budget-money-${attr(groupId)}" type="text"
               inputmode="numeric" autocomplete="off" data-budget-field="money"
               value="${attr(draft.values.money)}" placeholder="空白 = 不限制（必须同时填币种）">
        <details class="advanced-disclosure" data-role="budget-details-operations">
          <summary>功能操作次数（每功能一行，值为非负整数）</summary>
          ${kvRowsMarkup("operations", draft.values.operations, "功能名（如 tts）", { numeric: true, valueHint: "次数" })}
          <p class="field-hint">每一行都要填全：功能名不能空，次数不能空（空 ≠ 不限制，
            写 0 才是"一次都不许"）。留一整行都空着表示不添加。同一功能不能重复。</p>
        </details>
        <details class="advanced-disclosure" data-role="budget-details-covers">
          <summary>金额校验模型列表（本次保存有效）</summary>
          ${kvRowsMarkup("covers", draft.values.covers, "provider_key", { numeric: false, valueHint: "model" })}
          <p class="field-hint">
            这是<strong>本次保存的金额上限校验范围</strong>，用于在启用「每日金额上限」时
            逐个确认这些模型有完整单价、且币种与预算币种一致。
            <strong>它不写入配置、也不会被回读</strong>（服务端只拿它做本次校验），
            所以刷新页面后这份列表不会被服务器恢复；本机会保留你最近一次成功提交的
            内容作为下次的初始值。只有在填了金额上限时它才有作用。
          </p>
        </details>
        ${conflict ? renderBudgetConflict(groupId, conflict) : ""}
        <p class="field-error" data-role="budget-error" data-group-id="${attr(groupId)}"
           role="alert" hidden></p>
        <p class="field-hint" data-role="budget-revision">
          当前配置版本 revision=${escapeHtml(String(draft.revision))}${
            draft.submitted
              ? `；已提交 revision=${escapeHtml(String(draft.submitted.revision))} 但<strong>尚未回读确认</strong>`
              : ""
          }
        </p>
        <div class="button-row">
          <button class="primary-button" type="button" data-action="budget-save"
                  data-group-id="${attr(groupId)}">${icon("save")}保存预算</button>
          <button class="secondary-button" type="button" data-action="budget-reload"
                  data-group-id="${attr(groupId)}">${icon("refresh-cw")}重新读取</button>
        </div>
      </div>`;
  }

  /** 409 / 服务器 revision 前进时的对比面板。
   *
   * 原实现把「重新读取」写成 forgetBudgetDraft + clearError：按钮文案承诺
   * "对比差异"，实际直接把用户草稿删了 —— 用户一按就丢掉全部编辑。这里必须
   * 真的把差异列出来，并给两条**互斥且都得由用户选**的路。 */
  function renderBudgetConflict(groupId, conflict) {
    const diffs = conflict.diffs || [];
    const mineLabel = conflict.mineLabel || "我的草稿";
    // title 存在时用它当标题（例如"你的提交没有被确认"）：revision 变大并不等于
    // 我那次写入生效了，不能一律说成"其他会话更新"。
    const headline = conflict.title
      ? `${escapeHtml(conflict.title)}（你的是 revision=${escapeHtml(String(conflict.mineRevision))}，`
        + `服务器现在是 revision=${escapeHtml(String(conflict.serverRevision))}）。`
      : `配置已被其他会话更新（你编辑的是 revision=${escapeHtml(String(conflict.mineRevision))}，`
        + `服务器现在是 revision=${escapeHtml(String(conflict.serverRevision))}）。`;
    const titleHint = conflict.title ? `<p class="field-hint">${escapeHtml(conflict.note || "")}</p>` : "";
    return `
      <div class="budget-conflict" data-role="budget-conflict" data-group-id="${attr(groupId)}">
        <p class="field-error" role="alert">
          ${headline}
          <strong>你的草稿没有被覆盖，也没有自动重试。</strong>
        </p>
        ${titleHint}
        ${diffs.length
          ? `<ul class="budget-diff">${diffs.map(line => `<li>${escapeHtml(line)}</li>`).join("")}</ul>`
          : `<p class="field-hint">逐字段比较后没有发现差异。</p>`}
        <p class="field-hint">金额校验模型列表服务器不保存，不参与对比。</p>
        <div class="button-row">
          <button class="secondary-button" type="button" data-action="budget-conflict-use-server"
                  data-group-id="${attr(groupId)}">放弃${escapeHtml(mineLabel)}，用服务器值</button>
          <button class="primary-button" type="button" data-action="budget-conflict-apply-mine"
                  data-group-id="${attr(groupId)}">保留${escapeHtml(mineLabel)}并覆盖（revision=${escapeHtml(String(conflict.serverRevision))}）</button>
        </div>
      </div>`;
  }

  /** 读「功能名 → 次数」这些行。
   *
   * 四个都必须明确拒绝，不能吞：
   *   * 名称空、值有内容  —— 原实现 `if (!key) return;` 直接把这一行**丢掉**，
   *     用户以为加上了，其实一个字都没提交；
   *   * 名称有内容、值空 —— 原实现 parseStrictLimit("") 得到 null，再
   *     String(null)="null"、Number("null")=NaN，JSON 里变成 null，于是"留空"
   *     悄悄等于"不限制"；运维以为设了 0，实际把限制**解开**了；
   *   * 值不是非负整数；
   *   * 同一功能名出现两次 —— 原实现 Object.fromEntries 静默保留最后一个。
   */
  function readNumericRows(root, kind) {
    const keys = [...root.querySelectorAll(`[data-kv-key="${kind}"]`)];
    const values = [...root.querySelectorAll(`[data-kv-value="${kind}"]`)];
    const rows = [];
    const seen = new Set();
    for (let index = 0; index < keys.length; index += 1) {
      const key = String(keys[index].value || "").trim();
      const raw = String(values[index]?.value ?? "").trim();
      if (!key && !raw) continue; // 整行空 = 没有这一项
      if (!key) {
        return { ok: false, message: `第 ${index + 1} 行的名称是空的，但填了「${raw}」；请补上名称或删掉这一行。` };
      }
      if (!raw) {
        return {
          ok: false,
          message: `「${key}」的次数是空的。空白不等于不限制：要限制就填 0 或正整数，不想限制就删掉这一行。`,
        };
      }
      const parsed = parseStrictLimit(raw);
      if (!parsed.ok) {
        return { ok: false, message: `「${key}」的次数只能填非负整数（不接受小数、负号或夹杂其它字符）。` };
      }
      if (seen.has(key)) {
        return { ok: false, message: `「${key}」出现了两次，同一功能只能有一行。` };
      }
      seen.add(key);
      rows.push({ key, value: String(parsed.value) });
    }
    return { ok: true, rows };
  }

  /** 读金额校验模型行。两格都是文本，必须**同时**填 —— 单侧留空是错误，
   * 不是"空行"，也不能被忽略。 */
  function readCoverRows(root) {
    const keys = [...root.querySelectorAll('[data-kv-key="covers"]')];
    const values = [...root.querySelectorAll('[data-kv-value="covers"]')];
    const rows = [];
    const seen = new Set();
    for (let index = 0; index < keys.length; index += 1) {
      const key = String(keys[index].value || "").trim();
      const model = String(values[index]?.value ?? "").trim();
      if (!key && !model) continue;
      if (!key || !model) {
        return {
          ok: false,
          message: `第 ${index + 1} 行的 provider_key 与 model 都要填（现在 provider_key=「${key}」、model=「${model}」）；只填一半不会被忽略。`,
        };
      }
      const identity = `${key}\u0000${model}`;
      if (seen.has(identity)) {
        return { ok: false, message: `provider_key=${key} / model=${model} 重复了，请删掉多余的一行。` };
      }
      seen.add(identity);
      rows.push({ key, value: model });
    }
    return { ok: true, rows };
  }

  /** 把草稿值归一成"可以逐字段比较"的字符串地图（服务器草稿与用户草稿通用）。 */
  function budgetComparableValues(values) {
    const operations = {};
    for (const row of values.operations || []) {
      operations[String(row.key)] = String(row.value);
    }
    return {
      enabled: values.enabled ? "启用" : "关闭",
      currency: String(values.currency || ""),
      attempt: String(values.attempt ?? ""),
      token: String(values.token ?? ""),
      money: String(values.money ?? ""),
      operations,
    };
  }

  const BUDGET_FIELD_LABELS = {
    enabled: "启用状态",
    currency: "币种",
    attempt: "每日尝试次数上限",
    token: "每日 token 上限",
    money: "每日金额上限（微）",
  };

  /** 逐字段列出「我的草稿 vs 服务器回读」的差异。
   *
   * money_covers 不在比较范围内：服务端不保存它，回读永远是空的，把它算成
   * "差异"会得到永久的假冲突。 */
  function budgetDiffLines(mine, theirs) {
    const left = budgetComparableValues(mine.values || mine);
    const right = budgetComparableValues(theirs.values || theirs);
    const lines = [];
    for (const field of ["enabled", "currency", "attempt", "token", "money"]) {
      if (left[field] !== right[field]) {
        lines.push(
          `${BUDGET_FIELD_LABELS[field]}：我的草稿=${left[field] === "" ? "不限制" : left[field]}`
          + `，服务器=${right[field] === "" ? "不限制" : right[field]}`,
        );
      }
    }
    const names = new Set([...Object.keys(left.operations), ...Object.keys(right.operations)]);
    for (const name of [...names].sort()) {
      if (left.operations[name] !== right.operations[name]) {
        lines.push(
          `功能「${name}」次数：我的草稿=${left.operations[name] ?? "（无）"}`
          + `，服务器=${right.operations[name] ?? "（无）"}`,
        );
      }
    }
    return lines;
  }

  /** 服务端回读的文档哪里有毛病（空串 = 可以拿去核对）。
   *
   * 按**真实** schema 逐项验，而不是"有 limits 和 revision 就算完整"：
   *
   *   * ``group_id`` 必须与请求的群一致 —— 张冠李戴的回包（代理串了、缓存串了）
   *     会被拿去和"我的提交"比较，然后宣布别人的配置就是你的；
   *   * ``enabled`` 必须是**真布尔**、``currency`` 必须是字符串；
   *   * ``limits`` 的三个限额**键必须存在**："缺字段"与"值是不限制（null）"
   *     是完全不同的事，缺字段被读成不限制就等于把"我不知道"画成"没有上限"；
   *   * ``operation_limits`` 必须是对象（数组/字符串都不是合法的功能次数表）；
   *   * ``revision`` 与三个限额的值必须是**真正的数字**、非负、安全整数
   *     （``null`` 只对限额合法，表示"不限制"）。
   *
   * 最后这一条是**必须按类型判**、不能写 ``Number(value)`` 再判有限的：``Number(null)``、
   * ``Number(false)``、``Number("")`` 全都是 0，于是 revision 是 null/false、限额是字符串
   * ``"invalid"`` 这种半坏回包会被当成"合法且不限制"，直接放出一张可编辑的表单 ——
   * 用户看到"没有上限"，一保存就把真实额度抹掉。半坏回包必须按**读失败**处理。
   *
   * 任何一项不对 → 按**读失败**处理：草稿与待确认快照都保留，0 写入，不宣布任何
   * 确认。这里不校验 ``today`` 之类的非关键报表字段，也不要求后端新增字段。 */
  function budgetLimitValueProblem(value, label) {
    if (value === null) return "";
    if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
      return `${label} 必须是 null 或非负整数`;
    }
    return "";
  }

  function budgetSnapshotProblem(groupId, budget) {
    if (!budget || typeof budget !== "object") return "响应里没有 budget 对象";
    if (budget.group_id == null || String(budget.group_id) !== String(groupId)) {
      return `回包里的群不是这个群（收到 ${budget.group_id}）`;
    }
    if (
      typeof budget.revision !== "number"
      || !Number.isSafeInteger(budget.revision)
      || budget.revision < 0
    ) {
      return "revision 必须是 0 或非负整数";
    }
    if (typeof budget.enabled !== "boolean") return "enabled 不是布尔值";
    if (typeof budget.currency !== "string") return "currency 不是字符串";
    if (!budget.limits || typeof budget.limits !== "object") return "缺少 limits";
    for (const key of [
      "daily_attempt_limit",
      "daily_token_limit",
      "daily_money_micro_limit",
    ]) {
      if (!Object.prototype.hasOwnProperty.call(budget.limits, key)) {
        return `limits 缺少 ${key}`;
      }
      const problem = budgetLimitValueProblem(budget.limits[key], `limits.${key}`);
      if (problem) return problem;
    }
    const operations = budget.limits.operation_limits;
    if (!operations || typeof operations !== "object" || Array.isArray(operations)) {
      return "limits.operation_limits 不是对象";
    }
    for (const [name, value] of Object.entries(operations)) {
      const problem = budgetLimitValueProblem(
        value,
        `limits.operation_limits.${name}`,
      );
      if (problem) return problem;
    }
    return "";
  }

  /** 提交快照必须是**独立的一份数据**。
   *
   * 原先 ``submitted.values`` 与 ``draft.values`` 是**同一个对象**（保存时
   * ``values: collected.values`` 与 ``submitted.values: collected.values`` 指向
   * 同一份），而用户的每一次输入都原地改 ``draft.values``。于是：
   *
   *   * 「提交了 token=22、服务器顺利写入，随后我又把表单改成 23」→ 提交快照被
   *     自己的新编辑污染成 23，回读时反而判成"你的提交没有被确认：服务器现值与
   *     你提交的内容不一样"——**假冲突**，而且"旧提交已确认/新编辑未保存"这个
   *     真实状态被完全掩盖；
   *   * ACK 还没回来时打字 → 快照跟着变，ACK 回来后又被无条件 ``dirty=false``，
   *     于是草稿被贴上"我的提交"的错误标签。
   *
   * 所以快照要逐层深复制：顶层字段 + operations/covers 数组 + 每一行对象，都不
   * 与草稿、也不要与 ``state.budgetCoversRecent`` 共享引用。 */
  function budgetCloneValues(values) {
    if (!values || typeof values !== "object") return null;
    const cloneRows = rows => (Array.isArray(rows) ? rows : [])
      .map(row => ({ key: String(row?.key ?? ""), value: String(row?.value ?? "") }));
    return {
      ...values,
      operations: cloneRows(values.operations),
      covers: cloneRows(values.covers),
    };
  }

  /** 两份**已持久化字段**是否完全一致（enabled / currency / 三个限额 /
   * operation_limits）。money_covers 不落库，拿它比较只会得到永久的假冲突。 */
  function budgetValuesMatch(left, right) {
    return budgetDiffLines({ values: left }, { values: right }).length === 0;
  }

  /** "服务器现值与本地一致"时的提示文案。
   *
   * 有提交快照时说的是"你提交的内容"，否则只能说"你表单里的内容" ——
   * 不能把"恰好和别人写的一样"说成"你的写入被确认了"。 */
  function budgetMatchMessage(outcome, serverDraft) {
    return outcome.submittedRevision != null
      ? `当前服务器上的值与你提交的内容一致（revision=${serverDraft.revision}）。`
      : `当前服务器上的值与你表单里的内容一致（revision=${serverDraft.revision}）。`;
  }

  /** 唯一一份「服务器回读 → 与本地状态对账」的判定。
   *
   * 保存后的回读、点「重新读取」、成本面板的 cost-reload、切页回来重新渲染 ——
   * **全部**走这里。判定标准只有一条：把**提交快照**（或本地草稿）与服务器现值
   * **逐字段**比较，绝不因为 revision 变大就宣布成功。
   *
   * 原实现（renderBudgetPanel 里）写的是
   * ``draft.pendingRevision != null && serverRevision >= pendingRevision``
   * 就 ``draft = fromServer()`` + toast「之前未确认的保存已回读确认」：
   * revision 只要往前走（别的会话写了**别的值**也一样），用户那份根本没被确认的
   * 草稿就被替换成别人的配置，还被报成自己的保存成功。revision 变大只证明
   * "有别的东西写了"，完全不证明"我那次写入生效了"。
   *
   * 返回的 outcome 供调用方决定提示文案：
   *   ``confirmed``      提交快照与服务器现值逐字段一致 → 真确认；
   *   ``conflict``       不一致 → 保留草稿与提交快照，摆差异让用户选，绝不自动
   *                      用服务器值覆盖，也绝不报成功；
   *   ``keptEdits``      保留了用户自己的编辑（revision 没动 = 未保存的编辑，
   *                      不是别人改过）；
   *   ``adopted``        采用了服务器值（本地没有未提交编辑）；
   *   ``ignoredStale``   读到的 revision 比"已被接受的写入"还旧 → 这次回读不构成
   *                      任何证据，保持未确认（很可能是更早发出的 GET 回来得晚）。
   */
  function reconcileBudgetWithServer(groupId, serverDraft) {
    const outcome = {
      adopted: false,
      confirmed: false,
      conflict: false,
      keptEdits: false,
      revisionAdvanced: false,
      ignoredStale: false,
      submittedRevision: null,
    };
    const draft = budgetDraftFor(groupId);
    if (!draft) {
      rememberBudgetDraft(groupId, serverDraft);
      outcome.adopted = true;
      return outcome;
    }
    const serverRevision = Number(serverDraft.revision);
    const submitted = draft.submitted || null;
    if (submitted) {
      outcome.submittedRevision = Number(submitted.revision);
      if (serverRevision < Number(submitted.revision)) {
        // 「revision 比已确认的写入还旧」不得算成功，也不该制造冲突 —— 这次回读
        // 什么都没证明。保持待确认，等一次更新的回读。
        outcome.ignoredStale = true;
        return outcome;
      }
      const diffs = budgetDiffLines({ values: submitted.values }, serverDraft);
      if (diffs.length) {
        // 服务器现值 ≠ 我提交的内容：这**不是**成功。保留草稿与提交快照，
        // 逐字段列差异，由用户自己选用哪一份；服务器值不被自动写回表单。
        setBudgetConflict(groupId, {
          mineRevision: Number(submitted.revision),
          serverRevision,
          serverDraft,
          mineValues: draft.values,
          mineLabel: draft.dirty ? "我的草稿（提交之后又改过）" : "我的提交",
          diffs: budgetDiffLines(
            draft.dirty ? draft : { values: submitted.values },
            serverDraft,
          ),
          title: "你的提交没有被确认：服务器上的现值和你提交的内容不一样",
          note: "revision 往前走了只说明有别的东西写了，并不证明你那次写入生效。",
        });
        setBudgetError(groupId, "");
        outcome.conflict = true;
        return outcome;
      }
      // 逐字段一致 —— 这才叫确认。若服务器 revision 已往前走，只说"现值一致"，
      // 不声称"读到了历史上那次 revision 的写入"。
      outcome.confirmed = true;
      outcome.revisionAdvanced = serverRevision !== Number(submitted.revision);
      // 只清掉"这次提交的确认标识"：用户提交**之后**的新编辑留在表单里，两者
      // 分清楚（下面 outcome.keptEdits 分支给文案）。草稿值另外换一份独立副本 ——
      // 快照是"我当时提交了什么"的证据，不许再和可编辑表单共享引用。
      draft.submitted = null;
      draft.values = budgetCloneValues(draft.values) || draft.values;
      draft.revision = serverRevision;
      if (draft.dirty) {
        // 等待确认期间用户又改了：旧提交确认了，新编辑留着，两者分清楚。
        rememberBudgetDraft(groupId, draft);
        clearBudgetConflict(groupId);
        setBudgetError(
          groupId,
          `上一次提交（revision=${submitted.revision}）已经核对确认；`
          + "你之后又改了内容，这些新修改**还没有保存**。",
        );
        outcome.keptEdits = true;
        return outcome;
      }
      rememberBudgetDraft(groupId, serverDraft);
      clearBudgetConflict(groupId);
      setBudgetError(groupId, "");
      outcome.adopted = true;
      return outcome;
    }
    if (draft.dirty) {
      if (serverRevision === Number(draft.revision)) {
        // revision 没动 = 用户自己有没保存的编辑，不是"别人改过"。
        outcome.keptEdits = true;
        return outcome;
      }
      if (budgetValuesMatch(draft.values, serverDraft.values)) {
        // 值恰好一致：收口（不必再写一遍），但这是"现值一致"，不是别的证明。
        rememberBudgetDraft(groupId, serverDraft);
        clearBudgetConflict(groupId);
        setBudgetError(groupId, "");
        outcome.confirmed = true;
        outcome.adopted = true;
        outcome.revisionAdvanced = true;
        return outcome;
      }
      setBudgetConflict(groupId, {
        mineRevision: Number(draft.revision),
        serverRevision,
        serverDraft,
        mineValues: draft.values,
        mineLabel: "我的草稿",
        diffs: budgetDiffLines(draft, serverDraft),
        title: null,
        note: null,
      });
      outcome.conflict = true;
      return outcome;
    }
    if (serverRevision !== Number(draft.revision)) {
      rememberBudgetDraft(groupId, serverDraft);
      clearBudgetConflict(groupId);
      setBudgetError(groupId, "");
      outcome.adopted = true;
      return outcome;
    }
    // 没有本地编辑、revision 也没变：用服务器值刷新（今日用量等），保留表单内容。
    rememberBudgetDraft(groupId, { ...serverDraft, values: draft.values });
    return outcome;
  }

  function collectBudgetDraft(groupId) {
    const root = content.querySelector(`.budget-editor[data-group-id="${cssEscape(groupId)}"]`);
    if (!root) return { ok: false, message: "表单不存在，请重新读取。" };
    const draft = budgetDraftFor(groupId);
    if (!draft) return { ok: false, message: "尚未成功读取过预算，不能保存。" };

    const currency = parseCurrency(root.querySelector('[data-budget-field="currency"]')?.value);
    if (!currency.ok) return { ok: false, message: "币种必须是三个字母的 ISO 代码，例如 USD。" };

    const limits = {};
    for (const [field, label] of [
      ["attempt", "每日尝试次数上限"],
      ["token", "每日 token 上限"],
      ["money", "每日金额上限"],
    ]) {
      const parsed = parseStrictLimit(root.querySelector(`[data-budget-field="${field}"]`)?.value);
      if (!parsed.ok) {
        return { ok: false, message: `${label}只能填非负整数或留空（不接受小数、负号或夹杂其它字符）。` };
      }
      limits[field] = parsed.value;
    }
    // 服务端的合同：有金额上限就必须有币种，否则整次保存 400。这里先拦，
    // 免得用户填完一大串才被拒。
    if (limits.money !== null && currency.value === "") {
      return { ok: false, message: "填了每日金额上限就必须同时填币种。" };
    }

    const operations = readNumericRows(root, "operations");
    if (!operations.ok) return { ok: false, message: operations.message };
    const covers = readCoverRows(root);
    if (!covers.ok) return { ok: false, message: covers.message };

    const values = {
      enabled: Boolean(root.querySelector('[data-budget-field="enabled"]')?.checked),
      currency: currency.value,
      attempt: limits.attempt,
      token: limits.token,
      money: limits.money,
      operations: operations.rows,
      covers: covers.rows,
    };
    // 草稿存一份**独立副本**：调用方随后会把返回的 values 当成"这次提交的内容"
    // 深复制成提交快照，而用户接着打字是原地改草稿 —— 草稿与快照绝不能是同一个
    // 对象（这正是"提交后被自己的新编辑污染"的根源）。
    rememberBudgetDraft(groupId, { ...draft, values: budgetCloneValues(values) });
    return {
      ok: true,
      values,
      payload: {
        revision: draft.revision,
        enabled: values.enabled,
        currency: values.currency,
        daily_attempt_limit: values.attempt,
        daily_token_limit: values.token,
        daily_money_micro_limit: values.money,
        operation_limits: Object.fromEntries(
          values.operations.map(row => [row.key, Number(row.value)]),
        ),
        // 真实 API 收的是**数组** ``[{provider_key, model}]``。曾经发的是
        // ``{key: number}`` 映射，服务端直接 400「money_covers 必须是数组」。
        money_covers: values.covers.map(row => ({ provider_key: row.key, model: row.value })),
      },
    };
  }

  /** 保存预算，并且**只有在逐字段回读核验通过之后**才报告成功。
   *
   * 三种结果必须分开说（原实现把它们混成一句「预算已保存」）：
   *   1. 写被接受 + 回读逐字段一致   → 才算保存成功；
   *   2. 写被接受 + 回读失败/不一致  → 「已提交，未能确认」或冲突面板，
   *      保留草稿与提交快照，**不重发**；
   *   3. 写被拒（409/400）           → 保留草稿，给明确选择。
   *
   * 还有一条更硬的规矩：**上一次提交还没确认时，点保存不许再盲发一个同样的
   * PUT** —— 先 GET 核对，确认了才继续；读不到就保持待确认，一个写请求都不发。
   * 否则用户连点两次就会写出两个 revision，而第一次是否生效始终没人知道。
   */
  async function saveGroupBudget(groupId) {
    budgetState();
    if (state.budgetSaving.has(String(groupId))) return;
    // 冲突还没解决时不许保存：否则就是拿旧 revision 再撞一次 409，或者更糟 ——
    // 静默把服务器上的值覆盖掉。必须先由用户在冲突面板里明确选一份。
    if (state.budgetConflicts?.has(String(groupId))) {
      setBudgetError(
        groupId,
        "服务器上的配置与你的草稿不同：请先在冲突面板里明确选择「用服务器值」或"
        + "「保留我的草稿并覆盖」，然后才能再次保存。",
      );
      renderBudgetError(groupId);
      return;
    }
    let pending = budgetDraftFor(groupId)?.submitted || null;
    if (pending) {
      const fresh = await fetchGroupBudget(groupId);
      if (!fresh.ok) {
        setBudgetError(
          groupId,
          `上一次提交（revision=${pending.revision}）还没能确认，而且这次也读不到`
          + `服务器配置：${fresh.error?.message || "未知错误"}。`
          + "没有发出新的写入；请稍后重试读取，在此之前不要重复保存。",
        );
        renderBudgetError(groupId);
        showToast("上次提交仍未确认，读取失败，未发出新的写入。", "warning");
        return;
      }
      // 与「保存后的回读」「重新读取」**共用同一个**核对函数：旧 revision 的
      // 回读在这里同样不构成确认。这条分支以前自己算 diff、自己清 submitted，
      // 唯一缺的就是"revision 比待确认提交还旧"这一档 —— 于是同一个乱序 GET，
      // 走 reload 会被拒，走保存却被当成成功。
      const outcome = reconcileBudgetWithServer(
        groupId,
        budgetDraftFromServer(groupId, fresh.budget),
      );
      patchBudgetDraft(groupId, () => { }, { rerender: true });
      if (outcome.ignoredStale) {
        setBudgetError(
          groupId,
          `上一次提交（revision=${pending.revision}）还没能确认：这次读到的配置`
          + `（revision=${fresh.budget.revision}）比那次写入还旧，不构成确认。`
          + "没有发出新的写入；请稍后重试读取。",
        );
        renderBudgetError(groupId);
        showToast("上次提交仍未确认（读到的配置更旧），未发出新的写入。", "warning");
        return;
      }
      if (outcome.conflict) {
        showToast("上次提交与服务器现值不同，请先选择用哪一份。", "warning");
        return;
      }
      if (!outcome.confirmed) {
        // 没确认也没冲突（例如本地已被别的入口收口）：保持现状，不发新写入。
        setBudgetError(
          groupId,
          `上一次提交（revision=${pending.revision}）尚未确认，因此没有发出新的写入；`
          + "请点「重新读取」核对。",
        );
        renderBudgetError(groupId);
        showToast("上次提交仍未确认，未发出新的写入。", "warning");
        return;
      }
      const confirmedDraft = budgetDraftFor(groupId);
      if (!confirmedDraft?.dirty) {
        await loadGroupCostPanel(groupId);
        showToast(
          Number(fresh.budget.revision) === Number(pending.revision)
            ? `上一次提交已核对确认（revision=${fresh.budget.revision}），没有重复写入。`
            : `当前服务器上的值与你提交的内容一致（revision=${fresh.budget.revision}），`
              + "没有重复写入。",
          "success",
        );
        return;
      }
      // 用户之后又改了：先确认旧提交，再保存新修改（这是**新的**写入意图，
      // 不是把同一个 PUT 重发一遍）。
      setBudgetError(
        groupId,
        `上一次提交（revision=${pending.revision}）已经核对确认；`
        + "现在保存你之后的新修改。",
      );
    }
    const collected = collectBudgetDraft(groupId);
    if (!collected.ok) {
      setBudgetError(groupId, collected.message);
      renderBudgetError(groupId);
      return;
    }
    // **发请求之前**就把"这次提交的内容"深复制出来。PUT 的应答过一会儿才回来，
    // 这期间用户完全可能接着打字，而打字是原地改 draft.values 的 —— 快照必须与
    // 草稿彻底分开，否则 ACK 回来后记下的"提交快照"已经是用户改过的新内容。
    const sentValues = budgetCloneValues(collected.values);
    const sentEdits = Number(budgetDraftFor(groupId)?.edits || 0);
    state.budgetSaving.add(String(groupId));
    setBudgetError(groupId, "");
    renderBudgetError(groupId);
    try {
      let ack;
      try {
        ack = await apiFetch(`/api/v1/groups/${encodeURIComponent(groupId)}/budget`, {
          method: "PUT",
          body: JSON.stringify(collected.payload),
        });
      } catch (error) {
        const status = Number(error?.status || 0);
        if (status === 409) {
          await handleBudgetConflict(groupId, collected);
          showToast("保存冲突（409）：草稿已保留，请选择用哪一份。", "warning");
        } else {
          setBudgetError(
            groupId,
            `${error.message || "保存失败"}（草稿已保留，可重试）。`,
          );
          renderBudgetError(groupId);
          showToast("预算没有保存成功", "error");
        }
        return;
      }

      // 写已经被服务端接受。此时 revision 是**服务端给的**，不是我们猜的。
      const ackRevision = Number(ack?.revision ?? collected.payload.revision);
      const draft = budgetDraftFor(groupId) || { values: sentValues, edits: sentEdits };
      // 金额校验模型列表不落库，先在本地记住"最近一次成功提交"，回读时用它
      // 回填 —— 存的是**快照自己的副本**，不能和草稿共享数组。
      state.budgetCoversRecent.set(
        String(groupId),
        (sentValues.covers || []).map(row => ({ ...row })),
      );
      // ACK 期间用户又改过没有？改过就**不能**把 dirty 清掉：那些编辑还没提交，
      // 把它们标成"我的提交"会同时丢掉"未保存"的语义。用编辑代数判断，而不是
      // 靠"ACK 回来了"就当没有新编辑。
      const editsNow = Number(draft.edits || 0);
      const editedDuringAck = editsNow !== sentEdits;
      // 提交快照与用户草稿**分开记，而且是两份独立的对象**：确认时比的是这一份
      // 快照（提交那一刻的内容），用户之后的编辑只改草稿那一份。
      rememberBudgetDraft(groupId, {
        ...draft,
        values: editedDuringAck
          ? (budgetCloneValues(draft.values) || sentValues)
          : budgetCloneValues(sentValues),
        submitted: {
          revision: ackRevision,
          values: budgetCloneValues(sentValues),
          edits: sentEdits,
        },
        revision: Number.isFinite(ackRevision)
          ? ackRevision
          : Number(collected.payload.revision),
        dirty: editedDuringAck,
      });

      const fresh = await fetchGroupBudget(groupId);
      if (!fresh.ok) {
        // 写已接收、读回未确认。**不**显示成功，**不**重发（重发可能 409 或重复写）。
        setBudgetError(
          groupId,
          `保存请求已被服务端接受（revision=${ackRevision}），但配置回读失败：`
          + `${fresh.error?.message || "未知错误"}。因此**无法确认**它是否真的生效。`
          + `你提交的内容已保留；请点「重新读取」核验，在此之前不要重复保存。`,
        );
        patchBudgetDraft(groupId, () => { }, { rerender: true });
        showToast("已提交，但读回失败，未能确认", "warning");
        return;
      }

      const serverDraft = budgetDraftFromServer(groupId, fresh.budget);
      const outcome = reconcileBudgetWithServer(groupId, serverDraft);
      if (outcome.confirmed) {
        clearBudgetConflict(groupId);
        if (outcome.keptEdits) {
          // 等待回读期间用户又改了：只对**已提交的那一份**宣布确认，新编辑明确
          // 标成"还没有保存"，不显示"已保存"，也不发新的写入。
          patchBudgetDraft(groupId, () => { }, { rerender: true });
          renderBudgetError(groupId);
          showToast("已提交的内容已回读确认；你之后的新修改还没有保存", "info");
          return;
        }
        setBudgetError(groupId, "");
        await loadGroupCostPanel(groupId);
        showToast(
          outcome.revisionAdvanced
            ? budgetMatchMessage(outcome, serverDraft)
            : `预算已保存并回读核验（revision=${serverDraft.revision}）。`,
          "success",
        );
        return;
      }
      if (outcome.conflict) {
        patchBudgetDraft(groupId, () => { }, { rerender: true });
        showToast("已提交，但服务器现值与提交内容不同，未按成功处理", "warning");
        return;
      }
      // 走到这里说明回读到的 revision 比刚被接受的写入还旧（乱序 GET）：这次
      // 回读什么都没证明，保持未确认，绝不报成功。
      setBudgetError(
        groupId,
        `保存请求已被服务端接受（revision=${ackRevision}），但回读到的配置比这次`
        + `写入还旧（revision=${serverDraft.revision}），因此没有按成功处理。`
        + "你提交的内容已保留；请点「重新读取」核验。",
      );
      patchBudgetDraft(groupId, () => { }, { rerender: true });
      showToast("已提交，但回读结果比写入更旧，未能确认", "warning");
    } finally {
      state.budgetSaving.delete(String(groupId));
    }
  }

  /** 409 之后：把用户草稿**原样留下**，再去取服务器当前值做逐字段对比。
   *
   * 原实现承诺"请先点重新读取对比差异"，而「重新读取」的代码是
   * ``forgetBudgetDraft + clearError`` —— 按钮点下去草稿就没了，从来没有任何
   * 对比。这里既保留草稿，也真的算差异，并且**绝不**自动换 revision 重试。 */
  async function handleBudgetConflict(groupId, collected) {
    // 这次被拒的提交内容也存**独立副本**：冲突面板要拿它做对比，而用户接下来
    // 还能继续编辑表单 —— 面板里的"我的草稿"不能跟着表单一起变。
    const rejected = budgetCloneValues(collected.values);
    const draft = budgetDraftFor(groupId) || { values: rejected };
    rememberBudgetDraft(groupId, {
      ...draft,
      values: rejected,
      // 这次写入被 409 拒了，所以不留下任何"待确认的提交快照"：
      // 表单里的内容重新变成"未提交的草稿"。
      submitted: null,
      dirty: true,
    });
    const fresh = await fetchGroupBudget(groupId);
    if (!fresh.ok) {
      setBudgetConflict(groupId, {
        mineRevision: collected.payload.revision,
        serverRevision: "（未取到）",
        serverDraft: null,
        mineValues: budgetCloneValues(rejected),
        mineLabel: "我的草稿",
        diffs: [],
        title: null,
        note: null,
      });
      setBudgetError(
        groupId,
        `配置已被其他会话更新（409），而且服务器最新配置也没读回来：`
        + `${fresh.error?.message || "未知错误"}。你的草稿已保留；`
        + `在读到服务器值之前**不会**覆盖，请稍后点「重新读取」。`,
      );
    } else {
      const serverDraft = budgetDraftFromServer(groupId, fresh.budget);
      setBudgetConflict(groupId, {
        mineRevision: collected.payload.revision,
        serverRevision: serverDraft.revision,
        serverDraft,
        mineValues: budgetCloneValues(rejected),
        mineLabel: "我的草稿",
        diffs: budgetDiffLines({ values: rejected }, serverDraft),
        title: null,
        note: null,
      });
      // 差异清单已经在对比面板里，这里不再叠一条重复文案。
      setBudgetError(groupId, "");
    }
    patchBudgetDraft(groupId, () => { }, { rerender: true });
  }

  /** 「重新读取」。**唯一**的确认入口之一：有本地编辑时**不删草稿**、有待确认的
   * 提交时按逐字段比较判定，然后把服务器值和差异摆出来让用户自己选。 */
  async function reloadGroupBudget(groupId) {
    const before = budgetDraftFor(groupId);
    if (!before || (!before.dirty && !before.submitted)) {
      // 本地既没有未保存的编辑、也没有待确认的提交：直接重取整块面板
      // （含今日用量），行为与以前一致。
      clearBudgetConflict(groupId);
      setBudgetError(groupId, "");
      await loadGroupCostPanel(groupId);
      return;
    }
    const fresh = await fetchGroupBudget(groupId);
    if (!fresh.ok) {
      // 读取失败**不算**确认：待确认的提交保持待确认，草稿原样保留，不写任何东西。
      setBudgetError(
        groupId,
        `读取预算失败：${fresh.error?.message || "未知错误"}。`
        + (before.submitted
          ? `上一次提交（revision=${before.submitted.revision}）仍未确认，`
          : "你正在编辑的草稿")
        + "**已保留**，没有被覆盖，也**没有**发出新的写入。",
      );
      renderBudgetError(groupId);
      showToast("预算读取失败，草稿与待确认状态都已保留", "error");
      return;
    }
    const serverDraft = budgetDraftFromServer(groupId, fresh.budget);
    const outcome = reconcileBudgetWithServer(groupId, serverDraft);
    patchBudgetDraft(groupId, () => { }, { rerender: true });
    if (outcome.confirmed) {
      // 确认的是**已提交的那一份**；等待期间用户又改过的话，新编辑仍是"未保存"，
      // 不能拿一句"已回读确认"把两件事糊在一起。
      showToast(
        outcome.keptEdits
          ? `上一次提交（revision=${serverDraft.revision}）已核对确认；`
            + "你之后的新修改还没有保存。"
          : outcome.revisionAdvanced
            ? budgetMatchMessage(outcome, serverDraft)
            : `之前未确认的保存已回读确认（revision=${serverDraft.revision}）。`,
        outcome.keptEdits ? "info" : "success",
      );
      return;
    }
    if (outcome.conflict) {
      showToast("服务器现值与你的草稿不同，请选择用哪一份。", "warning");
      return;
    }
    if (outcome.keptEdits) {
      showToast("已读取服务器配置；你未保存的编辑仍然保留。", "info");
      return;
    }
    showToast("已读取服务器最新配置。", "info");
  }

  function renderBudgetError(groupId) {
    const box = content.querySelector(
      `.budget-editor[data-group-id="${cssEscape(groupId)}"] [data-role="budget-error"]`,
    );
    if (!box) return;
    const message = budgetErrorFor(groupId);
    box.hidden = !message;
    box.textContent = message || "";
  }

  /** 更新草稿。``rerender`` 只在"行的增删"时用 —— 边打字边重绘会把焦点抢走，
   * 用户一个字都打不下去（kv 输入框出现过这个问题）。
   *
   * ``dirty`` 只由**用户编辑**置位，纯重绘（``() => {}``）不能置位：否则一次
   * 刷新就把自己变成"有未确认编辑"，服务器再也无法安全地更新基线。 */
  function patchBudgetDraft(groupId, mutate, { rerender = false, dirty = false } = {}) {
    const draft = budgetDraftFor(groupId);
    if (!draft) return;
    mutate(draft.values);
    if (dirty) {
      draft.dirty = true;
      // 与输入监听同一把尺子：编辑代数只在**用户编辑**时前进。
      draft.edits = Number(draft.edits || 0) + 1;
    }
    rememberBudgetDraft(groupId, draft);
    if (rerender) {
      const root = content.querySelector(
        `.budget-editor[data-group-id="${cssEscape(groupId)}"]`,
      );
      if (root) {
        // 折叠状态是**界面状态**，不是草稿数据：outerHTML 重绘后必须原样还原。
        // 否则「添加一项」一点，所在的高级区就自己合上 —— 刚加的那一行看不见，
        // 关闭的 <details> 里子元素连命中测试都不参与，真实指针根本点不到它。
        const openFlags = [...root.querySelectorAll("details.advanced-disclosure")]
          .map(node => node.open);
        root.outerHTML = renderBudgetEditor(groupId, budgetDraftFor(groupId));
        const fresh = content.querySelector(
          `.budget-editor[data-group-id="${cssEscape(groupId)}"]`,
        );
        if (fresh) {
          [...fresh.querySelectorAll("details.advanced-disclosure")]
            .forEach((node, index) => { node.open = Boolean(openFlags[index]); });
        }
      }
      renderBudgetError(groupId);
    }
  }

  async function loadGroupCostPanel(groupId) {
    if (groupId == null) return;
    const panel = costPanelRoot(groupId);
    const summary = panel?.querySelector('[data-role="cost-summary"]');
    if (summary) {
      summary.innerHTML = `<div class="loading-state"><span class="spinner"></span><p>正在统计用量</p></div>`;
    }
    const [costs, budgetState] = await Promise.allSettled([
      apiFetch(`/api/v1/groups/${encodeURIComponent(groupId)}/costs?days=7`),
      fetchGroupBudget(groupId).then(result => {
        if (!result.ok) throw result.error;
        return { budget: result.budget };
      }),
    ]);
    // 面板必须按 groupId 定位：``querySelector('[data-role=...]')`` 在多群页面
    // 上永远取**第一个**，于是点 B 群的刷新会把 B 的数字画进 A 的面板。
    if (!panel || !panel.isConnected) return;
    const freshness = panel.querySelector('[data-role="cost-freshness"]');
    const budgetBox = panel.querySelector('[data-role="cost-budget"]');
    if (budgetBox) budgetBox.dataset.groupId = String(groupId);
    if (costs.status === "rejected") {
      if (summary) {
        // 读失败 = **不知道**，绝不是"这个群没花钱"。所以这里既要说清状态未知，
        // 也不能顺手把任何金额画成 0 —— 成本汇总渲染在这一支里根本不调用。
        summary.innerHTML = `
          <p class="field-error">用量与金额状态未知：${escapeHtml(costs.reason?.message || "读取失败")}，请重试。</p>
          <p class="field-hint">在确认之前不要假定本群没有花费，也不要按 0 估算。</p>`;
      }
      renderBudgetPanel(budgetBox, budgetState, null);
      return;
    }
    if (freshness) {
      freshness.innerHTML = `<p class="field-hint">${escapeHtml(costs.value?.source_note || "")}</p>`;
    }
    renderCostSummary(panel, costs.value, budgetState);
  }

  function renderBudgetPanel(budgetBox, budgetState, costs) {
    if (!budgetBox) return;
    if (budgetState.status === "rejected") {
      // 预算读不出来 = **不知道有没有在保护**。显示成「未启用 —— 没有任何额度
      // 限制」是最危险的一种错：故障被读成了"没保护"，管理员会以为限额还在。
      budgetBox.innerHTML = `
        <h4>预算护栏</h4>
        <p class="budget-state is-unknown">状态未知：无法读取预算配置</p>
        <p class="field-hint">${
          escapeHtml(budgetState.reason?.message || "读取失败")
        }，请重试。在确认之前请勿假定额度仍然生效。</p>
        <button class="secondary-button" type="button" data-action="cost-reload"
                data-group-id="${attr(costs?.group_id ?? "")}">${icon("refresh-cw")}重试</button>`;
      return;
    }
    const budget = budgetState.value?.budget || {};
    const groupId = String(budget.group_id ?? budgetBox.dataset.groupId ?? "");
    // 只有超管能编辑；读取失败时 budgetState 已在上面 return，所以走到这里
    // 一定是**真实读回**过的配置，可以安全地拿它当草稿基线。
    const editable = Boolean(state.session?.can_manage_global);
    let draft = null;
    if (editable) {
      const serverDraft = budgetDraftFromServer(groupId, budget);
      // 切页回来、成本面板刷新（cost-reload）都会走到这里。确认判定与保存回读、
      // 「重新读取」**共用同一份**逻辑：只有提交快照/表单值与服务器现值逐字段一致
      // 才算确认。原来这里写的是"服务器 revision >= 我提交时的 revision 就算确认"，
      // 于是别的会话写了别的值（revision 变大）也会把用户未确认的草稿换成别人的
      // 配置，还报一句"之前未确认的保存已回读确认" —— 那是伪造的确认。
      const outcome = reconcileBudgetWithServer(groupId, serverDraft);
      if (outcome.confirmed && !outcome.keptEdits) {
        showToast(budgetMatchMessage(outcome, serverDraft), "success");
      }
      draft = budgetDraftFor(groupId);
    }
    const figures = `
      <dl class="budget-figures">
        <dt>今日尝试</dt><dd>${escapeHtml(String(budget.today?.used_attempts ?? 0))}</dd>
        <dt>今日 token</dt><dd>${escapeHtml(formatTokens(budget.today?.used_tokens))}</dd>
        <dt>今日金额</dt>
        <dd>${escapeHtml(formatMicro(budget.today?.used_money_micro, budget.currency))}</dd>
        <dt>结果未知</dt><dd>${escapeHtml(String(budget.today?.unknown_attempts ?? 0))}</dd>
        <dt>统计日</dt><dd>${escapeHtml(String(budget.budget_date || ""))}</dd>
      </dl>
      <p class="field-hint">未计量项（TTS / 媒体 / 搜索等）不受金额护栏覆盖，只受功能操作次数限制。</p>`;
    budgetBox.innerHTML = `
      <h4>预算护栏</h4>
      <p class="budget-state ${budget.protecting ? "is-on" : "is-off"}">
        ${budget.protecting
          ? "已启用并正在保护本群的可选功能"
          : "未启用 —— 当前没有任何额度限制"}
      </p>
      ${figures}
      ${editable ? (draft ? renderBudgetEditor(groupId, draft) : "") : ""}`;
    if (editable && draft) renderBudgetError(groupId);
  }

  function renderCostSummary(panel, payload, budgetState) {
    const summary = panel?.querySelector('[data-role="cost-summary"]');
    const freshness = panel?.querySelector('[data-role="cost-freshness"]');
    const budgetBox = panel?.querySelector('[data-role="cost-budget"]');
    const rows = Array.isArray(payload?.rows) ? payload.rows : [];
    if (freshness) {
      // 明细台账是批量落盘的：界面必须说清"最新一行可能还没落库"。
      freshness.innerHTML = `<p class="field-hint">${escapeHtml(payload?.source_note || "")}</p>`;
    }
    if (summary) {
      summary.innerHTML = rows.length ? `
        ${costAmountsView(payload)}
        <table class="cost-table">
          <thead><tr><th>日期</th><th>用途</th><th>阶段</th><th>模型</th>
            <th>尝试</th><th>失败</th><th>输入</th><th>输出</th><th>缓存</th>
            <th>金额</th></tr></thead>
          <tbody>
            ${rows.map(row => `
              <tr>
                <td>${escapeHtml(row.usage_date)}</td>
                <td>${escapeHtml(COST_PURPOSE_LABELS[row.purpose] || row.purpose)}</td>
                <td>${escapeHtml(row.stage)}</td>
                <td>${escapeHtml(row.model)}</td>
                <td>${escapeHtml(String(row.attempts))}</td>
                <td>${escapeHtml(String((row.failures || 0) + (row.timeouts || 0)))}</td>
                <td>${escapeHtml(formatTokens(row.prompt_tokens))}</td>
                <td>${escapeHtml(formatTokens(row.output_tokens))}</td>
                <td>${escapeHtml(formatTokens(row.cached_tokens))}</td>
                <td class="cost-amount-cell">${escapeHtml(costRowAmountLabel(row))}</td>
              </tr>`).join("")}
          </tbody>
        </table>
      ` : `<p class="empty-state">所选窗口内没有用量记录。</p>`;
    }
    renderBudgetPanel(budgetBox, budgetState, payload);
  }

  async function reloadGroups() {
    if (state.loading || state.saving || state.reloadingGroups) return;
    state.reloadingGroups = true;
    content.setAttribute("aria-busy", "true");
    updateChrome();
    try {
      const document = await apiFetch("/api/v1/groups");
      applyGroupsDocument(document);
    } catch (error) {
      state.groupsError = error.message || "群组列表加载失败";
      showToast(state.groupsError, "error");
    } finally {
      state.reloadingGroups = false;
      content.setAttribute("aria-busy", "false");
      if (state.activeTab === "groups") renderContent();
      updateChrome();
    }
  }

  async function loadCallAdminTargets(groupId) {
    const key = String(groupId);
    const requestToken = Symbol("telegram-admins");
    state.groupTelegramAdmins.set(key, { loading: true, requestToken });
    renderContent();
    try {
      const result = await apiFetch(`/api/v1/groups/${encodeURIComponent(groupId)}/telegram-admins`);
      if (state.groupTelegramAdmins.get(key)?.requestToken !== requestToken) return;
      state.groupTelegramAdmins.set(key, { items: result.admins || [] });
    } catch (error) {
      if (state.groupTelegramAdmins.get(key)?.requestToken !== requestToken) return;
      state.groupTelegramAdmins.set(key, { error: error.message || "管理员列表加载失败" });
    }
    renderContent();
  }

  async function loadGroupPermissions(groupId) {
    const key = String(groupId);
    const group = state.groups.find(item => String(item.id) === key);
    if (!group || state.groupSaving.has(key) || state.groupPermissionLoads.has(key)) return;
    const requestToken = Symbol("group-permissions");
    const initialValue = clone(group.settings.default_permissions);
    state.groupPermissionLoads.set(key, requestToken);
    renderContent();
    updateChrome();
    try {
      const result = await apiFetch(`/api/v1/groups/${encodeURIComponent(groupId)}/default-permissions`);
      const currentGroup = state.groups.find(item => String(item.id) === key);
      if (
        state.groupPermissionLoads.get(key) !== requestToken
        || currentGroup !== group
        || !sameValue(currentGroup.settings.default_permissions, initialValue)
      ) return;
      if (Array.isArray(result.permission_fields) && result.permission_fields.length) {
        state.permissionFields = result.permission_fields;
      }
      currentGroup.settings.default_permissions = normalizeDefaultPermissions(result.default_permissions);
      showToast(
        result.repaired
          ? "已把旧版权限配置补全为当前字段，请检查后保存"
          : result.configured
            ? "已加载保存的群权限"
            : "已读取 Telegram 当前群权限，请检查后保存",
        result.repaired ? "warning" : "success",
        result.repaired ? 6000 : 3600,
      );
    } catch (error) {
      showToast(error.message || "读取群默认权限失败", "error", 6000);
    } finally {
      if (state.groupPermissionLoads.get(key) === requestToken) {
        state.groupPermissionLoads.delete(key);
        renderContent();
        updateChrome();
      }
    }
  }

  // Per-group user-policy lists are paged server-side (limit <= 500). Walk the
  // cursor so the panel still shows the whole list without ever asking the
  // worker to build one unbounded JSON document.
  async function loadPagedGroupResource(url, responseKey) {
    const all = [];
    let offset = 0;
    while (true) {
      const result = await apiFetch(`${url}?limit=500&offset=${offset}`);
      all.push(...(result?.[responseKey] || []));
      const next = result?.next_offset;
      if (next == null || Number(next) <= offset) return all;
      offset = Number(next);
    }
  }

  async function loadGroupResources(groupId) {
    const key = String(groupId);
    const previous = state.groupResources.get(key);
    const requestToken = Symbol("group-resources");
    const mutationEpoch = state.groupResourceMutationEpochs.get(key) || 0;
    state.groupResourceLoads.set(key, requestToken);
    state.groupResources.set(key, previous?.loaded
      ? { ...previous, refreshing: true, error: "" }
      : { loading: true, loaded: false, refreshing: false, error: "" });
    renderContent();
    const base = `/api/v1/groups/${encodeURIComponent(groupId)}`;
    const definitions = [
      ["rules", "rules", `${base}/rules`],
      ["memories", "memories", `${base}/memories`],
      ["warnings", "warnings", `${base}/warnings`],
      ["bans", "bans", `${base}/bans`],
      ["exemptions", "exemptions", `${base}/moderation-exemptions`],
      ["reply_mutes", "reply_mutes", `${base}/reply-mutes`],
      ["keyword_replies", "keyword_replies", `${base}/keyword-replies`],
      ["scheduled_messages", "scheduled_messages", `${base}/scheduled-messages`],
    ];
    const PAGED_GROUP_RESOURCES = new Set([
      "warnings",
      "bans",
      "exemptions",
      "reply_mutes",
    ]);
    const results = await Promise.allSettled(definitions.map(async ([property, responseKey, url]) => {
      if (!PAGED_GROUP_RESOURCES.has(property)) return await apiFetch(url);
      const items = await loadPagedGroupResource(url, responseKey);
      return { [responseKey]: items };
    }));
    if (state.groupResourceLoads.get(key) !== requestToken) return;
    const current = state.groupResources.get(key) || {};
    const staleAfterMutation = (state.groupResourceMutationEpochs.get(key) || 0) !== mutationEpoch;
    const next = {
      ...current,
      loaded: true,
      loading: false,
      refreshing: false,
      error: "",
    };
    const errors = [];
    definitions.forEach(([property, responseKey], index) => {
      const result = results[index];
      const deferredType = Object.keys(RESOURCE_TYPE_META).find(type => RESOURCE_TYPE_META[type].property === property);
      const preserveDrafts = deferredType
        && ["automation", "rules-memory"].includes(RESOURCE_TYPE_META[deferredType].category)
        && groupResourceDraftDirty(groupId, RESOURCE_TYPE_META[deferredType].category);
      if (result.status === "fulfilled" && preserveDrafts && !staleAfterMutation) {
        // Read-only server evidence is separate from editable local drafts.
        next.serverView = { ...(next.serverView || {}), [property]: result.value?.[responseKey] || [] };
      }
      if (result.status === "fulfilled" && !preserveDrafts && !staleAfterMutation) next[property] = result.value?.[responseKey] || [];
      else {
        if (!Array.isArray(next[property])) next[property] = [];
        if (result.status === "rejected") errors.push(result.reason?.message || `${RESOURCE_TYPE_META[deferredType]?.label || property}加载失败`);
      }
    });
    if (errors.length) next.error = [...new Set(errors)].join("；");
    state.groupResources.set(key, next);
    renderContent();
  }

  async function loadGroupOperations(groupId) {
    const key = String(groupId);
    const previous = state.groupOperations.get(key) || null;
    // 同一群已有请求在飞：直接不发第二个（连点刷新、toggle 重入都走这条）。
    if (previous?.loading || previous?.refreshing) return;
    const requestToken = Symbol("group-operations");
    state.groupOperationsLoads.set(key, requestToken);
    // in-flight 语义统一：首次加载 loading=true；已加载后刷新 loading+refreshing 同时为真，
    // 这样渲染层只需要看 loading 就能把按钮置忙（父代理独立验收 R2）。
    state.groupOperations.set(key, {
      attempted: true,
      loading: true,
      loaded: Boolean(previous?.loaded),
      refreshing: Boolean(previous?.loaded),
      stale: Boolean(previous?.loaded && previous?.stale),
      error: "",
      data: previous?.data ?? null,
    });
    renderContent();
    try {
      const body = await apiFetch(`/api/v1/groups/${encodeURIComponent(key)}/operations/task-status`);
      // 迟到的响应自己走掉：换群、连点刷新都不会把 A 群的数据画到 B 群上。
      if (state.groupOperationsLoads.get(key) !== requestToken) return;
      state.groupOperations.set(key, {
        attempted: true,
        loading: false,
        loaded: true,
        refreshing: false,
        stale: false,
        error: "",
        data: body?.task_status || null,
      });
    } catch (error) {
      if (state.groupOperationsLoads.get(key) !== requestToken) return;
      if (previous?.loaded) {
        // 刷新失败：保留上一次成功的数据，但明确标成「已过期」，绝不冒充实时成功。
        state.groupOperations.set(key, { ...previous, attempted: true, loading: false, refreshing: false, stale: true, error: error?.message || "运营任务状态加载失败" });
      } else {
        // 首次失败：记住「已经试过」，重渲染/关闭重开/切页回来都不会再自动发请求。
        state.groupOperations.set(key, { attempted: true, loading: false, loaded: false, refreshing: false, stale: false, error: error?.message || "运营任务状态加载失败", data: null });
      }
    }
    renderContent();
  }

  async function loadAccess() {
    const requestToken = Symbol("access");
    state.accessLoadToken = requestToken;
    state.access = { loading: true };
    renderContent();
    try {
      const groupsResult = await apiFetch("/api/v1/authorized-groups");
      const authorizedGroups = groupsResult.authorized_groups || [];
      const loadGlobalRegistry = async (endpoint, responseKey) => {
        const all = [];
        let offset = 0;
        while (true) {
          if (state.accessLoadToken !== requestToken) return all;
          const result = await apiFetch(`/api/v1/${endpoint}?limit=500&offset=${offset}`);
          if (state.accessLoadToken !== requestToken) return all;
          all.push(...(result[responseKey] || []));
          if (result.next_offset == null || Number(result.next_offset) <= offset) break;
          offset = Number(result.next_offset);
        }
        return all;
      };
      const [adminResults, bansResult, exemptionsResult] = await Promise.all([
        Promise.all(authorizedGroups.map(async group => {
          const result = await apiFetch(`/api/v1/groups/${encodeURIComponent(group.group_id)}/admins`);
          return (result.admins || []).map(admin => ({ ...admin, group_id: group.group_id }));
        })),
        loadGlobalRegistry("global-bans", "global_bans"),
        loadGlobalRegistry("global-exemptions", "global_exemptions"),
      ]);
      if (state.accessLoadToken !== requestToken) return;
      state.access = {
        authorized_groups: authorizedGroups,
        admins: adminResults.flat(),
        global_bans: bansResult,
        global_exemptions: exemptionsResult,
      };
    } catch (error) {
      if (state.accessLoadToken !== requestToken) return;
      state.access = { error: error.message || "权限数据加载失败", authorized_groups: [], admins: [], global_bans: [], global_exemptions: [] };
    }
    if (state.accessLoadToken === requestToken) renderContent();
  }

  function stripSecrets(config) {
    const payload = clone(config);
    // 登记在册的密钥在 GET 里本来就是空串（服务端 storage_payload 已经抹过），
    // 这里再抹一是为了兜住"本地草稿里残留了旧值"。
    //
    // 但抹之前必须先看是不是**脱敏标记**：`<redacted:current>` 被抹成 "" 等于
    // 悄悄清掉了运维今天还在用的凭据，而服务端把标记原样解析回去才是无损的
    // 那条路（改了别的字段顺手保存也不能把标记抹掉）。
    const keepMarker = value => hasOpaqueMarker(value);
    if (!keepMarker(payload.verification?.turnstile_secret_key)) payload.verification.turnstile_secret_key = "";
    if (!keepMarker(payload.verification?.hcaptcha_secret_key)) payload.verification.hcaptcha_secret_key = "";
    if (!keepMarker(payload.tts?.app_key)) payload.tts.app_key = "";
    if (!keepMarker(payload.tts?.access_key)) payload.tts.access_key = "";
    // Older settings documents may still contain the retired global Sub2API block.
    if (payload.sub2api && !keepMarker(payload.sub2api.api_key)) payload.sub2api.api_key = "";
    if (!keepMarker(payload.movie_info?.tmdb_read_access_token)) payload.movie_info.tmdb_read_access_token = "";
    if (!keepMarker(payload.movie_info?.imdb_api_key)) payload.movie_info.imdb_api_key = "";
    if (!keepMarker(payload.movie_info?.imdb_aws_access_key_id)) payload.movie_info.imdb_aws_access_key_id = "";
    if (!keepMarker(payload.movie_info?.imdb_aws_secret_access_key)) payload.movie_info.imdb_aws_secret_access_key = "";
    if (!keepMarker(payload.movie_info?.imdb_aws_session_token)) payload.movie_info.imdb_aws_session_token = "";
    for (const provider of payload.models?.providers || []) {
      if (!keepMarker(provider.api_key)) provider.api_key = "";
    }
    return payload;
  }

  function validateConfig() {
    const providers = state.config.models.providers;
    if (!providers.length) return "至少需要一个模型供应商";
    const names = providers.map(provider => String(provider.name || "").trim().toLowerCase());
    if (names.some(name => !/^[a-z0-9_-]{1,64}$/.test(name))) return "供应商配置名称格式不正确";
    if (new Set(names).size !== names.length) return "供应商配置名称不能重复";
    providers.forEach((provider, index) => { provider.name = names[index]; });
    const known = new Set(names);
    if (!known.has(state.config.models.main.provider)) return "主模型必须选择有效供应商";
    if (!String(state.config.models.main.model || "").trim()) return "主模型名称不能为空";
    for (const roleName of Object.keys(ROLE_META)) {
      const role = state.config.models[roleName];
      if (role.provider && !known.has(role.provider)) return `${ROLE_META[roleName].label}引用了不存在的供应商`;
      for (const fallback of role.fallbacks || []) {
        if (!known.has(fallback.provider)) return `${ROLE_META[roleName].label}的回退供应商不存在`;
      }
    }
    const pendingTurnstileSecret = state.secretChanges["verification.turnstile_secret_key"];
    if (
      pendingTurnstileSecret?.action === "replace"
      && String(pendingTurnstileSecret.value || "").trim()
        === String(state.config.verification.turnstile_site_key || "").trim()
    ) {
      return "Turnstile Secret Key 不能与 Site Key 相同";
    }
    const pendingHcaptchaSecret = state.secretChanges["verification.hcaptcha_secret_key"];
    if (
      pendingHcaptchaSecret?.action === "replace"
      && String(pendingHcaptchaSecret.value || "").trim()
        === String(state.config.verification.hcaptcha_site_key || "").trim()
    ) {
      return "hCaptcha Secret Key 不能与 Site Key 相同";
    }
    const nullPath = findNullNumber(state.config);
    if (nullPath) {
      revealFieldByPath(nullPath);
      return `${nullPath} 需要填写有效数字`;
    }
    const invalid = [...content.querySelectorAll(
      "[data-path], [data-secret-input], [data-prompt-input]",
    )]
      .find(control => !control.checkValidity());
    if (invalid) {
      revealAdvancedDisclosure(invalid);
      invalid.reportValidity();
      return "请修正标记的字段";
    }
    const replacingSecret = Object.values(state.secretChanges).some(change => change.action === "replace");
    if (replacingSecret && !state.document.bootstrap?.master_key_configured) return "需要先在部署环境配置 CONFIG_MASTER_KEY，才能保存密钥";
    return "";
  }

  function findNullNumber(value, path = "") {
    if (value === null) return path;
    if (!value || typeof value !== "object") return "";
    for (const [key, child] of Object.entries(value)) {
      const childPath = path ? `${path}.${key}` : key;
      // null is a valid JSON value inside provider-specific request objects;
      // only configuration fields represented by numeric inputs use null as
      // the browser's "missing number" sentinel.
      if (childPath.endsWith(".request_params")) continue;
      const found = findNullNumber(child, childPath);
      if (found) return found;
    }
    return "";
  }

  function applySecretResults() {
    const next = new Set(state.configuredSecrets);
    for (const [path, change] of Object.entries(state.secretChanges)) {
      if (change.action === "clear") next.delete(path);
      if (change.action === "replace") next.add(path);
    }
    const allowedProviderSecrets = new Set(state.config.models.providers.map(provider => `providers.${provider.name}.api_key`));
    for (const path of [...next]) {
      if (path.startsWith("providers.") && !allowedProviderSecrets.has(path)) next.delete(path);
    }
    state.configuredSecrets = next;
  }

  async function validateGroupForSave(group) {
    const groupId = String(group.id);
    const templateDraft = state.groupTemplateButtonDrafts.get(groupId);
    if (templateDraft?.error) {
      state.activeTab = "groups";
      state.groupCardOpen.set(groupId, true);
      state.groupSectionOpen.set(groupSectionStateKey(groupId, "onboarding"), true);
      const existingToggle = content.querySelector(`[data-action="toggle-group-card"][data-group-id="${CSS.escape(groupId)}"]`);
      existingToggle?.setAttribute("aria-expanded", "true");
      const existingSection = existingToggle?.closest("[data-group-card]")?.querySelector('[data-group-section="onboarding"]');
      if (existingSection) existingSection.open = true;
      updateChrome();
      renderContent({ resetScroll: true });
      window.requestAnimationFrame(() => {
        const control = content.querySelector(`[data-template-buttons][data-group-id="${CSS.escape(groupId)}"]`);
        control?.focus({ preventScroll: true });
        control?.reportValidity();
        control?.scrollIntoView({ block: "center", behavior: "smooth" });
      });
      showToast(`欢迎语按钮格式无效：${templateDraft.error}`, "error", 6500);
      return false;
    }
    const invalid = [...content.querySelectorAll("[data-group-key], [data-template-buttons], [data-permission-control], [data-group-api-model-query-secret-input]")].find(
      control => String(control.dataset.groupId) === String(groupId) && !control.checkValidity(),
    );
    if (invalid) {
      revealGroupControl(invalid);
      window.requestAnimationFrame(() => {
        invalid.focus({ preventScroll: true });
        invalid.reportValidity();
        invalid.scrollIntoView({ block: "center", behavior: "auto" });
      });
      showToast("请修正群组设置中标记的字段", "error", 5000);
      return false;
    }
    const apiModelQuerySecretChange = groupApiModelQuerySecretChange(groupId);
    if (group.settings.api_model_query_enabled && apiModelQuerySecretChange?.action === "clear") {
      revealGroupSection(groupId, "model-api");
      showToast(`${group.title || group.id}：清除模型 API Key 时必须同时关闭模型 API`, "error", 6000);
      return false;
    }
    if (group.settings.api_model_query_enabled && !String(group.settings.api_model_query_base_url || "").trim()) {
      revealGroupSection(groupId, "model-api");
      showToast(`${group.title || group.id}：开启模型 API 前请填写 Base URL`, "error", 6000);
      return false;
    }
    if (group.settings.api_model_query_enabled && !groupApiModelQuerySecretConfigured(group)) {
      revealGroupSection(groupId, "model-api");
      showToast(`${group.title || group.id}：开启模型 API 前请配置 API Key`, "error", 6000);
      return false;
    }
    const permissionConfig = group.settings.default_permissions;
    if (permissionConfig) {
      const emptyDays = permissionConfig.windows.find(window => !Array.isArray(window.days) || !window.days.length);
      if (emptyDays) {
        revealGroupSection(group.id, "permissions");
        showToast(`权限时段「${emptyDays.name || emptyDays.id}」至少选择一个星期`, "error", 6000);
        return false;
      }
      const emptyOverrides = permissionConfig.windows.find(window => !Object.keys(window.overrides || {}).length);
      if (emptyOverrides) {
        revealGroupSection(group.id, "permissions");
        showToast(`权限时段「${emptyOverrides.name || emptyOverrides.id}」至少覆盖一项权限`, "error", 6000);
        return false;
      }
    }
    const baseline = state.groupBaselines.get(String(group.id)) || {};
    if (!sameValue(group.settings.mimic_target_user_id, baseline.mimic_target_user_id)) {
      const hasExistingStyle = Boolean(
        baseline.mimic_target_user_id
        || baseline.mimic_target_user_name
        || baseline.mimic_profile_text
        || baseline.mimic_sample_count
        || baseline.mimic_distilled_at_count,
      );
      if (hasExistingStyle && !await askConfirmation(
        "更换风格目标",
        `${group.title || group.id}：更换或清除目标用户 ID 会重置已有名称、画像、采样计数和样本。`,
      )) return false;
    }
    return true;
  }

  function snapshotField(snapshot, name, fallback = "") {
    return Object.hasOwn(snapshot || {}, name) ? snapshot[name].value : fallback;
  }

  function valuesFromResourceSnapshot(type, snapshot, { includeEnabled = false } = {}) {
    if (type === "keyword-replies") {
      const values = {
        keyword: String(snapshotField(snapshot, "keyword")).trim(),
        match_type: String(snapshotField(snapshot, "match_type", "contains")),
        reply_text: String(snapshotField(snapshot, "reply_text")).trim(),
        buttons: parseTemplateButtonsText(snapshotField(snapshot, "buttons_text")),
        pin_message: Boolean(snapshotField(snapshot, "pin_message", false)),
        auto_delete: Boolean(snapshotField(snapshot, "auto_delete", true)),
        disable_link_preview: Boolean(snapshotField(snapshot, "disable_link_preview", true)),
      };
      if (includeEnabled) values.enabled = Boolean(snapshotField(snapshot, "enabled", true));
      if (!values.keyword) throw new Error("关键词不能为空");
      if (!values.reply_text) throw new Error("关键词回复内容不能为空");
      return values;
    }
    if (type === "scheduled-messages") {
      const scheduleType = String(snapshotField(snapshot, "schedule_type", "daily"));
      const scheduleTime = String(snapshotField(snapshot, "schedule_time", "09:00"));
      const intervalMinutes = Number(snapshotField(snapshot, "interval_minutes", 60));
      const values = {
        text: String(snapshotField(snapshot, "text")).trim(),
        buttons: parseTemplateButtonsText(snapshotField(snapshot, "buttons_text")),
        schedule_type: scheduleType,
        schedule_time: scheduleTime,
        interval_minutes: intervalMinutes,
        pin_message: Boolean(snapshotField(snapshot, "pin_message", false)),
        unpin_previous: Boolean(snapshotField(snapshot, "unpin_previous", false)),
        auto_delete: Boolean(snapshotField(snapshot, "auto_delete", false)),
        disable_link_preview: Boolean(snapshotField(snapshot, "disable_link_preview", true)),
      };
      if (includeEnabled) values.enabled = Boolean(snapshotField(snapshot, "enabled", true));
      if (!values.text) throw new Error("定时消息内容不能为空");
      if (scheduleType === "daily" && !/^([01]\d|2[0-3]):[0-5]\d$/.test(scheduleTime)) throw new Error("定时消息发送时间格式无效");
      if (scheduleType === "interval" && (!Number.isInteger(intervalMinutes) || intervalMinutes < 5 || intervalMinutes > 10080)) throw new Error("定时消息间隔需为 5-10080 分钟");
      return values;
    }
    if (type === "rules") {
      const values = {
        rule_type: String(snapshotField(snapshot, "rule_type", "keyword")),
        pattern: String(snapshotField(snapshot, "pattern")).trim(),
        action: String(snapshotField(snapshot, "action", "warn")),
        scan_scope: ruleScanScope({ scan_scope: snapshotField(snapshot, "scan_scope", "message") }),
      };
      if (includeEnabled) values.enabled = Boolean(snapshotField(snapshot, "enabled", true));
      if (!values.pattern) throw new Error("群规内容不能为空");
      return values;
    }
    const contentValue = String(snapshotField(snapshot, "content")).trim();
    if (!contentValue) throw new Error("永久记忆不能为空");
    return { content: contentValue };
  }

  function parseResourceDraftDescriptor(key) {
    const parts = String(key).split(":");
    if (parts[0] === "entry") return { kind: "update", groupId: parts[1], type: parts[2], id: parts.slice(3).join(":") };
    if (parts[0] === "rule") return { kind: "update", groupId: parts[1], type: "rules", id: parts.slice(2).join(":") };
    if (parts[0] === "memory") return { kind: "update", groupId: parts[1], type: "memories", id: parts.slice(2).join(":") };
    if (parts[0] === "create") return { kind: "create", groupId: parts[1], type: parts.slice(2).join(":"), id: "" };
    return null;
  }

  function collectDeferredResourceOperations() {
    captureResourceFormDrafts();
    const operations = [...state.pendingResourceDeletes.values()].map(operation => ({ ...operation, kind: "delete" }));
    const stagedEditKeys = new Set();
    for (const [operationKey, operation] of state.pendingResourceCreates.entries()) {
      const editKey = resourceEditDraftKey(operation.groupId, operation.type, operation.id);
      stagedEditKeys.add(editKey);
      const snapshot = state.resourceFormDrafts.get(editKey);
      const values = snapshot
        ? valuesFromResourceSnapshot(operation.type, snapshot, { includeEnabled: true })
        : clone(operation.values);
      operations.push({ ...operation, operationKey, editKey, kind: "create", values });
    }
    for (const [key, snapshot] of state.resourceFormDrafts.entries()) {
      if (stagedEditKeys.has(key)) continue;
      const descriptor = parseResourceDraftDescriptor(key);
      if (!descriptor || !["keyword-replies", "scheduled-messages", "rules", "memories"].includes(descriptor.type)) continue;
      if (descriptor.kind === "update" && state.pendingResourceDeletes.has(resourceOperationKey(descriptor.groupId, descriptor.type, descriptor.id))) continue;
      operations.push({
        ...descriptor,
        sourceKey: key,
        // Freeze the snapshot with its values, even when another PATCH waits first.
        draftSnapshot: clone(snapshot),
        values: valuesFromResourceSnapshot(descriptor.type, snapshot, { includeEnabled: descriptor.kind === "update" }),
      });
    }
    const order = { delete: 0, update: 1, create: 2 };
    return operations.sort((left, right) => order[left.kind] - order[right.kind]);
  }

  async function persistConfigChanges() {
    const requiresRestart = restartChanges().length > 0;
    const result = await apiFetch("/api/v1/settings", {
      method: "PUT",
      body: JSON.stringify({
        revision: state.document.revision,
        config: stripSecrets(state.config),
        secret_changes: clone(state.secretChanges),
      }),
    });
    const savedDocument = result?.document || result;
    if (savedDocument?.config) {
      applySecretResults();
      applySettingsDocument({
        ...state.document,
        ...savedDocument,
        configured_secrets: savedDocument.configured_secrets || [...state.configuredSecrets],
        bootstrap: savedDocument.bootstrap || state.document.bootstrap,
        restart_required_paths: savedDocument.restart_required_paths || state.document.restart_required_paths,
        restart_pending: savedDocument.restart_pending || [],
      });
    } else {
      applySecretResults();
      state.document.revision = result?.revision ?? (Number(state.document.revision) + 1);
      state.baseline = clone(state.config);
      state.secretChanges = {};
    }
    return requiresRestart;
  }

  async function persistGroupChanges(group) {
    const baseline = state.groupBaselines.get(String(group.id)) || {};
    const apiModelQuerySecretChange = groupApiModelQuerySecretChange(group.id);
    const changedSettings = Object.fromEntries(
      Object.entries(group.settings).filter(
        ([key, value]) => GROUP_EDITABLE_KEYS.has(key) && !sameValue(value, baseline[key]),
      ),
    );
    const result = await apiFetch(`/api/v1/groups/${encodeURIComponent(group.id)}/settings`, {
      method: "PUT",
      body: JSON.stringify({
        revision: group.revision,
        settings: clone(changedSettings),
        ...(apiModelQuerySecretChange
          ? { api_model_query_secret_change: clone(apiModelQuerySecretChange) }
          : {}),
      }),
    });
    const returnedSettings = result?.settings || result?.group?.settings;
    if (returnedSettings) group.settings = normalizeGroupSettings(returnedSettings);
    else if (apiModelQuerySecretChange) {
      group.settings.api_model_query_api_key_configured = apiModelQuerySecretChange.action === "replace";
    }
    if (result?.group?.revision) group.revision = String(result.group.revision);
    state.groupTemplateButtonDrafts.delete(String(group.id));
    state.groupApiModelQuerySecretChanges.delete(String(group.id));
    state.groupBaselines.set(String(group.id), clone(group.settings));
    return result?.permission_apply;
  }

  // 「保存全部」要把「本次真的改了」和「本次没改」分开计数：永久备忘的删除
  // 可能是「后端确认这条早就没了」，那不算本次删除成功。
  const RESOURCE_SAVE_NO_CHANGE = "no_change";

  // 2xx 但结果无法确认（字段互相矛盾、文档不完整、网络/HTTP 未知失败）。抛它
  // 的路径不会清掉草稿，也不会把这一次算成保存成功。四类群内资源共用。
  class MemoryWriteUnconfirmedError extends Error {
    constructor(message = "未能确认这次操作是否已生效，已保留草稿与待删除项") {
      super(message);
      this.name = "MemoryWriteUnconfirmedError";
    }
  }

  // 群内资源删除的三态：deleted / no_change / unconfirmed。字段必须彼此相容，
  // 任一缺失或矛盾都按「未确认」处理 —— 拿到 200 不等于本次删除成功。
  // 永久备忘、群规、关键词回复、定时消息共用这一个判据（后端四个端点返回同一
  // 组 status/changed/deleted 字段），不为某一种资源另复制一份。
  function memoryDeleteOutcome(result) {
    if (!result || typeof result !== "object") return { kind: "unconfirmed" };
    const status = typeof result.status === "string" ? result.status : "";
    const deleted = result.deleted;
    const changed = result.changed;
    if (typeof deleted !== "boolean" || typeof changed !== "boolean") {
      return { kind: "unconfirmed" };
    }
    if (status === "deleted" && deleted === true && changed === true) return { kind: "deleted" };
    if (status === "no_change" && deleted === false && changed === false) return { kind: "no_change" };
    return { kind: "unconfirmed" };
  }

  // 永久备忘修改必须带回完整的五字段文档；少一个字段就当这次没被确认。
  function memoryDocumentFromResult(result) {
    const document = result?.memory;
    if (!document || typeof document !== "object") return null;
    if (document.id === undefined || document.id === null) return null;
    if (typeof document.content !== "string") return null;
    if (!("created_by" in document) || !("created_at" in document) || !("updated_at" in document)) return null;
    return document;
  }

  function applySavedResourceOperation(operation, result) {
    const meta = RESOURCE_TYPE_META[operation.type];
    const list = resourceArray(operation.groupId, operation.type);
    if (operation.kind === "delete") {
      // 四类群内资源的删除回执共用同一套校验；未确认时保留待删项与草稿。
      const outcome = memoryDeleteOutcome(result);
      if (outcome.kind === "unconfirmed") {
        throw new MemoryWriteUnconfirmedError();
      }
      const index = list.findIndex(item => String(item.id) === String(operation.id));
      if (index >= 0) list.splice(index, 1);
      state.pendingResourceDeletes.delete(resourceOperationKey(operation.groupId, operation.type, operation.id));
      const editKey = resourceEditDraftKey(operation.groupId, operation.type, operation.id);
      state.resourceFormDrafts.delete(editKey);
      state.resourceFormBaselines.delete(editKey);
      bumpGroupResourceMutationEpoch(operation.groupId);
      return outcome.kind === "no_change" ? RESOURCE_SAVE_NO_CHANGE : null;
    }
    // Three sibling PATCHes require an explicit matched/committed confirmation.
    // Validate inside this function so existing DELETE pipelines remain unchanged.
    if (operation.kind === "update" && operation.type !== "memories") {
      const doc = result?.[meta.response];
      const required = operation.type === "rules"
        ? { rule_type: "string", pattern: "string", action: "string", scan_scope: "string", enabled: "boolean" }
        : operation.type === "keyword-replies"
          ? { keyword: "string", match_type: "string", reply_text: "string", enabled: "boolean", pin_message: "boolean", auto_delete: "boolean", disable_link_preview: "boolean" }
          : { text: "string", schedule_type: "string", schedule_time: "string", interval_minutes: "number", last_run_at: "string", enabled: "boolean", pin_message: "boolean", unpin_previous: "boolean", auto_delete: "boolean", disable_link_preview: "boolean" };
      if (result?.ok !== true || result?.status !== "updated" || result?.confirmed !== true
        || !doc || !Number.isSafeInteger(doc.id) || doc.id <= 0 || String(doc.id) !== String(operation.id)
        || Object.entries(required).some(([key, type]) => typeof doc[key] !== type)
        || (operation.type !== "rules" && !Array.isArray(doc.buttons))
        || (operation.type === "scheduled-messages" && (!Number.isInteger(doc.interval_minutes) || doc.interval_minutes < 5))) {
        throw new MemoryWriteUnconfirmedError("未能确认修改是否已生效，已保留草稿；请点击“重新查看”再决定是否重提");
      }
    }
    const document = operation.type === "memories"
      ? memoryDocumentFromResult(result)
      : result?.[meta.response];
    if (!document) {
      if (operation.type === "memories") throw new MemoryWriteUnconfirmedError();
      throw new Error(`${meta.label}保存响应缺少数据`);
    }
    if (operation.kind === "update") {
      const item = list.find(row => String(row.id) === String(operation.id));
      if (item) Object.assign(item, document);
      if (operation.type !== "memories" && operation.draftSnapshot) {
        captureResourceFormDrafts();
        const current = state.resourceFormDrafts.get(operation.sourceKey);
        // Even an edit back to the OLD value during the ACK wait is a new draft.
        // Compare to the exact dispatched snapshot, not the previous baseline.
        if (current && !sameValue(current, operation.draftSnapshot)) {
          state.resourceFormDrafts.set(operation.sourceKey, current);
          state.resourceFormBaselines.set(operation.sourceKey, operation.draftSnapshot);
        } else {
          state.resourceFormDrafts.delete(operation.sourceKey);
          state.resourceFormBaselines.delete(operation.sourceKey);
        }
      } else {
        state.resourceFormDrafts.delete(operation.sourceKey);
        state.resourceFormBaselines.delete(operation.sourceKey);
      }
      bumpGroupResourceMutationEpoch(operation.groupId);
      return;
    }
    if (operation.operationKey) {
      const temporaryIndex = list.findIndex(item => String(item.id) === String(operation.id));
      if (temporaryIndex >= 0) list.splice(temporaryIndex, 1);
      upsertResourceRow(operation.groupId, operation.type, document);
      state.pendingResourceCreates.delete(operation.operationKey);
      state.resourceFormDrafts.delete(operation.editKey);
      state.resourceFormBaselines.delete(operation.editKey);
    } else {
      upsertResourceRow(operation.groupId, operation.type, document);
      state.resourceFormDrafts.delete(operation.sourceKey);
      state.resourceFormBaselines.delete(operation.sourceKey);
    }
    bumpGroupResourceMutationEpoch(operation.groupId);
  }

  async function persistResourceOperation(operation) {
    const base = `/api/v1/groups/${encodeURIComponent(operation.groupId)}/${operation.type}`;
    if (operation.kind === "delete") {
      const result = await apiFetch(`${base}/${encodeURIComponent(operation.id)}`, { method: "DELETE" });
      return applySavedResourceOperation(operation, result);
    }
    const url = operation.kind === "update" ? `${base}/${encodeURIComponent(operation.id)}` : base;
    const trackedPatch = operation.kind === "update" && operation.type !== "memories";
    if (trackedPatch) {
      state.resourcePatchInFlight ||= new Set();
      operation.draftSnapshot ||= clone(state.resourceFormDrafts.get(operation.sourceKey));
      state.resourcePatchInFlight.add(operation.sourceKey);
    }
    try {
      const result = await apiFetch(url, {
        method: operation.kind === "update" ? "PATCH" : "POST",
        body: JSON.stringify(clone(operation.values)),
      });
      return applySavedResourceOperation(operation, result);
    } finally {
      if (trackedPatch) state.resourcePatchInFlight.delete(operation.sourceKey);
    }
  }

  async function saveAllChanges(options = {}) {
    // 顶栏保存按钮把 MouseEvent 传进来，所以只能按显式标记读，不能解构。
    const skipPreview = options?.skipPreview === true;
    if (
      state.loading
      || state.saving
      || state.reloadingGroups
      || state.accessMutating
      || state.immediateMutations > 0
      || state.groupPermissionLoads.size > 0
      || !hasUnsavedChanges()
    ) return;
    captureResourceFormDrafts();
    const invalidResourceForm = [...content.querySelectorAll(RESOURCE_DRAFT_FORM_SELECTOR)].find(form => (
      state.resourceFormDrafts.has(resourceDraftFormKey(form)) && !form.checkValidity()
    ));
    if (invalidResourceForm) {
      const invalidControl = [...invalidResourceForm.elements].find(control => !control.checkValidity());
      if (invalidControl) revealGroupControl(invalidControl);
      window.requestAnimationFrame(() => {
        invalidControl?.focus({ preventScroll: true });
        invalidResourceForm.reportValidity();
        invalidControl?.scrollIntoView({ block: "center", behavior: "smooth" });
      });
      showToast("请先补全标记的群管理草稿", "error", 5000);
      return;
    }
    let resourceOperations;
    try {
      resourceOperations = collectDeferredResourceOperations();
    } catch (error) {
      showToast(error.message || "请修正群管理草稿", "error", 6000);
      return;
    }
    const configNeedsSave = state.session?.can_manage_global && configDirty();
    if (configNeedsSave) {
      const validationError = validateConfig();
      if (validationError) {
        showToast(validationError, "error", 5000);
        return;
      }
    }
    const groupsToSave = state.groups.filter(groupDirty);
    for (const group of groupsToSave) {
      if (!await validateGroupForSave(group)) return;
    }

    const failures = [];
    const warnings = [];

    // 保存前差异预览是**默认路径**：先算差异、把面板摆出来，管理员看过之后再点
    // 面板里的「确认保存这些改动」才发 PUT。所以这一轮先把全局配置按住不动
    // （群设置与群管理草稿照常保存），把决定权交回给人。
    // 预览被显式跳过时才走直连保存。
    let previewed = false;
    if (configNeedsSave && !state.skipSavePreview && !skipPreview) {
      if (await runSavePreview()) {
        previewed = true;
        warnings.push("已生成保存前差异预览：确认无误后点预览面板里的「确认保存这些改动」");
      } else {
        // 预览都没算出来就不写：宁可让人再看一眼，也不盲写一份没见过的差异。
        showToast("差异预览没有成功，已取消全局配置保存", "error", 7000);
        return;
      }
    } else if (configNeedsSave) {
      state.savePreview = null;
    }
    const configToSave = configNeedsSave && !previewed;

    let successCount = 0;
    let noChangeCount = 0;
    const noChangeTypes = new Set();
    let restartSaved = false;
    state.saving = true;
    groupsToSave.forEach(group => state.groupSaving.add(String(group.id)));
    state.resourcePatchEditing = !configNeedsSave && groupsToSave.length === 0
      && resourceOperations.length > 0
      && resourceOperations.every(op => op.kind === "update" && op.type !== "memories");
    app.classList.add("is-saving");
    content.setAttribute("aria-busy", "true");
    updateChrome();
    try {
      if (configToSave) {
        try {
          restartSaved = await persistConfigChanges();
          successCount += 1;
          state.savePreview = null;
          // 历史列表的 revision 基线变了，交给下一次进入历史页重新拉取。
          state.configHistory.entries = [];
          state.configHistory.compare = null;
        } catch (error) {
          const conflict = error instanceof ApiError && error.status === 409;
          if (conflict) {
            state.savePreview = {
              ...(state.savePreview && state.savePreview.phase === "ready" ? state.savePreview : {}),
              phase: "conflict",
            };
          }
          failures.push(conflict
            ? "全局配置已被其他会话更新；草稿保留，可在上方预览面板重新加载后重试"
            : error.message || "全局配置保存失败");
        }
      }
      for (const group of groupsToSave) {
        try {
          const permissionApply = await persistGroupChanges(group);
          successCount += 1;
          if (permissionApply && !permissionApply.applied && !permissionApply.removed) {
            warnings.push(`${group.title || group.id} 已保存，但 Telegram 权限下发失败，后台会自动重试${permissionApply.last_error ? `：${permissionApply.last_error}` : ""}`);
          }
        } catch (error) {
          failures.push(error instanceof ApiError && error.status === 409
            ? `${group.title || group.id} 的群设置已发生变化`
            : `${group.title || group.id}：${error.message || "群设置保存失败"}`);
        }
      }
      for (const operation of resourceOperations) {
        try {
          const outcome = await persistResourceOperation(operation);
          if (outcome === RESOURCE_SAVE_NO_CHANGE) {
            // 后端确认「本次没删掉，这条已经不在库里了」：本地条目按不可用移除，
            // 但不算本次删除成功，也不说列表已经刷新过。
            noChangeCount += 1;
            noChangeTypes.add(operation.type);
            const noChangeLabel = RESOURCE_TYPE_META[operation.type]?.label || operation.type;
            warnings.push(operation.type === "memories"
              ? "永久记忆：此条永久记忆未由本次操作删除，可能已在别处删除。"
              : `${noChangeLabel}：此条未由本次操作删除，可能已在别处删除。`);
          } else {
            successCount += 1;
          }
        } catch (error) {
          failures.push(`${RESOURCE_TYPE_META[operation.type]?.label || operation.type}：${error.message || "保存失败"}${operation.kind === "update" && operation.type !== "memories" ? "；可能已生效，请重新查看后再决定是否重提" : ""}`);
        }
      }
    } finally {
      state.saving = false;
      state.resourcePatchEditing = false;
      state.groupSaving.clear();
      app.classList.remove("is-saving");
      content.setAttribute("aria-busy", "false");
      render();
    }

    if (warnings.length) showToast(warnings.join("；"), "warning", 8000);
    if (failures.length) {
      const prefix = successCount ? `已保存 ${successCount} 项，` : "";
      showToast(`${prefix}${failures.length} 项失败并已保留草稿：${failures.join("；")}`, "error", 9000);
    } else if (successCount) {
      const pendingRestart = state.document?.restart_pending || [];
      const restartTail = pendingRestart.length
        ? `需要重启才生效：${pendingRestart.join("、")}`
        : "重启项将在 Bot 重启后生效";
      showToast(restartSaved
        ? `已保存全部 ${successCount} 项更改；${restartTail}`
        : `已保存全部 ${successCount} 项更改`, restartSaved ? "warning" : "success", 8000);
    } else if (noChangeCount) {
      const onlyMemories = noChangeTypes.size === 1 && noChangeTypes.has("memories");
      if (onlyMemories) {
        showToast(`${noChangeCount} 项永久记忆已确认无需改动`, "warning", 8000);
      } else {
        showToast(`${noChangeCount} 项记录已确认无需改动`, "warning", 8000);
      }
    }
  }

  function providerReferences(name) {
    const references = [];
    for (const [roleName, meta] of Object.entries(ROLE_META)) {
      const role = state.config.models[roleName];
      if (role.provider === name) references.push(meta.label);
      if ((role.fallbacks || []).some(item => item.provider === name)) references.push(`${meta.label}回退链`);
    }
    return [...new Set(references)];
  }

  function renameProvider(index, oldName, rawName) {
    const provider = state.config.models.providers[index];
    if (!provider) return;
    const newName = String(rawName || "").trim().toLowerCase();
    if (!/^[a-z0-9_-]{1,64}$/.test(newName)) {
      provider.name = oldName;
      showToast("供应商名称只能包含字母、数字、下划线和连字符", "error");
      renderContent();
      return;
    }
    if (state.config.models.providers.some((item, itemIndex) => itemIndex !== index && item.name === newName)) {
      provider.name = oldName;
      showToast("供应商名称不能重复", "error");
      renderContent();
      return;
    }
    provider.name = newName;
    if (oldName && oldName !== newName) {
      for (const roleName of Object.keys(ROLE_META)) {
        const role = state.config.models[roleName];
        if (role.provider === oldName) role.provider = newName;
        for (const fallback of role.fallbacks || []) {
          if (fallback.provider === oldName) fallback.provider = newName;
        }
      }
      const oldSecretPath = `providers.${oldName}.api_key`;
      const newSecretPath = `providers.${newName}.api_key`;
      const pending = state.secretChanges[oldSecretPath];
      delete state.secretChanges[oldSecretPath];
      if (pending?.action === "replace") state.secretChanges[newSecretPath] = pending;
      if (state.configuredSecrets.has(oldSecretPath)) {
        showToast("供应商已重命名；请重新填写其 API Key", "warning", 5500);
      }
    }
    renderContent();
    updateChrome();
  }

  function readControlValue(target) {
    const kind = target.dataset.kind || "string";
    if (kind === "boolean") return target.checked;
    if (kind === "number") return target.value === "" ? null : Number(target.value);
    if (kind === "nonnegative-int") return target.value === "" ? 0 : Number(target.value);
    if (kind === "array") return target.value.split(",").map(value => value.trim()).filter(Boolean);
    if (kind === "nullable-boolean") {
      if (target.value === "") return null;
      return target.value === "true";
    }
    if (kind === "nullable-string") return target.value === "" ? null : target.value;
    if (kind === "nullable-int") return target.value === "" ? null : Number(target.value);
    if (kind === "lower-string") return target.value.trim().toLowerCase();
    if (kind === "json-object") return parseJsonObjectValue(target.value);
    return target.value;
  }

  function updateRewardControl(target) {
    const rows = readRewardEditor(target);
    setPath(state.config, target.dataset.rewardPath, rows);
    const editor = target.closest("[data-reward-editor]");
    const head = editor?.querySelector(".reward-editor-head");
    if (head) {
      const total = rows.reduce((sum, row) => sum + (Number(row?.weight) || 0), 0);
      const expectation = total
        ? rows.reduce((sum, row) => sum + (Number(row?.payout) || 0) * (Number(row?.weight) || 0), 0) / total
        : 0;
      const badges = head.querySelectorAll(".badge.info");
      if (badges[0]) badges[0].textContent = `总权重 ${total}`;
      if (badges[1]) badges[1].textContent = `期望 ${expectation.toFixed(2)} 分/次`;
    }
    updateChrome();
  }

  function updatePathControl(target) {
    try {
      setPath(state.config, target.dataset.path, readControlValue(target));
      target.setCustomValidity("");
    } catch (error) {
      target.setCustomValidity(error.message || "JSON 参数格式无效");
    }
    updateChrome();
  }

  function updateRenderedGroupDirty(groupId) {
    const group = state.groups.find(item => String(item.id) === String(groupId));
    const card = content.querySelector(`[data-group-card] [data-group-id="${CSS.escape(String(groupId))}"]`)?.closest("[data-group-card]");
    if (!group || !card) return;
    const dirty = groupDirty(group) || groupResourceDraftDirty(group.id);
    const headerActions = card.querySelector(".item-actions");
    let marker = headerActions?.querySelector(".group-save-state");
    if (dirty && !marker && headerActions) {
      marker = document.createElement("span");
      marker.className = "group-save-state";
      marker.textContent = "未保存";
      marker.setAttribute("role", "status");
      headerActions.prepend(marker);
    } else if (!dirty && marker) {
      marker.remove();
    }
  }

  function updateGroupCardState(target, group) {
    const card = target.closest("[data-group-card]");
    if (!card) return;
    updateRenderedGroupDirty(group.id);
    const section = target.closest("[data-group-settings-section]");
    if (section) {
      const settingKeys = String(section.dataset.groupSettingKeys || "").split(",").filter(Boolean);
      const sectionDirty = groupSectionDirty(group, settingKeys);
      updateRenderedGroupSectionDirty(section, sectionDirty);
    }
    updateChrome();
  }

  function handlePermissionControl(target) {
    if (!target.matches("[data-permission-control]")) return false;
    const group = state.groups.find(item => String(item.id) === String(target.dataset.groupId));
    const config = group?.settings?.default_permissions;
    if (!group || !config || state.groupSaving.has(String(group.id))) return true;
    if (target.dataset.permissionConfig) {
      config[target.dataset.permissionConfig] = target.type === "checkbox" ? target.checked : target.value;
    } else if (target.dataset.permissionBase) {
      config.base[target.dataset.permissionBase] = target.checked;
    } else {
      const index = Number(target.dataset.windowIndex);
      const window = config.windows[index];
      if (!window) return true;
      if (target.dataset.permissionWindowField) {
        const key = target.dataset.permissionWindowField;
        window[key] = target.dataset.windowKind === "boolean"
          ? target.checked
          : target.dataset.windowKind === "number"
            ? Number(target.value)
            : target.value;
      } else if (target.dataset.permissionWindowDay != null) {
        const day = Number(target.dataset.permissionWindowDay);
        const days = new Set(window.days || []);
        if (target.checked) days.add(day);
        else days.delete(day);
        window.days = [...days].sort((a, b) => a - b);
      } else if (target.dataset.permissionWindowOverride) {
        const key = target.dataset.permissionWindowOverride;
        if (target.value === "") delete window.overrides[key];
        else window.overrides[key] = target.value === "true";
      }
    }
    updateGroupCardState(target, group);
    return true;
  }

  // This control also has data-group-key. Keep input and blur/change on the
  // typed path so the generic group handler never replaces the array with text.
  function handleGroupTemplateButtonsControl(target) {
    if (!target.matches("[data-template-buttons]")) return false;
    const group = state.groups.find(item => String(item.id) === String(target.dataset.groupId));
    if (!group || state.groupSaving.has(String(group.id))) return true;
    try {
      const buttons = parseTemplateButtonsText(target.value);
      group.settings[target.dataset.groupKey] = buttons;
      state.groupTemplateButtonDrafts.set(String(group.id), {
        raw: target.value,
        error: "",
        buttons: clone(buttons),
      });
      target.setCustomValidity("");
    } catch (error) {
      const message = error.message || "内联按钮格式无效";
      state.groupTemplateButtonDrafts.set(String(group.id), {
        raw: target.value,
        error: message,
        buttons: clone(group.settings[target.dataset.groupKey]),
      });
      target.setCustomValidity(message);
    }
    updateGroupCardState(target, group);
    return true;
  }

  content.addEventListener("input", event => {
    const target = event.target;
    // 审批理由必须**边打边存**：只在失败时记的话，刷新/切页就把用户正在写的
    // 理由丢了。键是 request_id，重绘时从对应草稿恢复。
    if (target?.dataset?.role === "decision-reason") {
      rememberQueueDraft(String(target.dataset.requestId || ""), target.value);
      return;
    }
    if (target?.dataset?.priceField) {
      // 价格目录草稿：边打字边存，但**不重绘** —— 重绘会把光标和刚打的字抢走。
      // 编辑代数 +1 用于判断"这次 PUT 发出之后用户又改过没有"。
      const price = priceState();
      const index = Number(target.dataset.priceRow);
      if (price.rows[index]) {
        price.rows[index][target.dataset.priceField] = String(target.value);
        price.dirty = true;
        price.edits = Number(price.edits || 0) + 1;
      }
      return;
    }
    if (target?.dataset?.budgetField) {
      const editor = target.closest(".budget-editor");
      const groupId = editor?.dataset.groupId || "";
      const draft = budgetDraftFor(groupId);
      if (draft) {
        const field = target.dataset.budgetField;
        draft.values[field === "enabled" ? "enabled" : field] =
          field === "enabled" ? Boolean(target.checked) : String(target.value);
        draft.dirty = true;
        // 编辑代数：用来判断"这次 PUT 发出之后用户又改过没有"。ACK 回来时不能
        // 无条件把 dirty 清成 false —— 那样会把等待期间的新编辑标成"已提交"。
        draft.edits = Number(draft.edits || 0) + 1;
        rememberBudgetDraft(groupId, draft);
      }
      return;
    }
    if (target?.dataset?.kvKey || target?.dataset?.kvValue) {
      const editor = target.closest(".budget-editor");
      const groupId = editor?.dataset.groupId || "";
      const kind = String(target.dataset.kvKey || target.dataset.kvValue || "");
      const index = Number(target.dataset.index);
      patchBudgetDraft(groupId, values => {
        const rows = kind === "covers" ? values.covers : values.operations;
        if (!rows[index]) rows[index] = { key: "", value: "" };
        if (target.dataset.kvKey) rows[index].key = String(target.value);
        else rows[index].value = String(target.value);
      }, { dirty: true });
      return;
    }
    const resourceForm = target.closest(RESOURCE_DRAFT_FORM_SELECTOR);
    if (resourceForm) {
      syncScheduledFormFields(resourceForm);
      captureResourceFormDraft(resourceForm);
    }
    if (handlePermissionControl(target)) return;
    if (handleGroupTemplateButtonsControl(target)) return;
    // 回放候选的文本框**不重渲染**：重渲染会抢走焦点，一个字都打不下去。
    // 只把内存里的草稿同步好，再就地刷新 JSON 预览和运行按钮的可用状态。
    if (target.matches("[data-replay-rule-field='pattern']")) {
      const rule = state.replay.rules[Number(target.dataset.ruleIndex)];
      if (rule) rule.pattern = target.value;
      refreshReplayCandidateSurface();
      return;
    }
    if (target.matches("[data-replay-prompt]")) {
      state.replay.prompt = target.value;
      refreshReplayCandidateSurface();
      return;
    }
    if (target.matches("[data-reward-key]")) {
      updateRewardControl(target);
      return;
    }
    if (target.matches("[data-auto-delete-seconds]")) {
      const category = target.dataset.autoDeleteSeconds;
      const overrides = { ...(state.config.bot.auto_delete_category_seconds || {}) };
      const seconds = target.value === "" ? 0 : Math.max(0, Math.floor(Number(target.value) || 0));
      if (seconds > 0) overrides[category] = seconds;
      else delete overrides[category];
      state.config.bot.auto_delete_category_seconds = overrides;
      updateChrome();
      return;
    }
    if (target.matches("[data-path]")) {
      updatePathControl(target);
      return;
    }
    if (target.matches("[data-secret-input]")) {
      const path = target.dataset.secretInput;
      const value = target.value;
      if (value) state.secretChanges[path] = { action: "replace", value };
      else delete state.secretChanges[path];
      const badge = target.closest(".secret-control")?.querySelector(".badge");
      if (badge) {
        badge.className = `badge ${value ? "warning" : state.configuredSecrets.has(path) ? "success" : ""}`.trim();
        badge.textContent = value ? "等待替换" : state.configuredSecrets.has(path) ? "已配置" : "未配置";
      }
      updateChrome();
      return;
    }
    if (target.matches("[data-group-api-model-query-secret-input]")) {
      const group = state.groups.find(item => String(item.id) === String(target.dataset.groupId));
      if (!group || state.groupSaving.has(String(group.id))) return;
      const value = target.value.trim();
      if (value) state.groupApiModelQuerySecretChanges.set(String(group.id), { action: "replace", value });
      else state.groupApiModelQuerySecretChanges.delete(String(group.id));
      const badge = target.closest(".secret-control")?.querySelector(".badge");
      if (badge) {
        badge.className = `badge ${value ? "warning" : group.settings.api_model_query_api_key_configured ? "success" : ""}`.trim();
        badge.textContent = value ? "等待替换" : group.settings.api_model_query_api_key_configured ? "已配置" : "未配置";
      }
      updateGroupCardState(target, group);
      return;
    }
    if (target.matches("[data-prompt-input]")) {
      const key = target.dataset.promptInput;
      state.config.prompts[key] = target.value;
      const meta = document.getElementById("prompt-meta");
      if (meta) meta.textContent = `${target.value.length.toLocaleString("zh-CN")} 字符`;
      const button = content.querySelector(`[data-action="select-prompt"][data-prompt-key="${CSS.escape(key)}"]`);
      if (button) {
        const existingDot = button.querySelector(".prompt-dot");
        if (promptDirty(key) && !existingDot) button.insertAdjacentHTML("beforeend", `<span class="prompt-dot" aria-label="已修改"></span>`);
        if (!promptDirty(key) && existingDot) existingDot.remove();
      }
      updateChrome();
      return;
    }
    if (target.matches("[data-group-key]")) {
      const group = state.groups.find(item => String(item.id) === String(target.dataset.groupId));
      if (!group || state.groupSaving.has(String(group.id))) return;
      group.settings[target.dataset.groupKey] = readControlValue(target);
      updateGroupCardState(target, group);
      return;
    }
    if (target.id === "group-search") {
      state.groupSearch = target.value;
      applyGroupSearchFilter();
      return;
    }
    if (target.matches("[data-access-search]")) {
      const type = target.dataset.accessSearch;
      state.accessSearches[type] = target.value;
      state.listPages.set(`access:${type}`, 1);
      refreshGlobalAccessList(type);
    }
  });

  content.addEventListener("change", event => {
    const target = event.target;
    const resourceForm = target.closest(RESOURCE_DRAFT_FORM_SELECTOR);
    if (resourceForm) {
      syncScheduledFormFields(resourceForm);
      captureResourceFormDraft(resourceForm);
    }
    if (handlePermissionControl(target)) return;
    if (handleGroupTemplateButtonsControl(target)) return;
    // ---- 审核回放（dry-run）----
    if (target.matches("[data-replay-group]")) {
      // 换群 = 换作用域：样本 / 详情 / 跟进中的 run / idempotency_key 全部重来。
      // 先记住新群，再作废旧群的一切，否则 in-flight 的旧响应还能画到新卡片上。
      const picked = replayGroups().find(item => String(item.id) === String(target.value)) || null;
      resetReplayScope(picked);
      loadReplayWorkspace({ force: true, group: picked });
      return;
    }
    if (target.matches("[data-replay-show-expired]")) {
      state.replay.showExpired = target.checked;
      loadReplayWorkspace({ force: true, group: replayScopeOf(target) });
      return;
    }
    if (target.matches("[data-replay-use-baseline-prompt]")) {
      state.replay.useBaselinePrompt = target.checked;
      if (target.checked) state.replay.prompt = "";
      renderContent();
      return;
    }
    if (target.matches("[data-replay-rule-field]")) {
      const index = Number(target.dataset.ruleIndex);
      const rule = state.replay.rules[index];
      if (!rule) return;
      const name = target.dataset.replayRuleField;
      rule[name] = target.type === "checkbox" ? target.checked : target.value;
      renderContent();
      return;
    }
    if (target.matches("[data-history-limit]")) {
      state.configHistory.limit = Number(target.value) || 20;
      loadConfigHistory({ before: null });
      return;
    }
    if (target.matches("[data-history-against]")) {
      state.configHistory.against = Number(target.value);
      return;
    }
    if (target.matches("[data-history-compare]")) {
      state.configHistory.compareRevision = Number(target.value);
      return;
    }
    if (target.matches("[data-preview-skip]")) {
      state.skipSavePreview = target.checked;
      renderContent();
      return;
    }
    if (target.matches("[data-reward-key]")) {
      updateRewardControl(target);
      return;
    }
    if (target.matches("[data-call-admin-target]")) {
      const group = state.groups.find(item => String(item.id) === String(target.dataset.groupId));
      if (!group || state.groupSaving.has(String(group.id))) return;
      const boxes = [...content.querySelectorAll(`[data-call-admin-target][data-group-id="${CSS.escape(String(group.id))}"]`)];
      const checked = boxes.filter(box => box.checked).map(box => Number(box.dataset.callAdminTarget));
      // All checked = "mention everyone" (stored as empty so future admins
      // are included automatically).
      group.settings.call_admin_targets = checked.length === boxes.length
        ? []
        : checked.sort((a, b) => a - b);
      updateGroupCardState(target, group);
      return;
    }
    if (target.matches("[data-access-admin-filter]")) {
      state.accessAdminGroup = target.value;
      state.listPages.set("access:admins", 1);
      renderContent();
      return;
    }
    if (target.matches("[data-auto-delete-category]")) {
      const selected = [...content.querySelectorAll("[data-auto-delete-category]:checked")]
        .map(control => control.dataset.autoDeleteCategory);
      state.config.bot.auto_delete_categories = selected;
      const category = target.dataset.autoDeleteCategory;
      const modeSelect = content.querySelector(
        `[data-auto-delete-mode="${CSS.escape(category)}"]`,
      );
      if (modeSelect) modeSelect.disabled = !target.checked;
      const secondsInput = content.querySelector(
        `[data-auto-delete-seconds="${CSS.escape(category)}"]`,
      );
      if (secondsInput) {
        secondsInput.disabled = !target.checked || (modeSelect && modeSelect.value === "button");
      }
      updateChrome();
      return;
    }
    if (target.matches("[data-auto-delete-mode]")) {
      const category = target.dataset.autoDeleteMode;
      const modes = { ...(state.config.bot.auto_delete_category_mode || {}) };
      if (target.value === "button") modes[category] = "button";
      else delete modes[category];
      state.config.bot.auto_delete_category_mode = modes;
      const secondsInput = content.querySelector(
        `[data-auto-delete-seconds="${CSS.escape(category)}"]`,
      );
      if (secondsInput) secondsInput.disabled = target.value === "button" || target.disabled;
      updateChrome();
      return;
    }
    if (target.matches("[data-auto-delete-seconds]")) {
      const category = target.dataset.autoDeleteSeconds;
      const overrides = { ...(state.config.bot.auto_delete_category_seconds || {}) };
      const seconds = target.value === "" ? 0 : Math.max(0, Math.floor(Number(target.value) || 0));
      if (seconds > 0) overrides[category] = seconds;
      else delete overrides[category];
      state.config.bot.auto_delete_category_seconds = overrides;
      updateChrome();
      return;
    }
    if (target.matches("[data-path]")) {
      updatePathControl(target);
    }
    if (target.matches("[data-group-key]")) {
      const group = state.groups.find(item => String(item.id) === String(target.dataset.groupId));
      if (!group || state.groupSaving.has(String(group.id))) return;
      group.settings[target.dataset.groupKey] = readControlValue(target);
      updateGroupCardState(target, group);
    }
  });

  content.addEventListener("focusout", event => {
    const target = event.target;
    if (target.matches("[data-provider-index]")) {
      renameProvider(Number(target.dataset.providerIndex), target.dataset.providerOldName, target.value);
    }
  });

  // <details> 的 toggle 事件不保证冒泡，用捕获阶段统一记录展开状态，
  // 这样重渲染（切 tab、保存、刷新资源）之后高级项仍然保持原来的开合。
  content.addEventListener("toggle", event => {
    const panel = event.target;
    if (!panel?.matches?.("[data-advanced-panel]")) return;
    state.advancedOpen.set(panel.dataset.advancedPanel, panel.open);
  }, true);

  // 「运营任务」分节默认折叠；第一次展开才真正去读一次（只读、不轮询、不预取）。
  content.addEventListener("toggle", event => {
    const section = event.target;
    if (section?.dataset?.groupSection !== OPERATIONS_SECTION_KEY) return;
    if (!section.open) return;
    const groupId = section.dataset.groupId;
    if (!groupId) return;
    const entry = state.groupOperations.get(String(groupId));
    // 只有「从未尝试过」的群才在首次展开时自动读一次；失败过（attempted）、刷新中、已加载都不再隐式重试。
    if (entry?.attempted || entry?.loaded || entry?.loading || entry?.refreshing) return;
    loadGroupOperations(groupId);
  }, true);

  content.addEventListener("click", async event => {
    const rewardAdd = event.target.closest("[data-reward-add]");
    if (rewardAdd) {
      const target = rewardAdd.dataset.rewardAdd;
      const rows = Array.isArray(getPath(state.config, target)) ? [...getPath(state.config, target)] : [];
      rows.push({ payout: 0, weight: 1, label: "" });
      setPath(state.config, target, rows);
      renderContent();
      updateChrome();
      return;
    }
    const rewardRemove = event.target.closest("[data-reward-remove]");
    if (rewardRemove) {
      const target = rewardRemove.dataset.rewardRemove;
      const index = Number(rewardRemove.dataset.rewardIndex);
      const rows = Array.isArray(getPath(state.config, target)) ? [...getPath(state.config, target)] : [];
      if (rows.length > 1 && rows[index]) {
        rows.splice(index, 1);
        setPath(state.config, target, rows);
        renderContent();
        updateChrome();
      } else {
        showToast("奖池至少保留一档", "error", 3000);
      }
      return;
    }
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const action = button.dataset.action;
    if (action === "retry-load") {
      loadAll();
      return;
    }
    // ---- 审核回放（dry-run）----
    // 作用域一律从**触发它的那个面板**取（closest("[data-replay-scope]")），
    // 不读任何全局的"当前群"：逐群面板与跨群面板因此永远各说各的。
    if (action === "replay-load") {
      loadReplayWorkspace({ force: true, group: replayScopeOf(button) });
      return;
    }
    if (action === "replay-run") {
      startReplayRun(button);
      return;
    }
    if (action === "replay-cancel-run") {
      cancelReplayRun(button);
      return;
    }
    if (action === "replay-execute-run") {
      executeQueuedReplayRun(button);
      return;
    }
    if (action === "replay-stop-tracking") {
      forgetReplayRun(button);
      return;
    }
    if (action === "replay-label") {
      setReplaySampleLabel(button);
      return;
    }
    if (action === "replay-refresh") {
      loadReplayWorkspace({ force: true, group: replayScopeOf(button) });
      return;
    }
    if (action === "replay-import") {
      importReplaySamples(button);
      return;
    }
    if (action === "replay-delete-sample") {
      deleteReplaySample(button);
      return;
    }
    if (action === "replay-add-rule") {
      if (state.replay.rules.length >= REPLAY_MAX_RULES) {
        showToast(`候选规则最多 ${REPLAY_MAX_RULES} 条`, "warning", 4000);
        return;
      }
      state.replay.rules.push({ rule_type: "keyword", pattern: "", action: "warn", enabled: true, scan_scope: "message" });
      renderContent();
      return;
    }
    if (action === "replay-remove-rule") {
      state.replay.rules.splice(Number(button.dataset.ruleIndex), 1);
      renderContent();
      return;
    }
    if (action === "replay-copy-candidate") {
      const { candidate, error } = replayCandidatePreview();
      if (error) { showToast(error, "error", 5000); return; }
      copyCandidateJson(candidate);
      return;
    }
    if (action === "replay-export-candidate") {
      const { candidate, error } = replayCandidatePreview();
      if (error) { showToast(error, "error", 5000); return; }
      exportCandidateJson(candidate);
      return;
    }
    if (action === "replay-open-run") {
      openReplayRun(button.dataset.runId, replayScopeOf(button));
      return;
    }
    if (action === "replay-copy-run-candidate") {
      const detail = state.replay.detail;
      if (!detail) return;
      copyCandidateJson({
        rules: Array.isArray(detail.candidate_rules) ? detail.candidate_rules : [],
        ...(detail.candidate_prompt ? { prompt: String(detail.candidate_prompt) } : {}),
      });
      return;
    }
    // ---- 保存前差异预览 ----
    if (action === "save-preview-cancel") {
      state.savePreview = null;
      renderContent();
      return;
    }
    if (action === "save-preview-confirm") {
      // 人在看过差异之后点的这一下，才是真正的确认。skipPreview 让这一轮
      // 不再重复预览：差异刚摆出来，不该让人看第二遍。
      saveAllChanges({ skipPreview: true });
      return;
    }
    if (action === "save-preview-refresh") {
      runSavePreview();
      return;
    }
    if (action === "reload-config-after-conflict") {
      if (!await askConfirmation(
        "重新加载配置",
        "重新加载会丢弃所有未保存的配置、群组设置和群管理表单草稿。",
      )) return;
      state.configHistory.conflict = false;
      state.savePreview = null;
      state.skipSavePreview = false;
      state.groupTemplateButtonDrafts.clear();
      state.groupApiModelQuerySecretChanges.clear();
      clearResourceFormDrafts();
      loadAll();
      return;
    }
    // ---- 配置变更历史 ----
    if (action === "history-refresh") {
      loadConfigHistory();
      return;
    }
    if (action === "history-toggle") {
      toggleHistoryRevision(Number(button.dataset.revision));
      return;
    }
    if (action === "history-compare") {
      compareHistoryRevisions();
      return;
    }
    if (action === "history-restore") {
      prepareHistoryRestore(Number(button.dataset.revision));
      return;
    }
    if (action === "history-older") {
      loadOlderHistoryPage();
      return;
    }
    if (action === "history-newest") {
      loadNewestHistoryPage();
      return;
    }
    if (action === "history-restore-retry") {
      const revision = Number(state.configHistory.restore?.revision || 0);
      if (revision) prepareHistoryRestore(revision);
      return;
    }
    if (action === "history-restore-cancel") {
      state.configHistory.restore = null;
      renderContent();
      return;
    }
    if (action === "history-restore-confirm") {
      confirmHistoryRestore();
      return;
    }
    if (action === "toggle-group-card") {
      const card = button.closest("[data-group-card]");
      setGroupCardDisclosure(card, button.getAttribute("aria-expanded") !== "true");
      return;
    }
    if (action === "set-group-sections") {
      const card = button.closest("[data-group-card]");
      setGroupSectionsDisclosure(card, button.dataset.open === "true");
      return;
    }
    if (action === "jump-group-section") {
      revealGroupSection(button.dataset.groupId, button.dataset.groupSection);
      if (button.dataset.groupSection === "replay") {
        loadReplayWorkspace({ group: replayGroups().find(item => String(item.id) === String(button.dataset.groupId)) || null });
      }
      if (button.dataset.groupSection === OPERATIONS_SECTION_KEY) {
        const groupId = button.dataset.groupId;
        const entry = state.groupOperations.get(String(groupId));
        // 与展开路径同规：只有从未尝试过的群才顺带读一次，失败过就不再隐式重试。
        if (!entry?.attempted && !entry?.loaded && !entry?.loading && !entry?.refreshing) loadGroupOperations(groupId);
      }
      window.requestAnimationFrame(() => {
        const section = button.closest("[data-group-card]")?.querySelector(
          `[data-group-section="${CSS.escape(button.dataset.groupSection)}"]`,
        );
        section?.scrollIntoView({ block: "start", behavior: "smooth" });
      });
      return;
    }
    if (action === "list-page") {
      state.listPages.set(button.dataset.listKey, Number(button.dataset.page) || 1);
      renderContent();
      return;
    }
    if (action === "add-provider") {
      const existing = new Set(state.config.models.providers.map(item => item.name));
      let suffix = state.config.models.providers.length + 1;
      let name = `provider${suffix}`;
      while (existing.has(name)) name = `provider${++suffix}`;
      state.config.models.providers.push({
        name,
        provider: "openai",
        api_key: "",
        api_base: "",
        stream: false,
        chat_endpoint: "auto",
      });
      renderContent();
      updateChrome();
      return;
    }
    if (action === "remove-provider") {
      const index = Number(button.dataset.index);
      const provider = state.config.models.providers[index];
      if (!provider) return;
      if (state.config.models.providers.length <= 1) {
        showToast("至少需要保留一个模型供应商", "error");
        return;
      }
      const references = providerReferences(provider.name);
      if (references.length) {
        showToast(`请先移除以下引用：${references.join("、")}`, "warning", 6000);
        return;
      }
      if (!await askConfirmation("删除供应商", `确认删除 ${provider.name}？对应的已保存密钥也会在保存配置时清除。`)) return;
      state.config.models.providers.splice(index, 1);
      delete state.secretChanges[`providers.${provider.name}.api_key`];
      renderContent();
      updateChrome();
      return;
    }
    if (action === "add-fallback") {
      const role = state.config.models[button.dataset.role];
      const provider = state.config.models.providers[0]?.name || "";
      role.fallbacks.push({ provider, model: "", request_params: {} });
      renderContent();
      updateChrome();
      return;
    }
    if (action === "remove-fallback") {
      state.config.models[button.dataset.role].fallbacks.splice(Number(button.dataset.index), 1);
      renderContent();
      updateChrome();
      return;
    }
    if (action === "secret-clear") {
      const path = button.dataset.secretPath;
      state.secretChanges[path] = { action: "clear" };
      renderContent();
      updateChrome();
      return;
    }
    if (action === "secret-undo") {
      delete state.secretChanges[button.dataset.secretPath];
      renderContent();
      updateChrome();
      return;
    }
    if (action === "group-api-model-query-secret-clear") {
      state.groupApiModelQuerySecretChanges.set(String(button.dataset.groupId), { action: "clear" });
      renderContent();
      updateChrome();
      return;
    }
    if (action === "group-api-model-query-secret-undo") {
      state.groupApiModelQuerySecretChanges.delete(String(button.dataset.groupId));
      renderContent();
      updateChrome();
      return;
    }
    if (action === "select-prompt") {
      const promptList = content.querySelector(".prompt-list");
      const promptScrollLeft = promptList?.scrollLeft || 0;
      state.promptKey = button.dataset.promptKey;
      renderContent();
      const nextPromptList = content.querySelector(".prompt-list");
      if (nextPromptList) {
        nextPromptList.scrollLeft = promptScrollLeft;
        nextPromptList.querySelector(".prompt-button.active")?.scrollIntoView({
          block: "nearest",
          inline: "nearest",
        });
      }
      return;
    }
    if (action === "clear-prompt") {
      if (!await askConfirmation("使用内置 Prompt", "清空此文本后，运行时将使用项目内置的默认 Prompt。")) return;
      state.config.prompts[button.dataset.promptKey] = "";
      renderContent();
      updateChrome();
      return;
    }
    if (action === "load-call-admin-targets") {
      loadCallAdminTargets(button.dataset.groupId);
      return;
    }
    if (action === "load-group-permissions") {
      loadGroupPermissions(button.dataset.groupId);
      return;
    }
    if (action === "add-permission-window") {
      const group = state.groups.find(item => String(item.id) === String(button.dataset.groupId));
      const config = group?.settings?.default_permissions;
      if (!group || !config || state.groupSaving.has(String(group.id))) return;
      const ids = new Set(config.windows.map(window => window.id));
      let id = `window_${Date.now().toString(36)}`;
      while (ids.has(id)) id += "x";
      const firstField = state.permissionFields.find(field => field.key === "can_send_photos")?.key
        || state.permissionFields[0]?.key;
      config.windows.push({
        id,
        name: "夜间模式",
        enabled: true,
        start: "23:00",
        end: "07:00",
        days: [0, 1, 2, 3, 4, 5, 6],
        priority: 0,
        overrides: firstField ? { [firstField]: false } : {},
      });
      renderContent();
      updateChrome();
      return;
    }
    if (action === "remove-permission-window") {
      const group = state.groups.find(item => String(item.id) === String(button.dataset.groupId));
      const config = group?.settings?.default_permissions;
      if (!group || !config || state.groupSaving.has(String(group.id))) return;
      config.windows.splice(Number(button.dataset.windowIndex), 1);
      renderContent();
      updateChrome();
      return;
    }
    if (action === "trigger-patrol") {
      const groupId = button.dataset.groupId;
      if (!await askConfirmation(
        "手动触发巡检",
        "将立即分批检查该群所有已知成员的名字和简介；违规者会被禁言并要求真人质询。确认开始？",
      )) return;
      button.disabled = true;
      try {
        await apiFetch(`/api/v1/groups/${encodeURIComponent(groupId)}/patrol`, { method: "POST" });
        showToast("巡检已开始，将在后台分批执行");
      } catch (error) {
        showToast(error.message || "巡检启动失败", "error", 6000);
      } finally {
        button.disabled = false;
      }
      return;
    }
    if (action === "load-group-resources") {
      captureResourceFormDrafts();
      const preservingDrafts = groupResourceDraftDirty(button.dataset.groupId);
      await loadGroupResources(button.dataset.groupId);
      if (preservingDrafts) showToast("数据已刷新；有待保存草稿的分类保留本地内容", "success", 4800);
      return;
    }
    if (action === "load-group-operations") {
      // 只读面板：不抓表单草稿、不改配置，刷新不会碰未保存的输入。
      await loadGroupOperations(button.dataset.groupId);
      return;
    }
    if (action.startsWith("operations-")) {
      // B13b：来源/计划与六类任务工作流统一走这里（不新增委托监听器，
      // 现有 click/submit 处理器的边界不能动）。
      await handleOperationsAction(action, button);
      return;
    }
    if (action === "load-access") {
      loadAccess();
      return;
    }
    if (action === "delete-access") {
      const type = button.dataset.accessType;
      const id = button.dataset.accessId;
      captureResourceFormDrafts();
      if (type === "authorized-groups") {
        const targetGroup = state.groups.find(group => String(group.id) === String(id));
        if ((targetGroup && groupDirty(targetGroup)) || groupResourceDraftDirty(id)) {
          showToast("该群仍有待保存更改，请先“保存全部”或重新加载并丢弃草稿，再取消授权", "warning", 7000);
          return;
        }
      }
      let url = `/api/v1/${type}/${encodeURIComponent(id)}`;
      if (type === "admins") {
        const [groupId, userId] = String(id).split(":", 2);
        url = `/api/v1/groups/${encodeURIComponent(groupId)}/admins/${encodeURIComponent(userId)}`;
      }
      const accessDeleteMessage = type === "global-bans"
        ? "立即解除该用户的全局封禁？"
        : type === "global-exemptions"
          ? "立即取消该用户的全局资料筛查豁免？"
          : type === "admins"
            ? "立即移除该群管理员？"
            : "立即取消该群组授权？";
      if (!await askConfirmation("确认即时操作", `${accessDeleteMessage} 此操作不会等待“保存全部”。`)) return;
      button.disabled = true;
      beginImmediateMutation();
      const locksWorkspace = type === "authorized-groups";
      if (locksWorkspace) {
        state.accessMutating = true;
        updateChrome();
      }
      try {
        await apiFetch(url, { method: "DELETE" });
        if (type === "authorized-groups") {
          const groupKey = String(id);
          const groupIndex = state.groups.findIndex(group => String(group.id) === groupKey);
          if (groupIndex >= 0) state.groups.splice(groupIndex, 1);
          state.groupBaselines.delete(groupKey);
          state.groupTemplateButtonDrafts.delete(groupKey);
          state.groupApiModelQuerySecretChanges.delete(groupKey);
          state.groupResources.delete(groupKey);
          state.groupResourceLoads.delete(groupKey);
          state.groupPermissionLoads.delete(groupKey);
          state.groupTelegramAdmins.delete(groupKey);
          state.groupCardOpen.delete(groupKey);
          for (const key of [...state.groupSectionOpen.keys()]) {
            if (key.startsWith(`${groupKey}:`)) state.groupSectionOpen.delete(key);
          }
        }
        await loadAccess();
        if (type === "authorized-groups" && !anyGroupDirty() && !anyResourceFormDirty()) {
          await reloadGroups();
        }
        showToast(type === "authorized-groups" && (anyGroupDirty() || anyResourceFormDirty())
          ? "授权已更新；为保留未保存草稿，群组列表暂未刷新"
          : "操作已完成");
      } catch (error) {
        showToast(error.message || "操作失败", "error");
      } finally {
        button.disabled = false;
        if (locksWorkspace) {
          state.accessMutating = false;
          updateChrome();
        }
        endImmediateMutation();
      }
      return;
    }
    if (action === "delete-group-resource") {
      const groupId = button.dataset.groupId;
      const type = button.dataset.resourceType;
      const id = button.dataset.resourceId;
      if (["keyword-replies", "scheduled-messages", "rules", "memories"].includes(type)) {
        queueDeferredResourceDelete(groupId, type, id);
        return;
      }
      const actionText = type === "warnings"
        ? "清零此用户的警告记录"
        : type === "bans"
          ? "立即解除此用户的群内封禁"
          : type === "moderation-exemptions"
            ? "立即取消此用户的审核豁免"
            : "立即取消此用户的回复静默";
      if (!await askConfirmation("确认即时操作", `${actionText}？此操作会立即生效。`)) return;
      button.disabled = true;
      beginImmediateMutation();
      try {
        await apiFetch(`/api/v1/groups/${encodeURIComponent(groupId)}/${type}/${encodeURIComponent(id)}`, { method: "DELETE" });
        applyImmediateResourceDelete(groupId, type, id);
        renderContent();
        showToast("操作已完成");
      } catch (error) {
        showToast(error.message || "删除失败", "error");
      } finally {
        button.disabled = false;
        endImmediateMutation();
      }
      return;
    }
    if (action === "undo-delete-group-resource") {
      undoDeferredResourceDelete(button.dataset.groupId, button.dataset.resourceType, button.dataset.resourceId);
      return;
    }
    if (action === "onboarding-preset") {
      // chosen 在 forEach 回调里定义却在外面用 —— 真实点击会抛
      // ReferenceError，选中态与提交体里的 preset_id 因此都拿不到。
      const next = button.dataset.preset || "";
      state.onboardingPreset = state.onboardingPreset === next ? "" : next;
      const cards = content.querySelectorAll('[data-action="onboarding-preset"]');
      cards.forEach(card => {
        const chosen = (card.dataset.preset || "") === state.onboardingPreset;
        card.setAttribute("aria-checked", chosen ? "true" : "false");
        card.classList.toggle("is-selected", chosen);
      });
      return;
    }
    if (action === "onboarding-check") {
      await checkOnboardingReadiness(state.onboardingPreset);
      return;
    }
    if (action === "onboarding-submit") {
      await submitOnboardingRequest(state.onboardingPreset);
      return;
    }
    if (action === "onboarding-queue-refresh") {
      await loadOnboardingQueue();
      // 刷新也是"未确认决策"的收口入口：读回服务器上的终结状态才算确认。
      await resolvePendingConfirmations();
      return;
    }
    if (action === "onboarding-queue-more") {
      await loadOnboardingQueue({ append: true });
      return;
    }
    if (action === "onboarding-approve" || action === "onboarding-reject") {
      await decideOnboardingRequest(
        button.dataset.requestId,
        action === "onboarding-approve" ? "approve" : "reject",
      );
      return;
    }
    if (action === "onboarding-cancel") {
      await apiFetch(
        `/api/v1/onboarding/requests/${encodeURIComponent(button.dataset.requestId)}/cancel`,
        { method: "POST", body: JSON.stringify({}) },
      ).catch(error => showToast(error.message || "撤回失败", "error"));
      await loadOnboardingRequests(state.session);
      return;
    }
    if (action === "budget-save") {
      await saveGroupBudget(button.dataset.groupId);
      return;
    }
    if (action === "budget-reload") {
      await reloadGroupBudget(button.dataset.groupId);
      return;
    }
    if (action === "budget-conflict-use-server") {
      const groupId = button.dataset.groupId;
      const conflict = state.budgetConflicts?.get(String(groupId));
      if (conflict?.serverDraft) {
        // 采用服务器值：连"待确认的提交快照"一起丢掉 —— 那一次提交的内容已经
        // 被用户明确放弃了，留着只会让下次保存又去做一遍无谓的对账。
        rememberBudgetDraft(groupId, { ...conflict.serverDraft, submitted: null });
        clearBudgetConflict(groupId);
        setBudgetError(groupId, "");
        patchBudgetDraft(groupId, () => { }, { rerender: true });
        showToast("已放弃你的草稿，改用服务器上的配置。", "info");
      } else {
        // 没读到服务器值就不能假装"已采用"：走真正的重新读取。
        showToast("还没读到服务器配置，正在重新读取。", "info");
        await reloadGroupBudget(groupId);
      }
      return;
    }
    if (action === "budget-conflict-apply-mine") {
      const groupId = button.dataset.groupId;
      const conflict = state.budgetConflicts?.get(String(groupId));
      const draft = budgetDraftFor(groupId);
      if (!conflict || !draft) {
        showToast("冲突信息已失效，请点「重新读取」。", "error");
        return;
      }
      if (typeof conflict.serverRevision !== "number") {
        showToast("还没读到服务器最新的 revision，不能覆盖。", "error");
        return;
      }
      // 用户**显式**选择用新 revision 覆盖 —— 这是唯一会换 revision 重试的路径。
      // 同时清掉待确认的提交快照：那份内容已经被这次显式覆盖取代了。
      rememberBudgetDraft(groupId, {
        ...draft,
        revision: conflict.serverRevision,
        submitted: null,
      });
      clearBudgetConflict(groupId);
      await saveGroupBudget(groupId);
      return;
    }
    if (action === "budget-kv-add" || action === "budget-kv-remove") {
      const kind = button.dataset.kv;
      const groupId = button.closest(".budget-editor")?.dataset.groupId || "";
      const index = Number(button.dataset.index);
      patchBudgetDraft(groupId, values => {
        const rows = kind === "covers" ? values.covers : values.operations;
        if (action === "budget-kv-add") rows.push({ key: "", value: "" });
        else rows.splice(index, 1);
      }, { rerender: true, dirty: true });
      return;
    }
    if (action === "price-save") {
      await savePriceCatalog();
      return;
    }
    if (action === "price-reload") {
      await reloadPriceCatalog();
      return;
    }
    if (action === "price-row-add") {
      patchPriceDraft(
        rows => rows.push({ provider_key: "", model: "", currency: "", input: "", output: "", cache_read: "", cache_write: "" }),
        { rerender: true, dirty: true },
      );
      return;
    }
    if (action === "price-row-remove") {
      const index = Number(button.dataset.index);
      patchPriceDraft(rows => {
        if (index >= 0 && index < rows.length) rows.splice(index, 1);
      }, { rerender: true, dirty: true });
      return;
    }
    if (action === "price-conflict-use-server") {
      const price = priceState();
      const conflict = price.conflict;
      if (conflict?.serverRows) {
        // 采用服务器值：连"待确认的提交快照"一起丢掉 —— 那一次提交的内容已经被
        // 用户明确放弃了，留着只会让下次保存又做一遍无谓的对账。
        price.rows = priceCloneRows(conflict.serverRows);
        price.baseRows = priceCloneRows(conflict.serverRows);
        price.version = Number(conflict.serverVersion);
        price.submitted = null;
        price.conflict = null;
        price.dirty = false;
        price.error = "";
        price.status = "";
        renderPriceCatalog();
        showToast("已放弃你的草稿，改用服务器上的价格目录。", "info");
      } else {
        // 没读到服务器值就不能假装"已采用"：走真正的重新读取。
        showToast("还没读到服务器目录，正在重新读取。", "info");
        await reloadPriceCatalog();
      }
      return;
    }
    if (action === "price-conflict-apply-mine") {
      const price = priceState();
      const conflict = price.conflict;
      if (!conflict || !Number.isSafeInteger(Number(conflict.serverVersion))) {
        showToast("还没读到服务器最新版本，不能重基。", "error");
        return;
      }
      // 用户**显式**选择把草稿重基到新版本 —— 这里用三方合并把"我这一轮真的改过
      // 的字段"顶到服务器当前值上，其余字段采用服务器值（不整份覆盖别人的改动），
      // 并且**不**自动写入；保存仍要用户自己点。这是唯一会换版本的路径。
      // 行的增删也走三方：我本地删的行不会被复活，服务器删的行不会被加回来。
      const baseRows = priceCloneRows(price.baseRows);
      const mineRows = priceCloneRows(price.rows);
      const merge = mergePriceRows(baseRows, mineRows, conflict.serverRows);
      price.rows = merge.rows;
      price.baseRows = priceCloneRows(conflict.serverRows);
      price.version = Number(conflict.serverVersion);
      price.conflict = null;
      price.submitted = null;
      price.dirty = true;
      price.error = "";
      const notes = [];
      if (merge.keptDeletions.length) {
        notes.push(`你在本地删掉的 ${merge.keptDeletions.length} 行没有被复活（${merge.keptDeletions.join("、")}）`);
      }
      if (merge.serverDeletions.length) {
        notes.push(`服务器已经删掉的 ${merge.serverDeletions.length} 行没有替你加回来（${merge.serverDeletions.join("、")}）`);
      }
      price.status = `已把你的改动重基到服务器版本 revision=${price.version}（只把你在本轮改过的字段顶上去，其余字段用服务器当前值）：请确认后点「保存新版本」，不会自动写入。${notes.length ? ` ${notes.join("；")}。` : ""}`;
      renderPriceCatalog();
      showToast("草稿已重基到服务器最新版本", "info");
      return;
    }
    if (action === "cost-reload") {
      await loadGroupCostPanel(button.dataset.groupId);
      return;
    }
    if (action === "select-preset") {
      const groupId = String(button.dataset.groupId || "");
      const presetId = String(button.dataset.preset || "");
      if (!state.selectedPreset) state.selectedPreset = new Map();
      state.selectedPreset.set(
        groupId,
        state.selectedPreset.get(groupId) === presetId ? "" : presetId,
      );
      // 换预设就丢掉上一份预览：预览必须与当前选择一一对应，
      // 否则会出现"预览 A、应用 B"。
      state.presetPreview?.delete(groupId);
      render();
      return;
    }
    if (action === "preset-preview") {
      const group = state.groups.find(item => String(item.id) === String(button.dataset.groupId));
      const presetId = state.selectedPreset?.get(String(group?.id)) || "";
      if (!group || !presetId) {
        showToast("请先选择一个预设。", "error");
        return;
      }
      const preview = await apiFetch(
        `/api/v1/groups/${encodeURIComponent(group.id)}/preset-preview`,
        { method: "POST", body: JSON.stringify({ preset_id: presetId }) },
      );
      state.presetPreview?.set(String(group.id), preview);
      showToast(`将修改 ${(preview.diff || []).length} 项设置`, "info");
      render();
      return;
    }
    if (action === "preset-apply") {
      const group = state.groups.find(item => String(item.id) === String(button.dataset.groupId));
      const presetId = state.selectedPreset?.get(String(group?.id)) || "";
      if (!group || !presetId) {
        showToast("请先选择一个预设。", "error");
        return;
      }
      button.dataset.busy = "1";
      try {
        const result = await apiFetch(
          `/api/v1/groups/${encodeURIComponent(group.id)}/preset-apply`,
          {
            method: "POST",
            body: JSON.stringify({ preset_id: presetId, revision: group.revision }),
          },
        );
        state.groupBaselines.set(String(group.id), clone(result.group.settings));
        const index = state.groups.findIndex(item => String(item.id) === String(group.id));
        if (index >= 0) {
          state.groups[index] = {
            id: result.group.id,
            title: result.group.title,
            revision: result.group.revision,
            settings: normalizeGroupSettings(result.group.settings),
          };
        }
        state.presetPreview?.delete(String(group.id));
        render();
        showToast("预设已应用", "success");
      } catch (error) {
        // 409 = 别人改过群设置：草稿必须留着，让管理员自己决定重试还是丢弃。
        showToast(error.message || "预设应用失败", "error");
      } finally {
        delete button.dataset.busy;
      }
      return;
    }
    if (action === "reload-groups") {
      if (state.loading || state.saving || state.reloadingGroups || state.accessMutating || state.immediateMutations > 0 || state.groupSaving.size) return;
      captureResourceFormDrafts();
      if ((anyGroupDirty() || anyResourceFormDirty()) && !await askConfirmation(
        "刷新群组",
        "刷新会丢弃所有未保存的群组设置和群管理表单草稿。",
      )) return;
      state.groupTemplateButtonDrafts.clear();
      state.groupApiModelQuerySecretChanges.clear();
      clearResourceFormDrafts();
      await reloadGroups();
    }
  });

  const ENTRY_FORM_FIELDS = {
    "keyword-replies": {
      strings: ["keyword", "match_type", "reply_text"],
      booleans: ["pin_message", "auto_delete", "disable_link_preview", "enabled"],
      numbers: [],
    },
    "scheduled-messages": {
      strings: ["text", "schedule_type", "schedule_time"],
      booleans: ["pin_message", "unpin_previous", "auto_delete", "disable_link_preview", "enabled"],
      numbers: ["interval_minutes"],
    },
  };

  function readEntryFormValues(form, type, { includeEnabled }) {
    const spec = ENTRY_FORM_FIELDS[type];
    const values = {};
    for (const name of spec.strings) {
      const control = form.elements[name];
      if (!control || control.disabled) continue;
      values[name] = control.value;
    }
    for (const name of spec.numbers) {
      const control = form.elements[name];
      if (control && !control.disabled) values[name] = Number(control.value);
    }
    for (const name of spec.booleans) {
      if (name === "enabled" && !includeEnabled) continue;
      const control = form.elements[name];
      if (control) values[name] = control.checked;
    }
    const buttonsControl = form.elements.buttons_text;
    if (buttonsControl) values.buttons = parseTemplateButtonsText(buttonsControl.value);
    return values;
  }

  function readDeferredResourceFormValues(form, type, { includeEnabled = false } = {}) {
    if (ENTRY_FORM_FIELDS[type]) return readEntryFormValues(form, type, { includeEnabled });
    if (type === "rules") {
      return {
        rule_type: form.elements.rule_type.value,
        pattern: form.elements.pattern.value,
        scan_scope: ruleScanScope({ scan_scope: form.elements.scan_scope?.value }),
        action: form.elements.action.value,
        ...(includeEnabled ? { enabled: form.elements.enabled?.checked !== false } : {}),
      };
    }
    if (type === "memories") return { content: form.elements.content.value };
    throw new Error("不支持的草稿类型");
  }

  function deferredResourceDraftItem(type, id, values) {
    if (type === "keyword-replies") return {
      id,
      keyword: values.keyword,
      match_type: values.match_type || "contains",
      reply_text: values.reply_text,
      buttons: clone(values.buttons || []),
      pin_message: Boolean(values.pin_message),
      auto_delete: values.auto_delete !== false,
      disable_link_preview: values.disable_link_preview !== false,
      enabled: values.enabled !== false,
    };
    if (type === "scheduled-messages") return {
      id,
      text: values.text,
      buttons: clone(values.buttons || []),
      schedule_type: values.schedule_type || "daily",
      schedule_time: values.schedule_time || "09:00",
      interval_minutes: Number(values.interval_minutes || 60),
      pin_message: Boolean(values.pin_message),
      unpin_previous: Boolean(values.unpin_previous),
      auto_delete: Boolean(values.auto_delete),
      disable_link_preview: values.disable_link_preview !== false,
      enabled: values.enabled !== false,
    };
    if (type === "rules") return {
      id,
      rule_type: values.rule_type || "keyword",
      pattern: values.pattern,
      action: values.action || "warn",
      scan_scope: ruleScanScope({ scan_scope: values.scan_scope }),
      enabled: values.enabled !== false,
    };
    return { id, content: values.content || "" };
  }

  function stageDeferredResource(form) {
    const type = form.dataset.resourceForm;
    const meta = RESOURCE_TYPE_META[type];
    if (!meta || !["automation", "rules-memory"].includes(meta.category)) return false;
    syncScheduledFormFields(form);
    captureResourceFormDraft(form);
    if (!form.reportValidity()) return true;
    let values;
    try {
      values = readDeferredResourceFormValues(form, type);
    } catch (error) {
      showToast(error.message || "草稿格式无效", "error", 6000);
      return true;
    }
    const id = `draft-${Date.now().toString(36)}-${++state.resourceDraftSequence}`;
    const operation = { groupId: String(form.dataset.groupId), type, id, values: clone(values) };
    const operationKey = resourceOperationKey(operation.groupId, type, id);
    state.pendingResourceCreates.set(operationKey, operation);
    const list = resourceArray(operation.groupId, type);
    list.unshift(deferredResourceDraftItem(type, id, values));
    const createKey = resourceDraftFormKey(form);
    state.resourceFormDrafts.delete(createKey);
    state.resourceFormBaselines.delete(createKey);
    state.listPages.set(`group:${operation.groupId}:${type}`, 1);
    renderContent();
    updateChrome();
    showToast(`${meta.label}已加入待保存列表`, "success", 2600);
    return true;
  }

  function removePendingResourceCreate(groupId, type, id) {
    const operationKey = resourceOperationKey(groupId, type, id);
    if (!state.pendingResourceCreates.has(operationKey)) return false;
    state.pendingResourceCreates.delete(operationKey);
    state.pendingResourceDeletes.delete(operationKey);
    const list = resourceArray(groupId, type);
    const index = list.findIndex(item => String(item.id) === String(id));
    if (index >= 0) list.splice(index, 1);
    const editKey = resourceEditDraftKey(groupId, type, id);
    state.resourceFormDrafts.delete(editKey);
    state.resourceFormBaselines.delete(editKey);
    renderContent();
    updateChrome();
    return true;
  }

  function queueDeferredResourceDelete(groupId, type, id) {
    if (removePendingResourceCreate(groupId, type, id)) {
      showToast("已移除待新增草稿", "success", 2400);
      return;
    }
    const key = resourceOperationKey(groupId, type, id);
    state.pendingResourceDeletes.set(key, { groupId: String(groupId), type, id: String(id) });
    renderContent();
    updateChrome();
    showToast("已标记删除，点击“保存全部”后生效", "warning", 3600);
  }

  function undoDeferredResourceDelete(groupId, type, id) {
    state.pendingResourceDeletes.delete(resourceOperationKey(groupId, type, id));
    renderContent();
    updateChrome();
  }

  function setRequestFormPending(form, pending) {
    if (pending) form.dataset.requestPending = "true";
    else delete form.dataset.requestPending;
    form.setAttribute("aria-busy", String(pending));
    const submitButton = form.querySelector('[type="submit"]');
    if (submitButton) submitButton.disabled = pending;
  }

  function beginImmediateMutation() {
    state.immediateMutations += 1;
    updateChrome();
  }

  function endImmediateMutation() {
    state.immediateMutations = Math.max(0, state.immediateMutations - 1);
    updateChrome();
  }

  // During a pure PATCH ACK only sibling form controls remain interactive;
  // do not unlock deletes, creates, immediate actions or group navigation.
  for (const eventName of ["pointerdown", "click", "submit"]) {
    content.addEventListener(eventName, event => {
      if (!state.saving || !state.resourcePatchEditing) return;
      const form = event.target.closest('form[data-rule-edit-form], form[data-entry-edit-form="keyword-replies"], form[data-entry-edit-form="scheduled-messages"]');
      if (eventName === "submit" || !form || event.target.closest("button, a")) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    }, true);
  }

  content.addEventListener("submit", async event => {
    const memoryEdit = event.target.closest("[data-memory-edit]");
    if (memoryEdit) {
      event.preventDefault();
      // 人工纠错：草稿先落到状态，409/未确认都保留草稿，绝不自动重发。
      memoryFactEditState(memoryEdit.dataset.groupId, memoryEdit.dataset.memoryEdit).text = memoryEdit.querySelector('[name="text"]')?.value ?? "";
      await saveMemoryFactCorrection(memoryEdit.dataset.groupId, memoryEdit.dataset.memoryEdit);
      return;
    }
    const memoryFilter = event.target.closest("[data-memory-filter]");
    if (memoryFilter) {
      event.preventDefault();
      const read = memoryReadState(memoryFilter.dataset.groupId, memoryFilter.dataset.memoryFilter);
      // 只有“筛选”才提交草稿；旧 next_cursor 随之作废，回到第一页。
      read.draft = memoryFilterValues(memoryFilter);
      read.filters = { ...read.draft };
      read.applied = null;
      if (read.data) read.data.next_cursor = null;
      loadMemoryTransparency(memoryFilter.dataset.groupId, memoryFilter.dataset.memoryFilter);
      return;
    }
    const replayForm = event.target.closest("[data-replay-form]");
    if (replayForm) {
      event.preventDefault();
      if (replayForm.dataset.requestPending === "true") return;
      if (!replayForm.reportValidity()) return;
      replayForm.dataset.requestPending = "true";
      const submitButton = replayForm.querySelector('[type="submit"]');
      if (submitButton) submitButton.disabled = true;
      try {
        await createReplaySample(replayForm);
      } finally {
        delete replayForm.dataset.requestPending;
        if (submitButton) submitButton.disabled = false;
      }
      return;
    }
    const entryEditForm = event.target.closest("[data-entry-edit-form]");
    if (entryEditForm) {
      event.preventDefault();
      syncScheduledFormFields(entryEditForm);
      captureResourceFormDraft(entryEditForm);
      if (!entryEditForm.reportValidity()) return;
      try {
        readEntryFormValues(entryEditForm, entryEditForm.dataset.entryEditForm, { includeEnabled: true });
      } catch (error) {
        showToast(error.message || "内联按钮格式无效", "error", 6000);
        return;
      }
      showToast("修改已记录，点击“保存全部”后生效", "success", 2600);
      return;
    }
    const ruleEditForm = event.target.closest("[data-rule-edit-form]");
    if (ruleEditForm) {
      event.preventDefault();
      captureResourceFormDraft(ruleEditForm);
      if (!ruleEditForm.reportValidity()) return;
      showToast("群规修改已记录，点击“保存全部”后生效", "success", 2600);
      return;
    }
    const memoryEditForm = event.target.closest("[data-memory-edit-form]");
    if (memoryEditForm) {
      event.preventDefault();
      captureResourceFormDraft(memoryEditForm);
      if (!memoryEditForm.reportValidity()) return;
      showToast("记忆修改已记录，点击“保存全部”后生效", "success", 2600);
      return;
    }
    const accessForm = event.target.closest("[data-access-form]");
    if (accessForm) {
      event.preventDefault();
      if (accessForm.dataset.requestPending === "true") return;
      if (!accessForm.reportValidity()) return;
      const type = accessForm.dataset.accessForm;
      const values = Object.fromEntries(new FormData(accessForm).entries());
      for (const key of ["group_id", "user_id"]) if (values[key] != null) values[key] = Number(values[key]);
      let url = `/api/v1/${type}`;
      if (type === "admins") {
        url = `/api/v1/groups/${encodeURIComponent(values.group_id)}/admins`;
        delete values.group_id;
      }
      const accessMessage = type === "authorized-groups"
        ? `立即授权群组 ${values.group_id}？`
        : type === "admins"
          ? `立即添加管理员 ${values.user_id}？`
          : `立即全局封禁用户 ${values.user_id}？`;
      if (!await askConfirmation("确认即时操作", `${accessMessage} 此操作不会等待“保存全部”。`)) return;
      setRequestFormPending(accessForm, true);
      beginImmediateMutation();
      try {
        await apiFetch(url, { method: "POST", body: JSON.stringify(values) });
        await loadAccess();
        if (type === "authorized-groups" && !anyGroupDirty() && !anyResourceFormDirty()) {
          await reloadGroups();
        }
        showToast(type === "authorized-groups" && (anyGroupDirty() || anyResourceFormDirty())
          ? "授权已更新；为保留未保存草稿，群组列表暂未刷新"
          : "操作已完成");
      } catch (error) {
        showToast(error.message || "操作失败", "error");
      } finally {
        setRequestFormPending(accessForm, false);
        endImmediateMutation();
      }
      return;
    }
    const form = event.target.closest("[data-resource-form]");
    if (!form) return;
    event.preventDefault();
    if (form.dataset.requestPending === "true") return;
    if (form.dataset.saveScope === "deferred") {
      stageDeferredResource(form);
      return;
    }
    syncScheduledFormFields(form);
    if (!form.reportValidity()) return;
    const groupId = form.dataset.groupId;
    const type = form.dataset.resourceForm;
    let values;
    try {
      values = ENTRY_FORM_FIELDS[type]
        ? readEntryFormValues(form, type, { includeEnabled: false })
        : Object.fromEntries(new FormData(form).entries());
    } catch (error) {
      showToast(error.message || "内联按钮格式无效", "error", 6000);
      return;
    }
    if (values.user_id != null) values.user_id = Number(values.user_id);
    if (["bans", "moderation-exemptions", "reply-mutes"].includes(type)) {
      const message = type === "bans"
        ? `立即封禁用户 ${values.user_id}？`
        : type === "moderation-exemptions"
          ? `立即将用户 ${values.user_id} 加入审核豁免？`
          : `立即将用户 ${values.user_id} 加入回复静默名单？`;
      if (!await askConfirmation("确认即时操作", `${message} 此操作不会等待“保存全部”。`)) return;
    }
    setRequestFormPending(form, true);
    beginImmediateMutation();
    try {
      const result = await apiFetch(`/api/v1/groups/${encodeURIComponent(groupId)}/${type}`, {
        method: "POST",
        body: JSON.stringify(values),
      });
      applyImmediateResourceCreate(groupId, type, result);
      form.reset();
      renderContent();
      showToast("操作已完成");
    } catch (error) {
      showToast(error.message || "新增失败", "error");
    } finally {
      setRequestFormPending(form, false);
      endImmediateMutation();
    }
  });

  function switchTab(tab) {
    if (state.loading || state.saving || state.reloadingGroups || state.accessMutating || state.immediateMutations > 0) return;
    if (!navItems().some(item => item.id === tab)) return;
    const fromMobileDrawer = app.classList.contains("sidebar-open");
    state.activeTab = tab;
    setMobileSidebarOpen(false);
    updateChrome();
    renderContent({ resetScroll: true });
    // 两块按需加载的视图各自在进入时拉一次；没有它们的话页面会一直空着。
    if (tab === "safety") loadReplayWorkspace();
    if (tab === "history" && !state.configHistory.entries.length) loadConfigHistory();
    if (fromMobileDrawer) window.requestAnimationFrame(() => content.focus({ preventScroll: true }));
    const activeMobile = mobileNav.querySelector(`[data-nav="${CSS.escape(tab)}"]`);
    if (activeMobile) {
      const left = activeMobile.offsetLeft - (mobileNav.clientWidth - activeMobile.offsetWidth) / 2;
      mobileNav.scrollTo({ left: Math.max(0, left), behavior: "smooth" });
    }
  }

  document.addEventListener("click", event => {
    const nav = event.target.closest("[data-nav]");
    if (nav) switchTab(nav.dataset.nav);
  });

  function setMobileSidebarOpen(open) {
    const nextOpen = Boolean(open && mobileSidebarMedia.matches);
    sidebar.classList.toggle("mobile-open", nextOpen);
    app.classList.toggle("sidebar-open", nextOpen);
    sidebarToggle.setAttribute("aria-expanded", String(nextOpen));
    sidebarToggle.setAttribute("aria-label", nextOpen ? "关闭导航" : "打开导航");
    sidebarToggle.title = nextOpen ? "关闭导航" : "打开导航";
    syncWorkspaceInteractionLock();
    if (nextOpen) {
      window.requestAnimationFrame(() => {
        (sidebar.querySelector(".nav-button.active") || sidebar.querySelector(".nav-button"))?.focus();
      });
    }
  }

  sidebarToggle.addEventListener("click", () => {
    setMobileSidebarOpen(!sidebar.classList.contains("mobile-open"));
  });

  sidebarClose.addEventListener("click", () => {
    setMobileSidebarOpen(false);
    sidebarToggle.focus();
  });

  const handleMobileSidebarMediaChange = event => {
    if (!event.matches && app.classList.contains("sidebar-open")) setMobileSidebarOpen(false);
  };
  if (mobileSidebarMedia.addEventListener) mobileSidebarMedia.addEventListener("change", handleMobileSidebarMediaChange);
  else mobileSidebarMedia.addListener?.(handleMobileSidebarMediaChange);

  app.addEventListener("click", event => {
    if (event.target === app && app.classList.contains("sidebar-open")) {
      setMobileSidebarOpen(false);
      sidebarToggle.focus();
    }
  });

  document.addEventListener("keydown", event => {
    if (!app.classList.contains("sidebar-open")) return;
    if (event.key === "Escape") {
      setMobileSidebarOpen(false);
      sidebarToggle.focus();
      return;
    }
    if (event.key === "Tab") {
      const focusable = [...sidebar.querySelectorAll("button:not([disabled])")];
      if (!focusable.length) return;
      const current = focusable.indexOf(document.activeElement);
      const next = event.shiftKey
        ? (current <= 0 ? focusable.length - 1 : current - 1)
        : (current < 0 || current === focusable.length - 1 ? 0 : current + 1);
      event.preventDefault();
      focusable[next].focus();
    }
  });

  saveButton.addEventListener("click", saveAllChanges);

  reloadButton.addEventListener("click", async () => {
    if (state.loading || state.saving || state.reloadingGroups || state.accessMutating || state.immediateMutations > 0 || state.groupSaving.size) return;
    captureResourceFormDrafts();
    if ((configDirty() || anyGroupDirty() || anyResourceFormDirty()) && !await askConfirmation(
      "重新加载",
      "重新加载会丢弃所有未保存的配置、群组设置和群管理表单草稿。",
    )) return;
    state.groupTemplateButtonDrafts.clear();
    state.groupApiModelQuerySecretChanges.clear();
    clearResourceFormDrafts();
    // 「本次不再预览差异」承诺的是"仅本页有效"，重新加载就把它收回默认路径。
    state.skipSavePreview = false;
    loadAll();
  });

  window.addEventListener("beforeunload", event => {
    captureResourceFormDrafts();
    if (!configDirty() && !anyGroupDirty() && !anyResourceFormDirty()) return;
    event.preventDefault();
    event.returnValue = "";
  });

  telegramInit();
  updateNavigation();
  refreshIcons();
  loadAll({ keepTab: false });
})();
