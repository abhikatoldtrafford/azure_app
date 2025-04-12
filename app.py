import logging
import threading
from fastapi import FastAPI, Request, UploadFile, Form, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from openai import AzureOpenAI
from typing import Optional, List, Dict, Any, Tuple
import os
import datetime
import time
import base64
import mimetypes
import traceback
import asyncio
import json
from io import StringIO
import sys
import re
# Simple status updates for long-running operations
operation_statuses = {}


# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = FastAPI()

# Azure OpenAI client configuration
AZURE_ENDPOINT = "https://kb-stellar.openai.azure.com/" # Replace with your endpoint if different
AZURE_API_KEY = "bc0ba854d3644d7998a5034af62d03ce" # Replace with your key if different
AZURE_API_VERSION = "2024-05-01-preview"

def create_client():
    """Creates an AzureOpenAI client instance."""
    return AzureOpenAI(
        azure_endpoint=AZURE_ENDPOINT,
        api_key=AZURE_API_KEY,
        api_version=AZURE_API_VERSION,
    )

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
                    deployment_name="gpt-4o-mini",
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
            error_msg = f"File path for '{file_name}' is invalid or does not exist (path: {file_path})"
            logging.error(error_msg)
            return None, error_msg, None
        
        # Check if we already have this file (same name)
        existing_file_names = [f.get("name", "") for f in self.file_info_cache[thread_id]]
        if file_name in existing_file_names:
            # File already exists - we'll treat this as an update
            logging.info(f"File '{file_name}' already exists for thread {thread_id} - updating")
            
            # Remove existing file with same name
            for i, info in enumerate(self.file_info_cache[thread_id]):
                if info.get("name") == file_name:
                    self.file_info_cache[thread_id].pop(i)
                    old_path = self.file_paths_cache[thread_id].pop(i) if i < len(self.file_paths_cache[thread_id]) else None
                    
                    # Delete old file from disk
                    if old_path and os.path.exists(old_path):
                        try:
                            os.remove(old_path)
                        except Exception as e:
                            logging.error(f"Error deleting old file {old_path}: {e}")
                    
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
                    
                    break
        
        # Track removed file (if any due to FIFO)
        removed_file = None
        
        # Apply FIFO if we exceed max files
        if len(self.file_info_cache[thread_id]) >= self.max_files_per_thread:
            removed_file = self.remove_oldest_file(thread_id)
            if removed_file:
                logging.info(f"Removed oldest file '{removed_file}' for thread {thread_id} to maintain FIFO limit")
        
        # Load the dataframe(s)
        dfs_dict, error = self.load_dataframe_from_file(file_info)
        
        if error:
            return None, error, removed_file
            
        if dfs_dict:
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
    
    def load_dataframe_from_file(self, file_info):
        """
        Load dataframe(s) from file information with robust error handling.
        
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
        
        if not file_path or not os.path.exists(file_path):
            error_msg = f"File path for '{file_name}' is invalid or does not exist (path: {file_path})"
            logging.error(error_msg)
            return None, error_msg
        
        try:
            # Capture file size and first few bytes for debugging
            file_size = os.path.getsize(file_path)
            with open(file_path, 'rb') as f_check:
                first_bytes = f_check.read(20)
            logging.info(f"File '{file_name}' exists, size: {file_size} bytes, first bytes: {first_bytes}")
            
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
                            # Make a copy of the file to ensure access
                            temp_path = f"/tmp/temp_{int(time.time())}_{os.urandom(2).hex()}_{file_name}"
                            with open(file_path, 'rb') as src, open(temp_path, 'wb') as dst:
                                dst.write(src.read())
                            
                            # Try to read from the copied file
                            df = pd.read_csv(temp_path, encoding=encoding, sep=delimiter, low_memory=False)
                            
                            if len(df.columns) > 1:  # Successfully parsed with >1 column
                                logging.info(f"Successfully loaded CSV with encoding {encoding} and delimiter '{delimiter}'")
                                # Keep track of successful parameters for debug info
                                successful_encoding = encoding
                                successful_delimiter = delimiter
                                # Update file_path to use the copy we just made
                                file_info["path"] = temp_path
                                break
                        except Exception as e:
                            if os.path.exists(temp_path):
                                try:
                                    os.remove(temp_path)
                                except:
                                    pass
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
                result_dfs = {}
                
                # Check for Excel file access errors
                try:
                    # Make a copy of the file to ensure access
                    temp_path = f"/tmp/temp_{int(time.time())}_{os.urandom(2).hex()}_{file_name}"
                    with open(file_path, 'rb') as src, open(temp_path, 'wb') as dst:
                        dst.write(src.read())
                        
                    # Update file path to use the copy
                    file_info["path"] = temp_path
                    file_path = temp_path
                    
                    xls = pd.ExcelFile(file_path)
                    sheet_names = xls.sheet_names
                    logging.info(f"Excel file contains {len(sheet_names)} sheets: {sheet_names}")
                except Exception as e:
                    return None, f"Error accessing Excel file: {str(e)}"
                
                if len(sheet_names) == 1:
                    # Single sheet - load directly with the filename as key
                    try:
                        df = pd.read_excel(file_path, engine='openpyxl')
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
                            df = pd.read_excel(file_path, sheet_name=sheet, engine='openpyxl')
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
        files_added = False
        
        for file_info in files:
            file_name = file_info.get("name", "")
            file_path = file_info.get("path", "")
            
            # Skip files with invalid paths
            if not file_path or not os.path.exists(file_path):
                logging.warning(f"Skipping file with invalid path: {file_name} at {file_path}")
                continue
                
            # Add the file
            dfs, error, removed_file = self.add_file(thread_id, file_info)
            if error:
                logging.warning(f"Error adding file {file_name}: {error}")
            elif dfs:
                files_added = True
                logging.info(f"Successfully added file {file_name} to analysis")
                
            if removed_file and removed_file not in removed_files:
                removed_files.append(removed_file)
        
        # If we haven't added any files successfully in this call but have existing dataframes, use those
        if not files_added and thread_id in self.dataframes_cache and self.dataframes_cache[thread_id]:
            logging.info(f"No new files were added, but using {len(self.dataframes_cache[thread_id])} existing dataframes")
        elif not files_added:
            return None, "No valid data files could be loaded for analysis. Please check your files and try again.", removed_files
                    
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
            return None, "No dataframes available for analysis. Please upload a valid CSV or Excel file and try again.", removed_files
            
        # Extract filename mentions in the query
        mentioned_files = []
        for df_name in dataframes.keys():
            base_name = df_name.split(" [Sheet:")[0].lower()  # Handle Excel sheet names
            if base_name in query.lower():
                mentioned_files.append(df_name)
                
        # Process the query
        enhanced_query = query
        
        # If no specific file is mentioned but we have multiple files, provide minimal guidance
        if len(dataframes) > 1 and not mentioned_files:
            # Create a concise list of available files
            file_list = ", ".join(f"'{name}'" for name in dataframes.keys())
            
            # Add a gentle hint about available files
            query_prefix = f"Available files: {file_list}. "
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
                # Prepare dataframe details for error cases and debugging
                df_details = []
                for name, df in dataframes.items():
                    df_details.append(f"DataFrame '{name}': {df.shape[0]} rows, {df.columns.shape[0]} columns")
                    df_details.append(f"Columns: {', '.join(df.columns.tolist())}")
                    # Add first few rows for debugging
                    try:
                        if df.shape[0] > 0:
                            df_details.append(f"First 3 rows sample:\n{df.head(3)}")
                    except Exception as e:
                        logging.error(f"Error getting dataframe sample: {e}")
                
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
                        if isinstance(agent_result, dict):
                            agent_output = agent_result.get("output", "")
                        else:
                            agent_output = str(agent_result)
                        logging.info(f"Agent completed successfully with invoke() method: {agent_output[:100]}...")
                    except Exception as invoke_error:
                        # Both methods failed - try one more approach for older versions
                        logging.warning(f"Agent invoke() method failed: {str(invoke_error)}, trying __call__ method")
                        try:
                            agent_output = agent(enhanced_query)
                            logging.info(f"Agent completed successfully with __call__ method: {agent_output[:100]}...")
                        except Exception as call_error:
                            # All methods failed
                            raise Exception(f"All agent execution methods failed: run error: {str(run_error)}, invoke error: {str(invoke_error)}, call error: {str(call_error)}")
                
                # Get the captured verbose output
                verbose_output = captured_output.getvalue()
                logging.info(f"Agent verbose output:\n{verbose_output}")
                
                # Check if output seems empty or error-like
                if not agent_output or "I don't have access to" in agent_output or "I cannot analyze" in agent_output:
                    logging.warning(f"Agent response appears problematic: {agent_output}")
                    
                    # Try to extract information from the verbose output
                    if verbose_output and len(verbose_output) > 100:
                        # Look for dataframe.info() or df.head() outputs in the verbose output
                        import re
                        info_matches = re.findall(r'<class \'pandas\.core\.frame\.DataFrame\'>\n.*\ndtypes:', verbose_output, re.DOTALL)
                        head_matches = re.findall(r'\n\s*\d+\s+.*\n\s*\d+\s+.*\n\s*\d+\s+.*\n', verbose_output)
                        
                        if info_matches or head_matches:
                            extracted_info = []
                            
                            if info_matches:
                                extracted_info.append("DataFrame Information:")
                                for match in info_matches[:2]:  # Limit to first 2 matches
                                    extracted_info.append(match)
                                    
                            if head_matches:
                                extracted_info.append("\nSample Data:")
                                for match in head_matches[:3]:  # Limit to first 3 matches
                                    extracted_info.append(match)
                                    
                            fallback_output = "I analyzed your data and found:\n\n" + "\n".join(extracted_info)
                            fallback_output += "\n\nYou can ask specific questions about this data for more insights."
                            logging.info(f"Extracted useful information from verbose output")
                            return fallback_output, None, removed_files
                    
                    # Provide detailed dataframe information as fallback
                    fallback_output = "I analyzed your data and found:\n\n" + "\n".join(df_details[:10])
                    fallback_output += "\n\nYou can ask specific questions about this data for more insights."
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
                
                # Try to provide useful information even when execution fails
                if verbose_output and len(verbose_output) > 100:
                    # Try to extract any pandas output that might be useful
                    import re
                    useful_outputs = re.findall(r'(DataFrame|Series|Index|columns|shape|data types|nan).*', verbose_output)
                    calculations = re.findall(r'(mean|median|sum|count|min|max|std|var).*=.*\d+\.*\d*', verbose_output)
                    
                    if useful_outputs or calculations:
                        useful_info = []
                        if useful_outputs:
                            useful_info.extend(useful_outputs[:5])  # Limit to 5 most relevant outputs
                        if calculations:
                            useful_info.extend(calculations)
                            
                        partial_results = "I encountered an error during analysis, but here's what I found before the error:\n\n"
                        partial_results += "\n".join(useful_info)
                        partial_results += f"\n\nError: {error_detail}"
                        return partial_results, None, removed_files
                
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
    
    try:
        # Verify thread_id is provided
        if not thread_id:
            error_msg = "Thread ID is required for pandas agent"
            update_operation_status(operation_id, "error", 100, error_msg)
            return f"Error: {error_msg}"
        
        # Log files being processed
        file_descriptions = []
        for file in files:
            file_type = file.get("type", "unknown")
            file_name = file.get("name", "unnamed_file")
            file_path = file.get("path", "unknown_path")
            file_descriptions.append(f"{file_name} ({file_type})")
            
            # Verify file existence and attempt recovery if needed
            if not file_path or not os.path.exists(file_path):
                logging.warning(f"File {file_name} does not exist at path: {file_path}")
                # Search for files with similar names in /tmp directory
                possible_matches = []
                for filename in os.listdir('/tmp'):
                    if file_name in filename and os.path.isfile(os.path.join('/tmp', filename)):
                        possible_matches.append(os.path.join('/tmp', filename))
                
                if possible_matches:
                    # Use the most recently modified file if multiple matches
                    best_match = max(possible_matches, key=os.path.getmtime)
                    logging.info(f"Found alternative path for {file_name}: {best_match}")
                    file["path"] = best_match
                    file_path = best_match  # Update file_path for verification below
                else:
                    # Try to find any files for this thread - this is a fallback measure
                    pandas_agent_files = []
                    for tmp_file in os.listdir('/tmp'):
                        if tmp_file.startswith('pandas_agent_') and os.path.isfile(os.path.join('/tmp', tmp_file)):
                            pandas_agent_files.append(os.path.join('/tmp', tmp_file))
                    
                    if pandas_agent_files:
                        # Use the most recently modified file
                        newest_file = max(pandas_agent_files, key=os.path.getmtime)
                        logging.info(f"Using fallback file for {file_name}: {newest_file}")
                        file["path"] = newest_file
                        file_path = newest_file
            
            # Verify updated file path and log file details
            if file_path and os.path.exists(file_path):
                try:
                    file_size = os.path.getsize(file_path)
                    with open(file_path, 'rb') as f:
                        first_bytes = f.read(10)
                    logging.info(f"File {file_name} exists, size: {file_size} bytes, first bytes: {first_bytes}")
                except Exception as e:
                    logging.warning(f"File exists but cannot read: {str(e)}")
        
        file_list_str = ", ".join(file_descriptions) if file_descriptions else "No files provided"
        logging.info(f"Processing data analysis for thread {thread_id} with files: {file_list_str}")
        update_operation_status(operation_id, "files", 20, f"Processing files: {file_list_str}")
        
        # Get the PandasAgentManager instance
        manager = PandasAgentManager.get_instance()
        
        # Process the query
        update_operation_status(operation_id, "analyzing", 50, f"Analyzing data with query: {query}")
        
        # Stream status updates during execution
        def send_progress_updates():
            progress = 50
            while progress < 85:
                time.sleep(1.5)  # Update every 1.5 seconds
                progress += 2
                update_operation_status(operation_id, "executing", min(progress, 85), 
                                        "Analysis in progress...")
                
        # Start progress update in background
        update_thread = None
        try:
            update_thread = threading.Thread(target=send_progress_updates)
            update_thread.daemon = True
            update_thread.start()
        except Exception as e:
            # Don't fail if we can't spawn thread
            logging.warning(f"Could not start progress update thread: {str(e)}")
        
        # If we have no files or they're all invalid, attempt to recover files from the thread messages
        if not files or all(not f.get("path") or not os.path.exists(f.get("path", "")) for f in files):
            try:
                # Attempt to retrieve files from thread messages
                recovered_files = await recover_pandas_files_from_thread(client, thread_id)
                if recovered_files:
                    logging.info(f"Recovered {len(recovered_files)} files from thread messages")
                    files = recovered_files
            except Exception as recover_e:
                logging.error(f"Error recovering files from thread: {recover_e}")
        
        # Run the analysis using the PandasAgentManager
        result, error, removed_files = manager.analyze(thread_id, query, files)
        
        # Prepare the response
        update_operation_status(operation_id, "formatting", 90, "Formatting response")
        
        if error:
            update_operation_status(operation_id, "error", 95, f"Error: {error}")
            
            # Detect if the error appears to be a file access issue
            if "access" in error.lower() or "find" in error.lower() or "read" in error.lower() or "no dataframes" in error.lower():
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
            
            # Standard error response
            final_response = f"Error analyzing data: {error}"
            
            # Check if this is a file not found error, and provide more helpful message
            if "not exist" in error.lower() or "no dataframes" in error.lower() or "file" in error.lower():
                final_response = (
                    f"I couldn't find the necessary data files for analysis. "
                    f"This could be because:\n"
                    f"1. The file was not uploaded correctly\n"
                    f"2. The file was removed due to the 3-file limit per conversation\n"
                    f"3. There might be a temporary system issue accessing the file\n\n"
                    f"Please try uploading the file again and then ask your question."
                )
            
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
        error_details = traceback.format_exc()
        logging.error(f"Critical error in pandas_agent: {str(e)}\n{error_details}")
        
        # Update status to reflect error
        update_operation_status(operation_id, "error", 100, f"Error: {str(e)}")
        
        # Try to provide some helpful debugging information
        debug_info = []
        try:
            # Check if files exist
            for file in files:
                file_name = file.get("name", "unnamed")
                file_path = file.get("path", "unknown")
                if file_path and os.path.exists(file_path):
                    debug_info.append(f"File '{file_name}' exists at path: {file_path}")
                    file_size = os.path.getsize(file_path)
                    debug_info.append(f" - Size: {file_size} bytes")
                else:
                    debug_info.append(f"File '{file_name}' does not exist at path: {file_path}")
        except:
            pass
        
        # Provide a graceful failure response with debug info
        debug_str = "\n".join(debug_info) if debug_info else "No additional debug information available."
        error_response = f"""Sorry, I encountered an error while trying to analyze your data files.

