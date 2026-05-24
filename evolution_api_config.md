# Evolution API Integration & Configuration Guide

This guide describes how to configure, run, and integrate **Evolution API v1/v2** with the **Hamar AI Outreach CRM** to send WhatsApp messages and media files.

---

## 1. Environment Variables Configuration

The application reads configurations from environment variables or directly from the `.env` settings. To connect the CRM to your Evolution API host, set the following environment variables in your deployment environment or server settings:

| Variable Name | Description | Example / Recommended Value |
| :--- | :--- | :--- |
| `EVOLUTION_API_URL` | The base URL of your running Evolution API server | `http://72.60.211.205:8080` |
| `EVOLUTION_API_KEY` | Global authorization key for API authentication | `CHANGE_THIS_TO_A_STRONG_PASSWORD-` |
| `EVOLUTION_API_INSTANCE` | The specific instance name configured in Evolution API | `qsai` |

If these variables are not set or left empty, the application will automatically enter **Simulation Mode** (where it pretends to send messages with realistic latencies and success/failure rates).

---

## 2. Managing Evolution API Instances

### A. Creating an Instance
To create a new WhatsApp instance (e.g. `qsai`), send a `POST` request to the `/instance/create` endpoint:

* **Endpoint**: `POST {{EVOLUTION_API_URL}}/instance/create`
* **Headers**:
  ```http
  apikey: YOUR_GLOBAL_API_KEY
  Content-Type: application/json
  ```
* **Request Body**:
  ```json
  {
    "instanceName": "qsai",
    "token": "CHOOSE_A_SECURE_TOKEN_FOR_THIS_INSTANCE",
    "qrcode": true,
    "integration": "WHATSAPP-BAILEYS"
  }
  ```

### B. Connecting WhatsApp via QR Code
Once the instance is created, retrieve the QR code to pair your phone:

* **Endpoint**: `GET {{EVOLUTION_API_URL}}/instance/connect/qsai`
* **Headers**:
  ```http
  apikey: YOUR_GLOBAL_API_KEY
  ```
* Scan the returned QR code image with your WhatsApp app (**Linked Devices -> Link a Device**).

---

## 3. How Messages are Sent (Python Implementation)

The CRM encapsulates Evolution API message-sending inside `evolution_api.py`. The methods use standard JSON payloads (Flat Payloads) to communicate with your WhatsApp instance.

### A. Text Messages
For text-only campaigns, the CRM sends a `POST` request to the `/message/sendText/{instance_name}` endpoint:

* **Endpoint**: `POST {{EVOLUTION_API_URL}}/message/sendText/qsai`
* **Headers**:
  ```http
  apikey: YOUR_GLOBAL_API_KEY
  Content-Type: application/json
  ```
* **Payload**:
  ```json
  {
    "number": "919876543210",
    "text": "Hello! This is a test broadcast message."
  }
  ```

### B. Media Messages (Images, Videos, PDFs, Audios)
For campaigns with attachments, the CRM reads the local media file, encodes it as a base64 string, and sends a `POST` request to the `/message/sendMedia/{instance_name}` endpoint:

* **Endpoint**: `POST {{EVOLUTION_API_URL}}/message/sendMedia/qsai`
* **Headers**:
  ```http
  apikey: YOUR_GLOBAL_API_KEY
  Content-Type: application/json
  ```
* **Payload**:
  ```json
  {
    "number": "919876543210",
    "mediatype": "image",
    "mimetype": "image/png",
    "media": "iVBORw0KGgoAAAANSUhEUgAAADIAAAAyCAYAAAAeP4ixAAA...",
    "fileName": "offer_brochure.png",
    "caption": "Check out this beautiful design for you!"
  }
  ```

#### Media Type Auto-Detection Matrix:
* **Image Files** (`.png`, `.jpg`, `.jpeg`): `mediatype` -> `"image"`
* **Video Files** (`.mp4`, `.avi`): `mediatype` -> `"video"`
* **Audio Files** (`.mp3`, `.wav`, `.ogg`): `mediatype` -> `"audio"`
* **Other Files** (`.pdf`, `.csv`, `.docx`): `mediatype` -> `"document"`

---

## 4. Integration Code Snippet
Here is the core function from `evolution_api.py` handling the API communication:

```python
import base64
import requests
import mimetypes

# Setup basic request headers
headers = {
    "apikey": api_key,
    "Content-Type": "application/json"
}

# Sending media
if media_path:
    with open(media_path, "rb") as f:
        base64_data = base64.b64encode(f.read()).decode("utf-8")
        
    payload = {
        "number": cleaned_number,
        "mediatype": media_type,
        "mimetype": mime_type,
        "media": base64_data,
        "fileName": filename,
        "caption": text
    }
    url = f"{base_url}/message/sendMedia/{instance_name}"
    response = requests.post(url, json=payload, headers=headers)
    
# Sending text
else:
    payload = {
        "number": cleaned_number,
        "text": text
    }
    url = f"{base_url}/message/sendText/{instance_name}"
    response = requests.post(url, json=payload, headers=headers)
```

---

## 5. Troubleshooting & Best Practices

1. **Phone Number Formatting**: Ensure phone numbers do not contain special characters (`+`, `-`, spaces). The clean script in `evolution_api.py` automatically strips non-numeric characters before submitting.
2. **Instance State**: Ensure the instance is `CONNECTED` using `GET /instance/connectionState/qsai`. If the state is `DISCONNECTED`, scan the QR code again.
3. **Large File Uploads**: If you are sending files larger than 10MB, ensure your server's payload limit (e.g. Nginx client body limit) is configured to handle the size of base64 payloads.
