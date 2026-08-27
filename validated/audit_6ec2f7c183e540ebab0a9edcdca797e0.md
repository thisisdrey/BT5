### Title
Permanent burn of an account's balance when `DeleteAccountAction.beneficiary_id` does not exist on-chain — ([File: runtime/runtime/src/actions.rs])

### Summary
`action_delete_account` pays out an account's full remaining balance to an attacker/user-chosen `beneficiary_id` by pushing a "refund" receipt, without first checking that the beneficiary account exists. If the beneficiary account does not exist, the payout Transfer fails and — because it travels as a refund receipt — the entire balance is permanently burned instead of being returned to anyone. This is the nearcore analog of the reported bug class: an unvalidated/failure-prone payout recipient causes funds that should be recoverable to become permanently unretrievable.

### Finding Description
When an account is deleted, `action_delete_account` unconditionally enqueues the account's balance as a refund receipt to the caller-supplied `beneficiary_id`, deletes the account from state, and returns: [1](#0-0) 

The only validation ever performed on `beneficiary_id` is that it is a syntactically valid account id string (`InvalidAccountId`) — there is no check that the account actually exists at execution time: [2](#0-1) 

The payout is implemented via `Receipt::new_balance_refund`, i.e. a system-predecessor "refund" receipt. Refund receipts are special: they are free to execute, and critically, if their execution fails, the transferred amount is burnt rather than bounced back to anyone: [3](#0-2) 

Whether a `Transfer` to a non-existent account succeeds depends on `check_transfer_to_nonexisting_account`, which only allows implicit account auto-creation for ordinary (non-refund) receipts; the comment explicitly documents that refund receipts are deliberately excluded from implicit-account auto-creation to prevent abuse: [4](#0-3) 

Putting these together: if `beneficiary_id` is a syntactically valid but non-existent account (a typo'd named account, an account that has been deleted, or a near-implicit/eth-implicit-looking id that has never been funded), the follow-up Transfer inside the refund receipt fails with `AccountDoesNotExist`. Because it is a refund receipt, this failure does not return the balance to the original owner (whose account has already been removed from state by this point) or to the beneficiary — it silently burns the entire amount. There is no mechanism analogous to a normal deposit-refund (which goes back to `predecessor_id` on failure) because the balance payout on `DeleteAccount` is itself modeled as the one-shot refund, with no place left to bounce it back to.

This mirrors the reported bug class conceptually: the payout recipient is fixed at receipt-construction time with no verification that it can actually receive funds, and if it can't, the funds are unrecoverable — except here the failure mode is outright token burn rather than merely being stuck in a contract.

### Impact Explanation
An ordinary user who deletes their own account (or a relayer executing a meta-transaction `DeleteAccount`/self-deleting contract) and specifies a `beneficiary_id` that turns out not to exist at execution time (e.g., a typo, a beneficiary account that gets deleted in the same or an earlier block, or copy-pasting a public-key-derived implicit account id that has never been used) will have their entire account balance permanently destroyed instead of the action failing safely or the funds being returned. This is a permanent, protocol-level loss of user funds triggered from a completely standard, unprivileged transaction path (`DeleteAccountAction`), matching the "permanent freezing/loss of funds" impact bar.

### Likelihood Explanation
Likelihood is moderate: it requires the `beneficiary_id` to be invalid at delete-execution time. This can happen accidentally (typos are common in beneficiary fields; users commonly delete accounts and specify a beneficiary they believe exists, especially fresh/implicit accounts that have never received a transfer) or via a race condition (beneficiary account deleted between transaction construction and its cross-shard receipt execution). No special privilege is required to trigger it — only a normal `DeleteAccount` transaction with careless/racy input.

### Recommendation
Before deleting the account and enqueuing the balance-refund receipt, verify the `beneficiary_id` account currently exists (or fail the `DeleteAccount` action with an explicit error such as `BeneficiaryAccountDoesNotExist`, refunding the deposit through the normal deposit-refund path instead of via the payout channel). Alternatively, exempt the delete-account beneficiary payout from the "refunds never create implicit accounts" restriction so a non-existent implicit `beneficiary_id` at least gets created rather than burning funds, and still hard-fail for non-existent *named* accounts prior to removing the source account from state.

### Proof of Concept
Not runnable from the index alone — full confirmation would require tracing `Receipt::new_balance_refund`'s resulting `ActionReceipt` through `apply_action_receipt` → `apply_action` → `action_transfer_or_implicit_account_creation` to verify the exact `implicit_account_creation_eligible` flag value used for refund receipts, which I was unable to inspect directly within available iterations (only the doc-comment in `check_transfer_to_nonexisting_account` was found, stating refunds are intentionally excluded from implicit-account creation). A concrete PoC would be:
1. Create account `victim` with a nonzero balance.
2. Send `DeleteAccountAction { beneficiary_id: "doesnotexist.near" }` (or a well-formed but unfunded 64-hex/`0x…` implicit id) from `victim`.
3. Observe `victim`'s balance is deleted from state, the beneficiary transfer receipt fails with `AccountDoesNotExist`, and per `docs/RuntimeSpec/Refunds.md` the amount is added to `other_burnt_amount` rather than appearing in any account — confirmed by the existing burn-accounting code path for failed refunds: [5](#0-4)

### Citations

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

**File:** docs/RuntimeSpec/Refunds.md (L10-13)
```markdown
Refund receipts are identified by having `predecessor_id == "system"`. They are also special because they don't cost any gas to generate or execute. As a result, they also do not contribute to the block gas limit.

If the execution of a refund fails, the refund amount is burnt.
The refund receipt is an `ActionReceipt` that consists of a single action `Transfer` with the `deposit` amount of the refund.
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
