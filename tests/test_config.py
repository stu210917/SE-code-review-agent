# -*- coding: utf-8 -*-
"""配置加载测试：优先级、类型转换、校验与脱敏。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codereview.config import Config
from codereview.errors import ConfigError


class ConfigTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.missing_dotenv = self.root / "no-such.env"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def from_env(self, env: dict[str, str] | None = None, **overrides) -> Config:
        overrides.setdefault("workspace", self.root)
        return Config.from_env(env=env or {}, dotenv_path=self.missing_dotenv, **overrides)


class EnvParsingTests(ConfigTestCase):
    def test_prefixed_variables_are_read(self) -> None:
        config = self.from_env({
            "CR_BASE_URL": "https://api.deepseek.com/v1",
            "CR_API_KEY": "sk-test-key-1234567890",
            "CR_MODEL": "deepseek-chat",
            "CR_MAX_STEPS": "7",
            "CR_TEMPERATURE": "0.7",
            "CR_ALLOW_EXEC": "true",
        })
        self.assertEqual(config.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(config.model, "deepseek-chat")
        self.assertEqual(config.max_steps, 7)
        self.assertAlmostEqual(config.temperature, 0.7)
        self.assertTrue(config.allow_exec)

    def test_openai_standard_variables_are_supported(self) -> None:
        config = self.from_env({"OPENAI_API_KEY": "sk-x", "OPENAI_BASE_URL": "http://localhost:11434/v1"})
        self.assertEqual(config.api_key, "sk-x")
        self.assertEqual(config.base_url, "http://localhost:11434/v1")

    def test_prefixed_variable_wins_over_standard_one(self) -> None:
        config = self.from_env({"CR_API_KEY": "prefixed", "OPENAI_API_KEY": "standard"})
        self.assertEqual(config.api_key, "prefixed")

    def test_invalid_value_raises_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            self.from_env({"CR_MAX_STEPS": "不是数字"})

    def test_boolean_parsing_accepts_common_forms(self) -> None:
        for raw, expected in (("1", True), ("yes", True), ("off", False), ("False", False)):
            self.assertEqual(self.from_env({"CR_MOCK": raw}).mock, expected)

    def test_invalid_boolean_raises(self) -> None:
        with self.assertRaises(ConfigError):
            self.from_env({"CR_MOCK": "maybe"})


class DotenvTests(ConfigTestCase):
    def test_dotenv_file_is_parsed(self) -> None:
        path = self.root / ".env"
        path.write_text(
            "# 注释行\n"
            "export CR_MODEL=qwen-plus\n"
            "CR_API_KEY=\"quoted-key-value\"\n"
            "CR_MAX_STEPS=5\n"
            "CR_ALLOW_EXEC=yes\n",
            encoding="utf-8",
        )
        config = Config.from_env(env={}, dotenv_path=path, workspace=self.root)
        self.assertEqual(config.model, "qwen-plus")
        self.assertEqual(config.api_key, "quoted-key-value")
        self.assertEqual(config.max_steps, 5)
        self.assertTrue(config.allow_exec)

    def test_env_variable_overrides_dotenv(self) -> None:
        path = self.root / ".env"
        path.write_text("CR_MODEL=from-dotenv\n", encoding="utf-8")
        config = Config.from_env(env={"CR_MODEL": "from-env"}, dotenv_path=path, workspace=self.root)
        self.assertEqual(config.model, "from-env")

    def test_override_argument_wins_over_everything(self) -> None:
        path = self.root / ".env"
        path.write_text("CR_MODEL=from-dotenv\n", encoding="utf-8")
        config = Config.from_env(
            env={"CR_MODEL": "from-env"}, dotenv_path=path, workspace=self.root, model="from-cli"
        )
        self.assertEqual(config.model, "from-cli")


class DerivedPropertyTests(ConfigTestCase):
    def test_mock_is_forced_without_api_key(self) -> None:
        config = self.from_env({})
        self.assertTrue(config.effective_mock)

    def test_live_mode_with_api_key(self) -> None:
        config = self.from_env({"CR_API_KEY": "sk-real"})
        self.assertFalse(config.effective_mock)

    def test_explicit_mock_flag_wins(self) -> None:
        config = self.from_env({"CR_API_KEY": "sk-real"}, mock=True)
        self.assertTrue(config.effective_mock)

    def test_chat_completions_url_has_no_double_slash(self) -> None:
        config = self.from_env({"CR_BASE_URL": "https://api.openai.com/v1/"})
        self.assertEqual(config.chat_completions_url, "https://api.openai.com/v1/chat/completions")

    def test_api_key_is_masked_in_describe(self) -> None:
        config = self.from_env({"CR_API_KEY": "sk-abcdefghijklmnop"})
        described = config.describe()
        self.assertNotIn("abcdefghijklmnop", str(described))
        self.assertIn("*", described["api_key"])

    def test_empty_key_is_reported_as_unset(self) -> None:
        self.assertEqual(self.from_env({}).describe()["api_key"], "(未设置)")


class ValidationTests(ConfigTestCase):
    def test_workspace_must_exist(self) -> None:
        with self.assertRaises(ConfigError):
            Config.from_env(env={}, dotenv_path=self.missing_dotenv, workspace=self.root / "nope")

    def test_invalid_values_are_rejected(self) -> None:
        for overrides in (
            {"max_steps": 0},
            {"max_retries": -1},
            {"temperature": 5.0},
            {"timeout": 0},
            {"memory_token_budget": 100},
        ):
            with self.assertRaises(ConfigError):
                Config.from_env(env={}, dotenv_path=self.missing_dotenv, workspace=self.root, **overrides)

    def test_with_overrides_returns_copy(self) -> None:
        config = self.from_env({})
        changed = config.with_overrides(model="new-model", max_steps=None)
        self.assertNotEqual(config.model, "new-model")     # 原对象不受影响
        self.assertEqual(changed.model, "new-model")
        self.assertEqual(changed.max_steps, config.max_steps)  # None 表示「不覆盖」


if __name__ == "__main__":
    unittest.main()
