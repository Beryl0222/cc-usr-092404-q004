# 文化指标口径审议

文化产业主管部门审议指标定义、来源适用性与历史序列修订。

`fixtures/metric_proposal.json` 保存一条经过脱敏的业务样例，源代码只定义读取这份样例所需的最小合同。后续模块应保持既有标识和时间含义，新增状态必须说明迁移方式。

## 测试与构建

执行测试：

```bash
python3 -m unittest discover -s tests
```

执行编译检查：

```bash
python3 -m compileall -q src tests
```
