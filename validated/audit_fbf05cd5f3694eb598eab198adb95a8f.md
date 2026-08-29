### Title
Meta-transaction relayers underpay send fees for forwarded `DelegateAction` inner actions - (File: `runtime/runtime/src/actions.rs`)

### Summary
In `apply_delegate_action`, `prepaid_send_fees` is computed from `action_receipt.actions()` — the outer wrapper receipt's action list, which by protocol rule contains only the single `Action::Delegate` — instead of from `delegate_action.get_actions()`, the inner action list that is actually placed into the newly created outgoing receipt. This causes the per-byte/size-scaled "send" component of gas fees for the forwarded inner actions to never be charged, while the corresponding "execution" component is charged correctly via `receipt_required_cost`.

### Finding Description
`apply_delegate_action` builds `new_receipt` whose `ActionReceipt.actions` is set to `delegate_action.get_actions()` (the actions signed by the victim inside the `DelegateAction`): [1](#0-0) 

It then computes the two cost components that are supposed to fully account for creating and sending this new receipt: [2](#0-1) 

`required_cost` is derived from `new_receipt` via `receipt_required_cost` → `action_receipt_required_cost`, which correctly sums `total_prepaid_exec_fees` and `total_prepaid_gas` over the *inner* actions (`action_receipt.actions()` of `new_receipt`, i.e. `delegate_action.get_actions()`): [3](#0-2) 

However, `prepaid_send_fees` is computed from `action_receipt.actions()` where `action_receipt` is the *parameter* passed into `apply_delegate_action` — the outer receipt currently being executed, whose action list is (by the protocol's own batching restriction) just the single `Action::Delegate(...)` wrapper, not the inner forwarded actions. Each `ActionCosts` fee entry (`Fee { send_sir, send_not_sir, execution }`) has size-scaling components for actions such as `FunctionCall`, `DeployContract`, and `AddKey`; the `send_*` component of these fees is charged separately from the `execution` component and is meant to account for the cost of transmitting the receipt's action payload cross-shard. Because `total_prepaid_send_fees` is invoked against the outer wrapper (a single, fixed-size `Delegate` action) rather than the inner actions, none of the size-dependent send-fee cost of the actual forwarded actions (which can include large `FunctionCall` args, `DeployContract` code, many `AddKey`/method-name lists, etc., up to receipt size limits) is ever billed to `gas_burnt`/`gas_used`.

The comment at line 511-512 ("This gas was prepaid on Relayer shard. Need to burn it because the receipt is going to be sent.") confirms the intent is to charge the send cost of the receipt that is about to be sent — i.e. `new_receipt` — but the implementation sources the fee from the wrong action list.

No other check compensates for this: `validate_delegate_action_key` only validates nonce/permissions, and `receipt_required_cost` only covers execution-side costs of the inner actions, not their send-side costs.

### Impact Explanation
This is a gas-metering "totality" violation: the send-fee portion of the true cost of forwarding a `DelegateAction`'s inner actions is never charged to the relayer/victim, while the receiving shard will still process a receipt whose payload size/action count reflects the full inner action list. An attacker (who can sign a `DelegateAction` for their own account and also act as relayer, satisfying only unprivileged-client capabilities) can pack many size-heavy inner actions into a single `DelegateAction`, paying only the flat, size-independent send fee of one `Action::Delegate` wrapper action instead of the sum of the (size-scaled) send fees of all inner actions. This under-collects protocol fee revenue and lets the attacker generate expensive-to-transmit, large outgoing receipts cross-shard at a discount relative to their true network/bandwidth cost — a bounded but real economic/resource-accounting exploit ("gas metering" / free amplification category), not merely a hygiene concern, since it directly violates the fee model's requirement that every action's send cost be charged.

### Likelihood Explanation
Trivial to trigger: no special privileges are needed. An unprivileged account can sign a `DelegateAction` (as its own sender) containing multiple actions with large payloads (e.g. big `FunctionCall` args, big `DeployContract` code, or many `AddKey` entries), and submit it (via its own relaying or a public relayer) as the sole action in the outer receipt/transaction. Every meta-transaction (NEP-366) that carries more than a trivial-size inner action list will exhibit this under-accounting, making it 100% repeatable and low-cost.

### Recommendation
Compute `prepaid_send_fees` from the same action list used to build `new_receipt` (i.e., `delegate_action.get_actions()` / the `new_receipt`'s `ActionReceipt.actions()`), not from the outer `action_receipt.actions()`. E.g.:
```rust
let prepaid_send_fees = total_prepaid_send_fees(&apply_state.config, &delegate_action.get_actions())?;
```
Consider also charging the outer wrapper's own `Delegate`-action send fee, if that cost is not already accounted for at transaction-conversion time.

### Proof of Concept
Unit test in `runtime/runtime/src/actions.rs` (or a `runtime/runtime/tests` integration test):
1. Construct a `VersionedActionReceipt` whose `actions()` is `[Action::Delegate(signed_delegate_action)]` where `signed_delegate_action.delegate_action.actions` contains N (e.g. 5) `FunctionCall` actions each with large (e.g. 10KB) `args`.
2. Call `total_prepaid_send_fees(&config, action_receipt.actions())` (outer) and compare against `total_prepaid_send_fees(&config, &delegate_action.get_actions())` (inner).
3. Assert the inner-computed value is substantially larger than the outer-computed value (proportional to the extra `FunctionCall` byte-send costs).
4. Run `apply_delegate_action` end-to-end and assert `result.gas_burnt` equals the inner-computed send fee plus applicable overhead, expecting the test to fail against current code (which only reflects the outer/wrapper's flat `Delegate` send fee) — demonstrating the under-charge.

### Citations

**File:** runtime/runtime/src/actions.rs (L489-497)
```rust
        receipt: ReceiptEnum::Action(ActionReceipt {
            signer_id: action_receipt.signer_id().clone(),
            signer_public_key: action_receipt.signer_public_key().clone(),
            gas_price: action_receipt.gas_price(),
            output_data_receivers: vec![],
            input_data_ids: vec![],
            actions: delegate_action.get_actions(),
        }),
    });
```

**File:** runtime/runtime/src/actions.rs (L505-515)
```rust
    let prepaid_send_fees = total_prepaid_send_fees(&apply_state.config, action_receipt.actions())?;
    let required_cost = receipt_required_cost(apply_state, &new_receipt)?;
    // This gas will be burnt by the receiver of the created receipt.
    // Compute costs of that are not relevant at this point, the "used" gas is
    // only reserved for execution later, potentially on a different shard.
    result.gas_used = result.gas_used.checked_add_result(required_cost.gas)?;
    // This gas was prepaid on Relayer shard. Need to burn it because the receipt is going to be sent.
    // gas_used is incremented because otherwise the gas will be refunded. Refund function checks only gas_used.
    result.gas_used = result.gas_used.checked_add_result(prepaid_send_fees.gas)?;
    result.gas_burnt = result.gas_burnt.checked_add_result(prepaid_send_fees.gas)?;
    result.compute_usage = safe_add_compute(result.compute_usage, prepaid_send_fees.compute)?;
```

**File:** runtime/runtime/src/actions.rs (L537-556)
```rust
fn action_receipt_required_cost(
    apply_state: &ApplyState,
    receipt: &Receipt,
    action_receipt: VersionedActionReceipt,
) -> Result<ParameterCost, RuntimeError> {
    let mut required_gas = total_prepaid_exec_fees(
        &apply_state.config,
        &action_receipt.actions(),
        receipt.receiver_id(),
    )?;
    let attached_gas = total_prepaid_gas(&action_receipt.actions())?;
    // Gas attached to outgoing function calls have no associated compute costs.
    // Compute costs are only relevant when burning gas.
    let attached_gas_cost = ParameterCost { gas: attached_gas, compute: 0 };
    required_gas = required_gas.checked_add_result(attached_gas_cost)?;
    required_gas = required_gas.checked_add_result(
        apply_state.config.fees.fee(ActionCosts::new_action_receipt).exec_fee(),
    )?;
    Ok(required_gas)
}
```
