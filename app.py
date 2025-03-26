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
        
        # Add OCR prompt to always extract text if visible
        default_prompt = (
            "Analyze this image thoroughly and provide a detailed description. "
            "If there's any text visible in the image, perform OCR and extract ALL text content word-for-word. "
            "For documents, receipts or invoices, list every field and value visible. "
            "Provide a comprehensive analysis including visual elements, text content, and overall context."
        )
        combined_prompt = f"{default_prompt} {prompt}" if prompt else default_prompt
        
        # Use the existing client instead of creating a new one
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user", 
                "content": [
                    {"type": "text", "text": combined_prompt},
                    {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}}
                ]
            }],
            max_tokens=1000  # Increased token limit for more detailed analysis
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

# Helper function to add file awareness to thread
async def add_file_awareness(client, thread_id, file_info):
    """Add information about uploaded files to the thread so the assistant is aware of them."""
    if not thread_id or not file_info:
        return
    
    try:
        file_type = file_info.get("type", "unknown")
        filename = file_info.get("filename", "unknown")
        
        # Create a message that informs the assistant about the available file
        message = f"FILE NOTIFICATION: A new file has been uploaded and is available: {filename} (Type: {file_type}). "
        
        if file_type == "csv":
            # Add CSV specific details
            columns = file_info.get("columns", [])
            shape = file_info.get("shape", (0, 0))
            message += f"This is a CSV file with {shape[0]} rows and {shape[1]} columns. "
            message += f"The columns are: {', '.join(columns)}. "
            message += "You have access to this file through the code_interpreter tool. When asked about this file, use code_interpreter to analyze it."
            
        elif file_type == "excel":
            # Add Excel specific details
            sheets = file_info.get("sheets", [])
            message += f"This is an Excel file with {len(sheets)} sheets: {', '.join(sheets)}. "
            message += "You have access to this file through the code_interpreter tool. When asked about this file, use code_interpreter to analyze it."
            
        elif file_type == "image":
            # Add image specific details
            analysis = file_info.get("analysis", "No analysis available")
            message += f"This is an image file. An analysis has been stored: {analysis}"
            
        else:
            message += "This file is available through file_search."
        
        # Add the message to the thread
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=message,
            metadata={"type": "file_notification"}
        )
        
        logging.info(f"Added file awareness for {filename} to thread {thread_id}")
    except Exception as e:
        logging.error(f"Error adding file awareness to thread: {e}")
        # Continue the flow even if this fails

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
    assistant_tool_resources = {"file_search": {"vector_store_ids": [vector_store.id]}, "code_interpreter": {"file_ids": []}}
    system_prompt = '''
        You are a highly skilled Product Management AI Assistant and Co-Pilot. Your primary responsibilities include generating comprehensive Product Requirements Documents (PRDs) and providing insightful answers to a wide range of product-related queries. You seamlessly integrate information from uploaded files and your extensive knowledge base to deliver contextually relevant and actionable insights.

        ### **File Handling Instructions:**

        1. **CSV and Excel Files:**
           - You have access to these files through the code_interpreter tool
           - When asked about data in these files, ALWAYS use code_interpreter to analyze them
           - Use the exact filename when referencing these files in your code
           - Users may refer to files by partial names or extensions - match them appropriately
        
        2. **Image Files:**
           - When users ask about images, provide the full analysis that was performed
           - Users may refer to images by partial filenames - match appropriately
           - Always include text extracted from images when present
        
        3. **Document Files:**
           - You can search these through the file_search tool
           - Reference specific sections and content when answering questions

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
        - When asked about CSV or Excel files, ALWAYS use the code_interpreter tool to analyze them
        - When users mention specific files by name or even partial names, identify the correct file and use it
        - Present your analysis results in a clear, organized manner
        - If users ask about data without specifying the file, infer which file they are referring to

        ### **Behavioral Guidelines:**

        - **File Awareness:**
        - Maintain awareness of all files that have been uploaded
        - When users refer to files by name (even partial names), understand which file they mean
        - If a user asks about a specific file, assume they want you to analyze it
        
        - **Contextual Awareness:**
        - Always consider the context provided by the uploaded files and previous interactions.
        - Adapt your responses based on the specific needs and preferences of the user.

        - **Proactive Insight Generation:**
        - Go beyond surface-level answers by providing deep insights, trends, and actionable recommendations.
        - Anticipate potential follow-up questions and address them preemptively where appropriate.

        - **Professional Tone:**
        - Maintain a professional, clear, and concise communication style.
        - Ensure all interactions are respectful, objective, and goal-oriented.

        - **Error Handling:**
        - If you cannot find a file the user mentions, ask them to clarify or re-upload it
        - Clearly indicate when you are making assumptions due to missing information

        By adhering to these guidelines, you will function as an effective Product Management AI Assistant, delivering high-quality insights and analysis.
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
            
        # Check file type and process accordingly
        file_info = {"filename": filename}
        
        # Check if it's a tabular file (CSV/Excel)
        if is_tabular_file(filename):
            # Process the file with the code interpreter directly
            try:
                # Process tabular data for metadata
                tabular_data = await process_tabular_data(file_path, filename)
                file_info.update(tabular_data)
                
                # Upload file for code interpreter
                with open(file_path, "rb") as file_stream:
                    file_obj = client.files.create(
                        file=file_stream,
                        purpose="assistants"
                    )
                
                # Get existing file IDs for code interpreter
                current_assistant = client.beta.assistants.retrieve(
                    assistant_id=assistant.id
                )
                
                code_interpreter_resources = current_assistant.tool_resources.code_interpreter if hasattr(current_assistant.tool_resources, 'code_interpreter') else None
                existing_file_ids = code_interpreter_resources.file_ids if code_interpreter_resources and hasattr(code_interpreter_resources, 'file_ids') else []
                
                # Update the assistant with the new file ID specifically for code_interpreter
                updated_file_ids = existing_file_ids + [file_obj.id]
                client.beta.assistants.update(
                    assistant_id=assistant.id,
                    tool_resources={
                        "file_search": {"vector_store_ids": [vector_store.id]},
                        "code_interpreter": {"file_ids": updated_file_ids}
                    }
                )
                
                # Add message about the file to make the assistant aware of it
                await add_file_awareness(client, thread.id, file_info)
                
                logging.info(f"Tabular file {filename} associated with code interpreter")
            except Exception as e:
                logging.error(f"Error processing tabular file: {e}")
        
        # Check if it's an image file
        elif is_image_file(filename):
            try:
                # Analyze the image
                with open(file_path, "rb") as file_stream:
                    file_content = file_stream.read()
                
                analysis_text = await image_analysis(client, file_content, filename)
                file_info.update({"type": "image", "analysis": analysis_text})
                
                # Add message about the image analysis
                await add_file_awareness(client, thread.id, file_info)
                
                # Also add to vector store if possible
                try:
                    with open(file_path, "rb") as file_stream:
                        file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                            vector_store_id=vector_store.id, 
                            files=[file_stream]
                        )
                except Exception as e:
                    logging.warning(f"Could not add image to vector store: {e}")
                
                logging.info(f"Image file {filename} analyzed and added to thread")
            except Exception as e:
                logging.error(f"Error processing image file: {e}")
                
        else:
            # Normal file upload to vector store for file_search
            with open(file_path, "rb") as file_stream:
                file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                    vector_store_id=vector_store.id, 
                    files=[file_stream]
                )
            file_info.update({"type": "document"})
            await add_file_awareness(client, thread.id, file_info)
            logging.info(f"Document file {filename} uploaded to vector store")

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
        
        ### **File Handling Instructions:**

        1. **CSV and Excel Files:**
           - You have access to these files through the code_interpreter tool
           - When asked about data in these files, ALWAYS use code_interpreter to analyze them
           - Use the exact filename when referencing these files in your code
           - Users may refer to files by partial names or extensions - match them appropriately
        
        2. **Image Files:**
           - When users ask about images, provide the full analysis that was performed
           - Users may refer to images by partial filenames - match appropriately
           - Always include text extracted from images when present
        
        3. **Document Files:**
           - You can search these through the file_search tool
           - Reference specific sections and content when answering questions
        '''
        
        instructions = base_prompt if not system_prompt else f"{base_prompt} {system_prompt}"
        
        # Update to include code interpreter capability
        assistant = client.beta.assistants.create(
            name="demo_co_pilot",
            model="gpt-4o-mini",
            instructions=instructions,
            tools=[{"type": "code_interpreter"}, {"type": "file_search"}],
            tool_resources={
                "file_search": {"vector_store_ids": [vector_store_id]},
                "code_interpreter": {"file_ids": []}
            },
        )
        assistant_id = assistant.id
    else:
        # If user gave an assistant, update instructions if needed
        if system_prompt:
            # Ensure file handling instructions are included
            file_instructions = '''
            ### **File Handling Instructions:**

            1. **CSV and Excel Files:**
               - You have access to these files through the code_interpreter tool
               - When asked about data in these files, ALWAYS use code_interpreter to analyze them
               - Use the exact filename when referencing these files in your code
               - Users may refer to files by partial names or extensions - match them appropriately
            
            2. **Image Files:**
               - When users ask about images, provide the full analysis that was performed
               - Users may refer to images by partial filenames - match appropriately
               - Always include text extracted from images when present
            
            3. **Document Files:**
               - You can search these through the file_search tool
               - Reference specific sections and content when answering questions
            '''
            
            client.beta.assistants.update(
                assistant_id=assistant_id,
                instructions=(
                    f"You are a product management AI assistant, a product co-pilot. {file_instructions} {system_prompt}"
                    if system_prompt
                    else f"You are a product management AI assistant, a product co-pilot. {file_instructions}"
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
                
                # Get existing code_interpreter file_ids
                code_interpreter_resource = getattr(assistant_obj.tool_resources, "code_interpreter", None)
                existing_file_ids = (
                    code_interpreter_resource.file_ids
                    if (code_interpreter_resource and hasattr(code_interpreter_resource, "file_ids"))
                    else []
                )
                
                existing_tools = assistant_obj.tools if assistant_obj.tools else []
                if not any(t["type"] == "file_search" for t in existing_tools):
                    existing_tools.append({"type": "file_search"})
                    
                # Ensure code_interpreter is in the tools
                if not any(t["type"] == "code_interpreter" for t in existing_tools):
                    existing_tools.append({"type": "code_interpreter"})
                    
                client.beta.assistants.update(
                    assistant_id=assistant_id,
                    tools=existing_tools,
                    tool_resources={
                        "file_search": {"vector_store_ids": [vector_store_id]},
                        "code_interpreter": {"file_ids": existing_file_ids}
                    },
                )

    # Handle file upload if present
    if file:
        filename = file.filename
        file_path = f"/tmp/{filename}"
        with open(file_path, "wb") as ftemp:
            file_content = await file.read()
            ftemp.write(file_content)
        
        # Prepare file info for awareness
        file_info = {"filename": filename}
            
        # Check if it's a tabular file (CSV/Excel)
        if is_tabular_file(filename):
            try:
                # Process tabular data for metadata
                tabular_data = await process_tabular_data(file_path, filename)
                file_info.update(tabular_data)
                
                # Upload file for code interpreter
                with open(file_path, "rb") as file_stream:
                    file_obj = client.files.create(
                        file=file_stream,
                        purpose="assistants"
                    )
                
                # Get existing file IDs for code interpreter
                current_assistant = client.beta.assistants.retrieve(
                    assistant_id=assistant_id
                )
                
                code_interpreter_resources = current_assistant.tool_resources.code_interpreter if hasattr(current_assistant.tool_resources, 'code_interpreter') else None
                existing_file_ids = code_interpreter_resources.file_ids if code_interpreter_resources and hasattr(code_interpreter_resources, 'file_ids') else []
                
                # Update the assistant with the new file ID specifically for code_interpreter
                updated_file_ids = existing_file_ids + [file_obj.id]
                client.beta.assistants.update(
                    assistant_id=assistant_id,
                    tool_resources={
                        "file_search": {"vector_store_ids": [vector_store_id]},
                        "code_interpreter": {"file_ids": updated_file_ids}
                    }
                )
                
                # If thread exists, add file awareness
                if thread_id:
                    await add_file_awareness(client, thread_id, file_info)
                
                logging.info(f"Tabular file {filename} associated with code interpreter")
            except Exception as e:
                logging.error(f"Error processing tabular file: {e}")
        
        # Check if it's an image file
        elif is_image_file(filename):
            try:
                # Analyze the image
                analysis_text = await image_analysis(client, file_content, filename)
                file_info.update({"type": "image", "analysis": analysis_text})
                
                # If thread exists, add file awareness
                if thread_id:
                    await add_file_awareness(client, thread_id, file_info)
                
                # Also add to vector store if possible
                try:
                    with open(file_path, "rb") as file_stream:
                        file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                            vector_store_id=vector_store_id,
                            files=[file_stream]
                        )
                except Exception as e:
                    logging.warning(f"Could not add image to vector store: {e}")
                
                logging.info(f"Image file {filename} analyzed and added to thread")
            except Exception as e:
                logging.error(f"Error processing image file: {e}")
        
        else:
            # Normal file upload to vector store for file_search
            with open(file_path, "rb") as file_stream:
                client.beta.vector_stores.file_batches.upload_and_poll(
                    vector_store_id=vector_store_id,
                    files=[file_stream]
                )
            
            file_info.update({"type": "document"})
            if thread_id:
                await add_file_awareness(client, thread_id, file_info)
            
            logging.info(f"Document file {filename} uploaded to vector store")

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
    Handles different file types appropriately (tabular files use code_interpreter, others use vector_store).
    """
    client = create_client()
    thread_id = session  # Rename for clarity

    try:
        # Save the uploaded file locally and get the data
        file_content = await file.read()
        file_path = f"/tmp/{file.filename}"
        with open(file_path, "wb") as temp_file:
            temp_file.write(file_content)
        
        # Prepare file info for awareness
        file_info = {"filename": file.filename}
            
        # Determine file type
        is_image = is_image_file(file.filename, file.content_type)
        is_tabular = is_tabular_file(file.filename)
        
        # Retrieve the assistant
        assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant)
        
        # Get vector store IDs (if any)
        file_search_resource = getattr(assistant_obj.tool_resources, "file_search", None)
        vector_store_ids = (
            file_search_resource.vector_store_ids
            if (file_search_resource and hasattr(file_search_resource, "vector_store_ids"))
            else []
        )

        # Get existing file IDs for code interpreter
        code_interpreter_resource = getattr(assistant_obj.tool_resources, "code_interpreter", None)
        existing_file_ids = (
            code_interpreter_resource.file_ids
            if (code_interpreter_resource and hasattr(code_interpreter_resource, "file_ids"))
            else []
        )
        
        # Create vector store if we don't have one
        if not vector_store_ids:
            vector_store = client.beta.vector_stores.create(name=f"Assistant_{assistant}_Store")
            vector_store_id = vector_store.id
            vector_store_ids = [vector_store_id]
        else:
            vector_store_id = vector_store_ids[0]
        
        # Process based on file type
        if is_tabular:
            # This is a CSV or Excel file - use code interpreter
            try:
                # Process file data for metadata
                tabular_data = await process_tabular_data(file_path, file.filename)
                file_info.update(tabular_data)
                
                # Upload file to OpenAI for code interpreter
                with open(file_path, "rb") as file_stream:
                    file_obj = client.files.create(
                        file=file_stream,
                        purpose="assistants"
                    )
                
                # Update the assistant with the new file ID specifically for code_interpreter
                updated_file_ids = existing_file_ids + [file_obj.id]
                client.beta.assistants.update(
                    assistant_id=assistant,
                    tool_resources={
                        "file_search": {"vector_store_ids": vector_store_ids},
                        "code_interpreter": {"file_ids": updated_file_ids}
                    }
                )
                
                # If we have a thread, add file awareness
                if thread_id:
                    await add_file_awareness(client, thread_id, file_info)
                
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
        
        # For images, analyze and add to thread
        if is_image:
            logging.info(f"Analyzing image file: {file.filename}")
            analysis_text = await image_analysis(client, file_content, file.filename, prompt)
            file_info.update({"type": "image", "analysis": analysis_text})
            
            # If thread exists, add the analysis to the thread
            if thread_id:
                await add_file_awareness(client, thread_id, file_info)
            
            logging.info(f"Added image analysis to thread {thread_id}")
            
            # Try to add to vector store if possible (will likely fail but attempt)
            try:
                with open(file_path, "rb") as file_stream:
                    try:
                        file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                            vector_store_id=vector_store_id,
                            files=[file_stream]
                        )
                    except Exception as e:
                        logging.warning(f"Could not add image to vector store: {e}")
            except Exception as e:
                logging.warning(f"Error with vector store for image: {e}")
            
            return JSONResponse(
                {
                    "message": "Image successfully analyzed and processed.",
                    "image_analyzed": True,
                    "file_type": "image"
                },
                status_code=200
            )
        
        # For other document types, use vector store
        # Ensure the 'file_search' tool is present in the assistant's tools
        existing_tools = assistant_obj.tools if assistant_obj.tools else []
        if not any(t["type"] == "file_search" for t in existing_tools):
            existing_tools.append({"type": "file_search"})
            client.beta.assistants.update(
                assistant_id=assistant,
                tools=existing_tools
            )

        # For document files, upload to vector store
        with open(file_path, "rb") as file_stream:
            file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                vector_store_id=vector_store_id,
                files=[file_stream]
            )
        
        file_info.update({"type": "document"})
        if thread_id:
            await add_file_awareness(client, thread_id, file_info)
        
        logging.info(f"Document file {file.filename} uploaded to vector store")
        
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
                instructions='''
                You are a conversation assistant.
                
                ### **File Handling Instructions:**

                1. **CSV and Excel Files:**
                   - You have access to these files through the code_interpreter tool
                   - When asked about data in these files, ALWAYS use code_interpreter to analyze them
                   - Use the exact filename when referencing these files in your code
                   - Users may refer to files by partial names or extensions - match them appropriately
                
                2. **Image Files:**
                   - When users ask about images, provide the full analysis that was performed
                   - Users may refer to images by partial filenames - match appropriately
                   - Always include text extracted from images when present
                
                3. **Document Files:**
                   - You can search these through the file_search tool
                   - Reference specific sections and content when answering questions
                ''',
                tools=[{"type": "code_interpreter"}, {"type": "file_search"}],
                tool_resources={"code_interpreter": {"file_ids": []}}
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
                
                ### **File Handling Instructions:**

                1. **CSV and Excel Files:**
                   - You have access to these files through the code_interpreter tool
                   - When asked about data in these files, ALWAYS use code_interpreter to analyze them
                   - Use the exact filename when referencing these files in your code
                   - Users may refer to files by partial names or extensions - match them appropriately
                
                2. **Image Files:**
                   - When users ask about images, provide the full analysis that was performed
                   - Users may refer to images by partial filenames - match appropriately
                   - Always include text extracted from images when present
                
                3. **Document Files:**
                   - You can search these through the file_search tool
                   - Reference specific sections and content when answering questions
                ''',
                tools=[{"type": "code_interpreter"}, {"type": "file_search"}],
                tool_resources={"code_interpreter": {"file_ids": []}}
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
    Cleans up both vector store files and code interpreter files.
    Accepts form data.
    """
    # Get parameters from form data
    form_data = await request.form()
    vector_store_id = form_data.get("vector_store_id")
    assistant_id = form_data.get("assistant_id")  # Added to handle code interpreter files
    
    if not vector_store_id and not assistant_id:
        raise HTTPException(status_code=400, detail="Either vector_store_id or assistant_id is required")
    
    client = create_client()
    vs_deleted_count = 0
    vs_skipped_count = 0
    ci_deleted_count = 0
    ci_skipped_count = 0
    
    # Get current time for age comparison
    now = datetime.datetime.now()
    
    # Process vector store files if vector_store_id is provided
    if vector_store_id:
        try:
            # Step 1: Get all files in the vector store
            file_batches = client.beta.vector_stores.file_batches.list(vector_store_id=vector_store_id)
            
            if file_batches.data:
                # Step 2: Process each file batch to find files older than 48 hours
                for batch in file_batches.data:
                    # Calculate age in hours
                    batch_created = datetime.datetime.fromtimestamp(batch.created_at)
                    batch_age_hours = (now - batch_created).total_seconds() / 3600
                    
                    # Skip recent batches
                    if batch_age_hours <= 48:
                        vs_skipped_count += 1
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
                            vs_deleted_count += 1
                        except Exception as e:
                            logging.error(f"Error deleting file {file.id} from vector store: {e}")
        except Exception as e:
            logging.error(f"Error cleaning up vector store files: {e}")
    
    # Process code interpreter files if assistant_id is provided
    if assistant_id:
        try:
            # Get current assistant
            assistant = client.beta.assistants.retrieve(assistant_id=assistant_id)
            
            # Check if it has code_interpreter tool resources
            code_interpreter_resource = getattr(assistant.tool_resources, "code_interpreter", None)
            if code_interpreter_resource and hasattr(code_interpreter_resource, "file_ids"):
                file_ids = code_interpreter_resource.file_ids
                
                # No files to process
                if not file_ids:
                    logging.info(f"No code interpreter files found for assistant {assistant_id}")
                else:
                    # Process each file to determine age
                    current_file_ids = []
                    for file_id in file_ids:
                        try:
                            # Get file details
                            file = client.files.retrieve(file_id=file_id)
                            
                            # Calculate age in hours
                            file_created = datetime.datetime.fromtimestamp(file.created_at)
                            file_age_hours = (now - file_created).total_seconds() / 3600
                            
                            # Skip recent files
                            if file_age_hours <= 48:
                                current_file_ids.append(file_id)
                                ci_skipped_count += 1
                                continue
                                
                            # Delete old files
                            try:
                                client.files.delete(file_id=file_id)
                                ci_deleted_count += 1
                            except Exception as e:
                                logging.error(f"Error deleting file {file_id} from code interpreter: {e}")
                                # Keep it in the list if deletion fails
                                current_file_ids.append(file_id)
                                
                        except Exception as e:
                            logging.error(f"Error processing file {file_id}: {e}")
                            # Keep it in the list if processing fails
                            current_file_ids.append(file_id)
                    
                    # Update the assistant with the remaining file IDs
                    if len(current_file_ids) != len(file_ids):
                        tool_resources = assistant.tool_resources
                        
                        # Get vector store IDs if they exist
                        file_search_resource = getattr(tool_resources, "file_search", None)
                        vector_store_ids = (
                            file_search_resource.vector_store_ids
                            if (file_search_resource and hasattr(file_search_resource, "vector_store_ids"))
                            else []
                        )
                        
                        # Update the assistant
                        client.beta.assistants.update(
                            assistant_id=assistant_id,
                            tool_resources={
                                "file_search": {"vector_store_ids": vector_store_ids},
                                "code_interpreter": {"file_ids": current_file_ids}
                            }
                        )
                        logging.info(f"Updated assistant {assistant_id} with {len(current_file_ids)} code interpreter files")
        except Exception as e:
            logging.error(f"Error cleaning up code interpreter files: {e}")
    
    return JSONResponse({
        "status": "File cleanup completed",
        "vector_store": {
            "vector_store_id": vector_store_id,
            "files_deleted": vs_deleted_count,
            "batches_skipped": vs_skipped_count
        },
        "code_interpreter": {
            "assistant_id": assistant_id,
            "files_deleted": ci_deleted_count,
            "files_skipped": ci_skipped_count
        }
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
