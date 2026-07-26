### Title
`distribute()` Blocked by `assert_active_vesting_contract()` After Termination, Permanently Redirecting Shareholders' Already-Vested APT to `withdrawal_address` — (File: `aptos-move/framework/aptos-framework/sources/vesting.move`)

### Summary

In `vesting.move`, the same state guard `assert_active_vesting_contract()` (requiring `state == VESTING_POOL_ACTIVE`) is applied to both the unlock phase (`vest()` / `unlock_rewards()`) and the claim phase (`distribute()`). When a vesting contract is terminated, coins that were already moved to `pending_inactive` by prior `vest()` calls — but whose staking lockup had not yet expired — cannot be claimed by shareholders via `distribute()` after termination. The only post-termination exit is `admin_withdraw()`, which sends the entire balance to `withdrawal_address` rather than distributing proportionally to shareholders. This permanently redirects already-vested APT away from its rightful owners.

### Finding Description

The two-phase vesting flow is:

**Phase 1 — Unlock:** `vest(contract_address)` → `unlock_rewards()` → `total_accumulated_rewards()` → `assert_active_vesting_contract()` → `unlock_stake()` → `staking_contract::unlock_stake()` → `stake::unlock_with_cap()`. Coins move from `active` to `pending_inactive` in the underlying stake pool and remain locked there until the staking lockup cycle expires.

**Phase 2 — Claim:** `distribute(contract_address)` → `assert_active_vesting_contract()` → `withdraw_stake()` → `staking_contract::distribute()` → `stake::withdraw_with_cap()`. This withdraws only `inactive` coins (lockup expired) and sends them proportionally to shareholders.

The critical guard:

```move
// vesting.move line 720
public entry fun distribute(contract_address: address) acquires VestingContract {
    assert_active_vesting_contract(contract_address);   // ← blocks if TERMINATED
    ...
}
```

```move
// vesting.move line 1060-1063
fun assert_active_vesting_contract(contract_address: address) acquires VestingContract {
    assert_vesting_contract_exists(contract_address);
    let vesting_contract = borrow_global<VestingContract>(contract_address);
    assert!(vesting_contract.state == VESTING_POOL_ACTIVE, error::invalid_state(EVESTING_CONTRACT_NOT_ACTIVE));
}
```

When `terminate_vesting_contract()` is called:

```move
// vesting.move lines 771-793
public entry fun terminate_vesting_contract(admin: &signer, contract_address: address) acquires VestingContract {
    assert_active_vesting_contract(contract_address);
    distribute(contract_address);   // only distributes currently-inactive coins
    let vesting_contract = borrow_global_mut<VestingContract>(contract_address);
    verify_admin(admin, vesting_contract);
    ...
    vesting_contract.state = VESTING_POOL_TERMINATED;   // ← permanent state flip
    vesting_contract.remaining_grant = 0;
    unlock_stake(vesting_contract, active_stake);        // moves active → pending_inactive
    ...
}
```

The `distribute()` call inside `terminate_vesting_contract()` only withdraws coins already in `inactive` state. Any coins in `pending_inactive` (unlocked by prior `vest()` calls but lockup not yet expired) are **not** distributed. After the state is set to `VESTING_POOL_TERMINATED`, `distribute()` permanently aborts for all callers. Once the lockup expires and those coins become `inactive`, the only callable function is:

```move
// vesting.move lines 797-821
public entry fun admin_withdraw(admin: &signer, contract_address: address) acquires VestingContract {
    assert!(vesting_contract.state == VESTING_POOL_TERMINATED, ...);
    ...
    aptos_account::deposit_coins(vesting_contract.withdrawal_address, coins); // all to withdrawal_address
}
```

This sends the entire balance — including coins that shareholders had already earned via `vest()` — to `withdrawal_address`, not to the shareholders.

### Impact Explanation

Shareholders permanently lose their proportional share of APT that was already vested (unlocked via `vest()`) but sitting in `pending_inactive` state at the time of termination. The exact amount equals all `pending_inactive` stake at termination time (from prior `vest()` / `unlock_rewards()` calls) plus staking rewards accrued on it. These coins are unconditionally redirected to `withdrawal_address` via `admin_withdraw()`. This constitutes permanent, unauthorized reassignment of vesting balances — a direct match to the allowed impact gate.

