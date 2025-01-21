import googlemaps
from datetime import datetime
from config import Config
from dotenv import load_dotenv
import os

class MapsManager:
    def __init__(self):
        self.client = googlemaps.Client(key=os.getenv("GOOGLE_MAPS_API_KEY"))
        self.base_rate_per_mile = 1.50
        self.move_size_rates = {
            "studio": 0.80 * 400,
            "1-bedroom": 0.80 * 800,
            "2-bedroom": 0.80 * 1200,
            "3-bedroom": 0.80 * 1600,
            "4-bedroom": 0.80 * 2000,
            "office": 0.80 * 2500,
            "car": 0.80 * 150
        }
        self.additional_charges = {
            "packing": 150,
            "storage": 100
        }
        self.seasonality_rate = 0.10
        self.rural_location_rate = 0.10

    def calculate_distance(self, origin, destination):
        """Calculate the driving distance between two locations."""
        try:
            result = self.client.distance_matrix(origins=[origin], destinations=[destination], mode="driving")
            if result['rows'][0]['elements'][0]['status'] == 'OK':
                distance = result['rows'][0]['elements'][0]['distance']['value'] / 1609.34
                return round(distance, 2)
            else:
                return None
        except Exception as e:
            return f"Error: {e}"

    def estimate_cost(self, origin, destination, move_size, additional_services=None):
        """Estimate the cost of the move."""
        distance = self.calculate_distance(origin, destination)
        if distance is None:
            return None, "Error calculating distance."

        base_cost = distance * self.base_rate_per_mile
        move_size_cost = self.move_size_rates.get(move_size.lower(), 0)
        additional_cost = sum(self.additional_charges.get(service, 0) for service in (additional_services or []))
        total_cost = base_cost + move_size_cost + additional_cost

        min_cost = total_cost * 1.2
        max_cost = total_cost * 1.7

        return distance, (round(min_cost, 2), round(max_cost, 2))
