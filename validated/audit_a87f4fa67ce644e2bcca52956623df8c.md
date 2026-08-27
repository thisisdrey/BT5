Confirmed: `check_actor_permissions` in `runtime/runtime/src/actions.rs:739-785` explicitly places `Action::TransferToGasKey(_)` in the group requiring **no actor check** (line 780: `Action::CreateAccount(_) | Action::FunctionCall(_) | Action::Transfer(_) | Action::TransferToGasKey(_) => ()`), unlike `WithdrawFromGasKey`/`DeleteKey`/`DeleteAccount` which require `actor_id == account_id`. This means, exactly like a permissionless ERC20 donation, **any predecessor (any account or contract) can send a `TransferToGasKey` action to any other account**, crediting balance to a specific existing gas key it does not own, exactly as anyone can `IERC20.transfer` LP tokens into the victim strategy contract in the original report.

That balance then feeds into an aggregate, non-per-caller-tracked check: `compute_gas_key_balance_sum` (`core/store/src/utils/mod.rs:457-497`) sums **all** gas-key balances on the account, and `action_delete_account` (`runtime/runtime/src/actions.rs:354-363`) rejects the whole `DeleteAccount` action with `GasKeyBalanceTooHigh` if that sum exceeds `GasKeyInfo::MAX_BALANCE_TO_BURN` (1 NEAR, `core/primitives-core/src/account.rs:816-818`). The same aggregate-threshold pattern is used per-key in `delete_gas_key` (`runtime/runtime/src/access_keys.rs:93-111`) for `DeleteKey`.

### Title
Permissionless `TransferToGasKey` donations let anyone grief `DeleteAccount`/`DeleteKey` by pushing gas-key balances over the burn cap - (File: `runtime/runtime/src/actions.rs`)

### Summary
`TransferToGasKey` requires no actor/authorization check (`check_actor_permissions`, `runtime/runtime/src/actions.rs:739-785`), so any unprivileged account or contract can add NEAR to an existing gas key belonging to any other account, exactly like anyone being able to `transfer()` LP tokens into a victim contract in the original Convex report. This donated balance is folded into an aggregate check — `compute_gas_key_balance_sum` (`core/store/src/utils/mod.rs:457-497`) — that `action_delete_account` (`runtime/runtime/src/actions.rs:354-363`) and `delete_gas_key` (`runtime/runtime/src/access_keys.rs:93-111`) use to gate `DeleteAccount`/`DeleteKey`, rejecting the action outright (`GasKeyBalanceTooHigh`) once the sum exceeds `GasKeyInfo::MAX_BALANCE_TO_BURN` (1 NEAR).

### Finding Description
`action_transfer_to_gas_key` (`runtime/runtime/src/access_keys.rs:257-288`) simply looks up the gas key by `(account_id, public_key)` and adds `action.deposit` to `gas_key_info.balance` — there is no requirement that the predecessor of the receipt equals `account_id`. This is corroborated by `check_actor_permissions` (`runtime/runtime/src/actions.rs:739-785`), which explicitly exempts `Action::TransferToGasKey(_)` from the `actor_id != account_id` check applied to `WithdrawFromGasKey`, `DeleteKey`, `AddKey`, and `DeleteAccount`. Anyone can therefore construct a `Transfer`-style receipt (or a `promise_batch_action_transfer_to_gas_key` host call from any contract, `runtime/near-vm-runner/src/logic/logic.rs:3197-3245`) targeting a victim account and any of its known/observable gas-key public keys (gas-key public keys are visible via `view_access_key_list`, per `chain/rosetta-rpc/src/gas_key_utils.rs:70-117`).

Later, `action_delete_account` (`runtime/runtime/src/actions.rs:314-391`) computes `gas_key_balance_to_burn = compute_gas_key_balance_sum(state_update, account_id)` — the sum of *every* gas key's balance on the account, not something the account itself controls precisely — and fails the entire `DeleteAccount` action with `ActionErrorKind::GasKeyBalanceTooHigh` if that sum exceeds `GasKeyInfo::MAX_BALANCE_TO_BURN` (1 NEAR). The same aggregate/threshold pattern gates individual gas-key deletion in `delete_gas_key` (`runtime/runtime/src/access_keys.rs:93-111`).

