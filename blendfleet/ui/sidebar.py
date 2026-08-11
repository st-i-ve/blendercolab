"""The navigation sidebar: brand, one item per page, animated collapse.

The shape the reference design uses -- a floating rounded panel inset from
the window edge, collapsing to a narrow pill of circular icon buttons -- and
the piece that changes what BlendFleet IS more than any restyle: until now
the app had no navigation at all, just one scrolling column with everything
on it, and a 320px rail holding CONTENT (the per-account cards) where the
navigation should be. Those cards move onto the Dashboard page; this takes
their place.

Two Qt facts shape the code below:

  - A QPushButton has ONE text slot, so it cannot lay out an icon, a
    stretching label and a trailing count pill. NavButton is therefore a
    QAbstractButton with a real QHBoxLayout inside it -- it keeps every
    button behaviour (checkable, clicked, keyboard activation) and gains a
    layout.
  - A plain QWidget subclass ignores stylesheet backgrounds unless it is
    told not to, so NavButton sets WA_StyledBackground. Without that, the
    active-item accent wash simply does not paint.

The brand mark is theme.brand_icon() -- the app's own logo, tinted to the
active accent -- not an icon borrowed from the reference mockup.
"""
from __future__ import annotations

from PySide6.QtCore import (Property, QEasingCurve, QPropertyAnimation, QSize,
                            Qt, Signal)
