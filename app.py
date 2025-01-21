import logging
from fastapi import FastAPI, Request, UploadFile, Form, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from openai import AzureOpenAI
from typing import Optional
import os

app = FastAPI()

# Global and product-specific contexts
global_context = {
    "assistant_id": None,
    "session_id": None,
    "vector_store_id": None
}
product_context = {}

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

async def ensure_context(product_id: Optional[str] = None):
    """
    Ensures that either the global context or a product-specific context is initialized.
    If not, it calls initiate_chat to set it up.
    """
    context = global_context if product_id is None else product_context.get(product_id)

    if not context or not context.get("assistant_id") or not context.get("session_id") or not context.get("vector_store_id"):
        logging.info(f"Context missing for {'global' if product_id is None else f'product_id: {product_id}'}. Initializing.")
        response = await initiate_chat(product_id=product_id)
        # simulate FastAPI response reading
        if response.status_code == 200:
            data = response.body
            # response.body is bytes, decode and parse as needed
            # In FastAPI, we can directly access response in memory if needed.
            # Here, since we call internally, let's assume JSONResponse returns a dict.
            # Adjust as needed if calling differently.

            # Since we are calling initiate_chat() directly, let's just trust it returns JSONResponse and extract the dictionary from it.
            # In actual scenario, we could refactor initiate_chat to return a dict and convert to JSONResponse at the end.
            # For simplicity, let's decode here:
            data = response.media  # JSONResponse provides .media as the parsed content

            new_context = {
                "assistant_id": data["assistant"],
                "session_id": data["session"],
                "vector_store_id": data["vector_store"]
            }
            if product_id:
                product_context[product_id] = new_context
            else:
                global_context.update(new_context)
        else:
            raise HTTPException(status_code=500, detail="Failed to initialize context.")

@app.post("/initiate-chat")
async def initiate_chat(request: Request, product_id: Optional[str] = None):
    """Initiates the assistant and session and optionally uploads a file to its vector store, 
    all in one go. No separate /upload-file call is needed."""
    client = create_client()

    # Parse the form data
    form = await request.form()
    file = form.get("file", None)

    # Create a vector store up front.
    vector_store = client.beta.vector_stores.create(name="demo")

    # Always include file_search tool and associate with the vector store, even if no file is provided yet.
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

    # Update the global or product-specific context
    context = {
        "assistant_id": assistant.id,
        "session_id": thread.id,
        "vector_store_id": vector_store.id
    }

    if product_id:
        product_context[product_id] = context
    else:
        global_context.update(context)

    # If a file is provided, upload it now that assistant and vector store are ready
    if file:
        filename = file.filename
        file_path = os.path.join('/tmp/', filename)

        # Save the file locally
        with open(file_path, 'wb') as f:
            f.write(await file.read())

        # Upload the file to the existing vector store
        with open(file_path, "rb") as file_stream:
            file_batch = client.beta.vector_stores.file_batches.upload_and_poll(
                vector_store_id=vector_store.id, 
                files=[file_stream]
            )
        logging.info(f"File uploaded to vector store: status={file_batch.status}, count={file_batch.file_counts}")

    res = {
        "assistant": assistant.id,
        "session": thread.id,
        "vector_store": vector_store.id
    }

    return JSONResponse(res, media_type="application/json", status_code=200)
@app.post("/co-pilot")
async def co_pilot(request: Request, product_id: Optional[str] = None):
    """Handles co-pilot creation or updates with optional file upload and system prompt."""
    await ensure_context(product_id)
    context = global_context if product_id is None else product_context[product_id]

    form = await request.form()
    file = form.get("file", None)
    system_prompt = form.get("system_prompt", None)

    client = create_client()

    try:
        assistant_id = context.get("assistant_id")
        vector_store_id = context.get("vector_store_id")

        # Handle file upload
        if file:
            # If no vector store, create it
            if not vector_store_id:
                vector_store = client.beta.vector_stores.create(name="demo")
                vector_store_id = vector_store.id
                context["vector_store_id"] = vector_store_id

                # Update assistant
                assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant_id)
                client.beta.assistants.update(
                    assistant_id=assistant_id,
                    tools=assistant_obj.tools + [{"type": "file_search"}],
                    tool_resources={"file_search": {"vector_store_ids": [vector_store_id]}},
                )

            # Save and upload file
            file_path = f"/tmp/{file.filename}"
            with open(file_path, "wb") as f:
                f.write(await file.read())

            with open(file_path, "rb") as file_stream:
                client.beta.vector_stores.file_batches.upload_and_poll(
                    vector_store_id=vector_store_id,
                    files=[file_stream]
                )

        # Update assistant instructions
        client.beta.assistants.update(
            assistant_id=assistant_id,
            instructions=(
                f"You are a product management AI assistant, a product co-pilot. {system_prompt}"
                if system_prompt
                else "You are a product management AI assistant, a product co-pilot."
            ),
        )

        return JSONResponse(
            {
                "message": "Assistant updated successfully.",
                "assistant": assistant_id,
                "vector_store": vector_store_id,
            }
        )

    except Exception as e:
        logging.error(f"Error in co-pilot: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/upload-file")
