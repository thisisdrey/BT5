### Title
Emulated ERC-20 `transfer` calldata sends the user's attached native $NEAR value to the token contract instead of the token's recipient - (File: runtime/near-wallet-contract/implementation/wallet-contract/src/eth_emulation.rs)

### Summary
In the ETH-implicit account Wallet Contract, an Ethereum transaction whose calldata matches the ERC-20 `transfer(address,uint256)` selector is translated into a NEAR `FunctionCall` action targeting the token contract (`ft_transfer`). The transaction's native `value` field (the Ethereum-style ETH amount, converted to yoctoNEAR) is then attached as the **deposit of that same FunctionCall**, i.e. it is sent to the token contract account, not to the actual transfer recipient encoded in the calldata.

### Finding Description
The emulation path builds the action with a hardcoded `yocto_near: 1` (the value required by the NEP-141 `assert_one_yocto` convention) and targets the token contract itself: [1](#0-0) 

Later, when the parsed action is finalized, the transaction's `tx.value` (converted from wei to yoctoNEAR) is unconditionally folded into the deposit of whatever action was produced — including `FunctionCall`: [2](#0-1) [3](#0-2) 

The resulting `FunctionCallAction` (with the combined deposit) is dispatched to the token contract (`target`) via `action_to_promise`/`nep_141_storage_balance_callback`, not to the ERC-20 `to` address that the user actually intended to receive value: [4](#0-3) [5](#0-4) 

This mirrors the reported bug class: a "value" portion of a combined transfer is silently routed to a contract account (here, the fungible-token contract) instead of reaching the actual recipient, because the transfer-execution code only forwards one of the two asset legs to the intended destination.

### Impact Explanation
If a signer attaches a non-zero `value` to a signed Ethereum transaction whose calldata is an ERC-20 `transfer`, that native NEAR amount is delivered as attached deposit to the fungible-token contract's `ft_transfer` call rather than to the recipient named in the transfer. Depending on the target NEP-141 contract's behavior:
- If the contract enforces `assert_one_yocto()` (the standard pattern), the call panics on any deposit other than 1 yoctoNEAR; the deposit is then refunded back to the *wallet contract* account (not lost) per normal NEAR receipt-failure refund semantics, but the user's transfer as a whole fails.
- If the contract does not strictly reject extra deposits (non-strictly-compliant or custom NEP-141-like contracts), the extra NEAR is absorbed into the token contract's balance without being forwarded anywhere, permanently removing it from the user's control with no compensating transfer to the recipient — i.e. the base asset is effectively lost/trapped, analogous to the reported bridge issue.

### Likelihood Explanation
This requires the account owner (or software constructing transactions on their behalf) to sign an Ethereum-style transaction that combines a non-zero `value` with ERC-20 `transfer` calldata — an unusual but not-prevented usage pattern, since the wallet contract performs no validation rejecting `value > 0` on this code path. It cannot be forced by a malicious relayer, since `tx.value` is part of the signed Ethereum transaction and relayer tampering is caught by `validate_tx_relayer_data`. Real-world impact further depends on the specific NEP-141 contract targeted, which is outside the scope of this repository, so likelihood is assessed as low-to-medium, contingent on target-contract behavior.

### Recommendation
Reject (or explicitly refuse to merge) any `tx.value` into `FunctionCall` deposits produced by the ERC-20 (and other) emulation paths in `eth_emulation.rs`/`internal.rs`/`types.rs`. Native-value transfers should only ever be folded into genuine `Transfer` actions (or otherwise explicitly forwarded to the intended recipient), never attached silently to a `FunctionCall` whose receiver is a different account than the one the user intends to fund.

### Proof of Concept
1. Deploy the Wallet Contract as an eth-implicit account and fund it with $NEAR (as in `test_wallet_contract_interaction`, `runtime/near-wallet-contract/implementation/wallet-contract/src/tests/*`).
2. Construct and sign an Ethereum-style `Transaction2930` with `to` = a NEP-141 token contract, `data` = ERC-20 `transfer(to, amount)` selector + args, and `value` > 0.
3. Submit via `rlp_execute`; the resulting `FunctionCall` to the token contract will carry `deposit = 1 yoctoNEAR + value_converted` (see `types.rs` lines 238-253), sent to the *token contract*, not to `to`.
4. Observe: with an `assert_one_yocto`-style token contract, the call fails and the deposit refunds to the wallet contract (no ERC-20 transfer, no NEAR loss but the transfer failed unexpectedly); with a token contract that accepts and does not forward extra deposits, the NEAR is retained by the token contract permanently while the recipient never receives it.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/eth_emulation.rs (L83-93)
```rust
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
        }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/internal.rs (L159-165)
```rust
    validate_tx_value(&tx)?;

    // Call to `low_u128` here is safe because of the validation done in `validate_tx_value`
    let near_action = action
        .try_into_near_action(tx.value.raw().low_u128().saturating_mul(MAX_YOCTO_NEAR.into()))?;

    Ok((near_action, transaction_kind))
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs (L238-253)
```rust
    pub fn try_into_near_action(
        self,
        additional_value: u128,
    ) -> Result<near_action::Action, Error> {
        let action = match self {
            Action::FunctionCall { receiver_id: _, method_name, args, gas, yocto_near } => {
                let action = FunctionCallAction {
                    method_name,
                    args,
                    gas: Gas::from_gas(gas),
                    deposit: NearToken::from_yoctonear(
                        additional_value.saturating_add(yocto_near.into()),
                    ),
                };
                near_action::Action::FunctionCall(action)
            }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L433-458)
```rust
        TransactionKind::EthEmulation(EthEmulationKind::ERC20Transfer { receiver_id, .. }) => {
            // In the case of the emulated ERC-20 transfer, the receiving account
            // might not be registered with the NEP-141 contract (per the NEP-145)
            // storage standard. Therefore we must create a multi-step promise where
            // first we check if the receiver is registered and then if not call
            // `storage_deposit` in addition to `ft_transfer`.
            let token_id = target;
            let callback_gas = NEP_141_STORAGE_BALANCE_CALLBACK_GAS.saturating_add(action.gas());
            let ext: WalletContractExt =
                WalletContract::ext(current_account_id).with_static_gas(callback_gas);
            let storage_balance_args =
                format!(r#"{{"account_id": "{}"}}"#, receiver_id.as_str()).into_bytes();
            Promise::new(token_id.clone())
                .function_call(
                    "storage_balance_of".into(),
                    storage_balance_args,
                    NearToken::from_yoctonear(0),
                    NEP_141_STORAGE_BALANCE_OF_GAS,
                )
                .then(ext.nep_141_storage_balance_callback(
                    token_id,
                    receiver_id,
                    action,
                    caller_deposit,
                ))
        }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L475-483)
```rust
fn action_to_promise(target: AccountId, action: near_action::Action) -> Result<Promise, Error> {
    match action {
        near_action::Action::FunctionCall(action) => Ok(Promise::new(target).function_call(
            action.method_name,
            action.args,
            action.deposit,
            action.gas,
        )),
        near_action::Action::Transfer(action) => Ok(Promise::new(target).transfer(action.deposit)),
```
