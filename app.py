import logging
import threading
from fastapi import FastAPI, Request, UploadFile, Form, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
import mimetypes
from openai import AzureOpenAI
from typing import Optional, List, Dict, Any, Tuple
import os
from datetime import datetime
import time
import base64
import mimetypes
import traceback
import asyncio
import json
from io import StringIO
import sys
import re
from docx import Document
from docx.shared import Inches
from PIL import Image
from fastapi.responses import Response, FileResponse
from fastapi.staticfiles import StaticFiles
import hashlib
import shutil
import PyPDF2
import chardet
from bs4 import BeautifulSoup
import pandas as pd
import io
import uuid
import tempfile
import platform
# Simple status updates for long-running operations
operation_statuses = {}


# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure based on your needs
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Type", "Content-Length"]
)
# Azure OpenAI client configuration
AZURE_ENDPOINT = "https://prodhubfinnew-openai-97de.openai.azure.com/" # Replace with your endpoint if different
AZURE_API_KEY = "97fa8c02f9e64e8ea5434987b11fe6f4" # Replace with your key if different
AZURE_API_VERSION = "2024-12-01-preview"
DOWNLOADS_DIR = "/tmp/chat_downloads"  # Use /tmp for Azure App Service
MAX_DOWNLOAD_FILES = 10  # Keep only 10 most recent files

# Create downloads directory if it doesn't exist
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Mount static files directory for serving downloads
#app.mount("/download-files", StaticFiles(directory=DOWNLOADS_DIR), name="download-files")
def get_downloads_directory():
    """Get the appropriate downloads directory based on the environment."""
    if os.getenv("AZURE_APP_SERVICE", "").lower() == "true":
        # Azure App Service - use home directory which is persistent
        home_dir = os.getenv("HOME", "/tmp")
        downloads_dir = os.path.join(home_dir, "chat_downloads")
    elif platform.system() == "Windows":
        # Windows development
        downloads_dir = os.path.join(tempfile.gettempdir(), "chat_downloads")
    else:
        # Linux/Mac - use /tmp for development
        downloads_dir = "/tmp/chat_downloads"
    
    # Ensure directory exists with proper permissions
    try:
        os.makedirs(downloads_dir, mode=0o755, exist_ok=True)
        
        # Test write permissions
        test_file = os.path.join(downloads_dir, ".write_test")
        with open(test_file, 'w') as f:
            f.write("test")
        os.remove(test_file)
        
        logging.info(f"Downloads directory initialized: {downloads_dir}")
        return downloads_dir
    except Exception as e:
        logging.error(f"Failed to initialize downloads directory {downloads_dir}: {e}")
        # Fallback to temp directory
        fallback_dir = os.path.join(tempfile.gettempdir(), "chat_downloads")
        os.makedirs(fallback_dir, mode=0o755, exist_ok=True)
        logging.info(f"Using fallback directory: {fallback_dir}")
        return fallback_dir

# Update the global variable
DOWNLOADS_DIR = get_downloads_directory()

def create_client():
    """Creates an AzureOpenAI client instance."""
    return AzureOpenAI(
        azure_endpoint=AZURE_ENDPOINT,
        api_key=AZURE_API_KEY,
        api_version=AZURE_API_VERSION,
    )
def save_download_file(content: bytes, filename: str) -> str:
    """
    Save a file for download with proper permissions.
    
    Args:
        content: File content as bytes
        filename: Desired filename
        
    Returns:
        Full path to the saved file
    """
    # Sanitize filename
    safe_filename = secure_filename(filename)
    filepath = os.path.join(DOWNLOADS_DIR, safe_filename)
    
    try:
        # Write file with explicit permissions
        with open(filepath, 'wb') as f:
            f.write(content)
        
        # Set file permissions to be readable by the web server
        os.chmod(filepath, 0o644)
        
        logging.info(f"Saved download file: {filepath} ({len(content)} bytes)")
        return filepath
        
    except Exception as e:
        logging.error(f"Failed to save download file {filename}: {e}")
        raise

def secure_filename(filename: str) -> str:
    """
    Sanitize a filename to be safe for filesystem storage.
    
    Args:
        filename: Original filename
        
    Returns:
        Sanitized filename
    """
    import re
    
    # Remove any path components
    filename = os.path.basename(filename)
    
    # Replace unsafe characters
    filename = re.sub(r'[^\w\s.-]', '_', filename)
    filename = re.sub(r'[\s]+', '_', filename)
    
    # Ensure it has a proper extension
    if '.' not in filename:
        filename += '.bin'
    
    # Add timestamp to ensure uniqueness
    name, ext = os.path.splitext(filename)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    return f"{name}_{timestamp}{ext}"
def extract_csv_from_content(content: str) -> str:
    """
    Extract CSV content from AI response, handling various formats.
    
    Args:
        content: The AI response that may contain CSV data
        
    Returns:
        Clean CSV content
    """
    import re
    
    # First, try to extract content from code blocks
    patterns = [
        r'```(?:csv|CSV|plain|text)?\n(.*?)\n```',  # Standard code block
        r'```\n(.*?)\n```',  # Generic code block
        r'`([^`]+)`',  # Inline code (fallback)
    ]
    
    for pattern in patterns:
        match = re.search(pattern, content, re.DOTALL | re.MULTILINE)
        if match:
            csv_content = match.group(1).strip()
            # Validate it looks like CSV (has commas or pipes)
            if ',' in csv_content or '|' in csv_content:
                return csv_content
    
    # If no code blocks found, check if the entire content looks like CSV
    lines = content.strip().split('\n')
    if len(lines) >= 2:  # At least header and one data row
        # Check if first line has delimiters
        first_line = lines[0]
        if ',' in first_line or '\t' in first_line or '|' in first_line:
            # Looks like CSV, return as is
            return content.strip()
    
    # Last resort: return content as is
    return content.strip()
def debug_print_files(self, thread_id: str):
    """
    Debug function to print information about files registered with the pandas agent.
    
    Args:
        thread_id (str): The thread ID to check files for
    
    Returns:
        str: Debug information about files and dataframes
    """
    import pandas as pd
    
    debug_output = []
    debug_output.append(f"=== PANDAS AGENT DEBUG INFO FOR THREAD {thread_id} ===")
    
    # Get file info
    file_info = self.file_info_cache.get(thread_id, [])
    debug_output.append(f"Registered Files: {len(file_info)}")
    
    for i, info in enumerate(file_info):
        file_name = info.get("name", "unnamed")
        file_path = info.get("path", "unknown")
        file_type = info.get("type", "unknown")
        debug_output.append(f"\n[{i+1}] File: {file_name} ({file_type})")
        debug_output.append(f"    Path: {file_path}")
        debug_output.append(f"    Exists: {os.path.exists(file_path)}")
        
        if os.path.exists(file_path):
            try:
                file_size = os.path.getsize(file_path)
                debug_output.append(f"    Size: {file_size} bytes")
                
                # Read first few bytes
                with open(file_path, 'rb') as f:
                    first_bytes = f.read(50)
                debug_output.append(f"    First bytes: {first_bytes}")
            except Exception as e:
                debug_output.append(f"    Error reading file: {str(e)}")
    
    # Get dataframe info
    dataframes = self.dataframes_cache.get(thread_id, {})
    debug_output.append(f"\nLoaded DataFrames: {len(dataframes)}")
    
    for df_name, df in dataframes.items():
        debug_output.append(f"\nDataFrame: {df_name}")
        try:
            debug_output.append(f"  Shape: {df.shape}")
            debug_output.append(f"  Columns: {list(df.columns)}")
            debug_output.append(f"  Types: {df.dtypes.to_dict()}")
            
            # Sample data (first 3 rows)
            debug_output.append(f"  Sample data (3 rows):")
            debug_output.append(f"{df.head(3).to_string()}")
            
            # Detect potential issues
            has_nulls = df.isnull().any().any()
            debug_output.append(f"  Contains nulls: {has_nulls}")
            
        except Exception as e:
            debug_output.append(f"  Error examining dataframe: {str(e)}")
    
    # Return as string
    return "\n".join(debug_output)
