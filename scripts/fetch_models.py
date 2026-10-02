"""一键下载 RAG 需要的两个本地模型（向量 + 精排）。

## 为什么要有它

这两个模型**不进版本控制**（各 ≈87MB；进了 git 就永远留在历史里，删都删不掉），
项目默认「**离线优先**」：模型不在本地时，RAG 那 4 个工具会**立刻报错**并指向本脚本。

⚠️ **为什么不做"缺了自动下载"**（2026-10-02 实测后用户决定改成现在这样）：
原来那条自动下载路径实测要下 **671MB / 236 秒**（`ignore_file_pattern` 根本没滤掉
ONNX/OpenVINO 格式，而真正需要只有 87MB），而且它外面套着 `redirect_stderr(None)`，
把下载器的报错也一起堵死 —— 用户看到的是 `AttributeError: 'NoneType' object has no
attribute 'write'`，**完全看不出发生了什么**。

## 下到哪里（**不写死任何人的路径**）

`config.py` 的默认位置：`<仓库的上级目录>/embedding-model/<子目录>/<模型名>`
—— 换台机器/换个盘 clone，它自动跟着变（例：clone 到 `D:\\code\\ai-agent-test`，
模型就落在 `D:\\code\\embedding-model`）。想放别处用 `--target`，再改 `.env` 指过去。

## 用法

    uv run python scripts/fetch_models.py                    # 两个都下（已就位就跳过）
    uv run python scripts/fetch_models.py --only reranker    # 只下精排
    uv run python scripts/fetch_models.py --dry-run          # 只打印计划，不下载
    uv run python scripts/fetch_models.py --source modelscope  # hf-mirror 不通时的备选
    uv run python scripts/fetch_models.py --target D:\\models  # 自定义位置

只下**真正需要的十几个文件**（`allow_patterns` 白名单，≈87MB/个），
下完会校验"该有的文件在不在"，并打印一张汇总表。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 让脚本能直接 `uv run python scripts/fetch_models.py`（无需装成包）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.code_agent.config import EMBEDDING_MODEL_PATH  # noqa: E402

#: 默认下载根目录 = **仓库的上级目录**（与 config.py 的默认值同源，绝不写死个人路径）
DEFAULT_ROOT = EMBEDDING_MODEL_PATH.parent.parent

#: hf-mirror 是国内可直连的 HuggingFace 镜像（本机实测可用）
DEFAULT_HF_ENDPOINT = "https://hf-mirror.com"

#: 每个模型"最小必需文件集"（实测本机那份的真实清单，见 README/AGENTS 的记录）
REQUIRED_FILES: dict[str, tuple[str, tuple[str, ...]]] = {
    "embedding": (
        "sentence-transformers/all-MiniLM-L6-v2",
        (
            "model.safetensors",
            "config.json",
            "config_sentence_transformers.json",
            "modules.json",
            "sentence_bert_config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
            "vocab.txt",
            "1_Pooling/config.json",
        ),
    ),
    "reranker": (
        "cross-encoder/ms-marco-MiniLM-L-6-v2",
        (
            "model.safetensors",
            "config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
            "vocab.txt",
        ),
    ),
}


def target_dir(kind: str, root: Path | None = None) -> Path:
    """某个模型应该落在哪（默认 = 仓库上级目录下的 embedding-model/…）。"""
    repo_id, _ = REQUIRED_FILES[kind]
    return (root or DEFAULT_ROOT) / repo_id


def missing_files(kind: str, root: Path | None = None) -> list[str]:
    """返回缺失的必需文件（空列表 = 就位）。"""
    _, files = REQUIRED_FILES[kind]
    d = target_dir(kind, root)
    return [f for f in files if not (d / f).is_file()]


def total_size(path: Path) -> float:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total / 1024 / 1024


def download_with_hf(
    repo_id: str, files: tuple[str, ...], dest: Path, *, endpoint: str | None
) -> None:
    """走 huggingface_hub（可用 hf-mirror 镜像），只下白名单文件，直接落到 dest。"""
    if endpoint:
        os.environ["HF_ENDPOINT"] = endpoint
    from huggingface_hub import snapshot_download

    dest.mkdir(parents=True, exist_ok=True)
    snapshot_download(repo_id=repo_id, local_dir=str(dest), allow_patterns=list(files))


def download_with_modelscope(repo_id: str, root: Path) -> None:
    """备选通道：ModelScope（它的缓存布局恰好就是 `<cache>/<repo_id>`，与我们的目录一致）。"""
    from modelscope import snapshot_download

    snapshot_download(repo_id, cache_dir=str(root))


def fetch(kind: str, *, root: Path, source: str, dry_run: bool) -> bool:
    repo_id, files = REQUIRED_FILES[kind]
    dest = target_dir(kind, root)
    print(f"\n=== {kind} ===")
    print(f"  模型      : {repo_id}")
    print(f"  目标目录  : {dest}")
    print(f"  需要文件  : {len(files)} 个")

    miss = missing_files(kind, root)
    if not miss:
        print(f"  ✅ 已就位（{total_size(dest):.1f} MB）—— 跳过")
        return True

    print(f"  缺失      : {', '.join(miss)}")
    if dry_run:
        print("  （--dry-run：只打印计划，不下载）")
        return False

    try:
        if source == "modelscope":
            download_with_modelscope(repo_id, root)
        else:
            download_with_hf(repo_id, files, dest, endpoint=DEFAULT_HF_ENDPOINT)
    except Exception as exc:  # noqa: BLE001 —— 要把"为什么失败 + 下一步"讲清楚
        print(f"  ❌ 下载失败：{type(exc).__name__}: {str(exc)[:200]}")
        print("     可以试试：--source modelscope（换一条下载通道）")
        return False

    miss = missing_files(kind, root)
    if miss:
        print(f"  ❌ 下完了但仍缺文件：{', '.join(miss)}")
        return False
    print(f"  ✅ 完成（{total_size(dest):.1f} MB）")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="下载 RAG 需要的两个本地模型（向量 + 精排）—— 不进版本控制，用这个脚本装",
    )
    parser.add_argument(
        "--only",
        choices=["embedding", "reranker", "both"],
        default="both",
        help="只装其中一个（默认两个都装）",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=None,
        help=f"下载根目录（默认 {DEFAULT_ROOT}；改了记得同步 .env 的两个路径变量）",
    )
    parser.add_argument(
        "--source",
        choices=["hf-mirror", "modelscope"],
        default="hf-mirror",
        help="下载通道（默认 hf-mirror；连不上就换 modelscope）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不下载")
    args = parser.parse_args(argv)

    root = (args.target or DEFAULT_ROOT).resolve()
    kinds = ["embedding", "reranker"] if args.only == "both" else [args.only]

    print("RAG 本地模型安装")
    print(f"  下载根目录: {root}")
    print(f"  通道      : {args.source}")
    if root != DEFAULT_ROOT.resolve():
        print("  ⚠️ 你用了自定义目录：请在 .env 里设置")
        print("     CODE_AGENT_EMBEDDING_MODEL_PATH / CODE_AGENT_RERANKER_PATH 指到对应子目录")

    results = {k: fetch(k, root=root, source=args.source, dry_run=args.dry_run) for k in kinds}

    print("\n=== 汇总 ===")
    for kind, ok in results.items():
        dest = target_dir(kind, root)
        mark = "✅" if ok else ("（dry-run）" if args.dry_run else "❌")
        size = f"{total_size(dest):.1f} MB" if dest.exists() else "-"
        print(f"  {mark} {kind:<10} {dest}  {size}")

    if args.dry_run:
        return 0
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
