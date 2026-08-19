# Folio Translator / 多语言 PDF 翻译工作台

Folio Translator 是一个本机优先、无账号的 PDF 翻译与人工校对工作台。它保留原始页面视觉结构，支持逐块编辑、术语库、翻译记忆、草稿质量门，以及纯译文和左右双语 PDF 输出。

数字原生 PDF 的译文会依次尝试原框缩字和避障扩框。两种安全排版都失败时，系统按真实字体反算字符预算，并仅对未确认的机器译文执行最多两次受限重译；短译文必须保留数字和强制术语，并进入人工确认队列。

## 快速启动

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

打开 <http://127.0.0.1:8000>。默认 `FOLIO_QUEUE_MODE=auto`：Redis 可用时使用 RQ，否则自动使用进程内后台线程。要启用可跨进程恢复的后台队列：

```powershell
docker compose up -d redis
rq worker --url redis://127.0.0.1:6379/0 folio
```

在“服务设置”页配置 Azure Document Intelligence（扫描页 OCR）以及任意兼容 `/v1/chat/completions` 的翻译模型。密钥写入操作系统密钥环，不写入 SQLite。

## 首版边界

- 目标语言：简/繁中文、英、日、韩、法、德、西、葡、意、荷、波兰语。
- 最大 50MB、100 页；数字 PDF 无需 OCR 配置，扫描 PDF 需要 Azure Layout。
- 动态表单会被拒绝；数字签名会产生显式警告；嵌入图片内部文字保持原样。
- 任务文件保留 7 天，确认后的翻译记忆和术语库持续保留。
- 绑定地址默认为 `127.0.0.1`。无身份验证时不要暴露到公网。

## 测试

```powershell
pytest
ruff check .
```

本项目及 PyMuPDF 相关使用按 AGPL-3.0-only 发布。
