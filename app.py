import logging
from fastapi import FastAPI, Request, UploadFile, Form, HTTPException, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from openai import AzureOpenAI
from typing import Optional, List, Dict, Any
import os
import datetime
import time
import base64
import mimetypes
import pandas as pd
import io
import json

app = FastAPI()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Azure OpenAI client configuration
AZURE_ENDPOINT = "https://kb-stellar.openai.azure.com/"
AZURE_API_KEY = "bc0ba854d3644d7998a5034af62d03ce"
AZURE_API_VERSION = "2024-05-01-preview"

def create_client():
    return AzureOpenAI(
        azure_endpoint=AZURE_ENDPOINT,
        api_key=AZURE_API_KEY,
        api_version=AZURE_API_VERSION,
    )

# Helper function to check if a file is tabular data (CSV or Excel)
def is_tabular_file(filename):
    """Check if the file is a tabular data file (CSV or Excel)."""
    file_ext = os.path.splitext(filename.lower())[1]
    return file_ext in ['.csv', '.xlsx', '.xls']

# Helper function to check if a file is an image
def is_image_file(filename, content_type=None):
    """Check if the file is an image."""
    image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']
    file_ext = os.path.splitext(filename.lower())[1]
    return file_ext in image_extensions or (content_type and content_type.startswith('image/'))

# Helper function to process tabular data
async def process_tabular_data(file_path, filename, max_rows=1000):
    """Process CSV or Excel files and return summary data."""
    try:
        file_ext = os.path.splitext(filename.lower())[1]
        
        if file_ext == '.csv':
            # Read CSV file
            df = pd.read_csv(file_path)
            if len(df) > max_rows:
                df = df.sample(max_rows, random_state=42)
            return {
                "type": "csv", 
                "filename": filename,
                "shape": df.shape,
                "columns": list(df.columns),
                "dataframe": df
            }
            
        elif file_ext in ['.xlsx', '.xls']:
            # Read Excel file with multiple sheets
            xls = pd.ExcelFile(file_path)
            sheet_names = xls.sheet_names
            
            sheets_data = {}
            for sheet in sheet_names:
                df = pd.read_excel(file_path, sheet_name=sheet)
                if len(df) > max_rows:
                    df = df.sample(max_rows, random_state=42)
                sheets_data[sheet] = {
                    "shape": df.shape,
                    "columns": list(df.columns),
                    "dataframe": df
                }
            
            return {
                "type": "excel",
                "filename": filename,
                "sheets": sheet_names,
                "sheets_data": sheets_data
            }
            
    except Exception as e:
        logging.error(f"Error processing tabular file: {e}")
        return {"error": f"Error processing {filename}: {str(e)}"}

# Accept and ignore additional parameters
async def ignore_additional_params(request: Request):
    form_data = await request.form()
    return {k: v for k, v in form_data.items()}

async def image_analysis(client, image_data: bytes, filename: str, prompt: Optional[str] = None) -> str:
    """Analyzes an image using Azure OpenAI vision capabilities and returns the analysis text."""
    try:
        ext = os.path.splitext(filename)[1].lower()
        b64_img = base64.b64encode(image_data).decode("utf-8")
        # Default to jpeg if extension can't be determined
        mime = f"image/{ext[1:]}" if ext and ext[1:] in ['jpg', 'jpeg', 'png', 'gif', 'webp'] else "image/jpeg"
        data_url = f"data:{mime};base64,{b64_img}"
        
        default_prompt = (
            "Analyze this image and provide a thorough summary including all elements. "
            "If there's any text visible, include all the textual content (perform OCR). Describe:"
        )
        combined_prompt = f"{default_prompt} {prompt}" if prompt else default_prompt
        
        # Create a client for vision API
        vision_client = AzureOpenAI(
            azure_endpoint=AZURE_ENDPOINT,
            api_key=AZURE_API_KEY,
            api_version=AZURE_API_VERSION,
        )
        
        # Use the chat completions API to analyze the image
        response = vision_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user", 
                "content": [
                    {"type": "text", "text": combined_prompt},
                    {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}}
                ]
            }],
            max_tokens=1000
        )
        
        analysis_text = response.choices[0].message.content
        return analysis_text
        
    except Exception as e:
        logging.error(f"Image analysis error: {e}")
        return f"Error analyzing image: {str(e)}"
        
# Helper function to update user persona context
async def update_context(client, thread_id, context):
    """Updates the user persona context in a thread by adding a special message."""
    if not context:
        return
        
    try:
        # Get existing messages to check for previous context
        messages = client.beta.threads.messages.list(
            thread_id=thread_id,
            order="desc",
            limit=20  # Check recent messages
        )
        
        # Look for previous context messages to avoid duplication
        has_previous_context = False
        for msg in messages.data:
            if hasattr(msg, 'metadata') and msg.metadata.get('type') == 'user_persona_context':
                # Delete previous context message to replace it
                try:
                    client.beta.threads.messages.delete(
                        thread_id=thread_id,
                        message_id=msg.id
                    )
                except Exception as e:
                    logging.error(f"Error deleting previous context message: {e}")
                    # Continue even if delete fails
                has_previous_context = True
                break
        
        # Add new context message
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=f"USER PERSONA CONTEXT: {context}",
            metadata={"type": "user_persona_context"}
        )
        
        logging.info(f"Updated user persona context in thread {thread_id}")
    except Exception as e:
        logging.error(f"Error updating context: {e}")
        # Continue the flow even if context update fails

