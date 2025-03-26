import logging
from fastapi import FastAPI, Request, UploadFile, Form, HTTPException, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from openai import AzureOpenAI, RateLimitError, APIError # Import specific errors if needed
from typing import Optional, List, Dict, Any
import os
import datetime
import time
import base64
# import mimetypes # Removed unused import

# Configure basic logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = FastAPI()

# --- Azure OpenAI client configuration ---
# Use environment variables for sensitive data
AZURE_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "YOUR_DEFAULT_ENDPOINT_IF_ANY") # Replace with your endpoint or keep as env var
AZURE_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION = "2024-05-01-preview"

if not AZURE_API_KEY:
    logging.error("AZURE_OPENAI_API_KEY environment variable not set.")
    # You might want to raise an exception or exit here depending on deployment strategy
    # raise ValueError("AZURE_OPENAI_API_KEY must be set")

def create_client():
    """Creates and returns an AzureOpenAI client."""
    if not AZURE_API_KEY:
        # Handle case where key might still be missing if app continues after startup log
        raise HTTPException(status_code=500, detail="Azure OpenAI API Key is not configured.")
    try:
        return AzureOpenAI(
            azure_endpoint=AZURE_ENDPOINT,
            api_key=AZURE_API_KEY,
            api_version=AZURE_API_VERSION,
            max_retries=3, # Add some resilience
        )
    except Exception as e:
        logging.error(f"Failed to create AzureOpenAI client: {e}")
        raise HTTPException(status_code=500, detail="Could not initialize AI client.")

# --- Helper Functions ---

async def image_analysis(client: AzureOpenAI, image_data: bytes, filename: str, prompt: Optional[str] = None) -> str:
    """Analyzes an image using Azure OpenAI vision capabilities and returns the analysis text."""
    try:
        ext = os.path.splitext(filename)[1].lower()
        b64_img = base64.b64encode(image_data).decode("utf-8")
        # Default to jpeg if extension can't be determined reliably
        mime_type = f"image/{ext[1:]}" if ext and ext[1:] in ['jpg', 'jpeg', 'png', 'gif', 'webp'] else "image/jpeg"
        data_url = f"data:{mime_type};base64,{b64_img}"

        default_prompt = (
            "Analyze this image and provide a thorough summary including all elements. "
            "If there's any text visible, include all the textual content. Describe:"
        )
        combined_prompt = f"{default_prompt} {prompt}" if prompt else default_prompt

        # Use the existing client instance passed as argument
        response = client.chat.completions.create(
            model="gpt-4o-mini", # Ensure this model is deployed in your Azure endpoint
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": combined_prompt},
                    {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}}
                ]
            }],
            max_tokens=800 # Increased max_tokens slightly for potentially detailed analysis
        )

        analysis_text = response.choices[0].message.content
        if not analysis_text:
             return "No analysis text returned from the model."
        return analysis_text

    except RateLimitError as rle:
        logging.error(f"Image analysis rate limit error: {rle}")
        return f"Error analyzing image: Rate limit exceeded. Please try again later."
    except APIError as apie:
         logging.error(f"Image analysis API error: {apie}")
         return f"Error analyzing image: API error ({apie.status_code}). Details: {apie.message}"
    except Exception as e:
        logging.exception(f"Unexpected image analysis error for file {filename}") # Use logging.exception to include traceback
        return f"Error analyzing image: {str(e)}"

async def update_context(client: AzureOpenAI, thread_id: str, context: str):
    """Updates the user persona context in a thread by replacing the previous context message."""
    if not context:
        return

    logging.info(f"Attempting to update context for thread {thread_id}")
    try:
        # Get existing messages to check for previous context
        # Increased limit significantly to reduce chance of missing old context
        messages = client.beta.threads.messages.list(
            thread_id=thread_id,
            order="desc",
            limit=100 # Increased limit
        )

        previous_context_msg_id = None
        for msg in messages.data:
             # Check if message content exists and is text before accessing metadata
            if msg.content and len(msg.content) > 0 and isinstance(msg.content[0], dict) and msg.content[0].get("type") == "text":
                 # Check metadata using getattr safely
                 msg_metadata = getattr(msg, 'metadata', None)
                 if msg_metadata and msg_metadata.get('type') == 'user_persona_context':
                     previous_context_msg_id = msg.id
                     logging.info(f"Found previous context message {previous_context_msg_id} in thread {thread_id}")
                     break # Found the latest one (due to desc order)

        # Delete previous context message if found
        if previous_context_msg_id:
            try:
                client.beta.threads.messages.delete(
                    thread_id=thread_id,
                    message_id=previous_context_msg_id
                )
                logging.info(f"Deleted previous context message {previous_context_msg_id}")
            except Exception as e:
                logging.error(f"Error deleting previous context message {previous_context_msg_id}: {e}. Proceeding to add new context.")
                # Continue even if delete fails to ensure new context is added

        # Add new context message
        new_msg = client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=f"USER PERSONA CONTEXT: {context}",
            metadata={"type": "user_persona_context"}
        )
        logging.info(f"Added new user persona context message {new_msg.id} to thread {thread_id}")

    except Exception as e:
        logging.exception(f"Error updating context in thread {thread_id}")
        # Continue the flow even if context update fails

async def add_file_awareness(client: AzureOpenAI, thread_id: str, file_info: Dict[str, Any]):
    """Adds file awareness to the assistant by sending a message about the file."""
    if not file_info:
        return

    try:
        file_type = file_info.get("type", "unknown")
        file_name = file_info.get("name", "unnamed_file")
        processing_method = file_info.get("processing_method", "")

        awareness_message = f"FILE INFORMATION: A file named '{file_name}' of type '{file_type}' has been uploaded. "

        if processing_method == "code_interpreter":
            awareness_message += f"This file is available for analysis using the code interpreter."
            if file_type == "excel":
                 awareness_message += " This is an Excel file with potentially multiple sheets."
            # Add specific instructions for Excel/CSV handling
            awareness_message += "\n\nWhen analyzing this file, follow these instructions:\n"
            awareness_message += """
**File Handling:**
1. If receiving Excel (.xlsx/.xls/.xlsm):
    - Read ALL sheets using: `df_dict = pd.read_excel(file_path, sheet_name=None)`
    - Convert each sheet to CSV named: `<original_filename>_<sheet_name>.csv` (e.g., "sales.xlsx" → "sales_Orders.csv", "sales_Clients.csv")
    - Always reference both original file and sheet name in analysis. Report if sheets are empty.

2. If receiving CSV:
    - Use directly for analysis.
    - Preserve original filename in references.

**Analysis Requirements:**
- Start with data overview: shape, columns, data types, missing values per column. For Excel, do this per sheet.
- Perform sheet-specific analysis for Excel files. Note if any sheets are empty or unusable.
- Compare trends across sheets when applicable and meaningful.
- Generate visualizations (like plots) with clear titles and source identification (file/sheet). Save visualizations as files.
- Include relevant code snippets used for analysis with brief explanations.

**Output Formatting:**
- Begin analysis sections with: "Analyzing `[file.csv]`" or "Analyzing sheet `[sheet_name]` from `[file.xlsx]`".
- Use markdown tables for key statistics or data summaries.
- Place visualizations under clear headings and provide download links if possible.
- Separate analysis per sheet/file with horizontal rules (`---`).
"""
        elif processing_method == "thread_message": # Image analysis case
             awareness_message += "This image has been analyzed using vision capabilities, and the description/text content has been added to this conversation thread just before this message."
        elif processing_method == "vector_store":
             awareness_message += "This file has been added to the knowledge retrieval store and is available for search when answering questions."
        else:
            awareness_message += "This file has been processed." # Generic fallback

        # Send the message to the thread
        msg = client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user", # Important: This message comes from the 'user' side to inform the assistant
            content=awareness_message,
            metadata={"type": "file_awareness", "filename": file_name}
        )
        logging.info(f"Added file awareness message {msg.id} for {file_name} to thread {thread_id}")

    except Exception as e:
        logging.exception(f"Error adding file awareness for {file_name} to thread {thread_id}")
        # Continue the flow even if adding awareness fails

# --- FastAPI Endpoints ---

