"""Reusable native Qt controls and dialogs; no embedded browser."""
from __future__ import annotations

import json
from decimal import Decimal

from PySide6.QtCore import QObject, QRunnable, Qt, Signal, Slot
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog,
    QDialogButtonBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QMessageBox,
)

STYLE = """
QMainWindow, QDialog { background: #f4f5f7; color: #202b38; }
QWidget { font-size: 13px; }
QLabel { color: #202b38; background: transparent; }
QLabel#heading { font-size: 24px; font-weight: 600; }
QLabel#subtitle { color: #526174; }
QLabel#notice { background: #eef3f8; border: 1px solid #d7dde5;
    border-radius: 4px; padding: 10px; color: #245b87; }
QLabel#error { background: #fff1f1; border: 1px solid #e7b8bd;
    border-radius: 4px; padding: 10px; color: #b42332; }
QFrame#card { background: white; border: 1px solid #d7dde5; border-radius: 5px; }
QListWidget#navigation { background: #edf0f3; border: none; border-right: 1px solid #d7dde5; padding: 14px 8px; }
QListWidget#navigation::item { padding: 13px 16px; border-radius: 4px; margin: 2px; }
QListWidget#navigation::item:selected { background: #dce7f1; color: #19476c; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {
    background: white; color: #202b38; border: 1px solid #c5ced9; border-radius: 4px;
    padding: 7px; selection-background-color: #245b87; selection-color: white;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border-color: #245b87; }
QPushButton { background: white; color: #202b38; border: 1px solid #c5ced9;
    border-radius: 4px; padding: 8px 15px; min-height: 20px; }
QPushButton:hover { background: #edf2f7; border-color: #879aae; }
QPushButton:pressed { background: #dce7f1; }
QPushButton:disabled { color: #8793a0; background: #eef0f3; border-color: #dde2e7; }
QPushButton#primary { background: #245b87; border-color: #245b87; color: white; }
QPushButton#primary:hover { background: #19476c; }
QPushButton#primary:disabled { background: #8b9eae; border-color: #8b9eae; }
QPushButton#danger { color: #b42332; }
QTableWidget { background: white; alternate-background-color: #f7f9fb;
    color: #202b38; border: 1px solid #d7dde5; gridline-color: #e5e9ee;
    selection-background-color: #e1ebf4; selection-color: #19476c; }
QTableWidget::item { padding: 7px; }
QHeaderView::section { background: #edf0f3; color: #526174;
    border: none; border-bottom: 1px solid #d7dde5; padding: 10px; }
QStatusBar { background: white; color: #526174; border-top: 1px solid #d7dde5; }
QScrollArea { border: none; background: transparent; }
QToolTip { background: white; color: #202b38; border: 1px solid #c5ced9; }
"""

STATUS = {
    "queued": "等待执行", "running": "运行中", "succeeded": "已完成",
    "failed": "失败", "canceled": "已取消", "stopped": "已停止",
    "paused": "已暂停", "faulted": "故障锁定", "completed": "次数已用完",
    "replay": "行情回放", "simulation": "历史仿真", "candidate": "候选模型",
}


def text(value):
    """Never turn missing financial or operational evidence into a numeric zero."""
    if value is None:
        return "未记录"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def label(value, *, name=None):
    widget = QLabel(str(value))
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    if name:
        widget.setObjectName(name)
    return widget


def button(title, callback, *, primary=False):
    widget = QPushButton(title)
    widget.clicked.connect(callback)
    if primary:
        widget.setObjectName("primary")
    return widget


def apply_palette(app: QApplication):
    palette = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, "#f4f5f7"),
        (QPalette.ColorRole.WindowText, "#202b38"),
        (QPalette.ColorRole.Base, "#ffffff"),
        (QPalette.ColorRole.AlternateBase, "#f7f9fb"),
        (QPalette.ColorRole.Text, "#202b38"),
        (QPalette.ColorRole.Button, "#ffffff"),
        (QPalette.ColorRole.ButtonText, "#202b38"),
        (QPalette.ColorRole.Highlight, "#245b87"),
        (QPalette.ColorRole.HighlightedText, "#ffffff"),
        (QPalette.ColorRole.ToolTipBase, "#ffffff"),
        (QPalette.ColorRole.ToolTipText, "#202b38"),
    ):
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    app.setStyleSheet(STYLE)


class TaskSignals(QObject):
    completed = Signal(object, object)


