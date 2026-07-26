### Title
Staker Can Retroactively Zero Out Operator's Commission on Pending-Inactive Rewards via `update_commision` - (File: `aptos-move/framework/aptos-framework/sources/staking_contract.move`)

### Summary
`staking_contract::update_commision` allows the staker to change the commission percentage at any time. It calls `request_commission_internal` before updating the rate, but that function only captures rewards on `active + pending_active` stake — not on `pending_inactive` stake. Rewards that have already accrued on `pending_inactive` (from a prior `unlock_stake` call) are charged commission only when `update_distribution_pool` is called during a future `distribute_internal`, using whatever commission percentage is stored at that later time. A staker can therefore call `update_commision(0%)` while a lockup is still running, and when the lockup expires and `distribute` is called, the operator receives zero commission on all rewards that accumulated on the pending-inactive stake.

### Finding Description

`update_commision` is the staker-controlled entry point for changing the operator's commission rate: [1](#0-0) 

It calls `distribute_internal` (which returns early if nothing is yet withdrawable, i.e., the lockup has not expired) and then `request_commission_internal`, which only accounts for `active + pending_active` rewards: [2](#0-1) 

Rewards on `pending_inactive` are intentionally excluded from this calculation (per the inline comment). They are only settled when `update_distribution_pool` is called inside `distribute_internal`: [3](#0-2) 

`update_distribution_pool` charges commission on the growth of the distribution pool since the last snapshot, using `commission_percentage` as it exists **at call time** — not at the time the rewards accrued: [4](#0-3) 

Because `update_commision` does not settle pending-inactive rewards before changing the rate, the new rate is silently applied to all previously-accrued but unsettled rewards when `distribute` is eventually called.

### Impact Explanation

The operator is entitled to a commission on all rewards earned while their validator was active. After a staker calls `unlock_stake`, the unlocked amount sits in `pending_inactive` and continues to earn staking rewards until the lockup expires. Those rewards are tracked only inside `update_distribution_pool` and are charged commission at whatever rate is stored in `staking_contract.commission_percentage` at distribution time. By calling `update_commision(0)` before the lockup expires, the staker causes the operator to receive zero commission on those rewards — a direct theft of APT that the operator was contractually owed. [5](#0-4) 

### Likelihood Explanation

Any staker who has called `unlock_stake` and whose lockup has not yet expired can execute this at any time with a single transaction. No special permissions, no governance vote, no coordination is required. The staker has a direct financial incentive (keeping the operator's commission) and the operator has no on-chain mechanism to prevent or pre-empt it — `request_commission` also only captures active rewards and cannot lock in the rate on pending-inactive rewards. [6](#0-5) 

### Recommendation

Before updating `commission_percentage`, `update_commision` must also settle the commission owed on pending-inactive rewards. One approach: extend `request_commission_internal` (or add a parallel helper) to read `pending_inactive` from the stake pool, compute the rewards that have accrued on it since the last distribution-pool snapshot (`distribution_pool.total_coins()`), charge commission on that delta at the **old** rate, and record the operator's share in the distribution pool before the rate is changed. Alternatively, disallow commission-rate changes while any `pending_inactive` balance exists.

### Proof of Concept

```
// Setup: staker creates contract with 10% commission
create_staking_contract(staker, operator, voter, 1000 APT, 10, seed);

// Operator joins validator set; rewards start accruing
stake::join_validator_set(operator, pool_address);
stake::end_epoch(); // pool grows: 1000 → 1010 APT

// Staker unlocks 500 APT; request_commission_internal fires at 10%
// on active rewards, then 500 APT moves to pending_inactive
unlock_stake(staker, operator, 500);
// pending_inactive = 500 APT; distribution_pool has staker entry for 500

// More rewards accrue on pending_inactive (lockup still running)
stake::end_epoch(); // pending_inactive grows: 500 → 505 APT

// Staker atomically drops commission to 0% BEFORE lockup expires
// distribute_internal returns early (nothing withdrawable yet)
// request_commission_internal fires on active rewards only (not pending_inactive)
update_commision(staker, operator, 0);
// staking_contract.commission_percentage is now 0

// Lockup expires
timestamp::fast_forward_seconds(lockup_duration);
stake::end_epoch();

// Anyone calls distribute; update_distribution_pool fires with commission_percentage = 0
distribute(staker_address, operator_address);
// Operator receives 0 commission on the 5 APT reward that accrued on pending_inactive
// Staker receives the full 5 APT instead of 4.5 APT
// Operator is robbed of 0.5 APT (10% of 5 APT) per this cycle
``` [1](#0-0) [7](#0-6) [3](#0-2)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L566-601)
```text
    public entry fun update_commision(
        staker: &signer, operator: address, new_commission_percentage: u64
    ) acquires Store, BeneficiaryForOperator {
        assert!(
            new_commission_percentage >= 0 && new_commission_percentage <= 100,
            error::invalid_argument(EINVALID_COMMISSION_PERCENTAGE)
        );

        let staker_address = signer::address_of(staker);
        assert!(
            exists<Store>(staker_address),
            error::not_found(ENO_STAKING_CONTRACT_FOUND_FOR_STAKER)
        );

        let store = borrow_global_mut<Store>(staker_address);
        let staking_contract = store.staking_contracts.borrow_mut(&operator);
        distribute_internal(
            staker_address,
            operator,
            staking_contract,
        );
        request_commission_internal(
            operator,
            staking_contract,
        );
        let old_commission_percentage = staking_contract.commission_percentage;
        staking_contract.commission_percentage = new_commission_percentage;
        emit(
            UpdateCommission {
                staker: staker_address,
                operator,
                old_commission_percentage,
                new_commission_percentage
            }
        );
    }
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L607-634)
```text
    public entry fun request_commission(
        account: &signer, staker: address, operator: address
    ) acquires Store, BeneficiaryForOperator {
        let account_addr = signer::address_of(account);
        assert!(
            account_addr == staker
                || account_addr == operator
                || account_addr == beneficiary_for_operator(operator),
            error::unauthenticated(ENOT_STAKER_OR_OPERATOR_OR_BENEFICIARY)
        );
        assert_staking_contract_exists(staker, operator);

        let store = borrow_global_mut<Store>(staker);
        let staking_contract = store.staking_contracts.borrow_mut(&operator);
        // Short-circuit if zero commission.
        if (staking_contract.commission_percentage == 0) { return };

        // Force distribution of any already inactive stake.
        distribute_internal(
            staker,
            operator,
            staking_contract,
        );

        request_commission_internal(
            operator,
            staking_contract,
        );
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L637-674)
```text
    fun request_commission_internal(
        operator: address,
        staking_contract: &mut StakingContract,
    ): u64 {
        // Unlock just the commission portion from the stake pool.
        let (total_active_stake, accumulated_rewards, commission_amount) =
            get_staking_contract_amounts_internal(staking_contract);
        staking_contract.principal = total_active_stake - commission_amount;

        // Short-circuit if there's no commission to pay.
        if (commission_amount == 0) {
            return 0
        };

        // Add a distribution for the operator.
        add_distribution(
            operator,
            staking_contract,
            operator,
            commission_amount
        );

        // Request to unlock the commission from the stake pool.
        // This won't become fully unlocked until the stake pool's lockup expires.
        stake::unlock_with_cap(commission_amount, &staking_contract.owner_cap);

        let pool_address = staking_contract.pool_address;
        emit(
            RequestCommission {
                operator,
                pool_address,
                accumulated_rewards,
                commission_amount
            }
        );

        commission_amount
    }
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L880-886)
```text
        let distribution_pool = &mut staking_contract.distribution_pool;
        update_distribution_pool(
            distribution_pool,
            distribution_amount,
            operator,
            staking_contract.commission_percentage
        );
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L959-975)
```text
    /// Calculate accumulated rewards and commissions since last update.
    fun get_staking_contract_amounts_internal(
        staking_contract: &StakingContract
    ): (u64, u64, u64) {
        // Pending_inactive is not included in the calculation because pending_inactive can only come from:
        // 1. Outgoing commissions. This means commission has already been extracted.
        // 2. Stake withdrawals from stakers. This also means commission has already been extracted as
        // request_commission_internal is called in unlock_stake
        let (active, _, pending_active, _) =
            stake::get_stake(staking_contract.pool_address);
        let total_active_stake = active + pending_active;
        let accumulated_rewards = total_active_stake - staking_contract.principal;
        let commission_amount =
            accumulated_rewards * staking_contract.commission_percentage / 100;

        (total_active_stake, accumulated_rewards, commission_amount)
    }
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L1001-1039)
```text
    fun update_distribution_pool(
        distribution_pool: &mut Pool,
        updated_total_coins: u64,
        operator: address,
        commission_percentage: u64
    ) {
        // Short-circuit and do nothing if the pool's total value has not changed.
        if (distribution_pool.total_coins() == updated_total_coins) { return };

        // Charge all stakeholders (except for the operator themselves) commission on any rewards earnt relatively to the
        // previous value of the distribution pool.
        let shareholders = &distribution_pool.shareholders();
        shareholders.for_each_ref(
            |shareholder| {
                let shareholder: address = *shareholder;
                if (shareholder != operator) {
                    let shares = pool_u64::shares(distribution_pool, shareholder);
                    let previous_worth = pool_u64::balance(distribution_pool, shareholder);
                    let current_worth =
                        pool_u64::shares_to_amount_with_total_coins(
                            distribution_pool, shares, updated_total_coins
                        );
                    let unpaid_commission =
                        (current_worth - previous_worth) * commission_percentage / 100;
                    // Transfer shares from current shareholder to the operator as payment.
                    // The value of the shares should use the updated pool's total value.
                    let shares_to_transfer =
                        pool_u64::amount_to_shares_with_total_coins(
                            distribution_pool, unpaid_commission, updated_total_coins
                        );
                    pool_u64::transfer_shares(
                        distribution_pool, shareholder, operator, shares_to_transfer
                    );
                };
            }
        );

        distribution_pool.update_total_coins(updated_total_coins);
    }
```
