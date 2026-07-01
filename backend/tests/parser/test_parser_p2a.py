"""P2-A parser tests — verify DTO structure and fixtures.

Tests:
1. DTOs can be instantiated and validated
2. Sample fixtures are valid (20 samples)
3. Pipeline skeleton imports correctly
"""

import pytest
from dataclasses import dataclass
from typing import Optional, List

from app.modules.parser.dto import (
    ParsedLink,
    ParsedMetadata,
    ParsedResource,
    LinkProvider,
)


class TestParsedLink:
    """Test ParsedLink DTO instantiation and validation."""

    def test_parsedlink_basic(self):
        """Test basic ParsedLink creation."""
        link = ParsedLink(
            provider=LinkProvider.QUARK,
            original_text="夸克网盘链接",
            url="https://pan.quark.cn/s/abc123",
            share_id="abc123",
            access_code="xyz789",
            link_type="url",
            confidence=0.95,
        )

        assert link.provider == "quark"
        assert link.original_text == "夸克网盘链接"
        assert link.url == "https://pan.quark.cn/s/abc123"
        assert link.share_id == "abc123"
        assert link.access_code == "xyz789"
        assert link.link_type == "url"
        assert link.confidence == 0.95

    def test_parsedlink_optional_fields(self):
        """Test optional fields can be None."""
        link = ParsedLink(
            provider=LinkProvider.BAIDU,
            original_text="百度网盘",
            url=None,
            share_id=None,
            access_code=None,
            password=None,
        )

        assert link.url is None
        assert link.share_id is None
        assert link.access_code is None
        assert link.password is None

    def test_parsedlink_defaults(self):
        """Test default values."""
        link = ParsedLink(
            provider=LinkProvider.XUNLEI,
            original_text="迅雷链接",
        )

        assert link.link_type == "url"
        assert link.confidence == 1.0

    def test_parsedlink_all_providers(self):
        """Test all provider values."""
        providers = [
            LinkProvider.QUARK,
            LinkProvider.BAIDU,
            LinkProvider.XUNLEI,
            LinkProvider.ALIYUN,
            LinkProvider.MEGA,
            LinkProvider.GOOGLE,
            LinkProvider.OTHER,
        ]
        for provider in providers:
            link = ParsedLink(
                provider=provider,
                original_text=f"Test {provider.value}",
                url="https://example.com",
            )
            assert link.provider == provider.value

        assert "lnz" not in {provider.value for provider in LinkProvider}
        unknown = ParsedLink(provider="lnz", original_text="Unknown provider")
        assert unknown.provider == "other"


class TestParsedMetadata:
    """Test ParsedMetadata DTO instantiation and validation."""

    def test_parsedmetadata_basic(self):
        """Test basic ParsedMetadata creation."""
        metadata = ParsedMetadata(
            episode_no=10,
            season_no=2,
            episode_range="ep1-10",
            year=2023,
            quality="1080p",
            file_size="12.5GB",
            language="国语",
            subtitle="中字",
        )

        assert metadata.episode_no == 10
        assert metadata.season_no == 2
        assert metadata.episode_range == "ep1-10"
        assert metadata.year == 2023
        assert metadata.quality == "1080p"
        assert metadata.file_size == "12.5GB"
        assert metadata.language == "国语"
        assert metadata.subtitle == "中字"

    def test_parsedmetadata_optional_fields(self):
        """Test optional fields can be None."""
        metadata = ParsedMetadata(
            episode_no=None,
            season_no=None,
            episode_range=None,
            year=None,
            quality=None,
            file_size=None,
            language=None,
            subtitle=None,
        )

        assert metadata.episode_no is None
        assert metadata.season_no is None
        assert metadata.episode_range is None
        assert metadata.year is None
        assert metadata.quality is None
        assert metadata.file_size is None
        assert metadata.language is None
        assert metadata.subtitle is None

    def test_parsedmetadata_defaults(self):
        """Test default values (all None)."""
        metadata = ParsedMetadata()

        assert metadata.episode_no is None
        assert metadata.season_no is None
        assert metadata.episode_range is None
        assert metadata.year is None
        assert metadata.quality is None
        assert metadata.file_size is None
        assert metadata.language is None
        assert metadata.subtitle is None


