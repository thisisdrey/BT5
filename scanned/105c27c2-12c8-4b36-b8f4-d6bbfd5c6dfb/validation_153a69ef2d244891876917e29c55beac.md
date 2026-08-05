### Title
Snowbridge relayer reward is permanently lost when the async cross-chain deposit to the caller-chosen beneficiary fails after the local reward record is already cleared - ([File: bridges/snowbridge/primitives/core/src/reward.rs])

### Summary
`PaymentProcedure::pay_reward` for Snowbridge rewards (`PayAccountOnLocation::pay_reward`) only confirms that an XCM was *validated and handed to the delivery queue* to Asset Hub — it never confirms that the `DepositAsset` instruction inside that XCM actually succeeds at the destination. `pallet_bridge_relayers::do_claim_rewards` treats that local "Ok" as final settlement and permanently erases the relayer's `RelayerRewards` entry before the deposit at Asset Hub is known to succeed. If the caller-supplied `beneficiary` `Location` cannot accept the deposit on Asset Hub, the reward assets are trapped there while the relayer's on-chain claim record is already gone, with no way to re-claim.

### Finding Description
`PayAccountOnLocation::pay_reward` builds a fire-and-forget XCM and only checks `validate_send`, `charge_fees`, and `XcmSender::deliver`: [1](#0-0) 

None of these steps execute the `DepositAsset { assets: AllCounted(1).into(), beneficiary }` instruction — that only happens later, asynchronously, when the message is processed on Asset Hub. Meanwhile, `pallet_bridge_relayers::do_claim_rewards` removes the reward from storage and emits `RewardPaid` as soon as `pay_reward` returns `Ok`, i.e. as soon as the message is merely *queued*, not settled: [2](#0-1) 

The relayer freely chooses the `beneficiary` `Location` when calling `claim_rewards_to`. The constructed XCM contains no `SetAssetClaimer`, so if `DepositAsset` fails at Asset Hub (e.g. an unsupported junction/location kind, a beneficiary account that can't be created/doesn't satisfy Asset Hub's deposit rules, or any other `DepositAsset` failure), the `ReserveAssetDeposited` assets are trapped under Asset Hub's `AssetTrap` using the message's *origin context* (`DescendOrigin`+`UniversalOrigin(Ethereum)`), not an origin the relayer can ever reconstruct to submit a `ClaimAsset`. The emulated test confirms this trap-on-failure behavior for the same message shape: [3](#0-2) 

By the time that trap happens, `RelayerRewards` on BridgeHub has already been zeroed and `RewardPaid` already emitted, so a retry via `claim_rewards`/`claim_rewards_to` fails with `NoRewardForRelayer`: [4](#0-3) 

This is structurally the same bug class as the external report: value is pushed to a caller-specified destination and the sending side treats the transmission attempt as final settlement, without verifying the destination could actually accept the funds, and with no fallback/recovery path once that "success" is recorded.

### Impact Explanation
This breaks the invariant required by the pivots that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." Here the reward ledger entry (bridge payout state) is deleted based on local queuing success rather than destination-chain settlement success, causing a **permanent loss/lock of relayer reward funds** with no recovery mechanism through the pallet.

### Likelihood Explanation
Likelihood is moderate-to-high: any malformed, unsupported, or edge-case `Location`/`VersionedLocation` beneficiary passed to `claim_rewards_to` (accidentally by a relayer, or via any code path that computes/derives this location, e.g. tooling bugs, wrong network/junction versions, or Asset Hub-side deposit filters) results in unrecoverable fund loss the moment the XCM is merely accepted by the router — no privileged actor, malicious peer, or race condition is required.

### Recommendation
Do not remove the `RelayerRewards` entry (or emit `RewardPaid`) until settlement of the cross-chain deposit is confirmed, e.g. by using a two-phase claim (reserve/lock the entry, require a confirmation callback such as message-queue processing status, XCM `QueryResponse`, or a receipt from Asset Hub before final removal), and/or attach a `SetAssetClaimer` (set to the relayer's own reclaimable identity) to the outbound XCM so that failed deposits can still be recovered by the intended relayer.

### Proof of Concept
1. Relayer accrues a Snowbridge reward via `register_reward`, e.g. as in `bridge_rewards_works`.
2. Relayer calls `claim_rewards_to(BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(location))` with a `location` that is syntactically valid (passes `validate_send`/delivery) but which Asset Hub's `DepositAsset` cannot fulfill (e.g., an account/junction combination not supported by the destination's asset-deposit barrier/converter).
3. `PayAccountOnLocation::pay_reward` returns `Ok(())` because `validate_send` + `charge_fees` + `deliver` all succeed — the message is merely enqueued.
4. `do_claim_rewards` removes the `RelayerRewards` entry and emits `RewardPaid`.
5. On Asset Hub, message execution reaches `DepositAsset`, which fails; per the same mechanics validated by `invalid_xcm_traps_funds_on_ah`, the `ReserveAssetDeposited` reward assets are trapped (`AssetsTrapped` event) under an origin context the relayer has no means to claim from.
6. Relayer calls `claim_rewards`/`claim_rewards_to` again for the same reward kind and gets `Error::NoRewardForRelayer` — the reward is permanently gone.

### Citations

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L136-151)
```rust
		let xcm: Xcm<()> = alloc::vec![
			UnpaidExecution { weight_limit: Unlimited, check_origin: None },
			DescendOrigin(InboundQueueLocation::get().into()),
			UniversalOrigin(GlobalConsensus(EthereumNetwork::get())),
			ReserveAssetDeposited(assets.into()),
			DepositAsset { assets: AllCounted(1).into(), beneficiary },
		]
		.into();

		let (ticket, fee) =
			validate_send::<XcmSender>(AssetHubLocation::get(), xcm).map_err(|_| XcmSendFailure)?;
		XcmExecutor::charge_fees(relayer.clone(), fee).map_err(|_| ChargeFeesFailure)?;
		XcmSender::deliver(ticket).map_err(|_| XcmSendFailure)?;

		Ok(())
	}
```

**File:** bridges/modules/relayers/src/lib.rs (L107-119)
```rust
	#[pallet::call]
	impl<T: Config<I>, I: 'static> Pallet<T, I>
	where
		BeneficiaryOf<T, I>: From<<T as frame_system::Config>::AccountId>,
	{
		/// Claim accumulated rewards.
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::claim_rewards())]
		pub fn claim_rewards(origin: OriginFor<T>, reward_kind: T::Reward) -> DispatchResult {
			let relayer = ensure_signed(origin)?;

			Self::do_claim_rewards(relayer.clone(), reward_kind, relayer.into())
		}
```

**File:** bridges/modules/relayers/src/lib.rs (L263-301)
```rust
		fn do_claim_rewards(
			relayer: T::AccountId,
			reward_kind: T::Reward,
			beneficiary: BeneficiaryOf<T, I>,
		) -> DispatchResult {
			RelayerRewards::<T, I>::try_mutate_exists(
				&relayer,
				reward_kind,
				|maybe_reward| -> DispatchResult {
					let reward_balance =
						maybe_reward.take().ok_or(Error::<T, I>::NoRewardForRelayer)?;
					T::PaymentProcedure::pay_reward(
						&relayer,
						reward_kind,
						reward_balance,
						beneficiary.clone(),
					)
					.map_err(|e| {
						tracing::error!(
							target: LOG_TARGET,
							error=?e,
							?relayer,
							?reward_kind,
							?reward_balance,
							?beneficiary,
							"Failed to pay rewards"
						);
						Error::<T, I>::FailedToPayReward
					})?;

					Self::deposit_event(Event::<T, I>::RewardPaid {
						relayer: relayer.clone(),
						reward_kind,
						reward_balance,
						beneficiary,
					});
					Ok(())
				},
			)
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L917-926)
```rust
	AssetHubWestend::execute_with(|| {
		type RuntimeEvent = <AssetHubWestend as Chain>::RuntimeEvent;

		// Assets are trapped
		assert_expected_events!(
			AssetHubWestend,
			vec![RuntimeEvent::PolkadotXcm(pallet_xcm::Event::AssetsTrapped { .. }) => {},]
		);
	});
}
```
