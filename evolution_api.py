import requests
import os
import mimetypes
import logging

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class EvolutionAPI:
    def __init__(self, base_url=None, api_key=None, instance_name=None):
        self.base_url = base_url or os.environ.get("EVOLUTION_API_URL")
        self.api_key = api_key or os.environ.get("EVOLUTION_API_KEY")
        self.instance_name = instance_name or os.environ.get("EVOLUTION_API_INSTANCE", "hamar_ai")
        
    def is_configured(self):
        return bool(self.base_url and self.api_key and self.instance_name)

    def send_message(self, phone_number, text, media_path=None):
        """
        Sends a WhatsApp message via Evolution API.
        If not configured, simulates a successful/failed delivery.
        """
        # Formulate final phone number (Evolution API expects plain digits)
        cleaned_number = ''.join(filter(str.isdigit, phone_number))
        
        if not self.is_configured():
            # Run simulation
            import time
            import random
            time.sleep(0.5) # Simulate API latency
            
            # 5% chance of simulated failure for realism
            if random.random() < 0.05:
                return False, "Simulated error: Recipient WhatsApp account not found"
            
            logger.info(f"[SIMULATED] Message sent to {cleaned_number}. Content: {text[:30]}...")
            return True, None

        headers = {
            "apikey": self.api_key,
            "Content-Type": "application/json"
        }

        # If there is media, send via sendMedia endpoint
        if media_path:
            # Resolve relative paths (like /static/uploads/...) to absolute paths
            if media_path.startswith("/static/") or media_path.startswith("static/"):
                rel_path = media_path.lstrip("/")
                base_dir = os.path.dirname(os.path.abspath(__file__))
                resolved_path = os.path.join(base_dir, rel_path)
            else:
                resolved_path = media_path

            if not os.path.exists(resolved_path):
                logger.error(f"Media file not found at: {resolved_path}")
                return False, f"Media file not found at: {resolved_path}"

            filename = os.path.basename(resolved_path)
            mime_type, _ = mimetypes.guess_type(resolved_path)
            
            # Map mime type to Evolution API mediatype
            media_type = "document"
            if mime_type:
                if mime_type.startswith("image/"):
                    media_type = "image"
                elif mime_type.startswith("video/"):
                    media_type = "video"
                elif mime_type.startswith("audio/"):
                    media_type = "audio"
            
            url = f"{self.base_url.rstrip('/')}/message/sendMedia/{self.instance_name}"
            
            try:
                import base64
                with open(resolved_path, "rb") as f:
                    file_content = f.read()
                    base64_data = base64.b64encode(file_content).decode("utf-8")
                
                # Evolution API flat payload for sending media
                payload = {
                    "number": cleaned_number,
                    "mediatype": media_type,
                    "mimetype": mime_type or "application/octet-stream",
                    "media": base64_data,
                    "fileName": filename
                }
                if text:
                    payload["caption"] = text
                
                logger.info(f"Sending media message to {cleaned_number} using endpoint {url}")
                response = requests.post(url, json=payload, headers=headers, timeout=20)
                
                if response.status_code in [200, 201]:
                    logger.info(f"Media message sent successfully to {cleaned_number}")
                    return True, None
                else:
                    logger.error(f"Evolution API sendMedia error (Status {response.status_code}): {response.text}")
                    return False, f"Evolution API Error: {response.text}"
                    
            except Exception as e:
                logger.error(f"Failed to encode/send media to {cleaned_number}: {e}")
                return False, f"Failed to encode/send media: {str(e)}"
        else:
            # Send text message with Flat Payload
            url = f"{self.base_url.rstrip('/')}/message/sendText/{self.instance_name}"
            payload = {
                "number": cleaned_number,
                "text": text
            }
            
            logger.info(f"Sending text message to {cleaned_number} using endpoint {url}")
            try:
                response = requests.post(url, json=payload, headers=headers, timeout=20)
                if response.status_code in [200, 201]:
                    logger.info(f"Text message sent successfully to {cleaned_number}")
                    return True, None
                else:
                    logger.error(f"Evolution API sendText error (Status {response.status_code}): {response.text}")
                    return False, f"Evolution API Error: {response.text}"
            except Exception as e:
                logger.error(f"Connection to Evolution API failed for {cleaned_number}: {e}")
                return False, f"Connection to Evolution API failed: {str(e)}"
