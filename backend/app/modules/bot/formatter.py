"""Centralized, escaped Telegram HTML formatting."""

from datetime import datetime
from html import escape
from urllib.parse import urlsplit

from app.modules.resource.query_schema import (
    LinkView,
    ResourceDetail,
    ResourceListItem,
    SourceView,
)


def _escaped(value: object) -> str:
    return escape(str(value), quote=True)


def _code(value: object) -> str:
    return f"<code>{_escaped(value)}</code>"


def _safe_url(url: str) -> str:
    escaped_url = _escaped(url)
    try:
        parsed = urlsplit(url)
    except ValueError:
        return escaped_url
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return escaped_url
    return f'<a href="{escaped_url}">{escaped_url}</a>'


def _format_list_item(item: ResourceListItem) -> str:
    lines = [
        f"<b>{_escaped(item.title)}</b>",
        f"ID: {_code(item.resource_id)}",
        (
            f"类型: {_code(item.content_type)} / "
            f"{_code(item.resource_type)}"
        ),
        f"集数: {_code(item.episode_range)}",
        f"来源数: {_code(item.source_count)}",
    ]
    return "\n".join(lines)


def _format_link(link: LinkView) -> str:
    lines = [f"- <b>{_escaped(link.provider)}</b>"]
    if link.url:
        lines.append(f"  链接: {_safe_url(link.url)}")
    if link.access_code:
        lines.append(f"  提取码: {_code(link.access_code)}")
    if link.password:
        lines.append(f"  密码: {_code(link.password)}")
    return "\n".join(lines)


def _format_source(source: SourceView) -> str:
    channel = source.channel_title or "未知频道"
    username = (
        f" @{_escaped(source.channel_username)}"
        if source.channel_username
        else ""
    )
    return (
        f"- {_escaped(channel)}{username}\n"
        f"  匹配: {_escaped(source.matched_reason)}"
    )


def format_help() -> str:
    return "\n".join(
        [
            "<b>可用命令</b>",
            "/latest [1-50]",
            "/search &lt;关键词&gt;",
            "/resource &lt;ID&gt;",
            "/help",
        ]
    )


def format_parameter_error(command: str) -> str:
    usages = {
        "latest": "/latest [1-50]",
        "search": "/search <关键词>",
        "resource": "/resource <ID>",
    }
    usage = usages.get(command, "/help")
    return "\n".join(
        [
            "<b>参数错误</b>",
            f"用法: {_code(usage)}",
        ]
    )


def format_unknown_command() -> str:
    return "\n".join(
        [
            "<b>未知命令</b>",
            f"查看帮助: {_code('/help')}",
        ]
    )


def format_query_error() -> str:
    return "<b>查询暂时不可用，请稍后重试。</b>"


def format_latest(items: list[ResourceListItem]) -> str:
    if not items:
        return "<b>暂无资源</b>"
    body = "\n\n".join(_format_list_item(item) for item in items)
    return f"<b>最新资源</b>\n\n{body}"


def format_search_results(
    query: str,
    items: list[ResourceListItem],
) -> str:
    if not items:
        return f"<b>未找到资源</b>\n关键词: {_code(query)}"
    body = "\n\n".join(_format_list_item(item) for item in items)
    return (
        f"<b>搜索结果</b>\n"
        f"关键词: {_code(query)}\n\n"
        f"{body}"
    )


def format_resource_not_found(resource_id: int) -> str:
    return f"<b>未找到资源</b>\nID: {_code(resource_id)}"


def format_resource_detail(detail: ResourceDetail) -> str:
    lines = [
        f"<b>{_escaped(detail.title)}</b>",
        f"作品: {_escaped(detail.work_title)}",
        f"ID: {_code(detail.resource_id)}",
        (
            f"类型: {_code(detail.content_type)} / "
            f"{_code(detail.resource_type)}"
        ),
        f"集数: {_code(detail.episode_range)}",
        f"来源数: {_code(detail.source_count)}",
    ]
    if detail.description:
        lines.append(f"描述: {_escaped(detail.description)}")
    if detail.links:
        lines.append("<b>链接</b>")
        lines.extend(_format_link(link) for link in detail.links)
    if detail.sources:
        lines.append("<b>来源</b>")
        lines.extend(_format_source(source) for source in detail.sources)
    return "\n".join(lines)


def _format_notification(
    heading: str,
    detail: ResourceDetail,
    source_count: int,
    occurred_at: datetime,
) -> str:
    lines = [
        f"<b>{heading}</b>",
        f"<b>{_escaped(detail.title)}</b>",
        f"资源 ID: {_code(detail.resource_id)}",
        f"来源数: {_code(source_count)}",
        f"时间: {_code(occurred_at.isoformat())}",
    ]
    if detail.links:
        lines.append("<b>链接</b>")
        lines.extend(_format_link(link) for link in detail.links)
    return "\n".join(lines)


def format_resource_created_notification(
    detail: ResourceDetail,
    source_count: int,
    occurred_at: datetime,
) -> str:
    return _format_notification(
        "新增资源",
        detail,
        source_count,
        occurred_at,
    )


def format_resource_merged_notification(
    detail: ResourceDetail,
    source_count: int,
    occurred_at: datetime,
) -> str:
    return _format_notification(
        "资源更新",
        detail,
        source_count,
        occurred_at,
    )