@app.post("/initiate-chat")
async def initiate_chat(request: Request):
    """
    Initiates the assistant and session (thread). Optionally uploads a file and sets context.
    Note: Creates a new assistant and vector store each time, which is inefficient for reuse.
    Consider modifying if assistant/store persistence across calls is needed.
    """
    client = create_client()

    form = await request.form()
    file: Optional[UploadFile] = form.get("file", None)
    context: Optional[str] = form.get("context", None)

    logging.info("Initiating chat: Creating new vector store, assistant, and thread.")

    try:
        # Create a vector store up front
        vector_store = client.beta.vector_stores.create(name=f"ChatStore_{int(time.time())}")
        logging.info(f"Vector Store created: {vector_store.id}")
    except Exception as e:
        logging.exception("Failed to create vector store")
        raise HTTPException(status_code=500, detail=f"Failed to create vector store: {e}")

    # Define tools and resources
    assistant_tools = [{"type": "code_interpreter"}, {"type": "file_search"}]
    # Initialize empty file_ids list for code_interpreter
    code_interpreter_file_ids = []
    assistant_tool_resources = {
        "file_search": {"vector_store_ids": [vector_store.id]},
        "code_interpreter": {"file_ids": code_interpreter_file_ids}
    }

    system_prompt = '''
         You are a highly skilled Product Management AI Assistant and Co-Pilot. Your primary responsibilities include generating comprehensive Product Requirements Documents (PRDs) and providing insightful answers to a wide range of product-related queries. You seamlessly integrate information from uploaded files (using code interpreter for CSV/Excel analysis, file search for documents) and your extensive knowledge base to deliver contextually relevant and actionable insights. Remember to explicitly state which file or data source you are referencing in your responses.

         ### **Primary Tasks:**

         1.  **Generate Product Requirements Documents (PRDs):**
             * **Trigger:** When the user explicitly requests a PRD (e.g., "Create a PRD for...", "Generate PRD").
             * **Structure:** Follow a standard PRD template. If essential information (like Product Name, Vision) is missing, ask the user for clarification before proceeding.
                 * Product Manager: [Use user's name from context if available; otherwise, ask or leave blank]
                 * Product Name: [Derived from user input or files]
                 * Product Vision: [Extracted or synthesized]
                 * Customer Problem: [Identified from input/files]
                 * Personas: [Based on input or generated]
                 * Date: [Current date]
             * **Sections:** Include Executive Summary, Goals, Key Features, Functional Requirements, Non-Functional Requirements, Use Cases, Milestones, Risks. Generate 3-5 concise points per section based on available information.
             * **Guidelines:** Use `file_search` for relevant data in docs. Use `code_interpreter` if insights from uploaded CSV/Excel are needed. Clearly state assumptions if information is missing after attempting clarification.

         2.  **Answer Generic Product Management Questions:**
             * **Scope:** Strategy, market analysis, feature prioritization, user feedback, A/B testing, etc.
             * **Methodology:** Prioritize information from uploaded files (`file_search` for docs, `code_interpreter` for data files) if relevant. Supplement with your general knowledge. If the query is unrelated to uploaded files, answer using your general knowledge base. Clearly cite file sources when used.

         3.  **Data Analysis with Code Interpreter:**
             * When CSV or Excel files are uploaded, use `code_interpreter` for analysis as requested by the user.
             * Follow the specific **File Handling**, **Analysis Requirements**, and **Output Formatting** guidelines provided in the 'FILE INFORMATION' message you receive after such uploads.
             * Generate plots and tables. Explain your findings clearly. Reference the specific file and sheet (for Excel).

         ### **Behavioral Guidelines:**

         * **Contextual Awareness:** Pay close attention to user persona context, uploaded file information, and conversation history.
         * **Proactive & Insightful:** Offer actionable recommendations and anticipate follow-up needs.
         * **Professional Tone:** Clear, concise, objective, and respectful communication.
         * **Tool Usage:** Use `file_search` when the query likely relates to uploaded documents. Use `code_interpreter` for analyzing uploaded CSV/Excel data as instructed. Announce which tool you are using if performing a complex task.
         * **Clarification:** If a request is ambiguous or requires information not available, ask clarifying questions.

         ### **Important Notes:**

         * **Data Privacy:** Assume all user data and file contents are confidential. Do not repeat sensitive information unnecessarily.
         * **Assumption Handling:** State assumptions clearly (e.g., "Assuming the target market is X based on the document...").
         * **Error Handling:** If a tool fails or you encounter an error, inform the user clearly and suggest alternative approaches if possible.
         '''
    try:
        assistant = client.beta.assistants.create(
            name=f"PM_Assistant_{int(time.time())}",
            model="gpt-4o-mini", # Ensure this model is deployed
            instructions=system_prompt,
            tools=assistant_tools,
            tool_resources=assistant_tool_resources,
        )
        logging.info(f'Assistant created: {assistant.id}')
    except Exception as e:
        logging.exception("Failed to create assistant")
        # Attempt to clean up vector store if assistant creation fails
        try:
            client.beta.vector_stores.delete(vector_store_id=vector_store.id)
            logging.info(f"Cleaned up vector store {vector_store.id} after failed assistant creation.")
        except Exception as vs_del_e:
            logging.error(f"Failed to cleanup vector store {vector_store.id} after assistant creation error: {vs_del_e}")
        raise HTTPException(status_code=500, detail=f"Failed to create assistant: {e}")

    try:
        thread = client.beta.threads.create()
        logging.info(f"Thread created: {thread.id}")
    except Exception as e:
        logging.exception("Failed to create thread")
        # Attempt cleanup
        try:
            client.beta.assistants.delete(assistant_id=assistant.id)
            logging.info(f"Cleaned up assistant {assistant.id} after failed thread creation.")
        except Exception as asst_del_e:
            logging.error(f"Failed to cleanup assistant {assistant.id} after thread creation error: {asst_del_e}")
        try:
            client.beta.vector_stores.delete(vector_store_id=vector_store.id)
            logging.info(f"Cleaned up vector store {vector_store.id} after failed thread creation.")
        except Exception as vs_del_e:
            logging.error(f"Failed to cleanup vector store {vector_store.id} after thread creation error: {vs_del_e}")
        raise HTTPException(status_code=500, detail=f"Failed to create thread: {e}")

    # --- Add Context and File (if provided) ---
    if context:
        await update_context(client, thread.id, context) # Error handling inside function

    if file:
        filename = file.filename
        file_content = None
        file_path = None # Define file_path here
        try:
            file_content = await file.read()
            # Use a temporary directory structure if needed, ensure cleanup
            temp_dir = "/tmp/fastapi_uploads" # Example temp dir
            os.makedirs(temp_dir, exist_ok=True)
            file_path = os.path.join(temp_dir, filename)
            with open(file_path, 'wb') as f:
                f.write(file_content)
            logging.info(f"File '{filename}' saved temporarily to '{file_path}'")

            file_ext = os.path.splitext(filename)[1].lower()
            is_csv = file_ext == '.csv'
            is_excel = file_ext in ['.xlsx', '.xls', '.xlsm']
            # More robust image check using content type if available
            content_type = file.content_type
            is_image = (file_ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']) or \
                       (content_type and content_type.startswith('image/'))
            is_doc = file_ext in ['.pdf', '.doc', '.docx', '.txt', '.md'] # Common doc types for vector store

            file_info = {
                "name": filename,
                "type": file_ext[1:] if file_ext else "unknown",
                "id": None, # Will be set if applicable
                "processing_method": ""
            }

            if is_csv or is_excel:
                file_info["processing_method"] = "code_interpreter"
                file_info["type"] = "csv" if is_csv else "excel"
                with open(file_path, "rb") as file_stream:
                    uploaded_file = client.files.create(file=file_stream, purpose='assistants')
                file_info["id"] = uploaded_file.id
                code_interpreter_file_ids.append(uploaded_file.id)
                # Update assistant to include this file ID
                client.beta.assistants.update(
                    assistant_id=assistant.id,
                    tool_resources={
                        "code_interpreter": {"file_ids": code_interpreter_file_ids},
                        "file_search": {"vector_store_ids": [vector_store.id]}
                    }
                )
                logging.info(f"File {filename} ({uploaded_file.id}) added to assistant {assistant.id} for code interpreter.")
                await add_file_awareness(client, thread.id, file_info)

            elif is_image:
                file_info["processing_method"] = "thread_message"
                file_info["type"] = "image"
                # Ensure file_content is available from the read earlier
                if file_content:
                     analysis_text = await image_analysis(client, file_content, filename, None) # No specific prompt here
                     # Add analysis to thread FIRST
                     analysis_msg = client.beta.threads.messages.create(
                         thread_id=thread.id,
                         role="user", # Analysis result presented as if user provided it
                         content=f"--- Image Analysis Result for {filename} ---\n{analysis_text}\n--- End Analysis ---"
                     )
                     logging.info(f"Added image analysis message {analysis_msg.id} for {filename} to thread {thread.id}")
                     # THEN add awareness message
                     await add_file_awareness(client, thread.id, file_info)
                else:
                     logging.warning(f"File content was not read for image {filename}, skipping analysis.")


            elif is_doc: # Use vector store for known document types
                 file_info["processing_method"] = "vector_store"
                 with open(file_path, "rb") as file_stream:
                    # Use upload_and_poll for potentially long processing
                    file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                        vector_store_id=vector_store.id, files=[file_stream]
                    )
                 logging.info(f"File {filename} uploaded to vector store {vector_store.id}. Batch status: {file_batch.status}")
                 # Add awareness message AFTER upload completes
                 if file_batch.status == 'completed':
                     # Retrieve the actual file ID associated within the vector store if needed
                     # Note: The direct file ID might not be easily available from batch, depends on API version
                     # We'll use the filename for awareness here.
                     await add_file_awareness(client, thread.id, file_info)
                 else:
                      logging.error(f"Vector store upload failed for {filename}. Status: {file_batch.status}")
                      # Optionally inform the user/assistant
                      client.beta.threads.messages.create(
                          thread_id=thread.id, role="user",
                          content=f"SYSTEM ALERT: Failed to process document '{filename}' for retrieval."
                      )

            else: # Fallback for other unknown file types (treat as docs for vector store)
                 logging.warning(f"Unknown file type '{file_ext}' for {filename}. Attempting upload to vector store.")
                 file_info["processing_method"] = "vector_store"
                 try:
                     with open(file_path, "rb") as file_stream:
                         file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                             vector_store_id=vector_store.id, files=[file_stream]
                         )
                     logging.info(f"File {filename} (unknown type) uploaded to vector store {vector_store.id}. Batch status: {file_batch.status}")
                     if file_batch.status == 'completed':
                          await add_file_awareness(client, thread.id, file_info)
                     else:
                          logging.error(f"Vector store upload failed for {filename} (unknown type). Status: {file_batch.status}")
                          client.beta.threads.messages.create(
                              thread_id=thread.id, role="user",
                              content=f"SYSTEM ALERT: Failed to process file '{filename}' for retrieval."
                          )
                 except Exception as e:
                    logging.exception(f"Failed to upload fallback file {filename} to vector store.")
                    client.beta.threads.messages.create(
                        thread_id=thread.id, role="user",
                        content=f"SYSTEM ALERT: Error processing file '{filename}' for retrieval."
                    )

        except Exception as e:
            logging.exception(f"Error processing uploaded file {filename} during chat initiation")
            # Don't fail the entire chat initiation, but maybe inform the user
            try:
                client.beta.threads.messages.create(
                    thread_id=thread.id,
                    role="user",
                    content=f"SYSTEM ALERT: Failed to process the uploaded file '{filename}'. Error: {e}"
                )
            except Exception as msg_err:
                 logging.error(f"Failed to send file processing error message to thread {thread.id}: {msg_err}")

        finally:
             # Clean up the temporary file
             if file_path and os.path.exists(file_path):
                 try:
                     os.remove(file_path)
                     logging.info(f"Cleaned up temporary file: {file_path}")
                 except OSError as e:
                     logging.error(f"Error removing temporary file {file_path}: {e}")

    res = {
        "assistant": assistant.id,
        "session": thread.id, # Renamed 'thread' to 'session' for consistency with API params
        "vector_store": vector_store.id
    }

    return JSONResponse(content=res, status_code=200)