class RequestTask(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = TaskSignals()

    @Slot()
    def run(self):
        result, error = None, None
        try:
            result = self.function()
        except Exception as exc:
            error = exc
        finally:
            # Release password-bearing closures promptly after the request.
            self.function = None
        self.signals.completed.emit(result, error)


class CollectionPage(QWidget):
    """An owner-scoped paginated list, with native row selection and detail view."""

    def __init__(self, title, subtitle, columns):
        super().__init__()
        self.columns = columns
        self.items = []
        self.page = 1
        self.pages = 1
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(14)
        layout.addWidget(label(title, name="heading"))
        layout.addWidget(label(subtitle, name="subtitle"))
        self.toolbar = QHBoxLayout()
        layout.addLayout(self.toolbar)
        self.table = QTableWidget(0, len(columns))
        self.table.setHorizontalHeaderLabels([col[0] for col in columns])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        self.empty = label("尚未加载。", name="subtitle")
        layout.addWidget(self.empty)
        pagination = QHBoxLayout()
        self.previous = QPushButton("上一页")
        self.next = QPushButton("下一页")
        self.counter = label("—")
        pagination.addWidget(self.previous)
        pagination.addWidget(self.next)
        pagination.addStretch()
        pagination.addWidget(self.counter)
        layout.addLayout(pagination)

    def render(self, data):
        try:
            items = data["items"]
            if not isinstance(items, list) or len(items) > 500 or any(not isinstance(item, dict) for item in items):
                raise ValueError("Invalid list")
            page, pages = data.get("page", 1), data.get("pages", 1)
            if type(page) is not int or type(pages) is not int or not 1 <= page <= pages:
                raise ValueError("Invalid pagination")
            rows = [[text(getter(item)) for _, getter in self.columns] for item in items]
            total = data.get("total", len(items))
            if type(total) is not int or total < 0:
                raise ValueError("Invalid count")
        except Exception:
            self.clear()
            raise
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.setRowCount(0)
        self.items, self.page, self.pages = items, page, pages
        self.table.setRowCount(len(items))
        for row, values in enumerate(rows):
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(value))
        self.empty.setText("暂无记录。" if not self.items else "选择一行可查看详情或执行操作。")
        self.counter.setText(f"第 {self.page} / {self.pages} 页 · 共 {total} 条")
        self.update_pagination()

    def update_pagination(self):
        self.previous.setEnabled(self.page > 1)
        self.next.setEnabled(self.page < self.pages)

    def selected(self):
        row = self.table.currentRow()
        return self.items[row] if 0 <= row < len(self.items) else None

    def clear(self):
        self.items = []
        self.table.setRowCount(0)
        self.page = self.pages = 1
        self.counter.setText("—")
        self.empty.setText("尚未加载。")
        self.update_pagination()


