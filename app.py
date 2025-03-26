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
import json

app = FastAPI()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

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
        
        # Use the same client for vision API
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

async def process_excel_file(file_path: str, original_filename: str) -> List[Dict]:
    """Process Excel file, convert sheets to CSV, and return metadata."""
    try:
        # Read all sheets
        df_dict = pd.read_excel(file_path, sheet_name=None)
        file_info = []
        
        # Process each sheet
        for sheet_name, df in df_dict.items():
            # Create CSV filename for the sheet
            csv_filename = f"{os.path.splitext(original_filename)[0]}_{sheet_name}.csv"
            csv_path = os.path.join('/tmp/', csv_filename)
            
            # Save as CSV
            df.to_csv(csv_path, index=False)
            
            # Generate metadata
            sheet_info = {
                "original_file": original_filename,
                "sheet_name": sheet_name,
                "csv_filename": csv_filename,
                "csv_path": csv_path,
                "columns": list(df.columns),
                "row_count": len(df),
                "file_type": "excel_sheet"
            }
            file_info.append(sheet_info)
            
        return file_info
    except Exception as e:
        logging.error(f"Error processing Excel file: {e}")
        return []

async def process_csv_file(file_path: str, filename: str) -> Dict:
    """Process CSV file and return metadata."""
    try:
        df = pd.read_csv(file_path)
        return {
            "filename": filename,
            "path": file_path,
            "columns": list(df.columns),
            "row_count": len(df),
            "file_type": "csv"
        }
    except Exception as e:
        logging.error(f"Error processing CSV file: {e}")
        return {"filename": filename, "path": file_path, "error": str(e), "file_type": "csv"}
        
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

