# Digital Me — 打造你自己的 AI 数字分身

一个基于真实个人数据（社交、观影、听歌、阅读、博客）的 AI 数字分身项目。它用你的语气说话，记得你看过的电影、听过的歌、发过的牢骚——像一个会聊天的"你自己"。

**技术栈：** Python 3 + 原生 HTTP 服务 + SQLite + 任意 OpenAI 兼容 API

如果你对这个项目的来龙去脉感兴趣，可以读这篇博客：[打造自己的AI数字分身](/Users/ztshia/.openclaw-autoclaw/workspace/数字分身-续写完整版.md)。

---

## 功能

- 🤖 **AI 聊天** — 基于你的真实数据，用你的语气回答问题
- 🔍 **语义检索** — 从你的过往记忆中自动匹配相关内容
- 📊 **管理后台** — 查看访客对话记录、切换 AI 模型、统计地域分布
- 🎨 **明暗主题** — 支持浅色/深色切换，适配系统偏好
- 🔌 **博客嵌入** — 可作为 Widget 嵌入个人博客，右下角弹出聊天窗口
- 🐳 **Docker 部署** — 单容器运行，挂载目录热更新数据

---

## 快速开始

```bash
# 1. 克隆项目
git clone https://github.com/yourname/digitalme.git
cd digitalme

# 2. 准备数据
cp app/ persona-data.example.js app/persona-data.js
# 编辑 persona-data.js，填入你的数据（见下方「数据准备」）

# 3. 配置 API
cp app/server-config.example.json app/server-config.json
# 编辑 server-config.json，填入模型 API Base URL 和 Key

# 4. 启动
python3 app/server.py
# 浏览器打开 http://localhost:5616
```

---

## 数据准备

数据是分身的灵魂。你需要准备以下数据，填入 `app/persona-data.js`（文件结构见 `persona-data.example.js`）：

### 基础必填

| 字段 | 说明 | 最低要求 |
|------|------|---------|
| `PERSONA` | 你是谁？基本身份、说话风格、价值观边界、雷区 | 1 段描述 |
| `SAMPLES` | 真实发言样本 | ≥ 20 条 |

### 增强可选

| 字段 | 说明 | 来源 |
|------|------|------|
| `FACTS` | 确定事实列表（年龄、职业、教育等） | 自行整理 |
| `KNOWLEDGE` | 语义检索知识库 | 社交数据清洗 |
| `MOVIE` | 观影记录 + 演员对照表 | 豆瓣 / NeoDB |
| `MUSIC` | 听歌记录 | 网易云 API / Spotify API |
| `YEARLY` | 按年份的记忆摘要 | 社交数据 + 自行整理 |
| `POSTS` | 博客文章摘要 | WordPress / Typecho 导出 |
| `RECENT` | 近期动态 | 最近 1-3 个月数据 |

### 数据来源建议

根据原博客作者的经验，以下数据源比较实用：

