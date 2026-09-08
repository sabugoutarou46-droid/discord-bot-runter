---
name: Confirmation-time inventory
description: Compatibility rule for changing finite inventory from order-time reservation to confirmation-time consumption.
---

When changing inventory consumption timing, persisted orders need an explicit state that distinguishes legacy orders whose inventory was already removed from new orders that only reserve delivery contents until confirmation.

**Why:** Treating old pending orders as new reservations can double-count their reserved stock, while treating new orders as old reservations can restore or consume inventory at the wrong lifecycle step.

**How to apply:** Preserve the legacy state during normalization, subtract only uncommitted pending reservations when checking availability, and consume inventory plus increment sales exactly once when delivery completes.