import logging

from fastapi import FastAPI, Request, UploadFile, Form, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from openai import AzureOpenAI
from typing import Optional, List, Dict, Any
import os
import datetime
import time
import base64
import mimetypes
import traceback
import os
import asyncio
import json
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
    Enhanced pandas_agent that will be compatible with LangChain integration.
    Currently returns a placeholder message, but structured for future implementation.
    """
    operation_id = f"pandas_agent_{int(time.time())}_{os.urandom(2).hex()}"
    update_operation_status(operation_id, "started", 0, "Starting data analysis")
    
    try:
        # Update status
        update_operation_status(operation_id, "processing", 25, "Processing file information")
        
        # Extract data from file information
        dataframes = {}
        file_descriptions = []
        
        for file in files:
            file_type = file.get("type", "unknown")
            file_name = file.get("name", "unnamed_file")
            file_path = file.get("path", None)
            file_descriptions.append(f"{file_name} ({file_type})")
            
            # In future LangChain implementation, we would load the dataframes here
            # For now, we're not loading but making the code structure ready for that
            if file_path and os.path.exists(file_path):
                # This is where we would load dataframes in the future when using LangChain
                # if file_type == "csv":
                #     import pandas as pd
                #     dataframes[file_name] = pd.read_csv(file_path)
                # elif file_type == "excel":
                #     import pandas as pd
                #     dataframes[file_name] = pd.read_excel(file_path)
                pass
        
        file_list = ", ".join(file_descriptions) if file_descriptions else "No files available"
        
        # Update status
        update_operation_status(operation_id, "analyzing", 50, "Analyzing query")
        
        # FUTURE: This is where we would call LangChain pandas_agent
        # This structure is ready for future LangChain implementation
        """
        # Future implementation placeholder:
        if dataframes:
            from langchain.agents import create_pandas_dataframe_agent
            from langchain.llms import AzureOpenAI
            
            # For a single dataframe
            if len(dataframes) == 1:
                df = list(dataframes.values())[0]
                agent = create_pandas_dataframe_agent(
                    AzureOpenAI(
                        deployment_name="gpt-4o-mini", 
                        model_name="gpt-4o-mini"
                    ),
                    df,
                    verbose=True
                )
                result = agent.run(query)
                # Process result for return
            
            # For multiple dataframes
            else:
                # Logic for handling multiple dataframes
                pass
        """
        
        # For now, return placeholder message with specific file references
        response = f"""CSV/Excel file analysis is not supported yet. This function will be implemented in a future update.

        Query received: "{query}"

        Available files for analysis: {file_list}

        When implemented, this agent will be able to analyze your data files and provide insights based on your query.

        Operation ID: {operation_id}"""
        
        # Update status for thread response
        update_operation_status(operation_id, "responding", 75, "Adding response to thread")
        
        # If thread_id is provided, add the response to the thread
        if thread_id:
            try:
                client.beta.threads.messages.create(
                    thread_id=thread_id,
                    role="user",
                    content=f"[PANDAS AGENT RESPONSE]: {response}",
                    metadata={"type": "pandas_agent_response", "operation_id": operation_id}
                )
                logging.info(f"Added pandas_agent response directly to thread {thread_id}")
            except Exception as e:
                logging.error(f"Error adding pandas_agent response to thread: {e}")
                # Continue execution despite error with thread message
        
        # Mark operation as completed
        update_operation_status(operation_id, "completed", 100, "Analysis completed successfully")
        
        logging.info(f"Pandas agent processed query: '{query}' with {len(files)} files")
        return response
    
    except Exception as e:
        error_details = traceback.format_exc()
        logging.error(f"Critical error in pandas_agent: {str(e)}\n{error_details}")
        
        # Update status to reflect error
        update_operation_status(operation_id, "error", 100, f"Error: {str(e)}")
        
        # Provide a graceful failure response
        error_response = f"""Sorry, I encountered an error while trying to analyze your data files.

        Error details: {str(e)}

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
                                import json
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
                    import json
                    client.beta.threads.messages.create(
                        thread_id=thread_id,
                        role="user",
                        content="PANDAS_AGENT_FILES_INFO (DO NOT DISPLAY TO USER)",
                        metadata={
                            "type": "pandas_agent_files",
                            "files": json.dumps(pandas_files)
                        }
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
                                            
                                            # Execute the pandas agent
                                            pandas_agent_operation_id = f"pandas_agent_{int(time.time())}_{os.urandom(2).hex()}"
                                            update_operation_status(pandas_agent_operation_id, "started", 0, "Starting data analysis")
                                            
                                            # Process files and build response
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
                                            
                                            # Save for potential fallback
                                            tool_call_results.append(response)
                                            
                                        except Exception as e:
                                            error_details = traceback.format_exc()
                                            logging.error(f"Error processing pandas_agent tool call: {e}\n{error_details}")
                                            tool_outputs.append({
                                                "tool_call_id": tool_call.id,
                                                "output": f"Error processing pandas_agent request: {str(e)}"
                                            })
                                            yield f"\n[Error processing data request: {str(e)}]\n"
                                
                                # Submit tool outputs
                                if tool_outputs:
                                    retry_count = 0
                                    max_retries = 3
                                    submit_success = False
                                    
                                    yield "\n[Data analysis complete. Generating response...]\n"
                                    
                                    while retry_count < max_retries and not submit_success:
                                        try:
                                            client.beta.threads.runs.submit_tool_outputs(
                                                thread_id=session,
                                                run_id=event.data.id,
                                                tool_outputs=tool_outputs
                                            )
                                            submit_success = True
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
                        
                    # Ensure we have a response
                    if not completed and tool_call_results:
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
        # Validate resources with improved error handling
        if session or assistant:
            update_operation_status(operation_id, "validating", 10, "Validating resources")
            validation = await validate_resources(client, session, assistant)
            
            # Create new thread if invalid
            if session and not validation["thread_valid"]:
                logging.warning(f"Invalid thread ID: {session}, creating a new one")
                update_operation_status(operation_id, "recovery", 15, "Creating recovery thread")
                try:
                    thread = client.beta.threads.create()
                    session = thread.id
                    logging.info(f"Created recovery thread: {session}")
                except Exception as e:
                    logging.error(f"Failed to create recovery thread: {e}")
                    update_operation_status(operation_id, "error", 100, f"Thread recovery failed: {str(e)}")
                    raise HTTPException(status_code=500, detail="Failed to create a valid conversation thread")
            
            # Create new assistant if invalid
            if assistant and not validation["assistant_valid"]:
                logging.warning(f"Invalid assistant ID: {assistant}, creating a new one")
                update_operation_status(operation_id, "recovery", 20, "Creating recovery assistant")
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
                    update_operation_status(operation_id, "error", 100, f"Assistant recovery failed: {str(e)}")
                    raise HTTPException(status_code=500, detail="Failed to create a valid assistant")
        
        # Create defaults if not provided
        if not assistant:
            logging.warning("No assistant ID provided for /chat, creating a default one.")
            update_operation_status(operation_id, "setup", 25, "Creating default assistant")
            try:
                assistant_obj = client.beta.assistants.create(
                    name="default_chat_assistant",
                    model="gpt-4o-mini",
                    instructions="You are a helpful chat assistant.",
                )
                assistant = assistant_obj.id
            except Exception as e:
                logging.error(f"Failed to create default assistant: {e}")
                update_operation_status(operation_id, "error", 100, f"Default assistant creation failed: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to create default assistant")

        if not session:
            logging.warning("No session (thread) ID provided for /chat, creating a new one.")
            update_operation_status(operation_id, "setup", 30, "Creating default thread")
            try:
                thread = client.beta.threads.create()
                session = thread.id
            except Exception as e:
                logging.error(f"Failed to create default thread: {e}")
                update_operation_status(operation_id, "error", 100, f"Default thread creation failed: {str(e)}")
                raise HTTPException(status_code=500, detail="Failed to create default thread")

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
                        time.sleep(1)  # FIXED: Using time.sleep instead of await
                
                if run_status.status == "requires_action":
                    # Handle tool calls
                    if run_status.required_action.type == "submit_tool_outputs":
                        update_operation_status(operation_id, "tool_processing", 70, "Processing tool calls")
                        tool_calls = run_status.required_action.submit_tool_outputs.tool_calls
                        tool_outputs = []
                        
                        for tool_call in tool_calls:
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
                                            time.sleep(1)  # FIXED: Using time.sleep instead of await
                                    
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
                                    client.beta.threads.runs.submit_tool_outputs(
                                        thread_id=session,
                                        run_id=run_id,
                                        tool_outputs=tool_outputs
                                    )
                                    submit_success = True
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
                                    time.sleep(1)  # FIXED: Using time.sleep instead of await
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
                time.sleep(poll_interval)  # FIXED: Using time.sleep instead of await
            
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
                    time.sleep(1)  # FIXED: Using time.sleep instead of await
            
            response_content = ""
            if messages and messages.data:
                latest_message = messages.data[0]
                for content_part in latest_message.content:
                    if content_part.type == 'text':
                        response_content += content_part.text.value
            
            update_operation_status(operation_id, "completed", 100, "Chat request completed successfully")
            # return JSONResponse(content={
            #     "response": response_content,
            #     "operation_id": operation_id,
            #     "thread_id": session,
            #     "assistant_id": assistant
            # })
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