| 数据类型 | 平台 | 导出方式 |
|----------|------|---------|
| 社交短内容 | 饭否 | [饭否消息备份工具](https://export.fanfou.pro) |
| | Twitter/X | 设置 → 下载数据存档 |
| | 微博 | Chrome 插件导出（已注销帐号也可用） |
| | Mastodon | 服务端一键导出脚本 |
| 观影 | 豆瓣 | doumark-action（GitHub Action 自动同步） |
| | NeoDB | 自建服务器 API |
| 听歌 | 网易云音乐 | NAS 搭建 API 接口（[NeteaseCloudMusicApi](https://github.com/Binaryify/NeteaseCloudMusicApi)） |
| | Spotify | Spotify Web API |
| 阅读 | RSS | 自建阅读器 + Python 脚本每日导出 |
| 文章 | 博客 | WordPress XML / Typecho 数据库 / Markdown 文件 |

> 💡 **数据处理建议：** 把原始数据导出为 JSON 后，建议先写一个清洗脚本：删空消息、合并重复、去除纯表情和语气词。原始 JSON 噪音很大，直接喂给 AI 会被「呵呵」总结为「关于青春与梦想」。

---

## 部署指南

### 方式一：本地运行（适合开发调试）

```bash
# 安装 Python 3.12+
python3 app/server.py

# 或指定端口和监听地址
PORT=8080 HOST=0.0.0.0 python3 app/server.py
```

### 方式二：Docker 部署（适合 NAS / VPS）

```bash
# 构建并启动
docker compose up -d --build

# 停止
docker compose down

# 重启
docker compose restart
```

容器内默认监听 `0.0.0.0:5616`。数据文件 `persona-data.js` 和 `server-config.json` 通过卷挂载到宿主目录，修改后**无需重建容器**即可生效。

### 方式三：嵌入博客

聊天功能的 API 端点（`/proxy` 和 `/api/config`）支持 CORS。如果你想把聊天窗口嵌入自己的博客：

1. 编辑 `app/server.py`，在 `CORS_ORIGINS` 中添加你的博客域名：

```python
CORS_ORIGINS = (
    'https://yourblog.com',
    'http://localhost:4173',  # 本地开发保留
)
```

2. 博客 HTML 中引入聊天窗口（参考 `index.html` 的实现逻辑）。

> ⚠️ 个人域名建议套 CDN（如多吉云）隐藏源站 IP。

---

## 管理后台

访问 `http://your-host:5616/login`，默认账号密码：
- 用户名：`admin`
- 密码：`admin123`（可通过环境变量 `DM_ADMIN_PASS` 覆盖）

后台功能：
- **对话管理** — 按 IP 分组查看访客对话历史、按省/市筛选
- **安全管理** — 修改管理员密码
- **模型管理** — 添加/切换/测试多个 AI 模型方案（支持火山方舟、DeepSeek、商汤等）

---

## 项目结构

```
digitalme/
├── docker-compose.yml         # Docker 编排
├── Dockerfile                 # 镜像构建
├── README.md
└── app/
    ├── server.py              # 服务入口：静态页面 + /proxy 代理 + 管理 API
    ├── index.html             # 前端聊天页面
    ├── admin.html             # 管理后台页面
    ├── login.html             # 登录页
    ├── persona.py       # 人格注入模块（系统提示词 + 语义检索）
    ├── admin_db.py         # 后台逻辑（SQLite 会话/日志/配置读写）
    ├── persona-data.example.js  # ★ 数据模板（你的数据填这里）
    ├── server-config.example.json # ★ 模型配置模板
    ├── persona-data.js          # 你的数据文件（gitignore，不提交）
    ├── server-config.json     # 你的模型配置（gitignore，不提交）
    ├── admin_db.db         # SQLite 数据库（gitignore，自动生成）
    ├── avatar.svg             # 头像占位图（替换为你的头像）
    ├── logo.svg               # Logo 占位图
    ├── favicon.svg            # 站点图标
    └── vendor/                # 前端依赖（Marked / DOMPurify / FontAwesome）
```

---

## 自定义

### 更换模型

编辑 `server-config.json` 或通过管理后台操作。支持任意 OpenAI 兼容接口：

```json
{
  "presets": [
    {
      "name": "DeepSeek",
      "base": "https://api.deepseek.com/v1",
      "key": "sk-xxx",
      "model": "deepseek-chat"
    }
  ],
  "active": "DeepSeek"
}
```

低成本方案推荐 **DeepSeek V4 Flash**（0.05 倍率），适合个人项目。

### 更换头像 / Logo

- 替换 `app/avatar.svg`（头像）和 `app/logo.svg`（品牌 Logo）
- 支持 SVG / PNG / JPG，修改 `index.html` 中的引用文件名即可

### 调整人格规则

- `persona-data.js` 中的 `PERSONA` 字段控制分身的"人设"
- `SAMPLES` 的条数越多（建议 50-120 条），语气越像你
- 隐私相关规则在 `persona.py` 的 `build_system_prompt()` 函数末尾，可按需调整

---

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `PORT` | 服务端口 | `5616` |
| `HOST` | 监听地址（Docker 内需设 `0.0.0.0`） | `127.0.0.1` |
| `DM_ADMIN_USER` | 管理员用户名 | `admin` |
| `DM_ADMIN_PASS` | 管理员初始密码 | `admin123` |
| `DM_BASE` | API Base URL | 配置文件值 |
| `DM_KEY` | API Key | 配置文件值 |
| `DM_MODEL` | 模型名 | 配置文件值 |

---

## 常见问题

**Q: 分身的回答不像我怎么办？**
A: 检查 `PERSONA` 是否写得太笼统、`SAMPLES` 是否太少（20 条以下效果很差）。建议从饭否/Twitter 挑 50+ 条有代表性的发言。

**Q: AI 会编造我根本没说过的话（幻觉）？**
A: 正常现象。在 `persona.py` 中已加入规则限制（数据里没有的就说不知道）。如果仍有幻觉，检查 `FACTS` 和 `KNOWLEDGE` 是否准确、`PERSONA` 中的描述是否过于泛化。

**Q: Token 用得太快了怎么办？**
A: 两种方式控制开销：① 在 `server-config.json` 中降低 `max_tokens`（回复长度上限）；② 换用更便宜的模型（推荐 DeepSeek V4 Flash）。

**Q: 如何让分身"学会"新的记忆？**
A: 更新 `persona-data.js` 中的对应字段（`RECENT` 放近期内容，`KNOWLEDGE` 放长期记忆），重启服务即可（Docker 卷挂载模式下无需重建容器）。

**Q: 微信小程序能用吗？**
A: 腾讯规定个人开发者不得开发含 AI 文本对话功能的小程序。建议用网页版 + 博客嵌入的组合，或尝试 Telegram Bot。

---

## License

MIT
