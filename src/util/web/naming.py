"""
Web 面板「命名规则」页（后端）。

桌面端那套可视化编辑器不做移植：它的复杂度大半来自「片段 / 层级 ↔ raw 规则串」的
双向映射与拖拽排序，而网页里用户要的是「改一条规则、立刻看到它渲染成什么」。所以
这里只做四件事 —— 列规则、给变量表、按运行期同一条路径预览、保存前校验。

**校验与预览一律复用桌面端那几个入口**（rule_template.compile_rule、
VariableListFactory、FileNameFormatter），不另写一套判据。网页放行、运行期却渲染
不出来的规则，用户在界面上看不到任何提示：任务建得出来，文件名却是空的。

本模块不碰 Qt，它跑在 HTTP 线程上；Translator 是纯查表（面板的设置页在同一位置
也是这么用的）。唯一的例外是把新规则写回配置那一步，那一步由 server.py 回到
GUI 线程执行。

术语一律取 Translator（容器里 LANG = zh_CN，取到的是中文），与桌面端同一套词；
查不到键时退回原始键名，不至于让页面出现 None。
"""

from ..common.data.naming_convention import (
    MIXED_ENTRY_TYPES, SUPPORTED_SHAPES, SampleShape, VariableListFactory,
    convention_type_map, reversed_convention_type_map,
)
from ..common.enum import ConventionType
from ..common.naming_rules import display_name, load_default_rules, load_rules, save_rules
from ..common.translator import Translator
from ..format.rule_template import RuleSyntaxError, compile_rule

from copy import deepcopy
from uuid import uuid4
import re

# 规则串里不允许出现的字面量字符。与桌面端 EditRuleDialog.validate_rule 一字不差：
# 可选段的 <> 已被解析器吃掉，节点文本里再出现 <>，就确实是用户写下的非法字符
ILLEGAL_LITERAL_RE = re.compile(r'[<>:\\"|?*\x00-\x1f]')

# 一条规则的名字与规则串的长度上限。桌面端没有这一层，但面板的请求体来自网络，
# 不设上限等于让任何人往 config.json 里塞进任意大的字符串
MAX_NAME_LENGTH = 80
MAX_RULE_LENGTH = 500

class NamingError(ValueError):
    """校验不通过。文案直接面向用户，由 server.py 转成 400"""

def _factory() -> VariableListFactory:
    """变量清单工厂是无状态的几张静态表，每次新建一份，不做模块级单例"""
    return VariableListFactory()

def _translate(mapping, key) -> str:
    """取译名，缺键时退回键名本身"""
    return mapping(key) or str(key)

def shape_label(shape, type_id) -> str:
    """
    预览行的形态名

    合集类型下两行说的是「这一集在稿件内部是单P还是多P」，通用的「单个视频 /
    合集」会把两行都说成一集，看不出哪一行才是多P那个 —— 与桌面端同样的取舍
    """
    if type_id == ConventionType.COLLECTION:
        return "多P条目" if shape == SampleShape.COLLECTION else "单P条目"

    if shape == SampleShape.MULTI:
        return "多P视频"

    if shape == SampleShape.COLLECTION:
        return "合集"

    return "单个视频"

def _variable_entry(entry: dict) -> dict:
    """变量表的一行：插进规则串的写法、说明、示例值"""
    return {
        "var": entry["variable"],
        "desc": _translate(Translator.VARIABLE_DESCRIPTION, entry["description"]),
        "example": str(entry["example"]),
    }

def type_catalog() -> list:
    """
    每种类型一份：显示名、参考变量表、预览形态、会混进来的其他类型

    变量表只给该类型的**推荐**变量（full = False），与桌面端编辑器一致 —— 其余
    变量对这个类型永远取不到值，列出来只是噪音。键空间不受影响：校验放行的仍是
    全部变量名，运行期照样认得
    """
    factory = _factory()
    catalog = []

    for key, value in convention_type_map.items():
        catalog.append({
            "value": int(value),
            "label": _translate(Translator.CONVENTION_TYPE, key),
            "vars": [_variable_entry(entry) for entry in factory.build(value, full = False)],
            "shapes": [
                {"key": str(shape), "label": shape_label(shape, value)}
                for shape in SUPPORTED_SHAPES.get(value, (SampleShape.SINGLE,))
            ],
            # 来源类列表里混着的影视 / 课程条目：它们不套用这条规则，预览区要说明
            "mixed": [
                _translate(Translator.CONVENTION_TYPE, reversed_convention_type_map.get(item, item))
                for item in MIXED_ENTRY_TYPES.get(value, ())
            ],
        })

    return catalog

