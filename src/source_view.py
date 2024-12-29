import math
from PySide6.QtCore import QPointF, Qt, QTimer, QRectF
from PySide6.QtGui import QBrush, QColor, QMouseEvent, QPen, QPolygonF
from PySide6.QtWidgets import (
    QGraphicsPolygonItem,
    QGraphicsRectItem,
)

from camera_info import CameraInfo
from camera_view import CameraView
from storage import (
    TextDetectionTargetMemoryStorage,
    fetch_data,
    remove_data,
    store_data,
    subscribe_to_data,
)
from text_detection_target import TextDetectionTarget, TextDetectionTargetWithResult
from sc_logging import logger
from resizable_rect import MiniRect, ResizableRectWithNameTypeAndResult


def sort_points_clockwise(points: list[QGraphicsRectItem]) -> list[QGraphicsRectItem]:
    # Calculate the center point
    center_x = sum(point.x() for point in points) / len(points)
    center_y = sum(point.y() for point in points) / len(points)
    center = QPointF(center_x, center_y)

    # Define a function to calculate the angle between a point and the center
    def angle_from_center(point):
        return math.atan2(point.y() - center.y(), point.x() - center.x())

    # Sort the points based on their angles
    sorted_points = sorted(points, key=angle_from_center)

    # Rotate the list so that the top-left point comes first
    top_left = min(sorted_points, key=lambda p: p.x() + p.y())
    while sorted_points[0] != top_left:
        sorted_points = sorted_points[1:] + [sorted_points[0]]

    return sorted_points


