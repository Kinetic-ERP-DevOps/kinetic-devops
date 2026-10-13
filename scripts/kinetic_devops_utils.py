#!/usr/bin/env python3
"""
kinetic_devops_utils.py

Lightweight utilities for scripts using kinetic_devops.

Provides:
  1. KineticScriptLogger — centralized file+console logging
  2. Simple config retrieval wrapper (optional convenience)

For API calls, use service clients directly:
  - kinetic_devops.boreader_service.BOReaderService
  - Or KineticBaseClient.execute_request() for OData/custom endpoints
"""

from datetime import datetime
from pathlib import Path
from typing import Optional


class KineticScriptLogger:
    """Centralized logging for SDK-based scripts."""
    
    def __init__(self, log_file: Optional[Path] = None):
        self.log_file = log_file or Path("kinetic_script.log")
        self._init_log_file()
    
    def _init_log_file(self):
        """Initialize the log file."""
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
    
    def _log(self, level: str, message: str, newline: bool = True):
        """Write log message to file and console."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        prefix = f"[{timestamp}] [{level}] " if level else ""
        
        output = prefix + message
        
        if newline:
            print(output)
            with open(self.log_file, "a") as f:
                f.write(output + "\n")
        else:
            print(output, end="", flush=True)
            with open(self.log_file, "a") as f:
                f.write(output)
    
    def info(self, msg: str, newline: bool = True):
        """Log info level."""
        self._log("INFO", msg, newline)
    
    def debug(self, msg: str, newline: bool = True):
        """Log debug level."""
        self._log("DEBUG", msg, newline)
    
    def error(self, msg: str):
        """Log error level."""
        self._log("ERROR", msg)
    
    def section(self, title: str):
        """Log a section header."""
        self._log("", "=" * 60)
        self._log("", title)
        self._log("", "=" * 60)


# Example usage:
# 
# from kinetic_devops_utils import KineticScriptLogger
# from kinetic_devops.base_client import KineticBaseClient
# from kinetic_devops.boreader_service import BOReaderService
# from pathlib import Path
#
# logger = KineticScriptLogger(Path("operation.log"))
# logger.section("Fetching Data")
#
# client = KineticBaseClient()  # Interactive init: prompts for env, user, password, company
# logger.info(f"Connected to {client.config['url']}")
#
# # Use service directly
# service = BOReaderService(
#     base_url=client.config['url'],
#     headers=client.build_headers(client.config['token'], client.config['api_key'], client.config['company'])
# )
#
# result = service.get_list(client.config['company'], {
#     'serviceNamespace': 'Ice.Lib.SomeSvc',
#     'whereClause': 'Status = \'A\'',
#     'columnList': ['OrderNum', 'OrderDate']
# })
#
# logger.info(f"Retrieved {len(result.get('value', []))} records")


