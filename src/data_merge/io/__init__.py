"""Edge I/O: the only place in the package that touches the filesystem for output."""

from data_merge.io.csv_io import CsvIO, InPlaceWriteError
from data_merge.io.manifest import Manifest, ManifestEntry

__all__ = ["CsvIO", "InPlaceWriteError", "Manifest", "ManifestEntry"]