def _rule_entry(entry: dict) -> dict:
    """
    配置里的一条规则 → 页面要的形状

    name 给显示名、name_key 给存回去的原值：内置规则的 name 存的是翻译键
    （DEFAULT_FOR_NORMAL 之类），页面把译名显示在输入框里，用户没动过输入框时
    必须把翻译键原样存回去 —— 存了译名，界面语言一换这条规则的名字就固化了。
    这套往返与桌面端 EditRuleDialog.resolve_name() 完全一致
    """
    return {
        "id": str(entry.get("id") or ""),
        "name": display_name(entry),
        "name_key": str(entry.get("name") or ""),
        "type": int(entry.get("type") or 0),
        "rule": str(entry.get("rule") or ""),
        "default": bool(entry.get("default")),
    }

def list_rules() -> list:
    return [_rule_entry(entry) for entry in load_rules()]

def list_builtin_rules() -> list:
    """内置规则表：页面用它做「恢复内置默认」（按 id 匹配）与「全部恢复默认」"""
    return [_rule_entry(entry) for entry in load_default_rules()]

def render_preview(rule: str, type_id: int) -> list:
    """
    按该类型的每种形态各渲染一次，走的就是运行期那条路径

    FileNameFormatter 是延迟导入的：它牵出 parse / download 两条链，而本模块在
    面板启动时就会加载。它内部把渲染异常全吞了并记一条 traceback —— 所以调用方
    必须先校验再渲染，别拿畸形规则去刷日志
    """
    from ..format.file_name import FileNameFormatter

    factory = _factory()
    rows = []

    for shape in SUPPORTED_SHAPES.get(type_id, (SampleShape.SINGLE,)):
        formatter = FileNameFormatter()
        formatter.set_variable_data(factory.build_variable_data(type_id, shape))
        formatter.set_rule(rule)

        rows.append({
            "label": shape_label(shape, type_id),
            # 渲染失败时 format() 返回 None；原样交给页面，它显示成占位
            "path": formatter.format() or "",
        })

    return rows

def validate_rule(rule: str, type_id: int) -> str:
    """
    校验一条规则串，通过时返回空串

    判据与桌面端 EditRuleDialog.validate_rule 一一对应，顺序也一致 ——
    报错文案要指向用户真正改错的那个地方
    """
    if not rule:
        return "命名规则不能为空"

    if len(rule) > MAX_RULE_LENGTH:
        return f"命名规则不能超过 {MAX_RULE_LENGTH} 个字符"

    if rule.startswith(("/", ".")) or rule.endswith(("/", ".")):
        return "规则不能以 / 或 . 开头或结尾"

    try:
        template = compile_rule(rule, True)

    except RuleSyntaxError as error:
        return f"第 {error.position} 个字符处的 < 或 > 没有配对"

    for literal in template.literal_texts():
        if ILLEGAL_LITERAL_RE.search(literal):
            return "规则里含有非法字符：< > : \\ \" | ? * 或控制字符"

    # 未知变量比对的是**全部**变量名（build_variable_data 的键空间），不是当前
    # 类型的推荐清单：个人空间下写 {parent_title} 在运行期完全正常，只是没列进
    # 推荐表而已 —— 按推荐表判会把能用的规则拒掉
    known = set(_factory().build_variable_data(type_id, SampleShape.SINGLE))
    unknown = template.field_names() - known

    if unknown:
        return "未知变量：{" + sorted(unknown)[0] + "}"

    if template.has_empty_optional_segment():
        return "可选段 < > 里至少要有一个变量"

    # 最后一道把关：真渲染一次。这里走模板而不是 FileNameFormatter —— 后者把渲染
    # 异常吞掉并往日志里记一条完整 traceback，而本函数随每次输入触发，用户打错一个
    # 格式串就能在 app.log 里留下一堆堆栈。渲染真正会抛的只有 str.format 这一处
    # （变量净化与路径规范化都不会抛），对着模板渲染拿到的是同一份判据
    try:
        template.render(_factory().build_variable_data(type_id, SampleShape.SINGLE))

    except Exception:
        return "这条规则渲染不出文件名（多半是格式串与变量的类型对不上，例如给文本变量写了 :02d）"

    return ""

def _coerce_type(raw) -> int:
    try:
        return int(ConventionType(int(raw)))

    except (TypeError, ValueError):
        raise NamingError(f"未知的规则类型：{raw}")

