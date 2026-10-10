# -*- coding: utf-8 -*-
"""test_executors — ember/executors.py 的确定性测试（不调真实 LLM）。

覆盖：
1. load_executors：正常读取 / 缺文件回落 / 坏 JSON 回落 /
   非法值回落 / 多余任务点忽略；
2. load_engine：--engine 显式覆盖 > 配置文件 > glm 兜底；
3. call_llm：glm 分支请求构造（mock urlopen 验证 modelenv/
   temperature/timeout 按 task 区分）；未知 engine 报错。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from ember import executors  # noqa: E402


GLM_ALL = {"judge": "glm", "split": "glm", "mount": "glm", "summary": "glm"}


class TestLoadExecutors(unittest.TestCase):

    def _read(self, content: str | None) -> dict:
        with tempfile.TemporaryDirectory() as td:
            if content is not None:
                with open(os.path.join(td, "executors.json"), "w",
                          encoding="utf-8") as f:
                    f.write(content)
            return executors.load_executors(td)

    def test_normal_read(self):
        out = self._read(json.dumps({"judge": "qwen3", "split": "glm",
                                     "mount": "qwen3", "summary": "glm"}))
        self.assertEqual(out, {"judge": "qwen3", "split": "glm",
                               "mount": "qwen3", "summary": "glm"})

    def test_missing_file_falls_back_to_glm(self):
        out = self._read(None)
        self.assertEqual(out, GLM_ALL)

    def test_bad_json_falls_back_to_glm(self):
        out = self._read("{not json")
        self.assertEqual(out, GLM_ALL)

    def test_illegal_value_falls_back_per_task(self):
        out = self._read(json.dumps({"judge": "gpt4", "split": "GLM",
                                     "mount": "qwen3"}))
        self.assertEqual(out["judge"], "glm")   # 未知 engine → glm
        self.assertEqual(out["split"], "glm")   # 大小写敏感 → glm
        self.assertEqual(out["mount"], "qwen3") # 合法值保留
        self.assertEqual(out["summary"], "glm") # 缺项 → glm

    def test_extra_tasks_ignored(self):
        out = self._read(json.dumps({"judge": "glm", "chat": "qwen3"}))
        self.assertNotIn("chat", out)
        self.assertEqual(out["judge"], "glm")


class TestLoadEngine(unittest.TestCase):

    def test_override_wins(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "executors.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"judge": "glm"}, f)
            self.assertEqual(
                executors.load_engine(td, "judge", "qwen3"), "qwen3")

    def test_config_used_when_no_override(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "executors.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"judge": "qwen3"}, f)
            self.assertEqual(executors.load_engine(td, "judge"), "qwen3")

    def test_missing_config_defaults_glm(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(executors.load_engine(td, "mount"), "glm")


class TestCallLLM(unittest.TestCase):

    def _fake_urlopen(self, captured: dict):
        """构造 mock urlopen：记录请求，返回形如 OpenAI 的响应。"""
        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {"choices": [{"message": {"content": "ok"}}]}).encode()

        def _open(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResp()

        return _open

    def test_glm_task_specs_applied(self):
        captured: dict = {}
        env = {"ZHIPU_API_KEY": "k", "EMBER_SPLIT_MODEL": "glm-x"}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch("urllib.request.urlopen",
                        self._fake_urlopen(captured)):
            out = executors.call_llm("sys", "user", "glm", task="split")
        self.assertEqual(out, "ok")
        self.assertEqual(captured["body"]["model"], "glm-x")       # split 的模型变量
        self.assertEqual(captured["body"]["temperature"], 0.3)     # split 的温度
        self.assertEqual(captured["timeout"], 180)                 # split 的超时

    def test_glm_model_fallback_chain(self):
        captured: dict = {}
        env = {"ZHIPU_API_KEY": "k"}   # 无任何模型变量
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch("urllib.request.urlopen",
                        self._fake_urlopen(captured)):
            executors.call_llm("sys", "user", "glm", task="judge")
        self.assertEqual(captured["body"]["model"], "glm-4.7")     # 最终兜底

    def test_unknown_engine_raises(self):
        with self.assertRaises(ValueError):
            executors.call_llm("sys", "user", "gpt4", task="judge")


if __name__ == "__main__":
    unittest.main()
