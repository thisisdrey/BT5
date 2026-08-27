### Title
Malicious Meta-Transaction Sender Can Drain a Relayer's Attached Deposit By Forcing Failure On The Receiver Shard - (File: `runtime/runtime/src/actions.rs`, `runtime/runtime/src/lib.rs`, `runtime/runtime/src/config.rs`)

### Summary
In NEP-366 meta transactions, a relayer pays all gas and attaches all deposits for the inner actions of a `DelegateAction`, but if the receiver-shard receipt fails, the deposit refund is sent to the `sender_id` of the `DelegateAction` (the untrusted, unprivileged party who signed it) rather than to the relayer who actually paid for it. A malicious sender can therefore construct a meta transaction whose inner action is guaranteed (or engineered) to fail on the receiver's shard, causing the relayer's attached deposit to be refunded to the malicious sender instead of back to the relayer — a zero-cost transfer of the relayer's funds to the attacker, analogous to the `makerFee`/`takerFee` misdirection in the Notional finding where an untrusted, signature-checked party can redirect funds intended for the trusted payer to themselves.

### Finding Description
When a relayer submits a `SignedDelegateAction`, `apply_delegate_action` builds a new receipt whose `predecessor_id` is the `DelegateAction.sender_id` (the untrusted party, e.g. "Alice") and whose `receiver_id` is the inner action's target: [1](#0-0) 

The protocol's own documentation states this explicitly as an accepted trust assumption: [2](#0-1) 

