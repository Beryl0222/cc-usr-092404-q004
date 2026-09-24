# 文化指标口径审议

文化产业主管部门审议指标定义、来源适用性与历史序列修订。

## 模块

- `contracts.py`：遗留传输信封 `DomainRecord`，只用于读取早期样例
  `fixtures/metric_proposal.json`，不承担提案校验；既有标识与时间含义不变。
- `problems.py`：可区分的问题种类（`kind` 机器可读代码）。
- `registry.py`：领域、口径、来源版本、适用地区的登记簿；其他业务域
  也登记在册，用于识别"投错门"的记录。
- `proposals.py`：提案读取入口 `ProposalEntry` / `load_proposal`。
- `council.py`：多部门收件、会审、回算与发布的持久化服务 `CouncilService`。
- `batch.py`：批量文件处理 `process_batch`。

## 提案读取与审议入口的领域边界

- 载荷必须是普通 JSON 对象（`type(x) is dict`），只有对象自身字段参与
  判断，合同之外不补默认值；同一对象内重复键直接判为问题。
- 领域、口径标识、来源版本、适用地区、统计周期、带时区时间必须相互
  一致，矛盾返回 `inconsistent`；时间缺时区返回 `naive_time`。
- 未知字段、错误领域、非法修订分别返回 `unknown_field`、`wrong_domain`、
  `invalid_revision`，调用方可直接按 `kind` 分流。
- `CouncilService.submit` 会复查领域与修订衔接（首报须为 1、不得回退、
  不得跳号、同号不得换料），即使绕过读取入口也进不了审议流程。

## 多部门收件与会审

- 定义、分母、价格口径、来源授权按口径分别版本化，互不影响。
- 完全相同的材料合并为一次收件（按材料内容哈希），各部门登记在同一
  收件单上；内容冲突时各方版本全部保留，该方面进入 `open` 状态的会审，
  同一冲突方面只保留一个进行中的会审，不重复发起。
- 会审 `rule` 裁决保留版本后，`approve_caliber` 才能让口径通过；
  存在未结会审或未决冲突时拒绝通过。

## 回算与发布

- 只有口径通过（存在 `approved` 决定）后才能 `request_backcalc`。
- 回算不改写已发布序列：任务产出影响范围（生效期之后的单元格）、
  并行的新旧结果，并沿 `APPROVAL_CHAIN`
  （division_check → council_signoff → publish_approval）逐级签署。
- `publish_run` 以追加方式发布修订值，历史版本全部保留。

## 批量与重启

- 批量文件（数组或 `{schema_version, batch_id, items}` 信封）逐条处理，
  单条失败只记录在该条目名下，不阻断其他提案。
- 服务状态落盘为 `state.json`（原子替换写入）。重启后重放同一批文件：
  相同材料识别为合并收件，不重复发起会审，不重复发布回算任务。

## 状态与迁移

- 持久化状态带 `state_version`（当前为 1）；未来结构变更必须在加载时
  完成迁移，无法迁移时以 `unsupported_state` 拒绝启动。
- 新增状态均为增量：方面 `open/ruled` 会审、口径 `approved` 决定、
  回算 `pending_approval/approved/published`。遗留样例与 `DomainRecord`
  合同不受影响，无需迁移。

## 测试与构建

执行测试：

```bash
python3 -m unittest discover -s tests
```

执行编译检查：

```bash
python3 -m compileall -q src tests
```
