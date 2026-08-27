Confirmed: `validate_delete_action` only checks that `beneficiary_id` is a syntactically valid `AccountId` (`AccountId::validate`), never that it exists on-chain [1](#0-0) . `action_delete_account` then unconditionally converts the account's full balance into a `Receipt::new_balance_refund(&delete_account.beneficiary_id, account_balance)` [2](#0-1) , which is emitted as a system-predecessor `Transfer` action [3](#0-2) . Per the documented refund semantics, "If the execution of a refund fails, the refund amount is burnt" [4](#0-3) , and this is enforced in the runtime: for `receipt.predecessor_id().is_system()`, a failing result adds the deposit to `stats.balance.other_burnt_amount` instead of generating a further refund [5](#0-4) . A `Transfer` to a non-existent, non-implicit named account fails with `AccountDoesNotExist` [6](#0-5) , and unlike regular transfers, refund receipts do not get the implicit-account-creation exemption (comment: "Refunds don't automatically create accounts, because refunds are free and we don't want some type of abuse") [7](#0-6) .

### Title
`DeleteAccount` with a nonexistent (or later-invalidated) `beneficiary_id` permanently burns the account's entire balance - (File: `runtime/runtime/src/actions.rs`)

### Summary
An ordinary, unprivileged account owner (or their relayer in a meta-transaction) who submits a `DeleteAccount` action naming a `beneficiary_id` that does not exist as an account at refund time causes their entire remaining NEAR balance to be irrecoverably burnt, rather than returned or the deletion rejected.

### Finding Description
`DeleteAccountAction::beneficiary_id` is only validated for account-ID *syntax*, not existence, at `validate_delete_action` [1](#0-0) . `action_delete_account` immediately removes the account and queues a `Transfer` of the full balance to that address via `Receipt::new_balance_refund` [8](#0-7) . This refund receipt has `predecessor_id == "system"` [3](#0-2) . When that receipt is later processed, if `beneficiary_id` does not exist and is not an implicit account, `check_account_existence`/`check_transfer_to_nonexisting_account` reject it with `AccountDoesNotExist`, since system refunds are explicitly excluded from implicit-account-creation eligibility [7](#0-6) . The runtime then treats this as a failed system-predecessor receipt and burns the deposit instead of refunding it further [5](#0-4) , matching the documented behavior that failed refunds are burnt [4](#0-3) .

This is functionally the same bug class as the external report: an action that moves funds to an address which cannot legitimately receive/hold them causes irreversible loss, and there is no safeguard preventing an ordinary caller from choosing such a target. Unlike the Solidity report (which needed a privileged owner and was deemed out-of-scope/acknowledged), here the actor is the unprivileged owner of their own account executing a normal, permitted `DeleteAccount` action — no privilege escalation is required, and the loss is a protocol-level fund-burning defect, not merely a helper-contract design choice.

Because a beneficiary can also cease to exist between when a `DeleteAccount` transaction is signed/queued and when the resulting refund receipt actually executes (e.g., the beneficiary account is itself deleted, or is a cross-shard target whose existence changes across blocks/relayed meta-transactions), even a beneficiary that was valid at signing time is not guaranteed to exist at refund-processing time, making this reachable without any typo — simply through concurrent activity or relayer-mediated meta-transactions (`Action::DelegateV2`/`Delegate`).

### Impact Explanation
Successful exploitation (or accidental triggering) causes concrete, permanent loss of the deleting account's full balance — the funds are not returned to the signer, not sent to the beneficiary, and not recoverable; they are subtracted from total supply as burnt. This satisfies the "permanent freezing/loss of funds" impact bar. For balances of significant value, this is a severe unintended token-burning path reachable by any ordinary user's transaction, requiring no elevated permissions, malicious peer, or node compromise.

### Likelihood Explanation
Likelihood is limited by the fact that in the common case the caller controls both the deletion and the destination (self-authored transactions), and most non-existent named beneficiaries would trigger obvious immediate failure only after the whole account is already gone — meaning a careless typo, a relayer bug in a meta-transaction workflow, or a race where the beneficiary account is deleted between signing and receipt execution, are the realistic vectors. There is no on-chain incentive check (like existence verification) that would stop this before the irrecoverable state transition (account removal) happens.

### Recommendation
Before removing the account and generating the balance-refund receipt in `action_delete_account`, verify that `beneficiary_id` refers to an existing account (or is a validly re-creatable implicit account), and reject the `DeleteAccount` action with a clear `ActionError` (e.g., extending `BeneficiaryDoesNotExist`) if it does not, mirroring the existing `AccountDoesNotExist` check used for other actions in `check_account_existence` [9](#0-8) . Alternatively, exempt balance-refund receipts targeting nonexistent beneficiaries from the "refunds don't create accounts" rule specifically for `DeleteAccount` beneficiaries, or fall back to refunding the original deleting account's predecessor when the beneficiary cannot be resolved.

### Proof of Concept
1. Create account `victim.near` with a nonzero balance and a full-access key.
2. Submit `DeleteAccount { beneficiary_id: "doesnotexist.near" }` from `victim.near` (an account that has never been created), as in the existing test helper `delete_account_with_beneficiary_set` [10](#0-9) .
3. The `DeleteAccount` action succeeds and `victim.near` is removed, with a `Receipt::new_balance_refund` queued to `doesnotexist.near`.
4. On processing this system-predecessor receipt, `check_transfer_to_nonexisting_account` rejects it with `AccountDoesNotExist` because refunds do not qualify for implicit-account creation.
5. Because the failing receipt has `predecessor_id == "system"`, the runtime burns the deposit into `other_burnt_amount` instead of issuing a further refund.
6. Net effect: `victim.near`'s entire balance is permanently destroyed with no recipient — verifiable by observing `tokens_burnt`/`balance_burnt` increase and no account (`victim.near` or `doesnotexist.near`) ending up with the funds.

### Citations

**File:** runtime/runtime/src/action_validation.rs (L399-403)
```rust
fn validate_delete_action(action: &DeleteAccountAction) -> Result<(), ActionsValidationError> {
    validate_action_account_id(&action.beneficiary_id)?;

    Ok(())
}
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

**File:** runtime/runtime/src/actions.rs (L819-827)
```rust
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

**File:** runtime/runtime/src/actions.rs (L834-855)
```rust
        Action::DeployContract(_)
        | Action::FunctionCall(_)
        | Action::Stake(_)
        | Action::AddKey(_)
        | Action::DeleteKey(_)
        | Action::DeleteAccount(_)
        | Action::Delegate(_)
        | Action::DelegateV2(_)
        | Action::DeployGlobalContract(_)
        | Action::UseGlobalContract(_)
        | Action::TransferToGasKey(_)
        | Action::WithdrawFromGasKey(_) => {
            if account.is_none() {
                return Err(ActionErrorKind::AccountDoesNotExist {
                    account_id: account_id.clone(),
                }
                .into());
            }
        }
    };
    Ok(())
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

**File:** integration-tests/src/user/mod.rs (L249-260)
```rust
    fn delete_account_with_beneficiary_set(
        &self,
        signer_id: AccountId,
        receiver_id: AccountId,
        beneficiary_id: AccountId,
    ) -> Result<FinalExecutionOutcomeView, CommitError> {
        self.sign_and_commit_actions(
            signer_id,
            receiver_id,
            vec![Action::DeleteAccount(DeleteAccountAction { beneficiary_id })],
        )
    }
```