@app.post("/co-pilot")
async def co_pilot(request: Request):
    """
    Handles co-pilot assistant creation or updates. Can optionally add a file, system prompt, or context.
    Uses existing assistant/vector store if provided, otherwise creates new ones.
    """
    client = create_client()
    form = await request.form()

    assistant_id: Optional[str] = form.get("assistant", None)
    vector_store_id: Optional[str] = form.get("vector_store", None)
    thread_id: Optional[str] = form.get("session", None) # Use 'session' for thread ID consistency
    file: Optional[UploadFile] = form.get("file", None)
    system_prompt: Optional[str] = form.get("system_prompt", None)
    context: Optional[str] = form.get("context", None)

    assistant_obj = None
    vector_store_created = False

    try:
        # --- Determine Assistant and Vector Store ---
        if not assistant_id:
            logging.info("No assistant ID provided. Creating new assistant and vector store.")
            # Create Vector Store first
            if not vector_store_id:
                vector_store = client.beta.vector_stores.create(name=f"CoPilotStore_{int(time.time())}")
                vector_store_id = vector_store.id
                vector_store_created = True
                logging.info(f"Created new vector store: {vector_store_id}")

            # Prepare tools and resources for new assistant
            base_prompt = "You are a product management AI assistant, a product co-pilot."
            instructions = f"{base_prompt} {system_prompt}" if system_prompt else base_prompt
            assistant_tools = [{"type": "code_interpreter"}, {"type": "file_search"}]
            assistant_tool_resources = {
                "file_search": {"vector_store_ids": [vector_store_id]},
                "code_interpreter": {"file_ids": []} # Start with empty list
            }
            # Create Assistant
            assistant_obj = client.beta.assistants.create(
                name="demo_co_pilot", # Consider a more dynamic name
                model="gpt-4o-mini",
                instructions=instructions,
                tools=assistant_tools,
                tool_resources=assistant_tool_resources,
            )
            assistant_id = assistant_obj.id
            logging.info(f"Created new assistant: {assistant_id}")
        else:
            # Assistant ID provided, retrieve it
            logging.info(f"Using existing assistant ID: {assistant_id}")
            assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant_id)

            # Update instructions if system_prompt is provided
            if system_prompt:
                base_prompt = "You are a product management AI assistant, a product co-pilot."
                instructions = f"{base_prompt} {system_prompt}"
                client.beta.assistants.update(assistant_id=assistant_id, instructions=instructions)
                logging.info(f"Updated instructions for assistant {assistant_id}")

            # Ensure Vector Store ID exists and is linked
            if not vector_store_id:
                # Check if assistant already has a vector store linked
                current_tool_resources = getattr(assistant_obj, "tool_resources", None)
                file_search_res = getattr(current_tool_resources, "file_search", None)
                existing_vs_ids = getattr(file_search_res, "vector_store_ids", [])

                if existing_vs_ids:
                    vector_store_id = existing_vs_ids[0] # Use the first existing one
                    logging.info(f"Found existing vector store {vector_store_id} linked to assistant {assistant_id}")
                else:
                    # No vector store linked, create one and update assistant
                    logging.info(f"No vector store linked to assistant {assistant_id}. Creating a new one.")
                    vector_store = client.beta.vector_stores.create(name=f"CoPilotStore_{assistant_id}_{int(time.time())}")
                    vector_store_id = vector_store.id
                    vector_store_created = True
                    logging.info(f"Created new vector store: {vector_store_id}")

                    # Ensure file_search tool exists and update resources
                    current_tools = assistant_obj.tools if assistant_obj.tools else []
                    has_file_search = any(t.type == "file_search" for t in current_tools)
                    if not has_file_search:
                        current_tools.append({"type": "file_search"})

                    # Preserve existing code interpreter files if any
                    code_interpreter_res = getattr(current_tool_resources, "code_interpreter", None)
                    code_interpreter_fids = getattr(code_interpreter_res, "file_ids", [])

                    client.beta.assistants.update(
                        assistant_id=assistant_id,
                        tools=current_tools,
                        tool_resources={
                            "file_search": {"vector_store_ids": [vector_store_id]},
                            "code_interpreter": {"file_ids": code_interpreter_fids}
                        }
                    )
                    logging.info(f"Linked new vector store {vector_store_id} to assistant {assistant_id}")
            else:
                 logging.info(f"Using provided vector store ID: {vector_store_id}")
                 # Optional: Verify the vector store exists and is linked? Maybe too complex here.

        # --- Handle File Upload (if present) ---
        if file:
            filename = file.filename
            file_content = None
            file_path = None
            try:
                file_content = await file.read()
                temp_dir = "/tmp/fastapi_uploads"
                os.makedirs(temp_dir, exist_ok=True)
                file_path = os.path.join(temp_dir, filename)
                with open(file_path, 'wb') as f:
                    f.write(file_content)
                logging.info(f"File '{filename}' saved temporarily to '{file_path}' for co-pilot endpoint")

                file_ext = os.path.splitext(filename)[1].lower()
                is_csv = file_ext == '.csv'
                is_excel = file_ext in ['.xlsx', '.xls', '.xlsm']
                content_type = file.content_type
                is_image = (file_ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']) or \
                           (content_type and content_type.startswith('image/'))
                is_doc = file_ext in ['.pdf', '.doc', '.docx', '.txt', '.md']

                file_info = {
                    "name": filename,
                    "type": file_ext[1:] if file_ext else "unknown",
                    "id": None,
                    "processing_method": ""
                }

                if is_csv or is_excel:
                    file_info["processing_method"] = "code_interpreter"
                    file_info["type"] = "csv" if is_csv else "excel"
                    with open(file_path, "rb") as file_stream:
                        uploaded_file = client.files.create(file=file_stream, purpose='assistants')
                    file_info["id"] = uploaded_file.id

                    # Retrieve current list of file IDs and append
                    assistant_obj_updated = client.beta.assistants.retrieve(assistant_id=assistant_id) # Re-fetch latest state
                    current_tool_resources = getattr(assistant_obj_updated, "tool_resources", None)
                    code_interpreter_res = getattr(current_tool_resources, "code_interpreter", None)
                    current_fids = list(getattr(code_interpreter_res, "file_ids", [])) # Ensure it's a list

                    if uploaded_file.id not in current_fids:
                         current_fids.append(uploaded_file.id)

                    # Update assistant resources
                    file_search_res = getattr(current_tool_resources, "file_search", None)
                    vs_ids = getattr(file_search_res, "vector_store_ids", [vector_store_id]) # Use current VS ID

                    client.beta.assistants.update(
                        assistant_id=assistant_id,
                        tool_resources={
                            "code_interpreter": {"file_ids": current_fids},
                            "file_search": {"vector_store_ids": vs_ids}
                        }
                    )
                    logging.info(f"File {filename} ({uploaded_file.id}) added to assistant {assistant_id} for code interpreter.")
                    # Add awareness only if thread_id is available
                    if thread_id:
                        await add_file_awareness(client, thread_id, file_info)

                elif is_image:
                    if not thread_id:
                        logging.warning(f"Image file {filename} uploaded to /co-pilot without a session ID. Analysis cannot be added to a thread.")
                        # Optionally raise error or just log
                        # raise HTTPException(status_code=400, detail="Image uploads require a 'session' (thread_id) to add analysis.")
                    else:
                        file_info["processing_method"] = "thread_message"
                        file_info["type"] = "image"
                        if file_content:
                            analysis_text = await image_analysis(client, file_content, filename, None)
                            analysis_msg = client.beta.threads.messages.create(
                                thread_id=thread_id, role="user",
                                content=f"--- Image Analysis Result for {filename} ---\n{analysis_text}\n--- End Analysis ---"
                            )
                            logging.info(f"Added image analysis message {analysis_msg.id} for {filename} to thread {thread_id}")
                            await add_file_awareness(client, thread_id, file_info)
                        else:
                            logging.warning(f"File content was not read for image {filename}, skipping analysis.")

                elif is_doc:
                    file_info["processing_method"] = "vector_store"
                    with open(file_path, "rb") as file_stream:
                         file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                             vector_store_id=vector_store_id, files=[file_stream]
                         )
                    logging.info(f"File {filename} uploaded to vector store {vector_store_id}. Batch status: {file_batch.status}")
                    if file_batch.status == 'completed' and thread_id:
                         await add_file_awareness(client, thread_id, file_info)
                    elif file_batch.status != 'completed':
                         logging.error(f"Vector store upload failed for {filename}. Status: {file_batch.status}")
                         if thread_id:
                              client.beta.threads.messages.create(
                                  thread_id=thread_id, role="user",
                                  content=f"SYSTEM ALERT: Failed to process document '{filename}' for retrieval."
                              )

                else: # Fallback for other types to vector store
                    logging.warning(f"Unknown file type '{file_ext}' for {filename} in /co-pilot. Attempting vector store upload.")
                    file_info["processing_method"] = "vector_store"
                    try:
                        with open(file_path, "rb") as file_stream:
                            file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                                vector_store_id=vector_store_id, files=[file_stream]
                            )
                        logging.info(f"File {filename} (unknown type) uploaded to vector store {vector_store_id}. Status: {file_batch.status}")
                        if file_batch.status == 'completed' and thread_id:
                            await add_file_awareness(client, thread_id, file_info)
                        elif file_batch.status != 'completed':
                            logging.error(f"Vector store upload failed for {filename} (unknown type). Status: {file_batch.status}")
                            if thread_id:
                                client.beta.threads.messages.create(
                                    thread_id=thread_id, role="user",
                                    content=f"SYSTEM ALERT: Failed to process file '{filename}' for retrieval."
                                )
                    except Exception as e:
                        logging.exception(f"Failed to upload fallback file {filename} to vector store in /co-pilot.")
                        if thread_id:
                             client.beta.threads.messages.create(
                                 thread_id=thread_id, role="user",
                                 content=f"SYSTEM ALERT: Error processing file '{filename}' for retrieval."
                             )

            except Exception as e:
                logging.exception(f"Error processing uploaded file {filename} in /co-pilot")
                if thread_id:
                    try:
                        client.beta.threads.messages.create(
                            thread_id=thread_id, role="user",
                            content=f"SYSTEM ALERT: Failed to process the uploaded file '{filename}'. Error: {e}"
                        )
                    except Exception as msg_err:
                        logging.error(f"Failed to send file processing error message to thread {thread_id}: {msg_err}")
            finally:
                 if file_path and os.path.exists(file_path):
                     try:
                         os.remove(file_path)
                         logging.info(f"Cleaned up temporary file: {file_path}")
                     except OSError as e:
                         logging.error(f"Error removing temporary file {file_path}: {e}")

        # --- Update Context (if thread and context provided) ---
        if context and thread_id:
            await update_context(client, thread_id, context)

        return JSONResponse(
            {
                "message": "Assistant operation completed successfully.",
                "assistant": assistant_id,
                "vector_store": vector_store_id,
                # Optionally return thread_id if it was used/created
                "session": thread_id if thread_id else None,
            },
            status_code=200
        )

    except Exception as e:
        logging.exception("Error in /co-pilot endpoint")
        # Attempt cleanup if resources were newly created in this call
        if vector_store_created and vector_store_id:
             try:
                 client.beta.vector_stores.delete(vector_store_id=vector_store_id)
                 logging.info(f"Cleaned up vector store {vector_store_id} after error in /co-pilot")
             except Exception as vs_del_e:
                 logging.error(f"Failed to cleanup vector store {vector_store_id} after error: {vs_del_e}")
        # If assistant was created here, try deleting it (requires assistant_id to be set)
        # More complex cleanup might be needed depending on exact failure point
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")