from PySide6.QtGui import QIcon, QTransform
from PySide6.QtWidgets import (QAbstractButton, QHBoxLayout, QLabel,
                               QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from blendfleet.ui.theme import (brand_icon, current_accent, current_theme,
                                 icon, mono_font, theme_signal, tracked_font)

EXPANDED_WIDTH = 216
COLLAPSED_WIDTH = 68
COLLAPSE_MS = 220
NAV_ICON_SIZE = 16
BRAND_SIZE = 28
# A collapsed nav item is a circle, so its width and height must match and
# the radius must be exactly half -- derived here rather than written as
# three separate numbers that can drift apart.
COLLAPSED_ITEM = 44


class NavButton(QAbstractButton):
    """One navigation destination: icon, tracked-caps label, count pill.

    Checkable and exclusive-by-convention (Sidebar drives the checked state
    across the set) rather than in a QButtonGroup, because the group would
    also want to own the click routing that Sidebar.page_selected already
    provides.
    """

    def __init__(self, page: str, label: str, icon_name: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.page = page
        self._icon_name = icon_name
        self.setObjectName("navButton")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(label)
        # Without this, #navButton's stylesheet background never paints:
        # QWidget subclasses only honour QSS backgrounds when told to.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Fixed)
        self.setFixedHeight(COLLAPSED_ITEM)
        # Tracked explicitly rather than inferred from whether the label is
        # currently visible: before the sidebar is first shown, every child
        # reports isVisible() == False, so inferring it would report every
        # freshly built item as collapsed and hide its count pill for good.
        self._is_collapsed = False
        self._count: int | None = 0

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 0, 12, 0)
        row.setSpacing(11)

        self.icon_label = QLabel()
        self.icon_label.setFixedSize(NAV_ICON_SIZE, NAV_ICON_SIZE)
        row.addWidget(self.icon_label)

        self.text_label = QLabel(label)
        self.text_label.setFont(tracked_font(8))
        row.addWidget(self.text_label, 1)

        # Mono, because it is a count -- the same instrument-panel rule the
        # rest of the app follows for machine values.
        self.pill = QLabel("0")
        self.pill.setObjectName("navPill")
        self.pill.setFont(mono_font(8))
        self.pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.pill)

        self.refresh_accent()

    # ---- painting -------------------------------------------------------
    def paintEvent(self, event) -> None:   # noqa: N802 -- Qt override
        # Everything visible is a child widget or a stylesheet background,
        # so there is nothing to paint here -- but QAbstractButton is
        # abstract precisely because it demands this method exist.
        pass

    def sizeHint(self) -> QSize:           # noqa: N802 -- Qt override
        return QSize(EXPANDED_WIDTH - 24, COLLAPSED_ITEM)

    # ---- state ----------------------------------------------------------
    def refresh_accent(self) -> None:
        """Re-tint the icon and re-read the active/inactive text colour.

        Called on construction and again whenever theme_signal fires: an
        icon pixmap painted once from current_accent() does not repaint
        itself when the accent changes (see theme.theme_signal's docstring).
        """
        active = self.isChecked()
        colour = current_accent().base if active else current_theme().ink_2
        self.icon_label.setPixmap(
            icon(self._icon_name, colour, NAV_ICON_SIZE).pixmap(
                NAV_ICON_SIZE, NAV_ICON_SIZE))
        self.text_label.setStyleSheet(f"color: {colour};")

    def setChecked(self, checked: bool) -> None:   # noqa: N802 -- Qt override
        super().setChecked(checked)
        # The stylesheet selects on a dynamic property, which Qt does not
        # re-evaluate on its own -- unpolish/polish is what forces it.
        self.setProperty("active", "true" if checked else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.refresh_accent()

    def set_count(self, count: int | None) -> None:
        """`None` hides the pill entirely -- a destination with nothing
        countable (Settings) must not show a permanent, meaningless 0."""
        self._count = count
        if count is None:
            self.pill.setText("")
            self.pill.hide()
            return
        self.pill.setText(str(count))
        self.pill.setVisible(not self._is_collapsed)

    def set_collapsed(self, collapsed: bool) -> None:
        self._is_collapsed = collapsed
        self.text_label.setVisible(not collapsed)
        self.pill.setVisible(not collapsed and self._count is not None)
        layout = self.layout()
        if collapsed:
            layout.setContentsMargins(0, 0, 0, 0)
            self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        else:
            layout.setContentsMargins(12, 0, 12, 0)
            self.icon_label.setAlignment(Qt.AlignmentFlag.AlignLeft
                                         | Qt.AlignmentFlag.AlignVCenter)
        # Collapsed items are circles, not rounded rectangles -- a
        # stylesheet rule keyed on this property, since QSS cannot read a
        # Python attribute.
        self.setProperty("collapsed", "true" if collapsed else "false")
        self.style().unpolish(self)
        self.style().polish(self)


class Sidebar(QWidget):
    """Brand, the nav items, a collapse toggle and a footer.

    Emits `page_selected` with the page key of whichever item was clicked;
    it does NOT switch pages itself. The QStackedWidget lives in Dashboard,
    and a navigation widget that reaches into it would make the two
    impossible to test apart.
    """

    page_selected = Signal(str)
    collapsed_changed = Signal(bool)

    # (page key, label, bundled icon name)
    PAGES = [
        ("dashboard", "Dashboard", "activity"),
        ("files", "Files", "folder-open"),
        ("instances", "Instances", "cpu"),
        ("logs", "Logs", "triangle-alert"),
        ("settings", "Settings", "settings"),
    ]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(EXPANDED_WIDTH)
        self._collapsed = False

        v = QVBoxLayout(self)
        v.setContentsMargins(12, 16, 12, 14)
        v.setSpacing(4)

        # ---- brand ----
        brand = QHBoxLayout()
        brand.setContentsMargins(4, 0, 4, 12)
        brand.setSpacing(9)
        self.brand_mark = QLabel()
        self.brand_mark.setFixedSize(BRAND_SIZE, BRAND_SIZE)
        brand.addWidget(self.brand_mark)
        self.brand_name = QLabel("BlendFleet")
        self.brand_name.setObjectName("brandName")
        self.brand_name.setFont(tracked_font(9, tracking=10.0))
        brand.addWidget(self.brand_name, 1)
        v.addLayout(brand)

        # ---- nav ----
        self.buttons: dict[str, NavButton] = {}
        for page, label, icon_name in self.PAGES:
            button = NavButton(page, label, icon_name)
            button.clicked.connect(
                lambda _checked=False, p=page: self.page_selected.emit(p))
            self.buttons[page] = button
            v.addWidget(button)

        v.addStretch(1)

        # ---- collapse ----
        self.collapse_btn = QPushButton("  Collapse")
        self.collapse_btn.setObjectName("collapseButton")
        self.collapse_btn.setFont(tracked_font(7, tracking=14.0))
        self.collapse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.collapse_btn.setFixedHeight(COLLAPSED_ITEM - 8)
        self.collapse_btn.clicked.connect(self.toggle_collapsed)
        v.addWidget(self.collapse_btn)

        self.footer = QLabel("")
        self.footer.setObjectName("sidebarFooter")
        self.footer.setFont(mono_font(7))
        self.footer.setContentsMargins(6, 8, 6, 0)
        v.addWidget(self.footer)

        # The width animation drives setFixedWidth via the property below,
        # so minimum and maximum move together and no intermediate frame can
        # be laid out at a width neither side agreed on.
        self._animation = QPropertyAnimation(self, b"barWidth", self)
        self._animation.setDuration(COLLAPSE_MS)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutCubic)

        self._accent_connection = theme_signal.changed.connect(
            self.refresh_accent)
        self.refresh_accent()
        self.set_active("dashboard")

    # ---- animated width -------------------------------------------------
    def _get_bar_width(self) -> int:
        return self.width()

    def _set_bar_width(self, value: int) -> None:
        self.setFixedWidth(value)

    barWidth = Property(int, _get_bar_width, _set_bar_width)

    # ---- public API -----------------------------------------------------
    def set_active(self, page: str) -> None:
        for key, button in self.buttons.items():
            button.setChecked(key == page)

    def set_count(self, page: str, count: int | None) -> None:
        button = self.buttons.get(page)
        if button is not None:
            button.set_count(count)

    def set_footer(self, text: str) -> None:
        self.footer.setText(text)

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self._collapsed)

    def set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = collapsed
        # Text hides FIRST when collapsing and LAST when expanding, so a
        # label is never briefly drawn wider than the panel containing it.
        if collapsed:
            self._apply_collapsed_children(True)
        self._animation.stop()
        self._animation.setStartValue(self.width())
        self._animation.setEndValue(
            COLLAPSED_WIDTH if collapsed else EXPANDED_WIDTH)
        if not collapsed:
            self._animation.finished.connect(self._expand_children_once)
        self._animation.start()
        self.collapsed_changed.emit(collapsed)

    def _expand_children_once(self) -> None:
        self._animation.finished.disconnect(self._expand_children_once)
        self._apply_collapsed_children(False)

    def _apply_collapsed_children(self, collapsed: bool) -> None:
        self.brand_name.setVisible(not collapsed)
        self.footer.setVisible(not collapsed)
        self.collapse_btn.setText("" if collapsed else "  Collapse")
        self.collapse_btn.setToolTip(
            "Expand sidebar" if collapsed else "Collapse sidebar")
        for button in self.buttons.values():
            button.set_collapsed(collapsed)
        # The side margin stays 12 in BOTH states, and that is what makes a
        # collapsed item square: COLLAPSED_WIDTH - 2*12 == COLLAPSED_ITEM,
        # i.e. 68 - 24 == 44. Dropping the margin to 0 when collapsed would
        # give a 68x44 "circle" -- an oval.
        self.layout().setContentsMargins(12, 16, 12, 14)
        self.setProperty("collapsed", "true" if collapsed else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self._sync_collapse_icon()

    def refresh_accent(self) -> None:
        self.brand_mark.setPixmap(
            brand_icon(current_accent().base, BRAND_SIZE).pixmap(
                BRAND_SIZE, BRAND_SIZE))
        for button in self.buttons.values():
            button.refresh_accent()
        self._sync_collapse_icon()

    def _sync_collapse_icon(self) -> None:
        """One chevron asset, turned around when collapsed -- rather than a
        second mirrored SVG that could drift from the first."""
        pixmap = icon("chevron-left", current_theme().ink_3, 13).pixmap(13, 13)
        if self._collapsed:
            pixmap = pixmap.transformed(QTransform().rotate(180))
        self.collapse_btn.setIcon(QIcon(pixmap))

    def disconnect_theme_signal(self) -> None:
        """Idempotent, for the same reason InstanceCard's is -- see
        Dashboard.closeEvent: a repeat disconnect in PySide6 warns and
        silently does nothing rather than raising, so the handle is tracked
        and cleared instead of being disconnected twice."""
        if self._accent_connection is not None:
            theme_signal.changed.disconnect(self._accent_connection)
            self._accent_connection = None
