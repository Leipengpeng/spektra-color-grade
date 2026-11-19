"""The public effect menu and deterministic ID/name selection."""
import re
from pathlib import Path
from preferences import DEFAULTS, PRESETS

EFFECTS = [
    {"id": "1", "preset": "natural", "name": "Portra 400 · 自然人像", "kind": "彩色胶片配方",
     "description": "Portra 400 基础，额外全局冷暖、反差和饱和度保持中性，适合作为比较起点。",
     "aliases": ["Portra 400", "Kodak Portra 400", "kodak_portra_400", "自然人像"]},
    {"id": "2", "preset": "gold200", "name": "Gold 200 · 标准胶片", "kind": "彩色胶片配方",
     "description": "换用 Gold 200 材料响应，全局冷暖、反差和饱和度保持中性。",
     "aliases": ["Gold 200", "Kodak Gold 200", "kodak_gold_200", "Gold200胶片"]},
    {"id": "3", "preset": "ektar100", "name": "Ektar 100 · 标准胶片", "kind": "彩色胶片配方",
     "description": "换用 Ektar 100 材料响应，全局冷暖、反差和饱和度保持中性。",
     "aliases": ["Ektar 100", "Kodak Ektar 100", "kodak_ektar_100", "Ektar100胶片"]},
    {"id": "4", "preset": "airy", "name": "Portra 400 · 清透淡彩", "kind": "创作配方",
     "description": "Portra 400 基础，降低反差、饱和度和颗粒，并加入轻微冷调。",
     "aliases": ["清透淡彩", "清透", "日系清透"]},
    {"id": "5", "preset": "cinema", "name": "Portra 400 · 冷调电影", "kind": "创作配方",
     "description": "Portra 400 基础，加入冷调、较低饱和度、反差和轻微褪色。",
     "aliases": ["冷调电影", "冷调电影感", "电影感"]},
    {"id": "6", "preset": "bw", "name": "黑白 · 灰度后处理", "kind": "创作配方",
     "description": "Portra 400 渲染后转灰度并调整反差；不是独立黑白胶片材料。",
     "aliases": ["黑白", "黑白后处理", "灰度", "monochrome", "black and white"]},
]

FILM_NAMES = {"kodak_portra_400": "Kodak Portra 400", "kodak_gold_200": "Kodak Gold 200",
              "kodak_ektar_100": "Kodak Ektar 100"}

def _normalise(value):
    return re.sub(r"[\s_.·\-]+", "", value.casefold())

