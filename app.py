from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import os
import json
import csv
import io
import threading
import time
import logging
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()
from werkzeug.utils import secure_filename
from database import init_db, get_db_connection, DbIntegrityError
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

@app.before_request
def log_incoming_request():
    try:
        # Only log webhook or POST requests to avoid bloat
        if request.path.startswith('/webhook') or request.method == 'POST':
            log_file = os.path.join(app.root_path, 'scratch', 'requests_log.txt')
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            
            # Read raw body safely
            body = request.get_data(as_text=True)
            
            with open(log_file, 'a', encoding='utf-8') as f:
                import datetime
                timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                f.write(f"[{timestamp}] {request.method} {request.path}\n")
                f.write(f"Headers: {dict(request.headers)}\n")
                f.write(f"Body: {body}\n")
                f.write("-" * 80 + "\n")
    except Exception as e:
        logger.error(f"Error in request logger: {e}")

# Settings File Path
SETTINGS_PATH = os.path.join(app.root_path, 'settings.json')

def load_settings():
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {"api_url": "", "api_key": "", "instance_name": "hamar_ai", "webhook_url": "", "openai_api_key": ""}

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
            # 1. Fetch current running campaign (short-lived connection)
            conn = get_db_connection()
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
            
            # Get up to batch_limit pending logs
            logs = conn.execute(
                "SELECT ml.*, c.whatsapp_number, c.name as client_name "
                "FROM message_logs ml "
                "LEFT JOIN clients c ON ml.client_id = c.id "
                "WHERE ml.campaign_id = ? AND ml.status = 'Pending' "
                "ORDER BY ml.id ASC LIMIT ?",
                (campaign_id, batch_limit)
            ).fetchall()
            
            # Convert rows to standard dictionaries so we can use them after closing the connection
            logs_dict = [dict(log) for log in logs]
            conn.close() # Close immediately to release SQLite locks!
            
            if not logs_dict:
                # No more pending logs left, complete the campaign
                conn = get_db_connection()
                conn.execute(
                    "UPDATE campaigns SET status = 'Completed', progress = 100 WHERE id = ?",
                    (campaign_id,)
                )
                conn.commit()
                conn.close()
                logger.info(f"Campaign #{campaign_id} has completed sending.")
                continue
                
            logger.info(f"Processing batch of {len(logs_dict)} messages for Campaign #{campaign_id}.")
            
            # Initialize API
            settings = load_settings()
            api = EvolutionAPI(
                base_url=settings.get('api_url'),
                api_key=settings.get('api_key'),
                instance_name=settings.get('instance_name')
            )
            
            for index, log in enumerate(logs_dict):
                # Verify that the campaign status hasn't changed to Paused/Cancelled
                conn = get_db_connection()
                campaign_status = conn.execute(
                    "SELECT status FROM campaigns WHERE id = ?",
                    (campaign_id,)
                ).fetchone()
                
                if not campaign_status or campaign_status['status'] != 'Running':
                    conn.close()
                    logger.info(f"Campaign #{campaign_id} status changed to {campaign_status['status'] if campaign_status else 'None'}. Halting batch.")
                    break
                    
                log_id = log['id']
                
                # Optimistic lock: Update status to 'Sending' immediately inside this brief connection
                # This guarantees that NO concurrent thread/process can duplicate-send this log!
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE message_logs SET status = 'Sending' WHERE id = ? AND status = 'Pending'",
                    (log_id,)
                )
                conn.commit()
                
                if cursor.rowcount == 0:
                    # Already grabbed by another thread/process!
                    conn.close()
                    logger.info(f"Log #{log_id} already being processed or not pending. Skipping.")
                    continue
                    
                conn.close() # Close connection during the slow network API call!
                
                recipient_number = log['whatsapp_number']
                recipient_name = log['client_name'] or 'Deleted Client'
                
                if not recipient_number:
                    # Client no longer exists in DB
                    conn = get_db_connection()
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
                    conn.close()
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
                conn = get_db_connection()
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
                conn.close()
                
                # Apply delay between messages (if not the last one in this list)
                if index < len(logs_dict) - 1:
                    logger.info(f"Waiting {delay} seconds delay before sending next message...")
                    time.sleep(delay)
            
            # Post-batch assessment
            conn = get_db_connection()
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
    
    # Check today's celebrations (Premium client birthdays / custom special events)
    import datetime
    today_date = datetime.date.today()
    current_month_day = today_date.strftime('%m-%d')
    
    premium_clients = conn.execute("SELECT * FROM clients WHERE is_premium = 1").fetchall()
    celebrations = []
    
    for client in premium_clients:
        client_dict = dict(client)
        is_event_today = False
        event_type = ""
        
        if client_dict.get('dob') and len(client_dict['dob']) >= 5:
            dob_extracted = client_dict['dob'][5:10] if len(client_dict['dob']) == 10 else client_dict['dob']
            if dob_extracted == current_month_day:
                is_event_today = True
                event_type = "Birthday 🎉"
                
        if client_dict.get('event_date') and len(client_dict['event_date']) >= 5:
            event_extracted = client_dict['event_date'][5:10] if len(client_dict['event_date']) == 10 else client_dict['event_date']
            if event_extracted == current_month_day:
                is_event_today = True
                event_type = client_dict.get('event_name') or "Special Event 🌟"
                
        if is_event_today:
            client_dict['event_type'] = event_type
            celebrations.append(client_dict)
            
    conn.close()
    return render_template('index.html', active_page='dashboard', metrics=metrics, campaigns=campaigns, recent_leads=recent_leads, celebrations=celebrations)


