# 从零安装与宿主接入

先准备本机运行环境，再把 Skill 导入豆包或其他宿主，最后用一张照片验证。每一步都有成功标志；网络、下载、证书和系统权限问题按[故障处理](troubleshooting.md)交给用户修复，不要无限重试。

## 先选择一条后端路线

|路线|需要什么|已知状态|
|---|---|---|
|Python：固定版本 spektrafilm|Python 3.13、固定源码、核心数值依赖、LCMS2|提供与既有测试对应的源码提交，适合从零按固定组合准备；不安装上游GUI|
|Windows 原生：SpektraLab host|Python 3.13、Skill依赖、官方Windows host与资源、LCMS2|适配Windows；当前官方beta.3目录及协议启动已核对，完整渲染需首次小图自测|

可以只配置一条路线。`auto` 优先 SpektraLab，整套阶段失败时最多回退一次 Python，并记录实际引擎；用户只选某后端时传 `--engine spektrafilm` 或 `--engine spektralab`。两个后端不保证逐像素相同。版本、依赖与测试状态见[已测试组合](tested-versions.md)。

Skill ZIP 不带引擎程序、材料数据、Python环境或PS。只有聊天能力的模型也不能直接执行它：宿主必须能在**同一台执行机器**读取照片、执行Python。云沙箱不会自动获得个人电脑硬盘的访问权。

## 第 1 步：下载并解压 Skill

