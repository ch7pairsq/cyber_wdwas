"""C5：大文件读取策略必须留在 Worker 提示词里，且数值与工具截断上限一致。"""
from pathlib import Path
import inspect

from ctf_ext.__main__ import PROMPT_PATH
from ctf_ext.runtime.llm_worker import LlmWorker


def test_worker_prompt_states_output_budget_rules():
    text = Path(PROMPT_PATH).read_text(encoding="utf-8")
    # 输出上限的数值必须写进提示词（与 max_tool_result_bytes 一致）
    assert "4000 字节" in text
    # 定位到目标区间再取值：python_exec 匹配、只打印命中行
    assert "python_exec" in text and "print" in text
    # 反汇编按符号→地址区间→disasm_bytes 取片段，不整段读
    assert "symbols" in text and "disasm_bytes" in text
    # 侦察工具先收窄输出
    assert "min_len" in text and "length" in text
    # 同一文件不重复读
    assert "重复读" in text
    # 不能教模型去调 shell（本环境没有 shell/grep/sed 工具）
    assert "grep -n" not in text and "sed -n" not in text


def test_prompt_budget_number_matches_tool_cap():
    """提示词说 4000 字节，工具层默认上限就必须是 4000（防两处漂移）。"""
    default = inspect.signature(LlmWorker.__init__).parameters["max_tool_result_bytes"].default
    assert default == 4000