class PandasAgentManager:
    """
    Enhanced class to manage pandas agents and dataframes for different threads.
    Provides thread isolation, FIFO file storage, and prevents duplicate agent creation.
    """
    
    # Class-level singleton instance
    _instance = None
    
    @classmethod
    def get_instance(cls):
        """Get or create the singleton instance"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def __init__(self):
        """Initialize the manager"""
        # Cache for pandas agents by thread_id
        self.agents_cache = {}
        
        # Cache for dataframes by thread_id
        self.dataframes_cache = {}
        
        # Cache for file info by thread_id
        self.file_info_cache = {}
        
        # Cache for filepaths by thread_id
        self.file_paths_cache = {}
        
        # Maximum files per thread
        self.max_files_per_thread = 3
        
        # Initialize LangChain LLM
        self.langchain_llm = None
        
        # Check for required dependencies
        self._check_dependencies()
        
        logging.info("PandasAgentManager initialized")
    
    def _check_dependencies(self):
        """Check if required dependencies are available"""
        missing_deps = []
        
        try:
            import pandas as pd
            logging.info("Pandas version: %s", pd.__version__)
        except ImportError:
            missing_deps.append("pandas")
        
        try:
            import numpy as np
            logging.info("NumPy version: %s", np.__version__)
        except ImportError:
            missing_deps.append("numpy")
        
        try:
            import tabulate
            logging.info("Tabulate version: %s", tabulate.__version__)
        except ImportError:
            try:
                # Try to install tabulate automatically
                import subprocess
                logging.info("Installing missing tabulate dependency...")
                subprocess.check_call(["pip", "install", "tabulate"])
                logging.info("Successfully installed tabulate")
            except Exception as e:
                missing_deps.append("tabulate")
                logging.warning(f"Could not install tabulate: {str(e)}")
        
        try:
            # Try to import LangChain's create_pandas_dataframe_agent function
            try:
                from langchain_experimental.agents import create_pandas_dataframe_agent
                logging.info("Using langchain_experimental for pandas agent")
            except ImportError:
                try:
                    from langchain.agents import create_pandas_dataframe_agent
                    logging.info("Using langchain for pandas agent")
                except ImportError:
                    missing_deps.append("langchain[experimental]")
        except Exception as e:
            missing_deps.append("langchain components")
            logging.error(f"Error checking for LangChain: {str(e)}")
        
        if missing_deps:
            logging.warning(f"Missing dependencies: {', '.join(missing_deps)}")
            logging.warning("Install them with: pip install " + " ".join(missing_deps))
        
        return len(missing_deps) == 0
    
    def get_llm(self):
        """Get or initialize the LangChain LLM"""
        if self.langchain_llm is None:
            try:
                from langchain_openai import AzureChatOpenAI
                
                # Use the existing client configuration
                self.langchain_llm = AzureChatOpenAI(
                    azure_endpoint=AZURE_ENDPOINT,
                    api_key=AZURE_API_KEY,
                    api_version=AZURE_API_VERSION,
                    deployment_name="gpt-4o",
                    temperature=0
                )
                logging.info("Initialized LangChain LLM for pandas agents")
            except Exception as e:
                logging.error(f"Failed to initialize LangChain LLM: {e}")
                raise
        
        return self.langchain_llm
    
    def initialize_thread(self, thread_id: str):
        """Initialize storage for a thread if it doesn't exist yet"""
        if thread_id not in self.dataframes_cache:
            self.dataframes_cache[thread_id] = {}
        
        if thread_id not in self.file_info_cache:
            self.file_info_cache[thread_id] = []
            
        if thread_id not in self.file_paths_cache:
            self.file_paths_cache[thread_id] = []
    
    def remove_oldest_file(self, thread_id: str):
        """
        Remove the oldest file for a thread when max files is reached
        
        Args:
            thread_id (str): Thread ID
            
        Returns:
            str or None: Name of removed file or None
        """
        if thread_id not in self.file_info_cache or len(self.file_info_cache[thread_id]) <= self.max_files_per_thread:
            return None
        
        # Get the oldest file info
        oldest_file_info = self.file_info_cache[thread_id][0]
        oldest_file_name = oldest_file_info.get("name", "")
        
        # Remove the file info
        self.file_info_cache[thread_id].pop(0)
        
        # Remove the file path and delete file from disk
        if thread_id in self.file_paths_cache and len(self.file_paths_cache[thread_id]) > 0:
            oldest_path = self.file_paths_cache[thread_id].pop(0)
            # Delete the file from disk
            if os.path.exists(oldest_path):
                try:
                    os.remove(oldest_path)
                    logging.info(f"Deleted oldest file: {oldest_path} for thread {thread_id}")
                except Exception as e:
                    logging.error(f"Error deleting file {oldest_path}: {e}")
        
        # Remove any dataframes associated with this file
        if thread_id in self.dataframes_cache:
            # For exact filename match
            if oldest_file_name in self.dataframes_cache[thread_id]:
                del self.dataframes_cache[thread_id][oldest_file_name]
                
            # For Excel sheets with this filename
            keys_to_remove = []
            for key in self.dataframes_cache[thread_id].keys():
                if key.startswith(oldest_file_name + " [Sheet:"):
                    keys_to_remove.append(key)
            
            for key in keys_to_remove:
                del self.dataframes_cache[thread_id][key]
        
        # Invalidate the agent for this thread since dataframes changed
        if thread_id in self.agents_cache:
            self.agents_cache[thread_id] = None
            
        return oldest_file_name
    
    def add_file(self, thread_id, file_info):
        """
        Add a file to the thread, implementing FIFO if needed.
        Enhanced to prevent accidental deletion of files needed for follow-up queries.
        
        Args:
            thread_id (str): The thread ID
            file_info (dict): File information dictionary
            
        Returns:
            tuple: (dict of dataframes or None, error message or None, removed_file or None)
        """
        # Initialize thread storage if needed
        self.initialize_thread(thread_id)
        
        file_name = file_info.get("name", "unnamed_file")
        file_path = file_info.get("path", None)
        
        logging.info(f"Adding file '{file_name}' from path {file_path} to thread {thread_id}")
        
        # Verify file exists
        if not file_path or not os.path.exists(file_path):
            # Try to locate a matching file in /tmp with a more thorough search
            located_file_path = self._locate_file_in_tmp(file_name)
            if located_file_path:
                logging.info(f"Found alternative path for '{file_name}': {located_file_path}")
                file_path = located_file_path
                file_info["path"] = file_path
            else:
                error_msg = f"File path for '{file_name}' is invalid or does not exist (path: {file_path})"
                logging.error(error_msg)
                
                # Look for the safe copy that might have been created previously
                safe_pattern = f"/tmp/safe_*_{re.sub(r'[^a-zA-Z0-9.]', '_', file_name)}"
                try:
                    import glob
                    safe_copies = glob.glob(safe_pattern)
                    if safe_copies:
                        newest_safe_copy = max(safe_copies, key=os.path.getctime)
                        logging.info(f"Found safe copy for '{file_name}': {newest_safe_copy}")
                        file_path = newest_safe_copy
                        file_info["path"] = file_path
                    else:
                        return None, error_msg, None
                except Exception as e:
                    logging.error(f"Error looking for safe copies: {e}")
                    return None, error_msg, None
        
        # Check if we already have this file (same name)
        existing_file_names = [f.get("name", "") for f in self.file_info_cache[thread_id]]
        existing_file_index = None
        
        if file_name in existing_file_names:
            # File already exists - we'll treat this as an update, but ONLY if we have a valid new file
            logging.info(f"File '{file_name}' already exists for thread {thread_id} - updating")
            
            # Find existing file info but don't delete it yet
            for i, info in enumerate(self.file_info_cache[thread_id]):
                if info.get("name") == file_name:
                    existing_file_index = i
                    old_path = self.file_paths_cache[thread_id][i] if i < len(self.file_paths_cache[thread_id]) else None
                    
                    # If the paths are identical and the file exists, this is likely a follow-up query
                    # Just use the existing dataframe and don't try to reload
                    if old_path and old_path == file_path and os.path.exists(old_path):
                        logging.info(f"Using existing dataframe for '{file_name}' (follow-up query)")
                        if file_name in self.dataframes_cache[thread_id]:
                            return {file_name: self.dataframes_cache[thread_id][file_name]}, None, None
                    break
        
        # Track removed file (if any due to FIFO)
        removed_file = None
        
        # Apply FIFO if we exceed max files
        if len(self.file_info_cache[thread_id]) >= self.max_files_per_thread and existing_file_index is None:
            removed_file = self.remove_oldest_file(thread_id)
            if removed_file:
                logging.info(f"Removed oldest file '{removed_file}' for thread {thread_id} to maintain FIFO limit")
        
        # Load the dataframe(s)
        dfs_dict, error = self.load_dataframe_from_file(file_info)
        
        if error:
            # If there was an error loading the new dataframe but we have an existing one, keep using it
            if file_name in self.dataframes_cache[thread_id]:
                logging.info(f"Error loading updated file, falling back to existing dataframe for '{file_name}'")
                return {file_name: self.dataframes_cache[thread_id][file_name]}, None, removed_file
            
            return None, error, removed_file
            
        if dfs_dict:
            # If we're updating an existing file, remove the old one now that we have a valid replacement
            if existing_file_index is not None:
                old_info = self.file_info_cache[thread_id].pop(existing_file_index)
                
                # Remove the old path
                if existing_file_index < len(self.file_paths_cache[thread_id]):
                    old_path = self.file_paths_cache[thread_id].pop(existing_file_index)
                    
                    # Only delete the old file if it's different from the new one
                    if old_path and old_path != file_path and os.path.exists(old_path):
                        try:
                            # Create a backup before deleting
                            backup_path = f"{old_path}.bak"
                            with open(old_path, 'rb') as src, open(backup_path, 'wb') as dst:
                                dst.write(src.read())
                                
                            os.remove(old_path)
                            logging.info(f"Deleted old file: {old_path} for thread {thread_id} (backup at {backup_path})")
                        except Exception as e:
                            logging.error(f"Error deleting file {old_path}: {e}")
                
                # Remove old dataframes
                if file_name in self.dataframes_cache[thread_id]:
                    del self.dataframes_cache[thread_id][file_name]
                
                # Remove Excel sheets with this filename
                keys_to_remove = []
                for key in self.dataframes_cache[thread_id].keys():
                    if key.startswith(file_name + " [Sheet:"):
                        keys_to_remove.append(key)
                
                for key in keys_to_remove:
                    del self.dataframes_cache[thread_id][key]
            
            # Add dataframes to cache
            self.dataframes_cache[thread_id].update(dfs_dict)
            
            # Add file info to cache (append to end for FIFO ordering)
            self.file_info_cache[thread_id].append(file_info)
            
            # Add file path to cache
            if file_path:
                self.file_paths_cache[thread_id].append(file_path)
            
            # Reset agent to ensure it's recreated with new dataframes
            if thread_id in self.agents_cache:
                self.agents_cache[thread_id] = None
                
            logging.info(f"Added dataframe(s) for file '{file_name}' to thread {thread_id}")
            return dfs_dict, None, removed_file
        else:
            return None, f"Failed to load any dataframes from file '{file_name}'", removed_file

    def _locate_file_in_tmp(self, filename):
        """
        More thorough search for a file in the /tmp directory.
        
        Args:
            filename (str): Original filename to search for
            
        Returns:
            str or None: Path if found, None otherwise
        """
        try:
            # Try direct match first (pandas_agent_timestamp_filename)
            direct_matches = []
            prefix_matches = []
            partial_matches = []
            safe_copy_matches = []
            
            # Clean filename for pattern matching
            clean_filename = re.sub(r'[^a-zA-Z0-9.]', '_', filename)
            filename_lower = filename.lower()
            filename_parts = re.split(r'[_\s.-]', filename_lower)
            filename_parts = [p for p in filename_parts if len(p) > 2]  # Filter out very short parts
            
            for tmp_file in os.listdir('/tmp'):
                tmp_path = os.path.join('/tmp', tmp_file)
                if not os.path.isfile(tmp_path):
                    continue
                    
                tmp_lower = tmp_file.lower()
                
                # Direct pandas agent match
                if tmp_lower.startswith('pandas_agent_') and filename_lower in tmp_lower:
                    direct_matches.append(tmp_path)
                    
                # Safe copy match
                elif tmp_lower.startswith('safe_') and clean_filename.lower() in tmp_lower:
                    safe_copy_matches.append(tmp_path)
                    
                # Prefix match (any prefix + exact filename)
                elif tmp_lower.endswith(filename_lower):
                    prefix_matches.append(tmp_path)
                    
                # Partial match (filename parts)
                elif any(part in tmp_lower for part in filename_parts):
                    # Calculate match score: how many parts match
                    match_score = sum(1 for part in filename_parts if part in tmp_lower)
                    if match_score >= len(filename_parts) // 2:  # At least half the parts match
                        partial_matches.append((tmp_path, match_score))
            
            # Return best match in priority order
            if direct_matches:
                # Multiple matches - take newest
                return max(direct_matches, key=os.path.getctime)
            elif safe_copy_matches:
                return max(safe_copy_matches, key=os.path.getctime)
            elif prefix_matches:
                return max(prefix_matches, key=os.path.getctime)
            elif partial_matches:
                # Sort by match score (descending) and then by creation time (newest first)
                partial_matches.sort(key=lambda x: (-x[1], -os.path.getctime(x[0])))
                return partial_matches[0][0]
                
            return None
        except Exception as e:
            logging.error(f"Error locating file in /tmp: {e}")
            return None

    def load_dataframe_from_file(self, file_info):
        """
        Load dataframe(s) from file information with robust error handling.
        Enhanced with better file discovery and safe copy management.
        
        Args:
            file_info (dict): Dictionary containing file metadata
            
        Returns:
            tuple: (dict of dataframes, error message)
        """
        try:
            import pandas as pd
            import numpy as np
        except ImportError as e:
            logging.error(f"Required library not available: {e}")
            return None, f"Required library not available: {e}"
        
        file_type = file_info.get("type", "unknown")
        file_name = file_info.get("name", "unnamed_file")
        file_path = file_info.get("path", None)
        
        logging.info(f"Loading dataframe from file: {file_name} ({file_type}) from path: {file_path}")
        
        # Verify original file path
        if not file_path or not os.path.exists(file_path):
            # Try to find the file using our enhanced search method
            located_path = self._locate_file_in_tmp(file_name)
            
            if located_path:
                logging.info(f"Using alternative path for {file_name}: {located_path}")
                # Update file_info with the new path
                file_path = located_path
                file_info["path"] = file_path
            else:
                # Still couldn't find the file - create a detailed error
                error_msg = f"File '{file_name}' could not be found. Original path '{file_path}' does not exist and no matches found in /tmp."
                logging.error(error_msg)
                return None, error_msg
        
        try:
            # Verify file readability and log details
            file_size = os.path.getsize(file_path)
            with open(file_path, 'rb') as f_check:
                first_bytes = f_check.read(20)
            logging.info(f"File '{file_name}' exists, size: {file_size} bytes, first bytes: {first_bytes}")
            
            # Make a copy of the file with a simplified name to avoid path issues
            # Use a timestamp to ensure uniqueness and help track file age
            timestamp = int(time.time())
            simple_name = re.sub(r'[^a-zA-Z0-9.]', '_', file_name)  # Replace problematic chars with underscore
            safe_path = f"/tmp/safe_{timestamp}_{simple_name}"
            
            with open(file_path, 'rb') as src, open(safe_path, 'wb') as dst:
                dst.write(src.read())
            
            logging.info(f"Created safe copy of file at: {safe_path}")
            
            # Update file_path to use the safe copy
            file_path = safe_path
            file_info["path"] = safe_path
            
            # Rest of the existing file loading code 
            if file_type == "csv":
                # Try with different encodings and delimiters for robustness
                encodings = ['utf-8', 'latin-1', 'iso-8859-1']
                delimiters = [',', ';', '\t', '|']
                
                df = None
                error_msgs = []
                successful_encoding = None
                successful_delimiter = None
                
                # Try each encoding
                for encoding in encodings:
                    if df is not None:
                        break
                    
                    for delimiter in delimiters:
                        try:
                            # Use the safe path directly
                            df = pd.read_csv(safe_path, encoding=encoding, sep=delimiter, low_memory=False)
                            
                            if len(df.columns) > 1:  # Successfully parsed with >1 column
                                logging.info(f"Successfully loaded CSV with encoding {encoding} and delimiter '{delimiter}'")
                                successful_encoding = encoding
                                successful_delimiter = delimiter
                                break
                        except Exception as e:
                            error_msgs.append(f"Failed with {encoding}/{delimiter}: {str(e)}")
                            continue
                
                if df is None:
                    detailed_error = " | ".join(error_msgs[:5])
                    return None, f"Failed to load CSV file with any encoding/delimiter combination. Errors: {detailed_error}"
                
                # Clean up column names
                df.columns = df.columns.str.strip()
                
                # Replace NaN values with None for better handling
                df = df.replace({np.nan: None})
                
                # Log dataframe info for debugging
                logging.info(f"CSV loaded successfully. Shape: {df.shape}, Columns: {list(df.columns)}")
                logging.info(f"Used encoding: {successful_encoding}, delimiter: '{successful_delimiter}'")
                
                # Return with original filename as key
                return {file_name: df}, None
                
            elif file_type == "excel":
                # Excel loading code remains the same, but use safe_path
                result_dfs = {}
                
                try:
                    xls = pd.ExcelFile(safe_path)
                    sheet_names = xls.sheet_names
                    logging.info(f"Excel file contains {len(sheet_names)} sheets: {sheet_names}")
                except Exception as e:
                    return None, f"Error accessing Excel file: {str(e)}"
                
                if len(sheet_names) == 1:
                    # Single sheet - load directly with the filename as key
                    try:
                        df = pd.read_excel(safe_path, engine='openpyxl')
                        df.columns = df.columns.str.strip()
                        df = df.replace({np.nan: None})
                        result_dfs[file_name] = df
                        
                        # Log dataframe info for debugging
                        logging.info(f"Excel sheet loaded successfully. Shape: {df.shape}, Columns: {list(df.columns)}")
                    except Exception as e:
                        return None, f"Error reading Excel sheet: {str(e)}"
                else:
                    # Multiple sheets - load each sheet with a compound key
                    for sheet in sheet_names:
                        try:
                            df = pd.read_excel(safe_path, sheet_name=sheet, engine='openpyxl')
                            df.columns = df.columns.str.strip()
                            df = df.replace({np.nan: None})
                            
                            # Create a key that includes the sheet name
                            sheet_key = f"{file_name} [Sheet: {sheet}]"
                            result_dfs[sheet_key] = df
                            
                            # Log dataframe info for debugging
                            logging.info(f"Excel sheet '{sheet}' loaded successfully. Shape: {df.shape}, Columns: {list(df.columns)}")
                        except Exception as e:
                            logging.error(f"Error reading sheet '{sheet}' in {file_name}: {str(e)}")
                            # Continue with other sheets even if one fails
                
                if result_dfs:
                    return result_dfs, None
                else:
                    return None, "Failed to load any sheets from Excel file"
            else:
                return None, f"Unsupported file type: {file_type}"
                
        except Exception as e:
            error_msg = f"Error loading '{file_name}': {str(e)}"
            logging.error(f"{error_msg}\n{traceback.format_exc()}")
            return None, error_msg
    def get_or_create_agent(self, thread_id):
        """
        Get or create a pandas agent for a thread with clear dataframe reference instructions.
        
        Args:
            thread_id (str): Thread ID
            
        Returns:
            tuple: (agent, dataframes, errors)
        """
        # Initialize thread storage if needed
        self.initialize_thread(thread_id)
        
        # Import required modules
        try:
            # Try langchain.agents first (more stable)
            try:
                from langchain.agents import create_pandas_dataframe_agent
                from langchain.agents import AgentType
                agent_module = "langchain.agents"
            except ImportError:
                # Fall back to experimental if needed
                from langchain_experimental.agents import create_pandas_dataframe_agent
                from langchain.agents import AgentType
                agent_module = "langchain_experimental.agents"
                
            import pandas as pd
            logging.info(f"Using {agent_module} module for pandas agent creation")
        except ImportError as e:
            return None, None, [f"Required libraries not available: {str(e)}"]
        
        # Initialize the LLM
        try:
            llm = self.get_llm()
        except Exception as e:
            return None, None, [f"Failed to initialize LLM: {str(e)}"]
        
        # Check if we have any dataframes
        if not self.dataframes_cache[thread_id]:
            return None, None, ["No dataframes available for analysis"]
        
        # Create or update the agent if needed
        if thread_id not in self.agents_cache or self.agents_cache[thread_id] is None:
            try:
                # Get all dataframes and file names
                dfs = self.dataframes_cache[thread_id]
                
                # Log the dataframes we're dealing with
                df_names = list(dfs.keys())
                logging.info(f"Creating pandas agent for thread {thread_id} with dataframes: {df_names}")
                
                # Based on the documentation example, use ZERO_SHOT_REACT_DESCRIPTION agent type
                if len(dfs) == 1:
                    # For a single dataframe, use a simpler approach
                    df_name = list(dfs.keys())[0]
                    df = dfs[df_name]
                    
                    # Log dataframe info for debugging
                    logging.info(f"Creating single dataframe agent for '{df_name}', shape: {df.shape}")
                    
                    try:
                        # First try safe approach - no variable renaming
                        self.agents_cache[thread_id] = create_pandas_dataframe_agent(
                            llm,
                            df,  # Pass the dataframe directly
                            verbose=True,
                            agent_type="tool-calling",  # Use standard agent type
                            handle_parsing_errors=True, allow_dangerous_code=True, max_iterations=30, max_execution_time=120
                        )
                        logging.info(f"Successfully created pandas agent for thread {thread_id} using standard approach")
                    except Exception as e1:
                        logging.warning(f"Error creating agent with standard approach: {e1}, trying alternative approach")
                        # Try alternative approach with tool-calling agent type
                        try:
                            self.agents_cache[thread_id] = create_pandas_dataframe_agent(
                                llm,
                                df,  # Pass the dataframe directly
                                verbose=True,
                                agent_type=AgentType.ZERO_SHOT_REACT_DESCRIPTION,  # Try alternative agent type
                                handle_parsing_errors=True, allow_dangerous_code=True, max_iterations=30, max_execution_time=120
                            )
                            logging.info(f"Successfully created pandas agent for thread {thread_id} using tool-calling approach")
                        except Exception as e2:
                            logging.error(f"Both agent creation approaches failed. Second error: {e2}")
                            raise Exception(f"Failed to create agent: {e1}; Alternative approach also failed: {e2}")
                
                else:
                    # For multiple dataframes
                    df_list = list(dfs.values())
                    
                    logging.info(f"Creating multi-dataframe agent with {len(df_list)} dataframes")
                    
                    try:
                        # First try standard approach
                        self.agents_cache[thread_id] = create_pandas_dataframe_agent(
                            llm,
                            df_list,  # Pass list of dataframes 
                            verbose=True,
                            agent_type="tool-calling",  # Use standard agent type
                            handle_parsing_errors=True, allow_dangerous_code=True, max_iterations=30, max_execution_time=120 
                        )
                        logging.info(f"Successfully created multi-df pandas agent using standard approach")
                    except Exception as e1:
                        logging.warning(f"Error creating multi-df agent with standard approach: {e1}, trying alternative")
                        # Try alternative approach
                        try:
                            self.agents_cache[thread_id] = create_pandas_dataframe_agent(
                                llm,
                                df_list,  # Pass list of dataframes
                                verbose=True,
                                agent_type=AgentType.ZERO_SHOT_REACT_DESCRIPTION,  # Try alternative agent type
                                handle_parsing_errors=True, allow_dangerous_code=True, max_iterations=30, max_execution_time=120
                            )
                            logging.info(f"Successfully created multi-df pandas agent using tool-calling approach")
                        except Exception as e2:
                            logging.error(f"Both agent creation approaches failed. Second error: {e2}")
                            raise Exception(f"Failed to create agent: {e1}; Alternative approach also failed: {e2}")
                
            except Exception as e:
                error_msg = f"Failed to create pandas agent: {str(e)}"
                logging.error(f"{error_msg}\n{traceback.format_exc()}")
                return None, self.dataframes_cache[thread_id], [error_msg]
        
        return self.agents_cache[thread_id], self.dataframes_cache[thread_id], []
    
    def check_file_availability(self, thread_id, query):
        """
        Check if a file mentioned in the query is available in the dataframes cache.
        If not, identify which file is missing to inform the user.
        
        Args:
            thread_id (str): Thread ID
            query (str): The query string to check
            
        Returns:
            tuple: (bool, missing_file_name)
        """
        if thread_id not in self.dataframes_cache:
            return False, None
            
        # Get all dataframe names for this thread
        available_files = set(self.dataframes_cache[thread_id].keys())
        
        # Get all file information for this thread (for historical checks)
        all_files_ever_uploaded = set()
        
        # First add files that are currently in the cache
        for file_info in self.file_info_cache.get(thread_id, []):
            file_name = file_info.get("name", "")
            if file_name:
                all_files_ever_uploaded.add(file_name.lower())
        
        # Look for any file name in the query
        query_lower = query.lower()
        
        # First check active files
        for file_name in available_files:
            base_name = file_name.split(" [Sheet:")[0].lower()  # Handle Excel sheet names
            if base_name in query_lower:
                # Found a match in active files
                return True, None
                
        # Check if any file is mentioned but not available (may have been removed by FIFO)
        for file_name in all_files_ever_uploaded:
            if file_name in query_lower and not any(file_name in af.lower() for af in available_files):
                # Found a match in previously uploaded files, but not currently available
                return False, file_name
                
        # No file mentioned or all mentioned files are available
        return True, None
    
    def analyze(self, thread_id, query, files):
        """
        Analyze data with pandas agent.
        
        Args:
            thread_id (str): Thread ID
            query (str): The query string
            files (list): List of file info dictionaries
            
        Returns:
            tuple: (result, error, removed_files)
        """
        # Initialize thread storage if needed
        self.initialize_thread(thread_id)
        
        # Log analysis request details
        logging.info(f"Analyzing data for thread {thread_id} with query: {query}")
        logging.info(f"Files to process: {len(files)}")
        for i, file_info in enumerate(files):
            file_name = file_info.get("name", "unnamed_file")
            file_path = file_info.get("path", "unknown_path")
            file_type = file_info.get("type", "unknown_type")
            logging.info(f"  File {i+1}: {file_name} ({file_type}) at {file_path}")
            
            # Check file existence and readability
            if file_path and os.path.exists(file_path):
                try:
                    with open(file_path, 'rb') as f:
                        first_bytes = f.read(10)
                    logging.info(f"  File {file_name} is readable (first few bytes: {first_bytes})")
                except Exception as e:
                    logging.warning(f"  File {file_name} exists but might not be readable: {str(e)}")
            else:
                logging.warning(f"  File {file_name} path does not exist: {file_path}")
        
        # Process any new files
        removed_files = []
        for file_info in files:
            _, error, removed_file = self.add_file(thread_id, file_info)
            if error:
                logging.warning(f"Error adding file {file_info.get('name', 'unnamed')}: {error}")
            if removed_file and removed_file not in removed_files:
                removed_files.append(removed_file)
                
        # Check if files mentioned in the query are available
        files_available, missing_file = self.check_file_availability(thread_id, query)
        if not files_available and missing_file:
            return None, f"The file '{missing_file}' was mentioned in your query but is no longer available. Please re-upload the file as it may have been removed due to the 3-file limit per conversation.", removed_files
        
        # Get or create the agent
        agent, dataframes, agent_errors = self.get_or_create_agent(thread_id)
        
        if not agent:
            error_msg = f"Failed to create pandas agent: {'; '.join(agent_errors)}"
            return None, error_msg, removed_files
        
        if not dataframes:
            return None, "No dataframes available for analysis", removed_files
                
        # Extract filename mentions in the query
        mentioned_files = []
        for df_name in dataframes.keys():
            base_name = df_name.split(" [Sheet:")[0].lower()  # Handle Excel sheet names
            if base_name.lower() in query.lower():
                mentioned_files.append(df_name)
                
        # Process the query - FIX THE FILE LOADING ISSUE
        if "csv" in query.lower() or "excel" in query.lower() or ".xlsx" in query.lower() or ".xls" in query.lower():
            # If the query mentions a specific file, create a clear instruction to use the loaded dataframe
            if mentioned_files:
                mentioned_file = mentioned_files[0]
                # Create informative query that prevents file reloading
                enhanced_query = f"""
    The dataframe for '{mentioned_file}' is ALREADY LOADED. DO NOT try to load the file from disk.
    Instead, use the dataframe that is already available to you.

    Analyze this dataframe to answer: {query}
                """
            else:
                # Generic instruction for any CSV/Excel mention without specific match
                enhanced_query = f"""
    The dataframes are ALREADY LOADED. DO NOT try to load any files from disk.
    Use the dataframes that are already available to you.

    Analyze these dataframes to answer: {query}
                """
        else:
            # If no specific file type is mentioned, use original query
            enhanced_query = query
            
            # If no specific file is mentioned but we have multiple files, provide minimal guidance
            if len(dataframes) > 1 and not mentioned_files:
                # Create a concise list of available files
                file_list = ", ".join(f"'{name}'" for name in dataframes.keys())
                
                # Add a gentle hint about available files
                query_prefix = f"""
    Available dataframes: {file_list}
    DO NOT try to load any files from disk - the data is already loaded.

    """
                enhanced_query = query_prefix + query
                
        logging.info(f"Final query to process: {enhanced_query}")
        
        try:
            # Invoke the agent with the query
            import sys
            from io import StringIO
            
            # Capture stdout to get verbose output for debugging
            original_stdout = sys.stdout
            captured_output = StringIO()
            sys.stdout = captured_output
            
            try:
                # Prepare dataframe details for error cases
                df_details = []
                for name, df in dataframes.items():
                    df_details.append(f"DataFrame '{name}': {df.shape[0]} rows, {df.columns.shape[0]} columns")
                    df_details.append(f"Columns: {', '.join(df.columns.tolist())}")
                    # Add first few rows for debugging
                    try:
                        df_details.append(f"First 3 rows sample:\n{df.head(3)}")
                    except:
                        pass
                
                # First try using run method (per documentation)
                try:
                    logging.info(f"Executing agent with run method: {enhanced_query}")
                    agent_output = agent.run(enhanced_query)
                    logging.info(f"Agent completed successfully with run() method: {agent_output[:100]}...")
                except Exception as run_error:
                    # Fall back to invoke if run fails
                    logging.warning(f"Agent run() method failed: {str(run_error)}, trying invoke() method")
                    try:
                        agent_result = agent.invoke({"input": enhanced_query})
                        agent_output = agent_result.get("output", "")
                        logging.info(f"Agent completed successfully with invoke() method: {agent_output[:100]}...")
                    except Exception as invoke_error:
                        # Try one last approach - if we see a file not found error, generate a summary directly
                        error_msg = str(run_error) + " " + str(invoke_error)
                        if "FileNotFoundError" in error_msg or "No such file" in error_msg:
                            # Generate a direct summary from the dataframes
                            summary = []
                            for name, df in dataframes.items():
                                summary.append(f"## Summary of {name}")
                                summary.append(f"* Shape: {df.shape[0]} rows, {df.shape[1]} columns")
                                summary.append(f"* Columns: {', '.join(df.columns.tolist())}")
                                
                                # Add basic statistics for numeric columns
                                try:
                                    num_cols = df.select_dtypes(include=['number']).columns
                                    if len(num_cols) > 0:
                                        summary.append("\n### Basic Statistics for Numeric Columns:")
                                        summary.append(df[num_cols].describe().to_string())
                                except Exception as stats_err:
                                    summary.append(f"Error calculating statistics: {str(stats_err)}")
                                
                                # Add sample data
                                try:
                                    summary.append("\n### Sample Data (First 5 rows):")
                                    summary.append(df.head(5).to_string())
                                except Exception as sample_err:
                                    summary.append(f"Error showing sample: {str(sample_err)}")
                                    
                                summary.append("\n")
                                
                            return "\n".join(summary), None, removed_files
                        else:
                            # Both methods failed with a different error
                            raise Exception(f"Agent run() failed: {str(run_error)}; invoke() also failed: {str(invoke_error)}")
                
                # Get the captured verbose output
                verbose_output = captured_output.getvalue()
                logging.info(f"Agent verbose output:\n{verbose_output}")
                
                # Check if output seems empty or error-like
                if not agent_output or "I don't have access to" in agent_output or "not find" in agent_output.lower():
                    logging.warning(f"Agent response appears problematic: {agent_output}")
                    
                    # Check if there was a file not found error in the verbose output
                    if "FileNotFoundError" in verbose_output or "No such file" in verbose_output:
                        # Generate a direct summary from the dataframes
                        summary = []
                        for name, df in dataframes.items():
                            summary.append(f"## Summary of {name}")
                            summary.append(f"* Shape: {df.shape[0]} rows, {df.shape[1]} columns")
                            summary.append(f"* Columns: {', '.join(df.columns.tolist())}")
                            
                            # Add basic statistics for numeric columns
                            try:
                                num_cols = df.select_dtypes(include=['number']).columns
                                if len(num_cols) > 0:
                                    summary.append("\n### Basic Statistics for Numeric Columns:")
                                    summary.append(df[num_cols].describe().to_string())
                            except Exception as stats_err:
                                summary.append(f"Error calculating statistics: {str(stats_err)}")
                            
                            # Add sample data
                            try:
                                summary.append("\n### Sample Data (First 5 rows):")
                                summary.append(df.head(5).to_string())
                            except Exception as sample_err:
                                summary.append(f"Error showing sample: {str(sample_err)}")
                                
                            summary.append("\n")
                            
                        return "\n".join(summary), None, removed_files
                    else:
                        # Provide detailed dataframe information as fallback
                        fallback_output = "I analyzed your data and found:\n\n" + "\n".join(df_details[:10])
                        logging.info(f"Providing fallback output with basic dataframe info")
                        return fallback_output, None, removed_files
                
                # Final response - successful case
                return agent_output, None, removed_files
                
            except Exception as e:
                error_detail = str(e)
                tb = traceback.format_exc()
                logging.error(f"Agent execution error: {error_detail}\n{tb}")
                
                # Get verbose output for debugging
                verbose_output = captured_output.getvalue()
                logging.info(f"Agent debugging output before error:\n{verbose_output}")
                
                # Check if there was a file not found error
                if "FileNotFoundError" in verbose_output or "No such file" in verbose_output or "FileNotFoundError" in error_detail:
                    # Generate a direct summary from the dataframes
                    summary = []
                    for name, df in dataframes.items():
                        summary.append(f"## Summary of {name}")
                        summary.append(f"* Shape: {df.shape[0]} rows, {df.shape[1]} columns")
                        summary.append(f"* Columns: {', '.join(df.columns.tolist())}")
                        
                        # Add basic statistics for numeric columns
                        try:
                            num_cols = df.select_dtypes(include=['number']).columns
                            if len(num_cols) > 0:
                                summary.append("\n### Basic Statistics for Numeric Columns:")
                                summary.append(df[num_cols].describe().to_string())
                        except Exception as stats_err:
                            summary.append(f"Error calculating statistics: {str(stats_err)}")
                        
                        # Add sample data
                        try:
                            summary.append("\n### Sample Data (First 5 rows):")
                            summary.append(df.head(5).to_string())
                        except Exception as sample_err:
                            summary.append(f"Error showing sample: {str(sample_err)}")
                            
                        summary.append("\n")
                        
                    return "\n".join(summary), None, removed_files
                
                # Provide a more helpful error message with basic file info
                error_msg = f"Error analyzing data: {error_detail}"
                
                # Include dataframe details in error message for better diagnosis
                return None, f"{error_msg}\n\nDataFrame Information:\n" + "\n".join(df_details[:8]), removed_files
                
            finally:
                # Restore stdout
                sys.stdout = original_stdout
                
        except Exception as e:
            error_details = traceback.format_exc()
            logging.error(f"Critical error in analyze method: {str(e)}\n{error_details}")
            return None, f"Critical error in analysis: {str(e)}", removed_files
async def validate_resources(client: AzureOpenAI, thread_id: Optional[str], assistant_id: Optional[str]) -> Dict[str, bool]:
    """
    Validates that the given thread_id and assistant_id exist and are accessible.
    
    Args:
        client (AzureOpenAI): The Azure OpenAI client instance
        thread_id (Optional[str]): The thread ID to validate, or None
        assistant_id (Optional[str]): The assistant ID to validate, or None
        
    Returns:
        Dict[str, bool]: Dictionary with "thread_valid" and "assistant_valid" flags
    """
    result = {
        "thread_valid": False,
        "assistant_valid": False
    }
    
    # Validate thread if provided
    if thread_id:
        try:
            # Attempt to retrieve thread
            thread = client.beta.threads.retrieve(thread_id=thread_id)
            result["thread_valid"] = True
            logging.info(f"Thread validation: {thread_id} is valid")
        except Exception as e:
            result["thread_valid"] = False
            logging.warning(f"Thread validation: {thread_id} is invalid - {str(e)}")
    
    # Validate assistant if provided
    if assistant_id:
        try:
            # Attempt to retrieve assistant
            assistant = client.beta.assistants.retrieve(assistant_id=assistant_id)
            result["assistant_valid"] = True
            logging.info(f"Assistant validation: {assistant_id} is valid")
        except Exception as e:
            result["assistant_valid"] = False
            logging.warning(f"Assistant validation: {assistant_id} is invalid - {str(e)}")
    
    return result
