from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import os
import json
import csv
import io
import threading
import time
import logging
from werkzeug.utils import secure_filename
from database import init_db, get_db_connection
from evolution_api import EvolutionAPI

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = "hamar_ai_secret_key"
UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload size

# Ensure upload directory exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Settings File Path
SETTINGS_PATH = os.path.join(app.root_path, 'settings.json')

def load_settings():
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {"api_url": "", "api_key": "", "instance_name": "hamar_ai"}

def save_settings(data):
    try:
        with open(SETTINGS_PATH, 'w') as f:
            json.dump(data, f, indent=4)
        return True
    except Exception as e:
        logger.error(f"Error saving settings: {e}")
        return False

def normalize_phone(phone_str):
    if not phone_str:
        return ""
    # strip non-digits
    digits = "".join(filter(str.isdigit, str(phone_str)))
    if len(digits) == 10:
        return "91" + digits
    elif len(digits) == 11 and digits.startswith("0"):
        return "91" + digits[1:]
    return digits

# Initialize DB on Startup
init_db()

def simulate_customer_reply(client_id, client_name):
    # Wait to simulate realistic delay
    time.sleep(5)
    try:
        conn = get_db_connection()
        client = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        if not client:
            conn.close()
            return
            
        import random
        replies = [
            "Hi, what is the down payment?",
            "Interested, please share the location map.",
            "Is this property still available?",
            "What is the exact price?",
            "Can I schedule a visit tomorrow?",
            "Are there any 3BHK options?",
            "Is the price negotiable?"
        ]
        chosen_reply = random.choice(replies)
        
        lead = conn.execute("SELECT id FROM hot_leads WHERE client_id = ?", (client_id,)).fetchone()
        if lead:
            conn.execute(
                "UPDATE hot_leads SET last_message = ?, replied_at = CURRENT_TIMESTAMP, status = 'New' WHERE client_id = ?",
                (chosen_reply, client_id)
            )
        else:
            conn.execute(
                "INSERT INTO hot_leads (client_id, last_message, replied_at, status) VALUES (?, ?, CURRENT_TIMESTAMP, 'New')",
                (client_id, chosen_reply)
            )
        conn.commit()
        conn.close()
        logger.info(f"[SIMULATED REPLY] Client {client_name} (ID: {client_id}) replied: {chosen_reply}")
    except Exception as e:
        logger.error(f"Error in simulated reply: {e}")

