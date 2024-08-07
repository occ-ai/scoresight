from PySide6.QtWidgets import (
    QGraphicsView,
    QGraphicsScene,
    QGraphicsPixmapItem,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap, QPainter
from PySide6.QtCore import QThread, Signal

import platform
import time
import numpy as np
import cv2
import datetime
from datetime import datetime

from camera_info import CameraInfo
from ndi import NDICapture
from screen_capture_source import ScreenCapture
from storage import TextDetectionTargetMemoryStorage, subscribe_to_data, fetch_data
from tesseract import TextDetector
from text_detection_target import TextDetectionTargetWithResult
from sc_logging import logger
from frame_stabilizer import FrameStabilizer


# Function to set the resolution
def set_resolution(cap, width, height):
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)


# Function to get the resolution
def get_resolution(cap):
    width = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
    height = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    return width, height


# Function to set the camera to the highest resolution
def set_camera_highest_resolution(cap):
    # List of common resolutions to try
    resolutions = [(1920, 1080), (1280, 720), (1024, 768), (800, 600), (640, 480)]

    # grab one frame to make sure the camera is initialized
    ret, _ = cap.read()
    if not ret:
        logger.warn("Error: camera not initialized")
        return

    # grab the current resolution
    width, height = get_resolution(cap)

    # If the current resolution is already the highest, return
    if width >= resolutions[0][0] and height >= resolutions[0][1]:
        logger.info(
            "Camera is already at the highest resolution: %d x %d", width, height
        )
        return

    # Try each resolution and select the highest one that works
    highest_res = resolutions[0]
    for resolution in resolutions:
        logger.debug("Trying camera resolution: %d x %d", resolution[0], resolution[1])
        set_resolution(cap, *resolution)
        test_width, test_height = get_resolution(cap)
        logger.debug("Found camera resolution: %d x %d", test_width, test_height)
        # if found resolution is within close range of the target resolution, use it
        if (
            abs(test_width - resolution[0]) < 100
            and abs(test_height - resolution[1]) < 100
        ):
            logger.debug(
                "Camera highest resolution set to: %d x %d", test_width, test_height
            )
            highest_res = (test_width, test_height)
            break

    # Set the highest resolution
    logger.info("Setting camera resolution to: %d x %d", highest_res[0], highest_res[1])
    set_resolution(cap, *highest_res)


class FrameCrop:
    def __init__(self):
        self.isCropSet = fetch_data("scoresight.json", "crop_mode", False)
        self.cropTop = fetch_data("scoresight.json", "top_crop", 0)
        self.cropBottom = fetch_data("scoresight.json", "bottom_crop", 0)
        self.cropLeft = fetch_data("scoresight.json", "left_crop", 0)
        self.cropRight = fetch_data("scoresight.json", "right_crop", 0)
        subscribe_to_data("scoresight.json", "crop_mode", self.setCropMode)
        subscribe_to_data("scoresight.json", "top_crop", self.setCropTop)
        subscribe_to_data("scoresight.json", "bottom_crop", self.setCropBottom)
        subscribe_to_data("scoresight.json", "left_crop", self.setCropLeft)
        subscribe_to_data("scoresight.json", "right_crop", self.setCropRight)

    def setCropMode(self, crop_mode):
        self.isCropSet = crop_mode

    def setCropTop(self, crop_top):
        self.cropTop = crop_top

    def setCropBottom(self, crop_bottom):
        self.cropBottom = crop_bottom

    def setCropLeft(self, crop_left):
        self.cropLeft = crop_left

    def setCropRight(self, crop_right):
        self.cropRight = crop_right


