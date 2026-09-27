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
- `POST /api/items`（可携带`valid_from`/`valid_until`许可期限，格式`YYYY-MM-DD`）
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/renewals`，申请人提交续期（新期限`valid_until`与`materials`材料清单）
- `GET /api/items/{id}/renewals`，查看某许可的历次续期申请
- `GET /api/renewals`，查看全部续期申请
- `GET /api/renewals/{id}`，续期详情，含历次提交/退回/补件/批准意见
- `POST /api/renewals/{id}/resubmit`，退回后按原编号补件再提交，必须提交`expected_version`
- `POST /api/renewals/{id}/decision`，合规负责人`approve`或`return`（退回必须填`comment`），必须提交`expected_version`
- `GET /api/audit`

允许角色：applicant, inspector, compliance_manager, viewer。申报量超过许可量或检查发现高严重度问题时提高优先级；存在未关闭整改时不能批准。

## 许可续期规则

- 仅`approved`状态的许可可办理续期；续期沿用原许可号，申请编号为`{许可号}-R{序号}`。
- 同一许可同时只有一份待审（含退回待补件）申请，重复提交返回冲突并沿用原编号补件再提交。
- 材料缺失或原许可已过期时不能批准；批准后新期限写入许可，原检查记录与续期办理过程仍可查。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