# Function to associate files with an assistant
async def associate_file_with_assistant(client, file_id, assistant_id):
    """Associate a file with an assistant using the correct API method"""
    try:
        # Get current file IDs if any
        assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant_id)
        existing_file_ids = getattr(assistant_obj, "file_ids", []) or []
        
        # Add the new file ID to the list (avoid duplicates)
        if file_id not in existing_file_ids:
            updated_file_ids = existing_file_ids + [file_id]
            
            # Update the assistant with the new file IDs
            client.beta.assistants.update(
                assistant_id=assistant_id,
                file_ids=updated_file_ids
            )
            
            logging.info(f"File {file_id} associated with assistant {assistant_id}")
        else:
            logging.info(f"File {file_id} already associated with assistant {assistant_id}")
        return True
    except Exception as e:
        logging.error(f"Error associating file with assistant: {e}")
        return False

# Function to create a message with file attachment
async def create_message_with_attachment(client, thread_id, content, file_id):
    """Create a message with a file attachment in a thread"""
    try:
        # First attempt with attachments field
        try:
            client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=content,
                attachments=[
                    {
                        "file_id": file_id,
                        "tools": [{"type": "code_interpreter"}]
                    }
                ]
            )
            logging.info(f"Message with attachment created in thread {thread_id}")
            return True
        except Exception as e:
            logging.error(f"Error with attachments approach: {e}")
            
            # Try alternative approach with file_ids
            client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=content,
                file_ids=[file_id]
            )
            logging.info(f"Message with file_ids created in thread {thread_id}")
            return True
    except Exception as e:
        logging.error(f"All approaches to create message with attachment failed: {e}")
        
        # If all attachment approaches fail, just create a regular message
        try:
            client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=f"{content} (Note: File {file_id} is available for analysis.)"
            )
            logging.info(f"Fallback message created in thread {thread_id}")
            return True
        except Exception as e_fallback:
            logging.error(f"Fallback message creation failed: {e_fallback}")
            return False

# Add file reference to thread
async def add_file_reference(client, thread_id, file_info):
    """Add a file reference to the thread so assistant knows what files are available."""
    try:
        if not thread_id or not file_info:
            return
            
        file_type = file_info.get("type", "unknown")
        filename = file_info.get("filename", "unknown")
        
        message = f"FILE REFERENCE: A {file_type} file named '{filename}' has been uploaded and is available for analysis."
        
        if file_type == "tabular":
            details = file_info.get("details", {})
            if details:
                if "sheets" in details and details["sheets"]:
                    message += f" It is an Excel file with sheets: {', '.join(details['sheets'])}."
                elif "column_names" in details and details["column_names"]:
                    message += f" It is a CSV file with columns: {', '.join(details['column_names'])}."
                message += " Use the code_interpreter tool to analyze this file."
        elif file_type == "image":
            message += f" Image analysis/OCR result: {file_info.get('analysis', 'No analysis available')}"
        else:
            message += " Use the file_search tool to find information in this document."
            
        # Add the file reference to the thread
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=message,
            metadata={"type": "file_reference", "filename": filename}
        )
        
        logging.info(f"Added file reference for {filename} to thread {thread_id}")
        return True
    except Exception as e:
        logging.error(f"Error adding file reference: {e}")
        return False