# --- Background Queue Worker Loop ---
def campaign_worker_loop():
    logger.info("Background campaign worker started.")
    while True:
        try:
            conn = get_db_connection()
            # Fetch the first campaign currently running
            campaign = conn.execute(
                "SELECT * FROM campaigns WHERE status = 'Running' ORDER BY id ASC LIMIT 1"
            ).fetchone()
            
            if not campaign:
                conn.close()
                time.sleep(2)
                continue
                
            campaign_id = campaign['id']
            batch_limit = campaign['batch_limit']
            delay = campaign['delay']
            media_path = campaign['media_path']
            message_template = campaign['message_content']
            
            # Initialize API
            settings = load_settings()
            api = EvolutionAPI(
                base_url=settings.get('api_url'),
                api_key=settings.get('api_key'),
                instance_name=settings.get('instance_name')
            )
            
            # Get up to batch_limit pending logs
            logs = conn.execute(
                "SELECT ml.*, c.whatsapp_number, c.name as client_name "
                "FROM message_logs ml "
                "LEFT JOIN clients c ON ml.client_id = c.id "
                "WHERE ml.campaign_id = ? AND ml.status = 'Pending' "
                "ORDER BY ml.id ASC LIMIT ?",
                (campaign_id, batch_limit)
            ).fetchall()
            
            if not logs:
                # No more pending logs left, complete the campaign
                conn.execute(
                    "UPDATE campaigns SET status = 'Completed', progress = 100 WHERE id = ?",
                    (campaign_id,)
                )
                conn.commit()
                conn.close()
                logger.info(f"Campaign #{campaign_id} has completed sending.")
                continue
                
            logger.info(f"Processing batch of {len(logs)} messages for Campaign #{campaign_id}.")
            
            sent_in_batch = 0
            for index, log in enumerate(logs):
                # Verify that the campaign status hasn't changed to Paused/Cancelled in the meantime
                campaign_status = conn.execute(
                    "SELECT status FROM campaigns WHERE id = ?",
                    (campaign_id,)
                ).fetchone()
                
                if not campaign_status or campaign_status['status'] != 'Running':
                    logger.info(f"Campaign #{campaign_id} status changed to {campaign_status['status'] if campaign_status else 'None'}. Halting batch.")
                    break
                    
                log_id = log['id']
                recipient_number = log['whatsapp_number']
                recipient_name = log['client_name'] or 'Deleted Client'
                
                if not recipient_number:
                    # Client no longer exists in DB
                    conn.execute(
                        "UPDATE message_logs SET status = 'Failed', error_message = 'Client deleted from database', timestamp = CURRENT_TIMESTAMP WHERE id = ?",
                        (log_id,)
                    )
                    conn.commit()
                    # Recalculate progress ratio
                    stats = conn.execute(
                        "SELECT "
                        "SUM(case when status in ('Sent', 'Failed') then 1 else 0 end) as processed, "
                        "COUNT(*) as total "
                        "FROM message_logs "
                        "WHERE campaign_id = ?",
                        (campaign_id,)
                    ).fetchone()
                    if stats and stats['total'] > 0:
                        progress = int((stats['processed'] / stats['total']) * 100)
                        conn.execute(
                            "UPDATE campaigns SET progress = ? WHERE id = ?",
                            (progress, campaign_id)
                        )
                        conn.commit()
                    continue
                
                # Replace {name} placeholder
                personalized_message = message_template.replace('{name}', recipient_name)

                
                # Send message via API
                success, error_msg = api.send_message(
                    phone_number=recipient_number,
                    text=personalized_message,
                    media_path=media_path
                )
                
                new_status = 'Sent' if success else 'Failed'
                
                # Update status log in database
                conn.execute(
                    "UPDATE message_logs SET status = ?, error_message = ?, timestamp = CURRENT_TIMESTAMP WHERE id = ?",
                    (new_status, error_msg, log_id)
                )
                conn.commit()

                # Trigger simulated customer reply if sent successfully
                if success:
                    import random
                    # If simulation mode is active or as a 25% demo chance
                    if not api.is_configured() or random.random() < 0.25:
                        reply_thread = threading.Thread(
                            target=simulate_customer_reply,
                            args=(log['client_id'], recipient_name),
                            daemon=True
                        )
                        reply_thread.start()
                
                sent_in_batch += 1
                
                # Recalculate progress ratio
                stats = conn.execute(
                    "SELECT "
                    "SUM(case when status in ('Sent', 'Failed') then 1 else 0 end) as processed, "
                    "COUNT(*) as total "
                    "FROM message_logs "
                    "WHERE campaign_id = ?",
                    (campaign_id,)
                ).fetchone()
                
                if stats and stats['total'] > 0:
                    progress = int((stats['processed'] / stats['total']) * 100)
                    conn.execute(
                        "UPDATE campaigns SET progress = ? WHERE id = ?",
                        (progress, campaign_id)
                    )
                    conn.commit()
                
                # Apply delay between messages (if not the last one in this list)
                if index < len(logs) - 1:
                    logger.info(f"Waiting {delay} seconds delay before sending next message...")
                    time.sleep(delay)
            
            # Post-batch assessment
            # Count remaining pending logs
            remaining_pending = conn.execute(
                "SELECT COUNT(*) as count FROM message_logs WHERE campaign_id = ? AND status = 'Pending'",
                (campaign_id,)
            ).fetchone()['count']
            
            # Recheck status
            final_status = conn.execute(
                "SELECT status FROM campaigns WHERE id = ?",
                (campaign_id,)
            ).fetchone()
            
            if final_status and final_status['status'] == 'Running':
                if remaining_pending > 0:
                    # Batch limit reached, auto-pause
                    conn.execute(
                        "UPDATE campaigns SET status = 'Paused' WHERE id = ?",
                        (campaign_id,)
                    )
                    conn.commit()
                    logger.info(f"Campaign #{campaign_id} paused automatically (Throttling Batch Limit of {batch_limit} reached).")
                else:
                    # Completed campaign fully
                    conn.execute(
                        "UPDATE campaigns SET status = 'Completed', progress = 100 WHERE id = ?",
                        (campaign_id,)
                    )
                    conn.commit()
                    logger.info(f"Campaign #{campaign_id} completed successfully.")
            
            conn.close()
            time.sleep(1)
            
        except Exception as e:
            logger.error(f"Error in background campaign thread: {e}")
            time.sleep(5)

