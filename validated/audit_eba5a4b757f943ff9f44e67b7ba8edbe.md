The code path is fully traceable. Let me confirm the exact mechanics before rendering a verdict.

**Trace:**

1. `switch_operator` (line 783) removes the contract from the `old_operator` key, then:
   - Line 785–789: calls `distribute_internal(staker_address, old_operator, ...)` — correctly uses `operator = old_operator`, so any already-inactive stake is distributed with the beneficiary redirect intact.
   - Line 793–796: calls `request_commission_internal(old_operator, ...)` — this calls `add_distribution(old_operator, staking_contract, old_operator, commission_amount)`, recording `old_operator` as a shareholder in the distribution pool, and calls `stake::unlock_with_cap(commission_amount, ...)` — the commission is now **pending_inactive**, not yet withdrawable.
   - Line 803: stores the contract under `new_operator`.

2. Because the commission is only `pending_inactive` at switch time, the `distribute_internal` call at step 1 exits early at line 875–878 (`distribution_amount == 0`). The pending commission for `old_operator` remains in the distribution pool.

3. After the lockup period expires, anyone calls `distribute(staker, new_operator)`:
   - Line 847: borrows the contract keyed by `new_operator`.
   - Line 848–852: calls `distribute_internal(staker, new_operator, staking_contract)` — **`operator = new_operator`**.

4. Inside `distribute_internal` (line 889–911):
   - `recipient = old_operator` (the shareholder in the pool from step 1).
   - Line 896: `if (recipient == operator)` → `old_operator == new_operator` → **`false`**.
   - The beneficiary redirect at line 897 is **skipped**.
   - `old_operator` receives the commission directly instead of `beneficiary_for_operator(old_operator)`.

The existing test at lines 1741–1823 does **not** cover this case — it calls `distribute` and clears all pending distributions *before* the switch, so there is no `old_operator` shareholder in the pool post-switch.

---

### Title
Beneficiary redirect bypassed for old operator's pending commission after `switch_operator` — (`aptos-move/framework/aptos-framework/sources/staking_contract.move`)

### Summary
When a staker calls `switch_operator`, `request_commission_internal` records `old_operator` as a shareholder in the distribution pool for the pending commission. The staking contract is then re-keyed under `new_operator`. Any subsequent call to `distribute(staker, new_operator)` invokes `distribute_internal` with `operator = new_operator`. The beneficiary-redirect guard (`if (recipient == operator)`) compares `old_operator` against `new_operator`, evaluates to `false`, and sends the commission directly to `old_operator` instead of `beneficiary_for_operator(old_operator)`.

### Finding Description
`switch_operator` calls `request_commission_internal(old_operator, ...)` which adds `old_operator` to the distribution pool and unlocks the commission as `pending_inactive`. [1](#0-0) 

The contract is then stored under `new_operator`. [2](#0-1) 

When `distribute(staker, new_operator)` is later called, `distribute_internal` receives `operator = new_operator`. [3](#0-2) 

The beneficiary-redirect check compares `recipient` (which is `old_operator`) against `operator` (which is `new_operator`), and the redirect is never taken. [4](#0-3) 

### Impact Explanation
`old_operator`'s beneficiary — a distinct address explicitly configured by `old_operator` via `set_beneficiary_for_operator` — is deprived of APT commission that the protocol is designed to route to them. The commission is instead deposited to `old_operator` directly. This is an unauthorized reassignment of APT staking commission away from its intended on-chain recipient. [5](#0-4) 

### Likelihood Explanation
The preconditions are realistic and observable on-chain: `old_operator` has set a beneficiary (a common pattern for operator infrastructure setups), there are accrued rewards at switch time (virtually guaranteed for any active validator), and the staker switches operators (a supported and documented operation). The staker need not be malicious — the misdirection occurs mechanically regardless of intent.

### Recommendation
In `distribute_internal`, replace the narrow identity check with a lookup that is independent of the current `operator` parameter:

```move
// Instead of:
if (recipient == operator) {
    recipient = beneficiary_for_operator(operator);
};

// Use:
let redirected = beneficiary_for_operator(recipient);
if (redirected != recipient) {
    recipient = redirected;
};
```

This ensures any shareholder who has registered a beneficiary — including a former operator whose commission survived a switch — is always redirected correctly, regardless of what `operator` value is passed in.

### Proof of Concept
```move
#[test(...)]
fun test_beneficiary_bypass_after_switch_operator(...) {
    // 1. Setup: staker creates contract with operator1 (10% commission).
    setup_staking_contract(aptos_framework, staker, operator1, INITIAL_BALANCE, 10);

    // 2. operator1 sets a beneficiary.
    set_beneficiary_for_operator(operator1, beneficiary_address);

    // 3. Earn rewards (one epoch).
    stake::end_epoch();

    // 4. Staker switches to operator2 — internally calls request_commission_internal(operator1, ...)
    //    recording operator1 as a distribution pool shareholder, then re-keys under operator2.
    switch_operator(staker, operator1_address, operator2_address, 10);

    // 5. Wait for lockup to expire so the pending_inactive commission becomes inactive.
    stake::fast_forward_to_unlock(pool_address);

    // 6. Anyone distributes under the new operator key.
    distribute(staker_address, operator2_address);

    // 7. Assert: beneficiary received nothing; operator1 received the commission directly.
    assert!(coin::balance<AptosCoin>(beneficiary_address) == 0, 0);
    assert!(coin::balance<AptosCoin>(operator1_address) > INITIAL_BALANCE, 1);
    // Expected (correct) behavior: beneficiary_balance == commission, operator1_balance == INITIAL_BALANCE.
}
```

### Citations

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L361-368)
```text
    /// Return the beneficiary address of the operator.
    public fun beneficiary_for_operator(operator: address): address acquires BeneficiaryForOperator {
        if (exists<BeneficiaryForOperator>(operator)) {
            return borrow_global<BeneficiaryForOperator>(operator).beneficiary_for_operator
        } else {
            operator
        }
    }
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L793-796)
```text
        request_commission_internal(
            old_operator,
            &mut staking_contract,
        );
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L803-803)
```text
        staking_contracts.add(new_operator, staking_contract);
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L842-852)
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
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L895-898)
```text
            // If the recipient is the operator, send the commission to the beneficiary instead.
            if (recipient == operator) {
                recipient = beneficiary_for_operator(operator);
            };
```
