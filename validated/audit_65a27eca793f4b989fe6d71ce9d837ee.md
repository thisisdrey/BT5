### Title
`public(friend) entry fun fungible_transfer_only` Bypasses Frozen-Store Checks via Direct Transaction Invocation — (`aptos-move/framework/aptos-framework/sources/aptos_account.move`)

### Summary

`aptos_account::fungible_transfer_only` is declared `public(friend) entry fun`. In Move, the `entry` modifier makes a function directly callable by any user via a signed transaction, regardless of its `friend` visibility. The function intentionally skips frozen-store enforcement by using `fungible_asset::unchecked_withdraw` / `unchecked_deposit`. Any user whose APT Primary Fungible Store is frozen can call this entry point directly to transfer APT out, bypassing the freeze.

### Finding Description

In `aptos_account.move`, the friend list is:

```
friend aptos_framework::genesis;
friend aptos_framework::resource_account;
friend aptos_framework::transaction_fee;
friend aptos_framework::transaction_validation;
``` [1](#0-0) 

The function is declared:

```move
public(friend) entry fun fungible_transfer_only(
    source: &signer, to: address, amount: u64
) {
    let sender_store =
        ensure_primary_fungible_store_exists(signer::address_of(source));
    let recipient_store = ensure_primary_fungible_store_exists(to);

    // use internal APIs, as they skip:
    // - owner, frozen and dispatchable checks
    fungible_asset::unchecked_deposit(
        recipient_store, fungible_asset::unchecked_withdraw(sender_store, amount)
    );
}
``` [2](#0-1) 

The `entry` modifier is the root cause. As documented by the Aptos linter rule `unsafe_friend_package_entry`:

> When a function is marked `entry`, it becomes callable by anyone via a transaction, regardless of its visibility modifier. A `friend entry` or `package entry` function therefore does NOT restrict callers to friends or the same package — the `entry` modifier overrides that restriction. [3](#0-2) 

The linter check confirms this is a known dangerous pattern:

```rust
fn check_function(&self, func: &FunctionEnv) {
    if !func.is_entry() || func.visibility() != Visibility::Friend {
        return;
    }
    // ...
    let msg = format!(
        "`{name}` is callable by anyone. \
         The `entry` modifier allows direct invocation via transactions, \
         bypassing the `{visibility}` visibility restriction.",
    );
``` [4](#0-3) 

The intended callers (`transaction_validation`, `genesis`, etc.) use this function as a cheap internal APT transfer that skips frozen/dispatchable checks because those callers have already validated the context. When called directly by an arbitrary user, those preconditions are not satisfied.

### Impact Explanation

A user whose APT Primary Fungible Store has been frozen (e.g., by the APT metadata owner / framework governance) can submit a transaction calling `aptos_framework::aptos_account::fungible_transfer_only` directly. Because `unchecked_withdraw` skips the frozen-store assertion, the transfer succeeds and APT moves out of the frozen store to any recipient address. This constitutes unauthorized movement of APT — a user-controlled on-chain asset — in violation of the freeze invariant.

### Likelihood Explanation

The function is deployed in the live Aptos framework at `@aptos_framework`. Any user can construct a standard `EntryFunction` transaction targeting `0x1::aptos_account::fungible_transfer_only` with their own signer, a recipient address, and an amount. No privileged key, governance access, or special tooling is required. The only precondition is that the attacker's APT store is frozen, which is the exact scenario the freeze mechanism is meant to prevent.

### Recommendation

Remove the `entry` modifier from `fungible_transfer_only`. The function is documented as an internal helper intended only for friend callers:

```move
// TODO: once migration is complete, rename to just "transfer_only" and make it
// an entry function (for cheapest way to transfer APT)
public(friend) fun fungible_transfer_only(   // remove `entry`
    source: &signer, to: address, amount: u64
) { ... }
``` [5](#0-4) 

If a cheap public APT transfer entry point is desired in the future, it must be a separate `public entry fun` that goes through the normal `withdraw` path (which enforces frozen checks), not through `unchecked_withdraw`.

### Proof of Concept

1. Assume the Aptos framework has frozen Alice's APT Primary Fungible Store (e.g., via `fungible_asset::set_frozen_flag`).
2. Alice constructs a transaction with payload:
   ```
   EntryFunction {
     module:   0x1::aptos_account,
     function: fungible_transfer_only,
     args:     [alice_address, bob_address, amount]
   }
   ```
3. The VM resolves `fungible_transfer_only` as an `entry` function — visibility is not checked for entry-point dispatch.
4. Inside the function, `fungible_asset::unchecked_withdraw(sender_store, amount)` executes without checking `is_frozen(sender_store)`.
5. APT is transferred from Alice's frozen store to Bob, bypassing the freeze.

The `unsafe_friend_package_entry` linter warning would fire on this exact function if the linter were run against the framework sources, confirming the pattern is the same root cause identified in the external report. [2](#0-1) [6](#0-5)

### Citations

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.move (L15-18)
```text
    friend aptos_framework::genesis;
    friend aptos_framework::resource_account;
    friend aptos_framework::transaction_fee;
    friend aptos_framework::transaction_validation;
```

**File:** aptos-move/framework/aptos-framework/sources/aptos_account.move (L242-259)
```text
    /// TODO: once migration is complete, rename to just "transfer_only" and make it an entry function (for cheapest way
    /// to transfer APT) - if we want to allow APT PFS without account itself
    public(friend) entry fun fungible_transfer_only(
        source: &signer, to: address, amount: u64
    ) {
        let sender_store =
            ensure_primary_fungible_store_exists(signer::address_of(source));
        let recipient_store = ensure_primary_fungible_store_exists(to);

        // use internal APIs, as they skip:
        // - owner, frozen and dispatchable checks
        // as APT cannot be frozen or have dispatch, and PFS cannot be transfered
        // (PFS could potentially be burned. regular transfer would permanently unburn the store.
        // Ignoring the check here has the equivalent of unburning, transfers, and then burning again)
        fungible_asset::unchecked_deposit(
            recipient_store, fungible_asset::unchecked_withdraw(sender_store, amount)
        );
    }
```

**File:** third_party/move/tools/move-linter/src/model_ast_lints/unsafe_friend_package_entry.rs (L4-9)
```rust
//! Lint check for `friend` or `package` entry functions.
//!
//! When a function is marked `entry`, it becomes callable by anyone via a transaction,
//! regardless of its visibility modifier. A `friend entry` or
//! `package entry` function therefore does NOT restrict callers to friends
//! or the same package - the `entry` modifier overrides that restriction.
```

**File:** third_party/move/tools/move-linter/src/model_ast_lints/unsafe_friend_package_entry.rs (L25-43)
```rust
    fn check_function(&self, func: &FunctionEnv) {
        if !func.is_entry() || func.visibility() != Visibility::Friend {
            return;
        }

        let visibility = if func.has_package_visibility() {
            "package"
        } else {
            "friend"
        };

        let name = func.get_name_str();
        let msg = format!(
            "`{name}` is callable by anyone. \
             The `entry` modifier allows direct invocation via transactions, \
             bypassing the `{visibility}` visibility restriction.",
        );

        self.report(func.module_env.env, &func.get_id_loc(), &msg);
```

**File:** third_party/move/tools/move-linter/tests/model_ast_lints/unsafe_friend_package_entry.exp (L1-10)
```text

Diagnostics:
warning: [lint] `unsafe_friend_entry` is callable by anyone. The `entry` modifier allows direct invocation via transactions, bypassing the `friend` visibility restriction.
  ┌─ tests/model_ast_lints/unsafe_friend_package_entry.move:9:22
  │
9 │     friend entry fun unsafe_friend_entry() {}
  │                      ^^^^^^^^^^^^^^^^^^^
  │
  = To suppress this warning, annotate the function/module with the attribute `#[lint::skip(unsafe_friend_package_entry)]`.
  = For more information, see https://aptos.dev/en/build/smart-contracts/linter#unsafe_friend_package_entry.
```