@app.post("/initiate-chat")
async def initiate_chat(request: Request):
    """
    Initiates the assistant and session and optionally uploads a file to its vector store, 
    all in one go.
    """
    client = create_client()

    # Parse the form data
    form = await request.form()
    file = form.get("file", None)
    context = form.get("context", None)  # Get optional context parameter

    # Create a vector store up front
    vector_store = client.beta.vector_stores.create(name="demo")

    # Always include file_search tool and associate with the vector store
    assistant_tools = [{"type": "code_interpreter"}, {"type": "file_search"}]
    assistant_tool_resources = {"file_search": {"vector_store_ids": [vector_store.id]}}
    system_prompt = '''
        You are a highly skilled Product Management AI Assistant and Co-Pilot. Your primary responsibilities include generating comprehensive Product Requirements Documents (PRDs) and providing insightful answers to a wide range of product-related queries. You seamlessly integrate information from uploaded files and your extensive knowledge base to deliver contextually relevant and actionable insights.

        ### **File Handling Guidelines:**

        - **CSV and Excel Files:**
          - When a user refers to any CSV or Excel file by name, ALWAYS use the code_interpreter tool to analyze it.
          - Access these files directly, analyze them, and provide insights based on their contents.
          - Never claim you can't access a CSV or Excel file that has been mentioned in the conversation.
          - Generate appropriate visualizations when useful.

        - **Image Files:**
          - For any uploaded images, use the provided OCR/analysis text that has been extracted.
          - When a user refers to an image file by name, respond as if you can directly see the image.
          - Never claim you can't access an image that has been mentioned in the conversation.
          - Reference the comprehensive analysis already performed on these images.

        - **Other Document Files:**
          - For PDFs, DOCs, TXTs, and other document files, use the file_search tool to extract relevant information.
          - When a user refers to a document by name, search for its contents using file_search.

        - **File References:**
          - Pay careful attention to file references in the conversation.
          - When a user mentions any part of a filename, associate it with the full file that was uploaded.
          - For example, if "sales_data_2023.csv" was uploaded and the user asks about "sales data", recognize this refers to the CSV file.

        ### **Primary Tasks:**

        1. **Generate Product Requirements Documents (PRDs):**
        - **Trigger:** When the user explicitly requests a PRD.
        - **Structure:**
            - **Product Manager:** [Use the user's name if available; otherwise, leave blank]
            - **Product Name:** [Derived from user input or uploaded files]
            - **Product Vision:** [Extracted from user input or uploaded files]
            - **Customer Problem:** [Identified from user input or uploaded files]
            - **Personas:** [Based on user input; generate if not provided]
            - **Date:** [Current date]
        
        - **Sections to Include:**
            - **Executive Summary:** Deliver a concise overview by synthesizing information from the user and your knowledge base.
            - **Goals & Objectives:** Enumerate 2-4 specific, measurable goals and objectives.
            - **Key Features:** Highlight key features that align with the goals and executive summary.
            - **Functional Requirements:** Detail 3-5 functional requirements in clear bullet points.
            - **Non-Functional Requirements:** Outline 3-5 non-functional requirements in bullet points.
            - **Use Case Requirements:** Describe 3-5 use cases in bullet points, illustrating how users will interact with the product.
            - **Milestones:** Define 3-5 key milestones with expected timelines in bullet points.
            - **Risks:** Identify 3-5 potential risks and mitigation strategies in bullet points.

        2. **Answer Generic Product Management Questions:**
        - **Scope:** Respond to a broad range of product management queries, including strategy, market analysis, feature prioritization, user feedback interpretation, and more.
        - **Methodology:**
            - Use the file_search tool to find pertinent information within uploaded files.
            - Leverage your comprehensive knowledge base to provide thorough and insightful answers.
            - If a question falls outside the scope of the provided files and your expertise, default to a general GPT-4 response without referencing the files.
            - Maintain a balance between technical detail and accessibility, ensuring responses are understandable yet informative.

        3. **Data Analysis with Code Interpreter:**
        - When a user asks about any CSV or Excel file, ALWAYS use the code_interpreter tool to analyze the data.
        - Perform appropriate data analysis based on the query, including statistical summaries and visualizations.
        - Present analysis results in a clear, organized manner.
        - Never claim you don't have access to a CSV or Excel file that has been referenced.

        ### **Behavioral Guidelines:**

        - **File Awareness:**
        - Always maintain awareness of all files that have been uploaded.
        - When a user refers to a file, even partially (e.g., "the CSV" or "the image"), connect this to the specific file that was uploaded.
        - NEVER claim you don't have access to a file that has been mentioned earlier in the conversation.

        - **Contextual Awareness:**
        - Always consider the context provided by the uploaded files and previous interactions.
        - Adapt your responses based on the specific needs and preferences of the user.

        - **Proactive Insight Generation:**
        - Go beyond surface-level answers by providing deep insights, trends, and actionable recommendations.
        - Anticipate potential follow-up questions and address them preemptively where appropriate.

        - **Professional Tone:**
        - Maintain a professional, clear, and concise communication style.
        - Ensure all interactions are respectful, objective, and goal-oriented.

        By adhering to these guidelines, you will function as an effective Product Management AI Assistant, delivering high-quality PRDs and insightful answers that closely mimic the expertise of a seasoned product manager.
        '''
    # Create the assistant
    try:
        assistant = client.beta.assistants.create(
            name="demo_new_abhik",
            model="gpt-4o-mini",
            instructions=system_prompt,
            tools=assistant_tools,
            tool_resources=assistant_tool_resources,
        )
    except BaseException as e:
        logging.info(f"An error occurred while creating the assistant: {e}")
        raise HTTPException(status_code=400, detail="An error occurred while creating assistant")

    logging.info(f'Assistant created {assistant.id}')

    # Create a thread
    try:
        thread = client.beta.threads.create()
    except BaseException as e:
        logging.info(f"An error occurred while creating the thread: {e}")
        raise HTTPException(status_code=400, detail="An error occurred while creating the thread")

    logging.info(f"Thread created: {thread.id}")

    # If context is provided, add it as user persona context
    if context:
        try:
            await update_context(client, thread.id, context)
        except BaseException as e:
            logging.info(f"An error occurred while adding context to the thread: {e}")
            # Don't fail the entire request if just adding context fails

    # If a file is provided, upload it now
    if file:
        filename = file.filename
        file_path = os.path.join('/tmp/', filename)
        with open(file_path, 'wb') as f:
            file_content = await file.read()
            f.write(file_content)
            
        # Check if it's a tabular file (CSV/Excel)
        if is_tabular_file(filename):
            # Process the file with the code interpreter directly
            try:
                # Process tabular data to get file info
                tabular_data = await process_tabular_data(file_path, filename)
                
                # Upload file to OpenAI
                with open(file_path, "rb") as file_stream:
                    file_obj = client.files.create(
                        file=file_stream,
                        purpose="assistants"
                    )
                
                # Associate file with assistant
                await associate_file_with_assistant(client, file_obj.id, assistant.id)
                
                # Add file reference to thread
                file_info = {
                    "type": "tabular",
                    "filename": filename,
                    "details": {
                        "rows": tabular_data.get("shape", (0, 0))[0] if "shape" in tabular_data else 0,
                        "columns": len(tabular_data.get("columns", [])) if "columns" in tabular_data else 0,
                        "column_names": tabular_data.get("columns", []),
                        "sheets": tabular_data.get("sheets", []) if "sheets" in tabular_data else []
                    }
                }
                await add_file_reference(client, thread.id, file_info)
                
                # Create message to instruct code interpreter to process the file
                await create_message_with_attachment(
                    client, 
                    thread.id, 
                    f"I've uploaded a file named {filename}. Please analyze this data using the code_interpreter tool. This file is now available for analysis throughout our conversation whenever I refer to {filename}.",
                    file_obj.id
                )
                
                logging.info(f"Tabular file {filename} associated with code interpreter")
            except Exception as e:
                logging.error(f"Error processing tabular file: {e}")
                
        # Check if it's an image file
        elif is_image_file(filename, None):
            try:
                # Analyze the image
                analysis_result = await image_analysis(client, file_content, filename)
                
                # Add file reference with analysis to thread
                file_info = {
                    "type": "image",
                    "filename": filename,
                    "analysis": analysis_result
                }
                await add_file_reference(client, thread.id, file_info)
                
                # Create message about the image
                client.beta.threads.messages.create(
                    thread_id=thread.id,
                    role="user",
                    content=f"I've uploaded an image named {filename}. Here's what it contains: {analysis_result}"
                )
                
                logging.info(f"Image file {filename} analyzed and added to thread")
            except Exception as e:
                logging.error(f"Error processing image file: {e}")
        else:
            # Normal file upload to vector store for file_search
            try:
                with open(file_path, "rb") as file_stream:
                    file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                        vector_store_id=vector_store.id, 
                        files=[file_stream]
                    )
                    
                # Add file reference to thread
                file_info = {
                    "type": "document",
                    "filename": filename
                }
                await add_file_reference(client, thread.id, file_info)
                
                # Create message about the document
                client.beta.threads.messages.create(
                    thread_id=thread.id,
                    role="user",
                    content=f"I've uploaded a document named {filename}. The document has been processed and is now available for reference whenever I refer to this document."
                )
                
                logging.info(f"Document file {filename} uploaded to vector store")
            except Exception as e:
                logging.error(f"Error uploading document to vector store: {e}")

    res = {
        "assistant": assistant.id,
        "session": thread.id,
        "vector_store": vector_store.id
    }

    return JSONResponse(res, media_type="application/json", status_code=200)


