### Title
Frozen or Non-Accepting Beneficiary Address in `vesting::distribute()` Causes Full Distribution DoS for All Shareholders — (`aptos-move/framework/aptos-framework/sources/vesting.move`)

---

### Summary

`vesting::distribute()` iterates over every shareholder in a vesting contract and calls `aptos_account::deposit_coins()` for each one inside a single atomic transaction. There is no per-recipient error handling. If any one deposit aborts — because the recipient's APT fungible store is frozen, or because the recipient has disabled direct coin transfers and is not yet registered for APT — the entire transaction reverts. All other shareholders are blocked from receiving their vested funds until the blocking condition is resolved.

---

### Finding Description

`vesting::distribute()` is a public entry function callable by anyone:

```
public entry fun distribute(contract_address: address) acquires VestingContract {
    assert_active_vesting_contract(contract_address);
    let vesting_contract = borrow_global_mut<VestingContract>(contract_address);
    let coins = withdraw_stake(vesting_contract, contract_address);
    ...
    let shareholders = &grant_pool.shareholders();
    shareholders.for_each_ref(|shareholder| {
        ...
        let recipient_address = get_beneficiary(vesting_contract, shareholder);
        aptos_account::deposit_coins(recipient_address, share_of_coins);  // ← can abort
    });
``` [1](#0-0) 

`aptos_account::deposit_coins()` has two abort paths:

**Path 1 — `allow_arbitrary_coin_transfers = false`**: If the recipient account exists but is not yet registered for `AptosCoin`, the function asserts `can_receive_direct_coin_transfers(to)`. Any account owner can call `set_allow_direct_coin_transfers(false)` on their own address (unprivileged). If the admin later sets that address as a shareholder's beneficiary, or if the shareholder controls their own beneficiary address, the deposit aborts. [2](#0-1) 

**Path 2 — Frozen fungible store**: `coin::deposit` routes through `primary_fungible_store::deposit` → `dispatchable_fungible_asset::deposit`, which asserts `!fa_store.frozen`. If the APT fungible store of any beneficiary is frozen (e.g., by the framework during an incident response), the deposit aborts. [3](#0-2) 

The spec file for `aptos_account` explicitly documents that `deposit_coins` aborts if the recipient's store is frozen: [4](#0-3) 

The identical structural issue exists in `staking_contract::distribute_internal()`, which loops over all distribution pool shareholders and calls `aptos_account::deposit_coins()` for each: [5](#0-4) 

---

### Impact Explanation

When `distribute()` aborts mid-loop, the entire transaction is rolled back. The coins that were already extracted from the stake pool are not transferred to any shareholder. The vesting contract's state is unchanged, so the distribution must be retried. Until the blocking beneficiary address is fixed (unfrozen, or re-registered), every call to `distribute()` will revert. All shareholders — including those whose deposits would have succeeded — are denied their vested APT for the duration of the block.

This is a temporary but complete DoS on vesting distributions. Vesting contracts can hold up to 30 shareholders (`MAXIMUM_SHAREHOLDERS = 30`), so a single bad beneficiary blocks up to 29 other shareholders. [6](#0-5) 

---

### Likelihood Explanation

- **Path 1 (unprivileged)**: A shareholder who controls their own beneficiary address (or whose beneficiary is set to a fresh address they control) can call `set_allow_direct_coin_transfers(false)` before the address is registered for APT. This is a low-cost, unprivileged action. The admin's `update_beneficiary` function makes it possible to set any address as a beneficiary.
- **Path 2 (privileged)**: Requires the Aptos framework to freeze an APT store, which is an incident-response action. Low probability but non-zero.

Overall likelihood is low, but the structural absence of per-recipient error handling means the condition is permanent until manually resolved.

---

### Recommendation

Wrap each individual `deposit_coins` call in a guard that checks preconditions before attempting the deposit, and skip (or defer) recipients whose deposit would fail rather than aborting the entire loop. Concretely:

1. Before calling `deposit_coins(recipient_address, share_of_coins)`, check `primary_fungible_store::is_frozen(recipient_address, apt_metadata)` and `can_receive_direct_coin_transfers(recipient_address)`.
2. If either check fails, accumulate the skipped amount and send it to the `withdrawal_address` (or hold it in a claimable escrow per shareholder).
3. Apply the same fix to `staking_contract::distribute_internal()`.

---

### Proof of Concept

1. Admin creates a vesting contract with shareholders `[A, B, C]`.
2. Admin calls `update_beneficiary(contract_address, A, fresh_address)` where `fresh_address` is a new account controlled by shareholder A.
3. Shareholder A calls `aptos_account::set_allow_direct_coin_transfers(false)` on `fresh_address` (unprivileged entry function).
4. `fresh_address` has never received APT, so `coin::is_account_registered<AptosCoin>(fresh_address)` returns false.
5. Anyone calls `vesting::distribute(contract_address)`.
6. The loop reaches shareholder A's beneficiary (`fresh_address`), calls `deposit_coins(fresh_address, coins)`.
7. `deposit_coins` checks `can_receive_direct_coin_transfers(fresh_address)` → returns `false` → aborts with `EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS`.
8. The entire transaction reverts. Shareholders B and C receive nothing.
9. Every subsequent call to `distribute()` reverts identically until the admin changes A's beneficiary or A re-enables direct transfers.

### Citations

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L96-97)
```text
    /// Maximum number of shareholders a vesting pool can support.
    const MAXIMUM_SHAREHOLDERS: u64 = 30;
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L719-740)
```text
    public entry fun distribute(contract_address: address) acquires VestingContract {
        assert_active_vesting_contract(contract_address);

        let vesting_contract = borrow_global_mut<VestingContract>(contract_address);
        let coins = withdraw_stake(vesting_contract, contract_address);
        let total_distribution_amount = coin::value(&coins);
        if (total_distribution_amount == 0) {
            coin::destroy_zero(coins);
            return
        };

        // Distribute coins to all shareholders in the vesting contract.
        let grant_pool = &vesting_contract.grant_pool;
        let shareholders = &grant_pool.shareholders();
        shareholders.for_each_ref(|shareholder| {
            let shareholder = *shareholder;
            let shares = pool_u64::shares(grant_pool, shareholder);
            let amount = pool_u64::shares_to_amount_with_total_coins(grant_pool, shares, total_distribution_amount);
            let share_of_coins = coin::extract(&mut coins, amount);
            let recipient_address = get_beneficiary(vesting_contract, shareholder);
            aptos_account::deposit_coins(recipient_address, share_of_coins);
        });
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.move (L123-130)
```text
        if (!coin::is_account_registered<CoinType>(to)) {
            assert!(
                can_receive_direct_coin_transfers(to),
                error::permission_denied(EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS)
            );
            coin::register<CoinType>(&create_signer(to));
        };
        coin::deposit<CoinType>(to, coins)
```

**File:** aptos-move/framework/aptos-framework/sources/fungible_asset.move (L974-987)
```text
    inline fun withdraw_sanity_check_impl<T: key>(
        owner_address: address, store: Object<T>, abort_on_dispatch: bool
    ) {
        assert!(
            object::owns(store, owner_address),
            error::permission_denied(ENOT_STORE_OWNER)
        );
        let fa_store = borrow_store_resource(&store);
        assert!(
            !abort_on_dispatch || !has_withdraw_dispatch_function(fa_store.metadata),
            error::invalid_argument(EINVALID_DISPATCHABLE_OPERATIONS)
        );
        assert!(!fa_store.frozen, error::permission_denied(ESTORE_IS_FROZEN));
    }
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.spec.move (L228-229)
```text
        let if_exist_coin = exists<coin::CoinStore<CoinType>>(to);
        aborts_if if_exist_coin && global<coin::CoinStore<CoinType>>(to).frozen;
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L888-901)
```text
        // Buy all recipients out of the distribution pool.
        while (distribution_pool.shareholders_count() > 0) {
            let recipients = distribution_pool.shareholders();
            let recipient = recipients[0];
            let current_shares = distribution_pool.shares(recipient);
            let amount_to_distribute =
                distribution_pool.redeem_shares(recipient, current_shares);
            // If the recipient is the operator, send the commission to the beneficiary instead.
            if (recipient == operator) {
                recipient = beneficiary_for_operator(operator);
            };
            aptos_account::deposit_coins(
                recipient, coin::extract(&mut coins, amount_to_distribute)
            );
```