@app.route('/clients')
def clients_page():
    conn = get_db_connection()
    clients_rows = conn.execute("SELECT * FROM clients ORDER BY name ASC").fetchall()
    clients = [dict(row) for row in clients_rows]
    categories_rows = conn.execute("SELECT DISTINCT category FROM clients ORDER BY category ASC").fetchall()
    categories = [row['category'] for row in categories_rows if row['category']]
    conn.close()
    return render_template('clients.html', active_page='clients', clients=clients, categories=categories)


@app.route('/clients/upload', methods=['POST'])
def clients_upload():
    default_category = request.form.get('default_category', 'General').strip() or 'General'
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
            category_idx = -1
            
            name_keywords = ["name", "full name", "client", "customer", "recipient", "naam", "नाम"]
            phone_keywords = ["phone", "whatsapp", "number", "mobile", "contact", "wa", "मोबाइल", "नंबर"]
            category_keywords = ["category", "type", "tag", "group", "varg", "श्रेणी"]
            
            for i, col in enumerate(header):
                if any(kw in col for kw in name_keywords):
                    name_idx = i
                    break
            for i, col in enumerate(header):
                if any(kw in col for kw in phone_keywords):
                    phone_idx = i
                    break
            for i, col in enumerate(header):
                if any(kw in col for kw in category_keywords):
                    category_idx = i
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
                
                category = default_category
                if category_idx != -1 and len(row) > category_idx:
                    category = row[category_idx].strip() or default_category
                
                if name and phone:
                    normalized = normalize_phone(phone)
                    if normalized:
                        try:
                            conn.execute(
                                "INSERT INTO clients (name, whatsapp_number, category) VALUES (?, ?, ?)",
                                (name, normalized, category)
                            )
                            conn.commit()
                            success_count += 1
                        except DbIntegrityError:
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
    name = request.form.get('name', '').strip()
    phone = request.form.get('phone', '').strip()
    category = request.form.get('category', 'General').strip() or 'General'
    
    # Premium features
    is_premium = 1 if request.form.get('is_premium') == '1' else 0
    dob = request.form.get('dob', '').strip() or None
    event_name = request.form.get('event_name', '').strip() or None
    event_date = request.form.get('event_date', '').strip() or None
    
    # Payment details
    pending_amount_raw = request.form.get('pending_amount', '0').strip() or '0'
    pending_amount = float(pending_amount_raw)
    pending_reason = request.form.get('pending_reason', '').strip() or None
    
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
            "INSERT INTO clients (name, whatsapp_number, category, is_premium, dob, event_name, event_date, pending_amount, pending_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, normalized, category, is_premium, dob, event_name, event_date, pending_amount, pending_reason)
        )
        conn.commit()
        conn.close()
        flash(f"Client '{name}' added successfully under category '{category}'.", 'success')
    except DbIntegrityError:
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


