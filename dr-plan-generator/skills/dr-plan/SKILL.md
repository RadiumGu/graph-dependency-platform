---
name: dr-plan
description: Generate graph-driven disaster recovery switchover plans. Activate when user mentions DR plan, failover, disaster recovery, 容灾切换, 灾备, 切换演练, RTO/RPO assessment, or SPOF detection.
---

# DR Plan Generator Skill

When activated, read and follow the instructions in:

```
dr-plan-generator/AGENT.md
```

That file contains the complete interactive workflow, CLI reference, and conversation examples.

## Quick Reference

| Command | Purpose |
|---------|---------|
| `python3 main.py assess --scope az --failure apne1-az1` | Impact assessment |
| `python3 main.py plan --scope az --source apne1-az1 --target apne1-az2` | Generate switchover plan |
| `python3 main.py rollback --plan plans/<id>.json` | Generate rollback plan |
| `python3 main.py validate --plan plans/<id>.json` | Validate plan |
| `python3 main.py export-chaos --plan plans/<id>.json --output <dir>` | Export chaos experiments |

Working directory: `<project-root>/dr-plan-generator`
