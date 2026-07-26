### Title
Old-operator beneficiary bypassed after `switch_operator` — pending commission misdirected to operator address - (`File: aptos-move/framework/aptos-framework/sources/staking_contract.move`)

---

### Summary

`switch_operator` queues a commission distribution for `old_operator` inside the shared `distribution_pool`, then re-keys the `StakingContract` under `new_operator`. Every subsequent call to `distribute` / `distribute_internal` passes `new_operator` as the `operator` argument. The beneficiary-redirect guard `if (recipient == operator)` therefore never fires for `old_operator` (because `old_operator ≠ new_operator`), so the queued commission is deposited directly to `old_operator`'s address instead of to `beneficiary_for_operator(old_operator)`. The designated beneficiary permanently loses those funds.

---

### Finding Description

`BeneficiaryForOperator` is a per-operator resource that redirects commission payments away from the operator's own address to a chosen beneficiary. [1](#0-0) 

`distribute_internal` implements the redirect with a single identity check:

```move
// If the recipient is the operator, send the commission to the beneficiary instead.
if (recipient == operator) {
    recipient = beneficiary_for_operator(operator);
};
``` [2](#0-1) 

The `operator` argument is the **map key** under which the `StakingContract` is currently stored in `Store.staking_contracts`. After `switch_operator` that key is `new_operator`, not `old_operator`.

`switch_operator` does the following in order:

1. Removes the contract from the `old_operator` key.
2. Calls `distribute_internal(staker, old_operator, …)` — distributes any already-inactive stake (correct, uses `old_operator` here).
3. Calls `request_commission_internal(old_operator, …)` — **adds `old_operator` as a shareholder in `distribution_pool`** with the accrued commission amount.
4. Re-inserts the contract under `new_operator`. [3](#0-2) 

After step 4, the `distribution_pool` inside the contract (now keyed under `new_operator`) contains `old_operator` as a shareholder. When anyone later calls `distribute(staker, new_operator)`:

```
distribute_internal(staker, new_operator, staking_contract)
                              ^^^^^^^^^^^^
                              operator = new_operator
``` [4](#0-3) 

The loop iterates over all shareholders. When `recipient = old_operator`:

```
if (recipient == operator)   →   old_operator == new_operator   →   FALSE
```

The redirect is skipped. `old_operator` receives the coins directly, bypassing `beneficiary_for_operator(old_operator)`. [5](#0-4) 

The same path is reachable through `vesting::distribute` → `staking_contract::distribute` when a vesting admin calls `update_operator`. [6](#0-5) 

---

### Impact Explanation

The designated beneficiary of `old_operator` permanently loses the commission that was queued during `switch_operator`. Those APT coins are instead deposited to `old_operator`'s address. This is a direct, irreversible misdirection of staking-reward funds from the intended recipient (beneficiary) to an unintended one (old operator), matching the "unauthorized reassignment of staking balances" impact class.

---

### Likelihood Explanation

- `switch_operator` and `set_beneficiary_for_operator` are both public entry functions callable by any staker/operator without special privilege.
- The trigger (`distribute`) is also unprivileged and callable by anyone.
- The scenario (operator sets a beneficiary, staker later switches operators) is a normal operational sequence explicitly documented and tested in the framework.
- No existing guard in `distribute_internal` checks whether a distribution-pool shareholder is a *former* operator; the check is purely `recipient == operator` against the current map key.

---

### Recommendation

Replace the static `operator` identity check in `distribute_internal` with a per-recipient lookup that is independent of the current map key:

```move
// Instead of:
if (recipient == operator) {
    recipient = beneficiary_for_operator(operator);
};

// Use:
recipient = beneficiary_for_operator(recipient);
```

`beneficiary_for_operator` already returns the address itself when no `BeneficiaryForOperator` resource exists, so this change is backward-compatible for stakers and any other non-operator shareholders. [7](#0-6) 

---

### Proof of Concept

```
1. operator1 calls set_beneficiary_for_operator(operator1, beneficiary_addr)
   → BeneficiaryForOperator { beneficiary_for_operator: beneficiary_addr } stored at operator1

2. Rewards accumulate in the stake pool (e.g. 100 APT rewards, 10% commission = 10 APT owed to operator1)

3. staker calls switch_operator(staker, operator1, operator2, 10%)
   Inside switch_operator:
     a. distribute_internal(staker, operator1, contract)  ← distributes already-inactive stake (0 here)
     b. request_commission_internal(operator1, contract)
        → adds operator1 as shareholder in distribution_pool with 10 APT
     c. contract re-keyed under operator2

4. Anyone calls distribute(staker, operator2)
   → distribute_internal(staker, operator2, contract)
      operator = operator2
      Loop: recipient = operator1
        check: operator1 == operator2  →  FALSE
        → aptos_account::deposit_coins(operator1, 10 APT)   ← beneficiary_addr receives 0

Result: beneficiary_addr receives 0 APT; operator1 receives 10 APT.
Expected: beneficiary_addr receives 10 APT; operator1 receives 0 APT.
``` [8](#0-7) [9](#0-8)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L106-108)
```text
    struct BeneficiaryForOperator has key {
        beneficiary_for_operator: address
    }
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L362-368)
```text
    public fun beneficiary_for_operator(operator: address): address acquires BeneficiaryForOperator {
        if (exists<BeneficiaryForOperator>(operator)) {
            return borrow_global<BeneficiaryForOperator>(operator).beneficiary_for_operator
        } else {
            operator
        }
    }
```

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

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L856-860)
```text
    fun distribute_internal(
        staker: address,
        operator: address,
        staking_contract: &mut StakingContract,
    ) acquires BeneficiaryForOperator {
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

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L823-835)
```text
    public entry fun update_operator(
        admin: &signer,
        contract_address: address,
        new_operator: address,
        commission_percentage: u64,
    ) acquires VestingContract {
        let vesting_contract = borrow_global_mut<VestingContract>(contract_address);
        verify_admin(admin, vesting_contract);
        let contract_signer = &get_vesting_account_signer_internal(vesting_contract);
        let old_operator = vesting_contract.staking.operator;
        staking_contract::switch_operator(contract_signer, old_operator, new_operator, commission_percentage);
        vesting_contract.staking.operator = new_operator;
        vesting_contract.staking.commission_percentage = commission_percentage;
```