@app.route('/payments')
def payments_page():
    conn = get_db_connection()
    # Fetch all clients who have outstanding balance > 0
    pending_clients_rows = conn.execute(
        "SELECT * FROM clients WHERE pending_amount > 0 ORDER BY pending_amount DESC"
    ).fetchall()
    pending_clients = [dict(row) for row in pending_clients_rows]
    
    # Fetch all clients to allow payment assignments
    all_clients_rows = conn.execute("SELECT id, name, whatsapp_number, pending_amount, pending_reason FROM clients ORDER BY name ASC").fetchall()
    all_clients = [dict(row) for row in all_clients_rows]
    conn.close()
    
    return render_template('payments.html', active_page='payments', pending_clients=pending_clients, all_clients=all_clients)


@app.route('/clients/<int:client_id>/update-payment', methods=['POST'])
def client_update_payment(client_id):
    try:
        pending_amount_raw = request.form.get('pending_amount', '0').strip() or '0'
        pending_amount = float(pending_amount_raw)
        pending_reason = request.form.get('pending_reason', '').strip() or None
        
        conn = get_db_connection()
        conn.execute(
            "UPDATE clients SET pending_amount = ?, pending_reason = ? WHERE id = ?",
            (pending_amount, pending_reason, client_id)
        )
        conn.commit()
        conn.close()
        flash('Payment status updated successfully.', 'success')
    except Exception as e:
        flash(f'Error updating payment status: {str(e)}', 'error')
        
    return redirect(request.referrer or url_for('payments_page'))