def _check_entries(entries: list) -> list:
    """
    逐条校验提交上来的规则表，返回规范化后的新表

    任何一条不通过就整份拒收：命名规则是一张互相牵连的表（同类型下只能有一条
    默认规则），放一半进去会让配置停在一个自相矛盾的状态上
    """
    if not isinstance(entries, list) or not entries:
        raise NamingError("命名规则不能清空，至少要保留一条")

    cleaned = []
    seen_ids = set()

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise NamingError(f"第 {index + 1} 条规则格式不正确")

        type_id = _coerce_type(entry.get("type"))

        name = str(entry.get("name") or "").strip()
        name_key = str(entry.get("name_key") or "").strip()
        rule = str(entry.get("rule") or "").strip()

        if not name:
            raise NamingError(f"第 {index + 1} 条规则的名称不能为空")

        if len(name) > MAX_NAME_LENGTH:
            raise NamingError(f"规则名称不能超过 {MAX_NAME_LENGTH} 个字符")

        # 名字会进 config.json，也会被两个界面直接渲染（桌面端的列表行、面板的
        # 规则列表）。换行与控制字符在这里拦掉，两边都不必各自再防一次
        if re.search(r"[\x00-\x1f]", name):
            raise NamingError(f"「{name}」：规则名称里不能有换行或控制字符")

        error = validate_rule(rule, type_id)

        if error:
            raise NamingError(f"「{name}」：{error}")

        rule_id = str(entry.get("id") or "").strip()

        # id 由页面留空、服务端补：网页上拿不到 crypto.randomUUID（它不是安全
        # 上下文，局域网 http 下是 undefined），而 id 是「恢复内置默认」的唯一
        # 匹配依据，不能让它随机退化
        if not rule_id or rule_id in seen_ids:
            rule_id = str(uuid4())

        seen_ids.add(rule_id)

        cleaned.append({
            "id": rule_id,
            # 用户没改过名字时把翻译键原样存回去（见 _rule_entry 的说明）
            "name": name_key if name_key and name == _display_of_key(name_key) else name,
            "type": type_id,
            "rule": rule,
            "default": bool(entry.get("default")),
        })

    _check_type_constraints(cleaned)

    return cleaned

def _display_of_key(name_key: str) -> str:
    """按名字键算出它会显示成什么，供「用户有没有改过名字」的比对使用"""
    return display_name({"name": name_key}) or name_key

def _check_type_constraints(entries: list):
    """
    同类型下的两条约束：名字唯一、有且只有一条默认规则

    同名只在这一类型内有害 —— 下载选项的下拉框按类型列规则，两条同名并排出现时
    用户无从分辨。默认规则则必须存在：FileNameFormatter 按「type 相同且
    default 为真」取规则，一条都取不到时会回退成 {leaf_title}，文件名全错且
    整个过程不报错
    """
    by_type = {}

    for entry in entries:
        by_type.setdefault(entry["type"], []).append(entry)

    for type_id, group in by_type.items():
        # 比的是**显示名**而不是存的值：内置规则的 name 存的是翻译键，自建规则
        # 存的是字面量，两者不同名不代表用户看到的是两个名字。桌面端
        # RuleListDialog._other_names() 用的是同一把尺子
        names = [display_name(entry) for entry in group]
        duplicated = sorted({name for name in names if names.count(name) > 1})

        if duplicated:
            raise NamingError(f"同一类型下有重名的规则：{'、'.join(duplicated)}")

        defaults = [entry for entry in group if entry["default"]]

        if not defaults:
            label = _translate(Translator.CONVENTION_TYPE, reversed_convention_type_map.get(type_id, type_id))

            raise NamingError(f"「{label}」类型下必须有一条默认规则")

        # 默认规则是排他的：留下最后一条，其余取消 —— 与桌面端
        # RuleListDialog._set_default_rule 收敛到指定那一条的做法等价
        for entry in defaults[:-1]:
            entry["default"] = False

def build_payload() -> dict:
    """GET 的全部内容：规则表 + 类型目录 + 内置规则表"""
    return {
        "ok": True,
        "rules": list_rules(),
        "types": type_catalog(),
        "builtin": list_builtin_rules(),
    }

def preview(rule: str, type_id) -> dict:
    """预览接口：先校验，通过了才渲染（渲染失败会往日志里写 traceback）"""
    type_id = _coerce_type(type_id)

    error = validate_rule(rule, type_id)

    if error:
        return {"ok": True, "valid": False, "error": error, "rows": []}

    return {"ok": True, "valid": True, "error": "", "rows": render_preview(rule, type_id)}

def normalized_rules(entries: list) -> list:
    """
    校验并规范化一份提交的规则表（供 server.py 回主线程执行前先把错拦下来）

    校验会触发预览渲染，放在 HTTP 线程上做；真正写配置那一步才回 GUI 线程
    """
    cleaned = _check_entries(deepcopy(entries))

    return cleaned

def apply_rules(rules: list):
    """写回配置。**必须在 GUI 线程上调用** —— config.set 会发 Qt 信号"""
    save_rules(rules)
