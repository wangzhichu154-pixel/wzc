# Windows 桌面版验证

Windows x64 扩展沿用 rc.5 的原生 Qt 界面、身份认证与研究权限。没有增加实盘权限，没有改动交易引擎或数据库 schema。

## 本地预检

Linux 中执行 36 项客户端、Qt 和打包逻辑测试：35 项通过、1 项可选截图跳过；Ruff 和 diff 检查通过。
这不是 Windows 原生运行结果。

覆盖新增风险：

- 中文和空格文件名上传，CR/LF、0x1A 和非 ASCII 二进制数据保持完整。
- 恶意 multipart 文件名过滤在各平台均执行，不创建 Windows 非法文件名。
- 只允许 PE x64 包；拒绝错误架构、缺失运行时目录或无效 PE 头。
- ZIP 保留完整 `_internal`、许可与运行时，逐文件 SHA-256 验证。
- `.gitattributes` 固定 Linux 后端脚本为 LF，Windows 启动入口为 CRLF。

## Windows CI 实测结果（2026-11-07）

- 源码提交：`6006463a3824d38da5949b0167c670faad6330a5`。
- [Windows 构建 42333599344](https://github.com/wangzhichu154-pixel/wzc/actions/runs/42333599344) 已成功，job `128285405848`；环境为 Windows Server 2025 AMD64、Python 3.12。
- 36 项测试中 35 项通过，1 项可选截图未指定目的目录而跳过；不是测试失败。
- PyInstaller 实际生成 PE x64 应用；交付 ZIP 解压后，204 个文件的 SHA-256 与清单一致。
- 包内 EXE 在中文、空格路径下，分别通过 Qt offscreen 和原生 windows 插件完成启动检查；包内包含 `qwindows.dll`。
- `Start-Vector.cmd` 在独立中文、空格路径创建 Windows 虚拟环境、安装依赖并实际启动窗口；未创建后端凭据。
- 同一提交的 [Mac 回归构建 42333599333](https://github.com/wangzhichu154-pixel/wzc/actions/runs/42333599333) 两个架构均成功。
- [下载 Windows x64](https://github.com/wangzhichu154-pixel/wzc/actions/runs/42333599344/artifacts/13558845377)；GitHub artifact 保留至 2026-12-07，可重新运行工作流生成。

内层 `Vector-Windows-x64.zip` 的 SHA-256：

```text
4f6294cd9090bc1f0f712df50aa7b38aaa149f6383f42e19499763c200821608
```

外层 Actions artifact 的摘要、大小、各项报告和构建关联见 [机器可读证据](evidence/desktop-rc5/windows-ci.json)。不要将内层 ZIP 的摘要用于外层下载包。

## 自动化验证流程

`native-desktop-windows` 在 Windows runner 使用 x64 Python 3.12 执行桌面测试和 PyInstaller 构建。
实际交付 ZIP 解压到包含中文、空格的临时目录，确认包含 `qwindows.dll`，再通过 Python 等待窗口进程分别以 offscreen 和原生 windows 平台插件完成 `--smoke-test`。
源码 `.cmd` 启动器也在独立中文/空格目录创建虚拟环境、安装依赖并运行窗口，错误必须传播为非零退出码。
工作流失败时不上传交付包；启动检查只验证窗口初始化和正常退出，不能替代连接真实数据服务后的用户操作验收。

## 边界

- Windows runner 检查 offscreen 与原生 windows 平台插件；用户 Windows 11 桌面上的安装与交互未验证。
- CI runner 为 Windows Server，不代表所有 Windows 版本均受支持。建议 Windows 11 x64；未构建 Windows ARM64 原生包。
- Windows 代码签名未配置，开发预览包可能提示发布者未知。
- Windows 上的完整 Docker/WSL2 后端部署未在此次 CI 中执行。客户端仍需要可访问的本机数据服务。
- 数据在服务端；新电脑不会自动获得原 Mac 上的账户或数据库。实盘仍未启用。
