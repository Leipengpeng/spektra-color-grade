# 许可证、来源与致谢

本仓库的 Skill、Python 调度、分层交付与说明以 **GPL-3.0-only** 发布，完整许可证见 [LICENSE](LICENSE)。Copyright (c) 2026 Leipengpeng and contributors。使用、修改和再分发应保留本许可证及适用的来源声明。上游组件仍按其自己的授权声明使用，不把上游未明确授予的未来许可证版本选项扩展给本仓库用户。

这是一个独立的社区封装：对话、偏好编译、五阶段调用、16位差分PSD／PSB、配方续改及交付校验属于本仓库的工作流。胶片物理模拟由用户另行安装的上游引擎执行。它不是 SpektraLab 官方插件，也不是 Adobe 官方产品。

## 上游胶片引擎

- [Andrea Volpato / spektrafilm](https://github.com/andreavolpato/spektrafilm)：Python 参考实现；本封装开发和测试使用 0.3.4、提交 `3bb2c2d2801ff68b92019cf1dbcbb133d60832bc`。该固定版本声明 GPLv3，未进一步明确声明 or-later，不能据标准许可证文本中的示例推定其接受未来许可证版本。Python 适配器使用其公开模块及材料数据接口；对应实现及数据版权由上游保留。
- [JamesQiu2005 / SpektraLab](https://github.com/JamesQiu2005/SpektraLab)：源自 spektrafilm 的原生引擎与桌面应用。代码及材料／profile 数据按上游各自许可证发布；其仓库为代码声明 GPL-3.0-or-later，为部分 profile 资源声明 CC BY-SA 4.0。Windows adapter 调用用户另行安装的 `spektralab-host.exe` 与 `host/engine`，不在本仓库重新分发可执行文件或 profile 数据。

上游的程序、材料数据、第三方库和商标不因本仓库使用 GPL 而改用另一许可证。下载、复制或打包这些组件时，请保留对应发行包中的 LICENSE、NOTICE、数据来源及再分发条件。本仓库不重新授权这些内容。

材料的作者说明和完整许可见固定版本的 [SPEKTRAFILM_LICENSE.txt](https://github.com/andreavolpato/spektrafilm/blob/3bb2c2d2801ff68b92019cf1dbcbb133d60832bc/SPEKTRAFILM_LICENSE.txt)。

## 其他依赖

- [LittleCMS 2](https://github.com/mm2/Little-CMS)：本仓库的 `icc_float.py` 是对公开 C API 的独立 ctypes 绑定，动态调用本机安装的库；本仓库不包含其 DLL。LittleCMS 2 按上游 MIT 许可证发布。
- [psd-tools](https://github.com/psd-tools/psd-tools)：独立解析并重合成小图层文件；本仓库通过 Python 依赖调用，不复制其实现。
- Pillow、NumPy、tifffile、OpenImageIO 以及 spektrafilm 的科学计算依赖，按各自项目许可证安装。安装列表见 [scripts/requirements.txt](scripts/requirements.txt) 及[环境说明](references/integration.md)。

## 不随代码发布的内容

本地研发曾使用 DPReview 的公开相机样片、用户环境中的 Photoshop／GIMP，以及实际豆包界面录屏。这些摄影作品、PSD成片、应用界面、录屏、用户路径、机器配置和账号信息均不随本开源仓库或 Skill ZIP 分发。算法技术说明中的小段公式与接口约定用于解释本实现，不构成对第三方软件或材料数据的重新授权。

胶片名称、Adobe、Photoshop、Lightroom、豆包等名称用于说明兼容流程和材料选择；相关商标归各自权利人。本仓库未附带第三方背书或兼容性认证。
