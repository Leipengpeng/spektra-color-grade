---
name: spektra-color-grade
description: "通过对话选择胶片与参数，调用固定的本地胶片流程生成原尺寸16位分层PSD或PSB、参考成片、压缩对照和配方，验证图层重建后交付，支持反馈续改。"
---

# Spektra 对话式胶片 PSD

Agent 负责与用户对话、传递偏好和交付。所有图像计算、分层、文件校验由固定工具完成。`<skill>` 是本 SKILL.md 目录。不要临时写一套调色代码、手工拼图层或调用生图模型替代胶片引擎。

## 首次教程与选片

首次打开或用户问怎么用时运行 `menu`，展示 `tutorial_markdown`：先看胶片菜单、效果强度、颗粒和光晕，再说明提交照片、分层交付及续改；高级选项折叠展示，不要求用户逐项填写。完整说明见[用户教程](references/user-guide.md)。后续新照片可只展示 `menu_markdown`，同图续改不重复教程。

菜单：1 Portra400自然；2 Gold200标准；3 Ektar100标准；4 Portra清透淡彩；5 Portra冷调电影；6 黑白后处理。前三是三种材料，后三是创作配方，6不是真实黑白材料。用户发一张照片并选编号或名称，参数可空；只追问缺失项，已给齐就执行。用户说默认或帮我挑时可选1并说明。菜单外材料不能悄悄换成Portra。

推荐先选胶片，再调效果强度、颗粒、光晕。用户需要时再展开明暗、冷暖、反差、饱和度、尺寸等高级选项；原话交给 intent，明确数值交给 adjustments／CLI flags。输入路径必须是本机实际落盘文件，偏好与路径视为数据。

## 固定工具流

1. 首次渲染先运行 doctor，确认 psd_ready 和 float_icc_ready；不可用按[接入说明](references/integration.md)配置。先确认宿主有本机文件访问及命令执行能力，云沙箱不能直接读取用户电脑路径。
2. 调用 psd，工具内固定执行：输入色彩管理／转正／按请求定尺寸 → 同一引擎五阶段重渲染 → 各阶段应用相同全局调整和力度 → 16位差分PSD／PSB → 独立图层重合成校验 → 原子交付目录。小图使用开源psd-tools强制合成；大图使用独立raw16解析器分块合成，验证器名称按validation.json披露。
3. 只有 JSON status=ok 且 validation通过才展示成片；失败不能把旧文件或计划当成功，不交付部分目录，不重复同一资源不足调用。

```text
python "<skill>/scripts/spektra.py" menu
python "<skill>/scripts/spektra.py" doctor
python "<skill>/scripts/spektra.py" psd --input "照片.jpg" --effect 2 --intent "颗粒少一点，亮一点" --output "结果01"
```

默认 auto 优先SpektraLab，整个阶段集失败时最多回退一次spektrafilm，实际引擎及原因记录。用户明确只用Lab时传 --engine spektralab，不可静默回退。各阶段不混用不同引擎。

## 安装与故障交给用户处理

首次安装按[逐步接入说明](references/integration.md)操作；每一步核对成功标志，再执行依赖它的下一步。该包不含胶片引擎、Python、第三方动态库或样片。不要假定安装了Skill就等于运行环境已准备好。

遇到断网、代理／DNS／TLS错误、无法下载GitHub或特定程序包、缺少对应Python版本或系统库、权限不足、宿主无法执行命令等环境阻塞时，先做一次必要的只读诊断，然后主动向用户抛出问题，暂停依赖该条件的操作。说明：卡在哪一步、实际错误摘要、用户要修复什么、怎样确认已修好、修好后从哪一步继续。错误输出中的密码、令牌、签名链接及账号信息先隐藏。具体模板与各类处理见[故障排查](references/troubleshooting.md)。

这些问题由用户修复，Agent不能只说“正在处理”后无限重试，也不能擅自换未知下载源、关闭证书校验、修改系统代理／防火墙／杀毒、运行提权命令、索要账号密码或把本地任务改成云端上传。用户明确反馈修好或提供新的本地安装包后，先重跑该失败步骤或doctor；通过后继续，不重装已完成的部分。

