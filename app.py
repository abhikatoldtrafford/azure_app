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

# Dictionary to track assistant files
ASSISTANT_FILES = {}

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
            "If there's any text visible, include ALL TEXTUAL CONTENT using OCR. Describe:"
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

# Helper function to update the assistant's system prompt with file information
async def update_assistant_with_file_info(client, assistant_id, thread_id, file_info):
    """Updates the assistant's instructions with information about the files."""
    try:
        # Get current assistant
        assistant = client.beta.assistants.retrieve(assistant_id=assistant_id)
        current_instructions = assistant.instructions
        
        # Create file information text
        file_info_text = "\n\nYou have access to the following files:\n"
        for file_id, info in file_info.items():
            file_name = info.get("filename", "Unknown File")
            file_type = info.get("type", "Unknown")
            file_desc = info.get("description", "")
            
            file_info_text += f"- '{file_name}' (Type: {file_type})"
            if file_desc:
                file_info_text += f": {file_desc}"
            file_info_text += "\n"
        
        # Add usage instructions
        file_info_text += "\nWhen users refer to any of these files by name or partial name, you should be able to access and analyze them. "
        file_info_text += "For CSV and Excel files, use the code_interpreter tool to analyze the data. "
        file_info_text += "For images, refer to the image analysis that has been provided. "
        file_info_text += "Always acknowledge the existence of these files if asked, and be ready to analyze their contents."
        
        # Update assistant instructions
        if "You have access to the following files:" not in current_instructions:
            new_instructions = current_instructions + file_info_text
            client.beta.assistants.update(
                assistant_id=assistant_id,
                instructions=new_instructions
            )
            
        # Also add a message to the thread about the files
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=f"I've uploaded the following files that you can access:\n{file_info_text}",
            metadata={"type": "file_information"}
        )
        
        logging.info(f"Updated assistant with file information")
        return True
    except Exception as e:
        logging.error(f"Error updating assistant with file info: {e}")
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
        - Use the code_interpreter tool to analyze CSV, Excel, and other data files.
        - When users ask about a file or its contents, assume they want you to analyze it.
        - Perform data analysis including statistical summaries, visualizations, and relevant insights.
        - Present your analysis results in a clear, organized manner.

        4. **Image Analysis:**
        - When users upload images, you will have access to detailed descriptions and any text extracted via OCR.
        - When users ask about an image by name or reference, provide the analysis without disclaimers about not being able to see images.
        - Respond as if you can see and understand the images directly.

        ### **File Handling:**
        - You will be informed about all files uploaded and have access to them.
        - Always acknowledge that you have access to files when users mention them by name.
        - Use the appropriate tool (code_interpreter or file_search) based on file type.
        - For CSV and Excel files, use code_interpreter.
        - For images, refer to the image analysis provided.
        - For documents (PDF, TXT, etc.), use the file_search tool.

        ### **Behavioral Guidelines:**

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
        - Efficiently transition between PRD generation and generic question answering based on user prompts.
        - Recognize when a query is outside the scope of the uploaded files and adjust your response accordingly without prompting the user.

        - **Continuous Improvement:**
        - Learn from each interaction to enhance future responses.
        - Seek feedback when necessary to better align with the user's expectations and requirements.

        ### **Important Notes:**

        - **Tool Utilization:**
        - Always evaluate whether the file_search tool or code_interpreter can enhance the quality of your response before using it.
        - Use the appropriate tool based on file type and query.
        
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

    # Initialize file tracking for this assistant
    ASSISTANT_FILES[assistant.id] = {}

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
            
        # All files (CSV, Excel, images, etc.) will now be uploaded for code_interpreter
        try:
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
            
            # Track the file
            file_info = {
                "filename": filename,
                "file_id": file_obj.id,
                "type": "tabular" if is_tabular_file(filename) else "image" if is_image_file(filename) else "document"
            }
            
            ASSISTANT_FILES[assistant.id][file_obj.id] = file_info
            
            # If it's an image, also perform image analysis and add to thread
            if is_image_file(filename):
                with open(file_path, "rb") as img_file:
                    image_content = img_file.read()
                analysis_text = await image_analysis(client, image_content, filename)
                
                # Add analysis to file info
                file_info["description"] = "Image analysis available"
                file_info["analysis"] = analysis_text
                
                # Send the analysis to the thread
                client.beta.threads.messages.create(
                    thread_id=thread.id,
                    role="user",
                    content=f"Image Analysis for {filename}:\n\n{analysis_text}",
                    metadata={"type": "image_analysis", "filename": filename}
                )
            
            # If it's a tabular file, add a message prompting analysis
            elif is_tabular_file(filename):
                # Process file to get structure info
                tabular_data = await process_tabular_data(file_path, filename)
                
                if tabular_data.get("type") == "csv":
                    file_info["description"] = f"CSV file with {tabular_data.get('shape', (0, 0))[0]} rows and {tabular_data.get('shape', (0, 0))[1]} columns"
                    file_info["columns"] = tabular_data.get("columns", [])
                    
                    # Create a message to analyze the file
                    client.beta.threads.messages.create(
                        thread_id=thread.id,
                        role="user",
                        content=f"I've uploaded a CSV file named '{filename}' with {tabular_data.get('shape', (0, 0))[0]} rows and {tabular_data.get('shape', (0, 0))[1]} columns. The columns are: {', '.join(tabular_data.get('columns', []))}.",
                        metadata={"type": "file_info", "filename": filename}
                    )
                    
                elif tabular_data.get("type") == "excel":
                    sheets = tabular_data.get("sheets", [])
                    file_info["description"] = f"Excel file with {len(sheets)} sheets: {', '.join(sheets)}"
                    file_info["sheets"] = sheets
                    
                    # Create a message to analyze the file
                    sheet_info = []
                    for sheet in sheets:
                        sheet_data = tabular_data.get("sheets_data", {}).get(sheet, {})
                        shape = sheet_data.get("shape", (0, 0))
                        columns = sheet_data.get("columns", [])
                        sheet_info.append(f"Sheet '{sheet}': {shape[0]} rows, {shape[1]} columns")
                    
                    client.beta.threads.messages.create(
                        thread_id=thread.id,
                        role="user",
                        content=f"I've uploaded an Excel file named '{filename}' with {len(sheets)} sheets: {', '.join(sheets)}.\n\n{chr(10).join(sheet_info)}",
                        metadata={"type": "file_info", "filename": filename}
                    )
            else:
                # For text and other documents, create a simpler message
                client.beta.threads.messages.create(
                    thread_id=thread.id,
                    role="user",
                    content=f"I've uploaded a document file named '{filename}'.",
                    metadata={"type": "file_info", "filename": filename}
                )
                
                # Also add to vector store for file_search
                with open(file_path, "rb") as doc_stream:
                    file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                        vector_store_id=vector_store.id, 
                        files=[doc_stream]
                    )
                
                file_info["description"] = "Document accessible via file_search"
                
            # Update the assistant with file information
            await update_assistant_with_file_info(client, assistant.id, thread.id, ASSISTANT_FILES[assistant.id])
                
            logging.info(f"File {filename} uploaded and associated with assistant")
            
        except Exception as e:
            logging.error(f"Error processing file {filename}: {e}")
            # Continue with the request even if file processing fails

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
        base_prompt = """You are a product management AI assistant, a product co-pilot.
        
        You have access to various files that users upload, including CSV, Excel, images, and documents.
        When users refer to files by name, you should acknowledge that you have access to them and use the appropriate tools to analyze their contents.
        
        For CSV and Excel files, use the code_interpreter tool to perform data analysis.
        For images, refer to the image analysis that has been provided to you.
        For documents, use the file_search tool to find relevant information.
        
        Always act as if you have direct access to the files and can see/understand their contents.
        """
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
        
        # Initialize file tracking for this assistant
        ASSISTANT_FILES[assistant_id] = {}
    else:
        # If user gave an assistant, update instructions if needed
        if system_prompt:
            # Get current instructions
            assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant_id)
            current_instructions = assistant_obj.instructions
            
            # Update instructions while preserving file info section
            file_info_section = ""
            if "You have access to the following files:" in current_instructions:
                parts = current_instructions.split("You have access to the following files:")
                file_info_section = "You have access to the following files:" + parts[1]
            
            base_prompt = """You are a product management AI assistant, a product co-pilot.
            
            You have access to various files that users upload, including CSV, Excel, images, and documents.
            When users refer to files by name, you should acknowledge that you have access to them and use the appropriate tools to analyze their contents.
            
            For CSV and Excel files, use the code_interpreter tool to perform data analysis.
            For images, refer to the image analysis that has been provided to you.
            For documents, use the file_search tool to find relevant information.
            
            Always act as if you have direct access to the files and can see/understand their contents.
            """
            
            new_instructions = f"{base_prompt} {system_prompt}" if system_prompt else base_prompt
            
            if file_info_section:
                new_instructions += "\n\n" + file_info_section
            
            client.beta.assistants.update(
                assistant_id=assistant_id,
                instructions=new_instructions
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
        
        # Make sure we have a file tracking entry for this assistant
        if assistant_id not in ASSISTANT_FILES:
            ASSISTANT_FILES[assistant_id] = {}

    # If no thread, create one
    if not thread_id:
        thread = client.beta.threads.create()
        thread_id = thread.id

    # Handle file upload if present
    if file:
        filename = file.filename
        file_path = f"/tmp/{file.filename}"
        with open(file_path, "wb") as ftemp:
            file_content = await file.read()
            ftemp.write(file_content)
            
        # All files will be processed through code_interpreter now
        try:
            # Upload file for code interpreter
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
            
            # Track the file
            file_info = {
                "filename": filename,
                "file_id": file_obj.id,
                "type": "tabular" if is_tabular_file(filename) else "image" if is_image_file(filename) else "document"
            }
            
            ASSISTANT_FILES[assistant_id][file_obj.id] = file_info
            
            # If it's an image, also perform image analysis and add to thread
            if is_image_file(filename):
                with open(file_path, "rb") as img_file:
                    image_content = img_file.read()
                analysis_text = await image_analysis(client, image_content, filename)
                
                # Add analysis to file info
                file_info["description"] = "Image analysis available"
                file_info["analysis"] = analysis_text
                
                # Send the analysis to the thread
                client.beta.threads.messages.create(
                    thread_id=thread_id,
                    role="user",
                    content=f"Image Analysis for {filename}:\n\n{analysis_text}",
                    metadata={"type": "image_analysis", "filename": filename}
                )
            
            # If it's a tabular file, add a message prompting analysis
            elif is_tabular_file(filename):
                # Process file to get structure info
                tabular_data = await process_tabular_data(file_path, filename)
                
                if tabular_data.get("type") == "csv":
                    file_info["description"] = f"CSV file with {tabular_data.get('shape', (0, 0))[0]} rows and {tabular_data.get('shape', (0, 0))[1]} columns"
                    file_info["columns"] = tabular_data.get("columns", [])
                    
                    # Create a message to analyze the file
                    client.beta.threads.messages.create(
                        thread_id=thread_id,
                        role="user",
                        content=f"I've uploaded a CSV file named '{filename}' with {tabular_data.get('shape', (0, 0))[0]} rows and {tabular_data.get('shape', (0, 0))[1]} columns. The columns are: {', '.join(tabular_data.get('columns', []))}.",
                        metadata={"type": "file_info", "filename": filename}
                    )
                    
                elif tabular_data.get("type") == "excel":
                    sheets = tabular_data.get("sheets", [])
                    file_info["description"] = f"Excel file with {len(sheets)} sheets: {', '.join(sheets)}"
                    file_info["sheets"] = sheets
                    
                    # Create a message to analyze the file
                    sheet_info = []
                    for sheet in sheets:
                        sheet_data = tabular_data.get("sheets_data", {}).get(sheet, {})
                        shape = sheet_data.get("shape", (0, 0))
                        columns = sheet_data.get("columns", [])
                        sheet_info.append(f"Sheet '{sheet}': {shape[0]} rows, {shape[1]} columns")
                    
                    client.beta.threads.messages.create(
                        thread_id=thread_id,
                        role="user",
                        content=f"I've uploaded an Excel file named '{filename}' with {len(sheets)} sheets: {', '.join(sheets)}.\n\n{chr(10).join(sheet_info)}",
                        metadata={"type": "file_info", "filename": filename}
                    )
            else:
                # For text and other documents, create a simpler message
                client.beta.threads.messages.create(
                    thread_id=thread_id,
                    role="user",
                    content=f"I've uploaded a document file named '{filename}'.",
                    metadata={"type": "file_info", "filename": filename}
                )
                
                # Also add to vector store for file_search
                with open(file_path, "rb") as doc_stream:
                    file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                        vector_store_id=vector_store_id, 
                        files=[doc_stream]
                    )
                
                file_info["description"] = "Document accessible via file_search"
                
            # Update the assistant with file information
            await update_assistant_with_file_info(client, assistant_id, thread_id, ASSISTANT_FILES[assistant_id])
                
            logging.info(f"File {filename} uploaded and associated with assistant")
            
        except Exception as e:
            logging.error(f"Error processing file {filename}: {e}")
            # Continue with the request even if file processing fails

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
            "session": thread_id
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
    Handles different file types appropriately and ensures the assistant is aware of them.
    """
    client = create_client()
    thread_id = session  # Rename for clarity

    try:
        # Save the uploaded file locally and get the data
        file_content = await file.read()
        file_path = f"/tmp/{file.filename}"
        with open(file_path, "wb") as temp_file:
            temp_file.write(file_content)
        
        # Make sure we're tracking files for this assistant
        if assistant not in ASSISTANT_FILES:
            ASSISTANT_FILES[assistant] = {}
            
        # Create a thread if none provided
        if not thread_id:
            thread = client.beta.threads.create()
            thread_id = thread.id
            
        # Upload file for code interpreter (all file types now)
        with open(file_path, "rb") as file_stream:
            file_obj = client.files.create(
                file=file_stream,
                purpose="assistants"
            )
        
        # Associate the file with the assistant for code interpreter
        client.beta.assistants.files.create(
            assistant_id=assistant,
            file_id=file_obj.id
        )
        
        # Track the file
        filename = file.filename
        is_image = is_image_file(filename, file.content_type)
        is_tabular = is_tabular_file(filename)
        
        file_info = {
            "filename": filename,
            "file_id": file_obj.id,
            "type": "tabular" if is_tabular else "image" if is_image else "document"
        }
        
        ASSISTANT_FILES[assistant][file_obj.id] = file_info
        
        # If it's an image, also perform image analysis and add to thread
        if is_image:
            with open(file_path, "rb") as img_file:
                image_content = img_file.read()
            analysis_text = await image_analysis(client, image_content, filename, prompt)
            
            # Add analysis to file info
            file_info["description"] = "Image analysis available"
            file_info["analysis"] = analysis_text
            
            # Send the analysis to the thread
            client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=f"Image Analysis for {filename}:\n\n{analysis_text}",
                metadata={"type": "image_analysis", "filename": filename}
            )
            
            logging.info(f"Added image analysis to thread {thread_id}")
        
        # If it's a tabular file, add a message prompting analysis
        elif is_tabular:
            # Process file to get structure info
            tabular_data = await process_tabular_data(file_path, filename)
            
            if tabular_data.get("type") == "csv":
                file_info["description"] = f"CSV file with {tabular_data.get('shape', (0, 0))[0]} rows and {tabular_data.get('shape', (0, 0))[1]} columns"
                file_info["columns"] = tabular_data.get("columns", [])
                
                # Create a message to analyze the file
                client.beta.threads.messages.create(
                    thread_id=thread_id,
                    role="user",
                    content=f"I've uploaded a CSV file named '{filename}' with {tabular_data.get('shape', (0, 0))[0]} rows and {tabular_data.get('shape', (0, 0))[1]} columns. The columns are: {', '.join(tabular_data.get('columns', []))}.",
                    metadata={"type": "file_info", "filename": filename}
                )
                
            elif tabular_data.get("type") == "excel":
                sheets = tabular_data.get("sheets", [])
                file_info["description"] = f"Excel file with {len(sheets)} sheets: {', '.join(sheets)}"
                file_info["sheets"] = sheets
                
                # Create a message to analyze the file
                sheet_info = []
                for sheet in sheets:
                    sheet_data = tabular_data.get("sheets_data", {}).get(sheet, {})
                    shape = sheet_data.get("shape", (0, 0))
                    columns = sheet_data.get("columns", [])
                    sheet_info.append(f"Sheet '{sheet}': {shape[0]} rows, {shape[1]} columns")
                
                client.beta.threads.messages.create(
                    thread_id=thread_id,
                    role="user",
                    content=f"I've uploaded an Excel file named '{filename}' with {len(sheets)} sheets: {', '.join(sheets)}.\n\n{chr(10).join(sheet_info)}",
                    metadata={"type": "file_info", "filename": filename}
                )
        else:
            # Retrieve the assistant to check for vector_store
            assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant)
            file_search_resource = getattr(assistant_obj.tool_resources, "file_search", None)
            vector_store_ids = (
                file_search_resource.vector_store_ids
                if (file_search_resource and hasattr(file_search_resource, "vector_store_ids"))
                else []
            )
            
            # If vector store exists, also add document to it for file_search
            if vector_store_ids:
                vector_store_id = vector_store_ids[0]
                
                # For text and other documents, add to vector store too
                with open(file_path, "rb") as doc_stream:
                    file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                        vector_store_id=vector_store_id, 
                        files=[doc_stream]
                    )
                
                file_info["description"] = "Document accessible via file_search"
                
                # Create a message about the document
                client.beta.threads.messages.create(
                    thread_id=thread_id,
                    role="user",
                    content=f"I've uploaded a document file named '{filename}'.",
                    metadata={"type": "file_info", "filename": filename}
                )
            else:
                # Create a new vector store and add it to the assistant
                vector_store = client.beta.vector_stores.create(name=f"Assistant_{assistant}_Store")
                vector_store_id = vector_store.id
                
                # Add document to vector store
                with open(file_path, "rb") as doc_stream:
                    file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                        vector_store_id=vector_store_id, 
                        files=[doc_stream]
                    )
                
                # Ensure the assistant has the file_search tool with this vector store
                existing_tools = assistant_obj.tools if assistant_obj.tools else []
                if not any(t["type"] == "file_search" for t in existing_tools):
                    existing_tools.append({"type": "file_search"})
                
                client.beta.assistants.update(
                    assistant_id=assistant,
                    tools=existing_tools,
                    tool_resources={
                        "file_search": {
                            "vector_store_ids": [vector_store_id]
                        }
                    }
                )
                
                file_info["description"] = "Document accessible via file_search"
                
                # Create a message about the document
                client.beta.threads.messages.create(
                    thread_id=thread_id,
                    role="user",
                    content=f"I've uploaded a document file named '{filename}'.",
                    metadata={"type": "file_info", "filename": filename}
                )
        
        # Update the assistant with file information
        await update_assistant_with_file_info(client, assistant, thread_id, ASSISTANT_FILES[assistant])
        
        # If context provided, update context
        if context:
            try:
                await update_context(client, thread_id, context)
            except Exception as e:
                logging.error(f"Error updating context in thread: {e}")
                # Continue even if context update fails

        return JSONResponse(
            {
                "message": "File successfully processed.",
                "image_analyzed": is_image,
                "file_type": "image" if is_image else "tabular" if is_tabular else "document",
                "session": thread_id
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
    Handles conversation queries with streaming response.
    Preserves the original query parameters and output format.
    """
    client = create_client()

    try:
        # If no assistant or session provided, create them (same fallback approach)
        if not assistant:
            assistant_obj = client.beta.assistants.create(
                name="conversation_assistant",
                model="gpt-4o-mini",
                instructions="You are a conversation assistant.",
                tools=[{"type": "code_interpreter"}]
            )
            assistant = assistant_obj.id
            
            # Initialize file tracking
            if assistant not in ASSISTANT_FILES:
                ASSISTANT_FILES[assistant] = {}

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

        # Create and run the assistant with attachments
        run = client.beta.threads.runs.create(
            thread_id=session,
            assistant_id=assistant
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
    Handles conversation queries with non-streaming response.
    Preserves the original query parameters and output format.
    """
    client = create_client()

    try:
        # If no assistant or session provided, create them
        if not assistant:
            assistant_obj = client.beta.assistants.create(
                name="chat_assistant",
                model="gpt-4o-mini",
                instructions="You are a conversation assistant.",
                tools=[{"type": "code_interpreter"}]
            )
            assistant = assistant_obj.id
            
            # Initialize file tracking
            if assistant not in ASSISTANT_FILES:
                ASSISTANT_FILES[assistant] = {}

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

        # Create and run the assistant with attachments
        run = client.beta.threads.runs.create(
            thread_id=session,
            assistant_id=assistant
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
        
        # Also clean up assistant files
        for assistant_id, file_dict in ASSISTANT_FILES.items():
            assistant_files = client.beta.assistants.files.list(assistant_id=assistant_id)
            for file in assistant_files.data:
                file_id = file.id
                
                # Check if this is an old file (older than 48 hours)
                created_timestamp = file.created_at
                created_date = datetime.datetime.fromtimestamp(created_timestamp)
                file_age_hours = (now - created_date).total_seconds() / 3600
                
                if file_age_hours > 48:
                    try:
                        # Delete from the assistant
                        client.beta.assistants.files.delete(
                            assistant_id=assistant_id,
                            file_id=file_id
                        )
                        
                        # Also delete the file itself
                        client.files.delete(file_id=file_id)
                        
                        # Remove from tracking
                        if file_id in ASSISTANT_FILES[assistant_id]:
                            del ASSISTANT_FILES[assistant_id][file_id]
                            
                        deleted_count += 1
                    except Exception as e:
                        logging.error(f"Error deleting assistant file {file_id}: {e}")
        
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
