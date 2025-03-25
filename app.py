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

async def image_analysis(client, image_data: bytes, filename: str, prompt: Optional[str] = None) -> dict:
    """
    Analyzes an image using Azure OpenAI vision capabilities and returns both visual analysis and OCR text.
    Returns a dictionary with both 'analysis' and 'ocr_text' fields.
    """
    try:
        ext = os.path.splitext(filename)[1].lower()
        b64_img = base64.b64encode(image_data).decode("utf-8")
        # Default to jpeg if extension can't be determined
        mime = f"image/{ext[1:]}" if ext and ext[1:] in ['jpg', 'jpeg', 'png', 'gif', 'webp'] else "image/jpeg"
        data_url = f"data:{mime};base64,{b64_img}"
        
        # Create a client for vision API
        vision_client = AzureOpenAI(
            azure_endpoint=AZURE_ENDPOINT,
            api_key=AZURE_API_KEY,
            api_version=AZURE_API_VERSION,
        )
        
        # 1. General image analysis prompt
        default_prompt = (
            "Analyze this image and provide a thorough summary including all elements. "
            "If there's any text visible, mention that there is text but don't try to read it in detail. Describe:"
        )
        combined_prompt = f"{default_prompt} {prompt}" if prompt else default_prompt
        
        # 2. OCR-specific prompt to extract text
        ocr_prompt = (
            "This image may contain text. Please extract and transcribe ALL text visible in the image, "
            "preserving the format and layout as much as possible. If this is a document, invoice, receipt, "
            "or any form with text, please transcribe all written content exactly as shown."
        )
        
        # Run general analysis
        response = vision_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user", 
                "content": [
                    {"type": "text", "text": combined_prompt},
                    {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}}
                ]
            }],
            max_tokens=500
        )
        analysis_text = response.choices[0].message.content
        
        # Run OCR analysis
        ocr_response = vision_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user", 
                "content": [
                    {"type": "text", "text": ocr_prompt},
                    {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}}
                ]
            }],
            max_tokens=1000
        )
        ocr_text = ocr_response.choices[0].message.content
        
        return {
            "analysis": analysis_text,
            "ocr_text": ocr_text
        }
        
    except Exception as e:
        logging.error(f"Image analysis error: {e}")
        return {
            "analysis": f"Error analyzing image: {str(e)}",
            "ocr_text": "Failed to extract text from the image."
        }
        
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

