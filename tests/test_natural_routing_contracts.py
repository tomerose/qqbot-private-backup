import sys
import unittest
from pathlib import Path


PLUGINS = Path(__file__).resolve().parents[1] / "astrbot" / "data" / "plugins"
sys.path.insert(0, str(PLUGINS))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "astrbot"))

from data.plugins.ai_debate.main import parse_debate_topic  # noqa: E402
from data.plugins.ai_interview.main import parse_interview_start  # noqa: E402
from data.plugins.claude_code_agent.natural_router import route_natural_agent  # noqa: E402
from data.plugins.deep_think.main import extract_question  # noqa: E402
from data.plugins.draw_command.draw_core import (  # noqa: E402
    is_dewatermark_request,
    parse_draw_command,
    parse_edit_command,
)
from data.plugins.music_command.main import parse_original_song_prompt, parse_song_search  # noqa: E402
from data.plugins.search_command.main import parse_action_pack  # noqa: E402
from data.plugins.smart_translate.main import parse_translate_request  # noqa: E402
from data.plugins.video_agent.main import _parse_agent_command  # noqa: E402
from data.plugins.video_command.main import _parse_video_command  # noqa: E402
from data.plugins.video_pipeline.main import VideoPipelinePlugin  # noqa: E402
from data.plugins.web_studio.main import parse_web_intent  # noqa: E402


class NaturalRoutingContractTests(unittest.TestCase):
    def test_image_creation_edit_and_watermark_have_distinct_intents(self):
        self.assertEqual(parse_draw_command("帮我画一张雨夜海报"), "雨夜海报")
        self.assertIsNone(parse_edit_command("帮我画一张雨夜海报"))
        self.assertIn("红色", parse_edit_command("把这张图改成红色背景"))
        self.assertTrue(is_dewatermark_request("把右下角的@作者水印抹掉"))

    def test_video_search_generation_and_full_production_do_not_steal_each_other(self):
        self.assertEqual(_parse_video_command("帮我生成一段海边日落视频"), "海边日落")
        self.assertIsNone(_parse_agent_command("帮我生成一段海边日落视频"))
        self.assertIsNone(_parse_video_command("帮我做一段如何成为博主的视频"))
        self.assertEqual(_parse_agent_command("帮我做一段如何成为博主的视频"), "如何成为博主")
        self.assertIsNone(_parse_video_command("/做视频 如何在家做拿铁"))
        self.assertEqual(_parse_agent_command("/做视频 如何在家做拿铁"), "如何在家做拿铁")

    def test_specialized_natural_language_parsers_remain_reachable(self):
        self.assertEqual(parse_translate_request("帮我把你好翻译成英文"), ("en", "你好"))
        self.assertEqual(parse_debate_topic("圆桌讨论 AI 会不会降低人的能力"), "AI 会不会降低人的能力")
        self.assertEqual(parse_interview_start("帮我模拟产品经理面试"), "产品经理")
        self.assertEqual(extract_question("仔细分析这个方案的风险"), "这个方案的风险")
        self.assertEqual(parse_song_search("帮我点歌 稻香 周杰伦"), "稻香 周杰伦")
        self.assertEqual(parse_original_song_prompt("帮我写一首温暖的生日歌"), "温暖的生日歌")

    def test_artifact_and_research_ownership_is_explicit(self):
        web = parse_web_intent("帮我做一个记账网页")
        self.assertEqual((web.action, web.payload), ("create", "记账"))
        agent = route_natural_agent("帮我做一份暑假计划 Word")
        self.assertEqual(agent.action, "run")
        action = parse_action_pack("帮我比较 A 和 B 并给出决策报告")
        self.assertEqual(action[0], "decision")
        ambiguous_agent = route_natural_agent("帮我比较 A 和 B 并给出决策报告")
        self.assertTrue(ambiguous_agent.ambiguous)
        self.assertIn("Word、PPT", ambiguous_agent.clarification)

    def test_exact_gallery_web_request_and_latest_page_edit_are_reachable(self):
        web = parse_web_intent("能帮我做一个整理图库的东西吗")
        self.assertEqual((web.action, web.payload), ("create", "整理图库"))
        edit = parse_web_intent("刚才那个网页再加上导出功能")
        self.assertEqual((edit.action, edit.page_id, edit.payload), ("edit_latest", "", "加上导出功能"))

    def test_high_quality_video_pipeline_remains_distinct(self):
        text = "帮我做一个高质量的拿铁科普视频"
        self.assertTrue(VideoPipelinePlugin._is_natural_pipeline_request(text))
        self.assertIsNone(_parse_video_command(text))
        self.assertIsNone(_parse_agent_command(text))

    def test_task_followups_route_to_status_not_a_new_job(self):
        for text in ("任务进度怎么样", "刚才那个任务文件发了吗", "上次任务结果送到了吗"):
            intent = route_natural_agent(text)
            self.assertEqual(intent.action, "status", text)


if __name__ == "__main__":
    unittest.main()
