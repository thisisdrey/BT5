### Title
Single blocked/frozen asset in a multi-asset `DepositAsset` aborts and traps the entire batch, denying delivery of otherwise-valid assets to the beneficiary - (File: `polkadot/xcm/xcm-executor/src/lib.rs`)

### Summary
The reported Maia bug is that a redemption which pays out *multiple* tokens to a single, fixed recipient is executed atomically: if the recipient is blocked (blacklisted) for *any one* of the tokens, the whole batch reverts and *all* tokens — including the ones that could have been paid out fine — get stuck. The same broken invariant exists in the XCM executor's `DepositAsset` instruction handling: when a single `DepositAsset` command carries several distinct assets destined for one `beneficiary`, a deposit failure on just one asset (e.g. the beneficiary is `Blocked`/frozen for that one asset in `pallet-assets`) aborts the entire instruction and traps the whole bundle, instead of delivering the assets that could have succeeded.

### Finding Description
`DepositAsset` takes assets out of the holding register and calls `deposit_assets_with_retry`, which tries every asset in the bundle, retries the ones that failed once, and if the retry pass still fails for **any** asset it returns `Err`: [1](#0-0) [2](#0-1) 

Because `DepositAsset` is wrapped in `self.transactional_process(...)`, an `Err` from `deposit_assets_with_retry` rolls back *all* storage side effects of the instruction and restores `self.holding` to its full pre-instruction state — including assets whose deposit had already succeeded in the first pass. The full restored holding is then trapped by `post_process` via `Config::AssetTrap`, rather than delivering the assets that legitimately belong to the beneficiary.

This exact behavior is unit-tested and explicitly documented as intended: [3](#0-2) 

and again in the `InitiateTransfer` test suite: [4](#0-3) 

The failure trigger is any per-asset deposit rejection at the `TransactAsset`/`fungibles_adapter` layer — for `pallet-assets`-backed assets this includes an account that has been `Blocked` (the Substrate analogue of an ERC20 "blacklisted" address) or is `Frozen`, which causes `Assets::resolve`/`can_deposit` to fail and `deposit_asset` to return `Err`: [5](#0-4) 

This code path is directly reachable by cross-chain messages carrying **multiple assets to a single beneficiary in one `DepositAsset { assets: Wild(AllCounted(n)), beneficiary }` instruction** — exactly the multi-token settlement pattern from the original report. Snowbridge V2 inbound processing constructs such XCM programs routinely (WETH + arbitrary ERC-20 + leftover fee assets, all deposited to one beneficiary): [6](#0-5) [7](#0-6) 

### Impact Explanation
When any one of the several assets addressed to `beneficiary` cannot be deposited (blocked/frozen account, missing existential deposit for a fresh account created mid-batch failing on retry, etc.), the entire bundle — not just the offending asset — is diverted from the beneficiary into the `AssetTrap`. For assets arriving from an external message (Snowbridge), the trapping `origin` used by the executor is the message's processing origin (a sovereign/gateway-derived account), not a normal externally-owned key the end user controls, so recovering the trapped bundle through `ClaimAssets`/`claim_assets` is not something an ordinary depositor can straightforwardly do. This matches the "permanent user-fund lock" impact class: legitimate assets that would have settled correctly are withheld from their rightful beneficiary because of one unrelated asset's deposit failure, exactly mirroring the atomic "all funds locked because one token's recipient is blocked" defect from the source report.

### Likelihood Explanation
The precondition — a beneficiary account that is `Blocked` or `Frozen` for one specific asset while other assets in the same batch are fine — is a normal, permissionless, and common administrative state in `pallet-assets` (freezers/admins routinely block or freeze individual asset accounts, e.g. for compliance). No malicious relayer, validator, or governance action is required to *trigger* the loss condition once such a block exists; any inbound multi-asset message that happens to target a beneficiary blocked on one of the bundled assets exercises this path. The behavior is also exercised and confirmed by the repository's own test suite, indicating it is a known, reproducible, deterministic code path rather than a hypothetical one.

### Recommendation
Change `deposit_assets_with_retry` / the `DepositAsset` handler so that a single per-asset deposit failure does not roll back and trap assets that already deposited successfully. Either (a) commit successful per-asset deposits individually (non-atomically) and only trap the assets that actually failed, or (b) keep the current atomic guarantee but surface a clear, generally-actionable claim path (e.g. bind the trap ticket to a location the depositor/beneficiary can practically claim from, or auto-retry to an alternate, unblocked destination) so that non-offending assets are not held hostage by one blocked/frozen asset.

### Proof of Concept
The existing repository test already demonstrates the vulnerable behavior end-to-end: [3](#0-2) 
1. Fund `SENDER` with two assets: `(Here, 5)` (≥ ED, would deposit fine on its own) and `(Parent, 1)` (< ED, fails).
2. Execute `WithdrawAsset([Here:5, Parent:1])` then a single `DepositAsset { assets: Wild(All), beneficiary: RECIPIENT }`.
3. Observe: `vm.bench_process(xcm)` returns `Err` — the whole `DepositAsset` instruction aborts.
4. `post_process` traps **both** assets (`Here:5` and `Parent:1`) even though `Here:5` alone would have deposited successfully — confirmed by `asset_list(TRAPPED_ASSETS) == vec![(Here, 5).into(), (Parent, 1).into()]`.

Substitute the `(Parent, 1)` sub-ED failure with a `pallet-assets` `Blocked` beneficiary account for one of the bundled foreign assets (as happens in real Snowbridge V2 inbound processing carrying WETH + an arbitrary token to one `beneficiary`) and the same all-or-nothing trap occurs, withholding the otherwise-deliverable WETH/fee assets from the beneficiary.

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

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L1853-1892)
```rust
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

**File:** polkadot/xcm/xcm-executor/src/tests/deposit_with_retry.rs (L96-135)
```rust
/// Within a single `DepositAsset` containing multiple assets, a single per-asset failure
/// aborts the whole instruction. The holding-level rollback restores the full
/// pre-instruction holding, and `post_process` then traps it — including assets that
/// would have deposited fine on their own.
///
/// (Note: storage-level effects of the sibling deposits that succeeded in the first pass
/// would be rolled back in production by `Config::TransactionalProcessor`. The mock here
/// uses a no-op `TestTransactionalProcessor`, so we only assert the executor-level
/// invariants — the holding restoration and the trap — not the storage state of the
/// recipient account.)
#[test]
fn partial_deposit_failure_aborts_instruction_and_traps_full_holding() {
	add_asset(SENDER, (Here, 5u128)); // ≥ ED on its own
	add_asset(SENDER, (Parent, 1u128)); // < ED — will fail on retry

	let xcm = Xcm::<TestCall>(vec![
		WithdrawAsset(vec![(Here, 5u128).into(), (Parent, 1u128).into()].into()),
		DepositAsset {
			assets: AssetFilter::Wild(WildAsset::All),
			beneficiary: Location::from(AccountId32 { id: RECIPIENT, network: None }),
		},
	]);

	let (mut vm, weight) = instantiate_executor(SENDER, xcm.clone());

	let err = vm.bench_process(xcm).expect_err(
		"any per-asset deposit failure on the retry pass must abort the whole DepositAsset",
	);
	vm.set_error(Some((err.index, err.xcm_error)));

	let outcome = vm.bench_post_process(weight);
	assert!(
		matches!(outcome, Outcome::Incomplete { .. }),
		"expected Outcome::Incomplete, got {outcome:?}"
	);

	// `post_process` trapped the holding that `transactional_process` restored from the
	// pre-instruction backup — both assets are present.
	assert_eq!(asset_list(TRAPPED_ASSETS), vec![(Here, 5u128).into(), (Parent, 1u128).into()]);
}
```

**File:** polkadot/xcm/xcm-executor/src/tests/initiate_transfer.rs (L223-268)
```rust
#[test]
fn deposit_assets_with_retry_aborts_on_failure_and_post_process_traps() {
	// fund sender
	add_asset(SENDER, (Here, 200u128));

	// sub-ED amount (< ED=2): this `DepositAsset` will fail and abort the program.
	let dust: Asset = (Here, 1u128).into();

	// ≥ED amount: would succeed on its own — but the program aborts at the first error,
	// so this instruction never runs.
	let legit: Asset = (Here, 100u128).into();

	let xcm = Xcm::<TestCall>(vec![
		WithdrawAsset((Here, 101u128).into()),
		DepositAsset { assets: Definite(Assets::from(vec![dust])), beneficiary: RECIPIENT.into() },
		DepositAsset { assets: Definite(Assets::from(vec![legit])), beneficiary: RECIPIENT.into() },
	]);

	let (mut vm, weight) = instantiate_executor(SENDER, xcm.clone());

	let err = vm
		.bench_process(xcm)
		.expect_err("a sub-ED `DepositAsset` must error out and abort the rest of the XCM");
	vm.set_error(Some((err.index, err.xcm_error)));

	let outcome = vm.bench_post_process(weight);
	assert!(
		matches!(outcome, Outcome::Incomplete { .. }),
		"Expected Incomplete, got {:?}",
		outcome
	);

	// Nothing reached the recipient: the failing `DepositAsset` rolled back its own take,
	// and the subsequent `DepositAsset(legit)` was never executed because the program is
	// already in error state.
	let here_assets = asset_list(RECIPIENT);
	assert!(here_assets.is_empty(), "no deposit should reach recipient when program aborts");

	// `post_process` trapped the leftover holding — the full 101 originally withdrawn.
	let trapped = asset_list(TRAPPED_ASSETS);
	assert_eq!(
		trapped,
		vec![(Here, 101u128).into()],
		"the entire pre-instruction holding must be trapped (claimable later), not silently lost"
	);
}
```

**File:** polkadot/xcm/xcm-builder/src/fungibles_adapter.rs (L309-347)
```rust
	fn deposit_asset(
		mut what: AssetsInHolding,
		who: &Location,
		_context: Option<&XcmContext>,
	) -> Result<(), (AssetsInHolding, XcmError)> {
		tracing::trace!(
			target: "xcm::fungibles_adapter",
			?what, ?who,
			"deposit_asset"
		);
		defensive_assert!(what.len() == 1, "Trying to deposit more than one asset!");
		// Check we handle this asset.
		let maybe = what.fungible_assets_iter().next().and_then(|asset| {
			Matcher::matches_fungibles(&asset)
				.map(|(fungibles_id, amount)| (asset.id, fungibles_id, amount))
				.ok()
		});
		let Some((asset_id, fungibles_id, amount)) = maybe else {
			return Err((what, MatchError::AssetNotHandled.into()));
		};
		let Some(who) = AccountIdConverter::convert_location(who) else {
			return Err((what, MatchError::AccountIdConversionFailed.into()));
		};
		let Some(imbalance) = what.fungible.remove(&asset_id) else {
			return Err((what, MatchError::AssetNotHandled.into()));
		};
		// "manually" build the concrete credit and move the imbalance there.
		let mut credit = fungibles::Credit::<AccountId, Assets>::zero(fungibles_id);
		credit.saturating_subsume(imbalance);

		Assets::resolve(&who, credit).map_err(|unspent| {
			tracing::debug!(target: "xcm::fungibles_adapter", ?asset_id, ?who, ?amount, "Failed to deposit asset");
			(
				AssetsInHolding::new_from_fungible_credit(asset_id, Box::new(unspent)),
				XcmError::FailedToTransactAsset("")
			)
		})?;
		Ok(())
	}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L288-311)
```rust
	BridgeHubWestend::execute_with(|| {
		type RuntimeEvent = <BridgeHubWestend as Chain>::RuntimeEvent;
		let instructions = vec![
			RefundSurplus,
			DepositAsset { assets: Wild(AllCounted(2)), beneficiary: beneficiary.clone() },
		];
		let xcm: Xcm<()> = instructions.into();
		let versioned_message_xcm = VersionedXcm::V5(xcm);
		let origin = EthereumGatewayAddress::get();

		let message = Message {
			gateway: origin,
			nonce: 1,
			origin,
			assets,
			payload: Payload::Raw(versioned_message_xcm.encode()),
			claimer: Some(claimer_bytes),
			value: 3_500_000_000_000u128,
			execution_fee: 1_500_000_000_000u128,
			relayer_fee: relayer_reward,
		};

		EthereumInboundQueueV2::process_message(relayer_account.clone(), message).unwrap();

```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L446-450)
```rust
			ExpectTransactStatus(MaybeErrorCode::Success),
			RefundSurplus,
			// try to deposit new token, weth and leftover ether fees to beneficiary.
			DepositAsset { assets: Wild(AllCounted(3)), beneficiary: beneficiary.clone() },
		];
```
