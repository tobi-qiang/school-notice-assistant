"""ConfigStore 单元测试。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config_store import (
    AIConfig,
    ConfigStore,
    ConfigSecurityError,
    CONFIG_VERSION,
)
from app.config_store import (
    normalize_chat_url,
    normalize_embedding_url,
    normalize_rerank_url,
)


class TestNormalizeChatUrl(unittest.TestCase):
    """测试 normalize_chat_url。"""

    def test_empty(self):
        self.assertEqual(normalize_chat_url(''), '')
        self.assertEqual(normalize_chat_url('   '), '')

    def test_whitespace_trim(self):
        self.assertEqual(normalize_chat_url('  https://api.example.com  '), 'https://api.example.com/v1/chat/completions')

    def test_trailing_slash(self):
        self.assertEqual(normalize_chat_url('https://api.example.com/'), 'https://api.example.com/v1/chat/completions')

    def test_already_full(self):
        self.assertEqual(normalize_chat_url('https://api.example.com/v1/chat/completions'), 'https://api.example.com/v1/chat/completions')

    def test_ends_with_v1(self):
        self.assertEqual(normalize_chat_url('https://api.example.com/v1'), 'https://api.example.com/v1/chat/completions')

    def test_plain_url(self):
        self.assertEqual(normalize_chat_url('https://api.example.com'), 'https://api.example.com/v1/chat/completions')


class TestNormalizeEmbeddingUrl(unittest.TestCase):
    """测试 normalize_embedding_url。"""

    def test_empty(self):
        self.assertEqual(normalize_embedding_url(''), '')

    def test_from_chat_completions(self):
        self.assertEqual(normalize_embedding_url('https://api.example.com/v1/chat/completions'), 'https://api.example.com/v1/embeddings')

    def test_from_v1(self):
        self.assertEqual(normalize_embedding_url('https://api.example.com/v1'), 'https://api.example.com/v1/embeddings')

    def test_plain_url(self):
        self.assertEqual(normalize_embedding_url('https://api.example.com'), 'https://api.example.com/v1/embeddings')


class TestNormalizeRerankUrl(unittest.TestCase):
    """测试 normalize_rerank_url。"""

    def test_gte_rerank_v2(self):
        self.assertEqual(
            normalize_rerank_url('https://api.example.com/v1', 'gte-rerank-v2'),
            'https://api.example.com/api/v1/services/rerank/text-rerank/text-rerank'
        )

    def test_gte_rerank_v2_no_v1(self):
        self.assertEqual(
            normalize_rerank_url('https://api.example.com', 'gte-rerank-v2'),
            'https://api.example.com/api/v1/services/rerank/text-rerank/text-rerank'
        )

    def test_qwen3_rerank(self):
        self.assertEqual(
            normalize_rerank_url('https://api.example.com/v1', 'qwen3-rerank'),
            'https://api.example.com/compatible-api/v1/reranks'
        )

    def test_empty(self):
        self.assertEqual(normalize_rerank_url('', 'gte-rerank-v2'), '')


class TestAIConfig(unittest.TestCase):
    """测试 AIConfig 数据类。"""

    def test_defaults(self):
        config = AIConfig()
        self.assertEqual(config.base_url, '')
        self.assertEqual(config.model, '')
        self.assertFalse(config.embedding_enabled)
        # 实验模型默认值为空，避免旧版自动默认迁移后被误判为用户主动填写。
        self.assertEqual(config.embedding_model, '')
        self.assertEqual(config.embedding_dimensions, 1024)
        self.assertFalse(config.rerank_enabled)
        self.assertEqual(config.rerank_model, '')
        self.assertFalse(config.summary_enabled)
        self.assertEqual(config.summary_model, '')
        self.assertIsNone(config.api_key)

    def test_custom_values(self):
        config = AIConfig(
            base_url='https://api.example.com/v1',
            model='qwen3.5',
            embedding_enabled=True,
            embedding_model='custom-embedding',
            rerank_enabled=True,
            rerank_model='qwen3-rerank',
            api_key='test-key'
        )
        self.assertEqual(config.base_url, 'https://api.example.com/v1')
        self.assertEqual(config.model, 'qwen3.5')
        self.assertTrue(config.embedding_enabled)
        self.assertEqual(config.embedding_model, 'custom-embedding')
        self.assertTrue(config.rerank_enabled)
        self.assertEqual(config.rerank_model, 'qwen3-rerank')
        self.assertEqual(config.api_key, 'test-key')

    def test_summary_values_are_independent(self):
        config = AIConfig(
            summary_enabled=True, summary_url='https://small.example/v1',
            summary_model='small-model', summary_api_key='summary-secret',
        )
        self.assertTrue(config.summary_enabled)
        self.assertEqual(config.summary_model, 'small-model')
        self.assertIsNone(config.api_key)


class TestConfigStore(unittest.TestCase):
    """测试 ConfigStore。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = Path(self.temp_dir) / 'config.json'

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_save_and_load(self):
        store = ConfigStore(self.config_path)
        config = AIConfig(base_url='https://api.example.com/v1', model='qwen3.5')
        store.save(config)

        loaded = store.load()
        self.assertEqual(loaded.base_url, 'https://api.example.com/v1')
        self.assertEqual(loaded.model, 'qwen3.5')

    def test_key_not_in_json(self):
        """Key 不得进入 JSON 文件。"""
        store = ConfigStore(self.config_path)
        config = AIConfig(base_url='https://api.example.com/v1')

        with patch.dict(os.environ, {'SCHOOL_NOTICE_AI_KEY': 'env-key-789'}):
            store.save(config)

        json_text = self.config_path.read_text(encoding='utf-8')
        self.assertNotIn('env-key-789', json_text)
        self.assertNotIn('api_key', json_text)

    def test_key_hint(self):
        store = ConfigStore(self.config_path)
        self.assertEqual(store.key_hint(), '未设置')

    def test_load_nonexistent(self):
        store = ConfigStore(self.config_path)
        config = store.load()
        self.assertIsInstance(config, AIConfig)
        self.assertEqual(config.base_url, '')
        self.assertEqual(config.embedding_model, '')
        self.assertEqual(config.rerank_model, '')

    def test_environment_variable_key(self):
        """非 Windows 环境下从环境变量读取 Key。"""
        store = ConfigStore(self.config_path)
        config = AIConfig(base_url='https://api.example.com/v1')
        store.save(config)

        with patch.dict(os.environ, {'SCHOOL_NOTICE_AI_KEY': 'env-key-456'}):
            loaded = store.load()
            self.assertEqual(loaded.api_key, 'env-key-456')


