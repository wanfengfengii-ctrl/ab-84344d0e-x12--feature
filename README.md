# X12 信封审计 API

供应链集成平台在接收合作方 X12 批次前，对**信封层级**做结构审计，拒绝截断、拼接或
计数不符的报文，避免其进入后续结算。

- 纯 Python 3 标准库实现，无第三方依赖。
- 单个 `POST /api/x12/audit` 接口，接收 `application/octet-stream` 原始 ASCII 报文（≤ 2 MiB）。
- 从定长 ISA 段读取三个分隔符：
  - 元素分隔符：ISA 第 4 个字节（偏移 3）
  - 组件分隔符：ISA 第 105 个字节（偏移 104，即 ISA16）
  - 段终止符：ISA 第 106 个字节（偏移 105）

## 审计规则

- 全报文为 ASCII，长度 1..2 MiB，必须以且仅有一个 ISA 段开头。
- 唯一的 ISA/IEA 交换；报文结束后不得再出现任何段（防拼接）。
- 交换内含 1..64 个 GS/GE 功能组；每组内含 1..500 个 ST/SE 事务集。
- 层级不得交错（GS/ST/SE/GE/IEA 必须正确嵌套）。
- 成对控制号必须一致：ST02↔SE02、GS06↔GE02、ISA13↔IEA02。
- SE01 段数必须等于 ST 到 SE 的实际段数。
- GE01 事务数、IEA01 组数必须与实际计数吻合。
- 报文必须以段终止符结束（末段缺终止符视为截断）。

段按文档顺序处理，**内层信封错误先于任何外层汇总抛出**：即使 GE01、IEA01 同时
错误，较早的 SE 段数错误仍会先返回，不会被外层汇总掩盖。

## 接口

### `POST /api/x12/audit`

成功（200）：

```json
{
  "interchange_control_number": "000000001",
  "group_count": 2,
  "transaction_count": 3,
  "sha256": "f826db39…"
}
```

失败（信封类错误 422；空报文/非 ASCII 为 400；超过 2 MiB 为 413；
Content-Type 错误为 415）：

```json
{
  "error": {
    "code": "SEGMENT_COUNT_MISMATCH",
    "message": "SE01 declares 2 segments but the ST..SE envelope spans 3",
    "segment": 5
  }
}
```

`segment` 为首个可定位错误的 1 基段序号（ISA 为 1）。

### 合作方画像（`X-Partner-Profile`）

平台与每个合作方约定了各自的 X12 标识与实施版本。请求可携带可选的
`X-Partner-Profile` 请求头选择画像；**省略该头时，状态码、响应字段、错误优先级
与报文限制完全不变**。携带该头时，服务先完成原有信封审计（信封错误优先返回），
再按报文顺序核对：交换双方（ISA05/06/07/08）→ 每个功能组（GS02/03 及组契约
GS01/GS08）→ 组内全部事务（ST01 必须在允许集合内）。

- ISA 定长字段仅去除右侧填充空格后比较；其他标识一律区分大小写、按原样比较。
- 未知画像：400 `PROFILE_NOT_FOUND`。
- 业务不匹配：422 `PROFILE_MISMATCH`，响应含 `scope`
  （`interchange` / `group` / `transaction`）与首个违规段的 1 基序号 `segment`：

```json
{
  "error": {
    "code": "PROFILE_MISMATCH",
    "message": "ST01 '856' is not allowed by the 'PO'/'005010' contract of profile 'acme-po'",
    "scope": "transaction",
    "segment": 3
  }
}
```

画像通过环境变量 `X12_PARTNER_PROFILES` 以 JSON 配置（Compose 已为 api 与 verify
注入同一份文档，见 `docker-compose.yml`）：1..16 个唯一画像，每个画像声明
ISA05/06/07/08、GS02/03，以及 1..8 条组契约（GS01、GS08 与允许的 ST01 集合）：

```json
[
  {
    "name": "acme-po",
    "isa05": "ZZ", "isa06": "ACME-SENDER",
    "isa07": "ZZ", "isa08": "HUB-RECEIVER",
    "gs02": "ACMEAPP", "gs03": "HUBAPP",
    "groups": [
      {"gs01": "PO", "gs08": "005010", "st01": ["850", "855"]},
      {"gs01": "IN", "gs08": "004010", "st01": ["810"]}
    ]
  }
]
```

