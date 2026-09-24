"""
AI Client for calling OpenAI API to analyze exam papers
"""

import json
import logging
import re
from typing import Optional, Dict, Any
from openai import OpenAI

from config import SHOW_AI_DEBUG, MAX_LOGGED_RESPONSE_CHARS, DEBUG_DIR

logger = logging.getLogger(__name__)


class AIClient:
    """Client for calling OpenAI API"""
    
    def __init__(self, api_key: str, model: str = "gpt-4-turbo", base_url: str = None):
        """
        Initialize AI client
        
        Args:
            api_key: OpenAI API key
            model: Model name (default: gpt-4-turbo)
            base_url: API base URL (optional)
        """
        if not api_key:
            raise ValueError("API key is required")
        
        self.api_key = api_key
        self.model = model
        
        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
            
        self.client = OpenAI(**client_kwargs)
    
    def analyze_paper(self, md_content: str, paper_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Call AI to analyze exam paper structure

        Args:
            md_content: Markdown content of paper (sent to AI)

        Returns:
            Parsed AI response as dictionary

        Raises:
            ValueError: If AI response is invalid
            Exception: If API call fails
        """
        if not md_content or not md_content.strip():
            raise ValueError(f"MD paper content is empty for {paper_name or 'unknown paper'}")

        try:
            prompt = self._build_extraction_prompt(md_content, paper_name)

            if "{md_content}" in prompt:
                logger.warning("Prompt still contains unresolved content placeholders; check the template formatting.")

            if SHOW_AI_DEBUG:
                # Save prompt to file
                debug_file = DEBUG_DIR / "last_prompt.txt"
                try:
                    with open(debug_file, 'w', encoding='utf-8') as f:
                        f.write(f"Paper: {paper_name or 'unknown'}\n")
                        f.write("=" * 80 + "\n")
                        f.write(prompt)
                    logger.debug("[AI DEBUG] Prompt saved to %s", debug_file)
                except Exception as e:
                    logger.error("[AI DEBUG] Failed to save prompt to file: %s", e)

            logger.info(
                "Calling %s API with md=%d chars, paper=%s",
                self.model,
                len(md_content),
                paper_name or "unknown"
            )
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert English exam parser. "
                                   "You analyze exam papers and return structured JSON. "
                                   "IMPORTANT: Return ONLY valid JSON, no markdown, no explanations."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=0.1,
                max_tokens=32000,
                timeout=120,
                extra_body={"reasoning_effort": "low"}
            )

            message = response.choices[0].message
            finish_reason = response.choices[0].finish_reason

            # Reasoning models may exhaust max_tokens on reasoning and leave content=None
            if message.content is None:
                rt = None
                try:
                    ctd = response.usage.completion_tokens_details
                    rt = getattr(ctd, "reasoning_tokens", None) if ctd else None
                except Exception:
                    pass
                raise ValueError(
                    f"AI returned empty content (finish_reason={finish_reason}, "
                    f"reasoning_tokens={rt}). The reasoning model used up all "
                    f"max_tokens on reasoning. Try lowering reasoning_effort or "
                    f"increasing max_tokens."
                )

            result_text = message.content.strip()
            
            # Normalize curly/smart quotes to straight quotes
            result_text = self._normalize_quotes(result_text)
            
            # Log response length
            logger.debug(f"[AI DEBUG] Response length: {len(result_text)} characters")
            
            # Save original raw response before any processing
            if SHOW_AI_DEBUG:
                # Save raw response to file
                debug_file = DEBUG_DIR / "last_response_raw.txt"
                try:
                    with open(debug_file, 'w', encoding='utf-8') as f:
                        f.write(f"Paper: {paper_name or 'unknown'}\n")
                        f.write(f"Response Length: {len(result_text)} characters\n")
                        f.write("=" * 80 + "\n")
                        f.write("RAW RESPONSE (BEFORE PROCESSING):\n")
                        f.write("=" * 80 + "\n")
                        f.write(result_text)
                    logger.debug("[AI DEBUG] Raw response saved to %s", debug_file)
                except Exception as e:
                    logger.error("[AI DEBUG] Failed to save raw response to file: %s", e)
            
            logger.debug(f"API Response: {result_text[:500]}...")
            
            # Clean up response if it has markdown code fence
            if result_text.startswith("```"):
                result_text = result_text.split("```")[1]
                if result_text.startswith("json"):
                    result_text = result_text[4:]
                result_text = result_text.strip()
                
                # Log processed response
                if SHOW_AI_DEBUG:
                    debug_file = DEBUG_DIR / "last_response_processed.txt"
                    try:
                        with open(debug_file, 'w', encoding='utf-8') as f:
                            f.write(f"Paper: {paper_name or 'unknown'}\n")
                            f.write("=" * 80 + "\n")
                            f.write("PROCESSED RESPONSE (AFTER MARKDOWN FENCE REMOVAL):\n")
                            f.write("=" * 80 + "\n")
                            f.write(result_text)
                        logger.debug("[AI DEBUG] Processed response saved to %s", debug_file)
                    except Exception as e:
                        logger.error("[AI DEBUG] Failed to save processed response to file: %s", e)
            
            # Parse JSON
            try:
                result = json.loads(result_text)
                logger.info("Successfully parsed AI response")
                return self._straight_to_curly(result)
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON in AI response: {e}")
                logger.error(f"Response text preview: {result_text[:1000]}")
                
                # Try to fix incomplete JSON by adding closing brackets
                logger.warning("Attempting to fix incomplete JSON...")
                
                # Fix trailing commas (e.g., "],\n    }" or "},\n  ]")
                fixed_text = re.sub(r',\s*([}\]])', r'\1', result_text)
                had_trailing_comma = (fixed_text != result_text)
                
                # Count open and close brackets (on fixed text)
                open_braces = fixed_text.count('{')
                close_braces = fixed_text.count('}')
                open_brackets = fixed_text.count('[')
                close_brackets = fixed_text.count(']')
                
                logger.debug(f"Bracket count - Braces: {open_braces} open, {close_braces} close; Brackets: {open_brackets} open, {close_brackets} close")
                
                # Try to fix by adding missing closing brackets
                while fixed_text.count('{') > fixed_text.count('}'):
                    fixed_text += '}'
                while fixed_text.count('[') > fixed_text.count(']'):
                    fixed_text += ']'
                
                try:
                    result = json.loads(fixed_text)
                    logger.warning("Successfully fixed and parsed incomplete JSON response!")
                    fixed_details = []
                    if open_braces != close_braces:
                        fixed_details.append(f"{open_braces - close_braces} closing braces")
                    if open_brackets != close_brackets:
                        fixed_details.append(f"{open_brackets - close_brackets} closing brackets")
                    if had_trailing_comma:
                        fixed_details.append("trailing commas")
                    logger.warning(f"Fixed: {', '.join(fixed_details) if fixed_details else 'unknown'}")
                    
                    if SHOW_AI_DEBUG:
                        debug_file = DEBUG_DIR / "last_response_fixed.txt"
                        try:
                            with open(debug_file, 'w', encoding='utf-8') as f:
                                f.write(f"Paper: {paper_name or 'unknown'}\n")
                                f.write("=" * 80 + "\n")
                                f.write("FIXED JSON (AUTO-REPAIRED):\n")
                                f.write("=" * 80 + "\n")
                                f.write(fixed_text)
                            logger.debug("[AI DEBUG] Fixed response saved to %s", debug_file)
                        except Exception as e:
                            logger.error("[AI DEBUG] Failed to save fixed response to file: %s", e)
                    
                    return self._straight_to_curly(result)
                except json.JSONDecodeError as e2:
                    logger.error(f"Failed to fix JSON: {e2}")
                    raise ValueError(f"AI response is not valid JSON and auto-repair failed: {e}")
        
        except Exception as e:
            logger.error(f"OpenAI API error: {e}")
            raise
    
    @staticmethod
    def _straight_to_curly(data: Any) -> Any:
        """
        Recursively convert straight quotes to curly quotes in all string values.
        - " → " / " (alternating)
        - ' → ' / ' (contracting or alternating)
        """

        def _convert_quotes(text: str) -> str:
            result = []
            # Double quote state
            dq_open = True
            # Single quote state
            sq_open = True

            i = 0
            while i < len(text):
                ch = text[i]

                if ch == '"':
                    result.append('\u201c' if dq_open else '\u201d')
                    dq_open = not dq_open
                elif ch == "'":
                    # If left side is alnum, it's an apostrophe (contraction/possessive)
                    # like there's, don't, finalists' → always right single quote
                    prev_char = text[i - 1] if i > 0 else ''
                    if prev_char.isalnum():
                        result.append('\u2019')
                    else:
                        result.append('\u2018' if sq_open else '\u2019')
                        sq_open = not sq_open
                else:
                    result.append(ch)

                i += 1

            return ''.join(result)

        if isinstance(data, dict):
            return {k: AIClient._straight_to_curly(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [AIClient._straight_to_curly(item) for item in data]
        elif isinstance(data, str):
            return _convert_quotes(data)
        else:
            return data

    @staticmethod
    def _normalize_quotes(text: str) -> str:
        """
        Normalize curly/smart quotes to straight quotes.
        AI sometimes generates \u2018\u2019 (curly single quotes) and \u201c\u201d (curly double quotes)
        in anchor text, which won't match the straight quotes in MD/TXT files.
        """
        replacements = {
            '\u2018': "'",    # LEFT SINGLE QUOTATION MARK  →  '
            '\u2019': "'",    # RIGHT SINGLE QUOTATION MARK →  '
            '\u201c': '\\"',  # LEFT DOUBLE QUOTATION MARK  →  \" (escaped for JSON)
            '\u201d': '\\"',  # RIGHT DOUBLE QUOTATION MARK →  \" (escaped for JSON)
        }
        for curly, straight in replacements.items():
            text = text.replace(curly, straight)
        return text

    def _build_extraction_prompt(self, md_content: str, paper_name: Optional[str] = None) -> str:
        """
        Build the extraction prompt for AI

        Args:
            md_content: Markdown content (sent to AI)
            paper_name: Optional paper name for prompt context

        Returns:
            Prompt string with MD content
        """
        # Import here to avoid circular dependency
        from prompts.extraction_prompt import EXTRACTION_PROMPT_TEMPLATE

        # Only send MD to AI - AI reads the full MD with blanks and answers
        return EXTRACTION_PROMPT_TEMPLATE.format(
            md_content=md_content,
            paper_name=paper_name or "未知试卷"
        )
    
    def test_connection(self) -> bool:
        """
        Test API connection
        
        Returns:
            True if connection successful
        """
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "user", "content": "Hello"}
                ],
                max_tokens=10,
                temperature=0.5
            )
            logger.info("API connection test successful")
            return True
        except Exception as e:
            logger.error(f"API connection test failed: {e}")
            return False