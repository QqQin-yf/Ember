"""Sidecar 和测试的模型占位对象；Ember 不持有对话模型循环。"""


class MockLLMClient:
    """保留 service 构造器调用方式，任何模型调用都视为接线错误。"""

    def __init__(self):
        self.calls = []

    def chat_completion(self, *args, **kwargs):
        raise AssertionError("Dialogue model calls belong to App, not Ember sidecar")
