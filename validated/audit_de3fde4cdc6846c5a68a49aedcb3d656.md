### Title
Unprivileged Caller Can Freeze or Unfreeze Any Account's Fungible Asset Store via `migrate_coin_store_to_fungible_store` - (File: `aptos-move/framework/aptos-framework/sources/coin.move`)

---

### Summary

`migrate_coin_store_to_fungible_store` is a `public entry` function with no signer or access-control parameter. Any unprivileged account can call it with an arbitrary `vector<address>`. Internally it calls `maybe_convert_to_fungible_store`, which — when the victim's `CoinStore` frozen flag differs from their primary FA store frozen flag — calls `fungible_asset::set_frozen_flag_internal` to synchronize them. This lets an attacker permanently freeze a victim's primary fungible-asset store (or unfreeze a compliance-frozen FA store) without holding any `FreezeCapability` or `TransferRef`.

---

### Finding Description

**Root cause — missing access control on a migration entry point:** [1](#0-0) 

```move
/// Migrate to fungible store for `CoinType` if not yet.
public entry fun migrate_coin_store_to_fungible_store<CoinType>(
    accounts: vector<address>          // ← no signer, no capability
) acquires CoinStore, CoinConversionMap, CoinInfo {
    accounts.for_each(|account| {
        maybe_convert_to_fungible_store<CoinType>(account);
    });
}
```

The function accepts a raw `vector<address>` and iterates over every supplied address. There is no `&signer` parameter and no capability check, so any account on mainnet can submit this entry function with any victim address.

**The dangerous side-effect — frozen-flag synchronization:**

Inside `maybe_convert_to_fungible_store`, after moving coins from the `CoinStore` to the primary FA store, the code synchronizes the frozen flag: [2](#0-1) 

```move
// If the account owns a frozen CoinStore and an unfrozen primary fungible store,
// this function would convert and deposit the rest coin into the primary store
// and freeze it to make the `frozen` semantic as consistent as possible.
if (frozen != fungible_asset::is_frozen(store)) {
    fungible_asset::set_frozen_flag_internal(store, frozen);
}
```

`set_frozen_flag_internal` is a `public(friend)` function in `fungible_asset.move`, and `coin` is an explicit friend: [3](#0-2) 

This means `coin.move` can set the frozen flag on any FA store **without** a `TransferRef`, bypassing the normal capability gate.

**Two exploitable scenarios:**

| Scenario | Pre-condition | Attacker action | Result |
|---|---|---|---|
| **A – Freeze attack** | Victim has frozen `CoinStore` (value > 0) + unfrozen primary FA store | Call `migrate_coin_store_to_fungible_store<T>([victim])` | FA store is frozen; victim cannot transfer FA tokens |
| **B – Freeze bypass** | Victim has unfrozen `CoinStore` (value > 0) + frozen primary FA store | Call `migrate_coin_store_to_fungible_store<T>([victim])` | FA store is unfrozen; compliance freeze is bypassed |

The frozen-state sync only fires when `is_coin_initialized<CoinType>() && coin.value > 0`: [4](#0-3) 

so the victim must hold a non-zero legacy `CoinStore` balance for the attack to trigger.

---

### Impact Explanation

**Scenario A (freeze attack):** An unprivileged attacker permanently freezes a victim's primary FA store for any coin type that has a paired FA and whose `CoinStore` was frozen by the coin issuer. The victim can no longer transfer, withdraw, or use their fungible assets. This constitutes **unauthorized permanent freezing of user-controlled fungible assets**, which is explicitly in scope.

**Scenario B (freeze bypass):** A user (or attacker acting on their behalf) unfreezes a primary FA store that a coin issuer froze for compliance. This constitutes **unauthorized reassignment of asset state** and breaks the issuer's freeze invariant.

For APT specifically: the Aptos framework holds `FreezeCapability<AptosCoin>`. If governance ever freezes an account's APT `CoinStore` (e.g., sanctions enforcement) while the account's APT FA store remains unfrozen, any third party can call this function to freeze the FA store — or vice versa.

---

### Likelihood Explanation

- The function is `public entry`, callable by any account with a standard transaction.
- The coin-to-FA migration is an active, ongoing process on Aptos mainnet; many accounts still hold legacy `CoinStore` balances alongside primary FA stores.
- Coins with paired FAs (APT and any coin that called `ensure_paired_metadata`) are all affected.
- The only precondition is a mismatch between the `CoinStore.frozen` flag and the FA store's frozen flag, which is a realistic state during the transition period or after selective freeze actions by coin issuers.

---

### Recommendation

Add an authorization check so that only the account owner can trigger their own migration:

```move
public entry fun migrate_coin_store_to_fungible_store<CoinType>(
    caller: &signer,          // ← require signer
    accounts: vector<address>
) acquires CoinStore, CoinConversionMap, CoinInfo {
    // Only allow self-migration, or require a governance capability
    // for batch migration of other accounts.
    assert!(
        accounts.length() == 1 &&
        *accounts.borrow(0) == signer::address_of(caller),
        error::permission_denied(ENOT_AUTHORIZED)
    );
    accounts.for_each(|account| {
        maybe_convert_to_fungible_store<CoinType>(account);
    });
}
```

Alternatively, if batch migration by a privileged actor is needed, gate it behind a governance capability rather than leaving it open to any caller.

---

### Proof of Concept

```
Precondition:
  - Coin type T has a paired FA (e.g., AptosCoin or any migrated coin).
  - Victim address V has:
      CoinStore<T> { coin.value = 100, frozen = true }   // frozen by coin issuer
      primary FA store for T: frozen = false              // not yet frozen

Attack transaction (submitted by any attacker A):
  Entry function: 0x1::coin::migrate_coin_store_to_fungible_store<T>
  Arguments:      accounts = [V]

Execution trace:
  1. maybe_convert_to_fungible_store<T>(V)
  2. move_from<CoinStore<T>>(V)  → coin.value=100, frozen=true
  3. ensure_paired_metadata<T>() → metadata object
  4. ensure_primary_store_exists(V, metadata) → FA store (frozen=false)
  5. unchecked_deposit_with_no_events(fa_store, coin_to_fungible_asset(coin))
  6. frozen(true) != is_frozen(fa_store)(false)  → TRUE
  7. set_frozen_flag_internal(fa_store, true)     ← FA store is now FROZEN

Post-condition:
  - V's CoinStore<T> is destroyed.
  - V's primary FA store for T is frozen.
  - V cannot call primary_fungible_store::transfer / withdraw for T.
  - Attacker A spent only gas; no capability was required.
```

### Citations

**File:** aptos-move/framework/aptos-framework/sources/coin.move (L676-676)
```text
            if (is_coin_initialized<CoinType>() && coin.value > 0) {
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

**File:** aptos-move/framework/aptos-framework/sources/coin.move (L731-738)
```text
    /// Migrate to fungible store for `CoinType` if not yet.
    public entry fun migrate_coin_store_to_fungible_store<CoinType>(
        accounts: vector<address>
    ) acquires CoinStore, CoinConversionMap, CoinInfo {
        accounts.for_each(|account| {
                maybe_convert_to_fungible_store<CoinType>(account);
            });
    }
```

**File:** aptos-move/framework/aptos-framework/sources/fungible_asset.move (L17-17)
```text
    friend aptos_framework::coin;
```
