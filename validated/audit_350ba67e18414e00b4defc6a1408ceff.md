### Title
Postponed receipts survive `DeleteAccount` and can force a `Stake` action against a later account reusing the same name - ([File: core/store/src/utils/mod.rs])

### Finding Description
`remove_account` (`core/store/src/utils/mod.rs:505-575`) only removes `TrieKey::Account`, `TrieKey::ContractCode`, access keys/gas-key nonces, and `TrieKey::ContractData` for the deleted account: [1](#0-0) 

It never touches `TrieKey::PostponedReceipt`, `TrieKey::PendingDataCount`, or `TrieKey::PostponedReceiptId` entries keyed by that same `account_id`. `action_delete_account` (`runtime/runtime/src/actions.rs:314-390`) calls `remove_account` and then clears the in-memory `account` (`= None`), so `check_actor_permissions`'s `DeleteAccountStaking` check only inspects the *current* `account.locked()` field: [2](#0-1) 

That check has no knowledge of, and does not block deletion because of, a still-pending postponed receipt.

Separately, `check_actor_permissions` for `Action::Stake` only requires `actor_id == account_id` (string equality), with no notion of "same owner across account lifetimes": [3](#0-2) 

`actor_id` for a freshly-arriving action receipt is initialized from the receipt's own `predecessor_id` field (`runtime/runtime/src/lib.rs:855`), which was frozen into the receipt the moment it was postponed - it is *not* re-derived from anything about the account's current owner.

Exploit flow:
1. Attacker's contract at account `A` issues two receipts in the same execution: (a) a self-directed `ActionReceipt` with `receiver_id == predecessor_id == "A"`, containing `Action::Stake`, and with an `input_data_id` the attacker controls but withholds; (b) a `DeleteAccount` receipt (`locked == 0`, satisfying `DeleteAccountStaking`).
2. Receipt (a) has an unsatisfied dependency, so `process_action_receipt` (`runtime/runtime/src/lib.rs:1593-1655`) stores it as a `PostponedReceipt`/`PendingDataCount`/`PostponedReceiptId` triplet keyed by `receiver_id == "A"`.
3. Receipt (b) executes `action_delete_account`, which calls `remove_account` - deleting the account/keys/contract data for `A` but leaving the postponed-receipt trie entries for `A` untouched.
4. Later, a different party creates/claims account `A` again (e.g., via a shared registrar/factory pattern where subaccount names are recycled) and deposits their own funds.
5. The attacker finally delivers the withheld `DataReceipt` for the dependency. `process_receipt`'s `Data` branch (`runtime/runtime/src/lib.rs:1398-1455`) looks up `PostponedReceiptId{receiver_id: "A", data_id}` - still present - decrements `PendingDataCount` to 0, fetches the stale postponed `Receipt`, and calls `apply_action_receipt` against whatever account currently occupies `A`.
6. Inside `apply_action_receipt`, `account_id` resolves to the new account, but `actor_id` is taken from the stale receipt's `predecessor_id` (`"A"`), so `check_actor_permissions` sees `actor_id == account_id` and approves the `Stake` action. `action_stake` (`runtime/runtime/src/actions.rs:59-110`) then moves the new owner's `amount` into `locked` up to the attacker-specified `stake.stake` (bounded only by the new account's current balance and `minimum_stake`), with no signature or consent from the new owner.

### Impact Explanation
The new account owner's freshly-deposited balance can be moved into `locked` and staked to a validator public key chosen by the attacker, without the new owner ever submitting a `Stake` transaction. This is unauthorized staking / freezing of victim funds (funds become locked pending epoch unstaking, and are staked toward a validator the victim did not choose), a violation of authorization exactness and value conservation matching the described bounty category (theft or freezing of user funds via authorization escalation across accounts/promises).

### Likelihood Explanation
The attacker needs only: (1) their own contract/account, (2) the ability to create a self-referential postponed receipt with a controlled/withheld data dependency (ordinary promise/callback mechanics available to any deployed contract), and (3) a `DeleteAccount` action with zero locked balance (also ordinary). No validator, node, or privileged access is required. The remaining precondition — that the exact account name gets reused by an unrelated party — is scenario-dependent (most realistic for shared-parent/factory subaccount naming patterns where a dapp assigns per-user subaccounts, or squatted/recycled names), but is not prevented by any protocol-level safeguard; nothing invalidates or purges the dangling postponed-receipt state on deletion, and nothing prevents account-name reuse. The attack is fully repeatable and deterministic once the naming precondition is met.

### Recommendation
`remove_account` should also enumerate and remove any `PostponedReceipt`, `PendingDataCount`, and `PostponedReceiptId` entries (and any `ReceivedData` awaiting them) keyed by the account being deleted, so no residual promise/action state can outlive the account. Additionally, `check_actor_permissions`/`apply_action_receipt` should not treat a receipt's frozen `predecessor_id` as automatically authoritative over the *currently* resolved account when a `Stake` (or any self-authorizing) action is postponed across an account-lifetime boundary — e.g., by invalidating postponed receipts whose target account was deleted/recreated since they were postponed, or by binding postponed receipts to an account "generation"/incarnation identifier that changes on `CreateAccount` after a deletion.

### Proof of Concept
Integration/runtime-test-loop plan (mirrors the existing `test_delete_account_while_staking` / `test_function_call_after_same_chunk_delete_recreate_resolves_fresh_code` style tests in `integration-tests/src/tests/standard_cases/mod.rs` and `runtime/runtime/src/tests/apply.rs`):
1. Deploy a contract at account `A` with a method that, in one execution, creates (a) a promise-chained self-receipt with an unresolved `input_data_id` and a `Stake` action, and (b) a `DeleteAccount` action (as the batch's own receipt) with `beneficiary_id` set elsewhere; call this method.
2. Assert `A`'s account is deleted (`view_account` fails) and a postponed receipt still exists for `A` in the trie (verify via reading `TrieKey::PostponedReceipt`/`PendingDataCount` directly, or indirectly by asserting the pending `data_id` link is still resolvable).
3. Recreate account `A` via `CreateAccount` + `Transfer` from a different, unrelated predecessor, depositing a balance that does not send a `Stake` transaction.
4. Deliver the previously withheld `DataReceipt` for the dependency data id.
5. Assert: the new `A.locked` is non-zero and equals (or is bounded by) the attacker-specified stake amount, even though the new owner submitted no `Stake` action — proving unauthorized locking of the new owner's balance. Compare against expected invariant: `Account.locked` should remain `0` unless the new owner explicitly staked.

### Citations

**File:** core/store/src/utils/mod.rs (L509-510)
```rust
    state_update.remove(TrieKey::Account { account_id: account_id.clone() });
    state_update.remove(TrieKey::ContractCode { account_id: account_id.clone() });
```

**File:** runtime/runtime/src/actions.rs (L739-760)
```rust
pub(crate) fn check_actor_permissions(
    action: &Action,
    account: &Option<Account>,
    actor_id: &AccountId,
    account_id: &AccountId,
) -> Result<(), ActionError> {
    match action {
        Action::DeployContract(_)
        | Action::Stake(_)
        | Action::AddKey(_)
        | Action::DeleteKey(_)
        | Action::DeployGlobalContract(_)
        | Action::UseGlobalContract(_)
        | Action::WithdrawFromGasKey(_) => {
            if actor_id != account_id {
                return Err(ActionErrorKind::ActorNoPermission {
                    account_id: account_id.clone(),
                    actor_id: actor_id.clone(),
                }
                .into());
            }
        }
```

**File:** runtime/runtime/src/actions.rs (L761-776)
```rust
        Action::DeleteAccount(_) => {
            if actor_id != account_id {
                return Err(ActionErrorKind::ActorNoPermission {
                    account_id: account_id.clone(),
                    actor_id: actor_id.clone(),
                }
                .into());
            }
            let account = account.as_ref().unwrap();
            if !account.locked().is_zero() {
                return Err(ActionErrorKind::DeleteAccountStaking {
                    account_id: account_id.clone(),
                }
                .into());
            }
        }
```
