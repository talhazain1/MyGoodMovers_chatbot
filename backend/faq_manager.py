import openai
import numpy as np
import json
import os

class FAQManager:
    def __init__(self, embedding_model="text-embedding-ada-002"):
        self.embedding_model = embedding_model
        self.faq_data = []
        self.embeddings = []

    def load_faqs(self, dataset_path, cache_path="faq_embeddings.npy"):
        """
        Load FAQs from a dataset and precompute or load cached embeddings.
        """
        with open(dataset_path, "r") as file:
            self.faq_data = []
            for line in file:
                try:
                    entry = json.loads(line)
                    if "question" in entry and "answer" in entry:
                        self.faq_data.append(entry)
                except json.JSONDecodeError:
                    print(f"Skipping invalid JSON: {line}")

        if os.path.exists(cache_path):
            self.embeddings = np.load(cache_path)
        else:
            self.embeddings = [
                self.get_embedding(faq["question"]) for faq in self.faq_data
            ]
            np.save(cache_path, self.embeddings)

    def get_embedding(self, text):
        """
        Get the embedding for a given text using OpenAI.
        """
        if not text.strip():
            raise ValueError("Input text is empty.")
        response = openai.Embedding.create(
            model=self.embedding_model,
            input=text
        )
        if "data" not in response or not response["data"]:
            raise RuntimeError("Failed to fetch embedding from OpenAI.")
        return np.array(response["data"][0]["embedding"])

    def find_best_match(self, user_question):
        user_embedding = self.get_embedding(user_question)

        similarities = [
            np.dot(user_embedding, faq_embedding) /
            (np.linalg.norm(user_embedding) * np.linalg.norm(faq_embedding))
            for faq_embedding in self.embeddings
        ]

        best_match_idx = np.argmax(similarities)
        best_match_score = similarities[best_match_idx]

        threshold = 0.75
        if best_match_score > threshold:
            return self.faq_data[best_match_idx]["answer"]

        # Fallback
        return "I'm sorry, I couldn't find an exact match. Could you provide more details?"

        # Fallback to general query if no good match
        # return None
    