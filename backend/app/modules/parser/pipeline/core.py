"""Parser Pipeline — full implementation per ARCHITECTURE.md V2.1-final §3.5.

Pipeline order (DO NOT CHANGE):
1. PreProcessor      — clean/normalize raw_text
2. RuleParser        — extract title, raw_title, tags from cleaned text
3. ProviderDetector  — detect cloud links, extract url/share_id/access_code
4. MetadataExtractor — extract episode_no, season_no, quality, etc.
5. PostProcessor     — inject versions, compute confidence, ensure defaults

Output: list[ParsedResource]
"""

import re
from typing import Optional

from app.modules.parser.dto import (
    ParsedLink,
    ParsedMetadata,
    ParsedResource,
    LinkProvider,
)

# ---------------------------------------------------------------------------
# Version constants
# ---------------------------------------------------------------------------
PARSER_VERSION = "0.2.0"
RULE_VERSION = "0.2.0"

# ---------------------------------------------------------------------------
# Provider URL patterns
# ---------------------------------------------------------------------------
PROVIDER_URL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("quark", re.compile(r"https?://pan\.quark\.cn/s/([a-zA-Z0-9_-]+)")),
    ("baidu", re.compile(r"https?://pan\.baidu\.com/s/([a-zA-Z0-9_-]+)")),
    ("aliyun", re.compile(r"https?://www\.alipan\.com/s/([a-zA-Z0-9_-]+)|https?://www\.aliyundrive\.com/s/([a-zA-Z0-9_-]+)")),
]

# All URL patterns combined (for finding next URL position)
ALL_URL_RE = re.compile(
    r"https?://pan\.quark\.cn/s/\S+"
    r"|https?://pan\.baidu\.com/s/\S+"
    r"|https?://www\.alipan\.com/s/\S+"
    r"|https?://www\.aliyundrive\.com/s/\S+"
)

# Provider keyword → provider name mapping
PROVIDER_KEYWORD_MAP: list[tuple[str, str]] = [
    ("夸克网盘", "quark"),
    ("夸克", "quark"),
    ("百度网盘", "baidu"),
    ("百度", "baidu"),
    ("阿里云盘", "aliyun"),
    ("阿里云", "aliyun"),
    ("迅雷", "xunlei"),
]

# Extraction code pattern: 提取码 xxx / 提取码: xxx / 提取码：xxx
EXTRACTION_CODE_RE = re.compile(r"提取码[:：]?\s*([a-zA-Z0-9]+)")

# Xunlei command patterns
XUNLEI_COMMAND_RE = re.compile(r"迅雷口令[:：]\s*(\S+)")
PLAIN_COMMAND_RE = re.compile(r"口令\s+(\S+)")

# Magnet pattern
MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+")


# ===========================================================================
# Stage 1: PreProcessor
# ===========================================================================

class PreProcessor:
    """Clean and normalize Telegram raw text.

    - Remove emojis
    - Remove Telegram markdown formatting
    - Normalize whitespace and newlines
    - Preserve links, extraction codes, 【】 title markers
    """

    # Emoji ranges — carefully excludes CJK (U+4E00-U+9FFF) and other text ranges.
    # The old range U+24C2-U+1F251 was far too broad and swallowed all Chinese characters.
    _EMOJI_RE = re.compile(
        "["
        "\U0001F600-\U0001F64F"   # emoticons
        "\U0001F300-\U0001F5FF"   # symbols & pictographs
        "\U0001F680-\U0001F6FF"   # transport & map
        "\U0001F1E0-\U0001F1FF"   # flags
        "\U0001F900-\U0001F9FF"   # supplemental symbols
        "\U0001FA00-\U0001FA6F"
        "\U0001FA70-\U0001FAFF"
        "\U00002700-\U000027BF"   # dingbats
        "\U0001F000-\U0001F02F"   # mahjong
        "\U0001F0A0-\U0001F0FF"   # playing cards
        "]+",
        flags=re.UNICODE,
    )

    _MARKDOWN_PATTERNS = [
        (re.compile(r"\*\*(.+?)\*\*"), r"\1"),
        (re.compile(r"__(.+?)__"), r"\1"),
        (re.compile(r"~~(.+?)~~"), r"\1"),
        (re.compile(r"`(.+?)`"), r"\1"),
        (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),
    ]

    def process(self, raw_text: str) -> str:
        if not raw_text:
            return ""

        text = raw_text

        for pattern, replacement in self._MARKDOWN_PATTERNS:
            text = pattern.sub(replacement, text)

        text = self._EMOJI_RE.sub("", text)

        text = re.sub(r"\r\n", "\n", text)
        text = re.sub(r"\n{2,}", "\n", text)
        text = re.sub(r"[ \t]+", " ", text)

        text = "\n".join(line.strip() for line in text.split("\n"))
        text = text.strip()

        return text


