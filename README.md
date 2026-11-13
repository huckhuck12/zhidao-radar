# zhidao-radar

🎯 **雷达扫描「百度知道」上的求资源问题，自动应答分享资源**

## ✨ 特性

- 🔍 **真实浏览器检索**：Playwright 驱动 Chromium 执行 `site:zhidao.baidu.com` 站内检索，突破 HTTP 空响应与安全验证
- 🛡️ **风控退避**：遇到百度安全验证自动延迟重试，不硬刚
- 🎯 **智能分类**：关键词规则识别「求资源/求下载/求分享」类问题
- 📚 **资源库管理**：按关键词自动匹配最合适的资源
- 🤖 **自动答题**：Playwright 模拟浏览器提交答案（带资源链接+提取码）
- 📊 **可视化面板**：Streamlit 一站式操控 + 统计仪表盘

## 🚀 快速开始

### 1. 安装

```bash
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\playwright install chromium
```

### 2. 配置

复制模板文件并按需修改：

```bash
copy data\settings.example.json data\settings.json
copy data\resources.example.json data\resources.json
```

- `settings.json`：填入你的百度 Cookie、搜索关键词、节流参数
- `resources.json`：填入你的资源库（百度云/夸克链接 + 提取码 + 关键词）

### 3. 启动面板

```bash
venv\Scripts\streamlit run app.py
```

浏览器打开 http://localhost:8501

## 📂 项目结构

```
zhidao-radar/
├── app.py               # Streamlit 可视化面板（主入口）
├── crawler.py           # 问题检索 + 详情爬取
├── classifier.py        # 求资源问题分类器
├── resources.py         # 资源库管理
├── answerer.py          # Playwright 自动答题
├── login_helper.py      # 手动登录并保存会话（data/login_state.json）
├── config.py            # 设置持久化
├── data/
│   ├── settings.example.json    # 配置模板
│   ├── resources.example.json   # 资源库模板
│   ├── settings.json            # 你的配置（gitignore）
│   ├── resources.json           # 你的资源（gitignore）
│   ├── collected.json           # 采集的问题（运行时）
│   └── answered.json            # 已答记录（运行时）
└── requirements.txt
```

## ⚠️ 注意事项

- Cookie 有效期约 1–2 周，失效后重新登录复制
- 答题间隔默认 15s，过密会被百度风控
- 资源库中请只放你**有权分享**的资源，注意版权合规
- 本工具仅供学习交流，请勿用于违反百度用户协议的行为

## 📜 License

MIT
