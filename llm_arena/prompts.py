"""All prompt templates in one place.

Prompts are written in English (local models follow English instructions most
reliably) and ask the model to answer in the configured language. JSON keys
always stay in English so the parsers work regardless of language.
"""

from __future__ import annotations

LANG = "Always write your answer in {language} (JSON keys stay in English)."

# ------------------------------------------------------------------ arena
ARENA_SYSTEM = (
    "You are a helpful, precise expert assistant taking part in a side-by-side model comparison. "
    "Answer the user's task as well as you can. Be concrete and structured. " + LANG
)

ANALYSIS_PROMPT = """Analyse the following task thoroughly before any solution is built.

TASK:
{task}

Cover: goal, key requirements, constraints, risks, possible approaches (with trade-offs) and your recommended approach."""

# ----------------------------------------------------------------- debate
DEBATE_PROPONENT = (
    "You are the PROPONENT in a structured, good-faith technical debate. You propose and defend a position "
    "with concrete arguments and evidence, but you honestly concede valid criticism and improve your proposal. "
    "The goal is the best joint solution, not winning. " + LANG
)
DEBATE_CRITIC = (
    "You are the CRITIC in a structured, good-faith technical debate. You analyse the proponent's position, "
    "find weaknesses, risks, missing cases and wrong assumptions, and give counter-arguments or alternatives. "
    "Acknowledge strong points honestly. The goal is the best joint solution, not winning. " + LANG
)

DEBATE_JSON_TAIL = """
Finish your answer with a fenced ```json block summarising THIS turn:
{{"claims": ["..."], "objections": ["..."], "concessions": ["..."], "questions": ["..."], "proposals": ["..."]}}
(claims = your arguments, objections = criticisms of the other side, concessions = points you accept,
questions = open questions, proposals = concrete solution ideas). Keep each item one sentence."""

DEBATE_PHASES = {
    "position": "Round {round}. Present your position on the topic: your proposal, main arguments and supporting evidence.",
    "critique": "Round {round}. Analyse and criticise the proponent's position. Identify weaknesses, risks, "
                "wrong assumptions and give counter-arguments or better alternatives.",
    "rebuttal": "Round {round}. Respond to the critic's objections one by one: defend, concede or refine. "
                "Present your improved proposal.",
    "counter": "Round {round}. Respond to the proponent's new arguments and refinements. Which objections are "
               "resolved, which remain, and what is still missing?",
}

DEBATE_TURN = """DEBATE TOPIC:
{topic}

CURRENT STRUCTURED STATE:
{state}

TRANSCRIPT SO FAR:
{transcript}

YOUR TASK NOW:
{instruction}
""" + DEBATE_JSON_TAIL

DEBATE_SYNTHESIS = """You are the neutral moderator of a finished debate. Summarise it faithfully and fairly.

TOPIC:
{topic}

STRUCTURED STATE (arguments, objections, concessions, questions collected during the debate):
{state}

TRANSCRIPT:
{transcript}

Return ONLY a JSON object:
{{
 "arguments_summary": ["main arguments of the proponent"],
 "counterarguments_summary": ["main counter-arguments of the critic"],
 "facts": ["facts / points both sides agree on"],
 "disputed": [{{"point": "...", "proponent": "position of A", "critic": "position of B"}}],
 "solutions": ["concrete solution options, including combined ones"],
 "open_questions": ["unresolved questions"],
 "conclusion": "a joint conclusion that combines the best ideas of both sides"
}}
""" + LANG

DEBATE_SYNTHESIS_REVIEW = """Another model (the moderator) produced the following debate summary. You took part in the debate.
Check it for missing or distorted points.

TOPIC:
{topic}

SUMMARY:
{summary}

Return ONLY a JSON object:
{{"agree": true, "corrections": ["..."], "missing_facts": ["..."], "missing_disputed": ["..."],
 "additional_solutions": ["..."], "additional_questions": ["..."], "conclusion_comment": "..."}}
""" + LANG

