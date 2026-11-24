from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout


def text_label(text="", role=""):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    if role:
        label.setProperty("role", role)
    return label


def card(title, source=""):
    frame = QFrame()
    frame.setProperty("role", "card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(7)
    layout.addWidget(text_label(title, "cardTitle"))
    if source:
        layout.addWidget(text_label(source, "muted"))
    return frame, layout


# Match the billing panel's GB label without changing the existing byte scale.
BYTES_PER_DISPLAY_GB = 2**30


def display_gb(value, *, group=True):
    return f"{value / BYTES_PER_DISPLAY_GB:{',.2f' if group else '.2f'}} GB" if value is not None else "—"
