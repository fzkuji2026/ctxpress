import re
def has_error(output: str) -> bool:
        """Detect if output contains error messages"""
        error_patterns = [
            r"\bError\b",
            r"\bERROR\b",
            r"\berror:",
            r"\bFailed\b",
            r"\bFAILED\b",
            r"\bfailed\b",
            r"\bException\b",
            r"\bTraceback\b",
            r"command not found",
            r"No such file or directory",
            r"Permission denied",
            r"\bfatal:",
            r"\bE:",  # apt errors
            r"ModuleNotFoundError",
            r"ImportError",
            r"SyntaxError",
            r"NameError",
            r"TypeError",
            r"ValueError",
            r"KeyError",
            r"AttributeError",
            r"RuntimeError",
        ]
        for pattern in error_patterns:
            if re.search(pattern, output):
                return True
        return False
