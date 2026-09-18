# HTTP 内容协商调试服务（FastAPI）

一个用于调试 HTTP 内容协商（`Accept` 头）的小型 FastAPI 服务。给定原始
`Accept` 头字符串和一组候选表示（`media_type` + `server_priority`），
服务返回最终选定的表示，以及**每个候选命中的范围、q 值、匹配具体度和淘汰原因**，
方便直观地理解协商全过程。

另有 `POST /audit-accept` 端点做**静态审计**：不返回单一选定项，而是列出每个
候选命中的全部范围与最终生效范围、从未生效的 shadowed 范围，以及同具体度
q 值冲突。

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

### `POST /audit-accept`

请求体与 `POST /negotiate` 完全相同（`accept` + `candidates`），但只做**静态
审计**，不做跨候选排序、**不返回 `selected`**。输出稳定：候选按输入顺序，
每个候选的命中范围按**具体度降序、同具体度按头部索引升序**，`shadowed` 与
`parsed_accept` 按头部索引顺序。

#### 审计规则

1. `candidates[].hits`：该候选命中的**所有**合法范围（即使被更具体范围遮蔽也
   列出），每条携带头部 `index`、`q`、`specificity`；
2. `candidates[].effective`：**最终生效范围**，即命中范围中最具体、同具体度
   最早出现者（与 `/negotiate` 的 `matched_range` 完全一致）；无命中时为
   `null`；
3. `candidates[].conflicts`：**同一候选上具体度相同但 q 不同**的范围组，每组
   按头部索引升序列出冲突范围及其头部索引；q 相同的重复范围不算冲突；
4. `shadowed`：**从未对任何候选生效**的合法范围（按头部索引升序）。注意
   `q=0` 的范围若成为某候选的生效范围，不算 shadowed；
5. 语义与 `/negotiate` 保持一致：
   - 生效范围 `q=0` → `eligible: false`、`eliminated_reason: "q_zero"`；
   - 无任何命中 → `hits: []`、`effective: null`、`eliminated_reason: "no_match"`；
   - 审计不做跨候选选择，因此**不会出现 `outranked`**；
   - 当没有任何 `eligible` 候选时，返回与 `/negotiate` 相同的 **406** 格式
     （响应体仍包含完整审计明细）；
   - 任一输入非法（Accept 语法、候选类型、空候选列表、请求体格式）返回与
     `/negotiate` 相同的 **400** 格式。

#### 200 响应示例

```bash
curl -s -X POST http://localhost:8000/audit-accept \
  -H 'Content-Type: application/json' \
  -d '{
    "accept": "application/json;q=0.3, application/json;q=0.9, application/*;q=0.5, */*;q=0.1",
    "candidates": [
      {"media_type": "application/json", "server_priority": 1},
      {"media_type": "text/html", "server_priority": 5}
    ]
  }'
```

```json
{
  "candidates": [
    {
      "index": 0,
      "media_type": "application/json",
      "server_priority": 1.0,
      "hits": [
        {"index": 0, "range": "application/json", "q": 0.3,
         "specificity": 2, "specificity_name": "exact"},
        {"index": 1, "range": "application/json", "q": 0.9,
         "specificity": 2, "specificity_name": "exact"},
        {"index": 2, "range": "application/*", "q": 0.5,
         "specificity": 1, "specificity_name": "type_subtype_wildcard"},
        {"index": 3, "range": "*/*", "q": 0.1,
         "specificity": 0, "specificity_name": "wildcard"}
      ],
      "effective": {"index": 0, "range": "application/json", "q": 0.3,
                    "specificity": 2, "specificity_name": "exact"},
      "eligible": true,
      "eliminated_reason": null,
      "conflicts": [
        {"specificity": 2, "specificity_name": "exact",
         "ranges": [
           {"index": 0, "range": "application/json", "q": 0.3,
            "specificity": 2, "specificity_name": "exact"},
           {"index": 1, "range": "application/json", "q": 0.9,
            "specificity": 2, "specificity_name": "exact"}
         ]}
      ]
    },
    {
      "index": 1,
      "media_type": "text/html",
      "server_priority": 5.0,
      "hits": [
        {"index": 3, "range": "*/*", "q": 0.1,
         "specificity": 0, "specificity_name": "wildcard"}
      ],
      "effective": {"index": 3, "range": "*/*", "q": 0.1,
                    "specificity": 0, "specificity_name": "wildcard"},
      "eligible": true,
      "eliminated_reason": null,
      "conflicts": []
    }
  ],
  "shadowed": [
    {"index": 1, "range": "application/json", "q": 0.9,
     "specificity": 2, "specificity_name": "exact"},
    {"index": 2, "range": "application/*", "q": 0.5,
     "specificity": 1, "specificity_name": "type_subtype_wildcard"}
  ],
  "parsed_accept": [
    {"index": 0, "range": "application/json", "q": 0.3, "specificity": 2, "specificity_name": "exact"}
  ]
}
```

上例中：两条精确范围 `application/json`（头部索引 0、1）具体度相同但 q 不同，
构成冲突并按头部索引报告；头部索引 1 的重复精确范围与 `application/*`
（索引 2）从未对任何候选生效，进入 `shadowed`。

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
