"""代码分析 MCP Server — 为 Agent 提供读代码、生成 diff、解析 AST 的能力。"""

import ast
import difflib
import os
import sys
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

mcp = FastMCP("code_tools")


def _resolve_path(file_path: str) -> Path:
    """将用户输入的路径解析为绝对路径。"""
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


# ── 工具 1: 读取文件片段 ──────────────────────────────────────────

@mcp.tool(description="读取指定文件的指定行范围（1-based 行号），用于查看源码内容。读项目源码用相对路径，读 workspace 产物用其相对路径。")
def read_file_range(
    file_path: Annotated[
        str,
        Field(
            description="文件路径：项目源码用相对路径（如 app/code_agent/agent/code_agent.py），不要拼到 workspace 下",
            examples=["app/code_agent/agent/code_agent.py"],
        ),
    ],
    start_line: Annotated[int, Field(description="起始行号（从 1 开始）", examples=[1])] = 1,
    end_line: Annotated[int, Field(description="结束行号（含），0 表示读到文件末尾", examples=[20])] = 0,
) -> str:
    """读取指定文件的指定行范围（1-based），end_line=0 表示读到末尾。"""
    path = _resolve_path(file_path)
    if not path.is_file():
        return f"错误：文件不存在 — {path}"

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        return f"错误：无法读取文件 — {e}"

    total = len(lines)
    start = max(1, start_line)
    end = end_line if end_line > 0 else total
    end = min(end, total)

    result = []
    for i in range(start - 1, end):
        result.append(f"{i + 1:4d}| {lines[i]}")
    return f"文件 {path.name} (共 {total} 行，显示 {start}-{end}):\n" + "\n".join(result)


# ── 工具 2: 生成 unified diff ─────────────────────────────────────

@mcp.tool(description="对比文件当前内容与 new_content，返回 unified diff 格式的变更记录，用于展示代码修改")
def generate_diff(
    file_path: Annotated[
        str,
        Field(
            description="要对比的文件路径：项目源码用相对路径（如 app/code_agent/agent/code_agent.py）",
            examples=["app/code_agent/agent/code_agent.py"],
        ),
    ],
    new_content: Annotated[str, Field(description="修改后的完整文件内容（是全文，不是 diff 片段）")],
) -> str:
    """对比文件当前内容与 new_content，返回 unified diff 格式的差异。"""
    path = _resolve_path(file_path)

    if path.is_file():
        try:
            old_lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        except Exception as e:
            return f"错误：无法读取文件 — {e}"
    else:
        old_lines = []

    new_lines = [line + "\n" for line in new_content.splitlines()]
    # 确保最后一行有换行（unified diff 要求）
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"

    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=str(path),
        tofile=str(path),
    )
    result = "".join(diff)
    return result if result.strip() else "(无变更)"


# ── 工具 3: AST 解析 ──────────────────────────────────────────────

@mcp.tool(description="解析 Python 文件的 AST，返回函数/类/导入等代码结构摘要；语法错误会明确报告")
def analyze_ast(
    file_path: Annotated[
        str,
        Field(
            description="要解析的 Python 文件路径：项目源码用相对路径（如 app/code_agent/agent/prompts.py）",
            examples=["app/code_agent/agent/prompts.py"],
        ),
    ],
) -> str:
    """解析 Python 文件的 AST，返回函数、类、导入等代码结构摘要。"""
    path = _resolve_path(file_path)
    if not path.is_file():
        return f"错误：文件不存在 — {path}"

    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        return f"语法错误：{e}"
    except Exception as e:
        return f"解析失败：{e}"

    imports: list[str] = []
    functions: list[str] = []
    classes: list[str] = []
    top_level_assignments: list[str] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = ", ".join(a.name for a in node.names)
            imports.append(f"from {module} import {names}")
        elif isinstance(node, ast.FunctionDef):
            decorators = [f"@{ast.unparse(d)}" for d in node.decorator_list]
            prefix = "\n".join(decorators) + "\n" if decorators else ""
            functions.append(f"{prefix}def {node.name}(...)  (第 {node.lineno} 行)")
        elif isinstance(node, ast.AsyncFunctionDef):
            decorators = [f"@{ast.unparse(d)}" for d in node.decorator_list]
            prefix = "\n".join(decorators) + "\n" if decorators else ""
            functions.append(f"{prefix}async def {node.name}(...)  (第 {node.lineno} 行)")
        elif isinstance(node, ast.ClassDef):
            methods = [
                m.name for m in node.body
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            classes.append(f"class {node.name}(...)  — {len(methods)} 个方法 (第 {node.lineno} 行)")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    top_level_assignments.append(f"{target.id} = ... (第 {node.lineno} 行)")

    lines = [f"文件: {path.name}"]
    if imports:
        lines.append(f"\n📦 导入 ({len(imports)}):")
        lines.extend(f"  {i}" for i in imports)
    if classes:
        lines.append(f"\n🏛️ 类 ({len(classes)}):")
        lines.extend(f"  {c}" for c in classes)
    if functions:
        lines.append(f"\n⚡ 函数 ({len(functions)}):")
        lines.extend(f"  {f}" for f in functions)
    if top_level_assignments:
        lines.append(f"\n📝 顶层赋值 ({len(top_level_assignments)}):")
        lines.extend(f"  {a}" for a in top_level_assignments)

    return "\n".join(lines)


# ── 工具 4: 项目结构扫描 ──────────────────────────────────────────

@mcp.tool(description="递归列出目录结构树，用于了解项目组织。查看项目根用 '.' 或直接给 app/ 等子目录")
def list_project_structure(
    root_path: Annotated[
        str,
        Field(
            description="要查看的目录路径：项目根用 '.'，子目录直接给相对路径（如 app/）",
            examples=[".", "app/"],
        ),
    ] = ".",
    max_depth: Annotated[int, Field(description="递归最大深度", examples=[3])] = 3,
) -> str:
    """扫描目录结构，排除 __pycache__、.git、node_modules 等常见忽略目录。"""
    root = _resolve_path(root_path)
    if not root.is_dir():
        return f"错误：目录不存在 — {root}"

    IGNORE_DIRS = {
        "__pycache__", ".git", ".idea", ".venv", "venv",
        "node_modules", ".mypy_cache", ".pytest_cache",
        ".reasonix", "runtime", ".temp", ".code",
    }

    result: list[str] = [str(root)]

    def walk(current: Path, depth: int, prefix: str = "") -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(current.iterdir(), key=lambda e: (not e.is_dir(), e.name))
        except PermissionError:
            return

        for i, entry in enumerate(entries):
            if entry.name in IGNORE_DIRS or entry.name.startswith(".") and entry.is_dir():
                continue
            is_last = (i == len(entries) - 1)
            connector = "└── " if is_last else "├── "
            result.append(f"{prefix}{connector}{entry.name}{'/' if entry.is_dir() else ''}")

            if entry.is_dir():
                extension = "    " if is_last else "│   "
                walk(entry, depth + 1, prefix + extension)

    walk(root, 0)
    return "\n".join(result)


if __name__ == "__main__":
    mcp.run(transport="stdio")
