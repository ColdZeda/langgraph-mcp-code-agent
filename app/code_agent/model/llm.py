"""LLM 接入层：模型注册表 + 按角色取模型 + 降级链 + 超时。

分工（见 config/models.json 顶部说明）：
- **模型注册表 / 角色分配 / 降级链** → `config/models.json`（进版本控制）
- **密钥** → `.env` 的 `MODEL_API_KEY`（不进版本控制）

为什么要按角色分模型：Planner / Executor / Verifier / Router 的活不一样
（Router 只做二分类、Verifier 要独立判断），可以各自配不同模型；默认全用同一个，行为可预期。
"""

import asyncio
import json
import logging
from pathlib import Path

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.code_agent.config import (
    LLM_STREAM_USAGE,
    LLM_TIMEOUT,
    MODEL_API_KEY,
    MODEL_BASE_URL,
    MODEL_NAME,
    PROJECT_ROOT,
)

logger = logging.getLogger("code_agent.llm")

MODELS_CONFIG_PATH = Path(PROJECT_ROOT) / "config" / "models.json"

ROLE_NAMES = ("planner", "executor", "verifier", "router")
DEFAULT_ROLE = "executor"


def build_llm(
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float | None = None,
) -> ChatOpenAI:
    """构造 ChatOpenAI 实例（模型/地址/key/超时可覆盖，默认取 .env 配置）。"""
    resolved_key = api_key or MODEL_API_KEY
    if not resolved_key:
        raise ValueError("模型 API key 未配置，请在 .env 中设置 MODEL_API_KEY")
    llm_kwargs = dict(
        model=model or MODEL_NAME,
        base_url=base_url or MODEL_BASE_URL,
        api_key=SecretStr(resolved_key),
        streaming=True,
        timeout=timeout if timeout is not None else LLM_TIMEOUT,
    )
    # 流式响应默认不带 token usage（DeepSeek 需显式请求 include_usage），
    # evals 的 token_usage 统计依赖它；不支持的 API 可用 LLM_STREAM_USAGE=0 关闭。
    if LLM_STREAM_USAGE:
        llm_kwargs["model_kwargs"] = {"stream_options": {"include_usage": True}}
    return ChatOpenAI(**llm_kwargs)


