"""AI 客户端模块 - 调用大语言模型 API。"""
from __future__ import annotations

import json as json_module
import re
import time
from dataclasses import dataclass
from typing import Optional, Any, Callable

import requests


class AIError(Exception):
    """AI 相关错误基类。"""
    pass


class AuthenticationError(AIError):
    """鉴权失败 (401/403)。"""
    pass


class QuotaError(AIError):
    """配额/余额不足 (402 或正文明确 quota/balance)。"""
    pass


class RateLimitError(AIError):
    """限流 (429)。"""
    pass


class ModelNotFoundError(AIError):
    """模型不存在 (404)。"""
    pass


class NetworkAIError(AIError):
    """网络错误。"""
    pass


class InvalidAIResponse(AIError):
    """无效响应。"""
    pass


class ConfigSecurityError(Exception):
    """配置安全错误。"""
    pass


def filter_sensitive_text(text: str) -> str:
    """过滤敏感文本 - 学号/身份证样式替换为 [已隐藏]。"""
    if not text:
        return text
    pattern = r'\b[0-9]{8,18}\b'
    return re.sub(pattern, '[已隐藏]', text)


@dataclass
class ChatResponse:
    """聊天响应。"""
    content: str
    model: str
    usage: dict | None = None


def normalize_chat_url(url: str) -> str:
    """规范化聊天 URL。"""
    url = url.strip().rstrip('/')
    if not url:
        return ''
    if url.endswith('/v1/embeddings') or url.endswith('/embeddings'):
        return url
    if url.endswith('/v1/chat/completions'):
        return url
    elif url.endswith('/v1'):
        return url + '/chat/completions'
    else:
        return url + '/v1/chat/completions'


def normalize_embedding_url(url: str) -> str:
    """规范化嵌入 URL。"""
    url = url.strip().rstrip('/')
    if not url:
        return ''
    if url.endswith('/v1/embeddings') or url.endswith('/embeddings'):
        return url
    if url.endswith('/v1/chat/completions') or url.endswith('/v1'):
        base = url.rsplit('/v1', 1)[0]
        return base + '/v1/embeddings'
    # Embedding providers often expose non-OpenAI endpoint paths.  Treat a
    # user-entered URL with a path as the complete endpoint.
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    return url if parsed.path not in ('', '/') else url + '/v1/embeddings'


def normalize_rerank_url(base_url: str, model: str) -> str:
    """规范化重排序 URL。"""
    base_url = base_url.strip().rstrip('/')
    if not base_url:
        return ''
    if base_url.endswith('/text-rerank') or base_url.endswith('/reranks'):
        return base_url
    from urllib.parse import urlsplit
    parsed = urlsplit(base_url)
    # A custom model paired with a concrete path is provider-owned.  Never
    # replace it with an endpoint inferred from an unrelated built-in model.
    if model not in {'gte-rerank-v2', 'qwen3-rerank'}:
        return base_url
    if model == 'gte-rerank-v2':
        if '/v1' in base_url:
            host = base_url.split('/v1')[0]
            return host + '/api/v1/services/rerank/text-rerank/text-rerank'
        return base_url + '/api/v1/services/rerank/text-rerank/text-rerank'
    elif model == 'qwen3-rerank':
        if '/v1' in base_url:
            host = base_url.split('/v1')[0]
            return host + '/compatible-api/v1/reranks'
        return base_url + '/compatible-api/v1/reranks'
    return ''


def _is_minimax_m3(url: str, model: str) -> bool:
    """Return whether this is MiniMax M3, whose thinking can be disabled."""
    return (
        ('minimax' in url.lower() or model.lower().startswith('minimax-'))
        and model.strip().lower() == 'minimax-m3'
    )


def _chat_payload(
    url: str,
    model: str,
    messages: list[dict[str, Any]],
    *,
    reasoning: bool,
    max_completion_tokens: int,
    json_object: bool = False,
) -> dict[str, Any]:
    """Build one Chat Completions payload with task-appropriate M3 controls."""
    payload: dict[str, Any] = {
        'model': model,
        'messages': messages,
        'max_completion_tokens': max_completion_tokens,
    }
    if _is_minimax_m3(url, model):
        payload['thinking'] = {'type': 'adaptive' if reasoning else 'disabled'}
        if reasoning:
            # Keep private reasoning out of message.content.  Callers consume
            # only the final content and never display or cache these fields.
            payload['reasoning_split'] = True
    elif json_object:
        # Preserve structured-output support for other OpenAI-compatible
        # providers. MiniMax M3 does not document this Chat parameter, so its
        # JSON tasks rely on the prompt plus defensive parsing instead.
        payload['response_format'] = {'type': 'json_object'}
    return payload


