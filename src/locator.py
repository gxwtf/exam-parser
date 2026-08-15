"""
Locator module for finding and extracting content from MD files using anchors
"""

import re
import logging
from typing import Optional, Tuple, List
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class AnchorRange:
    """Represents a range defined by start and end anchors"""
    start_anchor: Optional[str]
    end_anchor: Optional[str]
    confidence: str = "high"  # high, medium, low


def normalize_for_match(text: str) -> str:
    """
    Normalize text for fuzzy matching.
    
    Handles:
    - Unicode smart quotes → straight quotes
    - Multiple whitespace → single space
    - \\r\\n → \\n
    - Markdown escape chars
    - Leading/trailing whitespace
    - Unicode spaces
    
    NOTE: This is ONLY for finding positions. Content must be sliced from original MD.
    """
    if not text:
        return ""
    
    text = text.strip()
    
    # Normalize line endings
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    
    # Smart quotes → straight quotes
    text = text.replace('\u2018', "'").replace('\u2019', "'")
    text = text.replace('\u201c', '"').replace('\u201d', '"')
    text = text.replace('\u2013', '-').replace('\u2014', '--')
    text = text.replace('\u2026', '...')
    
    # Unicode spaces → normal space
    text = re.sub(r'[\u00a0\u2000-\u200b\u202f\u205f\u3000]', ' ', text)
    
    # Strip Markdown formatting characters for matching
    # (only in normalized text, NEVER in original MD)
    text = text.replace('\\', '')  # Escape chars: \_**\_**\__ → _____
    text = text.replace('*', '')   # Bold markers
    text = text.replace('_', '')   # Italic markers: _bais_ → bais
    
    # Multiple whitespace → single space
    text = re.sub(r'[ \t]+', ' ', text)
    
    # Multiple newlines → single newline (but preserve paragraph breaks)
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    return text.strip()


