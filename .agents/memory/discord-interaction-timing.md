---
name: Discord interaction timing
description: The acknowledgement constraint that affects Discord button, select, and modal handlers.
---

Discord requires an interaction's initial response within roughly three seconds. Any handler that may read persistent data, fetch Discord resources, send messages or DMs, or refresh a panel should acknowledge first with a defer and send the result as a follow-up. Modal-opening handlers should open the modal immediately when possible; validate or perform slow work after the modal submission.

**Why:** Slow JSON and Discord operations can otherwise surface to users as “This interaction failed” even when the underlying operation eventually succeeds.

**How to apply:** Review every new View and Modal callback for its first awaited operation. Keep only lightweight validation before the initial response, and ensure exception handlers use an already-acknowledged response path.