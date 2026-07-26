### Title
`freeze_coin_store` Freeze Bypass via FA-Migration Withdrawal Path — (`aptos-move/framework/aptos-framework/sources/coin.move`)

### Summary

The `freeze_coin_store` function sets `CoinStore<CoinType>.frozen = true` to block transfers. After the FA migration, however, `coin::withdraw` and `coin::transfer` route entirely through `primary_fungible_store::withdraw`, which checks only the **FA store's** frozen flag. The CoinStore frozen flag is never consulted. Because `coin::deposit` simultaneously routes new deposits into the FA store (not the CoinStore), an account can accumulate a balance in an unfrozen FA store while its CoinStore is frozen, and then drain that balance freely — bypassing the freeze entirely.

### Finding Description

**`freeze_coin_store` only freezes the CoinStore:** [1](#0-0) 

```move
#[legacy_entry_fun]
public entry fun freeze_coin_store<CoinType>(
    account_addr: address, _freeze_cap: &FreezeCapability<CoinType>
) acquires CoinStore {
    let coin_store = borrow_global_mut<CoinStore<CoinType>>(account_addr);
    coin_store.frozen = true;
}
```

**`coin::deposit` now routes all incoming coins to the FA store, not the CoinStore:** [2](#0-1) 

```move
public fun deposit<CoinType>(
    account_addr: address, coin: Coin<CoinType>
) acquires CoinConversionMap, CoinInfo {
    primary_fungible_store::deposit(account_addr, coin_to_fungible_asset(coin));
}
```

**`coin::withdraw` routes entirely through the FA store, never reading `CoinStore.frozen`:** [3](#0-2) 

```move
public fun withdraw<CoinType>(
    account: &signer, amount: u64
): Coin<CoinType> acquires CoinConversionMap, CoinInfo, PairedCoinType {
    let fa =
        primary_fungible_store::withdraw(
            account, ensure_paired_metadata<CoinType>(), amount
        );
    fungible_asset_to_coin(fa)
}
```

`primary_fungible_store::withdraw` calls `dispatchable_fungible_asset::withdraw` → `fungible_asset::withdraw` → `withdraw_sanity_check_impl`, which asserts only `!fa_store.frozen`: [4](#0-3) 

The CoinStore frozen flag is never read. The formal spec for `coin::withdraw` acknowledges the invariant (`aborts_if coin_store.frozen`) but is disabled with `pragma verify = false`: [5](#0-4) 

**`coin::transfer` has the identical gap:** [6](#0-5) 

### Impact Explanation

A coin issuer holding `FreezeCapability<CoinType>` calls `freeze_coin_store` to block a sanctioned or misbehaving account. Because all post-migration deposits land in the FA store (unfrozen), the account retains a spendable balance there. Calling `coin::withdraw` or `coin::transfer` succeeds unconditionally, moving funds that the issuer intended to freeze. This is a direct bypass of the asset-freeze access-control invariant, allowing unauthorized reassignment of fungible assets.

### Likelihood Explanation

The condition is reachable on mainnet today:
- `COIN_TO_FUNGIBLE_ASSET_MIGRATION` is enabled on mainnet (feature flag 60).
- Any account that received coins via `coin::deposit` after migration has a balance in the FA store.
- `freeze_coin_store` is still a callable public entry function.
- The FA store is created unfrozen by default; `freeze_coin_store` does not touch it.

No special setup beyond a normal coin-issuer freeze workflow is required.

### Recommendation

`freeze_coin_store` must also freeze the paired FA store. The fix should mirror what `maybe_convert_to_fungible_store` already does for the migration path: [7](#0-6) 

Concretely, `freeze_coin_store` should call `fungible_asset::set_frozen_flag_internal` (or the equivalent via `get_paired_transfer_ref`) on the primary FA store whenever a paired metadata object exists. Symmetrically, `unfreeze_coin_store` must unfreeze the FA store. Alternatively, `freeze_coin_store` should be hard-deprecated and callers migrated to `primary_fungible_store::set_frozen_flag` directly.

### Proof of Concept

```
1. Coin issuer initializes CoinType, holds FreezeCapability.

2. User account has a CoinStore<CoinType> (created pre-migration).

3. Someone calls coin::deposit(user_addr, coins)
   → primary_fungible_store::deposit → FA store balance += N
   → CoinStore balance unchanged (still 0 or some legacy amount)

4. Issuer calls freeze_coin_store<CoinType>(user_addr, &freeze_cap)
   → CoinStore.frozen = true
   → FA store: frozen = false  ← untouched

5. User calls coin::withdraw<CoinType>(&user_signer, N)
   → primary_fungible_store::withdraw(user, metadata, N)
   → fungible_asset::withdraw checks fa_store.frozen == false → passes
   → N coins returned as Coin<CoinType>

6. User calls coin::transfer<CoinType>(&user_signer, recipient, N)
   → same FA path → succeeds

Result: N coins transferred out of a "frozen" account.
```

### Citations

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

**File:** aptos-move/framework/aptos-framework/sources/coin.move (L906-910)
```text
    public fun deposit<CoinType>(
        account_addr: address, coin: Coin<CoinType>
    ) acquires CoinConversionMap, CoinInfo {
        primary_fungible_store::deposit(account_addr, coin_to_fungible_asset(coin));
    }
```

**File:** aptos-move/framework/aptos-framework/sources/coin.move (L970-977)
```text
    #[legacy_entry_fun]
    /// Freeze a CoinStore to prevent transfers
    public entry fun freeze_coin_store<CoinType>(
        account_addr: address, _freeze_cap: &FreezeCapability<CoinType>
    ) acquires CoinStore {
        let coin_store = borrow_global_mut<CoinStore<CoinType>>(account_addr);
        coin_store.frozen = true;
    }
```

**File:** aptos-move/framework/aptos-framework/sources/coin.move (L1136-1144)
```text
    public entry fun transfer<CoinType>(
        from: &signer, to: address, amount: u64
    ) acquires CoinConversionMap, CoinInfo {
        let fa =
            primary_fungible_store::withdraw(
                from, ensure_paired_metadata<CoinType>(), amount
            );
        primary_fungible_store::deposit(to, fa);
    }
```

**File:** aptos-move/framework/aptos-framework/sources/coin.move (L1152-1160)
```text
    public fun withdraw<CoinType>(
        account: &signer, amount: u64
    ): Coin<CoinType> acquires CoinConversionMap, CoinInfo, PairedCoinType {
        let fa =
            primary_fungible_store::withdraw(
                account, ensure_paired_metadata<CoinType>(), amount
            );
        fungible_asset_to_coin(fa)
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

**File:** aptos-move/framework/aptos-framework/sources/coin.spec.move (L533-557)
```text
    spec withdraw<CoinType>(account: &signer, amount: u64): Coin<CoinType> {
        // TODO(fa_migration)
        pragma verify = false;
        include WithdrawAbortsIf<CoinType>;
        modifies global<CoinStore<CoinType>>(account_addr);
        let account_addr = signer::address_of(account);
        let coin_store = global<CoinStore<CoinType>>(account_addr);
        let balance = coin_store.coin.value;
        let post coin_post = global<CoinStore<CoinType>>(account_addr).coin.value;
        ensures coin_post == balance - amount;
        ensures result == Coin<CoinType> { value: amount };
    }

    spec schema WithdrawAbortsIf<CoinType> {
        account: &signer;
        amount: u64;
        let account_addr = signer::address_of(account);
        let coin_store = global<CoinStore<CoinType>>(account_addr);
        let balance = coin_store.coin.value;
        /// [high-level-req-6.6]
        aborts_if !exists<CoinStore<CoinType>>(account_addr);
        /// [high-level-req-8.1]
        aborts_if coin_store.frozen;
        aborts_if balance < amount;
    }
```
