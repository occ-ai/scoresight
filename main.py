from functools import partial
import os
import platform
import sys
import datetime
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QFileDialog,
    QLabel,
    QMenu,
    QDialog,
    QInputDialog,
    QTableWidgetItem,
)
from PySide6.QtUiTools import QUiLoader
from PySide6.QtGui import QIcon, QStandardItemModel, QStandardItem
from PySide6.QtCore import (
    Qt,
    Signal,
    Slot,
    QTranslator,
    QLocale,
    QObject,
    QCoreApplication,
    QEvent,
    QMetaMethod,
)
from dotenv import load_dotenv
from os import path

from camera_info import CameraInfo
from get_camera_info import get_camera_info
from http_server import start_http_server, update_http_server, stop_http_server
from screen_capture_source import ScreenCapture
from source_view import ImageViewer
from defaults import (
    default_boxes,
    default_info_for_box_name,
    normalize_settings_dict,
    format_prefixes,
)

from storage import (
    TextDetectionTargetMemoryStorage,
    fetch_data,
    remove_data,
    store_data,
    store_custom_box_name,
    rename_custom_box_name_in_storage,
    remove_custom_box_name_in_storage,
    fetch_custom_box_names,
)
from obs_websocket import (
    create_obs_scene_from_export,
    open_obs_websocket,
    update_text_source,
)

from text_detection_target import TextDetectionTarget, TextDetectionTargetWithResult
from sc_logging import logger
from update_check import check_for_updates
from log_view import LogViewerDialog
import file_output
from vmix_output import VMixAPI
from ui_mainwindow import Ui_MainWindow
from ui_about import Ui_Dialog as Ui_About
from ui_connect_obs import Ui_Dialog as Ui_ConnectObs
from ui_url_source import Ui_Dialog as Ui_UrlSource
from ui_screen_capture import Ui_Dialog as Ui_ScreenCapture


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
            widget = None
        else:
            clear_layout(item.layout())


