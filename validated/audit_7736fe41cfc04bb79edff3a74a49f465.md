### Title
Loss of Funds via Balance-Refund Redirection to a Non-Existent Account - (File: `runtime/near-vm-runner/src/logic/logic.rs`, `runtime/runtime/src/lib.rs`)

### Summary
A contract can redirect the balance refund of one of its own outgoing receipts to an arbitrary account via the `promise_set_refund_to` host function. If that receipt later fails and needs to be refunded, and the specified `refund_to` account does not exist on-chain (and cannot be auto-created because refund receipts are excluded from implicit-account creation), the refund transfer fails and the funds are **permanently burned** rather than returned to anyone. This mirrors the reported Arbitrum bug class: a self-chosen/derived refund address that is not a materialized, funds-receivable entity in the execution environment causes the refund to be irrecoverably lost.

### Finding Description
NEAR added the NEP-associated `refund_to` mechanism to `ActionReceiptV2`, allowing a receipt's predecessor to override where balance refunds are sent, instead of the default `predecessor_id`: [1](#0-0) 

The redirection is set from the WASM host function `promise_set_refund_to`, callable by any deployed contract for any of its outgoing receipts: [2](#0-1) 

This is exactly the analog of the Arbitrum `excessFeeRefundAddress`/`callValueRefundAddress` parameter passed to `createRetryableTicket`: a value freely chosen by the calling code, with no validation that it corresponds to a receivable account on the destination execution context.

When the receipt this refund is attached to fails, `refund_unspent_gas_and_deposits` emits `Receipt::new_balance_refund(receipt.balance_refund_receiver(), deposit_refund)`, where `balance_refund_receiver()` resolves to the `refund_to` account if set: [3](#0-2) [4](#0-3) 

This generated refund receipt has `predecessor_id == "system"`. Crucially, when the runtime later executes this `Transfer` action against the `refund_to` account, refund receipts are explicitly excluded from the implicit-account-creation path: [5](#0-4) 

`implicit_account_creation_eligible = is_the_only_action && !is_refund` — since `is_refund` is true for any receipt with `predecessor_id == "system"`, a refund can never implicitly create the target account, even if `refund_to` happens to be a syntactically valid (e.g., 64-hex "implicit") account ID that has simply never received a real transfer. If the account does not already exist, `check_account_existence` fails with `AccountDoesNotExist`, and the whole refund action fails.

Per the documented and coded behavior, when a refund action fails, the deposit is burned rather than returned anywhere: [6](#0-5) [7](#0-6) 

`validate_receipt`/`validate_action_receipt` only check that `refund_to` is a syntactically valid `AccountId` — it never checks that the account actually exists: [8](#0-7) [9](#0-8) 

So a contract (or a contract acting on behalf of/for a user, analogous to "Contract-A" in the reported Arbitrum scenario) that sets `refund_to` to:
- a typo'd account name,
- a not-yet-created sub-account,
- a still-unfunded/never-used implicit (hex) account,
- or any account that is deleted between the time the receipt is sent and the time the refund fires,

will have its refunded deposit **permanently burned** if the associated receipt subsequently fails, with no path to recovery — exactly the "address exists in form but not as a receivable entity" failure mode described in the Arbitrum report.

### Impact Explanation
This causes concrete, permanent loss of funds: attached deposits that should be refunded to the original depositor/sender are burned instead of returned, whenever the chosen `refund_to` account does not exist at refund time. This applies to any user or contract using `promise_batch_action_*` cross-contract calls combined with `promise_set_refund_to` — a documented, intended feature (see `test_refund_to` and the `sharded-contract` example using `refund_to_account_id`/`promise_set_refund_to`) — making it a realistic path reachable by an ordinary deployed contract, not a privileged or operator-only path.

### Likelihood Explanation
Likelihood is moderate-to-high: `promise_set_refund_to` is a general-purpose public host function with no existence check on the target account, and its canonical use case (per the `sharded-contract` example and NEP-616-style deterministic/derived accounts) is to redirect refunds to accounts that may not yet exist at call time (e.g., an account being deployed in the same batch, or a derived/implicit account that hasn't received a transfer yet). Any timing mismatch between receipt failure and account materialization — or a simple off-by-typo in `refund_to` — silently burns the deposit with no visible attacker required; it is a foot-gun baked into the API's design, same as the reported Arbitrum issue.

### Recommendation
- Document/require (and ideally enforce at the API level) that `refund_to` accounts should be capable of receiving refunds even when the source receipt fails, or
- Allow refund receipts targeting non-existent-but-implicit-format accounts to trigger implicit account creation the same way ordinary `Transfer` actions do (i.e., relax the `!is_refund` restriction specifically for `refund_to`-redirected refunds), or
- Add existence validation for `refund_to` at the time `promise_set_refund_to` is called (best-effort) and clearly surface in the outcome/logs when a refund is burned due to a non-existent `refund_to` account, so integrators can detect and remediate silent fund loss.

### Proof of Concept
1. Contract `A` issues a cross-contract call (`promise_batch_create`/`promise_batch_action_function_call`) to account `B` with an attached deposit, calling a method expected to fail (e.g., a non-existent method, as in `test_refund_to`).
2. Contract `A` calls `promise_set_refund_to(promise_idx, "typo_or_not_yet_created.near")` to redirect the balance refund to an account that does not currently exist on-chain.
3. Contract `B`'s receipt executes and fails (e.g., `MethodNotFound`), causing `refund_unspent_gas_and_deposits` to emit `Receipt::new_balance_refund("typo_or_not_yet_created.near", deposit)` with `predecessor_id == "system"`.
4. When this refund receipt is applied, `apply_action` computes `is_refund = true`, so `implicit_account_creation_eligible = false`; `check_account_existence` fails with `AccountDoesNotExist` (see `runtime/runtime/src/lib.rs:548-562` and `runtime/runtime/src/actions.rs:787-827`).
5. Per `runtime/runtime/src/lib.rs:993-1000`, because `receipt.predecessor_id().is_system()` and `result.result.is_err()`, the full `total_deposit` is added to `stats.balance.other_burnt_amount` — the deposit is burned, never returned to `A` or anyone else.

This is directly comparable to `runtime/runtime/tests/test_async_calls.rs:1204-1296` (`test_refund_to`), except substituting an existing beneficiary (`"near_3"`) with a non-existent account ID demonstrates the fund-burning outcome instead of a successful redirected refund.

### Citations

**File:** core/primitives/src/receipt.rs (L428-430)
```rust
    pub fn balance_refund_receiver(&self) -> &AccountId {
        self.refund_to().as_ref().unwrap_or_else(|| self.predecessor_id())
    }
```

**File:** core/primitives/src/receipt.rs (L496-510)
```rust
    pub fn new_balance_refund(receiver_id: &AccountId, refund: Balance) -> Self {
        Receipt::V0(ReceiptV0 {
            predecessor_id: "system".parse().unwrap(),
            receiver_id: receiver_id.clone(),
            receipt_id: CryptoHash::default(),
            receipt: ReceiptEnum::Action(ActionReceipt {
                signer_id: "system".parse().unwrap(),
                signer_public_key: PublicKey::empty(KeyType::ED25519),
                gas_price: Balance::ZERO,
                output_data_receivers: vec![],
                input_data_ids: vec![],
                actions: vec![Action::Transfer(TransferAction { deposit: refund })],
            }),
        })
    }
```

**File:** runtime/near-vm-runner/src/logic/logic.rs (L2509-2534)
```rust
    pub fn promise_set_refund_to(
        &mut self,
        promise_idx: u64,
        account_id_len: u64,
        account_id_ptr: u64,
    ) -> Result<()> {
        self.result_state.gas_counter.pay_base(base)?;
        if self.context.is_view() {
            return Err(HostError::ProhibitedInView {
                method_name: "promise_set_refund_to".to_string(),
            }
            .into());
        }
        let refund_to = self.read_and_parse_account_id(account_id_ptr, account_id_len)?;
        let promise = self
            .promises
            .get(promise_idx as usize)
            .ok_or(HostError::InvalidPromiseIndex { promise_idx })?;

        let receipt_idx = match &promise {
            Promise::Receipt(receipt_idx) => Ok(*receipt_idx),
            Promise::NotReceipt(_) => Err(HostError::CannotSetRefundToOnJointPromise),
        }?;

        self.ext.set_refund_to(receipt_idx, refund_to);
        Ok(())
```

**File:** runtime/runtime/src/lib.rs (L548-562)
```rust
        let is_refund = receipt.predecessor_id().is_system();
        let is_the_only_action = actions.len() == 1;
        let implicit_account_creation_eligible = is_the_only_action && !is_refund;

        // Account validation
        if let Err(e) = check_account_existence(
            action,
            account,
            account_id,
            &apply_state.config,
            implicit_account_creation_eligible,
        ) {
            result.result = Err(e);
            return Ok(result);
        }
```

**File:** runtime/runtime/src/lib.rs (L993-1000)
```rust
        let gas_refund_result = if receipt.predecessor_id().is_system() {
            // If the refund fails tokens are burned.
            if result.result.is_err() {
                stats.balance.other_burnt_amount = safe_add_balance(
                    stats.balance.other_burnt_amount,
                    total_deposit(&action_receipt.actions())?,
                )?
            }
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

**File:** docs/RuntimeSpec/Refunds.md (L10-13)
```markdown
Refund receipts are identified by having `predecessor_id == "system"`. They are also special because they don't cost any gas to generate or execute. As a result, they also do not contribute to the block gas limit.

If the execution of a refund fails, the refund amount is burnt.
The refund receipt is an `ActionReceipt` that consists of a single action `Transfer` with the `deposit` amount of the refund.
```

**File:** runtime/runtime/src/verifier.rs (L602-606)
```rust
    if let Some(account_id) = receipt.refund_to() {
        AccountId::validate(account_id.as_ref()).map_err(|_| {
            ReceiptValidationError::InvalidRefundTo { account_id: account_id.to_string() }
        })?;
    }
```

**File:** core/primitives/src/errors.rs (L507-508)
```rust
    /// The `refund_to` of an ActionReceipt is not valid.
    InvalidRefundTo { account_id: String } = 8,
```
