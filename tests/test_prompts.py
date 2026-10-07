"""测试 prompts.py 提示词构建。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.agent.prompts import (
    CLARIFY_PRINCIPLES,
    EXECUTOR_PLAN_PROMPT,
    PROMPT_CONTEXT,
    SYSTEM_PROMPT_TEMPLATE,
    build_user_prompt,
    prompt_context,
)


class TestSystemPrompt:
    """系统提示词测试。"""

    def test_contains_role_definition(self):
        """系统提示词应包含角色定义和 Plan-Execute-Verify 流程。"""
        assert "编程智能体" in SYSTEM_PROMPT_TEMPLATE or "Code Agent" in SYSTEM_PROMPT_TEMPLATE
        assert "Plan" in SYSTEM_PROMPT_TEMPLATE
        assert "Execute" in SYSTEM_PROMPT_TEMPLATE
        assert "Verify" in SYSTEM_PROMPT_TEMPLATE

    def test_has_placeholder(self):
        """模板应包含 {name} 占位符。"""
        assert "{name}" in SYSTEM_PROMPT_TEMPLATE

    def test_format_with_context(self):
        """用 `prompt_context()` 格式化后不包含原始占位符。

        ⚠️ 必须走 `prompt_context()` 而不是静态 `PROMPT_CONTEXT`（订正 #37）：
        后者**故意不含 `model_name`** —— 那个值要运行期从注册表取，
        否则切了模型提示词里还是旧名字（用户实测踩过）。
        """
        ctx = prompt_context("executor")
        formatted = SYSTEM_PROMPT_TEMPLATE.format(**ctx)
        assert "{name}" not in formatted
        assert "{model_name}" not in formatted
        assert ctx["name"] in formatted
        assert ctx["model_name"] in formatted


class TestUserPrompt:
    """用户提示词测试。"""

    def test_wraps_user_input(self):
        """用户输入应被包裹在执行要求中。"""
        result = build_user_prompt("帮我创建一个 hello.py")
        assert "帮我创建一个 hello.py" in result
        assert "Plan" in result
        assert "Execute" in result
        assert "Verify" in result

    def test_empty_input(self):
        """空输入也能生成有效提示词。"""
        result = build_user_prompt("")
        assert "用户问题" in result
        assert "执行要求" in result


class TestPromptContext:
    """PROMPT_CONTEXT 字典测试。"""

    def test_required_keys(self):
        """应包含所有必要变量。"""
        required = {
            "name",
            "workspace_dir",
            "wsl_distro",
            "vm_uploads_dir",
            "mysql_host",
            "mysql_port",
            "mysql_database",
        }
        assert required.issubset(set(PROMPT_CONTEXT.keys()))

    def test_name_is_string(self):
        assert isinstance(PROMPT_CONTEXT["name"], str)

    def test_mysql_port_is_int(self):
        assert isinstance(PROMPT_CONTEXT["mysql_port"], int)


class TestClarifyPrinciples:
    """阶段 8 · P1：候选池 §十五A 的「什么时候该先问一句」三条收敛原则。

    为什么要写进提示词：探索测试第 2 题里用户把模板占位符原样粘进来，模型**一句没问**、
    直接四处翻文件猜意图，烧了 20.8 万 token（账本 R2）。
    """

    def test_both_executor_prompts_carry_the_principles(self):
        """single（SYSTEM_PROMPT_TEMPLATE）与 multi/auto（EXECUTOR_PLAN_PROMPT）都要有。"""
        for name, template in (
            ("SYSTEM_PROMPT_TEMPLATE", SYSTEM_PROMPT_TEMPLATE),
            ("EXECUTOR_PLAN_PROMPT", EXECUTOR_PLAN_PROMPT),
        ):
            formatted = template.format(**prompt_context("executor"))
            for key in ("先问", "连续失败 2 次", "默认建议", "最多问 2 次"):
                assert key in formatted, f"{name} 少了这条原则：{key}"

    def test_principles_block_has_no_braces(self):
        """🔴 这段文本会被 `PromptTemplate.format()` 处理 ⇒ **正文里不能有 `{` `}`**。

        否则会被当成占位符：轻则 `KeyError`，重则把用户的词替换没了 ——
        这是往提示词里加字时最容易踩的坑（要举例请用「」或中括号）。
        """
        assert "{" not in CLARIFY_PRINCIPLES
        assert "}" not in CLARIFY_PRINCIPLES
        # 拼进模板后仍然能被 format（真跑一遍，而不是只检查字符）
        for template in (SYSTEM_PROMPT_TEMPLATE, EXECUTOR_PLAN_PROMPT):
            template.format(**prompt_context("executor"))

    def test_principles_keep_the_cost_judgement(self):
        """门槛那句话必须在：判据是「问一下的成本」vs「猜错重做的成本」。"""
        assert "猜错重做的成本" in CLARIFY_PRINCIPLES
        assert "不要问" in CLARIFY_PRINCIPLES, "既要会问，也要明确'什么时候别问'"
