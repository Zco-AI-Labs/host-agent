import os
import sys
import importlib.util

def load_local_tools(scripts_dir: str) -> list:
    app_dir = os.path.dirname(os.path.abspath(scripts_dir))
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    tools = []
    if not os.path.exists(scripts_dir):
        return tools
    for filename in os.listdir(scripts_dir):
        if filename.endswith(".py") and not filename.startswith("_"):
            module_name = filename[:-3]
            file_path = os.path.join(scripts_dir, filename)
            try:
                spec = importlib.util.spec_from_file_location(module_name, file_path)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    func = getattr(module, module_name, None)
                    if func and callable(func):
                        tools.append(func)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"Failed to load tool {module_name} from {file_path}: {e}", exc_info=True)
    return tools
