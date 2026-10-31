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
from ember.model_config import registry, ModelConfigError


GLM_ALL = {"split": "glm", "mount": "glm"}


def write_registry(td, engines=None, split_model=None):
    data=registry()
    for task,engine in (engines or {}).items():
        if task in executors.PURPOSES:
            data['purposes'][executors.PURPOSES[task]]['provider']={
                'glm':'zhipu_offline','qwen3':'ollama'}.get(engine,'unknown-provider')
    if split_model:
        data['purposes']['direction_split']['model']=split_model
    path=os.path.join(td,'models.json')
    with open(path,'w',encoding='utf-8') as stream:json.dump(data,stream)
    return path


class TestLoadExecutors(unittest.TestCase):

    def _read(self, content: str | None) -> dict:
        with tempfile.TemporaryDirectory() as td:
            try:
                engines=json.loads(content) if content is not None else {}
            except ValueError:
                path=os.path.join(td,'models.json')
                with open(path,'w',encoding='utf-8') as stream:stream.write(content)
            else:
                path=write_registry(td,engines)
            with mock.patch.dict(os.environ,{'EMBER_MODELS_CONFIG':path}):
                return executors.load_executors(td)

    def test_normal_read(self):
        out = self._read(json.dumps({"split": "qwen3",
                                     "mount": "qwen3", "observe": "glm"}))
        self.assertEqual(out, {"split": "qwen3",
                               "mount": "qwen3"})

    def test_missing_file_falls_back_to_glm(self):
        out = self._read(None)
        self.assertEqual(out, GLM_ALL)

    def test_bad_json_fails_explicitly(self):
        with self.assertRaises(ModelConfigError):self._read("{not json")

    def test_illegal_provider_fails_explicitly(self):
        with self.assertRaises(ModelConfigError):
            self._read(json.dumps({'split':'GLM','mount':'qwen3'}))

    def test_extra_tasks_ignored(self):
        out = self._read(json.dumps({"split": "glm", "judge": "qwen3", "chat": "qwen3"}))
        self.assertNotIn("chat", out)
        self.assertNotIn("judge", out)


class TestLoadEngine(unittest.TestCase):

    def test_override_wins(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "executors.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"split": "glm"}, f)
            self.assertEqual(
                executors.load_engine(td, "split", "qwen3"), "qwen3")

    def test_config_used_when_no_override(self):
        with tempfile.TemporaryDirectory() as td:
            path=write_registry(td,{'split':'qwen3'})
            with mock.patch.dict(os.environ,{'EMBER_MODELS_CONFIG':path}):
                self.assertEqual(executors.load_engine(td, "split"), "qwen3")

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
        with tempfile.TemporaryDirectory() as td:
            path=write_registry(td,split_model='glm-x')
            env={'ZHIPU_API_KEY':'k','EMBER_MODELS_CONFIG':path}
            with mock.patch.dict(os.environ,env),mock.patch('urllib.request.urlopen',self._fake_urlopen(captured)):
                out=executors.call_llm('sys','user','glm',task='split')
        self.assertEqual(out, "ok")
        self.assertEqual(captured["body"]["model"], "glm-x")       # split 的模型变量
        self.assertEqual(captured["body"]["temperature"], 0.3)     # split 的温度
        self.assertEqual(captured["timeout"], 180)                 # split 的超时

    def test_glm_model_fallback_chain(self):
        captured: dict = {}
        env = {"ZHIPU_API_KEY": "k"}   # 无任何模型变量
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("urllib.request.urlopen",
                        self._fake_urlopen(captured)):
            executors.call_llm("sys", "user", "glm", task="mount")
        self.assertEqual(captured["body"]["model"], "glm-4.7")     # 最终兜底

    def test_retired_or_unknown_task_rejected_before_any_engine(self):
        for task in ("judge", "observe", "observe_merge", "unknown"):
            for engine in ("glm", "qwen3"):
                with self.subTest(task=task, engine=engine), \
                     mock.patch("urllib.request.urlopen") as remote, \
                     mock.patch("subprocess.run") as local, self.assertRaises(ValueError):
                    executors.call_llm("sys", "user", engine, task=task)
                remote.assert_not_called()
                local.assert_not_called()

    def test_unknown_engine_raises(self):
        with self.assertRaises(ValueError):
            executors.call_llm("sys", "user", "gpt4", task="mount")


if __name__ == "__main__":
    unittest.main()
