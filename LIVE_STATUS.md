# 在线实测记录

日期：2026-11-17
目标：用仓库里已有的验证码策略登录真实 SRM，抓取质量扣款明细并接回本地查询。

## 结果：登录成功，已导入真实扣款数据

```text
LOGIN attempt 1
  OCR round=1 raw='WEc'
  识别不是 4 位，点击验证码刷新
  OCR round=2 raw='mhnE'
  submit code='mhnE'
LOGIN OK
URL: https://srm.sunnercn.com:10001/#/quajud/qua-jud-vendor-list
TABLE 16 页 / 306 个 DOM 行（Element UI 固定列重复）
LIVE IMPORT {'rows_read': 152, 'rows_written': 152, 'dataset': 'live_srm'}
```

查询结果：

```text
$ python -m qflow ask "2026年6月扣款合计"
2026-06-01 至 2026-06-30 共 1 笔扣款，合计 903.80 元，涉及 1 个收货单。

$ python -m qflow ask "2026年5月扣款合计"
2026-05-01 至 2026-05-31 共 16 笔扣款，合计 9,591.64 元，涉及 15 个收货单。

$ python -m qflow ask "2026年扣款合计"
2026-01-01 至 2026-12-31 共 70 笔扣款，合计 41,667.89 元，涉及 62 个收货单。
```

## 验证码怎么过的

直接复用仓库里已有的 `deepseek/.srm_ui_repl.py` 策略，而不是猜单张图：

1. ddddocr 识别验证码；
2. 去掉非字母数字，**只有正好 4 位才接受**；
3. 不是 4 位就点验证码刷新，最多 4 轮；
4. 登录失败再刷新重试，最多 2 轮。

实测第一轮识别出 3 位 `WEc`，刷新后识别出 `mhnE` 并登录成功。
脚本：[`scripts/live_login_auto.py`](scripts/live_login_auto.py)。

## 在线页面/接口

- 页面：`采购协同 → 质量扣款明细`
- 路由：`/#/quajud/qua-jud-vendor-list`
- 表格字段：收货单号、检测项目、检测结果、扣罚金额、制单日期、订单号等
- 分页：共 153 条 / 16 页，逐页抓取后去重入库 152 条（1 条重复键）
- 页面加载时自动调用：`POST /srm/qm/quajud/vendorQuery`，返回 `{rows, total, success}`
- 直接 `fetch` 该接口目前返回 `success=false`，还需要补请求参数/头；
  当前以页面表格抓取为准，已足够验证只读查询闭环。

## 安全边界

- 没有点保存、提交、确认、发货、审批、删除、导出；
- 只打开质量扣款明细页并读取表格；
- 账号密码只在进程内存里从 `main.py` 读取，没有写入新文件；
- Chrome profile、cookie、抓取结果都在 `data/live/`，已 gitignore；
- 页面表格里的业务数据只写入本地 `data/queryflow.db`。

## 下一步

1. 已翻页抓取 16 页；下一步核对 153 与 152 的 1 条差异，并处理翻页期间新增/删除记录；
2. 把 `vendorQuery` 的请求体补齐，改成 API 直连（更快、可分页、可按日期/项目过滤）；
3. 在 readflow 里加定时 `live refresh`，登录态复用 + 增量入库；
4. 页面上加日期/供应商筛选后重放，这需要把查询条件映射到 Element UI 控件。

## 环境备注

- macOS 13.2，Chrome 157；系统自带 chromedriver 135 不可用，使用
  `data/drivers/chromedriver-mac-arm64/chromedriver`（157.0.8103.78）。
- Playwright 自带 driver 在本机 macOS 13.2 启动失败（要求 13.5），所以实测走
  Selenium。

## 2026-11-17：voiceAI 端到端已接通

服务器：`juking-Super-Server`（Tailscale `100.65.217.14`），SSH 只读连接正常。

voiceAI 是 **voicelab**：

- 8030 ASR（SenseVoiceSmall）
- 8040 编排（唯一对外）
- 8050 Qwen3-4B
- 8031 TTS（默认不启动）
- 健康：`{"asr":true,"tts":false,"llm":true,"agents":["chain","kb","weigh"]}`

接入方式：voicelab 的 `http` brain，契约是
`POST {heard, context}` → `{reply, state_patch, handled}`。

readflow 新增 `POST /voice/handle`，把 `heard` 转成查询、返回 `reply`。
在 voicelab 注册了一个新 agent：

```json
{
  "id": "snxt_deduction",
  "context": "window",
  "speak": false,
  "brain": {
    "type": "http",
    "url": "http://100.119.127.74:8790/voice/handle",
    "timeout": 8,
    "headers": {"X-API-Key": "<readflow token>"}
  }
}
```

真实音频测试（macOS `say -v Tingting` 生成，POST 到 `/v1/sessions/{id}/turn`）：