### Likelihood Explanation

The scenario is reachable whenever:
1. `vest()` or `unlock_rewards()` has been called at least once (coins are in `pending_inactive`), and
2. The admin calls `terminate_vesting_contract()` before the staking lockup cycle expires.

Staking lockup cycles on Aptos mainnet are on the order of months. Any termination during an active lockup window triggers the loss. The admin's termination is a documented, legitimate operation; the bug is that the code does not preserve shareholders' claim to already-unlocked coins after termination.

### Recommendation

Remove `assert_active_vesting_contract()` from `distribute()`, or introduce a separate claim path that remains callable after termination. The active-state guard is appropriate for `vest()` and `unlock_rewards()` (no new vesting should occur post-termination) but must not block the claim of coins that were already moved to `pending_inactive` before termination. One concrete fix: allow `distribute()` to execute when `state == VESTING_POOL_TERMINATED` as well, so shareholders can claim their proportional share before `admin_withdraw()` sweeps the remainder.

### Proof of Concept

```
1. Admin creates a vesting contract with a 1-month lockup and a 3-month vesting schedule.
   state = VESTING_POOL_ACTIVE

2. After month 1, anyone calls vest(contract_address).
   → unlock_rewards() → assert_active_vesting_contract() passes (ACTIVE)
   → unlock_stake() moves, say, 1000 APT to pending_inactive in the stake pool.
   Lockup expires in 2 more months.

3. Admin calls terminate_vesting_contract() in month 1 (lockup not yet expired).
   → distribute(contract_address) is called internally:
       withdraw_stake() → staking_contract::distribute() → stake::withdraw_with_cap()
       inactive coins = 0 (lockup not expired) → nothing distributed to shareholders.
   → state = VESTING_POOL_TERMINATED
   → unlock_stake(active_stake) moves remaining active stake to pending_inactive.

4. Month 3: staking lockup expires. The 1000 APT (+ rewards) from step 2 become inactive.

5. Shareholder calls distribute(contract_address).
   → assert_active_vesting_contract() → state == VESTING_POOL_TERMINATED ≠ VESTING_POOL_ACTIVE
   → ABORTS with EVESTING_CONTRACT_NOT_ACTIVE (error code 8).
   Shareholder cannot claim their 1000 APT.

6. Admin calls admin_withdraw(admin, contract_address).
   → withdraw_stake() withdraws all inactive coins (including the 1000 APT from step 2).
   → aptos_account::deposit_coins(withdrawal_address, coins)
   All 1000 APT go to withdrawal_address, not to shareholders.
   Shareholders' already-vested APT is permanently lost.
```

**Exact corrupted value:** All APT that was in `pending_inactive` at termination time (from prior `vest()` / `unlock_rewards()` calls) is redirected from shareholders to `withdrawal_address`.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L719-720)
```text
    public entry fun distribute(contract_address: address) acquires VestingContract {
        assert_active_vesting_contract(contract_address);
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

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L797-812)
```text
    public entry fun admin_withdraw(admin: &signer, contract_address: address) acquires VestingContract {
        let vesting_contract = borrow_global<VestingContract>(contract_address);
        assert!(
            vesting_contract.state == VESTING_POOL_TERMINATED,
            error::invalid_state(EVESTING_CONTRACT_STILL_ACTIVE)
        );

        let vesting_contract = borrow_global_mut<VestingContract>(contract_address);
        verify_admin(admin, vesting_contract);
        let coins = withdraw_stake(vesting_contract, contract_address);
        let amount = coin::value(&coins);
        if (amount == 0) {
            coin::destroy_zero(coins);
            return
        };
        aptos_account::deposit_coins(vesting_contract.withdrawal_address, coins);
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L1060-1063)
```text
    fun assert_active_vesting_contract(contract_address: address) acquires VestingContract {
        assert_vesting_contract_exists(contract_address);
        let vesting_contract = borrow_global<VestingContract>(contract_address);
        assert!(vesting_contract.state == VESTING_POOL_ACTIVE, error::invalid_state(EVESTING_CONTRACT_NOT_ACTIVE));
```
