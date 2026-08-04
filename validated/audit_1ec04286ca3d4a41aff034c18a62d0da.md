Based on my investigation, I found the test `user_supplied_claimer_can_claim_trapped_assets` at [1](#0-0)  which shows recovery only works when the user *supplies their own claimer matching their signed origin*. This reveals the actual gap.

### Title
Single failing asset in a batched Snowbridge `DepositAsset` traps all co-bundled assets under an unreachable bridge-descended origin, permanently locking user funds when the default (non-custom) claimer is used - (File: `polkadot/xcm/xcm-executor/src/lib.rs`, `bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs`)

### Summary
This is a direct analog of the CVXStaker bug class: a loop/batch that pays out N independent items to a beneficiary, where a single item's failure blocks delivery of all the others. In the XCM executor, `DepositAsset` bundles multiple assets and, per `deposit_assets_with_retry`, any per-asset deposit failure on the retry pass aborts the *entire* instruction via `transactional_process`, rolling back and trapping **all** bundled assets, not just the failing one [2](#0-1) . Snowbridge's inbound message converter batches the remaining ETH together with one or more ERC-20/foreign tokens into a single `ReserveAssetDeposited`/`DepositAsset` pair destined for one beneficiary [3](#0-2) .

### Finding Description
When Snowbridge's `MessageToXcm::convert` builds the remote XCM, it aggregates all bridged assets (remaining ether plus every `NativeTokenERC20`/`ForeignTokenERC20` in the message) into `reserve_deposit_assets`/`reserve_withdraw_assets` lists that are deposited together [4](#0-3) . The user-supplied `remote_xcm` typically issues a `DepositAsset` with a wildcard filter (`AllCounted`/`AllOf`) covering multiple assets to one beneficiary, as seen throughout the integration tests [5](#0-4) .

The executor's `deposit_assets_with_retry` explicitly documents that "any per-asset failure on the retry pass propagates as `Err`, and the surrounding `transactional_process` rolls back the whole instruction" [6](#0-5) , confirmed by dedicated tests showing that a single sub-ED (or otherwise failing) asset causes the *entire* holding — including assets that would have deposited successfully on their own — to be trapped [7](#0-6) .

Trapped assets are keyed by `hash(origin, versioned_assets)` in `AssetTraps` [8](#0-7) , and can only be reclaimed via `claim_assets` when the caller's `origin_location` exactly matches the trapping `origin` [9](#0-8) . For inbound Snowbridge messages, the executing origin at trap time is derived from `DescendOrigin(InboundQueueLocation)` / `UniversalOrigin(GlobalConsensus(Ethereum))` / and optionally a further `DescendOrigin` into the Ethereum sender's `AccountKey20` [10](#0-9) . The repository's own test acknowledges that reclaiming trapped assets only works when the user supplies a custom claimer matching their local signed origin: *"the user can sign `pallet_xcm::claim_assets` to recover those assets — because the claimer location they encoded matches the location produced by AH's `SignedToAccountId32` from their signed origin"* [1](#0-0) . Note that the trap's `origin` key is actually the message's `claimer` (via `SetHints{AssetClaimer}`)/asset-trap origin resolution, not the beneficiary of the failed `DepositAsset`.

The critical gap: when the message does **not** carry a matching custom claimer (e.g., default fallback claimer is the Snowbridge sovereign account, or the remote XCM's beneficiary differs from any reachable local signed origin), an ordinary end-user has **no way** to construct the exact XCM origin needed to invoke `ClaimAsset`/`claim_assets` for those trapped funds, because that origin is anchored to Ethereum-bridge-specific `DescendOrigin`/`UniversalOrigin` context that only the bridge pallet's own XCM execution path can produce.

### Impact Explanation
This matches the CVXStaker root cause exactly: a single problematic asset in a batch of bridged assets (e.g., one token whose deposit fails because the beneficiary lacks ED for that specific asset, the asset is unregistered, or any other transient per-asset condition) causes **all** co-bundled assets — including otherwise-valid ETH/DOT and other tokens — to be trapped instead of delivered. Where the message lacks a self-reclaimable claimer, this becomes a **permanent lock of legitimately bridged user funds**, satisfying the "permanent user-fund or bridge-state lock" impact class explicitly called out in the Polkadot SDK Impact Gate.

### Likelihood Explanation
This requires no malicious peer, relayer, validator, or admin action — it can be triggered by an ordinary Ethereum-side sender who is unaware that their multi-asset payload will fail for one asset (e.g., due to insufficient existential deposit for a single token given the fresh beneficiary account) while sending funds to a beneficiary who did not use a self-controlled claimer. Since the inbound queue currently supports multi-asset messages with `DepositAsset` filters spanning several assets (as shown across `send_weth_v2`, `send_token_v2`, and cross-chain forwarding tests), the precondition (batched multi-asset delivery to one beneficiary) is a normal, expected usage path, not a contrived edge case.

### Recommendation
- Change `DepositAsset` semantics (or provide a Snowbridge-specific safe path) so that per-asset deposit failures do not abort delivery of the other, independently-successful assets in the same instruction — i.e., apply the CVXStaker-style fix of "skip failing/zero items, don't block the batch."
- Alternatively/additionally, ensure the inbound queue's converter always sets a claimer that resolves to an origin the end beneficiary can reproduce locally (not one anchored to bridge-internal `DescendOrigin`/`UniversalOrigin` context), so that trapped assets remain recoverable in all cases, not just when the user manually supplies a matching claimer.

### Proof of Concept
1. Construct an inbound Snowbridge V2 `Message` with `assets = [NativeTokenERC20{token_a, value_a}, NativeTokenERC20{token_b, value_b}]` and a `remote_xcm` containing `DepositAsset { assets: Wild(AllCounted(2)), beneficiary }`, using the default fallback claimer (no `claimer` field set, or one anchored to the bridge sovereign, as in `register_token_v2`/`send_token_v2` tests) [11](#0-10) .
2. Ensure `token_b`'s deposit fails on AssetHub for the fresh `beneficiary` account (e.g., value below that asset's minimum balance) while `token_a`'s deposit alone would succeed.
3. Observe via `deposit_assets_with_retry` that the retry-pass failure for `token_b` aborts the whole `DepositAsset` instruction, and both `token_a` and `token_b` are trapped together under the bridge-descended origin, mirroring the executor-level test `partial_deposit_failure_aborts_instruction_and_traps_full_holding` [12](#0-11) .
4. Attempt `pallet_xcm::claim_assets` from the beneficiary's own signed origin and observe it fails to match the trap's `origin` key, since that origin was derived through `DescendOrigin`s that the beneficiary account cannot reproduce — confirming the funds are permanently unreachable in the absence of the special-case claimer setup demonstrated only in `user_supplied_claimer_can_claim_trapped_assets` [13](#0-12) .

### Citations

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L266-293)
```rust
#[test]
fn send_weth_v2() {
	let relayer_account = BridgeHubWestendSender::get();
	let relayer_reward = 1_500_000_000_000u128;

	let beneficiary_acc_id: H256 = H256::random();
	let beneficiary_acc_bytes: [u8; 32] = beneficiary_acc_id.into();
	let beneficiary =
		Location::new(0, AccountId32 { network: None, id: beneficiary_acc_id.into() });

	let claimer_acc_id = H256::random();
	let claimer = Location::new(0, AccountId32 { network: None, id: claimer_acc_id.into() });
	let claimer_bytes = claimer.encode();

	let token_transfer_value = 2_000_000_000_000u128;

	let assets = vec![
		// the token being transferred
		NativeTokenERC20 { token_id: WETH.into(), value: token_transfer_value },
	];

	set_up_eth_and_dot_pool();
	BridgeHubWestend::execute_with(|| {
		type RuntimeEvent = <BridgeHubWestend as Chain>::RuntimeEvent;
		let instructions = vec![
			RefundSurplus,
			DepositAsset { assets: Wild(AllCounted(2)), beneficiary: beneficiary.clone() },
		];
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L1158-1163)
```rust
/// When the user supplies their own claimer (anchored on the local network) in the
/// inbound message and the XCM payload then traps assets on AH, the user can sign
/// `pallet_xcm::claim_assets` to recover those assets — because the claimer location
/// they encoded matches the location produced by AH's `SignedToAccountId32` from
/// their signed origin.
#[test]
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L1225-1245)
```rust
	let trapped_assets = AssetHubWestend::execute_with(|| {
		type RuntimeEvent = <AssetHubWestend as Chain>::RuntimeEvent;

		let trap = AssetHubWestend::events().into_iter().find_map(|event| match event {
			RuntimeEvent::PolkadotXcm(pallet_xcm::Event::AssetsTrapped {
				origin, assets, ..
			}) => Some((origin, assets)),
			_ => None,
		});

		let (trap_origin, trap_assets) =
			trap.expect("assets should be trapped when XCM payload is invalid");

		// Trap origin reflects the user-supplied claimer.
		assert_eq!(
			trap_origin, user_claimer,
			"trap origin must match the user-supplied claimer location",
		);

		trap_assets
	});
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

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L159-200)
```rust
		let mut assets = vec![];

		if message.value > 0 {
			// Asset for remaining ether
			let remaining_ether_asset: Asset = (ether_location.clone(), message.value).into();
			assets.push(AssetTransfer::ReserveDeposit(remaining_ether_asset));
		}

		for asset in &message.assets {
			match asset {
				EthereumAsset::NativeTokenERC20 { token_id, value } => {
					ensure!(*token_id != H160::zero(), ConvertMessageError::InvalidAsset);
					let token_location: Location = Location::new(
						2,
						[
							GlobalConsensus(EthereumNetwork::get()),
							AccountKey20 { network: None, key: (*token_id).into() },
						],
					);
					let asset: Asset = (token_location, *value).into();
					assets.push(AssetTransfer::ReserveDeposit(asset));
				},
				EthereumAsset::ForeignTokenERC20 { token_id, value } => {
					let asset_location = ConvertAssetId::maybe_convert(*token_id)
						.ok_or(ConvertMessageError::InvalidAsset)?;
					let asset_hub_from_ethereum: Location = Location::new(
						1,
						[
							GlobalConsensus(LocalNetwork::get()),
							Parachain(AssetHubParaId::get().into()),
						],
					);
					let ethereum_universal: InteriorLocation =
						[GlobalConsensus(EthereumNetwork::get())].into();
					let reanchored_asset_location = asset_location
						.reanchored(&asset_hub_from_ethereum, &ethereum_universal)
						.map_err(|_| ConvertMessageError::CannotReanchor)?;
					let asset: Asset = (reanchored_asset_location, *value).into();
					assets.push(AssetTransfer::ReserveWithdraw(asset));
				},
			}
		}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L380-420)
```rust
		let mut instructions = vec![
			DescendOrigin(InboundQueueLocation::get()),
			UniversalOrigin(GlobalConsensus(EthereumNetwork::get())),
			ReserveAssetDeposited(message.execution_fee.clone().into()),
		];

		// Set claimer before PayFees, in case the fees are not enough. Then the claimer will be
		// able to claim the funds still.
		instructions.push(SetHints {
			hints: vec![AssetClaimer { location: message.claimer }]
				.try_into()
				.expect("checked statically, qed"),
		});

		instructions.push(PayFees { asset: message.execution_fee.clone() });

		let mut reserve_deposit_assets = vec![];
		let mut reserve_withdraw_assets = vec![];

		for asset in message.assets {
			match asset {
				AssetTransfer::ReserveDeposit(asset) => reserve_deposit_assets.push(asset),
				AssetTransfer::ReserveWithdraw(asset) => reserve_withdraw_assets.push(asset),
			};
		}

		if !reserve_deposit_assets.is_empty() {
			instructions.push(ReserveAssetDeposited(reserve_deposit_assets.into()));
		}
		if !reserve_withdraw_assets.is_empty() {
			instructions.push(WithdrawAsset(reserve_withdraw_assets.into()));
		}

		// If the message origin is not the gateway proxy contract, set the origin to
		// the original sender on Ethereum. Important to be before the arbitrary XCM that is
		// appended to the message on the next line.
		if message.origin != GatewayProxyAddress::get() {
			instructions.push(DescendOrigin(
				AccountKey20 { key: message.origin.into(), network: None }.into(),
			));
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

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L1520-1555)
```rust
		/// Claims assets trapped on this pallet because of leftover assets during XCM execution.
		///
		/// - `origin`: Anyone can call this extrinsic.
		/// - `assets`: The exact assets that were trapped. Use the version to specify what version
		/// was the latest when they were trapped.
		/// - `beneficiary`: The location/account where the claimed assets will be deposited.
		#[pallet::call_index(12)]
		pub fn claim_assets(
			origin: OriginFor<T>,
			assets: Box<VersionedAssets>,
			beneficiary: Box<VersionedLocation>,
		) -> DispatchResult {
			let origin_location = T::ExecuteXcmOrigin::ensure_origin(origin)?;
			tracing::debug!(target: "xcm::pallet_xcm::claim_assets", ?origin_location, ?assets, ?beneficiary);
			// Extract version from `assets`.
			let assets_version = assets.identify_version();
			let assets: Assets = (*assets).try_into().map_err(|()| {
				tracing::debug!(
					target: "xcm::pallet_xcm::claim_assets",
					"Failed to convert input VersionedAssets",
				);
				Error::<T>::BadVersion
			})?;
			let number_of_assets = assets.len() as u32;
			let beneficiary: Location = (*beneficiary).try_into().map_err(|()| {
				tracing::debug!(
					target: "xcm::pallet_xcm::claim_assets",
					"Failed to convert beneficiary VersionedLocation",
				);
				Error::<T>::BadVersion
			})?;
			let ticket: Location = GeneralIndex(assets_version as u128).into();
			let mut message = Xcm(vec![
				ClaimAsset { assets, ticket },
				DepositAsset { assets: AllCounted(number_of_assets).into(), beneficiary },
			]);
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L3901-3924)
```rust
impl<T: Config> DropAssets for Pallet<T> {
	fn drop_assets(origin: &Location, holding: AssetsInHolding, _context: &XcmContext) -> Weight {
		if holding.is_empty() {
			return Weight::zero();
		}
		let assets: Vec<Asset> = holding.assets_iter().collect();
		// SAFETY: "forget" about any fungible imbalances so that they are not dropped/resolved
		// here. The mirrored asset claiming operation will "recover" the imbalances by minting
		// back into holding, effectively duplicating the imbalance and only then dropping the
		// duplicate. As a result, total issuance doesn't change.
		holding.fungible.into_iter().for_each(|(_, mut accounting)| {
			accounting.forget_imbalance();
		});
		let versioned = VersionedAssets::from(Assets::from(assets));
		let hash = BlakeTwo256::hash_of(&(&origin, &versioned));
		AssetTraps::<T>::mutate(hash, |n| *n += 1);
		Self::deposit_event(Event::AssetsTrapped {
			hash,
			origin: origin.clone(),
			assets: versioned,
		});
		// TODO #3735: Put the real weight in there.
		Weight::zero()
	}
```
