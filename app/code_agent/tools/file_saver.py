import os
import pickle
from pathlib import Path
from typing import Optional, Sequence, Any

import json
from langgraph.prebuilt import create_react_agent
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple, Checkpoint, CheckpointMetadata, \
    ChannelVersions


from app.code_agent.model.llm import llm

from app.code_agent.tools.file_tools import file_tools
from app.code_agent.config import CHECKPOINT_DIR


#values 褰撳墠鍐呭瓨閲屽畬鏁寸殑鍏ㄩ儴鐘舵€?  updates 杩欎竴姝ュ垰鏂板� / 淇�敼鐨勫唴瀹?

class FileCheckpointSaver(BaseCheckpointSaver[str]):
    def __init__(self, base_path: str | os.PathLike[str] = CHECKPOINT_DIR):
        super().__init__()
        self.base_path = str(base_path)

        os.makedirs(self.base_path, exist_ok=True) #鏂囦欢瀛樺湪鏃跺垱寤猴紝涓嶆姤閿?

    def _get_checkpoint_path(self,thread_id,checkpoint_id):
        dir_path = os.path.join(self.base_path, thread_id)
        os.makedirs(dir_path, exist_ok=True)
        file_path = os.path.join(dir_path,checkpoint_id+".json")
        return file_path

    def _serialize_checkpoint(self,data) -> str:
        import pickle,base64
        pickled = pickle.dumps(data)

        return base64.b64encode(pickled).decode()

    def _deserialize_data(self,data):
        import base64
        decoded = base64.b64decode(data)
        return pickle.loads(decoded)


    def get_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        """Fetch a checkpoint tuple using the given configuration.

        Args:
            config: Configuration specifying which checkpoint to retrieve.

        Returns:
            Optional[CheckpointTuple]: The requested checkpoint tuple, or None if not found.
        """
        #1.鎵惧埌姝ｇ‘鐨刢heckpoint鏂囦欢璺�緞
        thread_id = config["configurable"]["thread_id"]
        # checkpoint_id = config["configurable"].get["checkpoint_id"]

        #2.璇诲彇checkpoint鏂囦欢鍐呭�
        dir_path = os.path.join(self.base_path, thread_id)
        checkpoint_files = list(Path(dir_path).glob("*.json"))
        checkpoint_files.sort(key=lambda x:x.stem,reverse=True)
        if len(checkpoint_files) > 0:
            latest_checkpoint = checkpoint_files[0]
            checkpoint_id = latest_checkpoint.stem
            checkpoint_file_path = self._get_checkpoint_path(thread_id,checkpoint_id)

            #3.瀵规枃浠跺唴瀹硅繘琛屽弽搴忓垪鍖?
            with open(checkpoint_file_path,"r",encoding="utf-8") as checkpoint_files:
                data = json.load(checkpoint_files)

            checkpoint = self._deserialize_data(data["checkpoint"])
            metadata = self._deserialize_data(data["metadata"])




            #4.杩斿洖checkpoint瀵硅薄
            return CheckpointTuple(
                config={
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_id": checkpoint_id,
                    }
                },
                checkpoint=checkpoint,
                metadata=metadata,
            )
        else:
            return None

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> (RunnableConfig):
        """Store a checkpoint with its configuration and metadata.

        Args:
           config: Configuration for the checkpoint.
           checkpoint: The checkpoint to store.
           metadata: Additional metadata for the checkpoint.
           new_versions: New channel versions as of this write.

        Returns:
           RunnableConfig: Updated configuration after storing the checkpoint.
        """
        # 1.鐢熸垚瀛樺偍鐨凧SON鏂囦欢璺�緞
        thread_id = config["configurable"]["thread_id"]
        # dir_path = os.path.join(self.base_path, thread_id) #灏佽�鍦ㄤ笂闈�负鍐呴儴鏂规硶
        # os.makedirs(dir_path, exist_ok=True)
        # file_path = os.path.join(dir_path,checkpoint_id)

        checkpoint_id = checkpoint["id"]
        checkpoint_path = self._get_checkpoint_path(thread_id,checkpoint_id)

        # 2. 灏咰heckpoint杩涜�搴忓垪鍖?
        checkpoint_data = {
            "checkpoint": self._serialize_checkpoint(checkpoint),
            "metadata": self._serialize_checkpoint(metadata),
        }

        # 3.灏咰heckpoint瀛樺偍鍒版枃浠剁郴缁?
        with open(checkpoint_path, "w",encoding="utf-8") as f:
            json.dump(checkpoint_data, f,indent=2,ensure_ascii=False)



        # 4.鐢熸垚杩斿洖鍊?
        return {
            "configuration": {
                "thread_id": thread_id,
                "checkpoint_id": checkpoint_id,
            }
        }


    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Store intermediate writes linked to a checkpoint.

        Args:
            config: Configuration of the related checkpoint.
            writes: List of writes to store.
            task_id: Identifier for the task creating the writes.
            task_path: Path of the task creating the writes.
       """
        return self

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Asynchronously fetch a checkpoint tuple using the given configuration.

        Args:
            config: Configuration specifying which checkpoint to retrieve.

        Returns:
            Optional[CheckpointTuple]: The requested checkpoint tuple, or None if not found.

        Raises:
            NotImplementedError: Implement this method in your custom checkpoint saver.
        """
        return self.get_tuple(config)

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Asynchronously store a checkpoint with its configuration and metadata.

        Args:
            config: Configuration for the checkpoint.
            checkpoint: The checkpoint to store.
            metadata: Additional metadata for the checkpoint.
            new_versions: New channel versions as of this write.

        Returns:
            RunnableConfig: Updated configuration after storing the checkpoint.

        Raises:
            NotImplementedError: Implement this method in your custom checkpoint saver.
        """
        return self.put(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Asynchronously store intermediate writes linked to a checkpoint.

        Args:
            config: Configuration of the related checkpoint.
            writes: List of writes to store.
            task_id: Identifier for the task creating the writes.
            task_path: Path of the task creating the writes.

        Raises:
            NotImplementedError: Implement this method in your custom checkpoint saver.
        """
        return self.put_writes(config, writes, task_id, task_path)



if __name__ == "__main__":
    memory = FileCheckpointSaver()
    agent = create_react_agent(
        model=llm,
        tools=file_tools,
        checkpointer=memory,
        debug=False,
    )

    config = RunnableConfig(configurable={"thread_id":2})

    while True:
        user_input = input("鐢ㄦ埛: ")

        if user_input == "exit":
            break


        resp = agent.invoke(input={"messages":user_input},config=config)
        print("鍔╃悊:", resp['messages'][-1].content)
        print()