@app.post("/co-pilot")
async def co_pilot(request: Request):
    """
    Handles co-pilot creation or updates with optional file upload and system prompt.
    """
    client = create_client()
    
    # Parse the form data
    form = await request.form()
    file = form.get("file", None)
    system_prompt = form.get("system_prompt", None)
    context = form.get("context", None)  # Optional context parameter

    # Attempt to get the assistant & vector store from the form
    assistant_id = form.get("assistant", None)
    vector_store_id = form.get("vector_store", None)
    thread_id = form.get("session", None)

    # If no assistant, create one
    if not assistant_id:
        if not vector_store_id:
            vector_store = client.beta.vector_stores.create(name="demo")
            vector_store_id = vector_store.id
        base_prompt = '''
        You are a product management AI assistant, a product co-pilot.
        
        ### **File Handling Guidelines:**

        - **CSV and Excel Files:**
          - When a user refers to any CSV or Excel file by name, ALWAYS use the code_interpreter tool to analyze it.
          - Access these files directly and never claim you can't access a CSV or Excel file.

        - **Image Files:**
          - For any uploaded images, use the provided OCR/analysis text that has been extracted.
          - When a user refers to an image file by name, respond as if you can directly see the image.

        - **Other Document Files:**
          - For PDFs, DOCs, TXTs, and other document files, use the file_search tool to extract relevant information.

        - **File References:**
          - Pay careful attention to file references in the conversation.
          - When a user mentions any part of a filename, associate it with the full file that was uploaded.
        '''
        instructions = base_prompt if not system_prompt else f"{base_prompt} {system_prompt}"
        
        # Update to include code interpreter capability
        assistant = client.beta.assistants.create(
            name="demo_co_pilot",
            model="gpt-4o-mini",
            instructions=instructions,
            tools=[{"type": "code_interpreter"}, {"type": "file_search"}],
            tool_resources={"file_search": {"vector_store_ids": [vector_store_id]}},
        )
        assistant_id = assistant.id
    else:
        # If user gave an assistant, update instructions if needed
        if system_prompt:
            # Get existing assistant instructions
            assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant_id)
            existing_instructions = getattr(assistant_obj, "instructions", "")
            
            # Add file handling guidelines if not already present
            if "File Handling Guidelines" not in existing_instructions:
                file_guidelines = '''
                ### **File Handling Guidelines:**

                - **CSV and Excel Files:**
                  - When a user refers to any CSV or Excel file by name, ALWAYS use the code_interpreter tool to analyze it.
                  - Access these files directly and never claim you can't access a CSV or Excel file.

                - **Image Files:**
                  - For any uploaded images, use the provided OCR/analysis text that has been extracted.
                  - When a user refers to an image file by name, respond as if you can directly see the image.

                - **Other Document Files:**
                  - For PDFs, DOCs, TXTs, and other document files, use the file_search tool to extract relevant information.

                - **File References:**
                  - Pay careful attention to file references in the conversation.
                  - When a user mentions any part of a filename, associate it with the full file that was uploaded.
                '''
                updated_instructions = f"You are a product management AI assistant, a product co-pilot. {system_prompt}\n\n{file_guidelines}"
            else:
                updated_instructions = f"You are a product management AI assistant, a product co-pilot. {system_prompt}"
                
            client.beta.assistants.update(
                assistant_id=assistant_id,
                instructions=updated_instructions,
            )
        
        # If no vector_store, check existing or create new
        if not vector_store_id:
            assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant_id)
            file_search_resource = getattr(assistant_obj.tool_resources, "file_search", None)
            existing_stores = (
                file_search_resource.vector_store_ids
                if (file_search_resource and hasattr(file_search_resource, "vector_store_ids"))
                else []
            )
            if existing_stores:
                vector_store_id = existing_stores[0]
            else:
                vector_store = client.beta.vector_stores.create(name="demo")
                vector_store_id = vector_store.id
                existing_tools = assistant_obj.tools if assistant_obj.tools else []
                if not any(t["type"] == "file_search" for t in existing_tools):
                    existing_tools.append({"type": "file_search"})
                    
                # Ensure code_interpreter is in the tools
                if not any(t["type"] == "code_interpreter" for t in existing_tools):
                    existing_tools.append({"type": "code_interpreter"})
                    
                client.beta.assistants.update(
                    assistant_id=assistant_id,
                    tools=existing_tools,
                    tool_resources={"file_search": {"vector_store_ids": [vector_store_id]}},
                )

    # Handle file upload if present
    if file:
        filename = file.filename
        file_path = f"/tmp/{filename}"
        with open(file_path, "wb") as ftemp:
            file_content = await file.read()
            ftemp.write(file_content)
            
        # Check if it's a tabular file (CSV/Excel)
        if is_tabular_file(filename):
            try:
                # Process tabular data to get file info
                tabular_data = await process_tabular_data(file_path, filename)
                
                # Upload file to OpenAI
                with open(file_path, "rb") as file_stream:
                    file_obj = client.files.create(
                        file=file_stream,
                        purpose="assistants"
                    )
                
                # Associate file with assistant
                await associate_file_with_assistant(client, file_obj.id, assistant_id)
                
                # If thread exists, add file reference and create message
                if thread_id:
                    # Add file reference to thread
                    file_info = {
                        "type": "tabular",
                        "filename": filename,
                        "details": {
                            "rows": tabular_data.get("shape", (0, 0))[0] if "shape" in tabular_data else 0,
                            "columns": len(tabular_data.get("columns", [])) if "columns" in tabular_data else 0,
                            "column_names": tabular_data.get("columns", []),
                            "sheets": tabular_data.get("sheets", []) if "sheets" in tabular_data else []
                        }
                    }
                    await add_file_reference(client, thread_id, file_info)
                    
                    # Create message with file attachment
                    await create_message_with_attachment(
                        client, 
                        thread_id, 
                        f"I've uploaded a file named {filename}. Please analyze this data using the code_interpreter tool. This file is now available for analysis throughout our conversation whenever I refer to {filename}.",
                        file_obj.id
                    )
                
                logging.info(f"Tabular file {filename} associated with code interpreter")
            except Exception as e:
                logging.error(f"Error processing tabular file: {e}")
                
        # Check if it's an image file
        elif is_image_file(filename, None):
            try:
                # Analyze the image
                analysis_result = await image_analysis(client, file_content, filename)
                
                # If thread exists, add file reference with analysis and create message
                if thread_id:
                    # Add file reference with analysis to thread
                    file_info = {
                        "type": "image",
                        "filename": filename,
                        "analysis": analysis_result
                    }
                    await add_file_reference(client, thread_id, file_info)
                    
                    # Create message about the image
                    client.beta.threads.messages.create(
                        thread_id=thread_id,
                        role="user",
                        content=f"I've uploaded an image named {filename}. Here's what it contains: {analysis_result}"
                    )
                
                logging.info(f"Image file {filename} analyzed and added to thread")
            except Exception as e:
                logging.error(f"Error processing image file: {e}")
        else:
            # Normal file upload to vector store for file_search
            try:
                with open(file_path, "rb") as file_stream:
                    file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                        vector_store_id=vector_store_id,
                        files=[file_stream]
                    )
                    
                # If thread exists, add file reference and create message
                if thread_id:
                    # Add file reference to thread
                    file_info = {
                        "type": "document",
                        "filename": filename
                    }
                    await add_file_reference(client, thread_id, file_info)
                    
                    # Create message about the document
                    client.beta.threads.messages.create(
                        thread_id=thread_id,
                        role="user",
                        content=f"I've uploaded a document named {filename}. The document has been processed and is now available for reference whenever I refer to this document."
                    )
                
                logging.info(f"Document file {filename} uploaded to vector store")
            except Exception as e:
                logging.error(f"Error uploading document to vector store: {e}")

    # If context provided and thread exists, update context
    if context and thread_id:
        try:
            await update_context(client, thread_id, context)
        except Exception as e:
            logging.info(f"An error occurred while adding context to the thread: {e}")
            # Don't fail the entire request if just adding context fails

    return JSONResponse(
        {
            "message": "Assistant updated successfully.",
            "assistant": assistant_id,
            "vector_store": vector_store_id,
        }
    )