# -------------------------------------------------------------- consensus
CRITERIA = ["correctness", "completeness", "feasibility", "security", "performance", "testability"]
CRITERIA_HU = {
    "correctness": "Helyesség", "completeness": "Teljesség", "feasibility": "Műszaki megvalósíthatóság",
    "security": "Biztonság", "performance": "Teljesítmény", "testability": "Tesztelhetőség",
}

CONSENSUS_SYSTEM = "You are a fair, rigorous technical evaluator. You judge content, not style. " + LANG

CONSENSUS_EVAL = """Two candidate solutions (anonymous) were produced for the same task. Evaluate BOTH on every criterion
with an integer score 1-10 and identify the best, reusable ideas of each. This is NOT a winner/loser contest:
the goal is to build a combined solution from the best parts.

TASK:
{task}

=== SOLUTION 1 ===
{sol1}

=== SOLUTION 2 ===
{sol2}

Criteria: correctness, completeness, feasibility (technical feasibility), security, performance, testability.

Return ONLY a JSON object:
{{
 "scores": {{"1": {{"correctness": 0, "completeness": 0, "feasibility": 0, "security": 0, "performance": 0, "testability": 0}},
            "2": {{"correctness": 0, "completeness": 0, "feasibility": 0, "security": 0, "performance": 0, "testability": 0}}}},
 "strengths": {{"1": ["..."], "2": ["..."]}},
 "weaknesses": {{"1": ["..."], "2": ["..."]}},
 "best_ideas": [{{"from": "1", "idea": "..."}}, {{"from": "2", "idea": "..."}}],
 "conflicts": ["points where the two solutions contradict each other and how to resolve them"]
}}
"""

CONSENSUS_MERGE = """Build ONE combined solution for the task from two candidate solutions, using the evaluation below.
Take the best usable ideas of both, fix the weaknesses listed, resolve conflicts explicitly.

TASK:
{task}

=== SOLUTION A ===
{sol_a}

=== SOLUTION B ===
{sol_b}

=== JOINT EVALUATION ===
Criterion leaders: {leaders}
Best ideas to include:
{ideas}
Weaknesses to avoid:
{weaknesses}
Conflicts to resolve:
{conflicts}

Write the complete combined solution. At the end add a short section "Contributions" listing which ideas came from A and which from B."""

CONSENSUS_REVIEW = """Review this combined solution produced from your and another model's proposals.

TASK:
{task}

COMBINED SOLUTION:
{merged}

Return ONLY a JSON object: {{"approved": true, "remaining_issues": ["..."], "improvements": ["..."]}}
""" + LANG

CONSENSUS_REVISE = """Revise the combined solution to address the reviewer's remaining issues. Output the full revised solution.

COMBINED SOLUTION:
{merged}

REVIEWER ISSUES:
{issues}

SUGGESTED IMPROVEMENTS:
{improvements}"""

# ----------------------------------------------------------------- design
DEV_SYSTEM = (
    "You are the PRIMARY DEVELOPER in a two-model software team. You design and implement clean, correct, "
    "secure, well-structured {target} code. You take the reviewer's feedback seriously. " + LANG
)
REVIEWER_SYSTEM = (
    "You are the REVIEWER / AUDITOR in a two-model software team. You check logic errors, security problems, "
    "performance problems, incorrect API usage, edge cases, missing tests and maintainability. "
    "Be specific (file, function, line) and constructive. " + LANG
)

CODE_RULES = """Code rules:
- Language: {target}. Use only the standard library unless the requirements explicitly allow otherwise.
- Output every file as a fenced block preceded by a header line exactly like:
FILE: path/name.py
```python
...complete file content...
```
- Always output COMPLETE file contents, never diffs or placeholders like "...".
- Keep the program importable: no code with side effects at import time (use `if __name__ == "__main__":`)."""

