import sys
from backend.app import app  # Adjust the import path if needed

# Ensure the application can find your backend folder
sys.path.insert(0, "backend")

# The WSGI server needs this variable to serve the app
application = app
