## Confirmed local analog: Snowbridge inbound-queue **V1** builds `DepositAsset { beneficiary }` with no `AssetClaimer` hint, unlike V2

### Title
Snowbridge inbound-queue V1 `MessageToXcm::convert_send_token`/`convert_send_native_token` hard-codes the Ethereum-supplied beneficiary with no claimer fallback, so a blocked/frozen recipient permanently loses bridged funds - (File: `bridges/snowbridge/primitives/inbound-queue/src/v1.rs`)

### Summary
This is the direct on-chain analog of the Ajna report: a pallet builds a value-transfer instruction (`DepositAsset`) to a fixed beneficiary account with no mechanism to redirect funds to another address if that beneficiary cannot receive them. `pallet-assets` provides exactly the "blacklist" primitive referenced in the external report — an account can be put into `AccountStatus::Blocked` or `Frozen` via the `block`/`freeze` extrinsics [1](#0-0) , and a blocked/frozen account cannot receive deposits (`TokenError::Blocked` / `Error::Frozen`) [2](#0-1) .

### Finding Description
`MessageToXcm::convert_send_token` and `convert_send_native_token` (Snowbridge inbound queue V1) construct the destination-chain XCM program with `DepositAsset { assets: Wild(AllCounted(2)), beneficiary }`, where `beneficiary` is taken verbatim from the `Destination::AccountId32 { id }` field decoded from the Ethereum-originated message payload [3](#0-2) [4](#0-3) . This beneficiary is asserted only once at message-send time and there is no way for the recipient (or anyone) to redirect the transfer once the message is in flight — exactly the missing "recipient argument" flaw called out in the Ajna report.

Compare this to V2 of the same subsystem, which explicitly sets an `AssetClaimer` hint before executing the transfer so that if the deposit fails, the funds are trapped at a claimable location instead of being unrecoverable:
`instructions.push(SetHints { hints: vec![AssetClaimer { location: message.claimer }]... })` [5](#0-4) . V1's `convert_send_token`/`convert_send_native_token` contain no equivalent `AssetClaimer`/`SetHints` instruction anywhere in the file — the search for `SetHints|AssetClaimer|claimer` in `v1.rs` returns zero matches, confirming V1 has no fallback claim path.

On AssetHub, the beneficiary account for a token bridged from Ethereum is a `ForeignAssets` account, which is subject to `pallet_assets::block`/`freeze` by the asset's Freezer team [6](#0-5) . If that recipient account becomes `Blocked` (or `Frozen`) between the time the Ethereum-side transaction is submitted and the time the inbound message is relayed and executed on AssetHub, `DepositAsset` will fail with `TokenError::Blocked`/`Error::Frozen` [2](#0-1) . Because V1's XCM program carries no `AssetClaimer` hint, the resulting `AssetsTrapped` event anchors the trapped assets at whatever XCM origin happened to be active at that point in program execution (the bridge/relay-derived origin, not an address the end user controls), with no user-specified fallback recipient to redirect the funds to. This mirrors the Ajna bug precisely: funds are sent to a hard-coded destination account with no `recipient`/beneficiary-override parameter, and if that account is blacklisted (blocked/frozen), the principal is permanently stuck.

### Impact Explanation
Principal (bridged ERC-20/native token value) can become permanently locked with the pool/bridge if the destination beneficiary account is blocked or frozen on AssetHub before message execution — the exact "collateral/bond funds permanently frozen" pattern from the source report, but here applied to bridged asset value rather than a lending pool. This satisfies the "permanent user-fund or bridge-state lock" impact category.

### Likelihood Explanation
Likelihood is low-to-medium: it requires the recipient account to be blocked/frozen by the relevant `ForeignAssets` Freezer team after the Ethereum-side send is initiated but before the bridge message executes on AssetHub — a race condition rather than something the attacker fully controls, similar to the "low probability" severity assigned in the original Ajna report. It does not require a malicious relayer, validator, or governance actor; it can happen through ordinary asset-team compliance action combined with unlucky timing of a legitimate cross-chain transfer.

### Recommendation
Bring V1's converter in line with V2: add an `AssetClaimer`/`SetHints` instruction (or equivalent recipient-override mechanism) to `convert_send_token` and `convert_send_native_token` in `bridges/snowbridge/primitives/inbound-queue/src/v1.rs`, anchored to a claimer identity carried in the Ethereum message (or defaulting to the sender/bridge-owner sovereign, as V2 does), so that a failed `DepositAsset` traps funds at a location the legitimate owner can subsequently claim via `pallet_xcm::claim_assets`, instead of being permanently unrecoverable.

### Proof of Concept
1. On Ethereum, initiate `SendToken`/`SendNativeToken` targeting `Destination::AccountId32 { id: R }` on AssetHub, where `R` is a normal, unblocked account.
2. Before the message is relayed and executed on AssetHub, have the asset's Freezer call `pallet_assets::block(origin, asset_id, R)` (or `freeze`), analogous to the "blacklist" event in the original report [1](#0-0) .
3. The relayer submits the message; `EthereumInboundQueue::do_convert` builds the XCM with `DepositAsset { ..., beneficiary: R }` [7](#0-6) .
4. `DepositAsset` fails with `TokenError::Blocked`; because V1 sets no `AssetClaimer` hint (unlike V2 [5](#0-4) ), the assets are trapped without a controllable/claimable location for `R`, and `R` has no way to specify an alternate beneficiary to recover the funds — they remain locked indefinitely, reproducing the Ajna "frozen principal, no recipient override" bug in the Polkadot SDK bridge context.

### Citations

**File:** substrate/frame/assets/src/lib.rs (L1858-1893)
```rust
		/// Disallow further unprivileged transfers of an asset `id` to and from an account `who`.
		///
		/// Origin must be Signed and the sender should be the Freezer of the asset `id`.
		///
		/// - `id`: The identifier of the account's asset.
		/// - `who`: The account to be unblocked.
		///
		/// Emits `Blocked`.
		///
		/// Weight: `O(1)`
		#[pallet::call_index(31)]
		pub fn block(
			origin: OriginFor<T>,
			id: T::AssetIdParameter,
			who: AccountIdLookupOf<T>,
		) -> DispatchResult {
			let origin = ensure_signed(origin)?;
			let id: T::AssetId = id.into();

			let d = Asset::<T, I>::get(&id).ok_or(Error::<T, I>::Unknown)?;
			ensure!(
				d.status == AssetStatus::Live || d.status == AssetStatus::Frozen,
				Error::<T, I>::IncorrectStatus
			);
			ensure!(origin == d.freezer, Error::<T, I>::NoPermission);
			let who = T::Lookup::lookup(who)?;

			Account::<T, I>::try_mutate(&id, &who, |maybe_account| -> DispatchResult {
				maybe_account.as_mut().ok_or(Error::<T, I>::NoAccount)?.status =
					AccountStatus::Blocked;
				Ok(())
			})?;

			Self::deposit_event(Event::<T, I>::Blocked { asset_id: id, who });
			Ok(())
		}
```

**File:** substrate/frame/assets/src/tests.rs (L866-880)
```rust
#[test]
fn transferring_to_blocked_account_should_not_work() {
	build_and_execute(|| {
		assert_ok!(Assets::force_create(RuntimeOrigin::root(), 0, 1, true, 1));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(1), 0, 1, 100));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(1), 0, 2, 100));
		assert_eq!(Assets::balance(0, 1), 100);
		assert_eq!(Assets::balance(0, 2), 100);
		assert_ok!(Assets::block(RuntimeOrigin::signed(1), 0, 1));
		assert_noop!(Assets::transfer(RuntimeOrigin::signed(2), 0, 1, 50), TokenError::Blocked);
		assert_ok!(Assets::thaw(RuntimeOrigin::signed(1), 0, 1));
		assert_ok!(Assets::transfer(RuntimeOrigin::signed(2), 0, 1, 50));
		assert_ok!(Assets::transfer(RuntimeOrigin::signed(1), 0, 2, 50));
	});
}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v1.rs (L310-329)
```rust
		let (dest_para_id, beneficiary, dest_para_fee) = match destination {
			// Final destination is a 32-byte account on AssetHub
			Destination::AccountId32 { id } => {
				(None, Location::new(0, [AccountId32 { network: None, id }]), 0)
			},
			// Final destination is a 32-byte account on a sibling of AssetHub
			Destination::ForeignAccountId32 { para_id, id, fee } => (
				Some(para_id),
				Location::new(0, [AccountId32 { network: None, id }]),
				// Total fee needs to cover execution on AssetHub and Sibling
				fee,
			),
			// Final destination is a 20-byte account on a sibling of AssetHub
			Destination::ForeignAccountId20 { para_id, id, fee } => (
				Some(para_id),
				Location::new(0, [AccountKey20 { network: None, key: id }]),
				// Total fee needs to cover execution on AssetHub and Sibling
				fee,
			),
		};
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v1.rs (L374-381)
```rust
			None => {
				instructions.extend(vec![
					// Deposit both asset and fees to beneficiary so the fees will not get
					// trapped. Another benefit is when fees left more than ED on AssetHub could be
					// used to create the beneficiary account in case it does not exist.
					DepositAsset { assets: Wild(AllCounted(2)), beneficiary },
				]);
			},
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v1.rs (L418-426)
```rust
		let beneficiary = match destination {
			// Final destination is a 32-byte account on AssetHub
			Destination::AccountId32 { id } => {
				Ok(Location::new(0, [AccountId32 { network: None, id }]))
			},
			// Forwarding to a destination parachain is not allowed for PNA and is validated on the
			// Ethereum side. https://github.com/Snowfork/snowbridge/blob/e87ddb2215b513455c844463a25323bb9c01ff36/contracts/src/Assets.sol#L216-L224
			_ => Err(ConvertMessageError::InvalidDestination),
		}?;
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs (L386-394)
```rust
		// Set claimer before PayFees, in case the fees are not enough. Then the claimer will be
		// able to claim the funds still.
		instructions.push(SetHints {
			hints: vec![AssetClaimer { location: message.claimer }]
				.try_into()
				.expect("checked statically, qed"),
		});

		instructions.push(PayFees { asset: message.execution_fee.clone() });
```
