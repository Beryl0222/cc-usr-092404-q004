# 文化指标口径审议

文化产业主管部门审议指标定义、来源适用性与历史序列修订。

`fixtures/metric_proposal.json` 保存一条经过脱敏的业务样例。合同 v1（仅有
`schema_version/record_id/domain/occurred_at/revision/source` 六个字段）已升级为
**v2 严格合同**：读取入口只接受普通 JSON 对象自身的标量字段，并强制领域、口径
标识、来源版本、适用地区、统计周期与带时区时间相互一致。

## 领域边界与可区分错误

`metric_council.contracts.parse_proposal` / `load_proposal` 是提案读取入口，
`MetricCouncil.intake` 是审议系统收件入口。两层都会拒收“record_id 合法但
domain 写成其他业务”的文件，错误类型彼此可区分：

| 异常 | 触发情形 |
| --- | --- |
| `PayloadShapeError` | 顶层不是普通 JSON 对象（数组/标量/null），或字段值是嵌套对象/数组；JSON 无法解析 |
| `UnknownFieldError` | 出现合同之外的未知字段（`exc.unknown` 列出字段名） |
| `SchemaVersionError` | `schema_version` 非整数或版本不受支持 |
| `IllegalRevisionError` | `revision` 非正整数、回退、跳跃，或同一修订号携带不同内容重放 |
| `DomainMismatchError` | `domain != metric_council`；口径标识无 `cul_` 前缀；来源未归属文化域 |
| `InconsistentRecordError` | 来源/授权版本号、地区、价格口径与基准年、统计周期与带时区时间等不自洽 |

一致性要点：

- 口径标识必须为 `cul_` 前缀的小写蛇形代码；来源必须以 `文化` 或 `cul:` 归属
  文化域，防止其他业务条线来源被接入历史序列；
- 地区必须是 `CN` 或文化统计白名单内的 `CN-XX` 省级代码；
- 时间必须显式携带时区偏移；该瞬时在统一报送时区（UTC+8）与 UTC 下必须归属
  同一统计周期，跨月/季/年边界的瞬时要求澄清归属期后重报；
- 可比价（`constant`）必须给出 `constant_base_year`，名义价不得携带该字段；
- 来源授权以扁平自身字段编码：`"授权机构/文件号@版本"`。

v1 旧文件经 `load_record` 读取时同样走严格校验；缺少 v2 一致性字段会抛
`InconsistentRecordError`，须由提交方补齐后重报（系统不替业务方臆造口径、
地区与周期）。

## 收件、版本化与会审

`metric_council.pipeline.MetricCouncil`（状态由 `JsonStore` 原子落盘）：

- **四方版本化**：同一 `caliber_id` 被多部门提交时，定义、分母、价格口径、
  来源授权四个方面各自独立取号留痕（`component_versions` 可查各方版本）；
- **相同材料合并**：跨部门/跨记录提交的完全相同材料合并为一次收件
  （`IntakeOutcome.MERGED`），共同提交方合并登记；同一记录的后续修订号即使
  材料未变也是新收件事件；
- **冲突待会审**：任一方面出现两个以上不同版本，口径进入 `conflict`，
  `convene` 自动发起**会审**（`ReviewKind.JOINT`），裁决必须逐方面选定版本，
  禁止带着冲突默认通过；无冲突则为普通审议；
- **门禁**：审议先 `convene` 后 `decide`；通过前 `publish_recalculation` 一律
  拒绝；已通过/驳回口径不得重复发起审议，变更走修订流程。

## 修订与历史序列

- 历史序列只追加：`publish_series_point` 对已存在期间拒绝改写；
- `open_amendment` 按修订生成**影响范围**（受影响序列与期间、点数，全国修订
  覆盖省级点），并初始化有序**审批链**：文化统计处复核 → 来源机构会签 →
  分管局领导审批，必须逐级签署，不得跳级/重复签署；
- 审批链全部完成后 `publish_recalculation` 才能发布，产出**新旧并行**结果
  快照（`old_results` / `new_results`，新结果标记候选修订号），已发布序列保持
  原样；任务按“口径 + 修订号”幂等。

## 批量隔离与重启幂等

- `ingest_batch(paths)` 逐文件隔离：单个文件的合同错误或 IO 错误只产生一条
  `REJECTED` 结果（带 `error_type`/`error_message`），不阻断其他提案；
- 所有状态经临时文件 + `os.replace` 原子写入。重启后用同一状态文件重建
  `MetricCouncil`：同一 `record_id+revision` 重放判为 `DUPLICATE`，会审不会
  重复发起，回算任务不会重复发布。

## 测试与构建

执行测试：

```bash
python3 -m unittest discover -s tests
```

执行编译检查：

```bash
python3 -m compileall -q src tests
```