# Spawn worker thread as daemon (guard against double execution in debug mode)
if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
    worker_thread = threading.Thread(target=campaign_worker_loop, daemon=True)
    worker_thread.start()



# --- HTTP Routes ---

@app.route('/')
def dashboard():
    conn = get_db_connection()
    
    # Calculate Dashboard metrics
    total_clients = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    total_campaigns = conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
    
    sent_logs = conn.execute("SELECT COUNT(*) FROM message_logs WHERE status = 'Sent'").fetchone()[0]
    failed_logs = conn.execute("SELECT COUNT(*) FROM message_logs WHERE status = 'Failed'").fetchone()[0]
    
    # Hot leads count
    total_hot_leads = conn.execute("SELECT COUNT(*) FROM hot_leads").fetchone()[0]
    
    total_logs = sent_logs + failed_logs
    success_rate = 100
    if total_logs > 0:
        success_rate = round((sent_logs / total_logs) * 100, 1)
        
    metrics = {
        "total_clients": total_clients,
        "total_campaigns": total_campaigns,
        "total_sent": sent_logs,
        "success_rate": success_rate,
        "total_hot_leads": total_hot_leads
    }
    
    # Get campaigns
    campaigns_rows = conn.execute("SELECT * FROM campaigns ORDER BY id DESC LIMIT 5").fetchall()
    campaigns = [dict(row) for row in campaigns_rows]
    
    # Get recent hot leads
    recent_leads_rows = conn.execute('''
        SELECT hl.*, c.name, c.whatsapp_number
        FROM hot_leads hl
        JOIN clients c ON hl.client_id = c.id
        ORDER BY hl.replied_at DESC
        LIMIT 5
    ''').fetchall()
    recent_leads = [dict(row) for row in recent_leads_rows]
    
    conn.close()
    return render_template('index.html', active_page='dashboard', metrics=metrics, campaigns=campaigns, recent_leads=recent_leads)


@app.route('/clients')
def clients_page():
    conn = get_db_connection()
    clients = conn.execute("SELECT * FROM clients ORDER BY name ASC").fetchall()
    conn.close()
    return render_template('clients.html', active_page='clients', clients=clients)


