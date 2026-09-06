"""
Validation module for exam parser results
"""

import logging
import re
from typing import List, Dict, Any, Tuple, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ValidationError:
    """Represents a validation error"""
    level: str  # "error", "warning"
    code: str
    message: str
    section_index: Optional[int] = None
    question_number: Optional[int] = None


class AIResultValidator:
    """Validate AI results"""
    
    def __init__(self, md_content: str):
        """
        Initialize validator
        
        Args:
            md_content: Full markdown content
        """
        self.md_content = md_content
        self.errors: List[ValidationError] = []
    
    def validate(self, result: Dict[str, Any]) -> Tuple[bool, List[ValidationError]]:
        """
        Validate AI result
        
        Args:
            result: AI result dictionary
            
        Returns:
            Tuple of (is_valid, errors)
        """
        self.errors = []
        
        self._validate_basic_structure(result)
        self._validate_sections(result.get("sections", []))
        self._validate_anchors(result.get("sections", []))
        
        return len([e for e in self.errors if e.level == "error"]) == 0, self.errors
    
    def _validate_basic_structure(self, result: Dict[str, Any]):
        """Check basic result structure"""
        if not isinstance(result, dict):
            self.errors.append(ValidationError("error", "INVALID_TYPE", "Result is not a dictionary"))
            return
        
        if "paper" not in result:
            self.errors.append(ValidationError("error", "MISSING_PAPER", "Missing 'paper' field"))
        
        if "sections" not in result:
            self.errors.append(ValidationError("error", "MISSING_SECTIONS", "Missing 'sections' field"))
        elif not isinstance(result["sections"], list):
            self.errors.append(ValidationError("error", "INVALID_SECTIONS", "'sections' must be a list"))
    
    def _validate_sections(self, sections: List[Dict[str, Any]]):
        """Validate section structure and content"""
        from config import SUPPORTED_TYPES
        
        question_ranges = []
        
        for i, section in enumerate(sections):
            # Check section type
            if "type" not in section:
                self.errors.append(ValidationError(
                    "error", "MISSING_TYPE",
                    "Section missing 'type' field",
                    section_index=i
                ))
                continue
            
            section_type = section["type"]
            if section_type not in SUPPORTED_TYPES:
                self.errors.append(ValidationError(
                    "error", "INVALID_TYPE",
                    f"Unsupported section type: {section_type}",
                    section_index=i
                ))
            
            # Check question range
            if "questionStart" in section and "questionEnd" in section:
                start = section["questionStart"]
                end = section["questionEnd"]
                if start > end:
                    self.errors.append(ValidationError(
                        "error", "INVALID_RANGE",
                        f"questionStart ({start}) > questionEnd ({end})",
                        section_index=i
                    ))
                else:
                    question_ranges.append((start, end, i))
        
        # Check for overlapping questions
        self._check_question_overlap(question_ranges)
        
        # Check for gaps in questions
        self._check_question_continuity(question_ranges)
    
    def _validate_anchors(self, sections: List[Dict[str, Any]]):
        """Validate start anchors in sections"""
        from locator import ContentLocator
        
        locator = ContentLocator(self.md_content)
        
        for i, section in enumerate(sections):
            start = section.get("start")
            if not start:
                continue
            
            is_valid, msg = locator.validate_anchor(start)
            if not is_valid:
                self.errors.append(ValidationError(
                    "error", "INVALID_START_ANCHOR",
                    f"start invalid: {msg}",
                    section_index=i
                ))
    
    def _check_question_overlap(self, ranges: List[Tuple[int, int, int]]):
        """Check for overlapping question ranges"""
        for i, (start1, end1, idx1) in enumerate(ranges):
            for start2, end2, idx2 in ranges[i+1:]:
                # Check overlap
                if not (end1 < start2 or end2 < start1):
                    self.errors.append(ValidationError(
                        "error", "OVERLAPPING_QUESTIONS",
                        f"Section {idx1} ({start1}-{end1}) overlaps with Section {idx2} ({start2}-{end2})"
                    ))
    
    def _check_question_continuity(self, ranges: List[Tuple[int, int, int]]):
        """Check for gaps in question numbering"""
        if not ranges:
            return
        
        ranges = sorted(ranges, key=lambda x: x[0])
        
        for i, (start, end, idx) in enumerate(ranges):
            if i > 0:
                prev_end = ranges[i-1][1]
                if start != prev_end + 1:
                    gap_start = prev_end + 1
                    gap_end = start - 1
                    if gap_start <= gap_end:
                        self.errors.append(ValidationError(
                            "warning", "QUESTION_GAP",
                            f"Questions {gap_start}-{gap_end} missing between sections"
                        ))


