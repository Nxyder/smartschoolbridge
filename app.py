import os
from flask import Flask, request, jsonify
from flask_cors import CORS
from smartschool import Smartschool, EnvCredentials, PlannedElements

app = Flask(__name__)

# CORS inschakelen voor alle routes
# Je kunt ook specifieke origins opgeven: CORS(app, origins=["https://jouwsite.com"])
CORS(app)

@app.route('/planner', methods=['GET'])
def get_planner():
    # Haal optionele parameters op uit de URL
    start = request.args.get('start', '2026-09-27')
    end = request.args.get('end', '2026-10-31')
    
    try:
        session = Smartschool(EnvCredentials())
        
        data = []
        for element in PlannedElements(session):
            data.append({
                "naam": element.name,
                "vakken": [c.name for c in element.courses],
                "deadline": element.period.date_time_to.isoformat()
            })
        
        return jsonify({
            "status": "success",
            "count": len(data),
            "data": data
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint voor uptime monitors."""
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
