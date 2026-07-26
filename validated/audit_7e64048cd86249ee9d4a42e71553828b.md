### Title
Operator Can Redirect Pending Commission from Designated Beneficiary by Changing `BeneficiaryForOperator` Before Distribution - (File: `aptos-move/framework/aptos-framework/sources/staking_contract.move`)

---

### Summary

In `staking_contract.move`, the `set_beneficiary_for_operator` entry function allows an operator to freely change their commission-recipient address at any time, with no enforcement that pending commission is first distributed. Because `distribute_internal` resolves the beneficiary at distribution time (not at commission-request time), an operator can redirect already-earned, already-unlocked APT commission away from the designated beneficiary to any address they control — including themselves — by calling `set_beneficiary_for_operator` before `distribute` is called.

---

### Finding Description

**Commission tracking and distribution flow:**

When `request_commission_internal` is called, the commission amount is recorded in the `distribution_pool` under the **operator's address** as the shareholder:

```move
add_distribution(operator, staking_contract, operator, commission_amount)
``` [1](#0-0) 

The commission then enters `pending_inactive` state in the underlying stake pool and must wait for the lockup period to expire before it becomes withdrawable.

When `distribute_internal` is finally called (by anyone, since `distribute` is permissionless), it iterates through the `distribution_pool` shareholders and, for each recipient that equals the operator address, **looks up the beneficiary at that moment**:

```move
if (recipient == operator) {
    recipient = beneficiary_for_operator(operator);
};
aptos_account::deposit_coins(recipient, coin::extract(&mut coins, amount_to_distribute));
``` [2](#0-1) 

**The mutable field:**

`set_beneficiary_for_operator` unconditionally overwrites `BeneficiaryForOperator` with no check that pending commission has been distributed first:

```move
public entry fun set_beneficiary_for_operator(
    operator: &signer, new_beneficiary: address
) acquires BeneficiaryForOperator {
    ...
    borrow_global_mut<BeneficiaryForOperator>(operator_addr).beneficiary_for_operator =
        new_beneficiary;
``` [3](#0-2) 

The docstring acknowledges the risk but only offers an off-chain advisory ("one should first call `distribute` before switching the beneficiary") — there is no on-chain enforcement: [4](#0-3) 

**The same pattern exists in `delegation_pool.move`**, where `set_beneficiary_for_operator` also performs no synchronization before overwriting the beneficiary: [5](#0-4) 

---

### Impact Explanation

An operator who has designated a third-party beneficiary (e.g., a business partner or revenue-sharing counterparty) can steal that beneficiary's earned APT commission:

1. Commission is earned and `request_commission` is called → commission enters `pending_inactive` in the stake pool, recorded under the operator's address in `distribution_pool`.
2. The lockup period expires → commission becomes `inactive` (withdrawable).
3. **Before** `distribute` is called, the operator calls `set_beneficiary_for_operator(operator_own_address)`.
4. Anyone calls `distribute` → `distribute_internal` resolves `beneficiary_for_operator(operator)` to the operator's own address and deposits the full commission there.

The original beneficiary receives nothing despite having a legitimate expectation of payment. The stolen amount equals the full pending commission in APT (staking rewards × commission percentage), which can be substantial for large stake pools over long lockup periods.

This is a direct theft of APT staking balances from a user-controlled on-chain asset (the beneficiary's entitled commission).

---

### Likelihood Explanation

- The operator is the sole controller of `set_beneficiary_for_operator` and can call it at any time with no cooldown, no timelock, and no on-chain guard.
- `distribute` is permissionless, so the beneficiary cannot atomically "lock in" their payment — the operator can always front-run a `distribute` call with `set_beneficiary_for_operator` in the same or a prior block.
- The attack is profitable whenever the pending commission exceeds the gas cost of one transaction, which is trivially true for any meaningful stake pool.
- The operator has full information about when commission becomes withdrawable (lockup expiry is on-chain), so timing the attack requires no special capability.

---

### Recommendation

Enforce distribution of pending commission before allowing a beneficiary change, mirroring the pattern already used in `switch_operator` and `update_commision`:

```move
public entry fun set_beneficiary_for_operator(
    operator: &signer, new_beneficiary: address
) acquires Store, BeneficiaryForOperator {
    // Distribute all pending commission to the current beneficiary first.
    // Iterate over all staking contracts for this operator and distribute.
    // (Requires passing staker address or iterating a reverse index.)
    ...
    borrow_global_mut<BeneficiaryForOperator>(operator_addr).beneficiary_for_operator = new_beneficiary;
}
```

Alternatively, snapshot the beneficiary address at `request_commission_internal` time and store it alongside the distribution entry, so the payout target is immutable once commission is requested.

---

### Proof of Concept

```
1. staker S creates a staking contract with operator O, commission = 10%.
   beneficiary B is set via set_beneficiary_for_operator(O, B).

2. Stake pool earns rewards. O calls request_commission(O, S, O).
   → commission_amount = X APT enters pending_inactive.
   → distribution_pool records: operator O → X shares.

3. Lockup expires. X APT becomes inactive (withdrawable).

4. O calls set_beneficiary_for_operator(O, O_own_address).
   → BeneficiaryForOperator[O].beneficiary_for_operator = O_own_address.

5. Anyone calls distribute(S, O).
   → distribute_internal iterates distribution_pool.
   → recipient = O (operator) → beneficiary_for_operator(O) = O_own_address.
   → X APT deposited to O_own_address.

Result: B receives 0 APT. O steals X APT of commission that B was owed.
```

The root cause is identical to the Augur H02 finding: a mutable routing field (`BeneficiaryForOperator` ↔ Augur's `fingerprint`) that is resolved at payout time rather than at claim time, allowing the controlling party to redirect funds away from the legitimate recipient by updating the field between claim and payout. [6](#0-5) [7](#0-6)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L637-657)
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
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L807-823)
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
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L856-901)
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
```

**File:** aptos-move/framework/aptos-framework/sources/delegation_pool.move (L1272-1291)
```text
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
