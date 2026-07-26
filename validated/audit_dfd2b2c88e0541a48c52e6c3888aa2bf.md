Based on my research, I have found a valid analog. Let me confirm the exact `coin::deposit` implementation and the `maybe_convert_to_fungible_store` behavior before writing the final output.

### Title
Unchecked `deposit_coins` in shareholder distribution loop permanently freezes all vesting balances - (`File: aptos-move/framework/aptos-framework/sources/vesting.move`)

---

### Summary

`vesting::distribute()` iterates over every shareholder and calls `aptos_account::deposit_coins()` inside a closure with no error handling. `deposit_coins` aborts when the recipient has migrated away from the legacy `CoinStore` (making `coin::is_account_registered` return `false`) **and** has called `set_allow_direct_coin_transfers(false)`. Both actions are unprivileged. A single shareholder who performs them causes every subsequent `distribute()` call to revert, permanently freezing the entire vesting pool's APT for all shareholders. The same structural defect exists in `staking_contract::distribute_internal()`, which is called transitively from `vesting::withdraw_stake`.

---

### Finding Description

**Root cause — `vesting::distribute()` loop with no exception handling**

```
aptos-move/framework/aptos-framework/sources/vesting.move  lines 733-740
```

```move
shareholders.for_each_ref(|shareholder| {
    let shareholder = *shareholder;
    let shares = pool_u64::shares(grant_pool, shareholder);
    let amount = pool_u64::shares_to_amount_with_total_coins(grant_pool, shares, total_distribution_amount);
    let share_of_coins = coin::extract(&mut coins, amount);
    let recipient_address = get_beneficiary(vesting_contract, shareholder);
    aptos_account::deposit_coins(recipient_address, share_of_coins);   // ← can abort
});
``` [1](#0-0) 

The formal verifier itself acknowledges this is unverified:

```
spec distribute(contract_address: address) {
    // TODO: Can't handle abort in loop.
    pragma verify = false;
``` [2](#0-1) 

**Abort path inside `aptos_account::deposit_coins`**

```
aptos-move/framework/aptos-framework/sources/aptos_account.move  lines 123-128
```

```move
if (!coin::is_account_registered<CoinType>(to)) {
    assert!(
        can_receive_direct_coin_transfers(to),
        error::permission_denied(EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS)
    );
    coin::register<CoinType>(&create_signer(to));
};
``` [3](#0-2) 

`coin::is_account_registered<CoinType>` returns `false` once the legacy `CoinStore<AptosCoin>` resource has been removed. `maybe_convert_to_fungible_store` does exactly that — it `move_from`s the `CoinStore` and migrates the balance to the primary fungible store:

```move
let CoinStore<CoinType> { coin, frozen, deposit_events, withdraw_events } =
    move_from<CoinStore<CoinType>>(account);
``` [4](#0-3) 

After migration, `exists<CoinStore<CoinType>>(account)` is `false`, so `is_account_registered` returns `false`. If the same account has also called `set_allow_direct_coin_transfers(false)`, `can_receive_direct_coin_transfers` returns `false`, and `deposit_coins` aborts unconditionally.

**Same defect in `staking_contract::distribute_internal`**

The staking contract's distribution loop (called transitively by `vesting::withdraw_stake`) has the identical pattern:

```move
while (distribution_pool.shareholders_count() > 0) {
    ...
    aptos_account::deposit_coins(
        recipient, coin::extract(&mut coins, amount_to_distribute)
    );
``` [5](#0-4) 

---

### Impact Explanation

Once a single shareholder (or the operator's beneficiary) triggers the abort condition, every call to `vesting::distribute()` reverts. Because `terminate_vesting_contract()` calls `distribute()` as its first step, the admin cannot terminate the contract either:

```move
public entry fun terminate_vesting_contract(admin: &signer, contract_address: address) ... {
    distribute(contract_address);   // ← always reverts
``` [6](#0-5) 

All APT staking rewards and vested principal belonging to every shareholder in the contract are permanently frozen. There is no recovery path in the contract.

---

### Likelihood Explanation

Both triggering actions are unprivileged entry functions callable by any account:

- `coin::migrate_to_fungible_store<AptosCoin>()` — public entry, removes `CoinStore<AptosCoin>` [7](#0-6) 
- `aptos_account::set_allow_direct_coin_transfers(false)` — public entry, sets `allow_arbitrary_coin_transfers = false` [8](#0-7) 

A malicious or griefing shareholder can execute both transactions before any distribution occurs, permanently blocking the entire pool. The attack requires no special role, no governance action, and no privileged capability.

---

### Recommendation

Wrap each per-recipient `deposit_coins` call in a guard that skips (and accumulates) failed distributions rather than aborting the entire loop. For example, check `can_receive_direct_coin_transfers` and `coin::is_account_registered` before attempting the deposit, and route undeliverable amounts to the `withdrawal_address` or a recoverable escrow. The same fix should be applied to `staking_contract::distribute_internal`.

---

### Proof of Concept

1. Admin creates a vesting contract with shareholders Alice and Bob.
2. Bob (malicious shareholder) submits two transactions:
   - `0x1::coin::migrate_to_fungible_store<0x1::aptos_coin::AptosCoin>()` — destroys `CoinStore<AptosCoin>` at Bob's address; `coin::is_account_registered<AptosCoin>(bob)` now returns `false`.
   - `0x1::aptos_account::set_allow_direct_coin_transfers(false)` — sets `allow_arbitrary_coin_transfers = false`; `can_receive_direct_coin_transfers(bob)` now returns `false`.
3. Staking rewards accumulate. Anyone calls `vesting::distribute(contract_address)`.
4. The loop reaches Bob's entry. `deposit_coins(bob, share)` evaluates:
   - `!coin::is_account_registered<AptosCoin>(bob)` → `true`
   - `can_receive_direct_coin_transfers(bob)` → `false`
   - `assert!(false, EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS)` → **abort**
5. The entire transaction reverts. Alice's share is never delivered. The coins remain locked in the vesting contract.
6. Admin attempts `terminate_vesting_contract` — it calls `distribute` first and also reverts.
7. All APT in the vesting pool is permanently frozen with no on-chain recovery path.

### Citations

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L733-740)
```text
        shareholders.for_each_ref(|shareholder| {
            let shareholder = *shareholder;
            let shares = pool_u64::shares(grant_pool, shareholder);
            let amount = pool_u64::shares_to_amount_with_total_coins(grant_pool, shares, total_distribution_amount);
            let share_of_coins = coin::extract(&mut coins, amount);
            let recipient_address = get_beneficiary(vesting_contract, shareholder);
            aptos_account::deposit_coins(recipient_address, share_of_coins);
        });
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L771-775)
```text
    public entry fun terminate_vesting_contract(admin: &signer, contract_address: address) acquires VestingContract {
        assert_active_vesting_contract(contract_address);

        // Distribute all withdrawable coins, which should have been from previous rewards withdrawal or vest.
        distribute(contract_address);
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.spec.move (L307-314)
```text
    spec distribute(contract_address: address) {
        // TODO: Can't handle abort in loop.
        pragma verify = false;
        include ActiveVestingContractAbortsIf;

        let vesting_contract = global<VestingContract>(contract_address);
        include WithdrawStakeAbortsIf { vesting_contract };
    }
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.move (L123-129)
```text
        if (!coin::is_account_registered<CoinType>(to)) {
            assert!(
                can_receive_direct_coin_transfers(to),
                error::permission_denied(EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS)
            );
            coin::register<CoinType>(&create_signer(to));
        };
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.move (L188-219)
```text
    public entry fun set_allow_direct_coin_transfers(
        account: &signer, allow: bool
    ) acquires DirectTransferConfig {
        let addr = signer::address_of(account);
        if (exists<DirectTransferConfig>(addr)) {
            let direct_transfer_config = borrow_global_mut<DirectTransferConfig>(addr);
            // Short-circuit to avoid emitting an event if direct transfer config is not changing.
            if (direct_transfer_config.allow_arbitrary_coin_transfers == allow) { return };

            direct_transfer_config.allow_arbitrary_coin_transfers = allow;

            emit(
                DirectCoinTransferConfigUpdated {
                    account: addr,
                    new_allow_direct_transfers: allow
                }
            );
        } else {
            let direct_transfer_config = DirectTransferConfig {
                allow_arbitrary_coin_transfers: allow,
                update_coin_transfer_events: new_event_handle<
                    DirectCoinTransferConfigUpdatedEvent>(account)
            };
            emit(
                DirectCoinTransferConfigUpdated {
                    account: addr,
                    new_allow_direct_transfers: allow
                }
            );
            move_to(account, direct_transfer_config);
        };
    }
```

**File:** aptos-move/framework/aptos-framework/sources/coin.move (L673-675)
```text
        if (exists<CoinStore<CoinType>>(account)) {
            let CoinStore<CoinType> { coin, frozen, deposit_events, withdraw_events } =
                move_from<CoinStore<CoinType>>(account);
```

**File:** aptos-move/framework/aptos-framework/sources/coin.move (L724-728)
```text
    public entry fun migrate_to_fungible_store<CoinType>(
        account: &signer
    ) acquires CoinStore, CoinConversionMap, CoinInfo {
        let account_addr = signer::address_of(account);
        maybe_convert_to_fungible_store<CoinType>(account_addr);
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L889-901)
```text
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
