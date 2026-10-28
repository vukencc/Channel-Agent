"""桌面项目目录选择控件。"""
import os

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLineEdit, QPushButton, QWidget

from .theme import icon


def choose_project_directory(parent, current_path: str) -> str:
    dialog = QFileDialog(parent)
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
    dialog.setOption(QFileDialog.Option.ReadOnly, True)
    dialog.setOption(QFileDialog.Option.DontResolveSymlinks, True)
    dialog.setWindowTitle('选择已有项目')
    dialog.setFileMode(QFileDialog.FileMode.Directory)
    dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
    dialog.setLabelText(QFileDialog.DialogLabel.LookIn, '位置：')
    dialog.setLabelText(QFileDialog.DialogLabel.FileName, '目录：')
    dialog.setLabelText(QFileDialog.DialogLabel.Accept, '选择项目')
    dialog.setLabelText(QFileDialog.DialogLabel.Reject, '取消')
    dialog.setDirectory(current_path or os.path.expanduser('~'))
    for box in dialog.findChildren(QDialogButtonBox):
        for button in box.buttons():
            button.setIcon(QIcon())
    try:
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected = dialog.selectedFiles()
            return selected[0] if selected else ''
        return ''
    finally:
        # 对话框有父控件，显式释放，避免反复选择累积目录模型与监听器。
        dialog.deleteLater()


class ProjectPicker(QWidget):
    def __init__(self, parent=None, *, guard=None):
        super().__init__(parent)
        self.guard = guard
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.path_edit = QLineEdit()
        self.browse_button = QPushButton('选择')
        self.browse_button.setIcon(icon('folder', size=16))
        layout.addWidget(self.path_edit, 1)
        layout.addWidget(self.browse_button)
        self.browse_button.clicked.connect(self._browse)

    def _browse(self):
        token = self.guard() if self.guard else None
        directory = choose_project_directory(self, self.path_edit.text())
        if directory and (self.guard is None or (token is not None and token == self.guard())):
            self.path_edit.setText(directory)
