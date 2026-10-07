"""ACON AppWorld prompt templates, microsoft/acon@d63f9ae.

    MIT License

    Copyright (c) Microsoft Corporation.

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE

"""

SYSTEM = 'You are an agent tasked with extracting and refining a concise and optimized version of the context based on the user instruction and other provided information.'

OBSERVATION = 'Your task is to generate a "Reasoning" and a "Refined Observation" based on the inputs below.\n\nIn the "Reasoning", analyze the user instruction and history to identify what information from the current observation is necessary to complete the remaining steps.  \nThink about what parts can be summarized or transformed to reduce length, while ensuring that future actions can still be executed based on the refined observation alone.\n\nIn the "Refined Observation", include only the information that is minimal but sufficient for the next steps.\n\n[Information source]\n# User Instruction\n{{ task }}\n\n# History of interactions\n{{ history }}\n\n# Observation at the current time step\n{{ observation }}\n\n[Output format]\n# Reasoning\n... your reasoning for what matters and how to optimize it ...\n# Refined Observation\n... reduced and actionable observation ...'

HISTORY = 'You are maintaining a structured context-aware summary for a productivity agent. You will be given the user instruction for the agent, a list of interactions corresponding to actions taken by the agent, and the most recent previous summary if one exists. Produce the following:\n\n### REASONING\nSummarize key progress, decisions made, important observed outcomes, and rationale behind actions taken so far. Include how earlier steps influenced later ones and why certain data is retained in the summary.\n\n### COMPLETED\nList completed subtasks or successful outcomes, with brief results if applicable.\n\n---\n\n## [Information Source]\n\n### USER INSTRUCTION\n\n{{ task }}\n\n## [PREVIOUS SUMMARY] (if any)\n\n{{ prev_summary }}\n\n## [HISTORY OF INTERACTIONS]\n\n{{ history }}\n\n---\n\n## PRIORITIZE\n\n1. Keep all sections relevant and concise.  \n2. Use reusable structured formats when summarizing artifacts.  \n3. Ensure agent can resume task with no loss of information.\n4. Include key info from errors or failed attempts to prevent repeated mistakes.\n5. Preserve all essential artifacts and data needed to complete the task.\n\n---\n\n### [Output Format]\n\nDo **not** include the input or any additional explanation. Only return the formatted summary.'
