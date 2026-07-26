### Title
Immutable `DispatchFunctionStore` FunctionInfo References Allow Permanent Freezing of Fungible Asset Balances via Compatible Module Upgrade — (File: `aptos-move/framework/aptos-framework/sources/fungible_asset.move`)

---

### Summary

The `DispatchFunctionStore` resource in `fungible_asset.move` stores `FunctionInfo` hook references (module address + module name + function name) immutably at FA creation time. No update path exists after registration. A token issuer who published their hook module under a `compatible` upgrade policy can perform a compatible upgrade that changes the hook's runtime behavior (e.g., unconditional `abort`) while preserving the required signature. Because `fungible_asset::withdraw` explicitly aborts when a dispatch function is registered, and `dispatchable_fungible_asset::withdraw` delegates entirely to the stored hook, all user balances in the affected FA become permanently unclaimable with no framework-provided recovery path.

---

### Finding Description

**Root cause — immutable function pointer stored at FA creation time:**

`register_dispatch_functions` writes a `DispatchFunctionStore` to the metadata object address exactly once:

```move
// fungible_asset.move
assert!(
    !exists<DispatchFunctionStore>(
        constructor_ref.address_from_constructor_ref()
    ),
    error::already_exists(EALREADY_REGISTERED)   // EALREADY_REGISTERED = 29
);
let store_obj = &constructor_ref.generate_signer();
move_to<DispatchFunctionStore>(
    store_obj,
    DispatchFunctionStore {
        withdraw_function,
        deposit_function,
        derived_balance_function
    }
);
``` [1](#0-0) 

The stored `FunctionInfo` is:

```move
struct FunctionInfo has copy, drop, store {
    module_address: address,
    module_name: String,
    function_name: String,
}
``` [2](#0-1) 

There is no `update_dispatch_functions` entry point anywhere in the framework. The `EALREADY_REGISTERED` guard makes re-registration impossible.

**Dispatch path — hook is called unconditionally:**

`dispatchable_fungible_asset::withdraw` reads the stored `FunctionInfo` and dispatches to it:

```move
let func_opt = fungible_asset::withdraw_dispatch_function(store);
if (func_opt.is_some()) {
    let func = func_opt.borrow();
    if (features::is_function_value_dispatch_enabled()) {
        dispatch_withdraw_hook(store, amount, borrow_transfer_ref(store), func)
    } else {
        function_info::load_module_from_function(func);
        dispatchable_withdraw(store, amount, borrow_transfer_ref(store), func)
    }
}
``` [3](#0-2) 

**Non-dispatchable path is blocked for dispatchable FAs:**

`fungible_asset::withdraw` (the non-dispatchable variant) calls `withdraw_sanity_check` with `abort_on_dispatch = true`, which aborts if a dispatch function is registered:

```move
assert!(
    !abort_on_dispatch || !has_withdraw_dispatch_function(fa_store.metadata),
    error::invalid_argument(EINVALID_DISPATCHABLE_OPERATIONS)
);
``` [4](#0-3) 

This means once a dispatch hook is registered, the only withdrawal path goes through the hook. If the hook aborts, the balance is permanently frozen.

**Compatible upgrade can change hook behavior:**

The `compatible` upgrade policy preserves public function signatures but places no constraint on function bodies. A hook module published as `compatible` can be upgraded to unconditionally abort while keeping the same parameter and return types — satisfying the compatibility checker. The stored `FunctionInfo` still resolves to a valid function (same address, name, signature), but every call to it aborts. [5](#0-4) 

**`withdraw_with_ref` is not available to users:**

The only bypass is `fungible_asset::withdraw_with_ref`, which requires a `TransferRef`. The `TransferRef` is held exclusively by the token issuer inside `TransferRefStore` at the metadata address — users have no access to it. [6](#0-5) 

---

### Impact Explanation

All user balances in the affected dispatchable FA become permanently unclaimable. Every call to `dispatchable_fungible_asset::withdraw` (and by extension `primary_fungible_store::withdraw`, `transfer`, etc.) will abort. The non-dispatchable `fungible_asset::withdraw` is blocked by `EINVALID_DISPATCHABLE_OPERATIONS`. No framework-provided escape hatch exists. This constitutes **permanent freezing of fungible asset balances** — explicitly listed as in-scope impact.

---

### Likelihood Explanation

Requires the token issuer to perform a compatible module upgrade after users have deposited funds. The issuer is not a framework admin; any account that published a `compatible`-policy module can do this unilaterally. Users depositing into a dispatchable FA have no on-chain guarantee that the hook module will not be upgraded. The risk is invisible at deposit time.

---

### Recommendation

**Short term:** Add an emergency withdrawal function that allows users to reclaim their raw balance via `unchecked_withdraw` (or a `TransferRef`-gated path) when the dispatch hook is provably broken (e.g., reverts on a test call), bypassing the hook entirely.

**Short term:** Require that modules registered as dispatch hooks be published with `upgrade_policy = "immutable"`, enforced at `register_dispatch_functions` time by checking the on-chain `PackageRegistry` upgrade policy of the target module address.

**Long term:** Design and document a migration process for dispatchable FA holders analogous to the coin-to-FA migration path, so that if a hook module is upgraded in a breaking way, balances can be recovered or migrated to a new FA.

---

### Proof of Concept

```
Step 1 — Bob deploys a dispatchable FA module (upgrade_policy = "compatible"):

  module bob::hook_token {
      public fun withdraw<T: key>(
          store: Object<T>, amount: u64, transfer_ref: &TransferRef
      ): FungibleAsset {
          transfer_ref.withdraw_with_ref(store, amount)   // works normally
      }
  }

Step 2 — Bob creates the FA and registers the hook:

  dispatchable_fungible_asset::register_dispatch_functions(
      constructor_ref,
      option::some(function_info::new_function_info(bob, b"hook_token", b"withdraw")),
      option::none(),
      option::none(),
  );
  // DispatchFunctionStore { withdraw_function: Some(bob::hook_token::withdraw) }
  // stored immutably at metadata object address — EALREADY_REGISTERED prevents update

Step 3 — Alice deposits 1000 tokens into the FA.

Step 4 — Bob performs a compatible upgrade (same signature, body changed):

  module bob::hook_token {
      public fun withdraw<T: key>(
          store: Object<T>, amount: u64, transfer_ref: &TransferRef
      ): FungibleAsset {
          abort 0   // permanently blocks all withdrawals
      }
  }

Step 5 — Alice calls dispatchable_fungible_asset::withdraw(alice, alice_store, 1000):
  → reads DispatchFunctionStore.withdraw_function = bob::hook_token::withdraw
  → dispatches to bob::hook_token::withdraw → abort 0
  → transaction aborts; Alice's 1000 tokens remain locked

Step 6 — Alice tries fungible_asset::withdraw(alice, alice_store, 1000):
  → withdraw_sanity_check: abort_on_dispatch=true, has_withdraw_dispatch_function=true
  → aborts with EINVALID_DISPATCHABLE_OPERATIONS (error code 28)

Step 7 — No other framework path exists. Alice's balance is permanently frozen.
```

The exact corrupted state: `FungibleStore.balance` at Alice's store address retains its non-zero value but is permanently inaccessible because `DispatchFunctionStore.withdraw_function` points to a hook that unconditionally aborts, and no framework function can bypass it without the issuer's `TransferRef`. [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/fungible_asset.move (L156-161)
```text
    #[resource_group_member(group = aptos_framework::object::ObjectGroup)]
    struct DispatchFunctionStore has key {
        withdraw_function: Option<FunctionInfo>,
        deposit_function: Option<FunctionInfo>,
        derived_balance_function: Option<FunctionInfo>
    }
```

**File:** aptos-move/framework/aptos-framework/sources/fungible_asset.move (L370-448)
```text
    public(friend) fun register_dispatch_functions(
        constructor_ref: &ConstructorRef,
        withdraw_function: Option<FunctionInfo>,
        deposit_function: Option<FunctionInfo>,
        derived_balance_function: Option<FunctionInfo>
    ) {
        // Verify that caller type matches callee type so wrongly typed function cannot be registered.
        withdraw_function.for_each_ref(|withdraw_function| {
                let dispatcher_withdraw_function_info =
                    function_info::new_function_info_from_address(
                        @aptos_framework,
                        string::utf8(b"dispatchable_fungible_asset"),
                        string::utf8(b"dispatchable_withdraw")
                    );

                assert!(
                    function_info::check_dispatch_type_compatibility(
                        &dispatcher_withdraw_function_info,
                        withdraw_function
                    ),
                    error::invalid_argument(EWITHDRAW_FUNCTION_SIGNATURE_MISMATCH)
                );
            });

        deposit_function.for_each_ref(|deposit_function| {
                let dispatcher_deposit_function_info =
                    function_info::new_function_info_from_address(
                        @aptos_framework,
                        string::utf8(b"dispatchable_fungible_asset"),
                        string::utf8(b"dispatchable_deposit")
                    );
                // Verify that caller type matches callee type so wrongly typed function cannot be registered.
                assert!(
                    function_info::check_dispatch_type_compatibility(
                        &dispatcher_deposit_function_info,
                        deposit_function
                    ),
                    error::invalid_argument(EDEPOSIT_FUNCTION_SIGNATURE_MISMATCH)
                );
            });

        derived_balance_function.for_each_ref(|balance_function| {
                let dispatcher_derived_balance_function_info =
                    function_info::new_function_info_from_address(
                        @aptos_framework,
                        string::utf8(b"dispatchable_fungible_asset"),
                        string::utf8(b"dispatchable_derived_balance")
                    );
                // Verify that caller type matches callee type so wrongly typed function cannot be registered.
                assert!(
                    function_info::check_dispatch_type_compatibility(
                        &dispatcher_derived_balance_function_info,
                        balance_function
                    ),
                    error::invalid_argument(
                        EDERIVED_BALANCE_FUNCTION_SIGNATURE_MISMATCH
                    )
                );
            });
        register_dispatch_function_sanity_check(constructor_ref);
        assert!(
            !exists<DispatchFunctionStore>(
                constructor_ref.address_from_constructor_ref()
            ),
            error::already_exists(EALREADY_REGISTERED)
        );

        let store_obj = &constructor_ref.generate_signer();

        // Store the overload function hook.
        move_to<DispatchFunctionStore>(
            store_obj,
            DispatchFunctionStore {
                withdraw_function,
                deposit_function,
                derived_balance_function
            }
        );
    }
```

**File:** aptos-move/framework/aptos-framework/sources/fungible_asset.move (L982-985)
```text
        assert!(
            !abort_on_dispatch || !has_withdraw_dispatch_function(fa_store.metadata),
            error::invalid_argument(EINVALID_DISPATCHABLE_OPERATIONS)
        );
```

**File:** aptos-move/framework/aptos-framework/sources/function_info.move (L17-21)
```text
    struct FunctionInfo has copy, drop, store {
        module_address: address,
        module_name: String,
        function_name: String,
    }
```

**File:** aptos-move/framework/aptos-framework/sources/dispatchable_fungible_asset.move (L72-119)
```text
    public fun withdraw<T: key>(
        owner: &signer,
        store: Object<T>,
        amount: u64,
    ): FungibleAsset acquires TransferRefStore {
        fungible_asset::withdraw_sanity_check(owner, store, false);
        let func_opt = fungible_asset::withdraw_dispatch_function(store);
        if (func_opt.is_some()) {
            let func = func_opt.borrow();
            if (features::is_function_value_dispatch_enabled()) {
                dispatch_withdraw_hook(store, amount, borrow_transfer_ref(store), func)
            } else {
                function_info::load_module_from_function(func);
                dispatchable_withdraw(
                    store,
                    amount,
                    borrow_transfer_ref(store),
                    func,
                )
            }
        } else {
            fungible_asset::unchecked_withdraw(store.object_address(), amount)
        }
    }

    /// Deposit `amount` of the fungible asset to `store`.
    ///
    /// The semantics of deposit will be governed by the function specified in DispatchFunctionStore.
    public fun deposit<T: key>(store: Object<T>, fa: FungibleAsset) acquires TransferRefStore {
        fungible_asset::deposit_sanity_check(store, false);
        let func_opt = fungible_asset::deposit_dispatch_function(store);
        if (func_opt.is_some()) {
            let func = func_opt.borrow();
            if (features::is_function_value_dispatch_enabled()) {
                dispatch_deposit_hook(store, fa, borrow_transfer_ref(store), func)
            } else {
                function_info::load_module_from_function(func);
                dispatchable_deposit(
                    store,
                    fa,
                    borrow_transfer_ref(store),
                    func
                )
            }
        } else {
            fungible_asset::unchecked_deposit(store.object_address(), fa)
        }
    }
```

**File:** aptos-move/framework/aptos-framework/sources/dispatchable_fungible_asset.move (L206-213)
```text
    inline fun borrow_transfer_ref<T: key>(metadata: Object<T>): &TransferRef {
        let metadata_addr = fungible_asset::store_metadata(metadata).object_address();
        assert!(
            exists<TransferRefStore>(metadata_addr),
            error::not_found(ESTORE_NOT_FOUND)
        );
        &borrow_global<TransferRefStore>(metadata_addr).transfer_ref
    }
```

**File:** third_party/move/documentation/book/src/modules-and-packages.md (L601-607)
```markdown
- `compatible`: these upgrades must be backwards compatible, specifically:
  - For storage, all old struct declarations must be the same in
    the new code. This ensures that the existing state of storage is
    correctly interpreted by the new code. However, new struct declarations
    can be added.
  - For APIs, all existing public functions must have the same signature as
    before. New functions, including public and entry functions, can be added.
```
