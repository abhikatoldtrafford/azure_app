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
            "If there's any text visible, include all the textual content. Describe:"
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
            max_tokens=500
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

# Function to add file awareness to the assistant
async def add_file_awareness(client, thread_id, file_info):
    """Adds file awareness to the assistant by sending a message about the file."""
    if not file_info:
        return

    try:
        # Create a message that informs the assistant about the file
        file_type = file_info.get("type", "unknown")
        file_name = file_info.get("name", "unnamed_file")
        file_id = file_info.get("id", "")
        processing_method = file_info.get("processing_method", "")
        
        awareness_message = f"FILE INFORMATION: A file named '{file_name}' of type '{file_type}' has been uploaded. "
        
        if file_type in ["csv", "excel"]:
            awareness_message += f"This file is available for analysis using the code interpreter."
            if file_type == "excel":
                awareness_message += " This is an Excel file with potentially multiple sheets."
        elif file_type == "image":
            awareness_message += "This image has been analyzed and the content has been added to this thread."
        else:
            awareness_message += "This file has been added to the vector store and is available for search."
        
        # Add specific instructions for Excel/CSV handling
        if file_type in ["csv", "excel"]:
            awareness_message += "\n\nWhen analyzing this file, follow these instructions:\n"
            awareness_message += """
**File Handling:**
1. If receiving Excel (.xlsx/.xls):
   - Read ALL sheets using: `df_dict = pd.read_excel(file_path, sheet_name=None)`
   - Convert each sheet to CSV named: `<original_filename>_<sheet_name>.csv` (e.g., "sales.xlsx" → "sales_Orders.csv", "sales_Clients.csv")
   - Always reference both original file and sheet name in analysis

2. If receiving CSV:
   - Use directly for analysis
   - Preserve original filename in references

**Analysis Requirements:**
- Start with data overview: shape, columns, missing values
- Perform sheet-specific analysis for Excel files
- Compare trends across sheets when applicable
- Generate visualizations with clear source identification
- Include code snippets with explanations

**Output Formatting:**
- Begin with: "Analyzing [file.csv] / [sheet] from [file.xlsx]"
- Use markdown tables for key statistics
- Place visualizations under clear headings
- Separate analysis per sheet/file with horizontal rules
"""

        # Send the message to the thread
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=awareness_message,
            metadata={"type": "file_awareness"}
        )
        
        logging.info(f"Added file awareness for {file_name} to thread {thread_id}")
    except Exception as e:
        logging.error(f"Error adding file awareness: {e}")
        # Continue the flow even if adding awareness fails

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
    context = form.get("context", None)  # New: Get optional context parameter

    # Create a vector store up front
    vector_store = client.beta.vector_stores.create(name="demo")

    # Always include code_interpreter and file_search tools
    assistant_tools = [{"type": "code_interpreter"}, {"type": "file_search"}]
    assistant_tool_resources = {"file_search": {"vector_store_ids": [vector_store.id]}}
    
    # Initialize empty file_ids list for code_interpreter
    code_interpreter_file_ids = []
    assistant_tool_resources["code_interpreter"] = {"file_ids": code_interpreter_file_ids}
    
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
        - When users upload CSV or Excel files, you can analyze them using the code_interpreter tool.
        - For Excel files, remember to examine all sheets and provide comprehensive analysis.
        - Generate visualizations and statistics to help users understand their data.
        - Explain your analysis approach and findings clearly.

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
        - Always evaluate whether the file_search tool can enhance the quality of your response before using it.
        - Use code_interpreter for data analysis when working with CSV or Excel files.
        
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
        file_content = await file.read()
        with open(file_path, 'wb') as f:
            f.write(file_content)

        # Determine file type
        file_ext = os.path.splitext(filename)[1].lower()
        
        # Check if it's CSV or Excel
        is_csv = file_ext == '.csv'
        is_excel = file_ext in ['.xlsx', '.xls', '.xlsm']
        is_image = file_ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']
        is_pdf = file_ext in ['.pdf', '.doc', '.docx', '.txt']  # Explicitly identify document types for vector store
        
        if is_csv or is_excel:
            # Upload to code interpreter
            with open(file_path, "rb") as file_stream:
                uploaded_file = client.files.create(
                    file=file_stream,
                    purpose='assistants'
                )
            
            # Add file to code interpreter resource
            code_interpreter_file_ids.append(uploaded_file.id)
            client.beta.assistants.update(
                assistant_id=assistant.id,
                tool_resources={
                    "code_interpreter": {"file_ids": code_interpreter_file_ids},
                    "file_search": {"vector_store_ids": [vector_store.id]}
                }
            )
            
            # Add file awareness to the thread
            file_info = {
                "type": "csv" if is_csv else "excel",
                "name": filename,
                "id": uploaded_file.id,
                "processing_method": "code_interpreter"
            }
            await add_file_awareness(client, thread.id, file_info)
            
            logging.info(f"Added {filename} to code interpreter with file_id: {uploaded_file.id}")
        elif is_image:
            # Process image and add to thread
            analysis_text = await image_analysis(client, file_content, filename, None)
            
            # Add the analysis to the thread
            client.beta.threads.messages.create(
                thread_id=thread.id,
                role="user",
                content=f"Image Analysis for {filename}: {analysis_text}"
            )
            
            # Add file awareness
            file_info = {
                "type": "image",
                "name": filename,
                "processing_method": "thread_message"
            }
            await add_file_awareness(client, thread.id, file_info)
            
            logging.info(f"Added image analysis for {filename} to thread")
        elif is_pdf or not (is_csv or is_excel or is_image):
            # Upload to vector store for document types and any other non-CSV/Excel/Image files
            with open(file_path, "rb") as file_stream:
                file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                    vector_store_id=vector_store.id, 
                    files=[file_stream]
                )
            
            # Add file awareness
            file_info = {
                "type": file_ext[1:] if file_ext else "unknown",
                "name": filename,
                "processing_method": "vector_store"
            }
            await add_file_awareness(client, thread.id, file_info)
            
            logging.info(f"File uploaded to vector store: status={file_batch.status}, count={file_batch.file_counts}")

    res = {
        "assistant": assistant.id,
        "session": thread.id,
        "vector_store": vector_store.id
    }

    return JSONResponse(res, media_type="application/json", status_code=200)