class LLMRegistry:
    """模型注册表：按角色提供 LLM 实例，并给出该角色的降级链。

    - `roles`：角色 → 模型键（默认全用同一个模型）
    - `fallback`：角色 → 备用模型键列表（可空；非空时主力失败会自动降级）
    - 运行期可覆盖：`set_role_models()`（Web UI 下拉框）/ `override_from_spec()`（"planner=x,executor=y" 形式的 spec）
    """

    def __init__(self, config_path: Path | str = MODELS_CONFIG_PATH) -> None:
        self.config_path = Path(config_path)
        self._config = self._load_config()
        # 运行期覆盖（不改文件）：角色 → 模型键
        self._role_override: dict[str, str] = {}
        # 全局覆盖（来自 .env / Web UI 设置）
        self._api_key: str | None = None
        self._base_url: str | None = None
        # 用户自定义模型（Web 面板「我的模型」）——**自带凭据**，与上面那组全局凭据互不影响
        self._custom_models: dict[str, dict] = {}
        self._cache: dict[tuple, ChatOpenAI] = {}

    # ── 配置读取 ──
    def _load_config(self) -> dict:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as e:  # 配置缺失/损坏时退化为"单模型"，不让程序起不来
            logger.warning(
                f"读取 {self.config_path} 失败（{type(e).__name__}: {e}），回落默认单模型"
            )
            return {}
        return data if isinstance(data, dict) else {}

    @property
    def models(self) -> dict:
        return self._config.get("models") or {}

    @property
    def configured_roles(self) -> dict:
        return self._config.get("roles") or {}

    @property
    def timeout_sec(self) -> float:
        return float(self._config.get("timeout_sec") or LLM_TIMEOUT)

    # ── 角色 → 模型键 ──
    def model_key(self, role: str = DEFAULT_ROLE) -> str:
        """返回该角色当前使用的模型键（覆盖 > 配置文件 > .env 默认模型）。"""
        if role in self._role_override:
            return self._role_override[role]
        if role in self.configured_roles:
            return str(self.configured_roles[role])
        return MODEL_NAME

    def _build_for_key(self, key: str) -> ChatOpenAI:
        # ① 用户自定义模型：用它**自带**的 model / base_url / key
        #    （这样"内置用 .env 的 key、我自己加的用我自己的 key"才能共存）
        custom = self._custom_models.get(key)
        if custom:
            return build_llm(
                model=custom["model"],
                base_url=custom.get("base_url") or self._base_url,
                api_key=custom.get("api_key") or self._api_key,
                timeout=self.timeout_sec,
            )
        # ② 内置注册表：model / base_url 来自 models.json，key 用全局那一个
        spec = self.models.get(key)
        if not spec:  # 不在注册表里 → 当成"模型名"直接用（兼容 .env 单模型写法）
            return build_llm(
                model=key, base_url=self._base_url, api_key=self._api_key, timeout=self.timeout_sec
            )
        return build_llm(
            model=spec.get("model") or key,
            base_url=spec.get("base_url") or self._base_url,
            api_key=self._api_key,
            timeout=self.timeout_sec,
        )

    def get(self, role: str = DEFAULT_ROLE) -> ChatOpenAI:
        key = self.model_key(role)
        cache_key = (key, self._base_url, self._api_key, self.timeout_sec)
        if cache_key not in self._cache:
            self._cache[cache_key] = self._build_for_key(key)
        return self._cache[cache_key]

    def chain(self, role: str = DEFAULT_ROLE) -> list[ChatOpenAI]:
        """降级链：主力 + 备用（备用为空时只有主力）。"""
        keys = [self.model_key(role)]
        extra = (self._config.get("fallback") or {}).get(role) or []
        for k in extra:
            if k and k not in keys:
                keys.append(str(k))
        return [self.get_key(k) for k in keys]

    def get_key(self, key: str) -> ChatOpenAI:
        cache_key = (key, self._base_url, self._api_key, self.timeout_sec)
        if cache_key not in self._cache:
            self._cache[cache_key] = self._build_for_key(key)
        return self._cache[cache_key]

    # ── 运行期配置 ──
    def set_credentials(self, base_url: str | None = None, api_key: str | None = None) -> None:
        """更新全局凭据（Web UI 热切换用）；下次取实例时生效。"""
        if api_key is not None:
            self._api_key = api_key or None
        if base_url is not None:
            self._base_url = base_url or None
        self._cache.clear()

    def set_role_models(self, mapping: dict[str, str]) -> None:
        """按角色设置模型键（Web UI 下拉框 / `override_from_spec`）。空值表示恢复配置默认。"""
        for role, key in (mapping or {}).items():
            if role not in ROLE_NAMES:
                continue
            if key:
                self._role_override[role] = str(key)
            else:
                self._role_override.pop(role, None)
        self._cache.clear()

    def set_all_roles(self, model_key: str | None) -> None:
        """把所有角色设为同一个模型键（兼容旧的"一个模型"设置方式）。"""
        self.set_role_models(dict.fromkeys(ROLE_NAMES, model_key or ""))

    # ── 用户自定义模型（Web 面板「我的模型」）──
    def set_custom_models(self, entries: list | None) -> None:
        """登记/替换用户自定义模型。每项：`{id, label, model, base_url, api_key}`。

        ⚠️ **为什么不塞进 models.json**：那个文件进版本控制，用户的私人模型与密钥不该进去。
        自定义模型**自带凭据**：`_build_for_key` 会优先用它的 key/base_url，
        所以"内置模型用 .env 的 key、我加的模型用我自己的 key"可以共存 ——
        这也是它和"全局凭据"（`set_credentials`）的分工。

        缺字段的条目**跳过而不是报错**（配置文件被手改坏时不该让程序起不来）。
        """
        cleaned: dict[str, dict] = {}
        for item in entries or []:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or "").strip()
            model = str(item.get("model") or "").strip()
            if not cid or not model:
                continue
            cleaned[cid] = {
                "label": str(item.get("label") or "").strip() or cid,
                "model": model,
                "base_url": str(item.get("base_url") or "").strip(),
                "api_key": str(item.get("api_key") or "").strip(),
                "provider": "openai-compatible",
                "custom": True,
            }
        self._custom_models = cleaned
        self._cache.clear()

    @property
    def custom_models(self) -> dict[str, dict]:
        return {k: dict(v) for k, v in self._custom_models.items()}

    def resolve_model(self, role: str = DEFAULT_ROLE) -> dict:
        """解析某个角色**当前实际会用**的模型（给界面显示「当前生效模型」用）。

        三层兜底，与 `model_key()` / `_build_for_key()` 保持一致：
          ① 角色键在自定义模型里 → 用它的显示名 + 实际调用名；
          ② 角色键在内置注册表里 → 同上；
          ③ 都不是 → 就是「**系统默认**」（模型名 = `.env` 的 `MODEL_NAME`，`key` 为它本身）。

        ⚠️ 为什么界面要显示这个：模型是**全局配置、不随会话保存**，而且四个角色可以各不相同 ——
        不显示的话用户没法知道"现在到底是谁在干活"（2026-09-22 用户实测后提的需求）。
        """
        key = self.model_key(role)
        spec = self.all_models().get(key)
        if spec:
            return {
                "key": key,
                "label": spec.get("label") or key,
                "model": spec.get("model") or key,
                "custom": bool(spec.get("custom")),
            }
        return {"key": "", "label": "系统默认", "model": key, "custom": False}

    def effective_models(self) -> dict[str, dict]:
        """四个角色各自"当前实际会用"的模型（顶栏常驻显示 + 排查用）。"""
        return {role: self.resolve_model(role) for role in ROLE_NAMES}

    def all_models(self) -> dict[str, dict]:
        """内置注册表 + 用户自定义模型（**给下拉框当数据源**）。

        `label` 是显示名、`model` 是实际发给 API 的名字 —— 两者不同时前端会补一句
        「实际调用 xxx」（内置的 `deepseek-v4.1-flash → deepseek-flash` 就是这种情况）。
        自定义与内置**同 id 时自定义优先**（用户改过的应该生效）。

        ⚠️ **返回值里含 `api_key`（明文）** —— 这是内部结构。
        任何要发给前端的接口都必须先剥掉它（见 `app/web/server.py::list_models`）。
        """
        merged: dict[str, dict] = {}
        for key, spec in self.models.items():
            merged[key] = {
                "label": key,
                "model": spec.get("model") or key,
                "base_url": spec.get("base_url", ""),
                "provider": spec.get("provider", "openai-compatible"),
                "custom": False,
            }
        merged.update(self._custom_models)
        return merged

    def role_models(self) -> dict[str, str]:
        """当前四个角色各用什么模型键（写进运行结果）。"""
        return {role: self.model_key(role) for role in ROLE_NAMES}

    def override_from_spec(self, spec: str | None) -> dict[str, str]:
        """解析 `"planner=x,executor=y"` 形式的临时覆盖。返回生效的映射。"""
        if not spec:
            return {}
        mapping: dict[str, str] = {}
        for part in str(spec).split(","):
            if "=" not in part:
                continue
            role, key = part.split("=", 1)
            role, key = role.strip(), key.strip()
            if role in ROLE_NAMES and key:
                mapping[role] = key
        self.set_role_models(mapping)
        return mapping


