import os

class Config:
    # Redis Configuration
    REDIS_HOST = "localhost"
    REDIS_PORT = 6379
    REDIS_PASSWORD = None  # Add your Redis password if applicable

    # OpenAI Configuration
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # Set via environment variable

    # Google Maps API Configuration
    GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")  # Set via environment variable
