# Qyclaw Backend Skeleton

## 结构

- `backend/main.py`: FastAPI 应用入口
- `backend/core/config.py`: 从 `config.yaml` 加载配置
- `backend/api/routes/*`: 基础 REST 路由
- `backend/ws/chat.py`: 聊天 WebSocket 占位接口

## 启动

1. 安装依赖

```bash
pip install -r requirements.txt
pip install -r requirements-models.txt
```

2. 启动服务

```bash
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

3. 打开文档

- `http://localhost:8000/docs`

## 配置

默认读取根目录 `config.yaml`。

如需指定路径：

```bash
QYCLAW_CONFIG=/path/to/config.yaml uvicorn backend.main:app --reload
```
