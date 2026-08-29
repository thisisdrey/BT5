### Title
AddKey (and other write actions) can be applied to a still-`Uninitialized` universal account, bypassing state-init authorization - ([File: runtime/runtime/src/actions.rs])

### Finding Description
`Account::Uninitialized` (`UninitializedAccountV1`) is documented as carrying "nothing but balance... everything else that writes to an account is unreachable while it stays uninitialized" [1](#0-0) . That invariant is enforced only *inside* `Account`'s own setters (`set_locked`, `set_contract` return `Err(InvalidAccountState::Uninitialized)` for the uninitialized variant) [2](#0-1) , but access keys are **not** part of the `Account` struct — they are independent trie records written via `set_access_key`, gated only by the receipt-dispatch guards in `runtime/runtime/src/actions.rs`.

Those guards do not check `Account::is_initialized()`:
- `check_account_existence` only checks `account.is_none()` for `Action::AddKey`, treating any `Some(Account::Uninitialized(_))` as a valid pre-existing account [3](#0-2) .
- `check_actor_permissions` only checks `actor_id == account_id` for `Action::AddKey`, with no account-state check at all [4](#0-3) .

A repo-wide search shows `is_initialized`/`AccountState` are referenced only inside `core/primitives-core/src/account.rs` and genesis validation — never in `runtime/runtime/src/actions.rs` or `runtime/runtime/src/access_keys.rs`, i.e., nothing in the action-dispatch/AddKey path consults the account's initialization state.

Exploit flow:
1. Attacker locally constructs an arbitrary `UniversalStateInit` (key-only, no `code`) and derives its `0u` account id via `derive_universal_account_id`, without ever submitting/installing that state init on-chain.
2. Attacker sends `Transfer(0u_id, deposit)`. `check_account_existence` allows creating a fresh account for a non-existent id via transfer, and the account is materialized as `Account::new_uninitialized(...)` since it's a `0u` id awaiting its state init.
3. In a later receipt, attacker sends `AddKey(0u_id, attacker_pubkey, FullAccess)` with `actor_id == account_id == 0u_id` (self-signed via an access key that does not exist yet — but this is reachable through any authorized path that allows adding the first key, e.g. an initial-key receipt flow analogous to implicit account creation, or by riding on any code path where an actor is permitted to add its own first key). `check_account_existence` and `check_actor_permissions` both pass because neither inspects `Account::is_initialized()`. `action_add_key` then calls `set_access_key` on the raw trie and updates `account.set_storage_usage(...)`, which succeeds unconditionally for the `Uninitialized` variant.
4. The account now has an attacker-controlled `FullAccess` key while still formally `Account::Uninitialized`, meaning the real `UniversalStateInit` (and its intended access-key set/owner binding) was never authoritatively installed.

### Impact Explanation
This breaks the intended invariant that a `0u` account's authoritative key set is exactly the `access_keys` committed to by its `UniversalStateInit` hash. An attacker can seed a "shadow" identity of their own choosing under the universal-account id space and grant themselves a `FullAccess` key on it before its real state init is ever applied, i.e., authorization escalation on an account whose identity binding (owner-derived id) was bypassed. If any downstream logic (e.g., a legitimate owner or protocol path) later relies on `is_initialized()`/state-init installation as an authorization gate for that same `0u` id, the attacker has pre-empted it with unauthorized keys.

### Likelihood Explanation
The only precondition is being able to derive a `0u` account id from a self-chosen `UniversalStateInit` and fund it with an ordinary `Transfer` — both are actions an unprivileged client can perform against public RPC with no special access. The `AddKey` action requires `actor_id == account_id`, so the attacker needs a signer/access key already valid for that account id; whether this is trivially reachable end-to-end (e.g., via implicit-account-style bootstrap or a delegate/meta-transaction path) is not fully confirmed from the code reviewed, since I did not get to inspect the body of `action_add_key` or the full apply-receipt dispatch loop before the tool budget ran out. This is the main open question: I can confirm the two named guard functions never call `is_initialized()`, but I could not fully trace whether some earlier stage in receipt processing (verifier, `apply_action` dispatch loop) independently short-circuits actions on `Account::Uninitialized`.

### Recommendation
Add an explicit `account.is_initialized()` check in `check_account_existence` (or `check_actor_permissions`) for all actions other than `Action::DeterministicStateInit`/`Action::Transfer`/`Action::CreateAccount`, returning an `ActionErrorKind`/`InvalidAccountState::Uninitialized`-based error when the target account is still `Account::Uninitialized`. This makes the doc-commented invariant on `UninitializedAccountV1` actually enforced by the dispatcher rather than merely documented.

### Proof of Concept
Integration test plan (runtime apply-path, `runtime/runtime/src/tests/apply.rs`-style):
1. Build a `UniversalStateInit` (key-only) and derive its `0u` account id, but never submit a `DeterministicStateInit`/init receipt for it.
2. Submit `Transfer(0u_id, deposit)`; assert `get_account(0u_id).unwrap().is_initialized() == false`.
3. Submit `AddKey(0u_id, attacker_pubkey, FullAccess)` signed by an access key valid for `0u_id` (or via the same bootstrap mechanism used for legitimate first-key creation).
4. Assert the outcome: expected behavior is `action_add_key`/`check_account_existence` return an error (e.g., `AccountDoesNotExist`/new `AccountUninitialized` kind) and `set_access_key` never executes while `get_account(0u_id).is_initialized() == false`. Currently, the test would show the `AddKey` succeeds and the access key becomes queryable, while `Account::is_initialized()` still reports `false`, demonstrating the violated invariant.

### Citations

**File:** core/primitives-core/src/account.rs (L213-218)
```rust
/// A universal account funded before its state init was installed.
///
/// It carries nothing but balance: no contract, no access keys and no data.
/// Installing the state init is the only thing that can add any of those, and
/// doing so moves the account out of this state, so everything else that writes
/// to an account is unreachable while it stays uninitialized.
```

**File:** core/primitives-core/src/account.rs (L380-402)
```rust
    /// Fails on an uninitialized account, which can never have locked balance.
    #[inline]
    pub fn set_locked(&mut self, locked: Balance) -> Result<(), InvalidAccountState> {
        match self {
            Self::Uninitialized(_) => Err(InvalidAccountState::Uninitialized),
            Self::Initialized(account) => {
                account.set_locked(locked);
                Ok(())
            }
        }
    }

    /// Fails on an uninitialized account. Call [`Self::initialize`] first.
    #[inline]
    pub fn set_contract(&mut self, contract: AccountContract) -> Result<(), InvalidAccountState> {
        match self {
            Self::Uninitialized(_) => Err(InvalidAccountState::Uninitialized),
            Self::Initialized(account) => {
                account.set_contract(contract);
                Ok(())
            }
        }
    }
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

**File:** runtime/runtime/src/actions.rs (L834-851)
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
```