@app.route('/clients/upload', methods=['POST'])
def clients_upload():
    if 'file' not in request.files:
        flash('No file part selected', 'error')
        return redirect(url_for('clients_page'))
        
    file = request.files['file']
    if file.filename == '':
        flash('No selected file', 'error')
        return redirect(url_for('clients_page'))
        
    if file and file.filename.endswith('.csv'):
        try:
            # Parse CSV
            stream = io.StringIO(file.stream.read().decode("utf-8-sig"))
            reader = csv.reader(stream)
            rows = list(reader)
            
            if not rows:
                flash('The CSV file is empty', 'error')
                return redirect(url_for('clients_page'))
                
            header = [col.strip().lower() for col in rows[0]]
            name_idx = -1
            phone_idx = -1
            
            name_keywords = ["name", "full name", "client", "customer", "recipient", "naam", "नाम"]
            phone_keywords = ["phone", "whatsapp", "number", "mobile", "contact", "wa", "मोबाइल", "नंबर"]
            
            for i, col in enumerate(header):
                if any(kw in col for kw in name_keywords):
                    name_idx = i
                    break
            for i, col in enumerate(header):
                if any(kw in col for kw in phone_keywords):
                    phone_idx = i
                    break
                    
            has_header = True
            if name_idx == -1 or phone_idx == -1:
                name_idx = 0
                phone_idx = 1 if len(header) > 1 else 0
                has_header = False
                
            start_row = 1 if has_header else 0
            
            conn = get_db_connection()
            success_count = 0
            duplicate_count = 0
            
            for row in rows[start_row:]:
                if not row or len(row) <= max(name_idx, phone_idx):
                    continue
                name = row[name_idx].strip()
                phone = row[phone_idx].strip()
                
                if name and phone:
                    normalized = normalize_phone(phone)
                    if normalized:
                        try:
                            conn.execute(
                                "INSERT INTO clients (name, whatsapp_number) VALUES (?, ?)",
                                (name, normalized)
                            )
                            conn.commit()
                            success_count += 1
                        except sqlite3.IntegrityError:
                            # Already exists (duplicate phone)
                            duplicate_count += 1
                            
            conn.close()
            
            flash_msg = f"Import complete: {success_count} clients added."
            if duplicate_count > 0:
                flash_msg += f" ({duplicate_count} duplicates skipped)"
            flash(flash_msg, 'success')
            
        except Exception as e:
            flash(f"Error parsing CSV file: {str(e)}", 'error')
            
    else:
        flash('Invalid file format. Please upload a .csv file', 'error')
        
    return redirect(url_for('clients_page'))


@app.route('/clients/add', methods=['POST'])
def client_add():
    import sqlite3
    name = request.form.get('name', '').strip()
    phone = request.form.get('phone', '').strip()
    
    if not name or not phone:
        flash('Both Name and Phone Number are required.', 'error')
        return redirect(url_for('clients_page'))
        
    normalized = normalize_phone(phone)
    if not normalized:
        flash('Invalid phone number format.', 'error')
        return redirect(url_for('clients_page'))
        
    try:
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO clients (name, whatsapp_number) VALUES (?, ?)",
            (name, normalized)
        )
        conn.commit()
        conn.close()
        flash(f"Client '{name}' added successfully.", 'success')
    except sqlite3.IntegrityError:
        flash(f"Error: Phone number '{normalized}' already exists.", 'error')
    except Exception as e:
        flash(f"Database error: {str(e)}", 'error')
        
    return redirect(url_for('clients_page'))



@app.route('/clients/delete/<int:client_id>', methods=['POST'])
def client_delete(client_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))
    conn.commit()
    conn.close()
    flash('Client removed successfully.', 'success')
    return redirect(url_for('clients_page'))


@app.route('/clients/clear', methods=['POST'])
def clients_clear():
    conn = get_db_connection()
    conn.execute("DELETE FROM clients")
    conn.execute("DELETE FROM message_logs")
    conn.commit()
    conn.close()
    flash('All client database records and sending logs have been cleared.', 'success')
    return redirect(url_for('clients_page'))


@app.route('/campaigns')
def campaigns_list():
    conn = get_db_connection()
    campaigns_rows = conn.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall()
    campaigns = [dict(row) for row in campaigns_rows]
    conn.close()
    return render_template('campaigns_list.html', active_page='campaigns', campaigns=campaigns)


@app.route('/campaigns/new', methods=['GET', 'POST'])
def campaign_new():
    if request.method == 'POST':
        name = request.form.get('name')
        message_content = request.form.get('message_content')
        delay = int(request.form.get('delay', 5))
        batch_limit = int(request.form.get('batch_limit', 20))
        
        media_file = request.files.get('media')
        media_path = None
        
        if media_file and media_file.filename != '':
            filename = secure_filename(media_file.filename)
            file_dest = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            media_file.save(file_dest)
            media_path = f"/static/uploads/{filename}"

        conn = get_db_connection()
        # Ensure we have clients to send to
        clients = conn.execute("SELECT id FROM clients").fetchall()
        
        if not clients:
            conn.close()
            flash('Error: Your client database is empty. Import clients first before sending campaigns.', 'error')
            return redirect(url_for('campaign_new'))
            
        # Create Campaign
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO campaigns (name, message_content, media_path, delay, batch_limit, status, progress) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, message_content, media_path, delay, batch_limit, 'Running', 0)
        )
        campaign_id = cursor.lastrowid
        
        # Pre-seed message logs as Pending for all imported clients
        for client in clients:
            conn.execute(
                "INSERT INTO message_logs (client_id, campaign_id, status) VALUES (?, ?, ?)",
                (client['id'], campaign_id, 'Pending')
            )
            
        conn.commit()
        conn.close()
        
        flash(f"Campaign '{name}' created and launched in background.", 'success')
        return redirect(url_for('campaign_detail', campaign_id=campaign_id))
        
    return render_template('campaign_new.html', active_page='new_campaign')


