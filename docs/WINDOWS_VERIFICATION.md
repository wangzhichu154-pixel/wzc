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

## Windows CI 要求

`native-desktop-windows` 在 Windows runner 使用 x64 Python 3.12 执行桌面测试和 PyInstaller 构建。
实际交付 ZIP 解压到包含中文、空格的临时目录，再通过 Python 等待窗口进程完成 `--smoke-test`。
源码 `.cmd` 启动器也在独立中文/空格目录创建虚拟环境、安装依赖并运行窗口，错误必须传播为非零退出码。
CI 成功后公开其运行编号和摘要。没有 Windows CI 结果时不能声称 Windows 包已验证。

## 边界

- Qt 窗口检查使用 offscreen；用户 Windows 11 桌面上的安装与交互未验证。
- CI runner 为 Windows Server，不代表所有 Windows 版本均受支持。建议 Windows 11 x64；未构建 Windows ARM64 原生包。
- Windows 代码签名未配置，开发预览包可能提示发布者未知。
- Windows 上的完整 Docker/WSL2 后端部署未在此次 CI 中执行。客户端仍需要可访问的本机数据服务。
- 数据在服务端；新电脑不会自动获得原 Mac 上的账户或数据库。实盘仍未启用。
