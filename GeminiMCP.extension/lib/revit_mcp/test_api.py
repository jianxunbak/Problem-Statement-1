import urllib.request
import json
import ssl

def test_gemini_api(key, model):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    data = {
        "contents": [{"parts":[{"text": "Hello"}]}]
    }
    
    req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'})
    ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as f:
            print(f"SUCCESS with {model}")
            return True
    except Exception as e:
        print(f"FAILED with {model}: {e}")
        return str(e)

key = "AIzaSyAy27wbmB4UfP8roNRQmF1ohrXzQA8ABt8"
test_gemini_api(key, "gemini-1.5-flash")
test_gemini_api(key, "gemini-2.0-flash")
test_gemini_api(key, "gemini-2.5-flash")