DESIGN_STAGES = [
    # key, label (hu), actor, prompt
    ("requirements", "1. Követelmények elemzése", "dev",
     """Analyse the requirements of the program below.

REQUIREMENTS / TASK:
{requirements}

Describe the goal, functional and non-functional requirements, constraints and assumptions.
End with a fenced ```json block: {{"functional": ["..."], "non_functional": ["..."], "constraints": ["..."], "assumptions": ["..."]}}"""),
    ("missing_requirements", "2. Hiányzó követelmények", "rev",
     """The developer analysed the requirements. Identify missing, ambiguous or contradictory requirements and risks.

ORIGINAL TASK:
{requirements}

DEVELOPER'S ANALYSIS:
{prev_requirements}

End with a fenced ```json block: {{"missing": ["..."], "ambiguities": ["..."], "risks": ["..."], "suggested_additions": ["..."]}}"""),
    ("architecture", "3. Architektúra", "dev",
     """Design the architecture of the program.

TASK:
{requirements}

REQUIREMENTS ANALYSIS:
{prev_requirements}

REVIEWER'S NOTES ON MISSING REQUIREMENTS:
{prev_missing_requirements}

Describe the architectural style, layers/components with responsibilities, data flow and error handling strategy.
End with a fenced ```json block: {{"style": "...", "components": [{{"name": "...", "responsibility": "..."}}], "data_flow": "...", "decisions": ["..."]}}"""),
    ("modules", "4. Modulok és komponensek", "dev",
     """Based on the architecture, define the concrete modules/files and components with their public interfaces.

TASK:
{requirements}

ARCHITECTURE:
{prev_architecture}

End with a fenced ```json block: {{"modules": [{{"file": "name.py", "responsibility": "...", "public_api": ["function(signature) -> type"]}}]}}"""),
    ("data_structures", "5. Adatstruktúrák", "dev",
     """Design the data structures (classes, records, schemas, invariants) used by the modules.

MODULES:
{prev_modules}

End with a fenced ```json block: {{"data_structures": [{{"name": "...", "fields": ["name: type"], "invariants": ["..."]}}]}}"""),
    ("algorithms", "6. Algoritmusok", "dev",
     """Design the key algorithms: steps, complexity (time/space), edge cases and failure handling.

TASK:
{requirements}

MODULES:
{prev_modules}

DATA STRUCTURES:
{prev_data_structures}

End with a fenced ```json block: {{"algorithms": [{{"name": "...", "steps": ["..."], "complexity": "...", "edge_cases": ["..."]}}]}}"""),
    ("design_review", "6b. Tervezési review", "rev",
     """Review the complete design before implementation starts.

TASK:
{requirements}

ARCHITECTURE:
{prev_architecture}

MODULES:
{prev_modules}

DATA STRUCTURES:
{prev_data_structures}

ALGORITHMS:
{prev_algorithms}

List concrete problems and improvements (logic, security, performance, edge cases, testability, maintainability).
End with a fenced ```json block: {{"issues": [{{"category": "...", "severity": "high|medium|low", "description": "...", "suggestion": "..."}}], "verdict": "ok|needs_changes"}}"""),
    ("code", "7. Kód elkészítése", "dev",
     """Implement the program according to the design.

TASK:
{requirements}

MODULES:
{prev_modules}

DATA STRUCTURES:
{prev_data_structures}

ALGORITHMS:
{prev_algorithms}

DESIGN REVIEW TO ADDRESS:
{prev_design_review}

{code_rules}
Do NOT write tests yet. After the files, add a short note on how to run the program."""),
    ("code_review", "8. Kódellenőrzés", "rev",
     """Review the code thoroughly. Check: logic errors, security problems, performance problems, incorrect API usage,
edge cases, missing tests, maintainability.

TASK:
{requirements}

CODE:
{code}

End with a fenced ```json block:
{{"issues": [{{"id": "R1", "category": "logic|security|performance|api_misuse|edge_cases|missing_tests|maintainability",
 "severity": "critical|high|medium|low", "location": "file:function", "description": "...", "suggestion": "..."}}],
 "summary": "...", "verdict": "approve|needs_changes"}}"""),
    ("tests", "9. Tesztek készítése", "both", ""),
    ("fix", "10. Hibajavítás", "dev",
     """Fix the problems found in code review. Address every critical/high/medium issue; low issues when cheap.

TASK:
{requirements}

CURRENT CODE:
{code}

REVIEW ISSUES:
{review_issues}

{code_rules}
Output only the files you changed (complete contents). Then list for each issue id what you did."""),
    ("final", "11. Végleges verzió", "both", ""),
]

