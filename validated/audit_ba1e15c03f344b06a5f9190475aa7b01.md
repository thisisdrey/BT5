### Title
Malicious/rebasing NEP-141 ("ERC-20") token can silently drain wallet-contract NEAR balance via the hard-coded `storage_deposit` call - (File: `runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs`)

### Summary
The eth-implicit `WalletContract` emulates ERC-20 `transfer()` calls by mapping them to NEP-141 `ft_transfer`, and automatically pays for NEP-145 storage registration on the receiver's behalf whenever the target "token" reports the receiver as unregistered. This logic trusts the token contract's `storage_balance_of` response and its handling of the attached `storage_deposit`, with no verification that registration actually occurred and no refund path for that specific payment if the batch subsequently fails. A malicious or non-standard token contract placed at the user-chosen `target` account can therefore consume the hard-coded NEAR amount on every attempted transfer without ever registering the receiver, repeatedly extracting value from the wallet's own balance.

### Finding Description
`try_emulation` decodes an Ethereum-ABI `transfer(address,uint256)` call into a NEP-141 `ft_transfer` FunctionCall action [1](#0-0) .

In `inner_rlp_execute`, when the transaction kind is `ERC20Transfer`, the wallet contract does not send the `ft_transfer` action directly. Instead it first calls `storage_balance_of` on the target (arbitrary, relayer/user-selected) account and routes the result to `nep_141_storage_balance_callback` [2](#0-1) .

If that callback observes `None` (receiver "not registered" according to the token's own response), it unconditionally schedules a two-action batch: a `storage_deposit` call carrying a hard-coded `NEP_141_STORAGE_DEPOSIT_AMOUNT` (1.25 milli-NEAR) paid from the wallet's own account balance, followed by the actual transfer call [3](#0-2) . The constant and its rationale ("prevents malicious token contracts with very high `storage_balance_bounds` from taking lots of $NEAR") are declared explicitly, showing the authors anticipated malicious tokens but only bounded — not eliminated — the exposure [4](#0-3) .

Crucially, the code never verifies that `storage_deposit` actually registered the account, and this NEAR payment is not tracked as part of `caller_deposit`, so it is never refunded regardless of outcome. `rlp_execute_callback` only refunds the `caller_deposit` structure (the transfer's own attached `yocto_near`) when the final promise fails; the NEAR spent on `storage_deposit` is gone the moment the batch executes [5](#0-4) . A token contract can therefore: (1) always report `storage_balance_of` as `None` to force this code path on every call, (2) accept the 1.25 milli-NEAR `storage_deposit` payment without registering the account or refunding excess (violating, but not prevented from violating, the NEP-145/NEP-141 standard), and (3) let the following `ft_transfer` fail (e.g., always revert) so the whole batch fails and the process repeats identically on the user's next attempt — each time siphoning `NEP_141_STORAGE_DEPOSIT_AMOUNT` from the eth-implicit account's own NEAR balance with no way for the wallet contract or user to detect or stop it short of never calling that token again.

This is a direct analog of the reported bug class: the protocol (here, the wallet contract's ERC-20 emulation layer) is open to *any* NEP-141-labeled contract chosen through a user-signed Ethereum-style transaction, and assumes standards-compliant, honest balance/registration behavior from that contract. A "malicious ERC20 token" breaks that assumption and extracts value that was never explicitly authorized by the user's signed transaction (the user's signed ETH tx only authorizes the `transfer` call itself and its `yocto_near`/fee fields — not this implicit protocol-level NEAR expenditure).

### Impact Explanation
Each interaction with a malicious/misbehaving token silently and irreversibly transfers real NEAR value (`NEP_141_STORAGE_DEPOSIT_AMOUNT` ≈ 1.25 mN per attempt) out of the user's eth-implicit wallet account to the malicious contract's own balance, with no on-chain accounting that lets the wallet contract, relayer, or user recover it. Because failure just leaves the wallet ready to retry the exact same doomed sequence, an attacker who convinces (or automates) repeated calls can drain the amount many times over. While per-call value is small and capped, this is concrete, non-reversible theft of user funds enabled purely by the protocol's implicit trust in third-party token contracts, matching the "loss of funds" impact class.

### Likelihood Explanation
Likelihood is moderate: it requires a user (or a relayer acting on the user's signed transaction) to interact with a NEP-141 contract that is malicious or simply non-compliant with the standard's registration/refund guarantees. Since `target`/token accounts are effectively open (any deployed NEP-141-labeled account can be targeted through the ETH-emulation path), and users cannot easily audit arbitrary NEAR contracts' `storage_deposit`/`storage_balance_of` implementations from an Ethereum-style signing UI, this is a realistic vector, especially for phishing-style "airdrop"/"fake token" scenarios common in the ERC-20 ecosystem this bridge intentionally emulates.

### Recommendation
- Track the `storage_deposit` payment the same way `caller_deposit` is tracked, and refund it to the wallet/user if the subsequent transfer action in the batch fails.
- After the batch executes, verify (via an additional `storage_balance_of` check or by inspecting `ft_transfer` receipts) that registration and the transfer actually succeeded before treating the deposit as spent.
- Consider capping/bounding via a whitelist or safety check on which NEP-141 contracts are eligible for the automatic storage-deposit convenience path, consistent with the report's recommendation to whitelist trusted tokens, since the current hard-coded-amount mitigation only bounds — but does not prevent — repeated loss.

### Proof of Concept
1. Deploy a NEP-141-labeled malicious contract `M` implementing:
   - `storage_balance_of` → always returns `null`/`None`.
   - `storage_deposit` → accepts any attached deposit, does not register the account, does not refund excess.
   - `ft_transfer` → always panics/fails.
2. A user's eth-implicit wallet contract `W` signs an Ethereum-ABI `transfer(address,uint256)` call whose `to` resolves (via the relayer-supplied `target`) to `M`.
3. `inner_rlp_execute` routes this to the `ERC20Transfer` branch: `storage_balance_of` on `M` returns `None` → `nep_141_storage_balance_callback` schedules `storage_deposit` (1.25 mN paid from `W`'s balance) + `ft_transfer` in one batch [3](#0-2) .
4. `M.storage_deposit` keeps the 1.25 mN; `M.ft_transfer` panics, so the whole receipt fails.
5. `rlp_execute_callback` sees `PromiseResult::Failed` and refunds only the tracked `caller_deposit` (the transfer's tiny `yocto_near`), never the 1.25 mN paid to `M` [5](#0-4) .
6. Repeating steps 2–5 lets `M` collect 1.25 mN from `W` on every attempt, with no refund or detection mechanism in the wallet contract.

### Citations

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/eth_emulation.rs (L59-93)
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
        }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L26-34)
```rust
const MICRO_NEAR: u128 = 10_u128.pow(18);
const ADDRESS_REGISTRAR_ACCOUNT_ID: &str = std::include_str!("ADDRESS_REGISTRAR_ACCOUNT_ID");
/// This storage deposit value is the one used by the standard NEP-141 implementation,
/// which essentially all tokens use. Therefore we hard-code it here instead of doing
/// the extra on-chain call to `storage_balance_bounds`. This also prevents malicious
/// token contracts with very high `storage_balance_bounds` from taking lots of $NEAR
/// from eth-wallet-contract users.
const NEP_141_STORAGE_DEPOSIT_AMOUNT: NearToken = NearToken::from_yoctonear(1_250 * MICRO_NEAR);
const NEP_141_STORAGE_DEPOSIT_GAS: Gas = Gas::from_tgas(5);
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L239-269)
```rust
            None => {
                // receiver_id is not registered so we must call `storage_deposit` first.
                let storage_deposit_args =
                    format!(r#"{{"account_id": "{receiver_id}"}}"#).into_bytes();
                let transfer_function_call = match action {
                    near_action::Action::FunctionCall(x) => x,
                    _ => {
                        return PromiseOrValue::Value(ExecuteResponse {
                            success: false,
                            success_value: None,
                            error: Some(
                                "Expected function call action to perform NEP-141 transfer".into(),
                            ),
                        });
                    }
                };
                Promise::new(token_id)
                    .function_call(
                        "storage_deposit".into(),
                        storage_deposit_args,
                        NEP_141_STORAGE_DEPOSIT_AMOUNT,
                        NEP_141_STORAGE_DEPOSIT_GAS,
                    )
                    .function_call(
                        transfer_function_call.method_name,
                        transfer_function_call.args,
                        transfer_function_call.deposit,
                        transfer_function_call.gas,
                    )
                    .then(ext.rlp_execute_callback(caller_deposit))
            }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L296-317)
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
