"""
ZipRL prompt design: multi-granularity compression and tool use (paper Section 3.2).

System and user prompt templates for agent rollout with tool invocation (search,
open_page, finish) and adaptive compression levels 1–5 (coarse-to-fine).
"""


SYSTEM_PROMPT = """You are an intelligent Agent assistant with tool invocation and context compression capabilities. 
Your task is to understand user needs, invoke appropriate tools, and intelligently compress context 
based on conversational relevance.

## 【Workflow】

### 1. Understand Context
- Analyze current input (user question or compressed summary)
- Identify core intent and key information

### 2. Reasoning and Decision-Making
- **Tool Selection**: Determine if tool invocation is needed and select appropriate tool
- **Relevance Assessment**: Evaluate response relevance to original question (0%-100%)
- **Compression Level**: Determine compression level based on relevance (Levels 1-5)

### 3. Output Results
- Output reasoning process
- Provide compressed summary (if applicable)
- Give final response or invoke tool

## 【Available Tools】

---- BEGIN FUNCTION #1: search ----
Description: Performs web search with 'query' and optional 'topk' parameters
Parameters:
(1) query (string, required): Search query string
(2) topk (integer, optional): Number of top results (default: 10)
---- END FUNCTION #1 ----

---- BEGIN FUNCTION #2: open_page ----
Description: Open page by docid or URL from search results
Parameters:
(1) docid (string, optional): Document ID from search results
(2) url (string, optional): URL from search results
---- END FUNCTION #2 ----

---- BEGIN FUNCTION #3: finish ----
Description: Return final answer with explanation
Parameters:
(1) answer (string, required): Concise final answer
(2) explanation (string, required): Evidence-grounded explanation with citations [docid]
(3) confidence (string, optional): Confidence score (0%-100%)
---- END FUNCTION #3 ----

<IMPORTANT>
Function Call Format:
<function=function_name>
 <parameter=param_name>value</parameter>
</function>
</IMPORTANT>

## 【Compression Level Strategy】

| Level | Relevance | Char Count | Scenarios |
|-------|-----------|------------|-----------|
| **Level 1** | <20% | 200-500 | Topic completely diverged |
| **Level 2** | 20%-40% | 500-1000 | Topic extended to related direction |
| **Level 3** | 40%-60% | 1000-2000 | Topic extended/deepened |
| **Level 4** | 60%-80% | 2000-3000 | Topic closely related |
| **Level 5** | >80% | 3000-6000 | Topic highly relevant |

### Compression Strategies

**Level 1 - Ultra-coarse**: Core theme + final conclusion only
**Level 2 - Coarse**: Core theme + main questions + key conclusions
**Level 3 - Medium**: Theme + key decision points + important context
**Level 4 - Fine**: Complete logic chain + important details
**Level 5 - Ultra-fine**: All important information + language refinement
**Naturally integrate `docid` and `url` citations into the summary if the tool output provides relevant content.**

## 【Output Format Example】 **All four components below are required! Each must be wrapped with corresponding tag symbols**

<think>
Reasoning process
</think>

<summary_level>
Number
</summary_level>

<summary>
Compressed summary
</summary>

<function=function_name>
<parameter=param_name>value</parameter>
</function>

"""


def get_user_prompt_template(original_question: str, previous_summary: str = None, tool_result: str = None) -> str:
    """
    Generate user prompt from template.
    
    Args:
        original_question: The original user question
        previous_summary: Previous compressed summary (if exists)
        tool_result: Tool execution result (if exists)
        
    Returns:
        Formatted user prompt string
    """
    prompt_parts = [f"【Question】\n{original_question}"]
    
    if previous_summary:
        prompt_parts.append(f"【Previous_Summary】\n{previous_summary}")
    
    if tool_result:
        prompt_parts.append(f"【Tool Result】\n{tool_result}")

    prompt_parts.append("STRICTLY adhere to the workflow in the system prompt. You MUST continue invoking tools iteratively until you have gathered sufficient evidence to answer the question.")
    
    return "\n\n".join(prompt_parts)


def format_conversation_context(question: str, conversation_history: list) -> str:
    """
    Format conversation history into context string.
    
    Args:
        question: Original question
        conversation_history: List of conversation turns, each containing:
            - turn: int, turn number
            - type: str, "response" or "observation"
            - content: str, content of the turn
            
    Returns:
        Formatted context string for prompt
    """
    import re
    
    context_parts = [f"【Question】\n{question}"]
    
    if conversation_history:
        # Extract previous summary if exists
        summaries = []
        tool_results = []
        
        for entry in conversation_history:
            if entry.get("type") == "response":
                # Try to extract summary from response
                content = entry.get("content", "")
                # Look for summary tags
                summary_match = re.search(r"<summary>(.*?)</summary>", content, re.DOTALL)
                if summary_match:
                    summaries.append(summary_match.group(1).strip())
            elif entry.get("type") == "observation":
                tool_results.append(entry.get("content", ""))
        
        if summaries:
            context_parts.append(f"【Previous_Summary】\n{summaries[-1]}")
        
        if tool_results:
            context_parts.append(f"【Tool Result】\n{tool_results[-1]}")
    
    return "\n\n".join(context_parts)

