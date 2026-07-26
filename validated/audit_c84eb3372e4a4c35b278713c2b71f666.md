### Title
Old Operator's Post-Switch Commission Bypasses Beneficiary Redirect in `distribute_internal` — (`aptos-move/framework/aptos-framework/sources/staking_contract.move`)

---

### Summary

After `switch_operator` is called, the staking contract is re-keyed under `new_operator`. The distribution pool inside the contract retains a pending entry for `old_operator` (the commission just requested via `request_commission_internal`). When `distribute` is later called with `operator = new_operator`, `distribute_internal` receives `new_operator` as its `operator` parameter. The beneficiary-redirect check `if (recipient == operator)` then evaluates `old_operator == new_operator`, which is `false`, so `beneficiary_for_operator(old_operator)` is never called. The commission is deposited directly to `old_operator`'s address, bypassing the beneficiary entirely.

---

### Finding Description

**Step 1 — `switch_operator` execution:**

`switch_operator` first calls `distribute_internal(staker_address, old_operator, &mut staking_contract)` to flush any already-inactive stake. At this point `operator = old_operator`, so the beneficiary check works correctly for any pre-existing `old_operator` entries. [1](#0-0) 

It then calls `request_commission_internal(old_operator, &mut staking_contract)`, which calls `add_distribution(old_operator, staking_contract, old_operator, commission_amount)` — inserting `old_operator` as a shareholder in the distribution pool — and calls `stake::unlock_with_cap` to move the commission to `pending_inactive` (not yet withdrawable). [2](#0-1) 

Finally, the contract is re-keyed: `staking_contracts.add(new_operator, staking_contract)`. [3](#0-2) 

**Step 2 — `distribute` called after lockup expires:**

`distribute(staker, new_operator)` borrows the staking contract under `new_operator` and calls `distribute_internal(staker, new_operator, staking_contract)`. [4](#0-3) 

**Step 3 — Beneficiary check fails:**

Inside `distribute_internal`, `operator = new_operator`. The distribution pool still has `old_operator` as a shareholder. When iterating:

```
recipient = old_operator
if (recipient == operator)   // old_operator == new_operator → FALSE
```

The `beneficiary_for_operator(operator)` branch is never taken. Coins are deposited directly to `old_operator`. [5](#0-4) 

The `operator` parameter passed into `distribute_internal` is always the *current* contract key (`new_operator`), but the distribution pool entry was created under `old_operator`. There is no mechanism to carry the original operator identity alongside the distribution pool entry.

---

### Impact Explanation

The old operator's beneficiary — a distinct address set by the operator via `set_beneficiary_for_operator` — loses the commission APT they were entitled to receive. The commission is instead deposited to `old_operator`'s address. This is a permanent, irreversible misdirection of APT staking commission. The beneficiary has no recourse once `distribute` executes.

---

### Likelihood Explanation

This triggers on every `switch_operator` call where:
1. The old operator has accumulated rewards (commission > 0), and
2. The old operator has a beneficiary set via `set_beneficiary_for_operator`.

Both conditions are common in production. The staker need not be malicious — the bug fires naturally. Any caller can trigger `distribute` (it is permissionless), so the window cannot be closed by the beneficiary after the switch.

---

### Recommendation

`distribute_internal` must resolve the beneficiary using the *recipient's own address*, not the current contract's `operator` parameter. Replace:

```move
if (recipient == operator) {
    recipient = beneficiary_for_operator(operator);
};
```

with:

```move
recipient = beneficiary_for_operator(recipient);
```

This correctly redirects any shareholder whose address has a registered beneficiary, regardless of whether they are the current or a former operator. Since `beneficiary_for_operator` returns the address itself when no beneficiary is set, this is safe for all other shareholders (staker, etc.).

---

### Proof of Concept

```move
#[test(aptos_framework = @0x1, staker = @0x100, old_op = @0x200,
       beneficiary = @0x300, new_op = @0x400)]
public entry fun test_beneficiary_bypassed_after_switch(
    aptos_framework: &signer, staker: &signer,
    old_op: &signer, beneficiary: &signer, new_op: &signer
) acquires Store, BeneficiaryForOperator {
    // 1. Setup staking contract with old_op, 10% commission
    setup_staking_contract(aptos_framework, staker, old_op, INITIAL_BALANCE, 10);
    let staker_addr   = signer::address_of(staker);
    let old_op_addr   = signer::address_of(old_op);
    let ben_addr      = signer::address_of(beneficiary);
    let new_op_addr   = signer::address_of(new_op);
    let pool_address  = stake_pool_address(staker_addr, old_op_addr);

    // 2. old_op sets beneficiary
    set_beneficiary_for_operator(old_op, ben_addr);
    assert!(beneficiary_for_operator(old_op_addr) == ben_addr, 0);

    // 3. Generate rewards
    stake::end_epoch();

    // 4. Switch operator — internally calls request_commission_internal(old_op),
    //    inserting old_op into distribution pool, then re-keys under new_op.
    switch_operator(staker, old_op_addr, new_op_addr, 10);

    // 5. Wait for lockup to expire so pending_inactive becomes inactive
    stake::fast_forward_to_unlock(pool_address);

    let ben_before    = coin::balance<AptosCoin>(ben_addr);
    let old_op_before = coin::balance<AptosCoin>(old_op_addr);

    // 6. Distribute — operator param is new_op_addr, so recipient==old_op_addr
    //    fails the `recipient == operator` check; beneficiary is bypassed.
    distribute(staker_addr, new_op_addr);

    // 7. Assert: beneficiary received nothing, old_op received commission
    assert!(coin::balance<AptosCoin>(ben_addr) == ben_before, 1);       // FAILS expectation
    assert!(coin::balance<AptosCoin>(old_op_addr) > old_op_before, 2);  // old_op got it instead
}
```

The test demonstrates that after `switch_operator`, the old operator's commission is sent to `old_op_addr` rather than `ben_addr`, violating the invariant established by `set_beneficiary_for_operator`.

### Citations

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L651-661)
```text
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
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L783-789)
```text
        let (_, staking_contract) = staking_contracts.remove(&old_operator);
        // Force distribution of any already inactive stake.
        distribute_internal(
            staker_address,
            old_operator,
            &mut staking_contract,
        );
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L803-803)
```text
        staking_contracts.add(new_operator, staking_contract);
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L842-853)
```text
    public entry fun distribute(
        staker: address, operator: address
    ) acquires Store, BeneficiaryForOperator {
        assert_staking_contract_exists(staker, operator);
        let store = borrow_global_mut<Store>(staker);
        let staking_contract = store.staking_contracts.borrow_mut(&operator);
        distribute_internal(
            staker,
            operator,
            staking_contract,
        );
    }
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L895-901)
```text
            // If the recipient is the operator, send the commission to the beneficiary instead.
            if (recipient == operator) {
                recipient = beneficiary_for_operator(operator);
            };
            aptos_account::deposit_coins(
                recipient, coin::extract(&mut coins, amount_to_distribute)
            );
```
