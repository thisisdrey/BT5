### Title
`AssetsInHolding::is_empty` and `drop_assets` treat presence of an asset key as non-zero value, allowing zero-amount assets to be trapped into unbounded `AssetTraps` storage - ([File: polkadot/xcm/xcm-executor/src/assets.rs])

### Summary
`pallet_xcm`'s `DropAssets::drop_assets` (`polkadot/xcm/pallet-xcm/src/lib.rs`) decides whether to register a trapped-asset entry solely on `AssetsInHolding::is_empty()`, which only checks whether the `fungible`/`non_fungible` maps contain *any keys*, not whether each entry's *actual amount* is non-zero. This mirrors the reported `ds_token::check_wallets_for_list` bug: a structural/aggregate check (map non-emptiness) is substituted for a per-entity value check (each asset's real balance), so zero-value "empty" holdings can still be admitted into a tracked registry (`AssetTraps`).

### Finding Description
`AssetsInHolding::is_empty` is defined as: [1](#0-0) 
This only checks the *map keys*, not the accounted `amount()` of each fungible entry.

`drop_assets` guards on this same coarse check before registering a trap: [2](#0-1) 
If `holding.fungible` contains an `AssetId` key whose `ImbalanceAccounting` amount is `0` (map is non-empty by key count, but value-empty), `drop_assets` still proceeds: it hashes `(origin, versioned_assets)` and does `AssetTraps::<T>::mutate(hash, |n| *n += 1)`, permanently growing the `AssetTraps` map and emitting an `AssetsTrapped` event — exactly analogous to `check_wallets_for_list` adding a zero-balance wallet to `wallet_list`/`wallet_indexes` because it checked the investor's *total* balance rather than the specific wallet's balance.

This class of bug was already independently observed and partially patched: `prdoc/stable2603/pr_11389.prdoc` documents that `SwapFirstAssetTrader::buy_weight` could leave a `Fungible(0)` credit that got "unconditionally wrapped into an `AssetsInHolding` entry" and propagated through `fees → refund_surplus → holding → drop_assets`, producing an `AssetsTrapped` event with an undecodable `Fungible(0)` asset: [3](#0-2) 
That fix only guards one call site (`cumulus-primitives-utility`'s `SwapFirstAssetTrader`) by checking the value before insertion — it does **not** fix the root cause in `AssetsInHolding::is_empty`/`drop_assets`, so any other `WeightTrader`/`TransactAsset` implementation (custom trader chains, third-party asset transactors, or future code) that leaves a zero-amount fungible entry in the holding register will still trigger the same unbounded trap-registration path, just as `check_wallets_for_list` remained wrong for every call site until the check itself (not just one caller) was fixed.

### Impact Explanation
`AssetTraps` is a runtime storage map keyed by `BlakeTwo256::hash_of(&(origin, versioned_assets))` with no bound on the number of distinct keys. If dust/zero-amount fungible entries can be produced deterministically or with attacker-influenced variation (differing origins, differing residual asset-id sets after partial trades/refunds), each such “trap” is a permanent state write (`n += 1`, never garbage collected unless explicitly claimed) and an event emission. Because the guard is a coarse presence check rather than a real value check, low-cost XCM programs that intentionally trigger trader/refund code paths leaving stray zero-value entries can inflate `AssetTraps` storage indefinitely — degrading block production/storage growth on any XCM-processing chain (Polkadot/Kusama relay, or any parachain running `pallet_xcm`), consistent with the "public underpriced work that degrades block production" impact class in scope.

### Likelihood Explanation
Moderate-to-low but real: the exact scenario (a zero-amount credit reaching `drop_assets`) has already been observed and partially fixed for one trader (`SwapFirstAssetTrader`). Because the fix was applied at the individual call-site level rather than in `AssetsInHolding::is_empty`/`drop_assets` itself, any other trader, refund path, or custom `TransactAsset`/`WeightTrader` implementation used in a parachain's `XcmConfig` that leaves a zero-amount `fungible` map entry (instead of removing the key) will reproduce the same unbounded-trap-growth condition. No malicious peer, validator, or privileged actor is needed — only crafting or triggering execution paths (fee payment with exact quoted amounts, multi-trader configurations, etc.) that leave residual zero credits.

### Recommendation
- Fix the check at its source rather than at individual call sites: make `AssetsInHolding::is_empty()` (and any code deciding whether to trap/register assets) treat a fungible entry with `amount() == 0` as absent — either by filtering zero amounts out of the map on every mutation (`subsume_assets`, `saturating_subsume`, trader refunds) or by having `is_empty()`/`drop_assets` explicitly skip zero-amount entries when building the `assets` vector and computing the hash.
- Audit all `WeightTrader`/`TransactAsset` implementations that insert directly into `AssetsInHolding::fungible` for the same "leaves zero-amount key" pattern the `pr_11389` fix addressed only for `SwapFirstAssetTrader`.
- Add a `try_state`/invariant check ensuring no `AssetTraps` entry corresponds to a fully zero-valued `VersionedAssets` payload.

### Proof of Concept
Not independently reproduced in this pass — the analysis is based on static code review of `AssetsInHolding::is_empty` (`polkadot/xcm/xcm-executor/src/assets.rs:158-161`), `DropAssets::drop_assets` (`polkadot/xcm/pallet-xcm/src/lib.rs:3901-3924`), and the `pr_11389` prdoc confirming the exact zero-amount-trap scenario previously occurred for `SwapFirstAssetTrader::buy_weight`. A concrete PoC would require constructing an XCM program using a `WeightTrader`/fee-refund configuration (other than the already-patched `SwapFirstAssetTrader`) that leaves a zero-amount fungible entry in the executor's holding register at program termination, then observing that `drop_assets` still increments `AssetTraps` for that entry — this requires running the XCM executor with a specific runtime `XcmConfig` to confirm, which was not available in this read-only review.

### Citations

**File:** polkadot/xcm/xcm-executor/src/assets.rs (L158-161)
```rust
	/// Returns `true` if `self` contains no assets.
	pub fn is_empty(&self) -> bool {
		self.fungible.is_empty() && self.non_fungible.is_empty()
	}
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

**File:** prdoc/stable2603/pr_11389.prdoc (L1-14)
```text
title: 'Fix: AssetTrapped event with Fungible(0) due to `SwapFirstAssetTrader::buy_weight`
  for exact trades'
doc:
- audience: Runtime Dev
  description: "When `PayFees` contained the exact quoted fee, `SwapFirstAssetTrader::buy_weight`\
    \ produces zero swap change. This 0-amount credit was unconditionally wrapped\
    \ into an `AssetsInHolding` entry, which propagated through `fees` \u2192 `refund_surplus`\
    \ \u2192 `holding` \u2192 `drop_assets`, emitting an `AssetsTrapped` event with\
    \ `Fungible(0)` that fails to decode.\n\nThis PR simply guards that by checking\
    \ if value is 0 before putting it into the holding, and omitting the step if the\
    \ value is 0.\n\nCloses #11388"
crates:
- name: cumulus-primitives-utility
  bump: patch
```
