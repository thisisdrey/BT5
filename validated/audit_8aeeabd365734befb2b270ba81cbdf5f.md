### Title
Griefing Shareholder/Beneficiary Can Permanently DOS `vesting::distribute()` for All Shareholders — (`aptos-move/framework/aptos-framework/sources/vesting.move`)

---

### Summary

`vesting::distribute()` pushes APT coins to every shareholder (or their beneficiary) in a single atomic transaction. The inner call `aptos_account::deposit_coins` aborts if the recipient has not registered a legacy `CoinStore<AptosCoin>` **and** has set `allow_arbitrary_coin_transfers = false`. Because the entire loop is one transaction, a single blocking recipient causes the whole distribution to revert, permanently freezing vested APT for every other shareholder.

---

### Finding Description

`vesting::distribute()` iterates over all shareholders and calls `aptos_account::deposit_coins` for each one: [1](#0-0) 

`aptos_account::deposit_coins` contains the following guard: [2](#0-1) 

The check at line 123 is `coin::is_account_registered<CoinType>(to)`, which tests for the existence of a legacy `CoinStore<AptosCoin>` resource. In the current FA-migration era, accounts created via `aptos_account::create_account` receive only a primary fungible store, **not** a legacy `CoinStore<AptosCoin>`. Therefore `is_account_registered<AptosCoin>` returns `false` for such accounts. [3](#0-2) 

Any account holder can call `set_allow_direct_coin_transfers(false)` to set `allow_arbitrary_coin_transfers = false`: [4](#0-3) 

When both conditions hold for a recipient — no legacy `CoinStore<AptosCoin>` and `allow_arbitrary_coin_transfers = false` — `deposit_coins` aborts with `EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS`. Because `distribute()` processes all shareholders in one atomic transaction, this abort rolls back the entire call, blocking every other shareholder from receiving their vested coins.

The same root cause exists in `staking_contract::distribute_internal`, which also calls `aptos_account::deposit_coins` in a loop over all distribution-pool recipients: [5](#0-4) 

---

### Impact Explanation

All shareholders in the affected vesting contract are permanently unable to receive their vested APT through `distribute()`. The coins accumulate in the contract's stake pool but cannot be withdrawn. There is no mechanism in the vesting contract to remove a shareholder from the grant pool, so the DOS is durable. `terminate_vesting_contract` also calls `distribute()` internally: [6](#0-5) 

This means even contract termination and `admin_withdraw` are blocked, permanently freezing all remaining grant funds. This constitutes permanent freezing of user-controlled APT staking/vesting balances.

---

### Likelihood Explanation

The trigger is fully unprivileged and requires only two standard on-chain calls:

1. Create an account (receives primary fungible store, no legacy `CoinStore<AptosCoin>`).
2. Call `aptos_account::set_allow_direct_coin_transfers(false)`.

Any shareholder in a vesting contract, or any address that the admin designates as a beneficiary via `set_beneficiary`, can execute this. The `distribute()` entry function is callable by anyone: [7](#0-6) 

---

### Recommendation

Replace the push-based distribution loop with a pull-based (claim) pattern: record each shareholder's claimable balance in a table during `distribute()`, and let each shareholder call a separate `claim()` function to withdraw their own share. This isolates per-recipient failures so that one blocked recipient cannot affect others.

As a shorter-term mitigation, replace `aptos_account::deposit_coins` with `aptos_account::deposit_fungible_assets` (which uses the primary fungible store path and does not check `allow_arbitrary_coin_transfers`) for APT distributions, since APT is now a fungible asset: [8](#0-7) 

---

### Proof of Concept

1. Admin creates a vesting contract with two shareholders: `alice` (honest) and `bob` (attacker).
2. `bob` creates his account via `aptos_account::create_account` — this gives him only a primary fungible store, no legacy `CoinStore<AptosCoin>`.
3. `bob` calls `aptos_account::set_allow_direct_coin_transfers(false)`.
4. After the vesting period, anyone calls `vesting::distribute(contract_address)`.
5. The loop reaches `bob`; `deposit_coins<AptosCoin>(bob, ...)` checks `coin::is_account_registered<AptosCoin>(bob)` → `false`, then checks `can_receive_direct_coin_transfers(bob)` → `false`, and **aborts**.
6. The entire transaction reverts. `alice` receives nothing. The vested APT remains locked in the contract indefinitely.
7. `terminate_vesting_contract` also fails because it calls `distribute()` first, so `admin_withdraw` can never be reached. [9](#0-8) [10](#0-9)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L719-720)
```text
    public entry fun distribute(contract_address: address) acquires VestingContract {
        assert_active_vesting_contract(contract_address);
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L730-740)
```text
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

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L771-775)
```text
    public entry fun terminate_vesting_contract(admin: &signer, contract_address: address) acquires VestingContract {
        assert_active_vesting_contract(contract_address);

        // Distribute all withdrawable coins, which should have been from previous rewards withdrawal or vest.
        distribute(contract_address);
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

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.move (L168-173)
```text
    public fun deposit_fungible_assets(to: address, fa: FungibleAsset) {
        if (!account::exists_at(to)) {
            create_account(to);
        };
        primary_fungible_store::deposit(to, fa)
    }
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

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.move (L226-231)
```text
    public fun can_receive_direct_coin_transfers(
        account: address
    ): bool acquires DirectTransferConfig {
        !exists<DirectTransferConfig>(account)
            || borrow_global<DirectTransferConfig>(account).allow_arbitrary_coin_transfers
    }
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L888-911)
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

            emit(
                Distribute {
                    operator,
                    pool_address,
                    recipient,
                    amount: amount_to_distribute
                }
            );
        };
```
