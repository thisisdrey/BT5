Based on my investigation, I found a concrete local analog to the Magnetar bug class in the Snowbridge system-v2 pallet: an extrinsic that discards the identity proven by its own origin check and instead trusts a separately-supplied, unverified "sender" location argument to determine message attribution/authorization on the Ethereum side.

### Title
`register_token` in `snowbridge-pallet-system-v2` binds Gateway message authorization to an unverified `sender` argument instead of the value proven by `FrontendOrigin::ensure_origin` - (File: `bridges/snowbridge/pallets/system-v2/src/lib.rs`)

### Summary
`Pallet::register_token` in `snowbridge-pallet-system-v2` calls `T::FrontendOrigin::ensure_origin(origin)?` only to prove that the call arrived from a legitimate caller, but it throws away the `Location` (`Success`) that this check returns. It then uses a completely separate, caller-supplied `sender: Box<VersionedLocation>` parameter to compute the `origin: H256` field of the message dispatched to the Ethereum Gateway. This mirrors the Magnetar pattern of taking a sensitive "identity/address" as unchecked user input rather than deriving it from the verified caller, and using it later to authorize privileged action (here, message attribution/agent authorization on Ethereum).

### Finding Description [1](#0-0) 

```rust
pub fn register_token(
    origin: OriginFor<T>,
    sender: Box<VersionedLocation>,
    asset_id: Box<VersionedLocation>,
    metadata: AssetMetadata,
    amount: u128,
) -> DispatchResult {
    T::FrontendOrigin::ensure_origin(origin)?;
    let sender_location: Location = (*sender).try_into()...?;
    ...
    let message_origin = Self::location_to_message_origin(sender_location)?;
    Self::send(message_origin, command, amount)?;
    ...
}
```

`T::FrontendOrigin::ensure_origin(origin)?` returns `Success = Location`, but the return value is never captured — the code only checks that the call reaches this extrinsic through the expected channel (the `EnsureOrigin` succeeds), it does **not** verify that `sender` equals (or is nested within) the `Location` the origin check actually resolved. `sender` is instead taken verbatim from the call payload and converted via `location_to_message_origin` into the `origin: H256` field of the `Message` sent to the Ethereum Gateway (`Self::send`), which is the value used by the Gateway/agent system to attribute and authorize the `RegisterForeignToken` command.

