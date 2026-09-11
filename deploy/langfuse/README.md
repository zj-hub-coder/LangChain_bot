# Langfuse 自托管部署（可观测第二阶段）

本目录把 K8s 运维助手的 trace 从"本地 JSONL"升级为"JSONL + Langfuse UI 双写"。
本地 JSONL 始终保留（断网/未部署 Langfuse 时评估体系照常工作），Langfuse 提供
trace 可视化、按用户/会话检索、token 成本看板等能力。

## 架构（6 个容器，官方 compose）

```
langfuse-web:3000  ──┐
langfuse-worker     ├─→ postgres:17（元数据）
                     ├─→ clickhouse:25.12（观测事件）
                     ├─→ minio（事件/媒体对象存储，控制台 :9091）
                     └─→ redis:7（摄入队列）
```

资源建议 **4 CPU / 8 GB 内存**、约 5 GB 磁盘。Windows 用 Docker Desktop（WSL2）即可，
虚拟机 server1（2GB）跑不动，不要部署在那台。

## 一、部署

```powershell
# 1. 前置：启动 Docker Desktop
docker version

# 2. 准备配置
cd deploy/langfuse
copy .env.example .env
#   编辑 .env：至少替换 ENCRYPTION_KEY（64 位十六进制）、SALT、NEXTAUTH_SECRET
#   PowerShell 生成密钥：
#   python -c "import secrets; print(secrets.token_hex(32))"

# 3. 拉起（首次约 3-5 分钟拉镜像）
docker compose up -d
docker compose ps   # 6 个容器全部 healthy/running
```

访问 http://localhost:3000：

- 用 `.env` 里 `LANGFUSE_INIT_USER_EMAIL/PASSWORD` 登录（首次启动自动建管理员）；
- 若未配置 `LANGFUSE_INIT_PROJECT_*`，手动 New Organization → New Project；
- 进入 **Project Settings → API Keys**，记下 `Public Key (pk-lf-...)` 与
  `Secret Key (sk-lf-...)`。

## 二、接入本项目

1. 安装可选依赖：

```powershell
.\.venv\Scripts\pip.exe install -e ".[observability]"
```

2. 在**项目根目录** `.env` 增加（参见根目录 `.env.example` 观测段）：

```ini
LANGFUSE_ENABLED=true
LANGFUSE_HOST=http://localhost:3000
LANGFUSE_PUBLIC_KEY=pk-lf-xxxxxxxx
LANGFUSE_SECRET_KEY=sk-lf-xxxxxxxx
```

3. 正常使用 CLI / 飞书即可：每次运行同时落盘 `traces/traces-*.jsonl` 并上报
   Langfuse（trace 上带 source/session_id/user_id/mode 标签，可在 UI 按这些维度筛选）。

容错设计见 [observability/tracer.py](../../observability/tracer.py)：未安装
langfuse 包、未配置密钥或 Langfuse 不可达时，仅记一条 warning 并静默降级为
纯本地落盘，**不影响主对话链路**。

## 三、验证

```powershell
# 触发一次问答
.\.venv\Scripts\python.exe cli.py
# 问："帮我看看有哪些节点"
```

- 本地：`traces/traces-当天.jsonl` 新增一条；
- Langfuse：Tracing 列表出现同名 session（question 作为 trace 名），点进去可见
  LLM/工具步骤树、每步时延与 token 用量，与 JSONL 内容一致。

## 四、运维

| 操作 | 命令 |
|---|---|
| 停止 | `docker compose stop` |
| 重启 | `docker compose start` |
| 看日志 | `docker compose logs -f langfuse-web` |
| 升级 | 改镜像 tag 后 `docker compose pull && docker compose up -d`（先备份卷） |
| 数据位置 | docker volume：`langfuse_postgres_data` / `langfuse_clickhouse_data` 等 |

端口暴露策略沿用官方默认：只有 web(3000) 与 minio(9090) 对外开放，
postgres/clickhouse/redis/worker 均绑定 127.0.0.1。

## 五、与评估体系的关系

- 冻结数据集跑批（`evals/run_eval.py`）的 trace source=`eval`，上报后可在
  Langfuse 按标签筛出"评估流量"，与真实用户流量分开看；
- 双 Judge 的失败样本证据里带 `trace_run_id`，在 Langfuse 搜索框直接粘贴即可
  跳转到那次运行的完整链路（Lineage：样本 → 报告 → trace）。
