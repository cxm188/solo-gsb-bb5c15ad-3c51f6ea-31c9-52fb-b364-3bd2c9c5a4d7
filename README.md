# HTTP 内容协商调试服务（FastAPI）

一个用于调试 HTTP 内容协商（`Accept` 头）的小型 FastAPI 服务。给定原始
`Accept` 头字符串和一组候选表示（`media_type` + `server_priority`），
服务返回最终选定的表示，以及**每个候选命中的范围、q 值、匹配具体度和淘汰原因**，
方便直观地理解协商全过程。

## 一条命令启动

```bash
docker compose up --build
```

启动后访问：

- 服务地址与端口：**http://localhost:8000**
- 接口文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

> 依赖安装与代码预编译均在镜像构建阶段完成；容器启动时由 lifespan 完成初始化并输出就绪日志。

## 接口

### `POST /negotiate`

请求体：

```json
{
  "accept": "text/html;q=0.9, application/*;q=0.5, */*;q=0.1",
  "candidates": [
    {"media_type": "application/json", "server_priority": 1},
    {"media_type": "text/html", "server_priority": 5},
    {"media_type": "image/png", "server_priority": 9}
  ]
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `accept` | 原始 `Accept` 头字符串 |
| `candidates[].media_type` | 候选媒体类型，**必须具体**（`type/subtype`，不允许 `*`、参数） |
| `candidates[].server_priority` | 服务端优先级，数值越大越优先 |

#### 支持的 Accept 语法（受限子集）

- 范围仅支持：`type/subtype`、`type/*`、`*/*`，**不支持任何媒体参数**（如 `charset`）；
- 大小写不敏感（内部统一转小写比较）；
- 仅允许可选的 `q` 参数；`q` 缺省为 `1`，取值范围 `0..1`，最多 3 位小数；
- 任意非法语法（空条目、非法 token、未知参数、q 越界等）返回 **400**。

#### 协商规则

1. 每个候选在所有匹配的范围中，取**最具体**的范围：精确 `type/subtype`（具体度 2）
   > `type/*`（1） > `*/*`（0）；具体度相同时取头中**最早出现**的范围；
2. 该范围的 `q` 即候选的 q 值；`q=0` 表示显式拒绝，候选被排除；
3. 可用候选按 **q 降序 → 匹配具体度降序 → `server_priority` 降序 → 候选输入顺序**选定；
4. 没有任何可用候选（无范围命中或全部 `q=0`）时返回 **406**。

#### 200 响应示例（节选）

```json
{
  "selected": {
    "index": 1,
    "media_type": "text/html",
    "server_priority": 5.0,
    "matched_range": "text/html",
    "q": 0.9,
    "specificity": 2,
    "specificity_name": "exact"
  },
  "candidates": [
    {
      "index": 0,
      "media_type": "application/json",
      "server_priority": 1.0,
      "matched_range": "application/*",
      "q": 0.5,
      "specificity": 1,
      "specificity_name": "type_subtype_wildcard",
      "eligible": true,
      "eliminated_reason": "outranked"
    }
  ],
  "parsed_accept": [
    {"index": 0, "range": "text/html", "q": 0.9, "specificity": 2,
     "specificity_name": "exact"}
  ]
}
```

候选条目中的 `eliminated_reason` 取值：

| 值 | 含义 |
| --- | --- |
| `null` | 该候选即最终选中项 |
| `outranked` | 可用但排序靠后被淘汰 |
| `q_zero` | 命中最具体范围的 `q=0`，被显式排除 |
| `no_match` | 头中没有任何范围匹配该候选 |

`specificity_name`：`exact`（2）、`type_subtype_wildcard`（1）、`wildcard`（0）。

#### 错误响应

- `400`：Accept 语法非法、候选媒体类型不具体、候选列表为空或请求体格式错误；
- `406 Not Acceptable`：无可用候选（响应体仍包含每个候选的评估明细，便于调试）。

```bash
# 406 示例：所有候选被 q=0 排除
curl -i -X POST http://localhost:8000/negotiate \
  -H 'Content-Type: application/json' \
  -d '{"accept":"*/*;q=0","candidates":[{"media_type":"text/html","server_priority":0}]}'
```

## 本地开发（不使用容器）

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

运行测试：

```bash
pip install pytest httpx
pytest -q
```

## 项目结构

```
app/
  main.py          # FastAPI 路由、请求/响应模型、错误处理与启动初始化
  negotiation.py   # Accept 解析与内容协商核心逻辑（无框架依赖，可独立测试）
tests/             # 协商规则与 HTTP 接口测试
Dockerfile         # 依赖安装与初始化纳入镜像构建阶段
docker-compose.yml # 一键启动，映射 8000 端口
```
