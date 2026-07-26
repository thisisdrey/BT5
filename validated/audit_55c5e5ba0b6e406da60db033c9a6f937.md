Let me analyze the bug pattern: a capacity-limit check placed in a shared function fires incorrectly during "replace/transfer" operations (which don't increase count) as well as "create new" operations (which do). I'll search for analogous patterns in the Aptos codebase.

Let me look more specifically at operations that "replace" existing entries but share a creation path with capacity checks, particularly in delegation pools, vesting, and token modules.

Let me look more carefully at `pool_u64::transfer_shares` and `staking_contract.move` for the exact analog pattern.