async def pandas_agent(client: AzureOpenAI, thread_id: Optional[str], query: str, files: List[Dict[str, Any]]) -> str:
    """
    Enhanced pandas_agent that uses LangChain to analyze CSV and Excel files.
    Uses a class-based implementation to maintain isolation between threads.
    
    Args:
        client (AzureOpenAI): The Azure OpenAI client instance
        thread_id (Optional[str]): The thread ID to add the response to
        query (str): The query or question about the data
        files (List[Dict[str, Any]]): List of file information dictionaries
        
    Returns:
        str: The analysis result
    """
    # Create a unique operation ID for tracking
    operation_id = f"pandas_agent_{int(time.time())}_{os.urandom(2).hex()}"
    update_operation_status(operation_id, "started", 0, "Starting data analysis")
    
    # Flag for background thread to stop
    stop_background_updates = threading.Event()
    
    # Background thread for progress updates
    def send_progress_updates():
        progress = 50
        while progress < 85 and not stop_background_updates.is_set():
            time.sleep(1.5)  # Update every 1.5 seconds
            progress += 2
            update_operation_status(operation_id, "executing", min(progress, 85), 
                                   "Analysis in progress...")
    
    try:
        # Verify thread_id is provided
        if not thread_id:
            error_msg = "Thread ID is required for pandas agent"
            update_operation_status(operation_id, "error", 100, error_msg)
            return f"Error: {error_msg}"
        
        # Enhanced debugging: Log detailed info about files
        logging.info(f"PANDAS AGENT DEBUG - Query: '{query}'")
        logging.info(f"PANDAS AGENT DEBUG - Thread ID: {thread_id}")
        logging.info(f"PANDAS AGENT DEBUG - Files count: {len(files)}")
        
        # Log files being processed with much more detail
        file_descriptions = []
        valid_files = []
        invalid_files = []
        
        for i, file in enumerate(files):
            file_type = file.get("type", "unknown")
            file_name = file.get("name", "unnamed_file")
            file_path = file.get("path", "unknown_path")
            
            # Detailed file logging
            debug_info = (f"File {i+1}: '{file_name}' ({file_type}) - "
                         f"Path: {file_path}")
            logging.info(f"PANDAS AGENT DEBUG - {debug_info}")
            
            file_descriptions.append(f"{file_name} ({file_type})")
            
            # Verify file existence with more robust checking
            file_exists = False
            file_size = None
            first_bytes = None
            
            if file_path and os.path.exists(file_path):
                try:
                    file_size = os.path.getsize(file_path)
                    with open(file_path, 'rb') as f:
                        first_bytes = f.read(10)
                    file_exists = True
                    logging.info(f"PANDAS AGENT DEBUG - File verified: '{file_name}' exists, size: {file_size} bytes, first bytes: {first_bytes}")
                    valid_files.append(file)
                except Exception as e:
                    logging.warning(f"PANDAS AGENT DEBUG - File exists but cannot read: '{file_name}' - {str(e)}")
                    invalid_files.append((file_name, f"Read error: {str(e)}"))
            else:
                logging.warning(f"PANDAS AGENT DEBUG - File does not exist: '{file_name}' at path: {file_path}")
                invalid_files.append((file_name, f"Path not found: {file_path}"))
                
                # Enhanced path correction: Look for files with similar names in /tmp
                possible_paths = [
                    path for path in os.listdir('/tmp') 
                    if file_name.lower() in path.lower() and os.path.isfile(os.path.join('/tmp', path))
                ]
                
                if possible_paths:
                    logging.info(f"PANDAS AGENT DEBUG - Found possible alternatives for '{file_name}':")
                    for alt_path in possible_paths:
                        full_path = os.path.join('/tmp', alt_path)
                        alt_size = os.path.getsize(full_path)
                        logging.info(f"  - Alternative: {full_path} (size: {alt_size} bytes)")
                    
                    # Use the first alternative found
                    corrected_path = os.path.join('/tmp', possible_paths[0])
                    logging.info(f"PANDAS AGENT DEBUG - Using alternative path for {file_name}: {corrected_path}")
                    file["path"] = corrected_path
                    valid_files.append(file)
                else:
                    # Try a more aggressive search for similarly named files
                    all_tmp_files = os.listdir('/tmp')
                    csv_files = [f for f in all_tmp_files if f.endswith('.csv') or f.endswith('.xlsx') or f.endswith('.xls')]
                    
                    logging.info(f"PANDAS AGENT DEBUG - Available files in /tmp directory:")
                    for tmp_file in csv_files[:10]:  # Show first 10 to avoid log flooding
                        logging.info(f"  - {tmp_file}")
                    
                    # Check if filename parts match (for handling timestamp prefixes)
                    name_parts = re.split(r'[_\s]', file_name.lower())
                    for tmp_file in csv_files:
                        match_score = sum(1 for part in name_parts if part in tmp_file.lower())
                        if match_score >= 2:  # If at least 2 parts match
                            corrected_path = os.path.join('/tmp', tmp_file)
                            logging.info(f"PANDAS AGENT DEBUG - Found partial match for '{file_name}': {corrected_path}")
                            file["path"] = corrected_path
                            valid_files.append(file)
                            break
        
        # Replace original files list with validated files
        files = valid_files
        
        # Summary log
        file_list_str = ", ".join(file_descriptions) if file_descriptions else "No files provided"
        logging.info(f"PANDAS AGENT DEBUG - Processing data analysis for thread {thread_id} with files: {file_list_str}")
        logging.info(f"PANDAS AGENT DEBUG - Valid files: {len(valid_files)}, Invalid files: {len(invalid_files)}")
        
        if len(valid_files) == 0:
            update_operation_status(operation_id, "error", 100, "No valid files found for analysis")
            return f"Error: Could not find any valid files to analyze. Please verify the uploaded files and try again.\n\nDebug info: {file_list_str}"
            
        update_operation_status(operation_id, "files", 20, f"Processing files: {file_list_str}")
        
        # Get the PandasAgentManager instance
        manager = PandasAgentManager.get_instance()
        
        # Process the query
        update_operation_status(operation_id, "analyzing", 50, f"Analyzing data with query: {query}")
        
        # Start progress update in background
        update_thread = None
        try:
            update_thread = threading.Thread(target=send_progress_updates)
            update_thread.daemon = True
            update_thread.start()
        except Exception as e:
            # Don't fail if we can't spawn thread
            logging.warning(f"Could not start progress update thread: {str(e)}")
        
        # Run the analysis using the PandasAgentManager
        result, error, removed_files = manager.analyze(thread_id, query, files)
        
        # Stop the background updates
        stop_background_updates.set()
        
        # Wait for the update thread to terminate (with timeout)
        if update_thread and update_thread.is_alive():
            update_thread.join(timeout=1.0)
        
        # Prepare the response
        update_operation_status(operation_id, "formatting", 90, "Formatting response")
        
        if error:
            update_operation_status(operation_id, "error", 95, f"Error: {error}")
            
            # Detect if the error appears to be a file access issue
            if "access" in error.lower() or "find" in error.lower() or "read" in error.lower():
                # Try to get basic dataframe info as a fallback
                try:
                    df_info = []
                    dfs = manager.dataframes_cache.get(thread_id, {})
                    
                    if dfs:
                        for df_name, df in dfs.items():
                            df_info.append(f"- {df_name}: {df.shape[0]} rows, {len(df.columns)} columns")
                            df_info.append(f"  Columns: {', '.join(df.columns[:10].tolist())}")
                            
                            # Add sample data (first 3 rows) if we can
                            try:
                                sample = df.head(3).to_string()
                                df_info.append(f"  Sample data:\n{sample}")
                            except:
                                pass
                            
                        fallback_response = (
                            f"I encountered an issue while analyzing your data files but can provide "
                            f"basic information about them:\n\n{chr(10).join(df_info)}\n\n"
                            f"Error details: {error}"
                        )
                        
                        # If files were removed via FIFO, add a note about it
                        if removed_files:
                            removed_files_str = ", ".join(f"'{f}'" for f in removed_files)
                            fallback_response += f"\n\nNote: The following file(s) were removed due to the 3-file limit: {removed_files_str}"
                            
                        return fallback_response
                except Exception as fallback_e:
                    # If fallback fails, return original error
                    logging.error(f"Fallback info generation failed: {fallback_e}")
            
            # Add debug information to error response
            debug_info = ""
            if invalid_files:
                debug_info = "\n\nDebug information:\n"
                for name, err in invalid_files:
                    debug_info += f"- File '{name}': {err}\n"
                
                # Add info about files in /tmp
                try:
                    tmp_files = [f for f in os.listdir('/tmp') if f.endswith('.csv') or f.endswith('.xlsx')]
                    if tmp_files:
                        debug_info += f"\nAvailable files in /tmp:\n"
                        for i, tmp_file in enumerate(tmp_files[:10]):  # Show first 10
                            tmp_path = os.path.join('/tmp', tmp_file)
                            tmp_size = os.path.getsize(tmp_path)
                            debug_info += f"- {tmp_file} (size: {tmp_size} bytes)\n"
                except Exception as tmp_err:
                    debug_info += f"\nError listing /tmp: {str(tmp_err)}\n"
            
            # Standard error response with debugging info
            final_response = f"Error analyzing data: {error}{debug_info}"
            
            # If files were removed via FIFO, add a note about it
            if removed_files:
                removed_files_str = ", ".join(f"'{f}'" for f in removed_files)
                final_response += f"\n\nNote: The following file(s) were removed due to the 3-file limit: {removed_files_str}"
        else:
            final_response = result if result else "No results were returned from the analysis. Try reformulating your query."
            
            # If files were removed via FIFO, add a note about it
            if removed_files:
                removed_files_str = ", ".join(f"'{f}'" for f in removed_files)
                final_response += f"\n\nNote: The following file(s) were removed due to the 3-file limit: {removed_files_str}"
        
        # Add response to thread if provided
        if thread_id:
            update_operation_status(operation_id, "responding", 95, "Adding response to thread")
            try:
                client.beta.threads.messages.create(
                    thread_id=thread_id,
                    role="user",
                    content=f"[PANDAS AGENT RESPONSE]: {final_response}",
                    metadata={"type": "pandas_agent_response", "operation_id": operation_id}
                )
                logging.info(f"Added pandas_agent response to thread {thread_id}")
            except Exception as e:
                logging.error(f"Error adding pandas_agent response to thread: {e}")
                # Continue execution despite error with thread message
        
        # Mark operation as completed
        update_operation_status(operation_id, "completed", 100, "Analysis completed successfully")
        
        # Log completion
        logging.info(f"Pandas agent completed query: '{query}' for thread {thread_id}")
        
        return final_response
    
    except Exception as e:
        # Stop the background updates
        stop_background_updates.set()
        
        error_details = traceback.format_exc()
        logging.error(f"Critical error in pandas_agent: {str(e)}\n{error_details}")
        
        # Update status to reflect error
        update_operation_status(operation_id, "error", 100, f"Error: {str(e)}")
        
        # Try to provide some helpful debugging information
        debug_info = []
        try:
            # Check if files exist
            debug_info.append("File System Debugging Information:")
            for file in files:
                file_name = file.get("name", "unnamed")
                file_path = file.get("path", "unknown")
                if file_path and os.path.exists(file_path):
                    debug_info.append(f"- File '{file_name}' exists at path: {file_path}")
                    file_size = os.path.getsize(file_path)
                    debug_info.append(f"  - Size: {file_size} bytes")
                    # Try to read first 20 bytes to verify readability
                    try:
                        with open(file_path, 'rb') as f:
                            first_bytes = f.read(20)
                        debug_info.append(f"  - First bytes: {first_bytes}")
                    except Exception as read_err:
                        debug_info.append(f"  - Read error: {str(read_err)}")
                else:
                    debug_info.append(f"- File '{file_name}' does not exist at path: {file_path}")
                    
                    # Look for similar files
                    possible_paths = [
                        path for path in os.listdir('/tmp') 
                        if path.endswith(('.csv', '.xlsx', '.xls')) and os.path.isfile(os.path.join('/tmp', path))
                    ]
                    if possible_paths:
                        debug_info.append(f"  - Found possible CSV/Excel files in /tmp directory:")
                        for i, path in enumerate(possible_paths[:5]):  # Show first 5
                            debug_info.append(f"    {i+1}. {path}")
        except Exception as debug_err:
            debug_info.append(f"Error during debugging: {str(debug_err)}")
        
        # Provide a graceful failure response with debug info
        debug_str = "\n".join(debug_info)
        error_response = f"""Sorry, I encountered an error while trying to analyze your data files.

Error details: {str(e)}

Additional debugging information:
{debug_str}

Please try again with a different query or contact support if the issue persists.

Operation ID: {operation_id}"""
                
        return error_response
        
async def image_analysis(client: AzureOpenAI, image_data: bytes, filename: str, prompt: Optional[str] = None) -> str:
    """Analyzes an image using Azure OpenAI vision capabilities and returns the analysis text."""
    try:
        ext = os.path.splitext(filename)[1].lower()
        b64_img = base64.b64encode(image_data).decode("utf-8")
        # Try guessing mime type, default to jpeg if extension isn't standard or determinable
        mime, _ = mimetypes.guess_type(filename)
        if not mime or not mime.startswith('image'):
            mime = f"image/{ext[1:]}" if ext and ext[1:] in ['jpg', 'jpeg', 'png', 'gif', 'webp'] else "image/jpeg"

        data_url = f"data:{mime};base64,{b64_img}"

        default_prompt = (
            "Analyze this image and provide a thorough summary including all elements. "
            "If there's any text visible, include all the textual content. Describe:"
        )
        combined_prompt = f"{default_prompt} {prompt}" if prompt else default_prompt

        # Use the existing client instead of creating a new one
        response = client.chat.completions.create(
            model="gpt-4.1",  # Ensure this model supports vision
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": combined_prompt},
                    {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}}
                ]
            }],
            max_tokens=1000  # Increased max_tokens for potentially more detailed analysis
        )

        analysis_text = response.choices[0].message.content
        return analysis_text if analysis_text else "No analysis content received."

    except Exception as e:
        logging.error(f"Image analysis error for {filename}: {e}")
        return f"Error analyzing image '{filename}': {str(e)}"

# Helper function to update user persona context
async def update_context(client: AzureOpenAI, thread_id: str, context: str):
    """Updates the user persona context in a thread by adding/replacing a special message."""
    if not context:
        return

    try:
        # Get existing messages to check for previous context
        messages = client.beta.threads.messages.list(
            thread_id=thread_id,
            order="desc",
            limit=20  # Check recent messages is usually sufficient
        )

        # Look for previous context messages to avoid duplication
        previous_context_message_id = None
        for msg in messages.data:
            if hasattr(msg, 'metadata') and msg.metadata and msg.metadata.get('type') == 'user_persona_context':
                previous_context_message_id = msg.id
                break

        # If found, delete previous context message to replace it
        if previous_context_message_id:
            try:
                client.beta.threads.messages.delete(
                    thread_id=thread_id,
                    message_id=previous_context_message_id
                )
                logging.info(f"Deleted previous context message {previous_context_message_id} in thread {thread_id}")
            except Exception as e:
                logging.error(f"Error deleting previous context message {previous_context_message_id}: {e}")
            # Continue even if delete fails to add the new context

        # Add new context message
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=f"USER PERSONA CONTEXT: {context}",
            metadata={"type": "user_persona_context"}
        )

        logging.info(f"Updated user persona context in thread {thread_id}")
    except Exception as e:
        logging.error(f"Error updating context in thread {thread_id}: {e}")
        # Continue the flow even if context update fails

# Function to add file awareness to the assistant
async def add_file_awareness(client: AzureOpenAI, thread_id: str, file_info: Dict[str, Any]):
    """Adds file awareness to the assistant by sending a message about the file."""
    if not file_info:
        return

    try:
        # Create a message that informs the assistant about the file
        file_type = file_info.get("type", "unknown")
        file_name = file_info.get("name", "unnamed_file")
        processing_method = file_info.get("processing_method", "")

        awareness_message = f"FILE INFORMATION: A file named '{file_name}' of type '{file_type}' has been uploaded and processed. "

        if processing_method == "pandas_agent":
            awareness_message += f"This file is available for analysis using the pandas agent."
            if file_type == "excel":
                awareness_message += " This is an Excel file with potentially multiple sheets."
            elif file_type == "csv":
                awareness_message += " This is a CSV file."
            
            awareness_message += "\n\nIMPORTANT: You MUST use the pandas_agent tool for ANY request that mentions this file or asks about data analysis. This includes:"
            awareness_message += "\n- Simple requests like 'explain the file'"
            awareness_message += "\n- Vague requests like 'tell me about the data'"
            awareness_message += "\n- Explicit requests like 'analyze this CSV'" 
            awareness_message += "\n- Any query containing the filename"
            awareness_message += "\n- ANY follow-up question after discussing this file"
            
            awareness_message += "\n\nUser requests like 'explain the report' or 'summarize the data' should ALWAYS trigger you to use the pandas_agent tool."
            awareness_message += "\n\nNEVER try to answer questions about this file from memory - ALWAYS use the pandas_agent tool."
        
        elif processing_method == "thread_message":
            awareness_message += "This image has been analyzed and the descriptive content has been added to this thread."
        elif processing_method == "vector_store":
            awareness_message += "This file has been added to the vector store and its content is available for search."
        else:
            awareness_message += "This file has been processed."

        # Send the message to the thread
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",  # Sending as user so assistant 'sees' it as input/instruction
            content=awareness_message,
            metadata={"type": "file_awareness", "processed_file": file_name}
        )

        logging.info(f"Added file awareness for '{file_name}' ({processing_method}) to thread {thread_id}")
    except Exception as e:
        logging.error(f"Error adding file awareness for '{file_name}' to thread {thread_id}: {e}")
        # Continue the flow even if adding awareness fails
def update_operation_status(operation_id: str, status: str, progress: float, message: str):
    """Update the status of a long-running operation."""
    operation_statuses[operation_id] = {
        "status": status,
        "progress": progress,
        "message": message,
        "updated_at": time.time()
    }
    logging.info(f"Operation {operation_id}: {status} - {progress:.0f}% - {message}")

# Status endpoint
@app.get("/operation-status/{operation_id}")
async def check_operation_status(operation_id: str):
    """Check the status of a long-running operation."""
    if operation_id not in operation_statuses:
        return JSONResponse(
            status_code=404,
            content={"error": f"No operation found with ID {operation_id}"}
        )
    
    return JSONResponse(content=operation_statuses[operation_id])
