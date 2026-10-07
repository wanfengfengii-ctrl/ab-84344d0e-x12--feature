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

## 合作方画像（partner profile）

信封结构合法并不代表投递正确：发给某合作方的批次可能身份错投，或包含未约定的
事务类型。审计接口通过可选的 `X-Partner-Profile` 请求头选择画像，在**原有信封
审计全部通过之后**再核对业务身份；任一层不符都返回 422 `PROFILE_MISMATCH`，
批次不得进入结算。

- 省略 `X-Partner-Profile`：行为与原先完全一致（状态码、响应字段、错误优先级、
  2 MiB 报文限制均不变）。
- 画像在进程启动时由环境变量 `X12_PARTNER_PROFILES`（JSON 数组，1..16 个唯一
  画像）加载；变量缺省或为空白时画像功能关闭。配置非法时进程打印错误并以非零
  状态退出，服务不会带着坏配置启动。
- 头中画像名不存在：返回 400 `PROFILE_NOT_FOUND`（该检查先于信封审计，因此即使
  报文为空或已损坏，仍返回此错误）。

每个画像声明：

| 字段 | 含义 |
|---|---|
| `name` | 唯一画像名，即 `X-Partner-Profile` 头的取值 |
| `ISA05` / `ISA06` | 发送方限定符 / 标识（ISA 定长字段） |
| `ISA07` / `ISA08` | 接收方限定符 / 标识（ISA 定长字段） |
| `GS02` / `GS03` | 应用发送方 / 接收方代码，对该画像所有功能组生效 |
| `groups` | 1..8 个组契约，每项为 `GS01`、`GS08` 与允许的 `ST01` 集合 |

每个组契约：

- `GS01`：功能标识码（如 `PO`、`FA`）；
- `GS08`：版本/发布/行业标识（如 `005010`）；
- `ST01`：允许的事务集标识码数组（1..8 个，组内唯一）；
- 同一画像内 `GS01`+`GS08` 组合不得重复。

身份比较规则：ISA05..08 是定长字段，**只去除右侧填充空格**，前导空格和大小写
都敏感；GS01/GS02/GS03/GS08/ST01 原样逐字节比较，区分大小写。

选择画像后，按报文顺序核对：交换双方（ISA05/06/07/08，段 1）→ 每个功能组的
GS02/GS03 及其 `GS01`+`GS08` 契约 → 组内每个 ST01 是否属于该契约允许集合。
错误响应给出 `scope`（`interchange` / `group` / `transaction`）与首个违规段序号：

```json
{
  "error": {
    "code": "PROFILE_MISMATCH",
    "message": "ST01 '997' is not allowed for GS01/GS08 'PO'/'005010' in profile 'acme'",
    "scope": "transaction",
    "segment": 3
  }
}
```

请求处理顺序：传输层检查（Content-Type 415、报文超 2 MiB 413 等，规则不变）
→ 画像选择（未知画像 400 `PROFILE_NOT_FOUND`）→ 原有信封审计（400/422）→
画像匹配（422 `PROFILE_MISMATCH`，信封合法但身份越界）。

`X12_PARTNER_PROFILES` 示例：

```json
[
  {
    "name": "acme",
    "ISA05": "ZZ", "ISA06": "ACME",
    "ISA07": "ZZ", "ISA08": "PLATFORM",
    "GS02": "ACME", "GS03": "PLATFORM",
    "groups": [
      {"GS01": "PO", "GS08": "005010", "ST01": ["850", "855"]},
      {"GS01": "FA", "GS08": "005010", "ST01": ["997"]}
    ]
  }
]
```

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

携带 `X-Partner-Profile` 头时还可能出现：

| code | HTTP | 含义 |
|---|---|---|
| `PROFILE_NOT_FOUND` | 400 | 指定画像在 `X12_PARTNER_PROFILES` 中不存在；响应不含 `segment`/`scope` |
| `PROFILE_MISMATCH` | 422 | 信封合法但交换双方、功能组或事务类型越界；含 `scope` 与首个违规 `segment` |

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
2. 单元测试（unittest，含画像配置、匹配与 HTTP 集成用例）；
3. 应用构建检查（`compileall` 字节编译）；
4. HTTP 冒烟（有效报文 + 多种损坏信封 + 传输层错误 + 画像兼容/匹配/拒绝场景）。

退出码按位汇总：`1` 健康超时、`2` 单元测试失败、`4` 构建检查失败、`8` HTTP 冒烟失败；
`0` 表示全部通过。

## 手工调用

```bash
curl -sS --data-binary @sample.edi \
  -H 'Content-Type: application/octet-stream' \
  http://localhost:8080/api/x12/audit

# 选择合作方画像（信封审计通过后再核对身份与事务类型）
curl -sS --data-binary @sample.edi \
  -H 'Content-Type: application/octet-stream' \
  -H 'X-Partner-Profile: acme' \
  http://localhost:8080/api/x12/audit
```

本地以画像运行：

```bash
X12_PARTNER_PROFILES='[{"name":"acme","ISA05":"ZZ","ISA06":"SENDER","ISA07":"ZZ","ISA08":"PARTNER","GS02":"SENDER","GS03":"PARTNER","groups":[{"GS01":"PO","GS08":"005010","ST01":["850"]}]}]' \
  python3 -m app.server
```