# ===========================================================================
# Stage 2: RuleParser
# ===========================================================================

class RuleParser:
    """Extract title, raw_title, resource_type, tags from cleaned text.

    This stage does NOT depend on links — it works on text alone.
    It removes link-related fragments to isolate the title portion.
    """

    _TAG_PATTERNS: list[re.Pattern] = [
        re.compile(r"(HDR)"),
        re.compile(r"(杜比视界)"),
        re.compile(r"(杜比全景声)"),
        re.compile(r"\b(MKV)\b"),
        re.compile(r"\b(MP4)\b"),
    ]

    _MOVIE_INDICATORS = ["剧场版", "完整版", "电影版", "电影"]

    # Patterns to strip for title isolation (applied individually, not as one block)
    _URL_RE = re.compile(r"https?://\S+")
    _MAGNET_RE = re.compile(r"magnet:\?xt=\S+")
    _EXTRACTION_CODE_RE = re.compile(r"提取码[:：]?\s*[a-zA-Z0-9]+")
    _XUNLEI_COMMAND_RE = re.compile(r"迅雷口令[:：]\s*\S+")
    _PLAIN_COMMAND_RE = re.compile(r"口令\s+\S+")

    # Provider keywords + noise words to strip (standalone, after URLs removed)
    _PROVIDER_KEYWORDS_RE = re.compile(
        r"(?:夸克网盘|夸克|百度网盘|百度|阿里云盘|阿里云|迅雷|链接|网盘|多网盘|多平台)"
    )

    # Metadata tokens to strip from title for clean title extraction
    _METADATA_NOISE_RE = re.compile(
        r"(?:"
        r"第[一二三四五六七八九十百\d]+[-—~]?[一二三四五六七八九十百\d]*集"
        r"|S\d+E\d+(?:[-—]\d+)?"
        r"|更新至\d+集"
        r"|全集"
        r"|完整版"
        r"|第\d+季"
        r"|\d{4}年"
        r"|2160p|1080[pi]|720p|4K|蓝光"
        r"|HDR|杜比视界|杜比全景声"
        r"|国语|粤语|英语|日语|韩语"
        r"|中文字幕|内嵌字幕|双语字幕|中字|内嵌|外挂"
        r"|\d+\.?\d*\s*[GMTK]B"
        r"|MKV|MP4"
        r"|多网盘|多平台"
        r"|更新至"
        r")",
        flags=re.IGNORECASE,
    )

    def parse(self, cleaned_text: str) -> Optional[ParsedResource]:
        if not cleaned_text:
            return None

        # --- Isolate title text by removing link-related fragments ---
        title_text = cleaned_text

        # Remove URLs
        title_text = self._URL_RE.sub("", title_text)
        # Remove magnet links
        title_text = self._MAGNET_RE.sub("", title_text)
        # Remove extraction codes
        title_text = self._EXTRACTION_CODE_RE.sub("", title_text)
        # Remove xunlei commands
        title_text = self._XUNLEI_COMMAND_RE.sub("", title_text)
        title_text = self._PLAIN_COMMAND_RE.sub("", title_text)
        # Remove standalone provider keywords
        title_text = self._PROVIDER_KEYWORDS_RE.sub("", title_text)

        # Clean up leftover punctuation and whitespace
        title_text = re.sub(r"[：:]", " ", title_text)
        title_text = re.sub(r"\s+", " ", title_text)
        title_text = title_text.strip(" ，,、+/")

        if not title_text:
            return None

        raw_title = title_text

        # --- Extract clean title ---
        # Prefer 【】 content
        bracket_match = re.search(r"【(.+?)】", title_text)
        if bracket_match:
            title = bracket_match.group(1).strip()
        else:
            # Remove metadata noise from title to get the core name
            title = self._METADATA_NOISE_RE.sub(" ", title_text)
            title = re.sub(r"\s+", " ", title).strip(" ，,、+/")

        if not title:
            # Fallback: use raw_title if metadata stripping removed everything
            title = raw_title.strip(" 【】/+,，、")

        if not title:
            return None

        # Deduplicate repeated tokens (e.g. "家业 家业" → "家业")
        title_parts = title.split()
        if len(title_parts) > 1:
            seen: list[str] = []
            for part in title_parts:
                if part not in seen:
                    seen.append(part)
            title = " ".join(seen)

        # --- Determine resource_type ---
        resource_type = "drama"
        for indicator in self._MOVIE_INDICATORS:
            if indicator in cleaned_text:
                resource_type = "movie"
                break

        # --- Extract tags ---
        tags: list[str] = []
        for pattern in self._TAG_PATTERNS:
            match = pattern.search(cleaned_text)
            if match:
                tag = match.group(1)
                if tag not in tags:
                    tags.append(tag)

        return ParsedResource(
            title=title,
            raw_title=raw_title,
            resource_type=resource_type,
            tags=tags,
        )


