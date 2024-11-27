from qgis.PyQt.QtWidgets import QAction, QFileDialog, QMessageBox, QInputDialog, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QComboBox
from qgis.PyQt.QtGui import QIcon  # Import QIcon to set an icon for the action
from qgis.core import QgsProject, QgsRectangle, QgsVectorLayer, QgsCoordinateReferenceSystem, QgsCoordinateTransform
from PyQt5.QtCore import pyqtSignal, QObject, Qt, QThread
import duckdb
import os
import threading
import resources_rc
from pathlib import Path

class Worker(QObject):
    finished = pyqtSignal()
    error = pyqtSignal(str)
    load_layer = pyqtSignal(str)

    def __init__(self, dataset_url, extent, output_file, iface):
        super().__init__()
        self.dataset_url = dataset_url
        self.extent = extent
        self.output_file = output_file
        self.iface = iface
        self.killed = False

    def run(self):
        # Create source and destination CRS objects
        source_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
        dest_crs = QgsCoordinateReferenceSystem("EPSG:4326")

        # Create transform if needed
        if source_crs != dest_crs:
            transform = QgsCoordinateTransform(source_crs, dest_crs, QgsProject.instance())
            bbox = transform.transformBoundingBox(self.extent)
        else:
            bbox = self.extent

        conn = duckdb.connect()
        try:
            # Install and load the spatial extension
            conn.execute("INSTALL spatial;")
            conn.execute("LOAD spatial;")

            # Check if bbox column exists
            schema_query = f"DESCRIBE SELECT * FROM read_parquet('{self.dataset_url}')"
            schema_result = conn.execute(schema_query).fetchall()
            has_bbox = any(row[0].lower() == 'bbox' for row in schema_result)
            
            if not has_bbox:
                self.error.emit("This plugin currently only supports GeoParquet 1.1 files with a bbox column. " +
                              "Other GeoParquet formats are not yet supported.")
                return

            # TODO: Make this bounds check quicker, and move to validation# Quick bounds check
            if (False):
                bounds_query = f"""
                SELECT MIN(bbox.xmin) as min_x, MAX(bbox.xmax) as max_x,
                    MIN(bbox.ymin) as min_y, MAX(bbox.ymax) as max_y
                FROM read_parquet('{self.dataset_url}')
                """
                bounds_result = conn.execute(bounds_query).fetchone()
                
                if bounds_result and not self.killed:
                    min_x, max_x, min_y, max_y = bounds_result
                    request_bounds = (bbox.xMinimum(), bbox.xMaximum(), 
                                    bbox.yMinimum(), bbox.yMaximum())
                    
                    # Check for overlap
                    if (max_x < request_bounds[0] or min_x > request_bounds[1] or
                        max_y < request_bounds[2] or min_y > request_bounds[3]):
                        self.error.emit("The current view extent does not overlap with the data. " +
                                    f"\nData bounds: {min_x:.2f}, {min_y:.2f}, {max_x:.2f}, {max_y:.2f}" +
                                    f"\nRequested bounds: {request_bounds[0]:.2f}, {request_bounds[2]:.2f}, {request_bounds[1]:.2f}, {request_bounds[3]:.2f}")
                        return

            # Continue with regular query if bounds overlap
            select_query = "SELECT *"
            if not self.output_file.endswith(".parquet"):

                # Get the schema of the dataset to identify column types
                schema_query = f"DESCRIBE SELECT * FROM read_parquet('{self.dataset_url}')"
                schema_result = conn.execute(schema_query).fetchall()

                # Construct the SELECT clause with array conversion to strings
                columns = []
                for row in schema_result:
                    col_name = row[0]
                    col_type = row[1]
                    
                    
                    if 'STRUCT' in col_type.upper() or 'MAP' in col_type.upper():
                        columns.append(f"TO_JSON({col_name}) AS {col_name}")
                    elif '[]' in col_type:  # Check for array types like VARCHAR[]
                        columns.append(f"array_to_string({col_name}, ', ') AS {col_name}")
                    else:
                        columns.append(col_name)

                   # When we support more than overture just select the primary name when it's o

                    select_query = f"SELECT names.primary as name,{', '.join(columns)}"
            # Construct WHERE clause based on presence of bbox
            if has_bbox:
                where_clause = f"""
                WHERE bbox.xmin BETWEEN {bbox.xMinimum()} AND {bbox.xMaximum()}
                AND bbox.ymin BETWEEN {bbox.yMinimum()} AND {bbox.yMaximum()}
                """
            else:
                # Right now this will only work against epsg:4326 data - if we want to make it more robust
                # then should try to transform. Or else tell users the BBOX in v1.1 is required...
                where_clause = f"""
                WHERE ST_Intersects(
                    geometry,
                    ST_GeomFromText('POLYGON(({bbox.xMinimum()} {bbox.yMinimum()},
                                            {bbox.xMaximum()} {bbox.yMinimum()},
                                            {bbox.xMaximum()} {bbox.yMaximum()},
                                            {bbox.xMinimum()} {bbox.yMaximum()},
                                            {bbox.xMinimum()} {bbox.yMinimum()}))')
                )
                """

            # Base query
            base_query = f"""
            COPY (
                {select_query} FROM read_parquet('{self.dataset_url}')
                {where_clause}
            ) TO '{self.output_file}' 
            """

            # Format-specific options
            if self.output_file.endswith(".parquet"):
                format_options = "(FORMAT 'parquet', COMPRESSION 'ZSTD');"
            elif self.output_file.endswith(".gpkg"):
                format_options = "(FORMAT GDAL, DRIVER 'GPKG');"
            else:
                self.error.emit("Unsupported file format.")
            
            if self.killed:
                return

            # Complete query
            copy_query = base_query + format_options

            # Print the SQL query
            print("Executing SQL query:")
            print(copy_query)
            conn.execute(copy_query)

            if not self.killed:
                self.load_layer.emit(self.output_file)
                self.finished.emit()

        except Exception as e:
            if not self.killed:
                self.error.emit(str(e))
        finally:
            conn.close()

    def kill(self):
        self.killed = True

class DataSourceDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GeoParquet Data Source")
        self.setMinimumWidth(500)
        
        # Create layout
        layout = QVBoxLayout()
        
        # Preset URLs dropdown
        preset_layout = QHBoxLayout()
        preset_label = QLabel("Preset Sources:")
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("Custom URL...", "")
        self.preset_combo.addItem("Overture Places", "s3://overturemaps-us-west-2/release/2024-11-13.0/theme=places/type=place/*")
        self.preset_combo.addItem("Overture Roads", "s3://overturemaps-us-west-2/release/2024-11-13.0/theme=transportation/type=segment/*")
        self.preset_combo.addItem("Overture Buildings", "s3://overturemaps-us-west-2/release/2024-11-13.0/theme=buildings/type=building/*")
        preset_layout.addWidget(preset_label)
        preset_layout.addWidget(self.preset_combo)
        layout.addLayout(preset_layout)
        
        # URL input
        url_layout = QHBoxLayout()
        url_label = QLabel("Data URL:")
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Enter URL to Parquet file or folder (s3://, https://, or file://)")
        url_layout.addWidget(url_label)
        url_layout.addWidget(self.url_input)
        layout.addLayout(url_layout)
        
        # Buttons
        button_layout = QHBoxLayout()
        self.ok_button = QPushButton("OK")
        self.cancel_button = QPushButton("Cancel")
        button_layout.addWidget(self.ok_button)
        button_layout.addWidget(self.cancel_button)
        layout.addLayout(button_layout)
        
        self.setLayout(layout)
        
        # Connect signals
        self.preset_combo.currentIndexChanged.connect(self.preset_selected)
        self.ok_button.clicked.connect(self.validate_and_accept)
        self.cancel_button.clicked.connect(self.reject)
        
        # Initialize state
        self.preset_selected(0)
        
    def preset_selected(self, index):
        preset_url = self.preset_combo.currentData()
        if preset_url:  # If it's a preset
            self.url_input.setText(preset_url)
            self.url_input.setEnabled(False)
        else:  # If it's "Custom URL..."
            self.url_input.clear()
            self.url_input.setEnabled(True)
            
    def validate_and_accept(self):
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "Validation Error", "Please enter a URL")
            return
            
        # Skip validation for preset URLs
        if self.preset_combo.currentIndex() > 0:
            self.accept()
            return
            
        try:
            conn = duckdb.connect()
            conn.execute("INSTALL spatial;")
            conn.execute("LOAD spatial;")
            
            # Try to read the schema
            schema_query = f"DESCRIBE SELECT * FROM read_parquet('{url}')"
            schema_result = conn.execute(schema_query).fetchall()
            
            # Check if it's spatial data with bbox
            has_bbox = any(row[0].lower() == 'bbox' for row in schema_result)
            
            if not has_bbox:
                QMessageBox.warning(self, "Validation Error", 
                    "This plugin currently only supports GeoParquet with v1.1 bbox columns.")
                return

            bbox_col = next((row for row in schema_result if row[0].lower() == 'bbox'), None)
            
            # TODO: get this working right, maybe move location. 
            #            has_valid_bbox = False
            #            if bbox_col:
            #                col_type = bbox_col[1].upper()
            #                has_valid_bbox = (
            #                    'STRUCT' in col_type and 
            #                    all(field in col_type for field in ['XMIN', 'XMAX', 'YMIN', 'YMAX']) and
            #                    all(field in col_type for field in ['FLOAT', 'DOUBLE'])
            #                )
            
            #            if not has_valid_bbox:
            #                QMessageBox.warning(self, "Validation Error",
            #                    "This GeoParquet file does not have a properly formatted bbox column. " +
            #                    "The bbox column must be a STRUCT with xmin, xmax, ymin, ymax fields of type float or double.")
            #                return

            self.accept()
            
        except Exception as e:
            QMessageBox.critical(self, "Validation Error", f"Error validating source: {str(e)}")
        finally:
            conn.close()
            
    def get_url(self):
        return self.url_input.text().strip()