class JsonDialog(QDialog):
    def __init__(self, title, data, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(800, 560)
        layout = QVBoxLayout(self)
        layout.addWidget(label(title, name="heading"))
        report = QPlainTextEdit()
        report.setReadOnly(True)
        report.setFont(QFont("monospace", 11))
        report.setPlainText(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        layout.addWidget(report)
        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        controls.rejected.connect(self.reject)
        layout.addWidget(controls)


class SimulationForm(QWidget):
    FIELDS = (
        ("latency_ns", "下单延迟（毫秒）", 1, .001, 10000, 3, 1000000),
        ("cancel_latency_ns", "撤单延迟（毫秒）", 1, .001, 10000, 3, 1000000),
        ("simulation_window_ns", "报价生命周期（毫秒）", 1000, 1, 60000, 3, 1000000),
        ("fee_per_share_units", "每股费用（USD）", .003, 0, 100000, 4, 10000),
        ("min_fee_per_order_units", "每单最低费用（USD）", 0, 0, 100000, 4, 10000),
        ("rebate_per_share_units", "每股假设返佣（USD）", 0, 0, 100, 4, 10000),
        ("slippage_buffer_units", "每股滑点缓冲（USD）", 0, 0, 100000, 4, 10000),
        ("min_net_edge_units", "每股最小净边际（USD）", 0, 0, 100000, 4, 10000),
    )

    def __init__(self):
        super().__init__()
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        self.fields = {}
        for key, title, default, low, high, decimals, multiplier in self.FIELDS:
            field = QDoubleSpinBox()
            field.setDecimals(decimals)
            field.setRange(low, high)
            field.setValue(default)
            field.setSingleStep(10 ** -decimals)
            field.setKeyboardTracking(False)
            form.addRow(title, field)
            self.fields[key] = (field, multiplier)

    def values(self):
        return {key: int(Decimal(str(field.value())) * multiplier)
                for key, (field, multiplier) in self.fields.items()}


class CreateRunDialog(QDialog):
    def __init__(self, dataset, parent=None, *, automation=False):
        super().__init__(parent)
        self.setWindowTitle("新建自动仿真" if automation else "新建研究任务")
        self.resize(540, 610)
        layout = QVBoxLayout(self)
        layout.addWidget(label(dataset["name"], name="heading"))
        layout.addWidget(label("仅使用历史数据进行研究，不会向券商发送订单。", name="notice"))
        form = QFormLayout()
        self.mode = QComboBox()
        self.mode.addItem("行情回放", "replay")
        self.mode.addItem("历史仿真", "simulation")
        self.mode.setCurrentIndex(1 if automation else 0)
        self.mode.setEnabled(not automation)
        form.addRow("运行模式", self.mode)
        self.name = QLineEdit()
        self.name.setMaxLength(120)
        self.name.setPlaceholderText("为自动运行命名")
        self.interval = QSpinBox()
        self.interval.setRange(60, 86400)
        self.interval.setValue(300)
        self.runs = QSpinBox()
        self.runs.setRange(1, 1000)
        self.runs.setValue(10)
        if automation:
            form.addRow("名称", self.name)
            form.addRow("间隔（秒）", self.interval)
            form.addRow("最多运行次数", self.runs)
        layout.addLayout(form)
        self.simulation = SimulationForm()
        self.simulation.setEnabled(automation)
        self.mode.currentIndexChanged.connect(lambda: self.simulation.setEnabled(self.mode.currentData() == "simulation"))
        layout.addWidget(self.simulation)
        layout.addWidget(label("以上费用与延迟为研究假设，请按数据及交易场所情况调整。", name="subtitle"))
        self.validation = label("", name="error")
        self.validation.hide()
        layout.addWidget(self.validation)
        self.automation = automation
        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        controls.button(QDialogButtonBox.StandardButton.Ok).setText("保存为停止状态" if automation else "提交任务")
        controls.accepted.connect(self.validate)
        controls.rejected.connect(self.reject)
        layout.addWidget(controls)

    def validate(self):
        if self.automation and not self.name.text().strip():
            self.validation.setText("请填写自动运行名称。")
            self.validation.show()
            return
        self.accept()


def _message(parent, title, content, icon, buttons):
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(str(content))
    box.setIcon(icon)
    box.setStandardButtons(buttons)
    if buttons & QMessageBox.StandardButton.No:
        box.setDefaultButton(QMessageBox.StandardButton.No)
    return box.exec()


def question(parent, title, content):
    return _message(parent, title, content, QMessageBox.Icon.Question,
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)


def warning(parent, title, content):
    return _message(parent, title, content, QMessageBox.Icon.Warning, QMessageBox.StandardButton.Ok)


def information(parent, title, content):
    return _message(parent, title, content, QMessageBox.Icon.Information, QMessageBox.StandardButton.Ok)


def money(value):
    return f"{Decimal(value) / Decimal(10000):,.4f}" if type(value) is int else "估值不可用"


def job_summary(data):
    rows = [("任务状态", STATUS.get(data.get("status"), text(data.get("status")))),
            ("运行模式", STATUS.get(data.get("mode"), text(data.get("mode")))),
            ("创建时间", text(data.get("created_at"))), ("完成时间", text(data.get("finished_at")))]
    if data.get("error_message"):
        rows.append(("失败原因", data["error_message"]))
    if data.get("cancellation_requested"):
        rows.append(("取消请求", "已提交，请以任务最终状态为准"))
    result = data.get("result") or {}
    simulation = result.get("summary", {}).get("simulation")
    if isinstance(simulation, dict) and data.get("mode") == "simulation":
        valuation = simulation.get("valuation")
        if isinstance(valuation, dict):
            net = valuation.get("net_pnl_units") if valuation.get("net_pnl_valid") is True else None
            drawdown = valuation.get("max_drawdown_units") if valuation.get("drawdown_complete") is True else None
        else:
            net = simulation.get("net_pnl_units") if simulation.get("pnl_mark_valid") is True else None
            drawdown = simulation.get("drawdown_units") if simulation.get("pnl_mark_valid") is True else None
        rows.extend([("模拟净盈亏（USD）", money(net)), ("最大模拟回撤（USD）", money(drawdown)),
                     ("结束库存（股）", text(simulation.get("inventory"))),
                     ("模拟成交次数", text(simulation.get("fill_count"))),
                     ("费用（USD）", money(simulation.get("fees_units"))),
                     ("结束时开放订单", text(simulation.get("open_orders_at_end")))])
    return rows


class JobDialog(QDialog):
    def __init__(self, data, parent=None):
        super().__init__(parent)
        self.setWindowTitle("研究任务结果")
        self.resize(760, 580)
        layout = QVBoxLayout(self)
        layout.addWidget(label("研究任务结果", name="heading"))
        layout.addWidget(label("历史研究结果，仅表示相应输入与模拟假设下的表现。", name="notice"))
        form = QFormLayout()
        for title, value in job_summary(data):
            form.addRow(title, label(value))
        layout.addLayout(form)
        details = QPlainTextEdit()
        details.setReadOnly(True)
        details.setPlainText(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        details.hide()
        toggle = button("查看完整研究记录", lambda: details.setVisible(not details.isVisible()))
        layout.addWidget(toggle)
        layout.addWidget(details, 1)
        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        controls.rejected.connect(self.reject)
        layout.addWidget(controls)
