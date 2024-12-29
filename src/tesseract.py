from defaults import FieldType
from os import path
from PIL import Image
from PySide6.QtCore import QRectF
from tesserocr import PyTessBaseAPI, RIL, iterate_level, PSM
from threading import Lock
import cv2
import numpy as np
import re

from resource_path import resource_path
from storage import fetch_data
from text_detection_target import (
    TextDetectionResult,
    TextDetectionTarget,
    TextDetectionTargetWithResult,
)
from sc_logging import logger


def autocrop(image_in):
    image = image_in.copy()
    # check if this image is black-on-white or white-on-black by looking at the first few pixels
    if np.sum(image[0:5, 0:5]) > 0:
        # black-on-white
        # invert the image
        image = 255 - image

    # find the first row that has a pixel
    first_row = 0
    for row in range(image.shape[0]):
        if np.sum(image[row, :]) > 0:
            first_row = row
            break
    # find the last row that has a pixel
    last_row = image.shape[0] - 1
    for row in range(image.shape[0] - 1, -1, -1):
        if np.sum(image[row, :]) > 0:
            last_row = row
            break
    # find the first column that has a pixel
    first_col = 0
    for col in range(image.shape[1]):
        if np.sum(image[:, col]) > 0:
            first_col = col
            break
    # find the last column that has a pixel
    last_col = image.shape[1] - 1
    for col in range(image.shape[1] - 1, -1, -1):
        if np.sum(image[:, col]) > 0:
            last_col = col
            break
    # leave a 10 pixel border on each side
    first_row = max(0, first_row - 10)
    last_row = min(image.shape[0] - 1, last_row + 10)
    first_col = max(0, first_col - 10)
    last_col = min(image.shape[1] - 1, last_col + 10)
    return image_in[first_row:last_row, first_col:last_col], (
        first_row,
        last_row,
        first_col,
        last_col,
    )


def add_ordinal_indicator(text):
    if text == "":
        return ""
    if text.endswith("1") and text != "11":
        return text + "st"
    elif text.endswith("2") and text != "12":
        return text + "nd"
    elif text.endswith("3") and text != "13":
        return text + "rd"
    else:
        return text + "th"


def is_valid_regex(pattern):
    try:
        re.compile(pattern)
        return True
    except re.error:
        return False


