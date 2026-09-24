# xhs_note_state.json 来源说明

- 2026-09-22 由维护者用手机 UA `curl -L` 抓取公开笔记分享短链 `https://xhslink.cn/o/AbCdEf12345`
  （跳转到 `www.xiaohongshu.com/discovery/item/6aae7f7300000000290165a0?...&xsec_token=...`），
  从 HTML 里的 `window.__INITIAL_STATE__` 抽出 JSON（`undefined` 先替换成 `null` 再 `json.loads`）。
- 只保留 `noteData.data.noteData`（标题/正文/图片列表/作者/互动数）与 `noteData.data.commentData`
  （`commentCount`、`commentCountL1`、首屏 `comments[]` 含 `subCommentCount`/`subComments`），
  其余（`global`、`profile`、`widgets`、`relatedNotes`、`userOtherNotesData` 等）整块删除。
- 内容是公开笔记（作者 某作者《把小红书 MCP 搬上云端服务器》），未改动正文；
  文件里形如手机号的 11 位数字全是毫秒时间戳或评论 id 的片段，不是电话。
- 笔记 7 张图、正文 908 字、评论总数 46、首屏顶层 5 条、第一条楼中楼 4 条（预载 3 条）。