This is the same broken invariant as the Magnetar report: a privileged identity value (an address in Magnetar; a `Location`/agent-origin here) that should be derived exclusively from a verified, authenticated source is instead accepted as free-form caller input and used downstream for privileged accounting/authorization, with no binding check tying the two together. Contrast this with `bridges/snowbridge/runtime/runtime-common/src/v2/register_token.rs`'s `ForeignAssetOwner::try_origin`, which correctly derives the returned `Location` from the XCM origin itself and checks asset ownership against it — the exact "cluster whitelist"-style binding pattern recommended in the original report, that is missing between `FrontendOrigin`'s result and the `sender` argument in `system-v2::register_token`. [2](#0-1) 
Under the intended single-entry-point flow, `pallet-system-frontend::register_token` on AssetHub correctly derives `origin_location` from `RegisterTokenOrigin::ensure_origin` and passes that same value as `sender` when building the `EthereumSystemCall::RegisterToken` Transact call. So in the "happy path" the two values coincide. The vulnerability is that `system-v2::register_token` on BridgeHub has no code-level guarantee enforcing this coincidence — it is purely convention, not an enforced invariant, exactly like the Magnetar contract's `_mintFromBBAndLendOnSGL` case, which the auditors noted had a "useless" cluster check because the checked value and the value later acted upon were not provably the same.

### Impact Explanation
If this Transact call can ever be constructed or reach `system-v2::register_token` with a `sender` that differs from the actual verified `FrontendOrigin` location (e.g., through any future change to `FrontendOrigin`'s scope, a bug in the XCM origin-conversion/DescendOrigin handling on BridgeHub, or reuse of this pattern elsewhere), an attacker could attribute foreign-token registration and its associated fee/agent bookkeeping to an arbitrary Location/agent of their choosing rather than their own, which is unauthorized-origin/mis-bound-authorization impact matching the "unauthorized execution or origin escalation" and "forged or mis-bound proof or state acceptance" impact classes in the gate.

### Likelihood Explanation
Likelihood is Low-to-Medium and could not be conclusively confirmed within this investigation: exploitability strictly depends on whether an unprivileged actor can produce an origin that satisfies `T::FrontendOrigin::ensure_origin` on BridgeHub without going through the legitimate `pallet-system-frontend::register_token` code path (e.g., via a crafted XCM `DescendOrigin`/`Transact` from AssetHub). I was not able to fully inspect the concrete `FrontendOrigin` type wiring in `cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_to_ethereum_config.rs` before running out of tool calls, so I cannot confirm whether existing origin-conversion barriers already prevent such spoofing. Regardless of that outer question, the code-level defect — discarding the `ensure_origin` `Success` value and trusting an independently supplied `sender` argument for privileged attribution — is present and verifiable in the repository.

### Recommendation
Bind the `sender`/message-origin value to the value actually returned by `T::FrontendOrigin::ensure_origin(origin)?` rather than trusting the caller-supplied `sender` parameter. Concretely:
```rust
let checked_origin = T::FrontendOrigin::ensure_origin(origin)?;
ensure!(sender_location == checked_origin || checked_origin.contains(&sender_location), Error::<T>::InvalidAssetOwner);
```
or, preferably, remove the redundant `sender` parameter entirely and derive `message_origin` directly from `checked_origin`, following the pattern already used correctly in `ForeignAssetOwner::try_origin` (`bridges/snowbridge/runtime/runtime-common/src/v2/register_token.rs`).

### Proof of Concept
Not independently reproducible from the index alone, since it hinges on whether an attacker can produce a spoofed origin satisfying `T::FrontendOrigin` outside the legitimate frontend call path. Conceptually:
1. Attacker crafts an XCM program on AssetHub (e.g. via `pallet_xcm::send`) containing `DescendOrigin(<system-frontend PalletInstance junction>)` followed by `Transact { call: EthereumSystemCall::RegisterToken { sender: <victim/arbitrary Location>, asset_id, metadata, amount } }`, targeting BridgeHub — the same general spoofing technique already demonstrated against `ExportMessage` in `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_edge_case.rs` (`user_send_message_directly_bypass_exporter_from_ah_will_fail`, `user_exploit_with_arbitrary_message_will_fail`).
2. If BridgeHub's XCM origin conversion accepts this crafted origin as satisfying `T::FrontendOrigin`, `system-v2::register_token` executes with `sender` fully attacker-chosen, producing a `Message` to the Ethereum Gateway with `origin` attributed to the attacker-chosen `Location` instead of the attacker's real, verified location. [3](#0-2)

### Citations

**File:** bridges/snowbridge/pallets/system-v2/src/lib.rs (L209-249)
```rust
		#[pallet::call_index(2)]
		#[pallet::weight(<T as pallet::Config>::WeightInfo::register_token())]
		pub fn register_token(
			origin: OriginFor<T>,
			sender: Box<VersionedLocation>,
			asset_id: Box<VersionedLocation>,
			metadata: AssetMetadata,
			amount: u128,
		) -> DispatchResult {
			T::FrontendOrigin::ensure_origin(origin)?;

			let sender_location: Location =
				(*sender).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;
			let asset_location: Location =
				(*asset_id).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;

			let location = Self::reanchor(asset_location)?;
			let token_id = TokenIdOf::convert_location(&location)
				.ok_or(Error::<T>::LocationConversionFailed)?;

			if !ForeignToNativeId::<T>::contains_key(token_id) {
				ForeignToNativeId::<T>::insert(token_id, location.clone());
			}

			let command = Command::RegisterForeignToken {
				token_id,
				name: metadata.name.into_inner(),
				symbol: metadata.symbol.into_inner(),
				decimals: metadata.decimals,
			};

			let message_origin = Self::location_to_message_origin(sender_location)?;
			Self::send(message_origin, command, amount)?;

			Self::deposit_event(Event::<T>::RegisterToken {
				location: location.into(),
				foreign_token_id: token_id,
			});

			Ok(())
		}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L225-252)
```rust
		pub fn register_token(
			origin: OriginFor<T>,
			asset_id: Box<VersionedLocation>,
			metadata: AssetMetadata,
			fee_asset: Asset,
		) -> DispatchResult {
			ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);

			let asset_location: Location =
				(*asset_id).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;
			let origin_location = T::RegisterTokenOrigin::ensure_origin(origin, &asset_location)?;

			let ether_gained = if origin_location.is_here() {
				// Root origin/location does not pay any fees/tip.
				0
			} else {
				Self::swap_fee_asset_and_burn(origin_location.clone(), fee_asset)?
			};

			let call = Self::build_register_token_call(
				origin_location.clone(),
				asset_location,
				metadata,
				ether_gained,
			)?;

			Self::send_transact_call(origin_location, call)
		}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_edge_case.rs (L123-177)
```rust
#[test]
fn user_exploit_with_arbitrary_message_will_fail() {
	fund_on_bh();
	register_assets_on_ah();
	fund_on_ah();
	AssetHubWestend::execute_with(|| {
		type RuntimeEvent = <AssetHubWestend as Chain>::RuntimeEvent;
		type RuntimeOrigin = <AssetHubWestend as Chain>::RuntimeOrigin;

		let remote_fee_asset_location: Location =
			Location::new(2, [EthereumNetwork::get().into()]).into();

		let remote_fee_asset: Asset = (remote_fee_asset_location.clone(), 1).into();

		let assets = VersionedAssets::from(vec![remote_fee_asset]);

		let exploited_weth = Asset {
			id: AssetId(Location::new(0, [AccountKey20 { network: None, key: WETH.into() }])),
			// A big amount without burning
			fun: Fungible(TOKEN_AMOUNT * 1_000_000_000),
		};

		assert_ok!(<AssetHubWestend as AssetHubWestendPallet>::PolkadotXcm::transfer_assets_using_type_and_then(
			RuntimeOrigin::signed(AssetHubWestendSender::get()),
			bx!(VersionedLocation::from(ethereum())),
			bx!(assets),
			bx!(TransferType::DestinationReserve),
			bx!(VersionedAssetId::from(remote_fee_asset_location.clone())),
			bx!(TransferType::DestinationReserve),
			// exploited_weth here is far more than the burnt, which means instructions inner
			// are user provided and untrustworthy/dangerous!
			// Currently it depends on EthereumBlobExporter on BH to check the message is legal
			// and convert to Ethereum command.
			bx!(VersionedXcm::from(Xcm(vec![
				WithdrawAsset(exploited_weth.clone().into()),
				DepositAsset { assets: Wild(All), beneficiary: beneficiary() },
				SetTopic([0; 32]),
			]))),
			Unlimited
		));

		assert_expected_events!(
			AssetHubWestend,
			vec![RuntimeEvent::PolkadotXcm(pallet_xcm::Event::Sent{ .. }) => {},]
		);
	});

	BridgeHubWestend::execute_with(|| {
		type RuntimeEvent = <BridgeHubWestend as Chain>::RuntimeEvent;
		assert_expected_events!(
			BridgeHubWestend,
			vec![RuntimeEvent::MessageQueue(pallet_message_queue::Event::Processed{ success:false, .. }) => {},]
		);
	});
}
```
