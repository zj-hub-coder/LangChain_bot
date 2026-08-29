"""内置工具集合（自动发现）。

tools/ 目录下每个模块里被 @tool 装饰的对象（BaseTool 实例）会被自动收集注册。
新增内置工具：在本目录新建 .py，写一个 @tool 函数即可，无需改动本文件。
"""
import importlib
import pkgutil
import sys

from langchain_core.tools import BaseTool

# tools 包自身（本模块正在初始化，但包对象已存在于 sys.modules）
_pkg = sys.modules[__name__]


def _discover_tools() -> list[BaseTool]:
    """扫描 tools 包下所有非下划线开头的模块，收集 BaseTool 实例。"""
    discovered: list[BaseTool] = []
    for mod_info in pkgutil.iter_modules(_pkg.__path__):
        if mod_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{_pkg.__name__}.{mod_info.name}")
        for attr_name, attr in vars(module).items():
            if attr_name.startswith("_"):
                continue
            # @tool 装饰后得到的是 BaseTool 实例；排除重复与导入进来的类本身
            if isinstance(attr, BaseTool) and attr not in discovered:
                discovered.append(attr)
    return discovered


def get_builtin_tools() -> list[BaseTool]:
    """返回自动发现的全部内置工具。"""
    return _discover_tools()


__all__ = ["get_builtin_tools"]