DESIGN_ARCH_ALT = """Independently propose an architecture for this program (another model does the same; the best ideas will be merged).

TASK:
{requirements}

REQUIREMENTS ANALYSIS:
{prev_requirements}

MISSING REQUIREMENTS NOTES:
{prev_missing_requirements}

Describe style, components with responsibilities, data flow, error handling, key decisions."""

FINAL_README = """Write the final README.md for the program: purpose, features, file structure, how to run, how to run tests,
known limitations. Keep it concise.

TASK:
{requirements}

FILES:
{file_list}

TEST RESULTS:
{test_summary}

Output ONLY the README content as markdown (no surrounding code fence)."""

FINAL_AUDIT = """Perform the final audit of the finished program.

TASK:
{requirements}

FINAL CODE:
{code}

TEST RESULTS:
{test_summary}

OPEN REVIEW ISSUES:
{open_issues}

Return ONLY a JSON object:
{{"approved": true, "scores": {{"correctness": 0, "completeness": 0, "feasibility": 0, "security": 0, "performance": 0, "testability": 0}},
 "remaining_issues": ["..."], "strengths": ["..."], "recommendations": ["..."]}}
""" + LANG

# ---------------------------------------------------------------- testing
TESTS_DEV = """Write automated tests for the program below using Python's `unittest` module (tests must run with the standard library only).
Your part: UNIT tests and INTEGRATION tests.

TASK:
{requirements}

CODE:
{code}

Rules:
- Put unit tests in `test_unit.py` and integration tests in `test_integration.py`.
- Import the code under test from its module files (they are in the same directory).
- Tests must be deterministic, fast, no network, no user input, no sleeping.
- Assert concrete expected values that follow from the requirements.
Output each file as:
FILE: test_unit.py
```python
...
```"""

TESTS_REV = """Write automated tests for the program below using Python's `unittest` module (standard library only).
Your part as the reviewer: EDGE-CASE tests, ERROR-HANDLING tests and — only where meaningful — PERFORMANCE tests.

TASK:
{requirements}

CODE:
{code}

Rules:
- Files: `test_edge_cases.py`, `test_error_handling.py`, and optionally `test_performance.py` (generous time limits,
  e.g. assert an operation on a realistic input completes within a few seconds). Omit the performance file if it makes no sense.
- Import the code under test from its module files (same directory). Deterministic, no network, no user input.
Output each file as:
FILE: test_edge_cases.py
```python
...
```"""

TEST_ANALYSIS = """Automated tests failed. Analyse each failure and decide whether the CODE or the TEST is wrong
(tests can be wrong too – judge against the requirements).

TASK:
{requirements}

CODE AND TESTS:
{code}

FAILURES:
{failures}

Return ONLY a JSON object:
{{"failures": [{{"test": "test id", "root_cause": "...", "fix_target": "code|test", "location": "file:function", "suggestion": "..."}}],
 "summary": "..."}}
""" + LANG

TEST_FIX = """Fix the failing tests. Follow the analysis: change the CODE when the code is wrong, change the TEST only when the test
itself contradicts the requirements. Never delete a test just to make it pass.

TASK:
{requirements}

CURRENT FILES:
{code}

FAILURES:
{failures}

ANALYSIS:
{analysis}

{code_rules}
Output only the files you changed (complete contents), then a short list of what you fixed."""

JSON_REPAIR = """Your previous answer should have contained a valid JSON object but it could not be parsed.
Re-send ONLY the JSON object (no explanation, no markdown fence), keeping the same content.

PREVIOUS ANSWER:
{answer}"""

# --------------------------------------------------------------- pipeline
PIPELINE_TOPIC = """What is the best way to solve the following task? Debate the proposed approach.

TASK:
{task}

JOINTLY PROPOSED APPROACH (from the analysis phase):
{approach}"""

FINAL_REPORT = """Write the final report of this multi-model engineering session: what was asked, the key decisions (from the debate),
the architecture, the implementation, the test results, the remaining issues and recommended next steps.

TASK:
{task}

DEBATE CONCLUSION:
{debate}

DESIGN SUMMARY:
{design}

TEST SUMMARY:
{tests}

FINAL AUDIT:
{audit}

Write it as a concise, well-structured markdown document."""
