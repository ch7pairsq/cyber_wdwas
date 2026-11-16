"""ctf_ext：Cairn 之上的竞赛扩展层（设计文档附录 A）。

分层：harness（唯一协议边界）/ domain（竞赛对象）/ store（扩展板与证据库）
/ runtime（调度、预算、看门狗）/ gateway（策略、工具、下载、沙箱）
/ llm（模型接入）/ skills（索引先行的技能库）。

扩展层不 import cairn：即使上游尚未 vendored，也能以 mock 模式独立运行与测试。
"""
__version__ = "0.1.0"