@app.post("/upload-file")
async def upload_file(request: Request, file: UploadFile = Form(...), assistant: str = Form(...)): # Added request: Request
    """
    Uploads a file and associates it with the given assistant. Handles different file types.
    Requires 'assistant' ID. Optionally uses 'session' (thread_id) for context/awareness,
    'context' for user persona, and 'prompt' for image analysis guidance.
    """
    client = create_client()
    form = await request.form()
    # Use 'session' consistently for thread ID
    thread_id: Optional[str] = form.get("session", None)
    context: Optional[str] = form.get("context", None)
    prompt: Optional[str] = form.get("prompt", None) # Optional prompt for image analysis

    filename = file.filename
    file_content = None
    file_path = None

    try:
        # --- Retrieve Assistant and Validate ---
        try:
            assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant)
            logging.info(f"Retrieved assistant {assistant}")
        except Exception as e:
            logging.error(f"Failed to retrieve assistant {assistant}: {e}")
            raise HTTPException(status_code=404, detail=f"Assistant with ID '{assistant}' not found or error retrieving it.")

        # --- Save File Temporarily ---
        file_content = await file.read()
        temp_dir = "/tmp/fastapi_uploads"
        os.makedirs(temp_dir, exist_ok=True)
        file_path = os.path.join(temp_dir, filename)
        with open(file_path, "wb") as temp_file:
            temp_file.write(file_content)
        logging.info(f"File '{filename}' saved temporarily to '{file_path}' for /upload-file")

        # --- Determine File Type ---
        file_ext = os.path.splitext(filename)[1].lower()
        is_csv = file_ext == '.csv'
        is_excel = file_ext in ['.xlsx', '.xls', '.xlsm']
        content_type = file.content_type
        is_image = (file_ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']) or \
                   (content_type and content_type.startswith('image/'))
        is_doc = file_ext in ['.pdf', '.doc', '.docx', '.txt', '.md']

        file_info = {
            "name": filename,
            "type": file_ext[1:] if file_ext else "unknown",
            "id": None,
            "processing_method": ""
        }

        # --- Ensure Tools and Vector Store are Ready ---
        current_tools = assistant_obj.tools if assistant_obj.tools else []
        current_tool_resources = getattr(assistant_obj, "tool_resources", None)
        needs_update = False

        # Check for Code Interpreter
        has_code_interpreter = any(t.type == "code_interpreter" for t in current_tools)
        if (is_csv or is_excel) and not has_code_interpreter:
            logging.info(f"Adding 'code_interpreter' tool to assistant {assistant}")
            current_tools.append({"type": "code_interpreter"})
            needs_update = True

        # Check for File Search and Vector Store
        has_file_search = any(t.type == "file_search" for t in current_tools)
        file_search_res = getattr(current_tool_resources, "file_search", None)
        vector_store_ids = list(getattr(file_search_res, "vector_store_ids", [])) # Ensure list

        if is_doc or not (is_csv or is_excel or is_image): # If vector store is needed
            if not vector_store_ids:
                logging.info(f"No vector store linked to assistant {assistant}. Creating and linking a new one.")
                vector_store = client.beta.vector_stores.create(name=f"Assistant_{assistant}_Store_{int(time.time())}")
                vector_store_ids = [vector_store.id]
                if not has_file_search:
                    current_tools.append({"type": "file_search"})
                needs_update = True
            else:
                 logging.info(f"Using existing vector store(s): {vector_store_ids}")
            # Use the first vector store ID for uploading
            vector_store_id = vector_store_ids[0]
        else:
            vector_store_id = None # Not needed for CSV/Excel/Image only

        # Update assistant if tools or resources changed
        if needs_update:
            code_interpreter_res = getattr(current_tool_resources, "code_interpreter", None)
            code_interpreter_fids = getattr(code_interpreter_res, "file_ids", [])
            logging.info(f"Updating assistant {assistant} tools/resources.")
            client.beta.assistants.update(
                assistant_id=assistant,
                tools=current_tools,
                tool_resources={
                    "file_search": {"vector_store_ids": vector_store_ids},
                    "code_interpreter": {"file_ids": code_interpreter_fids}
                }
            )
            # Re-retrieve assistant object to have the latest state? Optional.
            # assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant)

        # --- Process File Based on Type ---
        response_data = {}
        status_code = 200

        if is_csv or is_excel:
            file_info["processing_method"] = "code_interpreter"
            file_info["type"] = "csv" if is_csv else "excel"
            with open(file_path, "rb") as file_stream:
                uploaded_file = client.files.create(file=file_stream, purpose='assistants')
            file_info["id"] = uploaded_file.id

            # Retrieve current file IDs and append
            # Re-fetch assistant state before update might be safer in concurrent scenarios
            assistant_obj_latest = client.beta.assistants.retrieve(assistant_id=assistant)
            latest_tool_resources = getattr(assistant_obj_latest, "tool_resources", None)
            code_interpreter_res = getattr(latest_tool_resources, "code_interpreter", None)
            current_fids = list(getattr(code_interpreter_res, "file_ids", []))
            if uploaded_file.id not in current_fids:
                 current_fids.append(uploaded_file.id)

            file_search_res = getattr(latest_tool_resources, "file_search", None) # Preserve file search resources
            vs_ids = getattr(file_search_res, "vector_store_ids", [])

            # Update assistant resources
            client.beta.assistants.update(
                assistant_id=assistant,
                tool_resources={
                    "code_interpreter": {"file_ids": current_fids},
                    "file_search": {"vector_store_ids": vs_ids}
                }
            )
            logging.info(f"File {filename} ({uploaded_file.id}) added to assistant {assistant} for code interpreter.")
            response_data = {"message": "File successfully uploaded for code interpreter.", "file_id": uploaded_file.id}
            if thread_id: await add_file_awareness(client, thread_id, file_info)

        elif is_image:
            # Image processing requires a thread_id to post the analysis
            if not thread_id:
                logging.error(f"Image file {filename} uploaded to /upload-file without a 'session' ID.")
                raise HTTPException(status_code=400, detail="Image uploads require a 'session' (thread_id) parameter to add the analysis.")
            else:
                file_info["processing_method"] = "thread_message"
                file_info["type"] = "image"
                if file_content:
                    analysis_text = await image_analysis(client, file_content, filename, prompt) # Pass optional prompt
                    analysis_msg = client.beta.threads.messages.create(
                        thread_id=thread_id, role="user",
                        content=f"--- Image Analysis Result for {filename} ---\n{analysis_text}\n--- End Analysis ---"
                    )
                    logging.info(f"Added image analysis message {analysis_msg.id} for {filename} to thread {thread_id}")
                    await add_file_awareness(client, thread_id, file_info) # Add awareness after analysis msg
                    response_data = {"message": "Image successfully analyzed and added to thread.", "image_analyzed": True}
                else:
                     # Should not happen if file read succeeded earlier, but as safeguard:
                     logging.error(f"File content missing for image {filename} during processing.")
                     raise HTTPException(status_code=500, detail="Failed to read image content.")

        elif is_doc or not (is_csv or is_excel or is_image): # Handle docs and fallback types
             if not vector_store_id: # Should have been created if needed, but double check
                 logging.error(f"Vector store ID missing for assistant {assistant} when trying to upload document {filename}.")
                 raise HTTPException(status_code=500, detail="Vector store configuration error for assistant.")

             file_info["processing_method"] = "vector_store"
             logging.info(f"Uploading file {filename} to vector store {vector_store_id}")
             with open(file_path, "rb") as file_stream:
                 file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                     vector_store_id=vector_store_id, files=[file_stream]
                 )
             logging.info(f"File {filename} upload to vector store {vector_store_id} status: {file_batch.status}")

             if file_batch.status == 'completed':
                 response_data = {"message": "File successfully uploaded to vector store."}
                 if thread_id: await add_file_awareness(client, thread_id, file_info)
             else:
                 logging.error(f"Vector store upload failed for {filename}. Status: {file_batch.status}")
                 # Don't raise HTTP error, but report failure in response
                 response_data = {"message": f"File upload to vector store failed. Status: {file_batch.status}"}
                 status_code = 500 # Indicate server-side issue
                 if thread_id:
                     client.beta.threads.messages.create(
                         thread_id=thread_id, role="user",
                         content=f"SYSTEM ALERT: Failed to process file '{filename}' for retrieval."
                     )

        # --- Update Context if provided ---
        if context and thread_id:
            await update_context(client, thread_id, context)

        return JSONResponse(content=response_data, status_code=status_code)

    except HTTPException as http_exc:
         raise http_exc # Re-raise FastAPI exceptions
    except Exception as e:
        logging.exception(f"Error uploading file {filename} for assistant {assistant}")
        raise HTTPException(status_code=500, detail=f"Failed to upload file: {str(e)}")
    finally:
        # Clean up the temporary file
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                logging.info(f"Cleaned up temporary file: {file_path}")
            except OSError as e:
                logging.error(f"Error removing temporary file {file_path}: {e}")