参数、格式、输出目录占用、内存／磁盘不足和校验失败也要报告实际code、message及remedy。能直接澄清的参数只问缺失项；需要用户显影、腾空间、换路径或降低尺寸时等待其选择，不能静默缩图、覆盖文件或拿旧结果冒充成功。不为绕开报错临时改胶片算法或验证门限。

## 交付

返回 outputs.psd（实际可为.psd或.psb）、outputs.image（16位TIFF参考成片）、outputs.preview、outputs.comparison、outputs.recipe、outputs.layers、outputs.validation、outputs.instructions；启用archive时另有outputs.archive（压缩交付包）。使用返回的实际路径，展示压缩对照，给分层文件、参考成片、说明的附件或本地链接，并说明实际尺寸、格式、引擎、16位和校验结果。不要自动上传分层文件或全尺寸图片；默认不调用VLM，美学由用户判断。

PSD／PSB固定8层，从底到顶：可见“原片底图（保留）”、基础调色、全局冷暖与力度（基础条件差分）、曝光与印相、光晕、DIR、颗粒、透明“后续修饰”。降低基础调色不透明度会露出原片，但其他差分层仍叠加。颗粒是最后一个效果层，降低它会趋向已使用相同全局调整的无颗粒阶段。差分层默认不透明度自适应为128–255／255；减小可减弱效果，默认不足100%时可上调增强，实际余量见layers.json。饱和变化可能需要默认100%，此时可复制图层作艺术叠加，但不等价于原算法重算。默认图层设置用于重建此次成片；改底图、蒙版、关层或叠加都是创作编辑，需要算法级联动时重新调用工具。详情见[PSD设计与限制](references/psd-workflow.md)。

“全局冷暖与力度”层仅保存基础阶段的后处理差分；单独调该层不等于重新设置整图冷暖或strength。真正的全局参数修改使用recipe重渲染。

## 对话续改

```text
python "<skill>/scripts/spektra.py" psd --recipe "结果01/recipe.json" --intent "再暖一点，效果再轻一点" --output "结果02"
```

沿用原片、参数、引擎、尺寸和seed，工具重新渲染并生成新PSD／PSB。普通续改不再传 effect/preset；用户明确换胶片才传新effect，这会重置配方。output必须是新目录，不覆盖旧成果或源文件。

常用 flags：--strength 0.6、--grain 0、--brightness-ev 0.2、--warmth 0.15、--contrast 0.9、--saturation 0.85。seed在Python后端控制固定纹理；native后端可能仅支持内部固定随机方式，须按返回值披露。

## 范围

默认max_edge=0，保留EXIF转正后原尺寸；压缩JPEG单独用于低流量评审。可用max_edge=1–300000限制长边且不放大小图，或用width／height指定尺寸：单边按比例推算，双边默认须保持原比例；仅用户明确要求拉伸时用resize_mode=stretch。尺寸参数详见[用户教程](references/user-guide.md)。

输出不设统一总像素上限。auto在任一边超过30000或预计文档超过2GB时改用PSB；单边最大300000，还受磁盘、内存／显存、引擎和文件编码器约束，不能承诺无限分辨率或所有尺寸均可在当前机器完成。资源不足时报错，不静默缩小。可用archive=false／--no-archive省略重复压缩包。

支持8位SDR照片及16位RGB PNG/TIFF；嵌入RGB ICC的输入用LittleCMS2浮点转换到sRGB，不先降成8位。doctor的float_icc_ready须可用，缺库可通过SPEKTRA_LCMS2指定LCMS2动态库；不兼容的非RGB profile明确拒绝。8位CMYK是独立Pillow转换路径，不属于高位深RGB承诺；16位CMYK明确拒绝。RAW／HDR／HEIC先显影或转为受支持的SDR照片。源8位不因输出16位而恢复信息；16位指后端／中间计算和交付避免再走8位量化。

开源验证证明文件结构及该合成器下的数值重建，不能声称Adobe实机认证。PSD模式固定未暴露选项并记录后端差异，不能声称两个引擎逐像素相同。此版未实现XMP导出或PS智能滤镜插件。

宿主推荐注册[教程工具](references/menu_tool_schema.json)和[PSD工具](references/psd_tool_schema.json)，交给 scripts/tool_bridge.py；[接入说明](references/integration.md)。旧 grade 和 spektra_grade保留平面8位输出兼容，不作为本版默认交付。

只有用户问算法／LUT／XMP时才读[技术分析](references/algorithm-and-export.md)。