# ===========================================================================
# Stage 3: ProviderDetector
# ===========================================================================

class ProviderDetector:
    """Detect cloud storage links and extract ParsedLink instances.

    Supports: quark, baidu, aliyun (URL), xunlei (command/magnet).
    """

    def detect(self, cleaned_text: str) -> list[ParsedLink]:
        if not cleaned_text:
            return []

        links: list[ParsedLink] = []
        consumed_spans: list[tuple[int, int]] = []

        # 1. URL-based providers (quark, baidu, aliyun)
        for provider_name, pattern in PROVIDER_URL_PATTERNS:
            for match in pattern.finditer(cleaned_text):
                url = match.group(0)
                # Extract share_id from first non-None group
                share_id = None
                for g in match.groups():
                    if g is not None:
                        share_id = g
                        break

                # Find provider keyword before URL for original_text
                start = self._find_keyword_start(cleaned_text, provider_name, match.start())

                # Find extraction code after URL, before next URL
                access_code = None
                next_url_match = ALL_URL_RE.search(cleaned_text, match.end())
                code_boundary = next_url_match.start() if next_url_match else len(cleaned_text)
                code_match = EXTRACTION_CODE_RE.search(cleaned_text, match.end())
                if code_match and code_match.start() < code_boundary:
                    access_code = code_match.group(1)
                    end = code_match.end()
                else:
                    end = match.end()

                original_text = cleaned_text[start:end].strip()

                links.append(ParsedLink(
                    provider=provider_name,
                    original_text=original_text,
                    url=url,
                    share_id=share_id,
                    access_code=access_code,
                    link_type="url",
                    confidence=1.0,
                ))
                consumed_spans.append((start, end))

        # 2. Xunlei command (迅雷口令：xxx)
        for match in XUNLEI_COMMAND_RE.finditer(cleaned_text):
            if self._is_consumed(match.start(), match.end(), consumed_spans):
                continue
            password = match.group(1)
            links.append(ParsedLink(
                provider=LinkProvider.XUNLEI,
                original_text=match.group(0).strip(),
                url=None,
                share_id=None,
                access_code=None,
                password=password,
                link_type="command",
                confidence=1.0,
            ))
            consumed_spans.append((match.start(), match.end()))

        # 3. Plain command (口令 xxx) — only if near 迅雷
        for match in PLAIN_COMMAND_RE.finditer(cleaned_text):
            if self._is_consumed(match.start(), match.end(), consumed_spans):
                continue
            context = cleaned_text[max(0, match.start() - 10):match.start()]
            if "迅雷" not in context:
                continue
            password = match.group(1)
            links.append(ParsedLink(
                provider=LinkProvider.XUNLEI,
                original_text=match.group(0).strip(),
                url=None,
                share_id=None,
                access_code=None,
                password=password,
                link_type="command",
                confidence=1.0,
            ))
            consumed_spans.append((match.start(), match.end()))

        # 4. Magnet links
        for match in MAGNET_RE.finditer(cleaned_text):
            if self._is_consumed(match.start(), match.end(), consumed_spans):
                continue
            links.append(ParsedLink(
                provider=LinkProvider.XUNLEI,
                original_text=match.group(0).strip(),
                url=match.group(0),
                share_id=None,
                access_code=None,
                link_type="magnet",
                confidence=1.0,
            ))
            consumed_spans.append((match.start(), match.end()))

        # Sort by position in text
        links.sort(key=lambda lnk: cleaned_text.find(lnk.original_text) if lnk.original_text else 0)

        return links

    def _find_keyword_start(self, text: str, provider_name: str, url_pos: int) -> int:
        """Find the start position of the provider keyword before a URL."""
        keyword_map = {
            "quark": ["夸克网盘", "夸克"],
            "baidu": ["百度网盘", "百度"],
            "aliyun": ["阿里云盘", "阿里云"],
            "xunlei": ["迅雷"],
        }
        keywords = keyword_map.get(provider_name, [])
        best_start = url_pos
        for kw in keywords:
            idx = text.rfind(kw, 0, url_pos)
            if idx != -1:
                gap = url_pos - idx
                if gap <= 20:
                    best_start = min(best_start, idx)
        return best_start

    @staticmethod
    def _is_consumed(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
        for s, e in spans:
            if start < e and end > s:
                return True
        return False


# ===========================================================================
# Stage 4: MetadataExtractor
# ===========================================================================

# Chinese numeral map
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
           "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
           "十六": 16, "十七": 17, "十八": 18, "十九": 19, "二十": 20}


