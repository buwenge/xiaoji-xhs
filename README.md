# xiaoji-xhs：让你的 AI 自己刷小红书

给跑在 Claude Code 上的 AI 伴侣机器人加一个"刷小红书"的能力。AI 在自己的 shell 里敲 `home 小红书 …`，就能读你发给它的笔记链接、自己搜索、刷首页、翻评论、看图、看视频。看到好玩的，还能把原图、截图、分享链接直接发进你们的聊天。你这边能在聊天页顶上实时**偷看它在刷什么**。

这个仓库是从一个自用的伴侣机器人项目（小机）里拆出来的脱敏版。人设口吻（"宝宝"之类）原样保留，按你的习惯改就行。

**只读。** 不点赞、不评论、不关注、不发帖，代码里就没有这些操作。

---

## 它能干什么

| 你/AI 说 | 效果 |
|---|---|
| `home 小红书 看 <链接>` | 读一篇笔记：标题、正文、每张图一句话描述、前几条评论和楼中楼条数 |
| `home 小红书 搜索 猫咪零食` / `首页` / `更多` | 自己找东西看，列表带序号 |
| `home 小红书 看 3` | 打开列表里第 3 条 |
| `home 小红书 评论 10条` / `评论 3 展开` | 往下翻评论、展开某条的楼中楼 |
| `home 小红书 读图 2 全文` / `读图 2 仔细` | 某张图的全部文字 / 让更强的模型仔细描述 |
| `home 小红书 抽帧 <想怎么看>` | 视频笔记：把原话转给识图帮手，它自己决定在哪几个时间点截帧（最多 12 帧） |
| `home 小红书 发原图 1 3` / `截图` / `截图 评论 3` / `发帧 1:00` / `分享链接` | 把看到的东西直接发进聊天给你看 |
| `home 小红书 今天` | 今天刷了多少 |
| `home 小红书 放下` | 关掉浏览器 |

完整命令表：`home 小红书 帮助`。命令有同义词兜底，"搜 xx""找 xx""看第三条""截屏"都能认。

### 偷看它刷

AI 一开浏览器，聊天页顶上就会冒出一个小条。点开是手机比例的浮层，每半秒刷新一次画面，看到的就是它屏幕上正在看的东西。画面是 ffmpeg 每秒 2 帧覆写进内存（`/dev/shm`）的**最新一张**，不落盘、不录像。浏览器空闲 10 分钟会自己关。

### 省上下文的设计

- **AI 自己不看图。** 图片交给一个隔离的无头 `claude -p` 子进程（默认 sonnet）批量描述，AI 只读到文字。系列文字卡只读首尾两张再合并成一行，视频只下封面。
- **预制提醒。** 当天读到 10k / 15k / 20k token 时，输出顶部会冒一句提醒（文案在 `xhs/budget.py`，改成你自己的话）。当天已经刷过 15k、隔了 5 小时以上又回来刷，会先问一句"确定还要刷吗？"。
- 下载的图保留 24 小时，读图描述缓存 7 天，发给你的图永久保留（存在你的 daemon 里）。

---

## 结构

```
AI（Claude Code 会话）
  │  home 小红书 …
  ▼
bin/home → home.py ─► xhs/cli.py（命令解析、派发）
                        ├─ fetch_light.py   轻量路径：只读分享链接，不开浏览器
                        ├─ browser.py       浏览器路径：Playwright 连 CDP 操作页面
                        │    ├─ pacing.py   真人节奏：打字搜索、点卡片、分段滚动、每天次数上限
                        │    └─ keeper.py   看守进程：Xvfb + 系统 Chrome + ffmpeg 截帧，空闲自动退出
                        ├─ vision.py / video.py  读图、抽帧（无头 claude -p 子进程）
                        ├─ share.py         发原图/截图/链接 → 你的 daemon HTTP
                        └─ budget.py        当天 token 计数与预制提醒

你的 daemon（aiohttp）                          你的前端（Next.js）
  ├─ POST /api/xiaoji/share   ◄─ share.py        ├─ LinkCard / PhotoStackAlbum（气泡里的分享）
  ├─ GET  /api/xhs/screen     ── 最新一帧 ──►    ├─ XhsLiveView（偷看浮层）
  ├─ GET  /api/xhs/live                          └─ useXhsLive
  └─ 每 5 秒 LiveStatusJob ── WS xhs_live ──►
```

| 路径 | 内容 |
|---|---|
| `home.py`、`bin/home` | 命令入口（公开版只带小红书这一个类别） |
| `xhs/` | 核心包，拷走就能用 |
| `daemon_api.py`、`headless_claude.py`、`token_estimate.py`、`cn_numerals.py`、`log_store.py`、`file_io.py` | xhs 包依赖的小工具模块 |
| `push_notify.py` | **占位**，换成你 daemon 的 WebSocket 广播函数 |
| `integration/` | 原版 daemon 里的接线**摘录**（路由、定时任务、分享接口），不是能直接 import 的文件 |
| `frontend/` | 前端组件与 `接入说明.md` |
| `docs/给AI的用法说明.md` | 贴进你机器人系统提示的教法段落 |
| `tests/` | 456 条测试，全部离线，不连小红书、不起真浏览器 |

