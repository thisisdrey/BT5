Let me analyze the bug pattern and search for analogs in the Aptos codebase. The root cause is: a supply/balance snapshot is taken BEFORE a burn operation, inflating the recorded value and causing incorrect distribution calculations.

Let me look more carefully at the `vest` function's use of `grant_pool.total_coins()` and the `pool_u64` module, plus the `delegation_pool` withdraw flow.