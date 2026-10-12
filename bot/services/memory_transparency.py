"""C2 group-only read model. No generation, cleanup, evidence text or writes."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit
from sqlalchemy import select, exists, or_, func, cast, case, String, Integer
from bot.db.models import UserFact, MemoryOptout, SearchResultRecord, GroupMessageArchive, Violation
from bot.services.long_term_memory import normalize_category
from bot.services.search_memory import search_record_retention_days, search_freshness_hours
from bot.services.group_summary import SqlGroupSummaryStore, group_summary_config
from bot.utils.message_links import message_link
from bot.utils.timezone import SHANGHAI_TZ, now_shanghai_naive

#: 生产写入的是上海本地墙上时间（naive），SQLite 日期函数会把无时区字符串按 UTC
#: 解释；带偏移的历史记录则按自己的偏移换算。两者必须落在同一绝对时刻上比较，
#: 否则同一时刻的两种写法会差 8 小时——留存边界直接把窗口内的行筛掉。
SHANGHAI_UTC_OFFSET_SECONDS = 8 * 60 * 60


def _stored_instant_seconds(column):
    """把库里的时间文本换算成绝对时刻（epoch 秒）的 SQL 表达式。

    naive 文本按上海墙上时间解释（减去 +08:00），带 ``+HH:MM`` / ``-HH:MM`` / ``Z``
    的文本按自身偏移解释；无法解析的时间返回 NULL，调用方按“未知时间”处理。
    """
    text = cast(column, String)
    after_date = func.substr(text, 11)
    carries_offset = or_(func.instr(after_date, "+") > 0, func.instr(after_date, "-") > 0,
                         func.upper(after_date).like("%Z%"))
    epoch = cast(func.strftime("%s", text), Integer)
    return case((carries_offset, epoch), else_=epoch - SHANGHAI_UTC_OFFSET_SECONDS)


def _retention_cutoff_seconds(now, retention_days):
    cutoff = now - timedelta(days=retention_days)
    return int((cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=SHANGHAI_TZ)).timestamp())


def safe_url(value):
    if not isinstance(value, str) or len(value) > 2048 or "\\" in value or any(ord(c) < 33 or ord(c) == 127 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            return None
        _ = parsed.port
    except ValueError:
        return None
    return value


def stamp(value):
    return value.isoformat() if value else None


async def facts(session_factory, group_id, *, limit=20, cursor=None, subject=None, category=None):
    now = now_shanghai_naive()
    # Explicit scalar projection: evidence_excerpt is never even loaded.
    columns = [UserFact.id, UserFact.subject_user_id, UserFact.fact_text, UserFact.category,
               UserFact.confidence, UserFact.confirm_count, UserFact.first_seen_at,
               UserFact.last_confirmed_at, UserFact.source_kind, UserFact.source_message_id,
               UserFact.revision, UserFact.corrected_by, UserFact.corrected_at]
    filters = [UserFact.scope == 'group', UserFact.scope_id == group_id,
               UserFact.status == 'active', or_(UserFact.expires_at.is_(None), UserFact.expires_at > now),
               ~exists(select(MemoryOptout.user_id).where(MemoryOptout.user_id == UserFact.subject_user_id))]
    if cursor is not None:
        filters.append(UserFact.id < cursor)
    if subject is not None:
        filters.append(UserFact.subject_user_id == subject)
    if category:
        known = {'identity', 'preference', 'relationship', 'event', 'taboo', 'skill'}
        filters.append(func.lower(func.trim(UserFact.category)) == category if category != 'other' else or_(UserFact.category.is_(None), ~func.lower(func.trim(UserFact.category)).in_(known)))
    async with session_factory() as session:
        rows = (await session.execute(select(*columns).where(*filters).order_by(UserFact.id.desc()).limit(limit + 1))).all()
        items = []
        for row in rows[:limit]:
            source_state = 'unavailable'
            source_url = None
            if row.source_message_id and row.source_message_id > 0:
                try:
                    present = (await session.execute(select(GroupMessageArchive.id, GroupMessageArchive.message_thread_id).where(
                        GroupMessageArchive.group_id == group_id,
                        GroupMessageArchive.telegram_message_id == row.source_message_id).limit(1))).first()
                    # Conservative: any moderation record suppresses a source link.
                    moderated = (await session.execute(select(Violation.id).where(
                        Violation.group_id == group_id, Violation.source_message_id == row.source_message_id).limit(1))).scalar_one_or_none()
                    source_state = 'unavailable' if moderated else 'verified' if present else 'unavailable'
                    if source_state == 'verified':
                        # C3：链接统一由 message_link 构造，Topics 线程只取这条归档行
                        # 自己的可信值（旧行 NULL → 保持原非 Topics 形状）。
                        # 透明面只拿到群 ID、没有 username，所以公开群也用成员可见形状。
                        source_url = message_link(
                            SimpleNamespace(id=group_id, username=''),
                            row.source_message_id,
                            present.message_thread_id,
                        )[0] or None
                except Exception:
                    source_state = 'unverifiable'
            items.append({'id': row.id, 'subject_user_id': row.subject_user_id,
                          'fact_text': str(row.fact_text or '')[:200], 'category': normalize_category(row.category),
                          'confidence': max(0, min(100, int(row.confidence or 0))),
                          'confirm_count': max(1, int(row.confirm_count or 1)),
                          'first_seen_at': stamp(row.first_seen_at), 'last_confirmed_at': stamp(row.last_confirmed_at),
                          'source_kind': row.source_kind if row.source_kind in {'passive', 'tool'} else 'unknown',
                          'revision': max(1, int(row.revision or 1)),
                          # 人工纠错标记：正文已被人工改写，不代表来源原话。
                          'corrected': row.corrected_by is not None,
                          'corrected_by': int(row.corrected_by) if row.corrected_by is not None else None,
                          'corrected_at': stamp(row.corrected_at),
                          'source_state': source_state, 'source_url': source_url})
    return {'items': items, 'has_more': len(rows) > limit,
            'next_cursor': str(items[-1]['id']) if len(rows) > limit else None}


async def searches(session_factory, settings, group_id, *, limit=20, cursor=None, kind=None):
    now = now_shanghai_naive()
    retention = search_record_retention_days(settings)
    # 留存边界按绝对时刻比较：naive 上海时间与带偏移的历史值必须等价，
    # 同时未知时间继续进列表（展示为 unknown），不在 SQL 之外做二次过滤。
    stored_instant = _stored_instant_seconds(SearchResultRecord.created_at)
    filters = [SearchResultRecord.scope == 'group', SearchResultRecord.scope_id == group_id,
               or_(stored_instant.is_(None), stored_instant >= _retention_cutoff_seconds(now, retention))]
    if cursor is not None:
        filters.append(SearchResultRecord.id < cursor)
    if kind:
        filters.append(SearchResultRecord.kind == kind if kind != 'unknown' else
                       ~SearchResultRecord.kind.in_({'price', 'news', 'fact'}))
    now_instant = _retention_cutoff_seconds(now, 0)
    async with session_factory() as session:
        rows = (await session.execute(select(SearchResultRecord.id, SearchResultRecord.query, SearchResultRecord.digest, SearchResultRecord.sources, SearchResultRecord.kind, SearchResultRecord.outcome, stored_instant.label('instant')).where(*filters).order_by(SearchResultRecord.id.desc()).limit(limit + 1))).all()
        items = []
        for row in rows[:limit]:
            normalized_kind = row.kind if row.kind in {'price', 'news', 'fact'} else 'unknown'
            hours = search_freshness_hours(settings, normalized_kind)
            # 展示时刻与留存筛选同源：都来自 SQL 里换算出的绝对时刻，
            # 不再用第二套 Python 解析，避免“筛掉了却显示很新”。
            try:
                created_at = datetime.fromtimestamp(row.instant, SHANGHAI_TZ).replace(tzinfo=None) if row.instant is not None else None
                age = (now_instant - row.instant) if created_at is not None else None
            except (OverflowError, OSError, ValueError):
                created_at, age = None, None
            freshness = 'unknown' if normalized_kind == 'unknown' or age is None or age < 0 else 'fresh' if age <= hours * 3600 else 'stale'
            sources = []
            for source in row.sources if isinstance(row.sources, list) else []:
                if not isinstance(source, dict):
                    continue
                url = safe_url(source.get('url'))
                if url:
                    sources.append({'title': str(source.get('title') or '')[:300], 'url': url})
            items.append({'id': row.id, 'query': str(row.query or '')[:512], 'digest': str(row.digest or '')[:6000],
                          'kind': normalized_kind, 'outcome': row.outcome if row.outcome in {'ok', 'empty'} else 'unknown',
                          'created_at': stamp(created_at), 'freshness': freshness,
                          'freshness_hours': hours if normalized_kind != 'unknown' else None, 'sources': sources[:20]})
    return {'items': items, 'has_more': len(rows) > limit,
            'next_cursor': str(items[-1]['id']) if len(rows) > limit else None, 'retention_days': retention}


async def summary(session_factory, settings, group_id):
    # Use the actual consumer's protection, not an invented age TTL.
    if not group_summary_config(settings).enabled:
        return {'status': 'disabled', 'document': None}
    store = SqlGroupSummaryStore(session_factory)
    record = await store.load(group_id)
    if record is None:
        return {'status': 'empty', 'document': None}
    valid = record.source_revision >= 0 and record.source_revision == await store.content_revision(group_id)
    if valid:
        valid = await store.coverage_intact(group_id, covered_from_id=record.covered_from_id,
                                            covered_through_id=record.covered_through_id,
                                            covered_count=record.covered_count, source_revision=record.source_revision)
    document = {'version': record.version, 'generated_at': stamp(record.generated_at),
                'covered_from_id': record.covered_from_id, 'covered_through_id': record.covered_through_id,
                'covered_count': record.covered_count, 'source_truncated': record.source_truncated,
                'source_revision': record.source_revision, 'covered_from_key': record.covered_from_key,
                'covered_through_key': record.covered_through_key, 'summary': record.summary if valid else None}
    return {'status': 'valid' if valid else 'invalid', 'document': document}