@app.get("/conversation")
async def conversation(
    session: Optional[str] = None, # Renamed thread_id to session
    prompt: Optional[str] = None,
    assistant: Optional[str] = None,
    context: Optional[str] = None
):
    """
    Handles streaming conversation queries. Creates assistant/session if not provided.
    """
    client = create_client()
    is_new_session = False

    try:
        # If no assistant provided, create a default one (consider if this is desired)
        if not assistant:
            logging.warning("No assistant ID provided for /conversation. Creating a default temporary assistant.")
            # Note: This default assistant won't have file search enabled by default without a vector store
            try:
                assistant_obj = client.beta.assistants.create(
                    name="default_conversation_assistant",
                    model="gpt-4o-mini",
                    instructions="You are a helpful conversation assistant.",
                    # Add tools if needed, e.g., {"type": "code_interpreter"}
                )
                assistant = assistant_obj.id
                logging.info(f"Created default assistant {assistant} for conversation.")
            except Exception as e:
                 logging.exception("Failed to create default assistant for /conversation")
                 raise HTTPException(status_code=500, detail="Failed to initialize assistant.")

        # If no session (thread) provided, create one
        if not session:
            logging.info("No session ID provided for /conversation. Creating a new thread.")
            try:
                thread = client.beta.threads.create()
                session = thread.id
                is_new_session = True
                logging.info(f"Created new thread {session} for conversation.")
            except Exception as e:
                 logging.exception("Failed to create new thread for /conversation")
                 raise HTTPException(status_code=500, detail="Failed to initialize session.")

        # Add context if provided (especially important for new sessions)
        if context:
             # Add context only if it's a new session or if explicitly provided again
             # Logic simplified: always update if context is present
             await update_context(client, session, context)

        # Add user message if prompt is given
        if prompt:
            try:
                client.beta.threads.messages.create(
                    thread_id=session,
                    role="user",
                    content=prompt
                )
                logging.info(f"Added prompt to thread {session}")
            except Exception as e:
                 logging.exception(f"Failed to add prompt to thread {session}")
                 raise HTTPException(status_code=500, detail="Failed to add message to conversation.")

        # --- Stream the response ---
        def stream_response():
            buffer = []
            try:
                logging.info(f"Starting stream for thread {session} with assistant {assistant}")
                with client.beta.threads.runs.stream(
                    thread_id=session,
                    assistant_id=assistant
                    # Potentially add event handlers here for tool calls, etc.
                    # event_handler=EventHandler() # Custom handler needed
                ) as stream:
                    for text in stream.text_deltas:
                        print(text, end="", flush=True) # Also print to server console for debugging
                        buffer.append(text)
                        # Yield chunks periodically to avoid large buffer buildup
                        # Adjust chunk size based on expected latency and throughput needs
                        if len(buffer) >= 5: # Smaller chunk size for lower latency perception
                            yield ''.join(buffer)
                            buffer = []
                    # Yield any remaining text in the buffer
                    if buffer:
                        yield ''.join(buffer)
                logging.info(f"Stream finished for thread {session}")
            except Exception as e:
                logging.exception(f"Streaming error in thread {session}")
                # Yield an error message within the stream
                yield f"\n[STREAM_ERROR] An error occurred during the response generation: {str(e)}. Please try again."

        # Use text/event-stream for standard streaming responses
        return StreamingResponse(stream_response(), media_type="text/event-stream")

    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        logging.exception("Error in /conversation endpoint")
        raise HTTPException(status_code=500, detail=f"Failed to process conversation: {str(e)}")