@app.post("/upload-file")
async def upload_file(
    file: UploadFile = Form(...), 
    assistant: str = Form(...), 
    session: Optional[str] = Form(None),
    context: Optional[str] = Form(None),
    prompt: Optional[str] = Form(None)
):
    """
    Uploads a file and associates it with the given assistant.
    Handles different file types appropriately (tabular files use code_interpreter, others use vector_store).
    """
    client = create_client()
    thread_id = session  # Rename for clarity

    try:
        # Save the uploaded file locally and get the data
        file_content = await file.read()
        filename = file.filename
        file_path = f"/tmp/{filename}"
        with open(file_path, "wb") as temp_file:
            temp_file.write(file_content)
            
        # Determine file type
        is_image = is_image_file(filename, file.content_type)
        is_tabular = is_tabular_file(filename)
        
        # Retrieve the assistant
        assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant)
        
        # Process based on file type
        if is_tabular:
            # This is a CSV or Excel file - use code interpreter
            try:
                # Process tabular data to get file info
                tabular_data = await process_tabular_data(file_path, filename)
                
                # Upload file to OpenAI
                with open(file_path, "rb") as file_stream:
                    file_obj = client.files.create(
                        file=file_stream,
                        purpose="assistants"
                    )
                
                # Associate file with assistant
                await associate_file_with_assistant(client, file_obj.id, assistant)
                
                # If we have a thread, add file reference and create message with attachment
                if thread_id:
                    # Add file reference to thread
                    file_info = {
                        "type": "tabular",
                        "filename": filename,
                        "details": {
                            "rows": tabular_data.get("shape", (0, 0))[0] if "shape" in tabular_data else 0,
                            "columns": len(tabular_data.get("columns", [])) if "columns" in tabular_data else 0,
                            "column_names": tabular_data.get("columns", []),
                            "sheets": tabular_data.get("sheets", []) if "sheets" in tabular_data else []
                        }
                    }
                    await add_file_reference(client, thread_id, file_info)
                    
                    # Create message with file attachment
                    if 'csv' in filename.lower():
                        columns = tabular_data.get("columns", [])
                        message = (
                            f"I've uploaded a CSV file named {filename}. "
                            f"The columns are: {', '.join(columns)}. "
                            f"Please analyze this data using the code_interpreter tool. "
                            f"This file is now available for analysis throughout our conversation whenever I refer to {filename}."
                        )
                    else:
                        sheets = tabular_data.get("sheets", [])
                        message = (
                            f"I've uploaded an Excel file named {filename} with sheets: {', '.join(sheets)}. "
                            f"Please analyze this data using the code_interpreter tool. "
                            f"This file is now available for analysis throughout our conversation whenever I refer to {filename}."
                        )
                    
                    await create_message_with_attachment(client, thread_id, message, file_obj.id)
                
                logging.info(f"Tabular file {filename} processed with code interpreter")
                
                return JSONResponse(
                    {
                        "message": "File successfully uploaded for code interpreter.",
                        "file_type": "tabular",
                        "file_id": file_obj.id
                    },
                    status_code=200
                )
                
            except Exception as e:
                logging.error(f"Error processing tabular file with code interpreter: {e}")
                return JSONResponse({"error": str(e)}, status_code=500)
                
        # For images, analyze and add to thread
        elif is_image:
            try:
                # Analyze the image
                analysis_result = await image_analysis(client, file_content, filename, prompt)
                
                # Add the analysis to the thread if thread_id is provided
                if thread_id:
                    # Add file reference with analysis to thread
                    file_info = {
                        "type": "image",
                        "filename": filename,
                        "analysis": analysis_result
                    }
                    await add_file_reference(client, thread_id, file_info)
                    
                    # Create message about the image
                    client.beta.threads.messages.create(
                        thread_id=thread_id,
                        role="user",
                        content=f"I've uploaded an image named {filename}. Here's what it contains: {analysis_result}"
                    )
                
                logging.info(f"Image file {filename} analyzed and added to thread")
                
                return JSONResponse(
                    {
                        "message": "Image file successfully analyzed and added to thread.",
                        "file_type": "image",
                        "image_analyzed": True
                    },
                    status_code=200
                )
                
            except Exception as e:
                logging.error(f"Error processing image file: {e}")
                return JSONResponse({"error": str(e)}, status_code=500)
        
        # For other files, use vector store
        else:
            # Check if there's a file_search resource
            file_search_resource = getattr(assistant_obj.tool_resources, "file_search", None)
            vector_store_ids = (
                file_search_resource.vector_store_ids
                if (file_search_resource and hasattr(file_search_resource, "vector_store_ids"))
                else []
            )

            if vector_store_ids:
                # If a vector store already exists, reuse it
                vector_store_id = vector_store_ids[0]
            else:
                # No vector store associated yet, create one
                logging.info("No associated vector store found. Creating a new one.")
                vector_store = client.beta.vector_stores.create(name=f"Assistant_{assistant}_Store")
                vector_store_id = vector_store.id

                # Ensure the 'file_search' tool is present in the assistant's tools
                existing_tools = assistant_obj.tools if assistant_obj.tools else []
                if not any(t["type"] == "file_search" for t in existing_tools):
                    existing_tools.append({"type": "file_search"})

                # Update the assistant to associate with this new vector store
                client.beta.assistants.update(
                    assistant_id=assistant,
                    tools=existing_tools,
                    tool_resources={
                        "file_search": {
                            "vector_store_ids": [vector_store_id]
                        }
                    }
                )

            try:
                with open(file_path, "rb") as file_stream:
                    file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                        vector_store_id=vector_store_id,
                        files=[file_stream]
                    )
                    
                # If thread exists, add file reference and create message
                if thread_id:
                    # Add file reference to thread
                    file_info = {
                        "type": "document",
                        "filename": filename
                    }
                    await add_file_reference(client, thread_id, file_info)
                    
                    # Create message about the document
                    client.beta.threads.messages.create(
                        thread_id=thread_id,
                        role="user",
                        content=f"I've uploaded a document named {filename}. The document has been processed and is now available for reference whenever I refer to this document."
                    )
                
                logging.info(f"Document file {filename} uploaded to vector store")
                
                return JSONResponse(
                    {
                        "message": "Document successfully uploaded to vector store.",
                        "file_type": "document"
                    },
                    status_code=200
                )
                
            except Exception as e:
                logging.error(f"Error uploading document to vector store: {e}")
                return JSONResponse({"error": str(e)}, status_code=500)
            
        # If context provided and thread exists, update context
        if context and thread_id:
            try:
                await update_context(client, thread_id, context)
            except Exception as e:
                logging.error(f"Error updating context in thread: {e}")
                # Continue even if context update fails

    except Exception as e:
        logging.error(f"Error uploading file: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/conversation")