class TimerThread(QThread):
    update_signal = Signal(object)
    update_error = Signal(object)
    ocr_result_signal = Signal(list)

    def __init__(
        self,
        camera_info: CameraInfo,
        detectionTargetsStorage: TextDetectionTargetMemoryStorage,
    ):
        super().__init__()
        self.camera_info = camera_info
        self.homography = None
        self.detectionTargetsStorage = detectionTargetsStorage
        self.textDetector = TextDetector()  # initialize tesseract
        self.show_binary = False
        self.retry_count = 0
        self.retry_count_max = 50
        self.retry_high_water_mark = 25
        self.stabilizationEnabled = False
        self.framestabilizer = FrameStabilizer()
        self.video_capture = None
        self.should_stop = False
        self.frame_interval = 30
        self.update_frame_interval = 200
        self.preview_frame_interval = 1000
        self.fps = 1000 / self.frame_interval  # frames per second
        self.pps = 1000 / self.preview_frame_interval  # previews per second
        self.ups = 1000 / self.update_frame_interval  # updates per second
        self.fps_alpha = 0.1  # Smoothing factor
        self.updateOnChange = True
        self.crop = FrameCrop()

    def connect_video_capture(self) -> bool:
        if self.camera_info.type == CameraInfo.CameraType.NDI:
            self.video_capture = NDICapture(self.camera_info.uuid)
        elif self.camera_info.type == CameraInfo.CameraType.SCREEN_CAPTURE:
            self.video_capture = ScreenCapture(self.camera_info.id)
        else:
            os_name = platform.system()
            if (
                os_name == "Windows"
                and self.camera_info.type != CameraInfo.CameraType.FILE
            ):
                # on windows use the dshow backend
                self.video_capture = cv2.VideoCapture(
                    self.camera_info.id, cv2.CAP_DSHOW
                )
            else:
                # for files/urls, mac and linux use the default backend
                self.video_capture = cv2.VideoCapture(self.camera_info.id)

        if self.retry_count != self.retry_high_water_mark:
            # at the high water mark this is a reconnect
            self.retry_count = 0

        if not self.video_capture.isOpened():
            logger.warn(
                "Error: unable to open camera. Check if the camera is connected."
            )
            self.update_error.emit("Error: Unable to play video stream")
            logger.info("Camera thread stopped")
            return False

        # attempt to set the highest resolution
        # check if camera index is a OpenCV camera index
        if self.camera_info.type == CameraInfo.CameraType.OPENCV:
            # make sure to open the camera at the highest resolution
            set_camera_highest_resolution(self.video_capture)

        return True

    def run(self):
        description_ascii = (
            self.camera_info.description.encode("ascii", errors="ignore").decode()
            if type(self.camera_info.description) == str
            else str(self.camera_info.description)
        )
        logger.info("Starting camera thread for: '%s'", description_ascii)

        if not self.connect_video_capture():
            self.should_stop = True
            return

        self.last_update_timestamp = datetime.now()
        self.last_frame_timestamp = datetime.now()
        self.last_emit_time = datetime.now()

        while not self.should_stop:
            if self.video_capture is None:
                logger.warn("Error: video capture is None")
                break

            if self.retry_count == self.retry_high_water_mark:
                logger.warn("Error: retry high water mark exceeded")
                # reconnect the video cap
                if self.video_capture is not None:
                    self.video_capture.release()
                    self.video_capture = None
                if not self.connect_video_capture():
                    self.should_stop = True
                    break
                self.sleep_fps_target()
                self.retry_count += 1
                continue
            if self.retry_count > self.retry_count_max:
                logger.warn("Error: retry count exceeded")
                self.should_stop = True
                self.update_error.emit("Error: Unable to play video stream")
                break

            # Read the frame from the camera
            ret, frame_rgb = None, None
            try:
                ret, frame_rgb = self.video_capture.read()
            except Exception as e:
                self.retry_count += 1
                logger.exception(
                    "Error: unable to read frame from camera (retry count: %d), exception %s",
                    self.retry_count,
                    e,
                )
                self.sleep_fps_target()
                continue

            if not ret:
                self.retry_count += 1
                if self.camera_info.type == CameraInfo.CameraType.FILE:
                    logger.debug("Restarting video file")
                    self.video_capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    self.sleep_fps_target()
                    continue
                logger.warn(
                    "Error: unable to read frame from camera, return value False (retry count: %d)",
                    self.retry_count,
                )
                self.sleep_fps_target()
                continue

            self.retry_count = 0  # good frame, reset the retry count
            current_time = datetime.now()

            # calculate the frame rate
            time_diff_ms = (
                current_time - self.last_frame_timestamp
            ).microseconds / 1000
            if time_diff_ms > 0:
                self.fps = (
                    self.fps_alpha * (1000 / time_diff_ms)
                    + (1.0 - self.fps_alpha) * self.fps
                )
            self.last_frame_timestamp = current_time

            # check that enough time has passed since last update
            time_diff_ms = (
                current_time - self.last_update_timestamp
            ).microseconds / 1000
            if time_diff_ms < self.update_frame_interval:
                # dump this frame since not enough time has passed
                self.sleep_fps_target()
                continue
            # process this frame
            self.last_update_timestamp = current_time
            self.ups = (
                self.fps_alpha * (1000 / time_diff_ms)
                + (1.0 - self.fps_alpha) * self.ups
            )

            # apply top-level crop if set
            if self.crop.isCropSet:
                frame_rgb = frame_rgb[
                    self.crop.cropTop : frame_rgb.shape[0] - self.crop.cropBottom,
                    self.crop.cropLeft : frame_rgb.shape[1] - self.crop.cropRight,
                ]

            # Stabilize the frame
            if self.stabilizationEnabled:
                frame_rgb = self.framestabilizer.stabilize_frame(frame_rgb)

            # Apply the homography to the frame
            if self.homography is not None:
                frame_rgb = cv2.warpPerspective(
                    frame_rgb, self.homography, (frame_rgb.shape[1], frame_rgb.shape[0])
                )

            gray = cv2.cvtColor(frame_rgb, cv2.COLOR_BGR2GRAY)
            _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)

            # Detect text in the target
            if not self.detectionTargetsStorage.is_empty():
                detectionTargets = self.detectionTargetsStorage.get_data()
                texts = self.textDetector.detect_multi_text(
                    binary, gray, detectionTargets
                )
                if len(texts) > 0 and len(detectionTargets) == len(texts):
                    # augment the text detection targets with the results
                    results = []
                    for i, result in enumerate(texts):
                        if self.updateOnChange:
                            if (
                                detectionTargets[i].last_text is not None
                                and detectionTargets[i].last_text == result.text
                            ):
                                result.state = (
                                    TextDetectionTargetWithResult.ResultState.SameNoChange
                                )

                        results.append(
                            TextDetectionTargetWithResult(
                                detectionTargets[i],
                                result.text,
                                result.state,
                                result.rect,
                                result.extra,
                            )
                        )
                        detectionTargets[i].last_text = result.text

                    # emit the results
                    self.ocr_result_signal.emit(results)

            # Emit the signal to update the pixmap once per second
            time_diff_prev = (current_time - self.last_emit_time).total_seconds() * 1000
            if time_diff_prev >= self.preview_frame_interval:
                if self.show_binary:
                    self.update_signal.emit(binary)
                else:
                    self.update_signal.emit(frame_rgb)
                self.last_emit_time = current_time
                self.pps = (
                    self.fps_alpha * (1000 / time_diff_prev)
                    + (1.0 - self.fps_alpha) * self.pps
                )

            self.sleep_fps_target()

        if self.video_capture is not None:
            self.video_capture.release()
            self.video_capture = None

        logger.info("Camera thread stopped")

    def sleep_fps_target(self):
        time_diff_ms = (datetime.now() - self.last_frame_timestamp).microseconds / 1000
        if time_diff_ms < self.frame_interval:
            time.sleep((self.frame_interval - time_diff_ms) / 1000.0)

    # on destroy, stop the timer
    def __del__(self):
        logger.info("Stopping camera")
        self.should_stop = True
        self.wait()

    def toggleStabilization(self, state):
        self.stabilizationEnabled = state
        if not state:
            self.framestabilizer.reset()


