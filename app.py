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
AZURE_API_KEY = "bc0ba854d3644d7998a5034af62d03ce"   # Replace with your key if different
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
            model="gpt-4o-mini", # Ensure this model supports vision
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": combined_prompt},
                    {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}}
                ]
            }],
            max_tokens=1000 # Increased max_tokens for potentially more detailed analysis
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
            # Check if metadata exists and is not None before accessing keys
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
            # No longer need specific Excel mention here as it's in the detailed instructions below
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
            role="user", # Sending as user so assistant 'sees' it as input/instruction
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
            model="gpt-4o-mini", # Ensure this model is deployed
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
        # Use a temporary directory that's more likely to be writable in various environments
        temp_dir = os.environ.get("TEMP", "/tmp") # Use TEMP env var if set, else default to /tmp
        if not os.path.exists(temp_dir):
             try:
                 os.makedirs(temp_dir)
             except OSError as e:
                 logging.error(f"Failed to create temporary directory {temp_dir}: {e}. Using current dir.")
                 temp_dir = "." # Fallback to current directory
        file_path = os.path.join(temp_dir, filename)

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
            is_document = file_ext in ['.pdf', '.doc', '.docx', '.txt', '.md', '.html', '.json'] # Common types for vector store

            file_info = {"name": filename}

            if is_csv or is_excel:
                # Upload to OpenAI files for code interpreter
                with open(file_path, "rb") as file_stream:
                    uploaded_file = client.files.create(
                        file=file_stream,
                        purpose='assistants' # Purpose must be 'assistants' for code interpreter/file search
                    )
                code_interpreter_file_ids.append(uploaded_file.id)

                # Update the assistant to link the file - uses the list populated above
                client.beta.assistants.update(
                    assistant_id=assistant.id,
                    tool_resources={
                        "code_interpreter": {"file_ids": code_interpreter_file_ids}, # This now contains the new ID
                        "file_search": {"vector_store_ids": [vector_store.id]} # Ensure VS link is included
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
                    role="user", # Add analysis as user message for context
                    content=f"Analysis result for uploaded image '{filename}':\n{analysis_text}"
                )
                file_info.update({
                    "type": "image",
                    "processing_method": "thread_message"
                })
                await add_file_awareness(client, thread.id, file_info)
                logging.info(f"Added image analysis for '{filename}' to thread {thread.id}")

            elif is_document or not (is_csv or is_excel or is_image): # Treat other types as documents for vector store
                # Upload to vector store
                with open(file_path, "rb") as file_stream:
                    # Use upload_and_poll for simplicity, handle potential long waits or switch to async polling if needed
                    file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                        vector_store_id=vector_store.id,
                        files=[file_stream]
                    )
                # Check batch status
                if file_batch.status == 'completed':
                    file_info.update({
                        "type": file_ext[1:] if file_ext else "document",
                        "processing_method": "vector_store"
                    })
                    await add_file_awareness(client, thread.id, file_info)
                    logging.info(f"File '{filename}' uploaded to vector store {vector_store.id}: status={file_batch.status}, count={file_batch.file_counts.total}")
                else:
                    logging.error(f"File batch upload for '{filename}' to vector store {vector_store.id} failed or timed out. Status: {file_batch.status}")
                    # Optionally, raise an error or return specific failure info
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
        "session": thread.id, # Use 'session' for thread_id consistency with other endpoints
        "vector_store": vector_store.id
    }

    return JSONResponse(res, status_code=200)


