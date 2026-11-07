"""ScanStack: turn a stack of scanned pages into one PDF.

Drop scans (or a folder of them) in, check the order, pick how small you want
the file, and save. Pages go onto US Letter by default.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from dataclasses import replace

from PySide6.QtCore import (
    QEvent, QObject, QRectF, QRunnable, QSettings, QSize, Qt, QThreadPool,
    QTimer, QUrl, Signal,
)
from PySide6.QtGui import (
    QAction, QColor, QDesktopServices, QIcon, QImage, QKeySequence, QPainter,
    QPixmap, QTransform,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog,
    QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QSizePolicy, QSlider, QSplitter, QToolButton, QVBoxLayout, QWidget,
)

import engine
from engine import PageSource, Settings

APP_NAME = "ScanStack"
THUMB = 150


def resource(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def app_version() -> str:
    try:
        with open(resource("VERSION")) as f:
            return f.read().strip()
    except OSError:
        return "dev"


def pil_to_qimage(im) -> QImage:
    if im.mode == "1":
        im = im.convert("L")
    if im.mode == "L":
        data = im.tobytes()
        q = QImage(data, im.width, im.height, im.width, QImage.Format_Grayscale8)
    else:
        im = im.convert("RGB")
        data = im.tobytes()
        q = QImage(data, im.width, im.height, im.width * 3, QImage.Format_RGB888)
    return q.copy()  # detach from the Python buffer


def human_size(n: int | None) -> str:
    if n is None:
        return "—"
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("bytes", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


# --------------------------------------------------------------------------- background jobs


class Signals(QObject):
    done = Signal(object, object)  # (token, result)


class Job(QRunnable):
    def __init__(self, token, func, *args):
        super().__init__()
        self.token, self.func, self.args = token, func, args
        self.signals = Signals()

    def run(self):
        try:
            result = self.func(*self.args)
        except Exception as e:
            result = e
        try:
            self.signals.done.emit(self.token, result)
        except RuntimeError:
            pass  # window closed while we worked


class BuildSignals(QObject):
    progress = Signal(int, int, str)
    finished = Signal(object)


class BuildJob(QRunnable):
    """Makes the PDF on a background thread so the window stays responsive."""

    def __init__(self, pages, out_path, settings):
        super().__init__()
        self.pages, self.out_path, self.settings = pages, out_path, settings
        self.cancel = threading.Event()
        self.signals = BuildSignals()

    def _progress(self, i, n, name):
        try:
            self.signals.progress.emit(i, n, name)
        except RuntimeError:
            self.cancel.set()

    def run(self):
        try:
            result = engine.build_pdf(
                self.pages, self.out_path, self.settings,
                progress=self._progress,
                cancelled=self.cancel.is_set,
            )
        except Exception as e:
            result = e
        try:
            self.signals.finished.emit(result)
        except RuntimeError:
            pass  # app quit while we worked


# --------------------------------------------------------------------------- widgets


class PageList(QListWidget):
    """Thumbnails you can drag to reorder, and drop files or folders onto."""

    files_dropped = Signal(list, int)  # paths, insert row (-1 = end)

    def __init__(self):
        super().__init__()
        self.setViewMode(QListWidget.IconMode)
        self.setFlow(QListWidget.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListWidget.Adjust)
        # Icon grids normally let you drag items to any spot without changing the
        # order. "Static" + InternalMove makes a drag actually reorder the pages.
        self.setMovement(QListWidget.Static)
        self.setIconSize(QSize(THUMB, THUMB))
        self.setGridSize(QSize(THUMB + 24, THUMB + 44))
        self.setSpacing(4)
        self.setUniformItemSizes(True)
        self.setWordWrap(True)
        self.setTextElideMode(Qt.ElideMiddle)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.set_editable(True)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDropIndicatorShown(True)
        self.setMinimumWidth(THUMB * 2 + 70)

    def set_editable(self, on: bool):
        self.setDragDropMode(QAbstractItemView.InternalMove if on else QAbstractItemView.NoDragDrop)
        self.setDragEnabled(on)
        self.setAcceptDrops(on)
        self.viewport().setAcceptDrops(on)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and e.source() is not self:
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls() and e.source() is not self:
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        if e.mimeData().hasUrls() and e.source() is not self:
            paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
            item = self.itemAt(e.position().toPoint())
            self.files_dropped.emit(paths, self.row(item) if item else -1)
            e.acceptProposedAction()
        else:
            super().dropEvent(e)
            self.model().layoutChanged.emit()

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.count() == 0:
            p = QPainter(self.viewport())
            p.setPen(self.palette().color(self.palette().ColorRole.PlaceholderText))
            f = p.font()
            f.setPointSize(f.pointSize() + 3)
            p.setFont(f)
            p.drawText(self.viewport().rect().adjusted(20, 20, -20, -20), Qt.AlignCenter | Qt.TextWordWrap,
                       "Drop your scans here\n— or a whole folder of them —\n\n"
                       "They're put in name order (2 before 10).\nDrag thumbnails to change the order.")
            p.end()


class PagePreview(QWidget):
    """Shows the selected page exactly as it will sit on the paper."""

    def __init__(self):
        super().__init__()
        self.image: QImage | None = None
        self.layout_info = None
        self.message = "No page selected"
        self.setMinimumSize(320, 380)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def show_page(self, qimg: QImage | None, layout=None, message: str = ""):
        self.image, self.layout_info, self.message = qimg, layout, message
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(24, 24, -24, -24)
        if self.layout_info is None:
            p.setPen(self.palette().color(self.palette().ColorRole.PlaceholderText))
            p.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap, self.message)
            return
        lay = self.layout_info
        scale = min(r.width() / lay.page_w_pt, r.height() / lay.page_h_pt)
        pw, ph = lay.page_w_pt * scale, lay.page_h_pt * scale
        x0 = r.x() + (r.width() - pw) / 2
        y0 = r.y() + (r.height() - ph) / 2
        paper = QRectF(x0, y0, pw, ph)
        # soft shadow
        for i, a in enumerate((28, 18, 10)):
            p.fillRect(paper.translated(0, 2 + i), QColor(0, 0, 0, a))
        p.fillRect(paper, Qt.white)
        bx, by, bw, bh = lay.box
        img_rect = QRectF(x0 + bx * scale, y0 + (lay.page_h_pt - by - bh) * scale, bw * scale, bh * scale)
        if self.image is not None:
            p.drawImage(img_rect, self.image)
        p.setPen(QColor(0, 0, 0, 60))
        p.drawRect(paper)


# --------------------------------------------------------------------------- main window


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 820)
        self.settings_store = QSettings("JRH Films", APP_NAME)
        self.pool = QThreadPool.globalInstance()
        self.pages: dict[int, PageSource] = {}
        self.next_key = 1
        self.thumb_cache: dict[tuple, QImage] = {}
        self.preview_token = 0
        self.estimate_token = 0
        self.build_job: BuildJob | None = None
        self.last_output: str | None = None
        self._jobs = set()  # keep Python wrappers alive until they report back

        self._build_ui()
        self._load_settings()

        self.preview_timer = QTimer(self, singleShot=True, interval=120, timeout=self.refresh_preview)
        self.estimate_timer = QTimer(self, singleShot=True, interval=450, timeout=self.refresh_estimate)
        self.update_state()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        # Left: the stack
        self.list = PageList()
        self.list.files_dropped.connect(self.add_paths)
        self.list.currentItemChanged.connect(lambda *_: self.schedule_preview())
        self.list.model().rowsMoved.connect(lambda *_: self.on_order_changed())
        self.list.model().layoutChanged.connect(lambda *_: self.on_order_changed())

        def tbtn(text, tip, slot, shortcut=None):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip + (f"  ({QKeySequence(shortcut).toString(QKeySequence.NativeText)})" if shortcut else ""))
            b.clicked.connect(slot)
            b.setToolButtonStyle(Qt.ToolButtonTextOnly)
            if shortcut:
                a = QAction(self)
                a.setShortcut(shortcut)
                a.triggered.connect(slot)
                self.addAction(a)
            return b

        self.btn_add = tbtn("Add Scans…", "Add image files", self.choose_files, QKeySequence.Open)
        self.btn_add_folder = tbtn("Add Folder…", "Add every image in a folder", self.choose_folder)
        self.btn_rot_l = tbtn("⟲", "Rotate selected pages left", lambda: self.rotate_selected(-90), "Ctrl+[")
        self.btn_rot_r = tbtn("⟳", "Rotate selected pages right", lambda: self.rotate_selected(90), "Ctrl+]")
        self.btn_remove = tbtn("Remove", "Remove selected pages from the stack", self.remove_selected, QKeySequence.Delete)
        a = QAction(self)
        a.setShortcut(Qt.Key_Backspace)
        a.triggered.connect(self.remove_selected)
        self.addAction(a)
        self.btn_sort = tbtn("Sort A→Z", "Put pages back in file-name order", self.sort_by_name)
        self.btn_reverse = tbtn("Reverse", "Reverse the whole order", self.reverse_order)
        self.btn_duplex = tbtn(
            "Merge Fronts/Backs",
            "For double-sided originals on a one-sided scanner: scan all fronts, flip the stack, "
            "scan all backs. This puts them back as front 1, back 1, front 2, back 2…",
            self.interleave,
        )
        self.btn_clear = tbtn("Clear", "Start over with an empty stack", self.clear_all)

        bar1 = QHBoxLayout()
        for b in (self.btn_add, self.btn_add_folder):
            bar1.addWidget(b)
        bar1.addStretch(1)
        for b in (self.btn_rot_l, self.btn_rot_r, self.btn_remove):
            bar1.addWidget(b)
        bar2 = QHBoxLayout()
        for b in (self.btn_sort, self.btn_reverse, self.btn_duplex):
            bar2.addWidget(b)
        bar2.addStretch(1)
        bar2.addWidget(self.btn_clear)

        self.count_label = QLabel()
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(10, 10, 4, 10)
        lv.addLayout(bar1)
        lv.addLayout(bar2)
        lv.addWidget(self.list, 1)
        lv.addWidget(self.count_label)

        # Middle: preview
        self.preview = PagePreview()
        self.preview_caption = QLabel()
        self.preview_caption.setAlignment(Qt.AlignCenter)
        self.preview_caption.setWordWrap(True)
        mid = QWidget()
        mv = QVBoxLayout(mid)
        mv.setContentsMargins(4, 10, 4, 10)
        mv.addWidget(self.preview, 1)
        mv.addWidget(self.preview_caption)

        # Right: settings
        self.paper = QComboBox()
        for k, label in engine.PAPER_LABELS.items():
            self.paper.addItem(label, k)
        self.orientation = QComboBox()
        for label, k in (("Automatic (match each scan)", "auto"), ("Always portrait", "portrait"),
                         ("Always landscape", "landscape")):
            self.orientation.addItem(label, k)
        self.sizing = QComboBox()
        for label, k in (("Fit whole scan on page", "fit"), ("Fill page (trim edges)", "fill"),
                         ("Actual size (uses scan DPI)", "actual")):
            self.sizing.addItem(label, k)
        self.sizing.setToolTip(
            "Fit: the whole scan, as large as it fits.\n"
            "Fill: covers the page; trims what overhangs.\n"
            "Actual size: a 4×6 photo stays 4×6 (if the scan recorded its DPI; "
            "otherwise it's fitted)."
        )
        self.margin = QComboBox()
        for label, v in (("None", 0.0), ("¼ inch", 0.25), ("½ inch", 0.5)):
            self.margin.addItem(label, v)

        page_box = QGroupBox("Page")
        pf = QFormLayout(page_box)
        pf.addRow("Paper:", self.paper)
        pf.addRow("Orientation:", self.orientation)
        pf.addRow("Scan size:", self.sizing)
        pf.addRow("Margin:", self.margin)

        self.color = QComboBox()
        for label, k in (("Automatic (grey unless there's colour)", "auto"), ("Colour", "color"),
                         ("Greyscale", "gray"), ("Black & white (smallest)", "bw")):
            self.color.addItem(label, k)
        self.color.setToolTip(
            "Automatic keeps colour pages in colour and stores plain\n"
            "black-and-grey pages as greyscale.\n\n"
            "Black & white is pure black ink on white, like a fax.\n"
            "Text pages shrink to a tiny fraction of the size,\n"
            "but photos turn into blotches — use it for documents."
        )
        self.bw_auto = QCheckBox("Automatic threshold")
        self.bw_auto.setToolTip("Works out where ink ends and paper begins on each page.")
        self.bw_slider = QSlider(Qt.Horizontal)
        self.bw_slider.setRange(60, 230)
        self.bw_slider.setToolTip("Move right to keep faint writing; left to drop smudges and show-through.")
        self.bw_value = QLabel()
        bw_row = QHBoxLayout()
        bw_row.addWidget(QLabel("Lighter"))
        bw_row.addWidget(self.bw_slider, 1)
        bw_row.addWidget(QLabel("Darker"))
        self.bw_widget = QWidget()
        bwl = QVBoxLayout(self.bw_widget)
        bwl.setContentsMargins(0, 0, 0, 0)
        bwl.addWidget(self.bw_auto)
        bwl.addLayout(bw_row)
        self.clean = QCheckBox("Clean up paper background")
        self.clean.setToolTip("Turns greyish or yellowed paper white. Photos are left alone.")

        look_box = QGroupBox("Look")
        lf = QFormLayout(look_box)
        lf.addRow("Colour:", self.color)
        lf.addRow("", self.bw_widget)
        lf.addRow("", self.clean)

        self.preset = QComboBox()
        for name in engine.PRESETS:
            self.preset.addItem(name, name)
        self.preset.addItem("Custom", "custom")
        self.dpi = QComboBox()
        for d in (100, 150, 200, 300, 400, 600):
            self.dpi.addItem(f"{d} dpi", d)
        self.dpi.setToolTip("150 is fine for reading on screen, 200 prints well, 300 for fine detail or photos.\n"
                            "Pages are never stored at more detail than the scan actually has.")
        self.quality = QSlider(Qt.Horizontal)
        self.quality.setRange(30, 95)
        self.quality_label = QLabel()
        self.quality_label.setMinimumWidth(28)
        qrow = QHBoxLayout()
        qrow.addWidget(self.quality, 1)
        qrow.addWidget(self.quality_label)
        self.quality_row = QWidget()
        self.quality_row.setLayout(qrow)
        qrow.setContentsMargins(0, 0, 0, 0)
        self.estimate_label = QLabel("—")
        f = self.estimate_label.font()
        f.setBold(True)
        self.estimate_label.setFont(f)

        size_box = QGroupBox("File size")
        sf = QFormLayout(size_box)
        sf.addRow("Preset:", self.preset)
        sf.addRow("Resolution:", self.dpi)
        sf.addRow("JPEG quality:", self.quality_row)
        sf.addRow("Estimated PDF:", self.estimate_label)

        self.open_after = QCheckBox("Open the PDF when it's saved")
        self.save_btn = QPushButton("Save PDF…")
        self.save_btn.setDefault(True)
        self.save_btn.setMinimumHeight(36)
        self.save_btn.clicked.connect(self.save_pdf)
        sa = QAction(self)
        sa.setShortcut(QKeySequence.Save)
        sa.triggered.connect(self.save_pdf)
        self.addAction(sa)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self.cancel_build)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.RichText)
        self.status.setOpenExternalLinks(False)
        self.status.linkActivated.connect(self.on_status_link)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(4, 10, 10, 10)
        for w in (page_box, look_box, size_box):
            rv.addWidget(w)
        rv.addStretch(1)
        rv.addWidget(self.open_after)
        rv.addWidget(self.save_btn)
        prow = QHBoxLayout()
        prow.addWidget(self.progress, 1)
        prow.addWidget(self.cancel_btn)
        rv.addLayout(prow)
        rv.addWidget(self.status)
        right.setMinimumWidth(330)
        right.setMaximumWidth(420)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(mid)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        split.setStretchFactor(2, 0)
        split.setSizes([400, 560, 340])
        self.setCentralWidget(split)

        # Changing settings updates the preview and the size estimate.
        for combo in (self.paper, self.orientation, self.sizing, self.margin, self.color, self.dpi):
            combo.currentIndexChanged.connect(self.on_settings_changed)
        self.preset.currentIndexChanged.connect(self.on_preset_changed)
        self.dpi.activated.connect(lambda *_: self.mark_custom())
        self.quality.valueChanged.connect(self.on_quality_changed)
        self.quality.sliderReleased.connect(self.mark_custom)
        self.bw_auto.toggled.connect(self.on_settings_changed)
        self.bw_slider.valueChanged.connect(self.on_settings_changed)
        self.clean.toggled.connect(self.on_settings_changed)

    # ------------------------------------------------------------------ settings
    def current_settings(self) -> Settings:
        return Settings(
            paper=self.paper.currentData(),
            orientation=self.orientation.currentData(),
            sizing=self.sizing.currentData(),
            margin_in=self.margin.currentData(),
            color=self.color.currentData(),
            bw_threshold=0 if self.bw_auto.isChecked() else self.bw_slider.value(),
            dpi=self.dpi.currentData(),
            quality=self.quality.value(),
            clean_background=self.clean.isChecked(),
        )

    def _set_combo(self, combo: QComboBox, value):
        i = combo.findData(value)
        if i >= 0:
            combo.setCurrentIndex(i)

    def _load_settings(self):
        st = self.settings_store
        d = Settings()
        widgets = [self.paper, self.orientation, self.sizing, self.margin, self.color, self.dpi, self.preset,
                   self.quality, self.bw_auto, self.bw_slider, self.clean]
        for w in widgets:
            w.blockSignals(True)
        self._set_combo(self.paper, st.value("paper", d.paper))
        self._set_combo(self.orientation, st.value("orientation", d.orientation))
        self._set_combo(self.sizing, st.value("sizing", d.sizing))
        self._set_combo(self.margin, float(st.value("margin", d.margin_in)))
        self._set_combo(self.color, st.value("color", d.color))
        self._set_combo(self.dpi, int(st.value("dpi", d.dpi)))
        self._set_combo(self.preset, st.value("preset", "Balanced"))
        self.quality.setValue(int(st.value("quality", d.quality)))
        self.bw_auto.setChecked(st.value("bw_auto", "true") in (True, "true"))
        self.bw_slider.setValue(int(st.value("bw_threshold", 150)))
        self.clean.setChecked(st.value("clean", "true") in (True, "true"))
        self.open_after.setChecked(st.value("open_after", "true") in (True, "true"))
        for w in widgets:
            w.blockSignals(False)
        geo = st.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        self.quality_label.setText(str(self.quality.value()))
        self.update_controls()

    def _save_settings(self):
        st = self.settings_store
        s = self.current_settings()
        st.setValue("paper", s.paper)
        st.setValue("orientation", s.orientation)
        st.setValue("sizing", s.sizing)
        st.setValue("margin", s.margin_in)
        st.setValue("color", s.color)
        st.setValue("dpi", s.dpi)
        st.setValue("quality", s.quality)
        st.setValue("preset", self.preset.currentData())
        st.setValue("bw_auto", self.bw_auto.isChecked())
        st.setValue("bw_threshold", self.bw_slider.value())
        st.setValue("clean", self.clean.isChecked())
        st.setValue("open_after", self.open_after.isChecked())
        st.setValue("geometry", self.saveGeometry())

    def update_controls(self):
        bw = self.color.currentData() == "bw"
        self.bw_widget.setVisible(bw)
        self.bw_slider.setEnabled(not self.bw_auto.isChecked())
        self.clean.setVisible(not bw)
        self.quality_row.setEnabled(not bw)
        self.quality_row.setToolTip("Black & white pages don't use JPEG, so quality doesn't apply." if bw else
                                    "Lower = smaller file. Around 60–75 is hard to tell apart from the scan.")

    def on_settings_changed(self, *_):
        self.update_controls()
        self.schedule_preview()
        self.schedule_estimate()

    def on_quality_changed(self, v):
        self.quality_label.setText(str(v))
        if not self.quality.isSliderDown():
            self.mark_custom()
        self.on_settings_changed()

    def on_preset_changed(self, *_):
        name = self.preset.currentData()
        if name in engine.PRESETS:
            dpi, q = engine.PRESETS[name]
            for w in (self.dpi, self.quality):
                w.blockSignals(True)
            self._set_combo(self.dpi, dpi)
            self.quality.setValue(q)
            self.quality_label.setText(str(q))
            for w in (self.dpi, self.quality):
                w.blockSignals(False)
        self.on_settings_changed()

    def mark_custom(self):
        name = self.preset.currentData()
        if name in engine.PRESETS and engine.PRESETS[name] != (self.dpi.currentData(), self.quality.value()):
            self.preset.blockSignals(True)
            self._set_combo(self.preset, "custom")
            self.preset.blockSignals(False)
        self.on_settings_changed()

    def _start(self, job: Job, slot):
        """Run a background job; drop our reference to it once it reports back."""
        sig = job.signals
        self._jobs.add(sig)
        sig.done.connect(slot)
        sig.done.connect(lambda *_: self._jobs.discard(sig))
        self.pool.start(job)

    # ------------------------------------------------------------------ the stack
    def ordered_pages(self) -> list[PageSource]:
        return [self.pages[self.list.item(i).data(Qt.UserRole)] for i in range(self.list.count())]

    def selected_keys(self) -> list[int]:
        return [it.data(Qt.UserRole) for it in self.list.selectedItems()]

    def choose_files(self):
        start = self.settings_store.value("last_dir", os.path.expanduser("~"))
        exts = " ".join(f"*{e}" for e in sorted(engine.IMAGE_EXTENSIONS))
        files, _ = QFileDialog.getOpenFileNames(self, "Add scans", start, f"Images ({exts});;All files (*)")
        if files:
            self.settings_store.setValue("last_dir", os.path.dirname(files[0]))
            self.add_paths(sorted(files, key=engine.natural_key))

    def choose_folder(self):
        start = self.settings_store.value("last_dir", os.path.expanduser("~"))
        d = QFileDialog.getExistingDirectory(self, "Add a folder of scans", start)
        if d:
            self.settings_store.setValue("last_dir", d)
            self.add_paths([d])

    def add_paths(self, paths: list[str], row: int = -1):
        files = engine.expand_paths(paths)
        if not files:
            if paths:
                self.set_status("No images found there.")
            return
        was_empty = self.list.count() == 0
        insert_at = self.list.count() if row < 0 else row
        for f in files:
            for src in engine.sources_for(f):
                key = self.next_key
                self.next_key += 1
                self.pages[key] = src
                item = QListWidgetItem(self._placeholder_icon(), src.name)
                item.setData(Qt.UserRole, key)
                item.setToolTip(src.path + (f"\nPage {src.frame + 1} of {src.n_frames}" if src.n_frames > 1 else ""))
                item.setFlags(item.flags() & ~Qt.ItemIsDropEnabled)
                item.setTextAlignment(Qt.AlignHCenter | Qt.AlignTop)
                self.list.insertItem(insert_at, item)
                insert_at += 1
                self.load_thumb(key)
        if was_empty:
            self.list.setCurrentRow(0)
            self.last_output = None
        self.set_status("")
        self.on_order_changed()

    def _placeholder_icon(self) -> QIcon:
        pm = QPixmap(THUMB * 3 // 4, THUMB)
        pm.fill(QColor(0, 0, 0, 0))
        p = QPainter(pm)
        p.fillRect(pm.rect().adjusted(2, 2, -2, -2), QColor(255, 255, 255, 120))
        p.end()
        return QIcon(pm)

    def load_thumb(self, key: int):
        src = self.pages[key]
        cache_key = (src.path, src.frame)
        if cache_key in self.thumb_cache:
            self._apply_thumb(key)
            return

        def work(s=src):
            return pil_to_qimage(engine.open_page(PageSource(s.path, s.frame, 0, s.n_frames), max_side=THUMB * 2))

        self._start(Job(key, work), self.on_thumb)

    def on_thumb(self, key, result):
        src = self.pages.get(key)
        if src is None:
            return
        if isinstance(result, Exception):
            item = self._item_for(key)
            if item:
                item.setText("⚠︎ " + src.name)
                item.setToolTip(f"{src.path}\nCan't open this file: {result}")
            return
        self.thumb_cache[(src.path, src.frame)] = result
        self._apply_thumb(key)

    def _apply_thumb(self, key: int):
        src = self.pages[key]
        q = self.thumb_cache[(src.path, src.frame)]
        pm = QPixmap.fromImage(q)
        if src.rotation:
            pm = pm.transformed(QTransform().rotate(src.rotation), Qt.SmoothTransformation)
        item = self._item_for(key)
        if item:
            item.setIcon(QIcon(pm))

    def _item_for(self, key: int):
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.data(Qt.UserRole) == key:
                return it
        return None

    def rotate_selected(self, deg: int):
        keys = self.selected_keys()
        for k in keys:
            src = self.pages[k]
            src.rotation = (src.rotation + deg) % 360
            if (src.path, src.frame) in self.thumb_cache:
                self._apply_thumb(k)
        if keys:
            self.schedule_preview()
            self.schedule_estimate()

    def remove_selected(self):
        rows = sorted((self.list.row(it) for it in self.list.selectedItems()), reverse=True)
        for r in rows:
            it = self.list.takeItem(r)
            self.pages.pop(it.data(Qt.UserRole), None)
        if rows and self.list.count():
            self.list.setCurrentRow(min(rows[-1], self.list.count() - 1))
        self.on_order_changed()

    def _reorder(self, keys: list[int]):
        cur = self.list.currentItem()
        cur_key = cur.data(Qt.UserRole) if cur else None
        items = {}
        while self.list.count():
            it = self.list.takeItem(0)
            items[it.data(Qt.UserRole)] = it
        for k in keys:
            self.list.addItem(items[k])
        if cur_key in items:
            self.list.setCurrentItem(items[cur_key])
        self.on_order_changed()

    def current_keys(self) -> list[int]:
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]

    def sort_by_name(self):
        keys = self.current_keys()
        keys.sort(key=lambda k: (engine.natural_key(self.pages[k].path), self.pages[k].frame))
        self._reorder(keys)

    def reverse_order(self):
        self._reorder(list(reversed(self.current_keys())))

    def interleave(self):
        keys = self.current_keys()
        if len(keys) < 2:
            return
        if len(keys) % 2:
            r = QMessageBox.question(
                self, "Odd number of pages",
                f"There are {len(keys)} pages, so fronts and backs don't pair up evenly.\n\n"
                "Merge anyway? The extra page will be treated as a front.")
            if r != QMessageBox.Yes:
                return
        self._reorder(engine.interleave_duplex(keys))
        self.set_status("Fronts and backs merged — flip through a few pages to check. "
                        "<b>Sort A→Z</b> puts them back the way they were scanned.")

    def clear_all(self):
        if self.list.count() and self.build_job is None:
            self.list.clear()
            self.pages.clear()
            self.thumb_cache.clear()
            self.on_order_changed()
            self.set_status("")

    def on_order_changed(self):
        self.update_state()
        self.schedule_preview()
        self.schedule_estimate()

    def update_state(self):
        n = self.list.count()
        busy = self.build_job is not None
        self.count_label.setText("No pages yet" if n == 0 else f"{n} page{'s' if n != 1 else ''}")
        self.save_btn.setEnabled(n > 0 and not busy)
        for b in (self.btn_rot_l, self.btn_rot_r, self.btn_remove, self.btn_sort, self.btn_reverse,
                  self.btn_duplex, self.btn_clear):
            b.setEnabled(n > 0 and not busy)
        for b in (self.btn_add, self.btn_add_folder):
            b.setEnabled(not busy)
        self.list.set_editable(not busy)

    # ------------------------------------------------------------------ preview + estimate
    def schedule_preview(self):
        self.preview_timer.start()

    def schedule_estimate(self):
        self.estimate_label.setText("…" if self.list.count() else "—")
        self.estimate_timer.start()

    def refresh_preview(self):
        item = self.list.currentItem()
        if item is None:
            self.preview.show_page(None, None, "Add some scans to get started" if not self.list.count()
                                   else "Select a page to preview it")
            self.preview_caption.setText("")
            return
        key = item.data(Qt.UserRole)
        src = self.pages[key]
        snap = PageSource(src.path, src.frame, src.rotation, src.n_frames)
        s = self.current_settings()
        self.preview_token += 1
        token = self.preview_token
        side = int(max(self.preview.width(), self.preview.height()) * self.devicePixelRatioF())
        side = max(600, min(1600, side))

        def work():
            rp = engine.render_page(snap, s, preview_side=side)
            return rp, pil_to_qimage(rp.image)

        row = self.list.row(item)
        self._start(Job(token, work), lambda t, r: self.on_preview(t, r, row))

    def on_preview(self, token, result, row):
        if token != self.preview_token:
            return
        if isinstance(result, Exception):
            self.preview.show_page(None, None, f"Can't open this file:\n{result}")
            self.preview_caption.setText("")
            return
        rp, q = result
        self.preview.show_page(q, rp.layout)
        kind = {"color": "Colour", "gray": "Greyscale", "bw": "Black & white"}[rp.kind]
        extra = f" · threshold {rp.threshold}" if rp.threshold is not None else ""
        self.preview_caption.setText(f"Page {row + 1} of {self.list.count()} · {kind}{extra}")

    def refresh_estimate(self):
        pages = [PageSource(p.path, p.frame, p.rotation, p.n_frames) for p in self.ordered_pages()]
        if not pages:
            self.estimate_label.setText("—")
            return
        s = self.current_settings()
        self.estimate_token += 1
        token = self.estimate_token
        self._start(Job(token, engine.estimate_size, pages, s, 4, lambda: token != self.estimate_token),
                    self.on_estimate)

    def on_estimate(self, token, result):
        if token != self.estimate_token:
            return
        if isinstance(result, Exception) or result is None:
            self.estimate_label.setText("—")
        else:
            approx = "≈ " if self.list.count() > 4 else ""
            self.estimate_label.setText(approx + human_size(result))

    # ------------------------------------------------------------------ saving
    def default_output_path(self) -> str:
        pages = self.ordered_pages()
        folder = os.path.dirname(pages[0].path) if pages else os.path.expanduser("~/Documents")
        name = os.path.basename(folder.rstrip(os.sep)) or "Scans"
        return os.path.join(folder, f"{name}.pdf")

    def save_pdf(self):
        if not self.list.count() or self.build_job is not None:
            return
        start = self.last_output or self.default_output_path()
        path, _ = QFileDialog.getSaveFileName(self, "Save PDF", start, "PDF (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        sources = {os.path.abspath(p.path) for p in self.ordered_pages()}
        if os.path.abspath(path) in sources:
            QMessageBox.warning(self, APP_NAME, "That would overwrite one of your scans. Pick another name.")
            return
        pages = [PageSource(p.path, p.frame, p.rotation, p.n_frames) for p in self.ordered_pages()]
        self.build_job = BuildJob(pages, path, self.current_settings())
        self.build_job.signals.progress.connect(self.on_build_progress)
        self.build_job.signals.finished.connect(self.on_build_finished)
        self.progress.setRange(0, len(pages))
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.cancel_btn.setVisible(True)
        self.set_status("Making the PDF…")
        self.update_state()
        self.pool.start(self.build_job)

    def cancel_build(self):
        if self.build_job:
            self.build_job.cancel.set()
            self.set_status("Cancelling…")

    def on_build_progress(self, i, n, name):
        self.progress.setValue(i)
        self.set_status(f"Page {i} of {n}: {name}")

    def on_build_finished(self, result):
        self.build_job = None
        self.progress.setVisible(False)
        self.cancel_btn.setVisible(False)
        self.update_state()
        if isinstance(result, engine.Cancelled):
            self.set_status("Cancelled. Nothing was saved.")
            return
        if isinstance(result, Exception):
            self.set_status("")
            QMessageBox.critical(self, APP_NAME, f"The PDF couldn't be saved.\n\n{result}")
            return
        self.last_output = result.path
        kinds = ", ".join(f"{v} {({'color': 'colour', 'gray': 'greyscale', 'bw': 'black & white'})[k]}"
                          for k, v in result.kinds.items())
        msg = (f"Saved <b>{os.path.basename(result.path)}</b> — {result.pages} pages, "
               f"{human_size(result.size)} ({kinds}).<br>"
               f"<a href='open'>Open</a> &nbsp; <a href='reveal'>Show in Finder</a>")
        if result.skipped:
            msg += "<br><br>⚠︎ Skipped because they couldn't be opened: " + ", ".join(
                n for n, _ in result.skipped[:6]) + (" …" if len(result.skipped) > 6 else "")
        self.set_status(msg)
        if self.open_after.isChecked():
            QDesktopServices.openUrl(QUrl.fromLocalFile(result.path))

    def on_status_link(self, link):
        if not self.last_output or not os.path.exists(self.last_output):
            return
        if link == "open":
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.last_output))
        elif link == "reveal":
            if sys.platform == "darwin":
                os.spawnlp(os.P_NOWAIT, "open", "open", "-R", self.last_output)
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(self.last_output)))

    def set_status(self, text: str):
        self.status.setText(text)

    # ------------------------------------------------------------------ window
    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "preview_timer"):
            self.preview_timer.start(250)

    def closeEvent(self, e):
        if self.build_job is not None:
            r = QMessageBox.question(self, APP_NAME, "A PDF is still being made. Stop and quit?")
            if r != QMessageBox.Yes:
                e.ignore()
                return
            self.build_job.cancel.set()
            self.pool.waitForDone(5000)
        self._save_settings()
        super().closeEvent(e)


class App(QApplication):
    """Catches files dropped on the Dock icon or opened with Finder's 'Open With'."""

    def __init__(self, argv):
        super().__init__(argv)
        self.window: MainWindow | None = None
        self.pending: list[str] = []

    def event(self, e):
        if e.type() == QEvent.FileOpen:
            path = e.file()
            if self.window:
                self.window.add_paths([path])
            else:
                self.pending.append(path)
            return True
        return super().event(e)


def main():
    if "--self-test" in sys.argv:
        with tempfile.TemporaryDirectory() as d:
            problems = engine.self_test(d)
        if problems:
            print("SELF-TEST FAILED:\n  " + "\n  ".join(problems))
            sys.exit(1)
        print(f"{APP_NAME} {app_version()} self-test passed (G4: {engine.HAVE_G4}, HEIC: {engine.HAVE_HEIF})")
        return

    app = App(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("JRH Films")
    app.setApplicationVersion(app_version())
    icon_path = resource("scanstack.svg")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    w = MainWindow()
    app.window = w
    w.show()
    args = [a for a in sys.argv[1:] if not a.startswith("-psn") and os.path.exists(a)]
    if args or app.pending:
        w.add_paths(args + app.pending)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