@app.get("/chat")
async def chat(
    session: Optional[str] = None, # Renamed thread_id to session
    prompt: Optional[str] = None,
    assistant: Optional[str] = None,
    context: Optional[str] = None
):
    """
    Handles non-streaming conversation queries. Creates assistant/session if not provided.
    Returns the complete response as JSON.
    """
    client = create_client()
    is_new_session = False

    try:
        # If no assistant provided, create a default one
        if not assistant:
            logging.warning("No assistant ID provided for /chat. Creating a default temporary assistant.")
            try:
                assistant_obj = client.beta.assistants.create(
                    name="default_chat_assistant", model="gpt-4o-mini",
                    instructions="You are a helpful chat assistant."
                )
                assistant = assistant_obj.id
                logging.info(f"Created default assistant {assistant} for chat.")
            except Exception as e:
                 logging.exception("Failed to create default assistant for /chat")
                 raise HTTPException(status_code=500, detail="Failed to initialize assistant.")

        # If no session (thread) provided, create one
        if not session:
            logging.info("No session ID provided for /chat. Creating a new thread.")
            try:
                thread = client.beta.threads.create()
                session = thread.id
                is_new_session = True
                logging.info(f"Created new thread {session} for chat.")
            except Exception as e:
                 logging.exception("Failed to create new thread for /chat")
                 raise HTTPException(status_code=500, detail="Failed to initialize session.")

        # Add context if provided
        if context:
            await update_context(client, session, context)

        # Add user message if prompt is given
        if prompt:
            try:
                client.beta.threads.messages.create(thread_id=session, role="user", content=prompt)
                logging.info(f"Added prompt to thread {session}")
            except Exception as e:
                 logging.exception(f"Failed to add prompt to thread {session}")
                 raise HTTPException(status_code=500, detail="Failed to add message to chat.")

        # --- Run and wait for completion ---
        response_text_parts = []
        try:
            logging.info(f"Starting run for thread {session} with assistant {assistant}")
            # Use stream and collect results for non-streaming endpoint
            with client.beta.threads.runs.stream(thread_id=session, assistant_id=assistant) as stream:
                for text in stream.text_deltas:
                    response_text_parts.append(text)
                # Wait for run completion to ensure all steps finished (useful if tools are used)
                run = stream.current_run
                logging.info(f"Run {run.id} completed with status: {run.status} for thread {session}")
                if run.status != 'completed':
                     logging.error(f"Run {run.id} did not complete successfully. Final status: {run.status}")
                     # Include error details if available
                     error_details = run.last_error if hasattr(run, 'last_error') and run.last_error else "No details available."
                     raise HTTPException(status_code=500, detail=f"Assistant run failed with status {run.status}. Error: {error_details}")

            full_response = ''.join(response_text_parts)
            logging.info(f"Completed run for thread {session}. Response length: {len(full_response)}")
            return JSONResponse(content={"response": full_response})

        except APIError as apie:
             logging.exception(f"API error during chat run for thread {session}")
             raise HTTPException(status_code=apie.status_code, detail=f"API Error: {apie.message}")
        except Exception as e:
            logging.exception(f"Error during chat run for thread {session}")
            raise HTTPException(status_code=500, detail=f"An error occurred during response generation: {str(e)}")

    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        logging.exception("Error in /chat endpoint setup")
        raise HTTPException(status_code=500, detail=f"Failed to process chat request: {str(e)}")


