"""P2-B Parser Pipeline tests.

Covers:
1. PreProcessor unit tests (emoji, markdown, whitespace, link preservation)
2. ProviderDetector unit tests (quark, baidu, aliyun, xunlei command, magnet, multi-link)
3. MetadataExtractor unit tests (episode patterns, quality, file_size, language, subtitle)
4. RuleParser unit tests (title extraction, raw_title, resource_type, tags)
5. PostProcessor unit tests (version injection, confidence, defaults)
6. Pipeline integration tests (20 fixtures with real assertions)
7. Boundary tests (empty text, no links, no title)
"""

import pytest
import asyncio

from app.modules.parser.dto import ParsedLink, ParsedMetadata, ParsedResource, LinkProvider
from app.modules.parser.pipeline.core import (
    PreProcessor,
    RuleParser,
    ProviderDetector,
    MetadataExtractor,
    PostProcessor,
    ParserPipeline,
    PARSER_VERSION,
    RULE_VERSION,
)
from tests.parser.fixtures import SAMPLE_RAW_MESSAGES


# ===========================================================================
# Helper: run async parse synchronously
# ===========================================================================
def run_parse(pipeline: ParserPipeline, raw_text: str):
    return asyncio.run(pipeline.parse(raw_text))


# ===========================================================================
# 1. PreProcessor Unit Tests
# ===========================================================================
class TestPreProcessor:
    """Test PreProcessor stage."""

    def test_empty_text(self):
        pp = PreProcessor()
        assert pp.process("") == ""

    def test_none_text(self):
        pp = PreProcessor()
        assert pp.process(None) == ""

    def test_preserves_chinese(self):
        """Chinese characters must NOT be stripped."""
        pp = PreProcessor()
        result = pp.process("家业 第一集 夸克网盘")
        assert "家业" in result
        assert "第一集" in result
        assert "夸克网盘" in result

    def test_preserves_links(self):
        """Links must be preserved."""
        pp = PreProcessor()
        result = pp.process("家业 https://pan.quark.cn/s/abc123 提取码 xyz789")
        assert "https://pan.quark.cn/s/abc123" in result
        assert "提取码" in result
        assert "xyz789" in result

    def test_preserves_brackets(self):
        """【】 brackets must be preserved (title markers)."""
        pp = PreProcessor()
        result = pp.process("【家业】第一集")
        assert "【家业】" in result

    def test_strips_markdown_bold(self):
        """Markdown **bold** should be stripped to inner text."""
        pp = PreProcessor()
        result = pp.process("**家业** 第一集")
        assert "**" not in result
        assert "家业" in result

    def test_strips_markdown_link(self):
        """Markdown [text](url) should become just text."""
        pp = PreProcessor()
        result = pp.process("[家业](https://t.me/c/123) 第一集")
        assert "[家业]" not in result
        assert "家业" in result

    def test_normalizes_whitespace(self):
        """Multiple spaces should become single space."""
        pp = PreProcessor()
        result = pp.process("家业   第一集    夸克")
        assert "  " not in result
        assert "家业 第一集 夸克" == result

    def test_strips_emoji(self):
        """Emoji should be removed."""
        pp = PreProcessor()
        result = pp.process("🎬家业 第一集📺")
        assert "🎬" not in result
        assert "📺" not in result
        assert "家业" in result


