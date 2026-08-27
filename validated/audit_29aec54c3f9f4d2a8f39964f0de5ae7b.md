### Title
Self-beneficiary `DeleteAccount` burns the deleted account's remaining balance instead of refunding it - ([File: runtime/runtime/src/actions.rs])

### Summary
Calling `DeleteAccount` with `beneficiary_id` set to the account's own `account_id` (the "transfer to self" analog) causes the account's remaining NEAR balance to be permanently burned rather than paid out, because the balance-refund receipt is generated *after* the account is removed and refund receipts are explicitly forbidden from re-creating accounts.

### Finding Description
`action_delete_account` computes the account's leftover balance and, if nonzero, emits a system-generated refund receipt to `delete_account.beneficiary_id` before calling `remove_account` to delete the account from state: [1](#0-0) 

Nothing in this function (or in `check_actor_permissions`/`validate_action`, per the delete-account tests) rejects the case `beneficiary_id == account_id`, unlike the Unlock Protocol bug where `transferFrom` allowed `_from == _recipient`.

If the beneficiary is the same account that is being deleted, the resulting `Receipt::new_balance_refund` is addressed to an account that no longer exists once this receipt executes. Refund receipts are explicitly designed to *not* auto-create accounts — this is documented directly in the runtime code: [2](#0-1) 

That comment states verbatim: "Account deletion with beneficiary creates a refund, so it'll not create a new account." Consequently, when the refund receipt is later processed, `check_account_existence`/`check_transfer_to_nonexisting_account` will reject it with `AccountDoesNotExist` because `implicit_account_creation_eligible` refunds are excluded from implicit re-creation and the account no longer exists in state.

Per the documented refund semantics, a failed refund is burned rather than requeued or returned to the user: [3](#0-2) 

This is corroborated by the pytest Rosetta test comment describing exactly this scenario for the AccountCostIncrease surplus refund sent to a just-deleted account: "that one is sent to the (now deleted) account, so it has no balance-changing operation." [4](#0-3) 

The end-to-end effect: an ordinary account owner (or a full-access key holder) that calls `DeleteAccount` with `beneficiary_id` equal to itself will have its account deleted and its remaining balance's refund receipt fail and be burned — the tokens are permanently destroyed instead of being paid to any beneficiary.

### Impact Explanation
This causes permanent, unrecoverable token loss: the leftover balance of the deleted account (potentially the entirety of the user's remaining funds) is burned rather than transferred anywhere. This matches the "token inflation or loss" / "permanent freezing of funds" bar, since burned tokens are irrecoverably destroyed for the account owner with no recourse, triggered purely by a self-inflicted (but easily made, e.g. by wallets/SDKs defaulting `beneficiary_id` to the caller) transaction from an ordinary signer — no special privileges required.

### Likelihood Explanation
Likelihood is moderate: it requires a user (or a wallet/SDK bug) to submit a `DeleteAccount` action with `beneficiary_id` equal to the account being deleted. This is a plausible operator/user error, analogous to the original Unlock Protocol finding, especially since `delete_account_with_beneficiary_set`/the client helper `delete_account` in `integration-tests/src/user/mod.rs` sets `beneficiary_id = signer_id` by default in one convenience path: [5](#0-4) 
showing that "self as beneficiary" is a natural/default value that could be replicated by external tooling, increasing the chance of accidental fund destruction.

### Recommendation
Add an explicit validation check (in `validate_action` for `DeleteAccountAction`, or at the start of `action_delete_account`) rejecting `beneficiary_id == account_id`, similar to the recommended `require(_from != _recipient)` fix in the original report, so users cannot accidentally target themselves as beneficiary and burn their remaining balance.

### Proof of Concept
1. Create account `alice.near` with balance `X`.
2. Submit `SignedTransaction::delete_account(nonce, "alice.near", "alice.near", beneficiary_id: "alice.near", signer, block_hash)` (i.e., `beneficiary_id == account_id`), mirroring the pattern used in `SignedTransaction::delete_account` test helpers: [6](#0-5) 
3. `action_delete_account` removes the account and emits `Receipt::new_balance_refund(&"alice.near", X)`.
4. When that refund receipt executes, `alice.near` no longer exists; `check_transfer_to_nonexisting_account` rejects it (refunds don't create accounts).
5. Per refund semantics, the failed refund's deposit `X` is added to `other_burnt_amount` and permanently destroyed — `alice.near`'s remaining balance is gone with no beneficiary receiving it.

### Citations

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

**File:** docs/RuntimeSpec/Refunds.md (L10-12)
```markdown
Refund receipts are identified by having `predecessor_id == "system"`. They are also special because they don't cost any gas to generate or execute. As a result, they also do not contribute to the block gas limit.

If the execution of a refund fails, the refund amount is burnt.
```

**File:** pytest/tests/sanity/rosetta.py (L984-991)
```python
        # The delete-account receipt refunds the remaining balance to the beneficiary. With the
        # feature it also emits a gas-price surplus refund; that one is sent to the (now deleted)
        # account, so it has no balance-changing operation and is only referenced, not fetched.
        # Gas burns at the same price as before, so the gas amounts differ between the two modes.
        delete_receipt_related = [{
            'direction': 'forward',
            'transaction_identifier': receipt_id_2
        }]
```

**File:** integration-tests/src/user/mod.rs (L262-268)
```rust
    fn delete_account(
        &self,
        signer_id: AccountId,
        receiver_id: AccountId,
    ) -> Result<FinalExecutionOutcomeView, CommitError> {
        self.delete_account_with_beneficiary_set(signer_id.clone(), receiver_id, signer_id)
    }
```

**File:** core/primitives/src/test_utils.rs (L418-434)
```rust
    pub fn delete_account(
        nonce: Nonce,
        signer_id: AccountId,
        receiver_id: AccountId,
        beneficiary_id: AccountId,
        signer: &Signer,
        block_hash: CryptoHash,
    ) -> Self {
        Self::from_actions(
            nonce,
            signer_id,
            receiver_id,
            signer,
            vec![Action::DeleteAccount(DeleteAccountAction { beneficiary_id })],
            block_hash,
        )
    }
```
