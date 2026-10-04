import unittest
from unittest.mock import AsyncMock, patch
from bot.config import ModelConfig
from bot.services import model_limits as ml
from bot.services.llm import LLMService

class OutputReservationSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ml.reset_model_limits_for_tests()
        self.addCleanup(ml.reset_model_limits_for_tests)

    def test_output_larger_than_business_budget_has_no_input(self):
        limits = ml.ModelLimits(model='probe', source=ml.LIMIT_SOURCE_GATEWAY, total_window=1000000)
        self.assertEqual(limits.business_input_budget(business_tokens=100000, reserve_tokens=20000, output_reserve=120000), 0)

    def test_reservation_larger_than_model_has_no_input(self):
        limits = ml.ModelLimits(model='probe', source=ml.LIMIT_SOURCE_GATEWAY, total_window=5000)
        self.assertEqual(limits.business_input_budget(business_tokens=278528, reserve_tokens=32768, output_reserve=6000), 0)

    def test_explicit_input_and_total_are_both_respected(self):
        limits = ml.ModelLimits(model='probe', source=ml.LIMIT_SOURCE_GATEWAY, total_window=5000, max_input_tokens=4000)
        self.assertEqual(limits.business_input_budget(business_tokens=278528, reserve_tokens=1024, output_reserve=3000), 2000)

    def test_input_only_metadata_does_not_double_subtract(self):
        limits = ml.ModelLimits(model='probe', source=ml.LIMIT_SOURCE_GATEWAY, max_input_tokens=4000)
        self.assertEqual(limits.business_input_budget(business_tokens=278528, reserve_tokens=32768, output_reserve=3000), 4000)

    async def test_zero_input_budget_does_not_disable_http_gate(self):
        config = ModelConfig(model='openai/probe', api_key='test-placeholder', api_base='https://example.invalid/v1', max_tokens=120000)
        ml.MODEL_LIMITS.record(config, total_window=1000000)
        llm = LLMService(config, config, business_context_tokens=100000, context_reserve_tokens=20000)
        mock = AsyncMock()
        with patch('bot.services.llm.litellm.acompletion', mock):
            result = await llm._chat_with_fallbacks(messages=[{'role':'user','content':'hello'}], candidates=llm._chat_candidates(config), label='skill', preview_limit=10)
        self.assertEqual(result, '')
        mock.assert_not_awaited()