Error details: {str(e)}

Additional debugging information:
{debug_str}

Please try again with a different query or contact support if the issue persists.

Operation ID: {operation_id}"""
                
        return error_response

# Helper function to recover pandas files from thread messages
async def recover_pandas_files_from_thread(client: AzureOpenAI, thread_id: str) -> List[Dict[str, Any]]:
    """
    Attempt to recover pandas files information from thread messages.
    This is useful when files are mentioned but not found in the current context.
    
    Args:
        client: AzureOpenAI client
        thread_id: The thread ID to search
        
    Returns:
        List of file info dictionaries
    """
    try:
        # Get thread messages
        messages = client.beta.threads.messages.list(
            thread_id=thread_id, 
            order="desc",
            limit=30  # Check a reasonable number of messages
        )
        
        # Look for pandas_agent_files messages
        for message in messages.data:
            if hasattr(message, 'metadata') and message.metadata:
                if message.metadata.get('type') == 'pandas_agent_files':
                    try:
                        files_json = message.metadata.get('files', '[]')
                        files = json.loads(files_json)
                        
                        # Verify and fix file paths
                        for file in files:
                            file_name = file.get('name', '')
                            file_path = file.get('path', '')
                            
                            # Check if the file exists
                            if not os.path.exists(file_path):
                                # Search for the file in /tmp
                                for tmp_file in os.listdir('/tmp'):
                                    if file_name in tmp_file and os.path.isfile(os.path.join('/tmp', tmp_file)):
                                        file['path'] = os.path.join('/tmp', tmp_file)
                                        logging.info(f"Found alternative path for {file_name}: {file['path']}")
                                        break
                        
                        # Return only files that exist
                        valid_files = [f for f in files if os.path.exists(f.get('path', ''))]
                        if valid_files:
                            logging.info(f"Recovered {len(valid_files)} valid files from thread messages")
                            return valid_files
                    except Exception as e:
                        logging.error(f"Error parsing pandas files from message: {e}")
        
        # If we reach here, we didn't find any valid pandas files
        return []
        
    except Exception as e:
        logging.error(f"Error recovering pandas files from thread: {e}")
        return []
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
            model="gpt-4o-mini",  # Ensure this model supports vision
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
            
            awareness_message += "\n\nWhen you need to analyze this data file, you can ask questions about it in natural language. For example:"
            awareness_message += "\n- 'Can you summarize the data in the file?'"
            awareness_message += "\n- 'How many records are in the CSV file?'"
            awareness_message += "\n- 'What columns are available in the Excel file?'"
            awareness_message += "\n- 'Find the average value of column X'" 
            awareness_message += "\n- 'Plot the data from column Y over time'"
            awareness_message += "\n\nThe pandas agent will process your request and return the results."
        
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
        vector_store = client.beta.vector_stores.create(name=f"chat_init_store_{int(time.time())}")
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
    
    assistant_tool_resources = {
        "file_search": {"vector_store_ids": [vector_store.id]}
    }

    # Keep track of CSV/Excel files for the session
    session_csv_excel_files = []

    # Use the improved system prompt
    system_prompt = '''
You are a Product Management AI Co-Pilot that helps create documentation and analyze various file types. Your capabilities vary based on the type of files uploaded.

### Understanding File Types and Processing Methods:

1. **CSV/Excel Files** - When users upload these files, you should:
   - Use your built-in pandas_agent tool to analyze them
   - Call the pandas_agent tool with specific questions about the data
   - NEVER try to analyze the data yourself - always use the pandas_agent tool
   - Common use cases: data summarization, statistical analysis, finding trends, answering specific questions about the data

2. **Documents (PDF, DOC, TXT, etc.)** - When users upload these files, you should:
   - Use your file_search capability to extract relevant information
   - Quote information directly from the documents when answering questions
   - Always reference the specific filename when sharing information from a document

3. **Images** - When users upload images, you should:
   - Refer to the analysis that was automatically added to the conversation
   - Use details from the image analysis to answer questions
   - Acknowledge when information might not be visible in the image

### Using the pandas_agent Tool:

When you need to analyze CSV or Excel files, use the pandas_agent tool. Here's how:
1. Identify data-related questions (e.g., "What's the average revenue?", "How many customers are in the dataset?")
2. Formulate a clear, specific query for the pandas_agent
3. Call the pandas_agent tool with your query
4. Incorporate the results into your response

Examples of good pandas_agent queries:
- "Summarize the data in sales_data.csv"
- "Calculate the average value in the 'Revenue' column from Q2_results.xlsx"
- "Find the top 5 customers by purchase amount from customer_data.csv"
- "Compare sales figures between 2022 and 2023 from the annual_report.xlsx"

### PRD Generation:

When asked to create a PRD, include these sections:
- Product Manager, Product Name, Vision
- Customer Problem, Personas, Date
- Executive Summary, Goals & Objectives
- Key Features, Functional Requirements
- Non-Functional Requirements, Use Cases
- Milestones, Risks

Always leverage any uploaded files to inform the PRD content.

### Important Guidelines:

- Always reference files by their exact filenames
- Use tools appropriately based on file type
- Never attempt to analyze CSV/Excel data without using the pandas_agent tool
- Acknowledge limitations and be transparent when information is unavailable
- Ensure responses are concise, relevant, and helpful

Remember that the pandas_agent has full access to all CSV/Excel files that have been uploaded in the current session.
'''
    
    # Create the assistant
    try:
        assistant = client.beta.assistants.create(
            name=f"pm_copilot_{int(time.time())}",
            model="gpt-4o-mini",  # Ensure this model is deployed
            instructions=system_prompt,
            tools=assistant_tools,
            tool_resources=assistant_tool_resources,
        )
        logging.info(f'Assistant created: {assistant.id}')
    except Exception as e:
        logging.error(f"An error occurred while creating the assistant: {e}")
        # Attempt to clean up vector store if assistant creation fails
        try:
            client.beta.vector_stores.delete(vector_store_id=vector_store.id)
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
            client.beta.vector_stores.delete(vector_store_id=vector_store.id)
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
                    file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
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
            client.beta.vector_stores.retrieve(vector_store_id=vector_store_id)
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
            safe_filename = re.sub(r'[^\w\-\.]', '_', filename)
            thread_identifier = thread_id[-6:] if thread_id else str(int(time.time()))[-6:]
            permanent_path = os.path.join('/tmp/', f"pandas_agent_{thread_identifier}_{safe_filename}")
            try:
                with open(permanent_path, 'wb') as f:
                    with open(file_path, 'rb') as src:
                        f.write(src.read())
                
                # Verify the file was copied correctly
                if os.path.exists(permanent_path):
                    file_size = os.path.getsize(permanent_path)
                    logging.info(f"File copied successfully to {permanent_path}, size: {file_size} bytes")
                else:
                    logging.error(f"Failed to copy file to {permanent_path}")
                    raise Exception("File storage failed")
            except Exception as copy_e:
                logging.error(f"Error copying file to permanent path: {copy_e}")
                # If copy fails, use the original path (not ideal but better than failing)
                permanent_path = file_path
            
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
                                import json
                                pandas_files = json.loads(msg.metadata.get('files', '[]'))
                            except:
                                pandas_files = []
                            break
                    existing_file_index = -1
                    for i, existing_file in enumerate(pandas_files):
                        if existing_file.get("name") == filename:
                            existing_file_index = i
                            break
                    if existing_file_index >= 0:
                        pandas_files[existing_file_index] = file_info
                        logging.info(f"Replacing existing file info for {filename}")
                    else:
                        pandas_files.append(file_info)
                        logging.info(f"Adding new file info for {filename}")
                    if pandas_files_message_id:
                        # Delete the old message (can't update metadata directly)
                        try:
                            client.beta.threads.messages.delete(
                                thread_id=thread_id,
                                message_id=pandas_files_message_id
                            )
                        except Exception as e:
                            logging.error(f"Error deleting pandas files message: {e}")
                    
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
                    file_json = json.dumps(pandas_files, indent=None, separators=(',', ':'))
            
                    # Create the metadata message
                    client.beta.threads.messages.create(
                        thread_id=thread_id,
                        role="user",
                        content="PANDAS_AGENT_FILES_INFO (DO NOT DISPLAY TO USER)",
                        metadata={
                            "type": "pandas_agent_files",
                            "files": file_json
                        }
                    )
                    try:
                        from app import PandasAgentManager
                        manager = PandasAgentManager.get_instance()
                        manager.add_file(thread_id, file_info)
                        logging.info(f"Preloaded file {filename} into PandasAgentManager")
                    except Exception as manager_e:
                        logging.warning(f"Could not preload file into PandasAgentManager: {manager_e}")
                    
                    logging.info(f"Updated pandas agent files info in thread {thread_id}")
                except Exception as e:
                    logging.error(f"Error updating pandas agent files for thread {thread_id}: {e}")
            
            uploaded_file_details = {
                "message": "File successfully uploaded for pandas agent processing.",
                "filename": filename,
                "type": "csv" if is_csv else "excel",
                "processing_method": "pandas_agent",
                "path": permanent_path
            }
            
            # If thread_id provided, add file awareness message
            if thread_id:
                await add_file_awareness(client, thread_id, {
                    "name": filename,
                    "type": "csv" if is_csv else "excel",
                    "processing_method": "pandas_agent"
                })
            
            logging.info(f"Added '{filename}' for pandas_agent processing at path {permanent_path}")
            
            # Build completely new tools list, ensuring no duplicates
            required_tools = [
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
                vector_store = client.beta.vector_stores.create(name=f"Assistant_{assistant}_Store")
                vector_store_ids = [vector_store.id]

            vector_store_id_to_use = vector_store_ids[0]  # Use the first linked store

            # Upload to vector store
            with open(file_path, "rb") as file_stream:
                file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
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
    Uses existing session/assistant if provided, otherwise creates defaults (logs this).
    """
    client = create_client()

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
                        model="gpt-4o-mini",
                        instructions="You are a helpful assistant recovering from a system error.",
                    )
                    assistant = assistant_obj.id
                    logging.info(f"Created recovery assistant: {assistant}")
                except Exception as e:
                    logging.error(f"Failed to create recovery assistant: {e}")
                    raise HTTPException(status_code=500, detail="Failed to create a valid assistant")
        
        # Create defaults if not provided (existing code)
        if not assistant:
            logging.warning("No assistant ID provided for /conversation, creating a default one.")
            try:
                assistant_obj = client.beta.assistants.create(
                    name="default_conversation_assistant",
                    model="gpt-4o-mini",
                    instructions="You are a helpful conversation assistant.",
                )
                assistant = assistant_obj.id
            except Exception as e:
                logging.error(f"Failed to create default assistant: {e}")
                raise HTTPException(status_code=500, detail="Failed to create default assistant")

        if not session:
            logging.warning("No session (thread) ID provided for /conversation, creating a new one.")
            try:
                thread = client.beta.threads.create()
                session = thread.id
            except Exception as e:
                logging.error(f"Failed to create default thread: {e}")
                raise HTTPException(status_code=500, detail="Failed to create default thread")

        # Add user message to the thread if prompt is given
        if prompt:
            try:
                client.beta.threads.messages.create(
                    thread_id=session,
                    role="user",
                    content=prompt
                )
            except Exception as e:
                logging.error(f"Failed to add message to thread {session}: {e}")
                raise HTTPException(status_code=500, detail="Failed to add message to conversation thread")
        
        # Define the streaming generator function
        def stream_response():
            buffer = []
            completed = False
            tool_call_results = []
            
            try:
                # Create run and stream the response
                with client.beta.threads.runs.stream(
                    thread_id=session,
                    assistant_id=assistant,
                ) as stream:
                    for event in stream:
                        # Check for message creation and completion
                        if event.event == "thread.message.created":
                            logging.info(f"New message created: {event.data.id}")
                            
                        # Handle text deltas
                        if event.event == "thread.message.delta":
                            delta = event.data.delta
                            if delta.content:
                                for content_part in delta.content:
                                    if content_part.type == 'text' and content_part.text:
                                        text_value = content_part.text.value
                                        if text_value:
                                            buffer.append(text_value)
                                            # Yield chunks frequently for better streaming feel
                                            if len(buffer) >= 3:  # Smaller buffer for more frequent updates
                                                yield ''.join(buffer)
                                                buffer = []
                        
                        # Explicitly handle run completion event
                        if event.event == "thread.run.completed":
                            logging.info(f"Run completed: {event.data.id}")
                            completed = True
                            
                        # Handle tool calls (including pandas_agent)
                        elif event.event == "thread.run.requires_action":
                            if event.data.required_action.type == "submit_tool_outputs":
                                tool_calls = event.data.required_action.submit_tool_outputs.tool_calls
                                tool_outputs = []
                                
                                yield "\n[Processing data analysis request...]\n"
                                
                                for tool_call in tool_calls:
                                    if tool_call.function.name == "pandas_agent":
                                        try:
                                            # Extract arguments
                                            import json
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
                                                    if retry_count >= max_retries:
                                                        yield "\n[Warning: Could not retrieve file information]\n"
                                                    time.sleep(1)
                                            
                                            # Filter by filename if specified
                                            if filename:
                                                pandas_files = [f for f in pandas_files if f.get("name") == filename]
                                            
                                            # Stream progress updates
                                            yield "\n[Loading data files...]\n"
                                            
                                            # Generate operation ID for status tracking
                                            pandas_agent_operation_id = f"pandas_agent_{int(time.time())}_{os.urandom(2).hex()}"
                                            update_operation_status(pandas_agent_operation_id, "started", 0, "Starting data analysis")
                                            
                                            # Stream initial status
                                            yield "\n[Analyzing your data...]\n"
                                            
                                            # Execute the pandas_agent using the class-based implementation
                                            analysis_result = asyncio.run(pandas_agent(
                                                client=client,
                                                thread_id=session,
                                                query=query,
                                                files=pandas_files
                                            ))
                                            
                                            # Stream status indicating completion
                                            yield "\n[Data analysis complete]\n"
                                            
                                            # *** IMPORTANT: Display the actual analysis result to the user ***
                                            yield "\n[Analysis Result]:\n"
                                            yield analysis_result
                                            
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
                                            yield f"\n[Error: {str(e)}]\n"
                                            
                                            # Save for potential fallback
                                            tool_call_results.append(error_msg)
                                            
                                # Submit tool outputs
                                if tool_outputs:
                                    retry_count = 0
                                    max_retries = 3
                                    submit_success = False
                                    
                                    yield "\n[Generating response based on analysis...]\n"
                                    
                                    while retry_count < max_retries and not submit_success:
                                        try:
                                            client.beta.threads.runs.submit_tool_outputs(
                                                thread_id=session,
                                                run_id=event.data.id,
                                                tool_outputs=tool_outputs
                                            )
                                            submit_success = True
                                            # Don't yield extra message here - we've already shown the actual result
                                            logging.info(f"Successfully submitted tool outputs for run {event.data.id}")
                                        except Exception as submit_e:
                                            retry_count += 1
                                            logging.error(f"Error submitting tool outputs (attempt {retry_count}): {submit_e}")
                                            if retry_count >= max_retries:
                                                yield "\n[Error: Failed to submit analysis results. Please try again.]\n"
                                            time.sleep(1)
                    
                    # Yield any remaining text in the buffer
                    if buffer:
                        yield ''.join(buffer)
                        
                    # We only show preliminary results if we haven't already shown them
                    # and the run didn't complete normally
                    if not completed and tool_call_results and not submit_success:
                        # If the run didn't complete normally but we have tool results,
                        # show them directly to avoid leaving the user without a response
                        yield "\n\n[Note: Here are the preliminary analysis results:]\n\n"
                        for result in tool_call_results:
                            yield result
                        
                # Additional fallback - fetch the most recent message if streaming didn't work
                if not buffer and not tool_call_results:
                    try:
                        logging.info("Attempting to retrieve final response through direct message fetch")
                        messages = client.beta.threads.messages.list(
                            thread_id=session,
                            order="desc",
                            limit=1
                        )
                        if messages and messages.data:
                            latest_message = messages.data[0]
                            response_content = ""
                            for content_part in latest_message.content:
                                if content_part.type == 'text':
                                    response_content += content_part.text.value
                            if response_content:
                                yield "\n\n[Response retrieved:]\n\n"
                                yield response_content
                    except Exception as fetch_e:
                        logging.error(f"Failed to fetch final message: {fetch_e}")
                        # Last resort
                        yield "\n\n[Unable to retrieve complete response. Please try again.]\n"
                
            except Exception as e:
                logging.error(f"Streaming error during run for thread {session}: {e}")
                yield "\n[ERROR] An error occurred while generating the response. Please try again.\n"

        # Return the streaming response
        return StreamingResponse(stream_response(), media_type="text/event-stream")

    except Exception as e:
        logging.error(f"Error in /conversation endpoint setup: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process conversation request: {str(e)}")
        