This is structurally the same bug class as the reported Convex issue: a privileged/self-service operation (`depositAll` / `DeleteAccount`) relies on a balance that includes value contributed by unprivileged third parties (arbitrary LP token donations / arbitrary `TransferToGasKey` donations), letting an attacker push that balance past a hard threshold and permanently block the legitimate operation.

### Impact Explanation
An attacker can watch the mempool/chain for a victim's `DeleteAccount` or gas-key `DeleteKey` transaction (or simply preemptively) and send one or more small `TransferToGasKey` deposits to gas keys on the victim's account, pushing the aggregate gas-key balance above `MAX_BALANCE_TO_BURN`. This permanently blocks `DeleteAccount` (and the corresponding `DeleteKey` for that key) from succeeding, since the account cannot control or refuse incoming `TransferToGasKey` deposits, and there is no `WithdrawFromGasKey`-then-delete race the victim can win reliably against a persistent attacker re-funding the key. This can result in **permanent freezing of the account's funds** (the account cannot be deleted to reclaim its balance to a beneficiary) — matching the "permanent freezing of funds" impact class.

### Likelihood Explanation
The attack requires only a standard signed transaction (or a contract-issued promise) with a `TransferToGasKey` action naming a public key that is a gas key on the target account — no special permissions, no cross-shard timing tricks beyond simple front-running of a `DeleteAccount`/`DeleteKey` transaction, and a griefing cost of only slightly more than 1 NEAR minus the victim's existing gas-key balance (funds that are then burned on successful deletion, but that don't need to be burned to achieve the DoS — merely to keep the sum above threshold indefinitely by re-funding after each victim `WithdrawFromGasKey` attempt).

### Recommendation
Restrict `TransferToGasKey` to require `actor_id == account_id` (i.e., self-funding only) in `check_actor_permissions`, or track/bound how much of a gas key's balance came from third parties versus the key owner, and exclude non-owner-contributed balance from `compute_gas_key_balance_sum`'s burn-threshold check (or refund excess third-party deposits automatically instead of rejecting the whole `DeleteAccount`/`DeleteKey`).

### Proof of Concept
1. Victim account `alice.near` has a `GasKeyFullAccess` key `K` with balance 0.
2. Attacker (any account, no relationship to `alice.near`) submits `SignedTransaction { signer_id: attacker, receiver_id: alice.near, actions: [Action::TransferToGasKey(TransferToGasKeyAction { public_key: K, deposit: 1_000_001_yoctoNEAR_above_1_NEAR })] }`. This succeeds because `check_actor_permissions` (`runtime/runtime/src/actions.rs:780`) does not check the actor for `TransferToGasKey`.
3. Alice later submits `Action::DeleteAccount(DeleteAccountAction { beneficiary_id: ... })`. `action_delete_account` computes `compute_gas_key_balance_sum` ≥ `MAX_BALANCE_TO_BURN` and returns `ActionErrorKind::GasKeyBalanceTooHigh` (`runtime/runtime/src/actions.rs:354-363`), permanently blocking account deletion as long as the attacker keeps the balance above the cap. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** runtime/runtime/src/actions.rs (L314-391)
```rust
pub(crate) fn action_delete_account(
    state_update: &mut TrieUpdate,
    account: &mut Option<Account>,
    actor_id: &mut AccountId,
    receipt: &Receipt,
    result: &mut ActionResult,
    account_id: &AccountId,
    delete_account: &DeleteAccountAction,
    config: &RuntimeConfig,
    current_protocol_version: ProtocolVersion,
) -> Result<(), StorageError> {
    let account_ref = account.as_ref().unwrap();
    let account_storage_usage = if ProtocolFeature::FixDeleteAccountGlobalContractStorageUsage
        .enabled(current_protocol_version)
    {
        let contract_storage = get_contract_storage_usage(state_update, account_id, account_ref)?;
        account_ref.storage_usage().saturating_sub(contract_storage)
    } else {
        // Legacy behavior: only subtracts local contract code, misses the
        // global contract identifier overhead.
        let account_storage_usage = account_ref.storage_usage();
        let code_len = get_code_len_or_default(
            state_update,
            account_id.clone(),
            account_ref.local_contract_hash().unwrap_or_default(),
        )?;
        debug_assert!(
            code_len == 0 || account_storage_usage > code_len,
            "account storage usage should be larger than code size. storage usage: {}, code size: {}",
            account_storage_usage,
            code_len
        );
        account_storage_usage.saturating_sub(code_len)
    };
    if account_storage_usage > Account::MAX_ACCOUNT_DELETION_STORAGE_USAGE {
        result.result =
            Err(ActionErrorKind::DeleteAccountWithLargeState { account_id: account_id.clone() }
                .into());
        return Ok(());
    }
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
    // We use current amount as a pay out to beneficiary.
    let account_balance = account_ref.amount();
    if account_balance > Balance::ZERO {
        result
            .new_receipts
            .push(Receipt::new_balance_refund(&delete_account.beneficiary_id, account_balance));
    }
    let remove_result = remove_account(state_update, account_id)?;
    result.tokens_burnt =
        result.tokens_burnt.checked_add(gas_key_balance_to_burn).ok_or_else(|| {
            StorageError::StorageInconsistentState("tokens_burnt overflow".to_string())
        })?;
    if remove_result.gas_key_nonce_count > 0 {
        let compute = storage_removes_compute(
            &config.wasm_config.ext_costs,
            remove_result.gas_key_nonce_count,
            remove_result.gas_key_nonce_total_key_bytes,
            AccessKey::NONCE_VALUE_LEN * remove_result.gas_key_nonce_count,
        );
        result.compute_usage = safe_add_compute(result.compute_usage, compute).map_err(|_| {
            StorageError::StorageInconsistentState("compute_usage overflow".to_string())
        })?;
    }
    *actor_id = receipt.predecessor_id().clone();
    *account = None;
    Ok(())
}

```