class TestConfigVersionedMigration(unittest.TestCase):
    """配置 JSON 版本化一次性迁移。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = Path(self.temp_dir) / 'config.json'

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_raw(self, data: dict) -> None:
        self.config_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )

    def test_old_default_embedding_model_is_cleared(self):
        """旧版本 embedding_model 恰为自动默认时清空，URL/启用/维度保留。"""
        self._write_raw({
            'version': 1,
            'base_url': 'https://api.example.com',
            'model': 'qwen3.5',
            'embedding_enabled': True,
            'embedding_url': 'https://embed.example.com/v1',
            'embedding_model': 'qwen3.7-text-embedding',
            'embedding_dimensions': 1024,
            'rerank_enabled': True,
            'rerank_url': 'https://rerank.example.com',
            'rerank_model': 'gte-rerank-v2',
        })

        config = ConfigStore(self.config_path).load()

        self.assertEqual(config.embedding_model, '')
        self.assertEqual(config.rerank_model, '')
        self.assertTrue(config.embedding_enabled)
        self.assertEqual(config.embedding_url, 'https://embed.example.com/v1')
        self.assertEqual(config.embedding_dimensions, 1024)
        self.assertEqual(config.base_url, 'https://api.example.com')
        self.assertEqual(config.model, 'qwen3.5')
        self.assertTrue(config.rerank_enabled)
        self.assertEqual(config.rerank_url, 'https://rerank.example.com')

    def test_old_default_rerank_model_only_is_cleared(self):
        """只有 rerank_model 命中旧默认时也清空，embedding_model 自定义保留。"""
        self._write_raw({
            'version': 1,
            'embedding_enabled': True,
            'embedding_url': 'https://embed.example.com',
            'embedding_model': 'text-embedding-3-small',
            'embedding_dimensions': 1536,
            'rerank_enabled': True,
            'rerank_url': 'https://rerank.example.com',
            'rerank_model': 'gte-rerank-v2',
        })

        config = ConfigStore(self.config_path).load()

        self.assertEqual(config.embedding_model, 'text-embedding-3-small')
        self.assertEqual(config.embedding_dimensions, 1536)
        self.assertEqual(config.rerank_model, '')
        self.assertEqual(config.rerank_url, 'https://rerank.example.com')

    def test_custom_embedding_model_is_preserved(self):
        """用户自定义 embedding_model 不被清空。"""
        self._write_raw({
            'version': 1,
            'embedding_enabled': True,
            'embedding_url': 'https://embed.example.com',
            'embedding_model': 'my-custom-embedder',
            'embedding_dimensions': 768,
            'rerank_enabled': False,
            'rerank_url': '',
            'rerank_model': 'gte-rerank-v2',
        })

        config = ConfigStore(self.config_path).load()

        self.assertEqual(config.embedding_model, 'my-custom-embedder')
        self.assertEqual(config.embedding_dimensions, 768)
        # rerank 命中旧默认 → 清空
        self.assertEqual(config.rerank_model, '')

    def test_custom_rerank_model_is_preserved(self):
        """用户自定义 rerank_model 不被清空。"""
        self._write_raw({
            'version': 1,
            'embedding_enabled': False,
            'embedding_url': '',
            'embedding_model': 'qwen3.7-text-embedding',
            'embedding_dimensions': 1024,
            'rerank_enabled': True,
            'rerank_url': 'https://rerank.example.com',
            'rerank_model': 'qwen3-rerank',
        })

        config = ConfigStore(self.config_path).load()

        self.assertEqual(config.embedding_model, '')
        self.assertEqual(config.rerank_model, 'qwen3-rerank')
        self.assertEqual(config.rerank_url, 'https://rerank.example.com')

    def test_no_version_field_is_migrated(self):
        """完全旧格式（无 version 字段）也被迁移。"""
        self._write_raw({
            'base_url': 'https://api.example.com',
            'model': 'qwen',
            'embedding_enabled': False,
            'embedding_url': '',
            'embedding_model': 'qwen3.7-text-embedding',
            'embedding_dimensions': 1024,
            'rerank_enabled': False,
            'rerank_url': '',
            'rerank_model': 'gte-rerank-v2',
        })

        config = ConfigStore(self.config_path).load()

        self.assertEqual(config.embedding_model, '')
        self.assertEqual(config.rerank_model, '')
        self.assertEqual(config.base_url, 'https://api.example.com')

    def test_migration_writes_new_version(self):
        """迁移后 JSON 写入新版本号。"""
        self._write_raw({
            'version': 1,
            'embedding_model': 'qwen3.7-text-embedding',
            'rerank_model': 'gte-rerank-v2',
        })

        ConfigStore(self.config_path).load()

        new_data = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertEqual(new_data['version'], CONFIG_VERSION)
        self.assertEqual(new_data['embedding_model'], '')
        self.assertEqual(new_data['rerank_model'], '')

    def test_migration_only_runs_once(self):
        """迁移只运行一次，重复 load 不再修改文件内容。"""
        self._write_raw({
            'version': 1,
            'embedding_model': 'qwen3.7-text-embedding',
            'rerank_model': 'gte-rerank-v2',
        })

        store = ConfigStore(self.config_path)
        first = store.load()
        first_mtime = self.config_path.stat().st_mtime_ns
        first_content = self.config_path.read_text(encoding='utf-8')

        second = store.load()

        self.assertEqual(first.embedding_model, '')
        self.assertEqual(first.rerank_model, '')
        self.assertEqual(second.embedding_model, '')
        self.assertEqual(second.rerank_model, '')
        self.assertEqual(self.config_path.read_text(encoding='utf-8'), first_content)

    def test_current_version_skips_migration(self):
        """已是当前版本时不再迁移，模型值即使等于旧默认也保留。"""
        self._write_raw({
            'version': CONFIG_VERSION,
            'embedding_model': 'qwen3.7-text-embedding',
            'rerank_model': 'gte-rerank-v2',
        })

        config = ConfigStore(self.config_path).load()

        # 用户在新版本下重新填了旧默认字符串，迁移不应再次清空。
        self.assertEqual(config.embedding_model, 'qwen3.7-text-embedding')
        self.assertEqual(config.rerank_model, 'gte-rerank-v2')

    def test_migration_preserves_unknown_fields(self):
        """迁移保留未知字段，便于将来扩展。"""
        self._write_raw({
            'version': 1,
            'embedding_model': 'qwen3.7-text-embedding',
            'rerank_model': 'gte-rerank-v2',
            'custom_field': 'keep-me',
            'future_capability': {'foo': 'bar'},
        })

        ConfigStore(self.config_path).load()
        data = json.loads(self.config_path.read_text(encoding='utf-8'))

        self.assertEqual(data.get('custom_field'), 'keep-me')
        self.assertEqual(data.get('future_capability'), {'foo': 'bar'})

    def test_migration_does_not_touch_secret_files(self):
        """迁移只写 JSON，绝不触碰任何 .key secret 文件。"""
        store = ConfigStore(self.config_path)
        key_path = store.key_path
        emb_key_path = store.embedding_key_path
        rk_key_path = store.rerank_key_path

        # 模拟已有 secret 文件（任意二进制内容，迁移过程不应读取或写入）。
        key_path.write_bytes(b'CHAT-KEY-ORIGINAL')
        emb_key_path.write_bytes(b'EMBEDDING-KEY-ORIGINAL')
        rk_key_path.write_bytes(b'RERANK-KEY-ORIGINAL')

        self._write_raw({
            'version': 1,
            'embedding_enabled': True,
            'embedding_url': 'https://embed.example.com',
            'embedding_model': 'qwen3.7-text-embedding',
            'embedding_dimensions': 1024,
            'rerank_enabled': True,
            'rerank_url': 'https://rerank.example.com',
            'rerank_model': 'gte-rerank-v2',
        })

        config = store.load()

        # secret 文件字节完全不变。
        self.assertEqual(key_path.read_bytes(), b'CHAT-KEY-ORIGINAL')
        self.assertEqual(emb_key_path.read_bytes(), b'EMBEDDING-KEY-ORIGINAL')
        self.assertEqual(rk_key_path.read_bytes(), b'RERANK-KEY-ORIGINAL')
        # secret 文件的时间戳也不应改变。
        self.assertEqual(key_path.stat().st_mtime_ns, key_path.stat().st_mtime_ns)
        # 模型被清空。
        self.assertEqual(config.embedding_model, '')
        self.assertEqual(config.rerank_model, '')

    def test_load_backward_compatibility_returns_aiconfig(self):
        """旧配置加载产出合法的 AIConfig 实例。"""
        self._write_raw({
            'base_url': 'https://api.example.com',
            'model': 'qwen',
            'embedding_enabled': False,
            'embedding_url': '',
            'embedding_model': 'qwen3.7-text-embedding',
            'embedding_dimensions': 1024,
            'rerank_enabled': False,
            'rerank_url': '',
            'rerank_model': 'gte-rerank-v2',
        })

        config = ConfigStore(self.config_path).load()

        self.assertIsInstance(config, AIConfig)
        self.assertEqual(config.base_url, 'https://api.example.com')
        self.assertEqual(config.model, 'qwen')
        # 迁移后空字符串。
        self.assertEqual(config.embedding_model, '')
        self.assertEqual(config.rerank_model, '')


class TestConfigAtomicSave(unittest.TestCase):
    """配置 JSON 原子写入。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = Path(self.temp_dir) / 'config.json'

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_save_writes_current_version(self):
        """save 写入当前 CONFIG_VERSION。"""
        store = ConfigStore(self.config_path)
        store.save(AIConfig(base_url='https://api.example.com'))

        data = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertEqual(data['version'], CONFIG_VERSION)

    def test_save_writes_empty_experiment_models_by_default(self):
        """AIConfig 默认保存出空实验模型字段。"""
        store = ConfigStore(self.config_path)
        store.save(AIConfig(base_url='https://api.example.com'))

        data = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertEqual(data['embedding_model'], '')
        self.assertEqual(data['rerank_model'], '')

    def test_save_does_not_leave_temp_file(self):
        """save 后不应残留临时文件。"""
        store = ConfigStore(self.config_path)
        store.save(AIConfig(base_url='https://api.example.com'))

        tmp_path = self.config_path.with_name(self.config_path.name + '.tmp')
        self.assertFalse(tmp_path.exists())

    def test_save_overwrites_stale_temp_file(self):
        """save 会覆盖同名残留临时文件，不污染目标。"""
        store = ConfigStore(self.config_path)
        tmp_path = self.config_path.with_name(self.config_path.name + '.tmp')
        tmp_path.write_text('STALE-CONTENT', encoding='utf-8')

        store.save(AIConfig(base_url='https://api.example.com'))

        self.assertFalse(tmp_path.exists())
        data = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertEqual(data['base_url'], 'https://api.example.com')

    def test_save_creates_parent_directory(self):
        """save 在缺失目录时自动创建父目录。"""
        nested = Path(self.temp_dir) / 'sub' / 'deep' / 'config.json'
        store = ConfigStore(nested)
        store.save(AIConfig(base_url='https://api.example.com'))

        self.assertTrue(nested.exists())
        data = json.loads(nested.read_text(encoding='utf-8'))
        self.assertEqual(data['base_url'], 'https://api.example.com')

    def test_save_does_not_create_secret_files_without_keys(self):
        """save 不传 Key 时不应创建任何 .key 文件。"""
        store = ConfigStore(self.config_path)
        store.save(AIConfig(base_url='https://api.example.com'))

        self.assertFalse(store.key_path.exists())
        self.assertFalse(store.embedding_key_path.exists())
        self.assertFalse(store.rerank_key_path.exists())
        self.assertFalse(store.summary_key_path.exists())


if __name__ == '__main__':
    unittest.main()
