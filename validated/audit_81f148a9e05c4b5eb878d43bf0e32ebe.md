### Title
Wallet Contract `has_in_flight_tx` can be permanently stuck at `true` if a mid-chain callback receipt fails, freezing the ETH-implicit account forever - ([File: runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs])

### Summary
The `WalletContract` used to power ETH-implicit accounts guards against concurrent transactions with a `has_in_flight_tx` flag: `rlp_execute` refuses to start a new transaction while the flag is `true`, and the flag is only reset to `false` inside one of the private callback methods (`address_check_callback`, `nep_141_storage_balance_callback`, `rlp_execute_callback`, `ban_relayer`) that run as chained cross-contract-call receipts [1](#0-0) . This mirrors the reported `Crowdsale.finalize()` pattern: a state transition ("finalizing"/"unlocking") is documented as always happening but is in fact gated behind a step (an external agent / a callback) that can fail to occur, leaving the contract permanently stuck in the "in-progress" state.

### Finding Description
`rlp_execute` sets `self.has_in_flight_tx = true` only after successfully building the outer promise, and that assignment is committed to state as part of that receipt's successful execution [2](#0-1) . The flag is reset back to `false` as the very first statement of each downstream callback (`address_check_callback`, `nep_141_storage_balance_callback`, `rlp_execute_callback`) [3](#0-2) [4](#0-3) [5](#0-4) .

Per the runtime's execution model, a receipt's state mutations are only durably committed if the receipt succeeds; if it fails for any reason (guest panic, `Exceeded the prepaid gas`, or any other `FunctionCallError`), the entire receipt's `TrieUpdate` is rolled back, discarding all in-memory mutations made during that receipt, including ones made before the point of failure: [6](#0-5) . This "failed receipt atomicity" behavior is explicitly documented: *"a receipt whose result is `Err` triggers `state_update.rollback()`, so no state changes persist except the outcome/gas accounting."*

Consequently: the `has_in_flight_tx = true` write from the successful `rlp_execute` receipt is durably committed on-chain. Any later callback receipt in the promise chain (`address_check_callback`, `nep_141_storage_balance_callback`, or `rlp_execute_callback`) that subsequently fails - e.g., by exceeding its statically attached gas budget (`RLP_EXECUTE_CALLBACK_GAS`/`ADDRESS_CHECK_CALLBACK_GAS`/`NEP_141_STORAGE_BALANCE_CALLBACK_GAS`, each fixed at only a few Tgas: 5-15 Tgas [7](#0-6) ), or panics for any other reason - has its own `self.has_in_flight_tx = false` write rolled back along with everything else in that receipt. The flag remains `true` forever.

Because `rlp_execute` unconditionally refuses to start a new transaction while `has_in_flight_tx` is `true` [8](#0-7) , and the flag can only be reset via that same callback chain, the account is bricked: no further `rlp_execute` calls can ever create a new resetting promise. There is no admin recovery path — ETH-implicit accounts cannot have a full-access key added and cannot be deleted, per protocol design [9](#0-8) , so all funds and functionality of the account become permanently inaccessible.

The existing test `test_insufficient_gas` only validates the case where `rlp_execute` itself fails on the *first* receipt before the flag transition is committed (in that case the whole receipt including the `has_in_flight_tx = true` assignment rolls back cleanly, leaving the contract usable) [10](#0-9) . It does not cover the scenario where the *initial* receipt succeeds (committing `has_in_flight_tx = true`) and a *subsequent* callback in the chain fails, which is the scenario that leads to permanent lockup.

### Impact Explanation
If reachable, this results in permanent freezing of funds: the entire balance of the ETH-implicit account (and any tokens/state reachable only through it) becomes permanently inaccessible, since `rlp_execute` is the sole entry point for any action from that account and it is now unconditionally rejected. This matches the "permanent freezing of funds" impact bar.

### Likelihood Explanation
Reaching a callback-stage failure after the flag has already been committed as `true` requires triggering an on-chain execution error inside one of the fixed, small statically-allocated gas budgets (`RLP_EXECUTE_CALLBACK_GAS = 5 Tgas`, `ADDRESS_CHECK_CALLBACK_GAS`, `NEP_141_STORAGE_BALANCE_CALLBACK_GAS`) for the callback itself, distinct from the `action.gas()` budget forwarded for the user's target call. This is plausible for `ERC20Transfer`/`EOABaseTokenTransfer` paths that chain through an external registrar or NEP-141 contract before the reset happens, or via any target contract returning an unusually large/expensive-to-process successful value, or any other transient host error in a chained receipt. I was not able to fully verify a concrete, deterministic minimal repro (e.g., the exact minimum-size return value needed to overrun `RLP_EXECUTE_CALLBACK_GAS`) within the available exploration budget — this would require deeper gas-accounting analysis or on-chain experimentation, which a background agent with full repo/tooling access could pursue.

### Recommendation
- Do not rely on always-successful callback completion to reset `has_in_flight_tx`. Instead, either (a) provide an unprivileged recovery path (e.g., a timeout-based self-reset, analogous to yield/resume timeouts) that clears `has_in_flight_tx` if no callback arrives within a bounded number of blocks, or (b) ensure sufficient/refundable gas is reserved specifically for the final flag-reset step so that failures in the "payload" portion of the callback cannot prevent the flag reset from being committed (e.g. by isolating the reset into a minimal, guaranteed-to-succeed final callback with a generous, non-attacker-influenced gas budget).
- Add integration tests that specifically fail a mid-chain callback (e.g., by making the target of `rlp_execute` an account that causes `rlp_execute_callback` to exceed its gas budget) and assert the wallet contract remains usable afterward, mirroring the existing `test_insufficient_gas` test but for the post-flag-set case.

### Proof of Concept
Conceptual (not fully verified end-to-end due to tool limitations):
1. Deploy the Wallet Contract to an ETH-implicit account and fund it.
2. Submit an RLP-encoded Ethereum transaction via `rlp_execute` whose target/action causes a chained receipt (`rlp_execute_callback`, `address_check_callback`, or `nep_141_storage_balance_callback`) to run out of its fixed static gas budget or otherwise fail — for example, by targeting a contract/method whose successful return value is large enough that copying/serializing it inside `rlp_execute_callback` (budget `RLP_EXECUTE_CALLBACK_GAS = 5 Tgas`) exceeds the prepaid gas for that receipt.
3. Observe that the outer `rlp_execute` receipt succeeds and commits `has_in_flight_tx = true`, while the downstream callback receipt fails and is rolled back (its `has_in_flight_tx = false` write never persists).
4. Any subsequent call to `rlp_execute` on this account now always returns `"Error: transaction already in progress, please try again later."` [11](#0-10) , permanently, with no way to reset the account.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L34-41)
```rust
const NEP_141_STORAGE_DEPOSIT_GAS: Gas = Gas::from_tgas(5);
const NEP_141_STORAGE_BALANCE_OF_GAS: Gas = Gas::from_tgas(5);
const REGISTRAR_LOOKUP_GAS: Gas = Gas::from_tgas(5);
const RLP_EXECUTE_CALLBACK_GAS: Gas = Gas::from_tgas(5);
const ADDRESS_CHECK_CALLBACK_GAS: Gas = Gas::from_tgas(5).saturating_add(RLP_EXECUTE_CALLBACK_GAS);
const NEP_141_STORAGE_BALANCE_CALLBACK_GAS: Gas = Gas::from_tgas(5)
    .saturating_add(NEP_141_STORAGE_DEPOSIT_GAS)
    .saturating_add(RLP_EXECUTE_CALLBACK_GAS);
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L46-55)
```rust
pub struct WalletContract {
    pub nonce: u64,
    /// Tracks whether a transaction is currently being executed
    /// (i.e. has receipts that have not yet resolved).
    /// Invariant: `has_in_flight_tx` must be `true` when a mutable method
    /// of this contract returns a promise and `false` otherwise (except
    /// for the check if a transaction is already in flight at the beginning
    /// of `rlp_execute`).
    pub has_in_flight_tx: bool,
}
```

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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L106-128)
```rust
        let current_account_id = env::current_account_id();
        let predecessor_account_id = env::predecessor_account_id();
        let result = inner_rlp_execute(
            current_account_id.clone(),
            predecessor_account_id,
            target,
            tx_bytes_b64,
            &mut self.nonce,
        );

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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L134-140)
```rust
    pub fn address_check_callback(
        &mut self,
        target: AccountId,
        action: near_action::Action,
        caller_deposit: Option<CallerDeposit>,
    ) -> PromiseOrValue<ExecuteResponse> {
        self.has_in_flight_tx = false;
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L194-202)
```rust
    #[private]
    pub fn nep_141_storage_balance_callback(
        &mut self,
        token_id: AccountId,
        receiver_id: AccountId,
        action: near_action::Action,
        caller_deposit: Option<CallerDeposit>,
    ) -> PromiseOrValue<ExecuteResponse> {
        self.has_in_flight_tx = false;
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L276-280)
```rust
    pub fn rlp_execute_callback(
        &mut self,
        caller_deposit: Option<CallerDeposit>,
    ) -> ExecuteResponse {
        self.has_in_flight_tx = false;
```

**File:** runtime/runtime/src/lib.rs (L1024-1034)
```rust
        // Committing or rolling back state.
        match &result.result {
            Ok(_) => {
                state_update.commit(StateChangeCause::ReceiptProcessing {
                    receipt_hash: receipt.get_hash(),
                });
            }
            Err(_) => {
                state_update.rollback();
            }
        };
```

**File:** docs/DataStructures/Account.md (L121-122)
```markdown
An ETH-implicit account can only be used by calling the methods of the [Wallet Contract](#wallet-contract). It cannot be deleted, nor can a full access key be added.
The primary purpose of ETH-implicit accounts is to enable seamless integration of existing Ethereum tools (such as wallets) with the NEAR blockchain.
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/tests/sanity.rs (L34-77)
```rust
#[tokio::test]
async fn test_insufficient_gas() -> anyhow::Result<()> {
    let TestContext { worker, wallet_contract, wallet_sk, .. } = TestContext::new().await?;

    // If not enough gas is attached to the `rlp_execute` call then the action fails.
    let target = "some.account.near".to_string();
    let action = Action::FunctionCall {
        receiver_id: target.clone(),
        method_name: "greet".into(),
        args: br#"{"name": "Aurora"}"#.to_vec(),
        gas: 5_000_000_000_000,
        yocto_near: 0,
    };
    let signed_transaction = utils::create_signed_transaction(
        0,
        &target.parse().unwrap(),
        Wei::zero(),
        action,
        &wallet_sk,
    );

    let error = wallet_contract
        .inner
        .call(crate::tests::RLP_EXECUTE)
        .args_json(serde_json::json!({
            "target": target,
            "tx_bytes_b64": codec::encode_b64(&codec::rlp_encode(&signed_transaction))
        }))
        .gas(near_gas::NearGas::from_tgas(7))
        .transact()
        .await
        .unwrap()
        .raw_bytes()
        .unwrap_err();

    assert!(
        error.to_string().contains("Exceeded the prepaid gas."),
        "Error should be that there was not enough gas"
    );

    // But the contract is still usable afterwards.
    utils::deploy_and_call_hello(&worker, &wallet_contract, &wallet_sk, 0).await?;

    Ok(())
```
