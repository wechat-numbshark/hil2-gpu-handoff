# 部署指南

需要 Python 3.11+ 或 Docker。复制 `.env.example` 为 `.env`，设置随机 `INGEST_TOKEN` 和 `JWT_SECRET`，并将 `MANAGED_CLUSTERS` 改为实际集群。若先看纯本地演示，可设置 `FEISHU_EVENT_MODE=off`、`SSO_ENABLED=false`，执行 `make dev && make demo && make run`，浏览器访问 `http://127.0.0.1:8848`。

Docker 入口为 `docker compose -f deploy/docker-compose.yml up -d --build`，默认仅绑定本机。需要接入飞书时，再配置应用凭据、群 ID、回调或长连接和所需权限；集群侧按 `agent/.env.example` 配置。正式上线前在测试群完成卡片、预约、权限与超时验收，见 [验收清单](ACCEPTANCE.md)。

默认集群编号 52/58/60、主机名及演示状态都是虚构样本。真实环境的容器规则、数据目录和进程名称必须现场确认；采集异常应保持 `UNKNOWN`。
