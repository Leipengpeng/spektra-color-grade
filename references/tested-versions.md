# 测试组合与边界

本表记录研发测试的范围，不表示任何同名上游下载附件始终保持相同内容，也不承诺不同引擎逐像素一致。公开发布日期为2026-11-19。

|项目|已知组合与状态|
|---|---|
|本Skill|1.0.0；胶片调度与PSD算法来自已验收版本，本次新增公开文档、故障暂停规则和打包检查|
|Python环境|Windows x64，CPython 3.13.9|
|Python胶片后端|spektrafilm 0.3.4，固定提交`3bb2c2d2801ff68b92019cf1dbcbb133d60832bc`，core-only安装；不依赖其Qt/napari GUI|
|旧Windows原生后端|本地已验收host对应SpektraLab 1.3.4、代码快照`a46627d250545a394ca21522f8a28b0fb89e4c42`。历史附件后来更新过；新下载的同名文件不应被当成旧二进制|
|官方Windows beta.3 portable|包SHA256与官方公布值相同；确认包含host及资源，hello应答protocol=1，但backend.available=false（本机系统commit不足）。实际五阶段试运行被相同预算保护拦截，未完成本组合的PSD渲染验收。安装后仍需在自己的机器做小图验证|
|PSD解析校验|psd-tools 1.26.1；大图走本仓库独立raw16解析/重合成，读取实际validation.json中的validator|
|浮点ICC|LittleCMS2动态库，Windows研发使用2.19；使用本机库且不随代码包分发|
|开放编辑器|GIMP3.2.8小图16位8层PSB读写及颗粒不透明度响应已测；不能从小图外推所有大图|
|Photoshop实机|Photoshop2025打开一份1799×2699 RGB16、8层PSD，演示原片/颗粒/光晕并另存；PS保存的合成对参考TIFF最大通道差10/65535，约0.03891个8位色阶，非bit exact|
|大图|已有6048×4032相机JPEG输入保留尺寸生成16位8层PSD并流式验收；不承诺任意尺寸均可在任意机器完成|
|macOS/Linux|有Python后端接入方式；本仓库未完成这两个系统的完整PSD实机验收，Windows原生adapter不适用于这两个系统|

## 研发环境中的关键依赖版本

Pillow12.3.0、NumPy2.5.4、tifffile2026.10.15、OpenImageIO3.1.19.0、psd-tools1.26.1、colour-science0.4.7、SciPy1.18.1、Numba0.68.1、scikit-image0.26.0、matplotlib3.11.2、opt-einsum3.4.0、pyFFTW0.15.1、rawpy0.26.1、exiv2 0.18.1、lensfunpy1.18.0。

这是实测版本记录，不是跨系统锁文件。安装文件使用上游版本范围；不同平台若无兼容wheel或依赖解析失败，请按[故障排查](troubleshooting.md)把具体包名和错误交给用户处理，不要假装安装成功或静默跳过所需库。

## 网络与数据

安装依赖或下载引擎需要用户网络；渲染步骤本身使用本机文件，不需要把照片发给VLM、生图服务或本仓库作者。公开包不附带DPReview样片、原始PSD、录屏或本地运行时配置。以上历史测试结论以对应引擎、文件和混合设置为范围。
