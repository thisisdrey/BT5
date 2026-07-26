### Title
Frozen Fungible Asset Store Silently Unfrozen During Coin Migration — (`File: aptos-move/framework/aptos-framework/sources/coin.move`)

---

### Summary

`maybe_convert_to_fungible_store` in `coin.move` unconditionally overwrites the `frozen` flag of an existing primary `FungibleStore` with the `frozen` value from the `CoinStore` being migrated. If an issuer has frozen the user's primary FA store (via `TransferRef`) but the user's `CoinStore` is unfrozen, calling the public entry `migrate_to_fungible_store` silently unfreezes the FA store — bypassing the issuer's freeze without any authorization check.

---

### Finding Description

`maybe_convert_to_fungible_store` is called by the public entry `migrate_to_fungible_store<CoinType>`. It removes the `CoinStore<CoinType>` from the account and deposits its balance into the primary `FungibleStore`. It then synchronizes the frozen flag: [1](#0-0) 

```move
if (frozen != fungible_asset::is_frozen(store)) {
    fungible_asset::set_frozen_flag_internal(store, frozen);
}
```

Here `frozen` is the `CoinStore.frozen` field (extracted from the destroyed `CoinStore`). If `CoinStore.frozen = false` and the FA store was independently frozen by the issuer (`is_frozen(store) = true`), the condition evaluates to `true` and `set_frozen_flag_internal` is called with `frozen = false`, **unfreezing the store**.

`set_frozen_flag_internal` is a `public(friend)` function that bypasses the `TransferRef` authorization check required by the normal `set_frozen_flag` path: [2](#0-1) 

The code comment at line 708 only describes the case where the CoinStore is frozen and the FA store is not — it does not account for the reverse, where the FA store was frozen by the issuer independently: [1](#0-0) 

The public entry point is: [3](#0-2) 

The code explicitly acknowledges that both stores can coexist: [1](#0-0) 

---

### Impact Explanation

An issuer (or the Aptos framework for APT) can freeze a user's primary FA store via `set_frozen_flag` / `set_frozen_flag_internal`. The freeze is intended to prevent `withdraw` and `deposit`: [4](#0-3) [5](#0-4) 

If the user still holds a non-zero, unfrozen `CoinStore<CoinType>`, they can call `migrate_to_fungible_store<CoinType>()` to unfreeze their FA store without the issuer's `TransferRef`. After migration, `withdraw` and `deposit` succeed on the previously frozen store, allowing unauthorized transfer of frozen fungible assets including APT.

---

### Likelihood Explanation

- `migrate_to_fungible_store` is a `public entry fun` callable by any user with no privilege requirement.
- A user can hold both a `CoinStore` and a primary FA store simultaneously; the code explicitly handles this case.
- The issuer freezing the FA store but not the CoinStore is a realistic scenario (they are separate operations via separate APIs).
- For APT, the framework holds freeze capability over both, but for other paired coins the issuer may freeze only the FA store.

---

### Recommendation

Do not overwrite the FA store's frozen flag with the CoinStore's frozen flag when the FA store was already frozen. The migration should only propagate the CoinStore's frozen state to the FA store if the FA store did not already have an independently set frozen state, or should only freeze (never unfreeze) during migration:

```move
// Only freeze if CoinStore was frozen; never unfreeze an already-frozen FA store
if (frozen && !fungible_asset::is_frozen(store)) {
    fungible_asset::set_frozen_flag_internal(store, true);
}
```

---

### Proof of Concept

1. Issuer creates `CoinType` with a paired FA and `TransferRef`.
2. User registers `CoinStore<CoinType>` (unfrozen) and receives some balance.
3. User's primary FA store is created (e.g., via a prior FA deposit).
4. Issuer calls `primary_fungible_store::set_frozen_flag(&transfer_ref, user_addr, true)` — FA store is now frozen.
5. User calls `coin::migrate_to_fungible_store<CoinType>()`.
6. Inside `maybe_convert_to_fungible_store`: `frozen = false` (from CoinStore), `is_frozen(store) = true` → condition is `true` → `set_frozen_flag_internal(store, false)` is called.
7. FA store is now unfrozen. User calls `primary_fungible_store::withdraw(user, metadata, amount)` — succeeds, bypassing the issuer's freeze. [6](#0-5) [2](#0-1) [7](#0-6)

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

**File:** aptos-move/framework/aptos-framework/sources/coin.move (L724-729)
```text
    public entry fun migrate_to_fungible_store<CoinType>(
        account: &signer
    ) acquires CoinStore, CoinConversionMap, CoinInfo {
        let account_addr = signer::address_of(account);
        maybe_convert_to_fungible_store<CoinType>(account_addr);
    }
```

**File:** aptos-move/framework/aptos-framework/sources/fungible_asset.move (L974-987)
```text
    inline fun withdraw_sanity_check_impl<T: key>(
        owner_address: address, store: Object<T>, abort_on_dispatch: bool
    ) {
        assert!(
            object::owns(store, owner_address),
            error::permission_denied(ENOT_STORE_OWNER)
        );
        let fa_store = borrow_store_resource(&store);
        assert!(
            !abort_on_dispatch || !has_withdraw_dispatch_function(fa_store.metadata),
            error::invalid_argument(EINVALID_DISPATCHABLE_OPERATIONS)
        );
        assert!(!fa_store.frozen, error::permission_denied(ESTORE_IS_FROZEN));
    }
```

**File:** aptos-move/framework/aptos-framework/sources/fungible_asset.move (L990-999)
```text
    public fun deposit_sanity_check<T: key>(
        store: Object<T>, abort_on_dispatch: bool
    ) acquires FungibleStore, DispatchFunctionStore {
        let fa_store = borrow_store_resource(&store);
        assert!(
            !abort_on_dispatch || !has_deposit_dispatch_function(fa_store.metadata),
            error::invalid_argument(EINVALID_DISPATCHABLE_OPERATIONS)
        );
        assert!(!fa_store.frozen, error::permission_denied(ESTORE_IS_FROZEN));
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

**File:** aptos-move/framework/aptos-framework/sources/primary_fungible_store.move (L179-185)
```text
    /// Withdraw `amount` of fungible asset from the given account's primary store.
    public fun withdraw<T: key>(owner: &signer, metadata: Object<T>, amount: u64): FungibleAsset acquires DeriveRefPod {
        let store = ensure_primary_store_exists(signer::address_of(owner), metadata);
        // Check if the store object has been burnt or not. If so, unburn it first.
        may_be_unburn(owner, store);
        dispatchable_fungible_asset::withdraw(owner, store, amount)
    }
```
