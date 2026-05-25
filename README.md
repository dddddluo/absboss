# Audiobookshelf Telegram Bot

Python Telegram bot，用 `/pp` 管理 Audiobookshelf 用户、注册资格、兑换码、签到积分、活跃保号和积分自动续期。

## 功能

- 管理员回复用户 `/pp` 或发送 `/pp tgid` 可打开目标用户管理面板。
- 普通用户私聊发送 `/start` 可查看个人面板、线路、个人信息、活跃时间、签到、兑换码、创建/重置/删除账号。
- 无号用户可绑定已有 Audiobookshelf 账号；如账号已绑定到其他 TG，可提交换绑申请，由管理员在群组中同意或拒绝。
- 开放注册支持人数名额，用完自动关闭。
- 兑换码支持注册码、续期码、白名单码。
- 活跃时间按 Audiobookshelf `lastSeen` 与最近播放会话 `updatedAt` 取最大值。
- 每日定时任务：活跃保号、积分自动续期。

## 部署

### 前提

- 可访问的 MySQL 数据库
- 已从 BotFather 创建的 bot token

### 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`：

| 变量 | 必填 | 说明 |
|------|:----:|------|
| `BOT_TOKEN` | ✓ | BotFather 创建的 bot token |
| `OWNER_TG_ID` | ✓ | owner Telegram ID，用于执行 `/setup` 初始化向导 |
| `ADMIN_TG_IDS` | ✓ | 管理员 Telegram ID，多个用逗号分隔 |
| `MYSQL_DSN` | ✓ | MySQL 连接串，例如 `mysql+aiomysql://user:pass@127.0.0.1:3306/audiobookshelf_bot?charset=utf8mb4` |
| `ABS_BASE_URL` | ✓ | Audiobookshelf 地址 |
| `ABS_API_TOKEN` | ✓ | Audiobookshelf 管理员 API token |
| `BOT_TIMEZONE` | | 定时任务时区，默认 `Asia/Shanghai` |
| `LOG_LEVEL` | | 日志级别，默认 `INFO` |
| `LOG_FILE` | | 日志文件路径，默认 `logs/app.log` |
| `LOG_MAX_BYTES` | | 单个日志文件最大字节数，默认 `10485760` |
| `LOG_BACKUP_COUNT` | | 日志轮转备份数量，默认 `5` |
| `BACKUP_DIR` | | 数据库备份目录，默认 `backups` |
| `BACKUP_KEEP_COUNT` | | 备份文件保留数量，默认 `7` |
| `REGISTRATION_QUEUE_DELAY_SECONDS` | | 注册队列处理延迟秒数，默认 `2` |

以下运行期配置已迁移到数据库（`bot_settings`），通过 owner 执行 `/setup` 向导设置：

- Bot 主群组 ID 和群组链接
- 默认注册天数
- 签到积分范围
- 面板图片路径
- 换绑审核群 ID
- 禁用账号自动删除等待天数

### 启动

`MYSQL_DSN` 填写可访问的外部 MySQL 地址，宿主机 MySQL 可用 `host.docker.internal`：

```env
MYSQL_DSN=mysql+aiomysql://absbot:absbot_password@host.docker.internal:3306/audiobookshelf_bot?charset=utf8mb4
```

```bash
docker compose up -d
```

停止：

```bash
docker compose down
```

### 初始化

首次启动后，`OWNER_TG_ID` 对应的用户向 bot 私聊发送 `/setup`，按向导完成主群组、注册天数、签到积分等运行期配置。完成后 bot 即可正常使用。

## 开发

### 环境

Python 3.10 及以上。

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

### 数据库

启动时会自动创建表。也可以使用 Alembic 手动迁移：

```bash
alembic upgrade head
```

### 运行

```bash
python -m absbot.main
```

### 测试

```bash
pytest
ruff check .
```

## 效果预览

| 个人面板 | 管理面板 |
|:---:|:---:|
| ![个人面板](imgs/start_panel.jpg) | ![管理面板](imgs/admin_panel.jpg) |

| 签到 | 备份 |
|:---:|:---:|
| ![签到](imgs/checkin_panel.jpg) | ![备份](imgs/backup_panel.jpg) |