And the runtime code comment confirms the same: [3](#0-2) 

Deposit refunds on receipt failure are calculated purely from `total_deposit` and paid to the receipt's balance-refund receiver, which for a normal (non-contract) predecessor defaults to the `predecessor_id`: [4](#0-3) [5](#0-4) 

Because the relayer purchases/attaches the deposit for the inner actions (as explicitly noted in the `total_deposit` comment), but the created receipt's `predecessor_id` is set to the delegate action's `sender_id`, any failure of the inner action on the receiver's shard results in the deposit being refunded to the sender (attacker), not the relayer who actually paid. The `sender_id` does not need any special privilege — this is exactly the "unprivileged signer" scenario: a validly-signed `DelegateAction` from an ordinary account triggers economically unfair fund routing without any additional validation preventing it, similar to how the Notional maker was able to divert `makerFee` to itself without the check being caught by `_validateOrder`.

An attacker ("Alice") can force this failure deterministically and cheaply, for example by:
- Directing the inner action (e.g., a `Transfer` or `FunctionCall` with attached deposit) at a receiver account that will reject/panic (e.g., a `FunctionCall` to a contract method that always errors, or a receiver that does not exist / has constraints that guarantee failure), or
- Colluding with (or controlling) the receiver contract to intentionally fail the call.

Since the relayer is the one who "purchases the gas for all inner actions ... If the inner actions have an attached token balance, this is also paid for by the relayer" (per the doc), while the attacker pays nothing (all costs, including gas, are covered by the relayer per NEP-366 design), the attacker can extract the relayer's attached deposit at zero cost to herself whenever she can induce failure on the receiver shard.

### Impact Explanation
This allows concrete theft of funds: a relayer that services meta-transactions (a core, documented use case for NEP-366) can have any deposit it attaches to inner actions of a delegate action diverted entirely to the malicious signer with a receiver-shard failure, at no cost to the attacker. This is not a hypothetical griefing issue — it results in direct loss of relayer funds and gain for the attacker, which is exactly the "malicious treasury manager" pattern of the original finding (a validly-signed, unprivileged party diverting funds intended for the honest counterparty to itself by manipulating a parameter/outcome the protocol does not adequately protect against).

### Likelihood Explanation
Likelihood is high for any relayer that services untrusted third-party `DelegateAction`s with non-zero attached deposits for actions whose success cannot be guaranteed by the relayer in advance (e.g., calling into a contract not controlled by the relayer, or a contract that can be made to fail based on state the attacker controls). This is a known, protocol-documented limitation ("this is something relayer implementations must be aware of since there is a financial incentive for Alice to submit meta transactions that have high balances attached but will fail on Bob's shard"), so it requires no protocol bug to exploit — only an untrusted relayer configuration or a relayer that fails to add its own safeguards.

### Recommendation
For inner actions of a `DelegateAction` that carry an attached deposit, the deposit-refund-on-failure path should route back to the actual payer (the relayer/signer of the outer transaction) rather than to the `sender_id` of the `DelegateAction`, or the protocol should require/allow the relayer to explicitly opt into accepting this risk (e.g., via `promise_set_refund_to`-style overriding at receipt-creation time in `apply_delegate_action`, or by disallowing non-zero deposits in meta-transaction inner actions unless the relayer signs off on the specific failure-refund target). At minimum, the risk should be more prominently exposed at the protocol level (not merely relayer-implementation guidance) since this is a systemic economic design gap rather than an implementation bug relayers can trivially work around.

### Proof of Concept
Not independently executable from the indexed context (no full runnable reproduction was located), but the mechanics are fully supported by the cited code and are also validated by the project's own documentation of the exact same attack: [6](#0-5) 
1. Attacker "Alice" signs a `DelegateAction` with `sender_id = Alice`, `receiver_id = Bob`, containing an inner action (e.g. `FunctionCall`) with a non-zero `deposit`, targeting a method on `Bob` that Alice knows/can ensure will fail (e.g., a nonexistent method, or one gated by a condition Alice controls).
2. An honest relayer, unaware of the guaranteed failure, wraps and submits the `SignedDelegateAction` in a transaction it signs, attaching the deposit and gas costs from its own account.
3. On Alice's shard, `apply_delegate_action` creates a new receipt with `predecessor_id = Alice`, `receiver_id = Bob`, carrying the inner action and its deposit: [7](#0-6) .
4. The inner action fails on Bob's shard as designed.
5. `refund_unspent_gas_and_deposits` computes `deposit_refund = total_deposit` since `result.result.is_err()`, and issues `Receipt::new_balance_refund(receipt.balance_refund_receiver(), deposit_refund)`: [8](#0-7) [9](#0-8) .
6. Since the receipt's predecessor is Alice (not the relayer), the refund is delivered back to Alice — the relayer's deposit has been transferred to Alice at zero cost to her.

### Citations

**File:** runtime/runtime/src/actions.rs (L483-503)
```rust
    // Generate a new receipt from DelegateAction.
    let new_receipt = Receipt::V0(ReceiptV0 {
        predecessor_id: sender_id.clone(),
        receiver_id: delegate_action.receiver_id().clone(),
        receipt_id: CryptoHash::default(),

        receipt: ReceiptEnum::Action(ActionReceipt {
            signer_id: action_receipt.signer_id().clone(),
            signer_public_key: action_receipt.signer_public_key().clone(),
            gas_price: action_receipt.gas_price(),
            output_data_receivers: vec![],
            input_data_ids: vec![],
            actions: delegate_action.get_actions(),
        }),
    });

    // Note, Relayer prepaid all fees and all things required by actions: attached deposits and attached gas.
    // If something goes wrong, deposit is refunded to the predecessor, this is sender_id/Sender in DelegateAction.
    // Gas is refunded to the signer, this is Relayer.
    // Some contracts refund the deposit. Usually they refund the deposit to the predecessor and this is sender_id/Sender from DelegateAction.
    // Therefore Relayer should verify DelegateAction before submitting it because it spends the attached deposit.
```

**File:** docs/architecture/how/meta-tx.md (L225-242)
```markdown
## Balance refunds in meta transactions

Unlike gas refunds, the protocol sends balance refunds to the predecessor
(a.k.a. sender) of the receipt. This makes sense, as we deposit the attached
balance to the receiver, who has to explicitly reattach a new balance to new
receipts they might spawn.

In the world of meta transactions, this assumption is also challenged. If an
inner action requires an attached balance (for example a transfer action) then
this balance is taken from the relayer.

The relayer can see what the cost will be before submitting the meta transaction
and agrees to pay for it, so nothing wrong so far. But what if the transaction
fails execution on Bob's shard? At this point, the predecessor is `Alice` and
therefore she receives the token balance refunded, not the relayer. This is
something relayer implementations must be aware of since there is a financial
incentive for Alice to submit meta transactions that have high balances attached
but will fail on Bob's shard.
```

**File:** runtime/runtime/src/config.rs (L604-621)
```rust
/// Get the total sum of deposits for given actions.
pub fn total_deposit(actions: &[Action]) -> Result<Balance, IntegerOverflowError> {
    let mut total_balance = Balance::ZERO;
    for action in actions {
        let action_balance;
        if let Some(delegate_action) = delegate_inner_action(action) {
            // Note, here Relayer pays the deposit but if actions fail, the deposit is
            // refunded to Sender of DelegateAction
            let actions = delegate_action.get_actions();
            action_balance = total_deposit(&actions)?;
        } else {
            action_balance = action.get_deposit_balance();
        }

        total_balance = safe_add_balance(total_balance, action_balance)?;
    }
    Ok(total_balance)
}
```

**File:** runtime/runtime/src/lib.rs (L1241-1253)
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
        let gross_gas_refund = if result.result.is_err() {
            prepaid_gas
                .checked_add(prepaid_exec_gas.gas)
                .ok_or(IntegerOverflowError)?
```

**File:** runtime/runtime/src/lib.rs (L1346-1353)
```rust
        let gas_balance_refund = safe_add_balance(unused_gas_balance_refund, burned_gas_refund)?;

        if deposit_refund > Balance::ZERO {
            result.new_receipts.push(Receipt::new_balance_refund(
                receipt.balance_refund_receiver(),
                deposit_refund,
            ));
        }
```