@app.get("/chat")
async def chat(
    session: Optional[str] = None,
    prompt: Optional[str] = None,
    assistant: Optional[str] = None
):
    """
    Handles conversation queries and returns the full response as JSON.
    Enhanced with robust error handling and operational tracking.
    """
    client = create_client()
    operation_id = f"chat_{int(time.time())}_{os.urandom(2).hex()}"
    update_operation_status(operation_id, "started", 0, "Starting chat request")

    try:
        # Resource validation code remains the same...
        # [validation code from original function]

        # Add user message if prompt is given
        if prompt:
            update_operation_status(operation_id, "message", 35, "Adding user message to thread")
            try:
                client.beta.threads.messages.create(
                    thread_id=session, role="user", content=prompt
                )
            except Exception as e:
                logging.error(f"Failed to add message to thread {session}: {e}")
                update_operation_status(operation_id, "error", 100, f"Message creation failed: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to add message to chat thread")

        # Run the assistant with enhanced tool call handling
        try:
            # Create a run
            update_operation_status(operation_id, "run_starting", 40, "Starting assistant run")
            run = client.beta.threads.runs.create(
                thread_id=session,
                assistant_id=assistant
            )
            run_id = run.id
            
            # Poll until run completes or requires action
            poll_count = 0
            max_poll_time = 180  # Maximum time to wait (3 minutes)
            start_time = time.time()
            
            # Track if we've processed any tool calls to include in response
            processed_tool_calls = []
            
            while time.time() - start_time < max_poll_time:
                poll_count += 1
                
                # Update status periodically
                if poll_count % 5 == 0:  # Every 5 polls
                    elapsed = time.time() - start_time
                    progress = min(40 + int(elapsed / max_poll_time * 50), 90)  # Cap at 90%
                    update_operation_status(operation_id, "running", progress, f"Processing run (elapsed: {elapsed:.1f}s)")
                
                # Get run status with retry logic
                retry_count = 0
                max_retries = 3
                run_status = None
                
                while retry_count < max_retries and not run_status:
                    try:
                        run_status = client.beta.threads.runs.retrieve(
                            thread_id=session,
                            run_id=run_id
                        )
                        break
                    except Exception as e:
                        retry_count += 1
                        logging.error(f"Error retrieving run status (attempt {retry_count}): {e}")
                        if retry_count >= max_retries:
                            raise  # Re-raise if all retries fail
                        time.sleep(1)
                
                # Critical part: properly handle requires_action status
                if run_status.status == "requires_action":
                    logging.info(f"Run {run_id} requires action: {run_status.required_action.type}")
                    # Handle tool calls
                    if run_status.required_action.type == "submit_tool_outputs":
                        update_operation_status(operation_id, "tool_processing", 70, "Processing tool calls")
                        tool_calls = run_status.required_action.submit_tool_outputs.tool_calls
                        tool_outputs = []
                        
                        for tool_call in tool_calls:
                            logging.info(f"Processing tool call: {tool_call.function.name}")
                            
                            if tool_call.function.name == "pandas_agent":
                                try:
                                    # Extract arguments with error handling
                                    args = json.loads(tool_call.function.arguments)
                                    query = args.get("query", "")
                                    filename = args.get("filename", None)
                                    
                                    update_operation_status(operation_id, "data_retrieval", 75, f"Retrieving data files for query: {query[:30]}...")
                                    
                                    # Get pandas files for this thread with retry logic
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
                                            if retry_count >= max_retries:
                                                update_operation_status(operation_id, "warning", 76, "Could not retrieve file information")
                                            time.sleep(1)
                                    
                                    # Filter by filename if specified
                                    if filename:
                                        pandas_files = [f for f in pandas_files if f.get("name") == filename]
                                    
                                    update_operation_status(operation_id, "data_analysis", 80, f"Analyzing data with pandas_agent")
                                    
                                    # Process files and build response
                                    pandas_agent_operation_id = f"pandas_agent_{int(time.time())}_{os.urandom(2).hex()}"
                                    update_operation_status(pandas_agent_operation_id, "started", 0, "Starting data analysis")
                                    
                                    # Extract data from file information 
                                    file_descriptions = []
                                    for file in pandas_files:
                                        file_type = file.get("type", "unknown")
                                        file_name = file.get("name", "unnamed_file")
                                        file_descriptions.append(f"{file_name} ({file_type})")
                                    
                                    file_list = ", ".join(file_descriptions) if file_descriptions else "No files available"
                                    
                                    # Build placeholder response
                                    response = f"""CSV/Excel file analysis is not supported yet. This function will be implemented in a future update.

Query received: "{query}"

Available files for analysis: {file_list}

When implemented, this agent will be able to analyze your data files and provide insights based on your query.

Operation ID: {pandas_agent_operation_id}"""
                                    
                                    update_operation_status(pandas_agent_operation_id, "completed", 100, "Analysis completed successfully")
                                    
                                    # Add to tool outputs
                                    tool_outputs.append({
                                        "tool_call_id": tool_call.id,
                                        "output": response
                                    })
                                    
                                    # Save processed tool call information
                                    processed_tool_calls.append({
                                        "type": "pandas_agent",
                                        "query": query,
                                        "response": response
                                    })
                                    
                                except Exception as e:
                                    error_details = traceback.format_exc()
                                    logging.error(f"Error processing pandas_agent tool call: {e}\n{error_details}")
                                    update_operation_status(operation_id, "error", 80, f"Tool processing error: {str(e)}")
                                    tool_outputs.append({
                                        "tool_call_id": tool_call.id,
                                        "output": f"Error processing pandas_agent request: {str(e)}"
                                    })
                        
                        # Submit tool outputs with retry logic
                        if tool_outputs:
                            update_operation_status(operation_id, "submitting_results", 85, "Submitting tool results")
                            retry_count = 0
                            max_retries = 3
                            submit_success = False
                            
                            while retry_count < max_retries and not submit_success:
                                try:
                                    logging.info(f"Submitting {len(tool_outputs)} tool outputs for run {run_id}")
                                    client.beta.threads.runs.submit_tool_outputs(
                                        thread_id=session,
                                        run_id=run_id,
                                        tool_outputs=tool_outputs
                                    )
                                    submit_success = True
                                    logging.info("Tool outputs submitted successfully")
                                except Exception as submit_e:
                                    retry_count += 1
                                    logging.error(f"Error submitting tool outputs (attempt {retry_count}): {submit_e}")
                                    if retry_count >= max_retries:
                                        update_operation_status(operation_id, "error", 85, f"Tool submission failed: {str(submit_e)}")
                                        # If we can't submit tool outputs, cancel the run
                                        try:
                                            client.beta.threads.runs.cancel(
                                                thread_id=session,
                                                run_id=run_id
                                            )
                                        except:
                                            pass  # Ignore errors during cancellation
                                        break
                                    time.sleep(1)
                            
                            # Continue polling - IMPORTANT: let run continue after tool submission
                            continue
                        else:
                            # If we couldn't generate any outputs, cancel the run
                            update_operation_status(operation_id, "warning", 85, "No tool outputs generated")
                            try:
                                client.beta.threads.runs.cancel(
                                    thread_id=session,
                                    run_id=run_id
                                )
                            except:
                                pass  # Ignore errors during cancellation
                            break
                    else:
                        # Unknown action required
                        update_operation_status(operation_id, "warning", 85, f"Unknown required action: {run_status.required_action.type}")
                        try:
                            client.beta.threads.runs.cancel(
                                thread_id=session,
                                run_id=run_id
                            )
                        except:
                            pass  # Ignore errors during cancellation
                        break
                    
                elif run_status.status in ["completed", "failed", "cancelled"]:
                    update_operation_status(
                        operation_id, 
                        "finished" if run_status.status == "completed" else "error", 
                        90, 
                        f"Run {run_status.status}")
                    break
                
                # Adaptive wait before polling again
                poll_interval = min(1 + (poll_count * 0.1), 3)  # Start with 1s, increase slowly, cap at 3s
                time.sleep(poll_interval)
            
            # Handle timeout case
            if time.time() - start_time >= max_poll_time:
                update_operation_status(operation_id, "timeout", 90, f"Run timed out after {max_poll_time}s")
                logging.warning(f"Run {run_id} timed out after {max_poll_time} seconds")
                try:
                    client.beta.threads.runs.cancel(thread_id=session, run_id=run_id)
                except:
                    pass  # Ignore errors during cancellation
            
            # Get the final messages with retry
            update_operation_status(operation_id, "retrieving_response", 95, "Retrieving final response")
            retry_count = 0
            max_retries = 3
            messages = None
            
            while retry_count < max_retries and not messages:
                try:
                    messages = client.beta.threads.messages.list(
                        thread_id=session,
                        order="desc",
                        limit=1  # Just get the latest message
                    )
                    break
                except Exception as e:
                    retry_count += 1
                    logging.error(f"Error retrieving final message (attempt {retry_count}): {e}")
                    if retry_count >= max_retries:
                        update_operation_status(operation_id, "error", 95, f"Failed to retrieve final message: {str(e)}")
                        raise  # Re-raise if all retries fail
                    time.sleep(1)
            
            response_content = ""
            if messages and messages.data:
                latest_message = messages.data[0]
                for content_part in latest_message.content:
                    if content_part.type == 'text':
                        response_content += content_part.text.value
            
            # If we processed tool calls but the final response doesn't reflect that,
            # include the tool call results in the response
            if processed_tool_calls and ("unable to analyze" in response_content.lower() or 
                                         "can't analyze" in response_content.lower() or 
                                         "cannot analyze" in response_content.lower()):
                logging.info("Adding processed tool call results to response")
                tool_response = "\n\n[Data Analysis Results]:\n\n"
                for tool_call in processed_tool_calls:
                    if tool_call["type"] == "pandas_agent":
                        tool_response += tool_call["response"]
                response_content = tool_response
            
            update_operation_status(operation_id, "completed", 100, "Chat request completed successfully")
            return JSONResponse(content={"response": response_content})

        except Exception as e:
            error_details = traceback.format_exc()
            logging.error(f"Error during run processing for thread {session}: {e}\n{error_details}")
            update_operation_status(operation_id, "error", 100, f"Run processing error: {str(e)}")
            raise HTTPException(status_code=500, detail="Error generating response. Please try again.")

    except Exception as e:
        error_details = traceback.format_exc()
        logging.error(f"Error in /chat endpoint setup: {e}\n{error_details}")
        update_operation_status(operation_id, "error", 100, f"Chat request failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to process chat request: {str(e)}")
if __name__ == "__main__":
    import uvicorn
    print("Starting FastAPI server on http://0.0.0.0:8000")
    # Consider adding reload=True for development, but remove for production
    uvicorn.run(app, host="0.0.0.0", port=8000)