def _cn_to_int(s: str) -> int | None:
    """Convert Chinese numeral to int. Returns None if not recognized."""
    if s.isdigit():
        return int(s)
    return _CN_NUM.get(s)


class MetadataExtractor:
    """Extract episode, season, year, quality, file_size, language, subtitle.

    Priority for episode: SxxExx > 更新至N集 > 第N-M集 > 第N集 > 全集 > EPxx
    Pure numeric "01" is intentionally NOT supported to avoid false positives.
    Chinese numerals (第一集) are supported for common numbers 1-20.
    """

    # Season + Episode: S02E01, S02E05-10
    _SEASON_EPISODE_RE = re.compile(r"S(\d+)E(\d+)(?:[-—](\d+))?", re.IGNORECASE)

    # 更新至N集
    _UPDATE_TO_RE = re.compile(r"更新至(\d+)集")

    # 第N-M集 (Arabic numerals)
    _EPISODE_RANGE_CN_RE = re.compile(r"第(\d+)[-—~](\d+)集")

    # 第N集 (Arabic or Chinese numerals)
    _EPISODE_CN_RE = re.compile(r"第([一二三四五六七八九十百\d]+)集")

    # 第N季
    _SEASON_CN_RE = re.compile(r"第([一二三四五六七八九十\d]+)季")

    # 全集
    _FULL_RE = re.compile(r"全集")

    # EP01, E01 — must be standalone (not part of a word like "code123")
    _EPISODE_EN_RE = re.compile(r"\bE(?:P)?0*(\d+)\b", re.IGNORECASE)

    # Year: 2023年
    _YEAR_RE = re.compile(r"(\d{4})年")

    # Quality: 2160p / 1080p / 1080i / 720p / 4K / 蓝光
    _QUALITY_RE = re.compile(r"(2160p|1080[pi]|720p|4K|蓝光)", re.IGNORECASE)

    # File size: 12.5GB / 800MB / 1.2TB / 500KB
    _FILE_SIZE_RE = re.compile(r"(\d+\.?\d*\s*[GMTK]B)", re.IGNORECASE)

    # Language: 国语 / 粤语 / 英语 / 日语 / 韩语
    _LANGUAGE_RE = re.compile(r"(国语|粤语|英语|日语|韩语)")

    # Subtitle: 中文字幕 / 内嵌字幕 / 双语字幕 / 中字 / 内嵌 / 外挂
    _SUBTITLE_RE = re.compile(r"(中文字幕|内嵌字幕|双语字幕|中字|内嵌|外挂)")

    def extract(self, cleaned_text: str) -> ParsedMetadata:
        if not cleaned_text:
            return ParsedMetadata()

        episode_no: Optional[int] = None
        season_no: Optional[int] = None
        episode_range: Optional[str] = None

        # Priority 1: SxxExx format
        se_match = self._SEASON_EPISODE_RE.search(cleaned_text)
        if se_match:
            season_no = int(se_match.group(1))
            ep_start = int(se_match.group(2))
            if se_match.group(3):
                ep_end = int(se_match.group(3))
                episode_range = f"s{season_no:02d}e{ep_start}-{ep_end}"
            else:
                episode_no = ep_start
                episode_range = f"s{season_no:02d}e{ep_start:02d}"
        else:
            # Priority 2: 第N季 (standalone season)
            season_cn_match = self._SEASON_CN_RE.search(cleaned_text)
            if season_cn_match:
                season_no = _cn_to_int(season_cn_match.group(1))

            # Priority 3: 更新至N集
            update_match = self._UPDATE_TO_RE.search(cleaned_text)
            if update_match:
                ep_end = int(update_match.group(1))
                episode_range = f"ep1-{ep_end}"
            else:
                # Priority 4: 第N-M集
                range_match = self._EPISODE_RANGE_CN_RE.search(cleaned_text)
                if range_match:
                    ep_start = int(range_match.group(1))
                    ep_end = int(range_match.group(2))
                    episode_range = f"ep{ep_start}-{ep_end}"
                else:
                    # Priority 5: 第N集 (Arabic or Chinese)
                    ep_cn_match = self._EPISODE_CN_RE.search(cleaned_text)
                    if ep_cn_match:
                        ep_val = _cn_to_int(ep_cn_match.group(1))
                        if ep_val is not None:
                            episode_no = ep_val
                            episode_range = f"ep{episode_no}"
                    else:
                        # Priority 6: 全集
                        full_match = self._FULL_RE.search(cleaned_text)
                        if full_match:
                            episode_range = "all"
                        else:
                            # Priority 7: EP01 / E01 (word-bounded)
                            ep_en_match = self._EPISODE_EN_RE.search(cleaned_text)
                            if ep_en_match:
                                episode_no = int(ep_en_match.group(1))
                                episode_range = f"ep{episode_no}"

        # Year
        year_match = self._YEAR_RE.search(cleaned_text)
        year = int(year_match.group(1)) if year_match else None

        # Quality
        quality_match = self._QUALITY_RE.search(cleaned_text)
        quality = quality_match.group(1) if quality_match else None

        # File size
        size_match = self._FILE_SIZE_RE.search(cleaned_text)
        file_size = size_match.group(1) if size_match else None

        # Language
        lang_match = self._LANGUAGE_RE.search(cleaned_text)
        language = lang_match.group(1) if lang_match else None

        # Subtitle
        sub_match = self._SUBTITLE_RE.search(cleaned_text)
        subtitle = sub_match.group(1) if sub_match else None

        return ParsedMetadata(
            episode_no=episode_no,
            season_no=season_no,
            episode_range=episode_range,
            year=year,
            quality=quality,
            file_size=file_size,
            language=language,
            subtitle=subtitle,
        )