def resolve_effect(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("effect must be a menu ID (1-6), supported film name, or preset name")
    target = _normalise(str(value))
    for effect in EFFECTS:
        if target in {_normalise(v) for v in [effect["id"], effect["preset"], effect["name"], *effect["aliases"]]}:
            return effect
    raise ValueError("Unsupported effect. Choose a menu ID (1-6): Portra 400, Gold 200, Ektar 100, 清透淡彩, 冷调电影, 黑白")

def effect_preset(effect=None, preset=None):
    if effect is None:
        return preset
    selected = resolve_effect(effect)["preset"]
    if preset is not None and selected != preset:
        raise ValueError("effect and preset select different recipes; provide one selection")
    return selected

def describe_selection(plan):
    film = plan["settings"]["film"]
    match = next((e for e in EFFECTS if e["preset"] == plan["preset"] and PRESETS[e["preset"]]["film"] == film), None)
    return {"effect_id": match["id"] if match else None,
            "effect_name": match["name"] if match else plan["label"] + "（兼容或自定义配方）",
            "film": film, "film_name": FILM_NAMES.get(film, film)}

def menu(report_path):
    report_path = Path(report_path)
    items = [{k: v for k, v in item.items() if k != "aliases"} | {
        "film": PRESETS[item["preset"]]["film"],
        "film_name": FILM_NAMES[PRESETS[item["preset"]]["film"]],
    } for item in EFFECTS]
    lines = ["可选胶片效果（3 种彩色胶片材料，6 个可调整配方）：", "", "| 编号 | 效果 | 配方特点 |", "|---|---|---|"]
    lines += [f"| {item['id']} | {item['name']} | {item['description']} |" for item in items]
    lines += ["", "发一张照片，再告诉我编号或名称即可。参数可不填；也可以加上‘不要颗粒、亮一点、效果轻一点’。",
              "不确定时可选 1 作为比较起点。4、5、6 是创作配方；6 是灰度后处理。最终观感请你判断。"]
    menu_markdown = "\n".join(lines)
    introduction = [
        "这是 Spektra 本地胶片调色 Skill。你给一张照片，选择胶片或配方，我会通过固定工具流生成原尺寸16位分层PSD或PSB、16位TIFF参考成片、压缩预览、前后对照和可继续修改的配方。",
        "",
        "先选胶片，再按需要调效果强度、颗粒和光晕，其他参数可以不填。拿分层文件到PS或兼容编辑器继续修饰，也可以通过对话重新渲染。",
        "",
        menu_markdown,
        "",
        "建议先用默认参数生成一版，再调整一两项：",
        "",
        "| 可以调什么 | 含义 | 可以怎么说 |",
        "|---|---|---|",
        "| 效果强度 | 0–1；越低混回越多原片 | 效果轻一点 |",
        "| 颗粒 | 0–1；0 关闭颗粒 | 不要颗粒 |",
        "| 光晕 | 0–1；0 关闭散射与晕光，默认0.25 | 光晕弱一点 |",
        "",
        "最简单的用法：发一张照片，告诉我‘选 2，不要颗粒，光晕弱一点，其他默认’。看到结果后，再说‘效果再轻一点’即可沿用原片和配方修改。",
        "",
        "默认保留照片转正后的原尺寸，另附小JPEG供低流量评审；大图自动改用PSB。照片在本地处理，原文件不覆盖，全尺寸图片和分层文件无需上传，美学由你判断。",
        "",
        "分层文件保留可见原片底图和透明修饰层。基础调色可减弱以露出原片；效果差分层可以减弱，有余量时也可增强。颗粒是最后一个效果层，降低它会趋向同样全局调整的无颗粒阶段。默认设置重建此次成片，需要原算法按参数联动时，我会沿用recipe重渲染。",
        "",
        "<details>",
        "<summary>高级参数、输出尺寸与技术范围</summary>",
        "",
        "需要时可以继续调亮度、冷暖、反差、饱和度、色偏、褪色和曝光。首次不必逐项填写，完整参数和范围见随附说明。",
        "",
        "尺寸默认max_edge=0。max_edge为1–300000时仅缩小超限长边；width或height指定单边时按比例推算，双边默认保持比例，明确resize_mode=stretch才允许拉伸。显式宽高与非零max_edge不能同时用。",
        "",
        "任一边超过30000或预计分层文档超过2GB时自动使用PSB。单边最多300000，实际仍受工作磁盘、内存/显存、引擎与编码器限制，资源不足会报错而不静默缩小。archive=false可省略delivery.zip。",
        "",
        "差分层默认不透明度约50%–100%，实际可上调倍数见layers.json；饱和变化可能已需100%。复制图层可艺术叠加，但不等价于物理模型重算。‘全局冷暖与力度’层只保存基础阶段差分，真正修改整图冷暖或整体力度需recipe重渲染。小图用psd-tools强制重合成，大图用独立raw16分块合成器，实际验证器与误差见validation.json。",
        "",
        "输入支持8位SDR照片和16位RGB PNG/TIFF；RGB ICC经LittleCMS2浮点转换，需要可用LCMS2动态库。8位CMYK另走Pillow转换，16位CMYK明确拒绝。RAW、HDR、HEIC先显影或转换。此版未提供XMP导出或PS智能滤镜插件，也不等于Adobe实机认证。",
        "",
        "</details>",
    ]
    tutorial = "\n".join(introduction)
    return {"status": "menu", "effects": items, "message": tutorial,
            "tutorial_markdown": tutorial, "menu_markdown": menu_markdown, "parameter_report": str(report_path),
            "parameter_report_markdown": report_path.read_text(encoding="utf-8") if report_path.is_file() else "",
            "recommended_controls": ["effect", "strength", "grain", "halation"],
            "defaults": {"max_edge": 0, "delivery": "layered_psd", "document_format": "auto",
                         "resize_mode": "keep_aspect", "archive": True, "depth": 16,
                         "preferred_engine": "auto", "paper": DEFAULTS["paper"]},
            "next_step": "If a photo and an explicit effect are already supplied, render immediately. Otherwise ask only for the missing photo or effect."}
