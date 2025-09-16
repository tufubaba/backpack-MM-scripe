# Backpack Scalper

## 项目简介

Backpack Scalper 是一个用于自动化交易的机器人，旨在连接到 Backpack 交易所，通过 WebSocket 和 REST API 实现市场数据处理、下单、撤单以及对冲策略。该项目旨在帮助用户在加密货币市场中进行高效的交易。

## 文件结构

```
backpack-scalper
├── src
│   └── backpack_scalper.py      # 主要的交易机器人实现
├── requirements.txt              # 项目所需的Python依赖库
├── .env.example                   # 环境变量的示例配置
├── .gitignore                     # 版本控制中应忽略的文件和目录
├── LICENSE                        # 项目的许可证信息
└── README.md                      # 项目的文档
```

## 安装

1. 克隆该项目到本地：

   ```
   git clone <项目的Git仓库地址>
   cd backpack-scalper
   ```

2. 创建并激活虚拟环境（可选）：

   ```
   python -m venv venv
   source venv/bin/activate  # 在Linux或MacOS上
   venv\Scripts\activate     # 在Windows上
   ```

3. 安装依赖库：

   ```
   pip install -r requirements.txt
   ```

4. 配置环境变量：

   复制 `.env.example` 文件为 `.env`，并根据需要填写相关的环境变量。

## 使用

1. 运行交易机器人：

   ```
   python src/backpack_scalper.py
   ```

2. 根据需要调整配置文件中的参数，以优化交易策略。

## 许可证

该项目遵循 [MIT 许可证](LICENSE)。请查看许可证文件以获取更多信息。

## 贡献

欢迎任何形式的贡献！请提交问题或拉取请求以帮助改进该项目。