"""Small, inspectable vocabulary for film starting points and user adjustments."""
import copy
import math
import re

PRESETS = {
    "natural": {"label": "自然人像", "film": "kodak_portra_400"},
    "gold200": {"label": "Gold 200 胶片", "film": "kodak_gold_200"},
    "ektar100": {"label": "Ektar 100 胶片", "film": "kodak_ektar_100"},
    "airy": {"label": "清透淡彩", "film": "kodak_portra_400", "exposure_ev": 0.25,
             "contrast": 0.92, "saturation": 0.90, "warmth": -0.05, "grain": 0.15},
    "warm": {"label": "暖调复古", "film": "kodak_gold_200", "warmth": 0.15,
             "contrast": 0.96, "saturation": 0.95},
    "cinema": {"label": "冷调电影感", "film": "kodak_portra_400", "exposure_ev": -0.15,
               "warmth": -0.20, "contrast": 1.10, "saturation": 0.82, "fade": 0.02},
    "vivid": {"label": "鲜明彩色", "film": "kodak_ektar_100", "contrast": 1.05,
              "saturation": 1.10},
    "bw": {"label": "黑白后处理", "film": "kodak_portra_400", "saturation": 0.0,
           "contrast": 1.08, "grain": 0.45},
}
DEFAULTS = {
    "film": "kodak_portra_400", "paper": "kodak_portra_endura",
    "exposure_ev": 0.0, "brightness_ev": 0.0, "print_exposure": 1.0, "auto_exposure": True,
    "grain": 0.30, "halation": 0.25, "dir_couplers": 1.0,
    "contrast": 1.0, "saturation": 1.0, "warmth": 0.0, "tint": 0.0,
    "fade": 0.0, "strength": 1.0,
}
RANGES = {
    "exposure_ev": (-2.0, 2.0), "brightness_ev": (-1.5, 1.5), "print_exposure": (0.25, 4.0),
    "grain": (0.0, 1.0), "halation": (0.0, 1.0), "dir_couplers": (0.0, 1.0),
    "contrast": (0.5, 1.5), "saturation": (0.0, 1.5), "warmth": (-1.0, 1.0),
    "tint": (-1.0, 1.0), "fade": (0.0, 0.15), "strength": (0.0, 1.0),
}
STYLE_WORDS = {
    "bw": ["黑白", "monochrome", "black and white"],
    "gold200": ["gold 200", "gold200"],
    "ektar100": ["ektar 100", "ektar100", "ektar"],
    "warm": ["暖调复古", "暖色复古", "复古", "warm vintage"],
    "cinema": ["电影", "港风", "cinema", "cinematic"],
    "vivid": ["鲜艳", "鲜明", "vivid"],
    "airy": ["清透", "日系", "淡彩", "airy", "pastel"],
    "natural": ["自然", "natural", "portra"],
}

def positive_match(text, words):
    for word in words:
        for match in re.finditer(re.escape(word), text):
            prefix = re.split(r"[，,。.!！？?；;\n]|但是|不过|而是|但", text[:match.start()])[-1]
            # Keep a negation in scope across compound words: in
            # '不喜欢暖调复古', both '暖调复古' and '复古' are negative.
            negated = re.search(r"不要|不想|不喜欢|不需要|不希望|避免|别|不是|不加|不做|\b(?:without|no|not|avoid)\b", prefix)
            if not negated:
                return True
    return False

def detect_style(intent):
    text = intent.lower().strip()
    for key, words in STYLE_WORDS.items():
        candidate = re.sub(r"自然肤色|肤色自然|不自然", "", text) if key == "natural" else text
        if positive_match(candidate, words):
            return key
    return None

