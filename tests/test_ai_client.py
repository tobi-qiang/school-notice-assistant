"""AIClient 单元测试 - Fake transport，严禁联网。"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.ai_client import (
    AIClient, AIError, AuthenticationError, QuotaError, RateLimitError,
    ModelNotFoundError, NetworkAIError, InvalidAIResponse,
    filter_sensitive_text, normalize_chat_url, normalize_embedding_url, normalize_rerank_url
)
from app.config_store import AIConfig


class TestFilterSensitiveText(unittest.TestCase):
    """测试敏感信息过滤。"""
    
    def test_student_id_8_digits(self):
        self.assertEqual(filter_sensitive_text('学号 12345678'), '学号 [已隐藏]')
    
    def test_student_id_10_digits(self):
        self.assertEqual(filter_sensitive_text('学号 1234567890'), '学号 [已隐藏]')
    
    def test_id_card_18_digits(self):
        self.assertEqual(filter_sensitive_text('身份证 123456789012345678'), '身份证 [已隐藏]')
    
    def test_mixed_content(self):
        result = filter_sensitive_text('学生 12345678 身份证 123456789012345678')
        self.assertEqual(result, '学生 [已隐藏] 身份证 [已隐藏]')
    
    def test_no_sensitive(self):
        self.assertEqual(filter_sensitive_text('正常文本'), '正常文本')
    
    def test_empty(self):
        self.assertEqual(filter_sensitive_text(''), '')


class TestFakeTransport(unittest.TestCase):
    """Fake transport 测试。"""
    
    def setUp(self):
        self.config = AIConfig(
            base_url='https://api.example.com/v1',
            model='qwen3.5',
            api_key='test-key',
            embedding_url='https://embed.example.com/v1',
            embedding_model='embed-model',
            embedding_api_key='embed-key',
            rerank_url='https://rerank.example.com/v1',
            rerank_model='rerank-model',
            rerank_api_key='rerank-key',
            summary_enabled=True,
            summary_url='https://summary.example.com/v1',
            summary_model='small-model',
            summary_api_key='summary-key',
        )
        self.mock_session = Mock()
        self.client = AIClient(self.config, session=self.mock_session)
    
    def test_chat_fake_response(self):
        """Fake 聊天响应。"""
        fake_response = Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            'choices': [{'message': {'content': '{"short_title": "测试", "summary": "摘要", "audience": "学生", "deadline": null, "action": "无", "start_time": null, "materials": [], "method_or_location": "线上", "contact": "无", "requires_external_system": false, "uncertainty": "无", "tags": []}'}}]
        }
        self.mock_session.request.return_value = fake_response
        
        result = self.client.enrich('测试标题', '测试正文')
        self.assertEqual(result['short_title'], '测试')
        self.assertEqual(result['summary'], '摘要')

    def test_share_summary_uses_one_small_twenty_second_json_request(self):
        fake_response = Mock(status_code=200)
        fake_response.json.return_value = {
            'choices': [{'message': {'content': '{"summary":"本科生创新项目开始申报"}'}}]
        }
        self.mock_session.request.return_value = fake_response

        assert self.client.summarize_share('项目通知', '正文内容') == '本科生创新项目开始申报'
        request = self.mock_session.request.call_args
        assert request.kwargs['timeout'] == 20
        assert request.kwargs['json']['max_completion_tokens'] == 256
        assert request.kwargs['json']['response_format'] == {'type': 'json_object'}
        assert len(request.kwargs['json']['messages']) == 1
        assert request.args[1] == 'https://summary.example.com/v1/chat/completions'
        assert request.kwargs['json']['model'] == 'small-model'
        assert request.kwargs['headers']['Authorization'] == 'Bearer summary-key'
        prompt = request.kwargs['json']['messages'][0]['content']
        assert '禁止出现月份、具体日期' in prompt
        assert '项目正式名称时保留' in prompt
        assert '宁可稍长，也不能截断' in prompt

    def test_share_summary_accepts_plain_text_without_retry(self):
        fake_response = Mock(status_code=200)
        fake_response.json.return_value = {
            'choices': [{'message': {'content': '本科生第一学期选课安排'}}]
        }
        self.mock_session.request.return_value = fake_response
        assert self.client.summarize_share('标题', '正文') == '本科生第一学期选课安排'
        assert self.mock_session.request.call_count == 1

    def test_share_summary_disables_minimax_m3_thinking(self):
        self.config.summary_url = 'https://api.minimaxi.com'
        self.config.summary_model = 'MiniMax-M3'
        fake_response = Mock(status_code=200)
        fake_response.json.return_value = {
            'choices': [{'message': {'content': '{"summary":"本科生创新项目开始申报"}'}}]
        }
        self.mock_session.request.return_value = fake_response

        assert self.client.summarize_share('标题', '正文') == '本科生创新项目开始申报'
        payload = self.mock_session.request.call_args.kwargs['json']
        assert payload['thinking'] == {'type': 'disabled'}
        assert 'reasoning_split' not in payload
        assert 'response_format' not in payload
        assert payload['max_completion_tokens'] == 256

    def test_summary_connection_disables_minimax_m3_thinking(self):
        self.config.summary_url = 'https://api.minimaxi.com'
        self.config.summary_model = 'MiniMax-M3'
        self.mock_session.request.return_value = Mock(status_code=200)

        assert self.client.test_connection('summary') == 'OK'
        payload = self.mock_session.request.call_args.kwargs['json']
        assert payload['thinking'] == {'type': 'disabled'}

    def test_chat_connection_disables_minimax_m3_thinking(self):
        self.config.base_url = 'https://api.minimaxi.com'
        self.config.model = 'MiniMax-M3'
        self.mock_session.request.return_value = Mock(status_code=200)

        assert self.client.test_connection('chat') == 'OK'
        payload = self.mock_session.request.call_args.kwargs['json']
        assert payload['thinking'] == {'type': 'disabled'}
        assert payload['max_completion_tokens'] == 16

    def test_share_summary_rejects_truncated_thinking_only(self):
        fake_response = Mock(status_code=200)
        fake_response.json.return_value = {
            'choices': [{'message': {'content': '<think>还在分析，输出额度已经结束'}}]
        }
        self.mock_session.request.return_value = fake_response

        with self.assertRaisesRegex(InvalidAIResponse, '思考过程'):
            self.client.summarize_share('标题', '正文')
    
    def test_embedding_fake_response(self):
        """Fake 嵌入响应。"""
        self.config.embedding_enabled = True
        fake_response = Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            'data': [{'embedding': [0.1, 0.2, 0.3]}]
        }
        self.mock_session.request.return_value = fake_response
        
        result = self.client.embedding('测试文本')
        self.assertEqual(result, [0.1, 0.2, 0.3])
    
    def test_rerank_fake_response_gte(self):
        """Fake 重排序响应 (gte-rerank-v2)。"""
        self.config.rerank_enabled = True
        self.config.rerank_model = 'gte-rerank-v2'
        fake_response = Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            'results': [
                {'relevance_score': 0.3},
                {'relevance_score': 0.9},
                {'relevance_score': 0.5}
            ]
        }
        self.mock_session.request.return_value = fake_response
        
        result = self.client.rerank('查询', ['doc1', 'doc2', 'doc3'])
        self.assertEqual(result, [1, 2, 0])
    
    def test_rerank_fake_response_qwen(self):
        """Fake 重排序响应 (qwen3-rerank)。"""
        self.config.rerank_enabled = True
        self.config.rerank_model = 'qwen3-rerank'
        fake_response = Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            'data': [
                {'index': 0, 'relevance_score': 0.3},
                {'index': 1, 'relevance_score': 0.9},
                {'index': 2, 'relevance_score': 0.5}
            ]
        }
        self.mock_session.request.return_value = fake_response
        
        result = self.client.rerank('查询', ['doc1', 'doc2', 'doc3'])
        self.assertEqual(result, [1, 2, 0])

    def test_each_capability_uses_its_own_key(self):
        fake_response = Mock(status_code=200)
        fake_response.json.return_value = {'data': [{'embedding': [1.0]}]}
        self.mock_session.request.return_value = fake_response
        self.config.embedding_enabled = True
        self.client.embedding('text')
        headers = self.mock_session.request.call_args.kwargs['headers']
        self.assertEqual(headers['Authorization'], 'Bearer embed-key')
        self.assertNotIn('test-key', headers['Authorization'])

    def test_query_expansion_json_object(self):
        fake_response = Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            'choices': [{'message': {'content': '{"queries":["大学生创新创业训练计划","创新训练"]}'}}]
        }
        self.mock_session.request.return_value = fake_response
        self.assertEqual(
            self.client.expand_query('大创'),
            ['大创', '大学生创新创业训练计划', '创新训练'],
        )

    def test_minimax_m3_enrichment_disables_thinking_and_parses_fenced_json(self):
        self.config.base_url = 'https://api.minimaxi.com'
        self.config.model = 'MiniMax-M3'
        fake_response = Mock(status_code=200)
        fake_response.json.return_value = {
            'choices': [{
                'finish_reason': 'stop',
                'message': {'content': (
                    '```json\n'
                    '{"short_title":"大创申报","summary":"本科生创新项目申报",'
                    '"audience":"本科生","materials":["申请表"],'
                    '"tags":["创新创业"]}\n```'
                )},
            }]
        }
        self.mock_session.request.return_value = fake_response

        result = self.client.enrich('项目通知', '正文')
        assert result['short_title'] == '大创申报'
        assert result['materials'] == ['申请表']
        payload = self.mock_session.request.call_args.kwargs['json']
        assert payload['thinking'] == {'type': 'disabled'}
        assert payload['max_completion_tokens'] == 2048
        assert 'reasoning_split' not in payload
        assert 'response_format' not in payload

    def test_minimax_m3_query_expansion_disables_thinking(self):
        self.config.base_url = 'https://api.minimaxi.com'
        self.config.model = 'MiniMax-M3'
        fake_response = Mock(status_code=200)
        fake_response.json.return_value = {
            'choices': [{
                'finish_reason': 'stop',
                'message': {'content': '```json\n{"queries":["创新训练","大创项目"]}\n```'},
            }]
        }
        self.mock_session.request.return_value = fake_response

        assert self.client.expand_query('大创') == ['大创', '创新训练', '大创项目']
        payload = self.mock_session.request.call_args.kwargs['json']
        assert payload['thinking'] == {'type': 'disabled'}
        assert payload['max_completion_tokens'] == 256

    def test_grounded_answer_uses_only_supplied_documents(self):
        fake_response = Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            'choices': [{'message': {'content': '根据公告，截止日期为六月。'}}]
        }
        self.mock_session.request.return_value = fake_response
        result = self.client.answer('什么时候截止？', ['标题：申报通知\n正文摘录：六月截止'])
        self.assertEqual(result, '根据公告，截止日期为六月。')
        payload = self.mock_session.request.call_args.kwargs['json']
        self.assertIn('不得使用模型记忆', payload['messages'][0]['content'])
        self.assertIn('六月截止', payload['messages'][1]['content'])

    def test_minimax_m3_answer_separates_reasoning_and_returns_only_final_content(self):
        self.config.base_url = 'https://api.minimaxi.com'
        self.config.model = 'MiniMax-M3'
        fake_response = Mock(status_code=200)
        fake_response.json.return_value = {
            'choices': [{
                'finish_reason': 'stop',
                'message': {
                    'reasoning_content': '先核对公告依据。',
                    'reasoning_details': [{'type': 'reasoning.text', 'text': '先核对公告依据。'}],
                    'content': '根据公告，截止日期为六月。',
                },
            }]
        }
        self.mock_session.request.return_value = fake_response

        result = self.client.answer('什么时候截止？', ['标题：申报通知\n正文摘录：六月截止'])
        assert result == '根据公告，截止日期为六月。'
        assert '先核对' not in result
        payload = self.mock_session.request.call_args.kwargs['json']
        assert payload['thinking'] == {'type': 'adaptive'}
        assert payload['reasoning_split'] is True
        assert payload['max_completion_tokens'] == 4096

    def test_token_truncated_answer_is_rejected(self):
        fake_response = Mock(status_code=200)
        fake_response.json.return_value = {
            'choices': [{
                'finish_reason': 'length',
                'message': {'content': '半截回答'},
            }]
        }
        self.mock_session.request.return_value = fake_response

        with self.assertRaisesRegex(InvalidAIResponse, 'token 上限'):
            self.client.answer('什么时候截止？', ['公告依据'])


class TestErrorClassification(unittest.TestCase):
    """测试错误分类。"""
    
    def setUp(self):
        self.config = AIConfig(
            base_url='https://api.example.com/v1',
            model='qwen3.5',
            api_key='test-key'
        )
        self.mock_session = Mock()
        self.client = AIClient(self.config, session=self.mock_session)
    
    def test_401_authentication_error(self):
        fake_response = Mock()
        fake_response.status_code = 401
        fake_response.text = 'Unauthorized'
        self.mock_session.request.return_value = fake_response
        
        with self.assertRaises(AuthenticationError):
            self.client.enrich('标题', '正文')
    
    def test_403_authentication_error(self):
        fake_response = Mock()
        fake_response.status_code = 403
        fake_response.text = 'Forbidden'
        self.mock_session.request.return_value = fake_response
        
        with self.assertRaises(AuthenticationError):
            self.client.enrich('标题', '正文')
    
    def test_402_quota_error(self):
        fake_response = Mock()
        fake_response.status_code = 402
        fake_response.text = 'Payment Required'
        self.mock_session.request.return_value = fake_response
        
        with self.assertRaises(QuotaError):
            self.client.enrich('标题', '正文')
    
    def test_quota_in_body(self):
        fake_response = Mock()
        fake_response.status_code = 400
        fake_response.text = 'quota exceeded'
        self.mock_session.request.return_value = fake_response
        
        with self.assertRaises(QuotaError):
            self.client.enrich('标题', '正文')
    
    def test_404_model_not_found(self):
        fake_response = Mock()
        fake_response.status_code = 404
        fake_response.text = 'Model not found'
        self.mock_session.request.return_value = fake_response
        
        with self.assertRaises(ModelNotFoundError):
            self.client.enrich('标题', '正文')
    
    def test_429_rate_limit(self):
        fake_response = Mock()
        fake_response.status_code = 429
        fake_response.text = 'Too Many Requests'
        self.mock_session.request.return_value = fake_response
        
        with self.assertRaises(RateLimitError):
            self.client.enrich('标题', '正文')
    
    def test_5xx_server_error(self):
        fake_response = Mock()
        fake_response.status_code = 500
        fake_response.text = 'Internal Server Error'
        self.mock_session.request.return_value = fake_response
        
        with self.assertRaises(AIError):
            self.client.enrich('标题', '正文')


class TestRetryAndCircuitBreaker(unittest.TestCase):
    """测试重试和熔断机制。"""
    
    def setUp(self):
        self.config = AIConfig(
            base_url='https://api.example.com/v1',
            model='qwen3.5',
            api_key='test-key'
        )
        self.mock_session = Mock()
        self.sleep_mock = Mock()
        self.client = AIClient(
            self.config,
            session=self.mock_session,
            sleeper=self.sleep_mock,
            max_retries=2,
            breaker_threshold=3
        )
    
    def test_retry_on_rate_limit(self):
        """限流时重试。"""
        rate_limit_response = Mock()
        rate_limit_response.status_code = 429
        rate_limit_response.text = 'Too Many Requests'
        
        success_response = Mock()
        success_response.status_code = 200
        success_response.json.return_value = {
            'choices': [{'message': {'content': '{"short_title": "OK"}'}}]
        }
        
        self.mock_session.request.side_effect = [rate_limit_response, rate_limit_response, success_response]
        
        result = self.client.enrich('标题', '正文')
        self.assertEqual(result['short_title'], 'OK')
        self.assertEqual(self.sleep_mock.call_count, 2)
    
    def test_circuit_breaker_after_threshold(self):
        """达到阈值后熔断。"""
        error_response = Mock()
        error_response.status_code = 500
        error_response.text = 'Error'
        
        self.mock_session.request.side_effect = error_response
        
        for _ in range(3):
            try:
                self.client.enrich('标题', '正文')
            except AIError:
                pass
        
        with self.assertRaises(AIError):
            self.client.enrich('标题', '正文')
    
    def test_reset_circuit_breaker(self):
        """重置熔断器。"""
        self.client._circuit_open['chat'] = True
        self.client.reset('chat')
        self.assertFalse(self.client._circuit_open.get('chat', False))


class TestUrlNormalization(unittest.TestCase):
    """测试 URL 规范化。"""
    
    def test_chat_url_rules(self):
        self.assertEqual(normalize_chat_url(''), '')
        self.assertEqual(normalize_chat_url('https://api.example.com/v1'), 'https://api.example.com/v1/chat/completions')
        self.assertEqual(normalize_chat_url('https://api.example.com/v1/chat/completions'), 'https://api.example.com/v1/chat/completions')
        self.assertEqual(normalize_chat_url('https://api.example.com'), 'https://api.example.com/v1/chat/completions')
    
    def test_embedding_url_rules(self):
        self.assertEqual(normalize_embedding_url(''), '')
        self.assertEqual(normalize_embedding_url('https://api.example.com/v1/chat/completions'), 'https://api.example.com/v1/embeddings')
        self.assertEqual(normalize_embedding_url('https://api.example.com/v1'), 'https://api.example.com/v1/embeddings')
    
    def test_rerank_url_rules(self):
        self.assertEqual(normalize_rerank_url('https://api.example.com/v1', 'gte-rerank-v2'), 'https://api.example.com/api/v1/services/rerank/text-rerank/text-rerank')
        self.assertEqual(normalize_rerank_url('https://api.example.com/v1', 'qwen3-rerank'), 'https://api.example.com/compatible-api/v1/reranks')
        self.assertEqual(
            normalize_rerank_url('https://provider.example/custom/rank', 'my-reranker'),
            'https://provider.example/custom/rank',
        )

    def test_custom_embedding_endpoint_is_preserved(self):
        self.assertEqual(
            normalize_embedding_url('https://provider.example/custom/embed'),
            'https://provider.example/custom/embed',
        )


class TestKeyNotInJson(unittest.TestCase):
    """测试 Key 不入 JSON。"""
    
    def test_api_key_not_in_request(self):
        """API Key 不应在日志或异常中暴露。"""
        config = AIConfig(base_url='https://api.example.com/v1', api_key='secret-key-123')
        mock_session = Mock()
        client = AIClient(config, session=mock_session)
        
        error_response = Mock()
        error_response.status_code = 500
        error_response.text = 'secret-key-123'
        mock_session.request.return_value = error_response
        
        try:
            client.enrich('标题', '正文')
        except AIError as e:
            error_msg = str(e)
            self.assertNotIn('secret-key-123', error_msg)


if __name__ == '__main__':
    unittest.main()
