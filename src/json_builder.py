"""
Final JSON builder to construct the output JSON structure
matching the target _ans.json format.
"""

import re
import json
import logging
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class JSONBuilder:
    """Build final exam parser JSON output"""

    def __init__(self):
        self.result = {}

    # ──────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────

    def build(self,
              ai_result: Dict[str, Any],
              sections_with_content: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Build final JSON from AI result and extracted content.

        Args:
            ai_result: AI analysis result
            sections_with_content: Sections with extracted content + blanks replaced

        Returns:
            Final JSON matching _ans.json format
        """
        paper_data = ai_result.get("paper", {})

        source = paper_data.get("source", paper_data.get("title", ""))
        grade = paper_data.get("grade", "")

        self.result = {
            "paper": self._build_paper(paper_data),
            "sections": self._build_sections(sections_with_content, source, grade)
        }
        return self.result

    # ──────────────────────────────────────────────────────────────────
    # Paper
    # ──────────────────────────────────────────────────────────────────

    def _build_paper(self, paper_data: Dict[str, Any]) -> Dict[str, Any]:
        title = paper_data.get("title", "")
        source = paper_data.get("source", title)
        grade = paper_data.get("grade", "")
        year = paper_data.get("year")
        subject = paper_data.get("subject", "英语")

        if isinstance(year, str):
            try:
                year = int(year)
            except ValueError:
                pass

        tags = self._make_tags(subject, grade, source)

        return {
            "title": title,
            "subject": subject,
            "source": source,
            "grade": grade,
            "year": year,
            "totalScore": paper_data.get("totalScore"),
            "duration": paper_data.get("duration"),
            "description": "",
            "tags": tags,
            "analyses": {}
        }

    # ──────────────────────────────────────────────────────────────────
    # Sections
    # ──────────────────────────────────────────────────────────────────

    def _build_sections(self,
                        sections: List[Dict[str, Any]],
                        source: str,
                        grade: str = "") -> List[Dict[str, Any]]:
        # Merge consecutive grammar sections (A/B/C) into one
        sections = self._merge_grammar_sections(sections)
        built = []
        for sec in sections:
            built.append(self._build_one_section(sec, source, grade))
        return built

    def _merge_grammar_sections(self,
                                 sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Merge consecutive grammar sections (A/B/C groups) into one."""
        merged = []
        grammar_buf = []
        
        for sec in sections:
            qtype = sec.get("questionType") or self._type_to_question_type(sec.get("type", ""))
            if qtype == "grammar":
                grammar_buf.append(sec)
            else:
                if grammar_buf:
                    merged.append(self._combine_grammar(grammar_buf))
                    grammar_buf = []
                merged.append(sec)
        
        if grammar_buf:
            merged.append(self._combine_grammar(grammar_buf))
        
        return merged
    
    def _combine_grammar(self,
                          grammar_sections: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Combine multiple grammar sections into one."""
        if len(grammar_sections) == 1:
            sec = grammar_sections[0]
            group = sec.get("group", "")
            if not group:
                title = sec.get("title", "")
                m = re.search(r'([A-C])', title)
                group = m.group(1) if m else "A"
            article = sec.get("article", "")
            if article and not article.startswith(f"## {group}"):
                sec["article"] = f"## {group}\n\n{article}"
            return sec
        
        # Build combined article with "## A\n\n" headers
        article_parts = []
        group_labels = ["A", "B", "C"]
        for i, sec in enumerate(grammar_sections):
            group = sec.get("group", "")
            if not group:
                title = sec.get("title", "")
                m = re.search(r'([A-C])', title)
                if m:
                    group = m.group(1)
                else:
                    group = group_labels[i] if i < len(group_labels) else str(i)
            article = sec.get("article", "")
            article_parts.append(f"## {group}\n\n{article}")
        combined_article = "\n\n".join(article_parts)
        
        # Combine questionsText
        combined_qtext = "\n\n".join(
            sec.get("questionsText", "") for sec in grammar_sections
            if sec.get("questionsText")
        )
        
        # Combine AI questions
        all_ai_questions = []
        for sec in grammar_sections:
            all_ai_questions.extend(sec.get("questions", []))
        
        # Combine blanks
        all_blanks = []
        for sec in grammar_sections:
            all_blanks.extend(sec.get("blanks", []))
        
        first = grammar_sections[0]
        last = grammar_sections[-1]
        
        return {
            **first,
            "title": "语法填空",
            "group": None,
            "article": combined_article,
            "questionsText": combined_qtext,
            "questions": all_ai_questions,
            "blanks": all_blanks,
            "questionStart": first.get("questionStart", 1),
            "questionEnd": last.get("questionEnd", 1),
        }

    def _build_one_section(self,
                           sec: Dict[str, Any],
                           source: str,
                           grade: str = "") -> Dict[str, Any]:
        question_type = sec.get("questionType", "")
        # Normalize legacy AI response names
        if question_type == "seven-to-five":
            question_type = "seven-choose-five"
        # Derive from Chinese type if questionType not provided by AI
        if not question_type:
            question_type = self._type_to_question_type(sec.get("type", ""))
        sec_type = sec.get("type", "")
        title = sec.get("title", "")
        # Force 语法填空/选词填空 title to not have AI-generated suffixes
        if sec_type in ("语法填空", "选词填空"):
            title = sec_type
        grade = sec.get("grade", grade) if grade else sec.get("grade", "")

        # Map questionType to target format
        qt_mapped = self._map_question_type(question_type)
        category = self._map_category(question_type)

        # Merge AI-provided tags with code-generated tags
        ai_tags = sec.get("tags", [])
        code_tags = self._make_tags("英语", grade, source, extra=category)
        merged_tags = self._merge_tags(code_tags, ai_tags)

        article = sec.get("article", "")
        questions_text = sec.get("questionsText", "")

        # For writing, article is the question text itself
        if question_type == "en-writing":
            if article:
                # Strip leading question number like "49\. " or "49. "
                article = re.sub(r'^\d+\\?[.．]\s*', '', article)
            elif questions_text:
                article = questions_text
                article = re.sub(r'^\d+\\?[.．]\s*', '', article)

        # If AI tagged this as "A篇"/"B篇"/"C篇"/"D篇", prepend heading
        article = self._prepend_heading(article, ai_tags)

        # Build questions (AI provides per-question score)
        questions, questions_total = self._build_questions(sec)
        # Use AI-provided section score, fall back to sum of question scores
        section_score = sec.get("score", questions_total) or questions_total

        # Build section base (without questions, to control insertion order)
        result = {
            "type": sec_type,
            "questionType": qt_mapped,
            "title": title,
            "category": category,
            "score": section_score,
            "grade": grade,
            "source": source,
            "tags": merged_tags,
            "article": article,
        }

        # Pass through passageTitle if AI detected one
        passage_title = sec.get("passageTitle", "")
        if passage_title:
            result["passageTitle"] = passage_title

        # For word-choice, rename article→content (word bank)
        if question_type == "word-choice":
            word_bank = sec.get("wordBank", "")
            if word_bank:
                result["content"] = word_bank
            elif article:
                # AI provided article; first line or paragraph is the word bank
                result["content"] = article

        # For seven-to-five, add section-level options before questions
        if question_type == "seven-choose-five" and questions:
            result["options"] = questions[0].get("options", [])

        result["questions"] = questions

        return result

    # ──────────────────────────────────────────────────────────────────
    # Questions
    # ──────────────────────────────────────────────────────────────────

    def _build_questions(self, sec: Dict[str, Any]) -> tuple:
        """Return (questions_list, total_score)"""
        question_type = sec.get("questionType", "")
        if question_type == "seven-to-five":
            question_type = "seven-choose-five"
        if not question_type:
            question_type = self._type_to_question_type(sec.get("type", ""))
        ai_questions = sec.get("questions", [])
        
        if not ai_questions:
            return [], 0

        # Parse question text into individual questions
        questions_text = sec.get("questionsText", "")
        article = sec.get("article", "")

        total_score = 0
        result = []
        
        # For seven-choose-five, extract options from section-level options
        all_options = []
        if question_type == "seven-choose-five":
            options_data = sec.get("options")
            if isinstance(options_data, list):
                if options_data and isinstance(options_data[0], dict):
                    # AI provided options as [{"id": "A", "label": "..."}, ...]
                    all_options = options_data
                else:
                    # AI provided options as flat strings ["A. ...", "B. ..."], convert
                    all_options = self._extract_options("\n".join(options_data), uppercase_id=True)
            elif isinstance(options_data, str) and options_data.strip():
                all_options = self._extract_options(options_data, uppercase_id=True)
        
        for i, ai_q in enumerate(ai_questions):
            q_id = i + 1  # Restart numbering from 1 within each section
            answer = self._normalize_answer(ai_q.get("answer", ""))
            analysis = ai_q.get("analysis", "")
            per_q_score = ai_q.get("score", 0)  # AI provides per-question score

            total_score += per_q_score

            q_content = ""
            options = []

            # For word-choice, AI provides content directly in questions
            if question_type == "word-choice":
                ai_content = ai_q.get("content", "")
                if ai_content:
                    q_content = ai_content
            
            # For cloze and reading, AI provides content and options directly
            if question_type in ("cloze", "reading", "en-reading"):
                q_content = ai_q.get("content", "") if question_type != "cloze" else ""
                ai_opts = ai_q.get("options", [])
                if ai_opts:
                    options = self._normalize_ai_options(ai_opts)
            
            # For writing, content is the question text (article == question)
            if question_type == "en-writing":
                q_content = questions_text or article
                # Strip leading question number like "49\. " or "49. "
                q_content = re.sub(r'^\d+\\?[.．]\s*', '', q_content)
                answer = ""  # Writing has no answer key
            elif question_type == "word-choice":
                # Only parse from questionsText if AI didn't provide content
                if not q_content and questions_text:
                    parsed_questions = self._parse_question_text(questions_text, question_type)
                    if i < len(parsed_questions):
                        q_content = parsed_questions[i].get("content", "")
            elif question_type != "seven-choose-five":
                # 完形填空/阅读的 content/options 已由 AI 提供，不再从 MD 解析
                if question_type not in ("cloze", "reading", "en-reading"):
                    parsed_questions = self._parse_question_text(questions_text, question_type)
                    if i < len(parsed_questions):
                        q_content = parsed_questions[i].get("content", "")
                        options = parsed_questions[i].get("options", [])

            # Build question
            if question_type == "seven-choose-five":
                q_obj = {
                    "id": q_id,
                    "questionType": "choice",
                    "options": all_options,
                    "answer": answer,
                    "analysis": analysis,
                    "score": per_q_score
                }
            else:
                q_obj = {
                    "id": q_id,
                    "questionType": "choice",
                    "content": q_content,
                    "options": options,
                    "answer": answer,
                    "analysis": analysis,
                    "score": per_q_score
                }

            # For grammar, use "input" type, remove content/options
            if question_type == "grammar":
                q_obj["questionType"] = "input"
                q_obj.pop("content", None)
                q_obj.pop("options", None)
            # For reading-expression, use "text" type
            elif question_type == "reading-expression":
                q_obj["questionType"] = "text"
            # For word-choice, use "input2" type
            elif question_type == "word-choice":
                q_obj["questionType"] = "input2"
            # For writing, use "text" type
            elif question_type == "en-writing":
                q_obj["questionType"] = "text"

            result.append(q_obj)

        return result, total_score

    # ──────────────────────────────────────────────────────────────────
    # Question text parsing
    # ──────────────────────────────────────────────────────────────────

    def _parse_question_text(self,
                             text: str,
                             question_type: str) -> List[Dict[str, Any]]:
        """
        Parse question text block into individual questions with options.

        Input format (from MD question text extraction):
            11. What does Odyssey of the Mind value most?
            A. Having innovative solutions.
            B. Getting the right answer.
            C. ...
            D. ...

            12. In Odyssey of the Mind, students will ...
            A. ...
            B. ...
        """
        if not text:
            return []

        questions = []

        if question_type in ("cloze",):
            # Cloze: options are like "2. A. cleaning B. providing C. saving D. filling"
            questions = self._parse_cloze_questions(text)
        elif question_type in ("reading", "en-reading"):
            # Reading: standard numbered questions with A/B/C/D options
            questions = self._parse_reading_questions(text)
        elif question_type == "seven-choose-five":
            questions = self._parse_seven_to_five_questions(text)
        elif question_type == "word-choice":
            questions = self._parse_word_choice_questions(text)
        elif question_type == "grammar":
            questions = self._parse_grammar_questions(text)
        elif question_type == "reading-expression":
            questions = self._parse_reading_expression_questions(text)
        elif question_type == "en-writing":
            questions = self._parse_writing_questions(text)
        else:
            questions = self._parse_reading_questions(text)

        return questions

    def _parse_cloze_questions(self, text: str) -> List[Dict[str, Any]]:
        """
        Parse cloze question text.
        MD format: "1\\. A. at hand B. on time C. in order D. in mind"
        Normal format: "1. A. at hand B. on time C. in order D. in mind"
        """
        questions = []
        # Split by question number: handles "1.", "1．", "1\\." (MD escaped), with optional space
        parts = re.split(r'\n(?=\d+\\?[.．]\s*)', text)
        
        for part in parts:
            if not part.strip():
                continue
            
            # Skip parts that don't start with a question number
            if not re.match(r'\d+\\?[.．]\s*', part.strip()):
                continue
            
            # Remove question number prefix (with or without escape, . or ．)
            # e.g. "1\\. A. at hand..." or "1．A．freer..."
            part_clean = re.sub(r'^\d+\\?[.．]\s*', '', part.strip())
            # Truncate at first blank line to exclude trailing section headers
            part_clean = re.split(r'\n\s*\n', part_clean)[0]
            
            options = self._extract_options(part_clean)
            
            questions.append({
                "content": "",
                "options": options
            })
        
        return questions

    def _parse_reading_questions(self, text: str) -> List[Dict[str, Any]]:
        """
        Parse reading question text.
        Format:
            11. What does Odyssey of the Mind value most?
            A. Having innovative solutions.
            B. Getting the right answer.
            ...
        Also handles MD escaped: 11\\. What does...
        """
        questions = []
        # Split by question number pattern (handles "11.", "11．", "11\\.")
        parts = re.split(r'\n(?=\d+\\?[.．]\s*)', text)

        for part in parts:
            if not part.strip():
                continue

            # Skip parts that don't start with a question number (e.g. article text)
            if not re.match(r'\d+\\?[.．]\s*', part.strip()):
                continue

            lines = part.strip().split('\n')
            if not lines:
                continue

            # First line is question content
            content_line = lines[0].strip()
            # Remove question number prefix (with or without MD escape, . or ．)
            content = re.sub(r'^\d+\\?[.．]\s*', '', content_line)

            # Remaining lines are options; filter out non-option lines
            option_lines = [l for l in lines[1:] if re.match(r'^[A-G]\s*[.．\)]', l.strip())]
            options_text = '\n'.join(option_lines) if option_lines else ""
            options = self._extract_options(options_text)

            questions.append({
                "content": content.strip(),
                "options": options
            })

        return questions

    def _parse_seven_to_five_questions(self, text: str) -> List[Dict[str, Any]]:
        """Parse seven-choose-five article text to identify blanks.
        Returns one entry per blank found in the text."""
        if not text:
            return []
        blanks = re.findall(r'_{2,}(\d+)_{2,}', text)
        return [{"content": "", "options": []} for _ in blanks]

    def _parse_word_choice_questions(self, text: str) -> List[Dict[str, Any]]:
        """Parse word-choice questions: sentences with blanks replaced by <Input2>"""
        return self._parse_grammar_questions(text)

    def _parse_grammar_questions(self, text: str) -> List[Dict[str, Any]]:
        """Grammar questions: no options, just fill-in-the-blank"""
        questions = []
        if not text:
            return questions

        parts = re.split(r'\n(?=\d+\\?[.．]\s*)', text)
        for part in parts:
            if not part.strip():
                continue
            if not re.match(r'\d+\\?[.．]\s*', part.strip()):
                continue
            content = re.sub(r'^\d+\\?[.．]\s*', '', part.strip())
            # Strip trailing answer blank lines (lines of only _, *, \, spaces)
            content = re.sub(r'(\n\s*[\\_*\s]+)+$', '', content)
            questions.append({
                "content": content,
                "options": []
            })
        return questions

    def _parse_reading_expression_questions(self, text: str) -> List[Dict[str, Any]]:
        questions = self._parse_grammar_questions(text)
        for q in questions:
            content = q.get("content", "")
            # Insert writing instruction after "and explain why."
            content = content.replace(
                "and explain why.",
                "and explain why. (**_Write the underlined part directly on the first line of the answer section_**)"
            )
            q["content"] = content
        return questions

    def _parse_writing_questions(self, text: str) -> List[Dict[str, Any]]:
        if not text:
            return [{"content": "", "options": []}]
        return [{"content": text.strip(), "options": []}]

    # ──────────────────────────────────────────────────────────────────
    # Options extraction
    # ──────────────────────────────────────────────────────────────────

    def _extract_options(self, text: str, uppercase_id: bool = False) -> List[Dict[str, str]]:
        """
        Extract options from text.
        Input: "A. at hand B. on time C. in order D. in mind"
        Output: [{"id": "a", "label": "at hand"}, ...]
        """
        options = []
        # Match A. xxx or A.xxx or A) xxx
        pattern = re.compile(r'([A-G])\s*[.．\)]\s*(.+?)(?=\s*[A-G]\s*[.．\)]|\n(?!\s*[A-G]\s*[.．\)])|$)')
        matches = pattern.findall(text)

        for match in matches:
            label = match[1].strip()
            opt_id = match[0].upper() if uppercase_id else match[0].lower()
            # Stop if letter repeats (new section, e.g. 七选五 A-G after reading A-D)
            if any(o["id"] == opt_id for o in options):
                break
            options.append({
                "id": opt_id,
                "label": label
            })

        return options

    # ──────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────

    def _type_to_question_type(self, type_cn: str) -> str:
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

    def _map_question_type(self, qt: str) -> str:
        mapping = {
            "cloze": "cloze",
            "reading": "en-reading",
            "en-reading": "en-reading",
            "seven-choose-five": "seven-choose-five",
            "seven-to-five": "seven-choose-five",  # legacy AI response
            "word-choice": "word-choice",
            "grammar": "grammar",
            "reading-expression": "reading-expression",
            "en-writing": "en-writing",
        }
        return mapping.get(qt, qt)

    def _map_category(self, qt: str) -> str:
        mapping = {
            "cloze": "完形填空",
            "reading": "阅读",
            "en-reading": "阅读",
            "seven-choose-five": "七选五",
            "seven-to-five": "七选五",  # legacy AI response
            "word-choice": "选词填空",
            "grammar": "语法填空",
            "reading-expression": "阅读表达",
            "en-writing": "作文",
        }
        return mapping.get(qt, qt)

    def _make_tags(self, subject: str, grade: str, source: str,
                   extra: str = "") -> List[str]:
        tags = []
        if subject:
            tags.append(subject)
        if grade:
            tags.append(grade)

        # Extract district first (西城/海淀/东城/朝阳/丰台/石景山), then city (北京)
        districts = ["西城", "海淀", "东城", "朝阳", "丰台", "石景山"]
        cities = ["北京"]

        for d in districts:
            if d in source:
                tags.append(d)
                break

        # Extract exam type
        for exam_type in ["期末", "期中", "一模", "二模", "真题"]:
            if exam_type in source:
                tags.append(exam_type)
                break

        # Extract year
        year_match = re.search(r'(\d{4})', source)
        if year_match:
            tags.append(year_match.group(1))

        for c in cities:
            if c in source:
                tags.append(c)
                break

        if extra and extra not in tags:
            tags.append(extra)

        return tags

    def _merge_tags(self,
                    code_tags: List[str],
                    ai_tags: List[str]) -> List[str]:
        """Merge AI-provided tags into code-generated tags, deduplicating."""
        result = list(code_tags)
        for tag in ai_tags:
            tag = str(tag).strip()
            if tag and tag not in result:
                result.append(tag)
        return result

    def _prepend_heading(self,
                         article: str,
                         ai_tags: List[str]) -> str:
        """
        If AI tags contain 'A篇'/'B篇'/'C篇'/'D篇', prepend '# A\\n\\n' to article.
        Also handles 语法填空 groups like 'A'/'B'/'C' (without '篇').
        """
        import re
        if not article:
            return article

        for tag in ai_tags:
            tag = str(tag).strip()
            # Match "A篇", "B篇", "C篇", "D篇"
            m = re.match(r'^([A-D])篇$', tag)
            if m:
                heading = f"# {m.group(1)}\n\n"
                if not article.startswith(heading):
                    article = heading + article
                return article

        return article

    def _normalize_answer(self, answer: str) -> str:
        """Normalize answer: strip whitespace, preserve original case, collapse ## gaps"""
        if not answer:
            return ""
        answer = answer.strip()
        # Normalize "/" separators to "##" (e.g. "replaced/had replaced" → "replaced##had replaced")
        answer = re.sub(r'\s*/\s*', '##', answer)
        # Collapse "a ## b" → "a##b"
        answer = re.sub(r'\s*##\s*', '##', answer)
        # Lowercase single-letter choice answers (A/B/C/D/E/F/G)
        if re.match(r'^[A-FG]+$', answer):
            answer = answer.lower()
        return answer

    def _normalize_ai_options(self, options: list) -> list:
        """Normalize AI-generated options: lowercase option IDs"""
        result = []
        for opt in options:
            if isinstance(opt, dict):
                opt_id = opt.get("id", "")
                if isinstance(opt_id, str):
                    opt["id"] = opt_id.lower()
            result.append(opt)
        return result

    # ──────────────────────────────────────────────────────────────────
    # Output
    # ──────────────────────────────────────────────────────────────────

    def _clean_dict(self, d: Any) -> Any:
        """
        Recursively remove empty values from dict/list:
        - empty string "" → removed
        - empty list [] → removed
        - empty dict {} → removed
        """
        if isinstance(d, dict):
            return {
                k: self._clean_dict(v)
                for k, v in d.items()
                if not (v == "" or v == [] or v == {} or v is None)
            }
        if isinstance(d, list):
            return [self._clean_dict(item) for item in d]
        return d

    def to_json(self) -> str:
        cleaned = self._clean_dict(self.result)
        return json.dumps(cleaned, ensure_ascii=False, indent=2)

    def to_dict(self) -> Dict[str, Any]:
        return self._clean_dict(self.result)

    def save_to_file(self, file_path: str):
        try:
            cleaned = self._clean_dict(self.result)
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(cleaned, f, ensure_ascii=False, indent=2)
            logger.info(f"Saved JSON to {file_path}")
        except Exception as e:
            logger.error(f"Error saving JSON: {e}")
            raise