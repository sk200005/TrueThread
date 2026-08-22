import os
import requests
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")

def test_openrouter():
    if not OPENROUTER_API_KEY:
        print("❌ OPENROUTER_API_KEY is not set in .env")
        return

    print("Testing OpenRouter API...")
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "meta-llama/llama-3.1-8b-instruct", # Using a standard model
        "messages": [{"role": "user", "content": "Say 'OpenRouter is working!' if you can hear me."}]
    }
    
    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        result = response.json()
        print(f"✅ OpenRouter Success! Response: {result['choices'][0]['message']['content']}\n")
    except Exception as e:
        print(f"❌ OpenRouter Failed: {e}")
        if 'response' in locals() and hasattr(response, 'text'):
            print(f"Response details: {response.text}\n")

def test_mistral():
    if not MISTRAL_API_KEY:
        print("❌ MISTRAL_API_KEY is not set in .env")
        return

    print("Testing Mistral API...")
    url = "https://api.mistral.ai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {MISTRAL_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    data = {
        "model": "mistral-small-latest",
        "messages": [{"role": "user", "content": "Say 'Mistral is working!' if you can hear me."}]
    }
    
    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        result = response.json()
        print(f"✅ Mistral Success! Response: {result['choices'][0]['message']['content']}\n")
    except Exception as e:
        print(f"❌ Mistral Failed: {e}")
        if 'response' in locals() and hasattr(response, 'text'):
            print(f"Response details: {response.text}\n")

if __name__ == "__main__":
    print("Starting LLM API Tests...\n")
    test_openrouter()
    test_mistral()
