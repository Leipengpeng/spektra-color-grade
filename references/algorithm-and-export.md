# Spektra 算法背景、3D LUT 与 XMP 的可迁移边界

审查日期：2026-11-12。代码快照：SpektraLab `a46627d250545a394ca21522f8a28b0fb89e4c42`，本机 Windows host 1.3.4；spektrafilm 0.3.4，`3bb2c2d2801ff68b92019cf1dbcbb133d60832bc`。软件和材料版本改变后，应重新核对。

## 对 language-guided-lut 的两个直接回答

已下载并阅读 `Leipengpeng/language-guided-lut`，提交 `08360698ff02607789d5c6f31b33d06284ffae98`。它已经提供普通可编辑 XMP 拟合和 Creative Profile 嵌表两条路线，不需要从零编写 LUT→XMP。

第一，spektrafilm 有完整 RGB LUT Creator；SpektraLab Windows host 也有 export_cube，但后者导出的是负片 CMY→印相／扫描阶段的表。二者都不能直接被当成“默认完整胶片模拟的天然无损 RGB LUT”。正式 LUT 模式关闭空间、随机及部分自适应功能，有限采样、序列化和应用插值还要另算。

第二，可以工程化串接“正确色彩域的固定胶片颜色 LUT→现有 XMP 转换器”，交付近似胶片色彩的 XMP。若目标是复现完整引擎，误差并不只在 LUT→XMP 一段；若把某张 LUT 及其执行规则定义为产品基准，可以只评估它之后新增的误差，但不能据此反推该 LUT 与完整物理模拟无损等价。

## 先回答“能否无损”

| 目标 | 判断 |
|---|---|
| 用一张通用、位置无关的 RGB 3D LUT，复现开启空间和随机效果的完整引擎 | 不可能普适无损表示：同一个 RGB 会因邻域、图像统计或随机场产生不同结果 |
| 固定胶片、相纸、曝光及输入输出约定，关闭或外置空间、随机、自适应项，只保留确定颜色函数 | 可以正向烘焙为 LUT；常用有限网格的插值一般仍有误差 |
| 固定点函数，输入严格限制为 8 位 RGB，逐一枚举全部颜色并直接查表 | 理论上可精确复现所有离散输入；这需要 256³ 个表项，并非普通 33³／65³ LUT 的普遍保证 |
| 反求一组 Lightroom/Camera Raw 普通预设参数 | 可尝试数值拟合；是否严格等价取决于目标映射是否属于 Adobe 算子族，不能保证 |
| 用增强 profile 或支持 LUT 的 Adobe 版本承载颜色 LUT | 可作为交付方式；色彩域、处理阶段、插值与空间效果仍要分别核对 |
| 从调色成片唯一恢复原始照片 | 一般不存在通用无损逆变换，裁切、灰度化、量化等会丢失信息 |

这里的“导出等效前向映射”和“求数学逆函数”是两个问题。内部使用光谱和非线性计算，不会自动阻止前向 LUT 烘焙。

## 项目背景与实际计算