# ===========================================================================
# 2. ProviderDetector Unit Tests
# ===========================================================================
class TestProviderDetector:
    """Test ProviderDetector stage."""

    def test_quark_url_with_code(self):
        pd = ProviderDetector()
        links = pd.detect("家业 第一集 夸克网盘 https://pan.quark.cn/s/abc123 提取码 xyz789")
        assert len(links) == 1
        link = links[0]
        assert link.provider == "quark"
        assert link.url == "https://pan.quark.cn/s/abc123"
        assert link.share_id == "abc123"
        assert link.access_code == "xyz789"
        assert link.link_type == "url"
        assert "夸克" in link.original_text

    def test_baidu_url_no_code(self):
        pd = ProviderDetector()
        links = pd.detect("家业 百度 https://pan.baidu.com/s/jkl012")
        assert len(links) == 1
        link = links[0]
        assert link.provider == "baidu"
        assert link.url == "https://pan.baidu.com/s/jkl012"
        assert link.share_id == "jkl012"
        assert link.access_code is None

    def test_aliyun_url_with_code(self):
        pd = ProviderDetector()
        links = pd.detect("家业 阿里云 https://www.aliyundrive.com/s/mno456 提取码 pqr789")
        assert len(links) == 1
        link = links[0]
        assert link.provider == "aliyun"
        assert link.url == "https://www.aliyundrive.com/s/mno456"
        assert link.share_id == "mno456"
        assert link.access_code == "pqr789"

    def test_xunlei_command(self):
        pd = ProviderDetector()
        links = pd.detect("家业 迅雷口令：xunlei123 第1集")
        assert len(links) == 1
        link = links[0]
        assert link.provider == "xunlei"
        assert link.password == "xunlei123"
        assert link.link_type == "command"
        assert link.url is None

    def test_magnet_link(self):
        pd = ProviderDetector()
        links = pd.detect("家业 全集 magnet:?xt=urn:btih:abc123def")
        assert len(links) == 1
        link = links[0]
        assert link.provider == "xunlei"
        assert link.link_type == "magnet"
        assert link.url == "magnet:?xt=urn:btih:abc123def"

    def test_multi_link_with_codes(self):
        """Two URLs with separate extraction codes."""
        pd = ProviderDetector()
        text = "家业 夸克 https://pan.quark.cn/s/abc123 提取码 xyz 百度 https://pan.baidu.com/s/def456 提取码 def"
        links = pd.detect(text)
        assert len(links) == 2
        assert links[0].provider == "quark"
        assert links[0].access_code == "xyz"
        assert links[1].provider == "baidu"
        assert links[1].access_code == "def"

    def test_no_links(self):
        pd = ProviderDetector()
        links = pd.detect("家业 第一集 没有链接")
        assert links == []


