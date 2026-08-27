### Title
Non-refunded `caller_deposit` on failed NEP-141 `storage_balance_of`/registrar lookups permanently strands relayer funds in the ETH-implicit Wallet Contract - ([File: runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs])

### Summary
The `WalletContract` (the ETH-implicit account's contract, `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`) accepts an attached deposit from an external caller/relayer (`caller_deposit`) when relaying an Ethereum-emulated transaction via `rlp_execute`. If the underlying action ultimately fails, `rlp_execute_callback` correctly refunds this deposit back to the caller. However, two other callback branches in the same execution pipeline — `address_check_callback` and `nep_141_storage_balance_callback` — return an error `ExecuteResponse` on a failed intermediate cross-contract call (`PromiseResult::Failed`) **without** refunding `caller_deposit`. Because this deposit is never returned in these paths, it becomes permanently stuck in the Wallet Contract's balance, exactly mirroring the "non-standard token / failure not properly handled → stuck funds" bug class from the reference report, but at the level of the emulated NEP-141/ERC-20 interaction rather than raw NEP-141 transfer.

### Finding Description
- `caller_deposit` is created in `inner_rlp_execute` (`runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs:345`, via `CallerDeposit::new(&context)`), capturing the attached deposit of an external, non-owner caller (e.g., a relayer) invoking `rlp_execute`.
- The happy/failure path that most examples/tests exercise is the final callback, `rlp_execute_callback` (`lib.rs:276-317`). Its `PromiseResult::Failed` branch explicitly refunds the caller: [1](#0-0) 
- But for ERC-20 emulation with an unregistered receiver, the flow first routes through `nep_141_storage_balance_callback` (`lib.rs:194-273`), which calls `storage_balance_of` on the target NEP-141 token. Its `PromiseResult::Failed` arm returns an error response directly, with no reference to `caller_deposit` at all: [2](#0-1) 
- Similarly, for a base-token transfer targeting another eth-implicit account requiring an address-registrar check, `address_check_callback` (`lib.rs:133-192`) has the identical shape: its `PromiseResult::Failed` arm for the registrar lookup returns failure without refunding `caller_deposit`, even though the parameter is passed into the function: [3](#0-2) 
- In both cases, the `caller_deposit` parameter is threaded through the call chain (`lib.rs:230-269`, `lib.rs:412-458`) purely so it can eventually reach `rlp_execute_callback` for a refund — but if the *intermediate* cross-contract call itself fails (rather than the final action), execution short-circuits before ever reaching the refund logic, silently dropping the deposit.
- A "failed" `storage_balance_of` or registrar `lookup` call is plausible for reasons entirely outside the Wallet Contract's control: a non-standard/misbehaving NEP-141 token contract that panics instead of returning `null`/`None` for unregistered accounts, insufficient gas on an unusually heavy view implementation, or the target account/token no longer existing. This is directly analogous to the report's premise — an external, non-privileged contract implementation "misbehaving" relative to the expected standard, and the Wallet Contract not handling that deviation safely — except here it causes deposit loss instead of returned-value loss.

### Impact Explanation
Any NEAR deposited by an external caller (typically a relayer compensating itself, or a user calling directly) alongside an ERC-20-emulated transfer to an unregistered receiver, or a base-token transfer to another not-yet-registered eth-implicit account, is permanently and unrecoverably locked in the Wallet Contract's account balance if the intermediate `storage_balance_of`/registrar `lookup` call fails. There is no code path that later reclaims or refunds this amount; it simply becomes part of the Wallet Contract's balance, indistinguishable from and unreachable by the account's rightful owner within the wallet-contract's fixed method set (which cannot be used to sweep undesignated NEAR back out). This is a permanent freezing-of-funds bug reachable purely by an ordinary external caller of `rlp_execute` — no privileged access needed.

### Likelihood Explanation
Moderate-to-low but non-negligible: it requires (a) an ERC-20 transfer emulation (or base-token transfer to another eth-implicit account) where the receiving account is unregistered with the token/registrar, and (b) the `storage_balance_of` or registrar `lookup` call failing rather than returning cleanly. This can happen with any NEP-141 token that does not strictly implement `storage_balance_of` to gracefully return `null` (deviating from NEP-145), with out-of-gas on the view call, or with a registrar/token contract deletion/misconfiguration mid-flight. Given the growing ecosystem of NEP-141 tokens with varying quality of implementation (directly paralleling the "not all ERC-20s are standard" premise of the original report), this is a realistic occurrence for relayers routinely funding these operations.

### Recommendation
In `address_check_callback` and `nep_141_storage_balance_callback`, mirror the refund logic already present in `rlp_execute_callback`'s `PromiseResult::Failed` branch: before returning the failure `ExecuteResponse`, if `caller_deposit` is `Some`, create a refund promise back to `caller_deposit.account_id` for `caller_deposit.yocto_near`, exactly as done at: [4](#0-3) 
Consider factoring this into a shared helper (e.g. `refund_caller_deposit(caller_deposit)`) invoked from all three failure branches (`address_check_callback`, `nep_141_storage_balance_callback`, `rlp_execute_callback`) to prevent this class of omission from recurring as new emulation branches are added.

### Proof of Concept
1. Deploy the Wallet Contract to an eth-implicit account `E` (owner controls a Secp256k1 key).
2. Deploy a NEP-141 token `T` whose `storage_balance_of` implementation panics (or runs out of the 5 TGas allotted by `NEP_141_STORAGE_BALANCE_OF_GAS`) for account IDs it has never seen, instead of returning `null` as NEP-145 specifies.
3. An external relayer `R` (not `E`'s owner) calls `E.rlp_execute(target=T, tx_bytes_b64=<ERC20 transfer to unregistered receiver X>)` attaching a NEAR deposit `D` (e.g., to cover an eventual relayer fee as in `test_base_token_transfer_with_relayer_refund`/`test_caller_refunds`).
4. Execution reaches `nep_141_storage_balance_callback` on `E`; the batched `storage_balance_of` promise resolves as `PromiseResult::Failed` due to `T`'s panic/OOG.
5. `nep_141_storage_balance_callback` returns `ExecuteResponse { success: false, ... }` immediately (`lib.rs:203-210`) — no refund promise for `R`'s deposit `D` is created.
6. `R`'s balance decreases by `D`, and `D` is now permanently added to `E`'s balance with no method on the Wallet Contract's ABI to recover it back to `R`.

This can be reproduced in the existing test harness (`runtime/near-wallet-contract/implementation/wallet-contract/src/tests/emulation.rs`) by substituting a token contract whose `storage_balance_of` reliably panics, then asserting (as `test_caller_refunds` does for the default path) that the relayer's balance is *not* restored — demonstrating the discrepancy versus the refund behavior already verified for the `rlp_execute_callback` path.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L141-148)
```rust
        let maybe_account_id: Option<AccountId> = match env::promise_result(0) {
            PromiseResult::Failed => {
                return PromiseOrValue::Value(ExecuteResponse {
                    success: false,
                    success_value: None,
                    error: Some("Call to Address Registrar contract failed".into()),
                });
            }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L201-221)
```rust
    ) -> PromiseOrValue<ExecuteResponse> {
        self.has_in_flight_tx = false;
        let maybe_storage_balance: Option<StorageBalance> = match env::promise_result(0) {
            PromiseResult::Failed => {
                return PromiseOrValue::Value(ExecuteResponse {
                    success: false,
                    success_value: None,
                    error: Some(format!("Call to NEP-141 {token_id}::storage_balance_of failed")),
                });
            }
            PromiseResult::Successful(value) => match serde_json::from_slice(&value) {
                Ok(x) => x,
                Err(_) => {
                    return PromiseOrValue::Value(ExecuteResponse {
                        success: false,
                        success_value: None,
                        error: Some("Unexpected response from NEP-141 storage_balance_of".into()),
                    });
                }
            },
        };
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L296-312)
```rust
        match env::promise_result(0) {
            PromiseResult::Failed => {
                // The cross-contract call failed, refund the caller if needed
                if let Some(CallerDeposit { account_id, yocto_near }) = caller_deposit {
                    let refund_promise = env::promise_batch_create(&account_id);
                    env::promise_batch_action_transfer(
                        refund_promise,
                        NearToken::from_yoctonear(yocto_near.into()),
                    );
                }

                ExecuteResponse {
                    success: false,
                    success_value: None,
                    error: Some("Failed Near promise".into()),
                }
            }
```
