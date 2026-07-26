### Title
Frozen/Opted-Out Shareholder Permanently Bricks `vesting::distribute()` for All Participants — (`File: aptos-move/framework/aptos-framework/sources/vesting.move`)

### Summary

`vesting::distribute()` atomically iterates over every shareholder and calls `aptos_account::deposit_coins(recipient_address, share_of_coins)` inside a single transaction. If any single deposit aborts — because the recipient's primary fungible store is frozen or because the recipient has opted out of direct coin transfers while lacking a legacy `CoinStore<AptosCoin>` — the entire transaction reverts. The vesting contract is then permanently stuck: no shareholder can ever receive their vested APT, and `terminate_vesting_contract()` (which calls `distribute()` first) is also bricked.

### Finding Description

**Root cause — atomic multi-recipient distribution with no per-recipient error isolation:** [1](#0-0) 

```move
// Distribute coins to all shareholders in the vesting contract.
let grant_pool = &vesting_contract.grant_pool;
let shareholders = &grant_pool.shareholders();
shareholders.for_each_ref(|shareholder| {
    let shareholder = *shareholder;
    let shares = pool_u64::shares(grant_pool, shareholder);
    let amount = pool_u64::shares_to_amount_with_total_coins(grant_pool, shares, total_distribution_amount);
    let share_of_coins = coin::extract(&mut coins, amount);
    let recipient_address = get_beneficiary(vesting_contract, shareholder);
    aptos_account::deposit_coins(recipient_address, share_of_coins);  // ← abort here reverts everything
});
``` [2](#0-1) 

**Abort path 1 — frozen primary fungible store:**

`coin::deposit<AptosCoin>` now routes through `primary_fungible_store::deposit` → `fungible_asset::deposit_sanity_check`, which asserts `!fa_store.frozen`: [3](#0-2) [4](#0-3) 

**Abort path 2 — opted-out recipient without a legacy `CoinStore<AptosCoin>` (non-privileged):**

`aptos_account::deposit_coins` checks `can_receive_direct_coin_transfers` before registering the coin store: [5](#0-4) 

A shareholder can call `set_allow_direct_coin_transfers(self, false)` at any time. Under the FA migration, new accounts may hold APT only in a primary fungible store (no legacy `CoinStore<AptosCoin>`), so `coin::is_account_registered<AptosCoin>` returns false, the `can_receive_direct_coin_transfers` guard fires, and the deposit aborts with `EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS`.

**Cascading effect — `terminate_vesting_contract` is also bricked:** [6](#0-5) 

`terminate_vesting_contract` calls `distribute(contract_address)` unconditionally before doing anything else. If `distribute` is permanently stuck, the admin can never terminate the contract and recover funds via `admin_withdraw`.

**Same pattern in `staking_contract::distribute_internal()`:** [7](#0-6) 

The same atomic loop over all distribution-pool recipients with `aptos_account::deposit_coins` exists here, with identical abort exposure.

### Impact Explanation

All shareholders' vesting balances are permanently inaccessible. The coins remain locked in the staking pool (the transaction reverts atomically), but no future call to `distribute()` can succeed as long as the blocking condition persists. Because `terminate_vesting_contract` is also blocked, the admin cannot recover funds via `admin_withdraw`. This constitutes permanent freezing of vesting balances for all participants — an impact explicitly listed in the Aptos bounty scope.

### Likelihood Explanation

- **Abort path 1 (frozen store):** Requires the APT freeze capability held by `@aptos_framework` — privileged, out of scope.
- **Abort path 2 (opted-out recipient):** A shareholder calls `set_allow_direct_coin_transfers(self, false)` — a public, unprivileged entry function. Under the FA migration, accounts that never registered a legacy `CoinStore<AptosCoin>` satisfy the `!is_account_registered` precondition. A malicious or griefing shareholder can trigger this at any time after joining the vesting contract, permanently blocking all other shareholders.

**Uncertainty note:** Whether `coin::is_account_registered<AptosCoin>` returns `false` for FA-only accounts on current mainnet depends on the exact migration state. If the function has been updated to also check the primary fungible store, abort path 2 does not apply and only the privileged path remains, making this out of scope.

### Recommendation

Replace the atomic loop with per-recipient error isolation. For each shareholder, either:
1. Use a try/catch pattern (not available in Move) — instead, track failed distributions in a separate map and allow individual shareholders to claim their share independently.
2. Separate the distribution into individual claimable entries: record each shareholder's pending amount in storage, and let each shareholder pull their own coins in a separate transaction (pull-over-push pattern, the same fix applied to the Axis Finance bug).

### Proof of Concept

1. Deploy a vesting contract with shareholders A and B.
2. Shareholder A calls `aptos_account::set_allow_direct_coin_transfers(A, false)` and ensures their account has no legacy `CoinStore<AptosCoin>` (FA-only account).
3. Rewards accumulate; `unlock_rewards` is called; stake lockup expires.
4. Anyone calls `vesting::distribute(contract_address)`.
5. The loop reaches shareholder A; `aptos_account::deposit_coins(A, ...)` aborts with `EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS`.
6. The entire transaction reverts. Shareholder B receives nothing.
7. Every subsequent call to `distribute()` aborts identically.
8. `terminate_vesting_contract` also aborts (calls `distribute` first).
9. All vesting balances are permanently frozen. [2](#0-1) [5](#0-4) [8](#0-7)

### Citations

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

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L771-793)
```text
    public entry fun terminate_vesting_contract(admin: &signer, contract_address: address) acquires VestingContract {
        assert_active_vesting_contract(contract_address);

        // Distribute all withdrawable coins, which should have been from previous rewards withdrawal or vest.
        distribute(contract_address);

        let vesting_contract = borrow_global_mut<VestingContract>(contract_address);
        verify_admin(admin, vesting_contract);
        let (active_stake, _, pending_active_stake, _) = stake::get_stake(vesting_contract.staking.pool_address);
        assert!(pending_active_stake == 0, error::invalid_state(EPENDING_STAKE_FOUND));

        // Unlock all remaining active stake.
        vesting_contract.state = VESTING_POOL_TERMINATED;
        vesting_contract.remaining_grant = 0;
        unlock_stake(vesting_contract, active_stake);

        emit(
            Terminate {
                admin: vesting_contract.admin,
                vesting_contract_address: contract_address,
            },
        );
    }
```

**File:** aptos-move/framework/aptos-framework/sources/fungible_asset.move (L990-999)
```text
    public fun deposit_sanity_check<T: key>(
        store: Object<T>, abort_on_dispatch: bool
    ) acquires FungibleStore, DispatchFunctionStore {
        let fa_store = borrow_store_resource(&store);
        assert!(
            !abort_on_dispatch || !has_deposit_dispatch_function(fa_store.metadata),
            error::invalid_argument(EINVALID_DISPATCHABLE_OPERATIONS)
        );
        assert!(!fa_store.frozen, error::permission_denied(ESTORE_IS_FROZEN));
    }
```

**File:** aptos-move/framework/aptos-framework/sources/coin.move (L906-910)
```text
    public fun deposit<CoinType>(
        account_addr: address, coin: Coin<CoinType>
    ) acquires CoinConversionMap, CoinInfo {
        primary_fungible_store::deposit(account_addr, coin_to_fungible_asset(coin));
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

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L856-920)
```text
    fun distribute_internal(
        staker: address,
        operator: address,
        staking_contract: &mut StakingContract,
    ) acquires BeneficiaryForOperator {
        let pool_address = staking_contract.pool_address;
        // Create the Staker resource if it doesn't exist to backfill the Staker resource for each pool.
        if (!exists<Staker>(pool_address)) {
            let pool_signer =
                &account::create_signer_with_capability(&staking_contract.signer_cap);
            move_to(pool_signer, Staker { staker });
        };
        let (_, inactive, _, pending_inactive) = stake::get_stake(pool_address);
        let total_potential_withdrawable = inactive + pending_inactive;
        let coins =
            stake::withdraw_with_cap(
                &staking_contract.owner_cap, total_potential_withdrawable
            );
        let distribution_amount = coin::value(&coins);
        if (distribution_amount == 0) {
            coin::destroy_zero(coins);
            return
        };

        let distribution_pool = &mut staking_contract.distribution_pool;
        update_distribution_pool(
            distribution_pool,
            distribution_amount,
            operator,
            staking_contract.commission_percentage
        );

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

        // In case there's any dust left, send them all to the staker.
        if (coin::value(&coins) > 0) {
            aptos_account::deposit_coins(staker, coins);
            distribution_pool.update_total_coins(0);
        } else {
            coin::destroy_zero(coins);
        }
    }
```
