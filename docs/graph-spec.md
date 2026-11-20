# 系统图谱标准 v1（只读）

> 状态：**标准已定，运行层尚未实现**。当前代码加载的仍是旧格式的根目录 `graph.json`，见 [`PLAN.md`](PLAN.md) 的 T1–T3。
> 给外部模型的操作说明见 [`explorer-brief.md`](explorer-brief.md)。

外部大模型探索一个系统后，交付一个符合本标准的 JSON 文件。运行层（readflow，部署在自己的服务器上）只做三件事：校验它、加载它、按它执行。运行层不写任何某个系统专用的代码。

## 1. 设计原则

- **一个系统一个文件**：`systems/<system_id>/graph.json`。多系统就是多个文件。
- **图谱必须说清「怎么执行」**，而不只是「有什么」。现在的图谱缺的就是这一块。
- **本地小模型只做填空**（服务器上是 llama.cpp 跑的 Qwen3，4B 量级）：把一句话对应到某个能力，并填出参数。取数、求和、分组都由程序按图谱确定地完成，数字不经过模型。
- **图谱里不放任何账号、密码、cookie 和真实个人数据**；样例数据要脱敏。账号和登录态按用户存在本地。
- **v1 只有读**。每个操作都带权限级别字段，写和不可逆操作以后再开放。

## 2. 文件结构

```jsonc
{
  "spec_version": "1.0",
  "system":   { "id": "sunner_srm", "label": "圣农 SRM", "base_url": "https://..." },
  "generated": { "at": "2026-11-20T10:00:00+08:00", "by": "模型名", "account_role": "供应商" },
  "auth":         { ... },   // 第 3 节
  "navigation":   [ ... ],   // 第 4 节
  "entities":     [ ... ],   // 第 5 节
  "operations":   [ ... ],   // 第 6 节
  "capabilities": [ ... ]    // 第 7 节
}
```

`account_role` 说明这张图是用哪种角色的账号探索的。同角色的用户共用这张图，各自用自己的账号登录，查到的是各自的数据。

## 3. auth：怎么登录，哪一步需要人

```jsonc
{
  "login_url": "/#/login",
  "steps": [
    { "kind": "input",   "field": "username", "locator": "input[placeholder*='账号']" },
    { "kind": "input",   "field": "password", "locator": "input[type=password]" },
    { "kind": "challenge", "type": "image_captcha", "locator": "img.vcode",
      "answer_locator": "input[placeholder*='验证码']", "solver": ["ocr", "human"] },
    { "kind": "submit",  "locator": "button.login-btn" }
  ],
  "session": {
    "carrier": "cookie",                       // cookie | header | local_storage
    "check":   { "operation": "op.whoami", "ok_when": "$.success == true" },
    "expired_when": [ { "status": 401 }, { "redirect_contains": "/login" } ]
  }
}
```

`challenge.type` 取值和本地处理方式：

| type | 本地怎么处理 |
|---|---|
| `image_captcha` | 先自动识别，失败就把原图给用户，用户输入后原样填回 |
| `sms_code` | 把「已发送验证码」提示给用户，用户输入后原样填回（有时效，实时转发） |
| `slider` / `qr` | 抄不了，把那块画面给用户，由用户自己操作 |
| `sso` | 跳转到第三方登录，由用户完成 |

登录成功后保存会话，过期才重新登录。

## 4. navigation：菜单树

```jsonc
[ { "id": "nav.purchase.deduction", "path": ["采购协同", "质量管理", "质量扣款明细"],
    "route": "/#/quajud/qua-jud-vendor-list",
    "capabilities": ["cap.deduction.summary", "cap.deduction.detail"],
    "not_queryable": null } ]
```

不能查的页面写成 `"not_queryable": {"reason": "数据由前端加密后请求", "gap": "G1"}`，`gap` 取第 10 节的编号，没有对应编号就省略。

两个用途：用户说的是菜单上的词，靠它对上；每个叶子页面要么挂了能力，要么写明 `not_queryable` 的原因，这样能算出覆盖率，知道探索是否完整。

## 5. entities：数据长什么样

```jsonc
{ "id": "entity.deduction", "label": "扣款明细", "key": ["asn", "item"],
  "fields": [
    { "name": "biz_date", "label": "制单日期", "type": "date" },
    { "name": "item",     "label": "检测项目", "type": "enum",
      "values": ["水分", "杂质", "霉变"], "synonyms": { "水分": ["含水", "水份"] } },
    { "name": "amount",   "label": "扣罚金额", "type": "money", "unit": "元" }
  ] }
```

`type` 取值：`string` `number` `money` `date` `datetime` `enum` `bool`。`synonyms` 是用户可能的说法，供本地听懂问题用。

## 6. operations：怎么取数（核心）

