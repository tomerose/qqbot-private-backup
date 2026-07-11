from copy import deepcopy
from typing import Any, Optional

RECOMMENDED_TAGS_TEXT = (
    "推荐的可选标签："
    "类型[科幻, 喜剧, 百合, 校园, 惊悚, 后宫, 机战, 悬疑, 恋爱, 奇幻, 推理, 运动, "
    "耽美, 音乐, 战斗, 冒险, 萌系, 穿越, 玄幻, 乙女, 恐怖, 历史, 日常, 剧情, 武侠, 美食, 职场]；"
    "设定[魔法少女, 超能力, 偶像, 网游, 末世, 乐队, 赛博朋克, 宫廷, 都市, 异世界, 性转, 龙傲天, 凤傲天]；"
    "角色[制服, 兽耳, 伪娘, 吸血鬼, 妹控, 萝莉, 傲娇, 女仆, 巨乳, 电波, 动物, 正太, 兄控, 僵尸, 群像, 美少女, 美少年]；"
    "地区[欧美, 日本, 美国, 中国, 法国, 韩国, 俄罗斯, 英国, 苏联, 香港, 捷克, 台湾]；"
    "情感[热血, 治愈, 温情, 催泪, 纯爱, 友情, 致郁]；"
    "来源[原创, 漫画改, 游戏改, 小说改]；"
    "受众[BL, GL, 子供向, 女性向, 少女向, 少年向, 青年向]；"
    "分级[R18]；"
    "分类[短片, 剧场版, TV, OVA, MV, CM, WEB, PV, 动态漫画]。"
    "建议优先使用以上标签，使用其他标签时可能无结果。"
)

_COMMON_FILTER_PROPERTIES: dict[str, Any] = {
    "subject_type": {
        "type": "integer",
        "description": (
            "条目类型。可选 1(书籍), 2(动画), 3(音乐), 4(游戏), 6(三次元)。"
            "不传则不按类型过滤。"
        ),
    },
    "tags": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "必须包含的标签，映射到 bgm `filter.tag`。"
            "多个值之间是“且”关系。"
            f"{RECOMMENDED_TAGS_TEXT}"
        ),
    },
    "exclude_tags": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "要排除的公共标签，映射到 bgm `filter.meta_tags`（自动加 `-` 前缀）。"
            "多个值之间是“且”关系。"
            f"{RECOMMENDED_TAGS_TEXT}"
        ),
    },
    "year_from": {
        "type": "integer",
        "description": "起始年份（含），映射为 `air_date >=YYYY-01-01`。",
    },
    "year_to": {
        "type": "integer",
        "description": "结束年份（含），映射为 `air_date <=YYYY-12-31`。",
    },
    "rating_min": {
        "type": "number",
        "description": "最低评分（含），例如 7.5。",
    },
    "rating_max": {
        "type": "number",
        "description": "最高评分（含），例如 9。",
    },
    "rank_max": {
        "type": "integer",
        "description": "最大排名（数值越小越靠前），例如 500 表示前 500 名内。",
    },
    "nsfw": {
        "type": "boolean",
        "description": "是否按 NSFW 过滤。",
    },
}

_PAGING_PROPERTIES: dict[str, Any] = {
    "limit": {
        "type": "integer",
        "description": "返回条数。默认 5，最大 10，超过会被截断到 10。",
    },
    "offset": {
        "type": "integer",
        "description": "分页偏移。默认 0，小于 0 会按 0 处理。",
    },
}


def build_filters_schema(
    *, description: str, default_subject_type: Optional[int] = None
) -> dict[str, Any]:
    properties = deepcopy(_COMMON_FILTER_PROPERTIES)
    if default_subject_type is not None:
        properties["subject_type"]["default"] = default_subject_type
    return {
        "type": "object",
        "description": description,
        "properties": properties,
    }


def build_advanced_parameters_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["keyword"],
        "properties": {
            "keyword": {
                "type": "string",
                "description": (
                    "搜索关键词。建议使用作品名、主题词或风格词。"
                    "当关键词+标签无结果时，会尝试做一次仅标签的兜底搜索。"
                ),
            },
            "filters": build_filters_schema(
                description=(
                    "高级筛选项。不同字段之间是“且”关系。"
                    "例如 year_from + year_to + tags 会同时生效。"
                )
            ),
            "sort": {
                "type": "string",
                "description": (
                    "排序方式："
                    "match(匹配度), heat(热度), rank(排名), score(评分)。"
                    "默认 match。"
                ),
            },
            **deepcopy(_PAGING_PROPERTIES),
        },
    }


def build_recent_hot_parameters_schema(*, min_recent_months: int) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "months": {
                "type": "integer",
                "description": (
                    "近期范围（月）。默认 3，最小 1。"
                    "会自动转换为从该时间点到现在的 `air_date` 过滤。"
                ),
                "minimum": min_recent_months,
            },
            "filters": build_filters_schema(
                description=(
                    "附加筛选条件。字段与 advanced_subject_search.filters 一致。"
                    "默认 subject_type=2（动画），可覆盖。"
                ),
                default_subject_type=2,
            ),
            **deepcopy(_PAGING_PROPERTIES),
        },
    }
