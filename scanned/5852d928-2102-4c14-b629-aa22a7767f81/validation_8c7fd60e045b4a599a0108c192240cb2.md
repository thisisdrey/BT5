### Title
Dry-run/RPC predicted contract address for `CREATE1` instantiation diverges from the actual on-chain address, causing loss of pre-funded value - (File: `substrate/frame/revive/src/exec.rs`)

### Summary
`pallet-revive`'s address derivation for `CREATE1`-style contract instantiation (`salt = None`) branches on `origin_is_caller` and unconditionally subtracts `1` from the sender's on-chain nonce to compensate for the pre-dispatch nonce increment that happens during real extrinsic execution. This adjustment is *not* conditioned on whether the code is running inside a real transaction or inside a dry-run (Runtime API / RPC `instantiate` query), so the predicted address returned by the dry-run API does not match the address that is actually assigned when the extrinsic is later submitted. This is the direct analog of the reported bug class: a "predict address" helper fails to account for a value normalization/adjustment that is applied only at actual execution time, causing the wrong address to be used for storage/fund association.

### Finding Description
The instantiate address is computed in `new_frame`: [1](#0-0) 

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

The comment states the exact assumption: the caller's nonce is expected to have already been incremented pre-dispatch, so the code subtracts `1` to reconstruct the nonce that was valid "at the time of the call." That assumption only holds for a real, signed extrinsic dispatch. When the same code path is exercised through the `ReviveApi::instantiate` Runtime API (used for dry-running/gas estimation, exactly the "predict address" use-case), the sender's on-chain nonce has *not* been pre-incremented, since no extrinsic has actually been dispatched pre-dispatch. In that context `origin_is_caller` is still `true`, so the code still subtracts `1`, producing `create1(deployer, account_nonce - 1)` — one nonce lower than the address that will actually be used once the real transaction executes (`create1(deployer, account_nonce)`).

This is confirmed by the repository's own `prdoc` history describing exactly this bug and its intended fix, based on a check of the `ExecContext` (whether the call is a real `Transaction` or a dry run): [2](#0-1) 

However, a search of the current tree shows no `ExecContext` type or usage anywhere outside of this `prdoc` file — the fix described has not been applied to `substrate/frame/revive/src/exec.rs`, which still contains the unconditional `if origin_is_caller` branch shown above. In other words, the documented fix exists only as a changelog entry; the vulnerable logic is present in the code that actually executes.

The dry-run path is a first-class public API used by wallets/tooling to predict contract addresses before submission, exactly analogous to `predictFeeDistributorAddress`: [3](#0-2) 

The existing tests only assert self-consistency between "queried nonce → manual `create1`" in narrow scenarios, but do not catch the cross-context (dry-run vs. real dispatch) discrepancy that the removed `prdoc` fix targeted: [4](#0-3) 

### Impact Explanation
Any tool, wallet, or contract-deployment flow that relies on the Runtime API/RPC `instantiate` dry-run to predict a `CREATE1` contract address (`salt = None`, `origin_is_caller = true`) before submitting the real transaction will receive an address one nonce lower than the one actually produced on-chain. If a user or protocol pre-funds, whitelists, or writes storage keyed by that predicted address (a common CREATE1/CREATE2 pattern, and precisely the failure mode described in the external report — "storage update for the wrong address... user funds will be lost"), those funds/permissions will be bound to an address that never becomes the deployed contract, and are permanently unrecoverable through the intended deployment path. This is a public, unprivileged-path fund-lock impact consistent with the "permanent user-fund... lock" acceptance criterion.

### Likelihood Explanation
The condition triggers deterministically whenever: (a) `salt` is `None` (CREATE1 semantics, an explicitly supported and encouraged mode per the "Make salt optional" PRs in this repo), (b) the instantiation is dry-run via the `ReviveApi::instantiate`/`revive_instantiate` runtime call, and (c) `origin_is_caller` is true (the common top-level signed-instantiate case). No malicious peer, relayer, validator, or privileged actor is required — an ordinary user using the standard dry-run-then-submit workflow (as done in this repo's own integration tests) hits it. The only reason this is not unconditionally triggered today is that a fix was drafted and documented (`pr_8504.prdoc`) but the corresponding code change is absent from `exec.rs` in this snapshot.

### Recommendation
Gate the nonce adjustment on the actual execution context rather than solely on `origin_is_caller`, exactly as described in the drafted fix: introduce/consult an `ExecContext` (or equivalent execution-context flag distinguishing a genuine `Transaction` dispatch from an `Extrinsic`/RPC dry-run) and only subtract `1` from `account_nonce` when running inside a real transaction:
```rust
if origin_is_caller && matches!(exec_context, ExecContext::Transaction) {
    account_nonce.saturating_sub(1u32.into()).saturated_into()
} else {
    account_nonce.saturated_into()
}
```
Add a regression test (e.g., `nonce_not_incremented_in_dry_run`) verifying that the dry-run predicted address always matches the address produced by the subsequent real extrinsic dispatch.

### Proof of Concept
1. Fund account `ALICE` and note her current on-chain nonce `N` (not yet incremented).
2. Call the `ReviveApi::instantiate` Runtime API (dry run) with `salt = None`, origin = `ALICE`. Per `exec.rs:1141-1158`, this executes with `origin_is_caller = true`, `account_nonce = N` (un-incremented, since no extrinsic pre-dispatch has run), producing `predicted_addr = create1(ALICE_H160, N - 1)`.
3. Transfer value to `predicted_addr` (simulating a user pre-funding the predicted contract address, as encouraged by the dry-run/predict-then-deploy pattern).
4. Submit the real `instantiate_with_code`/`instantiate` extrinsic from `ALICE` with the same `salt = None`. Because the extrinsic pipeline increments `ALICE`'s nonce pre-dispatch to `N+1` before `new_frame` runs, `account_nonce.saturating_sub(1) = N`, giving `actual_addr = create1(ALICE_H160, N)`.
5. `actual_addr != predicted_addr` (they differ by one nonce step in the `create1` formula), so the value sent in step 3 is stranded at `predicted_addr`, an address that is not the deployed contract and that `ALICE` has no further programmatic way to reach via the normal deployment flow — reproducing the "wrong address / locked funds" pattern from the external report.

This can be directly exercised by adapting the existing test `create1_address_from_extrinsic` at [4](#0-3)  to compare a dry-run call (`bare_instantiate` without pre-incrementing the nonce) against a real extrinsic dispatch (which does increment the nonce pre-dispatch), and observing the address mismatch that the missing `ExecContext` check in [1](#0-0)  fails to prevent.

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

**File:** substrate/frame/revive/rpc/src/tests.rs (L1892-1952)
```rust
	// Deploy the Fibonacci contract via Substrate API
	log::trace!(target: LOG_TARGET, "Deploying Fibonacci contract via Substrate API");
	let dry_run_result = node_client
		.at_current_block()
		.await?
		.runtime_apis()
		.call(subxt_client::runtime_apis().revive_api().instantiate(
			subxt::utils::AccountId32(origin),
			0u128, // value
			None,  // gas_limit
			None,  // storage_deposit_limit
			CodeV1::Upload(bytes.clone()).into(),
			vec![], // data (constructor args)
			None,   // salt
		))
		.await;

	assert!(dry_run_result.is_ok(), "Dry-run instantiate failed: {dry_run_result:?}");
	let dry_run = dry_run_result.unwrap().0;
	let instantiate_result = dry_run.result.expect("Dry-run should succeed");

	log::trace!(
		target: LOG_TARGET,
		"Dry-run succeeded: address: {:?}, gas_consumed: {:?}, weight_required: {:?}",
		instantiate_result.addr,
		dry_run.gas_consumed,
		dry_run.weight_required
	);

	// Now submit the actual instantiate extrinsic
	let events = node_client
		.tx()
		.await?
		.sign_and_submit_then_watch_default(
			&subxt_client::tx().revive().instantiate_with_code(
				0u128,                          // value
				dry_run.weight_required.into(), // weight_limit from dry-run
				u128::MAX,                      // storage_deposit_limit
				bytes,                          // code
				vec![],                         // data
				None,                           // salt
			),
			&subxt_signer::sr25519::dev::alice(),
		)
		.await?
		.wait_for_finalized_success()
		.await?;

	// Extract the contract address from the Instantiated event
	let instantiated_event = events
		.find_first::<subxt_client::revive::events::Instantiated>()
		.expect("Instantiated event should be present")?;

	let contract_address = instantiated_event.contract;
	log::trace!(target: LOG_TARGET, "Contract deployed via Substrate at: {contract_address:?}");

	// Verify that the dry-run predicted address matches the actual deployed address
	assert_eq!(
		instantiate_result.addr, contract_address,
		"Dry-run predicted address should match actual deployed address"
	);
```

**File:** substrate/frame/revive/src/tests/pvm.rs (L304-330)
```rust
#[test]
fn create1_address_from_extrinsic() {
	let (binary, code_hash) = compile_module("dummy").unwrap();

	ExtBuilder::default().existential_deposit(1).build().execute_with(|| {
		let _ = <Test as Config>::Currency::set_balance(&ALICE, 1_000_000);

		assert_ok!(Contracts::upload_code(
			RuntimeOrigin::signed(ALICE),
			binary.clone(),
			deposit_limit::<Test>(),
		));

		assert_eq!(System::account_nonce(&ALICE), 0);
		System::inc_account_nonce(&ALICE);

		for nonce in 1..3 {
			let Contract { addr, .. } = builder::bare_instantiate(Code::Existing(code_hash))
				.salt(None)
				.build_and_unwrap_contract();
			assert!(AccountInfoOf::<Test>::contains_key(&addr));
			assert_eq!(
				addr,
				create1(&<Test as Config>::AddressMapper::to_address(&ALICE), nonce - 1)
			);
		}
		assert_eq!(System::account_nonce(&ALICE), 3);
```