# ===========================================================================
# Stage 5: PostProcessor
# ===========================================================================

class PostProcessor:
    """Finalize ParsedResource: inject versions, compute confidence, ensure defaults."""

    def aggregate(
        self,
        resource: ParsedResource,
        links: list[ParsedLink],
        metadata: ParsedMetadata,
    ) -> ParsedResource:
        # Attach links and metadata
        resource.links = links if links else []
        resource.metadata = metadata

        # Ensure tags is a list
        if resource.tags is None:
            resource.tags = []

        # Inject versions
        resource.parser_version = PARSER_VERSION
        resource.rule_version = RULE_VERSION

        # Compute confidence
        confidence = 0.0
        if resource.title:
            confidence += 0.3
        if links:
            confidence += 0.4
        if metadata and any(
            v is not None
            for v in [
                metadata.episode_no,
                metadata.season_no,
                metadata.episode_range,
                metadata.year,
                metadata.quality,
                metadata.file_size,
                metadata.language,
                metadata.subtitle,
            ]
        ):
            confidence += 0.3

        resource.confidence = min(confidence, 1.0)

        return resource


# ===========================================================================
# ParserPipeline — orchestrates all 5 stages
# ===========================================================================

class ParserPipeline:
    """Parser Pipeline orchestrator.

    Stage order (per ARCHITECTURE.md V2.1 §3.5):
    PreProcessor → RuleParser → ProviderDetector → MetadataExtractor → PostProcessor

    Output: list[ParsedResource]
    - Empty list if no title can be extracted
    - Empty list if no links found
    """

    def __init__(self):
        self.preprocessor = PreProcessor()
        self.rule_parser = RuleParser()
        self.provider_detector = ProviderDetector()
        self.metadata_extractor = MetadataExtractor()
        self.post_processor = PostProcessor()

    async def parse(self, raw_text: str, raw_message_id: int | None = None) -> list[ParsedResource]:
        """Parse raw_text through the full pipeline.

        Returns:
            list[ParsedResource]: Empty list if parsing yields no valid result.
        """
        # Stage 1: PreProcessor
        cleaned = self.preprocessor.process(raw_text)
        if not cleaned:
            return []

        # Stage 2: RuleParser (title extraction, does not depend on links)
        resource = self.rule_parser.parse(cleaned)
        if resource is None:
            return []

        # Stage 3: ProviderDetector
        links = self.provider_detector.detect(cleaned)
        if not links:
            return []

        # Stage 4: MetadataExtractor
        metadata = self.metadata_extractor.extract(cleaned)

        # Stage 5: PostProcessor
        final = self.post_processor.aggregate(resource, links, metadata)

        return [final]