@app.post("/co-pilot")
async def co_pilot(request: Request): # Removed **kwargs, explicitly parse form
    """
    Creates or updates a co-pilot assistant. Handles optional file upload,
    system prompt update, and context setting. Designed for reuse.
    """
    client = create_client()

    # Parse the form data
    try:
        form = await request.form()
        file: Optional[UploadFile] = form.get("file", None)
        system_prompt: Optional[str] = form.get("system_prompt", None)
        context: Optional[str] = form.get("context", None)
        assistant_id: Optional[str] = form.get("assistant", None)
        vector_store_id: Optional[str] = form.get("vector_store", None)
        thread_id: Optional[str] = form.get("session", None) # Use 'session' for thread_id
    except Exception as e:
        logging.error(f"Error parsing form data: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid form data: {e}")

    code_interpreter_file_ids = [] # Will hold file IDs for the target assistant
    current_tools = []
    current_tool_resources = {}
    vector_store_ids_to_link = [] # Keep track of VS IDs for final update

    try:
        if not assistant_id:
            logging.info("No assistant ID provided, creating a new co-pilot assistant.")
            # Create VS if not provided
            if not vector_store_id:
                vector_store = client.beta.vector_stores.create(name=f"copilot_store_{int(time.time())}")
                vector_store_id = vector_store.id
                logging.info(f"Created new vector store for co-pilot: {vector_store_id}")
            vector_store_ids_to_link = [vector_store_id] # Use this new/provided VS

            # Use the comprehensive system prompt from /initiate-chat if none provided
            base_prompt = system_prompt or '''
                 You are a highly skilled Product Management AI Assistant and Co-Pilot...
                 [... rest of the detailed system prompt from initiate-chat ...]
                 - Gracefully manage any errors or uncertainties by informing the user and seeking clarification when necessary.
                 ''' # Truncated for brevity, assume full prompt is used

            current_tools = [{"type": "code_interpreter"}, {"type": "file_search"}]
            current_tool_resources = {
                 "file_search": {"vector_store_ids": vector_store_ids_to_link},
                 "code_interpreter": {"file_ids": []} # Start with empty list for new assistant
            }

            assistant = client.beta.assistants.create(
                name=f"copilot_assistant_{int(time.time())}",
                model="gpt-4o-mini",
                instructions=base_prompt, # Use the detailed prompt
                tools=current_tools,
                tool_resources=current_tool_resources,
            )
            assistant_id = assistant.id
            logging.info(f"Created new co-pilot assistant: {assistant_id}")

        else: # Assistant ID provided, retrieve and potentially update
            logging.info(f"Using existing assistant ID: {assistant_id}")
            assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant_id)
            needs_update = False # Flag to track if assistant update is needed

            # Update instructions if system_prompt is provided
            if system_prompt:
                # instructions = system_prompt # Use only the provided prompt if given
                # Let's assume we want to *replace* the instructions completely if a new one is provided
                client.beta.assistants.update(
                    assistant_id=assistant_id,
                    instructions=system_prompt,
                )
                logging.info(f"Updated instructions for assistant {assistant_id}")
                needs_update = True # Instructions were updated

            # Consolidate tools and resources from existing assistant
            current_tools = assistant_obj.tools if assistant_obj.tools else []
            current_tool_resources = assistant_obj.tool_resources if assistant_obj.tool_resources else {}

            # Ensure code interpreter tool exists
            if not any(getattr(tool, 'type', None) == "code_interpreter" for tool in current_tools):
                 current_tools.append({"type": "code_interpreter"})
                 needs_update = True

            # Ensure file search tool exists
            if not any(getattr(tool, 'type', None) == "file_search" for tool in current_tools):
                 current_tools.append({"type": "file_search"})
                 needs_update = True

            # Get current code interpreter file IDs
            ci_resources = getattr(current_tool_resources, "code_interpreter", None)
            if ci_resources and hasattr(ci_resources, "file_ids"):
                 code_interpreter_file_ids = list(ci_resources.file_ids)

            # Handle vector store ID consolidation
            fs_resources = getattr(current_tool_resources, "file_search", None)
            existing_vs_ids = list(fs_resources.vector_store_ids) if fs_resources and hasattr(fs_resources, "vector_store_ids") else []

            if vector_store_id: # User provided a specific VS
                 if vector_store_id not in existing_vs_ids:
                     vector_store_ids_to_link = existing_vs_ids + [vector_store_id] # Add the new one
                     logging.info(f"Associating provided vector store {vector_store_id} with assistant {assistant_id}")
                     needs_update = True
                 else:
                     vector_store_ids_to_link = existing_vs_ids # Use existing list including the provided one
            elif not existing_vs_ids: # No VS provided and none linked, create one
                vector_store = client.beta.vector_stores.create(name=f"copilot_store_{assistant_id}")
                vector_store_id = vector_store.id # Update vs_id for return value
                vector_store_ids_to_link = [vector_store_id]
                logging.info(f"Created and linked new vector store {vector_store_id} for assistant {assistant_id}")
                needs_update = True
            else: # Use the existing linked VS IDs if none provided
                vector_store_ids_to_link = existing_vs_ids
                if not vector_store_id: # Update vs_id for return value if not provided
                    vector_store_id = existing_vs_ids[0] # Return the first one

            # If tools or VS links changed, update assistant
            if needs_update:
                 update_payload = { "tools": current_tools }
                 # Construct tool_resources carefully
                 update_payload["tool_resources"] = {
                     "file_search": {"vector_store_ids": vector_store_ids_to_link},
                      # Always include code_interpreter resources, even if empty
                     "code_interpreter": {"file_ids": code_interpreter_file_ids}
                 }

                 client.beta.assistants.update(assistant_id=assistant_id, **update_payload)
                 logging.info(f"Updated tools/resources for assistant {assistant_id}")

        # --- Handle file upload (similar logic to /upload-file but updates assistant resources) ---
        if file:
            filename = file.filename
            file_content = await file.read()
            temp_dir = os.environ.get("TEMP", "/tmp")
            if not os.path.exists(temp_dir): os.makedirs(temp_dir, exist_ok=True)
            file_path = os.path.join(temp_dir, filename)


            try:
                with open(file_path, "wb") as ftemp:
                    ftemp.write(file_content)

                # Determine file type
                file_ext = os.path.splitext(filename)[1].lower()
                is_csv = file_ext == '.csv'
                is_excel = file_ext in ['.xlsx', '.xls', '.xlsm']
                mime_type, _ = mimetypes.guess_type(filename)
                is_image = file_ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'] or (mime_type and mime_type.startswith('image/'))
                is_document = file_ext in ['.pdf', '.doc', '.docx', '.txt', '.md', '.html', '.json']

                file_info = {"name": filename}
                assistant_needs_update_after_file = False

                if is_csv or is_excel:
                    # Upload to OpenAI files
                    with open(file_path, "rb") as file_stream:
                        uploaded_file = client.files.create(file=file_stream, purpose='assistants')

                    # Append file ID if not already present
                    if uploaded_file.id not in code_interpreter_file_ids:
                        code_interpreter_file_ids.append(uploaded_file.id)
                        assistant_needs_update_after_file = True # Flag for update

                        file_info.update({
                            "type": "csv" if is_csv else "excel",
                            "id": uploaded_file.id,
                            "processing_method": "code_interpreter"
                        })
                        logging.info(f"Prepared '{filename}' (ID: {uploaded_file.id}) for code interpreter linking to assistant {assistant_id}")
                    else:
                        logging.info(f"File '{filename}' (ID: {uploaded_file.id}) already associated with assistant {assistant_id}, no update needed.")
                        # Populate file_info even if already present for awareness message
                        file_info.update({
                            "type": "csv" if is_csv else "excel",
                            "id": uploaded_file.id,
                            "processing_method": "code_interpreter"
                        })


                elif is_image:
                    # Image analysis requires a thread context
                    if thread_id:
                        analysis_text = await image_analysis(client, file_content, filename, None)
                        client.beta.threads.messages.create(
                            thread_id=thread_id,
                            role="user",
                            content=f"Analysis result for uploaded image '{filename}':\n{analysis_text}"
                        )
                        file_info.update({
                            "type": "image",
                            "processing_method": "thread_message"
                        })
                        logging.info(f"Added image analysis for '{filename}' to thread {thread_id}")
                    else:
                        logging.warning(f"Image '{filename}' uploaded but no session/thread ID provided, analysis not added to a thread.")
                        file_info = {} # Clear file_info if not processed

                elif is_document or not (is_csv or is_excel or is_image):
                    if not vector_store_ids_to_link: # Check the list determined earlier
                         # This shouldn't happen due to the logic above (create if none exist)
                         raise ValueError("Vector store ID is required to upload documents but none was found or created.")
                    vs_id_to_use = vector_store_ids_to_link[0] # Use the first linked VS

                    # Upload to vector store
                    with open(file_path, "rb") as file_stream:
                         file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                             vector_store_id=vs_id_to_use,
                             files=[file_stream]
                         )

                    if file_batch.status == 'completed':
                        file_info.update({
                             "type": file_ext[1:] if file_ext else "document",
                             "processing_method": "vector_store"
                        })
                        logging.info(f"File '{filename}' uploaded to vector store {vs_id_to_use}: status={file_batch.status}, count={file_batch.file_counts.total}")
                    else:
                         logging.error(f"File batch upload for '{filename}' to vector store {vs_id_to_use} failed. Status: {file_batch.status}")
                         file_info = {} # Clear file_info if upload failed
                else:
                    logging.warning(f"File type for '{filename}' not explicitly handled for upload.")
                    file_info = {} # Clear file_info

                # Update assistant if a new CI file was added
                if assistant_needs_update_after_file:
                    client.beta.assistants.update(
                        assistant_id=assistant_id,
                        tool_resources={
                             # Preserve file_search resources
                            "file_search": {"vector_store_ids": vector_store_ids_to_link},
                             # Update code interpreter resources
                            "code_interpreter": {"file_ids": code_interpreter_file_ids}
                        }
                    )
                    logging.info(f"Updated assistant {assistant_id} to link new code interpreter file(s).")

                # Add awareness message if thread exists and file was processed
                if thread_id and file_info.get("processing_method"):
                    await add_file_awareness(client, thread_id, file_info)


            except Exception as e:
                logging.error(f"Error processing uploaded file '{filename}' in /co-pilot: {e}")
                # Allow endpoint to succeed but log error
            finally:
                 if os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except OSError as e:
                         logging.error(f"Error removing temporary file {file_path}: {e}")

        # If context provided and thread exists, update context
        if context and thread_id:
            await update_context(client, thread_id, context)

        return JSONResponse(
            {
                "message": "Co-pilot assistant processed successfully.",
                "assistant": assistant_id,
                "vector_store": vector_store_id, # Return the primary VS ID used/created
                "session": thread_id # Return thread_id if provided
            },
            status_code=200
        )

    except Exception as e:
        logging.error(f"Error in /co-pilot endpoint: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process co-pilot request: {str(e)}")


