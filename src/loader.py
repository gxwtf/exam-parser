"""
Module for loading paper data (MD files)
"""

import re
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


def normalize_paper_name(name: str) -> str:
    """Normalize paper filename: remove spaces between year and Chinese, remove '含答案' suffix"""
    # Remove space between year digits and Chinese characters: 2014 西城 -> 2014西城
    name = re.sub(r'(\d)\s+([\u4e00-\u9fff])', r'\1\2', name)
    # Remove '含答案' suffix
    name = re.sub(r'含答案$', '', name)
    return name


@dataclass
class PaperData:
    """Container for paper content"""
    md_content: str = ""
    md_path: Optional[Path] = None


class PaperLoader:
    """Load and process paper files"""
    
    def __init__(self, input_dir: Optional[Path] = None):
        """
        Initialize loader
        
        Args:
            input_dir: Directory containing input files
        """
        self.input_dir = input_dir
    
    def load_paper(self, base_name: str) -> PaperData:
        """
        Load paper file (MD required)
        
        Args:
            base_name: Base filename without extension (e.g., "2024北京海淀高一")
        
        Returns:
            PaperData object with file contents
            
        Raises:
            FileNotFoundError: If MD file doesn't exist
            ValueError: If files can't be read properly
        """
        if self.input_dir is None:
            raise ValueError("Input directory not set")
        
        md_path = self._find_file(base_name, ".md")
        if not md_path:
            raise FileNotFoundError(
                f"Cannot find MD file for '{base_name}' in {self.input_dir}"
            )
        
        try:
            md_content = self._read_file(md_path)
        except Exception as e:
            logger.error(f"Error reading paper file: {e}")
            raise ValueError(f"Failed to read paper file: {e}")
        
        logger.info(f"Loaded paper: {base_name}")
        logger.debug(f"MD: {len(md_content)} chars")
        
        return PaperData(
            md_content=md_content,
            md_path=md_path
        )
    
    def _find_file(self, base_name: str, extension: str) -> Optional[Path]:
        """
        Find file with given base name and extension
        
        Normalizes the base_name before searching
        """
        if not self.input_dir or not self.input_dir.exists():
            return None
        
        normalized = normalize_paper_name(base_name)
        
        for item in self.input_dir.rglob(f"*{normalized}*"):
            if item.suffix.lower() == extension.lower():
                return item
        
        return None
    
    @staticmethod
    def _read_file(file_path: Path, encoding: str = "utf-8") -> str:
        """
        Read file with error handling
        
        Args:
            file_path: Path to file
            encoding: File encoding (default UTF-8)
            
        Returns:
            File content
        """
        try:
            with open(file_path, "r", encoding=encoding) as f:
                return f.read()
        except UnicodeDecodeError:
            # Try with other common encodings
            for enc in ["gbk", "gb2312", "latin-1"]:
                try:
                    with open(file_path, "r", encoding=enc) as f:
                        logger.info(f"File {file_path} read with encoding {enc}")
                        return f.read()
                except UnicodeDecodeError:
                    continue
            raise ValueError(f"Cannot decode file {file_path}")