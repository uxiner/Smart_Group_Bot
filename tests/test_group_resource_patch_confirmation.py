"""B10 real init_db / signed HTTP and actual route-body cancellation identity."""
from __future__ import annotations

import asyncio
import inspect
import sqlite3
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp.test_utils import make_mocked_request

from bot.db.models import ModerationRule, KeywordReply, ScheduledMessage
from bot.db.sqlite_session import SQLiteSafeAsyncSession as Session
from bot.web.settings_api import _rule_document, _keyword_reply_document, _scheduled_message_document
from test_permanent_memory_create_dedup import _RaceInjectedTestCase, GROUP_A, GROUP_B, SUPER_ADMIN

RESOURCES = {
    'rules': (ModerationRule, 'rule', 'pattern', _rule_document, 'rule_not_found', '群规不存在。'),
    'keyword-replies': (KeywordReply, 'keyword_reply', 'reply_text', _keyword_reply_document, 'keyword_reply_not_found', '关键词回复不存在。'),
    'scheduled-messages': (ScheduledMessage, 'scheduled_message', 'text', _scheduled_message_document, 'scheduled_message_not_found', '定时消息不存在。'),
}


class GroupResourcePatchTests(_RaceInjectedTestCase):
    async def seed(self, kind, group_id=GROUP_A):
        model = RESOURCES[kind][0]
        values = {'group_id': group_id}
        if kind == 'rules':
            values.update(rule_type='keyword', pattern='old', action='warn')
        elif kind == 'keyword-replies':
            values.update(keyword='menu', match_type='exact', reply_text='old', created_by=42)
        else:
            values.update(text='old', schedule_type='daily', schedule_time='09:00',
                          last_run_at=datetime(2020, 1, 1), last_message_id=55, created_by=42)
        async with self.session_factory() as session:
            row = model(**values)
            session.add(row)
            await session.commit()
            return row.id

    async def http(self, kind, row_id, body):
        return await self.client.patch(f'/api/v1/groups/{GROUP_A}/{kind}/{row_id}',
                                      headers=self._headers(), json=body)

    async def direct(self, kind, row_id, body):
        route = next(r for r in self.client.server.app.router.routes()
                     if r.method == 'PATCH' and r.resource.canonical.startswith('/api/v1/groups/{id}/'+kind+'/'))
        request = make_mocked_request('PATCH', f'/api/v1/groups/{GROUP_A}/{kind}/{row_id}')
        request._match_info['id'] = str(GROUP_A)
        request._match_info['rule_id' if kind == 'rules' else 'entry_id'] = str(row_id)
        async def json_body():
            return body
        request.json = json_body
        return await inspect.unwrap(route.handler)(request, SimpleNamespace(id=SUPER_ADMIN))

    async def fresh(self, kind, row_id):
        model, _, _, document, _, _ = RESOURCES[kind]
        async with self.session_factory() as session:
            row = await session.get(model, row_id)
            return document(row) if row is not None else None

    async def run_normal(self, kind, same=False):
        row_id = await self.seed(kind)
        _, key, field, _, _, _ = RESOURCES[kind]
        response = await self.http(kind, row_id, {field: 'old' if same else 'new'})
        body = await response.json()
        self.assertEqual(response.status, 200)
        self.assertIs(body.get('confirmed'), True)
        self.assertEqual(body.get('status'), 'updated')
        self.assertEqual(body[key], await self.fresh(kind, row_id))
        self.assertEqual(body[key][field], 'old' if same else 'new')
        await self._assert_write_lock_available()

    async def run_missing(self, kind, cross=False):
        row_id = await self.seed(kind, GROUP_B) if cross else 999999
        _, _, field, _, code, message = RESOURCES[kind]
        response = await self.http(kind, row_id, {field: 'new'})
        self.assertEqual(response.status, 404)
        self.assertEqual((await response.json())['error'], {'code': code, 'message': message})
        if cross:
            self.assertEqual((await self.fresh(kind, row_id))[field], 'old')

    async def run_fault(self, kind, mode):
        row_id = await self.seed(kind)
        model, key, field, _, _, _ = RESOURCES[kind]
        real = {name: getattr(Session, name) for name in ['get', 'execute', 'commit', 'rollback', 'close', 'refresh', 'flush']}
        original = asyncio.CancelledError('private-original')
        cleanup_cancel = asyncio.CancelledError('private-cleanup')
        state = {'target': None, 'commits': 0, 'rollbacks': 0, 'closes': 0, 'updates': []}
        overrides = {'count_none': None, 'count_bool': True, 'count_float': 1.0, 'count_negative': -1, 'count_two': 2, 'count_string': '1'}

        async def get(session, entity, ident, *args, **kwargs):
            result = await real['get'](session, entity, ident, *args, **kwargs)
            if entity is model and ident == row_id and state['target'] is None:
                state['target'] = session
                if mode == 'delete_race':
                    with sqlite3.connect(self.db_path, timeout=1) as connection:
                        connection.execute(f'DELETE FROM {model.__tablename__} WHERE id = ?', (row_id,))
                        connection.commit()
            return result

        async def execute(session, statement, *args, **kwargs):
            if session is state['target'] and getattr(statement, 'is_update', False):
                state['updates'].append(str(statement))
                if mode == 'execute_error':
                    raise RuntimeError('private-SQL-body-secret')
            result = await real['execute'](session, statement, *args, **kwargs)
            if session is state['target'] and getattr(statement, 'is_update', False) and mode in overrides:
                return SimpleNamespace(rowcount=overrides[mode])
            return result

        async def commit(session):
            if session is state['target']:
                state['commits'] += 1
                if mode in {'commit_before', 'rollback_close_error', 'original_cancel', 'cleanup_cancel'}:
                    await session.flush()  # real write before fault, including baseline ORM
                    if mode == 'original_cancel':
                        raise original
                    raise RuntimeError('private-SQL-body-secret')
                if mode == 'commit_after':
                    await real['commit'](session)
                    raise RuntimeError('private-after-ack')
            return await real['commit'](session)

        async def flush(session, *args, **kwargs):
            result = await real['flush'](session, *args, **kwargs)
            if session is state['target'] and mode == 'flush_error':
                raise RuntimeError('private-flush')
            return result

        async def refresh(session, row, *args, **kwargs):
            if session is state['target'] and mode == 'refresh_error':
                raise RuntimeError('private-refresh')
            return await real['refresh'](session, row, *args, **kwargs)

        async def rollback(session):
            if session is state['target']:
                state['rollbacks'] += 1
                if mode in {'rollback_close_error', 'original_cancel', 'cleanup_cancel', 'all_cleanup_failed'}:
                    raise RuntimeError('private-rollback')
            return await real['rollback'](session)

        async def close(session):
            if session is state['target']:
                state['closes'] += 1
                if mode == 'all_cleanup_failed':
                    raise RuntimeError('private-close')
                if state['closes'] == 1 and mode in {'original_cancel', 'cleanup_cancel', 'close_cancel'}:
                    raise cleanup_cancel
                if state['closes'] == 1 and mode in {'rollback_close_error', 'close_error'}:
                    raise RuntimeError('private-close')
            return await real['close'](session)

        try:
            with patch.object(Session, 'get', get), patch.object(Session, 'execute', execute), \
                 patch.object(Session, 'commit', commit), patch.object(Session, 'refresh', refresh), patch.object(Session, 'flush', flush), \
                 patch.object(Session, 'rollback', rollback), patch.object(Session, 'close', close):
                if mode in {'original_cancel', 'cleanup_cancel', 'close_cancel'}:
                    caught = None
                    try:
                        await self.direct(kind, row_id, {field: 'new'})
                    except BaseException as exc:
                        caught = exc
                    self.assertIs(caught, original if mode == 'original_cancel' else cleanup_cancel)
                else:
                    response = await self.http(kind, row_id, {field: 'new'})
                    body = await response.json()
                    expected = 409 if mode == 'delete_race' else 500 if mode == 'all_cleanup_failed' else 503
                    if mode == 'close_error':
                        expected = 200
                    self.assertEqual(response.status, expected)
                    self.assertIs(body.get('ok'), mode == 'close_error')
                    if mode == 'close_error':
                        self.assertIs(body.get('confirmed'), True)
                        self.assertEqual(body.get('status'), 'updated')
                    self.assertNotIn('private-', str(body))
                    self.assertNotIn('UPDATE ', str(body))
                self.assertIsNotNone(state['target'])
                self.assertLessEqual(state['commits'], 1, 'no retry/compensating commit')
                if mode == 'all_cleanup_failed':
                    self.assertEqual(state['closes'], 2)
                else:
                    self.assertFalse(state['target'].in_transaction(), 'entry must release before teardown')
                    await self._assert_write_lock_available()
                if mode in {'rollback_close_error', 'original_cancel', 'cleanup_cancel'}:
                    self.assertEqual(state['rollbacks'], 2)
                    self.assertEqual(state['closes'], 2)
            fresh = await self.fresh(kind, row_id)
            if mode == 'delete_race':
                self.assertIsNone(fresh)
            elif mode in {'commit_after', 'close_error', 'close_cancel'}:
                self.assertEqual(fresh[field], 'new', 'after real commit may have taken effect')
            elif mode != 'all_cleanup_failed':
                self.assertEqual(fresh[field], 'old', 'before commit rollback preserves old value')
            if state['updates']:
                for sql in state['updates']:
                    where = sql.split('WHERE', 1)[1]
                    self.assertIn('group_id', where)
                    self.assertIn('.id', where)
        finally:
            if state['target'] is not None:
                await real['close'](state['target'])

    async def test_schedule_reenable_anchors_now_but_enabled_edit_does_not_delay(self):
        row_id = await self.seed('scheduled-messages')
        old = (await self.fresh('scheduled-messages', row_id))['last_run_at']
        response = await self.http('scheduled-messages', row_id, {'text': 'changed', 'enabled': True})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['scheduled_message']['last_run_at'], old)
        await self.http('scheduled-messages', row_id, {'enabled': False})
        response = await self.http('scheduled-messages', row_id, {'enabled': True})
        self.assertEqual(response.status, 200)
        anchored = datetime.fromisoformat((await response.json())['scheduled_message']['last_run_at'])
        # Bot uses Shanghai naive time; compare to its actual clock function.
        from bot.utils.timezone import now_shanghai_naive
        self.assertLess(abs((now_shanghai_naive() - anchored).total_seconds()), 5)
        self.assertNotEqual(anchored.isoformat(), old)
        async with self.session_factory() as session:
            row = await session.get(ScheduledMessage, row_id)
            self.assertEqual(row.last_message_id, 55)

    async def test_validation_is_before_destructive_update(self):
        for kind in RESOURCES:
            with self.subTest(kind=kind):
                row_id = await self.seed(kind)
                field = RESOURCES[kind][2]
                response = await self.http(kind, row_id, {field: '  '})
                self.assertEqual(response.status, 400)
                self.assertEqual((await self.fresh(kind, row_id))[field], 'old')


for _kind in RESOURCES:
    for _case in ['normal', 'same', 'missing', 'cross', 'delete_race', 'count_none', 'count_bool',
                  'count_float', 'count_negative', 'count_two', 'execute_error', 'refresh_error',
                  'commit_before', 'commit_after', 'rollback_close_error', 'original_cancel',
                  'cleanup_cancel', 'all_cleanup_failed', 'flush_error', 'count_string', 'close_error', 'close_cancel']:
        def build(kind, case):
            async def test(self):
                if case in {'normal', 'same'}:
                    await self.run_normal(kind, case == 'same')
                elif case in {'missing', 'cross'}:
                    await self.run_missing(kind, case == 'cross')
                else:
                    await self.run_fault(kind, case)
            return test
        setattr(GroupResourcePatchTests, 'test_'+_kind.replace('-', '_')+'_'+_case, build(_kind, _case))