@app.post("/initiate-chat")
async def initiate_chat(request: Request):
    """
    Initiates a new assistant, session (thread), and vector store.
    Optionally uploads a file and sets user context.
    """
    client = create_client()
    logging.info("Initiating new chat session...")

    # Parse the form data
    try:
        form = await request.form()
        file: Optional[UploadFile] = form.get("file", None)
        context: Optional[str] = form.get("context", None)
    except Exception as e:
        logging.error(f"Error parsing form data: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid form data: {e}")

    # Create a vector store up front
    try:
        vector_store =client.vector_stores.create(name=f"chat_init_store_{int(time.time())}")
        logging.info(f"Vector store created: {vector_store.id}")
    except Exception as e:
        logging.error(f"Failed to create vector store: {e}")
        raise HTTPException(status_code=500, detail="Failed to create vector store")

    # Include file_search and add pandas_agent as a function tool
    assistant_tools = [
        {"type": "file_search"},
        {
            "type": "function",
            "function": {
                "name": "pandas_agent",
                "description": "Analyzes CSV and Excel files to answer data-related questions and perform data analysis",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The specific question or analysis task to perform on the data. Be comprehensive and explicit."
                        },
                        "filename": {
                            "type": "string",
                            "description": "Optional: specific filename to analyze. If not provided, all available files will be considered."
                        }
                    },
                    "required": ["query"]
                }
            }
        }
    ]
    
    assistant_tool_resources = {
        "file_search": {"vector_store_ids": [vector_store.id]}
    }

    # Keep track of CSV/Excel files for the session
    session_csv_excel_files = []

    # Use the improved system prompt
    system_prompt = '''
You are an Advanced AI Assistant with comprehensive general knowledge and specialized expertise in product management, document analysis, and data processing. You excel equally at everyday conversations (like recipes, travel advice, or explaining concepts) and sophisticated professional tasks (like creating PRDs, analyzing data, or processing complex documents). Your versatility allows you to seamlessly switch between being a helpful companion for casual queries and a powerful tool for business analysis.

## CRITICAL DECISION FRAMEWORK - FOLLOW THIS EXACTLY:

### STEP 1: CHECK FOR UPLOADED FILES
Before responding to ANY query, ALWAYS scan the conversation history for "FILE INFORMATION:" messages. These messages tell you EXACTLY which files are available, when they were uploaded, and how they were processed. Make a mental list of ALL available files with their names, types, and upload order.

### STEP 2: CLASSIFY THE QUESTION

**FILE-BASED QUESTIONS** (require specific uploaded files):
- Questions that explicitly mention filenames or file types
- Questions immediately after file upload (assume it's about that file)
- Requests for analysis/summary/extraction from documents
- Questions about specific data, numbers, or content that would be in files
- Examples: "analyze the CSV", "what's in the report", "summarize the document"

**PRODUCT MANAGEMENT QUESTIONS** (may or may not involve files):
- PRD creation, review, or improvement requests
- Product strategy, roadmap, or feature prioritization
- Market analysis, competitive research, user personas
- Any PM frameworks, methodologies, or best practices
- **IMPORTANT**: These often have uploaded context files - ALWAYS check and ask

**GENERIC QUESTIONS** (use your general knowledge):
- How-to questions, explanations, definitions
- General recipes, procedures, concepts
- Questions clearly unrelated to any uploaded files
- Requests for general information or advice
- Examples: "how do I make cheesecake", "explain quantum computing"

### STEP 3: DETERMINE YOUR RESPONSE SOURCE

**For GENERIC QUESTIONS:**
1. Answer directly from your knowledge - DO NOT look for files
2. DO NOT use pandas_agent or file_search tools
3. At the END of your response, add: "*Responding from general knowledge*"
4. Optionally add: "If you have specific data files related to this topic, feel free to upload them for detailed analysis."

**For PRODUCT MANAGEMENT QUESTIONS:**
1. First, check if ANY files have been uploaded
2. If files exist, ask: "I see you have uploaded [list files]. Which file(s) should I reference for this [PRD/strategy/analysis]? Or would you like me to proceed with general guidance?"
3. If no files exist, provide general PM guidance and suggest: "For a more tailored [PRD/analysis], please upload relevant files such as:
   - Market research data (CSV/Excel)
   - Competitive analysis documents (PDF/DOCX)
   - User research findings
   - Product requirements or specifications"

**For FILE-BASED QUESTIONS:**
1. Check if you have relevant files from your Step 1 mental list
2. If YES: Use appropriate tools and cite the specific filename
3. If NO: Provide general knowledge if available, then explicitly state what file would be needed
4. Always mention: "*Responding from [filename]*" or "*Responding from general knowledge - please upload [type] file for specific analysis*"

## Core Capabilities & Expertise:

### 1. Advanced File Processing & Retrieval:
You are a specialist in handling, analyzing, and retrieving information from various file types:

- **Document Mastery**: Expert at extracting, analyzing, and synthesizing information from PDFs, Word docs, text files, HTML, and other document formats
- **Data Analysis Excellence**: Advanced capabilities with CSV/Excel files using the pandas_agent tool for complex data analysis, statistical insights, and trend identification
- **Image Understanding**: Sophisticated image analysis for diagrams, mockups, screenshots, and visual content
- **Smart File Search**: Intelligent retrieval using file_search to find specific information across multiple documents quickly and accurately
- **Context Preservation**: Maintains awareness of all uploaded files and can cross-reference information between them

### 2. Intelligent File Type Recognition & Processing:

**IMPORTANT RULE**: Before using ANY tool, check if you actually have relevant files available by reviewing FILE INFORMATION messages.

#### **CSV/Excel Files** - When users ask about data AND you have these files:
- Use pandas_agent ONLY when you have confirmed a CSV/Excel file exists
- Common indicators: mentions of data, statistics, analysis, spreadsheets
- Always cite the specific filename you're analyzing
- NEVER use pandas_agent for general knowledge questions
- For ANY question about CSV/Excel data, you MUST use the pandas_agent tool

#### **Documents (PDF, DOC, TXT, etc.)** - When users ask about documents AND you have these files:
- Use file_search to extract relevant information
- Quote directly from documents and cite the filename
- Always reference the specific filename when sharing information

#### **Images** - When users reference images AND they've been uploaded:
- Refer to the image analysis already in the conversation
- Use details from the image analysis to answer questions
- Acknowledge what was visible in the specific image file

### Using the pandas_agent Tool:

When a user asks ANY question about data in CSV or Excel files (including follow-up questions), and you have confirmed such files exist:
1. Identify that the question relates to data files
2. Formulate a clear, specific query for the pandas_agent that includes the necessary context
3. Call the pandas_agent tool with your query
4. Never try to answer data-related questions from memory of previous conversations

### 3. FILE AWARENESS PRIORITY RULES:

1. **Most Recent Files**: Questions after file uploads are usually about those files
2. **Check Before Tools**: NEVER use pandas_agent or file_search unless you've confirmed relevant files exist
3. **Be Explicit**: Always state which file you're using or that you're using general knowledge
4. **No Assumptions**: Don't assume files exist - check FILE INFORMATION messages
5. **Generic First**: For ambiguous questions, default to general knowledge unless files are explicitly mentioned
6. **Ask for Clarification**: When multiple files could be relevant, ask user to specify

### 4. Response Patterns:

**When files ARE available and relevant:**
- "*Analyzing data from [filename.csv]*..."
- "*Based on the content in [document.pdf]*..."
- "*Looking at the uploaded file [filename]*..."

**When files are NOT available but would help:**
- "[Answer from general knowledge]. *Responding from general knowledge*"
- "To provide specific analysis with your data, please upload a CSV/Excel file containing [describe needed data]."

**For Product Management questions with ambiguous file context:**
- "I see you have uploaded [file1.xlsx, file2.pdf]. Which of these should I use for creating your PRD?"
- "Would you like me to incorporate data from your uploaded files, or should I provide general PRD guidance?"

**For purely generic questions:**
- [Direct answer without mentioning files]
- "*Responding from general knowledge*"

### 5. Product Management Excellence:

You excel at all aspects of product management:

**Strategic Thinking**:
- Market analysis and competitive intelligence
- Product vision and roadmap development
- Business model evaluation and pricing strategies
- Go-to-market planning and execution strategies

**Documentation Mastery**:
- Create world-class PRDs with all required sections
- User story writing with acceptance criteria
- Technical specification development
- Requirements gathering and analysis
- Stakeholder communication documents

**Analytical Capabilities**:
- Data-driven decision making using uploaded data
- Metrics definition and KPI tracking
- User research synthesis and insights
- A/B testing analysis and recommendations
- Market sizing and opportunity assessment

### 6. PRD Generation Framework:

When creating a PRD, you produce comprehensive, professional documents with these mandatory sections:

#### 1. **Executive Overview:**
- Product Manager: [Name, contact details]
- Product Name: [Clear, memorable name]
- Version: [Document version and date]
- Vision Statement: [Compelling 1-2 sentence vision]
- Executive Summary: [High-level overview of the product]

#### 2. **Problem & Opportunity:**
- Problem Statement: [Clear articulation of the problem being solved]
- Market Opportunity: [TAM/SAM/SOM with data-backed analysis]
- Competitive Landscape: [Key competitors and differentiation]
- Why Now: [Timing and market readiness factors]

#### 3. **Customer & User Analysis:**
- Primary Personas: [Detailed personas with goals, pain points, behaviors]
- Secondary Personas: [Additional user types and their needs]
- User Journey Maps: [Current vs. future state journeys]
- Jobs to be Done: [Core jobs users are trying to accomplish]

#### 4. **Solution & Features:**
- Solution Overview: [High-level approach to solving the problem]
- Key Features: [Prioritized list with detailed descriptions]
- Feature Details: [User stories, acceptance criteria, mockups]
- MVP Definition: [Minimum viable product scope]
- Future Enhancements: [Post-MVP roadmap items]

#### 5. **Technical Architecture:**
- System Architecture: [High-level technical design]
- Technology Stack: [Required technologies and tools]
- Integration Points: [APIs, third-party services]
- Data Requirements: [Data models, storage, privacy]
- Security Considerations: [Security and compliance needs]

#### 6. **Success Metrics & Analytics:**
- Success Metrics: [Primary KPIs with targets]
- Secondary Metrics: [Supporting metrics to track]
- Analytics Plan: [How metrics will be measured]
- Success Criteria: [Definition of product success]

#### 7. **Go-to-Market Strategy:**
- Launch Strategy: [Phased rollout plan]
- Marketing Plan: [Positioning, messaging, channels]
- Sales Enablement: [Tools and training needed]
- Support Plan: [Customer support requirements]

#### 8. **Implementation Plan:**
- Development Timeline: [Phases with milestones]
- Resource Requirements: [Team and budget needs]
- Dependencies: [Internal and external dependencies]
- Risks & Mitigations: [Key risks and mitigation strategies]

#### 9. **Appendices:**
- Research Data: [Supporting research and analysis]
- Mockups/Wireframes: [Visual designs if available]
- Technical Specifications: [Detailed technical docs]
- References: [Sources and additional reading]

### 7. Intelligent Mode Switching & Context Awareness:

You seamlessly switch between different assistance modes based on user needs:

**General Assistant Mode**:
- Engage naturally when users ask general questions unrelated to files or product management
- Provide helpful, accurate answers without over-complicating responses
- Maintain a friendly, conversational tone for casual interactions
- Don't force product management context when it's not relevant

**Document Analysis Mode**:
- Activate when users upload files or reference uploaded documents
- Immediately acknowledge files and explain intended usage
- Use appropriate tools based on file types
- Maintain file context throughout the conversation

**Product Management Mode**:
- Engage when users ask about product strategy, PRDs, roadmaps, or PM-related topics
- **ALWAYS check for uploaded files first** - PM questions often have supporting documents
- Ask which files to reference when multiple options exist
- Leverage uploaded files to support product decisions when available
- Provide comprehensive, professional deliverables
- Apply PM frameworks and best practices

**Hybrid Mode**:
- Combine modes when users have general questions about their uploaded files
- Balance casual explanation with professional analysis
- Know when to dive deep vs. when to keep it simple

### 8. CRITICAL RULES TO PREVENT OVERTOOLING:

1. **Default to Knowledge**: Unless files are explicitly mentioned or clearly needed, use your general knowledge
2. **No Phantom Files**: NEVER attempt to analyze files that don't exist
3. **Tool Restraint**: Use tools ONLY when you have confirmed relevant files exist
4. **Clear Attribution**: Always distinguish between file-based and knowledge-based responses
5. **User Friendly**: Don't overwhelm users by constantly asking for files when you can answer their question
6. **NEVER** answer questions about CSV/Excel data without using pandas_agent
7. **ALWAYS** use tools when files are mentioned or data is referenced
8. **MAINTAIN** awareness of all uploaded files throughout the conversation
9. **VERIFY** information against source documents before stating facts
10. **ACKNOWLEDGE** when information is missing and request specific files
11. **ASK FOR CLARIFICATION** when multiple files could be relevant to the query

### 9. Response Guidelines:

- **Be Comprehensive**: Provide thorough, detailed responses that anticipate user needs
- **Stay Accurate**: Always verify information against uploaded files before responding
- **Be Actionable**: Include specific next steps and recommendations
- **Maintain Context**: Remember all files and previous analyses in the conversation
- **Professional Tone**: Balance expertise with approachability
- **Format Clearly**: Use markdown formatting for readability with headers, bullets, and tables
- **Cite Sources**: Always reference specific files and sections when quoting or analyzing

### 10. Example Query Handling:

**General Questions** (respond naturally without forcing file/PM context):
- "What's the weather like?" → Explain you don't have real-time data but can discuss weather patterns
- "How do I make pasta?" → Provide a helpful recipe and cooking tips
- "Explain quantum computing" → Give a clear, educational explanation
- "Tell me a joke" → Share appropriate humor
- "Help me plan a trip to Japan" → Offer travel advice and planning tips
- "Give me IPL top wicket takers" → Provide general knowledge list, then mention: "*Responding from general knowledge* - upload IPL statistics file for detailed analysis"

**File-Related Questions** (use tools and analysis):
- "Analyze this sales data" → Use pandas_agent for CSV/Excel analysis
- "Summarize this PDF" → Use file_search for document analysis
- "What's in this image?" → Reference the image analysis
- "Compare these two reports" → Cross-reference multiple documents

**Product Management Questions** (check for files, then apply PM expertise):
- "How do I write a PRD?" → Check for uploaded files first, ask which to use if any exist, then provide comprehensive PRD framework
- "What metrics should I track?" → Check for data files, suggest relevant KPIs based on files or general knowledge
- "Review my product strategy" → Look for strategy documents, offer analysis based on uploads or general guidance
- "Create a PRD for my app" → Ask: "I see you have [files]. Should I use these for your PRD, or would you like to upload specific requirements?"

Remember: You are a versatile AI assistant who excels at both everyday conversations and specialized product management tasks. Not every interaction needs to involve file analysis or product strategy - sometimes users just need a friendly, knowledgeable assistant for general questions.

For general queries, be naturally helpful without overcomplicating. For file-related or PM tasks, leverage your full analytical capabilities. Always gauge the appropriate level of detail and technicality based on the user's needs.

When users upload files, immediately acknowledge them and explain how you'll use them. When creating documents, exceed expectations with professional quality and comprehensive coverage. But when users just want to chat or ask general questions, be the friendly, knowledgeable assistant they need - no files or frameworks required.

You are the ultimate AI companion - equally comfortable discussing cooking recipes, explaining quantum physics, analyzing business data, or creating world-class PRDs. Your versatility is your strength.
'''
    
    # Create the assistant
    try:
        assistant = client.beta.assistants.create(
            name=f"pm_copilot_{int(time.time())}",
            model="gpt-4.1",  # Ensure this model is deployed
            instructions=system_prompt,
            tools=assistant_tools,
            tool_resources=assistant_tool_resources,
        )
        logging.info(f'Assistant created: {assistant.id}')
    except Exception as e:
        logging.error(f"An error occurred while creating the assistant: {e}")
        # Attempt to clean up vector store if assistant creation fails
        try:
            client.vector_stores.delete(vector_store_id=vector_store.id)
            logging.info(f"Cleaned up vector store {vector_store.id} after assistant creation failure.")
        except Exception as cleanup_e:
            logging.error(f"Failed to cleanup vector store {vector_store.id} after error: {cleanup_e}")
        raise HTTPException(status_code=500, detail=f"An error occurred while creating assistant: {e}")

    # Create a thread
    try:
        thread = client.beta.threads.create()
        logging.info(f"Thread created: {thread.id}")
    except Exception as e:
        logging.error(f"An error occurred while creating the thread: {e}")
        # Attempt cleanup
        try:
            client.beta.assistants.delete(assistant_id=assistant.id)
            logging.info(f"Cleaned up assistant {assistant.id} after thread creation failure.")
        except Exception as cleanup_e:
            logging.error(f"Failed to cleanup assistant {assistant.id} after error: {cleanup_e}")
        try:
            client.vector_stores.delete(vector_store_id=vector_store.id)
            logging.info(f"Cleaned up vector store {vector_store.id} after thread creation failure.")
        except Exception as cleanup_e:
            logging.error(f"Failed to cleanup vector store {vector_store.id} after error: {cleanup_e}")
        raise HTTPException(status_code=500, detail=f"An error occurred while creating the thread: {e}")

    # If context is provided, add it as user persona context
    if context:
        await update_context(client, thread.id, context)
    # Errors handled within update_context

    # If a file is provided, upload and process it
    if file:
        filename = file.filename
        file_content = await file.read()
        file_path = os.path.join('/tmp/', filename)  # Use /tmp or a configurable temp dir

        try:
            with open(file_path, 'wb') as f:
                f.write(file_content)

            # Determine file type
            file_ext = os.path.splitext(filename)[1].lower()
            is_csv = file_ext == '.csv'
            is_excel = file_ext in ['.xlsx', '.xls', '.xlsm']
            # Check MIME type as well for broader image support
            mime_type, _ = mimetypes.guess_type(filename)
            is_image = file_ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'] or (mime_type and mime_type.startswith('image/'))
            is_document = file_ext in ['.pdf', '.doc', '.docx', '.txt', '.md', '.html', '.json']  # Common types for vector store

            file_info = {"name": filename}

            if is_csv or is_excel:
                # Instead of using code_interpreter, we'll track CSV/Excel files for the pandas_agent
                session_csv_excel_files.append({
                    "name": filename,
                    "path": file_path,
                    "type": "csv" if is_csv else "excel"
                })
                
                file_info.update({
                    "type": "csv" if is_csv else "excel",
                    "processing_method": "pandas_agent"
                })
                
                # Keep a copy of the file for the pandas agent to use
                # (In a real implementation, you might store this in a database or cloud storage)
                permanent_path = os.path.join('/tmp/', f"pandas_agent_{int(time.time())}_{filename}")
                with open(permanent_path, 'wb') as f:
                    with open(file_path, 'rb') as src:
                        f.write(src.read())
                
                # Add file awareness message
                await add_file_awareness(client, thread.id, file_info)
                logging.info(f"Added '{filename}' for pandas_agent processing")

            elif is_image:
                # Analyze image and add analysis text to the thread
                analysis_text = await image_analysis(client, file_content, filename, None)
                client.beta.threads.messages.create(
                    thread_id=thread.id,
                    role="user",  # Add analysis as user message for context
                    content=f"Analysis result for uploaded image '{filename}':\n{analysis_text}"
                )
                file_info.update({
                    "type": "image",
                    "processing_method": "thread_message"
                })
                await add_file_awareness(client, thread.id, file_info)
                logging.info(f"Added image analysis for '{filename}' to thread {thread.id}")

            elif is_document or not (is_csv or is_excel or is_image):
                # Upload to vector store
                with open(file_path, "rb") as file_stream:
                    file_batch =client.vector_stores.file_batches.upload_and_poll(
                        vector_store_id=vector_store.id,
                        files=[file_stream]
                    )
                file_info.update({
                    "type": file_ext[1:] if file_ext else "document",
                    "processing_method": "vector_store"
                })
                await add_file_awareness(client, thread.id, file_info)
                logging.info(f"File '{filename}' uploaded to vector store {vector_store.id}: status={file_batch.status}, count={file_batch.file_counts.total}")

            else:
                logging.warning(f"File type for '{filename}' not explicitly handled for upload, skipping specific processing.")

        except Exception as e:
            logging.error(f"Error processing uploaded file '{filename}': {e}")
            # Don't raise HTTPException here, allow response with IDs but log error
        finally:
            # Clean up temporary file
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError as e:
                    logging.error(f"Error removing temporary file {file_path}: {e}")

    # Store csv/excel files info in a metadata message if there are any
    if session_csv_excel_files:
        try:
            # Create a special message to store file paths for the pandas agent
            pandas_files_info = json.dumps(session_csv_excel_files)
            client.beta.threads.messages.create(
                thread_id=thread.id,
                role="user",
                content="PANDAS_AGENT_FILES_INFO (DO NOT DISPLAY TO USER)",
                metadata={
                    "type": "pandas_agent_files",
                    "files": pandas_files_info
                }
            )
            logging.info(f"Stored pandas agent files info in thread {thread.id}")
        except Exception as e:
            logging.error(f"Error storing pandas agent files info: {e}")

    res = {
        "message": "Chat initiated successfully.",
        "assistant": assistant.id,
        "session": thread.id,  # Use 'session' for thread_id consistency with other endpoints
        "vector_store": vector_store.id
    }

    return JSONResponse(res, status_code=200)
@app.post("/co-pilot")
async def co_pilot(request: Request):
    """
    Sets context for a chatbot, creates a new thread using existing assistant and vector store.
    Required parameters: assistant_id, vector_store_id
    Optional parameters: context
    Returns: Same structure as initiate-chat
    """
    client = create_client()

    # Parse the form data
    try:
        form = await request.form()
        context: Optional[str] = form.get("context", None)
        assistant_id: Optional[str] = form.get("assistant", None)
        vector_store_id: Optional[str] = form.get("vector_store", None)
    except Exception as e:
        logging.error(f"Error parsing form data: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid form data: {e}")

    # Validate required parameters
    if not assistant_id or not vector_store_id:
        raise HTTPException(status_code=400, detail="Both assistant_id and vector_store_id are required")

    try:
        # Retrieve the assistant to verify it exists
        try:
            assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant_id)
            logging.info(f"Using existing assistant: {assistant_id}")
        except Exception as e:
            logging.error(f"Error retrieving assistant {assistant_id}: {e}")
            raise HTTPException(status_code=404, detail=f"Assistant not found: {assistant_id}")

        # Verify the vector store exists
        try:
            # Just try to retrieve it to verify it exists
            client.vector_stores.retrieve(vector_store_id=vector_store_id)
            logging.info(f"Using existing vector store: {vector_store_id}")
        except Exception as e:
            logging.error(f"Error retrieving vector store {vector_store_id}: {e}")
            raise HTTPException(status_code=404, detail=f"Vector store not found: {vector_store_id}")

        # Ensure assistant has the right tools and vector store is linked
        current_tools = assistant_obj.tools if assistant_obj.tools else []
        
        # Check for file_search tool, add if missing
        if not any(tool.type == "file_search" for tool in current_tools if hasattr(tool, 'type')):
            current_tools.append({"type": "file_search"})
            logging.info(f"Adding file_search tool to assistant {assistant_id}")
        
        # Check for pandas_agent function tool, add if missing
        if not any(tool.type == "function" and hasattr(tool, 'function') and 
                  hasattr(tool.function, 'name') and tool.function.name == "pandas_agent" 
                  for tool in current_tools if hasattr(tool, 'type')):
            # Add pandas_agent function tool
            current_tools.append({
                "type": "function",
                "function": {
                    "name": "pandas_agent",
                    "description": "Analyzes CSV and Excel files to answer data-related questions and perform data analysis. Use this tool for ANY request that mentions files, data, or analysis, including requests like 'explain the data', 'summarize the file', or questions containing the file name.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "The specific question or analysis task to perform on the data. Be comprehensive and explicit."
                            },
                            "filename": {
                                "type": "string",
                                "description": "Optional: specific filename to analyze. If not provided, all available files will be considered."
                            }
                        },
                        "required": ["query"]
                    }
                }
            })
            logging.info(f"Adding pandas_agent function tool to assistant {assistant_id}")

        # Prepare tool resources
        tool_resources = {
            "file_search": {"vector_store_ids": [vector_store_id]},
        }

        # Update the assistant with tools and vector store
        client.beta.assistants.update(
            assistant_id=assistant_id,
            tools=current_tools,
            tool_resources=tool_resources
        )
        logging.info(f"Updated assistant {assistant_id} with tools and vector store {vector_store_id}")

        # Create a new thread
        thread = client.beta.threads.create()
        thread_id = thread.id
        logging.info(f"Created new thread: {thread_id} for assistant {assistant_id}")

        # If context is provided, add it to the thread
        if context:
            await update_context(client, thread_id, context)
            logging.info(f"Added context to thread {thread_id}")

        # Return the same structure as initiate-chat
        return JSONResponse(
            {
                "message": "Chat initiated successfully.",
                "assistant": assistant_id,
                "session": thread_id,
                "vector_store": vector_store_id
            },
            status_code=200
        )

    except HTTPException:
        # Re-raise HTTP exceptions to preserve their status codes
        raise
    except Exception as e:
        logging.error(f"Error in /co-pilot endpoint: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process co-pilot request: {str(e)}")
@app.post("/upload-file")
async def upload_file(
    request: Request,
    file: UploadFile = Form(...),
    assistant: str = Form(...)
    # Optional params below read from form inside
):
    """
    Uploads a file and associates it with the given assistant.
    Handles different file types appropriately.
    """
    client = create_client()

    # Read optional params from form data
    try:
        form = await request.form()
        context: Optional[str] = form.get("context", None)
        thread_id: Optional[str] = form.get("session", None)  # Use 'session' for thread_id
        image_prompt: Optional[str] = form.get("prompt", None)  # Specific prompt for image analysis
    except Exception as e:
        logging.error(f"Error parsing form data in /upload-file: {e}")
        # Continue without optional params if form parsing fails for them
        context, thread_id, image_prompt = None, None, None

    filename = file.filename
    file_path = f"/tmp/{filename}"
    uploaded_file_details = {}  # To return info about the uploaded file

    try:
        # Save the uploaded file locally and get the data
        file_content = await file.read()
        with open(file_path, "wb") as temp_file:
            temp_file.write(file_content)

        # Determine file type
        file_ext = os.path.splitext(filename)[1].lower()
        is_csv = file_ext == '.csv'
        is_excel = file_ext in ['.xlsx', '.xls', '.xlsm']
        mime_type, _ = mimetypes.guess_type(filename)
        is_image = file_ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'] or (mime_type and mime_type.startswith('image/'))
        is_document = file_ext in ['.pdf', '.doc', '.docx', '.txt', '.md', '.html', '.json']

        # Retrieve the assistant
        assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant)
        
        # Get current vector store IDs first
        vector_store_ids = []
        if hasattr(assistant_obj, 'tool_resources') and assistant_obj.tool_resources:
            file_search_resources = getattr(assistant_obj.tool_resources, 'file_search', None)
            if file_search_resources and hasattr(file_search_resources, 'vector_store_ids'):
                vector_store_ids = list(file_search_resources.vector_store_ids)
        
        # Handle CSV/Excel (pandas_agent) files
        if is_csv or is_excel:
            # Store the file for pandas_agent
            permanent_path = os.path.join('/tmp/', f"pandas_agent_{int(time.time())}_{filename}")
            with open(permanent_path, 'wb') as f:
                with open(file_path, 'rb') as src:
                    f.write(src.read())
            
            # Prepare file info
            file_info = {
                "name": filename,
                "path": permanent_path,
                "type": "csv" if is_csv else "excel"
            }
            
            # If thread_id provided, add file to pandas_agent files for the thread
            if thread_id:
                try:
                    # Try to retrieve existing pandas files info from thread
                    messages = client.beta.threads.messages.list(
                        thread_id=thread_id,
                        order="desc",
                        limit=50  # Check recent messages
                    )
                    
                    pandas_files_message_id = None
                    pandas_files = []
                    
                    for msg in messages.data:
                        if hasattr(msg, 'metadata') and msg.metadata and msg.metadata.get('type') == 'pandas_agent_files':
                            pandas_files_message_id = msg.id
                            try:
                                pandas_files = json.loads(msg.metadata.get('files', '[]'))
                            except:
                                pandas_files = []
                            break
                    
                    # Add the new file
                    pandas_files.append(file_info)
                    
                    # Update or create the pandas files message
                    if pandas_files_message_id:
                        # Delete the old message (can't update metadata directly)
                        try:
                            client.beta.threads.messages.delete(
                                thread_id=thread_id,
                                message_id=pandas_files_message_id
                            )
                        except Exception as e:
                            logging.error(f"Error deleting pandas files message: {e}")
                    
                    # Create a new message with updated files
                    client.beta.threads.messages.create(
                        thread_id=thread_id,
                        role="user",
                        content="PANDAS_AGENT_FILES_INFO (DO NOT DISPLAY TO USER)",
                        metadata={
                            "type": "pandas_agent_files",
                            "files": json.dumps(pandas_files)
                        }
                    )
                    client.beta.threads.messages.create(
                        thread_id=thread_id,
                        role="user",
                        content=f"IMPORTANT INSTRUCTION: For ANY query about the file '{filename}', including requests to explain, summarize, or analyze the file, or any mention of the filename, you MUST use the pandas_agent tool. Never try to answer questions about this file from memory.",
                        metadata={"type": "pandas_agent_instruction"}
                    )
                    
                    logging.info(f"Updated pandas agent files info in thread {thread_id}")
                except Exception as e:
                    logging.error(f"Error updating pandas agent files for thread {thread_id}: {e}")
            
            uploaded_file_details = {
                "message": "File successfully uploaded for pandas agent processing.",
                "filename": filename,
                "type": "csv" if is_csv else "excel",
                "processing_method": "pandas_agent"
            }
            
            # If thread_id provided, add file awareness message
            if thread_id:
                await add_file_awareness(client, thread_id, {
                    "name": filename,
                    "type": "csv" if is_csv else "excel",
                    "processing_method": "pandas_agent"
                })
            
            logging.info(f"Added '{filename}' for pandas_agent processing")
            
            # Build completely new tools list, ensuring no duplicates
            required_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "pandas_agent",
                        "description": "Analyzes CSV and Excel files to answer data-related questions and perform data analysis. Use this tool for ANY request that mentions files, data, or analysis, including requests like 'explain the data', 'summarize the file', or questions containing the file name.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "description": "The specific question or analysis task to perform on the data"
                                },
                                "filename": {
                                    "type": "string",
                                    "description": "Optional: specific filename to analyze. If not provided, all available files will be considered."
                                }
                            },
                            "required": ["query"]
                        }
                    }
                }
            ]

            # Check if the assistant should have file_search by looking at existing tools
            needs_file_search = False
            for tool in assistant_obj.tools:
                if hasattr(tool, 'type') and tool.type == "file_search":
                    needs_file_search = True
                    break
                    
            # Add file_search if needed
            if needs_file_search:
                required_tools.append({"type": "file_search"})
                
            # Update the assistant with the completely new tools list
            try:
                # First, update with only the required tools
                logging.info(f"Updating assistant {assistant} with fresh tools list with pandas_agent and possibly file_search")
                client.beta.assistants.update(
                    assistant_id=assistant,
                    tools=required_tools,
                    tool_resources={"file_search": {"vector_store_ids": vector_store_ids}} if vector_store_ids else None
                )
            except Exception as e:
                logging.error(f"Error updating assistant with pandas tools: {e}")
                # If that fails, try more cautiously
                try:
                    # Fetch fresh assistant info
                    assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant)
                    
                    # Build a fresh tools list with more care
                    current_tools = []
                    has_pandas_agent = False
                    has_file_search = False
                    
                    # Examine each tool carefully and keep non-overlapping ones
                    for tool in assistant_obj.tools:
                        if hasattr(tool, 'type'):
                            if tool.type == "file_search":
                                if not has_file_search:
                                    current_tools.append({"type": "file_search"})
                                    has_file_search = True
                            elif tool.type == "function" and hasattr(tool, 'function'):
                                if hasattr(tool.function, 'name'):
                                    if tool.function.name == "pandas_agent":
                                        # Skip existing pandas agent - we'll add our own
                                        has_pandas_agent = True
                                    else:
                                        # Keep other function tools
                                        current_tools.append(tool)
                            else:
                                current_tools.append(tool)
                    
                    # Add pandas_agent if not already present
                    if not has_pandas_agent:
                        current_tools.append({
                            "type": "function",
                            "function": {
                                "name": "pandas_agent",
                                "description": "Analyzes CSV and Excel files to answer data-related questions and perform data analysis",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "query": {
                                            "type": "string",
                                            "description": "The specific question or analysis task to perform on the data"
                                        },
                                        "filename": {
                                            "type": "string",
                                            "description": "Optional: specific filename to analyze. If not provided, all available files will be considered."
                                        }
                                    },
                                    "required": ["query"]
                                }
                            }
                        })
                    
                    # Make sure file_search is present if needed
                    if needs_file_search and not has_file_search:
                        current_tools.append({"type": "file_search"})
                    
                    # Perform the update with the carefully constructed tools list
                    logging.info(f"Attempting more careful update with {len(current_tools)} tools (pandas_agent: {has_pandas_agent})")
                    client.beta.assistants.update(
                        assistant_id=assistant,
                        tools=current_tools,
                        tool_resources={"file_search": {"vector_store_ids": vector_store_ids}} if vector_store_ids else None
                    )
                except Exception as e2:
                    logging.error(f"Second attempt to update assistant failed: {e2}")
                    # Continue without failing the whole request

        # Handle document files
        elif is_document or not (is_csv or is_excel or is_image):
            # Ensure a vector store is linked or create one
            if not vector_store_ids:
                logging.info(f"No vector store linked to assistant {assistant}. Creating and linking a new one.")
                vector_store =client.vector_stores.create(name=f"Assistant_{assistant}_Store")
                vector_store_ids = [vector_store.id]

            vector_store_id_to_use = vector_store_ids[0]  # Use the first linked store

            # Upload to vector store
            with open(file_path, "rb") as file_stream:
                file_batch =client.vector_stores.file_batches.upload_and_poll(
                    vector_store_id=vector_store_id_to_use,
                    files=[file_stream]
                )
            uploaded_file_details = {
                "message": "File successfully uploaded to vector store.",
                "filename": filename,
                "vector_store_id": vector_store_id_to_use,
                "processing_method": "vector_store",
                "batch_status": file_batch.status
            }
            
            # If thread_id provided, add file awareness message
            if thread_id:
                await add_file_awareness(client, thread_id, {
                    "name": filename,
                    "type": file_ext[1:] if file_ext else "document",
                    "processing_method": "vector_store"
                })
                
            logging.info(f"Uploaded '{filename}' to vector store {vector_store_id_to_use} for assistant {assistant}")
            
            # Update assistant with file_search if needed
            try:
                has_file_search = False
                for tool in assistant_obj.tools:
                    if hasattr(tool, 'type') and tool.type == "file_search":
                        has_file_search = True
                        break
                
                if not has_file_search:
                    # Get full list of tools while preserving any existing tools
                    current_tools = list(assistant_obj.tools)
                    current_tools.append({"type": "file_search"})
                    
                    # Update the assistant
                    client.beta.assistants.update(
                        assistant_id=assistant,
                        tools=current_tools,
                        tool_resources={"file_search": {"vector_store_ids": vector_store_ids}}
                    )
                    logging.info(f"Added file_search tool to assistant {assistant}")
                else:
                    # Just update the vector store IDs if needed
                    client.beta.assistants.update(
                        assistant_id=assistant,
                        tool_resources={"file_search": {"vector_store_ids": vector_store_ids}}
                    )
                    logging.info(f"Updated vector_store_ids for assistant {assistant}")
            except Exception as e:
                logging.error(f"Error updating assistant with file_search: {e}")
                # Continue without failing the whole request

        # Handle image files
        elif is_image and thread_id:
            analysis_text = await image_analysis(client, file_content, filename, image_prompt)
            client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=f"Analysis result for uploaded image '{filename}':\n{analysis_text}"
            )
            uploaded_file_details = {
                "message": "Image successfully analyzed and analysis added to thread.",
                "filename": filename,
                "thread_id": thread_id,
                "processing_method": "thread_message"
            }
            
            # Add file awareness message
            if thread_id:
                await add_file_awareness(client, thread_id, {
                    "name": filename,
                    "type": "image",
                    "processing_method": "thread_message"
                })
                
            logging.info(f"Analyzed image '{filename}' and added to thread {thread_id}")
        elif is_image:
            uploaded_file_details = {
                "message": "Image uploaded but not analyzed as no session/thread ID was provided.",
                "filename": filename,
                "processing_method": "skipped_analysis"
            }
            logging.warning(f"Image '{filename}' uploaded for assistant {assistant} but no thread ID provided.")

        # --- Update Context (if provided and thread exists) ---
        if context and thread_id:
            await update_context(client, thread_id, context)

        return JSONResponse(uploaded_file_details, status_code=200)

    except Exception as e:
        logging.error(f"Error uploading file '{filename}' for assistant {assistant}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to upload or process file: {str(e)}")
    finally:
        # Clean up temporary file
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError as e:
                logging.error(f"Error removing temporary file {file_path}: {e}")