@app.route('/campaigns/<int:campaign_id>')
def campaign_detail(campaign_id):
    conn = get_db_connection()
    campaign_row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    
    if not campaign_row:
        conn.close()
        flash('Campaign not found', 'error')
        return redirect(url_for('dashboard'))
        
    campaign = dict(campaign_row)
    
    # Calculate statistics
    sent = conn.execute("SELECT COUNT(*) FROM message_logs WHERE campaign_id = ? AND status = 'Sent'", (campaign_id,)).fetchone()[0]
    failed = conn.execute("SELECT COUNT(*) FROM message_logs WHERE campaign_id = ? AND status = 'Failed'", (campaign_id,)).fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM message_logs WHERE campaign_id = ? AND status = 'Pending'", (campaign_id,)).fetchone()[0]
    
    counts = {"sent": sent, "failed": failed, "pending": pending}
    
    # Fetch logs
    logs_rows = conn.execute(
        "SELECT ml.*, c.name as client_name, c.whatsapp_number as client_number "
        "FROM message_logs ml "
        "LEFT JOIN clients c ON ml.client_id = c.id "
        "WHERE ml.campaign_id = ? "
        "ORDER BY ml.id ASC",
        (campaign_id,)
    ).fetchall()
    
    logs = [dict(row) for row in logs_rows]
    conn.close()

    
    return render_template('campaign_detail.html', campaign=campaign, counts=counts, logs=logs, active_page='campaigns')


@app.route('/campaigns/<int:campaign_id>/action/<action>', methods=['POST'])
def campaign_action(campaign_id, action):
    conn = get_db_connection()
    campaign = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    
    if not campaign:
        conn.close()
        flash('Campaign not found', 'error')
        return redirect(url_for('dashboard'))
        
    new_status = campaign['status']
    if action == 'start':
        new_status = 'Running'
        flash('Campaign resumed/started sending.', 'success')
    elif action == 'pause':
        new_status = 'Paused'
        flash('Campaign paused.', 'warning')
    elif action == 'cancel':
        new_status = 'Cancelled'
        # Set all remaining Pending logs to Failed / Cancelled
        conn.execute(
            "UPDATE message_logs SET status = 'Failed', error_message = 'Cancelled by user' "
            "WHERE campaign_id = ? AND status = 'Pending'",
            (campaign_id,)
        )
        flash('Campaign stopped and remaining queue cancelled.', 'danger')
        
    conn.execute("UPDATE campaigns SET status = ? WHERE id = ?", (new_status, campaign_id))
    conn.commit()
    conn.close()
    
    return redirect(url_for('campaign_detail', campaign_id=campaign_id))


