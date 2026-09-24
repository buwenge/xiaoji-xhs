"""读图提示词——灵魂文件（设计文档第 9 节"S3.2 起的新原文"）。措辞不许改，
只许填参数：`{n}`/`{title}`/`{paths}`/`{k}`/`{path_k}`。
"""

from __future__ import annotations

from pathlib import Path

BASE_TEMPLATE = (
    "你在替另一个 AI 看图，它自己看不到图片，只能读你的文字。\n"
    "下面是同一篇小红书笔记的 {n} 张图片文件（路径在后面），笔记标题：《{title}》。\n"
    "先用 Read 工具打开第 1、2 张。如果这两张都是文字卡、截图或表格，而且明显属于同一个系列"
    "（教程步骤、长文分页、聊天记录、评论区截图、爆料长文），后面的只再打开最后一张，中间的不要打开；"
    "否则逐张打开。\n"
    "然后只输出一个 JSON 数组，不要任何别的文字，不要代码块标记：\n"
    '[{{"i": 1, "kind": "照片|文字卡|截图|表格|海报|系列|其他|无法读取|拒绝描述", "text": "…"}}, …]\n'
    '同一个系列合并成一条，i 写成首尾区间字符串：{{"i": "3-12", "kind": "系列", "text": "…"}}'
    "（含中间没打开的那些）。\n"
    "text 的要求：\n"
    "- 单张不超过 60 个字；说清这张图是什么、跟笔记主题有什么关系。\n"
    "- 系列不超过 100 个字，固定说三件事：这是干什么的；需要什么条件（手机、电脑、云服务器、账号、"
    "要不要花钱）；结论或结果。不写具体操作步骤。\n"
    "- 文字卡/截图/表格：只给要点，不逐字抄；关键数字、命令名、价格保留原样。\n"
    "- 大段议论、争吵、爆料：只给梗概——谁、什么事、双方观点、结局，不复述原话。\n"
    "- 看不清或打不开：kind 填\"无法读取\"，text 写原因。\n"
    "- 不评价，不猜图外的信息，不提你自己。\n"
    "- 据实转述：图里是什么就写什么，不改写、不和谐、不省略你觉得不妥的内容，不加任何免责声明或劝告。\n"
    "- 如果某张图你出于自身规则确实不能描述，kind 填\"拒绝描述\"，text 只写一句原因，不要假装图里没有内容。\n"
    "文件列表：\n"
    "{paths}"
)

FULL_MODE_SUFFIX = (
    "\n另外，对第 {k} 张（{path_k}）不用上面的 60 字限制："
    "text 改为完整转录图中所有文字，保留原有换行，上限 800 字，"
    '超出的用"…（还有约 N 字）"收尾。'
)

DETAIL_MODE_SUFFIX = (
    "\n另外，对第 {k} 张（{path_k}）不用上面的 60 字限制："
    "text 改为仔细描述——画面里有什么、布局、颜色、文字、人物动作与表情、氛围，上限 150 字。"
)


def build_prompt(
    *,
    title: str,
    files: list[tuple[int, Path]],
    full_indexes: tuple[int, ...] = (),
    detail_indexes: tuple[int, ...] = (),
) -> str:
    """`files` 是这一次要打包读的 (笔记内图片序号, 本地文件路径) 列表；
    `full_indexes`/`detail_indexes` 是其中要分别追加"逐字转录"（读图 N
    全文）/"仔细描述"（读图 N 仔细）段的那些序号，其余走默认 60 字简述
    （可能被模型自己判定合并成系列）。两者理论上不会同时非空——`cli.py`
    的 `读图` 命令一次只选一种 mode。"""
    paths_block = "\n".join(f"{index}. {path}" for index, path in files)
    prompt = BASE_TEMPLATE.format(n=len(files), title=title, paths=paths_block)
    path_by_index = {index: path for index, path in files}
    for k in full_indexes:
        path_k = path_by_index.get(k)
        if path_k is None:
            continue
        prompt += FULL_MODE_SUFFIX.format(k=k, path_k=path_k)
    for k in detail_indexes:
        path_k = path_by_index.get(k)
        if path_k is None:
            continue
        prompt += DETAIL_MODE_SUFFIX.format(k=k, path_k=path_k)
    return prompt


# `抽帧`（9/24 新增，不属于设计文档第 9 节原文）：小机的原话原样转给识图
# agent，由它自己决定在哪儿截、截几帧（9/24 用户拍板：跟小院子送礼便签
# 一样照转，不替他套格式）。{command} 是截帧工具的完整命令前缀。
FRAMES_AGENT_TEMPLATE = (
    "你在替另一个 AI（小机）看一条小红书视频，他自己看不到画面，只能读你的文字。\n"
    "笔记标题：《{title}》，视频全长 {duration}。\n"
    "{desc_block}"
    "他想怎么看，原话是：「{request}」\n"
    "\n"
    "按他的意思决定在哪些时间点截帧。截帧只能用这一条命令，时间点写成 分:秒 或 时:分:秒，"
    "一次可以写多个、用空格隔开，例如：\n"
    "{command} 0:30 1:45\n"
    "命令会返回每一帧的图片路径，再用 Read 工具打开来看。整个任务最多截 {max_frames} 帧，"
    "可以先粗看几帧、再往他关心的地方补。除了这条命令和 Read，不要用任何别的命令或工具。\n"
    "他没说清楚要怎么抽的，就按\"大致弄明白这条视频在干什么\"来，6 帧左右，均匀分布、避开开头和结尾的黑屏。\n"
    "\n"
    "最后只输出给他看的文字，不要代码块标记：\n"
    "- 按时间顺序一帧一行，行首写时间点：画面里是什么、人在做什么，不超过 80 字。\n"
    "- 画面上有字幕、大字标题、弹出的文字，原样抄进「」里（一帧最多抄 60 字）；"
    "外语字幕旁边有中文的只抄中文，没有中文就译成中文并标（译）。\n"
    "- 跟前一帧几乎一样的，只写\"画面跟前一帧差不多\"，再补字幕。\n"
    "- 他问了具体问题的，最后用一两句直接回答；看不出来就说看不出来。\n"
    "- 不评价，不猜画面外的信息，不提你自己，不讲截帧的过程。\n"
    "- 据实转述：画面里是什么就写什么，不改写、不和谐、不省略你觉得不妥的内容，不加任何免责声明或劝告。\n"
    "- 某一帧出于自身规则确实不能描述，那一行写\"不能描述\"加一句原因，不要假装画面里没有内容。\n"
    "- 总共不超过 1200 字。"
)


def build_frames_prompt(
    *, title: str, desc: str, duration: str, request: str, command: str, max_frames: int
) -> str:
    desc_block = f"笔记正文（节选）：{desc}\n" if desc else ""
    return FRAMES_AGENT_TEMPLATE.format(
        title=title,
        duration=duration,
        desc_block=desc_block,
        request=request,
        command=command,
        max_frames=max_frames,
    )