async def update_file_registry(client, thread_id, file_info):
    """Updates the file registry in the thread so the assistant knows what files are available."""
    if not file_info:
        return
    
    try:
        # Create a file registry message that informs the assistant about available files
        file_registry_content = "FILE REGISTRY UPDATE: The following files are now available:\n\n"
        
        if isinstance(file_info, list):
            for item in file_info:
                if item.get("file_type") == "excel_sheet":
                    file_registry_content += (
                        f"- Sheet '{item['sheet_name']}' from Excel file '{item['original_file']}' "
                        f"is available as CSV '{item['csv_filename']}' for analysis via code interpreter\n"
                    )
                elif item.get("file_type") == "csv":
                    file_registry_content += (
                        f"- CSV file '{item['filename']}' is available for analysis via code interpreter\n"
                    )
                elif item.get("file_type") == "image":
                    file_registry_content += (
                        f"- Image file '{item['filename']}' has been analyzed and the content is available in this thread\n"
                    )
                else:
                    file_registry_content += (
                        f"- File '{item['filename']}' has been added to the vector store for reference\n"
                    )
        else:
            # Single file info object
            if file_info.get("file_type") == "excel_sheet":
                file_registry_content += (
                    f"- Sheet '{file_info['sheet_name']}' from Excel file '{file_info['original_file']}' "
                    f"is available as CSV '{file_info['csv_filename']}' for analysis via code interpreter\n"
                )
            elif file_info.get("file_type") == "csv":
                file_registry_content += (
                    f"- CSV file '{file_info['filename']}' is available for analysis via code interpreter\n"
                )
            elif file_info.get("file_type") == "image":
                file_registry_content += (
                    f"- Image file '{file_info['filename']}' has been analyzed and the content is available in this thread\n"
                )
            else:
                file_registry_content += (
                    f"- File '{file_info['filename']}' has been added to the vector store for reference\n"
                )
        
        # Add system message with file registry information
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=file_registry_content,
            metadata={"type": "file_registry"}
        )
        
        logging.info(f"Updated file registry in thread {thread_id}")
    except Exception as e:
        logging.error(f"Error updating file registry: {e}")

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

    # Enhanced system prompt with file handling instructions
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

        3. **File Handling:**
        - When working with uploaded files, be aware of their type and use the appropriate tool:
            - For Excel (.xlsx/.xls) and CSV files: Use code_interpreter to analyze the data
            - For other document types (PDF, Word, etc.): Use file_search to reference content
            - For images: Reference the image analysis provided in the thread

        - **When analyzing CSV and Excel files:**
            - Start with a data overview: shape, columns, missing values
            - For Excel files, perform sheet-specific analysis 
            - Compare trends across sheets when applicable
            - Generate visualizations with clear source identification
            - Include code snippets with explanations
            - Begin output with: "Analyzing [file.csv] / [sheet] from [file.xlsx]"
            - Use markdown tables for key statistics

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
        - Use code_interpreter for data analysis of CSV and Excel files.
        
        - **Data Privacy:**
        - Handle all uploaded files and user data with the utmost confidentiality and in compliance with relevant data protection standards.

        - **Assumption Handling:**
        - Clearly indicate when you are making assumptions due to missing information.
        - Provide rationales for your assumptions to maintain transparency.

        - **Error Handling:**
        - Gracefully manage any errors or uncertainties by informing the user and seeking clarification when necessary.

        By adhering to these guidelines, you will function as an effective Product Management AI Assistant, delivering high-quality PRDs and insightful answers that closely mimic the expertise of a seasoned product manager.
        '''

    # Always include code_interpreter and file_search tools
    assistant_tools = [
        {"type": "code_interpreter"}, 
        {"type": "file_search"}
    ]
    assistant_tool_resources = {
        "file_search": {"vector_store_ids": [vector_store.id]},
        "code_interpreter": {"file_ids": []}  # Initialize empty file_ids list
    }

    # Create the assistant
    try:
        assistant = client.beta.assistants.create(
            name="demo_new_abhik",
            model="gpt-4o-mini",
            instructions=system_prompt,
            tools=assistant_tools,
            tool_resources=assistant_tool_resources,
        )
    except Exception as e:
        logging.info(f"An error occurred while creating the assistant: {e}")
        raise HTTPException(status_code=400, detail="An error occurred while creating assistant")

    logging.info(f'Assistant created {assistant.id}')

    # Create a thread
    try:
        thread = client.beta.threads.create()
    except Exception as e:
        logging.info(f"An error occurred while creating the thread: {e}")
        raise HTTPException(status_code=400, detail="An error occurred while creating the thread")

    logging.info(f"Thread created: {thread.id}")

    # If context is provided, add it as user persona context
    if context:
        try:
            await update_context(client, thread.id, context)
        except Exception as e:
            logging.info(f"An error occurred while adding context to the thread: {e}")
            # Don't fail the entire request if just adding context fails

    # If a file is provided, upload it now
    file_info = None
    if file:
        filename = file.filename
        file_path = os.path.join('/tmp/', filename)
        with open(file_path, 'wb') as f:
            f.write(await file.read())
            
        # Determine file type and process accordingly
        file_ext = os.path.splitext(filename)[1].lower()
        
        # Process based on file type
        if file_ext in ['.csv']:
            # Process CSV file for code_interpreter
            file_info = await process_csv_file(file_path, filename)
            
            # Upload file for code_interpreter
            with open(file_path, "rb") as file_stream:
                openai_file = client.files.create(
                    file=file_stream,
                    purpose="assistants"
                )
                
                # Update assistant with file ID
                tool_resources = assistant.tool_resources if hasattr(assistant, 'tool_resources') else {}
                code_interpreter_resources = tool_resources.get('code_interpreter', {})
                file_ids = code_interpreter_resources.get('file_ids', [])
                file_ids.append(openai_file.id)
                
                client.beta.assistants.update(
                    assistant_id=assistant.id,
                    tool_resources={
                        "file_search": {"vector_store_ids": [vector_store.id]},
                        "code_interpreter": {"file_ids": file_ids}
                    }
                )
                
        elif file_ext in ['.xlsx', '.xls']:
            # Process Excel file for code_interpreter
            sheet_info = await process_excel_file(file_path, filename)
            file_info = sheet_info
            
            # Upload each CSV sheet file for code_interpreter
            file_ids = []
            for sheet in sheet_info:
                with open(sheet['csv_path'], "rb") as file_stream:
                    openai_file = client.files.create(
                        file=file_stream,
                        purpose="assistants"
                    )
                    file_ids.append(openai_file.id)
            
            # Update assistant with file IDs
            client.beta.assistants.update(
                assistant_id=assistant.id,
                tool_resources={
                    "file_search": {"vector_store_ids": [vector_store.id]},
                    "code_interpreter": {"file_ids": file_ids}
                }
            )
                
        elif file_ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']:
            # Process image file
            with open(file_path, "rb") as file_stream:
                file_content = file_stream.read()
                
            # Analyze image and add analysis to thread
            analysis_text = await image_analysis(client, file_content, filename, None)
            
            client.beta.threads.messages.create(
                thread_id=thread.id,
                role="user",
                content=f"Image Analysis for {filename}: {analysis_text}"
            )
            
            # Store OCR/description in a text file with same name
            ocr_file_path = os.path.join('/tmp/', f"{os.path.splitext(filename)[0]}_ocr.txt")
            with open(ocr_file_path, 'w') as f:
                f.write(analysis_text)
                
            file_info = {
                "filename": filename,
                "path": file_path,
                "ocr_path": ocr_file_path,
                "file_type": "image"
            }
            
        else:
            # For other document types, upload to vector store
            with open(file_path, "rb") as file_stream:
                file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                    vector_store_id=vector_store.id, 
                    files=[file_stream]
                )
            logging.info(f"File uploaded to vector store: status={file_batch.status}, count={file_batch.file_counts}")
            
            file_info = {
                "filename": filename,
                "path": file_path,
                "file_type": "document"
            }

        # Update file registry in thread
        if file_info:
            await update_file_registry(client, thread.id, file_info)

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
            
        # Enhanced base prompt with file handling instructions
        base_prompt = """
        You are a product management AI assistant, a product co-pilot.
        
        ### File Handling Instructions:
        
        1. If working with Excel (.xlsx/.xls) files:
           - Read ALL sheets using code_interpreter
           - Each sheet has been converted to CSV named: <original_filename>_<sheet_name>.csv
           - Always reference both original file and sheet name in analysis
        
        2. If working with CSV files:
           - Use code_interpreter for direct analysis
           - Preserve original filename in references
        
        3. When analyzing data files:
           - Start with data overview: shape, columns, missing values
           - Perform sheet-specific analysis for Excel files
           - Compare trends across sheets when applicable
           - Generate visualizations with clear source identification
           - Include code snippets with explanations
        
        4. For document files (PDF, DOCX, etc.):
           - Use file_search to reference content
           
        5. For images:
           - Reference the image analysis provided in the thread
        """
        
        instructions = base_prompt if not system_prompt else f"{base_prompt}\n\n{system_prompt}"
        
        # Create assistant with both code_interpreter and file_search tools
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
            base_prompt = """
            You are a product management AI assistant, a product co-pilot.
            
            ### File Handling Instructions:
            
            1. If working with Excel (.xlsx/.xls) files:
               - Read ALL sheets using code_interpreter
               - Each sheet has been converted to CSV named: <original_filename>_<sheet_name>.csv
               - Always reference both original file and sheet name in analysis
            
            2. If working with CSV files:
               - Use code_interpreter for direct analysis
               - Preserve original filename in references
            
            3. When analyzing data files:
               - Start with data overview: shape, columns, missing values
               - Perform sheet-specific analysis for Excel files
               - Compare trends across sheets when applicable
               - Generate visualizations with clear source identification
               - Include code snippets with explanations
            
            4. For document files (PDF, DOCX, etc.):
               - Use file_search to reference content
               
            5. For images:
               - Reference the image analysis provided in the thread
            """
            
            client.beta.assistants.update(
                assistant_id=assistant_id,
                instructions=f"{base_prompt}\n\n{system_prompt}" if system_prompt else base_prompt,
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
                
                # Get existing tool resources
                tool_resources = getattr(assistant_obj, 'tool_resources', {})
                code_interpreter_resources = getattr(tool_resources, 'code_interpreter', {})
                file_ids = getattr(code_interpreter_resources, 'file_ids', [])
                
                # Ensure tools include both code_interpreter and file_search
                existing_tools = assistant_obj.tools if assistant_obj.tools else []
                if not any(t["type"] == "file_search" for t in existing_tools):
                    existing_tools.append({"type": "file_search"})
                if not any(t["type"] == "code_interpreter" for t in existing_tools):
                    existing_tools.append({"type": "code_interpreter"})
                    
                client.beta.assistants.update(
                    assistant_id=assistant_id,
                    tools=existing_tools,
                    tool_resources={
                        "file_search": {"vector_store_ids": [vector_store_id]},
                        "code_interpreter": {"file_ids": file_ids}
                    },
                )

    # Handle file upload if present
    file_info = None
    if file:
        filename = file.filename
        file_path = os.path.join('/tmp/', filename)
        with open(file_path, 'wb') as f:
            f.write(await file.read())
            
        # Determine file type and process accordingly
        file_ext = os.path.splitext(filename)[1].lower()
        
        # Get existing assistant to check for file IDs
        assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant_id)
        
        # Get existing file IDs more safely
        existing_file_ids = []
        if hasattr(assistant_obj, 'tool_resources') and assistant_obj.tool_resources:
            tool_resources = assistant_obj.tool_resources
            if hasattr(tool_resources, 'code_interpreter') and tool_resources.code_interpreter:
                if hasattr(tool_resources.code_interpreter, 'file_ids'):
                    existing_file_ids = tool_resources.code_interpreter.file_ids
        
        # Process based on file type
        if file_ext in ['.csv']:
            # Process CSV file for code_interpreter
            file_info = await process_csv_file(file_path, filename)
            
            # Upload file for code_interpreter
            with open(file_path, "rb") as file_stream:
                openai_file = client.files.create(
                    file=file_stream,
                    purpose="assistants"
                )
                
                # Update file IDs
                file_ids = list(existing_file_ids)
                file_ids.append(openai_file.id)
                
                client.beta.assistants.update(
                    assistant_id=assistant_id,
                    tool_resources={
                        "file_search": {"vector_store_ids": [vector_store_id]},
                        "code_interpreter": {"file_ids": file_ids}
                    }
                )
                
        elif file_ext in ['.xlsx', '.xls']:
            # Process Excel file for code_interpreter
            sheet_info = await process_excel_file(file_path, filename)
            file_info = sheet_info
            
            # Upload each CSV sheet file for code_interpreter
            new_file_ids = []
            for sheet in sheet_info:
                with open(sheet['csv_path'], "rb") as file_stream:
                    openai_file = client.files.create(
                        file=file_stream,
                        purpose="assistants"
                    )
                    new_file_ids.append(openai_file.id)
            
            # Update assistant with file IDs
            file_ids = list(existing_file_ids) + new_file_ids
            client.beta.assistants.update(
                assistant_id=assistant_id,
                tool_resources={
                    "file_search": {"vector_store_ids": [vector_store_id]},
                    "code_interpreter": {"file_ids": file_ids}
                }
            )
                
        elif file_ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']:
            # Process image file
            with open(file_path, "rb") as file_stream:
                file_content = file_stream.read()
                
            # Analyze image and add analysis to thread if thread exists
            if thread_id:
                analysis_text = await image_analysis(client, file_content, filename, None)
                
                client.beta.threads.messages.create(
                    thread_id=thread_id,
                    role="user",
                    content=f"Image Analysis for {filename}: {analysis_text}"
                )
                
                # Store OCR/description in a text file with same name
                ocr_file_path = os.path.join('/tmp/', f"{os.path.splitext(filename)[0]}_ocr.txt")
                with open(ocr_file_path, 'w') as f:
                    f.write(analysis_text)
                    
                file_info = {
                    "filename": filename,
                    "path": file_path,
                    "ocr_path": ocr_file_path,
                    "file_type": "image"
                }
            
        else:
            # For other document types, upload to vector store
            with open(file_path, "rb") as file_stream:
                file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                    vector_store_id=vector_store_id,
                    files=[file_stream]
                )
            logging.info(f"File uploaded to vector store: status={file_batch.status}, count={file_batch.file_counts}")
            
            file_info = {
                "filename": filename,
                "path": file_path,
                "file_type": "document"
            }

        # Update file registry in thread if thread exists and file_info exists
        if thread_id and file_info:
            await update_file_registry(client, thread_id, file_info)

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
async def upload_file(file: UploadFile = Form(...), assistant: str = Form(...), session: Optional[str] = Form(None), prompt: Optional[str] = Form(None), context: Optional[str] = Form(None)):
    """
    Uploads a file and associates it with the given assistant.
    Handles different file types appropriately - CSV/Excel for code_interpreter, documents for vector store, images for analysis.
    """
    client = create_client()

    try:
        # Save the uploaded file locally and get the data
        file_content = await file.read()
        file_path = f"/tmp/{file.filename}"
        with open(file_path, "wb") as temp_file:
            temp_file.write(file_content)
            
        # Determine file type
        file_ext = os.path.splitext(file.filename.lower())[1]
        is_csv = file_ext == '.csv'
        is_excel = file_ext in ['.xlsx', '.xls']
        is_image = file_ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'] or (file.content_type and file.content_type.startswith('image/'))
        
        # Retrieve the assistant
        assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant)
        
        # Get existing tool resources
        tool_resources = getattr(assistant_obj, 'tool_resources', {})
        
        # Get existing file_search resources
        file_search_resource = getattr(tool_resources, "file_search", None)
        vector_store_ids = (
            file_search_resource.vector_store_ids
            if (file_search_resource and hasattr(file_search_resource, "vector_store_ids"))
            else []
        )

        # Get existing code_interpreter resources
        code_interpreter_resource = getattr(tool_resources, "code_interpreter", None)
        existing_file_ids = (
            code_interpreter_resource.file_ids
            if (code_interpreter_resource and hasattr(code_interpreter_resource, "file_ids"))
            else []
        )

        # Ensure vector store exists
        if not vector_store_ids:
            logging.info("No associated vector store found. Creating a new one.")
            vector_store = client.beta.vector_stores.create(name=f"Assistant_{assistant}_Store")
            vector_store_id = vector_store.id
            vector_store_ids = [vector_store_id]
        else:
            vector_store_id = vector_store_ids[0]

        # Ensure tools include both code_interpreter and file_search
        existing_tools = assistant_obj.tools if assistant_obj.tools else []
        if not any(t["type"] == "file_search" for t in existing_tools):
            existing_tools.append({"type": "file_search"})
        if not any(t["type"] == "code_interpreter" for t in existing_tools):
            existing_tools.append({"type": "code_interpreter"})
            
        # Initialize file_info
        file_info = None
            
        # Process file based on type
        if is_csv:
            # Process CSV file for code_interpreter
            file_info = await process_csv_file(file_path, file.filename)
            
            # Upload file for code_interpreter
            with open(file_path, "rb") as file_stream:
                openai_file = client.files.create(
                    file=file_stream,
                    purpose="assistants"
                )
                
                # Update file IDs
                new_file_ids = list(existing_file_ids)
                new_file_ids.append(openai_file.id)
                
                # Update assistant with both tools
                client.beta.assistants.update(
                    assistant_id=assistant,
                    tools=existing_tools,
                    tool_resources={
                        "file_search": {"vector_store_ids": vector_store_ids},
                        "code_interpreter": {"file_ids": new_file_ids}
                    }
                )
                
        elif is_excel:
            # Process Excel file for code_interpreter
            sheet_info = await process_excel_file(file_path, file.filename)
            file_info = sheet_info
            
            # Upload each CSV sheet file for code_interpreter
            new_file_ids = list(existing_file_ids)
            for sheet in sheet_info:
                with open(sheet['csv_path'], "rb") as file_stream:
                    openai_file = client.files.create(
                        file=file_stream,
                        purpose="assistants"
                    )
                    new_file_ids.append(openai_file.id)
            
            # Update assistant with file IDs
            client.beta.assistants.update(
                assistant_id=assistant,
                tools=existing_tools,
                tool_resources={
                    "file_search": {"vector_store_ids": vector_store_ids},
                    "code_interpreter": {"file_ids": new_file_ids}
                }
            )
                
        elif is_image:
            # Process image file
            # If session provided, analyze image and add to thread
            if session:
                analysis_text = await image_analysis(client, file_content, file.filename, prompt)
                
                # Add the analysis to the thread
                client.beta.threads.messages.create(
                    thread_id=session,
                    role="user",
                    content=f"Image Analysis for {file.filename}: {analysis_text}"
                )
                logging.info(f"Added image analysis to thread {session}")
                
                # Store OCR/description in a text file with same name
                ocr_file_path = os.path.join('/tmp/', f"{os.path.splitext(file.filename)[0]}_ocr.txt")
                with open(ocr_file_path, 'w') as f:
                    f.write(analysis_text)
                    
                file_info = {
                    "filename": file.filename,
                    "path": file_path,
                    "ocr_path": ocr_file_path,
                    "file_type": "image"
                }
            
        else:
            # For other document types, upload to vector store
            with open(file_path, "rb") as file_stream:
                file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                    vector_store_id=vector_store_id,
                    files=[file_stream]
                )
            logging.info(f"File uploaded to vector store: status={file_batch.status}, count={file_batch.file_counts}")
            
            # Update assistant if tools were added
            if len(existing_tools) > len(assistant_obj.tools):
                client.beta.assistants.update(
                    assistant_id=assistant,
                    tools=existing_tools,
                    tool_resources={
                        "file_search": {"vector_store_ids": vector_store_ids},
                        "code_interpreter": {"file_ids": existing_file_ids}
                    }
                )
                
            file_info = {
                "filename": file.filename,
                "path": file_path,
                "file_type": "document"
            }
            
        # Update file registry in thread if session exists and file_info exists
        if session and file_info:
            await update_file_registry(client, session, file_info)
            
        # If context provided and session exists, update context
        if context and session:
            try:
                await update_context(client, session, context)
            except Exception as e:
                logging.error(f"Error updating context in thread: {e}")
                # Continue even if context update fails

        return JSONResponse(
            {
                "message": "File successfully processed and added to assistant.",
                "image_analyzed": is_image and session is not None,
                "file_type": "csv" if is_csv else "excel" if is_excel else "image" if is_image else "document"
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
    context: Optional[str] = None,
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
    context: Optional[str] = None,
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
async def trim_thread(request: Request, assistant_id: str = Form(None), max_age_days: Optional[int] = Form(None)):
    """
    Gets all threads for a given assistant, summarizes them, and removes old threads.
    Uses 48 hours as the threshold for thread cleanup.
    """
    # Get parameters from form data if not provided in query
    form_data = await request.form()
    if not assistant_id:
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
async def file_cleanup(request: Request, vector_store_id: str = Form(None), assistant_id: str = Form(None)):
    """
    Cleans up files older than 48 hours:
    1. Removes files from vector store
    2. Unsets file_ids for code_interpreter to avoid ghost file references
    3. Cleans up local temporary files
    """
    # Get parameters from form data if not provided
    form_data = await request.form()
    if not vector_store_id:
        vector_store_id = form_data.get("vector_store_id")
    
    if not assistant_id:
        assistant_id = form_data.get("assistant_id")
    
    if not vector_store_id or not assistant_id:
        raise HTTPException(status_code=400, detail="Both vector_store_id and assistant_id are required")
    
    client = create_client()
    deleted_count = 0
    skipped_count = 0
    cleaned_ci_files = 0
    
    try:
        # Get current time for age comparison
        now = datetime.datetime.now()
        
        # PART 1: Clean vector store files
        if vector_store_id:
            # Get all files in the vector store
            file_batches = client.beta.vector_stores.file_batches.list(vector_store_id=vector_store_id)
            
            if file_batches.data:
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
                            deleted_count += 1
                        except Exception as e:
                            logging.error(f"Error deleting file {file.id} from vector store: {e}")
        
        # PART 2: Clean code interpreter files
        if assistant_id:
            try:
                # Get assistant to check code_interpreter files
                assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant_id)
                
                # Get code_interpreter file IDs
                tool_resources = getattr(assistant_obj, 'tool_resources', {})
                code_interpreter_resource = getattr(tool_resources, 'code_interpreter', {})
                file_ids = getattr(code_interpreter_resource, 'file_ids', [])
                
                if file_ids:
                    files_to_keep = []
                    
                    # Check each file's age
                    for file_id in file_ids:
                        try:
                            file_obj = client.files.retrieve(file_id=file_id)
                            # Check if file is older than 48 hours
                            file_created = datetime.datetime.fromtimestamp(file_obj.created_at)
                            file_age_hours = (now - file_created).total_seconds() / 3600
                            
                            if file_age_hours <= 48:
                                # Keep recent files
                                files_to_keep.append(file_id)
                            else:
                                # Delete old files
                                try:
                                    client.files.delete(file_id=file_id)
                                    cleaned_ci_files += 1
                                except Exception as e:
                                    logging.error(f"Error deleting code interpreter file {file_id}: {e}")
                        except Exception as e:
                            logging.error(f"Error retrieving file {file_id}: {e}")
                    
                    # Update assistant with only recent files
                    if len(files_to_keep) < len(file_ids):
                        client.beta.assistants.update(
                            assistant_id=assistant_id,
                            tool_resources={
                                "code_interpreter": {"file_ids": files_to_keep}
                            }
                        )
                        logging.info(f"Updated assistant {assistant_id} to remove {len(file_ids) - len(files_to_keep)} old files")
                    
            except Exception as e:
                logging.error(f"Error cleaning code interpreter files: {e}")
        
        # PART 3: Clean local temporary files
        tmp_dir = '/tmp'
        local_files_cleaned = 0
        
        try:
            for filename in os.listdir(tmp_dir):
                if filename == '.' or filename == '..':
                    continue
                    
                file_path = os.path.join(tmp_dir, filename)
                # Check if it's a regular file (not directory)
                if os.path.isfile(file_path):
                    # Get file modification time
                    file_mtime = os.path.getmtime(file_path)
                    file_mtime_dt = datetime.datetime.fromtimestamp(file_mtime)
                    file_age_hours = (now - file_mtime_dt).total_seconds() / 3600
                    
                    # Remove files older than 48 hours
                    if file_age_hours > 48:
                        try:
                            os.remove(file_path)
                            local_files_cleaned += 1
                        except Exception as e:
                            logging.error(f"Error removing local file {file_path}: {e}")
        except Exception as e:
            logging.error(f"Error cleaning local files: {e}")
        
        return JSONResponse({
            "status": "File cleanup completed",
            "vector_store_id": vector_store_id,
            "assistant_id": assistant_id,
            "vector_store_files_deleted": deleted_count,
            "batches_skipped": skipped_count,
            "code_interpreter_files_cleaned": cleaned_ci_files,
            "local_files_cleaned": local_files_cleaned
        })
        
    except Exception as e:
        logging.error(f"Error in file-cleanup: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to clean up files: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
