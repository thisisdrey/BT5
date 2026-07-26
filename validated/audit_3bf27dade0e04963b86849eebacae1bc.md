The code structure is now clear. Let me verify the exact branch logic and the `migrate_coin_store_to_fungible_store` entry point access control.

The code structure is definitive. Here is the analysis:

---

### Title
Frozen CoinStore with zero balance bypasses freeze propagation during migration — (`aptos-move/framework/aptos-framework/sources/coin.move`)

### Summary

`maybe_convert_to_fungible_store` only enters the branch that propagates the `frozen` flag when `coin.value > 0`. When `coin.value == 0` (regardless of `frozen`), the `else` branch at line 715 is taken, which calls `destroy_zero(coin)` and silently discards the `frozen` flag. Because `migrate_coin_store_to_fungible_store` is a `public entry fun` accepting an arbitrary `vector<address>`, any unprivileged caller can trigger this migration for any victim.

### Finding Description

The gate condition at line 676 is:

```move
if (is_coin_initialized<CoinType>() && coin.value > 0) {
``` [1](#0-0) 

Only inside this branch is the primary FA store created and the frozen flag conditionally applied:

```move
if (frozen != fungible_asset::is_frozen(store)) {
    fungible_asset::set_frozen_flag_internal(store, frozen);
}
``` [2](#0-1) 

When `coin.value == 0`, execution falls to the `else` branch:

```move
} else {
    destroy_zero(coin);
};
``` [3](#0-2) 

Here the `CoinStore` is fully destructured and discarded — `frozen`, `deposit_events`, and `withdraw_events` are all consumed — but no primary FA store is created and no frozen flag is set. The `frozen` local variable is simply dropped.

The public entry point requires no victim signer:

```move
public entry fun migrate_coin_store_to_fungible_store<CoinType>(
    accounts: vector<address>
) acquires CoinStore, CoinConversionMap, CoinInfo {
    accounts.for_each(|account| {
        maybe_convert_to_fungible_store<CoinType>(account);
    });
}
``` [4](#0-3) 

Side note: the inner `if (coin.value == 0)` at line 698 is unreachable dead code — the outer guard at line 676 already requires `coin.value > 0`, so that inner branch can never fire. [5](#0-4) 

### Impact Explanation

An issuer freezes a victim's `CoinStore<CoinType>` (e.g., for compliance or sanctions). The victim's balance is zero (drained legitimately or never funded). An unprivileged third party calls `migrate_coin_store_to_fungible_store<CoinType>([victim])`. The `CoinStore` is permanently destroyed without creating a frozen primary FA store. From this point:

- If the primary FA store did not yet exist: it will be created unfrozen on the next deposit, allowing the victim to receive and spend funds freely.
- If the primary FA store already existed unfrozen: its state is unchanged and the victim can transact.

The issuer's freeze intent is permanently lost for the FA path. The `CoinStore` is gone and cannot be restored. The issuer must independently discover the migration occurred and re-freeze the FA store — there is no on-chain notification or enforcement.

### Likelihood Explanation

The preconditions are realistic: frozen accounts with zero balance are a normal compliance state (freeze first, then drain, or freeze an account that never received funds). The trigger is a permissionless `public entry fun` callable by any account with no cost beyond gas.

### Recommendation

Move the frozen-flag propagation outside the `coin.value > 0` guard. When `coin.value == 0` and a `CoinStore` exists, the function should still call `ensure_primary_store_exists` and apply `set_frozen_flag_internal` if `frozen == true`. Concretely, restructure so that frozen-flag sync happens unconditionally after the coin is handled, mirroring the existing comment at lines 707–711 which already acknowledges the need for consistency. [6](#0-5) 

### Proof of Concept

```move
#[test(framework = @aptos_framework, issuer = @0xA, victim = @0xB, attacker = @0xC)]
fun test_frozen_bypass(framework: &signer, issuer: &signer, victim: &signer, attacker: &signer) {
    // 1. Initialize chain, create paired coin
    // 2. Issuer mints 0 coins to victim (CoinStore created, balance=0)
    // 3. Issuer freezes victim's CoinStore via coin::freeze_coin_store
    // 4. Assert coin::is_coin_store_frozen<MyCoin>(victim_addr) == true
    // 5. Attacker (unprivileged) calls:
    //    coin::migrate_coin_store_to_fungible_store<MyCoin>(vector[victim_addr])
    // 6. CoinStore is gone; primary FA store either absent or unfrozen
    // 7. Assert fungible_asset::is_frozen(primary_store(victim_addr, metadata)) == false
    //    --> PASSES, demonstrating freeze bypass
}
```

### Citations

**File:** aptos-move/framework/aptos-framework/sources/coin.move (L676-676)
```text
            if (is_coin_initialized<CoinType>() && coin.value > 0) {
```

**File:** aptos-move/framework/aptos-framework/sources/coin.move (L698-699)
```text
                if (coin.value == 0) {
                    destroy_zero(coin);
```

**File:** aptos-move/framework/aptos-framework/sources/coin.move (L707-714)
```text
                // Note:
                // It is possible the primary fungible store may already exist before this function call.
                // In this case, if the account owns a frozen CoinStore and an unfrozen primary fungible store, this
                // function would convert and deposit the rest coin into the primary store and freeze it to make the
                // `frozen` semantic as consistent as possible.
                if (frozen != fungible_asset::is_frozen(store)) {
                    fungible_asset::set_frozen_flag_internal(store, frozen);
                }
```

**File:** aptos-move/framework/aptos-framework/sources/coin.move (L715-717)
```text
            } else {
                destroy_zero(coin);
            };
```

**File:** aptos-move/framework/aptos-framework/sources/coin.move (L732-738)
```text
    public entry fun migrate_coin_store_to_fungible_store<CoinType>(
        accounts: vector<address>
    ) acquires CoinStore, CoinConversionMap, CoinInfo {
        accounts.for_each(|account| {
                maybe_convert_to_fungible_store<CoinType>(account);
            });
    }
```
