Audit Report

## Title
Snowbridge relayer reward is irrecoverably lost when the XCM-based payout to AssetHub fails after `BridgeRelayers::claim_rewards_to` has already deleted the reward entry - ([File: bridges/snowbridge/primitives/core/src/reward.rs])

## Summary
`pallet_bridge_relayers::do_claim_rewards` removes the relayer's `RelayerRewards` entry and emits `Event::RewardPaid` based solely on `PaymentProcedure::pay_reward` returning `Ok(())`. For Snowbridge, `PayAccountOnLocation::pay_reward` returns `Ok(())` once the XCM is successfully *delivered* to AssetHub (`validate_send`, `charge_fees`, `deliver` all succeed), without any confirmation that the subsequent `DepositAsset` instruction actually executes successfully on AssetHub. If `DepositAsset` fails there, the reserve-deposited asset is trapped under an origin (`UniversalOrigin(Ethereum)` + `DescendOrigin(InboundQueueLocation)`) that has no `SetAppendix`/fallback claimer and cannot be matched by any local signed account via AssetHub's ordinary origin converters, permanently losing the reward while BridgeHub's bookkeeping shows it as paid.

## Finding Description
`do_claim_rewards` uses `try_mutate_exists` to take the reward out of storage and only restores it if `pay_reward` errors: [1](#0-0) 

`PayAccountOnLocation::pay_reward` builds an XCM with `UnpaidExecution`, `DescendOrigin(InboundQueueLocation)`, `UniversalOrigin(GlobalConsensus(EthereumNetwork))`, `ReserveAssetDeposited`, and `DepositAsset { beneficiary, .. }`, then only returns `Err` for `validate_send`, `charge_fees`, or `deliver` failures — never for a subsequent execution failure of `DepositAsset` on AssetHub, since that execution happens asynchronously on a different chain: [2](#0-1) 

Unlike the inbound-queue message converter (`convert_send_token`), which explicitly adds a `SetAppendix` fallback to redirect leftover/undeliverable assets to the Snowbridge sovereign account, `pay_reward`'s XCM has no such appendix or claimer mechanism: [3](#0-2) 

The codebase itself already demonstrates this exact trap pattern (`UniversalOrigin`+`DescendOrigin` combination) producing genuinely unrecoverable trapped assets in the inbound-queue flow, which required a dedicated fix (setting `network: Some(LocalNetwork)`) so a signed origin's trap key could match: [4](#0-3)  That fix (`pr_11919.prdoc`) was scoped only to the inbound-queue message converter's default claimer, not to `pay_reward`'s trap origin, which remains without any claimer or appendix. The passing test `claim_rewards_works` only exercises the happy path where `DepositAsset` succeeds and the asset lands in the beneficiary's `ForeignAssets` balance: [5](#0-4)  there is no test covering the failure-to-deposit-on-AssetHub case for `pay_reward`.

## Impact Explanation
A relayer's legitimately earned reward is permanently and irrecoverably lost while BridgeHub's `RelayerRewards` storage and `RewardPaid` event report it as successfully settled. This is a genuine permanent user-fund lock in the reward-payout settlement path, matching the required invariant that payout state must only advance after execution/settlement succeeds atomically — here, settlement (`RelayerRewards` removal + `RewardPaid`) advances on mere XCM delivery, decoupled from the actual asynchronous `DepositAsset` execution on AssetHub.

## Likelihood Explanation
No privileged actor is required. `claim_rewards_to` is a public, unprivileged, signed extrinsic where the caller supplies the `beneficiary` `VersionedLocation` themselves: [6](#0-5)  A beneficiary that cannot accept the reserve-deposited asset on AssetHub (unregistered asset for that filter, below existential deposit, etc.) triggers deterministic, reproducible loss — no adversarial or privileged behavior needed, only an ordinary mis-specified or state-changed beneficiary at execution time.

## Recommendation
Do not finalize/remove the `RelayerRewards` entry based on XCM send success alone. Either (a) keep the reward pending until a delivery/execution confirmation (e.g., `QueryResponse`) from AssetHub confirms `DepositAsset` succeeded, only then clearing `RelayerRewards`; or (b) add a `SetAppendix`/fallback claimer in `pay_reward`'s XCM that redirects failed deposits to a location the relayer (or BridgeHub) can actually reclaim from, mirroring the fix already applied to the inbound-queue converter's fallback claimer.

## Proof of Concept
1. Relayer accrues a Snowbridge reward via `register_reward`.
2. Relayer calls `claim_rewards_to(origin, BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(loc))` with a `loc` that will fail `DepositAsset` on AssetHub (e.g., unregistered asset/filter mismatch or no ED).
3. `PayAccountOnLocation::pay_reward` succeeds (delivery only) → returns `Ok(())`; `do_claim_rewards` removes the `RelayerRewards` entry and emits `RewardPaid`.
4. On AssetHub, `ReserveAssetDeposited` populates holding but `DepositAsset` fails against the beneficiary; assets are trapped under the `UniversalOrigin(Ethereum)+DescendOrigin(InboundQueueLocation)` origin, matching the trap pattern already demonstrated in `invalid_xcm_traps_funds_on_ah`.
5. No signed account on AssetHub can reconstruct that trap origin via `SignedToAccountId32`, so `pallet_xcm::claim_assets` cannot recover the funds — the reward is permanently lost despite BridgeHub state showing it as paid.

### Citations

**File:** bridges/modules/relayers/src/lib.rs (L224-235)
```rust
		/// Claim accumulated rewards and send them to the alternative beneficiary.
		#[pallet::call_index(3)]
		#[pallet::weight(T::WeightInfo::claim_rewards_to())]
		pub fn claim_rewards_to(
			origin: OriginFor<T>,
			reward_kind: T::Reward,
			beneficiary: BeneficiaryOf<T, I>,
		) -> DispatchResult {
			let relayer = ensure_signed(origin)?;

			Self::do_claim_rewards(relayer, reward_kind, beneficiary)
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

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L127-151)
```rust
	fn pay_reward(
		relayer: &Relayer,
		_: (),
		reward: RewardBalance,
		beneficiary: Self::Beneficiary,
	) -> Result<(), Self::Error> {
		let ethereum_location = Location::new(2, [GlobalConsensus(EthereumNetwork::get())]);
		let assets: Asset = (ethereum_location.clone(), reward.into()).into();

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

**File:** bridges/snowbridge/primitives/inbound-queue/src/v1.rs (L349-356)
```rust
				instructions.extend(vec![
					// After program finishes deposit any leftover assets to the snowbridge
					// sovereign.
					SetAppendix(Xcm(vec![DepositAsset {
						assets: Wild(AllCounted(2)),
						beneficiary: bridge_location,
					}])),
					// Perform a deposit reserve to send to destination chain.
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L859-926)
```rust
#[test]
fn invalid_xcm_traps_funds_on_ah() {
	let relayer_account = BridgeHubWestendSender::get();
	let relayer_reward = 1_500_000_000_000u128;

	let token: H160 = TOKEN_ID.into();
	let claimer = AccountId32 { network: None, id: H256::random().into() };
	let claimer_bytes = claimer.encode();
	let beneficiary_acc_bytes: [u8; 32] = H256::random().into();

	AssetHubWestend::fund_accounts(vec![(
		sp_runtime::AccountId32::from(beneficiary_acc_bytes),
		3_000_000_000_000,
	)]);

	set_up_eth_and_dot_pool();

	let assets = vec![
		// to transfer assets
		NativeTokenERC20 { token_id: WETH.into(), value: 2_800_000_000_000u128 },
		// the token being transferred
		NativeTokenERC20 { token_id: token.into(), value: 2_000_000_000_000u128 },
	];

	BridgeHubWestend::execute_with(|| {
		type RuntimeEvent = <BridgeHubWestend as Chain>::RuntimeEvent;
		// invalid xcm
		let instructions = hex!("02806c072d50e2c7cd6821d1f084cbb4");
		let origin = EthereumGatewayAddress::get();

		let message = Message {
			gateway: origin,
			nonce: 1,
			origin,
			assets,
			payload: Payload::Raw(instructions.to_vec()),
			claimer: Some(claimer_bytes),
			value: 1_500_000_000_000u128,
			execution_fee: 1_500_000_000_000u128,
			relayer_fee: relayer_reward,
		};

		EthereumInboundQueueV2::process_message(relayer_account.clone(), message).unwrap();

		assert_expected_events!(
			BridgeHubWestend,
			vec![
				RuntimeEvent::XcmpQueue(cumulus_pallet_xcmp_queue::Event::XcmpMessageSent { .. }) => {},
				// Check that the relayer reward was registered.
				RuntimeEvent::BridgeRelayers(pallet_bridge_relayers::Event::RewardRegistered { relayer, reward_kind, reward_balance }) => {
					relayer: *relayer == relayer_account,
					reward_kind: *reward_kind == BridgeReward::Snowbridge,
					reward_balance: *reward_balance == relayer_reward,
				},
			]
		);
	});

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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_rewards.rs (L63-102)
```rust
		let relayer_location = Location::new(
			0,
			[Junction::AccountId32 { id: reward_address.clone().into(), network: None }],
		);
		let reward_beneficiary =
			BridgeRewardBeneficiaries::AssetHubLocation(VersionedLocation::V5(relayer_location));
		let result = BridgeRelayers::claim_rewards_to(
			RuntimeOrigin::signed(relayer_account.clone()),
			BridgeReward::Snowbridge,
			reward_beneficiary.clone(),
		);
		assert_ok!(result);

		assert_expected_events!(
			BridgeHubWestend,
			vec![
				// Check that the pay reward event was emitted on BH
				RuntimeEvent::BridgeRelayers(pallet_bridge_relayers::Event::RewardPaid { relayer, reward_kind, reward_balance, beneficiary }) => {
					relayer: *relayer == relayer_account,
					reward_kind: *reward_kind == BridgeReward::Snowbridge,
					reward_balance: *reward_balance == reward_amount,
					beneficiary: *beneficiary == reward_beneficiary,
				},
			]
		);
	});

	AssetHubWestend::execute_with(|| {
		type RuntimeEvent = <AssetHubWestend as Chain>::RuntimeEvent;
		assert_expected_events!(
			AssetHubWestend,
			vec![
				// Check that the reward was paid on AH
				RuntimeEvent::ForeignAssets(pallet_assets::Event::Deposited { asset_id, who: owner, .. }) => {
					asset_id: *asset_id == eth_location(),
					owner: *owner == reward_address.clone().into(),
				},
			]
		);
	})
```
