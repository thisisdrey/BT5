### Title
Single Beneficiary Opt-Out Permanently Blocks All Vesting Distributions - (File: aptos-move/framework/aptos-framework/sources/vesting.move)

### Summary

`vesting::distribute` and `staking_contract::distribute_internal` iterate over all recipients and call `aptos_account::deposit_coins` for each one inside a single atomic transaction. `deposit_coins` contains a hard `assert!` that aborts if the recipient has not registered for the coin type **and** has called `set_allow_direct_coin_transfers(false)`. A single beneficiary who satisfies both conditions causes the entire distribution transaction to abort, permanently blocking every other beneficiary from receiving their vested or staked APT.

### Finding Description

`vesting::distribute` (a `public entry fun` callable by anyone) iterates over all shareholders and calls `aptos_account::deposit_coins(recipient_address, share_of_coins)` inside a `for_each_ref` closure: [1](#0-0) 

`aptos_account::deposit_coins` contains the following guard: [2](#0-1) 

If `coin::is_account_registered<CoinType>(to)` is `false` **and** `can_receive_direct_coin_transfers(to)` is `false`, the function aborts with `EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS`. `can_receive_direct_coin_transfers` returns `false` whenever the account has published a `DirectTransferConfig` with `allow_arbitrary_coin_transfers = false`: [3](#0-2) 

Any account holder can set this flag to `false` at any time via the public entry function `set_allow_direct_coin_transfers`: [4](#0-3) 

Because `register_apt` now calls `ensure_primary_fungible_store_exists` (the FA migration path) rather than `coin::register<AptosCoin>`, accounts created after the FA migration do **not** have a `CoinStore<AptosCoin>` registered, so `coin::is_account_registered<AptosCoin>` returns `false` for them: [5](#0-4) 

The identical pattern exists in `staking_contract::distribute_internal`, which loops over all distribution-pool shareholders and calls `aptos_account::deposit_coins` for each: [6](#0-5) 

Because Move transactions are atomic, an abort on any single iteration rolls back the entire transaction. There is no per-recipient error handling, no skip-and-continue logic, and no claimable-pull model.

### Impact Explanation

A single beneficiary whose account was created via the FA path (no `CoinStore<AptosCoin>`) and who calls `set_allow_direct_coin_transfers(false)` causes every subsequent call to `vesting::distribute` or `staking_contract::distribute` to abort. All other beneficiaries are permanently unable to withdraw their vested APT or staking rewards. The locked coins remain in the vesting/staking contract indefinitely. This constitutes permanent freezing of vesting balances and staking balances for all non-attacking participants, which is explicitly within the Aptos bounty scope.

### Likelihood Explanation

- `set_allow_direct_coin_transfers` is a standard, documented, unprivileged entry function — no special role or capability is required.
- Accounts created after the FA migration naturally lack `CoinStore<AptosCoin>`, satisfying the second precondition without any extra action.
- `vesting::distribute` and `staking_contract::distribute` are both `public entry fun` callable by any address, so the attacker does not need to be the one triggering the distribution.
- The attacker only needs to submit one transaction to permanently wedge the contract.

### Recommendation

Replace the push-to-all-recipients loop with a pull (claimable) model: record each recipient's owed amount in a table during distribution, and let each recipient claim their own share in a separate transaction. This is the same recommendation given in the original Solidity report and eliminates the single-point-of-failure property entirely.

Alternatively, if a push model must be retained, wrap each `deposit_coins` call in a try/catch-style pattern (once Move supports it) or pre-validate that every recipient can receive the coin before withdrawing from the stake pool, so that a failing recipient is skipped rather than aborting the whole batch.

### Proof of Concept

```
// Step 1 – Attacker (beneficiary B) opts out of direct coin transfers.
// B's account was created via the FA path, so coin::is_account_registered<AptosCoin>(B) == false.
aptos_account::set_allow_direct_coin_transfers(&b_signer, false);

// Step 2 – Anyone calls distribute on the vesting contract.
// The loop reaches B:
//   deposit_coins(B, share_of_coins)
//     -> coin::is_account_registered<AptosCoin>(B) == false
//     -> can_receive_direct_coin_transfers(B)       == false
//     -> assert! FAILS  =>  entire tx aborts
vesting::distribute(contract_address);
// ^ aborts with EACCOUNT_DOES_NOT_ACCEPT_DIRECT_COIN_TRANSFERS (error code 3)

// Step 3 – All other beneficiaries A, C, D … receive nothing.
// Every future call to distribute() aborts at the same point.
// The vested APT is permanently locked in the contract.
```

The same sequence applies to `staking_contract::distribute(staker, operator)` when the staker or operator beneficiary satisfies the two preconditions. [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L719-756)
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

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.move (L188-197)
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

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.move (L233-235)
```text
    public(friend) fun register_apt(account_signer: &signer) {
        ensure_primary_fungible_store_exists(signer::address_of(account_signer));
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
