---
name: Template seeding recovery
description: Reliability rule for starter vending-machine catalog initialization on upgrades.
---

Starter catalog initialization should be idempotent and callable again from an admin entry point, not only during process startup.

**Why:** A persistent JSON file can outlive a deployment, and a startup migration can be skipped or interrupted while the bot still becomes available. An admin opening the relevant management flow needs a safe way to repair missing starter data.

**How to apply:** Seed only absent machine names, never overwrite existing machine configuration, and retry before building the machine-selection options.