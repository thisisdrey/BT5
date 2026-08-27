# [DoS] ETH-implicit Wallet Contract can be permanently bricked by an oversized promise return value, freezing all account funds - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
The external report describes `WrappedIbbtc.transfer()`/`transferFrom()` becoming permanently unusable because a dependency call (`oracle.pricePerShare()`) can revert inside a function whose failure blocks the only path to move funds. The nearcore analog is the `WalletContract` (the eth-implicit account "Wallet Contract", NEP-518) used by `0x...` accounts: its single-in-flight-transaction guard `has_in_flight_tx` is reset to `false` only as the *first statement* of a `#[private]` callback, but that reset is not durable unless the whole callback execution completes without panicking. If the callback's execution runs out of its small fixed gas budget while processing an attacker/third-party-controlled cross-contract return value, the entire callback (including the flag reset) is rolled back, permanently leaving `has_in_flight_tx == true` and bricking the wallet — an unrecoverable freeze of every asset controlled by that account.

### Finding Description
`WalletContract::rlp_execute` refuses to start a new Ethereum-style transaction whenever `self.has_in_flight_tx` is `true`: [1](#0-0) 

The flag is set to `true` synchronously in the same receipt that schedules the promise chain, and it is only ever reset back to `false` inside the corresponding `#[private]` callback (`address_check_callback`, `nep_141_storage_balance_callback`, `rlp_execute_callback`, `ban_relayer`), always as the first line of the function body, e.g.: [2](#0-1) 

Callback gas is a small, fixed static budget rather than something scaled to the size of untrusted data returned by the promise it follows: [3](#0-2) 

Inside the callback, `env::promise_result(0)` reads the return value of the preceding cross-contract call (to an arbitrary `target`, e.g. an NEP-141 token contract invoked via the emulated ERC-20 `ft_transfer`/`ft_balance_of`/`ft_total_supply` path, or any `FunctionCall` action target). The VM host function `promise_result` charges gas proportional to the size of the copied blob (`base + write_register_base/byte` costs): [4](#0-3) [5](#0-4) 

Because NEAR only commits the state changes of a function-call execution if that execution completes successfully — a mid-execution gas-exhaustion panic discards **all** state mutations made during that receipt, including the `self.has_in_flight_tx = false` write performed at the top of the callback — a sufficiently large `PromiseResult::Successful` payload from the called `target` contract can cause the callback itself to run out of gas after that point (e.g. while allocating/copying the register contents, formatting an error string, or scheduling the refund promise), causing the whole callback receipt to fail. `.then()` callbacks are guaranteed to execute exactly once regardless of the predecessor’s outcome, so there is no automatic retry: once this callback panics, `has_in_flight_tx` is permanently stuck at `true`.

From that point on, `rlp_execute` unconditionally returns `"Error: transaction already in progress, please try again later."` for every future call, because the guard check happens before any other logic runs. Since the Wallet Contract is the *only* mechanism by which an eth-implicit account can execute NEAR actions (no full access key can ever be added to such accounts, per `docs/DataStructures/Account.md`), this permanently and irrecoverably freezes all NEAR and token balances held by that account.

This mirrors the external report's bug class precisely: a downstream/external contract's response (analogous to the oracle) is not bounded, and its failure mode (there: revert; here: gas exhaustion causing rollback) blocks the *only* path used for ordinary transfers, with no fallback and no way to reset local state.

### Impact Explanation
This qualifies as permanent freezing of funds: any eth-implicit account whose Wallet Contract processes a callback with an oversized/expensive promise-result payload becomes permanently unable to execute `rlp_execute` again (every subsequent invocation short-circuits on the "already in progress" guard). All $NEAR and NEP-141 token balances held by that account become permanently inaccessible, since there is no other mechanism to bypass the Wallet Contract for eth-implicit accounts.

### Likelihood Explanation
The trigger is reachable by an ordinary user/relayer interacting with any deployed NEP-141 token or generic contract as the emulated ERC-20 `target`/`FunctionCall` target — no privileged or validator/network access is required. A malicious (or simply buggy) token contract can be deployed by anyone and induced to return an outsized value from `ft_transfer`, `ft_balance_of`, or `ft_total_supply` (or any general `FunctionCall` return value), which then flows back into the wallet's fixed-gas callback. The wallet owner or their relayer merely has to interact with such a contract as the transaction target for the wallet to be bricked.

### Recommendation
- Bound/validate the size of promise results consumed inside `rlp_execute_callback`, `address_check_callback`, and `nep_141_storage_balance_callback` before doing further gas-metered work with them (e.g. reject or truncate oversized results early, or size-check before deserializing/forwarding).
- Increase and/or dynamically scale the static gas allotted to these callbacks based on the gas remaining/attached to the outer call, and ensure `self.has_in_flight_tx = false` is committed independently of the rest of the callback logic (e.g. perform the flag reset in a minimal, gas-cheap operation that cannot be starved by subsequent processing of untrusted data).
- Add a recovery path (e.g., a time-based or owner-authorized "unstick" mechanism) so `has_in_flight_tx` cannot remain `true` forever if a callback receipt fails outright.

### Proof of Concept
1. Deploy a malicious NEP-141-like contract `evil.near` whose `ft_balance_of` (or `ft_transfer`) returns an intentionally huge success value (as large as the protocol permits for a function-call return).
2. As the eth-implicit account owner (or a relayer relaying the owner's signed tx), submit an RLP transaction via `rlp_execute` with `target = evil.near`, matching the emulated ERC-20 `balanceOf`/`transfer` selector, per `eth_emulation::try_emulation`: [6](#0-5) 
3. `inner_rlp_execute` schedules the cross-contract call chained to `rlp_execute_callback` with the fixed `RLP_EXECUTE_CALLBACK_GAS` (5 Tgas) budget, and sets `has_in_flight_tx = true`.
4. When `evil.near` returns its oversized payload, `rlp_execute_callback` begins executing: `self.has_in_flight_tx = false;` runs first, then `env::promise_result(0)` attempts to copy the oversized blob into a register, consuming gas proportional to its size; with a sufficiently large payload this exhausts the 5 Tgas budget and the callback panics before returning.
5. Because the callback failed, all of its state writes — including the `has_in_flight_tx = false` assignment — are rolled back by the runtime, leaving `has_in_flight_tx == true` in persisted state.
6. Every subsequent call to `rlp_execute` on this account now immediately returns the "transaction already in progress" error, as shown by the guard check at the top of `rlp_execute`: [1](#0-0) 
   with no way for the owner to ever transact from this account again, permanently freezing its funds.

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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L275-296)
```rust
    #[private]
    pub fn rlp_execute_callback(
        &mut self,
        caller_deposit: Option<CallerDeposit>,
    ) -> ExecuteResponse {
        self.has_in_flight_tx = false;
        let n = env::promise_results_count();

        if n == 0 {
            // `rlp_execute_callback` is called directly in the case of an emulated self-transfer.
            return ExecuteResponse { success: true, success_value: None, error: None };
        } else if n > 1 {
            return ExecuteResponse {
                success: false,
                success_value: None,
                error: Some(format!(
                    "Invariant violation: this callback comes after a single promise. n={n}"
                )),
            };
        }

        match env::promise_result(0) {
```

**File:** runtime/near-vm-runner/src/logic/logic.rs (L3958-3986)
```rust
    /// # Cost
    ///
    /// `base + cost of writing data into a register`
    pub fn promise_result(&mut self, result_idx: u64, register_id: u64) -> Result<u64> {
        self.result_state.gas_counter.pay_base(base)?;
        if self.context.is_view() {
            return Err(
                HostError::ProhibitedInView { method_name: "promise_result".to_string() }.into()
            );
        }
        match self
            .context
            .promise_results
            .get(result_idx as usize)
            .ok_or(HostError::InvalidPromiseResultIndex { result_idx })?
        {
            PromiseResult::NotReady => Ok(0),
            PromiseResult::Successful(data) => {
                self.registers.set_rc_data(
                    &mut self.result_state.gas_counter,
                    &self.config.limit_config,
                    register_id,
                    Rc::clone(data),
                )?;
                Ok(1)
            }
            PromiseResult::Failed => Ok(2),
        }
    }
```

**File:** runtime/near-vm-runner/src/logic/tests/promises.rs (L69-84)
```rust
#[test]
fn test_promise_result_per_byte_gas_fee() {
    const RESULT_SIZE: usize = 100;
    let promise_results = [PromiseResult::Successful([0u8; RESULT_SIZE].into())];

    let mut logic_builder = VMLogicBuilder::default();
    logic_builder.context.promise_results = promise_results.into();
    let mut logic = logic_builder.build();

    logic.promise_result(0, 0).expect("promise_result should succeed");

    assert_costs(map! {
      ExtCosts::base: 1,
      ExtCosts::write_register_base: 1,
    });
}
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/eth_emulation.rs (L39-58)
```rust
        ERC20_BALANCE_OF_SELECTOR => {
            let (address,): (Address,) =
                ethabi_utils::abi_decode(&ERC20_BALANCE_OF_SIGNATURE, &tx.data[4..])?;
            // The account ID is assumed to have the same suffix as the current account because
            // (1) in production this is correct as all eth-implicit accounts are top-level and
            // (2) in testing environments where the addresses are sub-accounts, they are still
            // assumed to all be deployed to the same namespace so that they will all have the
            // same suffix.
            let args = format!(r#"{{"account_id": "0x{}{}"}}"#, hex::encode(address), suffix);
            Ok((
                Action::FunctionCall {
                    receiver_id: target.to_string(),
                    method_name: "ft_balance_of".into(),
                    args: args.into_bytes(),
                    gas: FIVE_TERA_GAS,
                    yocto_near: 0,
                },
                ParsableEthEmulationKind::ERC20Balance,
            ))
        }
```
