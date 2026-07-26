### Title
Vested-but-Pending-Inactive Funds Permanently Redirected to Admin on Contract Termination - (File: `aptos-move/framework/aptos-framework/sources/vesting.move`)

---

### Summary

`terminate_vesting_contract` only distributes **already-withdrawable** (inactive) stake to shareholders before marking the contract terminated. Funds that have been vested via `vest()` but whose staking lockup has not yet expired sit in `pending_inactive` and are silently skipped. After termination, shareholders are permanently locked out of `distribute()`, and the admin can drain all remaining funds — including the already-vested portion — to `withdrawal_address` via `admin_withdraw`.

---

### Finding Description

The termination flow in `vesting::terminate_vesting_contract` is:

```
1. distribute(contract_address)          // only flushes inactive (withdrawable) stake
2. state = VESTING_POOL_TERMINATED
3. remaining_grant = 0
4. unlock_stake(vesting_contract, active_stake)   // only unlocks active stake
``` [1](#0-0) 

`distribute()` internally calls `withdraw_stake()`, which calls `staking_contract::distribute_internal`. [2](#0-1) 

`distribute_internal` reads `inactive + pending_inactive` but then calls `stake::withdraw_with_cap` which **caps the actual withdrawal to `inactive` only**: [3](#0-2) 

```move
let (_, inactive, _, pending_inactive) = stake::get_stake(pool_address);
let total_potential_withdrawable = inactive + pending_inactive;
let coins = stake::withdraw_with_cap(&staking_contract.owner_cap, total_potential_withdrawable);
```

And `stake::withdraw_with_cap` enforces: [4](#0-3) 

```move
withdraw_amount = min(withdraw_amount, coin::value(&stake_pool.inactive));
```

So any amount sitting in `pending_inactive` — i.e., already vested via `vest()` but whose lockup cycle has not yet expired — is **not distributed to shareholders** during termination.

After termination, `distribute()` requires `VESTING_POOL_ACTIVE`: [5](#0-4) 

Shareholders can never call `distribute()` again. The only exit path for those funds is `admin_withdraw`, which sends everything to `withdrawal_address`: [6](#0-5) 

---

### Impact Explanation

Shareholders permanently lose APT that has already vested (i.e., `vest()` was called and the vested amount moved to `pending_inactive`) but whose staking lockup had not yet expired at the time of termination. That amount is later swept entirely to the admin's `withdrawal_address` via `admin_withdraw`. This is direct theft of vesting balances — an explicitly in-scope impact.

The magnitude scales with the grant size and the timing window. A vesting period is 30 days and a typical staking lockup is also on the order of weeks, so there is a routine window during which `pending_inactive` vested funds exist and can be captured.

---

### Likelihood Explanation

Every call to `vest()` moves a fraction of the grant from `active` to `pending_inactive`. The lockup cycle is independent of the vesting period, so there is almost always a non-zero `pending_inactive` balance between a `vest()` call and the next epoch boundary that clears the lockup. Any admin who terminates during this window — intentionally or not — causes the loss. A malicious admin can deliberately time the termination to maximize the captured amount.

---

### Recommendation

Before setting `state = VESTING_POOL_TERMINATED`, the contract should vest and distribute all pending vested amounts to shareholders. Concretely:

1. Call `vest(contract_address)` to move any due vested amount from `active` to `pending_inactive`.
2. Wait for (or force-reset) the lockup so `pending_inactive` becomes `inactive`, **or** record the `pending_inactive` amount and distribute it proportionally to shareholders immediately from the remaining active stake before unlocking.

The simplest safe fix mirrors the SmartEscrow recommendation: compute the shareholder-owed `pending_inactive` balance and transfer it to beneficiaries before flipping the terminated flag, so `admin_withdraw` can only ever recover genuinely unvested funds.

---

### Proof of Concept

**State before termination:**

| Stake bucket | Amount | Owner |
|---|---|---|
| `active` | `R` (remaining unvested grant) | vesting contract |
| `pending_inactive` | `V` (vested this period, lockup not expired) | vesting contract |
| `inactive` | `0` | — |

**Step-by-step:**

1. Vesting period elapses. Anyone calls `vest(contract_address)`.
   - `V` APT moves from `active` → `pending_inactive` in the stake pool.
   - A distribution entry for `V` is recorded in `staking_contract`'s distribution pool for the vesting contract address.
   - `remaining_grant` decreases by `V`.

2. Lockup has **not** yet expired (e.g., 2 weeks remain in the lockup cycle).

3. Admin calls `terminate_vesting_contract(admin, contract_address)`.
   - `distribute(contract_address)` is called. `inactive = 0`, so nothing is sent to shareholders.
   - `active_stake = R` is read (line 779); `pending_inactive = V` is **ignored** (the `_` in the destructuring).
   - `state = VESTING_POOL_TERMINATED`, `remaining_grant = 0`.
   - `unlock_stake(vesting_contract, R)` moves `R` from `active` → `pending_inactive`.
   - Now `pending_inactive = V + R`. [7](#0-6) 

4. Shareholders attempt `distribute(contract_address)` → **aborts** with `EVESTING_CONTRACT_NOT_ACTIVE`. [5](#0-4) 

5. Lockup expires. `pending_inactive` (`V + R`) transitions to `inactive`.

6. Admin calls `admin_withdraw(admin, contract_address)`.
   - `withdraw_stake` → `staking_contract::distribute_internal` → withdraws `V + R` to the vesting contract's coin balance.
   - `aptos_account::deposit_coins(vesting_contract.withdrawal_address, coins)` sends **all** `V + R` to the admin. [6](#0-5) 

**Result:** Shareholders receive `0` for the already-vested `V` APT. The admin receives `V + R` instead of only `R`.

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

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L806-812)
```text
        let coins = withdraw_stake(vesting_contract, contract_address);
        let amount = coin::value(&coins);
        if (amount == 0) {
            coin::destroy_zero(coins);
            return
        };
        aptos_account::deposit_coins(vesting_contract.withdrawal_address, coins);
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L1071-1078)
```text
    fun withdraw_stake(vesting_contract: &VestingContract, contract_address: address): Coin<AptosCoin> {
        // Claim any withdrawable distribution from the staking contract. The withdrawn coins will be sent directly to
        // the vesting contract's account.
        staking_contract::distribute(contract_address, vesting_contract.staking.operator);
        let withdrawn_coins = coin::balance<AptosCoin>(contract_address);
        let contract_signer = &get_vesting_account_signer_internal(vesting_contract);
        coin::withdraw<AptosCoin>(contract_signer, withdrawn_coins)
    }
```

**File:** aptos-move/framework/aptos-framework/sources/staking_contract.move (L868-873)
```text
        let (_, inactive, _, pending_inactive) = stake::get_stake(pool_address);
        let total_potential_withdrawable = inactive + pending_inactive;
        let coins =
            stake::withdraw_with_cap(
                &staking_contract.owner_cap, total_potential_withdrawable
            );
```

**File:** aptos-move/framework/aptos-framework/sources/stake.move (L1195-1197)
```text
        // Cap withdraw amount by total inactive coins.
        withdraw_amount = min(withdraw_amount, coin::value(&stake_pool.inactive));
        if (withdraw_amount == 0) return coin::zero<AptosCoin>();
```