@app.post("/co-pilot")
async def co_pilot(request: Request, **kwargs):
    """
    Handles co-pilot creation or updates with optional file upload and system prompt.
    """
    client = create_client()
    
    # Parse the form data
    form = await request.form()
    file = form.get("file", None)
    system_prompt = form.get("system_prompt", None)
    context = form.get("context", None)  # New: Get optional context parameter

    # Attempt to get the assistant & vector store from the form
    assistant_id = form.get("assistant", None)
    vector_store_id = form.get("vector_store", None)
    thread_id = form.get("session", None)

    # Initialize code_interpreter file_ids
    code_interpreter_file_ids = []
    
    # If no assistant, create one
    if not assistant_id:
        if not vector_store_id:
            vector_store = client.beta.vector_stores.create(name="demo")
            vector_store_id = vector_store.id
        base_prompt = "You are a product management AI assistant, a product co-pilot."
        instructions = base_prompt if not system_prompt else f"{base_prompt} {system_prompt}"
        assistant = client.beta.assistants.create(
            name="demo_co_pilot",
            model="gpt-4o-mini",
            instructions=instructions,
            tools=[{"type": "code_interpreter"}, {"type": "file_search"}],
            tool_resources={
                "file_search": {"vector_store_ids": [vector_store_id]},
                "code_interpreter": {"file_ids": code_interpreter_file_ids}
            },
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
                if not any(t["type"] == "code_interpreter" for t in existing_tools):
                    existing_tools.append({"type": "code_interpreter"})
                
                # Get existing code_interpreter file_ids if any
                code_interpreter_resource = getattr(assistant_obj.tool_resources, "code_interpreter", None)
                if code_interpreter_resource and hasattr(code_interpreter_resource, "file_ids"):
                    code_interpreter_file_ids = code_interpreter_resource.file_ids
                
                client.beta.assistants.update(
                    assistant_id=assistant_id,
                    tools=existing_tools,
                    tool_resources={
                        "file_search": {"vector_store_ids": [vector_store_id]},
                        "code_interpreter": {"file_ids": code_interpreter_file_ids}
                    },
                )

    # Handle file upload if present
    if file:
        file_content = await file.read()
        file_path = f"/tmp/{file.filename}"
        with open(file_path, "wb") as ftemp:
            ftemp.write(file_content)
        
        # Determine file type
        file_ext = os.path.splitext(file.filename)[1].lower()
        
        # Check if it's CSV or Excel
        is_csv = file_ext == '.csv'
        is_excel = file_ext in ['.xlsx', '.xls', '.xlsm']
        is_image = file_ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']
        
        if is_csv or is_excel:
            # Upload to code interpreter
            with open(file_path, "rb") as file_stream:
                uploaded_file = client.files.create(
                    file=file_stream,
                    purpose='assistants'
                )
            
                            # Get the assistant to update its code_interpreter file_ids
            assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant_id)
            tool_resources = assistant_obj.tool_resources
            code_interpreter_file_ids = []
            
            if hasattr(tool_resources, "code_interpreter") and tool_resources.code_interpreter is not None:
                if hasattr(tool_resources.code_interpreter, "file_ids"):
                    code_interpreter_file_ids = list(tool_resources.code_interpreter.file_ids)
            
            # Add the new file
            code_interpreter_file_ids.append(uploaded_file.id)
            
            # Update the assistant
            client.beta.assistants.update(
                assistant_id=assistant_id,
                tool_resources={
                    "code_interpreter": {"file_ids": code_interpreter_file_ids},
                    "file_search": {"vector_store_ids": [vector_store_id] if vector_store_id else []}
                }
            )
            
            # Add file awareness if thread_id exists
            if thread_id:
                file_info = {
                    "type": "csv" if is_csv else "excel",
                    "name": file.filename,
                    "id": uploaded_file.id,
                    "processing_method": "code_interpreter"
                }
                await add_file_awareness(client, thread_id, file_info)
            
            logging.info(f"Added {file.filename} to code interpreter with file_id: {uploaded_file.id}")
        elif is_image and thread_id:
            # Process image and add to thread
            analysis_text = await image_analysis(client, file_content, file.filename, None)
            
            # Add the analysis to the thread
            client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=f"Image Analysis for {file.filename}: {analysis_text}"
            )
            
            # Add file awareness
            if thread_id:
                file_info = {
                    "type": "image",
                    "name": file.filename,
                    "processing_method": "thread_message"
                }
                await add_file_awareness(client, thread_id, file_info)
            
            logging.info(f"Added image analysis for {file.filename} to thread")
        elif is_pdf or not (is_csv or is_excel or is_image):
            # Upload to vector store for document types and any other non-CSV/Excel/Image files
            with open(file_path, "rb") as file_stream:
                client.beta.vector_stores.file_batches.upload_and_poll(
                    vector_store_id=vector_store_id,
                    files=[file_stream]
                )
            
            # Add file awareness if thread_id exists
            if thread_id:
                file_info = {
                    "type": file_ext[1:] if file_ext else "unknown",
                    "name": file.filename,
                    "processing_method": "vector_store"
                }
                await add_file_awareness(client, thread_id, file_info)

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
async def upload_file(file: UploadFile = Form(...), assistant: str = Form(...)):
    """
    Uploads a file and associates it with the given assistant.
    Handles different file types appropriately:
    - CSV and Excel files are sent to code interpreter
    - Images are analyzed and added to the thread
    - Other files are sent to vector store
    """
    client = create_client()
    # Get context if provided (in form data)
    form = await request.form()
    context = form.get("context", None)
    thread_id = form.get("session", None)
    prompt = form.get("prompt", None)  # Optional prompt for image analysis

    try:
        # Save the uploaded file locally and get the data
        file_content = await file.read()
        file_path = f"/tmp/{file.filename}"
        with open(file_path, "wb") as temp_file:
            temp_file.write(file_content)
            
        # Determine file type
        file_ext = os.path.splitext(file.filename.lower())[1]
        is_csv = file_ext == '.csv'
        is_excel = file_ext in ['.xlsx', '.xls', '.xlsm']
        is_image = file_ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'] or (file.content_type and file.content_type.startswith('image/'))
        is_pdf = file_ext in ['.pdf', '.doc', '.docx', '.txt']  # Explicitly identify document types for vector store
        
        # Retrieve the assistant
        assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant)
        
        # Check if code_interpreter tool is present
        has_code_interpreter = any(t["type"] == "code_interpreter" for t in assistant_obj.tools)
        
        # If not present and we need it for CSV/Excel, add it
        if (is_csv or is_excel) and not has_code_interpreter:
            existing_tools = assistant_obj.tools if assistant_obj.tools else []
            existing_tools.append({"type": "code_interpreter"})
            client.beta.assistants.update(
                assistant_id=assistant,
                tools=existing_tools
            )
            logging.info(f"Added code_interpreter tool to assistant {assistant}")
        
        # Check if there's a file_search resource for vector store files
        file_search_resource = None
        vector_store_ids = []
        
        if hasattr(assistant_obj, "tool_resources") and assistant_obj.tool_resources is not None:
            if hasattr(assistant_obj.tool_resources, "file_search") and assistant_obj.tool_resources.file_search is not None:
                file_search_resource = assistant_obj.tool_resources.file_search
                if hasattr(file_search_resource, "vector_store_ids"):
                    vector_store_ids = list(file_search_resource.vector_store_ids)

        if not vector_store_ids:
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
            vector_store_ids = [vector_store_id]
        
        vector_store_id = vector_store_ids[0]
        
        # Process the file based on its type
        if is_csv or is_excel:
            # Upload to code interpreter
            with open(file_path, "rb") as file_stream:
                uploaded_file = client.files.create(
                    file=file_stream,
                    purpose='assistants'
                )
            
            # Get existing code_interpreter file_ids
            code_interpreter_file_ids = []
            
            if hasattr(assistant_obj, "tool_resources") and assistant_obj.tool_resources is not None:
                if hasattr(assistant_obj.tool_resources, "code_interpreter") and assistant_obj.tool_resources.code_interpreter is not None:
                    if hasattr(assistant_obj.tool_resources.code_interpreter, "file_ids"):
                        code_interpreter_file_ids = list(assistant_obj.tool_resources.code_interpreter.file_ids)
            
            # Add the new file
            code_interpreter_file_ids.append(uploaded_file.id)
            
            # Update the assistant
            client.beta.assistants.update(
                assistant_id=assistant,
                tool_resources={
                    "file_search": {"vector_store_ids": vector_store_ids},
                    "code_interpreter": {"file_ids": code_interpreter_file_ids}
                }
            )
            
            # Add file awareness if thread_id exists
            if thread_id:
                file_info = {
                    "type": "csv" if is_csv else "excel",
                    "name": file.filename,
                    "id": uploaded_file.id,
                    "processing_method": "code_interpreter"
                }
                await add_file_awareness(client, thread_id, file_info)
            
            logging.info(f"Added {file.filename} to code interpreter with file_id: {uploaded_file.id}")
            
            return JSONResponse(
                {
                    "message": "File successfully uploaded to code interpreter.",
                    "file_id": uploaded_file.id
                },
                status_code=200
            )
        elif is_image and thread_id:
            # Process image and add to thread
            analysis_text = await image_analysis(client, file_content, file.filename, prompt)
            
            # Add the analysis to the thread
            client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=f"Image Analysis for {file.filename}: {analysis_text}"
            )
            
            # Add file awareness
            file_info = {
                "type": "image",
                "name": file.filename,
                "processing_method": "thread_message"
            }
            await add_file_awareness(client, thread_id, file_info)
            
            logging.info(f"Added image analysis for {file.filename} to thread {thread_id}")
            
            return JSONResponse(
                {
                    "message": "Image successfully analyzed and added to thread.",
                    "image_analyzed": True
                },
                status_code=200
            )
        elif is_pdf or not (is_csv or is_excel or is_image):
            # Upload to vector store for document types and any other non-CSV/Excel/Image files
            with open(file_path, "rb") as file_stream:
                client.beta.vector_stores.file_batches.upload_and_poll(
                    vector_store_id=vector_store_id,
                    files=[file_stream]
                )
            
            # Add file awareness if thread_id exists
            if thread_id:
                file_info = {
                    "type": file_ext[1:] if file_ext else "unknown",
                    "name": file.filename,
                    "processing_method": "vector_store"
                }
                await add_file_awareness(client, thread_id, file_info)
            
            logging.info(f"File uploaded to vector store: {file.filename}")
            
            return JSONResponse(
                {
                    "message": "File successfully uploaded to vector store."
                },
                status_code=200
            )

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
                instructions="You are a conversation assistant.",
                tools=[{"type": "code_interpreter"}, {"type": "file_search"}],
                tool_resources={
                    "code_interpreter": {"file_ids": []},
                    "file_search": {"vector_store_ids": []}
                }
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
                # Create run with streaming
                with client.beta.threads.runs.stream(
                    thread_id=session, 
                    assistant_id=assistant
                ) as stream:
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
        raise HTTPException(status_code=500, detail=f"Failed to process conversation: {str(e)}")


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
                instructions="You are a conversation assistant.",
                tools=[{"type": "code_interpreter"}, {"type": "file_search"}],
                tool_resources={
                    "code_interpreter": {"file_ids": []},
                    "file_search": {"vector_store_ids": []}
                }
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
async def trim_thread(request: Request, assistant_id: str = None, max_age_days: Optional[int] = None):
    """
    Gets all threads for a given assistant, summarizes them, and removes old threads.
    Uses 48 hours as the threshold for thread cleanup.
    Accepts both query parameters and form data.
    """
    # Get parameters from form data if not provided in query
    if not assistant_id:
        form_data = await request.form()
        assistant_id = form_data.get("assistant_id")
    
    # Get max_age_days from form if provided
    if max_age_days is None:
        form_data = await request.form()
        max_age_days_str = form_data.get("max_age_days")
        if max_age_days_str:
            try:
                max_age_days = int(max_age_days_str)
            except (ValueError, TypeError):
                max_age_days = None
    
    # Set default cleanup threshold to 48 hours or convert days to hours
    time_threshold_hours = 48
    if max_age_days:
        time_threshold_hours = max_age_days * 24
    
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
async def file_cleanup(request: Request, vector_store_id: str = None, assistant_id: str = None, **kwargs):
    """
    Cleans up files: 
    - For vector store, removes files older than 48 hours
    - For code interpreter, unsets file_ids to remove references
    
    Accepts both query parameters and form data.
    """
    client = create_client()
    deleted_vector_files = 0
    cleared_code_interpreter_files = 0
    skipped_count = 0
    
    if not vector_store_id and not assistant_id:
        raise HTTPException(status_code=400, detail="Either vector_store_id or assistant_id is required")
    
    if not vector_store_id and not assistant_id:
        raise HTTPException(status_code=400, detail="Either vector_store_id or assistant_id is required")
    
    client = create_client()
    deleted_vector_files = 0
    cleared_code_interpreter_files = 0
    skipped_count = 0
    
    try:
        # Clean up vector store files if vector_store_id provided
        if vector_store_id:
            # Step 1: Get all files in the vector store
            file_batches = client.beta.vector_stores.file_batches.list(vector_store_id=vector_store_id)
            
            if not file_batches.data:
                if not assistant_id:
                    return JSONResponse({
                        "status": "No files found in the vector store",
                        "vector_store_id": vector_store_id
                    })
            else:
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
                            deleted_vector_files += 1
                        except Exception as e:
                            logging.error(f"Error deleting file {file.id}: {e}")
        
        # Clean up code interpreter files if assistant_id provided
        if assistant_id:
            try:
                # Get the assistant
                assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant_id)
                
                # Check if code_interpreter tool is present
                tool_resources = assistant_obj.tool_resources
                file_ids = []
                
                if hasattr(tool_resources, "code_interpreter") and tool_resources.code_interpreter is not None:
                    if hasattr(tool_resources.code_interpreter, "file_ids"):
                        file_ids = list(tool_resources.code_interpreter.file_ids)
                
                if file_ids:
                    # Clear the file_ids
                    # Prepare the file_search resource to preserve
                    file_search_resource = {}
                    if hasattr(tool_resources, "file_search") and tool_resources.file_search is not None:
                        if hasattr(tool_resources.file_search, "vector_store_ids"):
                            file_search_resource = {"vector_store_ids": list(tool_resources.file_search.vector_store_ids)}
                    
                    client.beta.assistants.update(
                        assistant_id=assistant_id,
                        tool_resources={
                            "code_interpreter": {"file_ids": []},
                            "file_search": file_search_resource
                        }
                    )
                        cleared_code_interpreter_files = len(file_ids)
                        logging.info(f"Cleared {cleared_code_interpreter_files} code interpreter files from assistant {assistant_id}")
            except Exception as e:
                logging.error(f"Error clearing code interpreter files: {e}")
        
        return JSONResponse({
            "status": "File cleanup completed",
            "vector_store_id": vector_store_id,
            "assistant_id": assistant_id,
            "vector_files_deleted": deleted_vector_files,
            "code_interpreter_files_cleared": cleared_code_interpreter_files,
            "batches_skipped": skipped_count
        })
        
    except Exception as e:
        logging.error(f"Error in file-cleanup: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to clean up files: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
