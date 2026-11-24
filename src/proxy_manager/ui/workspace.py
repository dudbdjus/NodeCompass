from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QPushButton, QStackedWidget, QVBoxLayout, QWidget

from proxy_manager.branding import APP_NAME

from .cards import text_label


class Workspace(QWidget):
    def __init__(self, pages):
        super().__init__()
        self.setObjectName("workspace")
        shell = QHBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        rail = QFrame()
        rail.setObjectName("sidebar")
        rail.setFixedWidth(166)
        navigation = QVBoxLayout(rail)
        navigation.setContentsMargins(15, 22, 15, 16)
        navigation.setSpacing(7)
        brand = text_label(APP_NAME, "brand")
        navigation.addWidget(brand)
        navigation.addSpacing(19)
        shell.addWidget(rail)

        body = QWidget()
        content = QVBoxLayout(body)
        content.setContentsMargins(24, 17, 20, 16)
        content.setSpacing(12)
        self.heading = text_label("", "pageTitle")
        content.addWidget(self.heading)
        self.stack = QStackedWidget()
        content.addWidget(self.stack, 1)
        shell.addWidget(body, 1)

        self.navigation = QButtonGroup(self)
        for index, (title, page) in enumerate(pages):
            if index == len(pages) - 2:
                navigation.addStretch()
            button = QPushButton(title)
            button.setCheckable(True)
            button.setProperty("role", "navigation")
            self.navigation.addButton(button, index)
            navigation.addWidget(button)
            self.stack.addWidget(page)
        self.navigation.idClicked.connect(self.stack.setCurrentIndex)
        self.stack.currentChanged.connect(self.select)
        self.select(0)
        self.setStyleSheet(WORKSPACE_STYLE)

    def select(self, index):
        button = self.navigation.button(index)
        if button is not None:
            button.setChecked(True)
            self.heading.setText(button.text())


WORKSPACE_STYLE = """
QWidget#workspace, QWidget#dashboard, QStackedWidget {
    background: #f3f7fc; color: #152a49;
}
QFrame#sidebar { background: #fff; border-right: 1px solid #dbe5f3; }
QLabel { color: #213a5a; }
QLabel[role="brand"] { color: #174589; font-size: 16px; font-weight: 700; }
QLabel[role="pageTitle"] { color: #152a49; font-size: 27px; font-weight: 700; }
QLabel[role="sectionTitle"] { font-size: 17px; font-weight: 650; }
QLabel[role="cardTitle"] { color: #244369; font-size: 14px; font-weight: 650; }
QLabel[role="metric"] { color: #163f7f; font-size: 27px; font-weight: 700; }
QLabel[role="reading"] { color: #184b94; font-size: 14px; font-weight: 650; }
QLabel[role="muted"] { color: #516883; }
QFrame[role="card"] { background: #fff; border: 1px solid #dbe5f3; border-radius: 12px; }
QPushButton {
    color: #235396; background: #fff; border: 1px solid #c7d8ee;
    border-radius: 6px; padding: 5px 10px;
}
QPushButton:hover { background: #edf5ff; border-color: #93b8eb; }
QPushButton:pressed { background: #dbeaff; }
QPushButton:focus { border-color: #2166d9; }
QPushButton:disabled { color: #788ba4; background: #edf1f7; border-color: #e0e7f0; }
QPushButton[role="navigation"] {
    background: transparent; color: #334f74; text-align: left;
    min-height: 24px; padding: 7px 12px; border: 1px solid transparent;
}
QPushButton[role="navigation"]:hover { background: #f0f6ff; }
QPushButton[role="navigation"]:checked {
    background: #e6f0ff; color: #174d9b; border-left: 3px solid #2166d9; font-weight: 700;
}
QPushButton[role="navigation"]:focus { border-color: #2166d9; }
QProgressBar[role="usage"] {
    border: 0; border-radius: 4px; background: #e8eff9; min-height: 8px; max-height: 8px;
}
QProgressBar[role="usage"]::chunk {
    background: #2166d9; border-radius: 4px;
}
QTabWidget::pane { background: #fff; border: 1px solid #dbe5f3; }
QTabBar::tab {
    color: #385579; background: #ecf2f9; padding: 7px 14px;
    border: 1px solid #dbe5f3; border-bottom: 0;
}
QTabBar::tab:selected { background: #fff; color: #174d9b; border-top: 2px solid #2166d9; }
QGroupBox { color: #244369; }
QLineEdit, QComboBox, QSpinBox, QDateEdit {
    background: #fff; color: #213a5a; border: 1px solid #c7d8ee; border-radius: 4px; min-height: 23px;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDateEdit:focus { border-color: #2166d9; }
QTableView {
    background: #fff; color: #213a5a; alternate-background-color: #f4f8fe;
    border: 1px solid #dbe5f3; gridline-color: #e3ebf5;
    selection-background-color: #dbeaff; selection-color: #153d73;
}
QHeaderView::section { background: #eaf2fc; color: #294b76; padding: 4px; border: 1px solid #dbe5f3; }
"""
