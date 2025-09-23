"""
Extract printer, process, and filament profiles from Bambu Studio 3MF files
with proper inheritance resolution from BambuStudio application directories
"""

__version__ = "0.1.0"

from .extractor import BambuProfileExtractor

__all__ = ['BambuProfileExtractor']