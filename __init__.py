"""ComfyUI-CharNameSave — package entry point.

Registers the "Save Image by Char Tag" node. The implementation lives in the
``charnamesave_core`` subpackage; this module only re-exports the historical
public/internal names so existing tests and extensions keep working unchanged.

Import strategy: ComfyUI loads this file as a *package* (relative imports work),
while the unit tests import it as a top-level ``__init__`` module (absolute
imports work). The try/except below supports both without touching ``sys.path``.
"""

try:  # ComfyUI: loaded as a package (submodule_search_locations set) -> relative
    from .charnamesave_core import *  # noqa: F401,F403
    from . import charnamesave_core as _core
except ImportError:  # tests / direct execution: plugin dir is on sys.path -> absolute
    from charnamesave_core import *  # noqa: F401,F403
    import charnamesave_core as _core


# 模块加载时加载一次角色数据集：文件缺失/损坏只会打 warning，索引为空时自动
# 回退到原有的 "name (series)" 识别逻辑，不影响插件运行。
_load_character_index()

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
