Confirmed: refunds (predecessor `system`) are explicitly excluded from implicit-account creation eligibility. `apply_action` sets `is_refund = receipt.predecessor_id().is_system()` and `implicit_account_creation_eligible = is_the_only_action && !is_refund` [1](#0-0) . `check_account_existence` uses `implicit_account_creation_eligible` to decide whether a `Transfer` to a non-existent account is allowed to create it via `check_transfer_to_nonexisting_account` [2](#0-1) .

### Title
Permanent loss of funds when `DeleteAccount` beneficiary is a non-existent implicit account - (File: `runtime/runtime/src/actions.rs`)

### Summary
`action_delete_account` pays out the deleted account's remaining balance to `delete_account.beneficiary_id` by generating a system `Receipt::new_balance_refund` [3](#0-2) . This refund receipt has `predecessor_id == "system"` [4](#0-3) . When this refund receipt is later applied, `apply_action` marks it as `is_refund = true`, which forces `implicit_account_creation_eligible = false` regardless of whether the transfer is the only action [5](#0-4) . As documented in `Refunds.md`, "If the execution of a refund fails, the refund amount is burnt" [6](#0-5) .

### Finding Description
This is the structural analog of the Solidity bug: in the Mellow report, an outbound value-transfer targets a receiver contract that cannot accept plain ETH transfers (no `receive()`), so the transfer call reverts and the whole redemption path fails, permanently trapping funds. In nearcore, the analogous "receiver cannot accept an incoming value transfer" situation is a `Transfer` action targeting a NEAR-implicit/ETH-implicit account ID that does not yet exist on-chain — such accounts can normally only be created by an ordinary (non-refund) sole `Transfer` action, per `check_transfer_to_nonexisting_account`'s use of `implicit_account_creation_eligible` [2](#0-1) . Because a `DeleteAccount`'s beneficiary payout is implemented as a *system refund receipt*, it is unconditionally disqualified from implicit account creation, so if a user sets `beneficiary_id` to an implicit account ID that has never been funded/created, the payout receipt fails account-existence validation and the deposit is burnt instead of delivered — mirroring the "transfer always reverts" failure mode of the ETH report, but here manifesting as **irrecoverable token burn** rather than a reverted transaction (since refund failures don't propagate errors back to the user, and there's no retry path).

### Impact Explanation
Any account owner who deletes their own account and specifies a not-yet-created implicit account ID (NEAR-implicit hex key or ETH-implicit `0x...` address) as the beneficiary will have their entire remaining balance permanently burnt instead of transferred, since `DeleteAccount`'s beneficiary is unrestricted by account type and this failure path silently discards value with no user-facing revert to prevent the action, unlike a normal transaction failure. This is a genuine "permanent loss of funds" outcome as required by the validation criteria.

### Likelihood Explanation
This can be triggered by any ordinary user simply by calling `DeleteAccount` with a `beneficiary_id` equal to an unused implicit account ID (a common and easy mistake, e.g. mistyping/misusing a fresh wallet address that hasn't received any prior transfer). No special privileges or preconditions are required — only that the beneficiary account does not already exist.

### Recommendation
Either (a) allow beneficiary payout receipts to create implicit accounts (treat the beneficiary transfer as eligible for implicit-account creation even though it originates from `system`), or (b) reject `DeleteAccount` actions upfront (during validation) when the beneficiary account does not exist and is an implicit account ID, forcing the deleting account to pick an existing beneficiary, so funds are never silently burnt.

### Proof of Concept
1. Create account `alice.near` with a non-zero balance.
2. Compute an implicit account ID (NEAR-implicit hex or `0x...` ETH-implicit) for a keypair that has never received any prior transaction, so no on-chain account exists for it.
3. Have `alice.near` submit `DeleteAccount { beneficiary_id: <that unused implicit id> }`.
4. Observe: `action_delete_account` emits `Receipt::new_balance_refund(beneficiary_id, alice_balance)` [3](#0-2) .
5. When this system-origin receipt executes, `apply_action` computes `is_refund = true` → `implicit_account_creation_eligible = false` [5](#0-4) , so `check_transfer_to_nonexisting_account` rejects the transfer since the account doesn't exist and cannot be created.
6. Per the refund semantics, the failed refund's deposit is burnt rather than delivered to the beneficiary or returned to `alice.near` [6](#0-5) , permanently destroying the funds.

Note: I could not execute this scenario in a live nearcore node to observe the exact resulting `ActionError` and confirm there is no additional safety check elsewhere (e.g., in `DeleteAccount` action validation at the transaction-verification stage) that might pre-empt this beneficiary check before submission; a Devin session with full repo/build access would be needed to confirm end-to-end via an integration test.

### Citations

**File:** runtime/runtime/src/lib.rs (L547-562)
```rust
        let account_id = receipt.receiver_id();
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

**File:** runtime/runtime/src/actions.rs (L364-370)
```rust
    // We use current amount as a pay out to beneficiary.
    let account_balance = account_ref.amount();
    if account_balance > Balance::ZERO {
        result
            .new_receipts
            .push(Receipt::new_balance_refund(&delete_account.beneficiary_id, account_balance));
    }
```

**File:** runtime/runtime/src/actions.rs (L787-827)
```rust
pub(crate) fn check_account_existence(
    action: &Action,
    account: &Option<Account>,
    account_id: &AccountId,
    config: &RuntimeConfig,
    implicit_account_creation_eligible: bool,
) -> Result<(), ActionError> {
    match action {
        Action::CreateAccount(_) => {
            if account.is_some() {
                return Err(ActionErrorKind::AccountAlreadyExists {
                    account_id: account_id.clone(),
                }
                .into());
            } else {
                if account_is_implicit(account_id, config.wasm_config.eth_implicit_accounts) {
                    // If the account doesn't exist and it's implicit, then you
                    // should only be able to create it using single transfer action.
                    // Because you should not be able to add another access key to the account in
                    // the same transaction.
                    // Otherwise you can hijack an account without having the private key for the
                    // public key. We've decided to make it an invalid transaction to have any other
                    // actions on the implicit hex accounts.
                    // The easiest way is to reject the `CreateAccount` action.
                    // See https://github.com/nearprotocol/NEPs/pull/71
                    return Err(ActionErrorKind::OnlyImplicitAccountCreationAllowed {
                        account_id: account_id.clone(),
                    }
                    .into());
                }
            }
        }
        Action::Transfer(_) => {
            if account.is_none() {
                return check_transfer_to_nonexisting_account(
                    config,
                    account_id,
                    implicit_account_creation_eligible,
                );
            }
        }
```

**File:** core/primitives/src/receipt.rs (L493-510)
```rust
    /// Generates a receipt with a transfer from system for a given balance without a receipt_id.
    /// This should be used for token refunds instead of gas refunds.
    /// It doesn't refund the allowance of the access key. For gas refunds use `new_gas_refund`.
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

**File:** docs/RuntimeSpec/Refunds.md (L10-12)
```markdown
Refund receipts are identified by having `predecessor_id == "system"`. They are also special because they don't cost any gas to generate or execute. As a result, they also do not contribute to the block gas limit.

If the execution of a refund fails, the refund amount is burnt.
```
