### Title
External caller's attached deposit is not refunded when the ETH-implicit Wallet Contract's address-registrar lookup fails - (File: runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs)

### Summary
The Wallet Contract (NEP-518) tracks any NEAR deposit attached by an external (non-owner) caller of `rlp_execute` in a `CallerDeposit` struct so it can be refunded if the resulting cross-contract call fails [1](#0-0) . This refund is correctly performed in `rlp_execute_callback` when the final action's promise fails [2](#0-1) , but the same `caller_deposit` value is silently dropped without any refund in `address_check_callback` when the call to the address registrar itself fails [3](#0-2) .

### Finding Description
When an emulated Ethereum EOA base-token transfer targets another eth-implicit account, `inner_rlp_execute` builds a promise that first calls the address registrar to check whether the target address is actually a registered named account, then continues to `address_check_callback` [4](#0-3) . The `caller_deposit` (built from any deposit an external, non-owner predecessor attached to the `rlp_execute` call, per `CallerDeposit::new`) is passed into `address_check_callback` [5](#0-4) .

Inside `address_check_callback`, if `env::promise_result(0)` is `PromiseResult::Failed` (i.e., the registrar lookup call itself failed, e.g. due to insufficient gas, a paused/misbehaving registrar contract, or the registrar account not existing), the function immediately returns an `ExecuteResponse` with `success: false` and drops `caller_deposit` without issuing any refund promise [3](#0-2) . Compare this to the deserialization-failure branch in the same function, which also returns early and also fails to refund [6](#0-5) , and to the correctly-handled failure path in `rlp_execute_callback`, which explicitly creates a transfer promise back to `caller_deposit.account_id` before returning failure [2](#0-1) .

Because the attached deposit was transferred to the wallet contract's account balance as part of the original `rlp_execute` `FunctionCall` action (this is protocol-level behavior — the deposit is only refunded automatically if the entire receipt fails, not if a specific in-contract logic branch decides to return an error value), any deposit attached by an honest external relayer/caller is retained by the wallet contract instead of being returned to the caller in this failure path. The funds are not burned or destroyed, but they are misappropriated to the ETH-implicit account's own balance without the depositor's consent and with no code path to return them — the caller has no way to reclaim their attached NEAR, since only the account's Secp256k1 key owner (via a further signed `rlp_execute` transaction) can move funds out of that account.

### Impact Explanation
This is a fund-misdirection/permanent-loss bug for an unprivileged external caller (a relayer submitting `rlp_execute` on behalf of a user, or any other native NEAR account that attaches a deposit when calling this endpoint). Whenever the registrar lookup fails independent of the caller's fault (e.g., registrar contract gas/config issues, congestion, or registrar being paused), the caller's attached deposit becomes permanently unrecoverable by that caller. This matches the "permanent freezing/misappropriation of funds" bug class from the reported external report (ETH stuck in `MPROVesting.sol` due to no withdrawal path), translated to nearcore's unprivileged eth-implicit Wallet Contract execution path.

### Likelihood Explanation
This requires: (1) an external, non-owner caller attaching a deposit to `rlp_execute`, and (2) the target being another eth-implicit account so the address-registrar lookup path is taken, and (3) the registrar cross-contract call itself failing (not the subsequent action). This is plausible under normal operational conditions (gas exhaustion, registrar contract issues, or misconfiguration) and does not require any malicious validator or privileged actor — only a normal relayer/caller interacting with the deployed, production Wallet Contract global contract.

### Recommendation
In `address_check_callback`, in both the `PromiseResult::Failed` and the JSON-deserialization-failure branches, replicate the refund logic used in `rlp_execute_callback`: if `caller_deposit` is `Some`, create a `promise_batch_action_transfer` back to `caller_deposit.account_id` for `caller_deposit.yocto_near` before returning the `ExecuteResponse` with `success: false`.

### Proof of Concept
1. Deploy the Wallet Contract as the global contract for eth-implicit accounts (as in `test_wallet_contract_interaction`) [7](#0-6) .
2. As an external NEAR account (not the eth-implicit account's owner), call `rlp_execute` with an attached deposit, targeting a transaction whose `target` is a second, distinct eth-implicit account so that `TargetKind::EthImplicit` triggers the registrar-lookup path (`address_check_callback`) [4](#0-3) .
3. Cause the address-registrar's `lookup` cross-contract call to fail (e.g., attach insufficient gas via `REGISTRAR_LOOKUP_GAS`, or point `ADDRESS_REGISTRAR_ACCOUNT_ID` at a non-existent/failing account in a test harness).
4. Observe that `address_check_callback` returns `success: false` in `ExecuteResponse` but issues no transfer back to the external caller [3](#0-2) , while the existing test `test_caller_refunds` shows the expected/normal refund behavior only for failures at the final action-execution stage [8](#0-7) .
5. Verify the caller's NEAR balance decreased by the attached deposit and never recovers it, while the eth-implicit account's balance increased by that amount.

**Note on verification limits**: I was unable to fully confirm from the index whether existing integration tests (in `runtime/near-wallet-contract/implementation/wallet-contract/src/tests/relayer.rs`) already exercise a registrar-lookup failure scenario and assert on the deposit refund, since only partial file contents were retrievable via search. If such a test exists and asserts correct refunding here, this finding would need re-validation against that test's outcome; a Devin session with full file access would be needed to confirm this conclusively.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs (L172-192)
```rust
/// A data type to keep track of the deposit given by an external caller.
/// This allows us to refund the caller's deposit if the cross-contract call fails.
#[derive(Debug, PartialEq, Eq, Clone, serde::Serialize, serde::Deserialize)]
pub struct CallerDeposit {
    pub account_id: AccountId,
    pub yocto_near: NonZeroU128,
}

impl CallerDeposit {
    pub fn new(context: &ExecutionContext) -> Option<Self> {
        // Only track for external (non-self) callers
        if context.current_account_id == context.predecessor_account_id {
            return None;
        }

        NonZeroU128::new(context.attached_deposit.as_yoctonear()).map(|yocto_near| Self {
            account_id: context.predecessor_account_id.clone(),
            yocto_near,
        })
    }
}
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L130-139)
```rust
    /// Callback after checking if an address is contained in the registrar.
    /// This check happens when the target is another eth implicit account to
    /// confirm that the relayer really did check for a named account with that address.
    #[private]
    pub fn address_check_callback(
        &mut self,
        target: AccountId,
        action: near_action::Action,
        caller_deposit: Option<CallerDeposit>,
    ) -> PromiseOrValue<ExecuteResponse> {
```

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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L149-158)
```rust
            PromiseResult::Successful(value) => match serde_json::from_slice(&value) {
                Ok(x) => x,
                Err(_) => {
                    return PromiseOrValue::Value(ExecuteResponse {
                        success: false,
                        success_value: None,
                        error: Some("Unexpected response from account registrar".into()),
                    });
                }
            },
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L412-432)
```rust
    let promise = match transaction_kind {
        TransactionKind::EthEmulation(EthEmulationKind::EOABaseTokenTransfer {
            address_check: Some(address),
            ..
        }) => {
            let callback_gas = ADDRESS_CHECK_CALLBACK_GAS.saturating_add(action.gas());
            let ext = WalletContract::ext(current_account_id).with_static_gas(callback_gas);
            let address_registrar = {
                let account_id = ADDRESS_REGISTRAR_ACCOUNT_ID
                    .trim()
                    .parse()
                    .unwrap_or_else(|_| env::panic_str("Invalid address registrar"));
                ext_registrar::ext(account_id).with_static_gas(REGISTRAR_LOOKUP_GAS)
            };
            let address = format!("0x{}", hex::encode(address));
            address_registrar.lookup(address).then(ext.address_check_callback(
                target,
                action,
                caller_deposit,
            ))
        }
```

**File:** integration-tests/src/tests/features/wallet_contract.rs (L227-256)
```rust
#[test]
fn test_wallet_contract_interaction() {
    let genesis = Genesis::test(vec!["test0".parse().unwrap(), alice_account(), bob_account()], 1);
    let mut env = TestEnv::builder(&genesis.config).nightshade_runtimes(&genesis).build();

    let genesis_block = env.clients[0].chain.get_block_by_height(0).unwrap();
    let chain_id = &genesis.config.chain_id;
    let mut height = 1;
    let blocks_number = 10;

    // As the relayer, alice will be sending Near transactions which
    // contain the Ethereum transactions the user signs.
    let relayer = alice_account();
    let mut relayer_signer =
        NearSigner { account_id: &relayer, signer: create_user_test_signer(&relayer) };
    // Bob will receive a $NEAR transfer from the eth implicit account
    let receiver = bob_account();

    // Deploy the wallet contract as a global contract for ETH implicit accounts.
    let magic_bytes = wallet_contract_magic_bytes(chain_id);
    let wallet_code = wallet_contract(*magic_bytes.hash()).unwrap();
    let deploy_tx = SignedTransaction::deploy_global_contract(
        1,
        relayer.clone(),
        wallet_code.code().to_vec(),
        &relayer_signer.signer,
        *genesis_block.hash(),
        GlobalContractDeployMode::CodeHash,
    );
    height = check_tx_processing(&mut env, deploy_tx, height, blocks_number);
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/tests/sanity.rs (L170-213)
```rust
// An external caller gets its deposit back if the cross-contract call fails.
#[tokio::test]
async fn test_caller_refunds() -> anyhow::Result<()> {
    let TestContext { worker, wallet_contract, wallet_sk, address_registrar, .. } =
        TestContext::new().await?;

    let caller = worker.root_account()?;
    let deposit_amount = NearToken::from_near(3);
    let create_tx = |receiver_id: &AccountId, nonce: u64| {
        let method = "register";
        let args = br#"{"account_id": "birchmd.near"}"#;
        let action = Action::FunctionCall {
            receiver_id: receiver_id.to_string(),
            method_name: method.into(),
            args: args.to_vec(),
            gas: Gas::from_tgas(10).as_gas(),
            yocto_near: 0,
        };
        utils::create_signed_transaction(
            nonce,
            receiver_id,
            Wei::new_u128(deposit_amount.as_yoctonear() / (MAX_YOCTO_NEAR as u128)),
            action,
            &wallet_sk,
        )
    };

    // External caller gets a refund when the cross-contract call fails
    let pre_tx_account_balance = caller.view_account().await?.balance;
    let receiver_id: AccountId = "fake.near".parse()?;
    let result = wallet_contract
        .rlp_execute_from(
            &caller,
            receiver_id.as_str(),
            &create_tx(&receiver_id, 0),
            deposit_amount,
        )
        .await?;
    assert!(!result.success);
    let post_tx_account_balance = caller.view_account().await?.balance;
    assert!(
        pre_tx_account_balance.as_yoctonear() - post_tx_account_balance.as_yoctonear()
            < deposit_amount.as_yoctonear()
    );
```