# ===========================================================================
# 3. MetadataExtractor Unit Tests
# ===========================================================================
class TestMetadataExtractor:
    """Test MetadataExtractor stage."""

    def test_episode_cn_arabic(self):
        """第5集 → episode_no=5, episode_range=ep5."""
        me = MetadataExtractor()
        m = me.extract("家业 第5集 夸克")
        assert m.episode_no == 5
        assert m.episode_range == "ep5"

    def test_episode_cn_chinese_numeral(self):
        """第一集 → episode_no=1, episode_range=ep1."""
        me = MetadataExtractor()
        m = me.extract("家业 第一集 夸克")
        assert m.episode_no == 1
        assert m.episode_range == "ep1"

    def test_episode_range_cn(self):
        """第1-5集 → episode_range=ep1-5."""
        me = MetadataExtractor()
        m = me.extract("家业 第1-5集 阿里云")
        assert m.episode_range == "ep1-5"

    def test_update_to(self):
        """更新至10集 → episode_range=ep1-10."""
        me = MetadataExtractor()
        m = me.extract("家业 更新至10集 夸克")
        assert m.episode_range == "ep1-10"

    def test_full_collection(self):
        """全集 → episode_range=all."""
        me = MetadataExtractor()
        m = me.extract("家业 全集 夸克")
        assert m.episode_range == "all"

    def test_season_episode(self):
        """S02E01 → season_no=2, episode_no=1, episode_range=s02e01."""
        me = MetadataExtractor()
        m = me.extract("家业 S02E01 阿里云")
        assert m.season_no == 2
        assert m.episode_no == 1
        assert m.episode_range == "s02e01"

    def test_season_episode_range(self):
        """S02E05-10 → season_no=2, episode_range=s02e5-10."""
        me = MetadataExtractor()
        m = me.extract("家业 S02E05-10 夸克")
        assert m.season_no == 2
        assert m.episode_range == "s02e5-10"

    def test_quality_1080p(self):
        me = MetadataExtractor()
        m = me.extract("家业 1080p 第10集")
        assert m.quality == "1080p"

    def test_quality_4k(self):
        me = MetadataExtractor()
        m = me.extract("家业 4K HDR 国语")
        assert m.quality == "4K"

    def test_file_size(self):
        me = MetadataExtractor()
        m = me.extract("家业 全集 12.5GB MKV")
        assert m.file_size == "12.5GB"

    def test_language(self):
        me = MetadataExtractor()
        m = me.extract("家业 1080p 国语 中字 第10集")
        assert m.language == "国语"

    def test_subtitle(self):
        me = MetadataExtractor()
        m = me.extract("家业 1080p 国语 中字 第10集")
        assert m.subtitle == "中字"

    def test_subtitle_embedded(self):
        me = MetadataExtractor()
        m = me.extract("家业 内嵌字幕 2160p")
        assert m.subtitle == "内嵌字幕"

    def test_year(self):
        me = MetadataExtractor()
        m = me.extract("家业 2023年 720p 粤语")
        assert m.year == 2023

    def test_season_cn_standalone(self):
        """第3季 → season_no=3."""
        me = MetadataExtractor()
        m = me.extract("家业 第3季 1080p 国语 S03E01-05")
        # S03E01-05 takes priority for episode_range, but season from 第3季 should also be captured
        assert m.season_no == 3

    def test_no_metadata(self):
        """Text with no metadata → all fields None."""
        me = MetadataExtractor()
        m = me.extract("家业 夸克 https://pan.quark.cn/s/abc")
        assert m.episode_no is None
        assert m.season_no is None
        assert m.episode_range is None
        assert m.year is None
        assert m.quality is None
        assert m.file_size is None
        assert m.language is None
        assert m.subtitle is None


# ===========================================================================
# 4. RuleParser Unit Tests
# ===========================================================================
class TestRuleParser:
    """Test RuleParser stage."""

    def test_title_from_brackets(self):
        rp = RuleParser()
        r = rp.parse("【家业】更新至10集 夸克 https://pan.quark.cn/s/def456 提取码 123abc")
        assert r is not None
        assert r.title == "家业"

    def test_title_without_brackets(self):
        rp = RuleParser()
        r = rp.parse("家业 第一集 夸克网盘 https://pan.quark.cn/s/abc123 提取码 xyz789")
        assert r is not None
        assert r.title == "家业"

    def test_raw_title_preserved(self):
        rp = RuleParser()
        r = rp.parse("家业 1080p 国语 中字 第10集 夸克 https://pan.quark.cn/s/ep10")
        assert r is not None
        assert "1080p" in r.raw_title
        assert "国语" in r.raw_title
        assert "第10集" in r.raw_title

    def test_resource_type_drama(self):
        rp = RuleParser()
        r = rp.parse("家业 第一集 夸克 https://pan.quark.cn/s/abc")
        assert r.resource_type == "drama"

    def test_resource_type_movie(self):
        rp = RuleParser()
        r = rp.parse("【家业剧场版】完整版 25GB 2160p 夸克 https://pan.quark.cn/s/abc")
        assert r.resource_type == "movie"

    def test_tags_extraction(self):
        rp = RuleParser()
        r = rp.parse("家业 4K HDR 杜比视界 第1-12集 夸克 https://pan.quark.cn/s/abc")
        assert r is not None
        assert "HDR" in r.tags
        assert "杜比视界" in r.tags

    def test_empty_text_returns_none(self):
        rp = RuleParser()
        assert rp.parse("") is None

    def test_only_links_returns_none(self):
        """Text with only a URL and no title → None."""
        rp = RuleParser()
        r = rp.parse("https://pan.quark.cn/s/abc123 提取码 xyz789")
        assert r is None