1. 从[本仓库 Releases](https://github.com/Leipengpeng/spektra-color-grade/releases/latest)下载 `spektra-color-grade.zip`。
2. 解压后，找到真正包含 `SKILL.md` 的 `spektra-color-grade` 文件夹。
3. 放到有写权限的目录，例如 `C:/Tools/spektra-color-grade` 或自己的用户文件夹。不要放进需要管理员权限的系统目录。
4. 运行环境放到另一个文件夹，例如 `C:/SpektraRuntime` 或 `~/SpektraRuntime`；不要重新打进分享ZIP。

也可用Git下载源码：

```powershell
git clone https://github.com/Leipengpeng/spektra-color-grade.git
```

GitHub“Code → Download ZIP”是源码包。豆包导入优先用Releases中按Skill结构打好的ZIP。下载源码不等于完成引擎安装。

**成功标志：** 文件夹内有 `SKILL.md`、`scripts/spektra.py`、`scripts/requirements.txt`、`references/`。如果多套了一层文件夹，后面的 `$Skill` 必须指向包含 `SKILL.md` 的那一层。

## 第 2 步：安装 Python 3.13

Windows使用普通64位Python 3.13。用户从[Python官方下载页](https://www.python.org/downloads/)选择相应安装程序，保留pip和Python Launcher；安装说明见[Python 3.13 Windows文档](https://docs.python.org/3.13/using/windows.html)。安装结束后关闭旧终端，重新打开PowerShell：

```powershell
py -3.13 --version
py -3.13 -c "import struct; print(struct.calcsize('P') * 8)"
```

**成功标志：** 分别输出 `Python 3.13.x` 和 `64`。找不到 `py`、打开商店或版本错误时，先由用户解决Python安装／命令路径，不要继续安装依赖。其他Python版本或free-threaded版本不属于相同的已验证组合。

## 第 3 步：设置路径，创建隔离环境

把第一行替换成真实Skill路径。下面把环境放在当前用户自己的文件夹：

```powershell
$Skill = "C:/Tools/spektra-color-grade"
$Runtime = Join-Path $env:USERPROFILE "SpektraRuntime"
Test-Path "$Skill/SKILL.md"
```

**成功标志：** 最后一行输出 `True`，否则先修正 `$Skill`。

```powershell
New-Item -ItemType Directory -Force -Path $Runtime | Out-Null
py -3.13 -m venv "$Runtime/.venv-film"
$Py = "$Runtime/.venv-film/Scripts/python.exe"
& $Py --version
& $Py -m pip --version
```

**成功标志：** Python仍是3.13，pip显示的位置在新建的 `.venv-film` 内。这里直接调用 `$Py`，不需要执行 `Activate.ps1`，也无需为教程修改PowerShell执行策略。新开终端只需重新设置 `$Skill`、`$Runtime`、`$Py`，不要重复删除或重建已有环境。

## 第 4P 步：准备固定版本 Python 后端

需要用户先从[Git官方下载页](https://git-scm.com/downloads)安装对应系统的Git，新开终端检查 `git --version`。成功后再执行：

```powershell
New-Item -ItemType Directory -Force -Path "$Runtime/repos" | Out-Null
git clone https://github.com/andreavolpato/spektrafilm.git "$Runtime/repos/spektrafilm"
git -C "$Runtime/repos/spektrafilm" checkout 3bb2c2d2801ff68b92019cf1dbcbb133d60832bc
git -C "$Runtime/repos/spektrafilm" rev-parse HEAD
```

**成功标志：** 最后一行是 `3bb2c2d2801ff68b92019cf1dbcbb133d60832bc`，对应已验证的0.3.4源码。固定提交产生detached HEAD提示是正常Git状态。目录已存在时先核对现有内容和提交，不要自动删除重下。

本封装只使用数值核心。先装完整核心依赖和Skill依赖，再用 `--no-deps` 注册源码，避免拉入上游Qt／PySide6／napari界面：

```powershell
& $Py -m pip install --retries 0 --timeout 30 -r "$Skill/scripts/requirements-python.txt"
& $Py -m pip install --retries 0 --timeout 30 --no-deps -e "$Runtime/repos/spektrafilm"
& $Py -c "import spektrafilm; print(spektrafilm.__file__)"
```

**成功标志：** 命令正常结束，能导入 `spektrafilm`，输出位置在 `$Runtime/repos/spektrafilm/src/`。这不代表上游GUI已安装，也不表示可以直接启动其桌面入口。

`requirements-python.txt`包含本Skill的 `requirements.txt`。如果某个平台无匹配wheel或依赖冲突，报告实际包名、Python版本、系统／架构和错误，交给用户处理，不能声称所有平台都已验证。不要删掉导入链实际需要的包来凑检查通过。

完成后去第5步准备LCMS2。

## 第 4W 步：准备 Windows 原生 SpektraLab 后端

这一节与第4P步二选一；两套都要用时再分别安装。

先安装Skill依赖：

```powershell
& $Py -m pip install --retries 0 --timeout 30 -r "$Skill/scripts/requirements.txt"
& $Py -c "import PIL, numpy, tifffile, OpenImageIO, psd_tools; print('dependencies ok')"
```

**成功标志：** 最后一行输出 `dependencies ok`。这份清单不包含Python胶片引擎，也不安装PS。

官方可下载的Windows x64便携包选项：

- 页面：[SpektraLab Windows beta.3](https://github.com/JamesQiu2005/SpektraLab/releases/tag/spektralab-windows-v1.3.4-beta.3)
- 附件：[SpektraLab-1.3.4-beta.3-windows-x64-portable.zip](https://github.com/JamesQiu2005/SpektraLab/releases/download/spektralab-windows-v1.3.4-beta.3/SpektraLab-1.3.4-beta.3-windows-x64-portable.zip)
- 此次核对SHA256：`4945c80d552565382c2114f70ce82c137dd4502be9ddb832f79e20970fac7357`

这个包的布局和host协议启动已核对。当前测试机器因系统commit预算不足，完整小图渲染被资源检查阻止；不能写成beta.3已完整图像验收，也不能据此断言它不兼容。请完成下面步骤并做首次小图验证。它与历史演示中的旧包不同，不能承诺逐像素复现旧样片。详见[测试状态](tested-versions.md)。

1. 用户从上面的官方附件下载ZIP，不要选Mac DMG或仅有GUI的包。
2. 在PowerShell检查文件SHA256。例如文件在下载文件夹：

```powershell
$LabZip = Join-Path $env:USERPROFILE "Downloads/SpektraLab-1.3.4-beta.3-windows-x64-portable.zip"
(Get-FileHash -LiteralPath $LabZip -Algorithm SHA256).Hash
```

3. 输出应与上面的哈希一致，忽略大小写。若不一致，停下让用户确认是否下载损坏或上游资产已更换，不要继续解包并伪称同版。
4. 解压整个包，不只复制exe。示例先检查目标不存在，再执行：

```powershell
Test-Path "$Runtime/apps/SpektraLab"
```

应为 `False`。若已经是 `True`，先核对已有版本，不要覆盖。然后：

```powershell
New-Item -ItemType Directory -Force -Path "$Runtime/apps" | Out-Null
Expand-Archive -LiteralPath $LabZip -DestinationPath "$Runtime/apps"
Rename-Item -LiteralPath "$Runtime/apps/SpektraLab-1.3.4-beta.3-windows-x64-portable" -NewName "SpektraLab"
Test-Path "$Runtime/apps/SpektraLab/spektralab-host.exe"
Test-Path "$Runtime/apps/SpektraLab/host/engine"
```

**成功标志：** 最后两行都是 `True`，目录中保留官方包的其他必要DLL／文件。`spektralab.exe`不能代替 `spektralab-host.exe`，不要重命名GUI伪装host。

新版本布局可能变化。只有exe存在不证明GPU可运行；后面 `doctor` 通过后仍需显式 `--engine spektralab` 小图测试。缺host或资源时，让用户确认官方包／版本，或明确选择Python路线，不自动切到不明下载源。

## 第 5 步：准备 LittleCMS2 色彩管理库

RGB ICC输入使用浮点色彩管理，需要可被当前Python进程加载的LCMS2动态库。Pillow有 `ImageCms` 不代表本工具已经能独立加载该库。

Windows可先检查正规的[GIMP](https://www.gimp.org/downloads/)或[Krita](https://krita.org/en/download/)安装。工具会搜索常见安装路径及运行环境内的已知目录。GIMP／Krita在此可以只作为LCMS库来源，不需要每张照片启动它们。

其他位置由用户提供真实库路径。当前PowerShell临时测试示例：

```powershell
$env:SPEKTRA_LCMS2 = "C:/Program Files/GIMP 3/bin/liblcms2-2.dll"
Test-Path $env:SPEKTRA_LCMS2
```

路径只是示例，应替换成真正存在、架构匹配的文件，不能重命名其他DLL。如果要让豆包下次启动也识别，由用户设置同名“用户环境变量”并重启豆包／终端；临时PowerShell变量不会传到已运行的宿主。

**成功标志：** 下一步的 `float_icc_ready=true`，其中实际做过浮点ICC转换。不能以“找到了同名文件”替代加载验证，也不能通过剥ICC／先降8位掩盖失败。

## 第 6 步：记录配置，运行 doctor

```powershell
& $Py "$Skill/scripts/spektra.py" configure --root $Runtime
& $Py "$Skill/scripts/spektra.py" doctor
```

`configure`在这个Skill副本里生成 `runtime.local.json`，它含本机路径，不应提交GitHub或打进分享ZIP。

|JSON字段|成功标志|
|---|---|
|`runtime.root`／`python`|是刚才配置的实际目录和解释器|
|`backends.spektrafilm`|选择Python路线时为 `true`|
|`backends.spektralab`|选择Windows原生路线时为 `true`|
|`psd_ready`|为 `true`|
|`float_icc_ready`|为 `true`；本Skill首次使用先备齐浮点ICC能力|
|`psd_icc_ready`|为 `true`，表示PSD依赖和浮点ICC同时可用|

只装一条后端时，另一条为 `false` 不一定是问题。`status=ready`也不意味着所有可选项都就绪，仍需看这些字段。doctor检查文件、模块导入和真实浮点ICC转换，**不代替GPU／胶片渲染检查**。

## 第 7 步：菜单与首次小图验证

```powershell
& $Py "$Skill/scripts/spektra.py" menu
```

应返回 `tutorial_markdown` 和胶片菜单。Agent需要显示教程文字，不只给一个JSON文件位置。

选一张自己有权处理的8位SDR RGB JPEG，保存到执行机器，例如 `C:/photos/test.jpg`。首次验证可以**明确选择**长边256的小图减少等待；这只属于本次测试，正式处理不加尺寸参数时默认原尺寸。

Python路线：

```powershell
& $Py "$Skill/scripts/spektra.py" psd --input "C:/photos/test.jpg" --effect 1 --engine spektrafilm --max-edge 256 --no-archive --output "C:/photos/spektra-test01"
```

Windows原生路线：

```powershell
& $Py "$Skill/scripts/spektra.py" psd --input "C:/photos/test.jpg" --effect 1 --engine spektralab --max-edge 256 --no-archive --output "C:/photos/spektra-test01"
```

目录必须是新的；已有 `spektra-test01` 就换 `spektra-test02`，不要删旧成果。

**成功标志：** `status=ok`、`validation.status=ok`，实际引擎／尺寸／16位／8层与请求相符，返回文件均存在。小图只证明这一组合的一次运行，不保证所有尺寸和编辑器都兼容。资源／驱动或验证失败时停下，让用户处理后再试。

正式原尺寸示例，将引擎名换成你选定的后端：

```powershell
& $Py "$Skill/scripts/spektra.py" psd --input "C:/photos/portrait.jpg" --effect 1 --engine spektrafilm --strength 0.8 --grain 0.2 --halation 0.25 --no-archive --output "C:/photos/result01"
```

## 第 8 步：导入豆包或其他宿主

1. 上传Releases的Skill ZIP，在技能列表启用，从该技能发起工作对话。
2. 选择能读本机文件、执行Python的本地工作模式。只有云沙箱或纯聊天时，让用户先提供执行能力。
3. Agent定位本次真正加载的 `SKILL.md` 目录，不猜作者电脑路径。
4. 给这个副本配置实际运行环境，执行menu与doctor，再接受照片和胶片选择。

### 配置安装后的 Skill 副本

下载目录里配置过，不代表豆包内部复制品已有配置。可以直接对Agent说：

> 我已在本机准备好 `C:/SpektraRuntime`。请找到本次实际加载的 Spektra Color Grade 技能目录，在该副本执行 `scripts/spektra.py configure --root "C:/SpektraRuntime"`，再运行menu和doctor。路径不存在或没有本地执行能力时直接告诉我，等待我解决，不要虚构成功。

把示例换成第3步的实际 `$Runtime`。Agent可用普通Python启动主入口，入口按配置切换解释器。配置解释器不存在时要修复，不能将“能找到系统Python”当作依赖齐全。

### Function Calling宿主

模型发出的工具调用不是已经完成的计算。宿主需在本机接收调用、执行脚本、把实际JSON回填给模型。注册[menu接口](menu_tool_schema.json)与[PSD接口](psd_tool_schema.json)，调用[本地桥接](../scripts/tool_bridge.py)。

```python
import json
from pathlib import Path
import subprocess

def execute_spektra(name, arguments, skill_directory, python_executable):
    if name not in {"spektra_menu", "spektra_psd"}:
        raise ValueError("Unsupported tool name")
    result = subprocess.run(
        [str(python_executable), str(Path(skill_directory) / "scripts" / "tool_bridge.py")],
        input=json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False),
        encoding="utf-8", capture_output=True,
    )
    if not result.stdout.strip():
        raise RuntimeError(result.stderr[-2000:] or "Tool returned no JSON")
    return json.loads(result.stdout)  # 检查实际status和validation，不伪造成片
```

首次请求 `{"name":"spektra_menu","arguments":{}}`。用户提供照片与选择后：

```json
{"name":"spektra_psd","arguments":{"input":"C:/photos/portrait.jpg","effect":"1","engine":"spektrafilm","adjustments":{"strength":0.8,"grain":0.2,"halation":0.25},"archive":false,"output":"C:/photos/result01"}}
```

路径必须在桥接执行机器上。模型不能凭JSON自动获得用户电脑权限。API工具定义、权限和结果回填也需按所选模型服务当前文档配置。

## 续改、重启与离线准备

普通续改只传recipe、新偏好与新目录，不重复胶片编号：

```powershell
& $Py "$Skill/scripts/spektra.py" psd --recipe "C:/photos/result01/recipe.json" --intent "再暖一点，颗粒弱一点" --no-archive --output "C:/photos/result02"
```

新开终端只恢复三个路径变量，不必重装。移动环境后再configure，重新doctor和小图验证；不要直接拷贝别人的venv当通用安装包。

离线电脑需要用户先准备适配同系统／CPU／Python版本的引擎源码、安装包及全部wheel，经自己信任的渠道转移。本仓库不提供完整离线依赖镜像。缺包或断网时，Agent列缺项和失败域名后暂停，等待用户提供材料。

## macOS／Linux对应形式

下列是对应调用方式，**不是全平台验收承诺**。先准备Python3.13、Git和系统所需依赖，选Python后端：

```bash
SKILL="$HOME/Tools/spektra-color-grade"
RUNTIME="$HOME/SpektraRuntime"
python3.13 -m venv "$RUNTIME/.venv-film"
PY="$RUNTIME/.venv-film/bin/python"
"$PY" -m pip install --retries 0 --timeout 30 -r "$SKILL/scripts/requirements-python.txt"
```

按第4P步把固定源码放到 `$RUNTIME/repos/spektrafilm`，执行 `--no-deps -e` 安装。Unix的LCMS2用系统共享库，或用户将 `SPEKTRA_LCMS2` 指向实际 `.so`／`.dylib`；不要套用Windows DLL。

```bash
"$PY" "$SKILL/scripts/spektra.py" configure --root "$RUNTIME"
"$PY" "$SKILL/scripts/spektra.py" doctor
"$PY" "$SKILL/scripts/spektra.py" psd --input "$HOME/photos/test.jpg" --effect 1 --engine spektrafilm --max-edge 256 --no-archive --output "$HOME/photos/spektra-test01"
```

没有适配wheel、动态库加载失败或缺少系统依赖时，交给用户选择环境／安装方案，再继续。不要把Windows的测试结论外推到其他系统。