class TextDetector:
    # model name enum: daktronics=0, scoreboard_general=1
    class OcrModelIndex:
        DAKTRONICS = 0
        SCOREBOARD_GENERAL = 1
        GENERAL_ENGLISH = 2
        SCOREBOARD_GENERAL_LARGE = 3

    class BinarizationMethod:
        GLOBAL = 0
        NO_BINARIZATION = 1
        LOCAL = 2
        ADAPTIVE = 3

    def __init__(self):
        self.api_lock = Lock()
        self.api = None
        if (
            fetch_data(
                "scoresight.json",
                "ocr_model",
                TextDetector.OcrModelIndex.SCOREBOARD_GENERAL,
            )
            == TextDetector.OcrModelIndex.SCOREBOARD_GENERAL
        ):
            self.setOcrModel(TextDetector.OcrModelIndex.SCOREBOARD_GENERAL)
        else:
            self.setOcrModel(TextDetector.OcrModelIndex.DAKTRONICS)

    def setOcrModel(self, ocrModelIndex: OcrModelIndex | int | str | None = None):
        ocr_model = None
        model_folder = resource_path("tesseract", "tessdata")
        if ocrModelIndex == TextDetector.OcrModelIndex.DAKTRONICS:
            ocr_model = "daktronics"
        elif ocrModelIndex == TextDetector.OcrModelIndex.SCOREBOARD_GENERAL:
            ocr_model = "scoreboard_general"
        elif ocrModelIndex == TextDetector.OcrModelIndex.GENERAL_ENGLISH:
            ocr_model = "eng"
        elif ocrModelIndex == TextDetector.OcrModelIndex.SCOREBOARD_GENERAL_LARGE:
            ocr_model = "scoreboard_general_large"
        elif isinstance(ocrModelIndex, str):
            # check the model file exists at the path
            if path.exists(ocrModelIndex):
                # Take the folder as the tessdata folder
                model_folder = path.dirname(ocrModelIndex)
                # Take the model name without extension as the "language"
                ocr_model = path.basename(ocrModelIndex)
                ocr_model = path.splitext(ocr_model)[0]

        if ocr_model is None:
            return

        logger.info(f"Setting OCR model to {ocr_model} from {model_folder}")

        with self.api_lock:
            if self.api is not None:
                self.api.End()
                self.api = None
            self.api = PyTessBaseAPI(
                path=model_folder,
                lang=ocr_model,
            )
            # single word PSM
            self.api.SetPageSegMode(PSM.SINGLE_WORD)
            self.api.SetVariable("load_system_dawg", "F")
            self.api.SetVariable("load_freq_dawg", "F")

    def detect_text(self, image):
        if image is None:
            return ""
        if not isinstance(image, np.ndarray):
            return ""
        # check the image has rows and columns
        if len(image.shape) < 2 or image.shape[0] < 1 or image.shape[1] < 1:
            return ""
        pilimage = Image.fromarray(image)
        text = ""
        with self.api_lock:
            self.api.SetImage(pilimage)
            text = self.api.GetUTF8Text()
        return text.strip()

    def detect_mini_rects(
        self, imagecrop: np.ndarray, target_rect: TextDetectionTarget, scale_x, scale_y
    ):
        with self.api_lock:
            self.api.SetPageSegMode(PSM.SINGLE_CHAR)

        # iterate over the mini rects and get the text from each
        text = ""
        for i, mini_rect_ in enumerate(target_rect.mini_rects):
            if mini_rect_ is None or mini_rect_.width() < 1 or mini_rect_.height() < 1:
                continue

            mini_rect = QRectF(
                mini_rect_.x() * scale_x,
                mini_rect_.y() * scale_y,
                mini_rect_.width() * scale_x,
                mini_rect_.height() * scale_y,
            )

            if mini_rect.x() < 0:
                mini_rect.setX(0)
            if mini_rect.y() < 0:
                mini_rect.setY(0)

            if mini_rect.x() + mini_rect.width() > imagecrop.shape[1]:
                mini_rect.setWidth(imagecrop.shape[1] - mini_rect.x())
            if mini_rect.y() + mini_rect.height() > imagecrop.shape[0]:
                mini_rect.setHeight(imagecrop.shape[0] - mini_rect.y())

            mini_imagecrop = imagecrop[
                int(mini_rect.y()) : int(mini_rect.y() + mini_rect.height()),
                int(mini_rect.x()) : int(mini_rect.x() + mini_rect.width()),
            ]
            try:
                pilimage = Image.fromarray(mini_imagecrop)
                with self.api_lock:
                    self.api.SetPageSegMode(PSM.SINGLE_CHAR)
                    self.api.SetImage(pilimage)
                    text += self.api.GetUTF8Text().strip()
            except:
                logger.exception("Error in detect_mini_rects")
                pass

        with self.api_lock:
            self.api.SetPageSegMode(PSM.SINGLE_WORD)

        text = text.strip()
        return text

    def detect_rect(
        self,
        imagecrop,
        target_rect: TextDetectionTarget,
        scale_x,
        scale_y,
        effectiveRect=None,
    ):
        try:
            pilimage = Image.fromarray(imagecrop)
            with self.api_lock:
                self.api.SetImage(pilimage)
        except:
            return None, None

        text = ""
        extras = {}
        with self.api_lock:
            text = self.api.GetUTF8Text().strip()
            if text != "":
                # get the per-character boxes using an iterator with RIL_SYMBOL level
                it = self.api.GetIterator()
                extras["boxes"] = []
                wh_ratios = []
                for w in iterate_level(it, RIL.SYMBOL):
                    char = w.GetUTF8Text(RIL.SYMBOL)
                    box_tuple = w.BoundingBox(RIL.SYMBOL)
                    if (
                        box_tuple is None
                        or char is None
                        or char == ""
                        or len(box_tuple) != 4
                    ):
                        continue
                    box = {
                        "x": box_tuple[0],
                        "y": box_tuple[1],
                        "w": box_tuple[2] - box_tuple[0],
                        "h": box_tuple[3] - box_tuple[1],
                        "char": char,
                    }
                    # box is a dict with x, y, w and h
                    if scale_x != 1.0 or scale_y != 1.0:
                        box["x"] = int(box["x"] / scale_x)
                        box["y"] = int(box["y"] / scale_y)
                        box["w"] = int(box["w"] / scale_x)
                        box["h"] = int(box["h"] / scale_y)
                    if effectiveRect is not None:
                        box["x"] = int(box["x"] + effectiveRect.x())
                        box["y"] = int(box["y"] + effectiveRect.y())
                    extras["boxes"].append(box)
                    # if char is a "wide character" (like 0,2,3,4,5,6,7,8,9), add the width-to-height ratio
                    if char in "023456789" and box["h"] > 0:
                        wh_ratios.append(box["w"] / box["h"])
                if (
                    "normalize_wh_ratio" in target_rect.settings
                    and target_rect.settings["normalize_wh_ratio"]
                    and "median_wh_ratio" not in target_rect.settings
                    and len(wh_ratios) > 0
                ):
                    target_rect.settings["median_wh_ratio"] = np.median(wh_ratios)

        return text, extras

    def detect_multi_text(
        self, binary, gray, targets: list[TextDetectionTarget]
    ) -> list[TextDetectionResult]:
        if binary is None:
            return []
        if not isinstance(binary, np.ndarray):
            return []
        # check the image has rows and columns
        if len(binary.shape) < 2 or binary.shape[0] < 1 or binary.shape[1] < 1:
            return []

        texts = []
        for target_rect in targets:
            effectiveRect = None
            scale_x = 1.0
            scale_y = 1.0

            if (
                target_rect is None
                or target_rect.x() < 0
                or target_rect.y() < 0
                or target_rect.width() < 1
                or target_rect.height() < 1
            ):
                texts.append(
                    TextDetectionResult(
                        "", TextDetectionTargetWithResult.ResultState.Empty, None
                    )
                )
                continue

            if target_rect.x() >= binary.shape[1]:
                # move the rect inside the image
                target_rect.setX(binary.shape[1] - target_rect.width())
            if target_rect.y() >= binary.shape[0]:
                # move the rect inside the image
                target_rect.setY(binary.shape[0] - target_rect.height())
            if target_rect.x() + target_rect.width() > binary.shape[1]:
                target_rect.setWidth(binary.shape[1] - target_rect.x())
            if target_rect.y() + target_rect.height() > binary.shape[0]:
                target_rect.setHeight(binary.shape[0] - target_rect.y())

            if (
                target_rect.settings is not None
                and "binarization_method" in target_rect.settings
                and target_rect.settings["binarization_method"]
                != TextDetector.BinarizationMethod.GLOBAL
            ):
                if (
                    target_rect.settings["binarization_method"]
                    == TextDetector.BinarizationMethod.NO_BINARIZATION
                ):
                    # no binarization
                    imagecrop = gray[
                        int(target_rect.y()) : int(
                            target_rect.y() + target_rect.height()
                        ),
                        int(target_rect.x()) : int(
                            target_rect.x() + target_rect.width()
                        ),
                    ]
                elif (
                    target_rect.settings["binarization_method"]
                    == TextDetector.BinarizationMethod.LOCAL
                ):
                    # local binarization using Otsu's method
                    _, imagecrop = cv2.threshold(
                        gray[
                            int(target_rect.y()) : int(
                                target_rect.y() + target_rect.height()
                            ),
                            int(target_rect.x()) : int(
                                target_rect.x() + target_rect.width()
                            ),
                        ],
                        0,
                        255,
                        cv2.THRESH_BINARY + cv2.THRESH_OTSU,
                    )
                elif (
                    target_rect.settings["binarization_method"]
                    == TextDetector.BinarizationMethod.ADAPTIVE
                ):
                    # apply adaptive binarization
                    imagecrop = cv2.adaptiveThreshold(
                        gray[
                            int(target_rect.y()) : int(
                                target_rect.y() + target_rect.height()
                            ),
                            int(target_rect.x()) : int(
                                target_rect.x() + target_rect.width()
                            ),
                        ],
                        255,
                        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                        cv2.THRESH_BINARY,
                        # use a fraction of the patch area
                        max(int(target_rect.width() * target_rect.height() * 0.01), 3)
                        | 1,
                        2,
                    )
                # update the binary image for visualisation in the binary mode
                binary[
                    int(target_rect.y()) : int(target_rect.y() + target_rect.height()),
                    int(target_rect.x()) : int(target_rect.x() + target_rect.width()),
                ] = imagecrop
            else:
                imagecrop = binary[
                    int(target_rect.y()) : int(target_rect.y() + target_rect.height()),
                    int(target_rect.x()) : int(target_rect.x() + target_rect.width()),
                ]

            if (
                target_rect.settings is not None
                and "cleanup_thresh" in target_rect.settings
                and target_rect.settings["cleanup_thresh"] > 0
            ):
                # cleanup image from small components: find contours and remove small ones
                contours, _ = cv2.findContours(
                    imagecrop, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                )
                # cleanup_thresh is [0, 1.0], convert to [0, 0.05]
                cleanup_thresh = target_rect.settings["cleanup_thresh"] * 0.05
                img_area_thresh = (
                    imagecrop.shape[0] * imagecrop.shape[1] * cleanup_thresh
                )
                for contour in contours:
                    if cv2.contourArea(contour) < img_area_thresh:
                        cv2.drawContours(imagecrop, [contour], 0, 0, -1)

            if (
                target_rect.settings is not None
                and "vscale" in target_rect.settings
                and target_rect.settings["vscale"] != 10
            ):
                # vertical scale the image
                # the vscale input is in the range [1, 10] where 10 is the default (1:1)
                # scale the image in the y direction about the center
                rows, cols = imagecrop.shape
                # calculate the target height
                target_height = int(rows * (target_rect.settings["vscale"] / 10.0))
                scaled = cv2.resize(
                    imagecrop, (cols, target_height), 0, 0, cv2.INTER_AREA
                )
                # add padding to the top and bottom
                pad_top = (rows - target_height) // 2
                pad_bottom = rows - target_height - pad_top
                scaled = cv2.copyMakeBorder(
                    scaled, pad_top, pad_bottom, 0, 0, cv2.BORDER_REPLICATE
                )
                # make sure the image is the same size as the original
                scaled = scaled[:rows, :]
                # copy back into imagecrop and binary display
                binary[
                    int(target_rect.y()) : int(target_rect.y() + target_rect.height()),
                    int(target_rect.x()) : int(target_rect.x() + target_rect.width()),
                ] = scaled
                imagecrop = scaled

            if (
                target_rect.settings is not None
                and "skew" in target_rect.settings
                and target_rect.settings["skew"] != 0
            ):
                # skew the image in the x direction about the center
                rows, cols = imagecrop.shape
                # identity 2x2 matrix
                M = np.float32([[1, 0, 0], [0, 1, 0]])
                # add skew factor to matrix
                M[0, 1] = target_rect.settings["skew"] / 40.0
                try:
                    skewed = cv2.warpAffine(imagecrop, M, (cols, rows))
                    binary[
                        int(target_rect.y()) : int(
                            target_rect.y() + target_rect.height()
                        ),
                        int(target_rect.x()) : int(
                            target_rect.x() + target_rect.width()
                        ),
                    ] = skewed
                    imagecrop = skewed
                except:
                    pass

            if (
                target_rect.settings is not None
                and "dilate" in target_rect.settings
                and target_rect.settings["dilate"] > 0
                and imagecrop.shape[0] > 0
                and imagecrop.shape[1] > 0
            ):
                # dilate the image
                kernel = np.ones((3, 3), np.uint8)
                dilated = cv2.dilate(
                    imagecrop.copy(),
                    kernel,
                    iterations=int(target_rect.settings["dilate"]),
                )
                # copy back into image crop
                binary[
                    int(target_rect.y()) : int(target_rect.y() + target_rect.height()),
                    int(target_rect.x()) : int(target_rect.x() + target_rect.width()),
                ] = dilated

            if (
                target_rect.settings is not None
                and "invert_patch" in target_rect.settings
                and target_rect.settings["invert_patch"]
            ):
                # invert the image
                imagecrop = 255 - imagecrop

            if (
                target_rect.settings is not None
                and "skip_similar_image" in target_rect.settings
                and target_rect.settings["skip_similar_image"]
            ):
                # compare the image with the last image
                if (
                    target_rect.last_image is not None
                    and target_rect.last_image.shape == imagecrop.shape
                ):
                    # check if the difference is less than 5%
                    diff = cv2.absdiff(target_rect.last_image, imagecrop)
                    diff = diff.astype(np.float32)
                    diff = diff / 255.0
                    diff = diff.sum() / (imagecrop.shape[0] * imagecrop.shape[1])
                    if diff < 0.05:
                        # skip this image
                        texts.append(
                            TextDetectionResult(
                                "SIM",
                                TextDetectionTargetWithResult.ResultState.FailedFilter,
                                effectiveRect,
                            )
                        )
                        continue
                target_rect.last_image = imagecrop.copy()

            if (
                target_rect.settings is not None
                and "autocrop" in target_rect.settings
                and target_rect.settings["autocrop"]
            ):
                # auto crop the binary image around the text
                imagecrop, (first_row, last_row, first_col, last_col) = autocrop(
                    imagecrop
                )
                effectiveRect = QRectF(
                    first_col,
                    first_row,
                    last_col - first_col,
                    last_row - first_row,
                )

            # check if image is size 0
            if imagecrop.shape[0] == 0 or imagecrop.shape[1] == 0:
                texts.append(
                    TextDetectionResult(
                        "",
                        TextDetectionTargetWithResult.ResultState.Empty,
                        effectiveRect,
                    )
                )
                continue

            if (
                target_rect.settings is not None
                and "rescale_patch" in target_rect.settings
                and target_rect.settings["rescale_patch"]
            ):
                # rescale the image to 35 pixels height
                scale_x = 35 / imagecrop.shape[0]
                scale_y = scale_x

            if (
                target_rect.settings is not None
                and "normalize_wh_ratio" in target_rect.settings
                and target_rect.settings["normalize_wh_ratio"]
                and "median_wh_ratio" in target_rect.settings
                and target_rect.settings["median_wh_ratio"] > 0
            ):
                # rescale the image in x or in y such that the width-to-height ratio is 0.5
                scale_x *= 0.5 / target_rect.settings["median_wh_ratio"]

            if scale_x != 1.0 or scale_y != 1.0:
                imagecrop = cv2.resize(
                    imagecrop,
                    None,
                    fx=scale_x,
                    fy=scale_y,
                    interpolation=cv2.INTER_AREA,
                )

            # if dot detector count the blobs in the patch
            if (
                target_rect.settings is not None
                and "dot_detector" in target_rect.settings
                and target_rect.settings["dot_detector"]
            ):
                # find the contours
                contours, _ = cv2.findContours(
                    imagecrop, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                )
                # count the number of contours
                count = 0
                for contour in contours:
                    if cv2.contourArea(contour) > 5:
                        count += 1

                texts.append(
                    TextDetectionResult(
                        str(count),
                        TextDetectionTargetWithResult.ResultState.Success,
                        effectiveRect,
                    )
                )
                continue

            if target_rect.settings["type"] == FieldType.NUMBER:
                with self.api_lock:
                    self.api.SetVariable("tessedit_char_whitelist", "0123456789")
            elif target_rect.settings["type"] == FieldType.TIME:
                with self.api_lock:
                    self.api.SetVariable("tessedit_char_whitelist", "0123456789:.")
            else:  # general
                with self.api_lock:
                    self.api.SetVariable(
                        "tessedit_char_whitelist",
                        "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ .,;:!?-_()[]{}<>@#$%^&*+=|\\~`\"'",
                    )

            if len(target_rect.mini_rects) > 0:
                text = self.detect_mini_rects(imagecrop, target_rect, scale_x, scale_y)
                extras = {}
            else:
                text, extras = self.detect_rect(
                    imagecrop, target_rect, scale_x, scale_y, effectiveRect
                )
                if text is None:
                    texts.append(
                        TextDetectionResult(
                            "", TextDetectionTargetWithResult.ResultState.Empty, None
                        )
                    )
                    continue

            textstate = TextDetectionTargetWithResult.ResultState.Success
            if target_rect.settings is not None:
                if "format_regex" in target_rect.settings:
                    # validate the regex format is valid
                    if is_valid_regex(target_rect.settings["format_regex"]):
                        # check the text matches the regex fully
                        if not re.fullmatch(target_rect.settings["format_regex"], text):
                            textstate = (
                                TextDetectionTargetWithResult.ResultState.FailedFilter
                            )
                if "conf_thresh" in target_rect.settings:
                    with self.api_lock:
                        meanConf = self.api.MeanTextConf()
                    if meanConf < target_rect.settings["conf_thresh"]:
                        textstate = (
                            TextDetectionTargetWithResult.ResultState.FailedFilter
                        )
                if "smoothing" in target_rect.settings:
                    if target_rect.settings["smoothing"]:
                        # apply smoother
                        text = target_rect.ocrResultPerCharacterSmoother.get_smoothed_result(
                            text
                        )
                        if text is None:
                            text = ""
                if "remove_leading_zeros" in target_rect.settings:
                    if target_rect.settings["remove_leading_zeros"]:
                        # remove leading zeros
                        text = text.lstrip("0")
                        if text == "":
                            text = "0"
                if "ordinal_indicator" in target_rect.settings:
                    if target_rect.settings["ordinal_indicator"]:
                        # add ordinal indicator
                        text = add_ordinal_indicator(text)

            if text == "":
                textstate = TextDetectionTargetWithResult.ResultState.Empty

            result = TextDetectionResult(text, textstate, effectiveRect, extras)

            texts.append(result)
        return texts
