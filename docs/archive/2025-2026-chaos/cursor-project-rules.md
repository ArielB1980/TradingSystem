# Cursor Project Rules

## Core Principles

### Truth Protocol System
- Always tell the truth and use factual info
- Base answers on verified, credible, and current information. Cite sources clearly when making factual claims
- If information is uncertain or unavailable, explicitly say "I cannot confirm this" instead of guessing
- Never invent data, events, people, studies, or quotes
- Do not speculate or present interpretations without strong supporting evidence, and always identify when you are doing so

**Failsafe Check (before every response):**
- Internally ask yourself, "Is every statement I'm about to provide true, sourced, and transparent"
- If the answer is no, revise until it is yes
- Prioritize accuracy over speed, or creativity
- Provide step-by-step reasoning for complex answers
- Show calculations when giving numbers or statistics
- Be transparent about limitations and confidence levels in every response

### Project Context
- This repository is a **live trading system** (Python, Kraken futures, real capital at risk). Production entrypoint: `python -m src.entrypoints.prod_live` → LiveTrading; deployed via systemd on a Droplet.
- **Architecture and lessons:** See `FORAI.md` (lessons, deploy, components) and `ARCHITECTURE.md` (data flow, key files). Use them when working on execution, risk, live, or deployment.
- **Goal:** Reliable, invariant-preserving trading: signal generation → auction allocation → risk validation → execution → position/TP management. Backend + live loop + risk/execution; no separate frontend app in this repo.

**Important:**
- Follow the Project Rules.
- Do not ask me to confirm file creation or config choices.
- Create any missing files, config, or folders you need.
- If stack or behaviour is unclear, infer from existing code and FORAI/ARCHITECTURE; prefer safe defaults for risk/execution.
- For risk/execution changes: run relevant tests and consider invariants (see `.cursor/rules/risk-execution-safety.mdc`).

## Code Quality Rules

### Rule: Fix Root Cause, Not Band-Aids

**Intent**
Prefer solid, systemic fixes over quick patches that hide symptoms and accrue debt.

**Policy**
Every bug fix must: (a) identify the violated invariant, (b) restore it for all relevant code paths, and (c) prove it via tests.

**Procedure (Solid-Fix Protocol)**
1. Reproduce with a minimal failing test (unit or integration).
2. Localize the fault boundary (bad state/type/race/off-by-one/etc.).
3. Design the smallest change that restores the invariant across paths (not just the failing one).
4. Implement with clear pre/postconditions (types/asserts), explicit errors, and safe defaults.
5. Migrate public APIs carefully: add a compat layer + deprecation only if needed; update all known callers.
6. Test: convert reproduction to regression; add edge/property tests.
7. Document the root cause and rationale in the PR/commit and, if relevant, README/CHANGELOG.

**Do / Don't**
- ✅ Add missing validation and explicit error messages.
- ✅ Prefer deterministic, pure logic in core flows.
- ❌ Don't swallow exceptions or mask issues with retries/timeouts.
- ❌ Don't land "temporary" hacks without an issue link + removal plan.

**Commit template (use/adapt)**
```
fix(core): align FunctionA→FunctionB with canonical signature; restore invariant

Root cause: caller passed 3 args; FunctionB expects 2 (id, opts).
Change: updated all call sites; added safe defaults; improved error messages.
Tests: failing repro now passes; added edge cases.
Notes: FunctionB remains API source of truth; compat not required.
```

### Rule: Function Call / Argument Mismatches (Caller Must Conform)

**Intent**
Keep public APIs stable. If FunctionA calls FunctionB with the wrong number/types of args, fix the caller(s) to match FunctionB's canonical signature.

**Policy**
- FunctionB's signature is the source of truth.
- Do not change FunctionB's parameters unless intentionally evolving the API and updating all call sites in the same change.

