"""Tool self-evolution: an LLM rewrites the agent's read tools, real runs on top of infinite-context score them.

The evolved object is a *tool policy*, one Python module in ctxpress/methods/readless_policies/evolved/ (format and
checks in policy.py): the commands the agent can run to read code (`python3 -m <module> SUB ...`), what each returns,
how long whole-file prints are shown when they enter the context (view), and the guidance text that tells the agent
about them. ReadLess serves it in front of infinite-context, so every evaluation is our method plus the candidate.

    archive.py   candidates, their parents, scores; parent choice by fitness and novelty
    propose.py   the proposer: Codex rewrites a parent policy from its code, its waste report and the scores
    evaluate.py  eval configs for a candidate (fork screening from checkpoints, or full runs) and their results
    waste.py     per run: bill, score, and how many read tokens were never used afterwards
    loop.py      one generation: choose parents, propose, check, evaluate, record
"""