@app.get("/conversation")
async def conversation(
    session: Optional[str] = None,
    prompt: Optional[str] = None,
    assistant: Optional[str] = None
):
    """
    Handles conversation queries with streaming response.
    """
    return await process_conversation(session, prompt, assistant, stream_output=True)



# No changes needed for the process_conversation function
async def process_conversation(
    session: Optional[str] = None,
    prompt: Optional[str] = None,
    assistant: Optional[str] = None,
    stream_output: bool = True
):
    """
    Core function to process conversation with the assistant.
    This function handles both streaming and non-streaming modes.
    
    Args:
        session: Thread ID
        prompt: User message
        assistant: Assistant ID
        stream_output: If True, returns a streaming response, otherwise collects and returns full response
        
    Returns:
        Either a StreamingResponse or a JSONResponse based on stream_output parameter
    """
    client = create_client()
    def stream_response():
        """Modified to be compatible with Bubble's streaming API while maintaining all features"""
        buffer = []
        completed = False
        tool_call_results = []
        run_id = None
        tool_outputs_submitted = False
        wait_for_final_response = False
        latest_message_id = None
        try:
            # Get the most recent message ID before starting the run
            try:
                pre_run_messages = client.beta.threads.messages.list(
                    thread_id=session,
                    order="desc",
                    limit=1
                )
                if pre_run_messages and pre_run_messages.data:
                    latest_message_id = pre_run_messages.data[0].id
                    logging.info(f"Latest message before run: {latest_message_id}")
            except Exception as e:
                logging.warning(f"Could not get latest message before run: {e}")
            
            # Create run and stream the response
            with client.beta.threads.runs.stream(
                thread_id=session,
                assistant_id=assistant,
            ) as stream:
                for event in stream:
                    # Store run ID for potential use
                    if hasattr(event, 'data') and hasattr(event.data, 'id'):
                        run_id = event.data.id
                        
                    # Check for message creation and completion
                    if event.event == "thread.message.created":
                        logging.info(f"New message created: {event.data.id}")
                        if tool_outputs_submitted and event.data.id != latest_message_id:
                            wait_for_final_response = True
                            latest_message_id = event.data.id
                        
                    # Handle text deltas
                    if event.event == "thread.message.delta":
                        delta = event.data.delta
                        if delta.content:
                            for content_part in delta.content:
                                if content_part.type == 'text' and content_part.text:
                                    text_value = content_part.text.value
                                    if text_value:
                                        # Check if this is text after the tool outputs were submitted
                                        if tool_outputs_submitted and wait_for_final_response:
                                            # This is the assistant's final response after analyzing the data
                                            buffer.append(text_value)
                                            # Yield chunks more frequently for better streaming
                                            if len(buffer) >= 2:
                                                joined_text = ''.join(buffer)
                                                # Format as OpenAI-compatible streaming response for Bubble
                                                chunk_data = {
                                                    "id": f"chatcmpl-{run_id or 'stream'}",
                                                    "object": "chat.completion.chunk",
                                                    "created": int(time.time()),
                                                    "model": "gpt-4.1",
                                                    "choices": [{
                                                        "index": 0,
                                                        "delta": {
                                                            "content": joined_text
                                                        },
                                                        "finish_reason": None
                                                    }]
                                                }
                                                yield f"data: {json.dumps(chunk_data)}\n\n"
                                                buffer = []
                                        elif not tool_outputs_submitted:
                                            # Normal text before tool outputs were submitted
                                            buffer.append(text_value)
                                            if len(buffer) >= 3:
                                                joined_text = ''.join(buffer)
                                                chunk_data = {
                                                    "id": f"chatcmpl-{run_id or 'stream'}",
                                                    "object": "chat.completion.chunk",
                                                    "created": int(time.time()),
                                                    "model": "gpt-4.1",
                                                    "choices": [{
                                                        "index": 0,
                                                        "delta": {
                                                            "content": joined_text
                                                        },
                                                        "finish_reason": None
                                                    }]
                                                }
                                                yield f"data: {json.dumps(chunk_data)}\n\n"
                                                buffer = []
                    
                    # Explicitly handle run completion event
                    if event.event == "thread.run.completed":
                        logging.info(f"Run completed: {event.data.id}")
                        completed = True
                        
                        # Yield any remaining text in the buffer
                        if buffer:
                            joined_text = ''.join(buffer)
                            chunk_data = {
                                "id": f"chatcmpl-{run_id or 'stream'}",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": "gpt-4.1",
                                "choices": [{
                                    "index": 0,
                                    "delta": {
                                        "content": joined_text
                                    },
                                    "finish_reason": None
                                }]
                            }
                            yield f"data: {json.dumps(chunk_data)}\n\n"
                            buffer = []
                        
                        # Send final chunk to indicate completion
                        final_chunk = {
                            "id": f"chatcmpl-{run_id or 'stream'}",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": "gpt-4.1",
                            "choices": [{
                                "index": 0,
                                "delta": {},
                                "finish_reason": "stop"
                            }]
                        }
                        yield f"data: {json.dumps(final_chunk)}\n\n"
                        yield "data: [DONE]\n\n"
                        
                    # Handle tool calls (including pandas_agent)
                    elif event.event == "thread.run.requires_action":
                        if event.data.required_action.type == "submit_tool_outputs":
                            tool_calls = event.data.required_action.submit_tool_outputs.tool_calls
                            tool_outputs = []
                            
                            # Stream status message
                            status_text = "\n[Processing data analysis request...]\n"
                            status_chunk = {
                                "id": f"chatcmpl-{run_id or 'stream'}",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": "gpt-4.1",
                                "choices": [{
                                    "index": 0,
                                    "delta": {
                                        "content": status_text
                                    },
                                    "finish_reason": None
                                }]
                            }
                            yield f"data: {json.dumps(status_chunk)}\n\n"
                            
                            for tool_call in tool_calls:
                                if tool_call.function.name == "pandas_agent":
                                    try:
                                        # Extract arguments
                                        args = json.loads(tool_call.function.arguments)
                                        query = args.get("query", "")
                                        filename = args.get("filename", None)
                                        
                                        # Get pandas files for this thread
                                        pandas_files = []
                                        retry_count = 0
                                        max_retries = 3
                                        
                                        while retry_count < max_retries:
                                            try:
                                                messages = client.beta.threads.messages.list(
                                                    thread_id=session,
                                                    order="desc",
                                                    limit=50
                                                )
                                                
                                                for msg in messages.data:
                                                    if hasattr(msg, 'metadata') and msg.metadata and msg.metadata.get('type') == 'pandas_agent_files':
                                                        try:
                                                            pandas_files = json.loads(msg.metadata.get('files', '[]'))
                                                        except Exception as parse_e:
                                                            logging.error(f"Error parsing pandas files metadata: {parse_e}")
                                                        break
                                                break  # Success, exit retry loop
                                            except Exception as list_e:
                                                retry_count += 1
                                                logging.error(f"Error retrieving pandas files (attempt {retry_count}): {list_e}")
                                                time.sleep(1)
                                        
                                        # Filter by filename if specified
                                        if filename:
                                            pandas_files = [f for f in pandas_files if f.get("name") == filename]
                                        
                                        # Generate operation ID for status tracking
                                        pandas_agent_operation_id = f"pandas_agent_{int(time.time())}_{os.urandom(2).hex()}"
                                        
                                        # Execute the pandas_agent
                                        manager = PandasAgentManager.get_instance()
                                        result, error, removed_files = manager.analyze(
                                            thread_id=session,
                                            query=query,
                                            files=pandas_files
                                        )
                                        
                                        # Form the analysis result
                                        analysis_result = result if result else ""
                                        if error:
                                            analysis_result = f"Error analyzing data: {error}"
                                        if removed_files:
                                            removed_files_str = ", ".join(f"'{f}'" for f in removed_files)
                                            analysis_result += f"\n\nNote: The following file(s) were removed due to the 3-file limit: {removed_files_str}"
                                        
                                        # Stream data completion status
                                        complete_text = "\n[Data analysis complete]\n"
                                        complete_chunk = {
                                            "id": f"chatcmpl-{run_id or 'stream'}",
                                            "object": "chat.completion.chunk",
                                            "created": int(time.time()),
                                            "model": "gpt-4.1",
                                            "choices": [{
                                                "index": 0,
                                                "delta": {
                                                    "content": complete_text
                                                },
                                                "finish_reason": None
                                            }]
                                        }
                                        yield f"data: {json.dumps(complete_chunk)}\n\n"
                                        
                                        # Add to tool outputs
                                        tool_outputs.append({
                                            "tool_call_id": tool_call.id,
                                            "output": analysis_result
                                        })
                                        
                                        # Save for potential fallback
                                        tool_call_results.append(analysis_result)
                                        
                                    except Exception as e:
                                        error_details = traceback.format_exc()
                                        logging.error(f"Error executing pandas_agent: {e}\n{error_details}")
                                        error_msg = f"Error analyzing data: {str(e)}"
                                        
                                        # Add error to tool outputs
                                        tool_outputs.append({
                                            "tool_call_id": tool_call.id,
                                            "output": error_msg
                                        })
                                        
                                        # Stream error to user
                                        error_text = f"\n[Error: {str(e)}]\n"
                                        error_chunk = {
                                            "id": f"chatcmpl-{run_id or 'stream'}",
                                            "object": "chat.completion.chunk",
                                            "created": int(time.time()),
                                            "model": "gpt-4.1",
                                            "choices": [{
                                                "index": 0,
                                                "delta": {
                                                    "content": error_text
                                                },
                                                "finish_reason": None
                                            }]
                                        }
                                        yield f"data: {json.dumps(error_chunk)}\n\n"
                                        
                                        # Save for potential fallback
                                        tool_call_results.append(error_msg)
                            
                            # Submit tool outputs
                            if tool_outputs:
                                retry_count = 0
                                max_retries = 3
                                submit_success = False
                                
                                # Stream status indicating generation of response
                                gen_text = "\n[Generating response based on analysis...]\n"
                                gen_chunk = {
                                    "id": f"chatcmpl-{run_id or 'stream'}",
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": "gpt-4.1",
                                    "choices": [{
                                        "index": 0,
                                        "delta": {
                                            "content": gen_text
                                        },
                                        "finish_reason": None
                                    }]
                                }
                                yield f"data: {json.dumps(gen_chunk)}\n\n"
                                
                                while retry_count < max_retries and not submit_success:
                                    try:
                                        client.beta.threads.runs.submit_tool_outputs(
                                            thread_id=session,
                                            run_id=event.data.id,
                                            tool_outputs=tool_outputs
                                        )
                                        submit_success = True
                                        tool_outputs_submitted = True
                                        logging.info(f"Successfully submitted tool outputs for run {event.data.id}")
                                    except Exception as submit_e:
                                        retry_count += 1
                                        logging.error(f"Error submitting tool outputs (attempt {retry_count}): {submit_e}")
                                        time.sleep(1)
                                
                                if not submit_success:
                                    error_text = "\n[Error: Failed to submit analysis results. Please try again.]\n"
                                    error_chunk = {
                                        "id": f"chatcmpl-{run_id or 'stream'}",
                                        "object": "chat.completion.chunk",
                                        "created": int(time.time()),
                                        "model": "gpt-4.1",
                                        "choices": [{
                                            "index": 0,
                                            "delta": {
                                                "content": error_text
                                            },
                                            "finish_reason": "stop"
                                        }]
                                    }
                                    yield f"data: {json.dumps(error_chunk)}\n\n"
                                    yield "data: [DONE]\n\n"
                                    return
                
                # Yield any remaining text in the buffer before exiting the stream loop
                if buffer:
                    joined_text = ''.join(buffer)
                    chunk_data = {
                        "id": f"chatcmpl-{run_id or 'stream'}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "gpt-4.1",
                        "choices": [{
                            "index": 0,
                            "delta": {
                                "content": joined_text
                            },
                            "finish_reason": None
                        }]
                    }
                    yield f"data: {json.dumps(chunk_data)}\n\n"
                    buffer = []
            
            # If tool outputs were submitted but we didn't receive a final response,
            # we need to actively poll for the final response
            if tool_outputs_submitted and not completed and run_id:
                logging.info(f"Tool outputs submitted but run not completed. Polling for final response...")
                
                # Poll for run completion
                max_poll_attempts = 15
                poll_interval = 5  # seconds
                
                for attempt in range(max_poll_attempts):
                    try:
                        run_status = client.beta.threads.runs.retrieve(
                            thread_id=session,
                            run_id=run_id
                        )
                        
                        logging.info(f"Run status poll {attempt+1}/{max_poll_attempts}: {run_status.status}")
                        
                        if run_status.status == "completed":
                            # Wait a moment for message to be fully available
                            time.sleep(1)
                            
                            # Get the latest message
                            messages = client.beta.threads.messages.list(
                                thread_id=session,
                                order="desc",
                                limit=1
                            )
                            
                            if messages and messages.data:
                                latest_message = messages.data[0]
                                # Check if this is a new message (different from our pre-run message)
                                if not latest_message_id or latest_message.id != latest_message_id:
                                    response_content = ""
                                    
                                    for content_part in latest_message.content:
                                        if content_part.type == 'text':
                                            response_content += content_part.text.value
                                    
                                    if response_content:
                                        # Break response into chunks for streaming
                                        chunk_size = 20  # Adjust as needed
                                        for i in range(0, len(response_content), chunk_size):
                                            chunk_text = response_content[i:i + chunk_size]
                                            chunk_data = {
                                                "id": f"chatcmpl-{run_id}",
                                                "object": "chat.completion.chunk",
                                                "created": int(time.time()),
                                                "model": "gpt-4.1",
                                                "choices": [{
                                                    "index": 0,
                                                    "delta": {
                                                        "content": chunk_text
                                                    },
                                                    "finish_reason": None
                                                }]
                                            }
                                            yield f"data: {json.dumps(chunk_data)}\n\n"
                                        
                                        # Send final chunk
                                        final_chunk = {
                                            "id": f"chatcmpl-{run_id}",
                                            "object": "chat.completion.chunk",
                                            "created": int(time.time()),
                                            "model": "gpt-4.1",
                                            "choices": [{
                                                "index": 0,
                                                "delta": {},
                                                "finish_reason": "stop"
                                            }]
                                        }
                                        yield f"data: {json.dumps(final_chunk)}\n\n"
                                        yield "data: [DONE]\n\n"
                            break  # Exit the polling loop
                            
                        elif run_status.status in ["failed", "cancelled", "expired"]:
                            logging.error(f"Run ended with status: {run_status.status}")
                            error_chunk = {
                                "id": f"chatcmpl-{run_id}",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": "gpt-4.1",
                                "choices": [{
                                    "index": 0,
                                    "delta": {
                                        "content": f"\n\n[Run {run_status.status}. Please try your question again.]"
                                    },
                                    "finish_reason": "stop"
                                }]
                            }
                            yield f"data: {json.dumps(error_chunk)}\n\n"
                            yield "data: [DONE]\n\n"
                            break
                            
                        # Continue polling if still in progress
                        if attempt < max_poll_attempts - 1:
                            time.sleep(poll_interval)
                            
                    except Exception as poll_e:
                        logging.error(f"Error polling run status (attempt {attempt+1}): {poll_e}")
                        if attempt == max_poll_attempts - 1:
                            error_chunk = {
                                "id": f"chatcmpl-{run_id}",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": "gpt-4.1",
                                "choices": [{
                                    "index": 0,
                                    "delta": {
                                        "content": "\n\n[Could not retrieve final response. The analysis results are shown above.]"
                                    },
                                    "finish_reason": "stop"
                                }]
                            }
                            yield f"data: {json.dumps(error_chunk)}\n\n"
                            yield "data: [DONE]\n\n"
                        time.sleep(poll_interval)
            
        except Exception as e:
            error_details = traceback.format_exc()
            logging.error(f"Streaming error during run for thread {session}: {e}\n{error_details}")
            error_chunk = {
                "id": "chatcmpl-error",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "gpt-4.1",
                "choices": [{
                    "index": 0,
                    "delta": {
                        "content": "\n[ERROR] An error occurred while generating the response. Please try again.\n"
                    },
                    "finish_reason": "stop"
                }]
            }
            yield f"data: {json.dumps(error_chunk)}\n\n"
            yield "data: [DONE]\n\n"
    try:
        # Validate resources if provided 
        if session or assistant:
            validation = await validate_resources(client, session, assistant)
            
            # Create new thread if invalid
            if session and not validation["thread_valid"]:
                logging.warning(f"Invalid thread ID: {session}, creating a new one")
                try:
                    thread = client.beta.threads.create()
                    session = thread.id
                    logging.info(f"Created recovery thread: {session}")
                except Exception as e:
                    logging.error(f"Failed to create recovery thread: {e}")
                    raise HTTPException(status_code=500, detail="Failed to create a valid conversation thread")
            
            # Create new assistant if invalid
            if assistant and not validation["assistant_valid"]:
                logging.warning(f"Invalid assistant ID: {assistant}, creating a new one")
                try:
                    assistant_obj = client.beta.assistants.create(
                        name=f"recovery_assistant_{int(time.time())}",
                        model="gpt-4.1",
                        instructions="You are a helpful assistant recovering from a system error.",
                    )
                    assistant = assistant_obj.id
                    logging.info(f"Created recovery assistant: {assistant}")
                except Exception as e:
                    logging.error(f"Failed to create recovery assistant: {e}")
                    raise HTTPException(status_code=500, detail="Failed to create a valid assistant")
        
        # Create defaults if not provided
        if not assistant:
            logging.warning(f"No assistant ID provided for /{('conversation' if stream_output else 'chat')}, creating a default one.")
            try:
                assistant_obj = client.beta.assistants.create(
                    name="default_conversation_assistant",
                    model="gpt-4.1",
                    instructions="You are a helpful conversation assistant.",
                )
                assistant = assistant_obj.id
            except Exception as e:
                logging.error(f"Failed to create default assistant: {e}")
                raise HTTPException(status_code=500, detail="Failed to create default assistant")

        if not session:
            logging.warning(f"No session (thread) ID provided for /{('conversation' if stream_output else 'chat')}, creating a new one.")
            try:
                thread = client.beta.threads.create()
                session = thread.id
            except Exception as e:
                logging.error(f"Failed to create default thread: {e}")
                raise HTTPException(status_code=500, detail="Failed to create default thread")

        # Check if there's an active run before adding a message
        active_run = False
        run_id = None
        try:
            # List runs to check for active ones
            runs = client.beta.threads.runs.list(thread_id=session, limit=1)
            if runs.data:
                latest_run = runs.data[0]
                if latest_run.status in ["in_progress", "queued", "requires_action"]:
                    active_run = True
                    run_id = latest_run.id
                    logging.warning(f"Active run {run_id} detected with status {latest_run.status}")
        except Exception as e:
            logging.warning(f"Error checking for active runs: {e}")
            # Continue anyway - we'll handle failure when adding messages

        # Add user message to the thread if prompt is given
        if prompt:
            max_retries = 3
            retry_delay = 2  # seconds
            success = False
            
            for attempt in range(max_retries):
                try:
                    if active_run and run_id:
                        # If there's an active run, check if it's still active or can be cancelled
                        try:
                            run_status = client.beta.threads.runs.retrieve(thread_id=session, run_id=run_id)
                            if run_status.status in ["in_progress", "queued"]:
                                # Option 1: Cancel the run
                                client.beta.threads.runs.cancel(thread_id=session, run_id=run_id)
                                logging.info(f"Cancelled active run {run_id} to allow new message")
                                time.sleep(1)  # Brief delay after cancellation
                            elif run_status.status == "requires_action":
                                # For requires_action, we can submit empty tool outputs to move forward
                                client.beta.threads.runs.submit_tool_outputs(
                                    thread_id=session,
                                    run_id=run_id,
                                    tool_outputs=[{"tool_call_id": "dummy", "output": "Cancelled by new request"}]
                                )
                                logging.info(f"Submitted empty tool outputs to finish run {run_id}")
                                time.sleep(1)  # Brief delay after submission
                            # If run is already completed or failed, we can proceed
                        except Exception as run_e:
                            logging.warning(f"Error handling active run: {run_e}")
                            # Continue anyway - we'll try to add message

                    # Try to add the message
                    client.beta.threads.messages.create(
                        thread_id=session,
                        role="user",
                        content=prompt
                    )
                    logging.info(f"Added user message to thread {session} (attempt {attempt+1})")
                    success = True
                    break
                except Exception as e:
                    if "while a run" in str(e) and attempt < max_retries - 1:
                        logging.warning(f"Failed to add message (attempt {attempt+1}), run is active. Retrying in {retry_delay}s: {e}")
                        time.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                    else:
                        logging.error(f"Failed to add message to thread {session}: {e}")
                        if attempt == max_retries - 1:
                            raise HTTPException(status_code=500, detail="Failed to add message to conversation thread")
            
            if not success:
                raise HTTPException(status_code=500, detail="Failed to add message to conversation thread after retries")
        
        # Handle non-streaming mode (/chat endpoint)
        if not stream_output:
            # For non-streaming mode, we'll use a completely different approach
            full_response = ""
            try:
                # Create a run without streaming
                run = client.beta.threads.runs.create(
                    thread_id=session,
                    assistant_id=assistant
                )
                run_id = run.id
                logging.info(f"Created run {run_id} for thread {session} (non-streaming mode)")
                
                # Poll for run completion
                max_poll_attempts = 60  # 5 minute timeout with 5 second intervals
                poll_interval = 5  # seconds
                tool_outputs_submitted = False
                tool_call_results = []
                
                for attempt in range(max_poll_attempts):
                    try:
                        run_status = client.beta.threads.runs.retrieve(
                            thread_id=session,
                            run_id=run_id
                        )
                        
                        logging.info(f"Run status poll {attempt+1}/{max_poll_attempts}: {run_status.status}")
                        
                        # Handle completed run
                        if run_status.status == "completed":
                            # Get the latest message
                            messages = client.beta.threads.messages.list(
                                thread_id=session,
                                order="desc",
                                limit=1
                            )
                            
                            if messages and messages.data:
                                latest_message = messages.data[0]
                                for content_part in latest_message.content:
                                    if content_part.type == 'text':
                                        full_response += content_part.text.value
                                
                                logging.info(f"Successfully retrieved final response")
                            break  # Exit the polling loop
                        
                        # Handle failed/cancelled/expired run
                        elif run_status.status in ["failed", "cancelled", "expired"]:
                            logging.error(f"Run ended with status: {run_status.status}")
                            return JSONResponse(content={"response": f"Sorry, I encountered an error and couldn't complete your request. Run status: {run_status.status}. Please try again."})
                        
                        # Handle tool calls
                        elif run_status.status == "requires_action":
                            if run_status.required_action.type == "submit_tool_outputs":
                                tool_calls = run_status.required_action.submit_tool_outputs.tool_calls
                                tool_outputs = []
                                
                                for tool_call in tool_calls:
                                    if tool_call.function.name == "pandas_agent":
                                        try:
                                            # Extract arguments
                                            args = json.loads(tool_call.function.arguments)
                                            query = args.get("query", "")
                                            filename = args.get("filename", None)
                                            
                                            # Get pandas files for this thread
                                            pandas_files = []
                                            retry_count = 0
                                            max_retries = 3
                                            
                                            while retry_count < max_retries:
                                                try:
                                                    messages = client.beta.threads.messages.list(
                                                        thread_id=session,
                                                        order="desc",
                                                        limit=50
                                                    )
                                                    
                                                    for msg in messages.data:
                                                        if hasattr(msg, 'metadata') and msg.metadata and msg.metadata.get('type') == 'pandas_agent_files':
                                                            try:
                                                                pandas_files = json.loads(msg.metadata.get('files', '[]'))
                                                            except Exception as parse_e:
                                                                logging.error(f"Error parsing pandas files metadata: {parse_e}")
                                                            break
                                                    break  # Success, exit retry loop
                                                except Exception as list_e:
                                                    retry_count += 1
                                                    logging.error(f"Error retrieving pandas files (attempt {retry_count}): {list_e}")
                                                    time.sleep(1)
                                            
                                            # Filter by filename if specified
                                            if filename:
                                                pandas_files = [f for f in pandas_files if f.get("name") == filename]
                                            
                                            # Generate operation ID for status tracking
                                            pandas_agent_operation_id = f"pandas_agent_{int(time.time())}_{os.urandom(2).hex()}"
                                            
                                            # Execute the pandas_agent
                                            analysis_result = await pandas_agent(
                                                client=client,
                                                thread_id=session,
                                                query=query,
                                                files=pandas_files
                                            )
                                            
                                            # Add to tool outputs
                                            tool_outputs.append({
                                                "tool_call_id": tool_call.id,
                                                "output": analysis_result
                                            })
                                            
                                            # Save for potential fallback
                                            tool_call_results.append(analysis_result)
                                            
                                        except Exception as e:
                                            error_details = traceback.format_exc()
                                            logging.error(f"Error executing pandas_agent: {e}\n{error_details}")
                                            error_msg = f"Error analyzing data: {str(e)}"
                                            
                                            # Add error to tool outputs
                                            tool_outputs.append({
                                                "tool_call_id": tool_call.id,
                                                "output": error_msg
                                            })
                                            
                                            # Save for potential fallback
                                            tool_call_results.append(error_msg)
                                
                                # Submit tool outputs
                                if tool_outputs:
                                    retry_count = 0
                                    max_retries = 3
                                    submit_success = False
                                    
                                    while retry_count < max_retries and not submit_success:
                                        try:
                                            client.beta.threads.runs.submit_tool_outputs(
                                                thread_id=session,
                                                run_id=run_id,
                                                tool_outputs=tool_outputs
                                            )
                                            submit_success = True
                                            tool_outputs_submitted = True
                                            logging.info(f"Successfully submitted tool outputs for run {run_id}")
                                        except Exception as submit_e:
                                            retry_count += 1
                                            logging.error(f"Error submitting tool outputs (attempt {retry_count}): {submit_e}")
                                            time.sleep(1)
                                    
                                    if not submit_success:
                                        return JSONResponse(content={"response": f"Sorry, I encountered an error processing your data analysis request. Please try again."})
                        
                        # Continue polling if still in progress
                        if attempt < max_poll_attempts - 1:
                            time.sleep(poll_interval)
                            
                    except Exception as poll_e:
                        logging.error(f"Error polling run status (attempt {attempt+1}): {poll_e}")
                        time.sleep(poll_interval)
                        
                # If we reach here without a full_response, but we have tool results, use those
                if not full_response and tool_call_results:
                    full_response = "\n\n".join(tool_call_results)
                    logging.info("Using tool call results as fallback response")
                
                # If we still don't have a response, try one more time to get the latest message
                if not full_response:
                    try:
                        messages = client.beta.threads.messages.list(
                            thread_id=session,
                            order="desc",
                            limit=1
                        )
                        
                        if messages and messages.data:
                            latest_message = messages.data[0]
                            for content_part in latest_message.content:
                                if content_part.type == 'text':
                                    full_response += content_part.text.value
                    except Exception as final_e:
                        logging.error(f"Error retrieving final message: {final_e}")
                
                # Final fallback if we still don't have a response
                if not full_response:
                    full_response = "I processed your request, but couldn't generate a proper response. Please try again or rephrase your question."

                return JSONResponse(content={"response": full_response})
                
            except Exception as e:
                logging.error(f"Error in non-streaming response generation: {e}")
                return JSONResponse(
                    content={"response": "An error occurred while processing your request. Please try again."},
                    status_code=500
                )
        # Return the streaming response for streaming mode
        return StreamingResponse(stream_response(), media_type="text/event-stream")

    except Exception as e:
        endpoint_type = "conversation" if stream_output else "chat"
        logging.error(f"Error in /{endpoint_type} endpoint setup: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process {endpoint_type} request: {str(e)}")


