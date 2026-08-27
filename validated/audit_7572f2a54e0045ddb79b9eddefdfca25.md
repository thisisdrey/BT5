### Title
`DeleteAccount`'s `beneficiary_id` is never checked for existence, so funds can be permanently burned by refunding to a non‑existent account - (File: runtime/runtime/src/actions.rs)

### Summary
The Sherlock finding is about a position-transfer function that never checks the recipient for validity (`address(0)`), letting a user lock funds forever. The nearcore analog is `DeleteAccountAction`, whose `beneficiary_id` field is only validated for being a *syntactically* well-formed `AccountId` — never for actually existing. Any ordinary account can delete itself and set `beneficiary_id` to a syntactically valid but non-existent (and non-implicit-eligible) account, causing its full remaining balance to be permanently burned instead of transferred.

### Finding Description
`DeleteAccountAction` only carries a `beneficiary_id: AccountId` with no additional constraint [1](#0-0) . Documented validation only checks that it is a well-formed account id string (`InvalidAccountId`) — there is no check that the account exists [2](#0-1) .

When a `DeleteAccount` action executes, the remaining balance is packaged into a system-originated balance-refund receipt addressed to `beneficiary_id`, with no existence check performed beforehand: [3](#0-2) . `Receipt::new_balance_refund` sets `predecessor_id: "system"`, marking it as a refund receipt [4](#0-3) .

When that refund receipt is later processed as a `Transfer` action against a beneficiary account that does not exist, `action_transfer_or_implicit_account_creation` only creates an account for eligible implicit accounts; for a refund the assumption is `debug_assert!(!is_refund)` in the no-account branch, i.e. transfers to non-existent accounts are expected to have already failed before reaching account creation [5](#0-4) . The actual existence gate is `check_transfer_to_nonexisting_account`, which explicitly documents that refunds are *not* allowed to create new accounts and returns `AccountDoesNotExist` for any non-implicit-eligible target [6](#0-5) .

Critically, refund receipts are documented to burn their deposit on failure rather than being retried or returned to the original owner: "If the execution of a refund fails, the refund amount is burnt" [7](#0-6) , and this is implemented directly in the runtime: when `receipt.predecessor_id().is_system()` and the result is an error, the deposit is added to `other_burnt_amount` [8](#0-7) .

Putting this together: an ordinary user calling `DeleteAccount` with a syntactically valid but non-existent, non-implicit `beneficiary_id` (e.g. `"doesnotexist12345.near"`) will have their entire account balance irrecoverably burned, exactly analogous to the Sherlock bug where a missing recipient check on a value-transferring function permanently locks/loses the transferred asset.

### Impact Explanation
This causes concrete, permanent loss of user funds: the account's full NEAR balance at deletion time is burned rather than delivered to any account, with no way to recover it. This matches the "concrete theft or permanent freezing of funds" acceptance criterion.

### Likelihood Explanation
Trivial to trigger — any account holder can submit a `DeleteAccount` transaction from their own signer with an arbitrary `beneficiary_id` string that merely needs to pass the syntactic `AccountId` validator (e.g. a plausible but unregistered account name). No special privileges, front-running, or unusual state are required; it could also happen accidentally due to a typo in wallets/tooling that don't independently verify beneficiary existence before submitting the transaction.

### Recommendation
Before generating the balance-refund receipt in `action_delete_account`, verify that `beneficiary_id` refers to an existing account (or is otherwise guaranteed not to be silently burned), and reject the `DeleteAccount` action with a clear validation error (e.g. extend `ActionErrorKind`) if the beneficiary does not exist and is not implicit-account-creation eligible. Alternatively, disallow burning on refund failure specifically for delete-account beneficiary transfers and instead return the funds to the deleting account/predecessor, or require wallets/RPC layers to pre-validate `beneficiary_id` existence prior to submission.

### Proof of Concept
1. Create account `alice.near` with a balance and a `DeleteAccount` action targeting a non-existent account, e.g. `beneficiary_id = "nonexistent999.near"` (passes `AccountId` syntax validation).
2. Submit and execute the transaction — `action_delete_account` deletes `alice.near`'s state and enqueues `Receipt::new_balance_refund("nonexistent999.near", account_balance)` [3](#0-2) .
3. When this refund receipt executes, `check_transfer_to_nonexisting_account` fails with `AccountDoesNotExist` because `nonexistent999.near` is not implicit-eligible [6](#0-5) .
4. Because the receipt's `predecessor_id` is `"system"` (a refund), the failed deposit is burned into `other_burnt_amount` instead of being returned anywhere [8](#0-7) .
5. Result: `alice.near`'s entire balance is permanently destroyed with no recipient ever receiving it.

### Citations

**File:** core/primitives/src/action/mod.rs (L70-73)
```rust
#[cfg_attr(feature = "schemars", derive(schemars::JsonSchema))]
pub struct DeleteAccountAction {
    pub beneficiary_id: AccountId,
}
```

**File:** docs/RuntimeSpec/Actions.md (L278-300)
```markdown
## DeleteAccountAction

```rust
pub struct DeleteAccountAction {
    /// The remaining account balance will be transferred to the AccountId below
    pub beneficiary_id: AccountId,
}
```

**Outcomes**:

- The account, as well as all the data stored under the account, is deleted and the tokens are transferred to `beneficiary_id`.

### Errors

**Validation Error**:

- If `beneficiary_id` is not a valid account id, the following error will be returned

```rust
/// Invalid account ID.
InvalidAccountId { account_id: AccountId },
```
```

**File:** runtime/runtime/src/actions.rs (L364-371)
```rust
    // We use current amount as a pay out to beneficiary.
    let account_balance = account_ref.amount();
    if account_balance > Balance::ZERO {
        result
            .new_receipts
            .push(Receipt::new_balance_refund(&delete_account.beneficiary_id, account_balance));
    }
    let remove_result = remove_account(state_update, account_id)?;
```

**File:** runtime/runtime/src/actions.rs (L857-877)
```rust
fn check_transfer_to_nonexisting_account(
    config: &RuntimeConfig,
    account_id: &AccountId,
    implicit_account_creation_eligible: bool,
) -> Result<(), ActionError> {
    if implicit_account_creation_eligible
        && account_is_implicit(account_id, config.wasm_config.eth_implicit_accounts)
    {
        // OK. It's implicit account creation.
        // Notes:
        // - Transfer action has to be the only action in the transaction to avoid
        // abuse by hijacking this account with other public keys or contracts.
        // - Refunds don't automatically create accounts, because refunds are free and
        // we don't want some type of abuse.
        // - Account deletion with beneficiary creates a refund, so it'll not create a
        // new account.
        Ok(())
    } else {
        Err(ActionErrorKind::AccountDoesNotExist { account_id: account_id.clone() }.into())
    }
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

**File:** runtime/runtime/src/lib.rs (L993-1001)
```rust
        let gas_refund_result = if receipt.predecessor_id().is_system() {
            // If the refund fails tokens are burned.
            if result.result.is_err() {
                stats.balance.other_burnt_amount = safe_add_balance(
                    stats.balance.other_burnt_amount,
                    total_deposit(&action_receipt.actions())?,
                )?
            }
            GasRefundResult::default()
```

**File:** runtime/runtime/src/lib.rs (L2910-2957)
```rust
fn action_transfer_or_implicit_account_creation(
    account: &mut Option<Account>,
    deposit: Balance,
    is_refund: bool,
    action_receipt: &VersionedActionReceipt,
    receipt: &Receipt,
    state_update: &mut TrieUpdate,
    apply_state: &ApplyState,
    actor_id: &mut AccountId,
    epoch_info_provider: &dyn EpochInfoProvider,
) -> Result<(), RuntimeError> {
    Ok(if let Some(account) = account.as_mut() {
        let is_gas_refund = is_refund && action_receipt.signer_id() == receipt.receiver_id();
        // For gas refunds, try to refund to the gas key first. If the signer key is a gas key,
        // the refund goes to the gas key balance and we skip crediting the account balance.
        if is_gas_refund
            && try_refund_gas_key_balance(
                state_update,
                receipt.receiver_id(),
                &action_receipt.signer_public_key(),
                deposit,
            )?
        {
            return Ok(());
        }
        action_transfer(account, deposit)?;
        if is_gas_refund {
            try_refund_allowance(
                state_update,
                receipt.receiver_id(),
                &action_receipt.signer_public_key(),
                deposit,
            )?;
        }
    } else {
        debug_assert!(!is_refund);
        action_implicit_account_creation_transfer(
            state_update,
            &apply_state,
            &apply_state.config.fees,
            account,
            actor_id,
            receipt.receiver_id(),
            deposit,
            apply_state.block_height,
            epoch_info_provider,
        );
    })
```

**File:** docs/RuntimeSpec/Refunds.md (L10-13)
```markdown
Refund receipts are identified by having `predecessor_id == "system"`. They are also special because they don't cost any gas to generate or execute. As a result, they also do not contribute to the block gas limit.

If the execution of a refund fails, the refund amount is burnt.
The refund receipt is an `ActionReceipt` that consists of a single action `Transfer` with the `deposit` amount of the refund.
```
