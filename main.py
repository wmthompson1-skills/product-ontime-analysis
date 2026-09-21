import os
from pathlib import Path
from dotenv import load_dotenv

def _load_anchored_env():
    """Walk up from repo root looking for a .env file (up to 3 levels).
    Loads the first one found. Silently skips if none exists (e.g. Replit)."""
    anchor = Path(__file__).resolve().parent
    for _ in range(3):
        candidate = anchor / '.env'
        if candidate.exists():
            load_dotenv(candidate, override=False)
            return candidate
        anchor = anchor.parent
    return None

_load_anchored_env()

import requests as http_requests
from flask import Flask, request, jsonify, send_from_directory, render_template, send_file, Response
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import DeclarativeBase
from io import BytesIO
from config import SQLALCHEMY_DATABASE_URI


class Base(DeclarativeBase):
    pass


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or "a secret key"

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE')
    return response

app.config["SQLALCHEMY_DATABASE_URI"] = SQLALCHEMY_DATABASE_URI
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "connect_args": {"check_same_thread": False},
}

db = SQLAlchemy(model_class=Base)
db.init_app(app)

# Import and initialize models
from models import create_user_model
from app.contextual_hints import get_contextual_hints, expand_acronym
from app.excel_cleansing import cleanse_uploaded_excel

User = create_user_model(db)

# Create tables
with app.app_context():
    db.create_all()


@app.route('/')
def hello():
    from flask import redirect
    return redirect('/gradio/', code=302)

@app.route('/api/test')
def api_test():
    """
    Simple API test endpoint for connectivity testing.
    """
    from datetime import datetime
    return jsonify({
        'status': 'connected',
        'message': 'Flask backend is running successfully',
        'timestamp': datetime.now().isoformat(),
        'server': 'Flask/Python'
    })


@app.route('/health')
@app.route('/api/health')
def health_check():
    """Health check endpoint for API testing"""
    return jsonify({
        'status': 'healthy',
        'message': 'Flask API is running successfully!',
        'version': '1.0',
        'timestamp': '2025-07-28T02:25:00Z'
    })


@app.route('/api/users', methods=['GET'])
def get_users():
    """Get all users from database"""
    users = User.query.all()
    return jsonify([{
        'id': user.id,
        'name': user.name,
        'email': user.email
    } for user in users])


@app.route('/api/users', methods=['POST'])
def create_user():
    """Create a new user"""
    data = request.get_json()

    if not data or 'name' not in data or 'email' not in data:
        return jsonify({'error': 'Name and email are required'}), 400

    user = User(name=data['name'], email=data['email'])
    db.session.add(user)
    db.session.commit()

    return jsonify({
        'id': user.id,
        'name': user.name,
        'email': user.email
    }), 201


@app.route('/api/users/<int:user_id>', methods=['GET'])
def get_user(user_id):
    """Get a specific user"""
    user = User.query.get_or_404(user_id)
    return jsonify({'id': user.id, 'name': user.name, 'email': user.email})


@app.route('/api/users/<int:user_id>', methods=['PUT'])
def update_user(user_id):
    """Update a user"""
    user = User.query.get_or_404(user_id)
    data = request.get_json()

    if 'name' in data:
        user.name = data['name']
    if 'email' in data:
        user.email = data['email']

    db.session.commit()

    return jsonify({'id': user.id, 'name': user.name, 'email': user.email})


@app.route('/api/users/<int:user_id>', methods=['DELETE'])
def delete_user(user_id):
    """Delete a user"""
    user = User.query.get_or_404(user_id)
    db.session.delete(user)
    db.session.commit()

    return jsonify({'message': 'User deleted successfully'}), 200


@app.route('/my_model/<filename>', methods=['GET', 'OPTIONS'])
def serve_model_files(filename):
    """Serve Teachable Machine model files"""
    if request.method == 'OPTIONS':
        # Handle CORS preflight
        response = app.make_default_options_response()
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response

    try:
        from flask import Response
        import mimetypes

        # Set proper content type based on file extension
        if filename.endswith('.json'):
            mimetype = 'application/json'
        elif filename.endswith('.bin'):
            mimetype = 'application/octet-stream'
        else:
            mimetype = mimetypes.guess_type(filename)[0] or 'application/octet-stream'

        response = send_from_directory('my_model', filename, mimetype=mimetype)

        # Add CORS headers to allow cross-origin requests
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'

        return response
    except FileNotFoundError:
        return jsonify({'error': 'Model file not found'}), 404





