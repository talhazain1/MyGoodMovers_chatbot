import openai
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

class OpenAIManager:
    def __init__(self):
        # Set your OpenAI API key
        openai.api_key = os.getenv("OPENAI_API_KEY")
    def extract_fields_from_text(self, system_prompt, user_text):
        """
        Calls OpenAI with a system prompt that instructs the model
        to output strictly valid JSON containing fields:
          origin, destination, move_size, additional_services, username, contact_no
        or null if missing.

        Returns a JSON string. It's up to the caller to parse that JSON.
        """
        try:
            response = openai.ChatCompletion.create(
                model="gpt-4o-mini",  # or gpt-4, whichever you have access to
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text}
                ],
                temperature=0.0  # so it stays consistent
            )
            return response["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"extract_fields_from_text error: {e}")
            # If something goes wrong, return empty JSON so parse won't break
            return "{}"

    def get_general_response(self, system_content, user_content):
        """
        Fetch a response from OpenAI GPT for general queries using two separate inputs:
          1) system_content (instructions to keep answers short, brand context, etc.)
          2) user_content (the actual user prompt or combined conversation context).
        """
        try:
            response = openai.ChatCompletion.create(
                model="gpt-4o-mini",  # Use the correct model available for your API plan
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": user_content}
                ],
                max_tokens=200,
                temperature=0.4
            )
            # Extract and return the assistant's reply
            return response["choices"][0]["message"]["content"].strip()
        except openai.AuthenticationError:
            return "Error: Invalid API key. Please check your OpenAI API key."
        except openai.BadRequestError as e:
            return f"Error: Bad request. Details: {e}"
        except openai.RateLimitError:
            return "Error: Rate limit exceeded. Please try again later."
        except openai.APIError as e:
            return f"Error: OpenAI API error. Details: {e}"
        except Exception as e:
            return f"An unexpected error occurred: {e}"
