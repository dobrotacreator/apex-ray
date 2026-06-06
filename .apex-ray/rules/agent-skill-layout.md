---
id: agent-skill-layout
title: Keep agent skill installation paths current
severity: high
mode: strict
paths:
  - "src/apex_ray/config.py"
  - "tests/test_config.py"
  - "tests/test_cli.py"
  - "docs/**"
  - ".agents/**"
  - ".claude/**"
  - ".apex-ray/skills/**"
triggers:
  text:
    - .agents
    - .codex
    - skills
    - agent_files
    - Codex
    - Claude
---
Codex repository-scoped skills belong under `.agents/skills/<skill>/SKILL.md`.
Do not restore `.codex/skills` as the Codex project skill location.

Keep `.apex-ray/skills/<skill>/SKILL.md` as the canonical Apex Ray-generated skill source, and write agent-specific aliases from there:

- Codex aliases: `.agents/skills/<skill>/SKILL.md`
- Claude aliases: `.claude/skills/<skill>/SKILL.md`

`AGENTS.md` remains the project instruction file for Codex guidance. `.codex/` is not an Apex Ray-owned repository skill directory and should not be created by `apex-ray init` for Codex skills.