async def conversation(
    session: Optional[str] = None,
    prompt: Optional[str] = None,
    assistant: Optional[str] = None,
    context: Optional[str] = None
):
    """
    Handles conversation queries. 
    Preserves the original query parameters and output format.
    """
    client = create_client()

    try:
        # If no assistant or session provided, create them (same fallback approach)
        if not assistant:
            assistant_obj = client.beta.assistants.create(
                name="conversation_assistant",
                model="gpt-4o-mini",
                instructions='''
                You are a conversation assistant.
                
                ### **File Handling Guidelines:**

                - **CSV and Excel Files:**
                  - When a user refers to any CSV or Excel file by name, ALWAYS use the code_interpreter tool to analyze it.
                  - Access these files directly and never claim you can't access a CSV or Excel file.

                - **Image Files:**
                  - For any uploaded images, use the provided OCR/analysis text that has been extracted.
                  - When a user refers to an image file by name, respond as if you can directly see the image.

                - **Other Document Files:**
                  - For PDFs, DOCs, TXTs, and other document files, use the file_search tool to extract relevant information.

                - **File References:**
                  - Pay careful attention to file references in the conversation.
                  - When a user mentions any part of a filename, associate it with the full file that was uploaded.
                '''
            )
            assistant = assistant_obj.id

        if not session:
            thread = client.beta.threads.create()
            session = thread.id
            
        # If context is provided, update user persona context
        if context:
            await update_context(client, session, context)

        # Add message if prompt given
        if prompt:
            client.beta.threads.messages.create(
                thread_id=session,
                role="user",
                content=prompt
            )

        def stream_response():
            buffer = []
            try:
                with client.beta.threads.runs.stream(thread_id=session, assistant_id=assistant) as stream:
                    for text in stream.text_deltas:
                        buffer.append(text)
                        if len(buffer) >= 10:
                            yield ''.join(buffer)
                            buffer = []
                if buffer:
                    yield ''.join(buffer)
            except Exception as e:
                logging.error(f"Streaming error: {e}")
                yield "[ERROR] The response was interrupted. Please try again."

        return StreamingResponse(stream_response(), media_type="text/event-stream")

    except Exception as e:
        logging.error(f"Error in conversation: {e}")
        raise HTTPException(status_code=500, detail="Failed to process conversation")


