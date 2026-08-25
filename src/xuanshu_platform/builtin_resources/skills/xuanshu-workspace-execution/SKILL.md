---
name: xuanshu-workspace-execution
description: Use this platform skill whenever a bound Skill contains scripts or an agent must create files in its isolated application workspace.
metadata:
  author: xuanshu
  version: 1.0.0
---

# Application workspace execution

The application has a persistent isolated workspace exposed as
`$XUANSHU_WORKSPACE`. User uploads and all deliverable files belong there.

Complete bound Skill packages are copied into `$XUANSHU_SKILLS_DIR`. The
package roots selected for the current Agent are listed in
`$XUANSHU_SKILL_ROOTS`. Do not import a top-level `scripts` or `references`
package: several bound Skills can contain directories with those same names.
`$XUANSHU_SKILL_MAP` is a JSON object mapping each selected Skill's unique
Python namespace to its complete package root.

Import code through the unique namespace, for example:

```python
from xuanshu_skills.skill_3_official_doc_writer import SKILL_ROOT
from xuanshu_skills.skill_3_official_doc_writer.scripts.generator import build

rules = (SKILL_ROOT / "references" / "format.md").read_text(encoding="utf-8")
```

Read the actual namespace names from `$XUANSHU_SKILL_MAP`; never guess them.
Each namespace exposes `SKILL_ROOT`, so `references/`, `assets/`, templates,
and other non-Python resources remain tied to the correct Skill without any
extra copy. Relative paths in a Skill are resolved from that `SKILL_ROOT`.

Use `execute_skill_script` when starting an existing script from its package
directory is convenient. It is an optional helper: `execute_python` and
`execute_command` may also write new code, call Skill modules, install
dependencies, configure fonts, or combine several resources when the task
requires it. Dependencies are installed into the application's private
`$PIP_TARGET` under `$XUANSHU_RUNTIME_DIR`, which is injected consistently on
every execution.

Before finishing, check the tool result and report only the user-facing result.
The platform publishes files from `$XUANSHU_WORKSPACE` separately, so never
print host filesystem paths or construct Markdown links to local files.
