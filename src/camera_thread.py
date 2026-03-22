from PySide6.QtCore import QThread, Signal

import platform
import time
import cv2
from datetime import datetime
import threading

from base_video_capture import BaseVideoCapture
from camera_info import CameraInfo
from ndi import NDICapture
from screen_capture_source import ScreenCapture, ScreenCaptureType
from storage import TextDetectionTargetMemoryStorage, subscribe_to_data, fetch_data
from tesseract import TextDetector
from text_detection_target import TextDetectionTargetWithResult
from sc_logging import logger
from frame_stabilizer import FrameStabilizer
from ocr_training_data import ocr_training_data_options


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


class FrameCropAndRotation:
    def __init__(self):
        self.isCropSet = fetch_data("scoresight.json", "crop_mode", False)
        self.cropTop = fetch_data("scoresight.json", "top_crop", 0)
        self.cropBottom = fetch_data("scoresight.json", "bottom_crop", 0)
        self.cropLeft = fetch_data("scoresight.json", "left_crop", 0)
        self.cropRight = fetch_data("scoresight.json", "right_crop", 0)
        self.rotation = fetch_data("scoresight.json", "rotation", 0)
        subscribe_to_data("scoresight.json", "crop_mode", self.setCropMode)
        subscribe_to_data("scoresight.json", "top_crop", self.setCropTop)
        subscribe_to_data("scoresight.json", "bottom_crop", self.setCropBottom)
        subscribe_to_data("scoresight.json", "left_crop", self.setCropLeft)
        subscribe_to_data("scoresight.json", "right_crop", self.setCropRight)
        subscribe_to_data("scoresight.json", "rotation", self.setRotation)

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

    def setRotation(self, rotation):
        self.rotation = rotation


