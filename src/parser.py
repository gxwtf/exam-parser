"""
Main parser module orchestrating the entire workflow
"""

import logging
import json
import re
from typing import Dict, Any, Optional
from pathlib import Path

from src.loader import PaperLoader, PaperData
from src.ai_client import AIClient
from src.locator import ContentLocator
from src.validator import FinalJSONValidator
from src.json_builder import JSONBuilder

logger = logging.getLogger(__name__)


class ExamParser:
    """Main exam parser orchestrator"""
    
    def __init__(self, api_key: str, model: str = "gpt-4-turbo", base_url: str = None,
                 api_type: str = "openai"):
        """
        Initialize parser
        
        Args:
            api_key: API key
            model: Model name
            base_url: API base URL (optional)
            api_type: "openai" or "anthropic" (default: "openai")
        """
        self.ai_client = AIClient(api_key, model, base_url, api_type)
        self.paper_loader = PaperLoader()
        self.json_builder = JSONBuilder()
        self._last_validation_errors = []
    
    def parse_from_file(self, base_name: str, input_dir: Optional[Path] = None) -> Dict[str, Any]:
        """
        Parse paper from file
        
        Args:
            base_name: Base filename (e.g., "2024北京海淀高一")
            input_dir: Input directory (optional)
        
        Returns:
            Parsed result dictionary
        """
        if input_dir:
            self.paper_loader.input_dir = input_dir
        
        logger.info(f"Parsing: {base_name}")
        
        # Stage 1: Load paper
        paper_data = self._load_paper(base_name)
        
        # Stage 2: Call AI
        ai_result = self._call_ai(paper_data)
        
        # Stage 3: Override paper info from filename (not from AI)
        self._inject_paper_from_filename(paper_data, ai_result)
        
        # Stage 4: Process AI result - extract content for reading types,
        #          use AI article directly for blank types, inject answers/analysis
        sections_with_content = self._process_ai_result(paper_data, ai_result)
        
        # Stage 5: Build final JSON
        final_json = self._build_final_json(ai_result, sections_with_content)
        
        # Stage 6: Validate final JSON
        self._validate_final_json(final_json, paper_data.md_content)
        
        logger.debug("Parsing completed successfully")
        return final_json
    
    def _load_paper(self, base_name: str) -> PaperData:
        """Stage 1: Load MD file"""
        try:
            paper_data = self.paper_loader.load_paper(base_name)
            return paper_data
        except Exception as e:
            logger.error(f"✗ Failed to load paper: {e}")
            raise
    
    def _call_ai(self, paper_data: PaperData) -> Dict[str, Any]:
        """Stage 2: Call AI for analysis"""
        logger.info("Stage 2: Calling AI for analysis...")
        if not paper_data.md_content or not paper_data.md_content.strip():
            raise ValueError(f"MD input is empty for {paper_data.md_path.name}")

        try:
            ai_result = self.ai_client.analyze_paper(
                paper_data.md_content,
                paper_name=paper_data.md_path.stem if paper_data.md_path else "unknown"
            )
            logger.debug("AI analysis completed")
            return ai_result
        except Exception as e:
            logger.error(f"✗ AI analysis failed: {e}")
            raise
    
    def _process_ai_result(self,
                          paper_data: PaperData,
                          ai_result: Dict[str, Any]) -> list:
        """Stage 4: Process AI result - extract content and inject answers/analysis
        
        For 完形填空/七选五/语法填空/选词填空: use AI's article directly
        For 阅读/阅读表达/作文: extract article from MD using start anchor
        """
        locator = ContentLocator(paper_data.md_content)
        sections = ai_result.get("sections", [])
        processed = []
        
        has_article_types = {"完形填空", "七选五", "语法填空", "选词填空"}
        start_types = {"阅读", "阅读表达", "作文"}
        
        for i, section in enumerate(sections):
            section_copy = section.copy()
            section_type = section.get("type", "")
            question_type = section.get("questionType", "")
            if not question_type:
                question_type = self._cn_type_to_qt(section_type)
            
            article = section.get("article", "")
            
            if section_type in has_article_types:
                # AI already provided the complete article with blanks replaced
                if article:
                    # Prepend passage title if exists
                    passage_title = section.get("passageTitle", "")
                    if passage_title and not article.startswith("## "):
                        article = f"## {passage_title}\n\n{article}"
                    section_copy["article"] = article
                    
                    # 完形填空/七选五/语法填空的 content/options 由 AI 直接提供，不再从 MD 提取
                    # 选词填空 content 也由 AI 提供
                else:
                    logger.warning(f"  Section {i} ({section_type}): AI did not provide article")
            
            elif section_type in start_types:
                # Use start anchor to extract article from MD
                start = section.get("start")
                if not start:
                    range_obj = section.get("range", {})
                    start = range_obj.get("start") if isinstance(range_obj, dict) else None
                
                if start:
                    start_pos = locator.find_anchor_robust(start)
                    if start_pos is not None:
                        q_start = section.get("questionStart")
                        q_end = section.get("questionEnd")
                        article_end_pos = locator.find_article_end(start_pos, question_type, q_start)
                        article_text = locator.md_content[start_pos:article_end_pos].strip()
                        if article_text:
                            passage_title = section.get("passageTitle", "")
                            if passage_title:
                                article_text = f"## {passage_title}\n\n{article_text}"
                            section_copy["article"] = article_text
                            # For writing, article IS the question text
                            if question_type == "en-writing":
                                section_copy["questionsText"] = article_text
                        else:
                            logger.warning(f"  Section {i} ({section_type}): empty article extracted")
                        
                        # 阅读/阅读表达的 content/options 由 AI 直接提供，不再从 MD 提取
                    else:
                        logger.warning(f"  Section {i} ({section_type}): start not found: {start[:50]}...")
                else:
                    logger.warning(f"  Section {i} ({section_type}): no start anchor")
                    
                    # Fallback: if AI provided article, use it
                    if article:
                        passage_title = section.get("passageTitle", "")
                        if passage_title and not article.startswith("## "):
                            article = f"## {passage_title}\n\n{article}"
                        section_copy["article"] = article
                        if question_type == "en-writing":
                            section_copy["questionsText"] = article
            
            # Ensure questions have answers and analysis from AI
            self._ensure_questions(section_copy, section_type)
            
            # Clean spacing around blank tags in article
            if section_copy.get("article"):
                section_copy["article"] = self._clean_article_spacing(section_copy["article"])
            
            processed.append(section_copy)
        
        logger.debug("AI result processed")
        return processed
    
    def _ensure_questions(self, section: Dict[str, Any], section_type: str):
        """Ensure section has questions with proper answers and analysis."""
        ai_questions = section.get("questions", [])
        
        static_analysis = {
            "完形填空": "根据上下文语境选择最合适的词汇。",
            "阅读理解": "根据文章内容选择正确答案。",
            "七选五": "根据上下文逻辑选择最佳选项。",
            "语法填空": "",
            "选词填空": "根据句意和单词的适当形式填空。",
            "阅读表达": "根据文章内容回答问题。",
            "作文": "内容要点完整，语言表达准确流畅，结构清晰，词数符合要求。",
        }
        desc = static_analysis.get(section_type, "")
        
        if not ai_questions:
            return
        
        # Fill missing answers and analysis
        for q in ai_questions:
            if not q.get("answer"):
                if section_type == "作文":
                    q["answer"] = ""
                else:
                    q["answer"] = ""
                    logger.debug(f"  [{section_type}] Q{q.get('id')}: no answer from AI")
            if not q.get("analysis"):
                if section_type == "作文":
                    q["analysis"] = f"写作题评分标准：{desc}"
                elif desc:
                    q["analysis"] = f"第{q.get('id')}题解析: {desc}"
                else:
                    q["analysis"] = f"第{q.get('id')}题解析"
    
    def _inject_paper_from_filename(self, paper_data, ai_result: Dict[str, Any]):
        """Override paper info from filename"""
        
        filename = paper_data.md_path.stem if paper_data.md_path else "unknown"
        
        # Parse year
        year_match = re.search(r'(\d{4})', filename)
        year = int(year_match.group(1)) if year_match else None
        
        # Parse subject
        subject = "英语" if "英语" in filename else "未知"
        
        # Parse grade
        grade = ""
        if "高三" in filename:
            grade = "高三"
        elif "高二" in filename:
            grade = "高二"
        elif "高一" in filename:
            grade = "高一"
        
        # Build title/source: remove "英语" and "（教师版）" suffixes
        title = filename.replace("英语", "").replace("（教师版）", "").replace("(教师版)", "").strip()
        
        paper = ai_result.get("paper", {})
        paper["title"] = title
        paper["source"] = title
        paper["subject"] = subject
        if year:
            paper["year"] = year
        if grade:
            paper["grade"] = grade
        ai_result["paper"] = paper
        
        logger.debug(f"  title={title}, subject={subject}, year={year}, grade={grade}")
    
    @staticmethod
    def _clean_article_spacing(article: str) -> str:
        """清理文章里空标签周围的空格

        修复 AI 输出的常见空格错误：
        - <ClozeBlank></ClozeBlank> .  →  <ClozeBlank></ClozeBlank>.
        - .I <ClozeBlank></ClozeBlank>  →  . I <ClozeBlank></ClozeBlank>
        """
        blank_tags = ["ClozeBlank", "Input", "Input2", "Blank"]

        # 1. 删除空标签与标点之间的空格: </ClozeBlank> .  →  </ClozeBlank>.
        for tag in blank_tags:
            article = re.sub(
                rf'</{tag}>\s+([.,;!?])',
                rf'</{tag}>\1',
                article,
            )

        # 2. 标点后紧跟字母时补空格: .I  →  . I
        article = re.sub(r'([.,;!?])([A-Za-z])', r'\1 \2', article)

        return article

    @staticmethod
    def _cn_type_to_qt(type_cn: str) -> str:
        """Derive questionType from Chinese type name."""
        mapping = {
            "完形填空": "cloze",
            "语法填空": "grammar",
            "阅读": "reading",
            "七选五": "seven-choose-five",
            "选词填空": "word-choice",
            "阅读表达": "reading-expression",
            "作文": "en-writing",
        }
        return mapping.get(type_cn, "")
    
    def _build_final_json(self,
                         ai_result: Dict[str, Any],
                         sections: list) -> Dict[str, Any]:
        """Stage 5: Build final JSON"""
        
        # Use JSONBuilder to construct final structure
        final_json = self.json_builder.build(ai_result, sections)
        
        return final_json
    
    def _validate_final_json(self,
                            final_json: Dict[str, Any],
                            md_content: str):
        """Stage 6: Validate final JSON"""
        validator = FinalJSONValidator(md_content)
        is_valid, errors = validator.validate(final_json)

        self._last_validation_errors = errors

        error_count = 0
        warning_count = 0
        for error in errors:
            if error.level == "error":
                logger.error(f"✗ {error.code}: {error.message}")
                error_count += 1
            else:
                warning_count += 1

        if error_count > 0:
            logger.warning(f"⚠ {error_count} errors, {warning_count} warnings")
        elif warning_count > 0:
            logger.info(f"✓ {warning_count} warnings")
    
    def save_result(self, result: Dict[str, Any], output_path: Path):
        """
        Save parsing result to JSON file
        
        Args:
            result: Result dictionary
            output_path: Output file path
        """
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            logger.info(f"✓ Result saved to {output_path}")
        except Exception as e:
            logger.error(f"✗ Failed to save result: {e}")
            raise