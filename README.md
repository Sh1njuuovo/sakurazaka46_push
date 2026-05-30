# 櫻坂46 官方更新每日推送机器人

每天抓取櫻坂46官网的官方新闻、成员博客和日程/媒体出演，生成 Markdown 日报并通过 PushPlus 推送。脚本使用 SQLite 保存已经推送过的 URL，第一次运行会推送当前抓到的全部内容，之后只推新增。

抓取来源只使用櫻坂46官方站点：

- NEWS: <https://sakurazaka46.com/s/s46/news/list?ima=0000>
- BLOG: <https://sakurazaka46.com/s/s46/diary/blog?ima=0000>
- SCHEDULE: <https://sakurazaka46.com/s/s46/media/list?ima=0000>

## 本地运行

创建虚拟环境并安装依赖：

```bash
cd /Users/shinjuu/aicode/sakurazaka46_push
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

准备配置：

```bash
cp .env.example .env
```

编辑 `.env`，填入 PushPlus Token：

```env
PUSHPLUS_TOKEN=你的 PushPlus Token
ENABLE_SCHEDULE=true
PUSH_ONLY_TODAY=true
PUSH_EMPTY_MESSAGE=false
DATABASE_PATH=sakurazaka_push.sqlite3
```

运行：

```bash
python3 sakurazaka_push.py
```

如果不配置 `PUSHPLUS_TOKEN`，脚本不会推送微信，只会在控制台打印 Markdown，方便测试解析结果。

## 配置说明

- `PUSHPLUS_TOKEN`: PushPlus 的推送 Token。
- `ENABLE_SCHEDULE`: 是否抓取 SCHEDULE，默认 `true`。
- `PUSH_ONLY_TODAY`: 是否只推送当天日期的内容，默认 `true`。设为 `false` 时会推送所有未记录内容。
- `PUSH_EMPTY_MESSAGE`: 无新增时是否推送“今日暂无新更新”，默认 `false`。
- `DATABASE_PATH`: SQLite 数据库路径，默认 `sakurazaka_push.sqlite3`。
- `REQUEST_TIMEOUT`: 单次请求超时时间，默认 20 秒。
- `REQUEST_RETRIES`: 官网请求重试次数，默认 3 次。
- `PUSHPLUS_MAX_CONTENT_BYTES`: 单条 PushPlus 内容字节上限，默认 12000。第一次运行内容很多时会自动拆成多条推送，按 UTF-8 字节数控制以兼容日文内容。
- `PUSHPLUS_SEND_INTERVAL_SECONDS`: 多条 PushPlus 推送之间的间隔，默认 5 秒，用来避开频率限制。
- `USER_AGENT`: 请求官网时使用的 User-Agent。

当前版本默认使用 PushPlus，不再使用 Server酱。如需 Server酱，可以后续新增一个推送后端。

## macOS crontab 定时运行

先确认绝对路径：

```bash
which python3
```

编辑 crontab：

```bash
crontab -e
```

每天北京时间 09:00 运行：

```cron
0 9 * * * cd /Users/shinjuu/aicode/sakurazaka46_push && /Users/shinjuu/aicode/sakurazaka46_push/.venv/bin/python /Users/shinjuu/aicode/sakurazaka46_push/sakurazaka_push.py >> /Users/shinjuu/aicode/sakurazaka46_push/sakurazaka_push.log 2>&1
```

如果没有使用虚拟环境，把 Python 路径替换成你的 `which python3` 输出。

## GitHub Actions 定时运行

工作流文件位于：

```text
.github/workflows/sakurazaka_daily.yml
```

默认每天北京时间 09:00 运行，对应 UTC 01:00。

在 GitHub 仓库页面添加 Secret：

```text
Settings -> Secrets and variables -> Actions -> Secrets
```

添加：

```text
PUSHPLUS_TOKEN
```

为了让 Actions 提交更新后的 `sakurazaka_push.sqlite3`，避免下次运行重复推送，需要开启写权限：

```text
Settings -> Actions -> General -> Workflow permissions -> Read and write permissions
```

也可以在 Actions 页面手动点击 `Run workflow` 测试。

## 文件说明

- `sakurazaka_push.py`: 主脚本。
- `requirements.txt`: Python 依赖。
- `.env.example`: 本地配置模板。
- `sakurazaka_push.sqlite3`: 自动生成的 SQLite 去重数据库。
- `.github/workflows/sakurazaka_daily.yml`: GitHub Actions 定时任务。

## 注意事项

- 不要提交 `.env`，里面包含你的 PushPlus Token。
- 官网 HTML 结构可能变化，脚本包含多层解析兜底；如果连续抓不到内容，请先本地运行查看日志。
- PushPlus 免费额度和接口限制以 PushPlus 官方说明为准。
