"""
AI Client for calling OpenAI / Anthropic API to analyze exam papers
Supports both OpenAI-compatible and Anthropic-compatible APIs via API_TYPE config.
"""

import json
import logging
import re
from typing import Optional, Dict, Any
from openai import OpenAI
import requests

from config import SHOW_AI_DEBUG, MAX_LOGGED_RESPONSE_CHARS, DEBUG_DIR

logger = logging.getLogger(__name__)


class AIClient:
    """Client for calling OpenAI or Anthropic API"""

    def __init__(self, api_key: str, model: str = "gpt-4-turbo", base_url: str = None,
                 api_type: str = "openai"):
        """
        Initialize AI client

        Args:
            api_key: API key
            model: Model name (default: gpt-4-turbo)
            base_url: API base URL (optional)
            api_type: "openai" or "anthropic" (default: "openai")
        """
        if not api_key:
            raise ValueError("API key is required")

        self.api_key = api_key
        self.model = model
        self.base_url = (base_url or "").rstrip("/")
        self.api_type = api_type.lower()

        if self.api_type == "anthropic":
            self._init_anthropic()
        else:
            self._init_openai()

    def _init_openai(self):
        client_kwargs = {"api_key": self.api_key}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        self.client = OpenAI(**client_kwargs)
        logger.info("AI client initialized: OpenAI mode, base_url=%s", self.base_url)

    def _init_anthropic(self):
        self.client = None  # Use requests directly
        logger.info("AI client initialized: Anthropic mode, base_url=%s", self.base_url)

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
                self._save_debug("last_prompt.txt", f"Paper: {paper_name or 'unknown'}\n" + "=" * 80 + "\n" + prompt)

            logger.info(
                "Calling %s API (%s) with md=%d chars, paper=%s",
                self.model,
                self.api_type,
                len(md_content),
                paper_name or "unknown"
            )

            if self.api_type == "anthropic":
                result_text = self._call_anthropic(prompt)
            else:
                result_text = self._call_openai(prompt)

            # Normalize curly/smart quotes to straight quotes
            result_text = self._normalize_quotes(result_text)

            # Log response length
            logger.debug(f"[AI DEBUG] Response length: {len(result_text)} characters")

            if SHOW_AI_DEBUG:
                self._save_debug("last_response_raw.txt",
                    f"Paper: {paper_name or 'unknown'}\n"
                    f"Response Length: {len(result_text)} characters\n"
                    + "=" * 80 + "\n"
                    + "RAW RESPONSE (BEFORE PROCESSING):\n"
                    + "=" * 80 + "\n"
                    + result_text)

            logger.debug(f"API Response: {result_text[:500]}...")

            # Clean up response if it has markdown code fence
            result_text = self._clean_markdown_fence(result_text)

            if SHOW_AI_DEBUG:
                self._save_debug("last_response_processed.txt",
                    f"Paper: {paper_name or 'unknown'}\n"
                    + "=" * 80 + "\n"
                    + "PROCESSED RESPONSE (AFTER MARKDOWN FENCE REMOVAL):\n"
                    + "=" * 80 + "\n"
                    + result_text)

            return self._parse_and_fix_json(result_text)

        except Exception as e:
            logger.error(f"API error ({self.api_type}): {e}")
            raise

    def _call_openai(self, prompt: str) -> str:
        """Call OpenAI-compatible API and return response text"""
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

        return message.content.strip()

    def _call_anthropic(self, prompt: str) -> str:
        """Call Anthropic-compatible API and return response text"""
        url = f"{self.base_url}/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "max_tokens": 32000,
            "system": "You are an expert English exam parser. "
                      "You analyze exam papers and return structured JSON. "
                      "IMPORTANT: Return ONLY valid JSON, no markdown, no explanations.",
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1,
        }

        resp = requests.post(url, headers=headers, json=payload, timeout=180)
        resp.raise_for_status()
        data = resp.json()

        # Anthropic response: content is an array of blocks
        content_blocks = data.get("content", [])
        if not content_blocks:
            stop_reason = data.get("stop_reason", "unknown")
            raise ValueError(
                f"AI returned empty content (stop_reason={stop_reason})"
            )

        # Concatenate all text blocks
        text_parts = []
        for block in content_blocks:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))

        result = "".join(text_parts)
        if not result:
            raise ValueError("AI returned no text content in response")

        return result.strip()

    def _clean_markdown_fence(self, text: str) -> str:
        """Remove markdown code fences from response text"""
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        return text

    def _parse_and_fix_json(self, result_text: str) -> Dict[str, Any]:
        """Parse JSON response, with auto-repair for incomplete JSON"""
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

            logger.debug(f"Bracket count - Braces: {open_braces} open, {close_braces} close; "
                         f"Brackets: {open_brackets} open, {close_brackets} close")

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
                    self._save_debug("last_response_fixed.txt",
                        "FIXED JSON (AUTO-REPAIRED):\n" + "=" * 80 + "\n" + fixed_text)

                return self._straight_to_curly(result)
            except json.JSONDecodeError as e2:
                logger.error(f"Failed to fix JSON: {e2}")
                raise ValueError(f"AI response is not valid JSON and auto-repair failed: {e}")

    @staticmethod
    def _save_debug(filename: str, content: str):
        """Save debug content to file"""
        debug_file = DEBUG_DIR / filename
        try:
            with open(debug_file, 'w', encoding='utf-8') as f:
                f.write(content)
            logger.debug("[AI DEBUG] Saved to %s", debug_file)
        except Exception as e:
            logger.error("[AI DEBUG] Failed to save %s: %s", filename, e)

    @staticmethod
    def _straight_to_curly(data: Any) -> Any:
        """
        Recursively convert straight quotes to curly quotes in all string values.
        - " → " / " (alternating)
        - ' → ' / ' (contracting or alternating)
        """

        def _convert_quotes(text: str) -> str:
            result = []
            dq_open = True
            sq_open = True

            i = 0
            while i < len(text):
                ch = text[i]

                if ch == '"':
                    result.append('\u201c' if dq_open else '\u201d')
                    dq_open = not dq_open
                elif ch == "'":
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
            '\u2018': "'",
            '\u2019': "'",
            '\u201c': '\\"',
            '\u201d': '\\"',
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
        from prompts.extraction_prompt import EXTRACTION_PROMPT_TEMPLATE

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
            if self.api_type == "anthropic":
                return self._test_anthropic_connection()
            else:
                return self._test_openai_connection()
        except Exception as e:
            logger.error(f"API connection test failed: {e}")
            return False

    def _test_openai_connection(self) -> bool:
        """Test OpenAI-compatible API connection"""
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "user", "content": "Hello"}
            ],
            max_tokens=10,
            temperature=0.5
        )
        logger.info("OpenAI API connection test successful")
        return True

    def _test_anthropic_connection(self) -> bool:
        """Test Anthropic-compatible API connection"""
        url = f"{self.base_url}/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "max_tokens": 10,
            "messages": [
                {"role": "user", "content": "Hello"}
            ],
        }

        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        logger.info("Anthropic API connection test successful")
        return True