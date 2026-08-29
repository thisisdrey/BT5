### Title
Unbounded 1-yoctoNEAR subsidy accumulation via repeated `promise_batch_action_function_call_weight` calls on a zero-balance account mints unbacked deposits - (File: runtime/near-vm-runner/src/logic/logic.rs)

### Summary
`promise_batch_action_function_call_weight` contains a `skip_deduct` branch that lets a contract attach `1` yoctoNEAR to an outgoing `FunctionCall` action without deducting it from `current_account_balance`, provided the account balance is already zero. Because this check re-evaluates `current_account_balance.is_zero()` on every call and the balance is never modified when `skip_deduct` fires, the same zero-balance receipt can invoke this path an arbitrary number of times, each time appending `1` yoctoNEAR to `subsidized_amount` and attaching a real 1-yoctoNEAR deposit to a new `FunctionCall` action, without ever spending real account balance.

### Finding Description
In `promise_batch_action_function_call_weight`: [1](#0-0) 

`skip_deduct` is computed purely from the call's `amount` (must be exactly 1), a config flag `one_yocto_on_promise`, and `self.result_state.current_account_balance.is_zero()`. When true, the code adds `amount` to `subsidized_amount` via `checked_add` instead of calling `deduct_balance`. Since `deduct_balance` is the only place that would change `current_account_balance`, and it is skipped in this branch, `current_account_balance` remains zero for the *entire* duration of the receipt's execution. This means the precondition for `skip_deduct` (`current_account_balance.is_zero()`) is trivially re-satisfied on every subsequent call within the same host function invocation (e.g., a WASM loop that does `promise_batch_create` + `promise_batch_action_function_call_weight(amount=1)` N times).

Each iteration:
- Attaches a genuine 1-yoctoNEAR deposit to a distinct outgoing `FunctionCall` action (via `self.ext.append_action_function_call_weight(... amount ...)`), which downstream receivers (e.g., `ft_transfer_call`-style methods asserting `assert_one_yocto`) will treat as a real attached deposit.
- Increments `subsidized_amount` by 1, using `checked_add`, so after N repeats `subsidized_amount == N` yoctoNEAR — this scales linearly and is not capped to a single yoctoNEAR per receipt.

The only limiting factors are gas costs (`function_call_base`/`function_call_byte` action costs and `prepay_gas`), not a conservation check tying `subsidized_amount` to any real burn from the zero-balance account. Because the account never had — and never accrues — the balance to back these N deposits, and each of the N attached-deposit `FunctionCall` actions is independently real value seen by the receiving contract, the mechanism creates value that is not conserved: N distinct 1-yoctoNEAR deposits are dispatched from an account with a permanently-zero balance, backed by a `subsidized_amount` counter whose reconciliation against `total_balance_burnt` was not found to scale with N in the portions of the runtime accounting code I was able to inspect (`runtime/runtime/src/lib.rs`, `chain/chain/src/runtime/mod.rs`) — I could not confirm from the available indexed content whether the downstream reconciliation logic actually burns the full accumulated `subsidized_amount` (N) rather than a fixed/singular amount, which is the crux of the described invariant violation.

### Impact Explanation
If the downstream burn/reconciliation does not scale 1:1 with the accumulated `subsidized_amount`, this is a token-inflation primitive: an attacker can mint an arbitrary number of 1-yoctoNEAR-backed deposits from a permanently zero-balance account and route them to contracts requiring `assert_one_yocto`-style deposits (e.g., NEP-141 `ft_transfer_call`), effectively bypassing the storage/deposit economic backing entirely. Even at yoctoNEAR granularity this represents unbacked value creation and would match a "token inflation or loss" bounty category if the reconciliation gap is real.

### Likelihood Explanation
The precondition (a contract account with zero balance) is attacker-controlled: an unprivileged user can deploy a contract to a freshly created/drained account and drive execution via ordinary signed transactions — no privileged access is required. The repeated-call pattern (loop of `promise_batch_create` + `promise_batch_action_function_call_weight(amount=1)`) is straightforward WASM logic bounded only by gas, so it is cheap and fully repeatable per receipt, and can be repeated across many receipts/blocks.

### Recommendation
- Bound `skip_deduct` to at most one application per receipt (e.g., track a per-`ActionReceipt` "already subsidized" flag rather than re-checking `current_account_balance.is_zero()` on every call), or
- Ensure the runtime's balance-conservation check reconciles the *full* accumulated `subsidized_amount` (not a fixed constant) against `total_balance_burnt`/minted balance for the receipt, and add an explicit test proving N repeats yield exactly N in the burn accounting.

### Proof of Concept
Integration/runtime-test-loop plan:
1. Deploy a contract to an account with `current_account_balance == 0`.
2. In the contract, loop `N` times: `promise_batch_create(receiver)` then `promise_batch_action_function_call_weight(promise_idx, method, args, amount_ptr=1u128, gas, weight=0)`.
3. Execute this as a single receipt via `runtime/runtime/src/tests/apply.rs`-style test harness.
4. Assert `subsidized_amount == N` yoctoNEAR in the resulting `VMOutcome`/`ActionResult`.
5. Assert that each of the N generated `FunctionCall` actions carries `deposit == 1`.
6. Trace the value into `runtime/runtime/src/lib.rs`'s balance-conservation check and assert whether `total_balance_burnt` (or equivalent) is increased by exactly `N`, not `1` — a mismatch here confirms the inflation bug.

### Citations

**File:** runtime/near-vm-runner/src/logic/logic.rs (L3097-3112)
```rust
        // Allow attaching exactly 1 yoctoNEAR to a promise function call
        // when the contract has zero balance. This lets deterministic accounts
        // call functions like ft_transfer_call that require an attached deposit
        // without needing to be seeded with balance first.
        let skip_deduct = amount == Balance::from_yoctonear(1)
            && self.config.one_yocto_on_promise
            && self.result_state.current_account_balance.is_zero();
        if skip_deduct {
            self.result_state.subsidized_amount = self
                .result_state
                .subsidized_amount
                .checked_add(amount)
                .expect("subsidized_amount overflow");
        } else {
            self.result_state.deduct_balance(amount)?;
        }
```