class FinalJSONValidator:
    """Validate final JSON output"""
    
    def __init__(self, md_content: str):
        """
        Initialize validator
        
        Args:
            md_content: Full markdown content
        """
        self.md_content = md_content
        self.errors: List[ValidationError] = []
    
    def validate(self, result: Dict[str, Any]) -> Tuple[bool, List[ValidationError]]:
        """
        Validate final JSON
        
        Args:
            result: Final result dictionary
            
        Returns:
            Tuple of (is_valid, errors)
        """
        self.errors = []
        
        self._validate_paper_info(result.get("paper", {}))
        self._validate_sections(result.get("sections", []))
        self._validate_section_counts(result.get("sections", []))
        self._validate_blank_question_consistency(result.get("sections", []))
        self._validate_writing_content(result.get("sections", []), result.get("paper", {}))
        
        return len([e for e in self.errors if e.level == "error"]) == 0, self.errors
    
    def _validate_section_counts(self, sections: List[Dict[str, Any]]):
        """
        Validate section counts like parse-exam.js's validateSections
        Checks that each section type has reasonable question counts
        """
        section_counts = {}
        for sec in sections:
            sec_type = sec.get("type", "")
            if sec_type not in section_counts:
                section_counts[sec_type] = []
            questions = sec.get("questions", [])
            section_counts[sec_type].append({
                "questionCount": len(questions),
                "score": sec.get("score", 0),
            })
        
        section_type_map = {
            "完形填空": "cloze",
            "语法填空": "grammar",
            "阅读": "reading",
            "七选五": "seven",
            "选词填空": "word_choice",
            "阅读表达": "reading_expression",
            "作文": "writing",
        }
        
        for sec_type, entries in section_counts.items():
            mapped = section_type_map.get(sec_type, sec_type)
            
            if mapped == "cloze":
                qc = entries[0]["questionCount"] if entries else 0
                valid_cloze = [10, 12, 15, 20]
                if qc == 0:
                    self.errors.append(ValidationError(
                        "error", "CLOZE_ZERO",
                        "完形填空题目数为0，解析失败"
                    ))
                elif qc not in valid_cloze:
                    self.errors.append(ValidationError(
                        "warning", "CLOZE_COUNT",
                        f"完形填空题目数为{qc}，通常为10/12/15/20题，请核实"
                    ))
            
            elif mapped == "grammar":
                qc = entries[0]["questionCount"] if entries else 0
                if qc == 0:
                    self.errors.append(ValidationError(
                        "error", "GRAMMAR_ZERO",
                        "语法填空题目数为0，解析失败"
                    ))
                elif qc != 10:
                    self.errors.append(ValidationError(
                        "warning", "GRAMMAR_COUNT",
                        f"语法填空题目数为{qc}，通常为10题，请核实"
                    ))
            
            elif mapped == "reading":
                valid_reading = [3, 4, 5]
                if len(entries) < 3 or len(entries) > 4:
                    self.errors.append(ValidationError(
                        "warning", "READING_COUNT",
                        f"阅读篇章数为{len(entries)}，通常为3-4篇，请核实"
                    ))
                for i, entry in enumerate(entries):
                    if entry["questionCount"] == 0:
                        self.errors.append(ValidationError(
                            "error", "READING_ZERO",
                            f"第{i+1}篇阅读题目数为0，解析失败"
                        ))
                    elif entry["questionCount"] not in valid_reading:
                        self.errors.append(ValidationError(
                            "warning", "READING_ARTICLE_COUNT",
                            f"第{i+1}篇阅读题目数为{entry['questionCount']}，通常为3/4/5题，请核实"
                        ))
            
            elif mapped == "seven":
                qc = entries[0]["questionCount"] if entries else 0
                if qc == 0:
                    self.errors.append(ValidationError(
                        "error", "SEVEN_ZERO",
                        "七选五题目数为0，解析失败"
                    ))
                elif qc != 5:
                    self.errors.append(ValidationError(
                        "warning", "SEVEN_COUNT",
                        f"七选五题目数为{qc}，通常为5题，请核实"
                    ))
            
            elif mapped == "reading_expression":
                qc = entries[0]["questionCount"] if entries else 0
                valid_re = [4, 5]
                if qc == 0:
                    self.errors.append(ValidationError(
                        "error", "RE_ZERO",
                        "阅读表达题目数为0，解析失败"
                    ))
                elif qc not in valid_re:
                    self.errors.append(ValidationError(
                        "warning", "RE_COUNT",
                        f"阅读表达题目数为{qc}，通常为4/5题，请核实"
                    ))
            
            elif mapped == "writing":
                qc = entries[0]["questionCount"] if entries else 0
                if qc == 0:
                    self.errors.append(ValidationError(
                        "error", "WRITING_ZERO",
                        "作文题目数为0，解析失败"
                    ))
                elif qc not in [1, 2]:
                    self.errors.append(ValidationError(
                        "warning", "WRITING_COUNT",
                        f"作文题目数为{qc}，通常为1-2题，请核实"
                    ))
        
        # Check for missing expected section types
        expected = ["完形填空", "阅读", "七选五", "作文"]
        for exp_type in expected:
            if exp_type not in section_counts:
                self.errors.append(ValidationError(
                    "warning", "MISSING_SECTION",
                    f"未检测到{exp_type}题型"
                ))
    
    def _validate_blank_question_consistency(self, sections: List[Dict[str, Any]]):
        """
        Validate that for 完形填空/七选五/语法填空, the number of blanks
        (replaced components in article) matches the number of questions.
        """
        check_types = {"完形填空", "七选五", "语法填空"}
        
        for i, section in enumerate(sections):
            sec_type = section.get("type", "")
            if sec_type not in check_types:
                continue
            
            questions = section.get("questions", [])
            question_count = len(questions)
            if question_count == 0:
                continue
            
            article = section.get("article", "")
            if not article:
                continue
            
            cloze_count = article.count("<ClozeBlank>")
            input_count = article.count("<Input>")
            total = cloze_count + input_count
            
            if total > 0 and total != question_count:
                self.errors.append(ValidationError(
                    "error", "ARTICLE_BLANK_MISMATCH",
                    f"{sec_type}: 文章中空白数({total}) ≠ 题目数({question_count})",
                    section_index=i
                ))
    
    def _validate_writing_content(self, sections: List[Dict[str, Any]], paper: Dict[str, Any]):
        """Validate that English writing section content does not start with English words"""
        subject = paper.get("subject", "")
        if subject != "英语":
            return
        
        for i, section in enumerate(sections):
            if section.get("type") != "作文":
                continue
            
            for j, question in enumerate(section.get("questions", [])):
                content = question.get("content", "")
                if not content:
                    continue
                
                stripped = content.strip()
                if re.match(r'^[a-zA-Z]', stripped):
                    self.errors.append(ValidationError(
                        "warning", "WRITING_CONTENT_EN",
                        f"作文第{j+1}题题干以英文开头，疑似未正确解析",
                        section_index=i,
                        question_number=j+1
                    ))
    
    def _validate_paper_info(self, paper: Dict[str, Any]):
        """Validate paper metadata"""
        required_fields = ["title", "subject", "source"]
        for field in required_fields:
            if field not in paper or not paper[field]:
                self.errors.append(ValidationError(
                    "warning", "MISSING_PAPER_FIELD",
                    f"Paper field '{field}' is missing or empty"
                ))
    
    def _validate_sections(self, sections: List[Dict[str, Any]]):
        """Validate section content"""
        from config import SUPPORTED_TYPES
        
        for i, section in enumerate(sections):
            # Validate type
            if section.get("type") not in SUPPORTED_TYPES:
                self.errors.append(ValidationError(
                    "error", "INVALID_TYPE",
                    f"Invalid section type: {section.get('type')}",
                    section_index=i
                ))
            
            # Validate article/question content exists
            if "article" in section:
                if not section["article"] or section["article"].strip() == "":
                    self.errors.append(ValidationError(
                        "warning", "EMPTY_ARTICLE",
                        "Section article is empty",
                        section_index=i
                    ))
            
            # Validate blanks
            for blank in section.get("blanks", []):
                if "id" not in blank:
                    self.errors.append(ValidationError(
                        "error", "INVALID_BLANK",
                        "Blank missing 'id' field",
                        section_index=i
                    ))
            
            # Validate questions
            for question in section.get("questions", []):
                if "id" not in question:
                    self.errors.append(ValidationError(
                        "error", "INVALID_QUESTION",
                        "Question missing 'id' field",
                        section_index=i
                    ))
                if "answer" not in question:
                    self.errors.append(ValidationError(
                        "error", "INVALID_QUESTION",
                        "Question missing 'answer' field",
                        section_index=i
                    ))
            # Validate questions exist
            if not section.get("questions"):
                if section.get("type") != "作文":
                    self.errors.append(ValidationError(
                        "warning", "NO_QUESTIONS",
                        "Section has no questions",
                        section_index=i
                    ))