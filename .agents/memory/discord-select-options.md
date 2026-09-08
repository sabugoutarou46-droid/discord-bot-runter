---
name: Discord select options
description: discord.py UI constraint for empty dropdown states.
---

When a Discord dropdown has no usable choices, keep a placeholder option and disable the `discord.ui.Select` component itself; `discord.SelectOption` does not accept a `disabled` argument.

**Why:** Passing `disabled` to an option raises a runtime `TypeError` while constructing the view, preventing every admin interaction that opens that menu.

**How to apply:** Detect the placeholder value when constructing channel, machine, or product selects and pass `disabled=True` to `ui.Select`, while keeping the placeholder option free of unsupported keyword arguments.