@app.get("/chat")
async def chat(
    session: Optional[str] = None,
    prompt: Optional[str] = None,
    assistant: Optional[str] = None,
    context: Optional[str] = None
):
    """
    Handles conversation queries.
    Preserves the original query parameters and output format.
    """
    client = create_client()

    try:
        # If no assistant or session provided, create them
        if not assistant:
            assistant_obj = client.beta.assistants.create(
                name="chat_assistant",
                model="gpt-4o-mini",
                instructions='''
                You are a conversation assistant.
                
                ### **File Handling Guidelines:**

                - **CSV and Excel Files:**
                  - When a user refers to any CSV or Excel file by name, ALWAYS use the code_interpreter tool to analyze it.
                  - Access these files directly and never claim you can't access a CSV or Excel file.

                - **Image Files:**
                  - For any uploaded images, use the provided OCR/analysis text that has been extracted.
                  - When a user refers to an image file by name, respond as if you can directly see the image.

                - **Other Document Files:**
                  - For PDFs, DOCs, TXTs, and other document files, use the file_search tool to extract relevant information.

                - **File References:**
                  - Pay careful attention to file references in the conversation.
                  - When a user mentions any part of a filename, associate it with the full file that was uploaded.
                '''
            )
            assistant = assistant_obj.id

        if not session:
            thread = client.beta.threads.create()
            session = thread.id
            
        # If context is provided, update user persona context
        if context:
            await update_context(client, session, context)

        # Add message if prompt given
        if prompt:
            client.beta.threads.messages.create(
                thread_id=session,
                role="user",
                content=prompt
            )

        response_text = []
        try:
            with client.beta.threads.runs.stream(thread_id=session, assistant_id=assistant) as stream:
                for text in stream.text_deltas:
                    response_text.append(text)
        except Exception as e:
            logging.error(f"Streaming error: {e}")
            raise HTTPException(status_code=500, detail="The response was interrupted. Please try again.")

        full_response = ''.join(response_text)
        return JSONResponse(content={"response": full_response})

    except Exception as e:
        logging.error(f"Error in conversation: {e}")
        raise HTTPException(status_code=500, detail="Failed to process conversation")


