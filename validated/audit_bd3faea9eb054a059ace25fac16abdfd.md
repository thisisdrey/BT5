### Title
Pending Commission Redirected to New Beneficiary Without Prior Distribution — (`staking_contract.move`)

### Summary

`staking_contract::set_beneficiary_for_operator` changes the commission-payment destination for an operator without first distributing already-unlocked (inactive) commission funds. Because `distribute_internal` resolves the beneficiary at distribution time by calling `beneficiary_for_operator(operator)`, any commission that was already unlocked but not yet distributed will be sent to the **new** beneficiary instead of the **old** one. The operator can exploit this deliberately to steal pending commission from the old beneficiary.

### Finding Description

The root cause is structurally identical to the external report: a critical settlement step (distribute/harvest) must execute before a state change (beneficiary/adapter swap), but the code does not enforce it — it only documents it as a caller responsibility.

In `staking_contract.move`, `set_beneficiary_for_operator` (lines 810–838) simply overwrites the `BeneficiaryForOperator` resource and emits an event. It does **not** call `distribute_internal` or `request_commission_internal` first: [1](#0-0) 

The comment on lines 807–809 explicitly acknowledges the danger — "To ensures payment to the current beneficiary, one should first call `distribute` before switching the beneficiary" — but the function itself does not enforce this invariant.

When `distribute_internal` eventually runs, it resolves the recipient at that moment: [2](#0-1) 

So any commission already sitting in `inactive` (withdrawable) state is paid to whoever `beneficiary_for_operator(operator)` returns **at distribution time**, not at the time the commission was earned or unlocked.

The same pattern exists in `delegation_pool::set_beneficiary_for_operator` (lines 1272–1291), which also omits the mandatory `synchronize_delegation_pool` call before changing the beneficiary: [3](#0-2) 

By contrast, `delegation_pool::set_operator` correctly calls `synchronize_delegation_pool` before the operator change: [4](#0-3) 

And `staking_contract::switch_operator` correctly calls both `distribute_internal` and `request_commission_internal` before the operator change: [5](#0-4) 

### Impact Explanation

An operator who previously designated a third-party beneficiary (e.g., a business partner entitled to commission) can redirect already-unlocked commission funds away from that beneficiary by calling `set_beneficiary_for_operator` before anyone calls `distribute`. The old beneficiary permanently loses their earned APT commission. This is a direct theft of APT from the old beneficiary's entitled on-chain balance.

### Likelihood Explanation

The trigger is a single unprivileged entry-function call (`set_beneficiary_for_operator`) by the operator. No special timing or external conditions are required beyond having unlocked-but-undistributed commission in the pool. The operator has full unilateral control over when to make this call.

### Recommendation

`set_beneficiary_for_operator` in both `staking_contract.move` and `delegation_pool.move` should atomically distribute all pending commission to the old beneficiary before updating the beneficiary address. For `staking_contract`, this means iterating over all staking contracts for the operator and calling `distribute_internal` on each. For `delegation_pool`, this means calling `synchronize_delegation_pool` for each pool the operator manages before the beneficiary is changed.

### Proof of Concept

1. Operator `O` sets beneficiary to address `B` (old beneficiary, e.g., a business partner).
2. The stake pool earns rewards; `request_commission` is called, unlocking commission into `pending_inactive`.
3. The lockup period expires; commission moves to `inactive` (fully withdrawable).
4. Before anyone calls `distribute`, operator `O` calls `set_beneficiary_for_operator(O_address)` — redirecting future distributions to themselves.
5. Anyone calls `distribute(staker, O)` → `distribute_internal` executes → at line 897, `beneficiary_for_operator(O)` now returns `O_address` → the entire commission is deposited to `O` instead of `B`.
6. `B` receives zero APT despite having earned the commission. The operator has stolen `B`'s funds with a single transaction. [6](#0-5) [7](#0-6)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L783-804)
```text
        let (_, staking_contract) = staking_contracts.remove(&old_operator);
        // Force distribution of any already inactive stake.
        distribute_internal(
            staker_address,
            old_operator,
            &mut staking_contract,
        );

        // For simplicity, we request commission to be paid out first. This avoids having to ensure to staker doesn't
        // withdraw into the commission portion.
        request_commission_internal(
            old_operator,
            &mut staking_contract,
        );

        // Update the staking contract's commission rate and stake pool's operator.
        stake::set_operator_with_cap(&staking_contract.owner_cap, new_operator);
        staking_contract.commission_percentage = new_commission_percentage;

        let pool_address = staking_contract.pool_address;
        staking_contracts.add(new_operator, staking_contract);
        emit(SwitchOperator { pool_address, old_operator, new_operator });
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L807-838)
```text
    /// Allows an operator to change its beneficiary. Any existing unpaid commission rewards will be paid to the new
    /// beneficiary. To ensures payment to the current beneficiary, one should first call `distribute` before switching
    /// the beneficiary. An operator can set one beneficiary for staking contract pools, not a separate one for each pool.
    public entry fun set_beneficiary_for_operator(
        operator: &signer, new_beneficiary: address
    ) acquires BeneficiaryForOperator {
        assert!(
            features::operator_beneficiary_change_enabled(),
            std::error::invalid_state(EOPERATOR_BENEFICIARY_CHANGE_NOT_SUPPORTED)
        );
        // The beneficiay address of an operator is stored under the operator's address.
        // So, the operator does not need to be validated with respect to a staking pool.
        let operator_addr = signer::address_of(operator);
        let old_beneficiary = beneficiary_for_operator(operator_addr);
        if (exists<BeneficiaryForOperator>(operator_addr)) {
            borrow_global_mut<BeneficiaryForOperator>(operator_addr).beneficiary_for_operator =
                new_beneficiary;
        } else {
            move_to(
                operator,
                BeneficiaryForOperator { beneficiary_for_operator: new_beneficiary }
            );
        };

        emit(
            SetBeneficiaryForOperator {
                operator: operator_addr,
                old_beneficiary,
                new_beneficiary
            }
        );
    }
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L855-900)
```text
    /// Distribute all unlocked (inactive) funds according to distribution shares.
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
```

**File:** aptos-move/framework/aptos-framework/sources/delegation_pool.move (L1256-1266)
```text
    /// Allows an owner to change the operator of the underlying stake pool.
    public entry fun set_operator(
        owner: &signer,
        new_operator: address
    ) acquires DelegationPoolOwnership, DelegationPool, GovernanceRecords, BeneficiaryForOperator, NextCommissionPercentage {
        let pool_address = get_owned_pool_address(signer::address_of(owner));
        // synchronize delegation and stake pools before any user operation
        // ensure the old operator is paid its uncommitted commission rewards
        synchronize_delegation_pool(pool_address);
        stake::set_operator(&retrieve_stake_pool_owner(borrow_global<DelegationPool>(pool_address)), new_operator);
    }
```

**File:** aptos-move/framework/aptos-framework/sources/delegation_pool.move (L1268-1291)
```text
    /// Allows an operator to change its beneficiary. Any existing unpaid commission rewards will be paid to the new
    /// beneficiary. To ensure payment to the current beneficiary, one should first call `synchronize_delegation_pool`
    /// before switching the beneficiary. An operator can set one beneficiary for delegation pools, not a separate
    /// one for each pool.
    public entry fun set_beneficiary_for_operator(
        operator: &signer,
        new_beneficiary: address
    ) acquires BeneficiaryForOperator {
        // The beneficiay address of an operator is stored under the operator's address.
        // So, the operator does not need to be validated with respect to a staking pool.
        let operator_addr = signer::address_of(operator);
        let old_beneficiary = beneficiary_for_operator(operator_addr);
        if (exists<BeneficiaryForOperator>(operator_addr)) {
            borrow_global_mut<BeneficiaryForOperator>(operator_addr).beneficiary_for_operator = new_beneficiary;
        } else {
            move_to(operator, BeneficiaryForOperator { beneficiary_for_operator: new_beneficiary });
        };

        emit(SetBeneficiaryForOperator {
            operator: operator_addr,
            old_beneficiary,
            new_beneficiary,
        });
    }
```
