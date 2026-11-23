# 部署到服务器

目标：readflow 和 VoiceLab 跑在同一台机器上，VoiceLab 的 agent 调 `http://127.0.0.1:8790/voice/handle`。

实际使用的是开发机上仓库外的 `deploy_server.sh`。**它还是按旧版写的，部署新版前要先改**，要改的地方见 [`../docs/PLAN.md`](../docs/PLAN.md) 第 3 节。下面是新版手工部署的步骤。

## 1. 打包上传

```bash
cd readflow
COPYFILE_DISABLE=1 tar czf /tmp/readflow.tgz --exclude __pycache__ qflow systems deploy docs requirements.txt requirements-live.txt README.md
scp /tmp/readflow.tgz <server>:/tmp/
ssh <server> 'mkdir -p ~/projects/snxt-query && cd ~/projects/snxt-query && tar xzf /tmp/readflow.tgz'
```

**不要覆盖服务器上的 `data/`**：`data/systems/<id>/` 里是账号、登录态、浏览器资料和页面导入的图谱。

## 2. 依赖

查询和服务只用标准库。登录要开浏览器：

```bash
# 用服务实际运行的那个 Python（见 deploy/snxt-query.service 的 ExecStart）
python3 -m pip install -r requirements-live.txt
google-chrome --version      # 要有 Chrome；驱动由 selenium 自己下载，下不了就设 QFLOW_CHROME_DRIVER
```

## 3. 配置和服务

```bash
cd ~/projects/snxt-query
# 没有 .env 时才生成；已有的不要覆盖
[ -f .env ] || { printf 'QFLOW_TOKEN=%s\n' "$(openssl rand -hex 16)" > .env; chmod 600 .env; }
mkdir -p ~/.config/systemd/user
cp deploy/snxt-query.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now snxt-query
curl -s localhost:8790/health        # {"ok": true, "systems": [{"id": ..., "status": ...}]}
sudo loginctl enable-linger "$USER"  # 服务器重启后不登录也自启
```

`.env` 里能设的：

| 变量 | 作用 |
|---|---|
| `QFLOW_TOKEN` | 设了以后所有接口要带 `X-API-Key`；页面要口令 |
| `QFLOW_DESK_PASS` | 页面口令（给人输入的一句话）；不设就用 `QFLOW_TOKEN` |
| `QFLOW_LLM_URL` | 本地模型的 chat/completions 地址；不设就只用规则 |
| `QFLOW_DATA` | 账号、登录态、导入的图谱放哪，默认仓库下的 `data/` |
| `QFLOW_BROWSER_HEADLESS=0` + `DISPLAY` | 登录用真窗口（配合 Xvfb），反爬严的系统用 |
| `QFLOW_CACHE_TTL` | 同一个请求多少秒内不重复发给业务系统，默认 120 |

## 4. 登记到 VoiceLab

```bash
TOKEN=$(grep '^QFLOW_TOKEN=' .env | cut -d= -f2)
curl -X POST localhost:8040/v1/agents -H 'Content-Type: application/json' -d "{
  \"id\": \"snxt_deduction\",
  \"context\": \"window\",
  \"speak\": false,
  \"brain\": {\"type\": \"http\", \"url\": \"http://127.0.0.1:8790/voice/handle\",
             \"timeout\": 8, \"headers\": {\"X-API-Key\": \"$TOKEN\"}}
}"
```

登录态过期后的无人值守重新登录要开浏览器，可能超过这里的 8 秒；超时的那一句会失败，下一句就正常了。

## 5. 接入系统并验证

1. 打开页面 `/desk`，输入口令。
2. 顶部会提示哪个系统要填账号。填账号 → 登录（需要时输入验证码，或在浏览器窗口里操作）。
3. 问一句有确定答案的话，和业务系统页面上的数字核对。

`.env` 和 `data/` 含敏感信息，不要提交。
