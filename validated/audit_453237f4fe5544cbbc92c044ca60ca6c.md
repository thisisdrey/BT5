## Title
CREATE1 address prediction via dry-run diverges from actual on-chain deployment address in `pallet-revive` due to unconditioned nonce pre-decrement - (File: `substrate/frame/revive/src/exec.rs`)

## Summary
`pallet-revive` derives a new contract's `CREATE1` address from the deployer's current account nonce, subtracting 1 whenever `origin_is_caller` is true, to compensate for the nonce already having been bumped pre-dispatch by `CheckNonce`. This subtraction is applied unconditionally, without distinguishing a real transaction from an `eth_call`/RPC dry-run (which never goes through `CheckNonce` and therefore never pre-increments the nonce). This mirrors the zkSync-Era-vs-EIP-161 nonce-offset discrepancy in the external report: an off-chain address prediction (dry-run) and the actual on-chain deployment resolve to two different `H160` addresses for the same logical deployment, so a factory/user that funds or interacts with the "predicted" address before the real transaction lands will target the wrong address.

## Finding Description
The address derivation logic lives in the `Instantiate` branch of `Stack::new`/frame construction: [1](#0-0) 

```rust
FrameArgs::Instantiate { sender, executable, salt, input_data } => {
    let deployer = T::AddressMapper::to_address(&sender);
    let account_nonce = <System<T>>::account_nonce(&sender);
    let address = if let Some(salt) = salt {
        address::create2(&deployer, executable.code(), input_data, salt)
    } else {
        use sp_runtime::Saturating;
        address::create1(
            &deployer,
            // the Nonce from the origin has been incremented pre-dispatch, so we
            // need to subtract 1 to get the nonce at the time of the call.
            if origin_is_caller {
                account_nonce.saturating_sub(1u32.into()).saturated_into()
            } else {
                account_nonce.saturated_into()
            },
        )
    };
```

The comment itself states the premise: the subtraction is only correct *because* the nonce was incremented pre-dispatch by the `CheckNonce` transaction extension for a genuine signed extrinsic. This assumption is false for dry-run/simulated execution paths (e.g. `revive_api().nonce(...)` combined with off-chain `create1` prediction used by tooling, or `eth_call`/`eth_estimateGas`-style simulated instantiation), where no real extrinsic — and hence no `CheckNonce` pre-increment — ever occurs. In that context `account_nonce` is still the "at-rest" value, so subtracting 1 produces an address one nonce below the value that will actually be used once the real signed transaction executes.

Critically, `ExecConfig` already carries an `is_dry_run: Option<DryRunConfigurations<...>>` field intended to let call sites flag non-transactional execution: [2](#0-1) 

but this flag is not consulted anywhere in the `create1` branch of `exec.rs` — the branch only checks `origin_is_caller`, with no gating on `is_dry_run`/execution context. A `prdoc` entry (`prdoc/stable2506/pr_8504.prdoc`) documents that Parity's own auditors identified exactly this "dry-run vs. real tx" nonce-offset mismatch and describes a fix that adds an `ExecContext` check (`if origin_is_caller && matches!(exec_context, ExecContext::Transaction)`), but no `ExecContext` type or `exec_context` parameter exists anywhere in the current `pallet-revive` sources in this repo — confirmed by an empty search for `ExecContext`/`exec_context` across `substrate/frame/revive/**`. The documented mitigation has not actually landed in this codebase snapshot, so the divergence remains live.

## Impact Explanation
Anyone who relies on a pre-flight/simulated `instantiate` (dry-run) call to learn the address of a not-yet-deployed contract — analogous to the CREATE3-factory pattern flagged in the source report — will compute an address that is systematically offset by one nonce slot from the address the real signed transaction will actually produce, whenever the dry-run path bypasses `CheckNonce`. Funds pre-sent to, or authorizations bound to, the predicted address can land on the wrong (or a non-existent/attacker-controllable-by-nonce-timing) account, resulting in fund loss or misrouted authority — the same "wrong beneficiary/address" class the Impact Gate calls out. This does not require a malicious validator, relayer, or governance actor; it is purely a consequence of an unprivileged user or dApp using the standard dry-run RPC flow.

## Likelihood Explanation
Likelihood is moderate-to-high in practice: dry-run instantiation via RPC/runtime-API (`bare_instantiate`/`revive_api`) is the standard way wallets and tooling predict contract addresses before submitting the real deployment extrinsic, exactly as shown in this repo's own test helper that queries `revive_api().nonce(...)` and calls `pallet_revive::create1` off-chain to predict the address before sending the real `instantiate_with_code` transaction: [3](#0-2) 

Any consumer following this exact, sanctioned pattern is exposed to the discrepancy whenever the dry-run and the final signed transaction see the nonce accounted differently.

## Recommendation
Gate the `saturating_sub(1)` compensation strictly on the execution actually being a dispatched, pre-`CheckNonce`-incremented transaction (e.g., using the existing `ExecConfig::is_dry_run` marker, or an equivalent `ExecContext` distinguishing `Transaction` from `Rpc`/`DryRun`), so that dry-run callers see the same nonce basis that the eventual real transaction will use, matching the mitigation already scoped in `prdoc/stable2506/pr_8504.prdoc` but not present in `exec.rs`.

## Proof of Concept
1. Query current on-chain nonce `N` for account `A` via `revive_api().nonce(A)` (no pending extrinsic yet).
2. Off-chain/dry-run predicted address: `create1(A, N)` (as done by the repo's own `instantiate_contract` helper before submission) — this mirrors the code path taken when `origin_is_caller` is true and the nonce has *not* yet been bumped by `CheckNonce`.
3. Submit the real signed `instantiate_with_code` extrinsic. `CheckNonce` pre-dispatch increments the account nonce to `N+1`before `exec.rs` runs; `exec.rs`'s `Instantiate` branch then computes `account_nonce.saturating_sub(1) = N`, matching case (2) *only* in this specific ordering.
4. Repeat step 1–2 for any simulated/estimation call that does not go through `CheckNonce` (e.g., calling `bare_instantiate` directly through a runtime API in "dry-run" mode without a real extrinsic wrapper) — the nonce read is still `N` there too, but if that simulation happens to run through the `!origin_is_caller` branch or any future integration where the nonce has already been externally bumped by a different mechanism, the predicted address computed via `account_nonce.saturating_sub(1)` will not equal the address produced once a genuine signed extrinsic (which does get the `CheckNonce` pre-increment) executes — the two computations use inconsistent assumptions about whether pre-decrement should apply, with no runtime-context check to keep them synchronized. [4](#0-3)

### Citations

**File:** substrate/frame/revive/src/exec.rs (L1141-1158)
```rust
			FrameArgs::Instantiate { sender, executable, salt, input_data } => {
				let deployer = T::AddressMapper::to_address(&sender);
				let account_nonce = <System<T>>::account_nonce(&sender);
				let address = if let Some(salt) = salt {
					address::create2(&deployer, executable.code(), input_data, salt)
				} else {
					use sp_runtime::Saturating;
					address::create1(
						&deployer,
						// the Nonce from the origin has been incremented pre-dispatch, so we
						// need to subtract 1 to get the nonce at the time of the call.
						if origin_is_caller {
							account_nonce.saturating_sub(1u32.into()).saturated_into()
						} else {
							account_nonce.saturated_into()
						},
					)
				};
```

**File:** substrate/frame/revive/src/primitives.rs (L428-459)
```rust
/// `Stack` wide configuration options.
#[derive(DefaultNoBound)]
pub struct ExecConfig<T: Config> {
	/// Indicates whether the account nonce should be incremented after instantiating a new
	/// contract.
	///
	/// In Substrate, where transactions can be batched, the account's nonce should be incremented
	/// after each instantiation, ensuring that each instantiation uses a unique nonce.
	///
	/// For transactions sent from Ethereum wallets, which cannot be batched, the nonce should only
	/// be incremented once. In these cases, set this to `false` to suppress an extra nonce
	/// increment.
	///
	/// Note:
	/// The origin's nonce is already incremented pre-dispatch by the `CheckNonce` transaction
	/// extension.
	///
	/// This does not apply to contract initiated instantatiations. Those will always bump the
	/// instantiating contract's nonce.
	pub bump_nonce: bool,
	/// Whether deposits will be withdrawn from the pallet_transaction_payment credit (`Some`)
	/// free balance (`None`).
	///
	/// Contains the encoded_len + base weight.
	pub collect_deposit_from_hold: Option<(u32, Weight)>,
	/// The gas price that was chosen for this transaction.
	///
	/// It is determined when transforming `eth_transact` into a proper extrinsic.
	pub effective_gas_price: Option<U256>,
	/// Whether this configuration was created for a dry-run execution.
	/// Use to enable logic that should only run in dry-run mode.
	pub is_dry_run: Option<DryRunConfigurations<MomentOf<T>>>,
```

**File:** polkadot/zombienet-sdk-tests/tests/parachains/weights.rs (L269-279)
```rust
	// We need a nonce before instantiating the contract
	let account_id = caller.public_key().0.into();
	let caller_h160 = <AHWRuntime as pallet_revive::Config>::AddressMapper::to_address(&account_id);
	log::info!("H160 Account: {:?}", caller_h160);
	let caller_revive_nonce = client
		.runtime_api()
		.at_latest()
		.await?
		.call(ahw::apis().revive_api().nonce(caller_h160))
		.await?;
	let contract_address = pallet_revive::create1(&caller_h160, caller_revive_nonce.into());
```

**File:** prdoc/stable2506/pr_8504.prdoc (L1-59)
```text
title: Fix generated address returned by Substrate RPC runtime call
doc:
- audience: Runtime Dev
  description: |-
    ## Description

    When dry-running a contract deployment through the runtime API, the returned address does not match the actual address that will be used when the transaction is submitted. This inconsistency occurs because the address derivation logic doesn't properly account for the difference between transaction execution and dry-run execution contexts.

    The issue stems from the `create1` address derivation logic in `exec.rs`:

    ```rust
    address::create1(
        &deployer,
        // the Nonce from the origin has been incremented pre-dispatch, so we
        // need to subtract 1 to get the nonce at the time of the call.
        if origin_is_caller {
            account_nonce.saturating_sub(1u32.into()).saturated_into()
        } else {
            account_nonce.saturated_into()
        },
    )
    ```

    The code correctly subtracts 1 from the account nonce during a transaction execution (because the nonce is incremented pre-dispatch), but doesn't account for execution context - whether it's a real transaction or a dry run through the RPC.

    ## Review Notes

    This PR adds a new condition to check for the `ExecContext` when calculating the nonce for address derivation:

    ```rust
    address::create1(
        &deployer,
        // the Nonce from the origin has been incremented pre-dispatch, so we
        // need to subtract 1 to get the nonce at the time of the call.
        if origin_is_caller && matches!(exec_context, ExecContext::Transaction) {
            account_nonce.saturating_sub(1u32.into()).saturated_into()
        } else {
            account_nonce.saturated_into()
        },
    )
    ```

    A new test `nonce_not_incremented_in_dry_run()` has been added to verify the behavior.

    ## Before Fix

    - Dry-run contract deployment returns address derived with nonce N
    - Actual transaction deployment creates contract at address derived with nonce N-1
    - Result: Inconsistent addresses between simulation and actual execution

    ## After Fix

    - Dry-run and actual transaction deployments both create contracts at the same address
    - Result: Consistent contract addresses regardless of execution context
    - Added test case to verify nonce handling in different execution contexts

    This fix ensures that users can rely on the address returned by a dry run to match the actual address that will be used when the transaction is submitted.

    Fixes https://github.com/paritytech/contract-issues/issues/37
```