@app.get("/conversation")
async def conversation(
    session: Optional[str] = None,
    prompt: Optional[str] = None,
    assistant: Optional[str] = None
):
    """
    Handles conversation queries with streaming response.
    """
    return await process_conversation(session, prompt, assistant, stream_output=True)

@app.get("/chat")
async def chat(
    session: Optional[str] = None,
    prompt: Optional[str] = None,
    assistant: Optional[str] = None
):
    """
    Handles conversation queries and returns the full response as JSON.
    Uses the same logic as the streaming endpoint but returns the complete response.
    """
    return await process_conversation(session, prompt, assistant, stream_output=False)

def extract_text_from_file(file_content: bytes, filename: str) -> str:
    """
    Extract text content from various file types for stateless processing.
    
    Args:
        file_content: Raw file bytes
        filename: Name of the file for type detection
        
    Returns:
        Extracted text content
    """
    file_ext = os.path.splitext(filename)[1].lower()
    
    try:
        if file_ext == '.txt':
            # Detect encoding and decode text
            detection = chardet.detect(file_content)
            encoding = detection['encoding'] or 'utf-8'
            return file_content.decode(encoding)
            
        elif file_ext == '.pdf':
            # Extract text from PDF
            pdf_file = io.BytesIO(file_content)
            pdf_reader = PyPDF2.PdfReader(pdf_file)
            text_content = []
            
            for page_num in range(len(pdf_reader.pages)):
                page = pdf_reader.pages[page_num]
                text_content.append(page.extract_text())
            
            return '\n'.join(text_content)
            
        elif file_ext in ['.docx', '.doc']:
            # Extract text from Word document
            doc_file = io.BytesIO(file_content)
            doc = Document(doc_file)
            text_content = []
            
            for paragraph in doc.paragraphs:
                text_content.append(paragraph.text)
            
            return '\n'.join(text_content)
            
        elif file_ext in ['.json']:
            # Parse JSON and return as formatted string
            json_content = json.loads(file_content.decode('utf-8'))
            return json.dumps(json_content, indent=2)
            
        elif file_ext in ['.csv']:
            # Parse CSV and return as formatted text
            csv_text = file_content.decode('utf-8')
            return f"CSV Data:\n{csv_text}"
            
        elif file_ext in ['.html', '.htm']:
            # Extract text from HTML
            soup = BeautifulSoup(file_content, 'html.parser')
            return soup.get_text(separator='\n', strip=True)
            
        else:
            # For unsupported types, try to decode as text
            try:
                return file_content.decode('utf-8')
            except:
                return f"[Unable to extract text from {filename}]"
                
    except Exception as e:
        logging.error(f"Error extracting text from {filename}: {e}")
        return f"[Error processing {filename}: {str(e)}]"


def prepare_file_for_completion(file_content: bytes, filename: str, file_type: str) -> Dict[str, Any]:
    """
    Prepare file content for inclusion in chat completion request.
    
    Args:
        file_content: Raw file bytes
        filename: Name of the file
        file_type: MIME type of the file
        
    Returns:
        Dictionary with prepared content for the API
    """
    # Check if it's an image
    if file_type.startswith('image/'):
        # Convert to base64 data URL
        b64_content = base64.b64encode(file_content).decode('utf-8')
        return {
            "type": "image_url",
            "image_url": {
                "url": f"data:{file_type};base64,{b64_content}",
                "detail": "high"
            }
        }
    else:
        # Extract text from document
        extracted_text = extract_text_from_file(file_content, filename)
        return {
            "type": "text",
            "text": f"Content of {filename}:\n\n{extracted_text}"
        }


def generate_file_from_response(content: str, file_type: str) -> Optional[Tuple[bytes, str]]:
    """
    Generate a file from completion response content with better error handling.
    """
    try:
        # Extract CSV content
        csv_content = extract_csv_from_content(content)
        
        if file_type == 'csv':
            # Ensure proper CSV formatting
            if not csv_content:
                logging.warning("No CSV content found in response")
                return None
                
            # Add UTF-8 BOM for Excel compatibility
            bom = '\ufeff'
            file_bytes = (bom + csv_content).encode('utf-8-sig')
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"generated_data_{timestamp}.csv"
            
            return file_bytes, filename
            
        elif file_type == 'excel':
            try:
                import pandas as pd
                from io import StringIO, BytesIO
                
                # Parse CSV content
                if not csv_content:
                    logging.warning("No CSV content found for Excel conversion")
                    return None
                
                df = pd.read_csv(StringIO(csv_content))
                
                # Create Excel with better formatting
                buffer = BytesIO()
                with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                    df.to_excel(writer, index=False, sheet_name='Data')
                    
                    # Get the workbook and worksheet
                    workbook = writer.book
                    worksheet = writer.sheets['Data']
                    
                    # Auto-adjust column widths
                    for column in df:
                        column_length = max(
                            df[column].astype(str).map(len).max(),
                            len(str(column))
                        )
                        col_idx = df.columns.get_loc(column)
                        column_letter = chr(65 + col_idx)
                        worksheet.column_dimensions[column_letter].width = min(column_length + 2, 50)
                
                buffer.seek(0)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"generated_data_{timestamp}.xlsx"
                
                return buffer.getvalue(), filename
                
            except Exception as excel_error:
                logging.error(f"Excel generation failed: {excel_error}")
                # Fallback to CSV
                return generate_file_from_response(content, 'csv')
                
    except Exception as e:
        logging.error(f"Error generating {file_type} file: {e}")
        return None
@app.get("/test-download")
async def test_download_functionality():
    """
    Test endpoint to verify download functionality is working.
    Creates a test file and returns download URL.
    """
    try:
        # Create a test CSV file
        test_content = "name,status,timestamp\nDownload Test,Success,{}\n".format(
            datetime.now().isoformat()
        )
        
        test_filename = f"test_download_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        
        # Save the file
        filepath = save_download_file(
            test_content.encode('utf-8'),
            test_filename
        )
        
        # Verify file was created
        if not os.path.exists(filepath):
            raise Exception("File creation failed")
        
        # Test file permissions
        if not os.access(filepath, os.R_OK):
            raise Exception("File is not readable")
        
        return JSONResponse({
            "status": "success",
            "message": "Download test successful",
            "filename": test_filename,
            "download_url": f"/download-files/{test_filename}",
            "file_size": os.path.getsize(filepath),
            "downloads_directory": DOWNLOADS_DIR,
            "file_permissions": oct(os.stat(filepath).st_mode)[-3:]
        })
        
    except Exception as e:
        logging.error(f"Download test failed: {e}")
        return JSONResponse({
            "status": "error",
            "error": str(e),
            "downloads_directory": DOWNLOADS_DIR
        }, status_code=500)
# Add this new endpoint for stateless chat completion
@app.post("/completion")
async def chat_completion(
    request: Request,
    prompt: str = Form(...),
    model: str = Form("gpt-4.1"),
    temperature: float = Form(0.7),
    max_tokens: int = Form(1000),
    system_message: Optional[str] = Form(None),
    output_format: Optional[str] = Form(None),  # 'csv', 'excel', or None
    files: Optional[List[UploadFile]] = None
):
    """
    Stateless chat completion endpoint that accepts text, files, and images.
    Enhanced with better error handling and CSV extraction.
    """
    client = create_client()
    
    try:
        # Validate output format
        if output_format and output_format not in ['csv', 'excel']:
            return JSONResponse(
                status_code=400,
                content={
                    "status": "error",
                    "message": "Invalid output_format. Must be 'csv', 'excel', or None"
                }
            )
        
        # Start building the messages array
        messages = []
        
        # Add system message
        if system_message:
            messages.append({
                "role": "system",
                "content": system_message
            })
        else:
            # Default system message
            default_system = "You are a helpful, knowledgeable AI assistant. You can analyze images, documents, and answer questions on any topic."
            
            # If output format is specified, adjust system message
            if output_format in ['csv', 'excel']:
                default_system += (
                    f"\n\nIMPORTANT: The user wants the response in {output_format.upper()} format. "
                    "When presenting tabular data:\n"
                    "1. Provide ONLY the raw CSV data\n"
                    "2. Do NOT include any explanatory text before or after\n"
                    "3. Do NOT use markdown formatting or code blocks\n"
                    "4. Start directly with the header row\n"
                    "5. Use comma as delimiter\n"
                    "6. Quote fields that contain commas"
                )
            
            messages.append({
                "role": "system",
                "content": default_system
            })
        
        # Build user message content
        user_content = []
        
        # Add the text prompt
        user_content.append({
            "type": "text",
            "text": prompt
        })
        
        # Process uploaded files if any
        processed_files = []
        if files:
            for file in files:
                if not file.filename:  # Skip files without names
                    continue
                    
                try:
                    # Read file content
                    file_content = await file.read()
                    file_type = file.content_type or mimetypes.guess_type(file.filename)[0] or "application/octet-stream"
                    
                    logging.info(f"Processing file: {file.filename} ({file_type}, {len(file_content)} bytes)")
                    
                    # Validate file size (e.g., max 10MB)
                    if len(file_content) > 10 * 1024 * 1024:
                        logging.warning(f"File {file.filename} exceeds 10MB limit")
                        continue
                    
                    # Prepare file for inclusion
                    prepared_content = prepare_file_for_completion(
                        file_content, 
                        file.filename, 
                        file_type
                    )
                    
                    # Add to user content
                    user_content.append(prepared_content)
                    processed_files.append(file.filename)
                    
                except Exception as file_error:
                    logging.error(f"Error processing file {file.filename}: {file_error}")
                    # Continue with other files
        
        # Add user message with all content
        messages.append({
            "role": "user",
            "content": user_content
        })
        
        # Make the completion request
        logging.info(f"Making completion request with model: {model}, processed files: {processed_files}")
        
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens
            )
        except Exception as api_error:
            logging.error(f"OpenAI API error: {api_error}")
            return JSONResponse(
                status_code=503,
                content={
                    "status": "error",
                    "error": "AI service temporarily unavailable",
                    "message": "Please try again in a moment"
                }
            )
        
        # Extract the response
        response_content = completion.choices[0].message.content
        
        # Check if we need to generate a file
        download_url = None
        generated_filename = None
        
        if output_format in ['csv', 'excel'] and response_content:
            try:
                # Extract CSV content from response
                csv_content = extract_csv_from_content(response_content)
                
                # Validate CSV content
                if not csv_content or len(csv_content.strip().split('\n')) < 2:
                    logging.warning("Generated content doesn't appear to be valid CSV")
                else:
                    # Generate timestamp for filename
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    
                    if output_format == 'csv':
                        # Save as CSV
                        filename = f"generated_data_{timestamp}.csv"
                        file_bytes = csv_content.encode('utf-8')
                    else:
                        # Convert to Excel
                        try:
                            import pandas as pd
                            from io import StringIO, BytesIO
                            
                            # Parse CSV with pandas
                            df = pd.read_csv(StringIO(csv_content))
                            
                            # Validate dataframe
                            if df.empty:
                                raise ValueError("Parsed dataframe is empty")
                            
                            # Write to Excel
                            buffer = BytesIO()
                            with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                                df.to_excel(writer, index=False, sheet_name='Data')
                            
                            buffer.seek(0)
                            filename = f"generated_data_{timestamp}.xlsx"
                            file_bytes = buffer.getvalue()
                            
                        except Exception as excel_error:
                            logging.error(f"Error converting to Excel: {excel_error}")
                            # Fallback to CSV
                            filename = f"generated_data_{timestamp}.csv"
                            file_bytes = csv_content.encode('utf-8')
                            output_format = 'csv'  # Update format for response
                    
                    # Save file to downloads directory
                    filepath = os.path.join(DOWNLOADS_DIR, filename)
                    with open(filepath, 'wb') as f:
                        f.write(file_bytes)
                    
                    generated_filename = filename
                    
                    # Clean up old downloads
                    cleanup_old_downloads()
                    
                    # Generate download URL
                    download_url = construct_download_url(request, filename)
                    
                    logging.info(f"Generated {output_format} file: {filename}")
                    
            except Exception as file_gen_error:
                logging.error(f"Error generating {output_format} file: {file_gen_error}")
                # Continue without file generation
        
        # Return the response
        return JSONResponse({
            "status": "success",
            "response": response_content,
            "model": model,
            "usage": {
                "prompt_tokens": completion.usage.prompt_tokens,
                "completion_tokens": completion.usage.completion_tokens,
                "total_tokens": completion.usage.total_tokens
            },
            "download_url": download_url,
            "output_format": output_format,
            "filename": generated_filename,
            "processed_files": processed_files
        })
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Unexpected error in chat completion: {str(e)}\n{traceback.format_exc()}")
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "error": "Internal server error",
                "message": "An unexpected error occurred"
            }
        )
# Add this endpoint for review extraction

@app.post("/extract-reviews")
async def extract_reviews(
    request: Request,
    file: UploadFile = Form(...),
    columns: Optional[str] = Form("user,review,rating,date,source"),
    model: str = Form("gpt-4.1"),
    temperature: float = Form(0.1),
    output_format: str = Form("csv"),
    max_text_length: int = Form(50000)  # Increased from 10000
):
    """
    Extract reviews from uploaded files into structured tabular format.
    Enhanced with better validation and error handling.
    """
    client = create_client()
    
    try:
        # Validate output format
        if output_format not in ['csv', 'excel']:
            return JSONResponse(
                status_code=400,
                content={
                    "status": "error",
                    "message": "Invalid output_format. Must be 'csv' or 'excel'"
                }
            )
        
        # Validate file
        if not file.filename:
            return JSONResponse(
                status_code=400,
                content={
                    "status": "error",
                    "message": "No filename provided"
                }
            )
        
        # Read and extract text from the uploaded file
        try:
            file_content = await file.read()
            
            # Validate file size (max 50MB for text extraction)
            if len(file_content) > 50 * 1024 * 1024:
                return JSONResponse(
                    status_code=400,
                    content={
                        "status": "error",
                        "message": "File too large. Maximum size is 50MB"
                    }
                )
            
            extracted_text = extract_text_from_file(file_content, file.filename)
            
        except Exception as extract_error:
            logging.error(f"Error reading file: {extract_error}")
            return JSONResponse(
                status_code=400,
                content={
                    "status": "error",
                    "message": f"Could not read file: {str(extract_error)}"
                }
            )
        
        if extracted_text.startswith("[Error") or extracted_text.startswith("[Unable"):
            return JSONResponse(
                status_code=400,
                content={
                    "status": "error",
                    "message": f"Could not extract text from file: {extracted_text}"
                }
            )
        
        # Parse and validate columns
        column_list = [col.strip() for col in columns.split(',') if col.strip()]
        if not column_list:
            column_list = ["user", "review", "rating", "date", "source"]
        
        # Limit text length for API
        if len(extracted_text) > max_text_length:
            logging.warning(f"Text truncated from {len(extracted_text)} to {max_text_length} characters")
            extracted_text = extracted_text[:max_text_length]
        
        # Build the extraction prompt with better instructions
        system_message = f"""You are a data extraction specialist. Extract review data from the provided text into a structured CSV format.

CRITICAL INSTRUCTIONS:
1. Extract data into EXACTLY these columns: {','.join(column_list)}
2. Output ONLY the CSV data - no explanations, no markdown, no code blocks
3. Start with the header row, followed by data rows
4. Use proper CSV formatting:
   - Use comma as delimiter
   - Quote fields that contain commas, quotes, or newlines
   - Escape quotes within quoted fields by doubling them
5. For missing data:
   - Use empty string (two commas) for missing fields
   - Do not use "N/A", "null", or other placeholders
6. Extract ALL reviews found in the text
7. Preserve the original content as much as possible

Example output format:
{','.join(column_list)}
"John Doe","Great product! Really helped me.","5","2024-01-15","Amazon"
"Jane Smith","Not what I expected, but ""okay"" overall","2","2024-01-10","Website"
"Anonymous","Amazing service","5","",""
"""

        prompt = f"""Extract all reviews from the following text into CSV format with columns: {','.join(column_list)}

Text to analyze:
{extracted_text}

Remember: Output ONLY the CSV data, nothing else. Start with the header row."""

        # Make the completion request
        messages = [
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt}
        ]
        
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=4000  # Allow for longer responses with multiple reviews
            )
        except Exception as api_error:
            logging.error(f"OpenAI API error: {api_error}")
            return JSONResponse(
                status_code=503,
                content={
                    "status": "error",
                    "error": "AI service temporarily unavailable",
                    "message": "Please try again in a moment"
                }
            )
        
        # Extract the response
        csv_content = completion.choices[0].message.content
        
        # Clean the response using our improved function
        csv_content = extract_csv_from_content(csv_content)
        
        # Validate CSV structure
        try:
            import csv
            from io import StringIO
            
            # Try to parse the CSV
            csv_reader = csv.reader(StringIO(csv_content))
            rows = list(csv_reader)
            
            if len(rows) < 2:  # Need at least header and one data row
                return JSONResponse(
                    status_code=422,
                    content={
                        "status": "error",
                        "message": "No reviews could be extracted from the file"
                    }
                )
            
            # Validate header matches requested columns
            header = rows[0]
            if len(header) != len(column_list):
                logging.warning(f"Header mismatch: expected {len(column_list)} columns, got {len(header)}")
            
        except Exception as csv_error:
            logging.error(f"CSV validation error: {csv_error}")
            return JSONResponse(
                status_code=422,
                content={
                    "status": "error",
                    "message": "Generated CSV is malformed. Please try again with different parameters."
                }
            )
        
        # Generate file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        try:
            if output_format == 'excel':
                # Convert CSV to Excel
                import pandas as pd
                from io import StringIO, BytesIO
                
                df = pd.read_csv(StringIO(csv_content))
                
                # Clean up the dataframe
                df = df.fillna('')  # Replace NaN with empty strings
                
                buffer = BytesIO()
                with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                    df.to_excel(writer, index=False, sheet_name='Reviews')
                    
                    # Auto-adjust column widths
                    worksheet = writer.sheets['Reviews']
                    for column in df:
                        column_length = max(df[column].astype(str).map(len).max(), len(str(column)))
                        col_idx = df.columns.get_loc(column)
                        worksheet.column_dimensions[chr(65 + col_idx)].width = min(column_length + 2, 50)
                
                buffer.seek(0)
                filename = f"extracted_reviews_{timestamp}.xlsx"
                file_bytes = buffer.getvalue()
                
            else:
                # Save as CSV
                filename = f"extracted_reviews_{timestamp}.csv"
                file_bytes = csv_content.encode('utf-8')
                
        except Exception as file_gen_error:
            logging.error(f"Error generating {output_format} file: {file_gen_error}")
            # Fallback to CSV
            filename = f"extracted_reviews_{timestamp}.csv"
            file_bytes = csv_content.encode('utf-8')
            output_format = 'csv'
        
        # Save file
        filepath = os.path.join(DOWNLOADS_DIR, filename)
        with open(filepath, 'wb') as f:
            f.write(file_bytes)
        
        # Clean up old downloads
        cleanup_old_downloads()
        
        # Generate download URL
        download_url = construct_download_url(request, filename)
        
        # Count extracted reviews
        review_count = len(rows) - 1  # Subtract header row
        
        return JSONResponse({
            "status": "success",
            "message": f"Successfully extracted {review_count} reviews",
            "download_url": download_url,
            "filename": filename,
            "columns": column_list,
            "output_format": output_format,
            "review_count": review_count,
            "source_file": file.filename,
            "source_file_size": len(file_content),
            "usage": {
                "prompt_tokens": completion.usage.prompt_tokens,
                "completion_tokens": completion.usage.completion_tokens,
                "total_tokens": completion.usage.total_tokens
            }
        })
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Unexpected error in review extraction: {str(e)}\n{traceback.format_exc()}")
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "error": "Internal server error",
                "message": "An unexpected error occurred during extraction"
            }
        )
