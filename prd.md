Yeh raha aapke **Hamar AI CRM** ke liye ek professional Product Requirement Document (PRD). Yeh document aapke development process ko streamline karega.

---

# Product Requirement Document (PRD): Hamar AI Outreach CRM

## 1. Project Overview

**Hamar AI Outreach CRM** ek lightweight, web-based tool hai jo local businesses ke liye WhatsApp marketing automation aur lead follow-up ko manage karega. Iska main focus "One-by-one messaging" aur "Throttled distribution" hai.

## 2. Target Audience

* Local small businesses (Ali Mobiles, clothing stores, etc.).
* Hamar AI agency operations ke liye internal tool.

## 3. Core Features (MVP)

* **CSV Client Management:** CSV file upload karke client database (Name, WhatsApp Number) import karna.
* **Campaign Builder:**
* Text message drafting.
* Media support (Image, PDF, Video, Audio).


* **Smart Throttling:** Ek batch mein maximum 20 messages ka limit (Rate limiting for Evolution API protection).
* **Sequential Delivery:** One-by-one messaging logic with configurable time delay (e.g., 5-10 seconds between messages).
* **Status Tracking:** Message "Sent" ya "Failed" status ka log.

## 4. Technical Specifications

* **Backend:** Python (Flask)
* **Frontend:** HTML5, CSS (Bootstrap for responsive UI)
* **Database:** SQLite (Local storage)
* **Integration:** Evolution API (WhatsApp interaction)
* **Task Scheduling:** APScheduler (for background processing of message queues)

## 5. Functional Requirements

| ID | Feature | Description |
| --- | --- | --- |
| **FR-01** | CSV Upload | User .csv upload karega, system headers detect karke DB mein save karega. |
| **FR-02** | Campaign Editor | Form field for text, File input for media, "Send Campaign" button. |
| **FR-03** | Throttling | Queue system: Har 20 messages ke baad manual trigger ya auto-pause. |
| **FR-04** | Delay Logic | Do messages ke beech mein programmable delay (e.g., `time.sleep`). |
| **FR-05** | Media Handling | Uploaded media pehle local `/static/uploads` folder mein store hoga, phir API ko URL/base64 bheja jayega. |

## 6. Data Schema (Simplified)

* **`clients`**: `id`, `name`, `whatsapp_number`
* **`campaigns`**: `id`, `message_content`, `media_path`, `created_at`
* **`message_logs`**: `id`, `client_id`, `campaign_id`, `status`, `timestamp`

## 7. Success Metrics (KPIs)

* Successful CSV import time.
* Evolution API success rate (Sent messages count).
* UI responsiveness during bulk processing.

---

### Development Roadmap (Action Plan)

1. **Phase 1:** Setup Flask skeleton, Database models, and CSV upload logic.
2. **Phase 2:** Integrate Evolution API (sending test text/media).
3. **Phase 3:** Implement the queue engine (the 20-message loop).
4. **Phase 4:** Simple dashboard for viewing sent logs.

---

**Next Step Recommendation:**
Kya aap chahte hain ki main **Phase 1 (Flask setup aur Database Models)** ka code likhna shuru karoon, ya aapko is PRD mein kuch aur add karwana hai?