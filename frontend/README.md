# Data Agent Frontend

独立的 React + TypeScript + Vite 前端。业务源码唯一位于 `src/`。它只通过 HTTP
JSON 与 SSE 使用 Data Agent FastAPI，不读取后端资源，也不持有数据库、Redis
或模型密钥。

## 开发

```powershell
npm ci
$env:VITE_API_BASE_URL = "http://127.0.0.1:8000"
npm run dev
```

默认地址为 `http://127.0.0.1:5173`。API base 留空时请求使用当前 Origin；设置为
`/api` 时，client 会把版本化 `/api/v1/**` 请求映射到同域 `/api/v1/**`，不会重复
路径前缀。

## 验证与构建

```powershell
npm run lint
npm run typecheck
npm run test
npm run build
```

`dist/` 是可独立托管的静态产物。生产环境建议同域反向代理 `/api/`；SSE location
需要关闭代理缓冲，并将读取超时设置为大于后端 SSE 心跳间隔。
