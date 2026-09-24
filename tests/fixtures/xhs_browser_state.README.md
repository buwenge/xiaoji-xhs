# xhs_feed_feeds.json / xhs_note_detail.json 来源说明（S3 浏览器路径 fixture）

- 2026-09-22 05:00 由维护者在 VPS 上**匿名**（无登录态、临时 profile、已销毁）用 Xvfb `:99` + 系统
  `google-chrome-stable` 153（设计文档第 7 节 S3 的启动参数）+ Playwright `connect_over_cdp` 打开
  `https://www.xiaohongshu.com/explore`、`/search_result?keyword=猫咪零食&source=web_explore_feed`、
  `/explore/<id>?xsec_token=…&xsec_source=pc_feed` 三个页面，`page.evaluate` 读 `window.__INITIAL_STATE__`
  抽出来的真实片段。内容是公开笔记与公开评论，未改动正文；手机号形状的 11 位数字全是时间戳/ID 片段。
- **浏览器里的 `__INITIAL_STATE__` 已经是 Vue 响应式对象**（跟 S1 从 HTML 文本里正则抽出来的纯 JSON 不同）：
  `feed.feeds`、`search.feeds`、`note.currentNoteId`、`user.loggedIn` 都是 ref（`__v_isRef: true`，
  真值在 `._rawValue`，备用 `._value`）；`note.noteDetailMap` 是普通 reactive 对象、可直接取。
  两个 fixture 存的都是**解包之后**的值（`JSON.stringify` 时用 replacer 把 ref 换成 `_rawValue`）。
- `xhs_feed_feeds.json`：`feed.feeds` 解包后前 3 项（原始 30 项；第 3 项换成了列表里第一条 `type: "video"`
  的，保证 normal/video 两种都有）。每项 `{id, xsecToken, modelType, trackId, index, noteCard:{displayTitle,
  type, user:{nickname,userId,xsecToken}, interactInfo:{likedCount:"517"}, cover:{urlDefault,…}}}`。
  搜索页 `search.feeds` **匿名为空数组**（页面盖"登录后查看搜索结果"弹窗），没抓到真实搜索 fixture；
  网上多个实现与本次 `search` 顶层键（`feeds`、`hasMore`、`searchFeedsWrapper`、`filters`…）都指向
  搜索项与 feed 项同为 `noteCard` 形状，S3 先按同一形状解析，登录后由维护者核对。
- `xhs_note_detail.json`：`{"noteDetailMap": {<id>: {"note", "comments", "currentTime"}}, "currentNoteId"}`，
  即 `note.noteDetailMap` 里那一条（去掉 `widgets`/`seoRobots`）。`comments.list` 原 10 条留 3 条：
  `subCommentCount` 分别为 `"10+"`（`subCommentHasMore: true`）、`"7"`（true）、`"1"`（false）。
  - `note` 字段与 S1 fixture 的 `noteData.data.noteData` 一致（noteId/title/desc/imageList/user/interactInfo/
    time/tagList/xsecToken），**唯一差异：作者是 `user.nickname`（小写 n），S1 是 `user.nickName`**。
  - 评论字段与 S1 的 `commentData.comments` **不同**：`userInfo.nickname`（S1 `user.nickname`）、
    `createTime`（S1 `time`）、`likeCount`/`subCommentCount` 是**字符串**且可能带 `+`（S1 是 int）、
    `subComments` 预载 1 条（S1 预载 2–3 条）、多了 `subCommentHasMore`/`subCommentCursor`/`expended`。
    楼中楼每条 `{id, content, userInfo, createTime, targetComment, likeCount, ipLocation}`。
  - `comments` 顶层：`{list, cursor, hasMore, loading, firstRequestFinish}`；匿名首屏 10 条后是
    `.comments-login`"登录查看全部评论内容"。
- 登录态标志：`user.loggedIn`（ref）。匿名时 DOM 里有 `div.reds-modal.reds-modal-open.login-modal` 与侧栏
  `button#login-btn`。

## 站点 DOM 普查（同一次匿名抓取，供 `xhs/selectors.py` 起草；登录态下的差异待登录后核对）

| 用途 | 选择器 / 事实 |
|---|---|
| 登录弹窗 | `.login-modal`（`div.reds-modal.reds-modal-open.login-modal`）> `.login-container`，左栏 `.left`（`.login-reason` 文案"登录后推荐更懂你的笔记"/"登录后查看搜索结果"、`.qrcode` > `img.qrcode-img`），右栏 `.right`（"手机号登录"表单） |
| 二维码 | `img.qrcode-img` 的 `src` 是 `data:image/png;base64,…`，原图 128×128，页面显示约 160px |
| 手机号/验证码表单 | `.login-container .right form`：手机号 `input`、`label.auth-code input`、`span.code-button`"获取验证码"、`button.submit`"登录"、`.user-tips`"新用户可直接登录" |
| 首页列表 | `.feeds-page .feeds-container`，每条 `section.note-item`（30 个），封面 `.cover`；结构化数据一律从 state 读，DOM 只用于截图/滚动 |
| 搜索页 | `.search-layout`，输入框 `.search-input`；匿名时 `.note-item` 0 个 |
| 详情弹层 | `#noteContainer`（`div.note-container`），滚动容器 `.note-scroller`，正文 `.note-content`（`.desc`），互动栏 `.interact-container` |
| 评论区 | `.comments-el > .comments-container`，总数 `.total`（"共 3277 条评论"），顶层每条 `.parent-comment`（内含 `.comment-item` + `.reply-container`），楼中楼 `.comment-item-sub`，展开按钮 `.reply-container .show-more`（文案"展开 6 条回复"），评论配图 `.comment-picture`/`.comment-image-gallery` |
| 评论区末尾 | 匿名是 `.comments-login`（"登录查看全部评论内容" + `button.to-login`）与 `.tips-el`（"去首页，发现更多笔记"）；登录态到底文案（疑为 "- THE END -"）待核对 |
| 详情 URL | `https://www.xiaohongshu.com/explore/<id>?xsec_token=<urlencoded>&xsec_source=pc_feed`（首页来的）/`pc_search`（搜索来的）；从 explore 点开是弹层、直接 `goto` 也能开 |
| 验证码/风控页 | 本次匿名未撞到，无实测选择器 |
