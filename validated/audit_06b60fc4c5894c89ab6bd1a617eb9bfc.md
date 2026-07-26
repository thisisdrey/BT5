I need to trace the exact root cause from the Salty.IO bug into the Aptos codebase: a **user-controlled action that updates a per-user state variable (cooldown/timestamp), which is then checked by a privileged/forced action (liquidation), allowing the user to block that forced action indefinitely**.

Let me map this to Aptos candidates systematically.