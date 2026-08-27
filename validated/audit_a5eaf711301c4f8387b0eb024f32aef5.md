### Title
Silent-failure return values from arbitrary target contracts are treated as full success by the eth-implicit Wallet Contract, causing loss of forwarded deposits — (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
The Wallet Contract (the NEAR-side contract deployed to eth-implicit accounts to let Ethereum-signed transactions drive NEAR actions) determines the success of a relayed action purely from whether the underlying cross-contract `Promise` panicked (`PromiseResult::Failed`) versus did not panic (`PromiseResult::Successful`). It never inspects the semantic contents of a `Successful` result to see whether the target contract's application logic actually failed. This mirrors the MakerDAO `DssChangeRatesSpell` class of bug: a callee that signals failure via a return value (instead of reverting/panicking) causes the caller to treat the action as fully completed even though nothing meaningful happened, and any attached deposit forwarded with that call is not refunded.

### Finding Description
`rlp_execute` accepts an arbitrary `near_action::Action` (including `FunctionCall` to *any* target contract, not just well-behaved NEP-141 tokens) decoded from a user's Ethereum transaction and turns it into a `Promise` via `action_to_promise`, chained to `rlp_execute_callback`: [1](#0-0) 

The final outcome is decided in `rlp_execute_callback`, which only branches on `PromiseResult::Failed` vs. `PromiseResult::Successful(value)`: [2](#0-1) 

Refunding the caller's attached deposit (`caller_deposit`) only happens in the `Failed` branch: [3](#0-2) 

If the target contract's method returns *without panicking* — e.g. returns a boolean/`Result`-encoded failure, an empty value, or any application-level "not actually done" signal instead of raising an exception — the promise result is `Successful`, and:
1. `rlp_execute_callback` reports `ExecuteResponse { success: true, ... }` even though the underlying operation did not take effect.
2. No refund of the attached deposit is issued, because the refund path is gated on `PromiseResult::Failed` only.
3. The wallet's nonce (already incremented before the call, in `address_check_callback` / `inner_rlp_execute`) is not rolled back, permanently consuming that nonce for a transaction whose intended effect never happened.

This is functionally identical to the reported MakerDAO pattern: the caller (`cast`/`rlp_execute_callback`) assumes success is equivalent to "no exception," while some callees use return-value-based error signaling rather than panics/reverts.

### Impact Explanation
Because `FunctionCall` actions relayed through the Wallet Contract can target *any* contract and any method (the ERC-20 emulation path specifically constructs `ft_transfer`/`ft_transfer_call`-style calls, but the general `Action::FunctionCall` path in `near_action` is not restricted to standards-compliant, panic-based tokens), any target contract that signals failure via a returned value rather than panicking will cause:
- Silent loss of the yoctoNEAR/token deposit attached to the call (funds sent to the target are not returned, and the wallet's own `caller_deposit` refund never fires).
- A false-positive success response returned to the relayer/user, misleading them about the on-chain state.

This is a loss-of-funds condition for eth-implicit account users interacting with non-conforming or adversarial target contracts, and it is user/attacker-reachable simply by directing an eth-implicit-account transaction at such a contract — no privileged or validator/node role is required.

### Likelihood Explanation
Likelihood depends on the target contract's error-handling convention. NEAR's own NEP-141 reference implementation is exception-based (panics on failure), so the specific `ft_transfer` emulation path used by `eth_emulation.rs` is not directly exploitable by that particular built-in mapping ( [4](#0-3) ). However, `rlp_execute`/`inner_rlp_execute` also supports a general `Action::FunctionCall` (arbitrary receiver, method, args, deposit) decoded straight from a Near-action-encoded Ethereum payload, and `address_check_callback`/`nep_141_storage_balance_callback` forward that action verbatim to `action_to_promise` without constraining which contracts or methods can be targeted. Any third-party or custom contract that does not follow the panic-on-failure convention (a very common gotcha, as the MakerDAO documentation the report cites highlights) will trigger this issue whenever a user is induced or chooses to route funds through it.

### Recommendation
- In `rlp_execute_callback`, do not treat every `PromiseResult::Successful` as `success: true` for arbitrary `FunctionCall` actions; where the target's return type is known/checkable (e.g., booleans or `Result`-shaped JSON), parse and validate the actual payload semantics before reporting success, similar to how `address_check_callback`/`nep_141_storage_balance_callback` already deserialize and validate returned JSON.
- Document explicitly (per the short-term recommendation in the referenced report) that a NEAR promise being `Successful` only means "did not panic," not that the target's business logic succeeded, and that this can cause the caller's deposit to be non-refundable if the target uses return-value-based failure signaling.
- Consider restricting the general `Action::FunctionCall` relay path to a safelist of known, panic-based standards (as is effectively done for the ERC-20 emulation), or requiring targets to conform to an interface that lets the Wallet Contract validate the meaningful outcome, not just absence of panic.

### Proof of Concept
1. Deploy an eth-implicit account with the Wallet Contract, as in `test_eth_implicit_accounts`/`rlp_execute` flow: [5](#0-4) .
2. Deploy a "fake NEP-141-like" or otherwise non-conforming target contract whose transfer-equivalent method returns a JSON `false`/error object instead of panicking on failure (this is legal under NEAR's programming model; nothing in `near-vm-runner` or the runtime enforces panic-based error handling for contract calls — see the generic `promise_result` host function, which only exposes `NotReady`/`Successful`/`Failed`, with `Failed` reserved for actual execution failures: [6](#0-5) ).
3. Have the eth-implicit account owner sign an Ethereum transaction that produces an `Action::FunctionCall` targeting this contract with a non-zero deposit (`yocto_near`), via `parse_rlp_tx_to_action`/`inner_rlp_execute`.
4. Submit via `rlp_execute`; the target contract executes without panicking and returns a failure-indicating value.
5. Observe: `rlp_execute_callback` returns `ExecuteResponse { success: true, ... }` ( [7](#0-6) ), the caller's `caller_deposit` is never refunded (refund logic is only in the `Failed` arm, [8](#0-7) ), and the nonce has already advanced, so the deposit is unrecoverable and the transaction cannot be retried.

Note: I was unable to fully inspect `near_action.rs`/`action_to_promise` and `inner_rlp_execute` source in this pass (only their signatures/usages were found via grep, not full bodies), so the exact deposit-forwarding mechanics for the generic `FunctionCall` path could not be verified line-by-line; a Devin session with full file access would be needed to confirm the precise deposit-attachment code path before treating this as fully confirmed.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L88-128)
```rust
    #[payable]
    pub fn rlp_execute(
        &mut self,
        target: AccountId,
        tx_bytes_b64: String,
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L296-316)
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
            PromiseResult::Successful(value) => {
                ExecuteResponse { success: true, success_value: Some(value), error: None }
            }
        }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/eth_emulation.rs (L59-92)
```rust
        ERC20_TRANSFER_SELECTOR => {
            // We intentionally map to `u128` instead of `U256` because the NEP-141 standard
            // is to use u128.
            let (to, value): (Address, u128) =
                ethabi_utils::abi_decode(&ERC20_TRANSFER_SIGNATURE, &tx.data[4..])?;
            let receiver_id: AccountId = format!("0x{}{}", hex::encode(to), suffix)
                .parse()
                .unwrap_or_else(|_| env::panic_str("eth-implicit accounts are valid account ids"));

            // Include any data after the main args as a memo in the transfer.
            // The main data takes 68 bytes because there is a 4-byte selector followed
            // by two arguments which are each allocated 32 bytes according to the
            // Solidity ABI standard.
            let memo = if tx.data.len() > 68 {
                Some(format!(r#""0x{}""#, hex::encode(&tx.data[68..])))
            } else {
                None
            };
            let args = format!(
                r#"{{"receiver_id": "{}", "amount": "{}", "memo": {}}}"#,
                receiver_id.as_str(),
                value,
                memo.as_deref().unwrap_or("null"),
            );
            Ok((
                Action::FunctionCall {
                    receiver_id: target.to_string(),
                    method_name: "ft_transfer".into(),
                    args: args.into_bytes(),
                    gas: 2 * FIVE_TERA_GAS,
                    yocto_near: 1,
                },
                ParsableEthEmulationKind::ERC20Transfer { receiver_id, fee },
            ))
```

**File:** integration-tests/src/tests/features/stateless_validation.rs (L387-415)
```rust
/// Tests that eth-implicit accounts still work with stateless validation.
#[test]
fn test_eth_implicit_accounts() {
    let accounts =
        vec!["test0".parse().unwrap(), "test1".parse().unwrap(), "test2".parse().unwrap()];
    let genesis = Genesis::test(accounts.clone(), 2);
    let mut env = TestEnv::builder(&genesis.config)
        .validators(accounts.clone())
        .clients(accounts)
        .nightshade_runtimes(&genesis)
        .build();
    let genesis_block = env.clients[0].chain.get_block_by_height(0).unwrap();
    let chain_id = &genesis.config.chain_id;
    let signer = create_user_test_signer(AccountIdRef::new("test2").unwrap());

    // Deploy the wallet contract as a global contract for ETH implicit accounts.
    let mut next_nonce = 1;
    let magic_bytes = wallet_contract_magic_bytes(chain_id);
    let wallet_code = wallet_contract(*magic_bytes.hash()).unwrap();
    let deploy_tx = SignedTransaction::deploy_global_contract(
        next_nonce,
        signer.get_account_id(),
        wallet_code.code().to_vec(),
        &signer.clone().into(),
        *genesis_block.hash(),
        GlobalContractDeployMode::CodeHash,
    );
    next_nonce += 1;
    assert_eq!(env.rpc_handlers[0].process_tx(deploy_tx, false, false), ProcessTxResponse::ValidTx);
```

**File:** runtime/near-vm-runner/src/logic/logic.rs (L3961-3986)
```rust
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
