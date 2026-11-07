"""Native Qt workstation. All service requests run in a single bounded worker.

The interface uses QWidget controls only; it does not embed a web browser.
Server-side authentication, permissions and research risk rules remain authoritative.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QThreadPool, Qt, QTimer, Slot
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog,
    QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QLineEdit, QListWidget, QMainWindow,
    QMessageBox, QSpinBox,
    QStackedWidget, QVBoxLayout,
    QWidget,
)


from .widgets import (
    CollectionPage, CreateRunDialog, JobDialog, JsonDialog, RequestTask, STATUS,
    button, label, text, question, warning, information,
)
from .widgets import apply_palette as apply_palette


class MainWindow(QMainWindow):
    """Authenticated native desktop application; one service request at a time."""

    def __init__(self, client, *, auto_check=True):
        super().__init__()
        self.client = client
        self.user = None
        self.busy = False
        self.closing = False
        self._task = None
        self._success = None
        self._failure = None
        self._success_message = ""
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.setWindowTitle("Vector · 量化研究工作台")
        self.resize(1240, 820)
        self.setMinimumSize(960, 680)
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self._build_login()
        self._build_workspace()
        self.statusBar().showMessage("尚未登录 · 实盘未启用")
        self._build_menus()
        self.timer = QTimer(self)
        self.timer.setInterval(15000)
        self.timer.timeout.connect(self._poll)
        if auto_check:
            QTimer.singleShot(0, self.check_health)

    def _build_menus(self):
        account = self.menuBar().addMenu("账户")
        self.logout_action = QAction("退出登录", self)
        self.logout_action.triggered.connect(self.logout)
        self.logout_action.setEnabled(False)
        account.addAction(self.logout_action)
        quit_action = QAction("退出应用", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        account.addAction(quit_action)
        view = self.menuBar().addMenu("视图")
        self.refresh_action = QAction("刷新", self)
        self.refresh_action.setShortcut(QKeySequence.StandardKey.Refresh)
        self.refresh_action.triggered.connect(self.refresh)
        view.addAction(self.refresh_action)
        help_menu = self.menuBar().addMenu("帮助")
        about = QAction("关于 Vector", self)
        about.triggered.connect(lambda: information(self, "关于 Vector", "Vector 桌面研究工作台\n原生 Qt 界面 · 历史行情研究与仿真\n当前版本不启用实盘交易。\n\n关闭窗口不会停止后台研究任务。"))
        help_menu.addAction(about)

    def _build_login(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addStretch()
        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(460)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(32, 30, 32, 30)
        layout.setSpacing(16)
        layout.addWidget(label("Vector", name="heading"))
        layout.addWidget(label("量化研究工作台", name="subtitle"))
        layout.addWidget(label("登录已有账户，继续使用你的行情数据与研究任务。"))
        self.email = QLineEdit()
        self.email.setObjectName("loginEmail")
        self.email.setPlaceholderText("邮箱")
        self.email.setMaxLength(254)
        self.password = QLineEdit()
        self.password.setObjectName("loginPassword")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("密码")
        self.password.setMaxLength(1024)
        self.password.returnPressed.connect(self.login)
        self.email.returnPressed.connect(self.password.setFocus)
        layout.addWidget(self.email)
        layout.addWidget(self.password)
        self.login_button = button("登录", self.login, primary=True)
        layout.addWidget(self.login_button)
        self.login_message = label("正在等待连接检查。", name="subtitle")
        layout.addWidget(self.login_message)
        self.check_button = button("检查服务连接", self.check_health)
        layout.addWidget(self.check_button)
        layout.addWidget(label("账户密码只用于本次登录，不保存在桌面应用中。", name="subtitle"))
        outer.addWidget(card, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch()
        self.stack.addWidget(page)
        self.login_page = page

    def _build_workspace(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        self.navigation.setFixedWidth(190)
        self.navigation.addItems(["研究概览", "行情数据", "研究任务", "自动仿真", "研究风控", "AI 模型", "执行状态", "登录设备"])
        self.pages = QStackedWidget()
        layout.addWidget(self.navigation)
        layout.addWidget(self.pages, 1)
        self._build_overview()
        self.datasets_page = self._collection("行情数据", "导入有权使用的 NASDAQ ITCH 逐笔文件。", [
            ("名称", lambda x: x.get("name")), ("消息数", lambda x: x.get("messages")),
            ("大小（MiB）", lambda x: f"{x['bytes'] / 1048576:.2f}"),
            ("导入时间", lambda x: x.get("created_at")),
        ])
        self.datasets_page.toolbar.addWidget(button("导入文件", self.upload, primary=True))
        self.datasets_page.toolbar.addWidget(button("新建研究任务", self.create_run))
        self.datasets_page.toolbar.addWidget(button("新建自动仿真", lambda: self.create_run(automation=True)))
        self.datasets_page.toolbar.addWidget(button("删除", self.delete_dataset))
        self.jobs_page = self._collection("研究任务", "行情回放与历史仿真 · 结果来自后台任务。", [
            ("任务编号", lambda x: x.get("id")), ("模式", lambda x: STATUS.get(x.get("mode"), x.get("mode"))),
            ("状态", lambda x: STATUS.get(x.get("status"), x.get("status"))), ("创建时间", lambda x: x.get("created_at")),
        ])
        self.jobs_page.toolbar.addWidget(button("查看结果", self.show_job))
        self.jobs_page.toolbar.addWidget(button("请求取消", self.cancel_job))
        self.jobs_page.toolbar.addWidget(button("重试失败任务", self.retry_job))
        self.jobs_page.table.doubleClicked.connect(self.show_job)
        self.automations_page = self._collection("自动仿真", "按计划重复研究历史行情；每次运行使用独立模拟账户。", [
            ("名称", lambda x: x.get("name")), ("状态", lambda x: STATUS.get(x.get("state"), x.get("state"))),
            ("已运行 / 上限", lambda x: f"{x.get('runs_created')} / {x.get('max_runs')}"),
            ("风险锁定", lambda x: x.get("risk_latched")), ("下次执行", lambda x: x.get("next_run_at")),
        ])
        self.automations_page.toolbar.addWidget(button("查看配置", self.show_automation))
        self.automations_page.toolbar.addWidget(button("启动仿真", lambda: self.control_automation("start")))
        self.automations_page.toolbar.addWidget(button("停止仿真", lambda: self.control_automation("stop")))
        self._build_risk()
        self.ai_page = self._collection("AI 模型", "只读查看已登记的训练与评估证据。未经验证的模型不会自动上线。", [
            ("版本", lambda x: x.get("version")), ("阶段", lambda x: x.get("stage_label")),
            ("数据来源", lambda x: x.get("provenance_label")), ("预测验证通过", lambda x: x.get("evaluation_passed")),
            ("仿真验证通过", lambda x: x.get("simulation_passed")),
        ])
        self.ai_page.toolbar.addWidget(button("查看评估证据", self.show_model))
        self.execution_page = QWidget()
        execution_layout = QVBoxLayout(self.execution_page)
        execution_layout.setContentsMargins(28, 24, 28, 20)
        execution_layout.addWidget(label("执行状态", name="heading"))
        execution_layout.addWidget(label("此处只读。桌面应用不提供实盘下单入口。", name="notice"))
        self.execution_status = label("尚未获取执行状态。", name="subtitle")
        execution_layout.addWidget(self.execution_status)
        self.execution_values = {}
        form = QFormLayout()
        for key, title in (("connected", "券商连接"), ("armed", "风险准入"), ("kill_latched", "风险锁定"),
                           ("inventory", "当前库存"), ("open_orders", "未终结订单"), ("age_seconds", "状态年龄（秒）")):
            value = label("未记录")
            self.execution_values[key] = value
            form.addRow(title, value)
        execution_layout.addLayout(form)
        execution_layout.addWidget(label("状态不可用时，不能据此判断券商上没有挂单或风险敞口。", name="subtitle"))
        execution_layout.addStretch()
        self.pages.addWidget(self.execution_page)
        self.devices_page = self._collection("登录设备", "查看账户会话，并撤销不再使用的设备。", [
            ("客户端", lambda x: x.get("user_agent")), ("当前设备", lambda x: x.get("is_current")),
            ("最近活动", lambda x: x.get("last_seen_at")), ("失效时间", lambda x: x.get("expires_at")),
            ("已撤销", lambda x: bool(x.get("revoked_at"))),
        ])
        self.devices_page.toolbar.addWidget(button("撤销会话", self.revoke_device))
        self.navigation.currentRowChanged.connect(self._navigate)
        self.stack.addWidget(page)
        self.workspace = page
        self.navigation.setCurrentRow(0)

    def _collection(self, title, subtitle, columns):
        page = CollectionPage(title, subtitle, columns)
        page.toolbar.addWidget(button("刷新", self.refresh))
        page.previous.clicked.connect(lambda: self._paginate(page, -1))
        page.next.clicked.connect(lambda: self._paginate(page, 1))
        self.pages.addWidget(page)
        return page

    def _build_overview(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(18)
        layout.addWidget(label("研究概览", name="heading"))
        self.identity = label("", name="subtitle")
        layout.addWidget(self.identity)
        layout.addWidget(label("研究与仿真环境 · 实盘未启用", name="notice"))
        row = QHBoxLayout()
        self.metrics = {}
        for key, title in (("dataset_count", "行情数据集"), ("job_count", "研究任务"), ("running_count", "正在运行"), ("completed_count", "已完成")):
            card = QFrame()
            card.setObjectName("card")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(22, 20, 22, 20)
            card_layout.addWidget(label(title, name="subtitle"))
            self.metrics[key] = label("—", name="heading")
            card_layout.addWidget(self.metrics[key])
            row.addWidget(card)
        layout.addLayout(row)
        self.overview_state = label("点击刷新获取最新研究状态。")
        layout.addWidget(self.overview_state)
        layout.addWidget(button("刷新概览", self.refresh))
        layout.addWidget(label("工作流程", name="heading"))
        layout.addWidget(label("1. 在「行情数据」导入 ITCH 文件。\n2. 检查「研究风控」中的数量、库存和损失限额。\n3. 创建回放或历史仿真，在「研究任务」查看结果。\n4. 按需要创建自动仿真；启动与停止都需要密码复核。"))
        layout.addStretch()
        self.pages.addWidget(page)

    def _build_risk(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.addWidget(label("研究风控", name="heading"))
        layout.addWidget(label("参数只影响后续新建的研究任务。已运行任务保留创建时的参数快照。", name="notice"))
        form = QFormLayout()
        self.risk_fields = {}
        for key, title, low, high in (
            ("max_order_size", "最大单笔数量", 1, 10000),
            ("tick_size", "最小价位（1/10000 USD）", 1, 100000),
            ("soft_inventory_limit", "库存软限额", 1, 100000),
            ("hard_inventory_limit", "库存硬限额", 2, 100000),
            ("orders_per_second", "每秒订单上限", 1, 50),
        ):
            field = QSpinBox()
            field.setRange(low, high)
            field.setKeyboardTracking(False)
            form.addRow(title, field)
            self.risk_fields[key] = field
        loss = QDoubleSpinBox()
        loss.setRange(1, 9999999999.99)
        loss.setDecimals(2)
        loss.setKeyboardTracking(False)
        form.addRow("日内最大亏损（USD）", loss)
        self.risk_fields["daily_loss_limit"] = loss
        layout.addLayout(form)
        self.risk_save = button("保存研究参数", self.save_risk, primary=True)
        layout.addWidget(self.risk_save)
        self.risk_message = label("尚未加载参数。", name="subtitle")
        layout.addWidget(self.risk_message)
        layout.addStretch()
        self.risk_loaded = False
        self.risk_save.setEnabled(False)
        self.pages.addWidget(page)

    def submit(self, function: Callable, success: Callable, *, failure=None, message=""):
        if self.busy or self.closing:
            return False
        self.busy = True
        self._success, self._failure, self._success_message = success, failure, message
        self.stack.setEnabled(False)
        self.refresh_action.setEnabled(False)
        self.logout_action.setEnabled(False)
        self.statusBar().showMessage("正在处理，请稍候…")
        self._task = RequestTask(function)
        self._task.signals.completed.connect(self._complete)
        self.pool.start(self._task)
        return True

    @Slot(object, object)
    def _complete(self, result, error):
        success, failure, message = self._success, self._failure, self._success_message
        self._success = self._failure = self._task = None
        self.busy = False
        self.stack.setEnabled(True)
        self.refresh_action.setEnabled(True)
        self.logout_action.setEnabled(self.user is not None)
        if self.closing:
            self.close()
            return
        try:
            if error is not None:
                if getattr(error, "status", None) == 401 and self.user is not None:
                    self._logged_out()
                    self.login_message.setText("登录已过期，请重新登录。")
                elif failure:
                    failure(error)
                else:
                    self._show_error(error)
                self.statusBar().showMessage("操作未完成 · 请查看提示")
            else:
                success(result)
                if not self.busy:
                    self.statusBar().showMessage(message or "已更新 · 实盘未启用")
        except Exception:
            self.statusBar().showMessage("返回数据无法显示，请重新刷新。")
            warning(self, "无法显示数据", "服务返回的数据无法显示。请刷新；若持续发生，请保留请求编号交给管理员。")

    def _show_error(self, error):
        message = str(error)[:2000]
        if getattr(error, "code", "") in {"QUEUE_UNAVAILABLE", "CONNECTION_FAILED", "NETWORK_ERROR", "TIMEOUT"}:
            message += "\n若请求刚提交，请先刷新任务列表确认状态，避免重复创建。"
        if isinstance(getattr(error, "data", None), dict) and error.data.get("id"):
            message += f"\n已保存任务编号：{error.data['id']}"
        if getattr(error, "idempotency_key", None):
            message += f"\n操作追踪编号：{error.idempotency_key}"
        request_id = getattr(error, "request_id", "")
        if request_id:
            message += f"\n请求编号：{request_id}"
        warning(self, "操作未完成", message)

    def check_health(self):
        def complete(data):
            self.login_message.setText("本机服务可用，请登录。" if data.get("ready") else "服务尚未就绪，请稍后重试。")
        self.submit(self.client.health, complete, failure=lambda exc: self.login_message.setText("无法连接本机服务。请先启动数据服务，再点击「检查服务连接」。"))

    def login(self):
        if self.busy:
            return
        email, password = self.email.text().strip(), self.password.text()
        if not email or not password:
            self.login_message.setText("请填写邮箱和密码。")
            return
        self.password.clear()
        self.submit(lambda: self.client.login(email, password), self._logged_in,
                    failure=lambda exc: self.login_message.setText(str(exc)[:500]))

    def _logged_in(self, user):
        self.user = user
        self.identity.setText(f"{user.get('email', '')} · 数据与任务按账户隔离")
        self.logout_action.setEnabled(True)
        self.stack.setCurrentWidget(self.workspace)
        self.navigation.setCurrentRow(0)
        self.timer.start()
        self.refresh()

    def logout(self):
        if self.user and not self.busy:
            def failed(error):
                self._logged_out()
                self.login_message.setText("本机登录状态已清除；服务端退出未确认。可重新登录后撤销其他会话。")
            self.submit(self.client.logout, lambda _: self._logged_out(), failure=failed, message="已退出登录")

    def _logged_out(self):
        self.timer.stop()
        self.user = None
        self.password.clear()
        self.identity.clear()
        self.execution_status.setText("尚未获取执行状态。")
        for value in self.execution_values.values():
            value.setText("未记录")
        for metric in self.metrics.values():
            metric.setText("—")
        self.overview_state.clear()
        self.risk_loaded = False
        self.risk_save.setEnabled(False)
        for field in self.risk_fields.values():
            field.setValue(field.minimum())
        for page in (self.datasets_page, self.jobs_page, self.automations_page, self.ai_page, self.devices_page):
            page.clear()
        self.logout_action.setEnabled(False)
        self.stack.setCurrentWidget(self.login_page)
        self.login_message.setText("已退出登录。")

    def _navigate(self, row):
        self.pages.setCurrentIndex(row)
        if self.user:
            self.refresh()

    def _poll(self):
        if self.user and not self.busy and self.pages.currentIndex() in {0, 2, 3, 6} and QApplication.activeModalWidget() is None:
            self.refresh()

    def _paginate(self, page, delta):
        if not self.busy:
            page.page = max(1, min(page.pages, page.page + delta))
            self.refresh()

    def refresh(self):
        if self.busy:
            return
        if self.user is None:
            self.check_health()
            return
        row = self.pages.currentIndex()
        loaders = {
            0: (self.client.dashboard, self._render_dashboard),
            1: (lambda: self.client.datasets(self.datasets_page.page), self.datasets_page.render),
            2: (lambda: self.client.jobs(self.jobs_page.page), self.jobs_page.render),
            3: (lambda: self.client.automations(self.automations_page.page), self.automations_page.render),
            4: (self.client.risk, self._render_risk),
            5: (self.client.ai_status, self._render_ai),
            6: (self.client.execution_status, self._render_execution),
            7: (self.client.devices, lambda data: self.devices_page.render({"items": data.get("devices", []), "page": 1, "pages": 1})),
        }
        function, callback = loaders[row]
        if row == 4:
            self.risk_loaded = False
            self.risk_save.setEnabled(False)
        self.submit(function, callback)

    def _render_dashboard(self, data):
        for key, widget in self.metrics.items():
            widget.setText(text(data.get(key)))
        paused = data.get("research_paused")
        state = "已暂停接收" if paused is True else "可提交" if paused is False else "未记录"
        self.overview_state.setText(f"研究任务：{state}\n自动仿真运行中：{text(data.get('automation_running'))}；需关注：{text(data.get('automation_attention'))}")

    def _render_execution(self, data):
        states = {"not_configured": "尚未配置纸账户执行器。", "stale": "执行状态已过期。",
                  "invalid": "执行状态无效。", "unavailable": "执行状态不可用。",
                  "read_only": "只读连接", "awaiting_feed": "等待行情", "running": "运行中",
                  "halted": "已停止", "reconcile_required": "需要核对订单与持仓", "disconnected": "连接已断开"}
        available = data.get("available") is True
        self.execution_status.setText(states.get(data.get("state"), "执行状态未知。"))
        for key, value in self.execution_values.items():
            value.setText(text(data.get(key)) if available else "不可用")

    def _render_risk(self, data):
        self.risk_loaded = False
        self.risk_save.setEnabled(False)
        values = {}
        for key, field in self.risk_fields.items():
            if key == "daily_loss_limit":
                try:
                    value = Decimal(str(data[key]))
                    if not value.is_finite() or value < 1 or value > Decimal("9999999999.99") or value.as_tuple().exponent < -2:
                        raise ValueError("Invalid loss limit")
                except (InvalidOperation, KeyError):
                    raise ValueError("Invalid loss limit") from None
            else:
                value = data[key]
                if type(value) is not int or not field.minimum() <= value <= field.maximum():
                    raise ValueError("Invalid risk limit")
            values[key] = value
        if values["soft_inventory_limit"] >= values["hard_inventory_limit"]:
            raise ValueError("Invalid inventory bounds")
        for key, field in self.risk_fields.items():
            field.setValue(float(values[key]) if key == "daily_loss_limit" else values[key])
        self.risk_loaded = True
        self.risk_save.setEnabled(True)
        self.risk_message.setText("已保存的研究参数。" if data.get("persisted") else "当前为服务端默认研究参数，尚未保存自定义设置。")

    def save_risk(self):
        if not self.risk_loaded:
            return
        values = {key: field.value() for key, field in self.risk_fields.items()}
        if values["soft_inventory_limit"] >= values["hard_inventory_limit"]:
            warning(self, "检查限额", "库存软限额必须小于库存硬限额。")
            return
        values["daily_loss_limit"] = f"{values['daily_loss_limit']:.2f}"
        if question(self, "保存研究参数", "新参数只影响之后创建的研究任务。确认保存？") != QMessageBox.StandardButton.Yes:
            return
        self.submit(lambda: self.client.update_risk(values), self._render_risk, message="研究参数已保存")

    def _render_ai(self, data):
        models = data.get("models", [])
        self.ai_page.render({"items": models, "page": 1, "pages": 1})
        if not data.get("available"):
            states = {"not_configured": "尚未配置模型库。", "invalid": "模型证据无效，已拒绝使用。", "unavailable": "模型库暂不可用。"}
            self.ai_page.empty.setText(states.get(data.get("state"), "暂无可用模型证据。"))

    def _selected(self, page):
        item = page.selected()
        if item is None:
            information(self, "请选择记录", "请先在列表中选择一条记录。")
        return item

    def upload(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入 ITCH 文件", "", "ITCH 行情 (*.itch);;所有文件 (*)")
        if not path:
            return
        self.submit(lambda: self.client.upload_dataset(path, Path(path).stem[:120]), lambda _: self.refresh(), message="行情文件已导入")

    def delete_dataset(self):
        item = self._selected(self.datasets_page)
        if item and question(self, "删除行情数据", f"确认删除「{item['name']}」？文件将停止访问并进入删除队列。") == QMessageBox.StandardButton.Yes:
            self.submit(lambda: self.client.delete_dataset(item["id"]), lambda _: self.refresh(), message="删除请求已记录")

    def create_run(self, _checked=False, *, automation=False):
        item = self._selected(self.datasets_page)
        if not item:
            return
        dialog = CreateRunDialog(item, self, automation=automation)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        mode = dialog.mode.currentData()
        parameters = dialog.simulation.values() if mode == "simulation" else None
        if automation:
            name, interval, runs = dialog.name.text().strip(), dialog.interval.value(), dialog.runs.value()
            self.submit(lambda: self.client.create_automation(name, item["id"], interval_seconds=interval, max_runs=runs, simulation_parameters=parameters),
                        lambda _: self.navigation.setCurrentRow(3), message="自动仿真已保存为停止状态")
        else:
            self.submit(lambda: self.client.create_job(item["id"], mode=mode, simulation_parameters=parameters),
                        lambda _: self.navigation.setCurrentRow(2), message="研究任务已提交")

    def show_job(self, *_):
        item = self._selected(self.jobs_page)
        if item:
            self.submit(lambda: self.client.job(item["id"]), lambda data: JobDialog(data, self).exec())

    def cancel_job(self):
        item = self._selected(self.jobs_page)
        if item and question(self, "请求取消任务", "取消请求将在后台检查点生效。确认提交？") == QMessageBox.StandardButton.Yes:
            self.submit(lambda: self.client.cancel_job(item["id"]), lambda _: self.refresh(), message="已请求取消，请以任务最终状态为准")

    def retry_job(self):
        item = self._selected(self.jobs_page)
        if item and question(self, "重试任务", "将使用原任务参数创建一次新的研究任务。确认重试？") == QMessageBox.StandardButton.Yes:
            self.submit(lambda: self.client.retry_job(item["id"]), lambda _: self.refresh(), message="已提交重试")

    def show_automation(self):
        item = self._selected(self.automations_page)
        if item:
            self.submit(lambda: self.client.automation(item["id"]), lambda data: JsonDialog("自动仿真配置", data, self).exec())

    def control_automation(self, action):
        item = self._selected(self.automations_page)
        if not item:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("启动自动仿真" if action == "start" else "停止自动仿真")
        dialog.setMinimumWidth(440)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label(item["name"], name="heading"))
        layout.addWidget(label("请复核账户密码。此操作仅控制历史仿真，不向券商下单。", name="notice"))
        if item.get("last_reason"):
            layout.addWidget(label(item["last_reason"], name="subtitle"))
        password = QLineEdit()
        password.setEchoMode(QLineEdit.EchoMode.Password)
        password.setMaxLength(1024)
        password.setPlaceholderText("当前账户密码")
        layout.addWidget(password)
        acknowledge = QCheckBox("我已检查风险或故障原因，明确允许恢复自动仿真。")
        acknowledge.setVisible(action == "start" and bool(item.get("risk_latched")))
        layout.addWidget(acknowledge)
        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        controls.accepted.connect(dialog.accept)
        controls.rejected.connect(dialog.reject)
        layout.addWidget(controls)
        result = dialog.exec()
        secret = password.text()
        password.clear()
        allowed = acknowledge.isChecked()
        if result != QDialog.DialogCode.Accepted:
            return
        if not secret:
            warning(self, "需要密码", "请输入当前账户密码。")
            return
        self.submit(lambda: self.client.automation_command(item["id"], action, secret, acknowledge_latch=allowed),
                    lambda _: self.refresh(), message="控制命令已提交，请核对最新状态")

    def show_model(self):
        item = self._selected(self.ai_page)
        if item:
            JsonDialog("模型训练与评估证据", item, self).exec()

    def revoke_device(self):
        item = self._selected(self.devices_page)
        if item and question(self, "撤销登录会话", "确认撤销此设备的登录状态？当前设备也会立即退出。") == QMessageBox.StandardButton.Yes:
            callback = (lambda _: self._logged_out()) if item.get("is_current") else (lambda _: self.refresh())
            self.submit(lambda: self.client.revoke_device(item["id"]), callback, message="会话已撤销")

    def closeEvent(self, event):
        self.timer.stop()
        if self.busy:
            self.closing = True
            self.statusBar().showMessage("正在完成当前请求，随后关闭窗口…")
            event.ignore()
            return
        self.password.clear()
        event.accept()
