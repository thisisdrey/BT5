Now I have all the code I need. Let me read the exact lines around the critical condition to confirm the full path.

### Title
Unprivileged `migrate_to_fungible_store` Overwrites Frozen FA Store Flag with CoinStore's Unfrozen Flag — (`aptos-move/framework/aptos-framework/sources/coin.move`)

---

### Summary

`maybe_convert_to_fungible_store` unconditionally synchronises the `frozen` flag from the CoinStore to the primary fungible store whenever the two values differ. When an account holds an **unfrozen** CoinStore (with `coin.value > 0`) and a **frozen** primary fungible store, calling the public entry `migrate_to_fungible_store` causes the frozen FA store to be silently unfrozen — with no capability check, no privileged signer, and no abort.

---

### Finding Description

`migrate_to_fungible_store` is a `public entry fun` callable by any account owner. [1](#0-0) 

It delegates to `maybe_convert_to_fungible_store`, which:

1. Moves the `CoinStore<CoinType>` out of storage, extracting the `frozen` field.
2. Enters the active branch only when `coin.value > 0`.
3. Calls `ensure_primary_store_exists` — returning the already-frozen FA store if one exists.
4. Deposits the coin balance into the FA store via `unchecked_deposit_with_no_events` (bypasses the frozen guard).
5. Then executes the flag-sync block: [2](#0-1) 

```move
if (frozen != fungible_asset::is_frozen(store)) {
    fungible_asset::set_frozen_flag_internal(store, frozen);
}
```

`set_frozen_flag_internal` is a `public(friend)` function that directly mutates `FungibleStore.frozen` with no capability check: [3](#0-2) 

The developer comment at lines 707–711 only contemplates the **frozen CoinStore + unfrozen FA store** direction. The **unfrozen CoinStore + frozen FA store** direction — which is the exploit path — is unguarded and produces the opposite, harmful effect. [4](#0-3) 

---

### Impact Explanation

A coin issuer (or the framework) can freeze an account's primary fungible store via `fungible_asset::set_frozen_flag` using a `TransferRef`. This is a legitimate, privileged action intended to prevent the account from transferring assets (e.g., compliance, sanctions, emergency freeze).

The account owner can bypass this freeze entirely by calling `migrate_to_fungible_store` while holding any non-zero CoinStore balance with `frozen = false`. After the call:

- The FA store's `frozen` field is set to `false`.
- The account can now freely transfer assets that were supposed to be immovable.
- The coin issuer's freeze is silently and permanently overwritten.

This constitutes **unauthorized unfreezing of a frozen fungible store** — a direct bypass of an asset-control invariant, equivalent in harm to unauthorized asset reassignment.

---

### Likelihood Explanation

- The entry point is `public entry`, requiring only the account owner's signature — no special capability.
- The only precondition is `coin.value > 0` in the CoinStore, which is trivially satisfiable (the attacker simply needs any non-zero coin balance).
- The CoinStore and FA store frozen flags are managed by independent mechanisms (`FreezeCapability` vs. `TransferRef`), making the divergent-frozen state a realistic on-chain condition.
- No existing guard in `maybe_convert_to_fungible_store` checks whether the FA store was frozen by a privileged party before overwriting its flag.

---

### Recommendation

Before calling `set_frozen_flag_internal`, add a guard that refuses to **unfreeze** an FA store during migration. The migration should only ever propagate a freeze **onto** the FA store (CoinStore frozen → FA store frozen), never remove a freeze that was independently set:

```move
// Only freeze the FA store if CoinStore is frozen and FA store is not.
// Never unfreeze an FA store that was frozen independently.
if (frozen && !fungible_asset::is_frozen(store)) {
    fungible_asset::set_frozen_flag_internal(store, true);
}
```

Alternatively, skip the flag-sync entirely when the FA store is already frozen, preserving the stronger restriction.

---

### Proof of Concept

```move
#[test(framework = @aptos_framework, alice = @0xA11CE)]
fun test_migration_unfreezes_frozen_fa_store(
    framework: &signer, alice: &signer
) acquires CoinConversionMap, CoinInfo, CoinStore, PairedCoinType {
    account::create_account_for_test(signer::address_of(framework));
    account::create_account_for_test(signer::address_of(alice));
    let alice_addr = signer::address_of(alice);

    // 1. Initialize coin and give Alice a CoinStore with coins (frozen = false).
    let (burn_cap, _freeze_cap, mint_cap) =
        initialize_and_register_fake_money(framework, 1, true);
    register<FakeMoney>(alice);
    let coins = mint<FakeMoney>(100, &mint_cap);
    deposit(alice_addr, coins);
    // Alice's CoinStore: frozen = false, coin.value = 100

    // 2. Ensure the paired FA metadata exists and get the TransferRef.
    let metadata = ensure_paired_metadata<FakeMoney>();
    let metadata_addr = object::object_address(&metadata);
    let transfer_ref = &borrow_global<PairedFungibleAssetRefs>(metadata_addr)
        .transfer_ref_opt.borrow();

    // 3. Create Alice's primary FA store and freeze it via TransferRef (privileged action).
    let fa_store = primary_fungible_store::ensure_primary_store_exists(alice_addr, metadata);
    fungible_asset::set_frozen_flag(transfer_ref, fa_store, true);
    assert!(fungible_asset::is_frozen(fa_store), 0); // FA store is frozen

    // 4. Alice (unprivileged) calls migrate_to_fungible_store.
    migrate_to_fungible_store<FakeMoney>(alice);

    // 5. FA store is now UNFROZEN — freeze bypassed.
    assert!(!fungible_asset::is_frozen(fa_store), 1); // FAILS the invariant

    move_to(framework, FakeMoneyCapabilities { burn_cap, freeze_cap: _freeze_cap, mint_cap });
}
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

**File:** aptos-move/framework/aptos-framework/sources/coin.move (L724-729)
```text
    public entry fun migrate_to_fungible_store<CoinType>(
        account: &signer
    ) acquires CoinStore, CoinConversionMap, CoinInfo {
        let account_addr = signer::address_of(account);
        maybe_convert_to_fungible_store<CoinType>(account_addr);
    }
```

**File:** aptos-move/framework/aptos-framework/sources/fungible_asset.move (L1045-1051)
```text
    public(friend) fun set_frozen_flag_internal<T: key>(
        store: Object<T>, frozen: bool
    ) acquires FungibleStore {
        let store_addr = store.object_address();
        borrow_global_mut<FungibleStore>(store_addr).frozen = frozen;

        event::emit(Frozen { store: store_addr, frozen });
```
