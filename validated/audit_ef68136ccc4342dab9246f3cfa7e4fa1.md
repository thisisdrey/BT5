### Title
Deposit permanently lost when `DeterministicStateInitAction` fails due to nonexistent global contract - (File: runtime/runtime/src/deterministic_account_id.rs)

### Summary
`action_deterministic_state_init` creates a new zero-balance account before attempting to deploy the global contract referenced in `state_init.code`, then attempts deployment via `deploy_deterministic_account` → `use_global_contract`. If the referenced `GlobalContractIdentifier` is not yet present on the receiving shard, `use_global_contract` sets `result.result = Err(GlobalContractDoesNotExist)`, and the function returns early at the `if result.result.is_err() { return Ok(()); }` guard, before the deposit-handling/refund block that follows is ever reached.

### Finding Description
The relevant control flow is: [1](#0-0) 

- If `maybe_account` is `None`, a new account is created with `Balance::ZERO` at line 34 [2](#0-1) .
- `deploy_deterministic_account` is then called, which in turn calls `use_global_contract(state_update, account_id, account, state_init.code(), result)` [3](#0-2) . If the referenced global contract code has not been replicated to this shard yet, this call sets `result.result = Err(GlobalContractDoesNotExist)` and returns.
- Back in `action_deterministic_state_init`, the check `if result.result.is_err() { return Ok(()); }` at lines 53-55 triggers immediately, skipping the deposit-handling block at lines 57-91 entirely [4](#0-3) .
- The deposit-refund logic (`check_storage_stake`, applying deposit to cover storage staking, and generating `Receipt::new_balance_refund` for the remainder) only executes when `result.result` is `Ok` [5](#0-4) .
- `action.deposit` is referenced nowhere else in the function outside of that unreachable block, so on this failure path the value is neither credited to the newly created (zero-balance) account nor refunded via any receipt.

An attacker (an ordinary client) can trigger this by submitting a `DeterministicStateInitAction` (directly, or via a delegate/meta-transaction) whose `state_init.code` references a `GlobalContractIdentifier` (`AccountId` or `CodeHash`) known to not yet be distributed to the target shard, with the action's `deposit` field set to a nonzero amount, targeting the derived deterministic account id. No special privilege, access key type, or validator/node access is needed — this is standard action submission and global contract replication is inherently eventually-consistent across shards, so an attacker can pick a target shard where a legitimately-deployed (but very recently registered) global contract has not yet arrived, or reference a contract id that transiently doesn't exist yet.

### Impact Explanation
This is a straightforward value non-conservation bug: NEAR tokens attached as `deposit` are deducted from the sender when the receipt is created (standard receipt cost accounting) but on this failure path are never applied to the receiver's account nor returned to the sender via a refund receipt. This matches the NEAR bounty category of "permanent freezing/loss of user funds" — the deposit `D` becomes permanently unaccounted for/burned.

### Likelihood Explanation
- The attacker only needs to know or guess a `GlobalContractIdentifier` not yet replicated to a particular shard — this is a normal race condition during global contract distribution and requires no privileged access.
- Cost to the attacker is limited to gas fees plus the deposit amount they choose to lose (repeatable at attacker's own expense, or targeted at victims via meta-transactions/delegate actions carrying victim-funded deposits, though the described flow specifically covers self-inflicted or victim-relayed deposit loss).
- The bug is deterministically reproducible: any `DeterministicStateInitAction` with a currently-nonexistent global contract reference and `deposit > 0` triggers it every time.

### Recommendation
Move the deposit handling/refund logic so it executes regardless of whether `deploy_deterministic_account` succeeds. Specifically, when `result.result.is_err()` after `deploy_deterministic_account` fails and the account was newly created with zero balance, issue a full `Receipt::new_balance_refund(receipt.balance_refund_receiver(), action.deposit)` for the entire `action.deposit` before returning, mirroring the conservation guarantee provided in the success path (lines 57-91).

### Proof of Concept
Add a unit test in `runtime/runtime/src/deterministic_account_id.rs` (or an integration test in `test-loop-tests/src/tests/deterministic_account_id.rs`) that:
1. Constructs a `DeterministicStateInitAction` whose `state_init.code()` references a `GlobalContractIdentifier` (e.g., an `AccountId` or `CodeHash`) that has not been registered/replicated in the test's `TrieUpdate`/global contract state.
2. Sets `action.deposit` to a nonzero value (e.g., 1 NEAR).
3. Calls `action_deterministic_state_init` directly with `maybe_account = &mut None` on a fresh account id.
4. Asserts that `result.result` is `Err(ActionErrorKind::... GlobalContractDoesNotExist ...)` (or equivalent error surfaced from `use_global_contract`).
5. Asserts that either (a) the resulting account (if inserted into `maybe_account`) has `amount() == action.deposit`, or (b) `result.new_receipts` contains a `Receipt::new_balance_refund` for the full `action.deposit`.
6. Currently, both assertions in step 5 fail — the account remains at zero balance and `result.new_receipts` is empty — demonstrating the deposit is permanently lost.

### Citations

**File:** runtime/runtime/src/deterministic_account_id.rs (L26-55)
```rust
    let storage_usage_config = &apply_state.config.fees.storage_usage_config;
    let account = match maybe_account {
        Some(account) => account,
        None => {
            // cspell:ignore nonexist
            // `nonexist` -> `uninit` account state transition
            // Create with zero balance now and check later how much of the
            // provided deposit is needed.
            let new_account = create_deterministic_account(Balance::ZERO, storage_usage_config);
            maybe_account.insert(new_account)
        }
    };
    if account.contract().is_none() {
        // `uninit` -> `active` account state transition. "uninit" here is the
        // NEP-616 sense, a deterministic account with no contract yet, not
        // `Account::Uninitialized`: a `0u` id can never reach this, because
        // `validate_deterministic_state_init` pins the receiver to the derived
        // `0s` id.
        deploy_deterministic_account(
            state_update,
            account,
            account_id,
            &action.state_init,
            result,
            storage_usage_config,
        )?;
    }
    if result.result.is_err() {
        return Ok(());
    }
```

**File:** runtime/runtime/src/deterministic_account_id.rs (L57-91)
```rust
    // Use attached deposit to satisfy storage staking requirements and refund
    // the rest.
    let deposit_refund = match check_storage_stake(account, account.amount(), &apply_state.config) {
        Ok(_) => {
            // no additional storage needed, refunding all
            action.deposit
        }
        Err(StorageStakingError::LackBalanceForStorageStaking(missing_amount)) => {
            if missing_amount <= action.deposit {
                // use exactly as much as needed and refund the rest
                let new_balance = safe_add_balance(account.amount(), missing_amount)?;
                account.set_amount(new_balance);
                action
                    .deposit
                    .checked_sub(missing_amount)
                    .expect("just checked missing_amount <= action.deposit")
            } else {
                result.result = Err(ActionErrorKind::LackBalanceForState {
                    account_id: account_id.clone(),
                    amount: missing_amount,
                }
                .into());
                return Ok(());
            }
        }
        Err(StorageStakingError::StorageError(err)) => {
            return Err(RuntimeError::StorageError(StorageError::StorageInconsistentState(err)));
        }
    };

    if deposit_refund > Balance::ZERO {
        result
            .new_receipts
            .push(Receipt::new_balance_refund(receipt.balance_refund_receiver(), deposit_refund));
    }
```

**File:** runtime/runtime/src/deterministic_account_id.rs (L130-133)
```rust
    use_global_contract(state_update, account_id, account, state_init.code(), result)?;
    if result.result.is_err() {
        return Ok(());
    }
```
