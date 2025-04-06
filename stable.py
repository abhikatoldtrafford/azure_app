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
        # file_id = file_info.get("id", "") # ID might not always be relevant for the awareness message itself
        processing_method = file_info.get("processing_method", "")

        awareness_message = f"FILE INFORMATION: A file named '{file_name}' of type '{file_type}' has been uploaded and processed. "

        if processing_method == "code_interpreter":
            awareness_message += f"This file is available for analysis using the code interpreter."
            if file_type == "excel":
                awareness_message += " This is an Excel file with potentially multiple sheets."
        elif processing_method == "thread_message":
            awareness_message += "This image has been analyzed and the descriptive content has been added to this thread."
        elif processing_method == "vector_store":
            awareness_message += "This file has been added to the vector store and its content is available for search."
        else:
            awareness_message += "This file has been processed."

        # Add specific instructions for Excel/CSV handling if using code interpreter
        if processing_method == "code_interpreter" and file_type in ["csv", "excel"]:
            awareness_message += "\n\nWhen analyzing this data file, follow these instructions precisely:\n"
            awareness_message += """
**File Handling:**

1. If receiving Excel (.xlsx/.xls):
   - Read ALL sheets using: `df_dict = pd.read_excel(file_path, sheet_name=None)`
   - Convert each sheet dataframe into a separate CSV for easier handling: `<original_filename>_<sheet_name>.csv` (e.g., "sales.xlsx" with sheets 'Orders', 'Clients' → becomes available conceptually as "sales_Orders.csv", "sales_Clients.csv")
   - When referencing data, always mention both original file and sheet name (e.g., "from the 'Orders' sheet in sales.xlsx").
   - Ensure you analyze **all** relevant sheets unless instructed otherwise.

2. If receiving CSV (.csv):
   - Use the file directly for analysis.
   - Preserve original filename in references (e.g., "Analyzing sales_data.csv").

**Analysis Requirements:**
- Start with a data overview: shape (rows/columns), column names and types, count of missing values per column.
- For Excel files, perform sheet-specific analysis initially.
- Look for opportunities to compare trends or data across sheets if applicable.
- Generate visualizations (plots) where appropriate to illustrate findings. Ensure plots are saved as images and you provide the image reference. Include clear titles and labels.
- Include key code snippets used for analysis, briefly explaining each step.

**Output Formatting:**
- Begin analysis sections with: "Analyzing `[filename.csv]`" or "Analyzing sheet `[Sheet Name]` from `[filename.xlsx]`".
- Use markdown tables for summaries (e.g., overview stats, key findings).
- Place visualizations under clear headings describing what they show.
- Use horizontal rules (`---`) to separate analysis for different sheets or major sections.
"""

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

