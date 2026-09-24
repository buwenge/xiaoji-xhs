import unittest

from xhs import links


class FindLinkTests(unittest.TestCase):
    def test_short_link(self):
        self.assertEqual(
            links.find_link("看 https://xhslink.cn/o/AbCdEf12345"),
            "https://xhslink.cn/o/AbCdEf12345",
        )

    def test_bare_link_no_prefix(self):
        self.assertEqual(
            links.find_link("https://xhslink.cn/o/AbCdEf12345"),
            "https://xhslink.cn/o/AbCdEf12345",
        )

    def test_full_discovery_item_link(self):
        url = "https://www.xiaohongshu.com/discovery/item/6aae7f7300000000290165a0?xsec_token=ABC&xsec_source=pc"
        self.assertEqual(links.find_link(f"看 {url}"), url)

    def test_explore_link(self):
        url = "https://www.xiaohongshu.com/explore/6aae7f7300000000290165a0"
        self.assertEqual(links.find_link(url), url)

    def test_preserves_case_and_query(self):
        url = "https://www.xiaohongshu.com/discovery/item/AbC123?xsec_token=DeFgHiJ"
        self.assertEqual(links.find_link(url), url)

    def test_strips_trailing_chinese_punctuation(self):
        self.assertEqual(
            links.find_link("看看这篇 https://xhslink.cn/o/AbCdEf12345，好吗"),
            "https://xhslink.cn/o/AbCdEf12345",
        )

    def test_non_xhs_url_ignored(self):
        self.assertIsNone(links.find_link("看 https://example.com/foo"))

    def test_no_url_returns_none(self):
        self.assertIsNone(links.find_link("评论 3 展开"))

    def test_empty_text_returns_none(self):
        self.assertIsNone(links.find_link(""))

    def test_is_xhs_link_rejects_lookalike_host(self):
        self.assertFalse(links.is_xhs_link("https://notxiaohongshu.com/explore/1"))
        self.assertFalse(links.is_xhs_link("https://xiaohongshu.com.evil.example/x"))

    def test_is_xhs_link_accepts_both_hosts(self):
        self.assertTrue(links.is_xhs_link("https://xhslink.cn/o/abc"))
        self.assertTrue(links.is_xhs_link("https://www.xiaohongshu.com/explore/1"))


if __name__ == "__main__":
    unittest.main()