class TestParsedResource:
    """Test ParsedResource DTO instantiation and validation."""

    def test_parsedresource_basic(self):
        """Test basic ParsedResource creation."""
        link = ParsedLink(
            provider=LinkProvider.QUARK,
            original_text="夸克链接",
            url="https://pan.quark.cn/s/abc123",
        )

        metadata = ParsedMetadata(
            episode_no=5,
            quality="1080p",
        )

        resource = ParsedResource(
            title="家业",
            raw_title="家业 第5集",
            description="最新更新的剧集",
            resource_type="drama",
            links=[link],
            metadata=metadata,
            tags=["HDR", "国语"],
            confidence=0.9,
            parser_version="0.1.0",
            rule_version="drama_v1",
        )

        assert resource.title == "家业"
        assert resource.raw_title == "家业 第5集"
        assert resource.description == "最新更新的剧集"
        assert resource.resource_type == "drama"
        assert len(resource.links) == 1
        assert resource.links[0].provider == "quark"
        assert resource.metadata.episode_no == 5
        assert resource.metadata.quality == "1080p"
        assert resource.tags == ["HDR", "国语"]
        assert resource.confidence == 0.9
        assert resource.parser_version == "0.1.0"
        assert resource.rule_version == "drama_v1"

    def test_parsedresource_defaults(self):
        """Test default values."""
        link = ParsedLink(
            provider=LinkProvider.BAIDU,
            original_text="百度链接",
            url="https://pan.baidu.com/s/abc123",
        )

        resource = ParsedResource(
            title="测试资源",
            raw_title="测试资源 原始标题",
            links=[link],
        )

        assert resource.description is None
        assert resource.resource_type == "drama"
        assert resource.metadata is None
        assert resource.tags == []
        assert resource.confidence == 1.0
        assert resource.parser_version == ""
        assert resource.rule_version == ""

    def test_parsedresource_no_links(self):
        """Test ParsedResource without links."""
        resource = ParsedResource(
            title="无链接资源",
            raw_title="无链接资源 原始标题",
        )

        assert resource.links == []


class TestSampleFixtures:
    """Test sample fixtures are valid DTO instances."""

    def test_sample_raw_messages_count(self):
        """Test we have 20 sample raw messages."""
        from tests.parser.fixtures import SAMPLE_RAW_MESSAGES

        assert len(SAMPLE_RAW_MESSAGES) == 20

    def test_sample_raw_messages_structure(self):
        """Test sample raw messages have expected structure."""
        from tests.parser.fixtures import SAMPLE_RAW_MESSAGES

        for i, sample in enumerate(SAMPLE_RAW_MESSAGES):
            assert "raw_text" in sample
            assert "expected_links" in sample
            assert "expected_metadata" in sample

            # Test expected_links can create ParsedLink instances
            for link_data in sample["expected_links"]:
                link = ParsedLink(**link_data)
                assert link.provider in ["quark", "baidu", "xunlei", "aliyun", "other"]
                assert isinstance(link.original_text, str)
                assert link.url or link.share_id or link.access_code or link.password

            # Test expected_metadata can create ParsedMetadata instances
            if sample["expected_metadata"]:
                metadata = ParsedMetadata(**sample["expected_metadata"])
                # At least one field should be populated for valid metadata
                metadata_fields = [
                    metadata.episode_no,
                    metadata.season_no,
                    metadata.episode_range,
                    metadata.year,
                    metadata.quality,
                    metadata.file_size,
                    metadata.language,
                    metadata.subtitle,
                ]
                # Check that at least one field is not None (has a value)
                assert any(field is not None for field in metadata_fields), f"Sample {i} has no metadata values: {metadata}"


class TestPipelineImport:
    """Test pipeline skeleton imports correctly."""

    def test_pipeline_import(self):
        """Test pipeline core imports without errors."""
        from app.modules.parser.pipeline.core import ParserPipeline

        # Should be able to instantiate the pipeline skeleton
        pipeline = ParserPipeline()
        assert pipeline is not None

    def test_pipeline_stage_imports(self):
        """Test individual stage imports work."""
        from app.modules.parser.pipeline.core import (
            PreProcessor,
            RuleParser,
            ProviderDetector,
            MetadataExtractor,
            PostProcessor,
        )

        # All stages should be importable
        assert PreProcessor is not None
        assert RuleParser is not None
        assert ProviderDetector is not None
        assert MetadataExtractor is not None
        assert PostProcessor is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