# 模块级注册表（进程内单例）
registry = LLMRegistry()


def get_llm(role: str = DEFAULT_ROLE) -> ChatOpenAI:
    """按角色取 LLM（默认 executor；不带参数时等价于改造前的单例行为）。"""
    return registry.get(role)


def set_llm(
    model: str | None = None, base_url: str | None = None, api_key: str | None = None
) -> ChatOpenAI:
    """热切换：更新全局凭据；若给了 model，则把所有角色都设成它（旧语义保持兼容）。

    调用方需自行重建持有 llm 的 agent（见 app/web/server.py 的 runtime.rebuild_agents）。
    """
    registry.set_credentials(base_url=base_url, api_key=api_key)
    if model:
        registry.set_all_roles(model)
    return get_llm()


async def invoke_with_fallback(
    llms: list[ChatOpenAI], messages: list, *, timeout: float | None = None
):
    """按降级链依次调用：模型**报错**或**超时**就换下一个；全部失败才抛最后一个异常。

    ⚠️ 超时是"每个模型各自的预算"：主力太慢不会拖死整条链。
    """
    last_error: Exception | None = None
    for i, llm in enumerate(llms):
        try:
            return await asyncio.wait_for(
                llm.ainvoke(messages),
                timeout=timeout if timeout is not None else registry.timeout_sec,
            )
        except Exception as e:  # noqa: BLE001 —— 降级链要吞掉各种失败（含超时）
            last_error = e
            if i < len(llms) - 1:
                name = getattr(llm, "model_name", "?")
                logger.warning(
                    f"模型 {name} 调用失败（{type(e).__name__}: {e}），降级到下一个："
                    f"{getattr(llms[i + 1], 'model_name', '?')}"
                )
    assert last_error is not None
    raise last_error


def with_fallback(llms: list[ChatOpenAI]):
    """把降级链包成一个 Runnable（给 create_react_agent 用：模型失败自动换下一个）。"""
    if not llms:
        raise ValueError("降级链不能为空")
    if len(llms) == 1:
        return llms[0]
    return llms[0].with_fallback(llms[1:])


# 向后兼容别名（模块导入期快照；运行期热切换请用 get_llm()）
llm = get_llm()