class CameraView(QGraphicsView):
    first_frame_received_signal = Signal()

    def __init__(self, camera_index, detectionTargetsStorage=None):
        super().__init__()
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.timerThread = TimerThread(camera_index, detectionTargetsStorage)
        self.timerThread.update_signal.connect(self.update_pixmap)
        self.timerThread.update_error.connect(self.error_event)
        self.timerThread.start()
        self.scenePixmapItem = None
        self.detectionTargetsStorage = detectionTargetsStorage
        self.firstFrameReceived = False
        self.fps_text = None
        self.error_text = None
        self.showOSD = True

    def setUpdateOnChange(self, updateOnChange):
        self.timerThread.updateOnChange = updateOnChange

    def toggleOSD(self, state):
        self.showOSD = state
        if self.fps_text is not None:
            self.fps_text.setVisible(state)

    def update_pixmap(self, frame):
        if self.timerThread is None:
            return

        # check if frame is not contiguous
        if not frame.flags["C_CONTIGUOUS"]:
            frame = np.ascontiguousarray(frame)

        # Create a QImage from the frame data
        image = QImage(
            frame.data,
            frame.shape[1],
            frame.shape[0],
            frame.strides[0],
            (
                QImage.Format.Format_Grayscale8
                if frame.ndim == 2
                else (
                    QImage.Format.Format_BGR888
                    if frame.shape[2] == 3
                    else QImage.Format.Format_RGBA8888
                )
            ),
        )

        # Create a QPixmap from the QImage
        pixmap = QPixmap.fromImage(image)

        if self.scenePixmapItem is None:
            self.scenePixmapItem = QGraphicsPixmapItem(pixmap)
            self.scene.addItem(self.scenePixmapItem)
            self.scenePixmapItem.setZValue(0)
            self.fitInView(self.scenePixmapItem, Qt.AspectRatioMode.KeepAspectRatio)
        else:
            refit = False
            # check if the pixmap is the same size as the current one
            if self.scenePixmapItem.pixmap().size() != pixmap.size():
                logger.info(f"scene size: {self.scene.sceneRect()}")
                refit = True
            self.scenePixmapItem.setPixmap(pixmap)
            if refit:
                self.scene.setSceneRect(0, 0, pixmap.width(), pixmap.height())
                logger.info(f"scene size: {self.scene.sceneRect()}")
                logger.info(f"Refitting view to new pixmap size: {pixmap.size()}")
                self.fitInView(self.scenePixmapItem, Qt.AspectRatioMode.KeepAspectRatio)
                self.centerOn(self.scenePixmapItem)

        if not self.firstFrameReceived:
            self.firstFrameReceived = True
            self.first_frame_received_signal.emit()

        # update the fps text
        fps_text = f"Frames/s: {self.timerThread.fps:.2f}\nUpdates/s: {self.timerThread.ups:.2f}\nPreviews/s: {self.timerThread.pps:.2f}"
        if self.fps_text is None:
            self.fps_text = self.scene.addText(fps_text)
            self.fps_text.setPos(0, 0)
            self.fps_text.setZValue(2)
            self.fps_text.setDefaultTextColor(Qt.GlobalColor.white)
            # scale the text according to the view size so its always the same size
        else:
            self.fps_text.setPlainText(fps_text)
        self.fps_text.setScale(0.002 * self.scenePixmapItem.boundingRect().width())

    def error_event(self, error):
        if self.error_text is not None:
            self.scene.removeItem(self.error_text)
        if error is not None:
            logger.error("Error: %s", error)
            # add the error to the scene
            self.error_text = self.scene.addText(f"⚠️ {error}")
            self.error_text.setDefaultTextColor(Qt.GlobalColor.red)
            self.error_text.setScale(0.004 * self.width())
            # diplay error on the bottom of the video view
            self.error_text.setPos(
                0, self.height() - self.error_text.boundingRect().height() - 10
            )

    def setFourCornersForHomography(self, corners: list[tuple[int]]):
        if corners is None or len(corners) != 4:
            if self.timerThread is not None:
                self.timerThread.homography = None
            return
        corners_as_array = np.array(corners, dtype=np.float32)
        # Calculate bounding rectangle
        x, y, w, h = cv2.boundingRect(corners_as_array)
        # Destination points (corners of the bounding rectangle)
        dst_points = np.array(
            [[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.float32
        )
        # calculate the homography from the corners to the rect
        self.timerThread.homography, _ = cv2.findHomography(
            corners_as_array, dst_points
        )

    def closeEvent(self, event):
        logger.debug("Close")
        if self.timerThread is not None:
            # Stop the timer thread
            self.timerThread.should_stop = True
            self.timerThread.wait()
            self.timerThread = None

        # Call the base class closeEvent method
        super().closeEvent(event)

    # on destroy, stop the timer
    def __del__(self):
        if self.timerThread is not None:
            self.timerThread.should_stop = True
            self.timerThread.wait()
            self.timerThread = None