class ContentLocator:
    """Locate and extract content from markdown using anchors"""
    
    def __init__(self, md_content: str):
        """
        Initialize locator
        
        Args:
            md_content: Full markdown content
        """
        self.md_content = md_content
        self.md_normalized = normalize_for_match(md_content)
        self.md_lines = md_content.split("\n")
    
    def find_anchor(self, anchor: str, start_pos: int = 0) -> Optional[int]:
        """
        Find position of an anchor string in MD content.
        Uses multi-strategy matching: exact → normalized → fuzzy.
        
        Args:
            anchor: Anchor string to find
            start_pos: Starting position for search
            
        Returns:
            Position in md_content, or None if not found
        """
        if not anchor:
            return None
        
        try:
            # Strategy 1: Exact match
            pos = self.md_content.find(anchor, start_pos)
            if pos >= 0:
                return pos
            
            # Strategy 2: Fuzzy match with MD formatting tolerance
            # AI gives plain text, MD may have * and _ formatting
            # Slide through original MD, normalize each window, compare
            anchor_norm = normalize_for_match(anchor)
            if anchor_norm:
                first_char = anchor_norm[0]
                max_expansion = len(anchor_norm) * 3
                pos = start_pos
                while pos < len(self.md_content):
                    pos = self.md_content.find(first_char, pos)
                    if pos < 0:
                        break
                    window = self.md_content[pos:pos + max_expansion]
                    window_norm = normalize_for_match(window)
                    if window_norm.startswith(anchor_norm):
                        logger.debug(f"Anchor found via fuzzy match: {anchor[:50]}... at pos {pos}")
                        return pos
                    pos += 1
            
            # Strategy 3: Try with smart quotes reversed
            anchor_alt = anchor.replace("'", "\u2019").replace("'", "\u2018")
            pos = self.md_content.find(anchor_alt, start_pos)
            if pos >= 0:
                return pos
            
            anchor_alt2 = anchor.replace("\u2019", "'").replace("\u2018", "'")
            if anchor_alt2 != anchor:
                pos = self.md_content.find(anchor_alt2, start_pos)
                if pos >= 0:
                    return pos
            
            # Strategy 4: Try first 40 chars of anchor (if anchor is long enough)
            if len(anchor) > 40:
                prefix = anchor[:40]
                pos = self.md_content.find(prefix, start_pos)
                if pos >= 0:
                    logger.debug(f"Anchor found via prefix match: {prefix}...")
                    return pos
            
            # Strategy 5: Try with alternative option letters (A/B/C/D/E/F/G)
            # AI may say "D. The eagerness..." but MD has "C. The eagerness..."
            m = re.match(r'^([A-G])\.\s', anchor)
            if m:
                anchor_suffix = anchor[m.end():]
                for alt in ['A', 'B', 'C', 'D', 'E', 'F', 'G']:
                    if alt == m.group(1):
                        continue
                    alt_anchor = alt + '. ' + anchor_suffix
                    pos = self.md_content.find(alt_anchor, start_pos)
                    if pos >= 0:
                        logger.debug(f"Anchor found via option letter swap: {m.group(1)}→{alt}")
                        return pos
            
            return None
            
        except Exception as e:
            logger.error(f"Error finding anchor: {e}")
            return None
    
    def extract_by_anchors(self, 
                          start_anchor: Optional[str],
                          end_anchor: Optional[str],
                          include_anchors: bool = False,
                          line_boundary: bool = False) -> Optional[str]:
        """
        Extract content between two anchors.
        Content is sliced from ORIGINAL md_content (not normalized).
        
        Args:
            start_anchor: Anchor at start of content
            end_anchor: Anchor at end of content
            include_anchors: Include anchor strings in result
            line_boundary: Extend to start of line (start) and end of line (end)
            
        Returns:
            Extracted content from original MD, or None if extraction failed
        """
        if not start_anchor or not end_anchor:
            logger.warning("Start or end anchor is None")
            return None
        
        # When start and end anchors are the same (single-line content), 
        # extract the anchor text itself
        if start_anchor == end_anchor:
            return start_anchor if include_anchors else start_anchor
        
        start_pos = self.find_anchor(start_anchor)
        if start_pos is None:
            logger.error(f"Could not find start anchor: {start_anchor[:100]}")
            return None
        
        end_pos = self.find_anchor(end_anchor, start_pos + len(start_anchor))
        if end_pos is None:
            logger.error(f"Could not find end anchor: {end_anchor[:100]}")
            return None
        
        if end_pos < start_pos:
            logger.error(f"End anchor position is before start anchor")
            return None
        
        # Line boundary: extend to start/end of line
        if line_boundary:
            # Extend start backward to beginning of line
            line_start = self.md_content.rfind('\n', 0, start_pos)
            if line_start >= 0:
                start_pos = line_start + 1
            else:
                start_pos = 0
            
            # Extend end forward to end of line
            line_end = self.md_content.find('\n', end_pos + len(end_anchor))
            if line_end >= 0:
                end_pos = line_end
            else:
                end_pos = len(self.md_content)
        
        # Extract from ORIGINAL MD (never normalized)
        if include_anchors:
            end = end_pos if line_boundary else end_pos + len(end_anchor)
            content = self.md_content[start_pos:end]
        else:
            content = self.md_content[start_pos + len(start_anchor):end_pos]
        
        return content.strip()
    
    def find_questions_after_article(self, article_end_pos: int,
                                     question_start: int, question_end: int) -> Optional[str]:
        """
        Find questions section after article, using question numbers to locate.
        
        Scans from article_end_pos forward to find the first question number matching
        question_start and the last matching question_end, then extracts everything between
        (extending to line boundaries).
        
        Args:
            article_end_pos: Position in MD right after the article ends
            question_start: First question number
            question_end: Last question number
            
        Returns:
            Extracted questions text from original MD, or None if not found
        """
        if not question_start or not question_end:
            return None
        
        remaining = self.md_content[article_end_pos:]
        
        # Find the first question number
        start_num = str(question_start)
        start_pattern = re.compile(r'(?:^|\n)\s*' + re.escape(start_num) + r'\\?[.．]\s*')
        start_match = start_pattern.search(remaining)
        if not start_match:
            logger.warning(f"Could not find question {question_start} after article")
            return None
        
        # Find the last question number
        end_num = str(question_end)
        end_pattern = re.compile(r'(?:^|\n)\s*' + re.escape(end_num) + r'\\?[.．]\s*')
        end_match = end_pattern.search(remaining)
        if not end_match:
            logger.warning(f"Could not find question {question_end} after article")
            return None
        
        # Find where the questions end: next question number or section break
        next_q_num = str(question_end + 1)
        next_q_pattern = re.compile(r'(?:^|\n)\s*' + re.escape(next_q_num) + r'\\?[.．]\s*')
        after_end = remaining[end_match.end():]
        next_match = next_q_pattern.search(after_end)
        
        # Also check for section headers
        section_match = re.search(r'(?:^|\n)\s*\*\*', after_end)
        
        # Determine end boundary
        if next_match and section_match:
            boundary = min(next_match.start(), section_match.start())
        elif next_match:
            boundary = next_match.start()
        elif section_match:
            boundary = section_match.start()
        else:
            boundary = len(after_end)
        
        # Calculate absolute positions
        content_start = article_end_pos + start_match.start()
        # Extend to start of line
        line_start = self.md_content.rfind('\n', 0, content_start)
        content_start = line_start + 1 if line_start >= 0 else 0
        
        content_end = article_end_pos + end_match.end() + boundary
        
        questions_text = self.md_content[content_start:content_end].strip()
        logger.debug(f"  Found questions {question_start}-{question_end} after article ({len(questions_text)} chars)")
        return questions_text
    
    def find_seven_choose_five_options(self, article_end_pos: int) -> Optional[str]:
        """
        Find 七选五 options (A-G) after article. Format: A. xxx\nB. xxx\n...
        """
        remaining = self.md_content[article_end_pos:]
        start_match = re.search(r'^A[.．]\s*', remaining, re.MULTILINE)
        if not start_match:
            logger.warning("Could not find 七选五 option A after article")
            return None
        
        content_start = article_end_pos + start_match.start()
        line_start = self.md_content.rfind('\n', 0, content_start)
        content_start = line_start + 1 if line_start >= 0 else 0
        
        after_a = self.md_content[content_start:]
        end_match = re.search(r'^H[.．]\s*', after_a, re.MULTILINE)
        section_match = re.search(r'(?:^|\n)\s*\*\*', after_a)
        
        if end_match and section_match:
            boundary = min(end_match.start(), section_match.start())
        elif end_match:
            boundary = end_match.start()
        elif section_match:
            boundary = section_match.start()
        else:
            boundary = len(after_a)
        
        return self.md_content[content_start:content_start + boundary].strip()

    def find_article_end(self, start_pos: int, question_type: str,
                         question_start: int = None) -> int:
        """
        Auto-detect article end by scanning line-by-line (matching JS parse-exam.js logic).
        
        Returns:
            End position in md_content (exclusive), pointing to the newline before
            the first question/option line or section header.
        """
        remaining = self.md_content[start_pos:]
        lines = remaining.split('\n')
        
        char_pos = 0  # tracks position in `remaining`
        
        if question_type in ("cloze", "reading", "read-and-express", "reading-expression"):
            target_num = question_start or 1
            for i, line in enumerate(lines):
                stripped = line.strip()
                # Cloze: question line has options on same line (1. A. xxx B. xxx)
                if question_type == "cloze":
                    if re.match(rf'^{target_num}\\?[.．]\s*[A-D]\\?[.．]', stripped):
                        return start_pos + char_pos
                else:
                    # Reading/read-and-express: question line is just the number
                    if re.match(rf'^{target_num}\\?[.．]\s*', stripped):
                        return start_pos + char_pos
                # Section header (第二部分, 参考答案, etc.)
                if self._is_section_header(stripped):
                    return start_pos + char_pos
                char_pos += len(line) + 1  # +1 for \n
        
        elif question_type in ("seven-choose-five", "seven-to-five"):
            for i, line in enumerate(lines):
                stripped = line.strip()
                # First option line: A. xxx
                if re.match(r'^A[.．]\s*', stripped):
                    return start_pos + char_pos
                if self._is_section_header(stripped):
                    return start_pos + char_pos
                char_pos += len(line) + 1
        
        elif question_type == "grammar":
            for i, line in enumerate(lines):
                stripped = line.strip()
                if self._is_section_header(stripped):
                    return start_pos + char_pos
                char_pos += len(line) + 1
        
        elif question_type == "word-choice":
            line_end = remaining.find('\n')
            return start_pos + (line_end if line_end >= 0 else len(remaining))
        
        elif question_type == "en-writing":
            for i, line in enumerate(lines):
                stripped = line.strip()
                if re.match(r'^_?Dear\s', stripped):
                    return start_pos + char_pos
                if self._is_section_header(stripped):
                    return start_pos + char_pos
                char_pos += len(line) + 1
        
        # Fallback: return end of content
        return len(self.md_content)

    def _is_section_header(self, line: str) -> bool:
        """Check if a line is a section header (matching JS isSectionHeader logic)."""
        if re.match(r'^参考答案', line):
            return True
        if re.match(r'^\*\*第[一二三ⅠⅡⅢ]卷', line):
            return True
        if re.match(r'^(?:\*\*)?第[一二三四五六七八九十]+部分', line):
            return True
        if re.match(r'^(?:\*\*)?第[一二三]节', line):
            return True
        if re.match(r'^\*\*[IVⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+[.、]\s*(?:完形填空|阅读理解|语法填空|书面表达|选词填空)', line):
            return True
        # Grammar fill section markers: **A**, **B**, **C** on their own line
        if re.match(r'^\*\*[A-G]\*\*$', line):
            return True
        # Standalone single letter on its own line (A, B, C section markers)
        if re.match(r'^[A-G]$', line):
            return True
        return False

    def extract_line_range(self, start_line: int, end_line: int) -> Optional[str]:
        """
        Extract content by line numbers (1-indexed)
        
        Args:
            start_line: Starting line number
            end_line: Ending line number (inclusive)
            
        Returns:
            Extracted content
        """
        try:
            if start_line < 1 or end_line > len(self.md_lines) or start_line > end_line:
                return None
            
            return "\n".join(self.md_lines[start_line-1:end_line])
        except Exception as e:
            logger.error(f"Error extracting line range: {e}")
            return None
    
    def find_anchor_robust(self, anchor: str) -> Optional[int]:
        """
        Find anchor with binary search fallback.
        
        - If not found: binary search from right to find longest prefix that exists
        - If found multiple times: extend from match position in MD until unique
        """
        if not anchor:
            return None
        
        # Try full anchor first
        pos = self.find_anchor(anchor)
        if pos is not None and pos >= 0:
            occurrences = self.find_anchor_occurrences(anchor)
            if len(occurrences) == 1:
                return pos
            # Multiple matches: extend from first match in MD
            extended = self._extend_anchor_in_md(anchor, occurrences[0])
            if extended != anchor:
                logger.debug(f"Anchor extended for uniqueness: {anchor[:30]}... → {extended[:30]}...")
                return occurrences[0]
            # Can't extend, use first occurrence
            logger.warning(f"Anchor matches {len(occurrences)} times, using first: {anchor[:50]}...")
            return occurrences[0]
        
        # Not found: binary search for longest prefix that exists
        lo, hi = 10, len(anchor)
        best_pos = None
        best_len = 0
        
        while lo <= hi:
            mid = (lo + hi) // 2
            prefix = anchor[:mid]
            pos = self.find_anchor(prefix)
            if pos is not None and pos >= 0:
                best_pos = pos
                best_len = mid
                lo = mid + 1
            else:
                hi = mid - 1
        
        if best_pos is not None:
            logger.debug(
                f"Anchor shortened from {len(anchor)} to {best_len} chars: "
                f"{anchor[:best_len]}..."
            )
            return best_pos
        
        return None
    
    def _extend_anchor_in_md(self, anchor: str, pos: int) -> str:
        """Extend anchor by reading more chars from MD at the match position until unique."""
        md = self.md_content
        max_extend = 200
        extended = anchor
        
        for i in range(len(anchor) + 1, min(pos + max_extend, len(md))):
            candidate = md[pos:pos + i]
            if not candidate:
                break
            # Check if this candidate is unique in MD
            first = md.find(candidate)
            second = md.find(candidate, first + 1)
            if second == -1:
                return candidate
            extended = candidate
        
        return extended

    def find_anchor_occurrences(self, anchor: str) -> List[int]:
        positions = []
        start = 0
        while True:
            pos = self.find_anchor(anchor, start)
            if pos is None:
                break
            positions.append(pos)
            start = pos + 1
        return positions
    
    def validate_anchor(self, anchor: str) -> Tuple[bool, str]:
        """
        Validate an anchor for quality
        
        Args:
            anchor: Anchor to validate
            
        Returns:
            Tuple of (is_valid, message)
        """
        if not anchor:
            return False, "Anchor is empty"
        
        if len(anchor) < 10:
            return False, f"Anchor too short ({len(anchor)} chars, min 10)"
        
        if len(anchor) > 200:
            return False, f"Anchor too long ({len(anchor)} chars, max 200)"
        
        occurrences = self.find_anchor_occurrences(anchor)
        if len(occurrences) == 0:
            return False, "Anchor not found in content"
        
        if len(occurrences) > 1:
            return False, f"Anchor appears {len(occurrences)} times (not unique)"
        
        if anchor.startswith("第") or anchor.startswith("题") or anchor.startswith("答案"):
            return False, "Anchor looks like section/question label"
        
        if all(c in "。，、；：,. -" for c in anchor):
            return False, "Anchor contains only punctuation"
        
        return True, "Anchor is valid"