class QgisPluginGeoParquet:
    def __init__(self, iface):
        self.iface = iface
        self.worker = None
        self.worker_thread = None
        self.action = None
        # Create a default downloads directory in user's home directory
        self.download_dir = Path.home() / "Downloads" 
        # Create the directory if it doesn't exist
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def initGui(self):
        # Create the action with the new icon and tooltip
        self.action = QAction(QIcon(':/qgis_plugin_gpq_downloader/icons/download.svg'), "Download GeoParquet Data", self.iface.mainWindow())
        self.action.setToolTip("Download GeoParquet Data")
        self.action.triggered.connect(self.run)
        # Add the action to the toolbar
        self.iface.addToolBarIcon(self.action)
        # Optionally, add the action to a custom toolbar
        # self.toolbar = self.iface.addToolBar("GeoParquet")
        # self.toolbar.addAction(self.action)
        # Remove the menu-related code
        # self.iface.addPluginToMenu("GeoParquet Plugin", self.action)

    def unload(self):
        # Clean up worker and thread when plugin is unloaded
        self.cleanup_thread()
        # Remove the action from the toolbar
        self.iface.removeToolBarIcon(self.action)
        # Remove the custom toolbar if used
        # del self.toolbar
        # Remove the menu-related code
        # self.iface.removePluginMenu("GeoParquet Plugin", self.action)

    def run(self):
        # Show the data source dialog
        dialog = DataSourceDialog(self.iface.mainWindow())
        if dialog.exec_() != QDialog.Accepted:
            return
        
        dataset_url = dialog.get_url()
        
        # Get the current canvas extent
        extent = self.iface.mapCanvas().extent()
        
        # Generate default filename
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_filename = f"geoparquet_download_{timestamp}.parquet"
        default_save_path = str(self.download_dir / default_filename)
        
        # Show save file dialog
        output_file, _ = QFileDialog.getSaveFileName(
            self.iface.mainWindow(),
            "Save GeoParquet Data",
            default_save_path,
            "GeoParquet (*.parquet);;GeoPackage (*.gpkg)"
        )
        
        if output_file:
            self.download_and_save(dataset_url, extent, output_file)

    def download_and_save(self, dataset_url, extent: QgsRectangle, output_file: str):
        # Clean up any existing worker/thread
        if self.worker_thread is not None:
            self.cleanup_thread()

        # Create new worker and thread
        self.worker = Worker(dataset_url, extent, output_file, self.iface)
        self.worker_thread = QThread()
        
        # Move worker to thread
        self.worker.moveToThread(self.worker_thread)
        
        # Connect signals
        self.worker_thread.started.connect(self.worker.run)
        self.worker.error.connect(lambda message: QMessageBox.critical(self.iface.mainWindow(), "Error", message))
        self.worker.load_layer.connect(self.load_layer)
        self.worker.finished.connect(self.cleanup_thread)
        
        # Start the thread
        self.worker_thread.start()

    def cleanup_thread(self):
        if self.worker_thread is not None:
            self.worker.kill()
            self.worker_thread.quit()
            self.worker_thread.wait()
            self.worker_thread = None
            self.worker = None

    def load_layer(self, output_file):
        """Load the layer into QGIS"""
        layer = QgsVectorLayer(output_file, "Downloaded Layer", "ogr")
        if not layer.isValid():
            QMessageBox.critical(self.iface.mainWindow(), "Error", "Failed to load the layer.")
            return

        QgsProject.instance().addMapLayer(layer)

def classFactory(iface):
    return QgisPluginGeoParquet(iface)