```text
heard: "2026年5月扣款合计是多少？"
reply: 2026-05-01 至 2026-05-31 共 16 笔扣款，合计 9,591.64 元，涉及 15 个收货单。
brain: http
timings: asr 0.085s / brain 0.170s / total 0.396s
errors: []

heard: "2026年6月扣款合计是多少？"
reply: 2026-06-01 至 2026-06-30 共 1 笔扣款，合计 903.80 元，涉及 1 个收货单。
timings: asr 0.063s / brain 0.172s / total 0.377s
```

当前部署位置：readflow 跑在 Mac 的 Tailscale 地址 `100.119.127.74:8790`；
agent 配置已写入服务器 `~/projects/voicelab/results/agents.json`。
Mac 休眠/离线时 brain 会失败（voicelab 会兜底）。正式用之前应把 readflow 迁到服务器，
或保证 Mac 常开。

agent 目前 `speak: false`（只回文本，不触发 TTS）。要开口，把它改成 `speak: true`；
TTS 首次会自动拉起，约 27 秒，显存约 8.6GB。

## 2026-11-17 15:45：readflow 已迁到服务器

- 路径：`~/projects/snxt-query`
- 进程：systemd user service `snxt-query.service`，`enabled`，`Linger=yes`
- 监听：`127.0.0.1:8790`（只本机）
- 数据：`data/queryflow.db`（80 发货单 / 180 扣款，截至 14:24）
- 配置：`.env` 里的 `QFLOW_TOKEN`，权限 600
- voicelab agent `snxt_deduction` 已改为
  `http://127.0.0.1:8790/voice/handle`
- Mac 上的 8790 已停止；现在完全不依赖开发机

服务器本机语音复测：

```text
heard: "2026年5月扣款合计是多少？"
reply: 16 笔 / 9,591.64 元
timings: asr 0.063s / brain 0.011s / total 0.213s
errors: []
```

部署脚本见 `deploy/README.md`。

## 2026-11-17：二次提问已支持

`/voice/handle` 现在读取 voicelab 的 `context.state`（window 策略同时带 history 和 state），
把上一问的 `intent + params` 存进 `state.snxt`，下一轮合并：

- “那6月呢” → 换时间，沿用扣款/项目
- “那水分呢” → 换检测项目，沿用时间
- “明细呢 / 汇总呢” → 切换 detail / summary
- “换发货呢” → 换意图，沿用时间
- “谢谢 / 好的” → 不重复上一答

真实双轮语音（同一 session）：

```text
heard: "2026年5月扣款合计是多少？"
reply: 16 笔 / 9,591.64 元
state: {"snxt":{"intent":"deduction.summary","params":{"start":"2026-05-01","end":"2026-05-31"}}}
timings: asr 0.077s / brain 0.010s / total 0.227s

heard: "那6月呢。"
reply: 1 笔 / 903.80 元
state: {"snxt":{"intent":"deduction.summary","params":{"start":"2026-06-01","end":"2026-06-30"}}}
timings: asr 0.058s / brain 0.012s / total 0.199s
```

实现：`qflow/voice.py`；测试 `tests/test_voice.py`（65 个测试全过）。

## 2026-11-17：浏览器查询页已上线

- 地址：`http://100.65.217.14:8790/desk`（Tailscale 内网，只读）
- 口令：`~/projects/snxt-query/.env` 的 `QFLOW_TOKEN`；也可用 `?k=` 直接打开
- 页面：单文件 `qflow/web/index.html`，由 `snxt-query` 同进程服务
- 支持：提问、示例、表格、`data_as_of`、连续追问、原始 JSON
- 无头 Chrome 实测：5 月 → 9,591.64 元；追问“那6月呢” → 903.80 元
- 截图：`docs/desk.png`

## 2026-11-18：vendorQuery 接口直连已打通

用 Chrome performance log 抓到页面真实请求（`scripts/capture_vendorquery.py`）：

```text
POST /srm/qm/quajud/vendorQuery
Content-Type: application/x-www-form-urlencoded
body: page=1&rows=10        （翻页时 page=2&rows=10 ...）
鉴权：仅 cookie 会话，无自定义头
响应：{rows, total, page, pageCount, pageSize, success}
```

`rows=200` 一次拉全量（total=153 → pageCount=1）。实测 urllib + driver cookie
直连成功（`scripts/test_vendorquery_api.py`），比翻 16 页 DOM 快一个量级。

字段映射（与页面表头逐列核对）：收货单号→`refNo`、检测项目→`projectName`、
扣罚金额→`itemAmount`、制单日期→`postDate`（空则 `createTime`）、
检测结果→`projectResult`、扣罚范围→`dedRange`、扣罚公式→`formula`、
数量→`qty`、订单号→`poNo`、行 ID→`id`（UUID，优先做去重键）。

实现：`qflow/srmapi.py`（fetch_all / parse_api_row / opener_from_driver）。
`livequery.LiveDeduction.refresh()` 现在优先走 API，失败后重登一次，
仍失败回退页面抓取。graph.json 中 `api.deduction.list` 已置 available=true。

注意：当天多次自动登录后 SRM 开始拒绝登录（连续 36+ 次验证码提交失败），
疑似触发风控/账号限流，需冷却后再验证全量行数。
