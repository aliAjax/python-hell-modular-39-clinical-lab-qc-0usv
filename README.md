# 临床实验室质量控制与结果拦截

只使用Python标准库和SQLite的模块化服务，默认端口`8339`。覆盖检测项目、质控品批次、质控规则、允许范围、仪器校准、连续偏差、趋势、失控、结果拦截、复测、调查、批次切换和历史更正。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：QC规则计算、状态机、校准与放行约束。
- `src/repository.py`：SQLite持久化、乐观锁、幂等和审计查询。
- `src/service.py`：用例编排、权限校验和版本控制。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8339
```

## 核心对象

`assay`为检测项目，`qc_lot`为质控品批次，`instrument`为仪器，`qc_run`为质控结果，`result_batch`为患者结果批次。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `GET /api/audit`

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建。

## 质控结果更正与版本链

`qc_run`的`correct`动作（需要`reason`和新的`value`）不再就地修改，而是生成新版本：

- 旧版本保留全部历史，状态置为`superseded`，`data.superseded_by`指向新版本；新版本为`pending`，需重新`evaluate`后才能放行。
- 引用旧版本且已`released`的`result_batch`在同一事务内自动退回`waiting`，清除放行标记，并在`data.disposition_trail`留下`auto_recall`去向（原放行人、放行时间、替代版本），审计记`auto_recall`。
- 未放行（`waiting`/`intercepted`/`investigating`/`resolved`）的批次状态不变，`qc_run_id`重指向新版本，去向记为`repoint`。
- 同一仪器的更正与放行通过仪器级单调修订号仲裁：请求带`expected_revision`（`GET /api/entities/<instrument_id>`返回当前`revision`），先提交者推进修订号，落败方收到409 `ConflictError`（`instrument revision conflict ...`）。
- 更正支持`Idempotency-Key`：重试不会多生成新版本；修订号冲突发生在幂等记录落库之前，失败后可用同一键重试。
- 旧版本上的失控结论（`reject_reason`、`flags`、`findings`、`resolution`、`resolved_by`等）自动携带到新版本。
- 作废版本不参与后续质控评估的历史计算，也不能再新建或放行患者结果批次。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

质控规则为可运行的简化模型，包含1-3s、连续偏移和趋势检查，但不替代CLIA、ISO 15189、Westgard完整规则集或实验室信息系统接口。