@app.post("/initiate-chat")
async def initiate_chat(request: Request):
    """
    Initiates a new assistant, session (thread), and vector store.
    Optionally uploads a file and sets user context.
    Note: This endpoint creates *new* resources each time it's called.
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

    # Always include code_interpreter and file_search tools
    assistant_tools = [{"type": "code_interpreter"}, {"type": "file_search"}]
    # Initialize empty file_ids list for code_interpreter
    code_interpreter_file_ids = []
    assistant_tool_resources = {
        "file_search": {"vector_store_ids": [vector_store.id]},
        "code_interpreter": {"file_ids": code_interpreter_file_ids}
    }

    system_prompt = '''
You are a highly skilled Product Management AI Assistant and Co-Pilot. Your primary responsibilities include generating comprehensive Product Requirements Documents (PRDs) and providing insightful answers to a wide range of product-related queries. You seamlessly integrate information from uploaded files and your extensive knowledge base to deliver contextually relevant and actionable insights.

### **Primary Tasks:**

1. **Generate Product Requirements Documents (PRDs):**
   - **Trigger:** When the user explicitly requests a PRD.
   - **Structure:**
     - **Product Manager:** [Use the user's name if available from context; otherwise, leave blank]
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
   - For Excel files, remember to examine all sheets and provide comprehensive analysis as per file awareness instructions.
   - Generate visualizations and statistics to help users understand their data.
   - Explain your analysis approach and findings clearly.

### **Behavioral Guidelines:**

- **Contextual Awareness:**
  - Always consider the context provided by the uploaded files, user persona context messages, and previous interactions.
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
  - Always evaluate whether the file_search tool (for documents) or code_interpreter tool (for CSV/Excel) can enhance the quality of your response before using them. Follow instructions provided in file awareness messages.
  - Do not attempt to use code_interpreter on non-CSV/Excel files unless specifically instructed and feasible.

- **Data Privacy:**
  - Handle all uploaded files and user data with the utmost confidentiality and in compliance with relevant data protection standards. Avoid repeating sensitive information unnecessarily.

- **Assumption Handling:**
  - Clearly indicate when you are making assumptions due to missing information.
  - Provide rationales for your assumptions to maintain transparency.

- **Error Handling:**
  - Gracefully manage any errors or uncertainties by informing the user and seeking clarification when necessary.
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
                # Upload to OpenAI files for code interpreter
                with open(file_path, "rb") as file_stream:
                    uploaded_file = client.files.create(
                        file=file_stream,
                        purpose='assistants'  # Purpose must be 'assistants' for code interpreter/file search
                    )
                code_interpreter_file_ids.append(uploaded_file.id)

                # Update the assistant to link the file
                client.beta.assistants.update(
                    assistant_id=assistant.id,
                    tool_resources={
                        "code_interpreter": {"file_ids": code_interpreter_file_ids},
                        "file_search": {"vector_store_ids": [vector_store.id]}
                    }
                )
                file_info.update({
                    "type": "csv" if is_csv else "excel",
                    "id": uploaded_file.id,
                    "processing_method": "code_interpreter"
                })
                await add_file_awareness(client, thread.id, file_info)
                logging.info(f"Added '{filename}' to code interpreter for assistant {assistant.id} with file_id: {uploaded_file.id}")

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
        
        # Check for code_interpreter tool, add if missing
        if not any(tool.type == "code_interpreter" for tool in current_tools if hasattr(tool, 'type')):
            current_tools.append({"type": "code_interpreter"})
            logging.info(f"Adding code_interpreter tool to assistant {assistant_id}")

        # Prepare tool resources
        tool_resources = {
            "file_search": {"vector_store_ids": [vector_store_id]},
            "code_interpreter": {"file_ids": []}  # Start with empty list, files can be added later
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
    request: Request,  # Added missing request parameter
    file: UploadFile = Form(...),
    assistant: str = Form(...)
    # Optional params below read from form inside
):
    """
    Uploads a file and associates it with the given assistant.
    Handles different file types appropriately:
    - CSV/Excel files -> code interpreter
    - Images -> analyzed and added to thread (if session provided)
    - Other documents -> vector store
    Optionally takes 'session' (thread_id), 'context', 'prompt' (for image) from form data.
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

        # Consolidate tools and resources handling
        current_tools = assistant_obj.tools if assistant_obj.tools else []
        current_tool_resources = assistant_obj.tool_resources if assistant_obj.tool_resources else {}
        needs_update = False  # Flag if assistant needs updating

        # --- Code Interpreter Handling ---
        if is_csv or is_excel:
            # Ensure code interpreter tool exists
            if not any(tool.type == "code_interpreter" for tool in current_tools if hasattr(tool, 'type')):
                current_tools.append({"type": "code_interpreter"})
                needs_update = True
                logging.info(f"Adding code_interpreter tool to assistant {assistant}")

            # Get existing code_interpreter file_ids
            code_interpreter_file_ids = []
            ci_resources = getattr(current_tool_resources, "code_interpreter", None)
            if ci_resources and hasattr(ci_resources, "file_ids"):
                code_interpreter_file_ids = list(ci_resources.file_ids)

            # Upload to OpenAI files for code interpreter
            with open(file_path, "rb") as file_stream:
                uploaded_file = client.files.create(file=file_stream, purpose='assistants')

            if uploaded_file.id not in code_interpreter_file_ids:
                code_interpreter_file_ids.append(uploaded_file.id)
                needs_update = True  # Need to update assistant with new file ID

            uploaded_file_details = {
                "message": "File successfully uploaded and associated with code interpreter.",
                "file_id": uploaded_file.id,
                "filename": filename,
                "processing_method": "code_interpreter"
            }
            logging.info(f"Uploaded '{filename}' (ID: {uploaded_file.id}) for code interpreter, assistant {assistant}")
        else:
            uploaded_file_details = {
                "message": "File already associated with code interpreter.",
                "file_id": "",
                "filename": filename,
                "processing_method": "code_interpreter"
            }
            logging.info(f"File '{filename}' not recognized as CSV/Excel for code interpreter. Checking other possibilities...")

        # --- Vector Store Handling ---
        vector_store_ids = []
        fs_resources = getattr(current_tool_resources, "file_search", None)
        if fs_resources and hasattr(fs_resources, "vector_store_ids"):
            vector_store_ids = list(fs_resources.vector_store_ids)

        if is_document or not (is_csv or is_excel or is_image):
            # Ensure file search tool exists
            if not any(tool.type == "file_search" for tool in current_tools if hasattr(tool, 'type')):
                current_tools.append({"type": "file_search"})
                needs_update = True
                logging.info(f"Adding file_search tool to assistant {assistant}")

            # Ensure a vector store is linked or create one
            if not vector_store_ids:
                logging.info(f"No vector store linked to assistant {assistant}. Creating and linking a new one.")
                vector_store = client.beta.vector_stores.create(name=f"Assistant_{assistant}_Store")
                vector_store_ids = [vector_store.id]
                needs_update = True

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
            logging.info(f"Uploaded '{filename}' to vector store {vector_store_id_to_use} for assistant {assistant}")

        # --- Update Assistant if tools or resources changed ---
        if needs_update:
            update_payload = {"tools": current_tools, "tool_resources": {}}
            # Preserve/update file search resources
            update_payload["tool_resources"]["file_search"] = {"vector_store_ids": vector_store_ids}
            # Preserve/update code interpreter resources
            update_payload["tool_resources"]["code_interpreter"] = {"file_ids": code_interpreter_file_ids if (is_csv or is_excel) else []}

            client.beta.assistants.update(assistant_id=assistant, **update_payload)
            logging.info(f"Updated assistant {assistant} with new tool/resource associations.")

        # --- Image Handling (after potential assistant update) ---
        if is_image:
            if thread_id:
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
                logging.info(f"Analyzed image '{filename}' and added to thread {thread_id}")
            else:
                uploaded_file_details = {
                    "message": "Image uploaded but not analyzed as no session/thread ID was provided.",
                    "filename": filename,
                    "processing_method": "skipped_analysis"
                }
                logging.warning(f"Image '{filename}' uploaded for assistant {assistant} but no thread ID provided.")

        # --- Add File Awareness Message (if thread exists and file was processed) ---
        if thread_id and uploaded_file_details and uploaded_file_details.get("processing_method") not in ["skipped_analysis", None]:
            file_info = {
                "type": file_ext[1:] if file_ext else 'unknown',
                "name": filename,
                "id": uploaded_file_details.get("file_id"),  # Only present for code interpreter
                "processing_method": uploaded_file_details.get("processing_method")
            }
            # Correct file type for awareness message
            if is_csv:
                file_info["type"] = "csv"
            elif is_excel:
                file_info["type"] = "excel"
            elif is_image:
                file_info["type"] = "image"
            elif is_document:
                file_info["type"] = file_ext[1:] if file_ext else "document"

            await add_file_awareness(client, thread_id, file_info)

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
        # If no assistant or session provided, create defaults (log this behavior)
        if not assistant:
            logging.warning("No assistant ID provided for /conversation, creating a default one.")
            # Create a minimal default assistant
            try:
                assistant_obj = client.beta.assistants.create(
                    name="default_conversation_assistant",
                    model="gpt-4o-mini",  # Use a general-purpose model
                    instructions="You are a helpful conversation assistant.",
                    # No tools needed for basic conversation unless intended
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
            try:
                # Create run and stream the response
                with client.beta.threads.runs.stream(
                    thread_id=session,
                    assistant_id=assistant,
                    # Add event handlers if needed later for tool calls etc.
                ) as stream:
                    for event in stream:
                        # Check specifically for text deltas
                        if event.event == "thread.message.delta":
                            delta = event.data.delta
                            if delta.content:
                                for content_part in delta.content:
                                    if content_part.type == 'text' and content_part.text:
                                        text_value = content_part.text.value
                                        if text_value:
                                            buffer.append(text_value)
                                            # Yield chunks frequently for better streaming feel
                                            if len(buffer) >= 5:  # Adjust buffer size as needed
                                                yield ''.join(buffer)
                                                buffer = []
                # Yield any remaining text in the buffer
                if buffer:
                    yield ''.join(buffer)
            except Exception as e:
                logging.error(f"Streaming error during run for thread {session}: {e}")
                yield "\n[ERROR] An error occurred while generating the response. Please try again."
                # Consider raising HTTPException here too, but yielding error message is user-friendly

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
    Uses existing session/assistant if provided, otherwise creates defaults (logs this).
    """
    client = create_client()

    try:
        # Fallback logic similar to /conversation
        if not assistant:
            logging.warning("No assistant ID provided for /chat, creating a default one.")
            try:
                assistant_obj = client.beta.assistants.create(
                    name="default_chat_assistant", model="gpt-4o-mini",
                    instructions="You are a helpful chat assistant."
                )
                assistant = assistant_obj.id
            except Exception as e:
                logging.error(f"Failed to create default assistant: {e}")
                raise HTTPException(status_code=500, detail="Failed to create default assistant")

        if not session:
            logging.warning("No session (thread) ID provided for /chat, creating a new one.")
            try:
                thread = client.beta.threads.create()
                session = thread.id
            except Exception as e:
                logging.error(f"Failed to create default thread: {e}")
                raise HTTPException(status_code=500, detail="Failed to create default thread")

        # Add user message if prompt is given
        if prompt:
            try:
                client.beta.threads.messages.create(
                    thread_id=session, role="user", content=prompt
                )
            except Exception as e:
                logging.error(f"Failed to add message to thread {session}: {e}")
                raise HTTPException(status_code=500, detail="Failed to add message to chat thread")

        # Run the assistant and collect the full response
        response_text_parts = []
        try:
            # Use stream to collect deltas - often more reliable than run+retrieve+list messages
            with client.beta.threads.runs.stream(thread_id=session, assistant_id=assistant) as stream:
                for event in stream:
                    if event.event == "thread.message.delta":
                        delta = event.data.delta
                        if delta.content:
                            for content_part in delta.content:
                                if content_part.type == 'text' and content_part.text:
                                    text_value = content_part.text.value
                                    if text_value:
                                        response_text_parts.append(text_value)
            # Alternative: Use run = client.beta.threads.runs.create_and_poll(...) then list messages
            # This stream approach captures the final message content as it's generated.

        except Exception as e:
            logging.error(f"Error during run/stream for thread {session}: {e}")
            raise HTTPException(status_code=500, detail="Error generating response. Please try again.")

        full_response = ''.join(response_text_parts)
        return JSONResponse(content={"response": full_response})

    except Exception as e:
        logging.error(f"Error in /chat endpoint setup: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process chat request: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    print("Starting FastAPI server on http://0.0.0.0:8000")
    # Consider adding reload=True for development, but remove for production
    uvicorn.run(app, host="0.0.0.0", port=8000)