def compile_preferences(intent="", preset=None, overrides=None, base_settings=None):
    text = intent.lower().strip()
    if preset is not None and preset not in PRESETS:
        raise ValueError("preset must be one of: " + ", ".join(PRESETS))
    selected = preset or detect_style(intent) or "natural"
    settings = copy.deepcopy(DEFAULTS)
    settings.update({k: v for k, v in PRESETS[selected].items() if k != "label"})
    if base_settings is not None:
        settings = compile_preferences("", selected, base_settings)["settings"]
    matched = []
    adjustments = [
        (["亮一点", "更亮", "提亮", "brighter"], "brightness_ev", 0.20),
        (["暗一点", "压暗", "darker"], "brightness_ev", -0.25),
        (["暖一点", "偏暖", "温暖", "warmer"], "warmth", 0.20),
        (["冷一点", "偏冷", "青蓝", "cooler"], "warmth", -0.20),
        (["偏绿", "green tint"], "tint", -0.15),
        (["偏洋红", "magenta tint"], "tint", 0.15),
        (["柔和", "低反差", "柔一点", "soft contrast"], "contrast", -0.10),
        (["高反差", "对比强", "more contrast"], "contrast", 0.10),
        (["低饱和", "淡一点", "少饱和", "降低饱和", "less saturated"], "saturation", -0.15),
        (["更饱和", "more saturated"], "saturation", 0.15),
        (["效果轻一点", "效果再轻一点", "效果弱一点", "效果淡一点", "效果再淡一点", "减弱效果", "淡化效果", "weaker effect", "less effect"], "strength", -0.20),
        (["效果强一点", "效果再强一点", "效果重一点", "效果再重一点", "增强效果", "stronger effect", "more effect"], "strength", 0.20),
    ]
    for words, key, delta in adjustments:
        if positive_match(text, words):
            settings[key] += delta
            matched.append(key)
    if re.search(r"不要太黄|别太黄|不要泛黄|less yellow", text):
        settings["warmth"] = min(settings["warmth"], -0.10)
        matched.append("less_yellow")
    if re.search(r"(?:不要|无|去掉|没有|不想要?|不喜欢|不加|别加|不用|避免)(?:任何|明显的?)?颗粒|no grain|without grain", text):
        settings["grain"] = 0.0
        matched.append("grain_off")
    elif positive_match(text, ["少颗粒", "颗粒少", "颗粒再少", "轻颗粒", "less grain", "subtle grain"]):
        settings["grain"] = min(settings["grain"]*0.5, 0.15)
        matched.append("grain_low")
    elif positive_match(text, ["粗颗粒", "更多颗粒", "重颗粒", "more grain"]):
        settings["grain"] = min(1.0, settings["grain"]+0.20) if base_settings is not None else 0.8
        matched.append("grain_high")
    if re.search(r"不要光晕|无光晕|no halation|without halation", text):
        settings["halation"] = 0.0
        matched.append("halation_off")
    if re.search(r"保持亮度|曝光不变|别动曝光|keep exposure", text):
        settings["auto_exposure"] = False
        settings["exposure_ev"] = 0.0
        settings["brightness_ev"] = 0.0
        matched.append("keep_exposure")
    warnings = []
    if text and selected == "natural" and not matched and not positive_match(text, STYLE_WORDS["natural"]):
        warnings.append("描述未命中内置词汇，采用自然人像作为起点。可通过 preset 和显式参数继续调整。")
    if re.search(r"局部|只改|阴影.*高光|高光.*阴影|肤色保护|自然肤色|保留肤色|只调脸|reference|参考图", text):
        warnings.append("本次配方作用于整幅图像。局部蒙版或参考图精确匹配需要额外工作流。")
    if "keep_exposure" in matched:
        warnings.append("已禁用自动曝光并将曝光设置归零；胶片响应仍会改变影调，不保证输出平均亮度与原片相同。")
    for key, value in (overrides or {}).items():
        if key not in DEFAULTS:
            raise ValueError(f"Unknown adjustment: {key}")
        if key == "auto_exposure":
            if not isinstance(value, bool):
                raise ValueError("auto_exposure must be a JSON boolean")
        elif key in RANGES:
            low, high = RANGES[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{key} must be a finite number in [{low}, {high}]")
        elif value != DEFAULTS[key] and key == "paper":
            raise ValueError("This skill currently uses kodak_portra_endura paper")
        elif key == "film" and value not in {p["film"] for p in PRESETS.values()}:
            raise ValueError("Unsupported film profile")
        settings[key] = value
    # Intent-derived additions are bounded. Explicit out-of-range values above
    # are rejected, so an Agent can distinguish a bad argument from a style.
    for key, (low, high) in RANGES.items():
        settings[key] = min(high, max(low, settings[key]))
    return {"preset": selected, "label": PRESETS[selected]["label"], "intent": intent,
            "settings": settings, "matched_adjustments": matched, "warnings": warnings}
