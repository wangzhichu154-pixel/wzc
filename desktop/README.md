# Vector 桌面客户端

原生 PySide6 / Qt Widgets 桌面窗口，白色和浅灰配色、深色文字、蓝色强调。
没有 Chromium、WebView、浏览器标签或网页嵌入；窗口通过现有带会话、CSRF 和权限控制的 JSON API 访问本机服务。

## 在已经运行服务的 Mac 上使用

保持原项目的 Docker 服务运行，保留 `.env`、`.secrets` 和 Docker 数据卷。
原来的管理员邮箱和密码继续使用；客户端不会保存密码或持久化 Cookie。

1. 从 GitHub `vector/desktop-rc5` 分支下载源码 ZIP。
2. 解压，进入 `desktop` 文件夹，双击 `Start-Vector.command`。
3. 首次启动会创建独立 Python 虚拟环境并安装 Qt；需要联网和 Python 3.10–3.14。
4. 在桌面登录窗口输入原有账户。默认连接 `http://127.0.0.1:8080`。

如果 Finder 提示脚本无执行权限，在新解压目录执行一次：

```sh
chmod +x desktop/Start-Vector.command
open desktop/Start-Vector.command
```

也可以手动运行：

```sh
python3 -m venv desktop/.venv
desktop/.venv/bin/python -m pip install -r desktop/requirements.txt
desktop/.venv/bin/python desktop/run.py
```

源代码启动会保留终端用于错误提示；打包后的 `.app` 直接打开独立窗口。
关闭窗口不会停止后台计算或已有自动仿真任务，停止任务请使用应用内的停止/取消操作。

## 更新现有 rc.4 服务

桌面内的总览和风险编辑需要新增的 API。使用旧服务时这些页面会提示错误，不能假装保存成功。
先通过原项目备份流程备份现有数据，再将新源码的 **backend 文件夹**更新到原项目同名位置。
保留原项目的 `.env`、`.secrets`、Dockerfile 本机调整和 Compose 项目名，不覆盖数据库卷。
在**原项目目录**执行：

```sh
docker compose up -d --build
docker compose ps
```

本次没有数据库 migration。桌面客户端和业务服务分开运行，Docker 仍负责 PostgreSQL、Redis、Worker 和 C++ 计算。
这不是已经把数据库和券商运行时打进一个无需服务的离线安装包。

## Mac 应用包

仓库 Actions 的 `native-desktop` 工作流分别在 Mac runner 构建，并以实际 `uname -m` 标注 `arm64` 或 `x86_64`。
只有对应作业成功后，才能下载其 Artifacts 中的 `Vector-macOS-*`。
解压外层 artifact 和其中的 ZIP，将 `Vector.app` 移入“应用程序”后打开；应用包不需要用户安装 Python。
GitHub Actions artifact 下载需要登录 GitHub，默认保留 30 天。

构建包为开发预览，未配置 Apple Developer ID 签名/公证。macOS 可能阻止首次打开，需按系统的应用安全提示处理；本项目不提供关闭系统安全保护的命令。
Linux 上的构建结果不能当作 Mac 构建。实际构建和测试结果见 `docs/DESKTOP_VERIFICATION.md`。

开发者在目标 Mac 上打包：

```sh
desktop/.venv/bin/python -m pip install -r desktop/requirements-build.txt
desktop/.venv/bin/python desktop/build_app.py
```

## 范围与安全

- 账户登录/退出、数据导入、回放与历史仿真、任务查询/取消/重试、自动仿真、研究风险参数及运营状态按服务器权限执行；任务详情展示服务器实际返回的模拟结果。
- 网络调用在后台工作线程中执行；提交请求不自动重试。超时需要先查询任务，避免重复下单式的盲目重试。
- 默认只连接本机 HTTP 服务；拒绝远程地址、重定向和环境代理，不发送账户密码到外部主机。
- 这不是实盘授权工具。合成模型和旧版本发布门禁的限制继续有效，不会因为换了桌面界面而放开实盘。
- 平台管理、完整注册/验证/密码恢复、隐私导出和独立收益评估创建页尚未全部迁移为原生窗口，原管理界面保留用于运维；核心行情研究工作流不需要打开浏览器。
- PySide6 Essentials / Qt 采用 LGPLv3/GPLv3 或商业许可。发行时保留 Qt/PySide 的许可、动态链接组件和对应源码获取信息，见第三方许可说明。

## 验证

```sh
QT_QPA_PLATFORM=offscreen PYTHONPATH=desktop desktop/.venv/bin/python -m unittest discover -s desktop/tests -v
.venv/bin/python desktop/integration_check.py
```

GUI 单元测试的替身只用于测试，不随应用提供演示数据。集成检查使用临时数据库、明确标记的合成 ITCH 输入、真实服务 API 和真实 C++ 回放。
