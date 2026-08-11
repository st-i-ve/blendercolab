"""A layout that wraps its children onto as many rows as they need.

Qt has no equivalent of CSS grid's `repeat(auto-fill, minmax(320px, 1fr))`,
which is what the reference design uses for the instance-card grid: as many
cards per row as fit, reflowing as the window resizes. QGridLayout cannot do
it (the column count is fixed when you add the items) and QHBoxLayout cannot
do it (it never wraps). This is the standard Qt "flow layout" pattern --
measure each item, break to a new row when the next one would overflow --
kept in its own module so the card grid is the only thing that has to know
about it.

The one non-obvious part is heightForWidth: a wrapping layout's height
DEPENDS on the width it is given, and Qt only asks a layout about that if
hasHeightForWidth() says yes. Without it the layout reports a single row's
height forever, and every card below the first row is simply clipped -- so
_do_layout runs in two modes, one that only measures (test_only) and one
that actually moves the widgets.
"""
from __future__ import annotations

from PySide6.QtCore import QMargins, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout


class FlowLayout(QLayout):

    def __init__(self, parent=None, margin: int = 0, spacing: int = 12,
                 min_item_width: int | None = None) -> None:
        """`min_item_width` turns this into the reference's
        `repeat(auto-fill, minmax(<min>px, 1fr))`: as many columns as fit at
        that minimum, then every card STRETCHED to share the row equally.

        Left None, items keep their own sizeHint width and the row is
        left-packed, which is the plain flow behaviour.
        """
        super().__init__(parent)
        self._items: list = []
        self._spacing = spacing
        self._min_item_width = min_item_width
        self.setContentsMargins(QMargins(margin, margin, margin, margin))
        if parent is not None:
            # The parent widget's SIZE POLICY has to advertise
            # height-for-width, not just this layout. QWidgetItem -- what the
            # enclosing layout actually holds -- asks the widget's size
            # policy, never the widget's layout, so a FlowLayout whose host
            # widget was left on the default policy is measured at its
            # sizeHint and every row after the first is clipped away. Set
            # here rather than at the call site because forgetting it is
            # silent, and looks like the cards themselves being too short.
            policy = parent.sizePolicy()
            policy.setHeightForWidth(True)
            parent.setSizePolicy(policy)

    # ---- QLayout plumbing -------------------------------------------------
    def addItem(self, item) -> None:      # noqa: N802 -- Qt override
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):         # noqa: N802 -- Qt override
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):         # noqa: N802 -- Qt override
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):        # noqa: N802 -- Qt override
        # Nothing: the layout grows downward by wrapping, never by stretching
        # its children sideways past their own sizeHint.
        return Qt.Orientation(0)

    # ---- the wrapping itself ---------------------------------------------
    def hasHeightForWidth(self) -> bool:  # noqa: N802 -- Qt override
        return True

    def heightForWidth(self, width: int) -> int:   # noqa: N802 -- Qt override
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:    # noqa: N802 -- Qt override
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:          # noqa: N802 -- Qt override
        """One row's worth, as a floor.

        A wrapping layout genuinely cannot answer "how tall are you" without
        being told a width -- heightForWidth is the real answer, and this is
        only what Qt falls back to before it asks. It must at least cover the
        tallest single item, so a layout holding one card never reports
        itself shorter than that card.
        """
        return self.minimumSize()

    def minimumSize(self) -> QSize:       # noqa: N802 -- Qt override
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
            size.setHeight(max(size.height(), item.sizeHint().height()))
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(),
                            margins.top() + margins.bottom())

    def _column_width(self, available: int) -> int | None:
        """The width every item gets, in auto-fill mode.

        Columns are as many as fit at `min_item_width`, then the leftover
        is shared out so the row ends flush with the right edge -- the "1fr"
        half of minmax(). Returns None when auto-fill is off.
        """
        if self._min_item_width is None or available <= 0:
            return None
        stride = self._min_item_width + self._spacing
        columns = max(1, (available + self._spacing) // stride)
        return int((available - self._spacing * (columns - 1)) // columns)

    def _do_layout(self, rect: QRect, *, test_only: bool) -> int:
        """Place every item left-to-right, wrapping when the next one would
        overflow `rect`'s width. Returns the total height used, which is the
        answer heightForWidth needs.

        Two passes, because a row's height is only known once the row is
        complete:

          - An item's own height is the LARGER of its sizeHint and its
            heightForWidth. A card containing word-wrapped labels reports a
            sizeHint computed for some other width entirely, so trusting the
            hint alone clips exactly the cards with the most to say -- the
            ones reporting a failure.
          - Every item in a row is then given that row's full height, so
            cards sitting side by side line up along their bottom edge
            instead of raggedly. This is what CSS grid gets for free from
            `1fr` rows.
        """
        margins = self.contentsMargins()
        effective = rect.adjusted(margins.left(), margins.top(),
                                  -margins.right(), -margins.bottom())
        column_width = self._column_width(effective.width())
        rows: list[tuple[list[tuple[object, int, int, int]], int]] = []
        current: list[tuple[object, int, int, int]] = []
        row_height = 0
        x = effective.x()
        for item in self._items:
            hint = item.sizeHint()
            width = column_width if column_width is not None else hint.width()
            height = hint.height()
            widget = item.widget()
            if widget is not None and widget.hasHeightForWidth():
                height = max(height, widget.heightForWidth(width))
            height = max(height, hint.height())
            if current and x + width > effective.right() + 1:
                rows.append((current, row_height))
                current, row_height = [], 0
                x = effective.x()
            current.append((item, x, width, height))
            x += width + self._spacing
            row_height = max(row_height, height)
        if current:
            rows.append((current, row_height))

        y = effective.y()
        for entries, height in rows:
            if not test_only:
                for item, item_x, width, _item_height in entries:
                    item.setGeometry(QRect(QPoint(item_x, y),
                                           QSize(width, height)))
            y += height + self._spacing
        if rows:
            y -= self._spacing          # no trailing gap after the last row
        return y - rect.y() + margins.bottom()