@app.post("/trim-thread")
async def trim_thread(request: Request):
    """
    Gets all threads for a given assistant, summarizes them, and removes old threads.
    Uses 48 hours as the threshold for thread cleanup.
    Accepts both query parameters and form data.
    """
    # Get parameters from form data
    form_data = await request.form()
    assistant_id = form_data.get("assistant_id")
    max_age_days = form_data.get("max_age_days")
    
    if max_age_days is not None:
        try:
            max_age_days = int(max_age_days)
        except ValueError:
            max_age_days = None
    
    # Set default cleanup threshold to 48 hours
    time_threshold_hours = 48
    
    if not assistant_id:
        raise HTTPException(status_code=400, detail="assistant_id is required")
    
    client = create_client()
    summary_store = {}
    deleted_count = 0
    summarized_count = 0
    
    try:
        # Step 1: Get all runs to identify threads used with this assistant
        all_threads = {}
        
        # Get all runs (limited by API, may need pagination in production)
        runs = client.beta.threads.runs.list_all_runs()
        
        # Filter runs by assistant_id and collect their thread_ids
        for run in runs.data:
            if run.assistant_id == assistant_id:
                thread_id = run.thread_id
                # Get the last_active timestamp (using the run's created_at as proxy)
                last_active = datetime.datetime.fromtimestamp(run.created_at)
                
                if thread_id in all_threads:
                    # Keep the most recent timestamp
                    if last_active > all_threads[thread_id]['last_active']:
                        all_threads[thread_id]['last_active'] = last_active
                else:
                    all_threads[thread_id] = {
                        'thread_id': thread_id,
                        'last_active': last_active
                    }
        
        # Sort threads by last_active time (most recent first)
        sorted_threads = sorted(
            all_threads.values(), 
            key=lambda x: x['last_active'], 
            reverse=True
        )
        
        # Get current time for age comparison
        now = datetime.datetime.now()
        
        # Step 2: Process each thread
        for thread_info in sorted_threads:
            thread_id = thread_info['thread_id']
            last_active = thread_info['last_active']
            
            # Calculate hours since last activity
            thread_age_hours = (now - last_active).total_seconds() / 3600
            
            # Skip active threads that are recent
            if thread_age_hours <= 1:  # Keep very recent threads untouched
                continue
                
            # Check if thread has summary metadata
            try:
                thread = client.beta.threads.retrieve(thread_id=thread_id)
                metadata = thread.metadata if hasattr(thread, 'metadata') else {}
                
                # If it's a summary thread and too old, delete it
                if metadata.get('is_summary') and thread_age_hours > time_threshold_hours:
                    client.beta.threads.delete(thread_id=thread_id)
                    deleted_count += 1
                    continue
                
                # If regular thread and older than threshold, summarize it
                if thread_age_hours > time_threshold_hours:
                    # Get messages in the thread
                    messages = client.beta.threads.messages.list(thread_id=thread_id)
                    
                    if len(list(messages.data)) > 0:
                        # Create prompt for summarization
                        summary_content = "\n\n".join([
                            f"{msg.role}: {msg.content[0].text.value if hasattr(msg, 'content') and len(msg.content) > 0 else 'No content'}" 
                            for msg in messages.data
                        ])
                        
                        # Create a new thread for the summary
                        summary_thread = client.beta.threads.create(
                            metadata={"is_summary": True, "original_thread_id": thread_id}
                        )
                        
                        # Add a request to summarize
                        client.beta.threads.messages.create(
                            thread_id=summary_thread.id,
                            role="user",
                            content=f"Summarize the following conversation in a concise paragraph:\n\n{summary_content}"
                        )
                        
                        # Run the summarization
                        run = client.beta.threads.runs.create(
                            thread_id=summary_thread.id,
                            assistant_id=assistant_id
                        )
                        
                        # Wait for completion with timeout
                        max_wait = 30  # 30 seconds timeout
                        start_time = time.time()
                        
                        while True:
                            if time.time() - start_time > max_wait:
                                logging.warning(f"Timeout waiting for summarization of thread {thread_id}")
                                break
                                
                            run_status = client.beta.threads.runs.retrieve(
                                thread_id=summary_thread.id,
                                run_id=run.id
                            )
                            
                            if run_status.status == "completed":
                                # Get the summary
                                summary_messages = client.beta.threads.messages.list(
                                    thread_id=summary_thread.id,
                                    order="desc"
                                )
                                
                                # Extract the summary text
                                summary_text = next(
                                    (msg.content[0].text.value for msg in summary_messages.data 
                                     if msg.role == "assistant" and hasattr(msg, 'content') and len(msg.content) > 0),
                                    "Summary not available."
                                )
                                
                                # Store summary info
                                summary_store[thread_id] = {
                                    "summary": summary_text,
                                    "summary_thread_id": summary_thread.id,
                                    "original_thread_id": thread_id,
                                    "summarized_at": now.isoformat()
                                }
                                
                                # Delete the original thread
                                client.beta.threads.delete(thread_id=thread_id)
                                deleted_count += 1
                                summarized_count += 1
                                break
                            
                            elif run_status.status in ["failed", "cancelled", "expired"]:
                                logging.error(f"Summary generation failed for thread {thread_id}: {run_status.status}")
                                break
                                
                            time.sleep(1)
            
            except Exception as e:
                logging.error(f"Error processing thread {thread_id}: {e}")
                continue
        
        return JSONResponse({
            "status": "Thread trimming completed",
            "threads_processed": len(sorted_threads),
            "threads_summarized": summarized_count,
            "threads_deleted": deleted_count,
            "summaries_stored": len(summary_store)
        })
        
    except Exception as e:
        logging.error(f"Error in trim-thread: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to trim threads: {str(e)}")


@app.post("/file-cleanup")
async def file_cleanup(request: Request):
    """
    Lists and deletes files older than 48 hours.
    Simplified implementation with no summarization.
    Maintains the same vector store.
    Accepts both query parameters and form data.
    """
    # Get parameters from form data
    form_data = await request.form()
    vector_store_id = form_data.get("vector_store_id")
    
    if not vector_store_id:
        raise HTTPException(status_code=400, detail="vector_store_id is required")
    
    client = create_client()
    deleted_count = 0
    skipped_count = 0
    
    try:
        # Step 1: Get all files in the vector store
        file_batches = client.beta.vector_stores.file_batches.list(vector_store_id=vector_store_id)
        
        if not file_batches.data:
            return JSONResponse({
                "status": "No files found in the vector store",
                "vector_store_id": vector_store_id
            })
        
        # Get current time for age comparison
        now = datetime.datetime.now()
        
        # Step 2: Process each file batch to find files older than 48 hours
        for batch in file_batches.data:
            # Calculate age in hours
            batch_created = datetime.datetime.fromtimestamp(batch.created_at)
            batch_age_hours = (now - batch_created).total_seconds() / 3600
            
            # Skip recent batches
            if batch_age_hours <= 48:
                skipped_count += 1
                continue
                
            # Get files in this batch
            files = client.beta.vector_stores.files.list(
                vector_store_id=vector_store_id,
                file_batch_id=batch.id
            )
            
            # Delete files older than 48 hours
            for file in files.data:
                try:
                    client.beta.vector_stores.files.delete(
                        vector_store_id=vector_store_id,
                        file_id=file.id
                    )
                    deleted_count += 1
                except Exception as e:
                    logging.error(f"Error deleting file {file.id}: {e}")
        
        return JSONResponse({
            "status": "File cleanup completed",
            "vector_store_id": vector_store_id,
            "files_deleted": deleted_count,
            "batches_skipped": skipped_count
        })
        
    except Exception as e:
        logging.error(f"Error in file-cleanup: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to clean up files: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
