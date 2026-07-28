import pytest
from config_manager import deep_update

def test_shallow_update():
    base = {"app_name": "OpenClaw", "version": "1.0"}
    update = {"version": "1.1", "debug": True}
    expected = {"app_name": "OpenClaw", "version": "1.1", "debug": True}
    assert deep_update(base, update) == expected

def test_nested_deep_update():
    base = {
        "db": {"host": "localhost", "port": 5432},
        "env": "dev"
    }
    update = {
        "db": {"port": 5433}, # 期望只更新 port，保留 host
        "env": "prod"
    }
    expected = {
        "db": {"host": "localhost", "port": 5433},
        "env": "prod"
    }
    # 这个断言会失败，因为现有的代码会把 db 整个替换成 {"port": 5433}
    assert deep_update(base, update) == expected

# 新增到 test_config.py 末尾
def test_deep_nested_with_list():
    """三层嵌套 + 列表覆盖场景"""
    base = {
        "a": {
            "b": {
                "c": [1, 2, 3],
                "d": "old"
            }
        }
    }
    update = {
        "a": {
            "b": {
                "c": [4, 5],
                "e": "new"
            }
        }
    }
    expected = {
        "a": {
            "b": {
                "c": [4, 5],    # 列表应直接覆盖，而非递归合并
                "d": "old",
                "e": "new"
            }
        }
    }
    assert deep_update(base, update) == expected

def test_update_with_none():
    base = {
        "x": {"y": 1, "z": None},
        "w": None
    }
    update = {
        "x": {"z": 2},
        "w": {"new": 3}   # 原值 None 应被新字典覆盖
    }
    expected = {
        "x": {"y": 1, "z": 2},
        "w": {"new": 3}
    }
    assert deep_update(base, update) == expected