async def upload_file(file: UploadFile = Form(...), assistant: str = Form(...)):
    """
    Uploads a file and associates it with the given assistant.
    Maintains the same input-output as the current version, and ensures a single vector store per assistant.
    """
    client = create_client()

    try:
        # Retrieve the assistant
        assistant_obj = client.beta.assistants.retrieve(assistant_id=assistant)

        # 'tool_resources' is an object, not a dict.
        # Access the 'file_search' attribute if it exists.
        file_search_resource = None
        if assistant_obj.tool_resources and hasattr(assistant_obj.tool_resources, "file_search"):
            file_search_resource = assistant_obj.tool_resources.file_search

        # If file_search_resource exists and has vector_store_ids, retrieve them; otherwise use an empty list.
        vector_store_ids = file_search_resource.vector_store_ids if (file_search_resource and hasattr(file_search_resource, "vector_store_ids")) else []

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

        # Save the uploaded file locally
        file_path = f"/tmp/{file.filename}"
        with open(file_path, "wb") as temp_file:
            temp_file.write(await file.read())

        # Upload the file to the existing vector store
        with open(file_path, "rb") as file_stream:
            client.beta.vector_stores.file_batches.upload_and_poll(
                vector_store_id=vector_store_id,
                files=[file_stream]
            )

        return JSONResponse(
            {
                "message": "File successfully uploaded to vector store.",
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
    product_id: Optional[str] = None,
):
    """
    Handles conversation queries. 
    Preserves the original query parameters and output format.
    """
    await ensure_context(product_id)
    context = global_context if product_id is None else product_context[product_id]

    client = create_client()

    try:
        # Use session and assistant from context if not explicitly provided
        session_id = session or context.get("session_id")
        assistant_id = assistant or context.get("assistant_id")

        if not session_id or not assistant_id:
            raise HTTPException(status_code=400, detail="Session or assistant not initialized.")

        # Add message if prompt given
        if prompt:
            client.beta.threads.messages.create(
                thread_id=session_id,
                role="user",
                content=prompt
            )

        def stream_response():
            buffer = []
            try:
                with client.beta.threads.runs.stream(thread_id=session_id, assistant_id=assistant_id) as stream:
                    for text in stream.text_deltas:
                        buffer.append(text)
                        if len(buffer) >= 10:  # Adjust chunk size as needed
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

# @app.api_route("/chat", methods=["GET", "POST"])
# async def chat(query: str, product_id: Optional[str] = None):
#     """
#     Handles chat requests. 
#     Maintains the same input and output format as original. 
#     """
#     await ensure_context(product_id)
#     context = global_context if product_id is None else product_context[product_id]

#     client = create_client()

#     try:
#         session_id = context.get("session_id")
#         assistant_id = context.get("assistant_id")

#         # Add user message
#         client.beta.threads.messages.create(
#             thread_id=session_id,
#             role="user",
#             content=query
#         )

#         def stream_response():
#             buffer = []
#             try:
#                 with client.beta.threads.runs.stream(thread_id=session_id, assistant_id=assistant_id) as stream:
#                     for text in stream.text_deltas:
#                         buffer.append(text)
#                         if len(buffer) >= 10:  # Adjust chunk size as needed
#                             yield ''.join(buffer)
#                             buffer = []
#                 if buffer:
#                     yield ''.join(buffer)
#             except Exception as e:
#                 logging.error(f"Streaming error: {e}")
#                 yield "[ERROR] The response was interrupted. Please try again."
#         return StreamingResponse(stream_response(), media_type="text/event-stream")

#     except Exception as e:
#         logging.error(f"Error in chat: {e}")
#         raise HTTPException(status_code=500, detail="Failed to process chat")

@app.get("/chat")
async def chat(
    session: Optional[str] = None,
    prompt: Optional[str] = None,
    assistant: Optional[str] = None,
    product_id: Optional[str] = None,
):
    """
    Handles conversation queries. 
    Preserves the original query parameters and output format.
    """
    await ensure_context(product_id)
    context = global_context if product_id is None else product_context[product_id]

    client = create_client()

    try:
        # Use session and assistant from context if not explicitly provided
        session_id = session or context.get("session_id")
        assistant_id = assistant or context.get("assistant_id")

        if not session_id or not assistant_id:
            raise HTTPException(status_code=400, detail="Session or assistant not initialized.")

        # Add message if prompt given
        if prompt:
            client.beta.threads.messages.create(
                thread_id=session_id,
                role="user",
                content=prompt
            )

        response_text = []

        try:
            # Use the stream to collect all text deltas
            with client.beta.threads.runs.stream(thread_id=session_id, assistant_id=assistant_id) as stream:
                for text in stream.text_deltas:
                    response_text.append(text)
        except Exception as e:
            logging.error(f"Streaming error: {e}")
            raise HTTPException(status_code=500, detail="The response was interrupted. Please try again.")

        # Join all collected text into a single string
        full_response = ''.join(response_text)

        # Return the full response as JSON
        return JSONResponse(content={"response": full_response})

    except Exception as e:
        logging.error(f"Error in conversation: {e}")
        raise HTTPException(status_code=500, detail="Failed to process conversation")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

