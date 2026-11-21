# wzc — 日内做市低延迟交易系统设计

| 内容 | 路径 |
|---|---|
| **A 股 + 华泰证券 API 版设计文档（当前主线，v0.2）** | [`docs/design/ashare-huatai-market-making-system-design.md`](docs/design/ashare-huatai-market-making-system-design.md) |
| 美股版设计文档（v0.1，通用内核部分仍被 A 股版引用） | [`docs/design/hft-market-making-system-design.md`](docs/design/hft-market-making-system-design.md) |
| 热路径参考实现 + 单元测试 + 微基准（非生产代码） | [`docs/design/reference/`](docs/design/reference/) |

```bash
cd docs/design/reference
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j
./build/test_ashare          # A 股组件：L2 订单簿重建（深/沪语义）、T+1、费用、合规预算、事前风控
./build/test_bench           # 通用热路径组件
./build/test_oms
python3 houmen_null_test.py  # 厚门模型零假设检验
```

文档只讨论工程实现、合规与风险控制，不对任何策略的盈利能力做承诺。
