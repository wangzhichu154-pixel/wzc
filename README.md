# wzc — 美股日内做市低延迟交易系统

| 内容 | 路径 |
|---|---|
| 系统设计文档（架构评审稿 v0.1） | [`docs/design/hft-market-making-system-design.md`](docs/design/hft-market-making-system-design.md) |
| 热路径参考实现 + 单元测试 + 微基准（非生产代码） | [`docs/design/reference/`](docs/design/reference/) |

```bash
cd docs/design/reference
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j
./build/test_oms && ./build/test_bench        # expect "ALL OK"
python3 houmen_null_test.py                    # Appendix A.3 null-hypothesis test
```

文档只讨论工程实现与风险控制，不对任何策略的盈利能力做承诺。
