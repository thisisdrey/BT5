Found the exact analog. The XCM executor's `DepositAsset` instruction exhibits precisely the same bug class as the C4 report: a single failing asset in a multi-asset deposit aborts the *entire* instruction, blocking delivery of all other (unrelated, otherwise-valid) assets in the same batch — and this is explicitly documented/tested behavior in the repo itself.### Title
Single Poisoned Asset in a Batched `DepositAsset` Aborts and Traps All Other Legitimate Assets in the Same XCM Program - ([File: polkadot/xcm/xcm-executor/src/lib.rs])

### Summary
`DepositAsset { assets, beneficiary }` is executed via `transactional_process`, which calls `Self::deposit_assets_with_retry` over the *whole* filtered asset set in one shot. If any single asset in that set fails to deposit into the beneficiary (e.g. an asset that can't satisfy ED for a fresh account, a `sufficient=false`/frozen asset, or a foreign asset lacking a sufficient-asset landing pad), the retry pass returns `Err`, `transactional_process` rolls back the *entire* instruction, and the whole pre-instruction holding — including assets that would have deposited fine on their own — is discarded from the program and later trapped by `AssetTrap::drop_assets`. This mirrors the reported Solidity bug class exactly: one bad reward-token transfer poisons the whole batch and blocks payout of unrelated, otherwise-valid funds within the same call.

### Finding Description
`DepositAsset` in the XCM executor is implemented as: [1](#0-0) 
which calls `deposit_assets_with_retry`, whose own doc comment states the behavior explicitly: [2](#0-1) 

The function does a first pass over every per-asset holding, collecting failures into `failed_deposits`, then does a retry pass that **short-circuits on the very first error** with `?`. That means:
- If asset A (say, a native fee asset) and asset B (say, a reserve-transferred token controlled/frozen at the destination) are both included in one `DepositAsset` filter, and B fails permanently on retry (not a transient "account doesn't exist yet" issue but a hard failure — e.g. B is a non-sufficient asset and the beneficiary account has no ED-satisfying balance from any asset, or B's issuer implements a transfer filter/freeze), the retry loop returns `Err` for B.
- This error propagates out of `deposit_assets_with_retry`, which propagates out of the closure passed to `transactional_process`, causing the **entire instruction to roll back** — including asset A, which had already been successfully deposited in the first pass (its storage effects get reverted by `Config::TransactionalProcessor`, and `self.holding` is restored to its pre-instruction state).
- The rolled-back holding is not silently destroyed, but it is removed from program execution and handed to `AssetTrap::drop_assets` at `post_process`, becoming "trapped" — requiring a separate manual `ClaimAsset` recovery by whoever controls the origin, rather than being delivered as intended.

This is functionally identical to the reported bug: `withdrawTaxes()` looped over `pool.rewardTokens[i]` and any single reverting `transfer` call aborted the whole transaction, blocking withdrawal of all other reward tokens. Here, the XCM executor loops over per-asset holdings within one `DepositAsset`, and a single non-transient per-asset failure aborts delivery of every other asset bundled in the same instruction.

### Impact Explanation
This falls under "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" and "public underpriced work that degrades block production or stalls bridge processing" pivots: a cross-chain program (e.g. a `InitiateTransfer`/reserve-transfer carrying multiple assets to one beneficiary, as used throughout the Snowbridge/AssetHub flows shown in `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs`) can have its entire multi-asset settlement blocked and diverted into the asset-trap mechanism by the presence of one asset that cannot be deposited to the beneficiary, even though other bundled assets (e.g. DOT/WETH fee or principal amounts) were individually depositable. Funds are not permanently lost (traps are claimable), but delivery is denied/delayed and requires a privileged/aware party to issue a follow-up `ClaimAsset`, which is a form of public underpriced/DoS-style disruption of intended settlement and a deviation from "settle exactly once to the rightful beneficiary and amount" for the assets that should have succeeded.

### Likelihood Explanation
The repository's own test suite treats this as expected, by-design behavior rather than a defect, which lowers the likelihood this is considered a "bug" by maintainers — the tests (`deposit_assets_with_retry_aborts_on_failure_and_post_process_traps`, `deposit_assets_with_retry_all_failures_aborts_and_traps`, and the analogous test in `deposit_with_retry.rs`) explicitly assert that a single per-asset failure aborts the whole `DepositAsset` and traps everything, framed as a safety property (nothing is silently lost). This significantly weakens the case that it is an unintended vulnerability versus a known, accepted trade-off of the transactional/holding model. I flag this with lower confidence than a typical fresh finding.

### Recommendation
If this is to be treated as a defect: change `DepositAsset` (and `deposit_assets_with_retry`) to process each asset independently with its own transactional scope, so a permanent failure on one asset traps only that asset and still allows delivery of the others, rather than rolling back the whole instruction. This mirrors the C4 report's recommended mitigation of scoping withdrawal/payout to the granularity of the individual item rather than the whole batch.

### Proof of Concept
Repository test demonstrating the exact mechanics (already present in-repo, confirming the behavior): [3](#0-2) 
Here, `d1` (sub-ED, permanently undepositable to a fresh `RECIPIENT`) and `d2` (sub-ED, permanently undepositable to a fresh `RECIPIENT2`) are in two separate `DepositAsset` instructions to two different beneficiaries, and the first failure aborts the whole program, trapping assets meant for the second, unrelated beneficiary — demonstrating that a single non-recoverable per-asset failure denies settlement to all other legitimate parties bundled in the same XCM.

Given the maintainers' explicit test-level endorsement of this behavior as intentional (not accidental), I present this with the caveat that it may be judged out-of-scope as "working as designed" rather than a genuine vulnerability.

### Citations

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L1191-1202)
```rust
			DepositAsset { assets, beneficiary } => {
				self.transactional_process(|self_ref| {
					let deposited = self_ref.holding.saturating_take(assets);
					let surplus = Self::deposit_assets_with_retry(
						deposited,
						&beneficiary,
						Some(&self_ref.context),
					)?;
					self_ref.total_surplus.saturating_accrue(surplus);
					Ok(())
				})
			},
```

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L1837-1892)
```rust
	/// Deposit `to_deposit` assets to `beneficiary`, without giving up on the first (transient)
	/// error, and retrying once just in case one of the subsequently deposited assets satisfy some
	/// requirement.
	///
	/// Most common transient error is: `beneficiary` account does not yet exist and the first
	/// asset(s) in the (sorted) list does not satisfy ED, but a subsequent one in the list does.
	///
	/// Any per-asset failure on the retry pass propagates as `Err`, and the surrounding
	/// `transactional_process` rolls back the whole instruction (storage changes are reverted by
	/// `Config::TransactionalProcessor`, and `self.holding` is restored from its
	/// pre-instruction backup). Anything left in `self.holding` after the program finishes is
	/// then trapped by `post_process` via `Config::AssetTrap::drop_assets`, so funds are never
	/// silently lost.
	///
	/// This function can write into storage and also return an error at the same time, it should
	/// always be called within a transactional context.
	fn deposit_assets_with_retry(
		to_deposit: AssetsInHolding,
		beneficiary: &Location,
		context: Option<&XcmContext>,
	) -> Result<Weight, XcmError> {
		let mut total_surplus = Weight::zero();
		let mut failed_deposits = AssetsInHolding::new();

		// First pass: try to deposit each asset; failures go to retry.
		for single in to_deposit.into_per_asset_holdings() {
			match Config::AssetTransactor::deposit_asset_with_surplus(single, beneficiary, context)
			{
				Ok(surplus) => total_surplus.saturating_accrue(surplus),
				Err((unspent, _)) => {
					// First-pass failure: keep for retry. A subsequent deposit in the same
					// pass may create the destination account (by satisfying ED), allowing
					// the retry pass to succeed for assets that fall here.
					failed_deposits.subsume_assets(unspent);
				},
			}
		}

		// Retry previously failed deposits, this time short-circuiting on any error.
		for single in failed_deposits.into_per_asset_holdings() {
			let surplus =
				Config::AssetTransactor::deposit_asset_with_surplus(single, beneficiary, context)
					.map_err(|(unspent, error)| {
					tracing::debug!(
						target: "xcm::deposit_assets_with_retry",
						?error,
						?unspent,
						"Retry-pass deposit failed"
					);
					error
				})?;
			total_surplus.saturating_accrue(surplus);
		}

		Ok(total_surplus)
	}
```

**File:** polkadot/xcm/xcm-executor/src/tests/initiate_transfer.rs (L270-301)
```rust
#[test]
fn deposit_assets_with_retry_all_failures_aborts_and_traps() {
	// fund sender
	add_asset(SENDER, (Here, 20u128));

	// two sub-ED amounts, both < ED=2
	let d1: Asset = (Here, 1u128).into();
	let d2: Asset = (Here, 1u128).into();

	let xcm = Xcm::<TestCall>(vec![
		// withdraw 1+1 so the withdraw itself succeeds.
		WithdrawAsset((Here, (1u128 + 1u128)).into()),
		DepositAsset { assets: Definite(Assets::from(vec![d1])), beneficiary: RECIPIENT.into() },
		DepositAsset { assets: Definite(Assets::from(vec![d2])), beneficiary: RECIPIENT2.into() },
	]);

	let (mut vm, weight) = instantiate_executor(SENDER, xcm.clone());
	let err = vm.bench_process(xcm).expect_err("first sub-ED `DepositAsset` must abort");
	vm.set_error(Some((err.index, err.xcm_error)));

	let outcome = vm.bench_post_process(weight);
	assert!(matches!(outcome, Outcome::Incomplete { .. }));

	// Neither recipient sees anything.
	assert!(asset_list(RECIPIENT).is_empty(), "sub-ED deposit can't reach a fresh recipient");
	assert!(asset_list(RECIPIENT2).is_empty(), "second instruction never runs after the abort");

	// `post_process` trapped the entire restored holding (2 from the original
	// `WithdrawAsset`).
	let trapped = asset_list(TRAPPED_ASSETS);
	assert_eq!(trapped, vec![(Here, 2u128).into()], "undeposited assets must be trapped, not lost");
}
```
