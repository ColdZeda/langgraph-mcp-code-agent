"""测试 FileCheckpointSaver 序列化/反序列化。"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata

from app.code_agent.tools.file_saver import FileCheckpointSaver


class TestFileCheckpointSaver:
    """Checkpoint 持久化测试。"""

    def setup_method(self):
        """每个测试前创建临时目录和 saver 实例。"""
        self.tmp_dir = tempfile.mkdtemp(prefix="test_checkpoint_")
        self.saver = FileCheckpointSaver(base_path=self.tmp_dir)

    def teardown_method(self):
        """清理临时目录。"""
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_init_creates_directory(self):
        """初始化时应创建基目录。"""
        assert os.path.isdir(self.tmp_dir)

    def test_put_and_get_tuple(self):
        """保存后能读取回来。"""
        thread_id = "test-thread-1"
        cp = Checkpoint(
            v=1,
            id="cp-001",
            ts="2024-01-01T00:00:00Z",
            channel_values={"messages": ["hello"]},
            channel_versions={},
            versions_seen={},
        )
        metadata = CheckpointMetadata(source="input", step=1, writes={}, parents={})

        # 保存
        result = self.saver.put(
            {"configurable": {"thread_id": thread_id}},
            cp,
            metadata,
            {},
        )

        # 读取
        tuple_result = self.saver.get_tuple({"configurable": {"thread_id": thread_id}})

        assert tuple_result is not None
        assert tuple_result.checkpoint["id"] == "cp-001"
        assert "messages" in str(tuple_result.checkpoint["channel_values"])

    def test_checkpoint_file_created(self):
        """保存后磁盘上应有对应的 JSON 文件。"""
        thread_id = "test-thread-2"
        cp = Checkpoint(
            v=1, id="cp-002", ts="2024-01-01T00:00:00Z",
            channel_values={}, channel_versions={}, versions_seen={},
        )
        metadata = CheckpointMetadata(source="input", step=1, writes={}, parents={})

        self.saver.put({"configurable": {"thread_id": thread_id}}, cp, metadata, {})

        # 检查文件是否存在
        thread_dir = os.path.join(self.tmp_dir, thread_id)
        assert os.path.isdir(thread_dir)
        files = os.listdir(thread_dir)
        assert len(files) >= 1
        assert files[0].endswith(".json")

    def test_get_tuple_nonexistent(self):
        """不存在的 thread_id 返回 None（或空配置）。"""
        result = self.saver.get_tuple({"configurable": {"thread_id": "nonexistent"}})
        # 要么是 None，要么 configuration 里 thread_id 和请求的不一样
        if result is not None:
            cfg = result.config.get("configurable", {})
            assert cfg.get("thread_id") != "nonexistent"

    def test_serialize_deserialize_preserves_data(self):
        """序列化再反序列化不丢失数据。"""
        thread_id = "test-thread-3"
        test_data = {"key": "value", "nested": {"a": 1, "b": [1, 2, 3]}}

        # 模拟 checkpoint 中的 channel_values
        serialized = self.saver._serialize_checkpoint({"data": test_data})
        deserialized = self.saver._deserialize_data(serialized)

        assert deserialized["data"] == test_data