配置校验规则：画像名唯一；各标识为非空 ASCII 字符串（ISA05/07 ≤ 2 字符，
ISA06/08 ≤ 15 字符）；组契约 1..8 条且 (GS01, GS08) 不重复；ST01 集合非空。
变量未设置时功能休眠（任何画像名都视为未知）；**配置非法时进程以非零码退出**。

稳定错误码：

| code | 含义 |
|---|---|
| `EMPTY_MESSAGE` / `MESSAGE_TOO_LARGE` / `NON_ASCII` | 报文体量或编码问题 |
| `MISSING_ISA` / `ISA_TOO_SHORT` / `ISA_MALFORMED` / `BAD_DELIMITER` | ISA 定长结构或分隔符非法 |
| `MULTIPLE_INTERCHANGES` | 出现第二个 ISA（拼接报文） |
| `TRAILING_DATA` | IEA 之后还有数据 |
| `MISSING_TERMINATOR` / `EMPTY_SEGMENT` | 截断或空段 |
| `NESTING_VIOLATION` | 信封层级交错 |
| `GROUP_LIMIT_EXCEEDED` / `TRANSACTION_LIMIT_EXCEEDED` | 超出 64 组 / 每组 500 事务 |
| `ZERO_GROUPS` / `ZERO_TRANSACTIONS` | 组或事务为空 |
| `ST_MALFORMED` / `SE_MALFORMED` / `GE_MALFORMED` / `IEA_MALFORMED` / `GS_MALFORMED` | 信封段缺元素或计数非数字 |
| `SEGMENT_COUNT_MISMATCH` | SE01 与 ST..SE 实际段数不符 |
| `GE_COUNT_MISMATCH` / `IEA_COUNT_MISMATCH` | GE01/IEA01 计数不符 |
| `CONTROL_NUMBER_MISMATCH` | 成对控制号不一致 |
| `MISSING_SE` / `MISSING_GE` / `MISSING_IEA` | 报文截断、缺少闭合段 |
| `UNEXPECTED_SEGMENT` | 信封段之外的段出现在事务集外 |
| `PROFILE_NOT_FOUND` | 未知的 `X-Partner-Profile` 画像（400） |
| `PROFILE_MISMATCH` | 报文与所选画像不符（422，附 `scope` 与 `segment`） |

### `GET /health`

返回 `{"status":"ok"}`，供 Docker / Compose 健康检查与 verify 服务等待就绪。

## 本地运行（无需 Docker）

```bash
python3 -m app.server                       # 默认 0.0.0.0:8080
PORT=9090 python3 -m app.server
python3 -m unittest discover -s tests       # 单元测试
```

## Docker / Docker Compose

```bash
# 宿主机端口可配置（默认 8080）
HOST_PORT=9090 docker compose up --build -d api

# 一次性验证服务：等待 api 健康后执行全部检查，以退出码汇总
docker compose run --rm verify
# 或在 CI 中：
docker compose up --build --abort-on-container-exit --exit-code-from verify verify
```

`verify` 服务依次执行：

1. 等待 `http://api:8080/health` 就绪；
2. 单元测试（unittest，85 个用例）；
3. 应用构建检查（`compileall` 字节编译）；
4. HTTP 冒烟（有效报文 + 多种损坏信封 + 传输层错误 + 画像场景：
   省略请求头的兼容行为、画像匹配、未知画像与三级 mismatch 拒绝）。

退出码按位汇总：`1` 健康超时、`2` 单元测试失败、`4` 构建检查失败、`8` HTTP 冒烟失败；
`0` 表示全部通过。

## 手工调用

```bash
curl -sS --data-binary @sample.edi \
  -H 'Content-Type: application/octet-stream' \
  http://localhost:8080/api/x12/audit

# 选择合作方画像（省略该头则仅做信封审计）
curl -sS --data-binary @sample.edi \
  -H 'Content-Type: application/octet-stream' \
  -H 'X-Partner-Profile: acme-po' \
  http://localhost:8080/api/x12/audit
```