@app.post("/trim-thread")
async def trim_thread(request: Request, assistant_id: Optional[str] = None, max_age_days: Optional[int] = None):
    """
    (Experimental) Summarizes and deletes old threads associated with an assistant.
    Note: Uses `list_all_runs` which might be inefficient or require specific API access.
    Summaries are logged, not persistently stored. Use with caution.
    """
    form_data = await request.form()
    if not assistant_id:
        assistant_id = form_data.get("assistant_id")
    if max_age_days is None:
        max_age_days_str = form_data.get("max_age_days")
        if max_age_days_str:
            try:
                max_age_days = int(max_age_days_str)
            except (ValueError, TypeError):
                max_age_days = None

    # Default threshold: 48 hours (2 days)
    time_threshold_hours = (max_age_days * 24) if max_age_days is not None else 48

    if not assistant_id:
        raise HTTPException(status_code=400, detail="assistant_id is required via query parameter or form data.")

    client = create_client()
    deleted_count = 0
    summarized_count = 0
    processed_thread_ids = set() # Track processed threads to avoid duplicates if run appears multiple times

    logging.info(f"Starting thread trimming for assistant {assistant_id} with threshold {time_threshold_hours} hours.")

    try:
        # Step 1: Get runs associated with the assistant to find threads
        # Warning: `list_all_runs` might be inefficient/non-standard.
        # Consider alternative strategies if this fails or is too slow.
        # A potentially more robust but complex way: list threads, then check runs for each thread.
        threads_activity = {} # Store thread_id -> latest_activity_timestamp

        try:
             # Assuming list_all_runs exists and works as intended. Add pagination if needed.
             runs = client.beta.threads.runs.list_all_runs(assistant_id=assistant_id, limit=100, order='desc') # Filter by assistant if API supports
             logging.info(f"Retrieved {len(runs.data)} runs potentially associated with assistant {assistant_id}.")

             for run in runs.data:
                  # Ensure run has the assistant_id attribute and thread_id
                  if hasattr(run, 'assistant_id') and run.assistant_id == assistant_id and hasattr(run, 'thread_id'):
                       thread_id = run.thread_id
                       run_created_at = datetime.datetime.fromtimestamp(run.created_at, tz=datetime.timezone.utc) # Use timezone aware

                       if thread_id not in threads_activity or run_created_at > threads_activity[thread_id]:
                           threads_activity[thread_id] = run_created_at

        except AttributeError:
             logging.warning("`list_all_runs` might not be available or doesn't support assistant_id filter. Falling back to listing threads (less accurate for assistant association).")
             # Fallback: List threads and use thread metadata (less reliable for activity time/assistant link)
             # This part needs a proper implementation if list_all_runs fails. For now, log warning.
             raise HTTPException(status_code=501, detail="Thread trimming requires specific run listing capabilities not fully confirmed.")
        except Exception as e:
             logging.exception("Error retrieving runs for thread trimming.")
             raise HTTPException(status_code=500, detail=f"Failed to retrieve run data: {e}")


        # Sort threads by last activity (most recent first)
        sorted_thread_ids = sorted(threads_activity.keys(), key=lambda tid: threads_activity[tid], reverse=True)
        logging.info(f"Found {len(sorted_thread_ids)} unique threads associated with assistant {assistant_id} via runs.")

        now_utc = datetime.datetime.now(datetime.timezone.utc) # Use timezone aware comparison

        # Step 2: Process each thread
        for thread_id in sorted_thread_ids:
            if thread_id in processed_thread_ids:
                continue # Skip if already processed (e.g., via multiple runs)

            last_active = threads_activity[thread_id]
            thread_age_hours = (now_utc - last_active).total_seconds() / 3600

            # Skip very recent threads (e.g., < 2 hours to allow active use)
            if thread_age_hours <= 2:
                 logging.info(f"Skipping recent thread {thread_id} (age: {thread_age_hours:.2f} hours)")
                 processed_thread_ids.add(thread_id)
                 continue

            try:
                thread = client.beta.threads.retrieve(thread_id=thread_id)
                metadata = thread.metadata if hasattr(thread, 'metadata') else {}

                # Check if it's a summary thread (already summarized) and old enough to delete
                if metadata and metadata.get('is_summary') == True: # Explicit check for True
                    if thread_age_hours > time_threshold_hours:
                        logging.info(f"Deleting old summary thread {thread_id} (age: {thread_age_hours:.2f} hours)")
                        try:
                            client.beta.threads.delete(thread_id=thread_id)
                            deleted_count += 1
                        except Exception as del_e:
                             logging.error(f"Failed to delete summary thread {thread_id}: {del_e}")
                    else:
                         logging.info(f"Keeping summary thread {thread_id} (age: {thread_age_hours:.2f} hours)")
                    processed_thread_ids.add(thread_id)
                    continue # Move to next thread

                # If it's a regular thread and older than the threshold, summarize and delete
                if thread_age_hours > time_threshold_hours:
                    logging.info(f"Processing old regular thread {thread_id} (age: {thread_age_hours:.2f} hours) for summarization.")
                    # Get messages (limit to avoid excessive length, e.g., last 50)
                    messages = client.beta.threads.messages.list(thread_id=thread_id, limit=50, order='asc') # Ascending for chronological summary prompt
                    message_data = list(messages.data)

                    if not message_data:
                         logging.info(f"Thread {thread_id} has no messages. Deleting directly.")
                         try:
                             client.beta.threads.delete(thread_id=thread_id)
                             deleted_count += 1
                         except Exception as del_e:
                             logging.error(f"Failed to delete empty thread {thread_id}: {del_e}")
                         processed_thread_ids.add(thread_id)
                         continue

                    # Construct content for summarization prompt
                    summary_content_parts = []
                    for msg in message_data:
                         role = getattr(msg, 'role', 'unknown')
                         content_list = getattr(msg, 'content', [])
                         text_value = ""
                         if content_list and hasattr(content_list[0], 'text') and hasattr(content_list[0].text, 'value'):
                              text_value = content_list[0].text.value
                         # Keep it concise for the summary prompt
                         summary_content_parts.append(f"{role}: {text_value[:200]}...") # Limit length per message

                    summary_prompt_content = "\n".join(summary_content_parts)
                    full_prompt = f"Please provide a concise summary (1-2 paragraphs) of the following conversation excerpt:\n\n---\n{summary_prompt_content}\n---"

                    # Create a temporary thread for the summary generation run
                    summary_run_thread = None
                    try:
                        summary_run_thread = client.beta.threads.create(
                             messages=[{"role": "user", "content": full_prompt}],
                             metadata={"purpose": "temp_summary_generation", "original_thread_id": thread_id}
                        )
                        logging.info(f"Created temporary thread {summary_run_thread.id} for summarizing {thread_id}")

                        # Run the assistant on the temporary thread to get the summary
                        summary_run = client.beta.threads.runs.create_and_poll(
                             thread_id=summary_run_thread.id,
                             assistant_id=assistant_id, # Use the same assistant
                             # Timeout after reasonable period, e.g., 60 seconds
                             poll_interval_ms=5000, # Check every 5s
                             # create_and_poll doesn't have timeout param, implement manually if needed or use stream + wait
                        )
                        logging.info(f"Summary run {summary_run.id} status for thread {thread_id}: {summary_run.status}")

                        if summary_run.status == "completed":
                            summary_messages = client.beta.threads.messages.list(
                                 thread_id=summary_run_thread.id, order="desc" # Get latest message first
                            )
                            summary_text = "Summary could not be retrieved."
                            for msg in summary_messages.data:
                                 if msg.role == "assistant":
                                      content_list = getattr(msg, 'content', [])
                                      if content_list and hasattr(content_list[0], 'text') and hasattr(content_list[0].text, 'value'):
                                           summary_text = content_list[0].text.value
                                           break # Found the assistant's summary

                            logging.info(f"Summary for thread {thread_id}: {summary_text}")
                            summarized_count += 1

                            # --- Now, decide what to do with the summary ---
                            # Option 1: Log it (as done here)
                            # Option 2: Store it externally (DB, file, etc.) - Requires major changes
                            # Option 3: Create a *new persistent summary thread* (adds complexity)
                            # For minimal change, we just log it.

                            # Delete the original thread
                            logging.info(f"Deleting original thread {thread_id} after summarization.")
                            try:
                                client.beta.threads.delete(thread_id=thread_id)
                                deleted_count += 1
                            except Exception as del_e:
                                logging.error(f"Failed to delete original thread {thread_id} after summarization: {del_e}")

                        else:
                            logging.error(f"Summarization run for thread {thread_id} failed or timed out. Status: {summary_run.status}. Original thread will not be deleted.")
                            # Optionally: Add metadata to original thread indicating failed summary attempt?

                    except Exception as summary_e:
                         logging.exception(f"Error during summarization process for thread {thread_id}")
                    finally:
                         # Clean up the temporary summary run thread regardless of outcome
                         if summary_run_thread:
                              try:
                                   client.beta.threads.delete(thread_id=summary_run_thread.id)
                                   logging.info(f"Deleted temporary summary thread {summary_run_thread.id}")
                              except Exception as temp_del_e:
                                   logging.error(f"Failed to delete temporary summary thread {summary_run_thread.id}: {temp_del_e}")

                    processed_thread_ids.add(thread_id) # Mark as processed

                else:
                     # Thread is not old enough to be summarized/deleted
                     logging.info(f"Keeping regular thread {thread_id} (age: {thread_age_hours:.2f} hours)")
                     processed_thread_ids.add(thread_id)

            except Exception as e:
                logging.exception(f"Error processing thread {thread_id} during trimming")
                processed_thread_ids.add(thread_id) # Mark as processed to avoid retrying in this run
                continue # Move to next thread

        return JSONResponse({
            "status": "Thread trimming process completed",
            "assistant_id": assistant_id,
            "threads_considered": len(sorted_thread_ids),
            "threads_summarized_and_deleted": summarized_count,
            "threads_deleted_directly": deleted_count - summarized_count, # Those deleted because old summary or empty
            "total_threads_deleted": deleted_count,
        })

    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        logging.exception(f"Error in /trim-thread endpoint for assistant {assistant_id}")
        raise HTTPException(status_code=500, detail=f"Failed to trim threads: {str(e)}")


