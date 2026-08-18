# XCore 验证（测试）说明

## 运行方式

XCore 是独立包（`XCore/pyproject.toml`），零依赖（stdlib-only）。在仓库根目录：

```bash
cd XCore
../.venv/bin/python -m pytest          # 使用仓库虚拟环境
# 或从仓库根：
PYTHONPATH=XCore .venv/bin/python -m pytest XCore/tests
```

pytest 配置（`XCore/pyproject.toml`）：`asyncio_mode = "auto"`、`pythonpath = ["."]`、
`testpaths = ["tests"]`。全部测试使用临时目录，不触碰真实数据。

## 测试文件与覆盖

| 文件 | 覆盖 |
| --- | --- |
| `test_events.py` | on/once/off/disposer、prepend/global、五派发原语返回值与顺序、bail 判定边界、通配匹配矩阵、过滤链快照、错误传播/聚合 |
| `test_services.py` | set/get/unset/has/require、Service 基类、ctx.foo 访问、isolate 作用域、服务归属与自动注销 |
| `test_plugins.py` | 三形态归一化、Registry 去重与检查、Fiber 状态机、inject 等待/唤醒、required 拓扑、失败隔离、apply 返回 disposer、配置校验错误 |
| `test_lifecycle.py` | start/stop/restart/dispose、ready/dispose 事件、逆序清理、卸载异常聚合、重复 start/stop、start 后运行期挂载 |
| `test_state.py` | 持久化往返、原子写（模拟半写临时文件）、命名空间隔离、崩溃恢复 |
| `test_schema.py` | 各类型校验、默认值合并（嵌套）、未知键拒绝、错误路径定位、optional/default |
| `test_middleware.py` | 链顺序、短路、prepend、过滤快照、错误传播 |
| `test_public_api.py` | `xcore.__all__` 与 `docs/features/api.md` 清单一致 |

## 与 XBotv2 测试纪律对齐

- 测试是可观察行为的证据：断言返回值/顺序/状态转移/落盘内容，不断言私有字段。
- 状态测试用临时目录 + 真实文件读写（验证可恢复性，而非 mock）。
- 异步语义（并发、竞态）用真实 `asyncio` 事件循环验证。