---

## 安装

在一台 Linux 服务器上（原版跑在 Ubuntu VPS）：

```bash
# 1. 系统依赖
sudo apt install xvfb ffmpeg
# 系统版 Google Chrome（keeper.py 调用的是 google-chrome-stable）
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install ./google-chrome-stable_current_amd64.deb

# 2. Claude Code CLI（读图子进程要用），装好并登录
#    长期跑的话建议 `claude setup-token` 生成令牌，存成一行
#    CLAUDE_CODE_OAUTH_TOKEN=... 放到 ~/.claude-oauth-token.env

# 3. 本仓库
git clone https://github.com/buwenge/xiaoji-xhs /opt/xiaoji-xhs
cd /opt/xiaoji-xhs
python3 -m venv venv && venv/bin/pip install -r requirements.txt
# Playwright 只用来连系统 Chrome，不需要 `playwright install` 下浏览器

# 4. 入口命令
sudo cp bin/home /usr/local/bin/home
home 小红书 帮助
```

配置都有默认值，想改的看 `.env.example`。**`XHS_DIR` 必须在你机器人仓库的外面**：读图子进程会往上找 `CLAUDE.md`，放在仓库里面会把你机器人的人设文件整份塞进每次读图请求。

### 登录

搜索、首页、翻全部评论都要登录，只读分享链接不用。**请用小号。**

- `home 小红书 登录`：打开登录页，把二维码发进聊天（图片也会存在 `XHS_DIR` 下），用小红书 app 扫码
- `home 小红书 登录 状态`：看登没登上
- `home 小红书 登录 验证码 123456`：碰上手机验证码时用

登录态存在 `XHS_DIR/profile/`，别提交、别分享。

### 接进你的机器人

1. **让 AI 知道有这个命令**：把 `docs/给AI的用法说明.md` 里那段贴进它的系统提示或 CLAUDE.md。
2. **daemon**：照 `integration/daemon_integration.py` 挂三个路由和一个定时任务，把 `push_notify.py` 换成你的广播函数。`integration/assistant_share.py` 是分享接口的原版实现，里面的 `chat_upload`、`history_store` 换成你自己的上传和聊天记录模块。
3. **前端**：看 `frontend/接入说明.md`。
4. 只想让 AI 读链接、不要分享和偷看的话，第 2、3 步可以都不做。`看`、`搜索`、`评论`、`读图` 这些照样能用，只有 `发原图`、`截图`、`分享链接` 会报"发不出去"。

---

## 注意事项

- **封号风险自负。** 机房 IP 加自动化浏览器很容易触发小红书风控，请用小号，别拿主号登录。
- **浏览节奏是故意放慢的。** 为了更接近真人操作习惯，搜索是在搜索框里逐字打字再回车，看第几条是在列表里点卡片，滚动分成小段，点之前鼠标先移过去，进笔记会停几秒、翻几张图（`xhs/pacing.py`）。每条浏览器命令因此多等 5–15 秒，AI 这边的命令和输出不变。每天最多开 60 次浏览器，上限在 `pacing.py` 顶部改。这只能降低风险：机房 IP 和自动化浏览器本身的特征没有处理。
- **只做个人阅读。** 这是给自己的 AI 伴侣"陪你刷"用的，不要拿去批量抓取、搬运别人的内容，也不要自己加点赞、评论、关注这类互动操作。自动化互动既违反平台规则，也可能踩到 AI 服务商的使用政策。请遵守小红书的用户协议。
- 站点改版时，DOM 选择器和数据字段都集中在 `xhs/selectors.py` 一处，只改这里。
- 测试数据（`tests/fixtures/`）来自公开笔记，作者和评论者的昵称、ID、头像都已换成占位值。

## 测试

```bash
venv/bin/python -m pytest
```

全部离线：浏览器操作用假对象，看守进程用 `sleep` 冒充 Chrome，所有状态文件都改道到临时目录。有一条测试会真的起一个 Xvfb，没装 Xvfb 的话它会失败，其它测试不受影响。

## 关于代码注释

注释里常见"设计文档第 N 节""审查意见 1.3""S3.1"之类的说法，指的是原项目内部的施工记录和审查记录，没有随仓库公开。保留这些说法是为了让每个"为什么这么写"有个出处，读的时候当成"当时踩过坑、专门这么改的"就行。

## 许可证

MIT，见 `LICENSE`。

多图堆叠效果依赖的 [PhotoStack](https://github.com/Wren036/PhotoStack) 是 PolyForm Noncommercial 许可证，**没有**包含在本仓库里，需要的话去原仓库自取，见 `frontend/接入说明.md`。