def cleanup_old_downloads():
    """
    Remove old download files, keeping only the most recent MAX_DOWNLOAD_FILES.
    Now handles all file types: .docx, .csv, .xlsx
    """
    try:
        # Get all downloadable files in the downloads directory
        files = []
        for filename in os.listdir(DOWNLOADS_DIR):
            if filename.endswith(('.docx', '.csv', '.xlsx')):
                filepath = os.path.join(DOWNLOADS_DIR, filename)
                # Get file creation time
                file_time = os.path.getctime(filepath)
                files.append((filepath, file_time, filename))
        
        # Sort by creation time (oldest first)
        files.sort(key=lambda x: x[1])
        
        # Remove oldest files if we exceed the limit
        while len(files) > MAX_DOWNLOAD_FILES:
            old_file = files.pop(0)
            try:
                os.remove(old_file[0])
                logging.info(f"Removed old download file: {old_file[2]}")
            except Exception as e:
                logging.error(f"Error removing old file {old_file[0]}: {e}")
                
    except Exception as e:
        logging.error(f"Error during download cleanup: {e}")
def create_docx_from_content(content: str, images: Optional[List[bytes]] = None) -> bytes:
    """
    Convert chat content to DOCX format with proper Markdown table conversion.
    
    Args:
        content: Text content to convert (may include Markdown)
        images: Optional list of image bytes to include
        
    Returns:
        DOCX file as bytes
    """
    from docx import Document
    from docx.shared import Inches
    from io import BytesIO
    import re
    from PIL import Image as PILImage
    
    # Create document
    doc = Document()
    
    # Add a title with timestamp
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    doc.add_heading(f"Chat Response - {timestamp}", level=1)
    
    # Split content into blocks for processing
    blocks = content.split('\n\n')
    
    # Helper function to detect markdown tables
    def is_markdown_table(text):
        lines = text.strip().split('\n')
        if len(lines) < 2:
            return False
            
        # Check for table with | characters
        if all('|' in line for line in lines):
            # Check for separator row (e.g., |---|---|)
            for i in range(1, len(lines)):
                if re.match(r'^[\s]*\|[-:\|\s]+\|[\s]*$', lines[i]):
                    return True
        
        # Check for simple tables (e.g., Header | Header)
        if len(lines) >= 3 and '|' in lines[0] and all('-' in cell for cell in lines[1].split('|')):
            return True
            
        return False
    
    # Helper function to parse a markdown table
    def parse_markdown_table(text):
        rows = []
        lines = text.strip().split('\n')
        
        # Skip separator lines when processing
        for line in lines:
            if re.match(r'^[\s]*\|?[-:\|\s]+-\|?[\s]*$', line):
                continue
                
            # Extract cells from the line
            if '|' in line:
                # Remove leading/trailing | and split by |
                cells = line.strip()
                if cells.startswith('|'):
                    cells = cells[1:]
                if cells.endswith('|'):
                    cells = cells[:-1]
                cells = [cell.strip() for cell in cells.split('|')]
                rows.append(cells)
        
        return rows
    
    # Process each block
    for block in blocks:
        if not block.strip():
            continue
            
        # Check if this block is a markdown table
        if is_markdown_table(block):
            # Parse the table
            table_data = parse_markdown_table(block)
            if table_data and len(table_data) > 0:
                # Create Word table
                num_rows = len(table_data)
                num_cols = max(len(row) for row in table_data)
                table = doc.add_table(rows=num_rows, cols=num_cols)
                table.style = 'Table Grid'
                
                # Fill table with data
                for i, row_data in enumerate(table_data):
                    row = table.rows[i]
                    for j, cell_text in enumerate(row_data):
                        if j < len(row.cells):  # Ensure we don't exceed the columns
                            row.cells[j].text = cell_text
                            
                # Add spacing after table
                doc.add_paragraph()
        else:
            # Process non-table elements
            if block.startswith('# '):
                # Heading 1
                doc.add_heading(block[2:], level=1)
            elif block.startswith('## '):
                # Heading 2
                doc.add_heading(block[3:], level=2)
            elif block.startswith('### '):
                # Heading 3
                doc.add_heading(block[4:], level=3)
            elif block.startswith('- ') or block.startswith('* '):
                # Bullet points
                lines = block.split('\n')
                for line in lines:
                    if line.strip().startswith('- ') or line.strip().startswith('* '):
                        doc.add_paragraph(line.strip()[2:], style='List Bullet')
            elif re.match(r'^\d+\.\s', block):
                # Numbered list
                lines = block.split('\n')
                for line in lines:
                    if line.strip() and re.match(r'^\d+\.\s', line.strip()):
                        # Extract the content after the number and period
                        content_start = line.find('. ') + 2
                        doc.add_paragraph(line.strip()[content_start:], style='List Number')
            else:
                # Regular paragraph
                doc.add_paragraph(block)
    
    # Add images section if there are images
    if images:
        doc.add_heading("Visualizations", level=2)
        
        # Add each image to the document
        for i, img_bytes in enumerate(images):
            try:
                # Create a BytesIO object from the image bytes
                image_stream = BytesIO(img_bytes)
                
                # Try to open with PIL to verify it's a valid image
                pil_image = PILImage.open(image_stream)
                
                # Add a caption for the image
                doc.add_paragraph(f"Visualization {i+1}")
                
                # Reset stream position after PIL read
                image_stream.seek(0)
                
                # Add the image to the document - control width to fit page
                doc.add_picture(image_stream, width=Inches(6))
                
                # Add spacing after each image
                doc.add_paragraph()
            except Exception as img_err:
                # If image processing fails, add a note
                doc.add_paragraph(f"[Image {i+1} could not be included - {str(img_err)}]")
                logging.warning(f"Error adding image to DOCX: {str(img_err)}")
    
    # Save document to BytesIO buffer
    buffer = BytesIO()
    doc.save(buffer)
    buffer.seek(0)
    
    return buffer.getvalue()


# Add this new endpoint after the existing endpoints in app.py

@app.get("/download-chat")
async def download_chat(
    request: Request,
    session: Optional[str] = None,
    assistant: Optional[str] = None
):
    """
    Creates a DOCX file from the latest chat response and returns a download URL.
    
    Args:
        request: FastAPI request object (to construct full URL)
        session: Thread ID
        assistant: Assistant ID (optional, for validation)
        
    Returns:
        JSON response with download URL
    """
    client = create_client()
    
    # Validate session parameter
    if not session:
        raise HTTPException(status_code=400, detail="Session (thread) ID is required")
    
    try:
        # Validate resources if provided
        if session or assistant:
            validation = await validate_resources(client, session, assistant)
            
            if session and not validation["thread_valid"]:
                raise HTTPException(status_code=404, detail=f"Thread {session} not found")
            
            if assistant and not validation["assistant_valid"]:
                logging.warning(f"Assistant {assistant} not found, but continuing with thread messages")
        
        # Get the latest messages from the thread
        messages = client.beta.threads.messages.list(
            thread_id=session,
            order="desc",
            limit=20  # Get recent messages to find the latest assistant response
        )
        
        # Find the latest assistant message
        latest_assistant_message = None
        for msg in messages.data:
            if msg.role == "assistant":
                # Skip system messages and metadata messages
                skip_message = False
                if hasattr(msg, 'metadata') and msg.metadata:
                    msg_type = msg.metadata.get('type', '')
                    if msg_type in ['user_persona_context', 'file_awareness', 'pandas_agent_files']:
                        skip_message = True
                
                if not skip_message:
                    latest_assistant_message = msg
                    break
        
        if not latest_assistant_message:
            raise HTTPException(status_code=404, detail="No assistant response found in this thread")
        
        # Extract content from the message
        content_text = ""
        images = []
        
        for content_part in latest_assistant_message.content:
            if content_part.type == 'text':
                content_text += content_part.text.value
            elif content_part.type == 'image_file':
                # Handle image files if present
                try:
                    # Retrieve the file
                    file_id = content_part.image_file.file_id
                    file_data = client.files.retrieve(file_id)
                    file_content = client.files.content(file_id)
                    images.append(file_content.read())
                except Exception as img_e:
                    logging.warning(f"Could not retrieve image file {file_id}: {img_e}")
        
        # Remove any [PANDAS AGENT RESPONSE] prefix if present
        if content_text.startswith("[PANDAS AGENT RESPONSE]:"):
            content_text = content_text.replace("[PANDAS AGENT RESPONSE]:", "").strip()
        
        # Generate DOCX content
        try:
            docx_bytes = create_docx_from_content(content_text, images if images else None)
        except ImportError as e:
            # If docx library is not available, return error
            logging.error(f"DOCX library not available: {e}")
            raise HTTPException(
                status_code=500, 
                detail="DOCX generation library not available. Please install python-docx."
            )
        
        # Generate unique filename with timestamp and session ID
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Create a short hash of the session ID for the filename
        session_hash = hashlib.md5(session.encode()).hexdigest()[:8]
        filename = f"chat_response_{timestamp}_{session_hash}.docx"
        filepath = os.path.join(DOWNLOADS_DIR, filename)
        
        # Save the DOCX file
        with open(filepath, 'wb') as f:
            f.write(docx_bytes)
        
        logging.info(f"Created download file: {filepath}")
        
        # Clean up old files
        cleanup_old_downloads()
        
        # Construct the download URL
        # Get the base URL from the request
        base_url = str(request.base_url).rstrip('/')
        
        # For Azure App Service, use the proper host
        if 'azurewebsites.net' in str(request.headers.get('host', '')):
            # Use the Azure host
            base_url = f"https://{request.headers['host']}"
        
        download_url = f"{base_url}/download-files/{filename}"
        
        # Return the download URL
        return JSONResponse({
            "status": "success",
            "download_url": download_url,
            "filename": filename,
            "message": "Chat response ready for download",
            "expires_in": "File will be kept for the last 10 downloads"
        })
        
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logging.error(f"Error in /download-chat endpoint: {str(e)}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Failed to generate download: {str(e)}")