**Procedure**
1. Locate FunctionB's canonical signature (implementation/exported type/tests/docs).
2. Update all offending call sites to match (add/remove/reorder/rename or pass an options object).
3. If data is missing at the call site, plumb it from scope or use a named default; document rationale.
4. If multiple competing signatures exist, treat the widest-used, most recent one as canonical; schedule a follow-up refactor to deprecate others.

**Do / Don't**
- ✅ Fix every mismatching call site you touch.
- ✅ Add/extend tests covering the corrected calls and defaults.
- ❌ Don't "silently" modify FunctionB's params to suit one caller.
- ❌ Don't add shims unless you're explicitly creating a temporary compatibility layer with a removal date.

**Examples (concise)**
- TypeScript: `doThing(id: string, opts?: { force?: boolean })` → call as `doThing(user.id, { force: true })`.
- Python: `def load(path: str, *, cache: bool = True)` → call as `load(fp, cache=True)`.

## Development Workflow

### General Workflow
- Always run linters and formatters before committing
- Use proper TypeScript types and interfaces
- Implement proper error boundaries and fallback UI
- Use proper async/await patterns and error handling
- Implement proper loading states and user feedback
- Use proper caching strategies for performance
- Implement proper security headers and CORS policies
- Write clear README files with setup instructions

### AI Tools & Agent Development
- Design for scalability and maintainability from the start
- Implement proper error handling, logging, and monitoring
- Use environment variables for configuration and API keys
- Follow security best practices for AI applications
- Design intuitive user interfaces for complex AI functionality
- Implement proper data validation and sanitization
- Consider ethical implications and bias mitigation
- Document API endpoints and usage patterns clearly

### Web Development Best Practices
- Always implement mobile-first responsive design principles
- Use semantic HTML5 elements and proper accessibility standards (WCAG 2.1)
- Optimize for performance: lazy loading, image optimization, minimal bundle sizes
- Implement progressive enhancement and graceful degradation
- Follow modern CSS practices: Grid, Flexbox, custom properties
- Ensure cross-browser compatibility and test on multiple devices
- Use TypeScript for type safety in JavaScript projects
- Implement proper error handling and user feedback mechanisms

### Code Quality & Standards
- Write self-documenting code with clear variable and function names
- Implement comprehensive error handling and user feedback
- Follow DRY (Don't Repeat Yourself) and SOLID principles
- Write tests for critical functionality
- Use version control best practices with meaningful commit messages
- Optimize for both performance and maintainability
- Consider security implications in all implementations
- Document complex logic and architectural decisions

### Creative Writing Excellence (if applicable)
- Always maintain narrative consistency and character development
- Focus on visual storytelling techniques suitable for screen media
- Emphasize dialogue that sounds natural when spoken aloud
- Consider pacing, scene structure, and three-act format
- Include specific camera directions and visual cues when relevant
- Research genre conventions and audience expectations
- Balance exposition with action and dialogue
- Create compelling character arcs and emotional stakes

## Code Formatting & Citation Rules

### Code References (for existing code)
Use this exact syntax with three required components:
```
startLine:endLine:filepath
// code content here
```

**Required Components:**
1. **startLine**: The starting line number (required)
2. **endLine**: The ending line number (required)
3. **filepath**: The full path to the file (required)

**CRITICAL**: Do NOT add language tags or any other metadata to this format.

**Content Rules:**
- Include at least 1 line of actual code (empty blocks will break the editor)
- You may truncate long sections with comments like `// ... more code ...`
- You may add clarifying comments for readability
- You may show edited versions of the code

### Markdown Code Blocks (for new/proposed code)
Use standard markdown code blocks with ONLY the language tag:
```python
for i in range(10):
    print(i)
```

### Critical Formatting Rules
- Never include line numbers in code content
- NEVER indent the triple backticks (even in lists or nested contexts)
- Use CODE REFERENCES when showing existing code
- Use MARKDOWN CODE BLOCKS for new or proposed code
- NEVER mix formats
- NEVER add language tags to CODE REFERENCES
- ALWAYS include at least 1 line of code in any reference block
