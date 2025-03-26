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
        
        # Use the existing client for vision API
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

# Helper function to identify file type
def get_file_type(filename):
    """Determine if file is CSV, Excel, image, or other type"""
    ext = os.path.splitext(filename.lower())[1]
    if ext in ['.csv']:
        return 'csv'
    elif ext in ['.xlsx', '.xls']:
        return 'excel'
    elif ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']:
        return 'image'
    else:
        return 'other'

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

    # Always include file_search and code_interpreter tools
    assistant_tools = [{"type": "code_interpreter"}, {"type": "file_search"}]
    assistant_tool_resources = {"file_search": {"vector_store_ids": [vector_store.id]}}
    
    # Initialize empty file_ids list for code_interpreter
    code_interpreter_file_ids = []
    
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
            - For CSV and Excel files, use the code_interpreter tool to analyze the data.
            - Ensure all sections are contextually relevant, logically structured, and provide actionable insights.
            - If certain information is missing, make informed assumptions or prompt the user for clarification.
            - Incorporate industry best practices and standards where applicable.

        2. **Answer Generic Product Management Questions:**
        - **Scope:** Respond to a broad range of product management queries, including strategy, market analysis, feature prioritization, user feedback interpretation, and more.
        - **Methodology:**
            - Use the file_search tool to find pertinent information within uploaded files.
            - For data analysis of CSV and Excel files, use the code_interpreter tool.
            - Leverage your comprehensive knowledge base to provide thorough and insightful answers.
            - If a question falls outside the scope of the provided files and your expertise, default to a general GPT-4 response without referencing the files.
            - Maintain a balance between technical detail and accessibility, ensuring responses are understandable yet informative.

        ### **File Handling Guidelines:**
        1. When working with Excel files:
           - Analyze all sheets in the file
           - Reference both the original file and sheet name in your analysis
           - Perform sheet-specific analysis and compare trends across sheets when applicable

        2. When working with CSV files:
           - Start with data overview (shape, columns, missing values)
           - Generate appropriate visualizations with clear source identification
           - Preserve the original filename in references

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
        file_type = get_file_type(filename)
        
        if file_type in ['csv', 'excel']:
            # Upload to code interpreter
            with open(file_path, "rb") as file_stream:
                code_interpreter_file = client.files.create(
                    file=file_stream,
                    purpose="assistants"
                )
                code_interpreter_file_ids.append(code_interpreter_file.id)
                
            # Update the assistant with code_interpreter file
            if code_interpreter_file_ids:
                assistant_tool_resources["code_interpreter"] = {"file_ids": code_interpreter_file_ids}
                client.beta.assistants.update(
                    assistant_id=assistant.id,
                    tool_resources=assistant_tool_resources
                )
                
            # Add a message to the thread indicating the file type
            file_description = f"CSV file" if file_type == 'csv' else f"Excel file with multiple sheets"
            client.beta.threads.messages.create(
                thread_id=thread.id,
                role="user",
                content=f"I've uploaded a {file_description} named '{filename}'. Please use the code_interpreter tool to analyze it."
            )
            
            logging.info(f"{file_type.capitalize()} file '{filename}' uploaded to code interpreter")
        else:
            # Upload to vector store (original flow)
            with open(file_path, "rb") as file_stream:
                file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                    vector_store_id=vector_store.id, 
                    files=[file_stream]
                )
            logging.info(f"File '{filename}' uploaded to vector store: status={file_batch.status}, count={file_batch.file_counts}")
            
            # If it's an image, analyze it
            if file_type == 'image':
                analysis_text = await image_analysis(client, file_content, filename, None)
                client.beta.threads.messages.create(
                    thread_id=thread.id,
                    role="user",
                    content=f"Image Analysis for {filename}: {analysis_text}"
                )
                logging.info(f"Added image analysis for '{filename}' to thread {thread.id}")

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
    context = form.get("context", None)  # Get optional context parameter

    # Attempt to get the assistant & vector store from the form
    assistant_id = form.get("assistant", None)
    vector_store_id = form.get("vector_store", None)
    thread_id = form.get("session", None)

    # Initialize empty file_ids list for code_interpreter
    code_interpreter_file_ids = []
    
    # If no assistant, create one
    if not assistant_id:
        if not vector_store_id:
            vector_store = client.beta.vector_stores.create(name="demo")
            vector_store_id = vector_store.id
            
        base_prompt = "You are a product management AI assistant, a product co-pilot. "
        
        # Add file handling guidelines to the prompt
        file_handling_guidance = """
        When working with CSV and Excel files:
        1. For Excel files:
           - Analyze all sheets
           - Reference both original file and sheet names in your analysis
           - Compare trends across sheets when applicable
           
        2. For CSV files:
           - Provide data overview (shape, columns, missing values)
           - Generate appropriate visualizations
           - Preserve original filename in references
        """
        
        instructions = base_prompt + file_handling_guidance if not system_prompt else f"{base_prompt} {system_prompt} {file_handling_guidance}"
        
        tools = [{"type": "code_interpreter"}, {"type": "file_search"}]
        tool_resources = {"file_search": {"vector_store_ids": [vector_store_id]}}
        
        assistant = client.beta.assistants.create(
            name="demo_co_pilot",
            model="gpt-4o-mini",
            instructions=instructions,
            tools=tools,
            tool_resources=tool_resources,
        )
        assistant_id = assistant.id
    else:
        # If user gave an assistant, update instructions if needed
        if system_prompt:
            file_handling_guidance = """
            When working with CSV and Excel files:
            1. For Excel files:
               - Analyze all sheets
               - Reference both original file and sheet names in your analysis
               - Compare trends across sheets when applicable
               
            2. For CSV files:
               - Provide data overview (shape, columns, missing values)
               - Generate appropriate visualizations
               - Preserve original filename in references
            """
            
            updated_prompt = f"You are a product management AI assistant, a product co-pilot. {system_prompt} {file_handling_guidance}"
            
            client.beta.assistants.update(
                assistant_id=assistant_id,
                instructions=updated_prompt
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
                
                # Make sure we have both tools
                if not any(t["type"] == "file_search" for t in existing_tools):
                    existing_tools.append({"type": "file_search"})
                if not any(t["type"] == "code_interpreter" for t in existing_tools):
                    existing_tools.append({"type": "code_interpreter"})
                    
                # Get any existing code_interpreter file_ids
                code_interpreter_resource = getattr(assistant_obj.tool_resources, "code_interpreter", None)
                if code_interpreter_resource and hasattr(code_interpreter_resource, "file_ids"):
                    code_interpreter_file_ids = code_interpreter_resource.file_ids
                
                # Update with the tools and resources
                tool_resources = {
                    "file_search": {"vector_store_ids": [vector_store_id]}
                }
                
                if code_interpreter_file_ids:
                    tool_resources["code_interpreter"] = {"file_ids": code_interpreter_file_ids}
                    
                client.beta.assistants.update(
                    assistant_id=assistant_id,
                    tools=existing_tools,
                    tool_resources=tool_resources,
                )

    # Handle file upload if present
    if file:
        filename = file.filename
        file_path = f"/tmp/{file.filename}"
        file_content = await file.read()
        
        with open(file_path, "wb") as ftemp:
            ftemp.write(file_content)
            
        # Determine file type
        file_type = get_file_type(filename)
        
        if file_type in ['csv', 'excel']:
            # Upload to code interpreter
            with open(file_path, "rb") as file_stream:
                code_interpreter_file = client.files.create(
                    file=file_stream,
                    purpose="assistants"
                )
                code_interpreter_file_ids.append(code_interpreter_file.id)
                
            # Retrieve current assistant to get existing file_ids
            assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant_id)
            existing_tool_resources = assistant_obj.tool_resources if hasattr(assistant_obj, 'tool_resources') else {}
            
            # Prepare tool_resources with code_interpreter file_ids
            tool_resources = {}
            
            # Copy existing file_search if present
            file_search_resource = getattr(existing_tool_resources, "file_search", None)
            if file_search_resource and hasattr(file_search_resource, "vector_store_ids"):
                tool_resources["file_search"] = {"vector_store_ids": file_search_resource.vector_store_ids}
            elif vector_store_id:
                tool_resources["file_search"] = {"vector_store_ids": [vector_store_id]}
                
            # Update code_interpreter file_ids
            existing_code_interpreter = getattr(existing_tool_resources, "code_interpreter", None)
            existing_file_ids = []
            
            if existing_code_interpreter and hasattr(existing_code_interpreter, "file_ids"):
                existing_file_ids = existing_code_interpreter.file_ids
                
            # Combine existing and new file_ids
            all_file_ids = list(set(existing_file_ids + code_interpreter_file_ids))
            tool_resources["code_interpreter"] = {"file_ids": all_file_ids}
            
            # Update assistant with new tool_resources
            client.beta.assistants.update(
                assistant_id=assistant_id,
                tool_resources=tool_resources
            )
            
            logging.info(f"{file_type.capitalize()} file '{filename}' uploaded to code interpreter")
            
            # If thread exists, add a message about the file
            if thread_id:
                file_description = f"CSV file" if file_type == 'csv' else f"Excel file with multiple sheets"
                client.beta.threads.messages.create(
                    thread_id=thread_id,
                    role="user",
                    content=f"I've uploaded a {file_description} named '{filename}'. Please use the code_interpreter tool to analyze it."
                )
        else:
            # Upload to vector store (original flow)
            with open(file_path, "rb") as file_stream:
                client.beta.vector_stores.file_batches.upload_and_poll(
                    vector_store_id=vector_store_id,
                    files=[file_stream]
                )
                
            # If image and thread exists, analyze and add to thread
            if file_type == 'image' and thread_id:
                analysis_text = await image_analysis(client, file_content, filename, None)
                client.beta.threads.messages.create(
                    thread_id=thread_id,
                    role="user",
                    content=f"Image Analysis for {filename}: {analysis_text}"
                )
                logging.info(f"Added image analysis for '{filename}' to thread {thread_id}")

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
    Maintains the same input-output as before, ensures a single vector store per assistant.
    Adds support for CSV/Excel files to be used with code_interpreter.
    """
    client = create_client()
    # Parse form data if present
    form_data = {}
    try:
        # This is a workaround to get form data when file is specified via Form(...)
        request = file.request if hasattr(file, 'request') else None
        if request:
            form_data = await request.form()
    except Exception as e:
        logging.error(f"Error getting form data: {e}")
        
    # Get context and thread_id if provided
    context = form_data.get("context", None)
    thread_id = form_data.get("session", None)
    prompt = form_data.get("prompt", None)  # Optional prompt for image analysis

    try:
        # Save the uploaded file locally and get the data
        file_content = await file.read()
        file_path = f"/tmp/{file.filename}"
        with open(file_path, "wb") as temp_file:
            temp_file.write(file_content)
            
        # Determine file type
        file_type = get_file_type(file.filename)
        
        # Retrieve the assistant
        assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant)
        
        # Check for code_interpreter tool
        has_code_interpreter = any(tool.get("type") == "code_interpreter" for tool in assistant_obj.tools)
        
        # If not present, add it
        if not has_code_interpreter:
            existing_tools = assistant_obj.tools if hasattr(assistant_obj, 'tools') else []
            existing_tools.append({"type": "code_interpreter"})
            client.beta.assistants.update(
                assistant_id=assistant,
                tools=existing_tools
            )
            logging.info(f"Added code_interpreter tool to assistant {assistant}")
            
        # Get existing tool resources
        existing_tool_resources = assistant_obj.tool_resources if hasattr(assistant_obj, 'tool_resources') else {}
        
        # Check for file_search resource and vector store
        file_search_resource = getattr(existing_tool_resources, "file_search", None)
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
            existing_tools = assistant_obj.tools if hasattr(assistant_obj, 'tools') else []
            if not any(t.get("type") == "file_search" for t in existing_tools):
                existing_tools.append({"type": "file_search"})
                client.beta.assistants.update(
                    assistant_id=assistant,
                    tools=existing_tools
                )

        # Handle CSV or Excel files with code_interpreter
        if file_type in ['csv', 'excel']:
            # Upload to code_interpreter
            with open(file_path, "rb") as file_stream:
                code_interpreter_file = client.files.create(
                    file=file_stream,
                    purpose="assistants"
                )
                
            # Get existing code_interpreter file_ids
            code_interpreter_resource = getattr(existing_tool_resources, "code_interpreter", None)
            existing_file_ids = []
            
            if code_interpreter_resource and hasattr(code_interpreter_resource, "file_ids"):
                existing_file_ids = code_interpreter_resource.file_ids
                
            # Combine existing and new file_ids
            all_file_ids = list(set(existing_file_ids + [code_interpreter_file.id]))
            
            # Prepare tool_resources update
            tool_resources = {}
            
            # Include file_search if we have a vector store
            if vector_store_id:
                tool_resources["file_search"] = {"vector_store_ids": [vector_store_id]}
                
            # Add code_interpreter file_ids
            tool_resources["code_interpreter"] = {"file_ids": all_file_ids}
            
            # Update assistant with new tool_resources
            client.beta.assistants.update(
                assistant_id=assistant,
                tool_resources=tool_resources
            )
            
            logging.info(f"{file_type.capitalize()} file '{file.filename}' uploaded to code interpreter")
            
            # If thread exists, add a message about the file
            if thread_id:
                file_description = f"CSV file" if file_type == 'csv' else f"Excel file with multiple sheets"
                client.beta.threads.messages.create(
                    thread_id=thread_id,
                    role="user",
                    content=f"I've uploaded a {file_description} named '{file.filename}'. Please use the code_interpreter tool to analyze it."
                )
                
            result_message = f"{file_type.capitalize()} file uploaded to code interpreter."
        else:
            # For image files, analyze and add to thread if thread_id exists
            if file_type == 'image' and thread_id:
                logging.info(f"Analyzing image file: {file.filename}")
                analysis_text = await image_analysis(client, file_content, file.filename, prompt)
                
                # Add the analysis to the thread
                client.beta.threads.messages.create(
                    thread_id=thread_id,
                    role="user",
                    content=f"Image Analysis for {file.filename}: {analysis_text}"
                )
                logging.info(f"Added image analysis to thread {thread_id}")
                
            # Upload non-CSV/Excel files to vector store (original flow)
            with open(file_path, "rb") as file_stream:
                file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                    vector_store_id=vector_store_id,
                    files=[file_stream]
                )
                
            logging.info(f"File '{file.filename}' uploaded to vector store")
            result_message = "File successfully uploaded to vector store."
            
        # If context provided and thread exists, update context
        if context and thread_id:
            try:
                await update_context(client, thread_id, context)
            except Exception as e:
                logging.error(f"Error updating context in thread: {e}")
                # Continue even if context update fails

        return JSONResponse(
            {
                "message": result_message,
                "image_analyzed": file_type == 'image' and thread_id is not None
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
    context: Optional[str] = None,  # Optional context parameter
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
                tools=[{"type": "code_interpreter"}, {"type": "file_search"}]
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
    context: Optional[str] = None,  # Optional context parameter
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
                tools=[{"type": "code_interpreter"}, {"type": "file_search"}]
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
async def file_cleanup(request: Request, vector_store_id: str = None):
    """
    Lists and deletes files older than 48 hours.
    Handles both vector store files and code interpreter files.
    Accepts both query parameters and form data.
    """
    # Get parameters from form data if not provided in query
    if not vector_store_id:
        form_data = await request.form()
        vector_store_id = form_data.get("vector_store_id")
        assistant_id = form_data.get("assistant_id")
    else:
        assistant_id = None
    
    if not vector_store_id and not assistant_id:
        raise HTTPException(status_code=400, detail="vector_store_id or assistant_id is required")
    
    client = create_client()
    deleted_vs_count = 0
    deleted_ci_count = 0
    skipped_count = 0
    
    # Get current time for age comparison
    now = datetime.datetime.now()
    
    # Part 1: Clean up vector store files
    if vector_store_id:
        try:
            # Get all files in the vector store
            file_batches = client.beta.vector_stores.file_batches.list(vector_store_id=vector_store_id)
            
            if not file_batches.data:
                logging.info(f"No files found in vector store {vector_store_id}")
            else:
                # Process each file batch to find files older than 48 hours
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
                            deleted_vs_count += 1
                        except Exception as e:
                            logging.error(f"Error deleting vector store file {file.id}: {e}")
        except Exception as e:
            logging.error(f"Error cleaning up vector store files: {e}")
    
    # Part 2: Clean up code interpreter files for the assistant
    if assistant_id:
        try:
            # Get the assistant
            assistant = client.beta.assistants.retrieve(assistant_id=assistant_id)
            
            # Check if the assistant has code_interpreter tool resources
            tool_resources = assistant.tool_resources if hasattr(assistant, 'tool_resources') else None
            code_interpreter_resource = getattr(tool_resources, "code_interpreter", None)
            
            if code_interpreter_resource and hasattr(code_interpreter_resource, "file_ids"):
                file_ids = code_interpreter_resource.file_ids
                
                # Process each file
                files_to_keep = []
                
                for file_id in file_ids:
                    try:
                        # Get file info
                        file_info = client.files.retrieve(file_id=file_id)
                        
                        # Calculate age in hours
                        file_created = datetime.datetime.fromtimestamp(file_info.created_at)
                        file_age_hours = (now - file_created).total_seconds() / 3600
                        
                        # Keep files newer than 48 hours
                        if file_age_hours <= 48:
                            files_to_keep.append(file_id)
                            skipped_count += 1
                        else:
                            # Delete old files
                            client.files.delete(file_id=file_id)
                            deleted_ci_count += 1
                    except Exception as e:
                        logging.error(f"Error processing code interpreter file {file_id}: {e}")
                        # Keep files that error out
                        files_to_keep.append(file_id)
                
                # Update the assistant with remaining files
                if len(files_to_keep) < len(file_ids):
                    # Create updated tool_resources
                    updated_tool_resources = {}
                    
                    # Copy existing file_search if present
                    if hasattr(tool_resources, "file_search"):
                        updated_tool_resources["file_search"] = {
                            "vector_store_ids": tool_resources.file_search.vector_store_ids
                        }
                        
                    # Update code_interpreter with remaining files
                    if files_to_keep:
                        updated_tool_resources["code_interpreter"] = {"file_ids": files_to_keep}
                    
                    # Update the assistant
                    client.beta.assistants.update(
                        assistant_id=assistant_id,
                        tool_resources=updated_tool_resources
                    )
                    
                    logging.info(f"Updated assistant {assistant_id} with {len(files_to_keep)} remaining files")
        except Exception as e:
            logging.error(f"Error cleaning up code interpreter files: {e}")
    
    return JSONResponse({
        "status": "File cleanup completed",
        "vector_store_id": vector_store_id,
        "assistant_id": assistant_id,
        "vector_store_files_deleted": deleted_vs_count,
        "code_interpreter_files_deleted": deleted_ci_count,
        "files_skipped": skipped_count
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