# ===========================================================================
# 5. PostProcessor Unit Tests
# ===========================================================================
class TestPostProcessor:
    """Test PostProcessor stage."""

    def test_version_injection(self):
        pp = PostProcessor()
        resource = ParsedResource(title="家业", raw_title="家业")
        link = ParsedLink(provider=LinkProvider.QUARK, original_text="夸克")
        metadata = ParsedMetadata(episode_no=1)
        result = pp.aggregate(resource, [link], metadata)
        assert result.parser_version == PARSER_VERSION
        assert result.rule_version == RULE_VERSION

    def test_confidence_full(self):
        """Title + links + metadata → confidence=1.0."""
        pp = PostProcessor()
        resource = ParsedResource(title="家业", raw_title="家业")
        link = ParsedLink(provider=LinkProvider.QUARK, original_text="夸克")
        metadata = ParsedMetadata(episode_no=1, quality="1080p")
        result = pp.aggregate(resource, [link], metadata)
        assert result.confidence == 1.0

    def test_confidence_no_metadata(self):
        """Title + links but no metadata → confidence=0.7."""
        pp = PostProcessor()
        resource = ParsedResource(title="家业", raw_title="家业")
        link = ParsedLink(provider=LinkProvider.QUARK, original_text="夸克")
        metadata = ParsedMetadata()
        result = pp.aggregate(resource, [link], metadata)
        assert result.confidence == 0.7

    def test_confidence_no_links(self):
        """Title + metadata but no links → confidence=0.6."""
        pp = PostProcessor()
        resource = ParsedResource(title="家业", raw_title="家业")
        metadata = ParsedMetadata(episode_no=1)
        result = pp.aggregate(resource, [], metadata)
        assert result.confidence == 0.6

    def test_links_default_empty_list(self):
        pp = PostProcessor()
        resource = ParsedResource(title="家业", raw_title="家业")
        metadata = ParsedMetadata()
        result = pp.aggregate(resource, [], metadata)
        assert result.links == []

    def test_tags_default_empty_list(self):
        pp = PostProcessor()
        resource = ParsedResource(title="家业", raw_title="家业")
        metadata = ParsedMetadata()
        result = pp.aggregate(resource, [], metadata)
        assert result.tags == []


