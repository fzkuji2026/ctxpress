from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
@dataclass
class FilterContext:
    """
    Filter context - Passed between filters

    Contains information about the current filtering operation
    and allows filters to communicate with each other.
    """

    command: str  # Current command
    has_error: bool  # Whether output contains error
    state: FilterState  # Current state (IDLE/WAITING/POLLING)
    last_command: Optional[str]  # Previous command
    original_length: int  # Original output length

    # Flags that filters can set (affects subsequent logic)
    is_package_install: bool = False  # Detected as package installation
    is_redundant: bool = False  # Output is redundant (e.g., polling)
    suggested_llm_type: Optional[str] = None  # Suggested LLM prompt type
    short_circuit: bool = False  # Skip remaining filters

    # Debug info (populated when debug=True)
    filter_stats: Dict[str, Dict[str, int]] = field(default_factory=dict)

class BaseFilter(ABC):
    """
    Abstract base class for all filters

    To create a new filter:
    1. Inherit from BaseFilter
    2. Set name and priority
    3. Implement filter() method
    4. Optionally override should_skip()
    """

    # Filter name (used for enable/disable control)
    name: str = "base"

    # Priority (lower = executed first)
    priority: int = 50

    # Whether this filter suggests LLM compression after processing
    suggests_llm_compress: bool = False

    # Suggested LLM prompt type (if suggests_llm_compress is True)
    llm_compress_type: Optional[str] = None

    @abstractmethod
    def filter(self, text: str, context: FilterContext) -> str:
        """
        Execute filtering

        Args:
            text: Input text
            context: Filter context (can be modified to pass info to later filters)

        Returns:
            Filtered text
        """
        pass

    def should_skip(self, context: FilterContext) -> bool:
        """
        Whether to skip this filter (subclasses can override)

        Args:
            context: Filter context

        Returns:
            True to skip this filter
        """
        return False
