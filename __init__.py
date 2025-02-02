import os
import platform
import subprocess
import sys
from pathlib import Path

from qgis.PyQt.QtWidgets import QProgressBar
from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import Qgis
from qgis.utils import iface


# TODO: Either remove this or get it working - it seems like it was messing things up.
def install_duckdb_extensions():
    try:
        conn = duckdb.connect(':memory:')
        conn.execute("INSTALL httpfs;")
        conn.execute("INSTALL spatial;")
        conn.close()
        print("DuckDB extensions installed successfully")
        return True
    except Exception as e:
        print(f"Failed to install DuckDB extensions: {str(e)}")
        return False

def ensure_duckdb():
    try:
        import duckdb
        version = duckdb.__version__
        from packaging import version as version_parser
        
        if version_parser.parse(version) >= version_parser.parse("1.1.0"):
            print(f"DuckDB {version} already installed")
            return True #install_duckdb_extensions()
        else:
            print(f"DuckDB {version} found but needs upgrade to 1.1.0+")
            raise ImportError("Version too old")
            
    except ImportError:
        print("DuckDB not found or needs upgrade, attempting to install/upgrade...")
        try:
            qgis_bin = os.path.dirname(sys.executable)
            if platform.system() == "Windows":
                py_path = os.path.join(qgis_bin, "python.exe")
            elif platform.system() == "Darwin":
                # Search for python3 in common QGIS Mac locations
                possible_paths = [
                    os.path.join(qgis_bin, "python3"),
                    os.path.join(qgis_bin, "bin", "python3"),
                    os.path.join(qgis_bin, "Resources", "python", "bin", "python3")
                ]
                py_path = next((path for path in possible_paths if os.path.exists(path)), sys.executable)
            else:
                py_path = sys.executable

            msg_bar = iface.messageBar()
            progress = QProgressBar()

            # Set progress bar to be infinite
            progress.setMinimum(0)
            progress.setMaximum(0)
            progress.setValue(0)

            msg = msg_bar.createMessage("Installing DuckDB...")
            msg.layout().addWidget(progress)
            msg_bar.pushWidget(msg)
            QCoreApplication.processEvents()

            subprocess.check_call([py_path, "-m", "pip", "install", "--user", "duckdb"])

            msg_bar.clearWidgets()

            # Force Python to reload all modules to pick up the new installation
            import importlib
            importlib.invalidate_caches()
            
            # Try importing again
            import duckdb
            msg_bar.pushSuccess("Success", f"DuckDB {duckdb.__version__} installed successfully")
            print(f"DuckDB {duckdb.__version__} installed successfully")
            return True; #install_duckdb_extensions()
        except Exception as e:
            msg_bar.clearWidgets()
            msg_bar.pushWarning("Error", f"Failed to install/upgrade DuckDB")
            print(f"Failed to install/upgrade DuckDB: {str(e)}")
            return False

# Try to install/import duckdb
if not ensure_duckdb():
    print("Failed to setup DuckDB")

# Setup the path for duckdb
plugin_dir = os.path.dirname(__file__)
ext_libs_path = os.path.join(plugin_dir, 'ext-libs')
duckdb_path = os.path.join(ext_libs_path, 'duckdb')

# Add paths to sys.path if they're not already there
for path in [ext_libs_path, duckdb_path]:
    if path not in sys.path:
        sys.path.insert(0, path)

def classFactory(iface):
    from .qgis_plugin_gpq_downloader import QgisPluginGeoParquet
    return QgisPluginGeoParquet(iface)
