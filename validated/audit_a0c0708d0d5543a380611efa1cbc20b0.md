### Title
ETH-implicit Wallet Contract self-ban logic can permanently freeze the account via a stuck `has_in_flight_tx` flag - (File: runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs)

### Summary
Every ETH-implicit account is bound at creation to a single, fixed global-contract deployment of the Wallet Contract, and every mutating entry point is gated by a single in-memory boolean, `has_in_flight_tx`, that must be reset by a specific callback in order for the account to ever accept another `rlp_execute` call again. There is no privileged/alternate path (no full access key can ever be added to an ETH-implicit account, and it cannot be deleted) to reset this flag if the reset-clearing callback never runs. This mirrors the Vader bug class: a single hard-wired gate/whitelist with no alternate or governance-controlled path to recovery, so once the gate is stuck, all funds already sent to the account are permanently unreachable.

### Finding Description
`WalletContract::rlp_execute` refuses to process any new Ethereum-wrapped transaction whenever `self.has_in_flight_tx` is `true`: [1](#0-0) 

The flag is only ever cleared inside the various private callbacks (`address_check_callback`, `nep_141_storage_balance_callback`, `rlp_execute_callback`, `ban_relayer`), all of which are invoked as continuations of the promise created in `inner_rlp_execute`/`rlp_execute`.

When `rlp_execute` detects a faulty relayer (`Err(Error::Relayer(_))`) and the relayer is using an access key that lives directly on the wallet account itself, it builds a single promise batch that both revokes the relayer's key and clears the flag: [2](#0-1) 

`delete_key(pk)` and `function_call_weight("ban_relayer", ...)` are appended to the *same* `Promise`/action receipt rather than chained with `.then()`, i.e. they execute as two actions of one `ActionReceipt`. In NEAR's action-execution model, if an earlier action in a receipt fails (e.g. `DeleteKey` on a key that no longer exists), the receipt fails and later actions in that same receipt are not executed. Consequently, if the specific public key returned by `env::signer_account_pk()` has already been removed from the account by the time this receipt runs (for example because the true account owner legitimately revoked that same relayer key with an earlier, independently-processed `rlp_execute`/`DeleteKey` call, or the receipt was delayed/postponed due to congestion and a race occurs), the `DeleteKey` action fails and the trailing `ban_relayer` call — the only code path that resets `has_in_flight_tx` back to `false` in this branch — never runs.

Once `has_in_flight_tx` is left permanently `true`, every subsequent call to `rlp_execute` for that account is unconditionally rejected at the very first check, forever. Because ETH-implicit accounts:
- cannot have a full-access key added (`AddFullAccessKey` is explicitly rejected, and protocol rules bar it for this account type),
- cannot be deleted, and
- have no other privileged entry point to reset contract state,

there is no recovery path, analogous to how `VaderReserve` hard-wires a single authorized router with no governance path to add/replace it once liquidity is deployed against the "wrong" router.

### Impact Explanation
Any $NEAR balance or NEP-141/registered assets tied to that ETH-implicit account become permanently unusable, because the account's only method of egress (`rlp_execute`) is gated by a flag that can never be cleared again. This is a permanent freezing of funds for the affected account, matching the "permanent freezing of funds" acceptance criterion, and it is reachable purely from unprivileged, ordinary client activity (an owner revoking a relayer key combined with a relayer's faulty/self-signed transaction being processed later) — no validator, network, or operator privilege is required.

### Likelihood Explanation
The precondition requires a specific race: the relayer-held access key used to sign a transaction must be deleted (by a legitimate, independent action from the owner, or another concurrent relayer-ban) between the time the faulty transaction is accepted into the chain and the time its receipt is actually applied. This can occur under normal operation because `rlp_execute` explicitly supports multiple relayers holding independent `FunctionCall` access keys on the same wallet, and receipts to a busy account can be delayed by congestion/postponement, widening the race window. This is a moderate-likelihood, protocol-logic race rather than a trivially-always-triggerable bug, but it does not require any malicious infrastructure component — only ordinary relayer/owner transaction sequencing.

