#!/usr/bin/env python3
"""
Test script: Use existing AI response without calling API again

This script:
1. Loads MD file only (no TXT needed)
2. Uses the saved AI response from last_response_raw.txt
3. Runs: process AI result → build JSON → validate
4. Saves the final result
"""

import json
import logging
from pathlib import Path
from datetime import datetime

from src.loader import PaperLoader, PaperData
from src.parser import ExamParser
from src.json_builder import JSONBuilder
from src.validator import FinalJSONValidator
from config import INPUT_DIR, OUTPUT_DIR, DEBUG_DIR

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def load_ai_response(response_file: Path) -> dict:
    """Load AI response from file (handle various formats)"""
    with open(response_file, 'r', encoding='utf-8') as f:
        raw_content = f.read()
    
    # Normalize curly/smart quotes to straight quotes
    raw_content = raw_content.replace('\u2018', "'").replace('\u2019', "'")
    raw_content = raw_content.replace('\u201c', '\\"').replace('\u201d', '\\"')
    
    content = raw_content
    
    # Method 1: Look for ```json ... ``` block
    if '```json' in content:
        start = content.find('```json') + len('```json')
        end = content.find('```', start)
        if end > start:
            content = content[start:end].strip()
    elif '```' in content:
        # Method 2: Look for any ``` ... ``` block
        start = content.find('```') + 3
        if content[start:start+4] in ['json', 'JSON']:
            start += 4
        end = content.find('```', start)
        if end > start:
            content = content[start:end].strip()
    else:
        # Method 3: Find first { and last }
        start = content.find('{')
        end = content.rfind('}')
        if start >= 0 and end > start:
            content = content[start:end+1]
    
    content = content.strip()
    
    if not content:
        raise ValueError(f"No JSON found in {response_file}")
    
    return json.loads(content)


def test_with_existing_response(paper_name: str = "2026北京西城高三（上）期末"):
    """Test parsing with existing AI response"""
    
    print("\n" + "=" * 80)
    print(f"🧪 Testing with existing AI response for: {paper_name}")
    print("=" * 80 + "\n")
    
    try:
        # Stage 1: Load MD file only
        print("📂 Stage 1: Loading MD file...")
        loader = PaperLoader(INPUT_DIR)
        paper_data = loader.load_paper(paper_name)
        print(f"   ✓ Loaded MD: {paper_data.md_path.name} ({len(paper_data.md_content)} chars)")
        
        # Load existing AI response
        print("\n🤖 Loading existing AI response...")
        response_file = DEBUG_DIR / "last_response_raw.txt"
        ai_result = load_ai_response(response_file)
        print(f"   ✓ Loaded AI response ({len(json.dumps(ai_result, ensure_ascii=False))} chars)")
        print(f"   ✓ Found {len(ai_result.get('sections', []))} sections")
        
        # Create parser instance and use its methods
        parser = ExamParser.__new__(ExamParser)
        
        # Stage 2: Override paper info from filename
        print("\n📋 Stage 2: Parsing paper info from filename...")
        parser._inject_paper_from_filename(paper_data, ai_result)
        paper = ai_result.get("paper", {})
        print(f"   ✓ title={paper.get('title')}, year={paper.get('year')}, grade={paper.get('grade')}")
        
        # Stage 3: Process AI result
        print("\n🔧 Stage 3: Processing AI result...")
        sections = parser._process_ai_result(paper_data, ai_result)
        
        # Show results
        for i, section in enumerate(sections):
            section_type = section.get('type', 'unknown')
            article = section.get('article', '')
            has_article = bool(article)
            has_questions = bool(section.get('questionsText', ''))
            questions = section.get('questions', [])
            
            cloze_count = article.count('<ClozeBlank>')
            input_count = article.count('<Input')
            blank_count = article.count('<Blank>')
            input2_count = article.count('<Input2>')
            
            tags = []
            if cloze_count: tags.append(f"{cloze_count} ClozeBlank")
            if input_count: tags.append(f"{input_count} Input")
            if blank_count: tags.append(f"{blank_count} Blank")
            if input2_count: tags.append(f"{input2_count} Input2")
            
            status = "✅" if tags else ("📝" if has_article else "⚠️")
            q_info = ""
            if questions:
                q_info = f", {len(questions)} questions"
            tag_info = f" [{', '.join(tags)}]" if tags else ""
            print(f"   {status} [{i}] {section_type}: article={'YES' if has_article else 'NO'}, questionsText={'YES' if has_questions else 'NO'}{q_info}{tag_info}")
        
        # Stage 4: Build final JSON
        print("\n🏗️  Stage 4: Building final JSON...")
        json_builder = JSONBuilder()
        final_json = json_builder.build(ai_result, sections)
        print(f"   ✓ JSON built successfully")
        print(f"   ✓ Paper: {final_json['paper']['title']}")
        print(f"   ✓ Sections: {len(final_json['sections'])}")
        total = sum(s.get('score', 0) for s in final_json['sections'])
        print(f"   ✓ Total score: {total}")
        
        # Stage 5: Validate final JSON
        print("\n✅ Stage 5: Validating final JSON...")
        validator = FinalJSONValidator(paper_data.md_content)
        is_valid, errors = validator.validate(final_json)
        
        if errors:
            print(f"   ⚠ Found {len(errors)} issues:")
            for error in errors[:10]:
                symbol = "✗" if error.level == "error" else "⚠"
                print(f"      {symbol} {error.code}: {error.message}")
        else:
            print("   ✓ No issues found!")
        
        # Save result
        output_file = OUTPUT_DIR / f"{paper_name}_parsed.json"
        print(f"\n💾 Saving result to: {output_file}")
        
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(final_json, f, ensure_ascii=False, indent=2)
            print(f"   ✓ Saved successfully! ({output_file.stat().st_size} bytes)")
            
            # Show summary
            print("\n" + "=" * 80)
            print("📊 RESULT SUMMARY")
            print("=" * 80)
            
            for i, section in enumerate(final_json['sections']):
                s_type = section.get('type', 'unknown')
                q_start = section.get('questionStart', '?')
                q_end = section.get('questionEnd', '?')
                score = section.get('score', 0)
                
                article = section.get('article', '')
                has_components = any(tag in article for tag in ['<ClozeBlank>', '<Input', '<Blank>'])
                
                status = "✅" if has_components else ("📝" if article else "⚠️")
                print(f"{status} [{i}] {s_type}: Q{q_start}-{q_end} ({score}pts) | article={'YES + components' if has_components else ('YES' if article else 'NO')}")
            
            print("=" * 80)
            print("🎉 Test completed successfully!\n")
            
            return True
            
        except Exception as e:
            print(f"   ✗ Failed to save: {e}")
            raise
    
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    import sys
    
    paper_name = sys.argv[1] if len(sys.argv) > 1 else "2026北京西城高三（上）期末"
    
    success = test_with_existing_response(paper_name)
    
    sys.exit(0 if success else 1)