class ImageViewer(CameraView):
    def __init__(
        self,
        camera_index: CameraInfo,
        fourCornersAppliedCallback: callable,
        detectionTargetsStorage: TextDetectionTargetMemoryStorage | None,
        itemSelectedCallback: callable,
    ):
        super().__init__(camera_index, detectionTargetsStorage)
        self.setMouseTracking(True)
        self.fourCornerSelectionMode = False
        self.fourCorners = []
        self.fourCornerPolygon = None
        self.fourCornersAppliedCallback = fourCornersAppliedCallback
        self.itemSelectedCallback = itemSelectedCallback
        self.first_frame_received_signal.connect(self.detectionTargetsChanged)
        self.detectionTargetsStorage.data_changed.connect(self.detectionTargetsChanged)
        self.timerThread.ocr_result_signal.connect(self.ocrResult)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, False)
        if fetch_data("scoresight.json", "four_corners"):
            self.setFourCorners(fetch_data("scoresight.json", "four_corners"))
            self.fourCornersAppliedCallback(self.fourCorners)
        self._isScaling = False
        self._isPanning = False
        self._lastMousePosition = QPointF()

        self.boxDisplayStyleSetting: int = fetch_data(
            "scoresight.json", "box_display_style", 3
        )
        subscribe_to_data("scoresight.json", "box_display_style", self.boxDisplayStyle)

    def resizeEvent(self, event):
        if self._isScaling:
            return
        super().resizeEvent(event)
        self.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        self.detectionTargetsChanged()

    def boxDisplayStyle(self, state: int):
        self.boxDisplayStyleSetting = state
        for item in self.scene.items():
            if isinstance(item, ResizableRectWithNameTypeAndResult):
                item.setBoxDisplayStyle(state)

    def toggleStabilization(self, state):
        if self.firstFrameReceived and self.timerThread:
            self.timerThread.toggleStabilization(state)

    def toggleBinary(self):
        if self.firstFrameReceived and self.timerThread:
            self.timerThread.show_binary = not self.timerThread.show_binary

    def toggleFourCorner(self, state):
        self.setFourCorners(None)  # clear the current corners
        remove_data("scoresight.json", "four_corners")
        if state:
            # new four corner selection
            self.fourCornerSelectionMode = True

    def setFourCorners(self, corners):
        self.fourCorners = []
        self.fourCornerPolygon = None
        self.setFourCornersForHomography(corners)

    def detectionTargetsChanged(self):
        if not self.firstFrameReceived:
            return

        # get the detection targets from the storage
        detectionTargets: list[TextDetectionTarget] = (
            self.detectionTargetsStorage.get_data()
        )
        done_targets = []
        # add the boxes to the scene
        for detectionTarget in detectionTargets:
            done_targets.append(detectionTarget.name)
            if detectionTarget.settings["templatefield"]:
                # do not show the template fields
                continue
            boxFound = self.findBox(detectionTarget.name)
            if boxFound is None:
                boxFound = ResizableRectWithNameTypeAndResult(
                    detectionTarget,
                    # image size
                    self.scene.sceneRect().width(),
                    onCenter=False,
                    boxChangedCallback=self.boxChanged,
                    itemSelectedCallback=self.itemSelectedCallback,
                    boxDisplayStyle=self.boxDisplayStyleSetting,
                )
                self.scene.addItem(boxFound)
            else:
                boxFound.setRect(
                    detectionTarget.x() - boxFound.x(),
                    detectionTarget.y() - boxFound.y(),
                    detectionTarget.width(),
                    detectionTarget.height(),
                )
            boxFound.setMiniRectMode(detectionTarget.settings["composite_box"])

        # remove the boxes that are not in the storage
        for item in self.scene.items():
            if isinstance(item, ResizableRectWithNameTypeAndResult):
                if item.name not in done_targets:
                    self.scene.removeItem(item)

    def boxChanged(self, name: str, rect: QRectF, mini_rects: list[QRectF]):
        # update the detection target in the storage
        detectionTargets: list[TextDetectionTarget] = (
            self.detectionTargetsStorage.get_data()
        )
        for detectionTarget in detectionTargets:
            if detectionTarget.name == name:
                detectionTarget.setX(rect.x())
                detectionTarget.setY(rect.y())
                detectionTarget.setWidth(rect.width())
                detectionTarget.setHeight(rect.height())
                self.detectionTargetsStorage.edit_item(
                    detectionTarget.name, detectionTarget
                )
                detectionTarget.mini_rects = mini_rects
                break

    def findBox(self, name):
        # find the box with the name
        for item in self.scene.items():
            if isinstance(item, ResizableRectWithNameTypeAndResult):
                if item.name == name:
                    return item
        return None

    def removeBox(self, name):
        # find the box with the name
        item = self.findBox(name)
        if item:
            self.scene.removeItem(item)

    def selectBox(self, name):
        # deselect all the boxes and select the one with the name
        for item in self.scene.items():
            if isinstance(item, ResizableRectWithNameTypeAndResult):
                item.setSelected(item.name == name)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Delete:
            for item in self.scene.items():
                if item.isSelected():
                    if isinstance(item, MiniRect):
                        # If there are mini-rects, delete the selected mini-rect
                        item.parentItem().removeMiniRect(item)
                    if isinstance(item, ResizableRectWithNameTypeAndResult):
                        # If no mini-rects, send a signal to delete the box itself
                        self.removeBox(item.name)
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self._isPanning = True
            self._lastMousePosition = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif (
            self.fourCornerSelectionMode and event.button() == Qt.MouseButton.LeftButton
        ):
            # in four corner mode we want to add a point to the scene
            # and connect the points in a polygon
            # get the position of the click
            # convert the position to the scene position
            # create a new point
            point = QGraphicsRectItem(-10, -10, 20, 20)
            point.setPos(self.mapToScene(event.pos()))
            point.setBrush(QBrush(QColor("red")))
            point.setPen(QPen(Qt.GlobalColor.transparent))
            self.scene.addItem(point)
            # add the point to the list of points
            self.fourCorners.append(point)
            self.fourCorners = sort_points_clockwise(self.fourCorners)
            # if we have 4 points, create a polygon
            if len(self.fourCorners) >= 2:
                if not self.fourCornerPolygon:
                    self.fourCornerPolygon = QGraphicsPolygonItem()
                    self.fourCornerPolygon.setPen(QPen(QColor("red"), 3))
                    self.scene.addItem(self.fourCornerPolygon)
                self.fourCornerPolygon.setPolygon(
                    QPolygonF([corner.pos() for corner in self.fourCorners])
                )
            if len(self.fourCorners) == 4:
                # calculate the homography from the corners to the rect
                self.setFourCornersForHomography(
                    [(corner.x(), corner.y()) for corner in self.fourCorners]
                )
                self.fourCornerSelectionMode = False
                store_data(
                    "scoresight.json",
                    "four_corners",
                    [(corner.x(), corner.y()) for corner in self.fourCorners],
                )
                # hide polygon and points
                self.fourCornerPolygon.hide()
                for corner in self.fourCorners:
                    corner.hide()
        else:
            # deselect all the boxes
            self.selectBox(None)
            self.itemSelectedCallback(None)
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent | None) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self._isPanning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            super().mouseReleaseEvent(event)
            self.detectionTargetsStorage.saveBoxesToStorage()

    def mouseMoveEvent(self, event: QMouseEvent | None) -> None:
        if self._isPanning:
            delta = event.position() - self._lastMousePosition
            self._lastMousePosition = event.position()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x()
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y()
            )
        else:
            super().mouseMoveEvent(event)

    def wheelEvent(self, event):
        # check for ctrl key
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            self._isScaling = True
            factor = 1.05
            if event.angleDelta().y() > 0:
                # zoom in
                if self.transform().m11() < 3.0:
                    self.scale(factor, factor)
            else:
                # zoom out
                if self.transform().m11() > 0.33:
                    self.scale(1 / factor, 1 / factor)
            # Use QTimer.singleShot to delay resetting the flag
            QTimer.singleShot(0, self.resetScalingFlag)
        else:
            # scroll the scene
            super().wheelEvent(event)

    def resetZoom(self):
        self.resetTransform()
        self.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def resetScalingFlag(self):
        self._isScaling = False  # Reset the flag after potential resizeEvent

    def ocrResult(self, results: list[TextDetectionTargetWithResult]):
        if not self.firstFrameReceived:
            return
        # update the rect with the result
        for targetWithResult in results:
            if targetWithResult.settings["templatefield"]:
                # do not update template fields
                continue
            item = self.findBox(targetWithResult.name)
            if item:
                item.updateResult(targetWithResult)
            else:
                logger.debug(f"Could not find item with name {targetWithResult.name}")

    def closeEvent(self, event):
        logger.debug("Close")
        super().closeEvent(event)
