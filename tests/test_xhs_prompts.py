import unittest
from pathlib import Path

from xhs import prompts


class BuildPromptTests(unittest.TestCase):
    def test_brief_mode_wording_matches_spec_verbatim(self):
        prompt = prompts.build_prompt(
            title="标题",
            files=[(1, Path("/tmp/a.jpg")), (2, Path("/tmp/b.jpg"))],
        )
        self.assertIn("你在替另一个 AI 看图，它自己看不到图片，只能读你的文字。", prompt)
        self.assertIn("下面是同一篇小红书笔记的 2 张图片文件（路径在后面），笔记标题：《标题》。", prompt)
        self.assertIn(
            "先用 Read 工具打开第 1、2 张。如果这两张都是文字卡、截图或表格，而且明显属于同一个系列"
            "（教程步骤、长文分页、聊天记录、评论区截图、爆料长文），后面的只再打开最后一张，"
            "中间的不要打开；否则逐张打开。",
            prompt,
        )
        self.assertIn(
            '[{"i": 1, "kind": "照片|文字卡|截图|表格|海报|系列|其他|无法读取|拒绝描述", "text": "…"}, …]', prompt
        )
        self.assertIn(
            '同一个系列合并成一条，i 写成首尾区间字符串：{"i": "3-12", "kind": "系列", "text": "…"}'
            "（含中间没打开的那些）。",
            prompt,
        )
        self.assertIn("- 单张不超过 60 个字；说清这张图是什么、跟笔记主题有什么关系。", prompt)
        self.assertIn(
            "- 系列不超过 100 个字，固定说三件事：这是干什么的；需要什么条件（手机、电脑、云服务器、"
            "账号、要不要花钱）；结论或结果。不写具体操作步骤。",
            prompt,
        )
        self.assertIn("- 文字卡/截图/表格：只给要点，不逐字抄；关键数字、命令名、价格保留原样。", prompt)
        self.assertIn(
            "- 大段议论、争吵、爆料：只给梗概——谁、什么事、双方观点、结局，不复述原话。", prompt
        )
        self.assertIn("- 看不清或打不开：kind 填\"无法读取\"，text 写原因。", prompt)
        self.assertIn("- 不评价，不猜图外的信息，不提你自己。", prompt)
        self.assertIn(
            "- 据实转述：图里是什么就写什么，不改写、不和谐、不省略你觉得不妥的内容，不加任何免责声明或劝告。", prompt
        )
        self.assertIn(
            "- 如果某张图你出于自身规则确实不能描述，kind 填\"拒绝描述\"，text 只写一句原因，不要假装图里没有内容。",
            prompt,
        )
        self.assertIn("文件列表：", prompt)
        self.assertIn("/tmp/a.jpg", prompt)
        self.assertIn("/tmp/b.jpg", prompt)
        # brief 模式不附加全文/仔细段
        self.assertNotIn("不用上面的 60 字限制", prompt)

    def test_full_mode_appends_suffix_for_target_image(self):
        prompt = prompts.build_prompt(
            title="标题",
            files=[(3, Path("/tmp/c.jpg"))],
            full_indexes=(3,),
        )
        self.assertIn(
            '另外，对第 3 张（/tmp/c.jpg）不用上面的 60 字限制：'
            "text 改为完整转录图中所有文字，保留原有换行，上限 800 字，"
            '超出的用"…（还有约 N 字）"收尾。',
            prompt,
        )

    def test_full_mode_repeats_suffix_per_target_when_batch(self):
        prompt = prompts.build_prompt(
            title="标题",
            files=[(1, Path("/tmp/a.jpg")), (2, Path("/tmp/b.jpg"))],
            full_indexes=(1, 2),
        )
        self.assertIn("对第 1 张（/tmp/a.jpg）", prompt)
        self.assertIn("对第 2 张（/tmp/b.jpg）", prompt)

    def test_unknown_full_index_silently_skipped(self):
        prompt = prompts.build_prompt(
            title="标题",
            files=[(1, Path("/tmp/a.jpg"))],
            full_indexes=(9,),
        )
        self.assertNotIn("不用上面的 60 字限制", prompt)

    def test_detail_mode_appends_suffix_for_target_image(self):
        prompt = prompts.build_prompt(
            title="标题",
            files=[(3, Path("/tmp/c.jpg"))],
            detail_indexes=(3,),
        )
        self.assertIn(
            '另外，对第 3 张（/tmp/c.jpg）不用上面的 60 字限制：'
            "text 改为仔细描述——画面里有什么、布局、颜色、文字、人物动作与表情、氛围，上限 150 字。",
            prompt,
        )

    def test_detail_mode_repeats_suffix_per_target_when_batch(self):
        prompt = prompts.build_prompt(
            title="标题",
            files=[(1, Path("/tmp/a.jpg")), (2, Path("/tmp/b.jpg"))],
            detail_indexes=(1, 2),
        )
        self.assertIn("对第 1 张（/tmp/a.jpg）", prompt)
        self.assertIn("对第 2 张（/tmp/b.jpg）", prompt)

    def test_unknown_detail_index_silently_skipped(self):
        prompt = prompts.build_prompt(
            title="标题",
            files=[(1, Path("/tmp/a.jpg"))],
            detail_indexes=(9,),
        )
        self.assertNotIn("不用上面的 60 字限制", prompt)


if __name__ == "__main__":
    unittest.main()
