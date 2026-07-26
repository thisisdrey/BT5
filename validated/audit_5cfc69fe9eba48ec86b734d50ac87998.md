### Title
Single Shareholder Blocking All Vesting Distributions via Disabled Direct Coin Transfers — (`aptos-move/framework/aptos-framework/sources/vesting.move`)

---

### Summary

`vesting.distribute()` iterates through every shareholder in a vesting contract and calls `aptos_account::deposit_coins` for each one inside a closure with no error handling. If the deposit to any single shareholder aborts, the entire transaction reverts and **no shareholder receives their vested APT**. A shareholder can unilaterally trigger this condition by calling `set_allow_direct_coin_transfers(false)` on an account that has no registered `CoinStore<AptosCoin>`, causing `deposit_coins` to abort with `EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS`. The same structural defect exists in `staking_contract.distribute_internal`.

---

### Finding Description

`vesting.distribute()` withdraws all unlocked stake from the staking pool and then distributes it to every shareholder in a single atomic transaction:

```move
// aptos-move/framework/aptos-framework/sources/vesting.move  L730-L740
let grant_pool = &vesting_contract.grant_pool;
let shareholders = &grant_pool.shareholders();
shareholders.for_each_ref(|shareholder| {
    let shareholder = *shareholder;
    let shares = pool_u64::shares(grant_pool, shareholder);
    let amount = pool_u64::shares_to_amount_with_total_coins(grant_pool, shares, total_distribution_amount);
    let share_of_coins = coin::extract(&mut coins, amount);
    let recipient_address = get_beneficiary(vesting_contract, shareholder);
    aptos_account::deposit_coins(recipient_address, share_of_coins);   // ← no try/catch
});
``` [1](#0-0) 

`aptos_account::deposit_coins` contains the following guard:

```move
// aptos-move/framework/aptos-framework/sources/aptos_account.move  L123-L128
if (!coin::is_account_registered<CoinType>(to)) {
    assert!(
        can_receive_direct_coin_transfers(to),
        error::permission_denied(EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS)
    );
    coin::register<CoinType>(&create_signer(to));
};
``` [2](#0-1) 

`can_receive_direct_coin_transfers` returns `false` when the account has explicitly called `set_allow_direct_coin_transfers(false)`:

```move
// aptos-move/framework/aptos-framework/sources/aptos_account.move  L229-L231
public fun can_receive_direct_coin_transfers(account: address): bool acquires DirectTransferConfig {
    !exists<DirectTransferConfig>(account)
        || borrow_global<DirectTransferConfig>(account).allow_arbitrary_coin_transfers
}
``` [3](#0-2) 

`set_allow_direct_coin_transfers` is a public entry function callable by any account owner: [4](#0-3) 

The same pattern exists in `staking_contract.distribute_internal`, which loops over all distribution-pool recipients and calls `aptos_account::deposit_coins` for each without error handling: [5](#0-4) 

---

### Impact Explanation

When `distribute()` reverts, the coins extracted from the staking pool are not transferred to anyone — the entire Move transaction is rolled back. All shareholders, including those whose accounts are perfectly healthy, are denied their vested APT for the duration of the blockage. The admin must manually call `set_beneficiary` to reroute the problematic shareholder's share before `distribute()` can succeed again. Until that manual intervention occurs, the vesting contract is effectively frozen for all participants.

The `distribute_many` wrapper compounds the issue: a single bad contract in the batch causes every other contract in the same call to also fail. [6](#0-5) 

---

### Likelihood Explanation

The trigger is an unprivileged, two-step action available to any shareholder:

1. Call `aptos_account::set_allow_direct_coin_transfers(false)` — a standard public entry function.
2. Ensure the account has no `CoinStore<AptosCoin>` registered (e.g., an account created through a path that does not auto-register APT, or one that has migrated its coin store to the FA layer).

When `deposit_coins` is subsequently called for that address, the `!coin::is_account_registered<AptosCoin>(to)` branch is taken, `can_receive_direct_coin_transfers` returns `false`, and the abort fires. Because `distribute()` is a public entry function with no access control, any caller can trigger the revert once the precondition is in place. [7](#0-6) 

---

### Recommendation

Wrap each per-shareholder deposit in a pattern that skips rather than aborts on failure, or split distribution into two phases:

1. **Skip-and-accumulate**: if `deposit_coins` would abort (check `can_receive_direct_coin_transfers` and `coin::is_coin_store_frozen` before calling), accumulate the undistributed amount and send it to the `withdrawal_address` as a fallback, recording the owed amount on-chain for later individual claim.
2. **Pull-over-push**: instead of pushing coins to every shareholder atomically, record each shareholder's claimable balance and let them pull individually via a separate `claim(contract_address)` entry function. This eliminates the single-point-of-failure entirely.

The same fix should be applied to `staking_contract.distribute_internal`.

---

### Proof of Concept

1. Admin creates a vesting contract with two shareholders: `alice` (honest) and `bob` (attacker).
2. `bob` calls `aptos_account::set_allow_direct_coin_transfers(false)` and ensures his account has no `CoinStore<AptosCoin>` (e.g., account was created without APT registration).
3. After the lockup period, anyone calls `vesting::distribute(contract_address)`.
4. The loop reaches `bob`'s entry; `deposit_coins(bob, ...)` evaluates `!coin::is_account_registered<AptosCoin>(bob)` → `true`, then `can_receive_direct_coin_transfers(bob)` → `false`, and aborts with `EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS`.
5. The entire transaction reverts. `alice` receives nothing.
6. The vesting contract remains undistributed until the admin calls `set_beneficiary(admin, contract_address, bob, some_other_address)` to reroute `bob`'s share — a manual, privileged intervention identical to the "manager must update the priority queue" mitigation in the original report. [8](#0-7) [9](#0-8)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L706-716)
```text
    /// Call `vest` for many vesting contracts.
    public entry fun vest_many(contract_addresses: vector<address>) acquires VestingContract {
        let len = contract_addresses.length();

        assert!(len != 0, error::invalid_argument(EVEC_EMPTY_FOR_MANY_FUNCTION));

        contract_addresses.for_each_ref(|contract_address| {
            let contract_address = *contract_address;
            vest(contract_address);
        });
    }
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L718-756)
```text
    /// Distribute any withdrawable stake from the stake pool.
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

        // Send any remaining "dust" (leftover due to rounding error) to the withdrawal address.
        if (coin::value(&coins) > 0) {
            aptos_account::deposit_coins(vesting_contract.withdrawal_address, coins);
        } else {
            coin::destroy_zero(coins);
        };

        emit(
            Distribute {
                admin: vesting_contract.admin,
                vesting_contract_address: contract_address,
                amount: total_distribution_amount,
            },
        );
    }
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L759-768)
```text
    public entry fun distribute_many(contract_addresses: vector<address>) acquires VestingContract {
        let len = contract_addresses.length();

        assert!(len != 0, error::invalid_argument(EVEC_EMPTY_FOR_MANY_FUNCTION));

        contract_addresses.for_each_ref(|contract_address| {
            let contract_address = *contract_address;
            distribute(contract_address);
        });
    }
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.move (L111-131)
```text
    public fun deposit_coins<CoinType>(
        to: address, coins: Coin<CoinType>
    ) acquires DirectTransferConfig {
        if (!account::exists_at(to)) {
            create_account(to);
            spec {
                // TODO(fa_migration)
                // assert coin::spec_is_account_registered<AptosCoin>(to);
                // assume aptos_std::type_info::type_of<CoinType>() == aptos_std::type_info::type_of<AptosCoin>() ==>
                //     coin::spec_is_account_registered<CoinType>(to);
            };
        };
        if (!coin::is_account_registered<CoinType>(to)) {
            assert!(
                can_receive_direct_coin_transfers(to),
                error::permission_denied(EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS)
            );
            coin::register<CoinType>(&create_signer(to));
        };
        coin::deposit<CoinType>(to, coins)
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
