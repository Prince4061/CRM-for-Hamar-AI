# Hamar AI Outreach CRM - Feature Roadmap

This document outlines high-impact, premium features that can be added to make this WhatsApp Outreach CRM a enterprise-grade SaaS platform.

---

## 1. 📱 Direct WhatsApp Instance Manager & QR Code Scanner (Highest Priority)
Currently, creating instances and scanning QR codes requires external tools (like Curl or Postman). 
* **The Feature**: Add an **"Instance Settings"** tab in the sidebar where users can:
  * See all active Evolution API instances.
  * Create a new instance (e.g., `qsai`) directly from the UI.
  * Fetch and display the **Live QR Code** inside the CRM dashboard so the user can scan it directly to link their phone.
  * View connection status (`CONNECTED` or `DISCONNECTED`) in real time.

---

## 2. 👥 Message Personalization (Template Variables)
Sending the exact same message to hundreds of contacts increases the risk of WhatsApp banning the number.
* **The Feature**: Enable template variables in the campaign composer.
  * Allow users to write: `"Hello {name}, we found properties matching your search in {city}."`
  * The system will dynamically replace `{name}` and other custom columns from the uploaded client CSV list during execution.

---

## 3. 🛡️ Anti-Ban Smart Throttling & Random Delay
To mimic human behavior and protect WhatsApp numbers from getting banned/flagged as spam.
* **The Feature**:
  * **Randomized Delays**: Define a range (e.g., delay between 10 to 30 seconds) instead of a fixed delay.
  * **Daily Limits**: Set a daily threshold limit (e.g., maximum 200 messages per day per instance).
  * **Batch Sending**: Send messages in small batches (e.g., 20 messages, then pause for 10 minutes).

---

## 4. 💬 Interactive Webhook Listener & Live Chat Inbox
Track when clients reply to your campaign messages.
* **The Feature**:
  * **Webhook Endpoint**: Create a `/webhook/evolution` route in the Flask app to receive incoming messages.
  * **Live Chat Panel**: A unified inbox where you can see client replies in real-time and reply to them directly from the CRM without opening WhatsApp.
  * **Lead Status Auto-Update**: If a user replies with positive intent, mark their lead status as "Interested" automatically.

---

## 5. 🤖 Automated AI Agent Auto-Reply (LLM Integration)
Give the CRM "brains" to handle customer responses automatically.
* **The Feature**:
  * Integrate OpenAI / Gemini / Claude API.
  * When a customer replies to a broadcast, the AI reads the property context (from a pre-defined prompt or database) and replies naturally on WhatsApp.
  * Transfer to a human agent automatically if they ask to speak to someone or ask complex questions.

---

## 6. 📊 Advanced Analytics & Export Reports
Go beyond basic "Sent" / "Failed" tracking.
* **The Feature**:
  * Track read receipts (delivered vs. read/blue-ticked rates).
  * Graphical charts representing response rates and engagement over time.
  * **Export to PDF/Excel**: Generate beautiful executive campaign summaries to share or analyze offline.
