"""
Output parser for context zip compression training.

This module parses structured information from model outputs, including
reasoning process, compression level, summary and function calls.
"""

import re
from dataclasses import dataclass
from typing import Optional, Dict, Any, List


@dataclass
class ParsedOutput:
    """Parsed model output"""
    think: str  # Reasoning process
    summary_level: Optional[int]  # Compression level (1-5)
    summary: Optional[str]  # Compressed text
    function_call: Optional[Dict[str, Any]]  # Tool call
    is_valid: bool  # Whether parsing succeeded
    raw_output: str  # Raw output for debugging
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "think": self.think,
            "summary_level": self.summary_level,
            "summary": self.summary,
            "function_call": self.function_call,
            "is_valid": self.is_valid,
            "raw_output": self.raw_output,
        }


def parse_model_output(output: str) -> ParsedOutput:
    """
    Parse structured information from model output.
    
    Parses:
    - <think>...</think>
    - <summary_level>N</summary_level>
    - <summary>...</summary>
    - <function=...>...</function>
    
    Args:
        output: Raw model output string
        
    Returns:
        ParsedOutput object containing extracted information
    """
    if not output or not isinstance(output, str):
        return ParsedOutput(
            think="",
            summary_level=None,
            summary=None,
            function_call=None,
            is_valid=False,
            raw_output=output or "",
        )
    
    # Extract reasoning process
    think_match = re.search(r"<think>(.*?)</think>", output, re.DOTALL)
    think = think_match.group(1).strip() if think_match else ""
    
    # Remove think tags from output for extracting summary_level and summary
    output_without_think = re.sub(r"<think>.*?</think>", "", output, flags=re.DOTALL)

    # Extract compression level (from output without think tags)
    level_match = re.search(r"<summary_level>(.*?)</summary_level>", output_without_think, re.DOTALL)

    summary_level = None

    if level_match:
        level_content = level_match.group(1).strip()
        digit_match = re.search(r"(\d+)", level_content)
        if digit_match:
            try:
                summary_level = int(digit_match.group(1))
                # Validate level is in range 1-5
                if summary_level < 1 or summary_level > 5:
                    summary_level = None
            except ValueError:
                summary_level = None
    
    # Extract summary (from output without think tags)
    summary_match = re.search(r"<summary>(.*?)</summary>", output_without_think, re.DOTALL)
    summary = summary_match.group(1).strip() if summary_match else None
    
    # Extract function call
    function_call = _parse_function_call(output)
    
    # Determine validity
    # Output is valid if it contains at least reasoning or function call
    is_valid = bool(think or function_call)
    
    return ParsedOutput(
        think=think,
        summary_level=summary_level,
        summary=summary,
        function_call=function_call,
        is_valid=is_valid,
        raw_output=output,
    )


def _parse_function_call(text: str) -> Optional[Dict[str, Any]]:
    """
    Parse function call from text.
    
    Format:
    <function=function_name>
    <parameter=param_name>value</parameter>
    </function>
    
    Args:
        text: Text containing function call
        
    Returns:
        Dictionary containing function name and arguments, or None if not found
    """
    # Find function call block
    func_match = re.search(
        r"<function=([^>]+)>\s*(.*?)\s*</function>",
        text,
        re.DOTALL
    )
    
    if not func_match:
        return None
    
    function_name = func_match.group(1).strip()
    params_block = func_match.group(2)
    
    # Extract parameters
    param_matches = re.findall(
        r"<parameter=([^>]+)>(.*?)</parameter>",
        params_block,
        re.DOTALL
    )
    
    arguments = {}
    for param_name, param_value in param_matches:
        arguments[param_name.strip()] = param_value.strip()
    
    return {
        "function": function_name,
        "arguments": arguments,
    }


def extract_compression_info(output: str) -> Dict[str, Any]:
    """
    Extract compression-related information from output.
    
    Args:
        output: Model output string
        
    Returns:
        Dictionary containing compression info:
        - has_compression: bool
        - level: int or None
        - summary: str or None
        - summary_length: int
    """
    parsed = parse_model_output(output)
    
    return {
        "has_compression": parsed.summary is not None,
        "level": parsed.summary_level,
        "summary": parsed.summary,
        "summary_length": len(parsed.summary) if parsed.summary else 0,
        "is_valid": parsed.is_valid,
    }


def validate_compression_output(output: str) -> tuple[bool, str]:
    """
    Validate that output contains required compression information.
    
    Args:
        output: Model output string
        
    Returns:
        (is_valid, error_message) tuple
    """
    parsed = parse_model_output(output)
    
    if not parsed.is_valid:
        return False, "Output does not contain valid reasoning or function call"
    
    if parsed.summary_level is None and parsed.summary is not None:
        return False, "Summary provided but compression level missing"
    
    if parsed.summary_level is not None and parsed.summary is None:
        return False, "Compression level provided but summary missing"
    
    if parsed.summary_level is not None:
        if parsed.summary_level < 1 or parsed.summary_level > 5:
            return False, f"Invalid compression level: {parsed.summary_level} (must be 1-5)"
    
    return True, ""

