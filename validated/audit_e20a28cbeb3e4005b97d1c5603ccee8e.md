### Title
Permissionless `vesting::unlock_rewards` Enables Denial-of-Compounding Attack on Vesting Balances — (File: `aptos-move/framework/aptos-framework/sources/vesting.move`)

---

### Summary

`vesting::unlock_rewards` is a `public entry fun` that accepts only a `contract_address: address` parameter with **no signer or authorization check**. Any unprivileged account can call it on any active vesting contract at any time, forcing accumulated staking rewards out of the `active` stake pool and into `pending_inactive`. Repeated calls every epoch prevent rewards from compounding in the active pool, materially reducing the final vesting balances of all shareholders.

---

### Finding Description

`vesting::unlock_rewards` is declared as:

```move
/// Unlock any accumulated rewards.
public entry fun unlock_rewards(contract_address: address) acquires VestingContract {
    let accumulated_rewards = total_accumulated_rewards(contract_address);
    let vesting_contract = borrow_global<VestingContract>(contract_address);
    unlock_stake(vesting_contract, accumulated_rewards);
}
``` [1](#0-0) 

There is no `signer` parameter and no call to `verify_admin` or any equivalent access control. The function is callable by any account on mainnet.

`unlock_stake` internally calls `staking_contract::unlock_stake`, which calls `stake::unlock_with_cap`, moving the reward coins from the `active` bucket to `pending_inactive` in the underlying `StakePool`:

```move
fun unlock_stake(vesting_contract: &VestingContract, amount: u64) {
    let contract_signer = &get_vesting_account_signer_internal(vesting_contract);
    staking_contract::unlock_stake(contract_signer, vesting_contract.staking.operator, amount);
}
``` [2](#0-1) 

`staking_contract::unlock_stake` calls `stake::unlock_with_cap`, which extracts coins from `stake_pool.active` and merges them into `stake_pool.pending_inactive`:

```move
let unlocked_stake = coin::extract(&mut stake_pool.active, amount);
coin::merge<AptosCoin>(&mut stake_pool.pending_inactive, unlocked_stake);
``` [3](#0-2) 

Coins in `pending_inactive` stop compounding once the lockup expires and they transition to `inactive`. Coins in `active` compound every epoch.

By contrast, the intentionally permissionless `distribute` function carries an explicit design comment explaining *why* it needs no restriction:

> "Allow anyone to distribute already unlocked funds. This does not affect reward compounding and therefore does not need to be restricted to just the staker or operator." [4](#0-3) 

This comment implicitly confirms that functions **which do affect reward compounding** — like `unlock_rewards` — were intended to be restricted. The authorization check was simply omitted.

The same permissionless pattern applies to `vest` and `vest_many`, which call `unlock_rewards` internally:

```move
public entry fun vest(contract_address: address) acquires VestingContract {
    // Unlock all rewards first, if any.
    unlock_rewards(contract_address);
    ...
}
``` [5](#0-4) 

And `unlock_rewards_many` allows batching the attack across all vesting contracts in a single transaction:

```move
public entry fun unlock_rewards_many(contract_addresses: vector<address>) acquires VestingContract {
``` [6](#0-5) 

---

### Impact Explanation

An attacker who calls `unlock_rewards` on a vesting contract after every epoch forces all accumulated rewards into `pending_inactive` before they can compound. Coins in `pending_inactive` earn rewards only until the lockup expires; after that they become `inactive` and earn nothing. Coins left in `active` compound every epoch indefinitely.

For a vesting contract holding 10 million APT at a 1% per-epoch reward rate, the difference between compounded active rewards and non-compounded pending_inactive rewards over a multi-year vesting schedule is material — directly reducing the APT balance ultimately distributed to shareholders. This constitutes unauthorized reduction of vesting balances, which is within the Aptos bounty's allowed impact scope ("staking balances, vesting balances").

Additionally, each call to `unlock_rewards` triggers `request_commission_internal`, which pays the operator's commission on the current accumulated rewards. Repeated calls do not increase total commission paid (the principal is updated each time), but they do accelerate commission payouts and reduce the principal faster, compounding the compounding-loss effect for shareholders.

---

### Likelihood Explanation

The attack requires no special privileges, no tokens, and no setup beyond knowing a vesting contract address (all contract addresses are discoverable on-chain via `AdminStore.vesting_contracts`). The attacker pays only gas. A simple off-chain script can monitor epoch boundaries and call `unlock_rewards_many` on all known vesting contracts each epoch. The attack is cheap, repeatable, and fully automated.

---

### Recommendation

Add an authorization check to `unlock_rewards` (and `vest`) so that only the vesting contract admin, a designated management role holder, or a shareholder/beneficiary of the contract can trigger reward unlocking. For example:

```move
public entry fun unlock_rewards(caller: &signer, contract_address: address) acquires VestingContract {
    let vesting_contract = borrow_global<VestingContract>(contract_address);
    let caller_addr = signer::address_of(caller);
    assert!(
        caller_addr == vesting_contract.admin
            || pool_u64::contains(&vesting_contract.grant_pool, caller_addr)
            || /* beneficiary check */,
        error::permission_denied(EPERMISSION_DENIED)
    );
    let accumulated_rewards = total_accumulated_rewards(contract_address);
    unlock_stake(vesting_contract, accumulated_rewards);
}
```

Alternatively, follow the same pattern as `distribute` but document explicitly that `unlock_rewards` is intentionally permissionless and accept the compounding-loss risk — but this would be a deliberate design decision, not the current undocumented omission.

---

### Proof of Concept

1. Attacker deploys a monitoring script that listens for epoch-end events on Aptos mainnet.
2. On each epoch end, the script enumerates all `AdminStore.vesting_contracts` addresses on-chain.
3. The script submits a transaction calling `vesting::unlock_rewards_many(all_contract_addresses)`.
4. Each call moves all accumulated rewards for every vesting contract from `active` to `pending_inactive`.
5. Rewards in `pending_inactive` earn for one lockup cycle then become `inactive` (zero yield).
6. Shareholders receive materially less APT at distribution time compared to the case where rewards compound in `active`.

The existing test `test_unlock_rewards_twice_should_not_double_count` confirms the function is callable without any signer and that calling it moves rewards to `pending_inactive`:

```move
unlock_rewards(contract_address);
stake::assert_stake_pool(stake_pool_address, GRANT_AMOUNT, 0, 0, rewards);
``` [7](#0-6) 

No privileged access, no special setup, and no existing guard prevents this attack on mainnet.

### Citations

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L635-640)
```text
    /// Unlock any accumulated rewards.
    public entry fun unlock_rewards(contract_address: address) acquires VestingContract {
        let accumulated_rewards = total_accumulated_rewards(contract_address);
        let vesting_contract = borrow_global<VestingContract>(contract_address);
        unlock_stake(vesting_contract, accumulated_rewards);
    }
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L643-651)
```text
    public entry fun unlock_rewards_many(contract_addresses: vector<address>) acquires VestingContract {
        let len = contract_addresses.length();

        assert!(len != 0, error::invalid_argument(EVEC_EMPTY_FOR_MANY_FUNCTION));

        contract_addresses.for_each_ref(|contract_address| {
            let contract_address: address = *contract_address;
            unlock_rewards(contract_address);
        });
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L655-657)
```text
    public entry fun vest(contract_address: address) acquires VestingContract {
        // Unlock all rewards first, if any.
        unlock_rewards(contract_address);
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L1066-1069)
```text
    fun unlock_stake(vesting_contract: &VestingContract, amount: u64) {
        let contract_signer = &get_vesting_account_signer_internal(vesting_contract);
        staking_contract::unlock_stake(contract_signer, vesting_contract.staking.operator, amount);
    }
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L1432-1438)
```text
        unlock_rewards(contract_address);
        stake::assert_stake_pool(stake_pool_address, GRANT_AMOUNT, 0, 0, rewards);
        assert!(remaining_grant(contract_address) == GRANT_AMOUNT, 0);

        // Calling unlock_rewards a second time shouldn't change anything as no new rewards has accumulated.
        unlock_rewards(contract_address);
        stake::assert_stake_pool(stake_pool_address, GRANT_AMOUNT, 0, 0, rewards);
```

**File:** aptos-move/framework/aptos-framework/sources/stake.move (L1159-1161)
```text
        let amount = min(amount, coin::value(&stake_pool.active));
        let unlocked_stake = coin::extract(&mut stake_pool.active, amount);
        coin::merge<AptosCoin>(&mut stake_pool.pending_inactive, unlocked_stake);
```

**File:** aptos-move/framework/cached-packages/src/aptos_framework_sdk_builder.rs (L4978-4979)
```rust
/// Allow anyone to distribute already unlocked funds. This does not affect reward compounding and therefore does
/// not need to be restricted to just the staker or operator.
```
