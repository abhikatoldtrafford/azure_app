# Azure Copilot v2 - AI-Powered Assistant API

A comprehensive FastAPI-based AI assistant service powered by Azure OpenAI, offering conversational AI, document analysis, data processing, and content generation capabilities.

🌐 **Live API**: https://copilotv2.azurewebsites.net  
📚 **API Documentation**: https://copilotv2.azurewebsites.net/docs  
🎨 **Alternative Docs**: https://copilotv2.azurewebsites.net/redoc

## 📋 Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Quick Start](#quick-start)
- [API Endpoints](#api-endpoints)
  - [Health & Status](#health--status)
  - [AI Operations](#ai-operations)
  - [Chat Operations](#chat-operations)
  - [Data Processing](#data-processing)
  - [File Operations](#file-operations)
- [Usage Examples](#usage-examples)
- [Architecture](#architecture)
- [Security](#security)
- [Error Handling](#error-handling)
- [Development](#development)
- [Troubleshooting](#troubleshooting)

## 🚀 Overview

Azure Copilot v2 is an advanced AI assistant API that combines the power of Azure OpenAI with specialized tools for:
- 💬 Conversational AI with context awareness
- 📊 Data analysis using pandas for CSV/Excel files
- 📄 Document processing (PDF, DOCX, TXT, HTML, and more)
- 🖼️ Image analysis and understanding
- 📝 Content generation with export capabilities
- 🔍 Review extraction and structured data mining
- 🌊 Real-time streaming responses

## ✨ Features

### Core Capabilities
- **Multi-modal AI**: Process text, images, and documents
- **Stateless & Stateful Operations**: Both one-off completions and persistent conversations
- **File Processing**: Automatic handling of various file formats
- **Data Analysis**: Built-in pandas agent for CSV/Excel analysis
- **Export Options**: Generate downloadable CSV, Excel, and DOCX files
- **Streaming Support**: Real-time response streaming via Server-Sent Events (SSE)
- **Vector Search**: Document indexing and semantic search
- **Session Management**: Thread-based conversation tracking with context preservation

### Supported File Types
- **Documents**: PDF, DOCX, DOC, TXT, MD, HTML, JSON, XML, RTF, ODT
- **Data**: CSV, XLSX, XLS
- **Images**: JPG, JPEG, PNG, GIF, BMP, WEBP
- **Code**: PY, JS, JAVA, CPP, CS, PHP, RB, GO, RUST, SWIFT, KT

## 🏃 Quick Start

### Basic Text Completion
```bash
curl -X POST https://copilotv2.azurewebsites.net/completion \
  -F "prompt=Write a haiku about coding" \
  -F "temperature=0.7"
```

### Generate CSV Data
```bash
curl -X POST https://copilotv2.azurewebsites.net/completion \
  -F "prompt=Create a CSV with 10 employees: name, department, salary, hire_date" \
  -F "output_format=csv"
```

### Extract Reviews from Text
```bash
# Create a sample reviews file
echo -e "John: Amazing product! 5 stars\nJane: Not worth it. 2 stars" > reviews.txt

# Extract to structured CSV
curl -X POST https://copilotv2.azurewebsites.net/extract-reviews \
  -F "file=@reviews.txt" \
  -F "output_format=csv"
```

## 📚 API Endpoints

### Health & Status

#### `GET /`
Serves the AI Assistant Hub webpage interface.

#### `GET /health`
Basic health check for monitoring.

```bash
curl https://copilotv2.azurewebsites.net/health
```

**Response:**
```json
{
  "status": "healthy",
  "timestamp": "2024-01-15T10:30:00"
}
```

#### `GET /health-check`
Comprehensive system health check.

```bash
curl https://copilotv2.azurewebsites.net/health-check
```

**Response:**
```json
{
  "status": "healthy",
  "health_percentage": 100,
  "checks": {
    "azure_openai": {"status": "healthy"},
    "file_system": {"status": "healthy"},
    "dependencies": {"status": "healthy"},
    "pandas_agent": {"status": "healthy"}
  },
  "endpoints_tested": {
    "/health": {"status": "healthy", "response_time_ms": 5.2},
    "/docs": {"status": "healthy", "response_time_ms": 12.3}
  },
  "execution_time_ms": 245,
  "summary": {
    "total_checks": 10,
    "healthy_checks": 10,
    "warnings_count": 0,
    "errors_count": 0
  }
}
```

#### `POST /test-comprehensive` ⚡ STREAMING
Run comprehensive system tests with real-time updates.

**Parameters:**
- `test_mode`: Test mode (all, long_thread, concurrent, same_thread, scaling, tools)
- `test_duration`: Test duration in seconds (10-300, default: 60)
- `concurrent_users`: Number of concurrent users (1-20, default: 5)
- `messages_per_thread`: Messages for long thread test (10-200, default: 60)
- `verbose`: Include detailed logs (default: true)

```bash
# Stream test results in real-time
curl -N -X POST https://copilotv2.azurewebsites.net/test-comprehensive \
  -F "test_mode=concurrent" \
  -F "concurrent_users=10" \
  -F "test_duration=30"
```

### AI Operations

#### `POST /completion`
Generate AI completions without maintaining conversation state.

**Parameters:**
- `prompt` (required): The user's message or question
- `model` (optional): Model to use (default: "gpt-4.1")
- `temperature` (optional): Response randomness 0-2 (default: 0.7)
- `max_tokens` (optional): Maximum response length (default: 1000)
- `system_message` (optional): Custom system prompt
- `output_format` (optional): "csv", "excel", or "docx" for structured data
- `files` (optional): Upload files for analysis
- `rows_to_generate` (optional): Number of rows for data generation (default: 30)

**Example 1: Simple Text Generation**
```bash
curl -X POST https://copilotv2.azurewebsites.net/completion \
  -F "prompt=Explain quantum computing in simple terms" \
  -F "temperature=0.5" \
  -F "max_tokens=200"
```

**Example 2: Generate Excel File**
```bash
curl -X POST https://copilotv2.azurewebsites.net/completion \
  -F "prompt=Create a monthly budget spreadsheet with categories: income, expenses, savings" \
  -F "output_format=excel" \
  -F "temperature=0.1" \
  -F "rows_to_generate=50"
```

**Example 3: Analyze Uploaded Image**
```bash
curl -X POST https://copilotv2.azurewebsites.net/completion \
  -F "prompt=Describe this image in detail" \
  -F "files=@screenshot.png"
```

**Example 4: Process Multiple Files**
```bash
curl -X POST https://copilotv2.azurewebsites.net/completion \
  -F "prompt=Compare these two documents and summarize the differences" \
  -F "files=@document1.pdf" \
  -F "files=@document2.pdf"
```

### Chat Operations

#### `POST /initiate-chat`
Create a new chat session with an AI assistant.

**Parameters:**
- `file` (optional): Initial file to process
- `context` (optional): User persona or context information

```bash
curl -X POST https://copilotv2.azurewebsites.net/initiate-chat \
  -F "context=I am a data scientist working on sales analysis" \
  -F "file=@sales_data.csv"
```

**Response:**
```json
{
  "message": "Chat initiated successfully",
  "assistant": "asst_abc123...",
  "session": "thread_xyz789...",
  "vector_store": "vs_def456..."
}
```

#### `POST /co-pilot`
Create a new session using existing assistant and vector store.

**Parameters:**
- `assistant` (required): Assistant ID from previous session
- `vector_store` (optional): Vector store ID to reuse
- `file` (optional): Additional file to process
- `context` (optional): User context or request

```bash
curl -X POST https://copilotv2.azurewebsites.net/co-pilot \
  -F "assistant=asst_abc123" \
  -F "vector_store=vs_def456" \
  -F "context=Continue our previous analysis"
```

#### `GET /conversation` ⚡ STREAMING
Chat with streaming responses (Server-Sent Events format).

**Parameters:**
- `session` (required): Thread ID from initiate-chat
- `assistant` (required): Assistant ID
- `prompt` (required): User message

```bash
# Basic conversation
curl -N "https://copilotv2.azurewebsites.net/conversation?session=thread_xyz789&assistant=asst_abc123&prompt=What is the total revenue?"

# Follow-up question
curl -N "https://copilotv2.azurewebsites.net/conversation?session=thread_xyz789&assistant=asst_abc123&prompt=Break it down by quarter"
```

#### `POST /conversation` ⚡ STREAMING
Chat with file uploads and streaming responses.

**Parameters:**
- `session` (required): Thread ID
- `assistant` (required): Assistant ID
- `prompt` (required): User message
- `files` (optional): Files to analyze with the message

```bash
curl -N -X POST https://copilotv2.azurewebsites.net/conversation \
  -F "session=thread_xyz789" \
  -F "assistant=asst_abc123" \
  -F "prompt=Analyze this new data compared to our previous discussion" \
  -F "files=@new_data.csv"
```

#### `GET /chat`
Chat with complete responses (non-streaming).

**Parameters:**
- `session` (required): Thread ID
- `assistant` (required): Assistant ID
- `prompt` (required): User message

```bash
curl "https://copilotv2.azurewebsites.net/chat?session=thread_xyz789&assistant=asst_abc123&prompt=Summarize our conversation"
```

#### `POST /upload-file`
Upload files to an existing session.

**Parameters:**
- `file` (required): File to upload
- `assistant` (required): Assistant ID
- `session` (required): Thread ID
- `context` (optional): Additional context about the file

```bash
curl -X POST https://copilotv2.azurewebsites.net/upload-file \
  -F "file=@quarterly_report.xlsx" \
  -F "assistant=asst_abc123" \
  -F "session=thread_xyz789" \
  -F "context=This is Q4 2023 financial data"
```

### Data Processing

#### `POST /extract-reviews`
Extract structured review data from unstructured text.

**Parameters:**
- `file` (required): File containing reviews
- `columns` (optional): Comma-separated column names (default: "user,review,rating,date,source")
- `model` (optional): Model to use (default: "gpt-4.1")
- `temperature` (optional): Extraction precision (default: 0.1)
- `output_format` (optional): "csv" or "excel" (default: "csv")

```bash
# Extract reviews from a PDF
curl -X POST https://copilotv2.azurewebsites.net/extract-reviews \
  -F "file=@customer_feedback.pdf" \
  -F "columns=customer_name,feedback,rating,product,date" \
  -F "output_format=excel"
```

#### `POST /extract-to-structured-data`
Convert unstructured data to structured format with advanced options.

**Parameters:**
- `file` or `raw_text` or `prompt`: Input source
- `columns` (optional): Target column structure
- `rows_to_generate` (optional): Number of rows to generate
- `mode` (optional): "extract", "generate", or "auto"
- `temperature` (optional): Creativity level (0-2)
- `model` (optional): AI model to use
- `output_format` (optional): "csv" or "excel"
- `system_message` (optional): Custom instructions

```bash
# Extract data from a complex document
curl -X POST https://copilotv2.azurewebsites.net/extract-to-structured-data \
  -F "file=@annual_report.pdf" \
  -F "columns=metric,2022_value,2023_value,change_percentage,category" \
  -F "mode=extract" \
  -F "output_format=excel"
```

### File Operations

#### `GET /download-files/{filename}`
Download generated files.

```bash
# Download a generated CSV file
curl -O https://copilotv2.azurewebsites.net/download-files/generated_data_20240115_143022.csv

# Download with custom filename
curl https://copilotv2.azurewebsites.net/download-files/generated_data_20240115_143022.csv -o my_data.csv
```

#### `GET /verify-download/{filename}`
Check if a file is available for download.

```bash
curl https://copilotv2.azurewebsites.net/verify-download/generated_data_20240115_143022.csv
```

**Response:**
```json
{
  "available": true,
  "filename": "generated_data_20240115_143022.csv",
  "size": 2048,
  "mime_type": "text/csv"
}
```

#### `GET /download-chat`
Export chat conversation as DOCX.

**Parameters:**
- `session` (required): Thread ID
- `assistant` (required): Assistant ID

```bash
curl "https://copilotv2.azurewebsites.net/download-chat?session=thread_xyz789&assistant=asst_abc123" -o conversation.docx
```

## 🔧 Usage Examples

### Example 1: Complete Data Analysis Workflow

```bash
# 1. Create a session
RESPONSE=$(curl -s -X POST https://copilotv2.azurewebsites.net/initiate-chat \
  -F "context=I need to analyze sales data")
SESSION=$(echo $RESPONSE | jq -r .session)
ASSISTANT=$(echo $RESPONSE | jq -r .assistant)

# 2. Upload data file
curl -X POST https://copilotv2.azurewebsites.net/upload-file \
  -F "file=@sales_2023.csv" \
  -F "assistant=$ASSISTANT" \
  -F "session=$SESSION"

# 3. Analyze the data (streaming)
curl -N "https://copilotv2.azurewebsites.net/conversation?session=$SESSION&assistant=$ASSISTANT&prompt=What are the top 5 products by revenue?"

# 4. Generate a report
curl "https://copilotv2.azurewebsites.net/chat?session=$SESSION&assistant=$ASSISTANT&prompt=Create a detailed sales report with charts descriptions"

# 5. Export the conversation
curl "https://copilotv2.azurewebsites.net/download-chat?session=$SESSION&assistant=$ASSISTANT" -o sales_analysis_report.docx
```

### Example 2: Batch Review Processing

```bash
#!/bin/bash
# Process multiple review files

for file in reviews/*.txt; do
  echo "Processing $file..."
  RESPONSE=$(curl -s -X POST https://copilotv2.azurewebsites.net/extract-reviews \
    -F "file=@$file" \
    -F "output_format=excel")
  
  DOWNLOAD_URL=$(echo $RESPONSE | jq -r .download_url)
  FILENAME=$(basename "$file" .txt)
  
  curl -s "https://copilotv2.azurewebsites.net$DOWNLOAD_URL" -o "processed_${FILENAME}.xlsx"
done
```

### Example 3: Image Analysis Pipeline

```bash
# Analyze multiple images and compile results
echo "Image Analysis Report" > analysis_report.txt
echo "===================" >> analysis_report.txt

for img in images/*.jpg; do
  echo -e "\n\nAnalyzing: $img" >> analysis_report.txt
  curl -s -X POST https://copilotv2.azurewebsites.net/completion \
    -F "prompt=Describe this image focusing on: objects, text, colors, and any notable features" \
    -F "files=@$img" \
    | jq -r .content >> analysis_report.txt
done

# Convert report to DOCX
curl -X POST https://copilotv2.azurewebsites.net/completion \
  -F "prompt=Format this image analysis report professionally" \
  -F "files=@analysis_report.txt" \
  -F "output_format=docx"
```

### Example 4: Real-Time Data Generation Monitoring

```bash
# Generate data with streaming progress
curl -N -X POST https://copilotv2.azurewebsites.net/conversation \
  -F "session=thread_new" \
  -F "assistant=asst_new" \
  -F "prompt=Generate a comprehensive customer database with 100 records including names, emails, purchase history, and satisfaction scores" \
  | while IFS= read -r line; do
      if [[ $line == data:* ]]; then
          echo "${line:5}" | jq -r '.choices[0].delta.content // empty' 2>/dev/null
      fi
  done
```

## 🏗️ Architecture

### Technology Stack
- **Framework**: FastAPI with async support
- **AI Engine**: Azure OpenAI (GPT-4.1)
- **Data Processing**: Pandas, LangChain
- **File Handling**: PyPDF2, python-docx, Pillow, Unstructured
- **Export Generation**: openpyxl, python-docx
- **Hosting**: Azure App Service
- **Storage**: Temporary file system with automatic cleanup

### Key Components

1. **Stateless Completion Engine**
   - Direct AI completions without session management
   - Multi-modal input support
   - Automatic output formatting
   - File type detection and processing

2. **Session Manager**
   - Thread-based conversation tracking
   - Context preservation across messages
   - Multi-file awareness
   - Vector store integration

3. **Pandas Agent**
   - Automated data analysis
   - Natural language to pandas operations
   - Support for complex queries
   - Excel and CSV file handling

4. **File Processor**
   - Automatic file type detection
   - Content extraction and indexing
   - Format conversion
   - Image analysis support

5. **Export Engine**
   - CSV generation with proper formatting
   - Excel files with multiple sheets
   - DOCX documents with styling
   - Automatic file cleanup (10 file limit)

## 🔒 Security

### Best Practices
- Sanitized filenames prevent directory traversal
- File size limits prevent DoS attacks
- Secure file permissions (644) for downloads
- CORS headers configured for web access
- API key management through Azure

### Rate Limiting
The API implements reasonable rate limits:
- 100 requests per minute per IP
- 10MB max file upload size
- 10 file retention limit (FIFO)
- 300-second max test duration

## ⚠️ Production Considerations

This API is deployed in a **PRODUCTION** environment on Azure App Service. Please note:

1. **Endpoint Signatures**: All endpoints are stable and should not be modified
2. **API Compatibility**: Maintain backward compatibility for all changes
3. **Error Handling**: Comprehensive error responses for debugging
4. **Monitoring**: Use `/health` and `/health-check` endpoints
5. **File Storage**: Temporary storage only - files are not persisted

## ❌ Error Handling

### Common Error Responses

**400 Bad Request**
```json
{
  "status": "error",
  "message": "Invalid filename"
}
```

**404 Not Found**
```json
{
  "status": "error",
  "message": "File not found"
}
```

**422 Unprocessable Entity**
```json
{
  "detail": [
    {
      "loc": ["body", "prompt"],
      "msg": "field required",
      "type": "value_error.missing"
    }
  ]
}
```

**503 Service Unavailable**
```json
{
  "status": "error",
  "message": "AI service temporarily unavailable"
}
```

### Retry Strategy
```bash
# Example retry logic with exponential backoff
for i in {1..3}; do
  RESPONSE=$(curl -s -w "\n%{http_code}" https://copilotv2.azurewebsites.net/completion \
    -F "prompt=Test" -F "temperature=0.5")
  
  HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
  BODY=$(echo "$RESPONSE" | head -n-1)
  
  if [ "$HTTP_CODE" -eq 200 ]; then
    echo "$BODY"
    break
  else
    echo "Attempt $i failed with code $HTTP_CODE. Retrying..."
    sleep $((2**i))  # Exponential backoff: 2, 4, 8 seconds
  fi
done
```

## 💻 Development

### Local Setup

1. Clone the repository
```bash
git clone <repository-url>
cd azure-copilot-v2
```

2. Install dependencies
```bash
pip install -r requirements.txt
```

3. Set environment variables
```bash
export AZURE_ENDPOINT="https://kb-stellar.openai.azure.com/"
export AZURE_API_KEY="your-api-key"
export AZURE_API_VERSION="2024-12-01-preview"
```

4. Run locally
```bash
python app.py
# API will be available at http://localhost:8080
```

### Testing

Run the comprehensive test suite:
```bash
# Basic health check
curl http://localhost:8080/health

# Comprehensive health check
curl http://localhost:8080/health-check

# Run system tests (streaming)
curl -N -X POST http://localhost:8080/test-comprehensive \
  -F "test_mode=all" \
  -F "test_duration=30"

# Test specific endpoint
curl -X POST http://localhost:8080/completion \
  -F "prompt=Hello, world!"
```

## 🔍 Troubleshooting

### Common Issues

**1. File Download Failures**
- Check file exists: `GET /verify-download/{filename}`
- Verify health status: `GET /health-check`
- Check Azure logs for permission errors
- Ensure file hasn't been cleaned up (10 file limit)

**2. Timeout Errors**
- Use streaming endpoints (`/conversation`) for long responses
- Reduce `max_tokens` for faster responses
- Check Azure OpenAI quota limits
- Monitor with `/test-comprehensive` endpoint

**3. File Processing Errors**
- Verify file format is supported
- Check file size (<10MB recommended)
- Ensure file is not corrupted
- Test with `/extract-to-structured-data` in debug mode

**4. Session/Thread Errors**
- Verify thread ID is valid
- Check assistant ID exists
- Use `/co-pilot` to recover sessions
- Monitor thread continuity in responses

### Debug Commands

```bash
# Check service health with details
curl -v https://copilotv2.azurewebsites.net/health-check

# Test file system
curl https://copilotv2.azurewebsites.net/verify-download/test.txt

# Monitor streaming endpoint
curl -N "https://copilotv2.azurewebsites.net/conversation?session=test&assistant=test&prompt=test" \
  2>&1 | grep -E "(data:|error:)"

# Test comprehensive system performance
curl -N -X POST https://copilotv2.azurewebsites.net/test-comprehensive \
  -F "test_mode=all" \
  -F "verbose=true" \
  | jq '.data' 2>/dev/null
```

## 📊 Performance Tips

1. **Use Streaming for Long Operations**: Endpoints marked with ⚡ support streaming
2. **Batch File Processing**: Upload multiple files in a single request
3. **Optimize Prompts**: Clear, specific prompts yield better results
4. **Temperature Settings**: Lower values (0.1-0.3) for factual tasks, higher (0.7-1.0) for creative
5. **Session Reuse**: Use `/co-pilot` to continue previous analyses

## 📞 Support

For issues, feature requests, or contributions:
1. Check the [API Documentation](https://copilotv2.azurewebsites.net/docs)
2. Monitor [health status](https://copilotv2.azurewebsites.net/health-check)
3. Review Azure App Service logs
4. Submit issues via GitHub

---

**Version**: 1.0.0  
**Last Updated**: January 2025  
**API Endpoint**: https://copilotv2.azurewebsites.net  
**Status Page**: https://copilotv2.azurewebsites.net/health-check