@app.route('/api/send-payment-reminder', methods=['POST'])
def api_send_payment_reminder():
    try:
        data = request.get_json(force=True, silent=True) or {}
        client_id = data.get("client_id")
        message = data.get("message", "").strip()
        
        if not client_id or not message:
            return jsonify({"status": "error", "message": "Client ID and message are required."}), 400
            
        conn = get_db_connection()
        client = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        if not client:
            conn.close()
            return jsonify({"status": "error", "message": "Client not found."}), 404
            
        settings = load_settings()
        api = EvolutionAPI(
            base_url=settings.get('api_url'),
            api_key=settings.get('api_key'),
            instance_name=settings.get('instance_name')
        )
        
        # Send message via Evolution API
        success, error_msg = api.send_message(
            phone_number=client['whatsapp_number'],
            text=message
        )
        conn.close()
        
        if success:
            return jsonify({"status": "success", "message": f"Payment reminder successfully sent to {client['name']}!"})
        else:
            return jsonify({"status": "error", "message": f"Failed to send reminder: {error_msg}"})
            
    except Exception as e:
        logger.error(f"Error in send-payment-reminder API: {e}")
        return jsonify({"status": "error", "message": f"System error: {str(e)}"}), 500


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
        target_category = request.form.get('target_category', 'All').strip()
        
        media_file = request.files.get('media')
        existing_media_path = request.form.get('existing_media_path')
        media_path = existing_media_path if existing_media_path else None
        
        if media_file and media_file.filename != '':
            filename = secure_filename(media_file.filename)
            file_dest = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            media_file.save(file_dest)
            media_path = f"/static/uploads/{filename}"

        conn = get_db_connection()
        
        # Fetch clients based on target category
        if target_category == 'All':
            clients = conn.execute("SELECT id FROM clients").fetchall()
        else:
            clients = conn.execute("SELECT id FROM clients WHERE category = ?", (target_category,)).fetchall()
        
        if not clients:
            conn.close()
            flash(f"Error: No clients found in category '{target_category}'. Add contacts first.", 'error')
            return redirect(url_for('campaign_new'))
            
        # Create Campaign
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO campaigns (name, message_content, media_path, delay, batch_limit, status, progress, target_category) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, message_content, media_path, delay, batch_limit, 'Running', 0, target_category)
        )
        campaign_id = cursor.lastrowid
        
        # Pre-seed message logs as Pending for targeted clients only
        for client in clients:
            conn.execute(
                "INSERT INTO message_logs (client_id, campaign_id, status) VALUES (?, ?, ?)",
                (client['id'], campaign_id, 'Pending')
            )
            
        conn.commit()
        conn.close()
        
        flash(f"Campaign '{name}' targeting '{target_category}' created and launched in background.", 'success')
        return redirect(url_for('campaign_detail', campaign_id=campaign_id))
        
    # GET Request
    clone_id = request.args.get('clone_from')
    cloned_campaign = None
    if clone_id:
        try:
            conn = get_db_connection()
            cloned_row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (clone_id,)).fetchone()
            if cloned_row:
                cloned_campaign = dict(cloned_row)
            conn.close()
        except Exception as e:
            logger.error(f"Error loading clone campaign: {e}")

    conn = get_db_connection()
    categories_rows = conn.execute("SELECT DISTINCT category FROM clients ORDER BY category ASC").fetchall()
    categories = [row['category'] for row in categories_rows if row['category']]
    total_clients = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    conn.close()
    
    return render_template('campaign_new.html', active_page='new_campaign', categories=categories, total_clients=total_clients, cloned_campaign=cloned_campaign)


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
        webhook_url = request.form.get('webhook_url', '').strip()
        openai_api_key = request.form.get('openai_api_key', '').strip()
        
        settings = {
            "api_url": api_url,
            "api_key": api_key,
            "instance_name": instance_name,
            "webhook_url": webhook_url,
            "openai_api_key": openai_api_key
        }
        
        if save_settings(settings):
            flash('Integration settings saved successfully.', 'success')
            
            # If webhook URL is provided, register it with the Evolution API
            if api_url and api_key and webhook_url:
                api = EvolutionAPI(base_url=api_url, api_key=api_key, instance_name=instance_name)
                success, error_msg = api.register_webhook(webhook_url)
                if success:
                    flash('Webhook registered successfully with Evolution API!', 'success')
                else:
                    flash(f'Settings saved, but webhook auto-registration failed: {error_msg}', 'warning')
        else:
            flash('Failed to save settings. Check permissions.', 'error')
            
        return redirect(url_for('settings_page'))
        
    settings = load_settings()
    return render_template('settings.html', settings=settings, active_page='settings')


@app.route('/clients/<int:client_id>/simulate-reply', methods=['POST'])
def client_simulate_reply(client_id):
    custom_msg = request.form.get('message', '').strip()
    if not custom_msg:
        import random
        custom_msg = random.choice([
            "Hello! I am interested in this property. Can you share the pricing sheet?",
            "Hi, is this house still available? I want to visit it this weekend.",
            "Can I get a call back regarding this listing?",
            "What is the total carpet area of the 3BHK flat?",
            "Is the price negotiable? What is the down payment?"
        ])
        
    conn = get_db_connection()
    client = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not client:
        conn.close()
        flash("Client not found.", "error")
        return redirect(url_for('clients_page'))
        
    # Check if lead already exists
    lead = conn.execute("SELECT id FROM hot_leads WHERE client_id = ?", (client_id,)).fetchone()
    if lead:
        conn.execute(
            "UPDATE hot_leads SET last_message = ?, replied_at = CURRENT_TIMESTAMP, status = 'New' WHERE client_id = ?",
            (custom_msg, client_id)
        )
    else:
        conn.execute(
            "INSERT INTO hot_leads (client_id, last_message, replied_at, status) VALUES (?, ?, CURRENT_TIMESTAMP, 'New')",
            (client_id, custom_msg)
        )
    conn.commit()
    conn.close()
    flash(f"Simulated WhatsApp message from '{client['name']}' added to Hot Leads!", "success")
    return redirect(url_for('hot_leads'))



