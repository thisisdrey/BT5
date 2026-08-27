### Title
Wallet Contract's `has_in_flight_tx` flag has no recovery path if a callback fails to execute, permanently freezing the ETH-implicit account - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
The `WalletContract` (deployed on every ETH-implicit account, NEP-518) uses a single boolean state variable, `has_in_flight_tx`, as a mutex to serialize Ethereum-emulated transactions. This is structurally analogous to the Fantom `tokensTradeable` bug: a single stateful gate that must be flipped back by a specific code path (here, a callback, rather than an owner) in order for the contract to remain usable, with no fallback mechanism if that flip never happens.

### Finding Description
`rlp_execute` immediately rejects all incoming transactions whenever `self.has_in_flight_tx` is `true`: [1](#0-0) 

The flag is set to `true` right before a cross-contract promise chain is dispatched, and is only reset to `false` at the very top of one of the terminal callbacks (`address_check_callback`, `nep_141_storage_balance_callback`, `rlp_execute_callback`, `ban_relayer`): [2](#0-1) [3](#0-2) 

NEAR contract semantics only persist state mutations if the function call completes successfully; if a callback invocation fails before completing (e.g., it runs out of gas, or the underlying receipt/host call panics for any reason not already handled by the `Ok`/`Err` matching in the callback body), the `self.has_in_flight_tx = false` write is rolled back and the flag remains `true` forever. There is no owner override, timeout, or any other function in the contract that can reset `has_in_flight_tx` back to `false` — `rlp_execute`'s only exit when the flag is `true` is to always return the "transaction already in progress" error, permanently blocking all further use of the account through its only usable interface.

This mirrors the reported bug class: a single boolean whose "unlock" path is exclusively controlled by one narrow code path (there, the owner's `makeTradeable()`; here, a successful callback), with no alternate/timeout-based recovery if that path is never completed.

### Impact Explanation
If `has_in_flight_tx` gets stuck at `true`, the ETH-implicit account becomes permanently unusable: no transfers, function calls, or key management can be performed via `rlp_execute` ever again, since that is the sole entry point for ETH-implicit accounts (they cannot have access keys and cannot be deleted per `docs/DataStructures/Account.md:121`). This is a permanent freezing-of-funds condition for any balance held by that account. [4](#0-3) 

### Likelihood Explanation
This requires a callback (`address_check_callback`, `nep_141_storage_balance_callback`, or `rlp_execute_callback`) to fail before completing execution — most plausibly via out-of-gas during execution of the reserved static gas budget for a complex callback path (e.g., the multi-hop NEP-141 storage-deposit + transfer flow). I was not able to fully verify, within the available iterations, whether the static gas constants (`NEP_141_STORAGE_BALANCE_CALLBACK_GAS`, `RLP_EXECUTE_CALLBACK_GAS`, etc.) and the dynamically-added `action.gas()` are always sufficient to guarantee the callback itself cannot run out of gas under adversarial or edge-case inputs (e.g., very large `action.gas()` values requested by the user's emulated transaction, or unusual `promise_result` payload sizes). This is the key open question that determines actual exploitability.

### Recommendation
- Add a recovery mechanism (e.g., a permissionless "unstick" function callable after a timeout, similar in spirit to the report's suggested `require(msg.sender == owner || atNow() > deadline)`) that can reset `has_in_flight_tx` to `false` if no callback has resolved within a reasonable number of blocks.
- Audit and bound all gas paths reachable during a callback so that a callback invocation can never itself run out of gas after `has_in_flight_tx` has been set to `true`, and add regression tests that simulate callback failure/panic to confirm the flag does not get stuck.

### Proof of Concept
Not verified against a running node within the scope of this review — the finding is based on static code analysis of `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`. Confirming exploitability requires demonstrating a concrete scenario (e.g., a crafted `rlp_execute` call whose downstream `nep_141_storage_balance_callback` is given exactly enough gas to be scheduled but not enough to complete) that leaves `has_in_flight_tx` at `true` after the receipt fails, which I could not construct with the tools available in this session.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L93-105)
```rust
    ) -> PromiseOrValue<ExecuteResponse> {
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L116-128)
```rust
        match result {
            Ok(promise) => {
                self.has_in_flight_tx = true;
                PromiseOrValue::Promise(promise)
            }
            Err(Error::Relayer(_)) if env::signer_account_id() == current_account_id => {
                let promise = create_ban_relayer_promise(current_account_id);
                self.has_in_flight_tx = true;
                PromiseOrValue::Promise(promise)
            }
            Err(e) => PromiseOrValue::Value(e.into()),
        }
    }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L275-281)
```rust
    #[private]
    pub fn rlp_execute_callback(
        &mut self,
        caller_deposit: Option<CallerDeposit>,
    ) -> ExecuteResponse {
        self.has_in_flight_tx = false;
        let n = env::promise_results_count();
```

**File:** docs/DataStructures/Account.md (L119-122)
```markdown
Once a NEAR-implicit account is created it acts as a regular account until it's deleted.

An ETH-implicit account can only be used by calling the methods of the [Wallet Contract](#wallet-contract). It cannot be deleted, nor can a full access key be added.
The primary purpose of ETH-implicit accounts is to enable seamless integration of existing Ethereum tools (such as wallets) with the NEAR blockchain.
```
