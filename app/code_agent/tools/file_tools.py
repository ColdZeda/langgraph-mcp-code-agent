from langchain_community.agent_toolkits.file_management import FileManagementToolkit

from app.code_agent.config import WORKSPACE_DIR

WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
file_tools = FileManagementToolkit(root_dir=str(WORKSPACE_DIR)).get_tools()