@app.post("/file-cleanup")
async def file_cleanup(request: Request, vector_store_id: Optional[str] = None, assistant_id: Optional[str] = None):
    """
    Cleans up files associated with a vector store or an assistant's code interpreter.
    - Vector Store: Removes files in batches older than 48 hours.
    - Code Interpreter: Removes file references from the assistant AND deletes the actual files via the files API.
    Requires either vector_store_id or assistant_id.
    """
    form_data = await request.form()
    if not vector_store_id:
        vector_store_id = form_data.get("vector_store_id")
    if not assistant_id:
        assistant_id = form_data.get("assistant_id")

    if not vector_store_id and not assistant_id:
        raise HTTPException(status_code=400, detail="Either 'vector_store_id' or 'assistant_id' is required via query parameter or form data.")

    client = create_client()
    deleted_vector_files_count = 0
    cleared_code_interpreter_files_count = 0
    deleted_actual_files_count = 0
    skipped_batches_count = 0
    processed_batches = set()

    cleanup_results = {
         "vector_store_cleanup": {"processed": False, "deleted_files": 0, "skipped_batches": 0, "error": None},
         "code_interpreter_cleanup": {"processed": False, "cleared_references": 0, "deleted_files": 0, "error": None}
    }

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    time_threshold_hours = 48 # Hardcoded threshold for file cleanup

    # --- Vector Store File Cleanup ---
    if vector_store_id:
        cleanup_results["vector_store_cleanup"]["processed"] = True
        logging.info(f"Starting vector store file cleanup for VS ID: {vector_store_id}")
        try:
            # List file batches associated with the vector store
            # Add pagination if you expect many batches - API defaults may limit results
            file_batches = client.beta.vector_stores.file_batches.list(vector_store_id=vector_store_id, limit=100, order='desc')

            if not file_batches.data:
                logging.info(f"No file batches found in vector store {vector_store_id}.")
            else:
                logging.info(f"Found {len(file_batches.data)} file batches in vector store {vector_store_id}.")
                for batch in file_batches.data:
                    if batch.id in processed_batches: continue

                    batch_created_at = datetime.datetime.fromtimestamp(batch.created_at, tz=datetime.timezone.utc)
                    batch_age_hours = (now_utc - batch_created_at).total_seconds() / 3600

                    if batch_age_hours <= time_threshold_hours:
                        logging.info(f"Skipping recent batch {batch.id} (age: {batch_age_hours:.2f} hours)")
                        skipped_batches_count += 1
                        processed_batches.add(batch.id)
                        continue

                    logging.info(f"Processing old batch {batch.id} (age: {batch_age_hours:.2f} hours) for file deletion.")
                    try:
                        # List files within this specific old batch
                        # Add pagination if batches can contain many files
                        files_in_batch = client.beta.vector_stores.files.list(
                            vector_store_id=vector_store_id,
                            batch_id=batch.id, # Use batch_id filter if available, else list all files and filter
                            limit=100
                        )
                        logging.info(f"Found {len(files_in_batch.data)} files in batch {batch.id}.")

                        # Delete each file found in the old batch
                        for vs_file in files_in_batch.data:
                            try:
                                delete_status = client.beta.vector_stores.files.delete(
                                    vector_store_id=vector_store_id,
                                    file_id=vs_file.id
                                )
                                if delete_status.deleted:
                                     logging.info(f"Deleted vector store file {vs_file.id} from batch {batch.id}")
                                     deleted_vector_files_count += 1
                                else:
                                     logging.warning(f"Deletion reported as unsuccessful for vector store file {vs_file.id}")
                            except Exception as file_del_e:
                                logging.error(f"Error deleting vector store file {vs_file.id}: {file_del_e}")
                        processed_batches.add(batch.id)

                    except Exception as batch_proc_e:
                         logging.error(f"Error processing files within batch {batch.id}: {batch_proc_e}")
                         # Continue to next batch

            cleanup_results["vector_store_cleanup"]["deleted_files"] = deleted_vector_files_count
            cleanup_results["vector_store_cleanup"]["skipped_batches"] = skipped_batches_count

        except Exception as vs_e:
            logging.exception(f"Error during vector store cleanup for {vector_store_id}")
            cleanup_results["vector_store_cleanup"]["error"] = str(vs_e)

    # --- Code Interpreter File Cleanup ---
    if assistant_id:
        cleanup_results["code_interpreter_cleanup"]["processed"] = True
        logging.info(f"Starting code interpreter file cleanup for Assistant ID: {assistant_id}")
        try:
            assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant_id)
            tool_resources = getattr(assistant_obj, "tool_resources", None)
            code_interpreter_res = getattr(tool_resources, "code_interpreter", None)
            file_ids_to_clear = list(getattr(code_interpreter_res, "file_ids", []))

            if not file_ids_to_clear:
                logging.info(f"No code interpreter files currently associated with assistant {assistant_id}.")
            else:
                logging.info(f"Found {len(file_ids_to_clear)} code interpreter file references for assistant {assistant_id}.")

                # Step 1: Delete the actual files using the Files API
                for file_id in file_ids_to_clear:
                    try:
                        delete_status = client.files.delete(file_id=file_id)
                        if delete_status.deleted:
                            logging.info(f"Successfully deleted file {file_id} via Files API.")
                            deleted_actual_files_count += 1
                        else:
                            logging.warning(f"Deletion reported as unsuccessful for file {file_id} via Files API.")
                    except Exception as file_del_api_e:
                        # Log error but continue trying to clear references
                        logging.error(f"Error deleting file {file_id} via Files API: {file_del_api_e}. It might be orphaned.")

                # Step 2: Update the assistant to remove the references
                file_search_res = getattr(tool_resources, "file_search", None) # Preserve file search
                vs_ids = getattr(file_search_res, "vector_store_ids", [])

                client.beta.assistants.update(
                    assistant_id=assistant_id,
                    tool_resources={
                        "code_interpreter": {"file_ids": []}, # Set to empty list
                        "file_search": {"vector_store_ids": vs_ids}
                    }
                )
                cleared_code_interpreter_files_count = len(file_ids_to_clear)
                logging.info(f"Cleared {cleared_code_interpreter_files_count} code interpreter file references from assistant {assistant_id}.")

            cleanup_results["code_interpreter_cleanup"]["cleared_references"] = cleared_code_interpreter_files_count
            cleanup_results["code_interpreter_cleanup"]["deleted_files"] = deleted_actual_files_count

        except Exception as ci_e:
            logging.exception(f"Error during code interpreter cleanup for assistant {assistant_id}")
            cleanup_results["code_interpreter_cleanup"]["error"] = str(ci_e)

    # --- Return Combined Results ---
    final_status_code = 500 if (cleanup_results["vector_store_cleanup"]["error"] or cleanup_results["code_interpreter_cleanup"]["error"]) else 200

    return JSONResponse(
        content={
            "status": "File cleanup process finished.",
            "details": cleanup_results
        },
        status_code=final_status_code
    )


# --- Main Execution ---
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0")
    logging.info(f"Starting Uvicorn server on {host}:{port}")
    # Check for API key at startup
    if not AZURE_API_KEY:
         print("\n--- WARNING: AZURE_OPENAI_API_KEY environment variable is not set. API calls will fail. ---\n")
    uvicorn.run(app, host=host, port=port)
