# HIL2.0 常用操作。默认使用 .venv 里的解释器。
PY := .venv/bin/python
PIP := uv pip install --python $(PY)

.PHONY: help venv install dev test cov lint demo run agent-once probe clean docker

help:  ## 显示可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	 awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

venv:  ## 创建虚拟环境（需要 uv；没有就用 python3.11 -m venv .venv）
	uv venv --python 3.11 .venv

install: venv  ## 安装运行依赖
	$(PIP) -e .

dev: venv  ## 安装运行 + 测试依赖
	$(PIP) -e ".[dev]"

test:  ## 跑全部测试（Python + 安装脚本）
	$(PY) -m pytest tests/ -q
	@echo ""
	@bash tests/test_install_script.sh

cov:  ## 跑测试并输出覆盖率
	$(PY) -m pytest tests/ -q --cov=hil2 --cov-report=term-missing

lint:  ## 语法自检（含零依赖的集群脚本）
	$(PY) -m compileall -q hil2 agent scripts
	$(PY) -c "import hil2.api, hil2.coordinator, hil2.flow_engine; print('导入检查通过')"

demo:  ## 播种演示数据（写入 var/demo.db）
	$(PY) scripts/seed_demo.py

run:  ## 启动协调服务（读取 .env）
	$(PY) -m hil2

agent-once:  ## 在本机跑一次采集并打印事件（只读，不发消息）
	HIL_DRY_RUN=1 python3 agent/hil_agent.py --once --print-event

probe:  ## 现场只读探测，输出建议配置
	python3 agent/probe_site.py

install-agent:  ## 在本机安装采集 agent（需要 sudo，默认灰度）
	sudo ./scripts/install_agent.sh --cluster $(CLUSTER) --coordinator $(COORDINATOR)

deploy-agents:  ## 批量推送 agent 到各集群（HOSTS=hil-52,hil-58,hil-60）
	./scripts/deploy_agents.sh --hosts "$(HOSTS)" --coordinator "$(COORDINATOR)" --token "$(TOKEN)"

docker:  ## 构建并启动容器
	docker compose -f deploy/docker-compose.yml up -d --build

clean:  ## 清理临时产物
	rm -rf .pytest_cache .coverage htmlcov
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
