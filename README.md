# readflow

让不熟悉电脑的人**说一句话就查到**业务系统里的数据，不用去点三级菜单。只读，不替人在系统里办事。

换一个业务系统只换一份图谱，不换代码：这个仓库里没有任何某个系统专用的代码。

## 文档

| 文档 | 给谁看 |
|---|---|
| [`docs/PLAN.md`](docs/PLAN.md) | 接手的人：现状、数据流、做了什么、没做什么、没验证什么 |
| [`docs/graph-spec.md`](docs/graph-spec.md) | 系统图谱的标准：一个系统要描述成什么样 |
| [`docs/explorer-brief.md`](docs/explorer-brief.md) | 交给外部大模型（或人）的探索说明：怎么摸清一个系统并写出图谱 |
| [`deploy/README.md`](deploy/README.md) | 部署到服务器 |

## 怎么分工

| 谁 | 做什么 | 什么时候 |
|---|---|---|
| 外部大模型，或人用浏览器开发者工具 | 探索一个系统，写出图谱：怎么登录、有哪些菜单、哪个接口取什么数据、用户会怎么问 | 接入一个系统时一次；系统改版时再一次 |
| 本仓库（确定性程序） | 校验图谱；登录；按图谱发只读请求；过滤、求和、分组；回答 | 每次查询 |
| 本地小模型（可选） | 只做一道选择题：这句口语想查哪一类业务 | 关键词认不出来时。连不上就退回规则 |
| 人 | 填一次账号；自动登录过不去时输入验证码，或在页面里操作服务器上的浏览器 | 登录时 |

数字从不经过任何模型。

## 原子 agent

一个系统 = 图谱 + 账号 + 登录态（`qflow/agent.py`），对外三个动作，输出是同一种结构：

- **登录**：照图谱的 `auth` 做（`qflow/login.py`）。
- **执行**一个能力：`agent.run("cap.xxx", {...})`（`qflow/runner.py`）。
- **听懂**一句话再执行：`agent.ask("上个月扣了多少", state)`（`qflow/ask.py`）。

多个系统的串联、并联在这之上做，还没做。

## 快速开始

```bash
python -m qflow serve --port 8790          # 浏览器打开 http://127.0.0.1:8790/desk
python -m qflow systems                    # 接入了哪些系统、各自的状态
python -m qflow validate 某个图谱.json      # 校验一份图谱
python -m qflow login <系统 id>             # 在终端里登录（也可以在页面上登录）
python -m qflow ask "上个月扣款合计"
```

查询和服务只用 Python 标准库。登录要开浏览器：`pip install -r requirements-live.txt`，机器上要有 Chrome。

**接入一个新系统**：按 `docs/explorer-brief.md` 得到图谱 → `validate` 通过 → 页面「管理 → 导入图谱」（或放到 `systems/<id>/graph.json`）→ 页面上填账号、登录 → 提问。

**不想碰真实系统先试一下**：`python tests/fakesite.py --port 8901 --graph /tmp/demo.json` 会起一个假的「小店订单」系统（账号 `demo` / `demo123`），把 `/tmp/demo.json` 导入页面即可。

## HTTP 接口

| 接口 | 作用 |
|---|---|
| `GET /desk` | 页面 |
| `GET /health` | 服务是否在线、各系统状态（不需要口令） |
| `GET /capabilities` | 每个系统能查什么 |
| `POST /query` | `{"text": "...", "state": {...}}`；或点名执行：`{"system": id, "capability": id, "values": {...}}` |
| `POST /voice/handle` | VoiceLab 适配：`{"heard": "...", "context": {"state": {...}}}` → `{reply, state_patch, handled, followups}` |
| `/desk/api/*` | 页面用 |

设了 `QFLOW_TOKEN` 后，除 `/health` 外都要带 `X-API-Key`（或 `Authorization: Bearer`）；页面用口令换一个当天有效的会话。多轮对话的状态由调用方保存，每轮把上一轮的 `state_patch` 原样传回。

## 目录

```
qflow/
  agent.py      原子 agent：图谱注册表、账号、登录态
  ask.py        一句话 → 能力 + 条件
  timewords.py  中文时间词
  understand.py 本地模型的选择题
  runner.py     通用执行器
  login.py      通用登录      browser.py  登录用的浏览器
  spec.py       图谱校验      online.py   只读检查
  view.py       结果整理成页面、语音用的样子
  service.py    HTTP 服务     cli.py      命令行
  web/index.html 页面
systems/<id>/graph.json   随仓库发布的图谱
data/systems/<id>/        账号、登录态、页面导入的图谱（不进仓库）
docs/                     计划书、图谱标准、探索说明、图谱示例
tests/                    含一个假的业务系统 fakesite.py
```

## 测试

```bash
python -m pytest -q
```

装了 selenium 且有 Chrome 时，会多跑一条用真浏览器登录假系统的测试。
