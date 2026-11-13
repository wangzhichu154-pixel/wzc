# Mac 厚门研究版

下载与你电脑芯片一致的构建：Apple Silicon 使用 arm64；Intel 使用 x86_64。
打开 DMG，将 Vector 拖到 Applications；也可解压 ZIP 中的 Vector.app。
应用已包含 Python 和 Qt，无需安装 Python 即可打开本次原生入口。

初始页面可以离线打开，不需要 Docker。点击“打开训练报告”，选择之前厚门交付包的
`trading-system/artifacts/houmen/training/training-report.json`，同目录需保留 `model.json`。
读取和模型身份校验在本机完成，不上传文件。公开安装包不内置私人策略文档或模型权重。

“检查云端连接”使用 Alpaca Paper Key/Secret，只请求账户和时钟，不下单、不撤单。
凭据只在内存中用于当次请求，完成/关闭后清除输入，不保存到文件。
账户连接成功不等于行情订阅、真实交易或 USD/CNY 数据接入验证通过。

“打开原研究工作区”保留原本的本机服务登录入口，该工作区仍需要对应 Docker 后台。

当前厚门模型未通过研究检查，自动执行未在桌面中启用。后台执行代码与完整训练报告
见之前源码交付包。本安装包不提供通过按钮绕过模型审批或风控的入口。

这是未完成 Developer ID 签名与 Apple 公证的开发预览包。请遵循 macOS 安全提示，
不需要关闭 Gatekeeper 或其他系统安全策略。云端构建/启动检查不能替代你电脑上的安装验收。
