# Repository work rules

- Read README.md, API.md and MEMORY_LIFECYCLE_SPEC.md for current runtime contracts. Discussion notes and handoff receipts are local history, not public specifications.
- Keep only necessary runtime, interface and maintenance documentation in Git. Markdown uses an explicit .gitignore allowlist; discussions, philosophy, plans and coordination receipts remain local.
- Preserve local-only documents when changing Git tracking. Never stage secrets, instance data, logs or source backups.
- Before removing retired code, verify live references and retain an ignored backup. Preserve historical memory/source readers and shared stores used by the current pipeline.
- Verify changed behavior through focused tests and the relevant full/integration checks. Commit related verified cleanup together and push the intended branch.
- Source delivery does not change running services. Respect the user's current local/GitHub-only scope; do not deploy VPS or switch daily App services without authorization.