@app.route('/campaigns/<int:campaign_id>/delete', methods=['POST'])
def campaign_delete(campaign_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
    conn.execute("DELETE FROM message_logs WHERE campaign_id = ?", (campaign_id,))
    conn.commit()
    conn.close()
    flash('Campaign record deleted permanently.', 'success')
    return redirect(url_for('campaigns_list'))


@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    if request.method == 'POST':
        api_url = request.form.get('api_url', '').strip()
        api_key = request.form.get('api_key', '').strip()
        instance_name = request.form.get('instance_name', 'hamar_ai').strip()
        
        settings = {
            "api_url": api_url,
            "api_key": api_key,
            "instance_name": instance_name
        }
        
        if save_settings(settings):
            flash('Integration settings saved successfully.', 'success')
        else:
            flash('Failed to save settings. Check permissions.', 'error')
            
        return redirect(url_for('settings_page'))
        
    settings = load_settings()
    return render_template('settings.html', settings=settings, active_page='settings')


# --- Webhook and Hot Leads Routes ---

@app.route('/webhook/evolution', methods=['POST'])
def evolution_webhook():
    try:
        data = request.json
        if not data:
            return jsonify({"status": "error", "message": "No JSON payload"}), 400
            
        event = data.get("event")
        if event in ["messages.upsert", "MESSAGES_UPSERT"]:
            message_data = data.get("data", {})
            key = message_data.get("key", {})
            from_me = key.get("fromMe", True)
            
            if not from_me:
                remote_jid = key.get("remoteJid", "")
                phone = remote_jid.split("@")[0] if "@" in remote_jid else remote_jid
                phone_cleaned = "".join(filter(str.isdigit, phone))
                
                message_content = ""
                msg = message_data.get("message", {})
                if msg:
                    message_content = msg.get("conversation", "")
                    if not message_content and "extendedTextMessage" in msg:
                        message_content = msg.get("extendedTextMessage", {}).get("text", "")
                    if not message_content and "imageMessage" in msg:
                        message_content = msg.get("imageMessage", {}).get("caption", "")
                    if not message_content and "videoMessage" in msg:
                        message_content = msg.get("videoMessage", {}).get("caption", "")
                        
                if phone_cleaned:
                    conn = get_db_connection()
                    client = conn.execute(
                        "SELECT * FROM clients WHERE whatsapp_number LIKE ?", 
                        (f"%{phone_cleaned[-10:]}",)
                    ).fetchone()
                    
                    if client:
                        client_id = client['id']
                        lead = conn.execute("SELECT id FROM hot_leads WHERE client_id = ?", (client_id,)).fetchone()
                        if lead:
                            conn.execute(
                                "UPDATE hot_leads SET last_message = ?, replied_at = CURRENT_TIMESTAMP, status = 'New' WHERE client_id = ?",
                                (message_content or "Media/Other Message", client_id)
                            )
                        else:
                            conn.execute(
                                "INSERT INTO hot_leads (client_id, last_message, replied_at, status) VALUES (?, ?, CURRENT_TIMESTAMP, 'New')",
                                (client_id, message_content or "Media/Other Message")
                            )
                        conn.commit()
                        logger.info(f"[WEBHOOK LEAD] Lead added/updated for client {client['name']} ({phone_cleaned})")
                    conn.close()
                    
        return jsonify({"status": "success"}), 200
    except Exception as e:
        logger.error(f"Error in webhook: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/hot-leads')
def hot_leads():
    conn = get_db_connection()
    leads_rows = conn.execute('''
        SELECT hl.*, c.name, c.whatsapp_number
        FROM hot_leads hl
        JOIN clients c ON hl.client_id = c.id
        ORDER BY hl.replied_at DESC
    ''').fetchall()
    leads = [dict(row) for row in leads_rows]
    conn.close()
    return render_template('hot_leads.html', active_page='hot_leads', leads=leads)


@app.route('/hot-leads/<int:lead_id>/status', methods=['POST'])
def hot_lead_status(lead_id):
    status = request.form.get('status', 'New')
    conn = get_db_connection()
    conn.execute("UPDATE hot_leads SET status = ? WHERE id = ?", (status, lead_id))
    conn.commit()
    conn.close()
    flash('Lead status updated successfully.', 'success')
    return redirect(url_for('hot_leads'))


@app.route('/hot-leads/<int:lead_id>/delete', methods=['POST'])
def hot_lead_delete(lead_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM hot_leads WHERE id = ?", (lead_id,))
    conn.commit()
    conn.close()
    flash('Lead removed from Hot Leads.', 'success')
    return redirect(url_for('hot_leads'))


if __name__ == '__main__':
    import sqlite3
    app.run(debug=True, host='127.0.0.1', port=5000)