async def get_file_summary(file_path, filename):
    """
    Generate a summary of the file contents based on file type.
    """
    try:
        file_ext = os.path.splitext(filename.lower())[1]
        
        # CSV file summary
        if file_ext == '.csv':
            df = pd.read_csv(file_path)
            row_count = len(df)
            col_count = len(df.columns)
            columns = ", ".join(df.columns[:10]) + ("..." if len(df.columns) > 10 else "")
            sample = df.head(3).to_string()
            
            return (
                f"CSV file with {row_count} rows and {col_count} columns.\n"
                f"Columns: {columns}\n"
                f"Sample data:\n{sample}"
            )
            
        # Excel file summary
        elif file_ext in ['.xlsx', '.xls']:
            xls = pd.ExcelFile(file_path)
            sheet_names = xls.sheet_names
            sheets_info = []
            
            for sheet in sheet_names:
                df = pd.read_excel(file_path, sheet_name=sheet)
                sheets_info.append(f"Sheet '{sheet}': {len(df)} rows, {len(df.columns)} columns")
            
            return (
                f"Excel file with {len(sheet_names)} sheets: {', '.join(sheet_names)}.\n"
                f"Details:\n" + "\n".join(sheets_info)
            )
            
        # Text-based files
        elif file_ext in ['.txt', '.md', '.csv', '.json']:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read(1000)  # Read first 1000 chars for preview
            return f"Text file. Preview:\n{content}{'...' if len(content) >= 1000 else ''}"
            
        # PDF files would need a PDF parser
        elif file_ext in ['.pdf']:
            return "PDF document (content not previewed)"
            
        # Default for other files
        else:
            return f"File with extension {file_ext}"
            
    except Exception as e:
        logging.error(f"Error generating file summary: {e}")
        return f"Unable to summarize file: {str(e)}"

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

        - **Guidelines:**
            - Utilize the file_search tool to extract relevant data from uploaded files.
            - Ensure all sections are contextually relevant, logically structured, and provide actionable insights.
            - If certain information is missing, make informed assumptions or prompt the user for clarification.
            - Incorporate industry best practices and standards where applicable.

        2. **Answer Generic Product Management Questions:**
        - **Scope:** Respond to a broad range of product management queries, including strategy, market analysis, feature prioritization, user feedback interpretation, and more.
        - **Methodology:**
            - Use the file_search tool to find pertinent information within uploaded files.
            - Leverage your comprehensive knowledge base to provide thorough and insightful answers.
            - If a question falls outside the scope of the provided files and your expertise, default to a general GPT-4 response without referencing the files.
            - Maintain a balance between technical detail and accessibility, ensuring responses are understandable yet informative.

        3. **Data Analysis with Code Interpreter:**
        - When prompted with "dataframe to be processed by code interpreter: df", use the code_interpreter tool to analyze the provided data.
        - When a user uploads CSV or Excel files, they are automatically made available to you through the code interpreter.
        - For CSV files, you can directly read and analyze them using pandas.
        - For Excel files with multiple sheets, you can access each sheet individually.
        - Perform appropriate data analysis, including statistical summaries, visualizations, and relevant insights.
        - Present your analysis results in a clear, organized manner.

        4. **Image Analysis:**
        - When a user uploads an image, you'll receive the description and any text content from the image.
        - Reference the image naturally in your responses as if you can see it directly.
        - If the image contains text (OCR content), you can refer to specific text elements from the image.
        - Be confident and direct when discussing the content of images.

        ### **Behavioral Guidelines:**

        - **File Awareness:**
        - Always maintain awareness of all files that have been uploaded by the user.
        - Reference uploaded files by name when responding to related queries.
        - For images, refer to their visual content and text content naturally.
        - For data files, refer to their structure and content with confidence.

        - **Contextual Awareness:**
        - Always consider the context provided by the uploaded files and previous interactions.
        - Adapt your responses based on the specific needs and preferences of the user.

        - **Proactive Insight Generation:**
        - Go beyond surface-level answers by providing deep insights, trends, and actionable recommendations.
        - Anticipate potential follow-up questions and address them preemptively where appropriate.

        - **Professional Tone:**
        - Maintain a professional, clear, and concise communication style.
        - Ensure all interactions are respectful, objective, and goal-oriented.

        - **Seamless Mode Switching:**
        - Efficiently transition between PRD generation, data analysis, and generic question answering based on user prompts.
        - Recognize when a query is outside the scope of the uploaded files and adjust your response accordingly without prompting the user.

        - **Continuous Improvement:**
        - Learn from each interaction to enhance future responses.
        - Seek feedback when necessary to better align with the user's expectations and requirements.

        ### **Important Notes:**

        - **File Handling:**
        - When asked about files, respond as if you have direct access to them.
        - For images, refer to both visual elements and text from the OCR analysis.
        - For data files, refer to their structure and content confidently.
        - If a user asks about a specific file, check if you have information about it before responding.

        - **Data Privacy:**
        - Handle all uploaded files and user data with the utmost confidentiality and in compliance with relevant data protection standards.

        - **Assumption Handling:**
        - Clearly indicate when you are making assumptions due to missing information.
        - Provide rationales for your assumptions to maintain transparency.

        - **Error Handling:**
        - Gracefully manage any errors or uncertainties by informing the user and seeking clarification when necessary.

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
            f.write(await file.read())
            
        # Check if it's a tabular file (CSV/Excel)
        if is_tabular_file(filename):
            # Process the file with the code interpreter directly
            try:
                # Get file summary for the message
                tabular_data = await process_tabular_data(file_path, filename)
                file_summary = await get_file_summary(file_path, filename)
                
                # Upload file for code interpreter
                with open(file_path, "rb") as file_stream:
                    file_obj = client.files.create(
                        file=file_stream,
                        purpose="assistants"
                    )
                
                # Associate the file with the assistant for code interpreter
                client.beta.assistants.files.create(
                    assistant_id=assistant.id,
                    file_id=file_obj.id
                )
                
                # Create detailed message for the assistant
                file_type = "CSV" if tabular_data["type"] == "csv" else "Excel"
                
                if file_type == "CSV":
                    shape = tabular_data["shape"]
                    columns = tabular_data["columns"]
                    message = (
                        f"I've uploaded a {file_type} file named '{filename}' with {shape[0]} rows and {shape[1]} columns.\n\n"
                        f"Columns: {', '.join(columns)}\n\n"
                        f"This data file has been loaded and is available for analysis. You can reference it by name: '{filename}'.\n\n"
                        f"dataframe to be processed by code interpreter: df"
                    )
                else:
                    sheets = tabular_data["sheets"]
                    message = (
                        f"I've uploaded an {file_type} file named '{filename}' with {len(sheets)} sheets: {', '.join(sheets)}.\n\n"
                        f"This data file has been loaded and is available for analysis. You can reference it by name: '{filename}'.\n\n"
                        f"dataframe to be processed by code interpreter: df"
                    )
                
                # Create message to instruct code interpreter to process the file
                client.beta.threads.messages.create(
                    thread_id=thread.id,
                    role="user",
                    content=message
                )
                
                logging.info(f"Tabular file {filename} associated with code interpreter")
            except Exception as e:
                logging.error(f"Error processing tabular file: {e}")
        
        # Check if it's an image
        elif is_image_file(filename):
            try:
                # Analyze the image
                with open(file_path, "rb") as file_stream:
                    file_content = file_stream.read()
                
                # Run image analysis to get both visual description and OCR
                analysis_result = await image_analysis(client, file_content, filename)
                analysis_text = analysis_result["analysis"]
                ocr_text = analysis_result["ocr_text"]
                
                # Prepare a comprehensive message
                image_message = (
                    f"I've uploaded an image named '{filename}'.\n\n"
                    f"Description: {analysis_text}\n\n"
                )
                
                # If there's OCR text, add it
                if ocr_text and ocr_text.strip() and "no text" not in ocr_text.lower():
                    image_message += f"The image contains the following text:\n\n{ocr_text}\n\n"
                else:
                    image_message += "The image doesn't contain any significant text.\n\n"
                
                image_message += "You can reference this image by name in our conversation."
                
                # Add the comprehensive message to the thread
                client.beta.threads.messages.create(
                    thread_id=thread.id,
                    role="user",
                    content=image_message
                )
                
                logging.info(f"Image {filename} analyzed and added to thread")
            except Exception as e:
                logging.error(f"Error processing image: {e}")
        else:
            # Normal file upload to vector store for file_search
            with open(file_path, "rb") as file_stream:
                file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                    vector_store_id=vector_store.id, 
                    files=[file_stream]
                )
                
            # Get a summary of the file for the message
            file_summary = await get_file_summary(file_path, filename)
            
            # Add message about the uploaded document
            client.beta.threads.messages.create(
                thread_id=thread.id,
                role="user",
                content=f"I've uploaded a document named '{filename}'.\n\n{file_summary}\n\nYou can reference this document by name in our conversation."
            )
                
            logging.info(f"File uploaded to vector store: status={file_batch.status}, count={file_batch.file_counts}")

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
        base_prompt = "You are a product management AI assistant, a product co-pilot."
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
            client.beta.assistants.update(
                assistant_id=assistant_id,
                instructions=(
                    f"You are a product management AI assistant, a product co-pilot. {system_prompt}"
                    if system_prompt
                    else "You are a product management AI assistant, a product co-pilot."
                ),
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
        file_path = f"/tmp/{file.filename}"
        with open(file_path, "wb") as ftemp:
            file_content = await file.read()
            ftemp.write(file_content)
            
        # Check if it's a tabular file (CSV/Excel)
        if is_tabular_file(file.filename):
            try:
                # Get file summary for the message
                tabular_data = await process_tabular_data(file_path, file.filename)
                file_summary = await get_file_summary(file_path, file.filename)
                
                # Upload file to OpenAI for code interpreter
                with open(file_path, "rb") as file_stream:
                    file_obj = client.files.create(
                        file=file_stream,
                        purpose="assistants"
                    )
                
                # Associate the file with the assistant for code interpreter
                client.beta.assistants.files.create(
                    assistant_id=assistant_id,
                    file_id=file_obj.id
                )
                
                # If thread exists, create detailed message for the assistant
                if thread_id:
                    file_type = "CSV" if tabular_data["type"] == "csv" else "Excel"
                    
                    if file_type == "CSV":
                        shape = tabular_data.get("shape", (0, 0))
                        columns = tabular_data.get("columns", [])
                        message = (
                            f"I've uploaded a {file_type} file named '{file.filename}' with {shape[0]} rows and {shape[1]} columns.\n\n"
                            f"Columns: {', '.join(columns)}\n\n"
                            f"This data file has been loaded and is available for analysis. You can reference it by name: '{file.filename}'.\n\n"
                            f"dataframe to be processed by code interpreter: df"
                        )
                    else:
                        sheets = tabular_data.get("sheets", [])
                        message = (
                            f"I've uploaded an {file_type} file named '{file.filename}' with {len(sheets)} sheets: {', '.join(sheets)}.\n\n"
                            f"This data file has been loaded and is available for analysis. You can reference it by name: '{file.filename}'.\n\n"
                            f"dataframe to be processed by code interpreter: df"
                        )
                    
                    # Create message to instruct code interpreter to process the file
                    client.beta.threads.messages.create(
                        thread_id=thread_id,
                        role="user",
                        content=message
                    )
                
                logging.info(f"Tabular file {file.filename} associated with code interpreter")
            except Exception as e:
                logging.error(f"Error processing tabular file: {e}")
        
        # Check if it's an image
        elif is_image_file(file.filename):
            try:
                # Run image analysis to get both visual description and OCR
                analysis_result = await image_analysis(client, file_content, file.filename)
                analysis_text = analysis_result["analysis"]
                ocr_text = analysis_result["ocr_text"]
                
                # If thread exists, add comprehensive message
                if thread_id:
                    # Prepare a comprehensive message
                    image_message = (
                        f"I've uploaded an image named '{file.filename}'.\n\n"
                        f"Description: {analysis_text}\n\n"
                    )
                    
                    # If there's OCR text, add it
                    if ocr_text and ocr_text.strip() and "no text" not in ocr_text.lower():
                        image_message += f"The image contains the following text:\n\n{ocr_text}\n\n"
                    else:
                        image_message += "The image doesn't contain any significant text.\n\n"
                    
                    image_message += "You can reference this image by name in our conversation."
                    
                    # Add the comprehensive message to the thread
                    client.beta.threads.messages.create(
                        thread_id=thread_id,
                        role="user",
                        content=image_message
                    )
                
                logging.info(f"Image {file.filename} analyzed and added to thread")
            except Exception as e:
                logging.error(f"Error processing image: {e}")
        else:
            # Normal file upload to vector store for file_search
            with open(file_path, "rb") as file_stream:
                file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                    vector_store_id=vector_store_id,
                    files=[file_stream]
                )
            
            # If thread exists, add message about the uploaded document
            if thread_id:
                # Get a summary of the file for the message
                file_summary = await get_file_summary(file_path, file.filename)
                
                client.beta.threads.messages.create(
                    thread_id=thread_id,
                    role="user",
                    content=f"I've uploaded a document named '{file.filename}'.\n\n{file_summary}\n\nYou can reference this document by name in our conversation."
                )
            
            logging.info(f"File uploaded to vector store: {file.filename}")

    # If context provided and thread exists, update context
    if context and thread_id:
        try:
            await update_context(client, thread_id, context)
        except BaseException as e:
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
    Handles different file types appropriately and makes the assistant aware of the file.
    """
    client = create_client()
    thread_id = session  # Rename for clarity

    try:
        # Save the uploaded file locally and get the data
        file_content = await file.read()
        file_path = f"/tmp/{file.filename}"
        with open(file_path, "wb") as temp_file:
            temp_file.write(file_content)
            
        # Determine file type
        is_image = is_image_file(file.filename, file.content_type)
        is_tabular = is_tabular_file(file.filename)
        
        # Retrieve the assistant
        assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant)
        
        # Process based on file type
        if is_tabular:
            # This is a CSV or Excel file - use code interpreter
            try:
                # Get file summary for the message
                tabular_data = await process_tabular_data(file_path, file.filename)
                file_summary = await get_file_summary(file_path, file.filename)
                
                # Upload file to OpenAI for code interpreter
                with open(file_path, "rb") as file_stream:
                    file_obj = client.files.create(
                        file=file_stream,
                        purpose="assistants"
                    )
                
                # Associate the file with the assistant
                client.beta.assistants.files.create(
                    assistant_id=assistant,
                    file_id=file_obj.id
                )
                
                # If we have a thread, add a message to process the file
                if thread_id:
                    # Create a message with instructions to process the dataframe
                    file_type = "CSV" if tabular_data["type"] == "csv" else "Excel"
                    
                    if file_type == "CSV":
                        shape = tabular_data["shape"]
                        columns = tabular_data["columns"]
                        message = (
                            f"I've uploaded a {file_type} file named '{file.filename}' with {shape[0]} rows and {shape[1]} columns.\n\n"
                            f"Columns: {', '.join(columns)}\n\n"
                            f"This data file has been loaded and is available for analysis. You can reference it by name: '{file.filename}'.\n\n"
                            f"dataframe to be processed by code interpreter: df"
                        )
                    else:
                        sheets = tabular_data["sheets"]
                        message = (
                            f"I've uploaded an {file_type} file named '{file.filename}' with {len(sheets)} sheets: {', '.join(sheets)}.\n\n"
                            f"This data file has been loaded and is available for analysis. You can reference it by name: '{file.filename}'.\n\n"
                            f"dataframe to be processed by code interpreter: df"
                        )
                    
                    # Add message to thread for immediate awareness
                    client.beta.threads.messages.create(
                        thread_id=thread_id,
                        role="user",
                        content=message
                    )
                
                logging.info(f"Tabular file {file.filename} processed with code interpreter")
                
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
                # If code interpreter processing fails, try vector store as fallback
                
        # For images with thread_id, analyze and add to thread
        if is_image and thread_id:
            logging.info(f"Analyzing image file: {file.filename}")
            analysis_result = await image_analysis(client, file_content, file.filename, prompt)
            
            # Extract analysis and OCR text
            analysis_text = analysis_result["analysis"]
            ocr_text = analysis_result["ocr_text"]
            
            # Prepare a comprehensive message
            image_message = (
                f"I've uploaded an image named '{file.filename}'.\n\n"
                f"Description: {analysis_text}\n\n"
            )
            
            # If there's OCR text, add it
            if ocr_text and ocr_text.strip() and "no text" not in ocr_text.lower():
                image_message += f"The image contains the following text:\n\n{ocr_text}\n\n"
            else:
                image_message += "The image doesn't contain any significant text.\n\n"
            
            image_message += "You can reference this image by name in our conversation."
            
            # Add the comprehensive message to the thread
            client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=image_message
            )
            
            logging.info(f"Added image analysis to thread {thread_id}")
            
            return JSONResponse(
                {
                    "message": "Image successfully processed and analyzed.",
                    "image_analyzed": True,
                    "file_type": "image"
                },
                status_code=200
            )
        
        # Check if there's a file_search resource for non-tabular files
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

        # For non-tabular and non-image files, upload to vector store
        with open(file_path, "rb") as file_stream:
            file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                vector_store_id=vector_store_id,
                files=[file_stream]
            )
            
        # If thread exists, add message about the uploaded document
        if thread_id:
            # Get a summary of the file for the message
            file_summary = await get_file_summary(file_path, file.filename)
            
            client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=f"I've uploaded a document named '{file.filename}'.\n\n{file_summary}\n\nYou can reference this document by name in our conversation."
            )
            
        logging.info(f"File uploaded to vector store: {file.filename}")
            
        # If context provided and thread exists, update context
        if context and thread_id:
            try:
                await update_context(client, thread_id, context)
            except Exception as e:
                logging.error(f"Error updating context in thread: {e}")
                # Continue even if context update fails

        return JSONResponse(
            {
                "message": "File successfully processed.",
                "image_analyzed": is_image,
                "file_type": "document"
            },
            status_code=200
        )

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
                instructions="You are a conversation assistant."
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
                instructions="You are a conversation assistant."
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
