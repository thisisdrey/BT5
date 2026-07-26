### Title
Permissionless `migrate_coin_store_to_fungible_store` Allows Anyone to Unfreeze Any User's Primary Fungible Store — (File: `aptos-move/framework/aptos-framework/sources/coin.move`)

---

### Summary

`coin::migrate_coin_store_to_fungible_store<CoinType>` is a `public entry fun` that accepts a `vector<address>` with **no signer parameter**. Any unprivileged account can call it for any set of victim addresses. During migration, `maybe_convert_to_fungible_store` propagates the `frozen` flag from the victim's `CoinStore` to their primary `FungibleStore`. If the victim's primary fungible store was frozen by the asset issuer (e.g., for compliance/AML) but their `CoinStore` is not frozen, the migration silently **unfreezes** the primary fungible store — bypassing the asset issuer's freeze without the issuer's or the victim's consent.

---

### Finding Description

`migrate_coin_store_to_fungible_store` is declared as:

```move
public entry fun migrate_coin_store_to_fungible_store<CoinType>(
    accounts: vector<address>
) acquires CoinStore, CoinConversionMap, CoinInfo {
    accounts.for_each(|account| {
        maybe_convert_to_fungible_store<CoinType>(account);
    });
}
``` [1](#0-0) 

There is no `signer` parameter. Any account on mainnet can submit a transaction calling this entry function with an arbitrary list of victim addresses.

Inside `maybe_convert_to_fungible_store`, when the victim has a `CoinStore` with a non-zero balance, the code:

1. Destroys the `CoinStore`, extracting its `frozen` flag.
2. Ensures the primary fungible store exists.
3. Deposits the coin balance into the fungible store.
4. **Unconditionally synchronizes the frozen flag**:

```move
if (frozen != fungible_asset::is_frozen(store)) {
    fungible_asset::set_frozen_flag_internal(store, frozen);
}
``` [2](#0-1) 

`set_frozen_flag_internal` directly mutates `FungibleStore.frozen` without requiring a `TransferRef`:

```move
public(friend) fun set_frozen_flag_internal<T: key>(
    store: Object<T>, frozen: bool
) acquires FungibleStore {
    let store_addr = store.object_address();
    borrow_global_mut<FungibleStore>(store_addr).frozen = frozen;
``` [3](#0-2) 

The normal path for freezing a fungible store requires a `TransferRef` (held by the asset issuer):

```move
public fun set_frozen_flag<T: key>(
    self: &TransferRef, store: Object<T>, frozen: bool
) acquires FungibleStore {
    assert!(
        self.metadata == store_metadata(store),
        error::invalid_argument(ETRANSFER_REF_AND_STORE_MISMATCH)
    );
    set_frozen_flag_internal(store, frozen)
}
``` [4](#0-3) 

The migration path bypasses this `TransferRef` requirement entirely by calling `set_frozen_flag_internal` directly as a `friend` function.

---

### Impact Explanation

**Attack scenario — unfreezing a compliance-frozen fungible store:**

1. Asset issuer freezes victim's primary fungible store via `set_frozen_flag` (requires `TransferRef`). Victim's `CoinStore<CoinType>` remains unfrozen (e.g., issuer only froze the FA side, or froze FA after partial migration).
2. Victim still holds a non-zero coin balance in their `CoinStore`.
3. Attacker submits: `migrate_coin_store_to_fungible_store<CoinType>([victim_address])`.
4. `maybe_convert_to_fungible_store` reads `frozen = false` from the `CoinStore`, sees `is_frozen(store) = true` on the primary fungible store, and calls `set_frozen_flag_internal(store, false)`.
5. The primary fungible store is now **unfrozen** — the victim can freely transfer assets that the issuer intended to freeze.

This is a direct unauthorized state change to a user-controlled on-chain asset (fungible asset store frozen status), reachable from an unprivileged transaction with no special keys.

**Secondary impact — forced migration without consent:**

Even absent the frozen-flag scenario, any account can force-migrate any user's `CoinStore` to a fungible store, permanently destroying the `CoinStore` resource and changing the user's storage layout without their consent. This is the direct structural analog to the Lens `tryMigrate()` bug.

---

### Likelihood Explanation

- `migrate_coin_store_to_fungible_store` is a `public entry fun` callable by any account on mainnet with no preconditions beyond gas.
- The frozen-flag bypass requires: (a) victim has a non-zero `CoinStore` balance, and (b) victim's primary fungible store is frozen while their `CoinStore` is not. This state is reachable whenever an issuer freezes only the FA side (e.g., after partial migration or via a direct FA freeze call).
- No privileged access, leaked keys, or social engineering is required.

---

### Recommendation

1. **Add a signer check** to `migrate_coin_store_to_fungible_store` so only the account owner (or a governance-whitelisted address) can trigger migration for a given address — mirroring the voluntary `migrate_to_fungible_store` which correctly requires `account: &signer`.
2. **Do not propagate the frozen flag** from `CoinStore` to `FungibleStore` during a permissionless migration path, or gate the frozen-flag synchronization on the caller holding the `TransferRef`.
3. Alternatively, restrict `migrate_coin_store_to_fungible_store` to `aptos_framework` signer only (governance-gated), consistent with how other bulk framework operations are protected.

---

### Proof of Concept

**Attacker transaction (pseudocode):**

```move
// Precondition: victim has CoinStore<AptosCoin> with frozen=false and balance > 0
//               victim's primary APT fungible store has frozen=true (set by issuer)

// Attacker submits (no special keys needed):
coin::migrate_coin_store_to_fungible_store<AptosCoin>(vector[@victim]);

// Result:
// - victim's CoinStore<AptosCoin> is destroyed
// - victim's primary APT FungibleStore.frozen is set to false
// - victim can now transfer APT freely despite the issuer's freeze
assert!(primary_fungible_store::is_frozen(@victim, apt_metadata) == false);
```

**Root cause trace:**

```
unprivileged tx
  → coin::migrate_coin_store_to_fungible_store<CoinType>(accounts)   [no signer, line 732]
    → maybe_convert_to_fungible_store<CoinType>(account)              [line 670]
      → CoinStore { frozen=false, ... } = move_from<CoinStore>(account) [line 674]
      → store = ensure_primary_store_exists(account, metadata)         [line 679]
      → frozen(false) != is_frozen(store)(true) → true                [line 712]
      → set_frozen_flag_internal(store, false)                         [line 713]
        → FungibleStore.frozen = false                                 [line 1049]
``` [5](#0-4) [1](#0-0) [6](#0-5)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/coin.move (L670-721)
```text
    fun maybe_convert_to_fungible_store<CoinType>(
        account: address
    ) acquires CoinStore, CoinConversionMap, CoinInfo {
        if (exists<CoinStore<CoinType>>(account)) {
            let CoinStore<CoinType> { coin, frozen, deposit_events, withdraw_events } =
                move_from<CoinStore<CoinType>>(account);
            if (is_coin_initialized<CoinType>() && coin.value > 0) {
                let metadata = ensure_paired_metadata<CoinType>();
                let store =
                    primary_fungible_store::ensure_primary_store_exists(
                        account, metadata
                    );

                event::emit(
                    CoinStoreDeletion {
                        coin_type: type_info::type_name<CoinType>(),
                        event_handle_creation_address: guid::creator_address(
                            event::guid(&deposit_events)
                        ),
                        deleted_deposit_event_handle_creation_number: guid::creation_num(
                            event::guid(&deposit_events)
                        ),
                        deleted_withdraw_event_handle_creation_number: guid::creation_num(
                            event::guid(&withdraw_events)
                        )
                    }
                );

                if (coin.value == 0) {
                    destroy_zero(coin);
                } else {
                    fungible_asset::unchecked_deposit_with_no_events(
                        store.object_address(),
                        coin_to_fungible_asset(coin)
                    );
                };

                // Note:
                // It is possible the primary fungible store may already exist before this function call.
                // In this case, if the account owns a frozen CoinStore and an unfrozen primary fungible store, this
                // function would convert and deposit the rest coin into the primary store and freeze it to make the
                // `frozen` semantic as consistent as possible.
                if (frozen != fungible_asset::is_frozen(store)) {
                    fungible_asset::set_frozen_flag_internal(store, frozen);
                }
            } else {
                destroy_zero(coin);
            };
            event::destroy_handle(deposit_events);
            event::destroy_handle(withdraw_events);
        };
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

**File:** aptos-move/framework/aptos-framework/sources/fungible_asset.move (L1035-1043)
```text
    public fun set_frozen_flag<T: key>(
        self: &TransferRef, store: Object<T>, frozen: bool
    ) acquires FungibleStore {
        assert!(
            self.metadata == store_metadata(store),
            error::invalid_argument(ETRANSFER_REF_AND_STORE_MISMATCH)
        );
        set_frozen_flag_internal(store, frozen)
    }
```

**File:** aptos-move/framework/aptos-framework/sources/fungible_asset.move (L1045-1052)
```text
    public(friend) fun set_frozen_flag_internal<T: key>(
        store: Object<T>, frozen: bool
    ) acquires FungibleStore {
        let store_addr = store.object_address();
        borrow_global_mut<FungibleStore>(store_addr).frozen = frozen;

        event::emit(Frozen { store: store_addr, frozen });
    }
```