# --- Webhook and Hot Leads Routes ---

@app.route('/webhook/evolution', methods=['POST'])
def evolution_webhook():
    try:
        # Use get_json(force=True) to robustly parse JSON webhooks even if headers are fuzzy
        data = request.get_json(force=True, silent=True)
        if not data:
            logger.warning("[WEBHOOK] Received empty or invalid JSON payload.")
            return jsonify({"status": "error", "message": "No JSON payload"}), 400
            
        event = data.get("event")
        if event in ["messages.upsert", "MESSAGES_UPSERT"]:
            message_data = data.get("data", {})
            key = message_data.get("key", {})
            
            # Robustly parse Boolean / String representation of fromMe
            from_me_raw = key.get("fromMe", True)
            from_me = from_me_raw in [True, "true", "True", 1, "1"]
            
            if not from_me:
                remote_jid = key.get("remoteJid", "")
                
                # Ignore Group Chat replies to avoid false lead triggers
                if "@g.us" in remote_jid:
                    logger.info("[WEBHOOK] Ignoring group chat reply in webhook.")
                    return jsonify({"status": "success", "message": "Ignored group chat"}), 200
                    
                phone = remote_jid.split("@")[0] if "@" in remote_jid else remote_jid
                phone_cleaned = "".join(filter(str.isdigit, phone))
                
                message_content = ""
                msg = message_data.get("message", {})
                if msg and isinstance(msg, dict):
                    message_content = msg.get("conversation", "")
                    if not message_content and "extendedTextMessage" in msg:
                        ext_msg = msg.get("extendedTextMessage", {})
                        if isinstance(ext_msg, dict):
                            message_content = ext_msg.get("text", "")
                    if not message_content and "imageMessage" in msg:
                        img_msg = msg.get("imageMessage", {})
                        if isinstance(img_msg, dict):
                            message_content = img_msg.get("caption", "")
                    if not message_content and "videoMessage" in msg:
                        vid_msg = msg.get("videoMessage", {})
                        if isinstance(vid_msg, dict):
                            message_content = vid_msg.get("caption", "")
                            
                # Fallback to direct text field inside message data
                if not message_content:
                    message_content = message_data.get("text", "")
                        
                if phone_cleaned and len(phone_cleaned) >= 10:
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
                        logger.info(f"[WEBHOOK LEAD] Successfully captured hot lead for {client['name']} ({phone_cleaned}) - Message: {message_content[:30]}")
                    else:
                        logger.info(f"[WEBHOOK] Received message from unknown contact ({phone_cleaned}). Skipping lead capture.")
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
    
    # Fetch all clients to support direct simulation select options
    clients_rows = conn.execute("SELECT id, name FROM clients ORDER BY name ASC").fetchall()
    clients = [dict(row) for row in clients_rows]
    
    conn.close()
    return render_template('hot_leads.html', active_page='hot_leads', leads=leads, clients=clients)


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