```jsonc
{ "id": "op.deduction.list", "kind": "http", "access": "read",
  "request": {
    "method": "POST", "path": "/srm/qm/quajud/vendorQuery", "body_type": "form",
    "params": [
      { "name": "startDate", "type": "date", "format": "YYYY-MM-DD", "required": false },
      { "name": "endDate",   "type": "date", "format": "YYYY-MM-DD", "required": false }
    ]
  },
  "pagination": { "style": "page", "page_param": "page", "size_param": "rows",
                  "max_size": 200, "total_path": "$.total" },
  "response": {
    "ok_when": "$.success == true",
    "rows_path": "$.rows",
    "entity": "entity.deduction",
    "map": { "asn": "$.asnNo", "biz_date": "$.createDate", "item": "$.itemName", "amount": "$.deductAmt" }
  },
  "evidence": { "verified_at": "2026-11-18T22:50:00+08:00", "sample_rows": 3,
                "sample": { "request": { ... }, "response": { ... } } }
}
```

- `access`：`read` | `write` | `irreversible`。v1 本地只执行 `read`，其余一律拒绝。
- `kind`：v1 只有 `http`（系统有规整接口）。`page`（没有接口、只能从页面上读，例如部分电商网站）先保留名字，等做比价时再定。
- `evidence` 必填：外部模型必须真实调通过，并附上脱敏后的请求和响应样例。没有证据的操作，本地不加载。

## 7. capabilities：用户能问什么

```jsonc
{ "id": "cap.deduction.summary", "label": "扣款汇总", "access": "read",
  "nav": "nav.purchase.deduction",
  "operation": "op.deduction.list",
  "slots": [
    { "name": "period", "type": "date_range", "to": { "start": "startDate", "end": "endDate" },
      "default": "last_month" },
    { "name": "item", "type": "enum", "field": "item", "filter": "local" }
  ],
  "compute": { "group_by": ["item"],
               "metrics": [ { "key": "total", "fn": "sum", "field": "amount", "label": "扣款合计", "unit": "元" },
                            { "key": "count", "fn": "count", "label": "笔数", "unit": "笔" } ] },
  "answer":   "{period} 共 {count} 笔扣款，合计 {total} 元。",
  "examples": ["上个月扣款合计是多少", "本月水分扣了多少钱", "去年11月扣了多少"],
  "followups": ["明细呢", "只看水分", "那发货呢"]
}
```

- `slots`：这个问题需要哪些条件。`to` 表示传给接口的哪个参数；`filter: "local"` 表示接口不支持这个条件，由本地在取回的数据上过滤。
- `compute`：求和、计数、分组都由本地程序算，`fn` 取值 `sum` `count` `avg` `min` `max`。没有 `compute` 就是直接列明细。
- `answer`：回答模板，只能引用 slots 和 metrics 里的名字。
- `examples` 至少 3 条，`followups` 至少 1 条。

## 8. 验收：本地导入时自动检查，不通过就不加载

1. 结构符合本标准的 JSON Schema，所有引用的 id 都存在且不重复。
2. 每个 capability 指向的 operation 存在、`access` 为 `read`，且通过本地只读检查。
3. 每个 operation 都有 `evidence`，样例响应按 `rows_path` 和 `map` 能取出声明的字段。
4. 每条 `examples` 交给本地模型，都能对回它所属的 capability。对不回的列出来。
5. 试跑：登录后用默认参数真实调用每个 operation，确认能返回、字段齐全。
6. 输出覆盖率：菜单叶子页面里，有能力的、标了不可查的、两者都没有的各多少。

第 1 到 4 条离线就能查；第 5 条需要一个真实账号。

## 9. 已定的取舍

1. **求和和分组在本地算**（`compute`），不依赖系统返回的汇总。确定、可核对；代价是数据量大的查询要翻完所有页。
2. **v1 只支持有规整接口的系统**（`kind: "http"`）。
3. **一个账号角色一张图**（`generated.account_role`）。同角色的用户共用；同一系统不同角色菜单不同，就是两张图。

## 10. v1 明确不包含的（每一项都是缺口，不要当成已支持）

| 编号 | 缺口 | 什么时候需要 | 预留位置 |
|---|---|---|---|
| G1 | `kind: "page"`：没有接口、要从渲染后的页面读数据；或请求带 JS 现算的签名、有反爬 | 电商比价 | `operations[].kind` 已留这个取值，结构未定 |
| G2 | `access: "write"` / `"irreversible"`：填表、提交、删除 | 系统 A 填表这类场景 | `access` 字段已有，运行层一律拒绝 |
| G3 | 下载、导出文件 | 用户要原始报表时 | 无 |
| G4 | 跨系统：一个问题要用到两张图 | 系统 A + 系统 B 汇总 | 一系统一文件已为它留好边界 |
| G5 | 一个能力串联多个接口（先查列表再逐条查详情） | 明细在二级接口里的系统 | `capabilities[].operation` 目前只能填一个 |
| G6 | 图谱过期检测：系统改版后接口变了 | 上线后长期运行 | 可复用第 8 节第 5 条的试跑，定时执行 |
| G7 | 请求需要动态取的值（CSRF token、从上一个响应里取的 id） | 遇到这类系统时 | 无 |

外部模型探索时遇到 G1、G5、G7 的情况，不要硬凑成 `http` 操作，按 [`explorer-brief.md`](explorer-brief.md) 的要求记进 `navigation[].not_queryable`。
