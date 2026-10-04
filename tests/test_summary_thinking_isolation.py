from copy import deepcopy
from unittest.mock import AsyncMock, patch
import unittest
from bot.config import BotConfig
from bot.services.llm import LLMService

class SummaryThinkingIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_override_leaves_shared_routes_and_other_labels_unchanged(self):
        model = BotConfig().main_model.model_copy(update={
            'request_params': {'thinking': {'type': 'enabled'}, 'reasoning_effort': 'high', 'temperature_hint': 0.3}
        })
        llm = LLMService(model, model, compress=model, skill=model, moderation=model)
        before = deepcopy(model.model_dump())
        captured = []
        async def capture(**kwargs):
            captured.append(kwargs)
            return 'valid result'
        with patch.object(llm, '_chat_with_fallbacks', side_effect=capture):
            await llm.background_summary_completion([{'role': 'user', 'content': 'source'}], max_tokens=4096)
            await llm.background_summary_completion([{'role': 'user', 'content': 'source'}], label='style_distill')
            await llm.compress('system', 'source')
            await llm.generate('system', 'source')
            await llm.moderation('system', 'source')
        params = captured[0]['candidates'][0].request_params
        assert params['thinking'] == {'type': 'disabled'}
        assert 'reasoning_effort' not in params
        assert params['temperature_hint'] == 0.3
        assert captured[0]['candidates'][0].max_tokens == 4096
        for call in captured[1:]:
            assert call['candidates'][0].request_params == before['request_params']
        for route in (llm.main, llm.skill_config, llm.decision_config, llm.moderation_config, llm.compress_config):
            assert route.model_dump() == before

    async def test_summary_override_copies_each_candidate_without_mutating_fallback(self):
        llm = LLMService(BotConfig().main_model, BotConfig().decision_model)
        candidate = llm._chat_candidates(llm.compress_config)[0]
        fallback = candidate.model_copy(update={'model': 'openai/fallback', 'request_params': {'thinking': {'type': 'enabled'}, 'reasoning_effort': 'high'}})
        before = deepcopy(fallback.model_dump())
        capture = AsyncMock(return_value='valid result')
        with patch.object(llm, '_chat_candidates', return_value=[candidate, fallback]), patch.object(llm, '_chat_with_fallbacks', capture):
            await llm.background_summary_completion([{'role': 'user', 'content': 'source'}])
        candidates = capture.await_args.kwargs['candidates']
        assert len(candidates) == 2
        assert all(x.request_params['thinking'] == {'type': 'disabled'} for x in candidates)
        assert all('reasoning_effort' not in x.request_params for x in candidates)
        assert fallback.model_dump() == before