# ===========================================================================
# 6. Pipeline Integration Tests (20 fixtures)
# ===========================================================================
class TestPipelineIntegration:
    """Test full pipeline with 20 real fixtures — every sample has real assertions."""

    @pytest.mark.parametrize("sample_idx", range(len(SAMPLE_RAW_MESSAGES)))
    def test_pipeline_parse_fixture(self, sample_idx):
        """Parse each fixture and assert all fields match expected values."""
        sample = SAMPLE_RAW_MESSAGES[sample_idx]
        pipeline = ParserPipeline()
        result = run_parse(pipeline, sample["raw_text"])

        # --- Result count ---
        expected_count = sample["expected_result_count"]
        assert len(result) == expected_count, (
            f"Sample {sample_idx}: expected {expected_count} results, got {len(result)}"
        )

        if expected_count == 0:
            return

        resource = result[0]

        # --- Title ---
        assert resource.title == sample["expected_title"], (
            f"Sample {sample_idx}: title '{resource.title}' != expected '{sample['expected_title']}'"
        )
        assert resource.title, f"Sample {sample_idx}: title must not be empty"

        # --- raw_title ---
        assert resource.raw_title, f"Sample {sample_idx}: raw_title must not be empty"

        # --- resource_type ---
        assert resource.resource_type in ("drama", "movie"), (
            f"Sample {sample_idx}: invalid resource_type '{resource.resource_type}'"
        )

        # --- Links ---
        expected_links = sample["expected_links"]
        assert len(resource.links) == len(expected_links), (
            f"Sample {sample_idx}: {len(resource.links)} links != {len(expected_links)} expected"
        )

        for j, (actual_link, expected_link) in enumerate(zip(resource.links, expected_links)):
            assert actual_link.provider == expected_link["provider"], (
                f"Sample {sample_idx}, link {j}: provider '{actual_link.provider}' != '{expected_link['provider']}'"
            )
            assert actual_link.url == expected_link["url"], (
                f"Sample {sample_idx}, link {j}: url '{actual_link.url}' != '{expected_link['url']}'"
            )
            assert actual_link.share_id == expected_link["share_id"], (
                f"Sample {sample_idx}, link {j}: share_id '{actual_link.share_id}' != '{expected_link['share_id']}'"
            )
            assert actual_link.access_code == expected_link["access_code"], (
                f"Sample {sample_idx}, link {j}: access_code '{actual_link.access_code}' != '{expected_link['access_code']}'"
            )
            assert actual_link.link_type == expected_link["link_type"], (
                f"Sample {sample_idx}, link {j}: link_type '{actual_link.link_type}' != '{expected_link['link_type']}'"
            )
            # password check if present in expected
            if "password" in expected_link:
                assert actual_link.password == expected_link["password"], (
                    f"Sample {sample_idx}, link {j}: password mismatch"
                )
            # original_text must be non-empty string
            assert isinstance(actual_link.original_text, str) and actual_link.original_text, (
                f"Sample {sample_idx}, link {j}: original_text must be non-empty string"
            )

        # --- Metadata ---
        expected_metadata = sample["expected_metadata"]
        if expected_metadata:
            assert resource.metadata is not None, f"Sample {sample_idx}: metadata should not be None"
            for field, expected_val in expected_metadata.items():
                actual_val = getattr(resource.metadata, field)
                assert actual_val == expected_val, (
                    f"Sample {sample_idx}: metadata.{field} = '{actual_val}' != '{expected_val}'"
                )

        # --- Version injection ---
        assert resource.parser_version == PARSER_VERSION, (
            f"Sample {sample_idx}: parser_version mismatch"
        )
        assert resource.rule_version == RULE_VERSION, (
            f"Sample {sample_idx}: rule_version mismatch"
        )

        # --- Confidence ---
        assert 0.0 < resource.confidence <= 1.0, (
            f"Sample {sample_idx}: confidence {resource.confidence} out of range (0, 1]"
        )

        # --- Tags is a list ---
        assert isinstance(resource.tags, list), (
            f"Sample {sample_idx}: tags must be a list"
        )

        # --- Links is a list ---
        assert isinstance(resource.links, list), (
            f"Sample {sample_idx}: links must be a list"
        )


# ===========================================================================
# 7. Boundary Tests
# ===========================================================================
class TestPipelineBoundary:
    """Test pipeline behavior on edge cases."""

    def test_empty_text(self):
        """Empty text → empty list."""
        pipeline = ParserPipeline()
        result = run_parse(pipeline, "")
        assert result == []

    def test_whitespace_only(self):
        """Whitespace-only text → empty list."""
        pipeline = ParserPipeline()
        result = run_parse(pipeline, "   \n\n  \t  ")
        assert result == []

    def test_no_links(self):
        """Text with title but no links → empty list (no link-less resources)."""
        pipeline = ParserPipeline()
        result = run_parse(pipeline, "家业 第一集 没有链接的文本")
        assert result == []

    def test_only_links_no_title(self):
        """Text with only a URL and no title → empty list."""
        pipeline = ParserPipeline()
        result = run_parse(pipeline, "https://pan.quark.cn/s/abc123 提取码 xyz789")
        assert result == []

    def test_only_extraction_code(self):
        """Text with only extraction code → empty list."""
        pipeline = ParserPipeline()
        result = run_parse(pipeline, "提取码 xyz789")
        assert result == []

    def test_none_input(self):
        """None input → empty list, not crash."""
        pipeline = ParserPipeline()
        result = run_parse(pipeline, None)
        assert result == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
