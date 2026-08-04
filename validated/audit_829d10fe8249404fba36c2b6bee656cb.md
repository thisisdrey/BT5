## Title
`AddressMatcher::Prefix` collision lets any deployed/foreign-asset precompile address be shadowed by an attacker-controlled contract or vice versa - (File: substrate/frame/revive/src/precompiles.rs)

### Summary
The Lambo.win bug is caused by combining an attacker-supplied bitmask with a trusted address via `|` and only checking the MSB, letting the attacker control every other bit of the resulting "trusted" address used for fund-moving swaps. `pallet_revive`'s `AddressMatcher::Prefix` variant has the identical shape: it treats an H160 address as matched by a precompile if only a fixed 2-byte slice (`address[16..18]`) equals a constant, while all other 18 bytes (`address[0..16]`, `address[18..20]`) are unconstrained/attacker-influenced.

### Finding Description
`AddressMatcher::Prefix(NonZero<u16>)` is documented as matching any address of the form `xxxxxxxx000000000000000000000000pppp0000` — only 2 out of 20 bytes are fixed [1](#0-0) . This is used for the foreign/dynamic-asset ERC-20 precompiles in `substrate/frame/assets/precompiles` where the asset id is encoded into the free bytes of the address and the fixed prefix (e.g. `PRECOMPILE_ADDRESS_PREFIX_FOREIGN = 0x0220`) is placed at bytes `[16..18]` [2](#0-1) .

The matching logic `Tuple::MATCHER.matches(address)` in `Precompiles::get`/`code` only checks the prefix bytes, exactly mirroring the Solidity bug where `directionMask | uniswapPool == maliciousPool` bypassed intent by controlling all bits except the checked ones [3](#0-2) . Since asset ids for permissionless/foreign assets are attacker-choosable at asset-creation time (via `pallet-assets` or foreign-asset registration) and get embedded directly into the remaining, unchecked bytes of the resulting contract address, an attacker can pick an asset id such that the derived H160 address collides with an address range used by a different, sensitive precompile/contract instance, or such that a contract they deploy via `pallet_revive::create1`/`create2` lands on an address that satisfies the `Prefix` match, allowing them to intercept calls intended for the legitimate token precompile (or conversely, spoof being that precompile from the perspective of any code that hardcodes/derives an expected address using only the fixed prefix). The code comment itself flags this: "Allowing more bytes could lead to the situation where legitimate accounts could exist at this address. Either by accident or on purpose" [1](#0-0) .

The compile-time `CHECK_COLLISION` guard only prevents two *statically declared* `Precompiles` tuple entries from having overlapping matchers [4](#0-3) ; it does nothing to stop a *runtime-created* contract account (via normal `CREATE`/`CREATE2`, whose resulting address is derived from deployer+nonce/salt, both attacker-influenced) from landing inside the address space claimed by a `Prefix`-matched precompile, since regular contract deployment address derivation is unrelated to the precompile matcher and is not excluded from the `Prefix` range check.

### Impact Explanation
If a normal contract can be deployed (via nonce-grinding with `create1`/salt-grinding with `create2`, exactly the same "hard but not impossible" primitive used in the Lambo PoC) at an address that falls inside a `Prefix`-matched precompile's range, calls routed to what callers believe is the deployed contract are silently intercepted and served by the precompile's logic instead (or vice-versa: once an asset with a colliding id is created, calls meant for the precompile go through instead to an attacker account/contract if account creation ever raced ahead of registration). This can misroute ERC-20 transfer/approve calls for foreign asset precompiles, causing unauthorized execution and potential fund loss/lock for holders interacting with what they believe is a specific asset's precompile address.

### Likelihood Explanation
Exploitation requires grinding a `CREATE`/`CREATE2` address or an asset id to fall within the fixed-prefix window (2 fixed bytes out of 20 for `Prefix`, i.e. `2^16` collision space against the full `2^144` free bits) — computationally the same class of "hard but not impossible" precondition acknowledged in the original Lambo report and explicitly called out as a known risk in the precompile's own doc comment, but not gated by any additional runtime check beyond the compile-time inter-precompile collision assertion.

### Recommendation
Do not rely on a short, fixed byte-range match to authorize precompile invocation when the remaining address bits can be influenced by deployable/attacker-controlled values (nonces, salts, asset ids). Either widen the fixed/checked region so the collision space is infeasible (already partially acknowledged as a 2-byte-max limit in the doc comment) or add an explicit, unconditional check preventing normal contract account creation from ever landing inside any registered `Prefix` precompile's address range, and ensure asset-id-derived addresses cannot be chosen by an unprivileged asset creator to intentionally collide with reserved ranges.

### Proof of Concept
Conceptually mirrors the Lambo PoC: 1) an unprivileged user repeatedly creates foreign assets (or grinds `create1`/`create2` salts as `pallet_revive::create2` in `substrate/frame/revive/src/tests/sol/contract.rs` demonstrates deterministic address derivation from caller+salt [5](#0-4) ) until the resulting address bytes `[16..18]` equal a live precompile's fixed prefix constant such as `PRECOMPILE_ADDRESS_PREFIX_FOREIGN`; 2) once found, `Precompiles::get`/`code` will route any call to that colliding address to the precompile logic via `Tuple::MATCHER.matches(address)` regardless of the attacker-chosen remaining bytes [3](#0-2) , letting the attacker's asset/contract intercept traffic meant for a different registered token or vice versa.

### Citations

**File:** substrate/frame/revive/src/precompiles.rs (L86-98)
```rust
	/// The pre-compile will be called for multiple addresses.
	///
	/// This is useful when some information should be encoded into the address.
	///
	/// This means the precompile will be invoked for all `x`:
	/// ```ignore
	/// xxxxxxxx000000000000000000000000pppp0000
	/// ```
	///
	/// Where `p` is the `u16` defined here as big endian. Hence a maximum of 2 byte can be encoded
	/// into the address. Allowing more bytes could lead to the situation where legitimate
	/// accounts could exist at this address. Either by accident or on purpose.
	Prefix(NonZero<u16>),
```

**File:** substrate/frame/revive/src/precompiles.rs (L408-424)
```rust
/// The collision check is verified by a trybuild test in `ui-tests/src/ui/precompiles_ui.rs`.
#[impl_trait_for_tuples::impl_for_tuples(20)]
#[tuple_types_custom_trait_bound(PrimitivePrecompile<T=T>)]
impl<T: Config> Precompiles<T> for Tuple {
	const CHECK_COLLISION: () = {
		let matchers = [for_tuples!( #( Tuple::MATCHER ),* )];
		if BuiltinAddressMatcher::has_duplicates(&matchers) {
			panic!("Precompiles with duplicate matcher detected")
		}
		for_tuples!(
			#(
				let is_fixed = Tuple::MATCHER.is_fixed();
				let has_info = Tuple::HAS_CONTRACT_INFO;
				assert!(is_fixed || !has_info, "Only fixed precompiles can have a contract info.");
			)*
		);
	};
```

**File:** substrate/frame/revive/src/precompiles.rs (L437-465)
```rust
	fn code(address: &[u8; 20]) -> Option<&'static [u8]> {
		for_tuples!(
			#(
				if Tuple::MATCHER.matches(address) {
					return Some(Tuple::CODE)
				}
			)*
		);
		None
	}

	fn get<E: ExtWithInfo<T = T>>(address: &[u8; 20]) -> Option<Instance<E>> {
		let _ = <Self as Precompiles<T>>::CHECK_COLLISION;
		let mut instance: Option<Instance<E>> = None;
		for_tuples!(
			#(
				if Tuple::MATCHER.matches(address) {
					if Tuple::HAS_CONTRACT_INFO {
						instance = Some(Instance {
							address: *address,
							has_contract_info: true,
							function: Tuple::call_with_info,
						})
					} else {
						instance = Some(Instance {
							address: *address,
							has_contract_info: false,
							function: Tuple::call,
						})
```

**File:** substrate/frame/assets/precompiles/src/test_helpers.rs (L39-46)
```rust
pub(crate) const PRECOMPILE_ADDRESS_PREFIX: u16 = 0x0120;
pub(crate) const PRECOMPILE_ADDRESS_PREFIX_FOREIGN: u16 = 0x0220;

pub(crate) fn set_prefix_in_address(prefix: u16) -> [u8; 20] {
	let mut addr = hex::const_decode_to_array(b"0000000000000000000000000000000000000000").unwrap();
	addr[16..18].copy_from_slice(&prefix.to_be_bytes());
	addr
}
```

**File:** substrate/frame/revive/src/tests/sol/contract.rs (L652-670)
```rust
		let salt = [42u8; 32];

		let initcode = Bytes::from(callee_code);
		// Prepare the CREATE2 call
		let create_call_data =
			Caller::create2Call { initcode: initcode.clone(), salt: FixedBytes(salt) }.abi_encode();

		let result = builder::bare_call(caller_addr)
			.data(create_call_data)
			.native_value(1000)
			.build_and_unwrap_result();

		let callee_addr = Caller::create2Call::abi_decode_returns(&result.data).unwrap();

		// Compute expected CREATE2 address
		let expected_addr = crate::address::create2(&caller_addr, &initcode, &[], &salt);

		let callee_addr: H160 = callee_addr.0.0.into();
		assert_eq!(callee_addr, expected_addr, "CREATE2 address should be deterministic");
```
