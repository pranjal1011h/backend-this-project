import os
import re
import json
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import pytesseract
from PIL import Image
import google.generativeai as genai

# ------------------------------
# CONFIGURATION
# ------------------------------
app = Flask(__name__)
CORS(app)  # Allow frontend to call this backend

# Database (SQLite, file-based)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///sevagrid.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['JWT_SECRET_KEY'] = 'your-secret-key-change-in-production'  # Change this!

# Upload folder for temp images
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Tesseract path (Windows only) – adjust if needed
if os.name == 'nt':  # Windows
    pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

# Gemini API setup
genai.configure(api_key=os.getenv('GEMINI_API_KEY', 'YOUR_GEMINI_API_KEY_HERE'))
gemini_model = genai.GenerativeModel('gemini-1.5-flash')

db = SQLAlchemy(app)
jwt = JWTManager(app)

# ------------------------------
# DATABASE MODELS
# ------------------------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    full_name = db.Column(db.String(100))
    role = db.Column(db.String(50), default='coordinator')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class SurveyReport(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200))
    location = db.Column(db.String(200))
    area = db.Column(db.String(100))
    lat = db.Column(db.Float)
    lng = db.Column(db.Float)
    category = db.Column(db.String(50))   # Health, Food, Education, etc.
    urgency = db.Column(db.String(20))    # High, Medium, Low
    people_affected = db.Column(db.Integer)
    required_skills = db.Column(db.String(200))  # comma separated
    report_details = db.Column(db.Text)
    status = db.Column(db.String(50), default='pending')  # pending, assigned, resolved
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Volunteer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True)
    skills = db.Column(db.String(200))      # e.g., "doctor, first-aid"
    location_lat = db.Column(db.Float)
    location_lng = db.Column(db.Float)
    availability = db.Column(db.String(50)) # Available, On Standby, Busy
    reliability_score = db.Column(db.Float, default=5.0)  # 0-10
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Match(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey('survey_report.id'))
    volunteer_id = db.Column(db.Integer, db.ForeignKey('volunteer.id'))
    match_score = db.Column(db.Float)
    status = db.Column(db.String(50), default='suggested')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# Create tables (run once)
with app.app_context():
    db.create_all()

# ------------------------------
# HELPER: GEMINI EXTRACTION
# ------------------------------
def extract_with_gemini(raw_text):
    prompt = f"""
    You are an NGO report analyzer. Extract the following fields from the text below.
    Return ONLY valid JSON with these keys: category, urgency, people_affected, required_skills.
    - category: choose from Health, Food, Education, Shelter, Water, Other
    - urgency: High, Medium, Low based on language (critical, urgent = High; moderate = Medium; low = Low)
    - people_affected: an integer number
    - required_skills: comma-separated list (e.g., "doctor, nurse, logistics")
    If a field is missing, use null.

    Text: {raw_text[:2000]}
    """
    try:
        response = gemini_model.generate_content(prompt)
        json_str = response.text.strip().replace('```json', '').replace('```', '')
        return json.loads(json_str)
    except:
        # Fallback regex extraction
        people_match = re.search(r'(\d+)\s*(people|affected|families)', raw_text, re.I)
        people = int(people_match.group(1)) if people_match else 0
        urgency = "Medium"
        if re.search(r'urgent|critical|immediate|red zone', raw_text, re.I):
            urgency = "High"
        elif re.search(r'low|minor', raw_text, re.I):
            urgency = "Low"
        return {
            "category": "Other",
            "urgency": urgency,
            "people_affected": people,
            "required_skills": ""
        }

# ------------------------------
# API ENDPOINTS
# ------------------------------
@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

# ---------- AUTH ----------
@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.json
    if User.query.filter_by(email=data['email']).first():
        return jsonify({'error': 'Email already exists'}), 400
    hashed = generate_password_hash(data['password'])
    user = User(email=data['email'], password_hash=hashed, full_name=data.get('full_name'))
    db.session.add(user)
    db.session.commit()
    token = create_access_token(identity=user.id)
    return jsonify({'token': token, 'user': {'id': user.id, 'email': user.email}})

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    user = User.query.filter_by(email=data['email']).first()
    if not user or not check_password_hash(user.password_hash, data['password']):
        return jsonify({'error': 'Invalid credentials'}), 401
    token = create_access_token(identity=user.id)
    return jsonify({'token': token, 'user': {'id': user.id, 'email': user.email}})

# ---------- SURVEY UPLOAD (Paper to Data) ----------
@app.route('/api/upload-survey', methods=['POST'])
@jwt_required()
def upload_survey():
    user_id = get_jwt_identity()
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    # Save image temporarily
    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)
    
    # OCR
    image = Image.open(filepath)
    raw_text = pytesseract.image_to_string(image)
    os.remove(filepath)  # cleanup
    
    # Use Gemini to extract structured data
    extracted = extract_with_gemini(raw_text)
    
    # Also allow manual overrides from form data (if frontend sends)
    title = request.form.get('title') or "Survey from image"
    location = request.form.get('location')
    area = request.form.get('area')
    lat = request.form.get('latitude', type=float)
    lng = request.form.get('longitude', type=float)
    details = request.form.get('report_details') or raw_text[:500]
    
    # Create report
    report = SurveyReport(
        title=title,
        location=location,
        area=area,
        lat=lat,
        lng=lng,
        category=extracted.get('category'),
        urgency=extracted.get('urgency'),
        people_affected=extracted.get('people_affected'),
        required_skills=extracted.get('required_skills'),
        report_details=details,
        created_by=user_id
    )
    db.session.add(report)
    db.session.commit()
    
    return jsonify({
        'message': 'Survey saved',
        'report_id': report.id,
        'extracted': extracted,
        'raw_text': raw_text[:500]
    })