@app.get("/download-files/{filename}")
async def serve_download_file(
    filename: str,
    request: Request,
    token: Optional[str] = None  # Optional token for access control
):
    """
    Serve a file for download with proper headers and security.
    """
    try:
        # Security validation
        if not filename:
            raise HTTPException(status_code=400, detail="Filename is required")
        
        # Prevent directory traversal
        if any(char in filename for char in ['..', '/', '\\', '\x00']):
            logging.warning(f"Potential directory traversal attempt: {filename}")
            raise HTTPException(status_code=400, detail="Invalid filename")
        
        # Construct filepath
        filepath = os.path.join(DOWNLOADS_DIR, filename)
        
        # Verify file exists
        if not os.path.exists(filepath):
            logging.warning(f"File not found: {filepath}")
            raise HTTPException(status_code=404, detail="File not found")
        
        # Double-check file is in downloads directory
        real_filepath = os.path.realpath(filepath)
        real_downloads_dir = os.path.realpath(DOWNLOADS_DIR)
        
        if not real_filepath.startswith(real_downloads_dir):
            logging.error(f"Security violation: Attempted to access {real_filepath}")
            raise HTTPException(status_code=403, detail="Access forbidden")
        
        # Get file info
        file_stat = os.stat(filepath)
        file_size = file_stat.st_size
        
        # Log download attempt
        client_ip = request.client.host if request.client else "unknown"
        logging.info(f"Download request for {filename} from {client_ip} (size: {file_size} bytes)")
        
        # Determine MIME type
        mime_type, _ = mimetypes.guess_type(filename)
        if not mime_type:
            if filename.endswith('.docx'):
                mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            elif filename.endswith('.xlsx'):
                mime_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            elif filename.endswith('.csv'):
                mime_type = "text/csv; charset=utf-8"
            else:
                mime_type = "application/octet-stream"
        
        # Read file content (for smaller files, to ensure we can serve it)
        if file_size < 10 * 1024 * 1024:  # 10MB
            try:
                with open(filepath, 'rb') as f:
                    content = f.read()
                
                # Return Response with explicit headers
                return Response(
                    content=content,
                    media_type=mime_type,
                    headers={
                        "Content-Disposition": f'attachment; filename="{filename}"',
                        "Content-Type": mime_type,
                        "Content-Length": str(file_size),
                        "Cache-Control": "no-cache, no-store, must-revalidate",
                        "Pragma": "no-cache",
                        "Expires": "0",
                        "X-Content-Type-Options": "nosniff",
                        "X-Download-Options": "noopen"
                    }
                )
            except Exception as read_error:
                logging.error(f"Error reading file {filename}: {read_error}")
                # Fall back to FileResponse
        
        # For larger files or if reading fails, use FileResponse
        return FileResponse(
            path=filepath,
            filename=filename,
            media_type=mime_type,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
                "X-Content-Type-Options": "nosniff",
                "X-Download-Options": "noopen"
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Unexpected error serving file {filename}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
@app.get("/verify-download/{filename}")
async def verify_download(filename: str):
    """
    Verify if a file is available for download without actually downloading it.
    """
    try:
        # Security validation
        if not filename or any(char in filename for char in ['..', '/', '\\', '\x00']):
            return JSONResponse({
                "available": False,
                "error": "Invalid filename"
            }, status_code=400)
        
        filepath = os.path.join(DOWNLOADS_DIR, filename)
        
        if os.path.exists(filepath) and os.path.isfile(filepath):
            file_stat = os.stat(filepath)
            mime_type, _ = mimetypes.guess_type(filename)
            
            return JSONResponse({
                "available": True,
                "filename": filename,
                "size": file_stat.st_size,
                "modified": datetime.fromtimestamp(file_stat.st_mtime).isoformat(),
                "mime_type": mime_type or "application/octet-stream"
            })
        else:
            return JSONResponse({
                "available": False,
                "error": "File not found"
            }, status_code=404)
            
    except Exception as e:
        logging.error(f"Error verifying download {filename}: {e}")
        return JSONResponse({
            "available": False,
            "error": "Verification failed"
        }, status_code=500)
@app.get("/health-check")
async def comprehensive_health_check():
    """
    Comprehensive health check that tests all critical functionality without creating expensive resources.
    Performs lightweight tests and returns detailed status information.
    """
    start_time = time.time()
    
    health_status = {
        "status": "checking",
        "timestamp": datetime.now().isoformat(),
        "version": "1.0.0",  # Add your app version
        "checks": {
            "azure_openai": {"status": "pending"},
            "file_system": {"status": "pending"},
            "dependencies": {"status": "pending"},
            "endpoints": {"status": "pending"},
            "pandas_agent": {"status": "pending"},
        },
        "endpoints_tested": {},
        "warnings": [],
        "errors": [],
    }
    
    # Helper function to safely execute checks
    async def safe_check(check_name: str, check_func):
        try:
            result = await check_func() if asyncio.iscoroutinefunction(check_func) else check_func()
            health_status["checks"][check_name] = {"status": "healthy", **result}
            return True
        except Exception as e:
            error_msg = f"{check_name}: {str(e)}"
            health_status["checks"][check_name] = {"status": "unhealthy", "error": str(e)}
            health_status["errors"].append(error_msg)
            logging.error(f"Health check failed for {check_name}: {e}")
            return False
    
    # 1. Test Azure OpenAI Connection
    async def check_azure_openai():
        client = create_client()
        
        # Try a minimal API call - list models or assistants with limit=1
        try:
            # Try to list assistants (minimal call)
            assistants = client.beta.assistants.list(limit=1)
            
            # Try a simple completion to verify model access
            test_completion = client.chat.completions.create(
                model="gpt-4.1",
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=5,
                temperature=0
            )
            
            return {
                "endpoint": AZURE_ENDPOINT,
                "api_version": AZURE_API_VERSION,
                "model_accessible": True,
                "assistants_api": True
            }
        except Exception as e:
            # Try basic completion only
            try:
                test_completion = client.chat.completions.create(
                    model="gpt-4.1",
                    messages=[{"role": "user", "content": "Hi"}],
                    max_tokens=5
                )
                return {
                    "endpoint": AZURE_ENDPOINT,
                    "api_version": AZURE_API_VERSION,
                    "model_accessible": True,
                    "assistants_api": False,
                    "warning": "Assistants API not accessible"
                }
            except:
                raise
    
    # 2. Test File System
    def check_file_system():
        results = {
            "temp_dir_writable": False,
            "downloads_dir_exists": False,
            "downloads_dir_writable": False,
            "disk_space_available": 0
        }
        
        # Test /tmp directory
        try:
            test_file = os.path.join("/tmp", f"health_check_{uuid.uuid4().hex}.txt")
            with open(test_file, "w") as f:
                f.write("health check test")
            os.remove(test_file)
            results["temp_dir_writable"] = True
        except Exception as e:
            results["temp_write_error"] = str(e)
        
        # Test downloads directory
        try:
            results["downloads_dir_exists"] = os.path.exists(DOWNLOADS_DIR)
            if results["downloads_dir_exists"]:
                test_file = os.path.join(DOWNLOADS_DIR, f"health_check_{uuid.uuid4().hex}.txt")
                with open(test_file, "w") as f:
                    f.write("test")
                os.remove(test_file)
                results["downloads_dir_writable"] = True
                
                # Check disk space (in MB)
                stat = os.statvfs(DOWNLOADS_DIR)
                results["disk_space_available"] = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
        except Exception as e:
            results["downloads_error"] = str(e)
        
        return results
    
    # 3. Test Dependencies
    def check_dependencies():
        dependencies = {}
        critical_deps = {
            "pandas": None,
            "numpy": None,
            "openai": None,
            "langchain": None,
            "openpyxl": None,
            "PyPDF2": None,
            "python-docx": None,
            "Pillow": None,
            "beautifulsoup4": None
        }
        
        for dep_name in critical_deps:
            try:
                if dep_name == "python-docx":
                    import docx
                    dependencies[dep_name] = {"installed": True, "version": getattr(docx, "__version__", "unknown")}
                elif dep_name == "beautifulsoup4":
                    import bs4
                    dependencies[dep_name] = {"installed": True, "version": bs4.__version__}
                elif dep_name == "Pillow":
                    import PIL
                    dependencies[dep_name] = {"installed": True, "version": PIL.__version__}
                else:
                    module = __import__(dep_name)
                    version = getattr(module, "__version__", "unknown")
                    dependencies[dep_name] = {"installed": True, "version": version}
            except ImportError:
                dependencies[dep_name] = {"installed": False}
                if dep_name in ["pandas", "numpy", "openai", "langchain"]:
                    health_status["warnings"].append(f"Critical dependency {dep_name} not installed")
        
        return {"dependencies": dependencies}
    
    # 4. Test Pandas Agent Manager
    def check_pandas_agent():
        try:
            manager = PandasAgentManager.get_instance()
            
            # Check if dependencies are available
            deps_ok = manager._check_dependencies()
            
            # Test basic functionality without creating files
            test_thread_id = f"health_check_{uuid.uuid4().hex}"
            manager.initialize_thread(test_thread_id)
            
            # Verify initialization
            thread_initialized = (
                test_thread_id in manager.dataframes_cache and
                test_thread_id in manager.file_info_cache
            )
            
            # Clean up
            if test_thread_id in manager.dataframes_cache:
                del manager.dataframes_cache[test_thread_id]
            if test_thread_id in manager.file_info_cache:
                del manager.file_info_cache[test_thread_id]
            
            return {
                "instance_available": True,
                "dependencies_ok": deps_ok,
                "initialization_works": thread_initialized
            }
        except Exception as e:
            return {"error": str(e)}
    
    # 5. Test Endpoints (Lightweight)
    async def check_endpoints():
        endpoint_results = {}
        
        # Test /completion endpoint with minimal request
        try:
            # Create a mock request
            mock_request = type('Request', (), {
                'base_url': 'http://localhost:8080/',
                'headers': {'host': 'localhost:8080'}
            })()
            
            # Test with minimal parameters
            result = await chat_completion(
                request=mock_request,
                prompt="test",
                model="gpt-4.1",
                temperature=0,
                max_tokens=1
            )
            
            endpoint_results["/completion"] = {
                "status": "healthy",
                "response_type": type(result).__name__
            }
        except Exception as e:
            endpoint_results["/completion"] = {
                "status": "unhealthy",
                "error": str(e)[:100]  # Limit error message length
            }
        
        # Test file processing functions
        try:
            # Test text extraction
            test_text = b"Hello, world!"
            extracted = extract_text_from_file(test_text, "test.txt")
            
            endpoint_results["file_extraction"] = {
                "status": "healthy" if extracted == "Hello, world!" else "unhealthy",
                "test_passed": extracted == "Hello, world!"
            }
        except Exception as e:
            endpoint_results["file_extraction"] = {
                "status": "unhealthy",
                "error": str(e)[:100]
            }
        
        # Test CSV extraction function
        try:
            test_csv = "```csv\nname,age\nJohn,30\n```"
            extracted_csv = extract_csv_from_content(test_csv)
            expected = "name,age\nJohn,30"
            
            endpoint_results["csv_extraction"] = {
                "status": "healthy" if extracted_csv.strip() == expected else "unhealthy",
                "test_passed": extracted_csv.strip() == expected
            }
        except Exception as e:
            endpoint_results["csv_extraction"] = {
                "status": "unhealthy",
                "error": str(e)[:100]
            }
        
        # Test download URL construction
        try:
            mock_request = type('Request', (), {
                'base_url': 'http://localhost:8080/',
                'headers': {'host': 'localhost:8080'}
            })()
            
            url = construct_download_url(mock_request, "test.csv")
            
            endpoint_results["download_url_construction"] = {
                "status": "healthy",
                "sample_url": url
            }
        except Exception as e:
            endpoint_results["download_url_construction"] = {
                "status": "unhealthy", 
                "error": str(e)[:100]
            }
        
        return {"endpoints": endpoint_results}
    
    # Execute all checks
    await safe_check("azure_openai", check_azure_openai)
    await safe_check("file_system", check_file_system)
    await safe_check("dependencies", check_dependencies)
    await safe_check("pandas_agent", check_pandas_agent)
    await safe_check("endpoints", check_endpoints)
    
    # Calculate overall health
    total_checks = len(health_status["checks"])
    healthy_checks = sum(1 for check in health_status["checks"].values() 
                        if check.get("status") == "healthy")
    
    health_percentage = (healthy_checks / total_checks) * 100 if total_checks > 0 else 0
    
    # Determine overall status
    if health_percentage == 100:
        overall_status = "healthy"
        status_code = 200
    elif health_percentage >= 80:
        overall_status = "degraded"
        status_code = 200
    elif health_percentage >= 50:
        overall_status = "partial"
        status_code = 503
    else:
        overall_status = "unhealthy" 
        status_code = 503
    
    # Update final status
    health_status["status"] = overall_status
    health_status["health_percentage"] = round(health_percentage, 2)
    health_status["execution_time_ms"] = round((time.time() - start_time) * 1000, 2)
    
    # Add summary
    health_status["summary"] = {
        "total_checks": total_checks,
        "healthy_checks": healthy_checks,
        "warnings_count": len(health_status["warnings"]),
        "errors_count": len(health_status["errors"])
    }
    
    return JSONResponse(
        content=health_status,
        status_code=status_code
    )


# Add a lightweight health check endpoint for quick monitoring
@app.get("/health")
async def basic_health():
    """
    Basic health check endpoint for load balancers and monitoring.
    Returns quickly with minimal checks.
    """
    try:
        # Just verify the app is running and can create a client
        client = create_client()
        
        return JSONResponse({
            "status": "healthy",
            "timestamp": datetime.now().isoformat()
        })
    except Exception as e:
        return JSONResponse(
            content={
                "status": "unhealthy",
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            },
            status_code=503
        )


# Add endpoint-specific test endpoint for debugging
@app.post("/test-endpoint")
async def test_specific_endpoint(
    endpoint: str = Form(...),
    test_data: Optional[str] = Form(None)
):
    """
    Test a specific endpoint with mock data for debugging.
    Only available in non-production environments.
    """
    # Security: Only allow in development/staging
    if os.getenv("ENVIRONMENT", "development") == "production":
        raise HTTPException(status_code=403, detail="Test endpoint not available in production")
    
    test_results = {
        "endpoint": endpoint,
        "timestamp": datetime.now().isoformat(),
        "test_data": test_data,
        "result": None,
        "error": None
    }
    
    try:
        if endpoint == "/completion":
            # Test completion endpoint
            mock_request = type('Request', (), {
                'base_url': 'http://localhost:8080/',
                'headers': {'host': 'localhost:8080'}
            })()
            
            result = await chat_completion(
                request=mock_request,
                prompt=test_data or "Hello, this is a test",
                model="gpt-4.1",
                temperature=0.5,
                max_tokens=50
            )
            
            test_results["result"] = json.loads(result.body.decode())
            
        elif endpoint == "/extract-reviews":
            # Test with mock file
            mock_file = type('UploadFile', (), {
                'filename': 'test_reviews.txt',
                'content_type': 'text/plain',
                'read': lambda: asyncio.coroutine(lambda: test_data.encode() if test_data else b"User: John\nReview: Great product!\nRating: 5\n\nUser: Jane\nReview: Not bad\nRating: 3")()
            })()
            
            mock_request = type('Request', (), {
                'base_url': 'http://localhost:8080/',
                'headers': {'host': 'localhost:8080'}
            })()
            
            result = await extract_reviews(
                request=mock_request,
                file=mock_file,
                columns="user,review,rating",
                model="gpt-4.1",
                temperature=0.1,
                output_format="csv"
            )
            
            test_results["result"] = json.loads(result.body.decode())
            
        else:
            test_results["error"] = f"Unknown endpoint: {endpoint}"
            
    except Exception as e:
        test_results["error"] = str(e)
        test_results["traceback"] = traceback.format_exc()
    
    return JSONResponse(test_results)
from fastapi.responses import HTMLResponse

@app.get("/", response_class=HTMLResponse)
async def root():
    """
    Serve a beautiful landing page with API documentation.
    """
    html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Copilot v2 API - AI-Powered Assistant</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        :root {
            --primary: #6366f1;
            --primary-dark: #4f46e5;
            --secondary: #8b5cf6;
            --accent: #ec4899;
            --bg-dark: #0f172a;
            --bg-card: #1e293b;
            --bg-code: #0f172a;
            --text-primary: #e2e8f0;
            --text-secondary: #94a3b8;
            --border: #334155;
            --success: #10b981;
            --warning: #f59e0b;
            --error: #ef4444;
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background: var(--bg-dark);
            color: var(--text-primary);
            line-height: 1.6;
            overflow-x: hidden;
        }

        .gradient-bg {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            height: 100vh;
            background: radial-gradient(ellipse at top, #1e293b 0%, #0f172a 50%);
            z-index: -1;
        }

        .floating-gradient {
            position: fixed;
            width: 800px;
            height: 800px;
            background: radial-gradient(circle, rgba(99, 102, 241, 0.1) 0%, transparent 70%);
            border-radius: 50%;
            animation: float 20s infinite ease-in-out;
            z-index: -1;
        }

        .gradient-1 {
            top: -400px;
            left: -400px;
        }

        .gradient-2 {
            bottom: -400px;
            right: -400px;
            animation-delay: 10s;
            background: radial-gradient(circle, rgba(139, 92, 246, 0.1) 0%, transparent 70%);
        }

        @keyframes float {
            0%, 100% { transform: translate(0, 0) scale(1); }
            50% { transform: translate(30px, -30px) scale(1.1); }
        }

        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 2rem;
            position: relative;
        }

        /* Header */
        .header {
            text-align: center;
            padding: 4rem 0;
            position: relative;
        }

        .logo {
            display: inline-flex;
            align-items: center;
            gap: 1rem;
            margin-bottom: 2rem;
        }

        .logo-icon {
            width: 60px;
            height: 60px;
            background: linear-gradient(135deg, var(--primary), var(--secondary));
            border-radius: 16px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 2rem;
            box-shadow: 0 10px 30px rgba(99, 102, 241, 0.3);
        }

        h1 {
            font-size: 3.5rem;
            font-weight: 700;
            background: linear-gradient(135deg, var(--primary), var(--secondary), var(--accent));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 1rem;
        }

        .subtitle {
            font-size: 1.25rem;
            color: var(--text-secondary);
            margin-bottom: 3rem;
        }

        /* Status Badge */
        .status-badge {
            display: inline-flex;
            align-items: center;
            gap: 0.5rem;
            background: var(--bg-card);
            padding: 0.75rem 1.5rem;
            border-radius: 100px;
            border: 1px solid var(--border);
            margin-bottom: 3rem;
        }

        .status-indicator {
            width: 10px;
            height: 10px;
            background: var(--success);
            border-radius: 50%;
            animation: pulse 2s infinite;
        }

        @keyframes pulse {
            0% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
            70% { box-shadow: 0 0 0 10px rgba(16, 185, 129, 0); }
            100% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
        }

        /* Quick Start */
        .quick-start {
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 2rem;
            margin-bottom: 3rem;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.2);
        }

        .quick-start h2 {
            font-size: 1.5rem;
            margin-bottom: 1rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        .command-box {
            background: var(--bg-code);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 1rem;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.9rem;
            position: relative;
            margin-top: 1rem;
            overflow-x: auto;
        }

        .copy-btn {
            position: absolute;
            top: 0.75rem;
            right: 0.75rem;
            background: var(--primary);
            border: none;
            color: white;
            padding: 0.5rem 1rem;
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.875rem;
            transition: all 0.2s;
        }

        .copy-btn:hover {
            background: var(--primary-dark);
            transform: translateY(-1px);
        }

        /* Features Grid */
        .features {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 1.5rem;
            margin-bottom: 3rem;
        }

        .feature-card {
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 1.5rem;
            transition: all 0.3s;
        }

        .feature-card:hover {
            transform: translateY(-4px);
            border-color: var(--primary);
            box-shadow: 0 10px 30px rgba(99, 102, 241, 0.2);
        }

        .feature-icon {
            width: 48px;
            height: 48px;
            background: linear-gradient(135deg, var(--primary), var(--secondary));
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.5rem;
            margin-bottom: 1rem;
        }

        .feature-card h3 {
            font-size: 1.25rem;
            margin-bottom: 0.5rem;
        }

        .feature-card p {
            color: var(--text-secondary);
            font-size: 0.95rem;
        }

        /* Endpoints Section */
        .endpoints {
            margin-bottom: 3rem;
        }

        .section-header {
            display: flex;
            align-items: center;
            gap: 1rem;
            margin-bottom: 2rem;
        }

        .section-header h2 {
            font-size: 2rem;
            font-weight: 600;
        }

        .endpoint-group {
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 16px;
            margin-bottom: 2rem;
            overflow: hidden;
        }

        .endpoint-group-header {
            background: linear-gradient(135deg, rgba(99, 102, 241, 0.1), rgba(139, 92, 246, 0.1));
            padding: 1.5rem;
            border-bottom: 1px solid var(--border);
        }

        .endpoint-group-header h3 {
            font-size: 1.5rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        .endpoint-item {
            padding: 1.5rem;
            border-bottom: 1px solid var(--border);
            transition: background 0.2s;
        }

        .endpoint-item:last-child {
            border-bottom: none;
        }

        .endpoint-item:hover {
            background: rgba(99, 102, 241, 0.05);
        }

        .endpoint-header {
            display: flex;
            align-items: center;
            gap: 1rem;
            margin-bottom: 0.75rem;
        }

        .method-badge {
            padding: 0.25rem 0.75rem;
            border-radius: 6px;
            font-size: 0.875rem;
            font-weight: 600;
            font-family: 'JetBrains Mono', monospace;
        }

        .method-get {
            background: rgba(16, 185, 129, 0.2);
            color: var(--success);
        }

        .method-post {
            background: rgba(99, 102, 241, 0.2);
            color: var(--primary);
        }

        .endpoint-path {
            font-family: 'JetBrains Mono', monospace;
            font-size: 1.1rem;
            font-weight: 500;
        }

        .endpoint-description {
            color: var(--text-secondary);
            margin-bottom: 1rem;
        }

        .curl-command {
            background: var(--bg-code);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 1rem;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.85rem;
            position: relative;
            overflow-x: auto;
            white-space: pre-wrap;
            word-break: break-all;
        }

        /* Stats Section */
        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1.5rem;
            margin-bottom: 3rem;
        }

        .stat-card {
            background: linear-gradient(135deg, rgba(99, 102, 241, 0.1), rgba(139, 92, 246, 0.1));
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 1.5rem;
            text-align: center;
        }

        .stat-number {
            font-size: 2.5rem;
            font-weight: 700;
            background: linear-gradient(135deg, var(--primary), var(--secondary));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .stat-label {
            color: var(--text-secondary);
            font-size: 0.95rem;
        }

        /* Footer */
        .footer {
            text-align: center;
            padding: 3rem 0;
            border-top: 1px solid var(--border);
            margin-top: 5rem;
        }

        .footer-content {
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 1rem;
        }

        .created-by {
            font-size: 1.1rem;
            color: var(--text-secondary);
        }

        .created-by a {
            color: var(--primary);
            text-decoration: none;
            font-weight: 600;
            transition: color 0.2s;
        }

        .created-by a:hover {
            color: var(--secondary);
        }

        .tech-stack {
            display: flex;
            gap: 1rem;
            margin-top: 1rem;
        }

        .tech-badge {
            padding: 0.5rem 1rem;
            background: rgba(99, 102, 241, 0.1);
            border: 1px solid var(--border);
            border-radius: 6px;
            font-size: 0.875rem;
        }

        /* Responsive */
        @media (max-width: 768px) {
            h1 {
                font-size: 2.5rem;
            }
            
            .container {
                padding: 1rem;
            }
            
            .features {
                grid-template-columns: 1fr;
            }
            
            .stats {
                grid-template-columns: repeat(2, 1fr);
            }
        }
    </style>
</head>
<body>
    <div class="gradient-bg"></div>
    <div class="floating-gradient gradient-1"></div>
    <div class="floating-gradient gradient-2"></div>

    <div class="container">
        <!-- Header -->
        <header class="header">
            <div class="logo">
                <div class="logo-icon">
                    <i class="fas fa-robot"></i>
                </div>
            </div>
            <h1>Copilot v2 API</h1>
            <p class="subtitle">AI-Powered Assistant with Advanced File Processing</p>
            
            <div class="status-badge">
                <div class="status-indicator"></div>
                <span>API Operational</span>
            </div>
        </header>

        <!-- Quick Start -->
        <section class="quick-start">
            <h2><i class="fas fa-rocket"></i> Quick Start</h2>
            <p>Get started with a simple health check:</p>
            <div class="command-box">
                <code>curl https://copilotv2.azurewebsites.net/health</code>
                <button class="copy-btn" onclick="copyToClipboard('curl https://copilotv2.azurewebsites.net/health')">
                    <i class="fas fa-copy"></i> Copy
                </button>
            </div>
        </section>

        <!-- Features -->
        <section class="features">
            <div class="feature-card">
                <div class="feature-icon">
                    <i class="fas fa-comments"></i>
                </div>
                <h3>Chat Completions</h3>
                <p>Stateless AI completions with support for text, images, and documents</p>
            </div>
            
            <div class="feature-card">
                <div class="feature-icon">
                    <i class="fas fa-file-csv"></i>
                </div>
                <h3>Data Generation</h3>
                <p>Generate CSV and Excel files from natural language prompts</p>
            </div>
            
            <div class="feature-card">
                <div class="feature-icon">
                    <i class="fas fa-star"></i>
                </div>
                <h3>Review Extraction</h3>
                <p>Extract structured review data from unstructured text files</p>
            </div>
            
            <div class="feature-card">
                <div class="feature-icon">
                    <i class="fas fa-download"></i>
                </div>
                <h3>File Downloads</h3>
                <p>Secure file generation and download with multiple format support</p>
            </div>
            
            <div class="feature-card">
                <div class="feature-icon">
                    <i class="fas fa-brain"></i>
                </div>
                <h3>Pandas Agent</h3>
                <p>Advanced data analysis with pandas integration for CSV/Excel files</p>
            </div>
            
            <div class="feature-card">
                <div class="feature-icon">
                    <i class="fas fa-history"></i>
                </div>
                <h3>Conversation Memory</h3>
                <p>Stateful conversations with thread management and context preservation</p>
            </div>
        </section>

        <!-- Stats -->
        <section class="stats">
            <div class="stat-card">
                <div class="stat-number">15+</div>
                <div class="stat-label">API Endpoints</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">GPT-4</div>
                <div class="stat-label">Powered By</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">100%</div>
                <div class="stat-label">Azure Hosted</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">24/7</div>
                <div class="stat-label">Availability</div>
            </div>
        </section>

        <!-- Endpoints -->
        <section class="endpoints">
            <div class="section-header">
                <h2>API Endpoints</h2>
            </div>

            <!-- Health & Testing -->
            <div class="endpoint-group">
                <div class="endpoint-group-header">
                    <h3><i class="fas fa-heartbeat"></i> Health & Testing</h3>
                </div>
                
                <div class="endpoint-item">
                    <div class="endpoint-header">
                        <span class="method-badge method-get">GET</span>
                        <span class="endpoint-path">/health</span>
                    </div>
                    <p class="endpoint-description">Basic health check for monitoring</p>
                    <div class="curl-command">curl https://copilotv2.azurewebsites.net/health</div>
                </div>
                
                <div class="endpoint-item">
                    <div class="endpoint-header">
                        <span class="method-badge method-get">GET</span>
                        <span class="endpoint-path">/health-check</span>
                    </div>
                    <p class="endpoint-description">Comprehensive health check with detailed system status</p>
                    <div class="curl-command">curl https://copilotv2.azurewebsites.net/health-check</div>
                </div>
                
                <div class="endpoint-item">
                    <div class="endpoint-header">
                        <span class="method-badge method-get">GET</span>
                        <span class="endpoint-path">/test-download</span>
                    </div>
                    <p class="endpoint-description">Test file download functionality</p>
                    <div class="curl-command">curl https://copilotv2.azurewebsites.net/test-download</div>
                </div>
            </div>

            <!-- AI Completions -->
            <div class="endpoint-group">
                <div class="endpoint-group-header">
                    <h3><i class="fas fa-magic"></i> AI Completions</h3>
                </div>
                
                <div class="endpoint-item">
                    <div class="endpoint-header">
                        <span class="method-badge method-post">POST</span>
                        <span class="endpoint-path">/completion</span>
                    </div>
                    <p class="endpoint-description">Stateless chat completion with file support and optional CSV/Excel output</p>
                    <div class="curl-command">curl -X POST https://copilotv2.azurewebsites.net/completion \\
  -F "prompt=Create a sales report" \\
  -F "output_format=csv"</div>
                </div>
                
                <div class="endpoint-item">
                    <div class="endpoint-header">
                        <span class="method-badge method-post">POST</span>
                        <span class="endpoint-path">/extract-reviews</span>
                    </div>
                    <p class="endpoint-description">Extract structured review data from uploaded documents</p>
                    <div class="curl-command">curl -X POST https://copilotv2.azurewebsites.net/extract-reviews \\
  -F "file=@reviews.txt" \\
  -F "output_format=excel"</div>
                </div>
            </div>

            <!-- File Operations -->
            <div class="endpoint-group">
                <div class="endpoint-group-header">
                    <h3><i class="fas fa-file-alt"></i> File Operations</h3>
                </div>
                
                <div class="endpoint-item">
                    <div class="endpoint-header">
                        <span class="method-badge method-get">GET</span>
                        <span class="endpoint-path">/download-files/{filename}</span>
                    </div>
                    <p class="endpoint-description">Download generated files (CSV, Excel, DOCX)</p>
                    <div class="curl-command">curl -O https://copilotv2.azurewebsites.net/download-files/generated_data.csv</div>
                </div>
                
                <div class="endpoint-item">
                    <div class="endpoint-header">
                        <span class="method-badge method-get">GET</span>
                        <span class="endpoint-path">/verify-download/{filename}</span>
                    </div>
                    <p class="endpoint-description">Verify file availability before download</p>
                    <div class="curl-command">curl https://copilotv2.azurewebsites.net/verify-download/generated_data.csv</div>
                </div>
                
                <div class="endpoint-item">
                    <div class="endpoint-header">
                        <span class="method-badge method-get">GET</span>
                        <span class="endpoint-path">/download-chat</span>
                    </div>
                    <p class="endpoint-description">Export chat conversation as DOCX document</p>
                    <div class="curl-command">curl "https://copilotv2.azurewebsites.net/download-chat?session=thread_abc123"</div>
                </div>
            </div>

            <!-- Conversation Management -->
            <div class="endpoint-group">
                <div class="endpoint-group-header">
                    <h3><i class="fas fa-comments"></i> Conversation Management</h3>
                </div>
                
                <div class="endpoint-item">
                    <div class="endpoint-header">
                        <span class="method-badge method-post">POST</span>
                        <span class="endpoint-path">/initiate-chat</span>
                    </div>
                    <p class="endpoint-description">Create new assistant, thread, and vector store</p>
                    <div class="curl-command">curl -X POST https://copilotv2.azurewebsites.net/initiate-chat \\
  -F "context=Help me analyze data"</div>
                </div>
                
                <div class="endpoint-item">
                    <div class="endpoint-header">
                        <span class="method-badge method-get">GET</span>
                        <span class="endpoint-path">/conversation</span>
                    </div>
                    <p class="endpoint-description">Streaming chat responses with assistant</p>
                    <div class="curl-command">curl -N "https://copilotv2.azurewebsites.net/conversation?session=thread_abc&prompt=Hello&assistant=asst_xyz"</div>
                </div>
                
                <div class="endpoint-item">
                    <div class="endpoint-header">
                        <span class="method-badge method-get">GET</span>
                        <span class="endpoint-path">/chat</span>
                    </div>
                    <p class="endpoint-description">Non-streaming chat responses (full JSON response)</p>
                    <div class="curl-command">curl "https://copilotv2.azurewebsites.net/chat?session=thread_abc&prompt=Hello&assistant=asst_xyz"</div>
                </div>
                
                <div class="endpoint-item">
                    <div class="endpoint-header">
                        <span class="method-badge method-post">POST</span>
                        <span class="endpoint-path">/upload-file</span>
                    </div>
                    <p class="endpoint-description">Upload files to assistant (CSV, Excel, PDF, images)</p>
                    <div class="curl-command">curl -X POST https://copilotv2.azurewebsites.net/upload-file \\
  -F "file=@data.csv" \\
  -F "assistant=asst_xyz"</div>
                </div>
            </div>
        </section>

        <!-- Footer -->
        <footer class="footer">
            <div class="footer-content">
                <p class="created-by">
                    Created with <i class="fas fa-heart" style="color: var(--accent);"></i> by 
                    <a href="#" target="_blank">Abhik</a>
                </p>
                <div class="tech-stack">
                    <span class="tech-badge">FastAPI</span>
                    <span class="tech-badge">Azure OpenAI</span>
                    <span class="tech-badge">GPT-4</span>
                    <span class="tech-badge">Python</span>
                </div>
            </div>
        </footer>
    </div>

    <script>
        function copyToClipboard(text) {
            navigator.clipboard.writeText(text).then(() => {
                // Find the button that was clicked
                event.target.innerHTML = '<i class="fas fa-check"></i> Copied!';
                setTimeout(() => {
                    event.target.innerHTML = '<i class="fas fa-copy"></i> Copy';
                }, 2000);
            });
        }

        // Add copy functionality to all curl commands
        document.querySelectorAll('.curl-command').forEach(elem => {
            elem.style.cursor = 'pointer';
            elem.title = 'Click to copy';
            elem.onclick = function() {
                const text = this.textContent;
                navigator.clipboard.writeText(text).then(() => {
                    const originalBg = this.style.background;
                    this.style.background = 'rgba(16, 185, 129, 0.1)';
                    this.style.transition = 'background 0.3s';
                    setTimeout(() => {
                        this.style.background = originalBg;
                    }, 500);
                });
            };
        });

        // Animate stats on scroll
        const observerOptions = {
            threshold: 0.5,
            rootMargin: '0px'
        };

        const observer = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    entry.target.style.animation = 'fadeInUp 0.6s ease-out';
                }
            });
        }, observerOptions);

        document.querySelectorAll('.stat-card, .feature-card').forEach(el => {
            observer.observe(el);
        });

        // Add fade in animation
        const style = document.createElement('style');
        style.textContent = `
            @keyframes fadeInUp {
                from {
                    opacity: 0;
                    transform: translateY(30px);
                }
                to {
                    opacity: 1;
                    transform: translateY(0);
                }
            }
        `;
        document.head.appendChild(style);
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html_content)


# Optional: Add a favicon endpoint
@app.get("/favicon.ico")
async def favicon():
    """Return a simple favicon"""
    return Response(content="", media_type="image/x-icon")
if __name__ == "__main__":
    import uvicorn
    print("Starting FastAPI server on http://0.0.0.0:8080")
    # Consider adding reload=True for development, but remove for production
    uvicorn.run(app, host="0.0.0.0", port=8080)
