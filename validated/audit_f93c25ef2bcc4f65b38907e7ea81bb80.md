### Title
Deleting an account before its pending gas-key refund receipt is processed causes an unconditional panic (`"must be implicit"`), halting the shard - ([File: runtime/runtime/src/lib.rs], [File: runtime/runtime/src/actions.rs])

### Summary
`action_transfer_or_implicit_account_creation` assumes via `debug_assert!(!is_refund)` that a refund receipt can never target a nonexistent account, but this assumption is only checked in debug builds. If a user attaches gas to a gas key and then deletes their own account before the corresponding gas-refund receipt (`Receipt::new_gas_refund`) is processed, the receiver account is `None` in production, and the runtime falls into `action_implicit_account_creation_transfer`, which unconditionally panics for any `NamedAccount` id.

### Finding Description
`action_transfer_or_implicit_account_creation` in [1](#0-0)  handles both regular transfers and refunds. When the receiving `account` is `Some`, it tries `try_refund_gas_key_balance` first (matching signer/receiver for a gas refund) at [2](#0-1) . But when `account` is `None`, the code only has `debug_assert!(!is_refund)` before unconditionally calling `action_implicit_account_creation_transfer` at [3](#0-2) . In a release build this assertion is a no-op, so a refund receipt for a now-deleted account proceeds into the implicit-account-creation path.

Inside `action_implicit_account_creation_transfer`, the account id's type is matched via `account_id.get_account_type()` in [4](#0-3) . For any ordinary named account (the overwhelmingly common case for a gas-key owner), this match falls to `AccountType::NamedAccount => panic!("must be implicit")` at [5](#0-4) . This is a live `panic!`, not test-only code, and executing it during receipt application crashes chunk application, i.e. halts block/chunk production for that shard.

Exploit flow reachable by an ordinary unprivileged user:
1. Attacker's account `A` (a normal, non-implicit account id) has a gas key (`GasKeyInfo`) and submits a `FunctionCall`/meta-transaction action attaching gas from that gas key, addressed to a receiver on another shard (to introduce delay/cross-chunk timing), generating a pending gas-refund receipt back to `A` per [6](#0-5) .
2. Before that refund receipt is applied, `A` submits (and gets included) a `DeleteAccount` transaction removing account `A` from state.
3. When the refund receipt is later processed, `get_account(A)` returns `None`; `try_refund_gas_key_balance` is never reached because the `Some(account)` branch is skipped; the `None` branch is taken, `debug_assert!(!is_refund)` is compiled out, and `action_implicit_account_creation_transfer` panics because `A` is a `NamedAccount`.

No existing signature, nonce, access-key, or gas-metering check prevents self-deletion of an account with an outstanding gas-key refund in flight; deletion validity checks for `DeleteAccount` do not consider unresolved refund receipts targeting the account.

### Impact Explanation
This is a shard-halting panic reachable purely with unprivileged transaction submission (self-funded account, own DeleteAccount transaction, own gas-key transaction) — no validator/node/network access required. A panic during chunk application in production nearcore triggers node crashes / consensus stalls for the affected shard, matching the "shard-halting panic" bounty category.

### Likelihood Explanation
Preconditions are entirely within attacker control: create/fund an account with a gas key, issue one gas-key-funded call whose refund is delayed (e.g., cross-shard call), then submit `DeleteAccount` for the same account before the refund settles. This requires no special privileges, is cheap (a couple of transactions), and is deterministically repeatable given the receipt/refund timing is attacker-influenced (the attacker chooses when to send the `DeleteAccount` transaction relative to the refund receipt's expected arrival).

### Recommendation
In `action_transfer_or_implicit_account_creation`, when `account` is `None` and `is_refund` is true, do not fall through to `action_implicit_account_creation_transfer`. Instead handle the deleted-account refund case explicitly (e.g., burn the refund, log/telemetry it, or route it to a protocol-defined fallback), and promote the `debug_assert!` to a real runtime check (return an error/`ActionErrorKind`) so this condition cannot reach `action_implicit_account_creation_transfer`, which should keep its `panic!("must be implicit")` only as a truly unreachable invariant.

### Proof of Concept
Add an apply.rs-based test that:
1. Creates account `A` with a gas key (`GasKeyInfo`) and enough balance.
2. Submits a `FunctionCall` transaction from `A` using the gas key, targeting a receiver that requires at least one extra chunk to return the refund (e.g., cross-shard receiver, or a receipt that becomes delayed).
3. In the same or an immediately following chunk (before the refund receipt is applied), submits a `DeleteAccount` transaction removing account `A`.
4. Applies chunks until the gas-refund receipt (`ReceiptEnum` with `predecessor==receiver==A`, `is_refund=true`) would be processed.
5. Asserts on the outcome: expect either (a) the apply panics with `"must be implicit"` (confirming the crash), or, once fixed, (b) the refund is deterministically burnt/handled without panicking and without unexpectedly creating an implicit account for `A`'s id.

### Citations

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

**File:** runtime/runtime/src/actions.rs (L112-132)
```rust
/// Tries to refund gas to a gas key's balance.
/// Returns true if the key exists and is a gas key (balance was credited).
/// Returns false otherwise (key not found or is not a gas key).
pub(crate) fn try_refund_gas_key_balance(
    state_update: &mut TrieUpdate,
    account_id: &AccountId,
    public_key: &PublicKey,
    deposit: Balance,
) -> Result<bool, StorageError> {
    let Some(mut access_key) = get_access_key(state_update, account_id, public_key)? else {
        return Ok(false);
    };
    let Some(gas_key_info) = access_key.gas_key_info_mut() else {
        return Ok(false);
    };
    gas_key_info.balance = gas_key_info.balance.checked_add(deposit).ok_or_else(|| {
        StorageError::StorageInconsistentState("gas key balance integer overflow".to_string())
    })?;
    set_access_key(state_update, account_id.clone(), public_key.clone(), &access_key);
    Ok(true)
}
```

**File:** runtime/runtime/src/actions.rs (L224-271)
```rust
    *actor_id = account_id.clone();
    match account_id.get_account_type() {
        AccountType::NearImplicitAccount => {
            let mut access_key = AccessKey::full_access();
            access_key.nonce = initial_nonce_value(block_height);

            // unwrap: here it's safe because the `account_id` has already been determined to be implicit by `get_account_type`
            let public_key = PublicKey::from_near_implicit_account(account_id).unwrap();

            *account = Some(Account::new(
                deposit,
                Balance::ZERO,
                AccountContract::None,
                fee_config.storage_usage_config.num_bytes_account
                    + public_key.trie_id_len() as u64
                    + borsh::object_length(&access_key).unwrap() as u64
                    + fee_config.storage_usage_config.num_extra_bytes_record,
            ));

            set_access_key(state_update, account_id.clone(), public_key, &access_key);
        }
        // Invariant: The `account_id` is implicit.
        // It holds because in the only calling site, we've checked the permissions before.
        AccountType::EthImplicitAccount => {
            let chain_id = epoch_info_provider.chain_id();

            // Use a deployed global contract for ETH implicit accounts.
            let global_contract_hash = eth_wallet_global_contract_hash(&chain_id);
            let storage_usage = fee_config.storage_usage_config.num_bytes_account
                + global_contract_hash.as_bytes().len() as u64;

            *account = Some(Account::new(
                deposit,
                Balance::ZERO,
                AccountContract::Global(global_contract_hash),
                storage_usage,
            ));
        }
        AccountType::NearDeterministicAccount => {
            *account = Some(create_deterministic_account(
                deposit,
                &apply_state.config.fees.storage_usage_config,
            ));
        }
        // This panic is unreachable as this is an implicit account creation transfer.
        // `check_account_existence` would fail because `account_is_implicit` would return false for a Named account.
        AccountType::NamedAccount => panic!("must be implicit"),
    }
```