@app.route('/audio-classifier', methods=['GET'])
def audio_classifier():
    """Audio classification page using Teachable Machine model"""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Audio Classification Demo</title>
        <style>
            body { 
                font-family: Arial, sans-serif; 
                margin: 40px; 
                background-color: #f5f5f5; 
            }
            .container { 
                max-width: 800px; 
                margin: 0 auto; 
                background: white; 
                padding: 30px; 
                border-radius: 10px; 
                box-shadow: 0 2px 10px rgba(0,0,0,0.1); 
            }
            button { 
                padding: 15px 30px; 
                margin: 10px; 
                background: #007bff; 
                color: white; 
                border: none; 
                cursor: pointer; 
                border-radius: 5px; 
                font-size: 16px; 
            }
            button:hover { background: #0056b3; }
            button:disabled { background: #cccccc; cursor: not-allowed; }
            .status { 
                padding: 10px; 
                margin: 10px 0; 
                border-radius: 5px; 
                font-weight: bold; 
            }
            .listening { background-color: #d4edda; color: #155724; }
            .stopped { background-color: #f8d7da; color: #721c24; }
            .prediction { 
                margin: 10px 0; 
                padding: 15px; 
                background: #f8f9fa; 
                border-left: 4px solid #007bff; 
                border-radius: 4px; 
            }
            .prediction-item { 
                margin: 5px 0; 
                padding: 8px; 
                background: white; 
                border-radius: 4px; 
                border: 1px solid #e9ecef; 
            }
            .high-confidence { background-color: #d1ecf1; }
            .prediction-log { 
                max-height: 300px; 
                overflow-y: auto; 
                border: 1px solid #ddd; 
                padding: 10px; 
                margin-top: 20px; 
                background: #f8f9fa; 
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🎤 Audio Classification Demo</h1>
            <p>Click "Start Listening" to begin audio classification using your trained Teachable Machine model.</p>

            <div id="status" class="status stopped">Status: Not listening</div>

            <button id="startBtn" onclick="startListening()">Start Listening</button>
            <button id="stopBtn" onclick="stopListening()" disabled>Stop Listening</button>

            <div id="predictions" class="prediction">
                <h3>Current Predictions:</h3>
                <div id="label-container">Click "Start Listening" to see predictions...</div>
            </div>

            <div class="prediction-log">
                <h3>Prediction Log:</h3>
                <div id="log-container"></div>
            </div>
        </div>

        <script src="https://cdn.jsdelivr.net/npm/@tensorflow/tfjs@1.3.1/dist/tf.min.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/@tensorflow-models/speech-commands@0.4.0/dist/speech-commands.min.js"></script>

        <script type="text/javascript">
            // Construct absolute URLs for the model files
            const baseURL = window.location.origin;
            const URL = baseURL + "/my_model/";
            let recognizer = null;
            let isListening = false;

            async function createModel() {
                try {
                    console.log("Starting model creation...");
                    const checkpointURL = URL + "model.json";
                    const metadataURL = URL + "metadata.json";

                    console.log("Model URLs:", { checkpointURL, metadataURL });

                    // Test if files are accessible
                    const modelResponse = await fetch(checkpointURL);
                    const metadataResponse = await fetch(metadataURL);

                    console.log("Model fetch status:", modelResponse.status);
                    console.log("Metadata fetch status:", metadataResponse.status);

                    if (!modelResponse.ok) {
                        throw new Error(`Failed to fetch model.json: ${modelResponse.status}`);
                    }
                    if (!metadataResponse.ok) {
                        throw new Error(`Failed to fetch metadata.json: ${metadataResponse.status}`);
                    }

                    recognizer = speechCommands.create(
                        "BROWSER_FFT",
                        undefined,
                        checkpointURL,
                        metadataURL
                    );

                    console.log("Created recognizer, loading model...");
                    await recognizer.ensureModelLoaded();
                    console.log("Model loaded successfully");
                    return recognizer;
                } catch (error) {
                    console.error("Detailed error loading model:", error);
                    console.error("Error stack:", error.stack);
                    document.getElementById('status').innerHTML = 
                        `<span style="color: red;">Error: ${error.message}</span>`;
                    return null;
                }
            }

            async function startListening() {
                if (!recognizer) {
                    document.getElementById('status').textContent = "Loading model...";
                    recognizer = await createModel();
                    if (!recognizer) return;
                }

                const classLabels = recognizer.wordLabels();
                const labelContainer = document.getElementById("label-container");
                labelContainer.innerHTML = '';

                for (let i = 0; i < classLabels.length; i++) {
                    const div = document.createElement("div");
                    div.className = "prediction-item";
                    div.id = `prediction-${i}`;
                    labelContainer.appendChild(div);
                }

                recognizer.listen(result => {
                    const scores = result.scores;
                    const timestamp = new Date().toLocaleTimeString();
                    let maxScore = 0;
                    let maxIndex = 0;

                    // Update current predictions
                    for (let i = 0; i < classLabels.length; i++) {
                        const confidence = (scores[i] * 100).toFixed(1);
                        const predictionDiv = document.getElementById(`prediction-${i}`);
                        predictionDiv.innerHTML = `${classLabels[i]}: ${confidence}%`;

                        // Highlight high confidence predictions
                        if (scores[i] > 0.7) {
                            predictionDiv.classList.add('high-confidence');
                        } else {
                            predictionDiv.classList.remove('high-confidence');
                        }

                        if (scores[i] > maxScore) {
                            maxScore = scores[i];
                            maxIndex = i;
                        }
                    }

                    // Log prediction if confidence is high
                    if (maxScore > 0.75) {
                        logPrediction(classLabels[maxIndex], maxScore, timestamp);

                        // Send to Flask API
                        sendPredictionToAPI({
                            label: classLabels[maxIndex],
                            confidence: maxScore,
                            timestamp: timestamp,
                            all_scores: scores.map((score, idx) => ({
                                label: classLabels[idx],
                                confidence: score
                            }))
                        });
                    }
                }, {
                    includeSpectrogram: true,
                    probabilityThreshold: 0.75,
                    invokeCallbackOnNoiseAndUnknown: true,
                    overlapFactor: 0.50
                });

                isListening = true;
                document.getElementById('startBtn').disabled = true;
                document.getElementById('stopBtn').disabled = false;
                document.getElementById('status').className = 'status listening';
                document.getElementById('status').textContent = 'Status: Listening for audio...';
            }

            function stopListening() {
                if (recognizer && isListening) {
                    recognizer.stopListening();
                    isListening = false;
                    document.getElementById('startBtn').disabled = false;
                    document.getElementById('stopBtn').disabled = true;
                    document.getElementById('status').className = 'status stopped';
                    document.getElementById('status').textContent = 'Status: Stopped listening';
                }
            }

            function logPrediction(label, confidence, timestamp) {
                const logContainer = document.getElementById('log-container');
                const logEntry = document.createElement('div');
                logEntry.innerHTML = `
                    <strong>${timestamp}</strong>: Detected "${label}" 
                    (${(confidence * 100).toFixed(1)}% confidence)
                `;
                logEntry.style.margin = '5px 0';
                logEntry.style.padding = '5px';
                logEntry.style.backgroundColor = 'white';
                logEntry.style.borderRadius = '3px';

                logContainer.insertBefore(logEntry, logContainer.firstChild);

                // Keep only last 20 entries
                while (logContainer.children.length > 20) {
                    logContainer.removeChild(logContainer.lastChild);
                }
            }

            async function sendPredictionToAPI(prediction) {
                try {
                    const response = await fetch('/api/audio-prediction', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                        },
                        body: JSON.stringify(prediction)
                    });

                    if (!response.ok) {
                        console.warn('Failed to log prediction to API');
                    }
                } catch (error) {
                    console.warn('Error sending prediction to API:', error);
                }
            }

            // Initialize on page load
            window.addEventListener('load', () => {
                console.log('Audio classification page loaded');
            });
        </script>
    </body>
    </html>
    """
    return html


@app.route('/api/model-debug', methods=['GET'])
def debug_model_files():
    """Debug endpoint to check model files"""
    import os
    model_dir = 'my_model'

    try:
        files = os.listdir(model_dir)
        file_info = {}

        for file in files:
            file_path = os.path.join(model_dir, file)
            file_info[file] = {
                'exists': os.path.exists(file_path),
                'size': os.path.getsize(file_path) if os.path.exists(file_path) else 0,
                'url': f'/my_model/{file}'
            }

        return jsonify({
            'model_directory': model_dir,
            'files': file_info,
            'expected_files': ['model.json', 'metadata.json', 'weights.bin']
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/audio-prediction', methods=['POST'])
def log_audio_prediction():
    """API endpoint to log audio predictions"""
    try:
        data = request.get_json()

        # Log prediction (you could save to database here)
        print(f"Audio Prediction: {data['label']} ({data['confidence']:.3f}) at {data['timestamp']}")

        # You could store this in your database like:
        # prediction = AudioPrediction(
        #     label=data['label'],
        #     confidence=data['confidence'],
        #     timestamp=data['timestamp']
        # )
        # db.session.add(prediction)
        # db.session.commit()

        return jsonify({
            'status': 'success',
            'message': 'Prediction logged successfully'
        }), 200

    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@app.route('/demo', methods=['GET'])
def demo_page():
    """Demo page with form to test endpoints"""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Flask API Demo</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; }
            .endpoint { margin: 20px 0; padding: 15px; border: 1px solid #ddd; }
            button { padding: 10px 15px; margin: 5px; background: #007bff; color: white; border: none; cursor: pointer; }
            button:hover { background: #0056b3; }
            input, textarea { padding: 8px; margin: 5px; width: 200px; border: 1px solid #ddd; }
            pre { background: #f5f5f5; padding: 10px; overflow-x: auto; border: 1px solid #ddd; }
            .success { color: green; }
            .error { color: red; }
        </style>
    </head>
    <body>
        <h1>Flask API Demo</h1>

        <div style="margin: 20px 0; padding: 15px; background: #e9ecef; border-radius: 5px;">
            <h3>🎤 Audio Classification</h3>
            <p>Try our Teachable Machine audio classifier:</p>
            <a href="/audio-classifier" style="display: inline-block; padding: 10px 20px; background: #28a745; color: white; text-decoration: none; border-radius: 5px;">Open Audio Classifier</a>
        </div>

        <div class="endpoint">
            <h3>Create User (POST /api/users)</h3>
            <input type="text" id="name" placeholder="Name">
            <input type="email" id="email" placeholder="Email">
            <button onclick="createUser()">Create User</button>
        </div>

        <div class="endpoint">
            <h3>Get All Users (GET /api/users)</h3>
            <button onclick="getUsers()">Get Users</button>
        </div>

        <div class="endpoint">
            <h3>Health Check (GET /api/health)</h3>
            <button onclick="healthCheck()">Check Health</button>
        </div>

        <div id="result">
            <h3>Response:</h3>
            <pre id="response">Click a button to test the API...</pre>
        </div>

        <script>
        async function createUser() {
            const name = document.getElementById('name').value;
            const email = document.getElementById('email').value;

            if (!name || !email) {
                document.getElementById('response').textContent = 'Please enter both name and email';
                return;
            }

            try {
                const response = await fetch('/api/users', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name, email})
                });

                const data = await response.json();
                document.getElementById('response').textContent = JSON.stringify(data, null, 2);

                // Clear inputs on success
                if (response.ok) {
                    document.getElementById('name').value = '';
                    document.getElementById('email').value = '';
                }
            } catch (error) {
                document.getElementById('response').textContent = 'Error: ' + error.message;
            }
        }

        async function getUsers() {
            try {
                const response = await fetch('/api/users');
                const data = await response.json();
                document.getElementById('response').textContent = JSON.stringify(data, null, 2);
            } catch (error) {
                document.getElementById('response').textContent = 'Error: ' + error.message;
            }
        }

        async function healthCheck() {
            try {
                const response = await fetch('/api/health');
                const data = await response.json();
                document.getElementById('response').textContent = JSON.stringify(data, null, 2);
            } catch (error) {
                document.getElementById('response').textContent = 'Error: ' + error.message;
            }
        }
        </script>
    </body>
    </html>
    """
    return html



@app.route('/defect-analysis')
def defect_analysis():
    """Defect Rate Statistical Analysis Tool"""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Defect Rate Statistical Analysis</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; background-color: #f5f5f5; }
            .container { max-width: 900px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            .section { margin: 20px 0; padding: 20px; background: #f8f9fa; border-radius: 8px; }
            .button { background: #007bff; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; display: inline-block; margin: 10px 10px 10px 0; }
            .button:hover { background: #0056b3; }
            .upload-section { border: 2px dashed #ccc; padding: 20px; text-align: center; margin: 20px 0; }
            pre { background: #f4f4f4; padding: 15px; border-radius: 5px; overflow-x: auto; }
            .formula { background: #e3f2fd; padding: 15px; border-left: 4px solid #2196f3; margin: 15px 0; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Daily Product Defect Rate Statistical Analysis</h1>
            <p>Determine if your daily product defect rate is statistically significant within a 5% margin of error.</p>
            
            <div class="section">
                <h2>Statistical Methods Used</h2>
                <div class="formula">
                    <strong>Confidence Interval for Proportion:</strong><br>
                    CI = p ± z × √(p(1-p)/n)<br>
                    <small>Where p = defect rate, n = sample size, z = 1.96 for 95% confidence</small>
                </div>
                <ul>
                    <li><strong>Z-test for proportions</strong> - Tests if daily rate differs significantly from expected</li>
                    <li><strong>Control charts</strong> - Identifies process variation and out-of-control points</li>
                    <li><strong>Trend analysis</strong> - Mann-Kendall and linear regression tests</li>
                    <li><strong>Margin of error analysis</strong> - Ensures precision within 5% requirement</li>
                </ul>
            </div>
            
            <div class="section">
                <h2>Run Analysis</h2>
                <a href="/api/run-defect-analysis" class="button">Run Sample Analysis</a>
                <a href="/defect-rate-analyzer" class="button">Download Analysis Tool</a>
                
                <div class="upload-section">
                    <h3>Upload Your Data</h3>
                    <p>Prepare a CSV file with columns: date, total_produced, defective_units</p>
                    <form action="/api/upload-defect-data" method="post" enctype="multipart/form-data" style="display: inline-block;" onsubmit="handleUpload(event)">
                        <input type="file" name="file" accept=".csv" required style="margin: 10px;">
                        <button type="submit" class="button">Upload & Analyze</button>
                    </form>
                    <div id="upload-result" style="margin-top: 15px; padding: 10px; display: none; border-radius: 5px;"></div>
                    
                    <script>
                    async function handleUpload(event) {
                        event.preventDefault();
                        const form = event.target;
                        const formData = new FormData(form);
                        const resultDiv = document.getElementById('upload-result');
                        
                        resultDiv.style.display = 'block';
                        resultDiv.innerHTML = '<p>🔄 Analyzing your data...</p>';
                        resultDiv.style.background = '#fff3cd';
                        
                        try {
                            const response = await fetch('/api/upload-defect-data', {
                                method: 'POST',
                                body: formData
                            });
                            
                            const result = await response.json();
                            
                            if (result.status === 'success') {
                                resultDiv.innerHTML = '<h4>✅ Analysis Complete</h4><pre>' + result.output + '</pre>';
                                resultDiv.style.background = '#d4edda';
                            } else {
                                resultDiv.innerHTML = '<h4>❌ Analysis Failed</h4><p>' + result.message + '</p>';
                                if (result.error) {
                                    resultDiv.innerHTML += '<pre>' + result.error + '</pre>';
                                }
                                resultDiv.style.background = '#f8d7da';
                            }
                        } catch (error) {
                            resultDiv.innerHTML = '<h4>❌ Error</h4><p>' + error.message + '</p>';
                            resultDiv.style.background = '#f8d7da';
                        }
                    }
                    </script>
                </div>
            </div>
            
            <div class="section">
                <h2>Sample Data Format</h2>
                <pre>date,total_produced,defective_units
2025-08-01,1050,23
2025-08-02,980,19
2025-08-03,1120,28</pre>
                <p><a href="/sample-defect-data" class="button">Download Sample Data</a></p>
            </div>
            
            <div class="section">
                <h2>Analysis Results Include</h2>
                <ul>
                    <li>Overall defect rate with 95% confidence interval</li>
                    <li>Daily significance testing (p-values < 0.05)</li>
                    <li>Margin of error validation (≤ 5% requirement)</li>
                    <li>Process control charts with control limits</li>
                    <li>Trend analysis (increasing/decreasing patterns)</li>
                    <li>Statistical visualizations and control charts</li>
                </ul>
            </div>
            
            <p><a href="/">← Back to Main App</a></p>
        </div>
    </body>
    </html>
    """

@app.route('/visualization.html')
def langextract_demo():
    """Serve the LangExtract visualization demo"""
    import os
    try:
        # Check if visualization.html exists
        if os.path.exists('visualization.html'):
            with open('visualization.html', 'r', encoding='utf-8') as f:
                html_content = f.read()
            return html_content
        else:
            return """
            <html>
            <head><title>LangExtract Demo</title></head>
            <body style="font-family: Arial, sans-serif; margin: 40px;">
                <h1>LangExtract Demo</h1>
                <p>The visualization file hasn't been generated yet.</p>
                <p>Run the 401LangExtract.py script first to generate the visualization.</p>
                <p><a href="/">← Back to Main App</a></p>
            </body>
            </html>
            """
    except Exception as e:
        return f"""
        <html>
        <head><title>Error - LangExtract Demo</title></head>
        <body style="font-family: Arial, sans-serif; margin: 40px;">
            <h1>Error Loading LangExtract Demo</h1>
            <p>An error occurred: {str(e)}</p>
            <p><a href="/">← Back to Main App</a></p>
        </body>
        </html>
        """

@app.route('/api/run-defect-analysis')
def run_defect_analysis():
    """Run the defect rate analysis and return results"""
    import subprocess
    import sys
    import os

    try:
        # Run the analysis script
        result = subprocess.run([sys.executable, 'simple_defect_analyzer.py'],
                              capture_output=True, text=True, timeout=60)
        
        if result.returncode == 0:
            # Check if visualization was created
            visualization_exists = os.path.exists('defect_rate_analysis.png')
            
            return {
                "status": "success",
                "message": "Analysis completed successfully",
                "output": result.stdout,
                "visualization_created": visualization_exists
            }
        else:
            return {
                "status": "error", 
                "message": "Analysis failed",
                "error": result.stderr
            }
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "Analysis timed out"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.route('/defect-rate-analyzer')
def download_analyzer():
    """Serve the defect rate analyzer Python script"""
    try:
        with open('defect_rate_analyzer.py', 'r') as f:
            content = f.read()
        
        response = app.response_class(
            content,
            mimetype='text/plain',
            headers={'Content-Disposition': 'attachment; filename=defect_rate_analyzer.py'}
        )
        return response
    except Exception as e:
        return f"Error: {str(e)}"

@app.route('/sample-defect-data')
def download_sample_data():
    """Serve sample defect data CSV"""
    try:
        with open('sample_defect_data.csv', 'r') as f:
            content = f.read()
        
        response = app.response_class(
            content,
            mimetype='text/csv',
            headers={'Content-Disposition': 'attachment; filename=sample_defect_data.csv'}
        )
        return response
    except Exception as e:
        return f"Error: {str(e)}"

@app.route('/api/upload-defect-data', methods=['POST'])
def upload_defect_data():
    """Handle CSV file upload and run analysis"""
    from flask import request
    import os
    import csv
    
    if 'file' not in request.files:
        return {"status": "error", "message": "No file uploaded"}
    
    file = request.files['file']
    if file.filename == '':
        return {"status": "error", "message": "No file selected"}
    
    if not file.filename.endswith('.csv'):
        return {"status": "error", "message": "Please upload a CSV file"}
    
    try:
        # Save uploaded file
        filename = 'uploaded_defect_data.csv'
        file.save(filename)
        
        # Create custom analyzer script for uploaded data
        analyzer_script = f'''
import csv
from simple_defect_analyzer import SimpleDefectAnalyzer

analyzer = SimpleDefectAnalyzer()

# Load uploaded data
data = []
with open('{filename}', 'r') as f:
    reader = csv.DictReader(f)
    for row in reader:
        defect_rate = int(row['defective_units']) / int(row['total_produced'])
        data.append({{
            'date': row['date'],
            'total_produced': int(row['total_produced']),
            'defective_units': int(row['defective_units']),
            'defect_rate': defect_rate
        }})

analyzer.data = data
analyzer.generate_report()
'''
        
        # Write and run custom analysis
        with open('custom_analysis.py', 'w') as f:
            f.write(analyzer_script)
        
        import subprocess
        import sys
        result = subprocess.run([sys.executable, 'custom_analysis.py'],
                              capture_output=True, text=True, timeout=60)
        
        # Clean up files
        if os.path.exists(filename):
            os.remove(filename)
        if os.path.exists('custom_analysis.py'):
            os.remove('custom_analysis.py')
        
        if result.returncode == 0:
            return {
                "status": "success",
                "message": "Analysis completed successfully",
                "output": result.stdout
            }
        else:
            return {
                "status": "error",
                "message": "Analysis failed",
                "error": result.stderr
            }
            
    except Exception as e:
        return {"status": "error", "message": f"Error processing file: {str(e)}"}

@app.route('/ontime-analysis')
def ontime_analysis():
    """On Time Delivery Rate Statistical Analysis Tool"""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>On Time Delivery Rate Statistical Analysis</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; background-color: #f5f5f5; }
            .container { max-width: 900px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            .section { margin: 20px 0; padding: 20px; background: #f8f9fa; border-radius: 8px; }
            .button { background: #007bff; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; display: inline-block; margin: 10px 10px 10px 0; }
            .button:hover { background: #0056b3; }
            .upload-section { border: 2px dashed #ccc; padding: 20px; text-align: center; margin: 20px 0; }
            pre { background: #f4f4f4; padding: 15px; border-radius: 5px; overflow-x: auto; }
            .formula { background: #e3f2fd; padding: 15px; border-left: 4px solid #2196f3; margin: 15px 0; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Daily Product On Time Delivery Rate Statistical Analysis</h1>
            <p>Determine if your daily on-time delivery rate is statistically significant within a 5% margin of error.</p>
            
            <div class="section">
                <h2>Statistical Methods Used</h2>
                <div class="formula">
                    <strong>Confidence Interval for Proportion:</strong><br>
                    CI = p ± z × √(p(1-p)/n)<br>
                    <small>Where p = on-time rate, n = sample size, z = 1.96 for 95% confidence</small>
                </div>
                <ul>
                    <li><strong>Z-test for proportions</strong> - Tests if daily rate differs significantly from expected</li>
                    <li><strong>Control charts</strong> - Identifies process variation and out-of-control points</li>
                    <li><strong>Trend analysis</strong> - Mann-Kendall and linear regression tests</li>
                    <li><strong>Margin of error analysis</strong> - Ensures precision within 5% requirement</li>
                </ul>
            </div>
            
            <div class="section">
                <h2>Sample Analysis</h2>
                <p>Run analysis on sample on-time delivery data to see how the tool works:</p>
                <a href="/api/run-ontime-analysis" class="button">Run Sample Analysis</a>
                <a href="/sample-ontime-data" class="button">Download Sample Data</a>
            </div>
            
            <div class="section">
                <h2>Use Your Own Data</h2>
                
                <div class="upload-section">
                    <h3>Upload Your Data</h3>
                    <p>Prepare a CSV file with columns: date, total_received, received_late</p>
                    <form action="/api/upload-ontime-data" method="post" enctype="multipart/form-data" style="display: inline-block;" onsubmit="handleUpload(event)">
                        <input type="file" name="file" accept=".csv" required style="margin: 10px;">
                        <button type="submit" class="button">Upload & Analyze</button>
                    </form>
                    <div id="upload-result" style="margin-top: 15px; padding: 10px; display: none; border-radius: 5px;"></div>
                    
                    <script>
                    async function handleUpload(event) {
                        event.preventDefault();
                        const form = event.target;
                        const formData = new FormData(form);
                        const resultDiv = document.getElementById('upload-result');
                        
                        resultDiv.style.display = 'block';
                        resultDiv.innerHTML = '<p>🔄 Analyzing your data...</p>';
                        resultDiv.style.background = '#fff3cd';
                        
                        try {
                            const response = await fetch('/api/upload-ontime-data', {
                                method: 'POST',
                                body: formData
                            });
                            
                            const result = await response.json();
                            
                            if (result.status === 'success') {
                                resultDiv.innerHTML = '<h4>✅ Analysis Complete</h4><pre>' + result.output + '</pre>';
                                resultDiv.style.background = '#d4edda';
                            } else {
                                resultDiv.innerHTML = '<h4>❌ Analysis Failed</h4><p>' + result.message + '</p>';
                                if (result.error) {
                                    resultDiv.innerHTML += '<pre>' + result.error + '</pre>';
                                }
                                resultDiv.style.background = '#f8d7da';
                            }
                        } catch (error) {
                            resultDiv.innerHTML = '<h4>❌ Error</h4><p>' + error.message + '</p>';
                            resultDiv.style.background = '#f8d7da';
                        }
                    }
                    </script>
                </div>
                
                <div style="margin-top: 20px;">
                    <h3>CSV Format Requirements</h3>
                    <ul>
                        <li><strong>date</strong> - Date in YYYY-MM-DD format</li>
                        <li><strong>total_received</strong> - Total number of deliveries received that day</li>
                        <li><strong>received_late</strong> - Number of deliveries that arrived late</li>
                    </ul>
                    <p><strong>Example:</strong></p>
                    <pre>date,total_received,received_late
2025-08-01,1023,52
2025-08-02,1187,71
2025-08-03,892,36</pre>
                </div>
            </div>
            
            <div class="section">
                <h2>Interpretation Guide</h2>
                <ul>
                    <li><strong>Significant Days</strong> - Days where delivery rate differs significantly from baseline</li>
                    <li><strong>Margin of Error</strong> - Should be ≤5% for reliable analysis</li>
                    <li><strong>Process Control</strong> - ≥95% of days should be within expected range</li>
                    <li><strong>Confidence Interval</strong> - 95% confidence range for true delivery rate</li>
                </ul>
            </div>
        </div>
    </body>
    </html>
    """

@app.route('/api/run-ontime-analysis')
def run_ontime_analysis():
    """Run sample on-time delivery analysis"""
    try:
        import subprocess
        import sys
        result = subprocess.run([sys.executable, 'ontime_delivery_analyzer.py'],
                              capture_output=True, text=True, timeout=60)
        
        if result.returncode == 0:
            return {
                "status": "success",
                "output": result.stdout,
                "message": "Sample on-time delivery analysis completed successfully"
            }
        else:
            return {
                "status": "error", 
                "message": "Analysis failed",
                "error": result.stderr
            }
    except Exception as e:
        return {"status": "error", "message": f"Error running analysis: {str(e)}"}

@app.route('/sample-ontime-data')
def download_sample_ontime_data():
    """Serve sample on-time delivery data CSV"""
    try:
        with open('sample_ontime_data.csv', 'r') as f:
            content = f.read()
        
        response = app.response_class(
            content,
            mimetype='text/csv',
            headers={'Content-Disposition': 'attachment; filename=sample_ontime_data.csv'}
        )
        return response
    except Exception as e:
        return f"Error: {str(e)}"

@app.route('/api/upload-ontime-data', methods=['POST'])
def upload_ontime_data():
    """Handle CSV file upload and run on-time delivery analysis"""
    from flask import request
    import os
    import csv
    
    if 'file' not in request.files:
        return {"status": "error", "message": "No file uploaded"}
    
    file = request.files['file']
    if file.filename == '':
        return {"status": "error", "message": "No file selected"}
    
    if not file.filename.endswith('.csv'):
        return {"status": "error", "message": "Please upload a CSV file"}
    
    try:
        # Save uploaded file
        filename = 'uploaded_ontime_data.csv'
        file.save(filename)
        
        # Create custom analyzer script for uploaded data
        analyzer_script = f'''
import csv
from ontime_delivery_analyzer import OnTimeDeliveryAnalyzer

analyzer = OnTimeDeliveryAnalyzer()

# Load uploaded data
data = []
with open('{filename}', 'r') as f:
    reader = csv.DictReader(f)
    for row in reader:
        total_received = int(row['total_received'])
        received_late = int(row['received_late'])
        received_ontime = total_received - received_late
        ontime_rate = received_ontime / total_received
        data.append({{
            'date': row['date'],
            'total_received': total_received,
            'received_late': received_late,
            'received_ontime': received_ontime,
            'ontime_rate': ontime_rate
        }})

analyzer.data = data
analyzer.generate_report()
'''
        
        # Write and run custom analysis
        with open('custom_ontime_analysis.py', 'w') as f:
            f.write(analyzer_script)
        
        import subprocess
        import sys
        result = subprocess.run([sys.executable, 'custom_ontime_analysis.py'],
                              capture_output=True, text=True, timeout=60)
        
        # Clean up files
        if os.path.exists(filename):
            os.remove(filename)
        if os.path.exists('custom_ontime_analysis.py'):
            os.remove('custom_ontime_analysis.py')
        
        if result.returncode == 0:
            return {
                "status": "success",
                "message": "Analysis completed successfully",
                "output": result.stdout
            }
        else:
            return {
                "status": "error",
                "message": "Analysis failed",
                "error": result.stderr
            }
            
    except Exception as e:
        return {"status": "error", "message": f"Error processing file: {str(e)}"}


# Contextual Hints API endpoints (Database-Backed)
@app.route("/api/hints", methods=["POST"])
def get_query_hints():
    """Get contextual hints for query input with database integration"""
    try:
        data = request.json
        partial_query = data.get("query", "").strip()
        available_fields = data.get("fields", [])
        table_name = data.get("table_name")  # Optional: extracted from query like "Quality Control | Example: ..."
        
        if not partial_query:
            return jsonify({"hints": []})
        
        # Extract table name from query if present
        if not table_name and '|' in partial_query:
            parts = partial_query.split('|')
            if len(parts) >= 2:
                table_name = parts[0].strip()
        
        hints = get_contextual_hints(partial_query, available_fields, table_name)
        
        return jsonify({
            "hints": hints,
            "query": partial_query,
            "table_name": table_name,
            "field_count": len(available_fields),
            "source": "database-backed" if table_name else "mixed"
        })
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/acronym/<acronym>")
def get_acronym_info(acronym):
    """Get detailed information about manufacturing acronyms"""
    try:
        info = expand_acronym(acronym)
        if info:
            return jsonify(info)
        else:
            return jsonify({"error": "Acronym not found"}), 404
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/acronyms", methods=["POST"])
def add_acronym():
    """
    Add new acronym definition to database
    Expected format: "table_name | ACRONYM = Definition"
    Example: "quality_control | NCM = Non-Conformant Material"
    """
    try:
        data = request.json
        input_text = data.get("text", "").strip()
        
        if not input_text:
            return jsonify({"error": "No input provided"}), 400
        
        # Parse format: "table_name | ACRONYM = Definition"
        if '|' not in input_text or '=' not in input_text:
            return jsonify({
                "error": "Invalid format. Use: 'table_name | ACRONYM = Definition'"
            }), 400
        
        # Extract components
        parts = input_text.split('|')
        if len(parts) != 2:
            return jsonify({
                "error": "Invalid format. Expected: 'table_name | ACRONYM = Definition'"
            }), 400
        
        table_name = parts[0].strip()
        acronym_def = parts[1].strip()
        
        if '=' not in acronym_def:
            return jsonify({
                "error": "Missing '=' in acronym definition"
            }), 400
        
        acronym_parts = acronym_def.split('=', 1)
        acronym = acronym_parts[0].strip()
        definition = acronym_parts[1].strip()
        
        if not acronym or not definition:
            return jsonify({
                "error": "Both acronym and definition are required"
            }), 400
        
        import sqlite3
        from config import SQLITE_DB_PATH
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        
        cur.execute("""
            INSERT INTO manufacturing_acronyms (acronym, definition, table_name)
            VALUES (?, ?, ?)
            ON CONFLICT (acronym, table_name) 
            DO UPDATE SET 
                definition = excluded.definition,
                updated_at = CURRENT_TIMESTAMP
        """, (acronym, definition, table_name))
        
        cur.execute("SELECT last_insert_rowid() as acronym_id")
        result = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        
        # Clear cache so new acronyms are immediately available
        from app.database_hints_loader import get_database_hints_loader
        loader = get_database_hints_loader()
        loader.clear_cache()
        
        return jsonify({
            "status": "success",
            "message": f"Acronym '{acronym}' merged into '{table_name}' table",
            "data": {
                "acronym": acronym,
                "definition": definition,
                "table_name": table_name,
                "acronym_id": result[0],
                "created_at": result[1].isoformat() if result[1] else None,
                "updated_at": result[2].isoformat() if result[2] else None
            }
        }), 201
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/edges/metadata", methods=["POST"])
def add_edge_metadata():
    """
    Add or update edge metadata for join relationships
    Expected format: "from_table → to_table: description | Example: join example"
    Example: "equipment → production_line: line ID | Example: e.line_id = pl.line_id"
    """
    try:
        data = request.json
        input_text = data.get("text", "").strip()
        
        if not input_text:
            return jsonify({"error": "No input provided"}), 400
        
        # Parse format: "from_table → to_table: description | Example: join"
        # Support both → and -> for arrow
        arrow = '→' if '→' in input_text else '->'
        
        if arrow not in input_text or ':' not in input_text:
            return jsonify({
                "error": "Invalid format. Use: 'from_table → to_table: description | Example: join example'"
            }), 400
        
        # Extract edge and metadata
        parts = input_text.split(arrow)
        if len(parts) != 2:
            return jsonify({
                "error": "Invalid format. Expected: 'from_table → to_table: description | Example: join'"
            }), 400
        
        from_table = parts[0].strip()
        remainder = parts[1].strip()
        
        # Split description and example
        if '|' in remainder:
            desc_part, example_part = remainder.split('|', 1)
            description = desc_part.strip().lstrip(':').strip()
            
            # Extract example (handle double "Example: Example:" format)
            if 'Example:' in example_part:
                # Split on all "Example:" and take the last part
                example = example_part.split('Example:')[-1].strip()
            else:
                example = example_part.strip()
        else:
            description = remainder.lstrip(':').strip()
            example = ''
        
        # Extract to_table from description (format: "to_table: desc")
        if ':' in description:
            to_table_part, desc_only = description.split(':', 1)
            to_table = to_table_part.strip()
            description = desc_only.strip()
        else:
            return jsonify({
                "error": "Invalid format. Missing ':' after to_table name"
            }), 400
        
        if not from_table or not to_table or not description:
            return jsonify({
                "error": "Missing required fields: from_table, to_table, or description"
            }), 400
        
        import sqlite3
        from config import SQLITE_DB_PATH
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        
        natural_alias = f"{from_table} to {to_table} via {description}"
        
        cur.execute("""
            SELECT edge_id FROM schema_edges 
            WHERE from_table = ? AND to_table = ?
        """, (from_table, to_table))
        
        edge = cur.fetchone()
        
        if edge:
            cur.execute("""
                UPDATE schema_edges 
                SET join_column_description = ?,
                    natural_language_alias = ?,
                    few_shot_example = ?,
                    context = ?
                WHERE from_table = ? AND to_table = ?
            """, (description, natural_alias, example, 
                  f"Join {from_table} and {to_table} on {description}",
                  from_table, to_table))
            
            edge_id = edge['edge_id']
            action = "updated"
        else:
            conn.close()
            return jsonify({
                "error": f"Edge {from_table} → {to_table} does not exist. Create the edge first in schema_edges table.",
                "suggestion": "Add the edge with join_column information before updating metadata"
            }), 404
        
        conn.commit()
        cur.close()
        conn.close()
        
        # Clear cache so new metadata is immediately available
        from app.database_hints_loader import get_database_hints_loader
        loader = get_database_hints_loader()
        loader.clear_cache()
        
        return jsonify({
            "status": "success",
            "message": f"Edge metadata {action}: {from_table} → {to_table}",
            "data": {
                "from_table": from_table,
                "to_table": to_table,
                "description": description,
                "natural_alias": natural_alias,
                "example": example,
                "edge_id": edge_id
            }
        }), 200 if action == "updated" else 201
    
    except sqlite3.IntegrityError as e:
        return jsonify({
            "error": f"Database constraint violation: {str(e)}",
            "suggestion": "Ensure both table names exist in schema_nodes"
        }), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/contextual-hints-demo")
def contextual_hints_demo():
    """Demo page for contextual hints system"""
    return render_template("contextual_hints_demo.html")


@app.route("/excel-cleansing", methods=["GET", "POST"])
def excel_cleansing():
    """Excel data cleansing interface"""
    if request.method == "GET":
        return render_template("excel_cleansing.html")
    
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    
    file = request.files['file']
    
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400
    
    if not file.filename.endswith(('.xlsx', '.xls')):
        return jsonify({"error": "Please upload an Excel file (.xlsx or .xls)"}), 400
    
    schema_dict = None
    if 'schema' in request.files:
        schema_file = request.files['schema']
        if schema_file.filename != '':
            try:
                import json
                schema_content = schema_file.read().decode('utf-8')
                schema_dict = json.loads(schema_content)
            except json.JSONDecodeError as e:
                return jsonify({"error": f"Invalid JSON schema: {str(e)}"}), 400
            except Exception as e:
                return jsonify({"error": f"Error reading schema file: {str(e)}"}), 400
    
    try:
        df, report, cleansed_bytes = cleanse_uploaded_excel(file, schema_dict)
        
        preview_html = df.head(20).to_html(classes='table table-striped', index=False)
        
        response_data = {
            "success": True,
            "report": report,
            "preview": preview_html,
            "download_ready": True
        }
        
        return jsonify(response_data)
    
    except Exception as e:
        return jsonify({"error": f"Error processing file: {str(e)}"}), 500


@app.route("/excel-cleansing/download", methods=["POST"])
def download_cleansed_excel():
    """Download cleansed Excel file"""
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file provided"}), 400
        
        file = request.files['file']
        
        schema_dict = None
        if 'schema' in request.files:
            schema_file = request.files['schema']
            if schema_file.filename != '':
                import json
                schema_content = schema_file.read().decode('utf-8')
                schema_dict = json.loads(schema_content)
        
        df, report, cleansed_bytes = cleanse_uploaded_excel(file, schema_dict)
        
        return send_file(
            BytesIO(cleansed_bytes),
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f"cleansed_{file.filename}"
        )
    
    except Exception as e:
        return jsonify({"error": f"Error creating download: {str(e)}"}), 500


@app.route("/upload-file")
def upload_file_page():
    """File upload interface to save files to workspace"""
    return render_template('upload_file.html')


@app.route("/upload-file/save", methods=["POST"])
def save_uploaded_file():
    """Save uploaded file to workspace uploads directory"""
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file provided"}), 400
        
        file = request.files['file']
        
        if file.filename == '':
            return jsonify({"error": "No file selected"}), 400
        
        upload_dir = os.path.join(os.getcwd(), 'uploads')
        os.makedirs(upload_dir, exist_ok=True)
        
        filepath = os.path.join(upload_dir, file.filename)
        file.save(filepath)
        
        file_size = os.path.getsize(filepath)
        
        return jsonify({
            "success": True,
            "message": f"File uploaded successfully!",
            "filename": file.filename,
            "path": f"uploads/{file.filename}",
            "size": file_size,
            "size_kb": round(file_size / 1024, 2)
        })
    
    except Exception as e:
        return jsonify({"error": f"Error saving file: {str(e)}"}), 500


@app.route("/document-segmentation")
def document_segmentation_page():
    """Document segmentation interface"""
    return render_template('document_segmentation.html')


@app.route("/document-segmentation/process", methods=["POST"])
def process_document_segmentation():
    """Process document segmentation"""
    from app.document_segmentation import segment_document, format_segmentation_report
    import json as json_lib
    
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No Excel file provided"}), 400
        
        excel_file = request.files['file']
        
        if excel_file.filename == '':
            return jsonify({"error": "No file selected"}), 400
        
        segmentation_content = None
        if 'scheme' in request.files and request.files['scheme'].filename != '':
            scheme_file = request.files['scheme']
            segmentation_content = scheme_file.read().decode('utf-8')
        
        result = segment_document(excel_file, segmentation_content)
        
        report = format_segmentation_report(result)
        
        return jsonify({
            "success": True,
            "result": result,
            "report": report,
            "filename": excel_file.filename
        })
    
    except Exception as e:
        import traceback
        return jsonify({
            "error": f"Error processing segmentation: {str(e)}",
            "traceback": traceback.format_exc()
        }), 500


@app.route("/combined-pipeline")
def combined_pipeline_page():
    """Combined cleansing + segmentation pipeline interface"""
    return render_template('combined_pipeline.html')


@app.route("/combined-pipeline/process", methods=["POST"])
def process_combined_pipeline():
    """Process combined cleansing + segmentation pipeline"""
    import importlib
    import sys
    import base64
    if 'app.combined_pipeline' in sys.modules:
        importlib.reload(sys.modules['app.combined_pipeline'])
    from app.combined_pipeline import process_combined_pipeline
    import json as json_lib
    import zipfile
    from pathlib import Path
    
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No Excel file provided"}), 400
        
        excel_file = request.files['file']
        
        if excel_file.filename == '':
            return jsonify({"error": "No file selected"}), 400
        
        segmentation_content = None
        if 'scheme' in request.files and request.files['scheme'].filename != '':
            scheme_file = request.files['scheme']
            segmentation_content = scheme_file.read().decode('utf-8')
        
        schema_rules = {}
        if 'schema1' in request.files and request.files['schema1'].filename != '':
            schema1_file = request.files['schema1']
            try:
                schema_rules[1] = json_lib.load(schema1_file)
            except json_lib.JSONDecodeError as e:
                return jsonify({"error": f"Invalid JSON in Schema 1 file: {str(e)}"}), 400
        
        if 'schema2' in request.files and request.files['schema2'].filename != '':
            schema2_file = request.files['schema2']
            try:
                schema_rules[2] = json_lib.load(schema2_file)
            except json_lib.JSONDecodeError as e:
                return jsonify({"error": f"Invalid JSON in Schema 2 file: {str(e)}"}), 400
        
        if not schema_rules:
            schema_rules = None
        
        result = process_combined_pipeline(
            excel_file=excel_file,
            segmentation_scheme=segmentation_content,
            schema_rules=schema_rules
        )
        
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for csv_name, csv_path in result['csv_files'].items():
                with open(csv_path, 'rb') as f:
                    zip_file.writestr(csv_name, f.read())
        
        zip_buffer.seek(0)
        
        zip_base64 = base64.b64encode(zip_buffer.getvalue()).decode('utf-8')
        
        return jsonify({
            "success": True,
            "result": result,
            "report": result['report'],
            "filename": excel_file.filename,
            "zip_download": zip_base64,
            "csv_count": len(result['csv_files'])
        })
    
    except Exception as e:
        import traceback
        return jsonify({
            "error": f"Error processing pipeline: {str(e)}",
            "traceback": traceback.format_exc()
        }), 500


@app.route('/huggingface-mcp')
def huggingface_mcp_page():
    """Hugging Face MCP Server Interface for Manufacturing Semantic Context"""
    return render_template('huggingface_mcp.html')




@app.route('/huggingface-mcp/search')
def huggingface_mcp_search():
    """Search Hugging Face Hub via MCP interface"""
    from app.huggingface_mcp import create_hf_mcp_client
    
    query = request.args.get('query', '')
    search_type = request.args.get('type', 'models')
    limit = int(request.args.get('limit', 10))
    task = request.args.get('task', '')
    sort = request.args.get('sort', 'downloads')
    
    if not query:
        return jsonify({"error": "Query is required"}), 400
    
    try:
        client = create_hf_mcp_client()
        if not client:
            return jsonify({"error": "HUGGINGFACE_TOKEN not configured"}), 500
        
        if search_type == 'models':
            result = client.search_models(query=query, task=task if task else None, limit=limit, sort=sort)
        elif search_type == 'datasets':
            result = client.search_datasets(query=query, limit=limit, sort=sort)
        elif search_type == 'spaces':
            result = client.search_spaces(query=query, limit=limit, sort=sort)
        else:
            return jsonify({"error": f"Unknown search type: {search_type}"}), 400
        
        if result.success:
            return jsonify({"results": result.data})
        else:
            return jsonify({"error": result.error}), 500
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/huggingface-mcp/model/<path:model_id>')
def huggingface_mcp_model_info(model_id):
    """Get detailed information about a specific model"""
    from app.huggingface_mcp import create_hf_mcp_client
    
    try:
        client = create_hf_mcp_client()
        if not client:
            return jsonify({"error": "HUGGINGFACE_TOKEN not configured"}), 500
        
        result = client.get_model_info(model_id)
        
        if result.success:
            return jsonify({"model": result.data})
        else:
            return jsonify({"error": result.error}), 500
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/huggingface-mcp/tools')
def huggingface_mcp_tools():
    """List available MCP tools"""
    from app.huggingface_mcp import create_hf_mcp_client
    
    try:
        client = create_hf_mcp_client()
        if not client:
            return jsonify({"error": "HUGGINGFACE_TOKEN not configured"}), 500
        
        tools = client.get_mcp_tools()
        return jsonify({"tools": tools})
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/schema-helper")
def schema_helper_page():
    """CSV to JSON Schema helper interface"""
    return render_template('schema_helper.html')


@app.route("/schema-helper/convert", methods=["POST"])
def convert_csv_to_schema():
    """Convert CSV sample to JSON schema"""
    from app.schema_helper import process_csv_for_block_schema
    
    try:
        data = request.get_json()
        if not data or 'csv_content' not in data:
            return jsonify({"error": "No CSV content provided"}), 400
        
        csv_content = data['csv_content']
        block_type = data.get('block_type', 'tabular')
        keep_nbsp = data.get('keep_nbsp', True)
        
        result = process_csv_for_block_schema(csv_content, block_type, keep_nbsp)
        
        return jsonify({
            "success": True,
            "schema": result['schema'],
            "json_output": result['json_output'],
            "columns": result['columns'],
            "column_count": result['column_count'],
            "type_summary": result['type_summary'],
            "nbsp_info": result['nbsp_info'],
            "nbsp_count": result['nbsp_count'],
            "keep_nbsp": result['keep_nbsp']
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500


GRADIO_BACKEND = os.environ.get("GRADIO_BACKEND", "http://127.0.0.1:5000")


@app.route('/gradio')
def proxy_gradio_root():
    from flask import redirect
    return redirect('/gradio/', code=302)

@app.route('/gradio/', defaults={'path': ''})
@app.route('/gradio/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def proxy_gradio(path):
    target_url = f"{GRADIO_BACKEND}/gradio/{path}"
    if request.query_string:
        target_url += f"?{request.query_string.decode()}"
    try:
        resp = http_requests.request(
            method=request.method,
            url=target_url,
            headers={k: v for k, v in request.headers if k.lower() not in ('host', 'content-length')},
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False,
            timeout=30,
            stream=True,
        )
        excluded = {'content-encoding', 'transfer-encoding', 'connection'}
        headers = [(k, v) for k, v in resp.raw.headers.items() if k.lower() not in excluded]
        return Response(resp.content, status=resp.status_code, headers=headers)
    except http_requests.exceptions.ConnectionError:
        return Response("The Gradio app is starting up — please refresh in a moment.", status=503)

_DR_STATIC = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "hf-space-inventory-sqlgen", "static", "define-relationship"
)

@app.route('/define-relationship')
def dr_redirect():
    from flask import redirect
    return redirect('/define-relationship/', code=302)

def _dr_index_name():
    for name in ("index.html", "index-define-relationship.html"):
        if os.path.isfile(os.path.join(_DR_STATIC, name)):
            return name
    return None

@app.route('/define-relationship/')
def dr_index():
    name = _dr_index_name()
    if name:
        return send_from_directory(_DR_STATIC, name)
    return "Define Relationship build not found", 404

@app.route('/define-relationship/<path:path>')
def dr_asset(path):
    full = os.path.join(_DR_STATIC, path)
    if os.path.isfile(full):
        return send_from_directory(_DR_STATIC, path)
    name = _dr_index_name()
    if name:
        return send_from_directory(_DR_STATIC, name)
    return "Define Relationship build not found", 404

@app.route('/mcp/', defaults={'path': ''})
@app.route('/mcp/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def proxy_mcp(path):
    target_url = f"{GRADIO_BACKEND}/mcp/{path}"
    if request.query_string:
        target_url += f"?{request.query_string.decode()}"
    try:
        resp = http_requests.request(
            method=request.method,
            url=target_url,
            headers={k: v for k, v in request.headers if k.lower() not in ('host', 'content-length')},
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False,
            timeout=30,
            stream=True,
        )
        excluded = {'content-encoding', 'transfer-encoding', 'connection'}
        headers = [(k, v) for k, v in resp.raw.headers.items() if k.lower() not in excluded]
        return Response(resp.content, status=resp.status_code, headers=headers)
    except http_requests.exceptions.ConnectionError:
        return Response("The Gradio app is starting up — please refresh in a moment.", status=503)


MOCKUP_BACKEND = "http://127.0.0.1:23636"

@app.route('/__mockup/', defaults={'path': ''})
@app.route('/__mockup/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def proxy_mockup(path):
    """Proxy to mockup sandbox Vite dev server."""
    target_url = f"{MOCKUP_BACKEND}/__mockup/{path}"
    if request.query_string:
        target_url += f"?{request.query_string.decode()}"
    try:
        resp = http_requests.request(
            method=request.method,
            url=target_url,
            headers={k: v for k, v in request.headers if k.lower() not in ('host', 'content-length')},
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False,
            timeout=30,
            stream=True,
        )
        excluded = {'content-encoding', 'transfer-encoding', 'connection'}
        headers = [(k, v) for k, v in resp.raw.headers.items() if k.lower() not in excluded]
        return Response(resp.content, status=resp.status_code, headers=headers)
    except http_requests.exceptions.ConnectionError:
        return Response("Mockup sandbox is not running.", status=503)


if __name__ == '__main__':
    import os
    port = int(os.environ.get('FLASK_PORT', 3000))
    use_reloader = os.environ.get('FLASK_NO_RELOAD', '0') != '1'
    app.run(debug=True, host='0.0.0.0', port=port, use_reloader=use_reloader)