class MainWindow(QMainWindow):
    # add a signal to update sources
    update_sources = Signal(list)
    get_sources = Signal()

    def __init__(self, translator: QTranslator, parent: QObject):
        super(MainWindow, self).__init__()
        self.parent_object = parent
        self.ui = Ui_MainWindow()
        logger.info("Starting ScoreSight")
        self.ui.setupUi(self)
        self.translator = translator
        # load env variables
        load_dotenv()
        self.setWindowTitle(f"ScoreSight - v{os.getenv('LOCAL_RELEASE_TAG')}")
        if platform.system() == "Windows":
            # set the icon
            self.setWindowIcon(
                QIcon(
                    path.abspath(
                        path.join(path.dirname(__file__), "icons/Windows-icon-open.ico")
                    )
                )
            )

        self.menubar = self.menuBar()
        file_menu = self.menubar.addMenu("File")

        # check for updates
        check_for_updates(False)
        file_menu.addAction("Check for Updates", lambda: check_for_updates(True))
        file_menu.addAction("About", self.openAboutDialog)
        file_menu.addAction("View Current Log", self.openLogsDialog)
        file_menu.addAction("Import Configuration", self.importConfiguration)
        file_menu.addAction("Export Configuration", self.exportConfiguration)

        # Add "Language" menu
        languageMenu = file_menu.addMenu("Language")

        # Add language options
        self.addLanguageOption(languageMenu, "English (US)", "en_US")
        self.addLanguageOption(languageMenu, "French (France)", "fr_FR")
        self.addLanguageOption(languageMenu, "Spanish (Spain)", "es_ES")
        self.addLanguageOption(languageMenu, "German", "de_DE")
        self.addLanguageOption(languageMenu, "Italian", "it_IT")
        self.addLanguageOption(languageMenu, "Japanese", "ja_JP")
        self.addLanguageOption(languageMenu, "Korean", "ko_KR")
        self.addLanguageOption(languageMenu, "Dutch", "nl_NL")
        self.addLanguageOption(languageMenu, "Polish", "pl_PL")
        self.addLanguageOption(languageMenu, "Portuguese (Brazil)", "pt_BR")
        self.addLanguageOption(languageMenu, "Portuguese (Portugal)", "pt_PT")
        self.addLanguageOption(languageMenu, "Russian", "ru_RU")
        self.addLanguageOption(languageMenu, "Chinese (Simplified)", "zh_CN")

        # Hide the menu bar by default
        self.menubar.setVisible(False)

        # Show the menu bar when the Alt key is pressed
        self.installEventFilter(self)

        self.ui.pushButton_connectObs.clicked.connect(self.openOBSConnectModal)
        self.ui.statusbar.showMessage("OBS: Not Connected")

        self.vmixUiSetup()

        start_http_server()

        self.ui.pushButton_stabilize.setEnabled(True)
        self.ui.pushButton_stabilize.clicked.connect(self.toggleStabilize)

        self.ui.toolButton_topCrop.clicked.connect(self.cropMode)
        # check configuation if crop is enabled
        self.ui.toolButton_topCrop.setChecked(
            fetch_data("scoresight.json", "crop_mode", False)
        )
        self.ui.widget_cropPanel.setVisible(self.ui.toolButton_topCrop.isChecked())
        self.ui.widget_cropPanel.setEnabled(self.ui.toolButton_topCrop.isChecked())
        self.ui.spinBox_leftCrop.valueChanged.connect(
            partial(self.globalSettingsChanged, "left_crop")
        )
        self.ui.spinBox_leftCrop.setValue(fetch_data("scoresight.json", "left_crop", 0))
        self.ui.spinBox_rightCrop.valueChanged.connect(
            partial(self.globalSettingsChanged, "right_crop")
        )
        self.ui.spinBox_rightCrop.setValue(
            fetch_data("scoresight.json", "right_crop", 0)
        )
        self.ui.spinBox_topCrop.valueChanged.connect(
            partial(self.globalSettingsChanged, "top_crop")
        )
        self.ui.spinBox_topCrop.setValue(fetch_data("scoresight.json", "top_crop", 0))
        self.ui.spinBox_bottomCrop.valueChanged.connect(
            partial(self.globalSettingsChanged, "bottom_crop")
        )
        self.ui.spinBox_bottomCrop.setValue(
            fetch_data("scoresight.json", "bottom_crop", 0)
        )

        self.ui.widget_detectionCadence.setVisible(True)
        self.ui.horizontalSlider_detectionCadence.setValue(
            fetch_data("scoresight.json", "detection_cadence", 5)
        )
        self.ui.horizontalSlider_detectionCadence.valueChanged.connect(
            self.detectionCadenceChanged
        )
        self.ui.toolButton_addBox.clicked.connect(self.addBox)
        self.ui.toolButton_removeBox.clicked.connect(self.removeCustomBox)

        self.obs_websocket_client = None

        ocr_models = [
            "Daktronics",
            "General Scoreboard",
            "General Fonts (English)",
            "General Scoreboard Large",
        ]
        self.ui.comboBox_ocrModel.addItems(ocr_models)
        self.ui.comboBox_ocrModel.setCurrentIndex(
            fetch_data("scoresight.json", "ocr_model", 1)
        )  # default to General Scoreboard

        self.ui.frame_source_view.setEnabled(False)
        self.ui.groupBox_target_settings.setEnabled(False)
        self.ui.pushButton_makeBox.clicked.connect(self.makeBox)
        self.ui.pushButton_removeBox.clicked.connect(self.removeBox)
        self.ui.tableWidget_boxes.itemClicked.connect(self.listItemClicked)
        # connect the edit triggers
        self.ui.tableWidget_boxes.itemDoubleClicked.connect(self.editBoxName)
        self.ui.pushButton_refresh_sources.clicked.connect(
            lambda: self.get_sources.emit()
        )
        self.detectionTargetsStorage = TextDetectionTargetMemoryStorage()
        self.detectionTargetsStorage.data_changed.connect(self.detectionTargetsChanged)
        self.ui.pushButton_createOBSScene.clicked.connect(self.createOBSScene)
        self.ui.pushButton_selectFolder.clicked.connect(self.selectOutputFolder)
        self.ui.toolButton_trashFolder.clicked.connect(self.clearOutputFolder)
        self.ui.pushButton_stopUpdates.toggled.connect(self.toggleStopUpdates)
        self.ui.comboBox_ocrModel.currentIndexChanged.connect(self.ocrModelChanged)
        self.ui.pushButton_restoreDefaults.clicked.connect(self.restoreDefaults)
        self.ui.toolButton_zoomReset.clicked.connect(self.resetZoom)
        self.ui.toolButton_osd.toggled.connect(self.toggleOSD)
        self.ui.toolButton_showOCRrects.toggled.connect(self.toggleOCRRects)
        self.ui.checkBox_smoothing.toggled.connect(
            partial(self.genericSettingsChanged, "smoothing")
        )
        self.ui.checkBox_skip_empty.toggled.connect(
            partial(self.genericSettingsChanged, "skip_empty")
        )
        self.ui.horizontalSlider_conf_thresh.valueChanged.connect(
            self.confThreshChanged
        )
        self.ui.lineEdit_format.textChanged.connect(
            partial(self.genericSettingsChanged, "format_regex")
        )
        self.ui.comboBox_fieldType.currentIndexChanged.connect(
            partial(self.genericSettingsChanged, "type")
        )
        self.ui.checkBox_skip_similar_image.toggled.connect(
            partial(self.genericSettingsChanged, "skip_similar_image")
        )
        self.ui.checkBox_autocrop.toggled.connect(
            partial(self.genericSettingsChanged, "autocrop")
        )
        self.ui.horizontalSlider_cleanup.valueChanged.connect(self.cleanupThreshChanged)
        self.ui.horizontalSlider_dilate.valueChanged.connect(
            partial(self.genericSettingsChanged, "dilate")
        )
        self.ui.horizontalSlider_skew.valueChanged.connect(
            partial(self.genericSettingsChanged, "skew")
        )
        self.ui.horizontalSlider_vscale.valueChanged.connect(
            partial(self.genericSettingsChanged, "vscale")
        )
        self.ui.checkBox_removeLeadingZeros.toggled.connect(
            partial(self.genericSettingsChanged, "remove_leading_zeros")
        )
        self.ui.checkBox_rescalePatch.toggled.connect(
            partial(self.genericSettingsChanged, "rescale_patch")
        )
        self.ui.checkBox_normWHRatio.toggled.connect(
            partial(self.genericSettingsChanged, "normalize_wh_ratio")
        )
        self.ui.checkBox_invertPatch.toggled.connect(
            partial(self.genericSettingsChanged, "invert_patch")
        )
        self.ui.checkBox_dotDetector.toggled.connect(
            partial(self.genericSettingsChanged, "dot_detector")
        )
        self.ui.checkBox_ordinalIndicator.toggled.connect(
            partial(self.genericSettingsChanged, "ordinal_indicator")
        )
        self.ui.comboBox_binarizationMethod.currentIndexChanged.connect(
            partial(self.genericSettingsChanged, "binarization_method")
        )
        self.ui.comboBox_formatPrefix.currentIndexChanged.connect(
            self.formatPrefixChanged
        )
        self.ui.checkBox_updateOnchange.toggled.connect(self.toggleUpdateOnChange)

        # populate the tableWidget_boxes with the default and custom boxes
        custom_boxes_names = fetch_custom_box_names()

        for box_name in [box["name"] for box in default_boxes] + custom_boxes_names:
            item = QTableWidgetItem(
                QIcon(
                    path.abspath(
                        path.join(path.dirname(__file__), "icons/circle-x.svg")
                    )
                ),
                box_name,
            )
            item.setData(Qt.ItemDataRole.UserRole, "unchecked")
            self.ui.tableWidget_boxes.insertRow(self.ui.tableWidget_boxes.rowCount())
            self.ui.tableWidget_boxes.setItem(
                self.ui.tableWidget_boxes.rowCount() - 1, 0, item
            )
            disabledItem = QTableWidgetItem()
            disabledItem.setFlags(Qt.ItemFlag.NoItemFlags)
            self.ui.tableWidget_boxes.setItem(
                self.ui.tableWidget_boxes.rowCount() - 1, 1, disabledItem
            )

        self.image_viewer = None
        self.obs_connect_modal = None
        self.source_name = None
        self.updateOCRResults = True
        self.log_dialog = None
        if fetch_data("scoresight.json", "open_on_startup", False):
            logger.info("Opening log dialog on startup")
            self.openLogsDialog()

        if fetch_data("scoresight.json", "obs"):
            self.connectObs()

        self.out_folder = fetch_data("scoresight.json", "output_folder")
        if self.out_folder:
            if not path.exists(self.out_folder):
                self.out_folder = None
                remove_data("scoresight.json", "output_folder")
            else:
                self.ui.lineEdit_folder.setText(self.out_folder)

        self.first_csv_append = True
        self.last_aggregate_save = datetime.datetime.now()
        self.ui.checkBox_saveCsv.toggled.connect(
            partial(self.globalSettingsChanged, "save_csv")
        )
        self.ui.checkBox_saveCsv.setChecked(
            fetch_data("scoresight.json", "save_csv", False)
        )
        self.ui.checkBox_saveXML.toggled.connect(
            partial(self.globalSettingsChanged, "save_xml")
        )
        self.ui.checkBox_saveXML.setChecked(
            fetch_data("scoresight.json", "save_xml", False)
        )
        self.ui.comboBox_appendMethod.currentIndexChanged.connect(
            partial(self.globalSettingsChanged, "append_method")
        )
        self.ui.horizontalSlider_aggsPerSecond.valueChanged.connect(
            partial(self.globalSettingsChanged, "aggs_per_second")
        )
        self.ui.comboBox_appendMethod.setCurrentIndex(
            fetch_data("scoresight.json", "append_method", 3)
        )
        self.ui.horizontalSlider_aggsPerSecond.setValue(
            fetch_data("scoresight.json", "aggs_per_second", 5)
        )
        self.ui.checkBox_updateOnchange.setChecked(
            fetch_data("scoresight.json", "update_on_change", True)
        )

        self.update_sources.connect(self.updateSources)
        self.get_sources.connect(self.getSources)
        self.get_sources.emit()

    def cropMode(self):
        # if the toolButton_topCrop is unchecked, go to crop mode
        if self.ui.toolButton_topCrop.isChecked():
            self.ui.widget_cropPanel.setVisible(True)
            self.ui.widget_cropPanel.setEnabled(True)
            self.globalSettingsChanged("crop_mode", True)
        else:
            self.ui.widget_cropPanel.setVisible(False)
            self.ui.widget_cropPanel.setEnabled(False)
            self.globalSettingsChanged("crop_mode", False)

    def globalSettingsChanged(self, settingName, value):
        store_data("scoresight.json", settingName, value)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Alt:
            self.menubar.setVisible(True)
        elif event.type() == QEvent.FocusOut and self.menubar.isVisible():
            self.menubar.setVisible(False)
        elif event.type() == QEvent.WindowDeactivate and self.menubar.isVisible():
            self.menubar.setVisible(False)
        return super().eventFilter(obj, event)

    def focusOutEvent(self, event):
        if self.menubar.isVisible():
            self.menubar.setVisible(False)
        super().focusOutEvent(event)

    def changeEvent(self, event):
        if event.type() == QEvent.WindowDeactivate and self.menubar.isVisible():
            self.menubar.setVisible(False)
        super().changeEvent(event)

    def changeLanguage(self, locale):
        locale_file = path.abspath(
            path.join(path.dirname(__file__), "translations", f"scoresight_{locale}.qm")
        )
        logger.info(f"Changing language to {locale_file}")
        if not self.translator.load(locale_file):
            logger.error(f"Could not load translation for {locale_file}")
            return
        appInstance = QApplication.instance()
        if appInstance:
            logger.info(f"installing translator for {locale}")
            appInstance.installTranslator(self.translator)
            self.ui.retranslateUi(self)

    def addLanguageOption(self, menu: QMenu, language_name: str, locale: str):
        menu.addAction(language_name, lambda: self.changeLanguage(locale))

    def toggleUpdateOnChange(self, value):
        self.globalSettingsChanged("update_on_change", value)
        if self.image_viewer:
            self.image_viewer.setUpdateOnChange(value)

    def formatPrefixChanged(self, index):
        if index == 12:
            return  # do nothing if "Select Preset" is selected
        # based on the selected index, set the format prefix
        # change lineEdit_format to the selected format prefix
        self.ui.lineEdit_format.setText(format_prefixes[index])

    def importConfiguration(self):
        # open a file dialog to select a configuration file
        file, _ = QFileDialog.getOpenFileName(
            self, "Open Configuration File", "", "Configuration Files (*.json)"
        )
        if not file:
            return
        # load the configuration from the file
        if not self.detectionTargetsStorage.loadBoxesFromFile(file):
            # show an error message
            self.ui.statusbar.showMessage("Error loading configuration file")

    def exportConfiguration(self):
        # open a file dialog to select the output file
        file, _ = QFileDialog.getSaveFileName(
            self, "Save Configuration File", "", "Configuration Files (*.json)"
        )
        if not file:
            return
        # save the configuration to the file
        self.detectionTargetsStorage.saveBoxesToFile(file)

    def toggleOSD(self, value):
        if self.image_viewer:
            self.image_viewer.toggleOSD(value)

    def toggleOCRRects(self, value):
        if self.image_viewer:
            self.image_viewer.toggleOCRRects(value)

    def resetZoom(self):
        if self.image_viewer:
            self.image_viewer.resetZoom()

    def detectionCadenceChanged(self, detections_per_second):
        self.globalSettingsChanged("detection_cadence", detections_per_second)
        if self.image_viewer and self.image_viewer.timerThread:
            # convert the detections_per_second to milliseconds
            self.image_viewer.timerThread.update_frame_interval = (
                1000 / detections_per_second
            )

    def ocrModelChanged(self, index):
        self.globalSettingsChanged("ocr_model", index)
        # update the ocr model in the text detector
        if (
            self.image_viewer
            and self.image_viewer.timerThread
            and self.image_viewer.timerThread.textDetector
        ):
            self.image_viewer.timerThread.textDetector.setOcrModel(index)

    def openLogsDialog(self):
        if self.log_dialog is None:
            # open the logs dialog
            self.log_dialog = LogViewerDialog()
            self.log_dialog.setWindowTitle("Logs")

        # show the dialog, non modal
        self.log_dialog.show()

    def openAboutDialog(self):
        # open the about dialog
        about_dialog = QDialog()
        about_dialog_ui = Ui_About()
        about_dialog_ui.setupUi(about_dialog)
        about_dialog.setWindowTitle("About ScoreSight")
        about_dialog.exec()

    def toggleStabilize(self):
        if not self.image_viewer:
            return
        # start or stop the stabilization
        self.image_viewer.toggleStabilization(self.ui.pushButton_stabilize.isChecked())

    def toggleStopUpdates(self, value):
        self.ui.statusbar.showMessage(
            self.translator.translate("main", "Stopped updates")
            if value
            else self.translator.translate("main", "Resumed updates")
        )
        self.updateOCRResults = not value
        # change the text on the button
        self.ui.pushButton_stopUpdates.setText(
            self.translator.translate("main", "Resume updates")
            if value
            else self.translator.translate("main", "Stop updates")
        )

    def selectOutputFolder(self):
        # open a Qt dialog to select the output folder
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Output Folder",
            fetch_data("scoresight.json", "output_folder", ""),
            options=QFileDialog.Option.ShowDirsOnly,
        )
        if folder and len(folder) > 0:
            self.ui.lineEdit_folder.setText(folder)
            self.out_folder = folder
            self.globalSettingsChanged("output_folder", folder)

    def clearOutputFolder(self):
        # clear the output folder
        self.ui.lineEdit_folder.setText("")
        self.out_folder = None
        remove_data("scoresight.json", "output_folder")

    def editSettings(self, settingsMutatorCallback):
        # update the selected item's settings in the detectionTargetsStorage
        item = self.ui.tableWidget_boxes.currentItem()
        if not item:
            logger.info("no item selected")
            return
        item_name = item.text()
        item_obj = self.detectionTargetsStorage.find_item_by_name(item_name)
        if not item_obj:
            logger.info("item not found: %s", item_name)
            return
        item_obj = settingsMutatorCallback(item_obj)
        self.detectionTargetsStorage.edit_item(item_name, item_obj)

    def restoreDefaults(self):
        # restore the default settings for the selected item
        def restoreDefaultsSettings(item_obj):
            info = default_info_for_box_name(item_obj.name)
            item_obj.settings = normalize_settings_dict({}, info)
            return item_obj

        self.editSettings(restoreDefaultsSettings)
        self.populateSettings(self.ui.tableWidget_boxes.currentItem().text())

    def confThreshChanged(self):
        def editConfThreshSettings(item_obj):
            item_obj.settings["conf_thresh"] = (
                float(self.ui.horizontalSlider_conf_thresh.value()) / 100.0
            )
            return item_obj

        self.editSettings(editConfThreshSettings)

    def cleanupThreshChanged(self):
        def editCleanupThreshSettings(item_obj):
            item_obj.settings["cleanup_thresh"] = (
                float(self.ui.horizontalSlider_cleanup.value()) / 100.0
            )
            return item_obj

        self.editSettings(editCleanupThreshSettings)

    def genericSettingsChanged(self, settingName, value):
        def editGenericSettings(item_obj):
            item_obj.settings[settingName] = value
            return item_obj

        self.editSettings(editGenericSettings)

    def vmixConnectionChanged(self):
        self.vmixUpdater = VMixAPI(
            self.ui.lineEdit_vmixHost.text(),
            self.ui.lineEdit_vmixPort.text(),
            self.ui.inputLineEdit_vmix.text(),
            {},
        )
        self.globalSettingsChanged("vmix_host", self.ui.lineEdit_vmixHost.text())
        self.globalSettingsChanged("vmix_port", self.ui.lineEdit_vmixPort.text())
        self.globalSettingsChanged("vmix_input", self.ui.inputLineEdit_vmix.text())

    def vmixMappingChanged(self, _):
        # store entire mapping data in scoresight.json
        mapping = {}
        model = self.ui.tableView_vmixMapping.model()
        if isinstance(model, QStandardItemModel):
            for i in range(model.rowCount()):
                item = model.item(i, 0)
                value = model.item(i, 1)
                if item and value:
                    mapping[item.text()] = value.text()
            self.globalSettingsChanged("vmix_mapping", mapping)
            self.vmixUpdater.set_field_mapping(mapping)

    def vmixUiSetup(self):
        # populate the vmix connection from storage
        self.ui.lineEdit_vmixHost.setText(
            fetch_data("scoresight.json", "vmix_host", "localhost")
        )
        self.ui.lineEdit_vmixPort.setText(
            fetch_data("scoresight.json", "vmix_port", "8099")
        )
        self.ui.inputLineEdit_vmix.setText(
            fetch_data("scoresight.json", "vmix_input", "1")
        )
        # connect the lineEdits to vmixConnectionChanged
        self.ui.lineEdit_vmixHost.textChanged.connect(self.vmixConnectionChanged)
        self.ui.lineEdit_vmixPort.textChanged.connect(self.vmixConnectionChanged)
        self.ui.inputLineEdit_vmix.textChanged.connect(self.vmixConnectionChanged)

        # create the vmixUpdater
        self.vmixUpdater = VMixAPI(
            self.ui.lineEdit_vmixHost.text(),
            self.ui.lineEdit_vmixPort.text(),
            self.ui.inputLineEdit_vmix.text(),
            {},
        )
        # add standard item model to the tableView_vmixMapping
        self.ui.tableView_vmixMapping.setModel(QStandardItemModel())
        mapping = fetch_data("scoresight.json", "vmix_mapping", {})
        if mapping:
            self.vmixUpdater.set_field_mapping(mapping)

        self.ui.tableView_vmixMapping.model().dataChanged.connect(
            self.vmixMappingChanged
        )

        self.ui.pushButton_startvmix.toggled.connect(self.togglevMix)

    def togglevMix(self, value):
        if not self.vmixUpdater:
            return
        if value:
            self.ui.pushButton_startvmix.setText("🛑 Stop vMix")
            self.vmixUpdater.running = True
        else:
            self.ui.pushButton_startvmix.setText("▶️ Start vMix")
            self.vmixUpdater.running = False

    def updatevMixTable(self, detectionTargets):
        mapping_storage = fetch_data("scoresight.json", f"vmix_mapping")
        model = QStandardItemModel()
        model.blockSignals(True)

        for box in detectionTargets:
            # add the detection to the vmix output mapping: tableView_vmixMapping
            # check if the table already has the detectionTarget
            items = model.findItems(box.name, Qt.MatchFlag.MatchExactly)
            if len(items) == 0:
                # add the item to the list
                row = model.rowCount()
                model.insertRow(row)
                model.setItem(row, 0, QStandardItem(box.name))
                # the first item shouldn't be editable
                model.item(row, 0).setFlags(Qt.ItemFlag.NoItemFlags)
                if mapping_storage and box.name in mapping_storage:
                    model.setItem(row, 1, QStandardItem(mapping_storage[box.name]))
                else:
                    model.setItem(row, 1, QStandardItem(box.name))
            else:
                # update the item in the list
                item = items[0]
                row = item.row()
                # get value from storage
                if mapping_storage and box.name in mapping_storage:
                    model.setItem(row, 1, QStandardItem(mapping_storage[box.name]))
                else:
                    model.setItem(row, 1, QStandardItem(box.name))
        # remove the items that are not in the detectionTargets
        for i in range(model.rowCount()):
            item = model.item(i, 0)
            if not any([box.name == item.text() for box in detectionTargets]):
                model.removeRow(i)

        model.blockSignals(False)
        self.ui.tableView_vmixMapping.setModel(model)

    def detectionTargetsChanged(self, detectionTargets):
        for box in detectionTargets:
            logger.debug(f"Change: Detection target: {box.name}")
            # change the list icon to green checkmark
            items = self.ui.tableWidget_boxes.findItems(
                box.name, Qt.MatchFlag.MatchExactly
            )
            if len(items) == 0:
                logger.warning(f"Item not found: {box.name}. Adding it to the list.")
                # add the item to the list
                self.ui.tableWidget_boxes.insertRow(
                    self.ui.tableWidget_boxes.rowCount()
                )
                item = QTableWidgetItem(box.name)
                self.ui.tableWidget_boxes.setItem(
                    self.ui.tableWidget_boxes.rowCount() - 1, 0, item
                )
                disabledItem = QTableWidgetItem()
                disabledItem.setFlags(Qt.ItemFlag.NoItemFlags)
                self.ui.tableWidget_boxes.setItem(
                    self.ui.tableWidget_boxes.rowCount() - 1, 1, disabledItem
                )
            else:
                item = items[0]
            item.setIcon(
                QIcon(
                    path.abspath(
                        path.join(path.dirname(__file__), "icons/circle-check.svg")
                    )
                )
            )
            item.setData(Qt.ItemDataRole.UserRole, "checked")

        self.updatevMixTable(detectionTargets)

        # if save_csv is enabled, truncate the aggregate file
        if self.ui.checkBox_saveCsv.isChecked() and self.out_folder:
            csv_output_file_path = path.abspath(
                path.join(self.out_folder, "results.csv")
            )
            try:
                with open(csv_output_file_path, "w") as f:
                    f.write("")
                self.first_csv_append = True
                self.last_aggregate_save = datetime.datetime.now()
            except Exception as e:
                logger.error(f"Error truncating aggregate file: {e}")

    def populateSettings(self, name):
        self.ui.lineEdit_format.blockSignals(True)
        self.ui.comboBox_fieldType.blockSignals(True)
        self.ui.checkBox_smoothing.blockSignals(True)
        self.ui.checkBox_skip_empty.blockSignals(True)
        self.ui.horizontalSlider_conf_thresh.blockSignals(True)
        self.ui.checkBox_autocrop.blockSignals(True)
        self.ui.checkBox_skip_similar_image.blockSignals(True)
        self.ui.horizontalSlider_cleanup.blockSignals(True)
        self.ui.horizontalSlider_dilate.blockSignals(True)
        self.ui.horizontalSlider_skew.blockSignals(True)
        self.ui.horizontalSlider_vscale.blockSignals(True)
        self.ui.checkBox_removeLeadingZeros.blockSignals(True)
        self.ui.checkBox_rescalePatch.blockSignals(True)
        self.ui.checkBox_normWHRatio.blockSignals(True)
        self.ui.checkBox_invertPatch.blockSignals(True)
        self.ui.checkBox_ordinalIndicator.blockSignals(True)
        self.ui.comboBox_binarizationMethod.blockSignals(True)
        self.ui.comboBox_formatPrefix.blockSignals(True)

        # populate the settings from the detectionTargetsStorage
        item_obj = self.detectionTargetsStorage.find_item_by_name(name)
        if not item_obj:
            self.ui.lineEdit_format.setText("")
            self.ui.comboBox_fieldType.setCurrentIndex(0)
            self.ui.checkBox_smoothing.setChecked(True)
            self.ui.checkBox_skip_empty.setChecked(True)
            self.ui.horizontalSlider_conf_thresh.setValue(50)
            self.ui.checkBox_autocrop.setChecked(False)
            self.ui.checkBox_skip_similar_image.setChecked(False)
            self.ui.horizontalSlider_cleanup.setValue(0)
            self.ui.horizontalSlider_dilate.setValue(1)
            self.ui.horizontalSlider_skew.setValue(0)
            self.ui.horizontalSlider_vscale.setValue(10)
            self.ui.label_selectedInfo.setText("")
            self.ui.checkBox_removeLeadingZeros.setChecked(False)
            self.ui.checkBox_rescalePatch.setChecked(False)
            self.ui.checkBox_normWHRatio.setChecked(False)
            self.ui.checkBox_invertPatch.setChecked(False)
            self.ui.checkBox_ordinalIndicator.setChecked(False)
            self.ui.comboBox_binarizationMethod.setCurrentIndex(0)
        else:
            item_obj.settings = normalize_settings_dict(
                item_obj.settings, default_info_for_box_name(item_obj.name)
            )
            self.ui.label_selectedInfo.setText(f"{item_obj.name}")
            self.ui.lineEdit_format.setText(item_obj.settings["format_regex"])
            self.ui.comboBox_fieldType.setCurrentIndex(item_obj.settings["type"])
            self.ui.checkBox_smoothing.setChecked(item_obj.settings["smoothing"])
            self.ui.checkBox_skip_empty.setChecked(item_obj.settings["skip_empty"])
            self.ui.horizontalSlider_conf_thresh.setValue(
                int(item_obj.settings["conf_thresh"] * 100)
            )
            self.ui.checkBox_autocrop.setChecked(item_obj.settings["autocrop"])
            self.ui.checkBox_skip_similar_image.setChecked(
                item_obj.settings["skip_similar_image"]
            )
            self.ui.horizontalSlider_cleanup.setValue(
                int(item_obj.settings["cleanup_thresh"] * 100)
            )
            self.ui.horizontalSlider_dilate.setValue(item_obj.settings["dilate"])
            self.ui.horizontalSlider_skew.setValue(item_obj.settings["skew"])
            self.ui.horizontalSlider_vscale.setValue(item_obj.settings["vscale"])
            self.ui.checkBox_removeLeadingZeros.setChecked(
                item_obj.settings["remove_leading_zeros"]
            )
            self.ui.checkBox_rescalePatch.setChecked(item_obj.settings["rescale_patch"])
            self.ui.checkBox_normWHRatio.setChecked(
                item_obj.settings["normalize_wh_ratio"]
            )
            self.ui.checkBox_invertPatch.setChecked(item_obj.settings["invert_patch"])
            self.ui.checkBox_dotDetector.setChecked(item_obj.settings["dot_detector"])
            self.ui.checkBox_ordinalIndicator.setChecked(
                item_obj.settings["ordinal_indicator"]
            )
            self.ui.comboBox_binarizationMethod.setCurrentIndex(
                item_obj.settings["binarization_method"]
            )

        self.ui.comboBox_formatPrefix.setCurrentIndex(12)

        self.ui.lineEdit_format.blockSignals(False)
        self.ui.comboBox_fieldType.blockSignals(False)
        self.ui.checkBox_smoothing.blockSignals(False)
        self.ui.checkBox_skip_empty.blockSignals(False)
        self.ui.horizontalSlider_conf_thresh.blockSignals(False)
        self.ui.checkBox_autocrop.blockSignals(False)
        self.ui.checkBox_skip_similar_image.blockSignals(False)
        self.ui.horizontalSlider_cleanup.blockSignals(False)
        self.ui.horizontalSlider_dilate.blockSignals(False)
        self.ui.horizontalSlider_skew.blockSignals(False)
        self.ui.horizontalSlider_vscale.blockSignals(False)
        self.ui.checkBox_removeLeadingZeros.blockSignals(False)
        self.ui.checkBox_rescalePatch.blockSignals(False)
        self.ui.checkBox_normWHRatio.blockSignals(False)
        self.ui.checkBox_invertPatch.blockSignals(False)
        self.ui.checkBox_ordinalIndicator.blockSignals(False)
        self.ui.comboBox_binarizationMethod.blockSignals(False)
        self.ui.comboBox_formatPrefix.blockSignals(False)

    def listItemClicked(self, item):
        if item.data(Qt.ItemDataRole.UserRole) == "checked":
            # enable the remove box button and disable the make box button
            self.ui.pushButton_removeBox.setEnabled(True)
            self.ui.pushButton_makeBox.setEnabled(False)
            self.ui.groupBox_target_settings.setEnabled(True)
            self.populateSettings(item.text())
        else:
            # enable the make box button and disable the remove box button
            self.ui.pushButton_removeBox.setEnabled(False)
            self.ui.pushButton_makeBox.setEnabled(True)
            self.ui.groupBox_target_settings.setEnabled(False)
            self.populateSettings("")

    def openOBSConnectModal(self):
        # disable OBS options
        self.ui.lineEdit_sceneName.setEnabled(False)
        self.ui.checkBox_recreate.setEnabled(False)
        self.ui.pushButton_createOBSScene.setEnabled(False)

        # load the ui from "connect_obs.ui"
        self.obs_modal_ui = Ui_ConnectObs()
        self.obs_connect_modal = QDialog()
        self.obs_modal_ui.setupUi(self.obs_connect_modal)
        self.obs_connect_modal.setWindowTitle("Connect to OBS")

        # connect the "connect" button to a function
        self.obs_modal_ui.pushButton_connect.clicked.connect(self.connectObs)

        # load the saved data from scoresight.json
        obs_data = fetch_data("scoresight.json", "obs")
        if obs_data:
            self.obs_modal_ui.lineEdit_ip.setText(obs_data["ip"])
            self.obs_modal_ui.lineEdit_port.setText(obs_data["port"])
            self.obs_modal_ui.lineEdit_password.setText(obs_data["password"])
        # show the modal
        self.obs_connect_modal.show()
        # focus the connect button
        self.obs_modal_ui.pushButton_connect.setFocus()

    def connectObs(self):
        # open a websocket connection to OBS using obs_websocket.py
        # enable the save button in the modal if the connection is successful
        if self.obs_connect_modal is not None:
            self.obs_websocket_client = open_obs_websocket(
                {
                    "ip": self.obs_modal_ui.lineEdit_ip.text(),
                    "port": self.obs_modal_ui.lineEdit_port.text(),
                    "password": self.obs_modal_ui.lineEdit_password.text(),
                }
            )
        else:
            self.obs_websocket_client = open_obs_websocket(
                fetch_data("scoresight.json", "obs")
            )
        if not self.obs_websocket_client:
            # show error in label_error
            if self.obs_connect_modal:
                self.obs_modal_ui.label_error.setText("Cannot connect to OBS")
            return

        # connection was successful
        if self.obs_connect_modal:
            store_data(
                "scoresight.json",
                "obs",
                {
                    "ip": self.obs_modal_ui.lineEdit_ip.text(),
                    "port": self.obs_modal_ui.lineEdit_port.text(),
                    "password": self.obs_modal_ui.lineEdit_password.text(),
                },
            )
            self.obs_connect_modal.close()

        self.ui.lineEdit_sceneName.setEnabled(True)
        self.ui.checkBox_recreate.setEnabled(True)
        self.ui.pushButton_createOBSScene.setEnabled(True)

        # set OBS status to connected in the status bar
        self.ui.statusbar.showMessage("OBS: Connected")

    @Slot()
    def getSources(self):
        self.update_sources.emit(get_camera_info())

    @Slot(list)
    def updateSources(self, camera_sources: list[CameraInfo]):
        self.reset_playing_source()
        # clear all the items after "Screen Capture"
        for i in range(4, self.ui.comboBox_camera_source.count()):
            self.ui.comboBox_camera_source.removeItem(4)

        # populate the combobox with the sources
        for source in camera_sources:
            self.ui.comboBox_camera_source.addItem(source.description, source)

        self.ui.comboBox_camera_source.setEnabled(True)
        currentIndexChangedSignal = QMetaMethod.fromSignal(
            self.ui.comboBox_camera_source.currentIndexChanged
        )
        if self.ui.comboBox_camera_source.isSignalConnected(currentIndexChangedSignal):
            self.ui.comboBox_camera_source.currentIndexChanged.disconnect()
        self.ui.comboBox_camera_source.currentIndexChanged.connect(self.sourceChanged)

        # enable the source view frame
        self.ui.frame_source_view.setEnabled(True)

        selected_source_from_storage = fetch_data("scoresight.json", "source_selected")
        if type(selected_source_from_storage) == str:
            logger.info(
                "Source selected from storage: %s", selected_source_from_storage
            )
            # check if the source is a file path
            if path.exists(selected_source_from_storage):
                logger.info("File exists: %s", selected_source_from_storage)
                self.ui.comboBox_camera_source.blockSignals(True)
                self.ui.comboBox_camera_source.setCurrentIndex(1)
                self.ui.comboBox_camera_source.blockSignals(False)
                self.source_name = selected_source_from_storage
                self.sourceSelectionSucessful()
            else:
                # select the last selected source
                self.ui.comboBox_camera_source.setCurrentText(
                    selected_source_from_storage
                )

    def reset_playing_source(self):
        if self.image_viewer:
            # remove the image viewer from the layout frame_for_source_view_label
            self.ui.frame_for_source_view_label.layout().removeWidget(self.image_viewer)
            self.image_viewer.close()
            self.image_viewer = None
        # add a label with markdown text
        label_select_source = QLabel("### Open a Camera or Load a File")
        label_select_source.setTextFormat(Qt.TextFormat.MarkdownText)
        label_select_source.setEnabled(False)
        label_select_source.setAlignment(Qt.AlignmentFlag.AlignCenter)
        clear_layout(self.ui.frame_for_source_view_label.layout())
        self.ui.frame_for_source_view_label.layout().addWidget(label_select_source)

    def sourceChanged(self, index):
        # get the source name from the combobox
        self.source_name = None
        self.ui.groupBox_sb_info.setEnabled(False)
        self.ui.tableWidget_boxes.setEnabled(False)
        self.ui.pushButton_fourCorner.setEnabled(False)
        self.ui.pushButton_binary.setEnabled(False)
        if self.ui.comboBox_camera_source.currentIndex() == 0:
            self.reset_playing_source()
            return
        elif self.ui.comboBox_camera_source.currentIndex() == 1:
            logger.info("Open File selection dialog")
            # open a file dialog to select a video file
            file, _ = QFileDialog.getOpenFileName(
                self, "Open Video File", "", "Video Files (*.mp4 *.avi *.mov)"
            )
            if not file or not path.exists(file):
                # no file selected - change source to "Select a source"
                logger.error("No file selected")
                self.ui.comboBox_camera_source.setCurrentText("Select a source")
                return
            else:
                logger.info("File selected: %s", file)
            self.source_name = file
        elif self.ui.comboBox_camera_source.currentIndex() == 2:
            # open a dialog to enter the url
            url_dialog = QDialog()
            ui_urlsource = Ui_UrlSource()
            ui_urlsource.setupUi(url_dialog)
            url_dialog.setWindowTitle("URL Source")
            # focus on url input
            ui_urlsource.lineEdit_url.setFocus()
            url_dialog.exec()  # wait for the dialog to close
            # check if the dialog was accepted
            if url_dialog.result() != QDialog.DialogCode.Accepted:
                self.ui.comboBox_camera_source.setCurrentIndex(0)
                return
            self.source_name = ui_urlsource.lineEdit_url.text()
            if self.source_name == "":
                self.ui.comboBox_camera_source.setCurrentIndex(0)
                return
        elif self.ui.comboBox_camera_source.currentIndex() == 3:
            # open a dialog to select the screen
            screen_dialog = QDialog()
            ui_screencapture = Ui_ScreenCapture()
            ui_screencapture.setupUi(screen_dialog)
            # set width and height of the dialog
            screen_dialog.setFixedWidth(400)

            screen_dialog.setWindowTitle(
                QCoreApplication.translate(
                    "MainWindow", "Screen Capture Selection", None
                )
            )
            # populate comboBox_window with the available windows
            ui_screencapture.comboBox_window.clear()
            ui_screencapture.comboBox_window.addItem(
                QCoreApplication.translate(
                    "MainWindow", "Capture the entire screen", None
                ),
                -1,
            )
            for window in ScreenCapture.list_windows():
                ui_screencapture.comboBox_window.addItem(window[0], window[1])
            screen_dialog.exec()
            # check if the dialog was accepted
            if screen_dialog.result() != QDialog.DialogCode.Accepted:
                self.ui.comboBox_camera_source.setCurrentIndex(0)
                return
            # get the window ID from the comboBox_window
            window_id = ui_screencapture.comboBox_window.currentData()
            self.source_name = window_id

        # store the source selection in scoresight.json
        self.globalSettingsChanged("source_selected", self.source_name)
        self.sourceSelectionSucessful()

    def itemSelected(self, item_name):
        # select the item in the tableWidget_boxes
        items = self.ui.tableWidget_boxes.findItems(
            item_name, Qt.MatchFlag.MatchExactly
        )
        if len(items) == 0:
            return
        item = items[0]
        item.setSelected(True)
        self.ui.tableWidget_boxes.setCurrentItem(item)
        self.listItemClicked(item)

    def fourCornersApplied(self, corners):
        # check the button
        self.ui.pushButton_fourCorner.setChecked(True)

    def sourceSelectionSucessful(self):
        if self.ui.comboBox_camera_source.currentIndex() == 0:
            return

        self.ui.frame_source_view.setEnabled(False)

        if self.ui.comboBox_camera_source.currentIndex() == 1:
            if self.source_name is None or not path.exists(self.source_name):
                logger.error("No file selected")
                self.ui.comboBox_camera_source.setCurrentIndex(0)
                return
            logger.info("Loading file selected: %s", self.source_name)
            camera_info = CameraInfo(
                self.source_name,
                self.source_name,
                self.source_name,
                CameraInfo.CameraType.FILE,
            )
        elif self.ui.comboBox_camera_source.currentIndex() == 2:
            if self.source_name is None or self.source_name == "":
                logger.error("No url entered")
                self.ui.comboBox_camera_source.setCurrentIndex(0)
                return
            logger.info("Loading url: %s", self.source_name)
            camera_info = CameraInfo(
                self.source_name,
                self.source_name,
                self.source_name,
                CameraInfo.CameraType.URL,
            )
        elif self.ui.comboBox_camera_source.currentIndex() == 3:
            if self.source_name is None:
                logger.error("No screen capture selected")
                self.ui.comboBox_camera_source.setCurrentIndex(0)
                return
            logger.info("Loading screen capture: %s", self.source_name)
            camera_info = CameraInfo(
                self.source_name,
                self.source_name,
                self.source_name,
                CameraInfo.CameraType.SCREEN_CAPTURE,
            )
        else:
            if self.ui.comboBox_camera_source.currentData() is None:
                return

            logger.info(
                "Loading camera: %s", self.ui.comboBox_camera_source.currentText()
            )
            camera_info = self.ui.comboBox_camera_source.currentData()

        if self.image_viewer:
            # remove the image viewer from the layout frame_for_source_view_label
            self.ui.frame_for_source_view_label.layout().removeWidget(self.image_viewer)
            self.image_viewer.close()
            self.image_viewer = None

        # clear self.ui.frame_for_source_view_label
        clear_layout(self.ui.frame_for_source_view_label.layout())

        # set the pixmap to the image viewer
        self.image_viewer = ImageViewer(
            camera_info,
            self.fourCornersApplied,
            self.detectionTargetsStorage,
            self.itemSelected,
        )
        self.ui.pushButton_fourCorner.setEnabled(True)
        self.ui.pushButton_binary.setEnabled(True)
        self.ui.pushButton_fourCorner.toggled.connect(
            self.image_viewer.toggleFourCorner
        )
        self.ui.pushButton_binary.clicked.connect(self.image_viewer.toggleBinary)
        if self.image_viewer.timerThread:
            self.image_viewer.timerThread.ocr_result_signal.connect(self.ocrResult)
            self.image_viewer.timerThread.update_error.connect(self.updateError)
        self.image_viewer.first_frame_received_signal.connect(
            self.cameraConnectedEnableUI
        )
        self.ocrModelChanged(fetch_data("scoresight.json", "ocr_model", 1))

        # set the image viewer to the layout frame_for_source_view_label
        self.ui.frame_for_source_view_label.layout().addWidget(self.image_viewer)

    def cameraConnectedEnableUI(self):
        # enable groupBox_sb_info
        self.ui.groupBox_sb_info.setEnabled(True)
        self.ui.tableWidget_boxes.setEnabled(True)
        self.ui.frame_source_view.setEnabled(True)
        self.ui.widget_viewTools.setEnabled(True)

        # load the boxes from scoresight.json
        self.detectionTargetsStorage.loadBoxesFromStorage()
        self.updateError(None)

    def updateError(self, error):
        if not error:
            self.ui.statusbar.clearMessage()
            return
        # show the error in the status bar
        self.ui.statusbar.showMessage(error)
        self.ui.frame_source_view.setEnabled(True)
        self.ui.widget_viewTools.setEnabled(False)

    def ocrResult(self, results: list[TextDetectionTargetWithResult]):
        if not self.updateOCRResults:
            # don't update the results, the user has disabled updates
            return

        update_http_server(results)

        # update vmix
        self.vmixUpdater.update_vmix(results)

        # update the table widget value items
        for targetWithResult in results:
            if (
                targetWithResult.result_state
                == TextDetectionTargetWithResult.ResultState.Success
            ):
                items = self.ui.tableWidget_boxes.findItems(
                    targetWithResult.name, Qt.MatchFlag.MatchExactly
                )
                if len(items) == 0:
                    continue
                item = items[0]
                # get the value (1 column) of the item
                item = self.ui.tableWidget_boxes.item(item.row(), 1)
                item.setText(targetWithResult.result)

        if self.out_folder is None:
            return

        if not path.exists(path.abspath(self.out_folder)):
            self.out_folder = None
            remove_data("scoresight.json", "output_folder")
            logger.warning("Output folder does not exist")
            return

        # check if enough time has passed since last file save according to aggs per second
        if (
            datetime.datetime.now() - self.last_aggregate_save
        ).total_seconds() < 1.0 / self.ui.horizontalSlider_aggsPerSecond.value():
            return

        self.last_aggregate_save = datetime.datetime.now()

        # update the obs scene sources with the results, use update_text_source
        for targetWithResult in results:
            if targetWithResult.result is None:
                continue
            if (
                targetWithResult.settings is not None
                and "skip_empty" in targetWithResult.settings
                and targetWithResult.settings["skip_empty"]
                and len(targetWithResult.result) == 0
            ):
                continue
            if (
                targetWithResult.result_state
                != TextDetectionTargetWithResult.ResultState.Success
            ):
                continue

            if (
                self.obs_websocket_client is not None
                and targetWithResult.settings is not None
            ):
                # find the source name for the target from the default boxes
                update_text_source(
                    self.obs_websocket_client,
                    targetWithResult.settings["obs_source_name"],
                    targetWithResult.result,
                )

        # save the results to text files
        file_output.save_text_files(
            results, self.out_folder, self.ui.comboBox_appendMethod.currentIndex()
        )

        # save the results to a csv file
        if self.ui.checkBox_saveCsv.isChecked():
            file_output.save_csv(
                results,
                self.out_folder,
                self.ui.comboBox_appendMethod.currentIndex(),
                self.first_csv_append,
            )

        # save the results to an xml file
        if self.ui.checkBox_saveXML.isChecked():
            file_output.save_xml(
                results,
                self.out_folder,
            )

    def addBox(self):
        # add a new box to the tableWidget_boxes
        # find the number of custom boxes
        custom_boxes = []
        for i in range(self.ui.tableWidget_boxes.rowCount()):
            item = self.ui.tableWidget_boxes.item(i, 0)
            if item.text() not in [o["name"] for o in default_boxes]:
                custom_boxes.append(item.text())

        i = len(custom_boxes)
        new_box_name = f"Custom {i + 1}"
        custom_boxes_names = fetch_data("scoresight.json", "custom_boxes_names", [])
        # find if the name already exists
        while new_box_name in custom_boxes or new_box_name in custom_boxes_names:
            i += 1
            new_box_name = f"Custom {i + 1}"

        store_custom_box_name(new_box_name)
        item = QTableWidgetItem(
            QIcon(
                path.abspath(path.join(path.dirname(__file__), "icons/circle-x.svg"))
            ),
            new_box_name,
        )
        item.setData(Qt.ItemDataRole.UserRole, "unchecked")
        self.ui.tableWidget_boxes.insertRow(self.ui.tableWidget_boxes.rowCount())
        self.ui.tableWidget_boxes.setItem(
            self.ui.tableWidget_boxes.rowCount() - 1, 0, item
        )
        disabledItem = QTableWidgetItem()
        disabledItem.setFlags(Qt.ItemFlag.NoItemFlags)
        self.ui.tableWidget_boxes.setItem(
            self.ui.tableWidget_boxes.rowCount() - 1, 1, disabledItem
        )

    def removeCustomBox(self):
        item = self.ui.tableWidget_boxes.currentItem()
        if not item:
            logger.info("No item selected")
            return
        if item.column() != 0:
            item = self.ui.tableWidget_boxes.item(item.row(), 0)
        self.removeBox()
        remove_custom_box_name_in_storage(item.text())
        # only allow removing custom boxes
        if item.text() in [o["name"] for o in default_boxes]:
            logger.info("Cannot remove default box")
            return
        # remove the selected item from the tableWidget_boxes
        self.ui.tableWidget_boxes.removeRow(item.row())

    def editBoxName(self, item):
        if item.text() in [o["name"] for o in default_boxes]:
            return
        new_name, ok = QInputDialog.getText(
            self, "Edit Box Name", "New Name:", text=item.text()
        )
        if ok and new_name != "" and new_name != item.text():
            # check if name doesn't exist already
            for i in range(self.ui.tableWidget_boxes.rowCount()):
                if new_name == self.ui.tableWidget_boxes.item(i, 0).text():
                    logger.info("Name '%s' already exists", new_name)
                    return
            # rename the item in the detectionTargetsStorage
            if not self.detectionTargetsStorage.rename_item(item.text(), new_name):
                logger.info("Error renaming item in application storage")
                return
            # rename the item in the tableWidget_boxes
            item.setText(new_name)
            rename_custom_box_name_in_storage(item.text(), new_name)

    def makeBox(self):
        item = self.ui.tableWidget_boxes.currentItem()
        if not item:
            return
        # create a new box on self.image_viewer with the name of the selected item from the tableWidget_boxes
        # change the list icon to green checkmark
        item.setIcon(
            QIcon(
                path.abspath(
                    path.join(path.dirname(__file__), "icons/circle-check.svg")
                )
            )
        )
        item.setData(Qt.ItemDataRole.UserRole, "checked")
        self.listItemClicked(item)

        # get the size of the box from the name
        info = default_info_for_box_name(item.text())

        self.detectionTargetsStorage.add_item(
            TextDetectionTarget(
                info["x"],
                info["y"],
                info["width"],
                info["height"],
                item.text(),
                normalize_settings_dict({}, info),
            )
        )

    def removeBox(self):
        item = self.ui.tableWidget_boxes.currentItem()
        if not item:
            return
        # change the list icon to red x
        item.setIcon(
            QIcon(path.abspath(path.join(path.dirname(__file__), "icons/circle-x.svg")))
        )
        item.setData(Qt.ItemDataRole.UserRole, "unchecked")
        self.listItemClicked(item)
        self.detectionTargetsStorage.remove_item(item.text())

    def createOBSScene(self):
        self.ui.statusbar.showMessage("Creating OBS scene")
        # get the scene name from the lineEdit_sceneName
        scene_name = self.ui.lineEdit_sceneName.text()
        # clear or create a new scene
        create_obs_scene_from_export(self.obs_websocket_client, scene_name)
        self.ui.statusbar.showMessage("Finished creating scene")

    # on destroy, close the OBS connection
    def closeEvent(self, event):
        logger.info("Closing")
        if self.image_viewer:
            self.image_viewer.close()
            self.image_viewer = None

        if self.log_dialog:
            self.log_dialog.close()
            self.log_dialog = None

        # store the boxes to scoresight.json
        self.detectionTargetsStorage.saveBoxesToStorage()
        if self.obs_websocket_client:
            # destroy the client object
            self.obs_websocket_client = None

        super().closeEvent(event)


if __name__ == "__main__":
    # only attempt splash when not on Mac OSX
    os_name = platform.system()
    if os_name != "Darwin":
        try:
            import pyi_splash  # type: ignore

            pyi_splash.close()
        except ImportError:
            pass
    app = QApplication(sys.argv)

    # Get system locale
    locale = QLocale.system().name()

    # Load the translation file based on the locale
    translator = QTranslator()
    locale_file = path.abspath(
        path.join(path.dirname(__file__), "translations", f"scoresight_{locale}.qm")
    )
    # check if the file exists
    if not path.exists(locale_file):
        # load the default translation file
        locale_file = path.abspath(
            path.join(path.dirname(__file__), "translations", "scoresight_en_US.qm")
        )
    if translator.load(locale_file):
        app.installTranslator(translator)

    # show the main window
    mainWindow = MainWindow(translator, app)
    mainWindow.show()

    app.exec()
    logger.info("Exiting...")

    stop_http_server()
