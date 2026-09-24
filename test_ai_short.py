#!/usr/bin/env python3
"""
Quick test script to verify AI connectivity with a short prompt
"""

import logging
from config import OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL, API_TYPE, SHOW_AI_DEBUG
from src.ai_client import AIClient

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def test_short_prompt():
    """Test AI with a very short prompt"""
    
    print("=" * 80)
    print("Testing AI Connection with Short Prompt")
    print("=" * 80)
    print(f"Model: {OPENAI_MODEL}")
    print(f"Debug Mode: {SHOW_AI_DEBUG}")
    print("=" * 80 + "\n")
    
    # Initialize client
    try:
        client = AIClient(OPENAI_API_KEY, model=OPENAI_MODEL, base_url=OPENAI_BASE_URL, api_type=API_TYPE)
        print("✓ AI Client initialized successfully\n")
    except Exception as e:
        print(f"✗ Failed to initialize AI Client: {e}\n")
        return False
    
    # Test 1: Connection test
    print("Test 1: Basic Connection Test")
    print("-" * 40)
    try:
        client.test_connection()
        print("✓ Connection test passed\n")
    except Exception as e:
        print(f"✗ Connection test failed: {e}\n")
        return False
    
    # Test 2: Simple JSON analysis
    print("Test 2: Simple JSON Generation (2 questions)")
    print("-" * 40)
    
    short_txt = """
    Complete the sentences:
    1. I _____ (go) to school yesterday.
    2. She _____ (be) happy about this.
    """
    
    short_md = """
    ## Grammar Exercise

    Complete the sentences:
    1. I _____ (go) to school yesterday.
    2. She _____ (be) happy about this.
    """
    
    try:
        print("Sending short prompt to AI...")
        result = client.analyze_paper(short_txt, short_md)
        
        print("✓ AI Response received successfully!")
        print(f"Response type: {type(result)}")
        print(f"Response keys: {list(result.keys()) if isinstance(result, dict) else 'N/A'}")
        print(f"Full response:\n{result}\n")
        return True
        
    except Exception as e:
        print(f"✗ AI analysis failed: {e}\n")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = test_short_prompt()
    
    print("=" * 80)
    if success:
        print("✓ All tests passed! AI connection is working.")
        print("\nNext steps:")
        print("1. If short prompt works, gradually increase size")
        print("2. Check timeout settings in config.py")
        print("3. Then try full paper analysis")
    else:
        print("✗ Tests failed. Check the errors above.")
    print("=" * 80)