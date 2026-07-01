"""Parser test fixtures — 20 sample raw_text messages.

Covers typical Telegram resource sharing patterns:
- Single/multiple links
- Various providers (Quark, Baidu, Xunlei, Aliyun)
- With/without extraction codes
- With/without metadata (episodes, seasons, quality, language)
- Complex combinations

Each fixture contains:
- raw_text: The original Telegram message text
- expected_links: List of expected ParsedLink DTOs
- expected_metadata: Expected ParsedMetadata DTO (None if no metadata)

Sample coverage:
- Single provider (quark/baidu/xunlei/aliyun): 8 samples
- Multiple providers: 2 samples
- With/without extraction codes: 6 samples
- With metadata (episodes/seasons/quality/language): 10 samples
- Complex combinations: 3 samples
"""

from typing import List, Dict, Any, Optional

SAMPLE_RAW_MESSAGES: List[Dict[str, Any]] = [
    # Single provider - Quark (2 samples)
    {
        "raw_text": "家业 第一集 夸克网盘 https://pan.quark.cn/s/abc123 提取码 xyz789",
        "expected_title": "家业",
        "expected_result_count": 1,
        "expected_links": [
            {
                "provider": "quark",
                "original_text": "夸克网盘 https://pan.quark.cn/s/abc123 提取码 xyz789",
                "url": "https://pan.quark.cn/s/abc123",
                "share_id": "abc123",
                "access_code": "xyz789",
                "link_type": "url",
                "confidence": 1.0,
            }
        ],
        "expected_metadata": {
            "episode_no": 1,
            "episode_range": "ep1",
            "quality": None,
            "language": None,
        }
    },
    {
        "raw_text": "【家业】更新至10集 夸克 https://pan.quark.cn/s/def456 提取码 123abc",
        "expected_title": "家业",
        "expected_result_count": 1,
        "expected_links": [
            {
                "provider": "quark",
                "original_text": "夸克 https://pan.quark.cn/s/def456 提取码 123abc",
                "url": "https://pan.quark.cn/s/def456",
                "share_id": "def456",
                "access_code": "123abc",
                "link_type": "url",
                "confidence": 1.0,
            }
        ],
        "expected_metadata": {
            "episode_no": None,
            "episode_range": "ep1-10",
            "quality": None,
            "language": None,
        }
    },

    # Single provider - Baidu (2 samples)
    {
        "raw_text": "家业 百度网盘链接 https://pan.baidu.com/s/ghi789 提取码 456def",
        "expected_title": "家业",
        "expected_result_count": 1,
        "expected_links": [
            {
                "provider": "baidu",
                "original_text": "百度网盘链接 https://pan.baidu.com/s/ghi789 提取码 456def",
                "url": "https://pan.baidu.com/s/ghi789",
                "share_id": "ghi789",
                "access_code": "456def",
                "link_type": "url",
                "confidence": 1.0,
            }
        ],
        "expected_metadata": None,
    },
    {
        "raw_text": "家业 第5集 百度 https://pan.baidu.com/s/jkl012",
        "expected_title": "家业",
        "expected_result_count": 1,
        "expected_links": [
            {
                "provider": "baidu",
                "original_text": "百度 https://pan.baidu.com/s/jkl012",
                "url": "https://pan.baidu.com/s/jkl012",
                "share_id": "jkl012",
                "access_code": None,
                "link_type": "url",
                "confidence": 1.0,
            }
        ],
        "expected_metadata": {
            "episode_no": 5,
            "episode_range": "ep5",
            "quality": None,
            "language": None,
        }
    },

    # Single provider - Xunlei (2 samples)
    {
        "raw_text": "家业 迅雷口令：xunlei123 家业 第1集",
        "expected_title": "家业",
        "expected_result_count": 1,
        "expected_links": [
            {
                "provider": "xunlei",
                "original_text": "迅雷口令：xunlei123",
                "url": None,
                "share_id": None,
                "access_code": None,
                "password": "xunlei123",
                "link_type": "command",
                "confidence": 1.0,
            }
        ],
        "expected_metadata": {
            "episode_no": 1,
            "episode_range": "ep1",
            "quality": None,
            "language": None,
        }
    },
    {
        "raw_text": "家业 全集 迅雷 magnet:?xt=urn:btih:abc123...",
        "expected_title": "家业",
        "expected_result_count": 1,
        "expected_links": [
            {
                "provider": "xunlei",
                "original_text": "magnet:?xt=urn:btih:abc123",
                "url": "magnet:?xt=urn:btih:abc123",
                "share_id": None,
                "access_code": None,
                "link_type": "magnet",
                "confidence": 1.0,
            }
        ],
        "expected_metadata": None,
    },

    # Single provider - Aliyun (2 samples)
    {
        "raw_text": "家业 阿里云盘 https://www.aliyundrive.com/s/mno456 提取码 pqr789",
        "expected_title": "家业",
        "expected_result_count": 1,
        "expected_links": [
            {
                "provider": "aliyun",
                "original_text": "阿里云盘 https://www.aliyundrive.com/s/mno456 提取码 pqr789",
                "url": "https://www.aliyundrive.com/s/mno456",
                "share_id": "mno456",
                "access_code": "pqr789",
                "link_type": "url",
                "confidence": 1.0,
            }
        ],
        "expected_metadata": None,
    },
    {
        "raw_text": "家业 S02E01 阿里云 https://www.aliyundrive.com/s/stu123",
        "expected_title": "家业",
        "expected_result_count": 1,
        "expected_links": [
            {
                "provider": "aliyun",
                "original_text": "阿里云 https://www.aliyundrive.com/s/stu123",
                "url": "https://www.aliyundrive.com/s/stu123",
                "share_id": "stu123",
                "access_code": None,
                "link_type": "url",
                "confidence": 1.0,
            }
        ],
        "expected_metadata": {
            "episode_no": 1,
            "season_no": 2,
            "episode_range": "s02e01",
            "quality": None,
            "language": None,
        }
    },

    # Multiple providers (2 samples)
    {
        "raw_text": "家业 多网盘：夸克 https://pan.quark.cn/s/abc123 提取码 xyz 百度 https://pan.baidu.com/s/def456",
        "expected_title": "家业",
        "expected_result_count": 1,
        "expected_links": [
            {
                "provider": "quark",
                "original_text": "夸克 https://pan.quark.cn/s/abc123 提取码 xyz",
                "url": "https://pan.quark.cn/s/abc123",
                "share_id": "abc123",
                "access_code": "xyz",
                "link_type": "url",
                "confidence": 1.0,
            },
            {
                "provider": "baidu",
                "original_text": "百度 https://pan.baidu.com/s/def456",
                "url": "https://pan.baidu.com/s/def456",
                "share_id": "def456",
                "access_code": None,
                "link_type": "url",
                "confidence": 1.0,
            }
        ],
        "expected_metadata": None,
    },
    {
        "raw_text": "家业 迅雷+夸克：口令 xunlei123 夸克 https://pan.quark.cn/s/xyz789",
        "expected_title": "家业",
        "expected_result_count": 1,
        "expected_links": [
            {
                "provider": "xunlei",
                "original_text": "口令 xunlei123",
                "url": None,
                "share_id": None,
                "access_code": None,
                "password": "xunlei123",
                "link_type": "command",
                "confidence": 1.0,
            },
            {
                "provider": "quark",
                "original_text": "夸克 https://pan.quark.cn/s/xyz789",
                "url": "https://pan.quark.cn/s/xyz789",
                "share_id": "xyz789",
                "access_code": None,
                "link_type": "url",
                "confidence": 1.0,
            }
        ],
        "expected_metadata": None,
    },

    # With extraction codes (6 total - already included above, but let's add 2 more)
    {
        "raw_text": "家业 第8集 夸克 https://pan.quark.cn/s/code123 提取码 pass456",
        "expected_title": "家业",
        "expected_result_count": 1,
        "expected_links": [
            {
                "provider": "quark",
                "original_text": "夸克 https://pan.quark.cn/s/code123 提取码 pass456",
                "url": "https://pan.quark.cn/s/code123",
                "share_id": "code123",
                "access_code": "pass456",
                "link_type": "url",
                "confidence": 1.0,
            }
        ],
        "expected_metadata": {
            "episode_no": 8,
            "episode_range": "ep8",
            "quality": None,
            "language": None,
        }
    },
    {
        "raw_text": "家业 阿里云 https://www.aliyundrive.com/s/nocode 提取码 789abc",
        "expected_title": "家业",
        "expected_result_count": 1,
        "expected_links": [
            {
                "provider": "aliyun",
                "original_text": "阿里云 https://www.aliyundrive.com/s/nocode 提取码 789abc",
                "url": "https://www.aliyundrive.com/s/nocode",
                "share_id": "nocode",
                "access_code": "789abc",
                "link_type": "url",
                "confidence": 1.0,
            }
        ],
        "expected_metadata": None,
    },

    # With metadata (10 total - already included above, but let's add 4 more)
    {
        "raw_text": "家业 1080p 国语 中字 第10集 夸克 https://pan.quark.cn/s/ep10",
        "expected_title": "家业",
        "expected_result_count": 1,
        "expected_links": [
            {
                "provider": "quark",
                "original_text": "夸克 https://pan.quark.cn/s/ep10",
                "url": "https://pan.quark.cn/s/ep10",
                "share_id": "ep10",
                "access_code": None,
                "link_type": "url",
                "confidence": 1.0,
            }
        ],
        "expected_metadata": {
            "episode_no": 10,
            "episode_range": "ep10",
            "quality": "1080p",
            "language": "国语",
            "subtitle": "中字",
        }
    },
    {
        "raw_text": "家业 2023年 720p 粤语 内嵌 S01E01 迅雷 magnet:?xt=urn:btih:s01e01...",
        "expected_title": "家业",
        "expected_result_count": 1,
        "expected_links": [
            {
                "provider": "xunlei",
                "original_text": "magnet:?xt=urn:btih:s01e01",
                "url": "magnet:?xt=urn:btih:s01e01",
                "share_id": None,
                "access_code": None,
                "link_type": "magnet",
                "confidence": 1.0,
            }
        ],
        "expected_metadata": {
            "episode_no": 1,
            "season_no": 1,
            "episode_range": "s01e01",
            "year": 2023,
            "quality": "720p",
            "language": "粤语",
            "subtitle": "内嵌",
        }
    },
    {
        "raw_text": "家业 4K HDR 国语 第1-5集 阿里云 https://www.aliyundrive.com/s/ep1-5",
        "expected_title": "家业",
        "expected_result_count": 1,
        "expected_links": [
            {
                "provider": "aliyun",
                "original_text": "阿里云 https://www.aliyundrive.com/s/ep1-5",
                "url": "https://www.aliyundrive.com/s/ep1-5",
                "share_id": "ep1-5",
                "access_code": None,
                "link_type": "url",
                "confidence": 1.0,
            }
        ],
        "expected_metadata": {
            "episode_no": None,
            "episode_range": "ep1-5",
            "quality": "4K",
            "language": "国语",
            "subtitle": None,
        }
    },
    {
        "raw_text": "家业 全集 12.5GB MKV 国语 夸克 https://pan.quark.cn/s/full",
        "expected_title": "家业",
        "expected_result_count": 1,
        "expected_links": [
            {
                "provider": "quark",
                "original_text": "夸克 https://pan.quark.cn/s/full",
                "url": "https://pan.quark.cn/s/full",
                "share_id": "full",
                "access_code": None,
                "link_type": "url",
                "confidence": 1.0,
            }
        ],
        "expected_metadata": {
            "episode_no": None,
            "episode_range": "all",
            "file_size": "12.5GB",
            "language": "国语",
        }
    },

    # Complex combinations (3 samples)
    {
        "raw_text": "【家业】S02E05-10 1080p 国语 中字 更新至第10集 夸克+百度 夸克 https://pan.quark.cn/s/s02e5-10 提取码 abc123 百度 https://pan.baidu.com/s/s02e5-10 提取码 def456",
        "expected_title": "家业",
        "expected_result_count": 1,
        "expected_links": [
            {
                "provider": "quark",
                "original_text": "夸克 https://pan.quark.cn/s/s02e5-10 提取码 abc123",
                "url": "https://pan.quark.cn/s/s02e5-10",
                "share_id": "s02e5-10",
                "access_code": "abc123",
                "link_type": "url",
                "confidence": 1.0,
            },
            {
                "provider": "baidu",
                "original_text": "百度 https://pan.baidu.com/s/s02e5-10 提取码 def456",
                "url": "https://pan.baidu.com/s/s02e5-10",
                "share_id": "s02e5-10",
                "access_code": "def456",
                "link_type": "url",
                "confidence": 1.0,
            }
        ],
        "expected_metadata": {
            "episode_no": None,
            "season_no": 2,
            "episode_range": "s02e5-10",
            "quality": "1080p",
            "language": "国语",
            "subtitle": "中字",
        }
    },
    {
        "raw_text": "家业 2024年 4K HDR 杜比视界 第1-12集 迅雷口令：family2024 夸克 https://pan.quark.cn/s/family2024 提取码 drama123",
        "expected_title": "家业",
        "expected_result_count": 1,
        "expected_links": [
            {
                "provider": "xunlei",
                "original_text": "迅雷口令：family2024",
                "url": None,
                "share_id": None,
                "access_code": None,
                "password": "family2024",
                "link_type": "command",
                "confidence": 1.0,
            },
            {
                "provider": "quark",
                "original_text": "夸克 https://pan.quark.cn/s/family2024 提取码 drama123",
                "url": "https://pan.quark.cn/s/family2024",
                "share_id": "family2024",
                "access_code": "drama123",
                "link_type": "url",
                "confidence": 1.0,
            }
        ],
        "expected_metadata": {
            "episode_no": None,
            "episode_range": "ep1-12",
            "year": 2024,
            "quality": "4K",
            "language": None,
            "subtitle": None,
        }
    },
    {
        "raw_text": "【家业剧场版】完整版 25GB MP4 2160p 国语 内嵌字幕 多平台 夸克 https://pan.quark.cn/s/theater 提取码 theater123 阿里云 https://www.aliyundrive.com/s/theater 提取码 cloud456 百度 https://pan.baidu.com/s/theater 提取码 baidu789",
        "expected_title": "家业剧场版",
        "expected_result_count": 1,
        "expected_links": [
            {
                "provider": "quark",
                "original_text": "夸克 https://pan.quark.cn/s/theater 提取码 theater123",
                "url": "https://pan.quark.cn/s/theater",
                "share_id": "theater",
                "access_code": "theater123",
                "link_type": "url",
                "confidence": 1.0,
            },
            {
                "provider": "aliyun",
                "original_text": "阿里云 https://www.aliyundrive.com/s/theater 提取码 cloud456",
                "url": "https://www.aliyundrive.com/s/theater",
                "share_id": "theater",
                "access_code": "cloud456",
                "link_type": "url",
                "confidence": 1.0,
            },
            {
                "provider": "baidu",
                "original_text": "百度 https://pan.baidu.com/s/theater 提取码 baidu789",
                "url": "https://pan.baidu.com/s/theater",
                "share_id": "theater",
                "access_code": "baidu789",
                "link_type": "url",
                "confidence": 1.0,
            }
        ],
        "expected_metadata": {
            "episode_no": None,
            "episode_range": None,
            "file_size": "25GB",
            "quality": "2160p",
            "language": "国语",
            "subtitle": "内嵌字幕",
        }
    },

    # Additional sample to make it 20
    {
        "raw_text": "家业 第3季 1080p 国语 中字 S03E01-05 夸克 https://pan.quark.cn/s/s03e1-5 提取码 season3",
        "expected_title": "家业",
        "expected_result_count": 1,
        "expected_links": [
            {
                "provider": "quark",
                "original_text": "夸克 https://pan.quark.cn/s/s03e1-5 提取码 season3",
                "url": "https://pan.quark.cn/s/s03e1-5",
                "share_id": "s03e1-5",
                "access_code": "season3",
                "link_type": "url",
                "confidence": 1.0,
            }
        ],
        "expected_metadata": {
            "episode_no": None,
            "season_no": 3,
            "episode_range": "s03e1-5",
            "quality": "1080p",
            "language": "国语",
            "subtitle": "中字",
        }
    },
]