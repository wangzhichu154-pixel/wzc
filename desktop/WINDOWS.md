# Vector Windows 桌面版

Windows 版沿用原生 Qt 窗口和浅色主题，包含账户登录、行情导入、回放/仿真、任务管理、自动仿真、研究风控及运营状态。界面没有嵌入浏览器。

当前提供 **Windows x64 开发预览包**，建议 Windows 11 x64。Windows ARM64 原生包、代码签名和用户电脑上的安装验收尚未完成。已执行的 Windows runner 测试和构建以仓库验证记录及 Actions 结果为准。

## 下载并打开应用

1. 登录 GitHub，打开本仓库 [Actions](https://github.com/wangzhichu154-pixel/wzc/actions)。
2. 选择包含 Windows 作业且成功完成的桌面构建，下载 Artifacts 中的 `Vector-Windows-x64`。
3. 解压下载的 artifact；如果里面还有 `Vector-Windows-x64.zip`，继续解压该文件。
4. 保留整个 `Vector-Windows-x64` 文件夹，双击其中的 **`Vector.exe`**。可为该文件创建桌面快捷方式。

应用包已经包含 Python 与 Qt 运行时，**打开打包应用无需另装 Python**。不要只拷贝 `Vector.exe`，它必须与同目录的 `_internal` 和其他随包文件一起保存。SHA-256 文件用于校验相应内层 ZIP；不要在 ZIP 预览中直接运行程序。

这是未签名的开发预览包，Windows 可能显示发布者未知或 SmartScreen 提示。请核对仓库来源及文件摘要，遵循系统的应用安全提示；本项目不要求关闭 Defender、SmartScreen 或系统安全策略。

## 连接本机数据服务

默认地址为 `http://127.0.0.1:8080`。先让对应数据服务运行，再用该服务中已有的邮箱和密码登录。客户端不持久化账户密码或 Cookie，不提供实盘授权。

如果 Windows 上还没有部署数据服务：

1. 安装并启动 Docker Desktop，按其官方指引启用 **WSL 2 后端和 Linux containers**。
2. 下载当前桌面分支的完整源码，在含 `docker-compose.yml` 的项目根目录操作。
3. 首次初始化配置需使用 Python 3.12；已有 `.env`、`.secrets` 或数据卷时保留原配置，跳过初始化。

在 PowerShell 或命令提示符中逐条执行（第一行仅首次初始化）：

```powershell
py -3.12 scripts\init_env.py
docker compose up -d --build
docker compose ps
```

首次部署并且尚无账户时，在服务正常后创建管理员：

```powershell
docker compose exec backend python manage.py createsuperuser
```

新服务使用独立数据卷，不会自动得到另一台 Mac 上的账户或研究数据。数据库备份/恢复遵循仓库部署手册；不要把不同电脑上的环境文件、密码和旧数据库卷任意混用。

如果数据服务已在当前电脑部署，直接沿用即可。升级旧 rc.4 服务时，参考 [桌面升级说明](README.md#更新现有-rc4-服务)，保留原有配置、Compose 项目名及数据卷。原生总览和风险编辑需要新版服务接口。

## 继续使用另一台电脑上的服务

Windows 的 `127.0.0.1` 指向 Windows 本机，不指向原来的 Mac。客户端不接受直接填写远程 HTTP 地址。
有相应主机访问权限时，可由运维配置 SSH 本机端口转发，例如：

```powershell
ssh -N -L 127.0.0.1:8080:127.0.0.1:8080 your-user@your-server
```

将示例用户和主机替换为实际值，保持 SSH 连接运行，并确认 Windows 的 8080 端口未被另一套服务占用。连接的是远端同一套服务时，原有账户与数据才会继续可见。不要将数据库、Redis 或未配置 HTTPS 的应用端口直接开放到公网。

## 从源码启动

有 64 位 Python 3.10–3.14 时，可以在解压源码的 `desktop` 文件夹双击 **`Start-Vector.cmd`**。脚本优先选择 Python 3.12，再检查默认 Python；它会建立独立的 `desktop\.venv` 并安装已固定版本的依赖。

- 首次安装依赖需要联网。
- 路径包含空格时可正常启动；脚本始终以自身目录为工作目录。
- 脚本不创建、覆盖或删除后端的 `.env`、`.secrets` 或数据库卷。
- 从 Mac/Linux 拷贝来的 `.venv` 不能用于 Windows；脚本会提示先将不兼容目录另行保留，不会自动删除。
- 启动失败时窗口保留错误供排查。自动化 CI 环境不暂停等待按键。
- 关闭桌面窗口不会停止服务器上的任务；停止自动仿真应在应用中执行。

开发者在 Windows 本机打包：

```powershell
py -3.12 -m venv desktop\.venv
desktop\.venv\Scripts\python.exe -m pip install -r desktop\requirements-build.txt
desktop\.venv\Scripts\python.exe desktop\build_app.py
```

不要用 Mac 或 Linux 的打包结果冒充 Windows 可执行文件。本版本仍是研究与仿真客户端，真实资金交易没有因为增加 Windows 界面而启用。
