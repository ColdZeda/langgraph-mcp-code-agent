from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.code_agent.config import LLM_STREAM_USAGE, MODEL_API_KEY, MODEL_BASE_URL, MODEL_NAME


def build_llm(
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> ChatOpenAI:
    """构造 ChatOpenAI 实例（模型/地址/key 可覆盖，默认取 .env 配置）。"""
    resolved_key = api_key or MODEL_API_KEY
    if not resolved_key:
        raise ValueError("模型 API key 未配置，请在 .env 中设置 LLM_API_KEY 或 MODEL_API_KEY")
    llm_kwargs = dict(
        model=model or MODEL_NAME,
        base_url=base_url or MODEL_BASE_URL,
        api_key=SecretStr(resolved_key),
        streaming=True,
    )
    # 流式响应默认不带 token usage（DeepSeek 需显式请求 include_usage），
    # evals 的 token_usage 统计依赖它；不支持的 API 可用 LLM_STREAM_USAGE=0 关闭。
    if LLM_STREAM_USAGE:
        llm_kwargs["model_kwargs"] = {"stream_options": {"include_usage": True}}
    return ChatOpenAI(**llm_kwargs)


# 模块级当前实例：REPL / evals 直接用默认配置；Web UI 通过 set_llm() 热切换
_llm = build_llm()


def get_llm() -> ChatOpenAI:
    return _llm


def set_llm(model: str | None = None, base_url: str | None = None, api_key: str | None = None) -> ChatOpenAI:
    """替换当前 llm 实例（只覆盖传入的字段）。调用方需自行重建持有 llm 的 agent。"""
    global _llm
    _llm = build_llm(model=model, base_url=base_url, api_key=api_key)
    return _llm


# 向后兼容别名（模块导入期快照；运行期热切换请用 get_llm()）
llm = _llm