# ---------- VOLUNTEER MANAGEMENT ----------
@app.route('/api/volunteers', methods=['POST'])
@jwt_required()
def add_volunteer():
    data = request.json
    volunteer = Volunteer(
        full_name=data['full_name'],
        email=data.get('email'),
        skills=data.get('skills'),
        location_lat=data.get('location_lat'),
        location_lng=data.get('location_lng'),
        availability=data.get('availability', 'Available'),
        reliability_score=data.get('reliability_score', 5.0),
        created_by=get_jwt_identity()
    )
    db.session.add(volunteer)
    db.session.commit()
    return jsonify({'message': 'Volunteer added', 'id': volunteer.id})

@app.route('/api/volunteers', methods=['GET'])
@jwt_required()
def get_volunteers():
    volunteers = Volunteer.query.all()
    return jsonify([{
        'id': v.id,
        'full_name': v.full_name,
        'skills': v.skills,
        'availability': v.availability,
        'location_lat': v.location_lat,
        'location_lng': v.location_lng,
        'reliability_score': v.reliability_score
    } for v in volunteers])

# ---------- SMART MATCHING ----------
def calculate_distance(lat1, lng1, lat2, lng2):
    # Simple Euclidean (for demo), use haversine in production
    if None in (lat1, lng1, lat2, lng2):
        return 999
    return ((lat1-lat2)**2 + (lng1-lng2)**2)**0.5 * 111  # approx km

@app.route('/api/match-volunteers/<int:report_id>', methods=['GET'])
@jwt_required()
def match_volunteers(report_id):
    report = SurveyReport.query.get_or_404(report_id)
    volunteers = Volunteer.query.filter_by(availability='Available').all()
    
    report_skills = set([s.strip().lower() for s in (report.required_skills or "").split(',') if s])
    matches = []
    for vol in volunteers:
        vol_skills = set([s.strip().lower() for s in (vol.skills or "").split(',') if s])
        # Skill match score (Jaccard)
        if report_skills:
            intersection = len(report_skills & vol_skills)
            union = len(report_skills | vol_skills)
            skill_score = intersection / union if union > 0 else 0
        else:
            skill_score = 0.5
        
        # Distance score (inverse, max 20km)
        dist_km = calculate_distance(report.lat, report.lng, vol.location_lat, vol.location_lng)
        distance_score = max(0, 1 - dist_km/20)
        
        # Reliability score (0-10 -> 0-1)
        reliability_score = vol.reliability_score / 10.0
        
        # Weighted final score
        total = (skill_score * 0.5) + (distance_score * 0.3) + (reliability_score * 0.2)
        
        matches.append({
            'volunteer_id': vol.id,
            'name': vol.full_name,
            'skills': vol.skills,
            'distance_km': round(dist_km, 1),
            'availability': vol.availability,
            'score': round(total * 100),
            'skill_match': round(skill_score * 100),
            'reliability': vol.reliability_score
        })
    
    # Sort by score descending
    matches.sort(key=lambda x: x['score'], reverse=True)
    
    # Store matches in database
    for m in matches[:5]:
        match = Match.query.filter_by(report_id=report_id, volunteer_id=m['volunteer_id']).first()
        if not match:
            match = Match(report_id=report_id, volunteer_id=m['volunteer_id'], match_score=m['score'])
            db.session.add(match)
    db.session.commit()
    
    return jsonify({'report_title': report.title, 'matches': matches[:5]})

# ---------- DASHBOARD STATS ----------
@app.route('/api/dashboard', methods=['GET'])
@jwt_required()
def dashboard():
    total_affected = db.session.query(db.func.sum(SurveyReport.people_affected)).scalar() or 0
    urgent_reports = SurveyReport.query.filter_by(urgency='High').count()
    available_volunteers = Volunteer.query.filter_by(availability='Available').count()
    needs_saved = SurveyReport.query.count()  # number of reports processed
    
    # Urgency heatmap data (list of reports with lat/lng and urgency)
    heatmap_data = []
    for r in SurveyReport.query.filter(SurveyReport.lat.isnot(None), SurveyReport.lng.isnot(None)).all():
        heatmap_data.append({
            'lat': r.lat, 'lng': r.lng,
            'urgency': r.urgency,
            'category': r.category,
            'people': r.people_affected
        })
    
    return jsonify({
        'total_affected': total_affected,
        'urgent_reports': urgent_reports,
        'available_volunteers': available_volunteers,
        'needs_saved': needs_saved,
        'heatmap_data': heatmap_data,
        'avg_urgency_min': 18  # placeholder, can be computed from created_at
    })

# ------------------------------
# RUN THE SERVER
# ------------------------------
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)