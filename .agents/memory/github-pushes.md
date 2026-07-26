---
name: GitHub pushes
description: Reliable GitHub push behavior in this imported-project environment.
---

Use the project’s official GitHub push integration for authenticated pushes when a raw `git push` over HTTPS fails because no username or token is available locally.

**Why:** The imported repository can have a valid GitHub origin but no local Git credential helper; the managed push flow can still authenticate and update the requested branch.

**How to apply:** Confirm the target branch and origin first, avoid force-pushing shared branches, then verify that the local commit and remote branch resolve to the same commit.