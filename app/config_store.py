"""AI 配置存储模块 - 安全存储配置和 API Key。"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# 配置 JSON 的版本号。加载旧配置时执行一次性迁移，然后写回当前版本。
CONFIG_VERSION = 3
# 旧版本自动注入的实验模型默认值，用于一次性迁移识别。
_LEGACY_DEFAULT_EMBEDDING_MODEL = 'qwen3.7-text-embedding'
_LEGACY_DEFAULT_RERANK_MODEL = 'gte-rerank-v2'


@dataclass
class AIConfig:
    """AI 服务配置。"""
    base_url: str = ''
    model: str = ''
    embedding_enabled: bool = False
    embedding_url: str = ''
    embedding_model: str = ''
    embedding_dimensions: int = 1024
    rerank_enabled: bool = False
    rerank_url: str = ''
    rerank_model: str = ''
    summary_enabled: bool = False
    summary_url: str = ''
    summary_model: str = ''
    api_key: Optional[str] = field(default=None, repr=False)
    embedding_api_key: Optional[str] = field(default=None, repr=False)
    rerank_api_key: Optional[str] = field(default=None, repr=False)
    summary_api_key: Optional[str] = field(default=None, repr=False)


class ConfigSecurityError(Exception):
    """配置安全相关错误。"""
    pass


def normalize_chat_url(url: str) -> str:
    """规范化聊天补全 URL。

    - 去除首尾空白
    - 去除尾部斜杠
    - 已有/v1/chat/completions 原样返回
    - 末尾/v1 补/chat/completions
    - 其他补/v1/chat/completions
    """
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
    """规范化嵌入 URL。

    - 若输入是 chat endpoint 或/v1，归一为/v1/embeddings
    - 否则基于主机推导
    """
    url = url.strip().rstrip('/')
    if not url:
        return ''

    if url.endswith('/v1/chat/completions') or url.endswith('/v1'):
        base = url.rsplit('/v1', 1)[0]
        return base + '/v1/embeddings'
    else:
        return url + '/v1/embeddings'


def normalize_rerank_url(base_url: str, model: str) -> str:
    """规范化重排序 URL。

    - gte-rerank-v2 => 同协议主机/api/v1/services/rerank/text-rerank/text-rerank
    - qwen3-rerank => 同协议主机/compatible-api/v1/reranks
    - 显式非空 rerank_url 由调用方保留
    """
    base_url = base_url.strip().rstrip('/')
    if not base_url:
        return ''
    if base_url.endswith('/text-rerank') or base_url.endswith('/reranks'):
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


class ConfigStore:
    """配置存储 - 普通配置 JSON，Key 用 DPAPI 加密 (Windows) 或环境变量。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.key_path = self.path.with_suffix('.key')
        self.embedding_key_path = self.path.with_name(self.path.stem + '.embedding.key')
        self.rerank_key_path = self.path.with_name(self.path.stem + '.rerank.key')
        self.summary_key_path = self.path.with_name(self.path.stem + '.summary.key')

    def _try_dpapi_encrypt(self, data: bytes) -> bytes | None:
        """Windows DPAPI 加密。"""
        if sys.platform != 'win32':
            return None
        try:
            import ctypes
            from ctypes import wintypes

            cryptprotect = ctypes.windll.crypt32

            class DATA_BLOB(ctypes.Structure):
                _fields_ = [('cbData', wintypes.DWORD),
                            ('pbData', ctypes.POINTER(ctypes.c_ubyte))]

            in_buffer = ctypes.create_string_buffer(data)
            in_blob = DATA_BLOB(
                len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_ubyte))
            )

            out_blob = DATA_BLOB()
            if cryptprotect.CryptProtectData(
                ctypes.byref(in_blob),
                None, None, None, None, 0,
                ctypes.byref(out_blob)
            ):
                result = ctypes.string_at(out_blob.pbData, out_blob.cbData)
                ctypes.windll.kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))
                return result
        except Exception:
            pass
        return None

    def _try_dpapi_decrypt(self, data: bytes) -> bytes | None:
        """Windows DPAPI 解密。"""
        if sys.platform != 'win32':
            return None
        try:
            import ctypes
            from ctypes import wintypes

            cryptprotect = ctypes.windll.crypt32

            class DATA_BLOB(ctypes.Structure):
                _fields_ = [('cbData', wintypes.DWORD),
                            ('pbData', ctypes.POINTER(ctypes.c_ubyte))]

            in_buffer = ctypes.create_string_buffer(data)
            in_blob = DATA_BLOB(
                len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_ubyte))
            )

            out_blob = DATA_BLOB()
            if cryptprotect.CryptUnprotectData(
                ctypes.byref(in_blob),
                None, None, None, None, 0,
                ctypes.byref(out_blob)
            ):
                result = ctypes.string_at(out_blob.pbData, out_blob.cbData)
                ctypes.windll.kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))
                return result
        except Exception:
            pass
        return None

    def _save_secret(self, path: Path, value: Optional[str], *, clear: bool = False) -> None:
        if clear:
            if path.exists():
                path.unlink()
            return
        if value is None:
            return
        if not value:
            return
        encrypted = self._try_dpapi_encrypt(value.encode('utf-8'))
        if encrypted is None:
            raise ConfigSecurityError('DPAPI 加密失败，无法安全保存 Key')
        path.write_bytes(encrypted)

    def _atomic_write_json(self, config_dict: dict) -> None:
        """原子写入 JSON：先写临时文件，再 os.replace 覆盖。

        失败时清理临时文件，绝不让目标 JSON 处于半写状态。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(self.path.name + '.tmp')
        try:
            tmp_path.write_text(
                json.dumps(config_dict, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            raise

    def _migrate_config(self, config_dict: dict) -> tuple[dict, bool]:
        """对配置 JSON 执行一次性迁移。

        仅在 ``version`` 缺失或小于 :data:`CONFIG_VERSION` 时运行：
            - 若 ``embedding_model`` 恰为旧自动默认 ``qwen3.7-text-embedding``，则清空；
            - 若 ``rerank_model`` 恰为旧自动默认 ``gte-rerank-v2``，则清空；
            - 其他字段（URL、enabled、dimensions、Chat 模型与 Key、自定义实验模型等）
              一律保留；
            - 写回 ``version = CONFIG_VERSION``，下次加载时跳过迁移。

        返回 ``(new_dict, changed)``；``changed`` 为真表示对内容做了修改，
        调用方应通过 :meth:`_atomic_write_json` 写回 JSON。本方法绝不触碰
        ``.key`` 加密 secret 文件。
        """
        existing_version = config_dict.get('version')
        if isinstance(existing_version, int) and existing_version >= CONFIG_VERSION:
            return config_dict, False

        new_dict = dict(config_dict)

        if new_dict.get('embedding_model') == _LEGACY_DEFAULT_EMBEDDING_MODEL:
            new_dict['embedding_model'] = ''
        if new_dict.get('rerank_model') == _LEGACY_DEFAULT_RERANK_MODEL:
            new_dict['rerank_model'] = ''

        new_dict['version'] = CONFIG_VERSION
        return new_dict, True

    def save(
        self, config: AIConfig, new_api_key: Optional[str] = None,
        new_embedding_api_key: Optional[str] = None,
        new_rerank_api_key: Optional[str] = None, *,
        new_summary_api_key: Optional[str] = None,
        clear_api_key: bool = False, clear_embedding_api_key: bool = False,
        clear_rerank_api_key: bool = False, clear_summary_api_key: bool = False,
    ) -> None:
        """保存配置。Key 不进 JSON/数据库/日志。"""
        config_dict = {
            'version': CONFIG_VERSION,
            'base_url': config.base_url,
            'model': config.model,
            'embedding_enabled': config.embedding_enabled,
            'embedding_url': config.embedding_url,
            'embedding_model': config.embedding_model,
            'embedding_dimensions': config.embedding_dimensions,
            'rerank_enabled': config.rerank_enabled,
            'rerank_url': config.rerank_url,
            'rerank_model': config.rerank_model,
            'summary_enabled': config.summary_enabled,
            'summary_url': config.summary_url,
            'summary_model': config.summary_model,
        }

        self._atomic_write_json(config_dict)

        self._save_secret(self.key_path, new_api_key, clear=clear_api_key)
        self._save_secret(
            self.embedding_key_path, new_embedding_api_key, clear=clear_embedding_api_key
        )
        self._save_secret(self.rerank_key_path, new_rerank_api_key, clear=clear_rerank_api_key)
        self._save_secret(self.summary_key_path, new_summary_api_key, clear=clear_summary_api_key)

    def load(self) -> AIConfig:
        """加载配置，必要时对旧版本做一次性迁移；尝试解密 Key 或从环境变量读取。"""
        config_dict: dict = {}
        if self.path.exists():
            config_text = self.path.read_text(encoding='utf-8')
            config_dict = json.loads(config_text)

        migrated_dict, changed = self._migrate_config(config_dict)
        if changed:
            # 迁移只触 JSON，绝不触碰任何 .key 文件。
            self._atomic_write_json(migrated_dict)
            config_dict = migrated_dict

        config = AIConfig(
            base_url=config_dict.get('base_url', ''),
            model=config_dict.get('model', ''),
            embedding_enabled=bool(config_dict.get('embedding_enabled', False)),
            embedding_url=config_dict.get('embedding_url', ''),
            embedding_model=config_dict.get('embedding_model', ''),
            embedding_dimensions=int(config_dict.get('embedding_dimensions', 1024)),
            rerank_enabled=bool(config_dict.get('rerank_enabled', False)),
            rerank_url=config_dict.get('rerank_url', ''),
            rerank_model=config_dict.get('rerank_model', ''),
            summary_enabled=bool(config_dict.get('summary_enabled', False)),
            summary_url=config_dict.get('summary_url', ''),
            summary_model=config_dict.get('summary_model', ''),
        )

        config.api_key = self._load_secret(self.key_path, 'SCHOOL_NOTICE_AI_KEY')
        config.embedding_api_key = self._load_secret(
            self.embedding_key_path, 'SCHOOL_NOTICE_EMBEDDING_KEY'
        )
        config.rerank_api_key = self._load_secret(
            self.rerank_key_path, 'SCHOOL_NOTICE_RERANK_KEY'
        )
        config.summary_api_key = self._load_secret(
            self.summary_key_path, 'SCHOOL_NOTICE_SUMMARY_KEY'
        )
        return config

    def _load_secret(self, path: Path, environment_name: str) -> str | None:
        if path.exists():
            decrypted = self._try_dpapi_decrypt(path.read_bytes())
            if decrypted:
                return decrypted.decode('utf-8')
        return os.environ.get(environment_name)

    def key_hint(self, capability: str = 'chat') -> str:
        """返回 Key 状态提示。"""
        paths = {
            'chat': (self.key_path, 'SCHOOL_NOTICE_AI_KEY'),
            'embedding': (self.embedding_key_path, 'SCHOOL_NOTICE_EMBEDDING_KEY'),
            'rerank': (self.rerank_key_path, 'SCHOOL_NOTICE_RERANK_KEY'),
            'summary': (self.summary_key_path, 'SCHOOL_NOTICE_SUMMARY_KEY'),
        }
        path, environment_name = paths.get(capability, paths['chat'])
        if path.exists():
            return '已保存 (加密)'
        elif os.environ.get(environment_name):
            return '来自环境变量'
        return '未设置'
