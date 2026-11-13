"""Native local research evidence viewer and read-only cloud connection check."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFileDialog, QTableWidget, QTableWidgetItem, QLineEdit, QFormLayout, QGroupBox)
from .widgets import RequestTask


def read_evidence(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Invalid report")
    report = json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    model_path = path.parent / "model.json"
    if model_path.is_symlink() or not model_path.is_file() or model_path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Missing model")
    model = json.loads(model_path.read_text(encoding="utf-8"))
    content = {k: v for k, v in model.items() if k != "model_version"}
    digest = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    if model.get("model_version") != "houmen-ridge-" + digest[:24] or report.get("model_version") != model["model_version"]:
        raise ValueError("Model identity mismatch")
    rows = [("模型版本", model["model_version"]), ("开发样本", report["development_samples"]),
            ("测试样本", report["holdout_samples"]),
            ("AI 预测误差（基点）", f"{report['holdout']['ridge']['rmse_bps']:.2f}"),
            ("零收益基线误差（基点）", f"{report['holdout']['zero_return']['rmse_bps']:.2f}")]
    for name, title in (("houmen_rules", "厚门规则"), ("houmen_ai", "厚门＋AI")):
        value = report["backtests"][name]
        rows += [(title + " · 扣成本收益", f"{value['net_return']:.2%}"),
                 (title + " · 最大回撤", f"{value['max_drawdown']:.2%}"),
                 (title + " · 完整交易次数", value["completed_round_trips"])]
    return rows, report.get("research_checks_passed") is True


class HoumenWindow(QMainWindow):
    def __init__(self, legacy_factory=None):
        super().__init__()
        self.setWindowTitle("Vector · 厚门研究")
        self.resize(1000, 780)
        self.setMinimumSize(760, 640)
        self.busy = self.closing = False
        self.task = None
        self.legacy_factory, self.legacy = legacy_factory, None
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(32, 24, 32, 24)
        title = QLabel("厚门 · 日线波段研究")
        title.setObjectName("heading")
        layout.addWidget(title)
        layout.addWidget(QLabel("查看真实训练证据，检查券商云端连接。此窗口不会发送订单。"))
        buttons = QHBoxLayout()
        load = QPushButton("打开训练报告…")
        load.clicked.connect(self.choose_report)
        buttons.addWidget(load)
        if legacy_factory:
            legacy = QPushButton("打开原研究工作区")
            legacy.clicked.connect(self.open_legacy)
            buttons.addWidget(legacy)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.report_status = QLabel("尚未加载报告。请选择交付包 training 目录中的 training-report.json；同目录需保留 model.json。")
        self.report_status.setWordWrap(True)
        self.report_status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.report_status)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["项目", "实际结果"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 280)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)
        group = QGroupBox("Alpaca 云端连接 · Paper 只读检查")
        form = QFormLayout(group)
        self.key = QLineEdit()
        self.secret = QLineEdit()
        for field in (self.key, self.secret):
            field.setEchoMode(QLineEdit.EchoMode.Password)
            field.setMaxLength(512)
        form.addRow("Paper API Key", self.key)
        form.addRow("Paper Secret", self.secret)
        self.connect_button = QPushButton("检查云端连接")
        self.connect_button.clicked.connect(self.check_cloud)
        form.addRow(self.connect_button)
        self.connection = QLabel("未连接。凭据仅用于本次检查，不保存到文件。")
        self.connection.setWordWrap(True)
        self.connection.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow(self.connection)
        layout.addWidget(group)
        footer = QLabel("自动交易未启用：需通过模型验证、有效 USD/CNY 数据及券商联调。\n当前安装包提供报告查看与云端检查，后台执行器见源码交付包。")
        footer.setWordWrap(True)
        layout.addWidget(footer)
        self.statusBar().showMessage("本地研究入口 · 无需 Docker 即可打开")

    def choose_report(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择厚门训练报告", "", "JSON (*.json)")
        if path:
            self.load_report(path)

    def load_report(self, path):
        self.table.setRowCount(0)
        try:
            rows, passed = read_evidence(path)
            self.table.setRowCount(len(rows))
            for row, (name, value) in enumerate(rows):
                self.table.setItem(row, 0, QTableWidgetItem(str(name)))
                self.table.setItem(row, 1, QTableWidgetItem(str(value)))
            self.report_status.setText("研究检查通过；尚不构成交易批准。" if passed else "研究检查未通过 · 模型不可据此启用交易。零成交不代表盈利能力。")
        except Exception:
            self.table.setRowCount(0)
            self.report_status.setText("无法读取报告：请保留同目录的原始 model.json 和 training-report.json，并检查文件是否完整。")

    def check_cloud(self):
        if self.busy:
            return
        from execution.alpaca_cloud import AlpacaClient, AlpacaConfig
        key, secret = self.key.text().strip(), self.secret.text().strip()
        self.key.clear()
        self.secret.clear()
        if not key or not secret:
            self.connection.setText("请填写 Paper API Key 和 Secret。")
            return
        def query():
            client = AlpacaClient(AlpacaConfig(key, secret, timeout_seconds=5))
            account, clock = client.get_account(), client.get_clock()
            return account.get("status") == "ACTIVE", clock.get("is_open") is True
        self.busy = True
        self.connect_button.setEnabled(False)
        self.connection.setText("正在检查券商云端连接…")
        self.task = RequestTask(query)
        self.task.signals.completed.connect(self.complete_cloud)
        self.pool.start(self.task)

    def complete_cloud(self, result, error):
        self.busy = False
        self.task = None
        self.connect_button.setEnabled(True)
        if error is not None:
            self.connection.setText("连接未成功。请检查 Paper 凭据、网络或券商服务；未发送交易指令。")
        else:
            self.connection.setText("云端连接成功 · 账户" + ("正常" if result[0] else "状态需检查") +
                                    (" · 正常交易时段" if result[1] else " · 非正常交易时段") + "。行情权限和下单尚未验证。")
        if self.closing:
            self.close()

    def open_legacy(self):
        if self.legacy is None:
            self.legacy = self.legacy_factory()
        self.legacy.show()
        self.legacy.raise_()

    def closeEvent(self, event):
        self.key.clear()
        self.secret.clear()
        if self.busy:
            self.closing = True
            self.hide()
            event.ignore()
        else:
            event.accept()