Andrea Volpato 的 spektrafilm 是 Python 参考实现，以公开厂家材料数据、摄影化学模型和经验校正构造负片—印相—扫描过程。SpektraLab 在这个模型基础上提供原生 C++/GPU 引擎与桌面产品；本机 Windows 路径使用 Vulkan，macOS 路径使用 Metal。Skill 再增加偏好解析、配方选择、全局后处理与交付文件。[spektrafilm 项目介绍](https://github.com/andreavolpato/spektrafilm/blob/3bb2c2d2801ff68b92019cf1dbcbb133d60832bc/README.md)、[SpektraLab 架构](https://github.com/JamesQiu2005/SpektraLab/blob/a46627d250545a394ca21522f8a28b0fb89e4c42/ARCHITECTURE.md)

材料模拟有物理依据，但并非所有系数都来自独立实测。原项目说明部分 CMY 染料数据采用通用近似，部分偶合剂参数含经验校正。它也不能从相机 JPEG 唯一还原拍摄现场的光谱。RGB 只有三个刺激值，多种光谱可以形成相同颜色；项目选择的是符合先验的光谱代表。[材料说明](https://github.com/andreavolpato/spektrafilm/blob/3bb2c2d2801ff68b92019cf1dbcbb133d60832bc/README.md#introduction)、[光谱重建实现](https://github.com/andreavolpato/spektrafilm/blob/3bb2c2d2801ff68b92019cf1dbcbb133d60832bc/src/spektrafilm/utils/spectral_upsampling.py)

主要流程如下：

1. 解码输入 RGB 的传递函数并做色彩管理。SpektraLab 内部采用线性 ProPhoto RGB；旧 grade 入口先整理为8位sRGB；新PSD入口支持8位SDR照片与16位RGB PNG/TIFF，嵌入RGB ICC经LittleCMS2 TYPE_RGB_FLT转到sRGB，不先量化为8位。再以浮点交给Python，或经16位带ICC的TIFF交给native。无ICC按编码sRGB解释；不兼容profile或所需动态库缺失会明确报错。8位CMYK沿用独立Pillow转换路径，16位CMYK明确拒绝。RAW／HDR／HEIC须先显影或转换；保留原尺寸或输出16位均不会恢复已经丢失的源数据。
2. 用 RGB／XYZ 色度与亮度估计胶片三层感光响应。光谱采样覆盖 380–780 nm，每 5 nm 一点，共 81 波段。默认 Hanatos 路径把光谱与感光层敏感度的积分预计算为二维色度表，运行时查表再恢复亮度尺度，并不要求整张照片常驻 81 个通道。
3. 施加曝光与光传播，再以 log 曝光—密度特性曲线计算胶片染料密度，加入 DIR 显影抑制与颗粒模型。
4. 由 CMY 染料与基底重建随波长变化的光学密度，以 10 的负密度次幂求透射率；虚拟放大机的光源、滤片和负片透射共同形成相纸曝光，再计算相纸染料密度。
5. 虚拟扫描把相纸反射光与观看光源、CIE 配色函数积分为 XYZ，转换到目标 RGB，执行输出色域和编码处理。[曝光阶段](https://github.com/andreavolpato/spektrafilm/blob/3bb2c2d2801ff68b92019cf1dbcbb133d60832bc/src/spektrafilm/runtime/stages/filming.py)、[显影](https://github.com/andreavolpato/spektrafilm/blob/3bb2c2d2801ff68b92019cf1dbcbb133d60832bc/src/spektrafilm/model/develop.py)、[印相](https://github.com/andreavolpato/spektrafilm/blob/3bb2c2d2801ff68b92019cf1dbcbb133d60832bc/src/spektrafilm/runtime/stages/printing.py)、[扫描](https://github.com/andreavolpato/spektrafilm/blob/3bb2c2d2801ff68b92019cf1dbcbb133d60832bc/src/spektrafilm/runtime/stages/scanning.py)

相同名称的“曝光”“冷暖”“反差”，在 Adobe 与本引擎中也不必处于同一个处理阶段或采用同一个公式，数值不能直接一一照抄。

## 数学上怎样判断 3D LUT 能否表示

标准位置无关 RGB LUT 表示一个固定函数：

`y = L(r, g, b)`

固定颜色核也可写成 `y = fθ(r,g,b)`，θ 包括材料、曝光、印相和色彩域约定。光谱中间维数多、曲线非线性、通道互相耦合，都不妨碍它仍然是三输入、三输出的函数；采样这个已知函数即可烘焙，无需先从少量照片猜参数。

完整引擎则更接近：

`Y(p) = Gθ(I, p, η)`

I 是整幅照片，p 是位置，η 是随机场或种子。这不是仅由当前像素 RGB 决定的函数。最小反例是两个相同灰色像素，一个邻近亮光，另一个邻近暗部：开启光晕时它们可以受到不同的散射贡献。若它们都输入同一个 L(c)，查表结果只能相同，与目标不同构成矛盾。因此，增加 LUT 分辨率也不能补回被省略的邻域信息。

即便固定随机种子，颗粒仍可随位置变化；固定种子解决重复性，不会把随机场变成位置无关的 RGB 函数。同一张照片中，相同输入 RGB 若对应不同目标输出，也无法逐像素精确拟合成一张颜色 LUT。少量照片上没有出现冲突，也不能证明新照片或未采样颜色上成立。

这个限制有严格的误差下界：若相同输入颜色 c 的两个上下文要求输出 y1、y2，且 y1 不等于 y2，则任何查表结果 L(c) 都满足 `max(‖L(c)−y1‖, ‖L(c)−y2‖) ≥ ‖y1−y2‖/2 > 0`。例如简单邻域平均算子中，中心均为 0、邻点分别为 0 和 1，目标输出是 0 和 0.5，任何颜色 LUT 的两例最大误差至少为 0.25。这是数学示例，不是项目实测误差；它说明此类结构误差无法靠加密网格消除。需要确认上下文依赖确实影响输出，不能仅凭代码读取了邻点就作结论。

### 有限网格为何通常不能“零误差”

三线性插值在每个小立方体内属于多仿射函数族；四面体插值在每个四面体内是仿射函数。一般颜色函数不属于这两个有限函数族。例如 `f(r,g,b)=(r²,g,b)`，沿 r 方向具有曲率，任何有限非退化线性分段都不能在整个连续区间与 r² 完全相同。

因此，9³、17³、33³、65³ 等有限表格通常是近似。非线性本身并非绝对障碍：某些多仿射非线性函数可由三线性 LUT 精确表示，或者目标函数本身就定义为同一张 LUT。应逐个映射证明，不能把“所有非线性都不可能无损”当通则。

对紧致颜色域上的连续点函数，在数学实数模型下，加密网格可以使合适插值的误差趋近于零；“任意小”仍不等于某个有限网格必然“严格等于零”。硬跳变、有限精度、量化和宿主实现需要另外检查。感知上看不出差异、误差小于一个码值、每个输出字节一致，也是三个不同验收标准。

### 8 位离散输入的精确例外

如果输入域严格只有 256³ 种 RGB，且 f 已是固定、确定、位置无关的函数，可以为每个输入直接保存它的最终输出，再按整数索引查表。如此可以对该离散域逐项精确。

表大小：RGB8 输出需要 `256³ × 3 = 50,331,648` 字节，即 48 MiB；若每通道存 32 位浮点则为 192 MiB。以上不含容器、元数据或实现开销，也不是 .cube 文本文件大小。色彩编码、位深、参数或宿主流程改变后，此保证不再自动成立；它仍无法表示完整算法的邻域和图像依赖。

精确性针对预先确定的像素输出位模式，文件编码和元数据字节是另一层问题；也不能将 32 位表值当成任意精度实数。标准宿主未必支持 256³ 网格，这是一项存在性说明，不是当前 Skill 或 Adobe 已具备此无损导出功能。

## 各算子能否进入颜色 LUT

| 算子 | 固定条件下的判断 |
|---|---|
| 传递函数、色域转换、光谱估计、固定曝光、胶片／相纸曲线、染料吸收积分、固定滤片和预闪 | 确定的点映射，可采样；有限表格有插值误差 |
| 无空间扩散的 DIR 跨层抑制 | 通道耦合仍是同一像素的 RGB→RGB，可采样 |
| DIR 扩散 | 依赖邻域，需在 LUT 外实现 |
| 光晕、散射、镜头模糊、扫描锐化 | 邻域运算，需另实现 |
| 颗粒与随机 glare | 随机／空间场，需另实现；仅固定种子不够 |
| 相机自动曝光、开启的高光 boost | 读取照片统计或全图最大值；需先测量并固定或外置 |
| 相纸中灰曝光归一化、扫描黑白参考校正 | 来自合成中灰和材料参考，并非照片 min/max；固定材料与参数后原则上可烘焙 |
| Skill 追加的明暗、冷暖、反差、饱和度、褪色及固定混合强度 | 若其输入颜色核是点映射，这些确定后处理也可合并进去 |

当前两份本地代码的完整模式默认开启 DIR，扩散核心为 20 µm、尾尺度 200 µm、尾权重 0.06。只关闭颗粒与光晕，仍不足以证明流程已变为纯点函数；还需核对 DIR 扩散、锐化、glare 和测光等。[DIR 实现](https://github.com/andreavolpato/spektrafilm/blob/3bb2c2d2801ff68b92019cf1dbcbb133d60832bc/src/spektrafilm/model/couplers.py)、[默认参数](https://github.com/andreavolpato/spektrafilm/blob/3bb2c2d2801ff68b92019cf1dbcbb133d60832bc/src/spektrafilm/runtime/params_schema.py)、[相机测光](https://github.com/andreavolpato/spektrafilm/blob/3bb2c2d2801ff68b92019cf1dbcbb133d60832bc/src/spektrafilm/utils/autoexposure.py)、[固定扫描参考](https://github.com/andreavolpato/spektrafilm/blob/3bb2c2d2801ff68b92019cf1dbcbb133d60832bc/src/spektrafilm/runtime/services/color_reference.py)

## 原项目已有 LUT 功能，但有特定语义

Python 仓库的 LUT Creator 支持完整颜色链与拆分的 1/2/3/4 LUT 拓扑，输出 .cube、.3dl 和 Hald-CLUT，并可生成 OCIO 配置。它在专用 lut_mode 中关闭空间和随机项、自动曝光、高光 boost、扫描锐化等；还重置胶片曝光与印相曝光、关闭部分参考校正。后面这些设置并非全部在数学上不可烘焙，而是现有导出器采用的标准化策略。四段式 LUT 暴露中间阶段，供宿主插入光晕或颗粒；四张表本身不会生成空间效果。[构建器](https://github.com/andreavolpato/spektrafilm/blob/3bb2c2d2801ff68b92019cf1dbcbb133d60832bc/src/spektrafilm_lut_creator/builders.py#L774-L795)、[LUT 模式开关](https://github.com/andreavolpato/spektrafilm/blob/3bb2c2d2801ff68b92019cf1dbcbb133d60832bc/src/spektrafilm/runtime/params_builder.py#L99-L142)

因此，现成导出器不是当前 Skill 全部配方的自动克隆。需要额外适配以保留可烘焙的确定参数，明确输入增益、动态范围、输入／输出色彩空间和传递函数，并对宿主结果验证。

SpektraLab Windows host 的 export_cube 更窄：它导出随应用预烘焙的“归一化负片 CMY 密度→印相／扫描 RGB”表，不捕获当前 session 所有滑杆。这种表的输入不是原始照片的 sRGB；直接套在照片上会使用错误的输入语义。[host 导出入口](https://github.com/JamesQiu2005/SpektraLab/blob/a46627d250545a394ca21522f8a28b0fb89e4c42/engine/host/main.cpp#L1539-L1555)、[印相表元数据](https://github.com/JamesQiu2005/SpektraLab/blob/a46627d250545a394ca21522f8a28b0fb89e4c42/engine/resources/print_luts.json)

## 小规模数值核查：有限网格确实出现误差

用固定 Portra 400／Portra Endura，开启 lut_mode，关闭测光、空间与随机项，额外关闭可选印相／扫描加速 3D LUT，保留固定二维光谱表。测试函数明确为“编码 sRGB→sRGB 解码→固定线性 sRGB 模拟→sRGB 编码”，浮点比较前不裁切。用固定种子 20261112 选 1000 个不同的 8 位 RGB/255，排除烘焙网格节点；按标准三线性插值比较原函数与采样表。

| 网格 | 最大绝对通道误差 | 平均通道误差 | 95 分位误差 | 8 位输出至少一通道不同的点 | 最大码值差 |
|---|---:|---:|---:|---:|---:|
| 9³ | 0.02959113 | 0.00350622 | 0.01083818 | 926/1000（92.6%） | 7 |
| 17³ | 0.00971934 | 0.00100544 | 0.00311509 | 562/1000（56.2%） | 3 |

浮点单位为 [0,1] 编码 sRGB 通道值，统计覆盖 3000 个通道样本；8 位量化使用 round-to-nearest-even。它只证明这两个有限网格对该固定函数和这些测试点不是严格零误差，不代表全域最坏误差，也不能外推到 33³／65³。没有进行美学评分。本机另交付复现脚本、完整参数、逐点结果和实际网格。

## XMP、Lab 与“反向”各指什么

XMP 是可扩展元数据格式。把 Spektra 参数保存为自定义 XMP 标签是可行的，但读取端仍需执行 Spektra 算法。普通 Lightroom/Camera Raw 预设只调用 Adobe 已实现的调整算子，不能通过改扩展名获得原引擎。[Adobe XMP 规范](https://developer.adobe.com/xmp/docs/xmp-specifications/)

可用曲线、HSL 等去拟合颜色，也可用增强 XMP profile 承载 RGB LUT／Look Table；SDK 说明这些表位于不同处理阶段。Adobe 也支持部分局部修正与颗粒，不能说所有 XMP 都只支持全局颜色。准确区别在于：执行 Adobe 支持的算子，与执行 Spektra 自定义物理算子，不是相同的能力。[Adobe 官方 Profile SDK](https://www.adobe.com/support/downloads/dng/dng_sdk.html)、[增强 profile 文档所在 SDK](https://download.adobe.com/pub/adobe/lightroom/profile-sdk/ACR_and_Lightroom_Profile_SDK.zip)

截至 2026-11-12 查到的 Camera Raw 官方文档（11 月 10 日更新）已支持直接导入 CUBE，因此无需普遍假定一定要先封装增强 profile。具体用户版本与 Lightroom 支持情况仍要分别实测。[Camera Raw 官方 LUT 文档](https://helpx.adobe.com/camera-raw/desktop/edit-and-enhance-images/tone-and-color/apply-luts-to-photos-in-camera-raw.html)

如果“拉的空间”指的是 CIE Lab：它是另一套颜色坐标，L* 为明度，a*/b* 为颜色轴。可以在 Lab 坐标中定义或采样颜色函数，但换坐标不会消除邻域、随机性或信息损失。如果指 LUT，则适用上面的点映射判断。[Adobe 颜色模式说明](https://helpx.adobe.com/photoshop/desktop/adjust-color/choose-colors/color-modes-in-photoshop.html)

真正的数学逆函数还要求映射一一对应。原模型有曲线端点钳位，交付链还有裁切、灰度化与量化，多个输入可能成为同一输出，所以不能普遍从成片唯一还原原图。即使某个无噪声卷积可在理论上逆解，其逆也不等于一张 RGB 3D LUT。[曲线端点实现](https://github.com/andreavolpato/spektrafilm/blob/3bb2c2d2801ff68b92019cf1dbcbb133d60832bc/src/spektrafilm/utils/fast_interp.py)

## language-guided-lut 实际做了哪些拟合

### 原图／参考到 LUT

其默认 MKL 路径把图片颜色变成 Oklab，用均值和协方差拟合 Gaussian，再计算 Monge–Kantorovich 仿射运输；多图 Bures 用于两组 Gaussian 的融合，单图时退化为单 Gaussian 运输。它不按对应像素还原参考图，颜色统计也不能唯一识别参考的全部调色行为。对 RGB 网格采样后输出默认 65³ LUT，再重载，以三线性插值渲染。

项目本身分别记录“参考图差异”“解析颜色映射与 LUT 的采样误差”“保存重载误差”和“PNG 与重载 LUT 的采样一致性”。后者不能证明 LUT 无损复现最初参考。[MKL 与 LUT 生成](https://github.com/Leipengpeng/language-guided-lut/blob/08360698ff02607789d5c6f31b33d06284ffae98/skills/color-match-lut/scripts/color_match_lut.py#L248-L300)、[LUT 近似与重载检查](https://github.com/Leipengpeng/language-guided-lut/blob/08360698ff02607789d5c6f31b33d06284ffae98/skills/color-match-lut/scripts/color_match_lut.py#L502-L545)

已有兼容的胶片 RGB LUT 时，参考生成和 MKL／Bures 拟合可以跳过；`color_match_lut.py apply` 可直接应用。`text_style_lut.py --guide-cube` 则只生成供 Image 使用的引导图，finish 仍重新拟合，不是直接复用 LUT 的捷径。[直接 apply 入口](https://github.com/Leipengpeng/language-guided-lut/blob/08360698ff02607789d5c6f31b33d06284ffae98/skills/color-match-lut/scripts/color_match_lut.py#L601-L625)

### LUT 到普通可编辑 XMP

`lut_to_xmp.py` 以原照片为主产生训练样本，而非遍历整张 LUT：最多 2000 个照片像素占权重 80%，384 个色域探针占 16%，48 个灰阶占 4%；另用照片留出像素及 4096 个随机 RGB 评估。[目标与采样](https://github.com/Leipengpeng/language-guided-lut/blob/08360698ff02607789d5c6f31b33d06284ffae98/skills/color-match-lut/scripts/lut_to_xmp.py#L160-L187)

彩色候选是 27 变量的三 RGB 曲线、33 变量的主曲线＋八色 HSL，以及 60 变量的混合模型；每条曲线只有 9 个固定横坐标。黑白候选为 9 变量主曲线或再加 8 个灰度混色控制。它不拟合任意三维表、曝光、白平衡、颗粒或蒙版；这是该导出器的选择，不是 XMP 格式本身的总能力。[候选定义](https://github.com/Leipengpeng/language-guided-lut/blob/08360698ff02607789d5c6f31b33d06284ffae98/skills/color-match-lut/scripts/lut_to_xmp.py#L189-L205)

目标函数是加权 Oklab 数据误差，加上负斜率、曲线平滑和恒等先验正则；使用有界非线性最小二乘，单一初始化，最多 45 次函数评估，没有零误差／全局最优保证。拟合后曲线与滑杆取整，并将曲线投影为单调节点。[优化函数](https://github.com/Leipengpeng/language-guided-lut/blob/08360698ff02607789d5c6f31b33d06284ffae98/skills/color-match-lut/scripts/lut_to_xmp.py#L86-L141)

baseline、RAWmakase 与 Photo Room 都是本地响应模型，没有调用本机 Adobe。RAWmakase 使用测量响应和公式，也仍有其处理基线假设。报告在写出、读回 XMP 后，继续用同一本地模型评估；指标为 100×Oklab 欧氏距离的中位数／95 分位，不是 Adobe 实机误差或全域最大误差。[响应模型假设](https://github.com/Leipengpeng/language-guided-lut/blob/08360698ff02607789d5c6f31b33d06284ffae98/skills/color-match-lut/scripts/adobe_response.py#L1-L16)、[序列化后的评估](https://github.com/Leipengpeng/language-guided-lut/blob/08360698ff02607789d5c6f31b33d06284ffae98/skills/color-match-lut/scripts/lut_to_xmp.py#L203-L236)

所以，即使冻结源 LUT，实际 Adobe 结果也可分为两项：`Adobe输出−LUT目标 = (Adobe输出−本地XMP模型输出) + (本地XMP模型输出−LUT目标)`。代码测量采样点上的第二项，没有实测第一项。

### LUT 到 Creative Profile XMP

`lut_to_xmp_profile.py` 直接嵌三维表，不做上面的滑杆参数优化。但默认最大网格 32³，可设置到 64³；输入 65³ 时默认重新采样为 32³，设 64 仍会重采样。表值再量化到 16 位。仅节点量化误差约为每通道不超过 0.5/65535，它不是最终成片误差上限。zlib／Base85／XML 编码本身是无损的序列化，不应与表值量化混淆。[封装实现](https://github.com/Leipengpeng/language-guided-lut/blob/08360698ff02607789d5c6f31b33d06284ffae98/skills/color-match-lut/scripts/lut_to_xmp_profile.py#L28-L91)

封装器固定声明 sRGB 原色、sRGB 编码与超色域裁切，不会替外部表转换颜色语义。其重采样采用三线性，而 Adobe DNG SDK 的 RGB 表参考实现采用四面体插值；节点间不应假定相同。具体 Adobe 版本和显影链仍须实测。[DNG SDK RGB 表参考实现](https://android.googlesource.com/platform/external/dng_sdk/%2B/de700ad461e35af50b28b861943a0b0753b10929/source/dng_reference.cpp)

SpektraLab 的印相表以归一化 CMY 密度为输入、encoded Display P3 为输出，直接送入这个 sRGB→sRGB 封装器会同时错置输入物理量和输出颜色空间。这是接口语义错误，不能仅当成小的拟合误差。原生 host 写 .cube 节点还仅保留六位小数，格式本身也不能保证任意 float32 节点逐位往返。[原生 cube 文本写出](https://github.com/JamesQiu2005/SpektraLab/blob/a46627d250545a394ca21522f8a28b0fb89e4c42/engine/host/output.cpp#L824-L837)

### 应如何理解“只有一段损失”

| 基准与步骤 | 是否可省略或忽略 |
|---|---|
| 原项目从参考图提取全局颜色，再拟合 MKL | 有兼容现成 LUT 时可以跳过 |
| 完整胶片模拟降为固定颜色核 | 对完整效果是改变目标；有空间效果时不能当作零差异 |
| 固定颜色核采样成有限 LUT | 一般存在网格插值误差，另有保存精度和约定问题 |
| LUT 拟合普通 XMP | 有参数族、采样、优化、正则和整数化误差 |
| 本地 XMP 模型换成实际 Adobe | 另有宿主响应与显影链差异，原报告未实测 |
| LUT 直接嵌 Creative Profile | 省去少量滑杆拟合，仍可能有重采样、量化、插值和宿主差异 |

如果以完整 Spektra 渲染为目标，在共同颜色坐标中可以拆成：`完整渲染−Adobe输出 = (完整渲染−固定颜色核) + (固定颜色核−LUT输出) + (LUT输出−本地XMP输出) + (本地XMP输出−Adobe输出)`。这是误差来源划分；不同指标或百分比不能直接相加，实际误差可能抵消，也可能叠加。

如果产品验收基准明确就是“指定 LUT、指定色彩域、指定插值与裁切的输出”，原算法到 LUT 的步骤可以不再属于这份验收合同。此时应称“匹配该 LUT”，不据此声称完整胶片算法无损迁移。

可以复用现有 `lut_to_xmp.py --content ... --lut ... --engine rawmakase`，或选 Creative Profile 路线。前提是先生成并验证正确域的完整 RGB 颜色 LUT；不能直接连接 Lab 的 CMY 印相表。新项目已下载并阅读，本轮没有运行其 XMP 优化器、导入 Adobe 或新增 XMP 导出按钮。

## 对产品实现的含义

若目标是手机本地单张调色，可进一步验证“颜色 LUT＋独立空间／颗粒算子”的方案。为了接近原模型，应在曝光阶段处理散射／光晕、在密度阶段处理颗粒，再接印相与扫描；只在最终 RGB 上叠一层颗粒或泛光是另一种近似。完整保真仍可选择运行原引擎。

拟合的验收应分别报告：输入域和参数、是否关闭／外置空间项、插值方式、浮点误差、量化后字节差异，以及独立样本误差。训练色块零误差或人眼难辨，均不能代替全域无损证明。美学由用户人工核查。本报告未声称已完成 XMP 导出、Adobe 宿主验证或微信小程序实机测试。


## 当前PSD交付实现

固定分层工作流以真实后端输出的五阶段Y0至Y4为输入，对每个Yi执行同一个后处理P（包括strength）。图层固定8层，从底到顶为：可见原片底图、raw Y0基础调色、全局冷暖与力度（基础条件差分）、曝光与印相、光晕、DIR、颗粒、透明修饰层。五个差分依次为 `P(Y0)−Y0`、`P(Y1)−P(Y0)`、`P(Y2)−P(Y1)`、`P(Y3)−P(Y2)`、`P(Y4)−P(Y3)`，相加重建最终P(Y4)。基础调色降低不透明度时露出原片，但上方差分仍叠加。

颗粒成为最后一个效果层，降低它的不透明度趋向P(Y3)，即已使用同样全局调整和strength的无颗粒条件阶段；strength=0时各P(Yi)均为原片，四个物理效果差分为0，避免顶层最终混合重新抵消颗粒调节。“全局冷暖与力度”层只呈现基础阶段P(Y0)−Y0，调它不等于重新设置整图冷暖或strength，真正的全局参数修改仍须recipe重算。

差分按 `S=(Δ/a+1)/2` 编码为16位Linear Light层，默认不透明度a由差值范围及底图黑白端点余量决定，再向上量化到128–255／255，避免先裁切后混合造成默认重建损失。有余量时可从默认值向100%增强，饱和变化可能没有余量；复制层是创作叠加，不等价于物理参数重新计算。实际默认值和编码增益写入layers.json。

默认max_edge=0保留转正后原尺寸，也可指定按比例的width／height，显式stretch才改变长宽比；不设统一总像素输出上限。自动模式在任一边超过30000或预计文档超过2GB时生成真实PSB v2，包含对应64位长度字段；PSB单边最多300000。磁盘映射中间帧和分块IO降低了内存占用，但实际可运行尺寸仍受磁盘、内存／显存、引擎及编码器约束，不能承诺无限尺寸。archive=false可省略重复ZIP打包。

默认复合与参考成片由独立验证器从实际图层重算比较，不将内嵌merged预览当作图层验证：小图用开源psd-tools，大图用独立raw16 PSD／PSB解析与分块Normal／Linear Light合成器，validation.json披露实际方法。后者针对本工具的层结构，不表示已支持任意PSD功能，也不是psd-tools全图重合成或Adobe实机认证。

它属于固定成片分层表示，需要按原模型改变参数时由Agent通过recipe重新生成。完整合同见[PSD工作流](psd-workflow.md)，RGB ICC库配置见[接入说明](integration.md)。
