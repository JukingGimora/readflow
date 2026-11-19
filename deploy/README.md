# 部署到服务器（以 juking-super-server 为例）

目标：readflow 跑在 voiceAI 同机，agent 用 `http://127.0.0.1:8790/voice/handle`，
不再依赖开发机。

## 1. 打包

```bash
cd readflow
python - <<'PY'
import sqlite3
src = sqlite3.connect('data/queryflow.db')
dst = sqlite3.connect('/tmp/queryflow_deploy.db')
src.backup(dst)
PY
tar czf /tmp/snxt-query.tgz qflow graph.json README.md requirements.txt
```

## 2. 上传

```bash
ssh <server> 'mkdir -p ~/projects/snxt-query/data'
scp /tmp/snxt-query.tgz /tmp/queryflow_deploy.db <server>:/tmp/
ssh <server> 'cd ~/projects/snxt-query && tar xzf /tmp/snxt-query.tgz && mv /tmp/queryflow_deploy.db data/queryflow.db'
```

## 3. 服务

```bash
ssh <server>
cd ~/projects/snxt-query
printf 'QFLOW_TOKEN=%s\n' "$(openssl rand -hex 16)" > .env
chmod 600 .env
mkdir -p ~/.config/systemd/user
cp deploy/snxt-query.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now snxt-query
curl -s localhost:8790/health
```

如果希望服务器重启后不登录也自启：

```bash
sudo loginctl enable-linger "$USER"
```

## 4. 更新 voicelab agent

```bash
TOKEN=$(cut -d= -f2 .env)
curl -X POST localhost:8040/v1/agents -H 'Content-Type: application/json' -d "{
  \"id\": \"snxt_deduction\",
  \"context\": \"window\",
  \"speak\": false,
  \"brain\": {\"type\": \"http\", \"url\": \"http://127.0.0.1:8790/voice/handle\",
             \"timeout\": 8, \"headers\": {\"X-API-Key\": \"$TOKEN\"}}
}"
```

## 5. 验证

```bash
curl -s localhost:8790/health
curl -s -X POST localhost:8040/v1/sessions -H 'Content-Type: application/json' \
  -d '{"agent_id":"snxt_deduction"}'
# 用 /v1/sessions/{id}/turn 上传一段中文音频
```

注意：`.env`、`data/queryflow.db`、`results/agents.json` 都含敏感信息，不要提交。
