import os
import json
import time
from pathlib import Path
from chromadb.api.types import EmbeddingFunction, Documents, Embeddings
from google import genai
from google.genai import types

# Load config
CONFIG_PATH = Path(__file__).parent / "config.json"
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

EMBEDDING_MODEL = config.get("embedding_model", "gemini-embedding-2-preview")

class GeminiEmbeddingFunction(EmbeddingFunction):
    """
    Custom ChromaDB Embedding Function that uses google.genai SDK
    to generate embeddings using Gemini models.
    """
    def __init__(self, api_key: str = None, task_type: str = "RETRIEVAL_DOCUMENT"):
        # Initialize the GenAI client. It will automatically pick up GOOGLE_API_KEY from environment if not provided.
        self.client = genai.Client(api_key=api_key)
        self.task_type = task_type
        
    def __call__(self, input: Documents) -> Embeddings:
        """
        Embed a list of documents with automatic batching (max 100 per request).
        """
        if not input:
            return []
            
        all_embeddings = []
        batch_size = 100
        
        for i in range(0, len(input), batch_size):
            batch = input[i : i + batch_size]
            
            import backoff
            from google.genai.errors import APIError
            
            # Decorator to catch 429 quota errors and apply exponential backoff
            @backoff.on_exception(
                backoff.expo, 
                APIError, 
                max_tries=5, 
                giveup=lambda e: e.code != 429,
                factor=2,
                jitter=backoff.full_jitter
            )
            def request_embeddings_with_retry(batch_data):
                return self.client.models.embed_content(
                    model=EMBEDDING_MODEL,
                    contents=batch_data,
                    config=types.EmbedContentConfig(
                        task_type=self.task_type
                    )
                )

            try:
                response = request_embeddings_with_retry(batch)
                
                if isinstance(response.embeddings, list):
                    for emb in response.embeddings:
                        all_embeddings.append(emb.values)
                else:
                    all_embeddings.append(response.embeddings.values)
                    
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Error generating embeddings for batch {i//batch_size}: {e}")
                raise
                
        return all_embeddings
