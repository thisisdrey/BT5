### Title
`withdraw_stake` Sweeps Entire Vesting Contract APT Balance Including Accidentally Deposited Funds — (`aptos-move/framework/aptos-framework/sources/vesting.move`)

---

### Summary

`vesting.move::withdraw_stake` reads `coin::balance<AptosCoin>(contract_address)` — the **entire** APT balance of the vesting resource account — rather than only the amount just distributed by the underlying staking contract. Because `distribute()` is a permissionless `public entry fun`, any APT accidentally (or deliberately) sent to the vesting contract address is permanently swept into the distribution pool and paid out to shareholders, with no recovery path for the original sender.

---

### Finding Description

The internal helper `withdraw_stake` is called by both the permissionless `distribute` and the admin-only `admin_withdraw`:

```move
// vesting.move lines 1071-1078
fun withdraw_stake(vesting_contract: &VestingContract, contract_address: address): Coin<AptosCoin> {
    // Claim any withdrawable distribution from the staking contract. The withdrawn coins will be sent directly to
    // the vesting contract's account.
    staking_contract::distribute(contract_address, vesting_contract.staking.operator);
    let withdrawn_coins = coin::balance<AptosCoin>(contract_address); // ← reads ENTIRE balance
    let contract_signer = &get_vesting_account_signer_internal(vesting_contract);
    coin::withdraw<AptosCoin>(contract_signer, withdrawn_coins)
}
``` [1](#0-0) 

After `staking_contract::distribute` deposits the staker's share of unlocked stake into the vesting contract account, the code reads `coin::balance<AptosCoin>(contract_address)` — which is the **total** APT balance at that address, not just the freshly deposited amount. Any APT that was already sitting at the address (from accidental transfers, direct sends, or any other source) is included in `withdrawn_coins` and subsequently distributed to shareholders.

The caller `distribute` has no access control:

```move
// vesting.move lines 719-756
public entry fun distribute(contract_address: address) acquires VestingContract {
    assert_active_vesting_contract(contract_address);
    let vesting_contract = borrow_global_mut<VestingContract>(contract_address);
    let coins = withdraw_stake(vesting_contract, contract_address);
    ...
    // distributes proportionally to all shareholders
}
``` [2](#0-1) 

The vesting contract is a resource account with a **deterministic address** derived from the admin address, nonce, and a fixed salt:

```move
// vesting.move lines 1030-1049
fun create_vesting_contract_account(admin: &signer, contract_creation_seed: vector<u8>)
    : (signer, SignerCapability) acquires AdminStore {
    ...
    seed.append(VESTING_POOL_SALT);
    seed.append(contract_creation_seed);
    let (account_signer, signer_cap) = account::create_resource_account(admin, seed);
    coin::register<AptosCoin>(&account_signer);
    ...
}
``` [3](#0-2) 

Because the address is computable off-chain and the account is registered to receive APT, anyone can send APT to it. The next call to `distribute()` — by any unprivileged account — will sweep those funds into the shareholder distribution.

---

### Impact Explanation

Any APT sent to a vesting contract address — whether by mistake or by a griefing attacker — is **permanently and irrecoverably redirected** to the vesting contract's shareholders and withdrawal address. The original sender has no mechanism to reclaim the funds. This constitutes direct theft of user-controlled APT assets via an unprivileged, permissionless transaction path.

---

### Likelihood Explanation

- Vesting contract addresses are deterministic and publicly derivable; they appear in on-chain events (`CreateVestingContract`) and are indexed by explorers.
- `aptos_account::transfer` and `coin::transfer` to a vesting contract address succeed silently (the account is registered for APT).
- `distribute()` is callable by any account with no fee beyond gas, so a griefing attacker pays only gas to trigger the sweep.
- Accidental sends to contract addresses are a well-documented real-world occurrence.

---

### Recommendation

Replace the raw `coin::balance` read with a delta measurement — record the balance **before** calling `staking_contract::distribute`, then withdraw only the difference:

```move
fun withdraw_stake(vesting_contract: &VestingContract, contract_address: address): Coin<AptosCoin> {
    let before = coin::balance<AptosCoin>(contract_address);
    staking_contract::distribute(contract_address, vesting_contract.staking.operator);
    let after = coin::balance<AptosCoin>(contract_address);
    let withdrawn_coins = after - before;
    if (withdrawn_coins == 0) return coin::zero<AptosCoin>();
    let contract_signer = &get_vesting_account_signer_internal(vesting_contract);
    coin::withdraw<AptosCoin>(contract_signer, withdrawn_coins)
}
```

This mirrors the fix recommended in the NestedFactory report (checking `_fromReserve`) and the pattern already used in `confidential_asset.move` where a before/after balance delta is used to credit only what was actually received.

---

### Proof of Concept

1. A vesting contract exists at deterministic address `V` (derivable from admin address + nonce + salt).
2. Alice accidentally sends 100 APT to `V` via `aptos_account::transfer(alice, V, 100_APT)`.
3. Attacker (or anyone) calls `vesting::distribute(V)`.
4. `withdraw_stake` calls `staking_contract::distribute(V, operator)`, which deposits, say, 10 APT of staking rewards into `V`.
5. `coin::balance<AptosCoin>(V)` now returns `110 APT` (10 from staking + 100 from Alice).
6. All 110 APT are withdrawn and distributed to shareholders proportionally.
7. Alice's 100 APT is permanently lost; she has no recourse.

The root cause — using the contract's total balance as a proxy for "funds received from the legitimate source" — is identical to the NestedFactory `transferInputTokens` bug where `address(this).balance` included accidentally sent ETH. [1](#0-0) [4](#0-3)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L719-728)
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
```

**File:** aptos-move/framework/aptos-framework/sources/vesting.move (L1030-1049)
```text
    fun create_vesting_contract_account(
        admin: &signer,
        contract_creation_seed: vector<u8>,
    ): (signer, SignerCapability) acquires AdminStore {
        let admin_store = borrow_global_mut<AdminStore>(signer::address_of(admin));
        let seed = bcs::to_bytes(&signer::address_of(admin));
        seed.append(bcs::to_bytes(&admin_store.nonce));
        admin_store.nonce += 1;

        // Include a salt to avoid conflicts with any other modules out there that might also generate
        // deterministic resource accounts for the same admin address + nonce.
        seed.append(VESTING_POOL_SALT);
        seed.append(contract_creation_seed);

        let (account_signer, signer_cap) = account::create_resource_account(admin, seed);
        // Register the vesting contract account to receive APT as it'll be sent to it when claiming unlocked stake from
        // the underlying staking contract.
        coin::register<AptosCoin>(&account_signer);

        (account_signer, signer_cap)
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