**File:** runtime/runtime/src/actions.rs (L739-785)
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
        Action::DeterministicStateInit(_) => (),
    };
    Ok(())
}
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

**File:** core/store/src/utils/mod.rs (L457-497)
```rust
/// Computes the total balance across all gas keys for a given account.
pub fn compute_gas_key_balance_sum(
    state_update: &TrieUpdate,
    account_id: &AccountId,
) -> Result<Balance, StorageError> {
    let mut total = Balance::ZERO;
    let lock = state_update.trie().lock_for_iter();
    for raw_key in state_update
        .locked_iter(&trie_key_parsers::get_raw_prefix_for_access_keys(account_id), &lock)?
    {
        let raw_key = raw_key?;
        let key_handle = trie_key_parsers::parse_key_handle_from_access_key_key(
            &raw_key, account_id,
        )
        .map_err(|_e| {
            StorageError::StorageInconsistentState(
                "Can't parse key handle from raw key for AccessKey".to_string(),
            )
        })?;
        let nonce_index =
            trie_key_parsers::parse_nonce_index_from_gas_key_key(&raw_key, account_id, &key_handle)
                .map_err(|_e| {
                    StorageError::StorageInconsistentState(
                        "Can't parse nonce index from raw key for AccessKey".to_string(),
                    )
                })?;
        if nonce_index.is_some() {
            continue;
        }
        if let Some(balance) = get_access_key_by_handle(state_update, account_id, &key_handle)?
            .as_ref()
            .and_then(|access_key| access_key.gas_key_info())
            .map(|gas_key_info| gas_key_info.balance)
        {
            total = total.checked_add(balance).ok_or_else(|| {
                StorageError::StorageInconsistentState("gas key balance overflow".to_string())
            })?;
        }
    }
    Ok(total)
}
```

**File:** core/primitives-core/src/account.rs (L815-823)
```rust
impl GasKeyInfo {
    /// Maximum gas key balance that can be burned during key or account deletion.
    /// Deletion fails if the (sum of) gas key balance(s) exceeds this threshold.
    pub const MAX_BALANCE_TO_BURN: Balance = Balance::from_near(1);

    pub fn borsh_len() -> usize {
        borsh::object_length(&Self { balance: Balance::from_yoctonear(0), num_nonces: 0 }).unwrap()
    }
}
```