### Recommendation
- Do not couple the relayer-key deletion with the `has_in_flight_tx` reset in a single fallible receipt. Either:
  - Reset `has_in_flight_tx = false` unconditionally as the very first (or only) action's local state change before dispatching the `delete_key`/`ban_relayer` promise (i.e., clear the flag synchronously in `rlp_execute` before returning the promise, since the ban itself does not depend on it being `true`), or
  - Chain `ban_relayer` via `.then()` instead of batching it after `delete_key`, so it always executes regardless of whether `delete_key` succeeds, and have `ban_relayer` unconditionally clear `has_in_flight_tx` (as it already does) even when it receives a `PromiseResult::Failed` from the preceding `delete_key`.
- Add a private, callable "self-recovery" method (invocable only via a still-valid signed Ethereum transaction from the true owner) that force-resets `has_in_flight_tx` after some inactivity, as a defense-in-depth backstop against any other, currently-undiscovered path that could leave the flag stuck.

### Proof of Concept
1. Owner creates an ETH-implicit account and grants `FunctionCall` access key `K` to `relayer1` via `AddKey` (as in `runtime/near-wallet-contract/implementation/wallet-contract/src/tests/relayer.rs:26`, `test_register_relayer`).
2. `relayer1` submits a transaction using `K` that will be judged "faulty" by `validate_tx_relayer_data` (e.g. wrong nonce/target — see `runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs:318-368`), but the receipt is delayed (e.g. congestion, cross-shard postponement).
3. Before that delayed receipt executes, the owner independently sends a valid, correctly-signed `DeleteKey` action (through a separate, successful `rlp_execute` call) removing key `K` from the account (legitimate revocation).
4. The delayed faulty-relayer receipt from step 2 finally executes: `rlp_execute` detects `Err(Error::Relayer(_))` with `signer_account_id() == current_account_id`, and dispatches `create_ban_relayer_promise` which tries `delete_key(K)` followed by `ban_relayer`. Because `K` no longer exists, `DeleteKey` fails with `DeleteKeyDoesNotExist`, the receipt fails, and `ban_relayer` (which clears `has_in_flight_tx`) never executes.
5. `has_in_flight_tx` remains `true` on the account forever; every future `rlp_execute` call returns `"Error: transaction already in progress, please try again later."` (`runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs:97-105`), permanently freezing any funds held by that ETH-implicit account.

Note: Verifying the exact NEAR receipt-execution semantics for early termination on action failure within a single receipt (i.e., confirming that a failed `DeleteKey` action prevents a subsequent `FunctionCall` action in the same receipt from executing) required inspecting `runtime/runtime/src/actions.rs`/`lib.rs` action-application loop, which the available index snippets did not fully expose in this session. This detail should be double-checked directly in the repository (e.g., via a full Devin session) before treating this as fully confirmed, though the pattern (`Promise::new(...).delete_key(pk).function_call_weight(...)` compiling to one multi-action receipt with sequential, halt-on-failure semantics) is consistent with documented NEAR receipt execution behavior.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L94-105)
```rust
        // To ensure user actions are executed in the desired order,
        // having multiple transactions in flight at the same time is
        // not allowed.
        if self.has_in_flight_tx {
            return PromiseOrValue::Value(ExecuteResponse {
                success: false,
                success_value: None,
                error: Some(
                    "Error: transaction already in progress, please try again later.".into(),
                ),
            });
        }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L503-512)
```rust
fn create_ban_relayer_promise(current_account_id: AccountId) -> Promise {
    let pk = env::signer_account_pk();
    Promise::new(current_account_id).delete_key(pk).function_call_weight(
        "ban_relayer".into(),
        Vec::new(),
        NearToken::from_yoctonear(0),
        Gas::from_tgas(1),
        GasWeight(1),
    )
}
```