class OpenCVVideoCaptureWithSettings(BaseVideoCapture):
    def __init__(self, capture_id: int | str, capture_backend: int = cv2.CAP_ANY):
        super().__init__(capture_id, capture_backend)
        self.video_capture = cv2.VideoCapture(capture_id, capture_backend)
        set_camera_highest_resolution(self.video_capture)
        self.video_capture_mutex = threading.Lock()
        self.fps = fetch_data("scoresight.json", "fps", 30)
        self.width = fetch_data("scoresight.json", "width", 0)
        self.height = fetch_data("scoresight.json", "height", 0)
        self.fourcc = fetch_data("scoresight.json", "fourcc", "MJPG")
        self.backend = fetch_data("scoresight.json", "backend", capture_backend)
        subscribe_to_data("scoresight.json", "fps", self.setFps)
        subscribe_to_data("scoresight.json", "width", self.setWidth)
        subscribe_to_data("scoresight.json", "height", self.setHeight)
        subscribe_to_data("scoresight.json", "fourcc", self.setFourcc)
        subscribe_to_data("scoresight.json", "backend", self.setBackend)

    def setFps(self, fps):
        self.fps = fps
        self.set(cv2.CAP_PROP_FPS, fps)

    def setWidth(self, width):
        self.width = width
        self.set(cv2.CAP_PROP_FRAME_WIDTH, width)

    def setHeight(self, height):
        self.height = height
        self.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

    def setFourcc(self, fourcc: str):
        self.fourcc = fourcc
        self.set(cv2.CAP_PROP_FOURCC, int.from_bytes(fourcc.encode(), "little"))

    def setBackend(self, backend):
        self.backend = backend
        self.release()
        with self.video_capture_mutex:
            self.video_capture = cv2.VideoCapture(self.capture_id, self.backend)

    def isOpened(self):
        with self.video_capture_mutex:
            return self.video_capture.isOpened()

    def read(self):
        with self.video_capture_mutex:
            return self.video_capture.read()

    def release(self):
        with self.video_capture_mutex:
            self.video_capture.release()

    def set(self, propId, value):
        with self.video_capture_mutex:
            self.video_capture.set(propId, value)

    def get(self, propId):
        with self.video_capture_mutex:
            return self.video_capture.get(propId)


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
        self.video_capture: BaseVideoCapture | None = None
        self.should_stop = False
        self.frame_interval = 30
        self.update_frame_interval = 1000 / fetch_data(
            "scoresight.json", "detection_cadence", 5
        )
        subscribe_to_data(
            "scoresight.json", "detection_cadence", self.setUpdateFrameInterval
        )
        self.preview_frame_interval = self.frame_interval  # match camera frame rate for smooth preview
        self.fps = 1000 / self.frame_interval  # frames per second
        self.pps = 1000 / self.preview_frame_interval  # previews per second
        self.ups = 1000 / self.update_frame_interval  # updates per second
        self.fps_alpha = 0.1  # Smoothing factor
        self.updateOnChange = True
        self.crop = FrameCropAndRotation()
        self.speed = 1

    def getSpeed(self):
        return self.speed

    def setSpeed(self, speed):
        self.speed = speed

    def setUpdateFrameInterval(self, cadence):
        self.update_frame_interval = 1000 / cadence

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
                and self.camera_info.type != CameraInfo.CameraType.URL
            ):
                # on windows use the dshow backend by default
                self.video_capture = OpenCVVideoCaptureWithSettings(
                    self.camera_info.id, cv2.CAP_DSHOW
                )
            else:
                # for files/urls, mac and linux use the default backend
                self.video_capture = OpenCVVideoCaptureWithSettings(self.camera_info.id)

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
        # if self.camera_info.type == CameraInfo.CameraType.OPENCV:
        #     # make sure to open the camera at the highest resolution
        #     set_camera_highest_resolution(self.video_capture)

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

            if self.camera_info.type == CameraInfo.CameraType.FILE:
                if self.speed != 1:
                    self.video_capture.set(
                        cv2.CAP_PROP_POS_FRAMES,
                        self.video_capture.get(cv2.CAP_PROP_POS_FRAMES) + self.speed,
                    )

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
            ).total_seconds() * 1000
            if time_diff_ms > 0:
                self.fps = (
                    self.fps_alpha * (1000 / time_diff_ms)
                    + (1.0 - self.fps_alpha) * self.fps
                )
            self.last_frame_timestamp = current_time

            # ----- Apply transforms (fast) on every frame -----

            # apply rotation if set
            if self.crop.rotation != 0:
                # use cv2.rotate to rotate the frame
                rotateCode = (
                    cv2.ROTATE_90_CLOCKWISE
                    if self.crop.rotation == 90
                    else (
                        cv2.ROTATE_90_COUNTERCLOCKWISE
                        if self.crop.rotation == 270
                        else cv2.ROTATE_180
                    )
                )
                frame_rgb = cv2.rotate(frame_rgb, rotateCode)

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

            # ----- Emit preview at real-time frame rate (decoupled from OCR) -----
            time_diff_prev = (current_time - self.last_emit_time).total_seconds() * 1000
            if time_diff_prev >= self.preview_frame_interval:
                if self.show_binary:
                    gray_preview = cv2.cvtColor(frame_rgb, cv2.COLOR_BGR2GRAY)
                    _, binary_preview = cv2.threshold(
                        gray_preview, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU
                    )
                    self.update_signal.emit(binary_preview)
                else:
                    self.update_signal.emit(frame_rgb)
                self.last_emit_time = current_time
                self.pps = (
                    self.fps_alpha * (1000 / time_diff_prev)
                    + (1.0 - self.fps_alpha) * self.pps
                )

            # ----- OCR processing at its own slower cadence -----
            time_since_update = (
                current_time - self.last_update_timestamp
            ).total_seconds() * 1000
            if time_since_update < self.update_frame_interval:
                # not time for OCR yet, skip to next frame
                self.sleep_fps_target()
                continue
            # process this frame for OCR
            self.last_update_timestamp = current_time
            self.ups = (
                self.fps_alpha * (1000 / time_since_update)
                + (1.0 - self.fps_alpha) * self.ups
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

                    if ocr_training_data_options.save_ocr_training_data:
                        # save the image and the text
                        ocr_training_data_options.save_ocr_results_to_folder(
                            binary, gray, results
                        )

            self.sleep_fps_target()

        if self.video_capture is not None:
            self.video_capture.release()
            self.video_capture = None

        logger.info("Camera thread stopped")

    def sleep_fps_target(self):
        time_diff_ms = (datetime.now() - self.last_frame_timestamp).total_seconds() * 1000
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
