This confirms the analog. `action_delete_account` (`runtime/runtime/src/actions.rs:354-363`) computes `compute_gas_key_balance_sum` across all of an account's gas keys and rejects deletion with `GasKeyBalanceTooHigh` if the sum exceeds `GasKeyInfo::MAX_BALANCE_TO_BURN` (1 NEAR) — the same threshold enforced per-key in `delete_gas_key` (`runtime/runtime/src/access_keys.rs:103-111`). Since `TransferToGasKey` is exempt from the actor-permission check in `check_actor_permissions` (`runtime/runtime/src/actions.rs:739-785`, listed alongside `CreateAccount`/`FunctionCall`/`Transfer`), any third party who knows a victim's gas-key public key (visible on-chain) can call `action_transfer_to_gas_key` (`runtime/runtime/src/access_keys.rs:257-288`) to push that key's `GasKeyInfo.balance` over the 1 NEAR ceiling without the owner's consent — permanently blocking that key's deletion and, if it's the account's only/last remaining excess, blocking account deletion entirely.

### Title
Unprivileged `TransferToGasKey` deposits let anyone permanently lock a victim's gas key/account by exceeding the burn cap - (File: runtime/runtime/src/access_keys.rs)

### Summary
`TransferToGasKeyAction` requires no permission check tying it to the account owner: `check_actor_permissions` explicitly allows it to be executed by any `actor_id`, unlike `WithdrawFromGasKey`, `AddKey`, `DeleteKey`, `Stake`, etc., which require `actor_id == account_id`. [1](#0-0)  This means any account can send a `TransferToGasKey` action to any other account's gas key, as long as they know the gas key's public key (which is public on-chain data readable via `view_access_keys`).

### Finding Description
`action_transfer_to_gas_key` looks up the target gas key purely from `(account_id, public_key)` and adds `action.deposit` to `gas_key_info.balance` with no restriction on who the caller (`predecessor`/`actor_id`) is: [2](#0-1) 

Meanwhile, gas-key deletion enforces a hard cap: `delete_gas_key` refuses to delete (and thus refuses to burn) a gas key whose `balance` exceeds `GasKeyInfo::MAX_BALANCE_TO_BURN` (1 NEAR), returning `ActionErrorKind::GasKeyBalanceTooHigh` and leaving the key completely intact: [3](#0-2) 

The same cap is enforced in aggregate for `action_delete_account`, which sums the balances of *all* of an account's gas keys via `compute_gas_key_balance_sum` and rejects the entire account deletion if the sum exceeds the 1 NEAR cap: [4](#0-3) 

Because any unprivileged account can call `TransferToGasKey`, an attacker can, without the victim's consent, top up a victim's gas key balance above 1 NEAR. This is directly analogous to the reported `TradingVault.deposit()` issue where a caller deposits on behalf of a user to unilaterally impose an unwanted state change (there, a lock timestamp; here, a balance that trips a hard-coded threshold) that blocks the victim's own withdrawal/cleanup operation.

### Impact Explanation
This causes a permanent freezing/DoS condition initiated by an unprivileged third party:
- The specific gas key can never be deleted via `DeleteKey` (`GasKeyBalanceTooHigh` is returned every time, and the key/nonces/storage usage are left untouched).
- If the attacker inflates the aggregate gas-key balance across an account past 1 NEAR, `DeleteAccount` itself is permanently blocked (`ActionErrorKind::GasKeyBalanceTooHigh` at the account level), since the delete path refuses to proceed when it cannot safely burn the balances.
- The `MAX_BALANCE_TO_BURN` cap exists specifically to bound how much value can be silently burned on deletion; it was not designed as a target for third-party griefing. Because the attacker pays only the deposit amount (~1 NEAR or slightly more, since gas is cheap and this is a normal `TransferToGasKey` action) to permanently deny the victim's ability to delete the key or account, this is a low-cost, high-impact denial-of-service/fund-freezing primitive — the victim's storage-staked balance behind the account/key becomes permanently unrecoverable through the normal deletion path.

### Likelihood Explanation
Gas-key public keys are public on-chain state (readable via `view_access_key`/`view_access_keys`), so an attacker can trivially target any account with an existing gas key. The action itself, `TransferToGasKey`, has no special preconditions and is cheap to execute (deposit ≈ slightly over 1 NEAR, no elevated permissions). Since `check_actor_permissions` explicitly does not restrict this action's actor, no signature, no relationship, and no consent from the account owner is required.

### Recommendation
Restrict `Action::TransferToGasKey` to the same actor-permission rule as `Action::WithdrawFromGasKey` (i.e., require `actor_id == account_id`) in `check_actor_permissions`, or otherwise decouple the deletion-burn cap from a value that unprivileged third parties can inflate (e.g., cap only the amount actually burned rather than blocking the whole deletion, or letting the owner reclaim/withdraw excess balance before deletion regardless of who funded it).

### Proof of Concept
1. Victim account `alice.near` has a gas key `GK` with `balance = 0`.
2. Attacker `mallory.near` (no special permission) submits a transaction with a single `Action::TransferToGasKey(TransferToGasKeyAction { public_key: GK, deposit: 2 NEAR })` targeting `alice.near`. `check_actor_permissions` allows this since `TransferToGasKey` is unrestricted. [5](#0-4) 
3. `action_transfer_to_gas_key` executes, setting `GK.balance = 2 NEAR`, exceeding `MAX_BALANCE_TO_BURN = 1 NEAR`. [6](#0-5) 
4. Alice submits `Action::DeleteKey(GK)`; `delete_gas_key` returns `ActionErrorKind::GasKeyBalanceTooHigh` and the key remains, undeleted and unusable for cleanup. [7](#0-6) 
5. If Alice instead tries `Action::DeleteAccount`, and the aggregate gas-key balance (including `GK`) exceeds 1 NEAR, `action_delete_account` likewise fails with `GasKeyBalanceTooHigh`, permanently preventing account deletion. [4](#0-3)

### Citations

**File:** runtime/runtime/src/actions.rs (L354-363)
```rust
    let gas_key_balance_to_burn = compute_gas_key_balance_sum(state_update, account_id)?;
    if gas_key_balance_to_burn > GasKeyInfo::MAX_BALANCE_TO_BURN {
        result.result = Err(ActionErrorKind::GasKeyBalanceTooHigh {
            account_id: account_id.clone(),
            public_key: None,
            balance: gas_key_balance_to_burn,
        }
        .into());
        return Ok(());
    }
```

**File:** runtime/runtime/src/actions.rs (L739-781)
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
        Action::CreateAccount(_)
        | Action::FunctionCall(_)
        | Action::Transfer(_)
        | Action::TransferToGasKey(_) => (),
        Action::Delegate(_) | Action::DelegateV2(_) => (),
```

**File:** runtime/runtime/src/access_keys.rs (L93-111)
```rust
fn delete_gas_key(
    config: &RuntimeConfig,
    state_update: &mut TrieUpdate,
    account: &mut Account,
    result: &mut ActionResult,
    account_id: &AccountId,
    public_key: &PublicKey,
    access_key: &AccessKey,
    gas_key_info: &GasKeyInfo,
) -> Result<(), RuntimeError> {
    if gas_key_info.balance > GasKeyInfo::MAX_BALANCE_TO_BURN {
        result.result = Err(ActionErrorKind::GasKeyBalanceTooHigh {
            account_id: account_id.clone(),
            public_key: Some(Box::new(public_key.clone())),
            balance: gas_key_info.balance,
        }
        .into());
        return Ok(());
    }
```

**File:** runtime/runtime/src/access_keys.rs (L257-288)
```rust
pub(crate) fn action_transfer_to_gas_key(
    state_update: &mut TrieUpdate,
    result: &mut ActionResult,
    account_id: &AccountId,
    action: &TransferToGasKeyAction,
) -> Result<(), RuntimeError> {
    let Some(mut access_key) = get_access_key(state_update, account_id, &action.public_key)? else {
        result.result = Err(ActionErrorKind::GasKeyDoesNotExist {
            account_id: account_id.clone(),
            public_key: Box::new(action.public_key.clone()),
        }
        .into());
        return Ok(());
    };
    let Some(gas_key_info) = access_key.gas_key_info_mut() else {
        // Key exists but is not a gas key
        result.result = Err(ActionErrorKind::GasKeyDoesNotExist {
            account_id: account_id.clone(),
            public_key: Box::new(action.public_key.clone()),
        }
        .into());
        return Ok(());
    };

    gas_key_info.balance = gas_key_info.balance.checked_add(action.deposit).ok_or_else(|| {
        RuntimeError::StorageError(StorageError::StorageInconsistentState(
            "gas key balance integer overflow".to_string(),
        ))
    })?;
    set_access_key(state_update, account_id.clone(), action.public_key.clone(), &access_key);
    Ok(())
}
```
