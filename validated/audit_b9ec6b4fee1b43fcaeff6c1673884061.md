### Title
Double-crediting of deposits when a multi-action receipt has a successful `Transfer` followed by a failing action - ([File: runtime/runtime/src/lib.rs])

### Finding Description
`refund_unspent_gas_and_deposits` computes the deposit refund as `total_deposit(&action_receipt.actions())`, i.e. the sum of **all** deposits attached to **every** action in the receipt, not just the deposits of actions that never executed [1](#0-0) . This value is used unconditionally whenever the aggregate `result.result` for the receipt is `Err`.

The action-execution loop in `apply_action_receipt` runs actions sequentially against a single in-memory `Option<Account>` for the receiver, applying `set_account`/balance mutations for each action that succeeds (e.g. `Transfer` credits `account.amount()` immediately), and only stops looping when an action returns an error; it does not undo the state mutations already made by prior successful actions in the same receipt — those mutations are persisted to the account when the loop exits, independent of whether the overall action-receipt result later turns out to be `Err`.

An attacker can submit (via a normal signed transaction or a promise-batch from their own deployed contract) an `ActionReceipt` targeting an account they control with actions `[Transfer(X), FunctionCall(deliberately fails)]`:
1. The `Transfer(X)` action succeeds and increases the receiver account's `amount` by `X`.
2. The `FunctionCall` action fails (e.g. the contract asserts/panics), causing `action_result.result` to become `Err` and the loop to stop.
3. `refund_unspent_gas_and_deposits` computes `total_deposit` over **both** actions, including the deposit already delivered by the successful `Transfer`, and issues a refund receipt of that full amount back to the predecessor (sender).

No existing check (signature/nonce/access-key/gas-metering/storage-staking) prevents this, because those checks validate the receipt's construction, not the interaction between per-action success and the aggregate refund calculation. The bug is a straightforward accounting inconsistency between "actions are not atomic within a receipt" and "refund uses the full deposit total regardless of which actions already delivered their deposit."

### Impact Explanation
This results in **token inflation**: the receiver keeps the `X` NEAR credited by the successful `Transfer`, while the sender also receives `X` back via the deposit refund, creating `X` extra tokens out of thin air on every exploit. This violates NEAR's value-conservation invariant and falls under the "token inflation" bounty category.

### Likelihood Explanation
The attack requires nothing beyond ordinary unprivileged capabilities: an attacker deploys a contract with a method that reliably fails (panics/asserts) and sends a receipt (directly or via a promise batch from their own contract) with actions `[Transfer(X), FunctionCall(to the failing method)]` targeting an account they control. This is fully deterministic, requires no races, no validator/node privileges, and is trivially repeatable to mint funds at will (bounded only by attacker's willingness to pay gas and prepaid gas for the failing call).

### Recommendation
Track and refund only the deposits of actions that did not successfully execute (e.g. accumulate `total_deposit` incrementally as actions are attempted, subtracting the deposit of each action once it is confirmed successful), or make action execution within a single receipt properly atomic (roll back all state changes for that receipt if any action fails) so that a failed receipt's refund and the receiver's balance changes cannot both reflect the same deposit.

### Proof of Concept
Unit/integration test in `runtime/runtime/src/tests` (or similar) driving `apply_action_receipt` directly with a constructed `ActionReceipt`:
1. Create receiver account with initial balance `B`.
2. Build an `ActionReceipt` with actions `[Transfer(X), FunctionCall(method that always panics)]`, attached deposit `X` on the `Transfer`.
3. Call `apply_action_receipt` (or run through the full `apply` pipeline) and inspect:
   - Receiver's final balance (`get_account` after apply) — expect `B + X` from the successful Transfer.
   - The generated refund receipt sent back to the predecessor — expect it to contain amount `X` as well.
4. Assert failure: `receiver_balance_increase (X) + refund_amount (X) != X` (total deposit), demonstrating `2X` was created instead of `X`, violating conservation of value.

### Citations

**File:** runtime/runtime/src/lib.rs (L1241-1249)
```rust
        let total_deposit = total_deposit(&action_receipt.actions())?;
        let prepaid_gas = total_prepaid_gas(&action_receipt.actions())?
            .checked_add(total_prepaid_send_fees(config, &action_receipt.actions())?.gas)
            .ok_or(IntegerOverflowError)?;
        let prepaid_exec_gas =
            total_prepaid_exec_fees(config, &action_receipt.actions(), receipt.receiver_id())?
                .checked_add(config.fees.fee(ActionCosts::new_action_receipt).exec_fee())
                .ok_or(IntegerOverflowError)?;
        let deposit_refund = if result.result.is_err() { total_deposit } else { Balance::ZERO };
```
