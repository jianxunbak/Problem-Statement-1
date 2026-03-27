# -*- coding: utf-8 -*-
import json
import urllib.request
import ssl
import os

# Mock the client's logic for testing
def test_gemini_call(api_key, model, prompt, history=None):
    url = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent?key={}".format(model, api_key)
    
    contents = []
    if history:
        for msg in history:
            role = "user" if msg["is_user"] else "model"
            contents.append({"role": role, "parts": [{"text": msg["text"]}]})
    
    # SYSTEM INSTRUCTION
    instr = "You are a test agent. All responses should be short."
    full_prompt = "SYSTEM: {}\n\nUSER: {}".format(instr, prompt)
    
    # Protocol compliance: If history already has a user message, we must interleave a model response OR replace the last user message.
    # But for THIS test, let's see what happens if we SEND TWO USERS in a row.
    contents.append({"role": "user", "parts": [{"text": full_prompt}]})

    data = {
        "contents": contents,
        "generationConfig": {"temperature": 0.1}
    }
    
    headers = {'Content-Type': 'application/json'}
    req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers=headers)
    ctx = ssl._create_unverified_context()
    
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as f:
            res = json.loads(f.read().decode('utf-8'))
            print("SUCCESS: Received response.")
            if 'candidates' in res:
                print("Text:", res['candidates'][0]['content']['parts'][0]['text'])
    except Exception as e:
        print("FAILED: ", str(e))
        if hasattr(e, 'read'):
            print("Error details:", e.read().decode('utf-8'))

if __name__ == "__main__":
    # Get key from .env
    api_key = "AIzaSyB0-OoEZavP2hnc0OB-lPdSzszq3D-wVXE" # From .env
    model = "gemini-2.0-flash" # FIXED
    
    print("Test 1: Single message")
    test_gemini_call(api_key, model, "Hello?")
    
    print("\nTest 2: Two consecutive user messages (Expected to fail)")
    history = [{"text": "First user message", "is_user": True}]
    test_gemini_call(api_key, model, "Second user message", history)