@app.post("/upload-file")
async def upload_file(
    request: Request, # <<< Added missing request parameter
    file: UploadFile = Form(...),
    assistant: str = Form(...) # Assistant ID is required
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
        thread_id: Optional[str] = form.get("session", None) # Use 'session' for thread_id
        image_prompt: Optional[str] = form.get("prompt", None) # Specific prompt for image analysis
    except Exception as e:
         logging.error(f"Error parsing form data in /upload-file: {e}")
         # Continue without optional params if form parsing fails for them
         context, thread_id, image_prompt = None, None, None


    filename = file.filename
    temp_dir = os.environ.get("TEMP", "/tmp")
    if not os.path.exists(temp_dir): os.makedirs(temp_dir, exist_ok=True)
    file_path = os.path.join(temp_dir, filename)

    uploaded_file_details = {} # To return info about the uploaded file

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
        current_tools = list(assistant_obj.tools) if assistant_obj.tools else []
        current_tool_resources = assistant_obj.tool_resources if assistant_obj.tool_resources else {}
        needs_update = False # Flag if assistant needs updating

        # --- Prepare resource lists ---
        # Get existing code_interpreter file_ids
        code_interpreter_file_ids = []
        ci_resources = getattr(current_tool_resources, "code_interpreter", None)
        if ci_resources and hasattr(ci_resources, "file_ids"):
             code_interpreter_file_ids = list(ci_resources.file_ids)

        # Get existing vector_store_ids
        vector_store_ids = []
        fs_resources = getattr(current_tool_resources, "file_search", None)
        if fs_resources and hasattr(fs_resources, "vector_store_ids"):
            vector_store_ids = list(fs_resources.vector_store_ids)


        # --- Code Interpreter Handling ---
        if is_csv or is_excel:
            # Ensure code interpreter tool exists
            if not any(getattr(tool, 'type', None) == "code_interpreter" for tool in current_tools):
                 current_tools.append({"type": "code_interpreter"})
                 needs_update = True
                 logging.info(f"Adding code_interpreter tool to assistant {assistant}")

            # Upload to OpenAI files for code interpreter
            with open(file_path, "rb") as file_stream:
                uploaded_file = client.files.create(file=file_stream, purpose='assistants')

            if uploaded_file.id not in code_interpreter_file_ids:
                code_interpreter_file_ids.append(uploaded_file.id)
                needs_update = True # Need to update assistant with new file ID

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
                    "file_id": uploaded_file.id,
                    "filename": filename,
                    "processing_method": "code_interpreter"
                 }
                 logging.info(f"File '{filename}' (ID: {uploaded_file.id}) already associated with assistant {assistant}")


        # --- Vector Store Handling ---
        # Check if it's a document or fallback type
        elif is_document or not (is_csv or is_excel or is_image):
             # Ensure file search tool exists
             if not any(getattr(tool, 'type', None) == "file_search" for tool in current_tools):
                 current_tools.append({"type": "file_search"})
                 needs_update = True
                 logging.info(f"Adding file_search tool to assistant {assistant}")

             # Ensure a vector store is linked or create one
             if not vector_store_ids:
                 logging.info(f"No vector store linked to assistant {assistant}. Creating and linking a new one.")
                 vector_store = client.beta.vector_stores.create(name=f"Assistant_{assistant}_Store")
                 vector_store_ids = [vector_store.id]
                 needs_update = True # Need update to link VS
             vector_store_id_to_use = vector_store_ids[0] # Use the first linked store

             # Upload to vector store
             with open(file_path, "rb") as file_stream:
                 file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                     vector_store_id=vector_store_id_to_use,
                     files=[file_stream]
                 )

             if file_batch.status == 'completed':
                 uploaded_file_details = {
                     "message": "File successfully uploaded to vector store.",
                     "filename": filename,
                     "vector_store_id": vector_store_id_to_use,
                     "processing_method": "vector_store",
                     "batch_status": file_batch.status
                 }
                 logging.info(f"Uploaded '{filename}' to vector store {vector_store_id_to_use} for assistant {assistant}")
             else:
                  logging.error(f"File batch upload failed for '{filename}' to VS {vector_store_id_to_use}. Status: {file_batch.status}")
                  uploaded_file_details = {
                     "message": f"File upload to vector store failed with status: {file_batch.status}.",
                     "filename": filename,
                     "vector_store_id": vector_store_id_to_use,
                     "processing_method": "vector_store_failed",
                     "batch_status": file_batch.status
                 }


        # --- Update Assistant if tools or resources changed ---
        if needs_update:
            update_payload = {"tools": current_tools, "tool_resources": {}}

            # Construct final tool_resources payload ensuring all parts are preserved correctly
            # File Search: Always use the current/updated vector_store_ids list
            fs_resource_payload = {"vector_store_ids": vector_store_ids}
            update_payload["tool_resources"]["file_search"] = fs_resource_payload

            # Code Interpreter: Always use the current/updated code_interpreter_file_ids list
            # <<< FIX: Ensure the potentially updated list is used, not conditionally based on the current file type >>>
            ci_resource_payload = {"file_ids": code_interpreter_file_ids}
            update_payload["tool_resources"]["code_interpreter"] = ci_resource_payload

            try:
                client.beta.assistants.update(assistant_id=assistant, **update_payload)
                logging.info(f"Updated assistant {assistant} with new tool/resource associations.")
            except Exception as update_err:
                logging.error(f"Failed to update assistant {assistant}: {update_err}")
                # Depending on severity, might want to raise HTTPException or just log


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

        # --- Add File Awareness Message (if thread exists and file was processed successfully) ---
        if thread_id and uploaded_file_details and uploaded_file_details.get("processing_method") not in ["skipped_analysis", "vector_store_failed", None]:
             file_info = {
                 "type": file_ext[1:] if file_ext else 'unknown',
                 "name": filename,
                 "id": uploaded_file_details.get("file_id"), # Only present for code interpreter
                 "processing_method": uploaded_file_details.get("processing_method")
             }
             # Correct file type for awareness message
             if is_csv: file_info["type"] = "csv"
             elif is_excel: file_info["type"] = "excel"
             elif is_image: file_info["type"] = "image"
             elif is_document or uploaded_file_details.get("processing_method") == "vector_store": # Ensure correct type for documents
                 file_info["type"] = file_ext[1:] if file_ext else "document"

             await add_file_awareness(client, thread_id, file_info)


        # --- Update Context (if provided and thread exists) ---
        if context and thread_id:
            await update_context(client, thread_id, context)

        # Determine status code based on processing outcome
        status_code = 200
        if uploaded_file_details.get("processing_method") == "vector_store_failed":
             status_code = 500 # Indicate failure if VS upload failed critically

        return JSONResponse(uploaded_file_details, status_code=status_code)

    except Exception as e:
        logging.error(f"Error uploading file '{filename}' for assistant {assistant}: {e}")
        # Consider specific exceptions for not found vs other errors
        if "No assistant found with id" in str(e):
            raise HTTPException(status_code=404, detail=f"Assistant with ID '{assistant}' not found.")
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
    assistant: Optional[str] = None,
    context: Optional[str] = None
):
    """
    Handles conversation queries with streaming response.
    Uses existing session/assistant if provided, otherwise creates defaults (logs this).
    """
    client = create_client()

    try:
        # Validate required parameters if needed (e.g., assistant should exist)
        # For now, retain fallback logic

        # If no assistant or session provided, create defaults (log this behavior)
        if not assistant:
            logging.warning("No assistant ID provided for /conversation, creating a default one.")
            # Create a minimal default assistant
            try:
                assistant_obj = client.beta.assistants.create(
                    name="default_conversation_assistant",
                    model="gpt-4o-mini", # Use a general-purpose model
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

        # If context is provided, update user persona context in the session
        if context:
            await update_context(client, session, context)

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
        async def stream_response(): # Make generator async
            buffer = []
            try:
                # Create run and stream the response
                async with client.beta.threads.runs.stream( # Use async context manager
                    thread_id=session,
                    assistant_id=assistant,
                    # Add event handlers if needed later for tool calls etc.
                    # event_handler=YourEventHandler() # If using class-based handler
                ) as stream:
                    async for event in stream: # Iterate asynchronously
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
                                             # Adjust buffer size based on typical chunk sizes observed
                                             yield ''.join(buffer) # Yield immediately or based on buffer size
                                             buffer = [] # Reset buffer after yielding

                # Yield any remaining text in the buffer
                if buffer:
                    yield ''.join(buffer)
            except Exception as e:
                logging.error(f"Streaming error during run for thread {session}: {e}")
                yield f"\n[ERROR] An error occurred while generating the response: {str(e)}. Please check logs."
                # Consider different error propagation for production

        # Return the streaming response
        return StreamingResponse(stream_response(), media_type="text/event-stream") # text/plain? text/event-stream is common

    except Exception as e:
        logging.error(f"Error in /conversation endpoint setup: {e}")
        # Check for specific errors like invalid IDs
        if "No thread found with id" in str(e):
            raise HTTPException(status_code=404, detail=f"Session (Thread) with ID '{session}' not found.")
        if "No assistant found with id" in str(e):
             raise HTTPException(status_code=404, detail=f"Assistant with ID '{assistant}' not found.")
        raise HTTPException(status_code=500, detail=f"Failed to process conversation request: {str(e)}")


@app.get("/chat")
async def chat(
    session: Optional[str] = None,
    prompt: Optional[str] = None,
    assistant: Optional[str] = None,
    context: Optional[str] = None
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

        # Update context if provided
        if context:
            await update_context(client, session, context)

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
            # Needs to be async if using stream with async for
            async with client.beta.threads.runs.stream(thread_id=session, assistant_id=assistant) as stream:
                async for event in stream:
                    if event.event == "thread.message.delta":
                        delta = event.data.delta
                        if delta.content:
                            for content_part in delta.content:
                                if content_part.type == 'text' and content_part.text:
                                    text_value = content_part.text.value
                                    if text_value:
                                         response_text_parts.append(text_value)
            # Alternative: Use run = client.beta.threads.runs.create_and_poll(...) then list messages
            # run = client.beta.threads.runs.create_and_poll(
            #     thread_id=session,
            #     assistant_id=assistant
            # )
            # if run.status == 'completed':
            #     messages = client.beta.threads.messages.list(thread_id=session, order='desc', limit=1)
            #     if messages.data and messages.data[0].role == 'assistant':
            #          for content_part in messages.data[0].content:
            #              if content_part.type == 'text':
            #                  response_text_parts.append(content_part.text.value)
            # else:
            #      logging.error(f"Run failed or did not complete. Status: {run.status}")
            #      raise HTTPException(status_code=500, detail=f"Assistant run failed: {run.last_error or run.status}")

        except Exception as e:
            logging.error(f"Error during run/stream for thread {session}: {e}")
            raise HTTPException(status_code=500, detail=f"Error generating response: {str(e)}. Please check logs.")

        full_response = ''.join(response_text_parts)
        if not full_response and prompt: # Check if response is empty despite a prompt
            logging.warning(f"Assistant returned an empty response for thread {session}, prompt: '{prompt}'")
            # Optionally return a specific message or just the empty string based on desired behavior
            full_response = "[Assistant did not provide a text response]"


        return JSONResponse(content={"response": full_response})

    except Exception as e:
        logging.error(f"Error in /chat endpoint setup: {e}")
        if "No thread found with id" in str(e):
            raise HTTPException(status_code=404, detail=f"Session (Thread) with ID '{session}' not found.")
        if "No assistant found with id" in str(e):
             raise HTTPException(status_code=404, detail=f"Assistant with ID '{assistant}' not found.")
        raise HTTPException(status_code=500, detail=f"Failed to process chat request: {str(e)}")


@app.post("/trim-thread")
async def trim_thread(request: Request, assistant_id_query: Optional[str] = None, max_age_days_query: Optional[int] = None):
    """
    Summarizes and removes old threads associated with a given assistant.
    Deletes summary threads older than the threshold.
    Uses a configurable age threshold (default 48 hours).
    Accepts assistant_id and max_age_days via query params or form data.
    """
    client = create_client()
    deleted_count = 0
    summarized_count = 0
    processed_count = 0

    # Get parameters from form data first, then fallback to query params
    try:
        form_data = await request.form()
        assistant_id = form_data.get("assistant_id", assistant_id_query)
        max_age_days_str = form_data.get("max_age_days")
    except Exception as e:
        logging.warning(f"Could not parse form data for /trim-thread: {e}. Falling back to query params.")
        assistant_id = assistant_id_query
        max_age_days_str = None # Will use max_age_days_query below


    if not assistant_id:
        raise HTTPException(status_code=400, detail="assistant_id is required (provide in query or form data)")

    max_age_days = None
    if max_age_days_str:
        try:
            max_age_days = int(max_age_days_str)
        except (ValueError, TypeError):
            logging.warning(f"Invalid max_age_days value '{max_age_days_str}' from form data, using query/default.")
            max_age_days = max_age_days_query # Fallback to query param if form value invalid
    elif max_age_days_query is not None:
         max_age_days = max_age_days_query


    # Set default cleanup threshold to 48 hours or convert days to hours
    time_threshold_hours = 48
    if max_age_days and max_age_days > 0:
        time_threshold_hours = max_age_days * 24
        logging.info(f"Using custom time threshold: {time_threshold_hours} hours ({max_age_days} days)")
    else:
        # If max_age_days was invalid or 0, use default
        if max_age_days is not None: # Log if an invalid value was provided
            logging.warning(f"Invalid max_age_days ({max_age_days}) provided, using default threshold.")
        logging.info(f"Using default time threshold: {time_threshold_hours} hours")


    all_threads_info = {}

    try:
        logging.info(f"Starting thread trimming for assistant: {assistant_id}")
        # Step 1: Get runs associated with this assistant to find relevant threads
        logging.info("Fetching runs to identify threads... (May require pagination for many runs)")

        # --- More Robust Run Fetching with Pagination ---
        has_more = True
        after_cursor = None
        runs_limit = 100 # Max allowed by API per page

        while has_more:
             try:
                 runs_list = client.beta.threads.runs.list(limit=runs_limit, after=after_cursor)
                 if not runs_list.data:
                     break # No more runs found

                 for run in runs_list.data:
                     if run.assistant_id == assistant_id:
                         thread_id = run.thread_id
                         # Use run creation time as proxy for thread activity
                         last_active_ts = datetime.datetime.fromtimestamp(run.created_at, tz=datetime.timezone.utc)

                         if thread_id not in all_threads_info or last_active_ts > all_threads_info[thread_id]['last_active']:
                             all_threads_info[thread_id] = {
                                 'thread_id': thread_id,
                                 'last_active': last_active_ts
                             }

                 has_more = runs_list.has_more
                 if has_more:
                     after_cursor = runs_list.last_id
                     logging.info(f"Fetching next page of runs after {after_cursor}")
                 else:
                      logging.info("Fetched all runs.")

             except Exception as list_runs_e:
                 logging.error(f"Error fetching runs page: {list_runs_e}. Stopping run fetching.")
                 has_more = False # Stop pagination on error

        logging.info(f"Identified {len(all_threads_info)} unique threads associated with assistant {assistant_id} via runs.")

        # Sort threads by last active time (most recent first)
        sorted_threads = sorted(all_threads_info.values(), key=lambda x: x['last_active'], reverse=True)

        # Get current time (UTC) for age comparison
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        threshold_delta = datetime.timedelta(hours=time_threshold_hours)

        # Step 2: Process each identified thread
        for thread_info in sorted_threads:
            thread_id = thread_info['thread_id']
            last_active = thread_info['last_active']
            processed_count += 1

            try:
                # Check age against threshold
                if now_utc - last_active > threshold_delta:
                    thread_age_hours = (now_utc - last_active).total_seconds() / 3600

                    # Retrieve thread to check metadata
                    try:
                        thread = client.beta.threads.retrieve(thread_id=thread_id)
                        metadata = thread.metadata if hasattr(thread, 'metadata') and thread.metadata else {}
                        is_summary_thread = metadata.get('is_summary', False)
                    except Exception as retrieve_e:
                         # If thread retrieval fails (e.g., already deleted), log and skip
                         if "No thread found with id" in str(retrieve_e):
                             logging.warning(f"Thread {thread_id} not found, likely already deleted. Skipping.")
                             continue
                         else:
                              logging.error(f"Error retrieving thread {thread_id}: {retrieve_e}")
                              continue # Skip this thread on error

                    # --- Cleanup Logic ---
                    if is_summary_thread:
                        # Delete old summary threads
                        logging.info(f"Deleting old summary thread {thread_id} (age: {thread_age_hours:.1f} hours)")
                        client.beta.threads.delete(thread_id=thread_id)
                        deleted_count += 1
                    else:
                        # Summarize and delete old regular threads
                        logging.info(f"Summarizing and deleting old thread {thread_id} (age: {thread_age_hours:.1f} hours)")

                        # Get messages (limit to avoid excessive context, get recent ones first for summary)
                        messages = client.beta.threads.messages.list(thread_id=thread_id, limit=50, order='desc') # Get most recent first
                        message_content_list = []
                        # Iterate in reverse to get chronological order for the prompt
                        for msg in reversed(messages.data):
                             # Robust content extraction
                             text_content = ""
                             if msg.content:
                                 for content_part in msg.content:
                                     if content_part.type == 'text' and content_part.text:
                                         text_content += content_part.text.value + " "
                             if text_content.strip(): # Only add if there is actual text content
                                message_content_list.append(f"{msg.role}: {text_content.strip()}")

                        if not message_content_list:
                            logging.info(f"Thread {thread_id} has no text content to summarize. Deleting.")
                            client.beta.threads.delete(thread_id=thread_id)
                            deleted_count += 1
                            continue # Skip to next thread

                        summary_prompt_content = "\n\n".join(message_content_list)
                        # Make prompt slightly more robust
                        full_prompt = (
                             "Please act as a conversation archivist. Summarize the key points, decisions, "
                             f"and outcomes from the following conversation excerpt involving assistant {assistant_id}. "
                             "Provide a concise summary (1-3 paragraphs) suitable for future reference.\n\n"
                             "--- CONVERSATION START ---\n"
                             f"{summary_prompt_content}\n"
                             "--- CONVERSATION END ---"
                        )

                        # Create a new thread for the summary
                        summary_thread = client.beta.threads.create(
                            metadata={
                                "is_summary": True,
                                "original_thread_id": thread_id,
                                "summarized_at": now_utc.isoformat(),
                                "original_assistant_id": assistant_id # Store original assistant ID
                                }
                        )

                        # Add summarization request message
                        client.beta.threads.messages.create(
                            thread_id=summary_thread.id,
                            role="user",
                            content=full_prompt
                        )

                        # Run summarization (using create_and_poll for simplicity)
                        try:
                            run = client.beta.threads.runs.create_and_poll(
                                thread_id=summary_thread.id,
                                assistant_id=assistant_id, # Use the same assistant
                                # instructions="Summarize the provided conversation concisely.", # Optional override
                                # model="gpt-4o-mini" # Can specify model if needed
                            )

                            if run.status == "completed":
                                # Optionally retrieve and log the summary, but main goal is deletion
                                try:
                                    summary_messages = client.beta.threads.messages.list(thread_id=summary_thread.id, order="desc", limit=1)
                                    summary_text = "[Summary generation completed]"
                                    if summary_messages.data and summary_messages.data[0].role == 'assistant' and summary_messages.data[0].content:
                                         content_part = summary_messages.data[0].content[0]
                                         if content_part.type == 'text' and content_part.text:
                                             summary_text = content_part.text.value[:200] + ("..." if len(content_part.text.value) > 200 else "")

                                    logging.info(f"Summary generated in thread {summary_thread.id} for original {thread_id}. Summary starts: '{summary_text}'")
                                except Exception as log_summary_e:
                                     logging.warning(f"Could not retrieve or log summary text for {summary_thread.id}: {log_summary_e}")

                                # Delete the original thread AFTER successful summary run
                                try:
                                    client.beta.threads.delete(thread_id=thread_id)
                                    deleted_count += 1
                                    summarized_count += 1
                                except Exception as delete_orig_e:
                                     logging.error(f"Summary successful, but failed to delete original thread {thread_id}: {delete_orig_e}")

                            else:
                                logging.error(f"Summarization run for thread {thread_id} failed or timed out. Status: {run.status}, Error: {run.last_error}. Original thread NOT deleted.")
                                # Optionally delete the failed summary thread to avoid clutter
                                try:
                                     client.beta.threads.delete(thread_id=summary_thread.id)
                                     logging.info(f"Deleted failed summary thread {summary_thread.id}")
                                except Exception as delete_fail_e:
                                     logging.error(f"Failed to delete the failed summary thread {summary_thread.id}: {delete_fail_e}")

                        except Exception as run_e:
                            logging.error(f"Error during summarization run creation/polling for thread {thread_id}: {run_e}. Original thread NOT deleted.")
                            # Optionally delete the failed summary thread
                            try:
                                client.beta.threads.delete(thread_id=summary_thread.id)
                                logging.info(f"Deleted failed summary thread {summary_thread.id} after run error.")
                            except Exception as delete_fail_e:
                                logging.error(f"Failed to delete the failed summary thread {summary_thread.id} after run error: {delete_fail_e}")

                else:
                    # Thread is not older than threshold - Log less verbosely
                    # logging.debug(f"Skipping thread {thread_id} (age: {(now_utc - last_active).total_seconds() / 3600:.1f} hours) - within threshold.")
                    pass # Keep it concise unless debugging

            except Exception as process_e:
                logging.error(f"Unhandled error processing thread {thread_id}: {process_e}")
                continue # Move to the next thread

        logging.info("Thread trimming process finished.")
        # Calculate count of old summary threads deleted correctly
        old_summary_threads_deleted = deleted_count - summarized_count

        return JSONResponse({
            "status": "Thread trimming completed",
            "assistant_id": assistant_id,
            "threads_identified": len(all_threads_info),
            "threads_processed": processed_count,
            "threads_summarized_and_deleted": summarized_count,
            "old_summary_threads_deleted": old_summary_threads_deleted,
            "total_threads_deleted": deleted_count,
            "threshold_hours": time_threshold_hours
        })

    except Exception as e:
        logging.error(f"Critical error in /trim-thread endpoint: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to trim threads: {str(e)}")


@app.post("/file-cleanup")
async def file_cleanup(request: Request, vector_store_id_query: Optional[str] = None, assistant_id_query: Optional[str] = None):
    """
    Cleans up files older than 48 hours:
    - For specific vector stores (if vector_store_id provided or linked to assistant_id).
    - Removes *all* code interpreter file associations from an assistant (if assistant_id provided).

    Accepts vector_store_id and assistant_id via query params or form data.
    Uses a fixed 48-hour threshold for vector store files.
    """
    client = create_client()
    deleted_vector_files = 0
    cleared_code_interpreter_files = 0
    vector_stores_cleaned = []
    assistants_processed = []

    # Get parameters from form data first, then fallback to query params
    try:
        form_data = await request.form()
        vector_store_id_param = form_data.get("vector_store_id", vector_store_id_query)
        assistant_id_param = form_data.get("assistant_id", assistant_id_query)
    except Exception as e:
         logging.warning(f"Could not parse form data for /file-cleanup: {e}. Falling back to query params.")
         vector_store_id_param = vector_store_id_query
         assistant_id_param = assistant_id_query


    if not vector_store_id_param and not assistant_id_param:
        raise HTTPException(status_code=400, detail="Either vector_store_id or assistant_id (or both) is required")

    # --- Determine Vector Store(s) to Clean ---
    vs_ids_to_clean = set()
    if vector_store_id_param:
        vs_ids_to_clean.add(vector_store_id_param)

    # If assistant_id is provided, try to find its linked vector store(s)
    if assistant_id_param:
         assistants_processed.append(assistant_id_param)
         try:
             assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant_id_param)
             if hasattr(assistant_obj, "tool_resources") and assistant_obj.tool_resources:
                 fs_resources = getattr(assistant_obj.tool_resources, "file_search", None)
                 if fs_resources and hasattr(fs_resources, "vector_store_ids"):
                     found_vs_ids = [vs_id for vs_id in fs_resources.vector_store_ids if vs_id]
                     if found_vs_ids:
                         vs_ids_to_clean.update(found_vs_ids)
                         logging.info(f"Identified vector stores linked to assistant {assistant_id_param}: {found_vs_ids}")
                     else:
                          logging.info(f"Assistant {assistant_id_param} has file_search enabled but no vector stores linked.")
                 else:
                      logging.info(f"Assistant {assistant_id_param} does not have file_search resources configured.")
             else:
                  logging.info(f"Assistant {assistant_id_param} does not have tool_resources configured.")

         except Exception as e:
             # Handle case where assistant doesn't exist
             if "No assistant found with id" in str(e):
                  logging.warning(f"Assistant {assistant_id_param} not found. Cannot clean its linked vector stores or CI files.")
             else:
                  logging.error(f"Could not retrieve assistant {assistant_id_param} to find linked vector stores: {e}")


    # --- Vector Store File Cleanup (Fixed 48-hour threshold) ---
    if vs_ids_to_clean:
        logging.info(f"Starting vector store file cleanup for VS IDs: {list(vs_ids_to_clean)} (older than 48 hours)")
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        threshold_seconds = 48 * 3600
        threshold_time = now_utc - datetime.timedelta(seconds=threshold_seconds)

        for vs_id in vs_ids_to_clean:
            try:
                logging.info(f"Processing vector store: {vs_id}")
                # List files directly with pagination
                has_more = True
                after_cursor = None
                limit = 100

                while has_more:
                    files_in_store = client.beta.vector_stores.files.list(
                        vector_store_id=vs_id,
                        limit=limit,
                        after=after_cursor
                    )
                    if not files_in_store.data:
                        break

                    for file in files_in_store.data:
                        try:
                            file_created_ts = datetime.datetime.fromtimestamp(file.created_at, tz=datetime.timezone.utc)

                            if file_created_ts < threshold_time:
                                logging.info(f"Deleting old file {file.id} (created: {file_created_ts.isoformat()}) from VS {vs_id}")
                                # Attempt deletion, handle potential errors (e.g., file already deleted)
                                try:
                                    client.beta.vector_stores.files.delete(
                                        vector_store_id=vs_id,
                                        file_id=file.id
                                    )
                                    deleted_vector_files += 1
                                except Exception as delete_e:
                                     # Check if it's a "not found" error, which is okay
                                     if "No file found with id" in str(delete_e) or "FileNotFoundError" in str(delete_e): # Adapt based on actual API error
                                         logging.warning(f"File {file.id} already deleted or not found in VS {vs_id}. Skipping.")
                                     else:
                                          logging.error(f"Error deleting file {file.id} from VS {vs_id}: {delete_e}")
                            # else:
                                # logging.debug(f"Skipping recent file {file.id} in VS {vs_id}")

                        except Exception as file_process_e:
                            logging.error(f"Error processing file {file.id} in VS {vs_id}: {file_process_e}")

                    has_more = files_in_store.has_more
                    if has_more:
                        after_cursor = files_in_store.last_id
                    else:
                         logging.info(f"Finished processing files for VS {vs_id}")

                vector_stores_cleaned.append(vs_id)

            except Exception as vs_e:
                 # Handle case where VS doesn't exist
                 if "No vector store found with id" in str(vs_e) or "VectorStoreNotFoundError" in str(vs_e):
                     logging.warning(f"Vector store {vs_id} not found. Skipping cleanup for this VS.")
                 else:
                      logging.error(f"Error processing vector store {vs_id}: {vs_e}")
                 continue # Move to next vector store

    # --- Code Interpreter File Cleanup (Removes ALL associated files) ---
    if assistant_id_param:
        logging.info(f"Starting code interpreter file cleanup for assistant: {assistant_id_param}")
        try:
            # Retrieve assistant again to ensure we have latest state (handle not found)
            try:
                 assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant_id_param)
            except Exception as retrieve_e:
                 if "No assistant found with id" in str(retrieve_e):
                      logging.warning(f"Assistant {assistant_id_param} not found. Skipping CI file cleanup.")
                      assistant_obj = None # Ensure assistant_obj is None if not found
                 else:
                      raise retrieve_e # Re-raise other retrieval errors

            if assistant_obj: # Proceed only if assistant was found
                current_tool_resources = assistant_obj.tool_resources if assistant_obj.tool_resources else {}

                # Get current code interpreter file IDs
                code_interpreter_file_ids = []
                ci_resources = getattr(current_tool_resources, "code_interpreter", None)
                if ci_resources and hasattr(ci_resources, "file_ids"):
                    code_interpreter_file_ids = list(ci_resources.file_ids)

                if code_interpreter_file_ids:
                     # Preserve file search resources
                     fs_resource_payload = {}
                     fs_resources = getattr(current_tool_resources, "file_search", None)
                     if fs_resources and hasattr(fs_resources, "vector_store_ids"):
                         fs_resource_payload = {"vector_store_ids": list(fs_resources.vector_store_ids)}

                     # Update assistant to clear code interpreter files
                     client.beta.assistants.update(
                         assistant_id=assistant_id_param,
                         tool_resources={
                             "code_interpreter": {"file_ids": []}, # Set to empty list
                             "file_search": fs_resource_payload # Keep existing VS links
                         }
                     )
                     cleared_code_interpreter_files = len(code_interpreter_file_ids)
                     logging.info(f"Cleared {cleared_code_interpreter_files} code interpreter file associations from assistant {assistant_id_param}")

                     # Optionally: Delete the actual files from OpenAI storage (use with caution)
                     # for file_id in code_interpreter_file_ids:
                     #     try:
                     #         client.files.delete(file_id)
                     #         logging.info(f"Deleted file {file_id} from OpenAI storage.")
                     #     except Exception as delete_file_e:
                     #         logging.error(f"Failed to delete file {file_id} from storage: {delete_file_e}")

                else:
                     logging.info(f"No code interpreter files associated with assistant {assistant_id_param}.")

        except Exception as e:
            # Log general errors during CI cleanup, avoid raising HTTPException if VS cleanup might have worked
            logging.error(f"Error cleaning code interpreter files for assistant {assistant_id_param}: {e}")

    logging.info("File cleanup process finished.")
    return JSONResponse({
        "status": "File cleanup completed",
        "vector_stores_processed": list(vs_ids_to_clean),
        "vector_files_deleted_older_than_48h": deleted_vector_files,
        "assistants_processed_for_ci": assistants_processed,
        "code_interpreter_files_cleared": cleared_code_interpreter_files,
    })


if __name__ == "__main__":
    import uvicorn
    # Get port from environment variable or default to 8000
    port = int(os.environ.get("PORT", 8000))
    print(f"Starting FastAPI server on http://0.0.0.0:{port}")
    # Set reload=False for production environments like Azure Web Apps
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)
