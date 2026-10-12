"""C2 signed HTTP, actual project SQLite, group isolation and zero-write snapshots."""
from datetime import timedelta, timezone
import sqlite3
from unittest.mock import patch
from sqlalchemy import select
from bot.db.models import UserFact, SearchResultRecord, MemoryOptout, GroupMessageArchive, GroupSummary, Admin
from bot.utils.timezone import now_shanghai_naive
from test_permanent_memory_create_dedup import _RaceInjectedTestCase, GROUP_A, GROUP_B


class MemoryTransparencyTests(_RaceInjectedTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        now = now_shanghai_naive()
        async with self.session_factory() as session:
            session.add(Admin(group_id=GROUP_A, user_id=99, role='admin'))
            for i in range(25):
                session.add(UserFact(scope='group', scope_id=GROUP_A, subject_user_id=i % 2,
                                     fact_text=f'visible-{i}', fingerprint=f'v-{i}', evidence_excerpt='SECRET_EXCERPT'))
            for scope, gid, status, subject, expiry, text in [
                ('private', GROUP_A, 'active', 1, None, 'PRIVATE'),
                ('group', GROUP_B, 'active', 1, None, 'FOREIGN'),
                ('group', GROUP_A, 'deleted', 1, None, 'DELETED'),
                ('group', GROUP_A, 'superseded', 1, None, 'SUPERSEDED'),
                ('group', GROUP_A, 'active', 8, None, 'OPTOUT'),
                ('group', GROUP_A, 'active', 1, now-timedelta(days=1), 'EXPIRED')]:
                session.add(UserFact(scope=scope, scope_id=gid, status=status, subject_user_id=subject,
                                     expires_at=expiry, fact_text=text, fingerprint=text, evidence_excerpt='SECRET_EXCERPT'))
            session.add(MemoryOptout(user_id=8))
            session.add(UserFact(scope='group', scope_id=GROUP_A, fact_text='<script>hostile</script>',
                                 fingerprint='hostile', source_message_id=55, confidence=999, category='bad', source_kind='bad'))
            session.add(GroupMessageArchive(group_id=GROUP_A, message_key='u:55', telegram_message_id=55, content='RAW_SOURCE_SECRET'))
            for i in range(23):
                session.add(SearchResultRecord(scope='group', scope_id=GROUP_A, query=f'query-{i}', digest='<b>text</b>', kind='price', outcome='ok', created_at=now-timedelta(hours=3),
                                               sources=[{'title':'<img>title', 'url':'https://example.com/test'}, {'url':'javascript:alert(1)'}, {'url':'https://u:p@example.com/'}]))
            session.add(SearchResultRecord(scope='private', scope_id=GROUP_A, query='PRIVATE_SEARCH'))
            session.add(SearchResultRecord(scope='group', scope_id=GROUP_B, query='FOREIGN_SEARCH'))
            session.add(SearchResultRecord(scope='group', scope_id=GROUP_A, query='RETAINED_TOO_OLD', created_at=now-timedelta(days=400)))
            session.add(GroupSummary(group_id=GROUP_A, summary='SECRET_INVALID_SUMMARY', source_revision=-1, version=2))
            await session.commit()
        # Settings is a live object updated by manager, set synthetic runtime values only in fixture.
        self.settings.bot.search_freshness_price_hours = 1
        self.settings.bot.search_record_retention_days = 7
        self.settings.bot.group_summary_enabled = True

    def snapshot(self):
        with sqlite3.connect(self.db_path) as conn:
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            return [(name, sorted(conn.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr)) for name, in tables]

    async def read(self, section, query='', user=42, group=GROUP_A):
        return await self.client.get(f'/api/v1/groups/{group}/memory-transparency/{section}{query}', headers=self._headers(user))

    async def test_facts_are_visible_set_only_and_readonly(self):
        before = self.snapshot()
        response = await self.read('facts')
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(len(body['items']), 20)
        self.assertIs(body['has_more'], True)
        for secret in ['PRIVATE','FOREIGN','DELETED','SUPERSEDED','OPTOUT','EXPIRED','SECRET_EXCERPT','RAW_SOURCE_SECRET']:
            self.assertNotIn(secret, str(body))
        self.assertEqual(self.snapshot(), before)
        hostile = body['items'][0]
        self.assertEqual(hostile['category'], 'other')
        self.assertEqual(hostile['confidence'], 100)
        self.assertEqual(hostile['source_kind'], 'unknown')
        self.assertEqual(hostile['source_state'], 'verified')

    async def test_cursor_filters_and_empty_no_hidden_count(self):
        response = await self.read('facts', '?limit=2&subject=0')
        self.assertEqual(response.status, 200)
        first = await response.json()
        second = await (await self.read('facts', '?limit=2&subject=0&cursor='+first['next_cursor'])).json()
        self.assertFalse(set(x['id'] for x in first['items']) & set(x['id'] for x in second['items']))
        hidden = await (await self.read('facts','?subject=8')).json()
        self.assertEqual(hidden['items'], [])
        self.assertIs(hidden['has_more'], False)
        self.assertNotIn('total', hidden)

    async def test_search_links_freshness_retention_and_readonly(self):
        before = self.snapshot()
        response = await self.read('searches')
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body['retention_days'], 7)
        self.assertIs(body['has_more'], True)
        for item in body['items']:
            self.assertEqual(item['freshness'], 'stale')
            self.assertEqual(item['freshness_hours'], 1)
            self.assertEqual(len(item['sources']), 1)
        for text in ['PRIVATE_SEARCH','FOREIGN_SEARCH','RETAINED_TOO_OLD','javascript:', 'u:p@']:
            self.assertNotIn(text, str(body))
        self.assertEqual(self.snapshot(), before)

    async def test_search_retention_compares_the_same_absolute_instant(self):
        """同一绝对时刻的 naive 上海写法与带 offset 写法必须同进同出，留存边界不整体偏移 8 小时。"""
        fixed = now_shanghai_naive().replace(microsecond=0)
        inside = fixed - timedelta(days=7) + timedelta(hours=1)
        boundary = fixed - timedelta(days=7)
        outside = fixed - timedelta(days=7) - timedelta(hours=1)

        def as_utc(wall_clock):
            return (wall_clock - timedelta(hours=8)).replace(tzinfo=timezone.utc)

        stored = {
            'rw-naive-within': inside.isoformat(),
            'rw-utc-within': as_utc(inside).isoformat(),
            'rw-offset-within': as_utc(inside).astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat(),
            'rw-z-within': as_utc(inside).isoformat().replace('+00:00', 'Z'),
            'rw-naive-boundary': boundary.isoformat(),
            'rw-utc-boundary': as_utc(boundary).isoformat(),
            'rw-naive-outside': outside.isoformat(),
            'rw-utc-outside': as_utc(outside).isoformat(),
            'rw-recent': (fixed - timedelta(minutes=30)).isoformat(),
        }
        ids = {}
        async with self.session_factory() as session:
            for name in stored:
                row = SearchResultRecord(scope='group', scope_id=GROUP_A, query=name, digest='d', kind='price',
                                         outcome='ok', created_at=fixed)
                session.add(row)
                await session.flush()
                ids[name] = row.id
            await session.commit()
        with sqlite3.connect(self.db_path) as conn:
            for name, text in stored.items():
                conn.execute('UPDATE search_result_records SET created_at = ? WHERE id = ?', (text, ids[name]))
            conn.commit()
        before = self.snapshot()
        with patch('bot.services.memory_transparency.now_shanghai_naive', return_value=fixed):
            response = await self.read('searches', '?limit=50')
        self.assertEqual(response.status, 200)
        items = {item['query']: item for item in (await response.json())['items']}
        self.assertEqual(sorted(name for name in stored if name in items),
                         sorted(name for name in stored if 'outside' not in name))
        for name in ('rw-utc-within', 'rw-offset-within', 'rw-z-within'):
            self.assertEqual(items[name]['created_at'], items['rw-naive-within']['created_at'])
            self.assertEqual(items[name]['freshness'], items['rw-naive-within']['freshness'])
        self.assertEqual(items['rw-utc-boundary']['created_at'], items['rw-naive-boundary']['created_at'])
        self.assertEqual(items['rw-recent']['freshness'], 'fresh')
        self.assertEqual(self.snapshot(), before)

    async def test_summary_invalid_disabled_empty_and_valid_consumer(self):
        response = await self.read('summary')
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body['status'], 'invalid')
        self.assertNotIn('SECRET_INVALID_SUMMARY', str(body))
        self.settings.bot.group_summary_enabled = False
        self.assertEqual((await (await self.read('summary')).json())['status'], 'disabled')
        self.settings.bot.group_summary_enabled = True
        self.assertEqual((await (await self.read('summary',group=GROUP_B)).json())['status'], 'empty')
        from bot.services.group_summary import SqlGroupSummaryStore
        store = SqlGroupSummaryStore(self.session_factory)
        rev = await store.content_revision(GROUP_A)
        async with self.session_factory() as session:
            row = await session.get(GroupSummary, GROUP_A)
            archive = await session.scalar(select(GroupMessageArchive).where(GroupMessageArchive.group_id==GROUP_A))
            row.summary = 'valid synthetic summary'
            row.source_revision = rev
            row.covered_from_id = row.covered_through_id = archive.id
            row.covered_count = 1
            await session.commit()
        before = self.snapshot()
        self.assertEqual((await (await self.read('summary')).json())['status'], 'valid')
        self.assertEqual(self.snapshot(), before)
        async with self.session_factory() as session:
            archive = await session.get(GroupMessageArchive, archive.id)
            archive.content = 'edited'
            await session.commit()
        self.assertEqual((await (await self.read('summary')).json())['status'], 'invalid')

    async def test_permission_matrix(self):
        for section in ('facts','searches','summary'):
            with self.subTest(section=section):
                self.assertEqual((await self.read(section,user=99)).status, 200)
                self.assertEqual((await self.read(section,user=777)).status, 403)
                self.assertEqual((await self.read(section,user=99,group=GROUP_B)).status, 403)

    async def test_strict_queries_cannot_expand_scope(self):
        for query in ['?scope=private','?group='+str(GROUP_B),'?user=1','?limit=0','?limit=2abc','?limit=1&limit=2','?cursor=-1','?subject=-1','?category=deleted']:
            with self.subTest(query=query):
                self.assertEqual((await self.read('facts',query)).status, 400)
        self.assertEqual((await self.read('summary','?limit=20')).status, 400)

    async def test_read_failures_explicit_and_recover(self):
        from bot.db.sqlite_session import SQLiteSafeAsyncSession
        original = SQLiteSafeAsyncSession.execute
        async def fault(session, statement, *args, **kwargs):
            if any(name in str(statement) for name in ['user_facts','search_result_records','group_summaries']):
                raise RuntimeError('PRIVATE_SQL_SECRET')
            return await original(session, statement, *args, **kwargs)
        real_get = SQLiteSafeAsyncSession.get
        async def fault_get(session, model, *args, **kwargs):
            if model is GroupSummary: raise RuntimeError('PRIVATE_SQL_SECRET')
            return await real_get(session, model, *args, **kwargs)
        with patch.object(SQLiteSafeAsyncSession,'execute',fault), patch.object(SQLiteSafeAsyncSession,'get',fault_get):
            for section in ('facts','searches','summary'):
                response = await self.read(section)
                self.assertEqual(response.status, 503)
                self.assertNotIn('PRIVATE_SQL_SECRET', await response.text())
        self.assertEqual((await self.read('facts')).status, 200)

    async def test_evidence_fault_is_unverifiable_not_deleted(self):
        from bot.db.sqlite_session import SQLiteSafeAsyncSession
        original = SQLiteSafeAsyncSession.execute
        async def fault(session, statement, *args, **kwargs):
            if 'group_message_archive' in str(statement):
                raise RuntimeError('evidence fault')
            return await original(session, statement, *args, **kwargs)
        with patch.object(SQLiteSafeAsyncSession,'execute',fault):
            response = await self.read('facts','?limit=1')
            self.assertEqual(response.status, 200)
            body = await response.json()
            self.assertEqual(body['items'][0]['source_state'], 'unverifiable')
            self.assertNotIn('RAW_SOURCE_SECRET',str(body))
        self.assertEqual((await (await self.read('facts','?limit=1')).json())['items'][0]['source_state'], 'verified')

    async def test_moderation_missing_and_probe_fault_suppress_links(self):
        from bot.db.models import Violation
        from bot.db.sqlite_session import SQLiteSafeAsyncSession
        async with self.session_factory() as session:
            session.add(Violation(group_id=GROUP_A,user_id=1,source_message_id=55,action_taken='delete'))
            await session.commit()
        before=self.snapshot()
        item=(await (await self.read('facts','?limit=1')).json())['items'][0]
        self.assertEqual(item['source_state'],'unavailable'); self.assertIsNone(item['source_url'])
        self.assertEqual(before,self.snapshot())
        original=SQLiteSafeAsyncSession.execute
        async def fault(session,statement,*args,**kwargs):
            if 'violations' in str(statement): raise RuntimeError('secret evidence')
            return await original(session,statement,*args,**kwargs)
        with patch.object(SQLiteSafeAsyncSession,'execute',fault):
            item=(await (await self.read('facts','?limit=1')).json())['items'][0]
            self.assertEqual(item['source_state'],'unverifiable'); self.assertIsNone(item['source_url'])
        async with self.session_factory() as session:
            archive=await session.scalar(select(GroupMessageArchive).where(GroupMessageArchive.group_id==GROUP_A))
            await session.delete(archive); await session.commit()
        self.assertIsNone((await (await self.read('facts','?limit=1')).json())['items'][0]['source_url'])

    async def test_unknown_search_metadata_and_runtime_change(self):
        now=now_shanghai_naive()
        async with self.session_factory() as session:
            session.add(SearchResultRecord(scope='group',scope_id=GROUP_A,query='unknown',kind='alien',outcome='bad',sources={'url':'javascript:x'}))
            session.add(SearchResultRecord(scope='group',scope_id=GROUP_A,query='bad-time',kind='price',created_at=now))
            session.add(SearchResultRecord(scope='group',scope_id=GROUP_A,query='future',kind='news',created_at=now+timedelta(days=1)))
            await session.commit()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE search_result_records SET created_at='not-a-time' WHERE query='bad-time'");conn.commit()
        before=self.snapshot()
        body=await (await self.read('searches')).json()
        for item in body['items'][:3]: self.assertEqual(item['freshness'],'unknown')
        unknown=next(i for i in body['items'] if i['query']=='unknown')
        self.assertEqual(unknown['outcome'],'unknown');self.assertEqual(unknown['sources'],[])
        self.assertEqual((await (await self.read('searches','?kind=unknown')).json())['items'][0]['query'],'unknown')
        self.settings.bot.search_freshness_price_hours=8
        body=await (await self.read('searches','?kind=price')).json()
        self.assertTrue(any(i['freshness']=='fresh' and i['freshness_hours']==8 for i in body['items']))
        self.assertEqual(before,self.snapshot())

    async def test_cursor_other_group_and_private_never_expand_scope(self):
        async with self.session_factory() as session:
            private_id=await session.scalar(select(UserFact.id).where(UserFact.scope=='private'))
            foreign_id=await session.scalar(select(UserFact.id).where(UserFact.scope_id==GROUP_B))
        for cursor in (private_id,foreign_id):
            body=await (await self.read('facts',f'?cursor={cursor}')).json()
            self.assertNotIn('PRIVATE',str(body));self.assertNotIn('FOREIGN',str(body))
        # Preserve existing super-admin unrestricted group management: unknown group is empty, never private.
        response=await self.read('facts',group=-100111222333)
        self.assertEqual(response.status,200)
        self.assertEqual((await response.json())['items'],[])
        self.assertEqual((await self.read('facts',group=-100111222333,user=99)).status,403)

    async def test_all_reads_no_dml_no_generation_or_supplier_calls(self):
        from bot.db.sqlite_session import SQLiteSafeAsyncSession
        from bot.services.llm import LLMService
        original=SQLiteSafeAsyncSession.execute
        sql=[]
        async def trace(session,statement,*args,**kwargs):
            sql.append(str(statement))
            self.assertFalse(getattr(statement,'is_dml',False),'C2 must never issue DML')
            return await original(session,statement,*args,**kwargs)
        before=self.snapshot()
        # Calls to configured supplier compression are a hard failure, not a mock response.
        with patch.object(SQLiteSafeAsyncSession,'execute',trace), patch.object(LLMService,'compress',side_effect=AssertionError('supplier forbidden')) as supplier:
            for section in ('facts','searches','summary'):
                self.assertEqual((await self.read(section)).status,200)
            supplier.assert_not_called()
        self.assertEqual(before,self.snapshot())
        fact_sql=[q for q in sql if 'FROM user_facts' in q]
        self.assertTrue(fact_sql)
        self.assertTrue(all('evidence_excerpt' not in q.split('FROM')[0] for q in fact_sql))
        archive_sql=[q for q in sql if 'FROM group_message_archive' in q]
        self.assertTrue(all('raw_text' not in q and 'group_message_archive.content' not in q for q in archive_sql))

    async def test_summary_coverage_failure_is_failure_not_empty(self):
        from bot.services.group_summary import SqlGroupSummaryStore
        async with self.session_factory() as session:
            summary=await session.get(GroupSummary,GROUP_A)
            summary.source_revision=await SqlGroupSummaryStore(self.session_factory).content_revision(GROUP_A)
            summary.covered_count=0
            await session.commit()
        before=self.snapshot()
        with patch.object(SqlGroupSummaryStore,'coverage_intact',side_effect=RuntimeError('secret coverage')):
            response=await self.read('summary');self.assertEqual(response.status,503)
            self.assertNotIn('secret coverage',await response.text())
        self.assertEqual(before,self.snapshot())
        self.assertEqual((await self.read('summary')).status,200)

    async def test_strict_group_paths_and_readonly_methods(self):
        for group in ('0','+1','01','-0','-01',' 1','1.0','99999999999999999999','9223372036854775808'):
            with self.subTest(group=group):
                self.assertEqual((await self.read('facts',group=group)).status,400)
        for section in ('facts','searches','summary'):
            for method in ('post','patch','delete','put'):
                self.assertEqual((await getattr(self.client,method)(f'/api/v1/groups/{GROUP_A}/memory-transparency/{section}',headers=self._headers(42))).status,405)

    async def test_search_visible_pagination_empty_outcome_and_scope(self):
        async with self.session_factory() as session:
            session.add(SearchResultRecord(scope='group',scope_id=GROUP_A,query='empty-positive',kind='fact',outcome='empty',digest='no results'))
            await session.commit()
        seen=[];cursor=None
        for _ in range(20):
            query='?limit=3'+('&cursor='+cursor if cursor else '')
            body=await (await self.read('searches',query)).json()
            self.assertNotIn('PRIVATE_SEARCH',str(body));self.assertNotIn('FOREIGN_SEARCH',str(body))
            seen.extend(i['id'] for i in body['items'])
            if not body['has_more']: break
            cursor=body['next_cursor']
        self.assertEqual(len(seen),24);self.assertEqual(len(seen),len(set(seen)))
        body=await (await self.read('searches','?kind=fact')).json()
        self.assertEqual(body['items'][0]['outcome'],'empty');self.assertFalse(body['has_more'])
        self.settings.bot.search_record_retention_days=1
        self.assertEqual((await (await self.read('searches')).json())['retention_days'],1)

    async def test_category_normalization_and_source_thread_topics_link(self):
        async with self.session_factory() as session:
            row=await session.scalar(select(UserFact).where(UserFact.fingerprint=='hostile'))
            row.category=' IDENTITY '
            archive=await session.scalar(select(GroupMessageArchive).where(GroupMessageArchive.group_id==GROUP_A))
            archive.message_thread_id=7
            await session.commit()
        body=await (await self.read('facts','?category=identity')).json()
        self.assertEqual(len(body['items']),1);self.assertEqual(body['items'][0]['category'],'identity')
        # C3：可信归档线程进入统一构造器，Topics 深链按官方 path 形状给出；
        # 线程只来自那一条归档行（不是正文/模型里的猜测）。
        self.assertEqual(body['items'][0]['source_url'], f'https://t.me/c/{str(GROUP_A)[4:]}/7/55')
        self.assertEqual(body['items'][0]['revision'], 1)
        self.assertIs(body['items'][0]['corrected'], False)