def _final_content(content: Any) -> str:
    """Return final text without exposing an inline MiniMax thinking block."""
    if not isinstance(content, str) or not content.strip():
        return ''
    candidate = content.strip()
    candidate = re.sub(r'<think>[\s\S]*?</think>', '', candidate, flags=re.I).strip()
    if candidate.lower().startswith('<think>'):
        # The model reached a token or transport limit before final output.
        return ''
    return candidate


def _json_object_result(content: Any) -> dict[str, Any] | None:
    """Extract a JSON object from plain, fenced, or surrounding-text output."""
    candidate = _final_content(content)
    if not candidate:
        return None
    fenced = re.findall(r'```(?:json)?\s*([\s\S]*?)\s*```', candidate, flags=re.I)
    attempts = [*reversed(fenced), candidate]
    decoder = json_module.JSONDecoder()
    for value in attempts:
        value = str(value).strip()
        try:
            parsed = json_module.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        # A model may surround the object with one explanatory sentence. Raw
        # decoding from each opening brace also handles nested arrays/objects.
        for index, character in enumerate(value):
            if character != '{':
                continue
            try:
                parsed, _end = decoder.raw_decode(value[index:])
            except Exception:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def _choice_message(data: Any) -> dict[str, Any]:
    """Read the first completed choice and reject token-truncated output."""
    try:
        choice = data['choices'][0]
        if str(choice.get('finish_reason') or '').lower() == 'length':
            raise InvalidAIResponse('模型输出达到 token 上限，未产生完整最终答案')
        message = choice['message']
    except InvalidAIResponse:
        raise
    except Exception as exc:
        raise InvalidAIResponse('回答响应格式无效') from exc
    if not isinstance(message, dict):
        raise InvalidAIResponse('回答响应格式无效')
    return message


def _summary_result(content: Any) -> str:
    """Extract a summary from OpenAI-compatible plain/JSON/reasoning output."""
    candidate = _final_content(content)
    parsed = _json_object_result(candidate)
    if parsed:
        summary = str(parsed.get('summary') or '').strip()
        if summary:
            return summary
    return re.sub(r'^(?:摘要|主题)\s*[：:]\s*', '', candidate).strip()