@app.route('/api/generate-campaign-message', methods=['POST'])
def generate_campaign_message():
    try:
        data = request.get_json(force=True, silent=True) or {}
        prompt_text = data.get("prompt", "").strip()
        
        if not prompt_text:
            return jsonify({"status": "error", "message": "Prompt is required."}), 400
            
        settings = load_settings()
        openai_api_key = settings.get("openai_api_key", "").strip() or os.getenv("OPENAI_API_KEY", "").strip()
        if not openai_api_key:
            return jsonify({
                "status": "error", 
                "message": "OpenAI API key not found. Please set it in System Settings or your .env file."
            }), 400
            
        # Using LangChain to generate the response
        from langchain_openai import ChatOpenAI
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_core.output_parsers import StrOutputParser
        
        # Instantiate ChatOpenAI
        chat = ChatOpenAI(
            openai_api_key=openai_api_key,
            model="gpt-3.5-turbo",
            temperature=0.7
        )
        
        # Create a professional prompt template for WhatsApp marketing copy
        system_prompt = (
            "You are a professional copywriting AI agent specialized in high-conversion, clean, and highly humanized WhatsApp marketing and outreach campaigns.\n"
            "Your goal is to generate a highly engaging, concise, and extremely natural WhatsApp message based on the user's instructions.\n\n"
            "CRITICAL RULES:\n"
            "1. Personalization: You MUST ALWAYS use the literal template placeholder '{{{{name}}}}' for the recipient's name. NEVER invent, hardcode, or write a specific name (like Suresh, Ramesh, Amit) in the text, even if the user mentions a specific name in their prompt! (e.g., if the user says 'write a birthday wish for Suresh', you must output 'Hi {{{{name}}}}, Happy Birthday!' and NOT 'Hi Suresh').\n"
            "2. Formatting: Absolutely DO NOT use asterisks (*) for bold text or any markdown characters. Under no circumstances should you output any '*' characters (e.g. do NOT write *Namaste* or *Happy Birthday!*). Keep the text completely plain, clean, and beautifully structured. No bold markdown elements at all. Emojis are welcome but use them tastefully.\n"
            "3. Tone: Ensure the tone is extremely warm, natural, humanized, and professional. Write exactly how a human business owner or a thoughtful friend would write. Avoid robotic, cheesy, or overly generic AI clichés (e.g., do NOT use phrases like 'Celebrate your special day with joy and laughter', 'Stay blessed and keep shining', 'dher saari badhaiyaan', etc. unless specifically requested). Keep it simple, genuine, and conversational.\n"
            "4. Clean Signatures: Do NOT add generic placeholders or sign-offs at the end like '[Your Name]', '[Your Company Name]', '[Phone Number]', or sign-offs like 'Warm Regards, [Your Name]'. The message should be complete, self-contained, and ready to send, without needing any bracketed placeholders at the end.\n"
            "5. Output ONLY the raw message content itself, ready to be sent. Do not include any subject lines, headers, or greeting metadata."
        )
        
        prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            ("user", "{user_input}")
        ])
        
        # Chain pipeline
        chain = prompt | chat | StrOutputParser()
        
        # Execute the chain
        generated_copy = chain.invoke({"user_input": prompt_text})
        
        return jsonify({
            "status": "success",
            "message": generated_copy.strip()
        })
        
    except Exception as e:
        logger.error(f"Error generating AI message: {e}")
        return jsonify({
            "status": "error",
            "message": f"AI Generation failed: {str(e)}"
        }), 500


@app.route('/api/send-quick-wish', methods=['POST'])
def api_send_quick_wish():
    try:
        data = request.get_json(force=True, silent=True) or {}
        client_id = data.get("client_id")
        message = data.get("message", "").strip()
        
        if not client_id or not message:
            return jsonify({"status": "error", "message": "Client ID and message are required."}), 400
            
        conn = get_db_connection()
        client = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        if not client:
            conn.close()
            return jsonify({"status": "error", "message": "Client not found."}), 404
            
        settings = load_settings()
        api = EvolutionAPI(
            base_url=settings.get('api_url'),
            api_key=settings.get('api_key'),
            instance_name=settings.get('instance_name')
        )
        
        # Send message via API
        success, error_msg = api.send_message(
            phone_number=client['whatsapp_number'],
            text=message
        )
        conn.close()
        
        if success:
            return jsonify({"status": "success", "message": f"Wish successfully sent to {client['name']}!"})
        else:
            return jsonify({"status": "error", "message": f"Failed to send wish: {error_msg}"})
            
    except Exception as e:
        logger.error(f"Error in send-quick-wish API: {e}")
        return jsonify({"status": "error", "message": f"System error: {str(e)}"}), 500


if __name__ == '__main__':
    import sqlite3
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
