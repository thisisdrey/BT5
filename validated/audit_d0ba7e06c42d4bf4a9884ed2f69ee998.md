### Title
`action_create_account` unconditionally overwrites an existing account's balance/state instead of rejecting creation when the target account already exists - ([File: runtime/runtime/src/actions.rs])

### Summary
`action_create_account` in `runtime/runtime/src/actions.rs` never checks whether `*account` is already `Some(..)` before replacing it. It only validates the naming rule (top-level vs. `is_sub_account_of(predecessor_id)`), and then unconditionally overwrites the account record with a fresh zero-balance `Account`. If a batched receipt (`CreateAccount` → `DeployContract`/`AddKey`) targets a sub-account name that already exists under an attacker-controlled parent namespace, the pre-existing account's balance, locked stake, and contract pointer are wiped and replaced, and the attacker's subsequent `AddKeyWithFullAccess` action installs their key on top of the (now zeroed) account.

### Finding Description
`action_create_account` (`runtime/runtime/src/actions.rs`, lines 167-210) performs only two checks:
- for top-level names: length + registrar restriction,
- for sub-accounts: `account_id.is_sub_account_of(predecessor_id)`. [1](#0-0) 

There is no check of the form `if account.is_some() { return Err(AccountAlreadyExists) }`. The function ends by unconditionally doing:
```
*actor_id = account_id.clone();
*account = Some(Account::new(Balance::ZERO, Balance::ZERO, AccountContract::None, fee_config.storage_usage_config.num_bytes_account));
```
which replaces whatever `Account` state was previously loaded for `account_id` (balance, locked/staked amount, contract pointer) with a fresh, zero-balance record — regardless of whether that account previously existed and held funds/state.

Reachability requires only that the attacker control the immediate parent account named in the receipt's `predecessor_id` (e.g. `attacker.near`), because the only gate is `is_sub_account_of`. If `victim.attacker.near` was previously provisioned under that same namespace (e.g. by a factory/registrar-style contract deployed at `attacker.near` that hands out sub-accounts to different end users, or an account that changed effective control while remaining a literal sub-account of `attacker.near`), the attacker can send a promise batch `CreateAccount` + `DeployContract` + `AddKeyWithFullAccess(attacker_pubkey)` targeting `victim.attacker.near`. `action_create_account` does not reject this because it never inspects the pre-existing account state; it only validates the naming relationship, which the attacker already legitimately satisfies as owner of `attacker.near`.

### Impact Explanation
The batch causes the victim account's `amount`/`locked` balance to be reset to zero and its contract state pointer cleared, then a `DeployContract` and attacker-controlled full-access key are installed. This is a permanent loss/freezing-of-funds bug: the victim's previously recorded on-chain balance for that account is destroyed with no compensating credit, and the account's contract/code is replaced without the original owner's consent — matching the "theft or permanent freezing of user funds" bounty category.

### Likelihood Explanation
Preconditions are narrow but realistic: the attacker must control the *immediate parent* account of the target (`attacker.near`), and the target sub-account (`victim.attacker.near`) must already exist and be relied upon by a different party for value (e.g. a shared-parent/factory pattern where the parent's owner can still re-issue `CreateAccount` for previously-issued child names). Given that precondition, the attack is a single self-funded transaction (cost = gas + minimal deposit) and is trivially repeatable against any sub-account under the attacker's own namespace.

### Recommendation
Add an existence check at the top of `action_create_account` (or at its call site before invoking it) that rejects the action with `ActionErrorKind::AccountAlreadyExists` (or equivalent) whenever `account.is_some()` for `account_id`, ensuring `CreateAccount` can only initialize genuinely new accounts and never overwrite/zero an existing one.

### Proof of Concept
Runtime apply-level unit test:
1. Seed state with an existing account `victim.attacker.near` funded with a non-zero balance and an access key belonging to a different (victim) keypair.
2. Submit a receipt from `attacker.near` with actions `[CreateAccount, DeployContract(bytes), AddKey(attacker_pubkey, FullAccess)]` targeting `victim.attacker.near`.
3. Apply the receipt through the runtime and assert:
   - The action result contains `ActionErrorKind::AccountAlreadyExists` (after the fix) instead of success.
   - Pre-fix (current code), the test demonstrates the bug: `Account::amount()` for `victim.attacker.near` becomes `0`, the victim's original access key remains but the account's contract/storage state has been replaced, and the attacker's key is now present with `FullAccess` permission — confirming silent balance destruction and unauthorized key injection.

### Citations

**File:** runtime/runtime/src/actions.rs (L167-210)
```rust
pub(crate) fn action_create_account(
    fee_config: &RuntimeFeesConfig,
    account_creation_config: &AccountCreationConfig,
    account: &mut Option<Account>,
    actor_id: &mut AccountId,
    account_id: &AccountId,
    predecessor_id: &AccountId,
    result: &mut ActionResult,
) {
    if account_id.is_top_level() {
        if account_id.len() < account_creation_config.min_allowed_top_level_account_length as usize
            && predecessor_id != &account_creation_config.registrar_account_id
        {
            // A short top-level account ID can only be created registrar account.
            result.result = Err(ActionErrorKind::CreateAccountOnlyByRegistrar {
                account_id: account_id.clone(),
                registrar_account_id: account_creation_config.registrar_account_id.clone(),
                predecessor_id: predecessor_id.clone(),
            }
            .into());
            return;
        } else {
            // OK: Valid top-level Account ID
        }
    } else if !account_id.is_sub_account_of(predecessor_id) {
        // The sub-account can only be created by its root account. E.g. `alice.near` only by `near`
        result.result = Err(ActionErrorKind::CreateAccountNotAllowed {
            account_id: account_id.clone(),
            predecessor_id: predecessor_id.clone(),
        }
        .into());
        return;
    } else {
        // OK: Valid sub-account ID by proper predecessor.
    }

    *actor_id = account_id.clone();
    *account = Some(Account::new(
        Balance::ZERO,
        Balance::ZERO,
        AccountContract::None,
        fee_config.storage_usage_config.num_bytes_account,
    ));
}
```