class AIClient:
    """AI 客户端 - 支持聊天、嵌入、重排序。"""
    
    def __init__(
        self,
        config: Any,
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_retries: int = 2,
        breaker_threshold: int = 3
    ):
        self.config = config
        self.session = session or requests.Session()
        self.sleeper = sleeper
        self.max_retries = max_retries
        self.breaker_threshold = breaker_threshold
        self._failure_counts: dict[str, int] = {}
        self._circuit_open: dict[str, bool] = {}
    
    def _record_failure(self, capability: str) -> None:
        """记录失败。"""
        self._failure_counts[capability] = self._failure_counts.get(capability, 0) + 1
        if self._failure_counts[capability] >= self.breaker_threshold:
            self._circuit_open[capability] = True
    
    def _record_success(self, capability: str) -> None:
        """记录成功。"""
        self._failure_counts[capability] = 0
        self._circuit_open[capability] = False
    
    def reset(self, capability: str) -> None:
        """重置熔断器。"""
        self._failure_counts[capability] = 0
        self._circuit_open[capability] = False
    
    def _check_circuit(self, capability: str) -> None:
        """检查熔断器。"""
        if self._circuit_open.get(capability, False):
            raise AIError(f'能力 {capability} 已熔断')
    
    def _request(self, method: str, url: str, *, capability: str = 'chat', **kwargs) -> requests.Response:
        """发送请求，处理错误分类。"""
        headers = kwargs.pop('headers', {})
        keys = {
            'chat': self.config.api_key,
            'embedding': self.config.embedding_api_key,
            'rerank': self.config.rerank_api_key,
            'summary': self.config.summary_api_key,
        }
        api_key = keys.get(capability)
        if not api_key:
            raise AuthenticationError(f'{capability} API Key 未配置')
        headers['Authorization'] = f'Bearer {api_key}'
        kwargs['headers'] = headers
        
        try:
            response = self.session.request(method, url, **kwargs)
            return response
        except requests.exceptions.RequestException as e:
            self._record_failure('network')
            raise NetworkAIError(f'网络错误：{type(e).__name__}')
    
    def _handle_error(self, response: requests.Response, capability: str) -> None:
        """处理 HTTP 错误。"""
        status = response.status_code
        
        try:
            body_text = response.text[:200]
        except Exception:
            body_text = ''
        
        if status in (401, 403):
            self._record_failure(capability)
            raise AuthenticationError(f'鉴权失败 {status}')
        
        if status == 402 or 'quota' in body_text.lower() or 'balance' in body_text.lower():
            self._record_failure(capability)
            raise QuotaError(f'配额不足 {status}')
        
        if status == 404:
            self._record_failure(capability)
            raise ModelNotFoundError(f'模型不存在 {status}')
        
        if status == 429:
            self._record_failure(capability)
            raise RateLimitError(f'限流 {status}')
        
        if status >= 500:
            self._record_failure(capability)
            raise AIError(f'服务器错误 {status}')
        
        if status >= 400:
            self._record_failure(capability)
            raise AIError(f'客户端错误 {status}')
    
    def test_connection(self, capability: str = 'chat') -> str:
        """测试连接。"""
        self._check_circuit(capability)

        if capability == 'chat':
            if not (self.config.base_url and self.config.model and self.config.api_key):
                raise AuthenticationError('Chat URL、Key 和模型必须完整配置')
            url = normalize_chat_url(self.config.base_url)
            payload = _chat_payload(
                url, self.config.model,
                [{'role': 'user', 'content': 'ping'}],
                reasoning=False, max_completion_tokens=16,
            )
        elif capability == 'embedding':
            if not (self.config.embedding_url and self.config.embedding_model and self.config.embedding_api_key):
                raise AuthenticationError('Embedding URL、Key 和模型必须完整配置')
            url = normalize_embedding_url(self.config.embedding_url)
            payload = {'model': self.config.embedding_model, 'input': 'ping'}
        elif capability == 'rerank':
            if not (self.config.rerank_url and self.config.rerank_model and self.config.rerank_api_key):
                raise AuthenticationError('Rerank URL、Key 和模型必须完整配置')
            url = normalize_rerank_url(self.config.rerank_url, self.config.rerank_model)
            documents = [{'text': 'ping'}] if self.config.rerank_model == 'qwen3-rerank' else ['ping']
            payload = {'model': self.config.rerank_model, 'query': 'ping', 'documents': documents}
        elif capability == 'summary':
            if not (self.config.summary_url and self.config.summary_model and self.config.summary_api_key):
                raise AuthenticationError('摘要模型 URL、Key 和模型必须完整配置')
            url = normalize_chat_url(self.config.summary_url)
            payload = _chat_payload(
                url, self.config.summary_model,
                [{'role': 'user', 'content': 'ping'}],
                reasoning=False, max_completion_tokens=16,
            )
        else:
            raise AIError('未知能力')
        try:
            response = self._request(
                'POST', url, capability=capability, json=payload, timeout=10
            )
            if response.status_code == 200:
                self._record_success(capability)
                return 'OK'
            self._handle_error(response, capability)
        except AIError:
            raise
        except Exception as e:
            self._record_failure(capability)
            raise AIError(f'连接失败：{type(e).__name__}') from e
        raise AIError('连接失败')
    
    def enrich(self, title: str, body: str, attachments: list | None = None) -> dict:
        """丰富通知内容。"""
        self._check_circuit('chat')
        
        filtered_title = filter_sensitive_text(title)
        filtered_body = filter_sensitive_text(body)
        
        prompt = f'''请分析以下通知内容，返回 JSON 格式：
标题：{filtered_title}
正文：{filtered_body}

请返回以下字段：
- short_title: 简短标题
- summary: 摘要
- audience: 受众
- deadline: 截止时间 (如有)
- action: 需要执行的操作
- start_time: 开始时间 (如有)
- materials: 需要的材料 (列表)
- method_or_location: 方式或地点
- contact: 联系方式
- requires_external_system: 是否需要外部系统 (布尔或 null)
- uncertainty: 不确定内容
- tags: 标签 (列表)

只返回 JSON，不要其他内容。'''
        
        url = normalize_chat_url(self.config.base_url)
        payload = _chat_payload(
            url, self.config.model,
            [{'role': 'user', 'content': prompt}],
            reasoning=False, max_completion_tokens=2048, json_object=True,
        )
        
        retries = 0
        last_error = None
        
        while retries <= self.max_retries:
            try:
                response = self._request('POST', url, json=payload, timeout=30)
                if response.status_code == 200:
                    try:
                        message = _choice_message(response.json())
                        result = _json_object_result(message.get('content', ''))
                    except InvalidAIResponse:
                        self._record_failure('chat')
                        raise
                    if not result:
                        self._record_failure('chat')
                        raise InvalidAIResponse('公告富化未返回有效 JSON')
                    self._record_success('chat')
                    return {
                        'short_title': result.get('short_title', title[:50]),
                        'summary': result.get('summary', ''),
                        'audience': result.get('audience', ''),
                        'deadline': result.get('deadline'),
                        'action': result.get('action', ''),
                        'start_time': result.get('start_time'),
                        'materials': result.get('materials', []),
                        'method_or_location': result.get('method_or_location', ''),
                        'contact': result.get('contact', ''),
                        'requires_external_system': result.get('requires_external_system'),
                        'uncertainty': result.get('uncertainty', ''),
                        'tags': result.get('tags', []),
                    }
                
                self._handle_error(response, 'chat')
            except (RateLimitError, NetworkAIError) as e:
                last_error = e
                retries += 1
                if retries <= self.max_retries:
                    self.sleeper(0.5 * retries)
                continue
            except AIError:
                raise
            except Exception as e:
                last_error = InvalidAIResponse(f'响应解析失败：{e}')
                retries += 1
                if retries <= self.max_retries:
                    self.sleeper(0.5 * retries)
                continue
        
        self._record_failure('chat')
        raise last_error or AIError('请求失败')
    
    def expand_query(self, query: str) -> list[str]:
        """扩展查询。"""
        self._check_circuit('chat')
        
        prompt = f'请为以下查询生成 3 个相关变体，返回 JSON 对象，格式为 {{"queries":["..."]}}：{query}'
        
        url = normalize_chat_url(self.config.base_url)
        payload = _chat_payload(
            url, self.config.model,
            [{'role': 'user', 'content': prompt}],
            reasoning=False, max_completion_tokens=256, json_object=True,
        )
        
        try:
            response = self._request('POST', url, json=payload, timeout=15)
            if response.status_code == 200:
                try:
                    message = _choice_message(response.json())
                    result = _json_object_result(message.get('content', ''))
                    values = result.get('queries', []) if isinstance(result, dict) else result
                    if isinstance(values, list):
                        expanded = [str(x).strip() for x in values if str(x).strip()]
                        self._record_success('chat')
                        return [query, *expanded]
                except Exception:
                    pass
            self._record_failure('chat')
        except AIError:
            pass
        except Exception:
            pass
        
        return [query]

    def answer(self, question: str, documents: list[str]) -> str:
        """Answer only from caller-supplied notice excerpts."""
        self._check_circuit('chat')
        if not documents:
            raise InvalidAIResponse('没有可用于回答的公告依据')
        context = '\n\n---\n\n'.join(filter_sensitive_text(item) for item in documents)
        url = normalize_chat_url(self.config.base_url)
        payload = _chat_payload(
            url, self.config.model,
            [
                {'role': 'system', 'content': (
                    '你只能根据用户提供的学校公告摘录回答。不得使用模型记忆补充学校事实；'
                    '依据不足时明确说未找到。附件内容未提供时必须提示查看原文附件。'
                )},
                {'role': 'user', 'content': f'问题：{filter_sensitive_text(question)}\n\n公告依据：\n{context}'},
            ],
            reasoning=True, max_completion_tokens=4096,
        )
        response = self._request('POST', url, json=payload, timeout=30)
        if response.status_code != 200:
            self._handle_error(response, 'chat')
        try:
            message = _choice_message(response.json())
            content = _final_content(message.get('content', ''))
        except InvalidAIResponse:
            self._record_failure('chat')
            raise
        if not isinstance(content, str) or not content.strip():
            self._record_failure('chat')
            raise InvalidAIResponse('回答内容为空')
        self._record_success('chat')
        return content.strip()

    def summarize_share(self, title: str, body: str) -> str:
        """Produce a concise, topic-only share summary for one notice.

        Distinct from :meth:`enrich`: a single Chat call, 20 second timeout and
        no automatic retries.  MiniMax M3 thinking is disabled for this simple
        extraction task. Any parse failure or empty value raises
        :class:`AIError` so the caller can fall back to a cached value or local
        title; it never retries with a different model.
        """
        self._check_circuit('summary')

        filtered_title = filter_sensitive_text(title)
        filtered_body = filter_sensitive_text(body)

        prompt = (
            '请为以下学校公告生成一句用于转发的简短中文主题摘要。\n'
            '严格要求：\n'
            '1. 只概括“什么对象的什么事项或项目”，不要复述具体安排；\n'
            '2. 禁止出现月份、具体日期、时刻、时间区间、截止时间、电话号码或文件编号；'
            '年份仅在它属于年级、届次或项目正式名称时保留；\n'
            '3. 不得猜测正文没有的信息，不得改变年级、对象或事项名称；\n'
            '4. 建议 10～16 个汉字，表达自然完整；宁可稍长，也不能截断数字、词语或事实；\n'
            '5. 单行、无标签、无引号、无“摘要：”等前缀。\n'
            '示例：2026级本科生第一学期选课安排\n'
            f'标题：{filtered_title}\n'
            f'正文：{filtered_body}\n'
            '只返回 JSON：{"summary": "..."}'
        )

        url = normalize_chat_url(self.config.summary_url)
        payload = _chat_payload(
            url, self.config.summary_model,
            [{'role': 'user', 'content': prompt}],
            reasoning=False, max_completion_tokens=256, json_object=True,
        )

        response = self._request('POST', url, capability='summary', json=payload, timeout=20)
        if response.status_code != 200:
            self._handle_error(response, 'summary')
        try:
            message = _choice_message(response.json())
            content = message.get('content', '')
        except InvalidAIResponse:
            self._record_failure('summary')
            raise
        summary = _summary_result(content)
        if not summary:
            self._record_failure('summary')
            raise InvalidAIResponse('模型只返回了思考过程或空内容；请重试或换用非思考模型')
        self._record_success('summary')
        return summary

    def embedding(self, text: str) -> list[float]:
        """获取文本嵌入。"""
        self._check_circuit('embedding')
        
        if not self.config.embedding_enabled:
            raise AIError('嵌入功能未启用')
        
        if not (self.config.embedding_url and self.config.embedding_model and self.config.embedding_api_key):
            raise AuthenticationError('Embedding URL、Key 和模型必须独立配置')
        url = normalize_embedding_url(self.config.embedding_url)
        payload = {
            'model': self.config.embedding_model,
            'input': filter_sensitive_text(text)
        }
        
        try:
            response = self._request('POST', url, capability='embedding', json=payload, timeout=30)
            if response.status_code == 200:
                data = response.json()
                embedding = data.get('data', [{}])[0].get('embedding', [])
                self._record_success('embedding')
                return embedding
            self._handle_error(response, 'embedding')
        except AIError:
            raise
        except Exception as e:
            self._record_failure('embedding')
            raise AIError(f'嵌入失败：{e}')
        
        return []
    
    def rerank(self, query: str, documents: list[str]) -> list[int]:
        """重排序文档。"""
        self._check_circuit('rerank')
        
        if not self.config.rerank_enabled:
            raise AIError('重排序功能未启用')
        
        model = self.config.rerank_model
        if not (self.config.rerank_url and self.config.rerank_model and self.config.rerank_api_key):
            raise AuthenticationError('Rerank URL、Key 和模型必须独立配置')
        base_url = self.config.rerank_url
        url = normalize_rerank_url(base_url, model)
        
        if model == 'gte-rerank-v2':
            payload = {
                'model': model,
                'query': filter_sensitive_text(query),
                'documents': [filter_sensitive_text(doc) for doc in documents]
            }
        elif model == 'qwen3-rerank':
            payload = {
                'model': model,
                'query': filter_sensitive_text(query),
                'documents': [{'text': filter_sensitive_text(doc)} for doc in documents]
            }
        else:
            payload = {
                'model': model,
                'query': filter_sensitive_text(query),
                'documents': documents
            }
        
        try:
            response = self._request('POST', url, capability='rerank', json=payload, timeout=30)
            if response.status_code == 200:
                data = response.json()
                
                if 'results' in data:
                    results = data['results']
                    if results and 'relevance_score' in results[0]:
                        scored = [(i, r['relevance_score']) for i, r in enumerate(results)]
                        scored.sort(key=lambda x: x[1], reverse=True)
                        self._record_success('rerank')
                        return [i for i, _ in scored]
                
                if 'data' in data:
                    scored = []
                    for item in data['data']:
                        idx = item.get('index', 0)
                        score = item.get('relevance_score', 0)
                        scored.append((idx, score))
                    scored.sort(key=lambda x: x[1], reverse=True)
                    self._record_success('rerank')
                    return [i for i, _ in scored]
                
                self._record_failure('rerank')
                raise InvalidAIResponse('重排序响应格式未知')
            
            self._handle_error(response, 'rerank')
        except AIError:
            raise
        except Exception as e:
            self._record_failure('rerank')
            raise AIError(f'重排序失败：{e}')
        
        return list(range(len(documents)))
