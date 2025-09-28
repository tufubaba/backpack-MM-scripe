# Backpack 做市机器人

本项目提供一个使用 Backpack Exchange API 的轻量级做市脚本，核心逻辑包含 WebSocket 行情监听、点差与库存管理、自动对冲以及安全退出等功能。脚本仅依赖单个 Python 文件，便于部署与二次开发。

## 功能特性
- **多源环境变量加载**：优先读取 `ENV_FILE` 指定文件，其次自动搜索 `.env`、`backpack.env` 等常见命名，避免手动切换配置。
- **盘口监听与稳健报价**：通过公共 WebSocket `bookTicker` 流实时获取最佳买卖价，支持最小点差、挂单偏移、Post-only 保价等限制。
- **库存偏移与暂停机制**：基于净仓位动态调整买卖档位（`SOFT_POS`/`SKEW_TICKS_PER_UNIT`），当仓位触达 `HARD_POS` 时自动停止一侧挂单。
- **自动 Reduce-only 对冲**：在净仓位超过阈值时（`HEDGE_TRIGGER`），触发市价或 IOC 限价对冲；支持阶梯加码与冷却时间。
- **安全退出**：捕获 SIGINT/SIGTERM，退出前先调用 `cancel_all()` 撤单，降低遗留挂单风险。

## 快速开始
1. **准备 Python 环境**：建议使用 Python 3.10+。
2. **安装依赖**：
   ```bash
   pip install python-dotenv requests websocket-client pynacl
   ```
3. **配置密钥**：在 Backpack 交易所生成 ED25519 私钥，将 seed（32 字节）按 hex 或 base64 写入 `SECRET_KEY`。
4. **复制环境文件**：可直接编辑仓库中提供的 `backpack.env`，或创建新的 `.env` 文件。
5. **启动脚本**：
   ```bash
   python backpack_MMbot.py
   ```

## 环境变量说明
常用字段如下（未列出的保持默认即可）：

| 变量名 | 说明 |
| --- | --- |
| `SECRET_KEY` | **必填**。ED25519 私钥 seed，支持 hex/base64；缺失时脚本直接报错退出。 |
| `API_KEY_B64` | 可选。若为空，将由 `SECRET_KEY` 推导公钥后自动生成。 |
| `SYMBOL` | 做市标的（默认 `SOL_USDC_PERP`）。 |
| `QTY` | 基础挂单数量；会按交易所 `stepSize` 自动截断。 |
| `POST_ONLY` | 是否仅挂 Maker 单，避免吃单（默认 `true`）。 |
| `MIN_SPREAD_TICKS` | 只有点差≥该阈值才会挂单。 |
| `BID_OFFSET_TICKS`/`ASK_OFFSET_TICKS` | 买/卖挂单相对 best bid/ask 的档位偏移。 |
| `SOFT_POS`/`HARD_POS` | 软/硬仓位阈值，用于调整/暂停挂单。 |
| `HEDGE_TRIGGER`/`HEDGE_CHUNK` | 净仓位触发对冲的阈值与单次对冲量。 |
| `HEDGE_MODE` | 对冲模式：`market` 或 `limit`（IOC）。 |
| `REFRESH_MS` | 最快重新挂单周期（毫秒）。 |
| `WS_STALE_MS` | WebSocket 行情过期阈值，超时自动停止更新报价。 |
| `DRY_RUN` | 置为 `true` 时仅打印动作，不真正下单。 |

更多字段及默认值可参考仓库中的 `backpack.env`。

## 运行提示
- **测试环境**：建议先在 `DRY_RUN=true` 下观察日志，再切换实盘配置。
- **代理设置**：若 WebSocket 报 `latin-1` 编码错误，可尝试清除 `http_proxy`/`https_proxy` 环境变量。
- **风控**：合理设置 `HARD_POS`/`HEDGE_*` 参数，以免高波动时仓位失控。

## 目录结构
```
├── backpack_MMbot.py   # 主脚本
├── backpack.env        # 参数示例
└── README.md           # 使用说明（本文件）
```

## 许可证
若无特殊说明，本仓库默认以私有用途为主。若需开源或商业使用，请自行补充许可证或与作者沟通。
