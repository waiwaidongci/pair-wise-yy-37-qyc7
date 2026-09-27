# 空气污染源许可与合规检查

管理设施、排放口、治理设备、现场检查、整改和许可续期。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8314
```

默认端口为`8314`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`，可选`valid_from`、`valid_until`（YYYY-MM-DD或ISO时间）设置许可期限
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/renewals`：申请人按`permit_no`（原许可号）和`expected_version`（当前版本）提交续期，含新期限与`materials`
- `GET /api/items/{id}/renewals`、`GET /api/renewals?item_id={id}`：续期列表
- `GET /api/renewals/{renewal_no}?item_id={id}`：续期详情，含全部材料与历次审批意见
- `POST /api/renewals/{renewal_no}`：`action=supplement`补件再提交（沿用原编号）；`action=review`合规负责人审批（`decision=approve|return`，退回必须给`opinion`）
- `GET /api/audit`，支持`item_id`过滤

允许角色：applicant, inspector, compliance_manager, viewer。申报量超过许可量或检查发现高严重度问题时提高优先级；存在未关闭整改时不能批准。

### 续期办理规则

- 仅已批准的许可可续期；同一许可同时只有一份待审申请（submitted/returned），重复提交被拒绝并提示沿用原编号；退回后补件再提交沿用同一`renewal_no`。
- 申请人必须按原许可号和许可当前版本提交，新期限截止时间必须晚于当前时间。
- 合规负责人核对材料后批准或退回；退回必须填写意见，所有意见按时间顺序保留在续期详情中。
- 材料缺失或原许可已过期时不能批准；批准后新期限写入许可且许可版本递增，原检查记录、历次申请与审批过程仍可通过续期详情和审计链查询。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
