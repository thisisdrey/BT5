Let me analyze the bug report's root cause and search for analogs in the Aptos codebase.

The core issue: a health/status check misclassifies an **empty/zero-collateral state** as `LIQUIDATABLE`, enabling unauthorized operations. I need to find similar patterns where zero/uninitialized values cause incorrect status classification that bypasses authorization.

Let me search more specifically for the root cause pattern: ratio/percentage calculations returning 0 for empty state, and status-gated authorization in Move framework code.

Let me look more specifically at staking contract ratio calculations and status-based authorization patterns in Move framework files.