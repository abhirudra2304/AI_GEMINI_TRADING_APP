"""
Data Package for Historical Constituents and Management.

This package provides a clean interface for downloading, cleaning, and accessing
point-in-time historical index constituent data to mitigate survivorship bias
in backtesting.
"""
from .provider import ConstituentProvider
from .downloader import download_historical_constituents
from .cleaner import run_cleanup

# Define what gets imported with 'from data import *'
__all__ = [
    'ConstituentProvider',
    'download_historical_constituents',
    'run_cleanup',
]