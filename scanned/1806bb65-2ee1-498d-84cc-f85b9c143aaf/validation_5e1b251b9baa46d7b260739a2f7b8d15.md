Confirmed: `origin_is_caller` is a plain boolean parameter passed into `new_frame`/`push_frame` (`Stack::new` calls it with `true` for the first/top frame), and it is set purely based on call-stack position, not on execution context. There is no `ExecContext` type or dry-run distinction anywhere in `substrate/frame/revive/src/exec.rs` or elsewhere in the repo — `grep_search` for `ExecContext` only matches the prdoc file, never the actual source.

### Title
Dry-run/runtime-API `create1` address prediction diverges from actual on-chain deployment address due to un-implemented nonce-context fix - (File: `substrate/frame/revive/src/exec.rs`)

### Summary
`pallet-revive`'s `Stack::new_frame` derives the address of a to-be-deployed contract with `address::create1(&deployer, nonce)`, subtracting 1 from the caller's on-chain nonce whenever `origin_is_caller` is true, on the assumption that the nonce was "already incremented pre-dispatch" by `CheckNonce`. [1](#0-0)  This assumption is only true for a *real dispatched transaction*. `prdoc/stable2506/pr_8504.prdoc` documents that this exact bug was found (dry-run/runtime-API predicted address ≠ actual deployed address) and describes a fix that adds an `ExecContext::Transaction` check before applying the `-1` adjustment. [2](#0-1)  However, no such `ExecContext` type or check exists anywhere in the actual `exec.rs` source, and `origin_is_caller` is set unconditionally to `true` for the first frame regardless of whether the call originates from a real dispatched extrinsic or from an off-chain simulation (e.g. `bare_instantiate`, `eth_estimateGas`/`eth_call` style dry-runs, or a `ContractsApi`/`ReviveApi` runtime-API call). [3](#0-2) 

### Finding Description
For a real transaction, `frame_system`'s `CheckNonce` transaction extension increments the signer's nonce before `Stack::new` builds the first frame, so subtracting 1 in `create1` correctly recovers the "nonce at time of call" and produces the address that will actually be used on-chain. [4](#0-3) 

For a dry-run / runtime-API path (used by wallets, block-explorers and CI/tests to predict the contract address before submitting the real transaction, as shown in the test helper `instantiate_contract`, which explicitly reads `caller_revive_nonce` off-chain and calls `pallet_revive::create1` with it to get the address the caller will use to pre-fund/interact with the not-yet-deployed contract), the pre-dispatch nonce increment from `CheckNonce` never runs — the state is queried directly against the current best/at-latest block. [5](#0-4)  Yet `Stack::new` still passes `origin_is_caller = true` unconditionally for that same first frame, so `new_frame` still subtracts 1 from the (not-yet-incremented) nonce, producing `create1(deployer, nonce-1)` instead of the correct `create1(deployer, nonce)` that the actual transaction will use once `CheckNonce` increments the nonce. The `ExecConfig::is_dry_run` / `DryRunConfigurations` field exists and is threaded through `exec.rs` for other purposes (e.g. timestamp override at `Stack::new`, tracing suppression during constructor execution) but is never consulted in the `create1` nonce-subtraction branch. [6](#0-5) [7](#0-6) 

This reproduces the exact bug class from the referenced report: an off-chain/predictive nonce-based address computation silently diverges by exactly one from the address that results from the actual on-chain deployment, because the "pre-increment" assumption baked into the address-derivation code does not hold for every code path that reaches it.

### Impact Explanation
Any caller (wallet, dApp, factory contract front-end, or automated tooling) that relies on `create1`/`ReviveApi::nonce` + dry-run simulation to predict the address of a contract it is about to deploy — in order to, for example, pre-fund the future contract, register it, whitelist it, or hand its address to a third party before the deployment transaction lands — will compute the wrong address. Funds or permissions bound to the mispredicted address are sent to an account that is never the actual deployed contract, causing those funds to become effectively stranded/lost to the true beneficiary (matching the "public underpriced work" / "permanent user-fund lock" impact class: value settles to the wrong beneficiary because the runtime's own dry-run API returns an incorrect commitment for the deployed address). Because the divergence is systemic (every dry-run against the on-chain runtime-API is affected, not a corner case), and the prdoc shows the maintainers themselves classified this as a distinct fix-worthy bug, the impact is consistent with the "wrong beneficiary or amount" / EVM-non-equivalence class the external report was judged Medium for.

### Likelihood Explanation
No privileged actor, malicious peer, relayer, or governance action is required — this is triggered purely by any unprivileged user calling the standard, publicly exposed dry-run/runtime-API instantiate path (`bare_instantiate`, `eth_call`/`eth_estimateGas`-style flows, or any `ReviveApi`/`ContractsApi` state call) followed by a normal signed deployment transaction. The divergence is deterministic and 100% reproducible whenever a first-frame instantiate is dry-run before being dispatched as a real extrinsic, since `origin_is_caller` is always `true` for the first frame and the nonce subtraction is applied unconditionally in that branch. [8](#0-7) 

### Recommendation
Implement the fix actually described in `prdoc/stable2506/pr_8504.prdoc`: thread an execution-context distinction (e.g. an `ExecContext` enum, or reuse the existing `exec_config.is_dry_run` flag) into `Stack::new_frame`, and only subtract 1 from `account_nonce` in the `create1` computation when the call is known to be a real dispatched transaction (i.e., `origin_is_caller && exec_config.is_dry_run.is_none()`), leaving the un-adjusted nonce for dry-run/runtime-API simulation paths so that predicted and actual deployment addresses always match. Add a regression test analogous to the `nonce_not_incremented_in_dry_run()` test mentioned in the prdoc, and verify that `bare_instantiate` / `ReviveApi` code paths pass the correct `is_dry_run` context into `ExecConfig`.

### Proof of Concept
1. Call the runtime API path used for address prediction (mirroring the pattern in `instantiate_contract`): read the caller's current nonce via `ReviveApi::nonce`, then compute `expected = pallet_revive::create1(&caller_h160, nonce)`. [5](#0-4) 
2. Separately, perform a dry-run instantiate through `Contracts::bare_instantiate` (or equivalent `Stack::new` first-frame path) with `salt = None`; because `origin_is_caller = true` is passed unconditionally, `new_frame` computes `address::create1(&deployer, nonce.saturating_sub(1))`, i.e., `create1(deployer, nonce-1)`, which is off by one from `expected`. [1](#0-0) 
3. Submit the real signed `instantiate_with_code` extrinsic; `CheckNonce` increments the account nonce pre-dispatch, so the on-chain deployment now uses `create1(deployer, nonce)` — matching `expected` from step 1 but *not* matching the dry-run address computed in step 2.
4. Any off-chain logic that trusted the dry-run/runtime-API address from step 2 (rather than deriving it the way the existing test suite happens to, via a separately-tracked nonce as in `instantiate_contract`) ends up holding a stale, incorrect address, demonstrating the discrepancy is live in code, not merely fixed and documented in the prdoc.

### Citations

**File:** substrate/frame/revive/src/exec.rs (L1007-1029)
```rust
	fn new(
		args: FrameArgs<T, E>,
		origin: Origin<T>,
		transaction_meter: &'a mut TransactionMeter<T>,
		value: U256,
		exec_config: &'a ExecConfig<T>,
		input_data: &Vec<u8>,
	) -> Result<Option<(Self, ExecutableOrPrecompile<T, E, Self>)>, ExecError> {
		origin.ensure_mapped()?;
		let Some((first_frame, executable)) = Self::new_frame(
			args,
			value,
			transaction_meter,
			&CallResources::NoLimits,
			false,
			true,
			input_data,
			exec_config,
		)?
		else {
			return Ok(None);
		};

```

**File:** substrate/frame/revive/src/exec.rs (L1032-1040)
```rust
		// if dry run with timestamp override is provided we simulate the run in a `pending` block
		if let Some(timestamp_override) =
			exec_config.is_dry_run.as_ref().and_then(|cfg| cfg.timestamp_override)
		{
			block_number = block_number.saturating_add(1u32.into());
			// Delta is in milliseconds; increment timestamp by one second
			let delta = 1000u32.into();
			timestamp = cmp::max(timestamp.saturating_add(delta), timestamp_override);
		}
```

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

**File:** prdoc/stable2506/pr_8504.prdoc (L9-41)
```text
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
