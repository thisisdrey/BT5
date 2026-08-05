### Title
Oracle median aggregation returns the wrong element for even-sized operator sets, allowing a lower-bound-fed price to be adopted as the trusted oracle value - (File: `substrate/frame/honzon/oracle/src/default_combine_data.rs`)

### Summary
`DefaultCombineData::combine_data` is the median-aggregation logic used by the Oracle pallet (`substrate/frame/honzon/oracle/src/lib.rs`) to turn multiple operator-submitted `TimestampedValue`s into the single trusted `Values` entry that is exposed to consumers via `DataProvider`/`get_value`. Exactly like the reported `NFTFloorOracle._combine()` bug, when the number of valid (non-expired) submissions is even, the code does not average the two middle values — it simply selects a single element at index `count / 2`, which is the *upper* of the two middle elements in sorted order.

### Finding Description
`combine_data` filters out expired timestamped values, then computes:

```
let mid_index = count / 2;
let (_, value, _) = values.select_nth_unstable_by(mid_index as usize, |a, b| a.value.cmp(&b.value));
Some(value.clone())
``` [1](#0-0) 

`select_nth_unstable_by` with `mid_index = count / 2` on a 0-indexed sorted array picks the element that is one position past the true lower-middle. For an even-length array the correct median is the average of index `count/2 - 1` and `count/2`; this code instead returns only the value at `count/2`, which is always the *higher* of the two central values (or lower, depending on distribution) — never their average. This mirrors the exact defect described in the external report for `NFTFloorOracle._combine()`: with `values = [1000, 1500, 2250, 3375]`, the correct median is `1875` but the pallet returns `2250`, a ~20% deviation from the true median.

The same flawed pattern also appears in the standalone `median()` helper in `traits.rs` used by `create_median_value_data_provider!` [2](#0-1) , confirming this is a systemic, repeated bug in this pallet's aggregation code rather than an isolated typo.

### Impact Explanation
`Values` (the aggregated oracle price) is read by any downstream pallet via the `DataProvider`/`DataProviderExtended` trait implementations [3](#0-2)  and by the public `get_value`/`all_values` view functions [4](#0-3) . Because the aggregation logic is called on every `feed_values` dispatch via `combined()` [5](#0-4) , any consumer relying on this pallet for price-dependent decisions (e.g., collateral valuation, liquidation thresholds, loan health checks in a CDP-style module built atop this oracle) will systematically receive a skewed value whenever the operator set that has submitted valid, non-expired data happens to be even in size. This is a data-integrity bug in the intended aggregation algorithm — not privileged misuse — and directly corresponds to the "runtime bugs that compromise intended behavior" impact class.

### Likelihood Explanation
The condition triggers deterministically whenever the number of currently-valid raw submissions for a key is even — a state that occurs naturally as operators submit/expire data over time and requires no malicious operator collusion, admin action, or privileged access. It is a straightforward logic/off-by-one class defect reachable through the normal `feed_values` extrinsic path used by any legitimate oracle member, making the trigger condition entirely within the reach of ordinary chain operation. The magnitude of the resulting deviation grows with the price spread between the two middle submissions, which the report's PoC shows can reach ~20% in realistic distributions.

### Recommendation
Fix `combine_data` (and the analogous `median()` helper in `traits.rs`) to correctly implement the median formula: for odd `count`, return the value at `count/2`; for even `count`, average the values at `count/2 - 1` and `count/2` (or otherwise document/adjust the pallet's median semantics to explicitly define "select the upper of the two middle values" as an accepted behavior, if that is the intended design). Since `OracleValue` is a generic `Parameter + Member + Ord` type without a guaranteed arithmetic "average" operation, the fix likely requires adding a numeric bound (e.g., requiring `Saturating`/`CheckedAdd`+division or a configurable averaging trait) so genuine averaging can be performed generically.

### Proof of Concept
Given four operators feeding valid (non-expired) values for the same key, sorted ascending: `[1000, 1500, 2250, 3375]`.

- `count = 4`, `mid_index = 4 / 2 = 2`.
- `select_nth_unstable_by(2, ...)` places `2250` at index 2 and returns it as the "median."
- The mathematically correct median is `(1500 + 2250) / 2 = 1875`.
- The pallet's `Values` storage is updated to `2250` instead of `1875` — a 375-unit (~20%) upward deviation — exactly reproducing the external report's PoC pattern inside `substrate/frame/honzon/oracle/src/default_combine_data.rs`.

### Citations

**File:** substrate/frame/honzon/oracle/src/default_combine_data.rs (L46-59)
```rust
		values.retain(|x| x.timestamp.saturating_add(expires_in) > now);

		let count = values.len() as u32;
		let minimum_count = MinimumCount::get();
		if count < minimum_count || count == 0 {
			return prev_value;
		}

		let mid_index = count / 2;
		// Won't panic as `values` ensured not empty.
		let (_, value, _) =
			values.select_nth_unstable_by(mid_index as usize, |a, b| a.value.cmp(&b.value));
		Some(value.clone())
	}
```

**File:** substrate/frame/honzon/oracle/src/traits.rs (L41-52)
```rust
/// Returns the median of a list of values.
pub fn median<T: Ord + Clone>(mut items: Vec<T>) -> Option<T> {
	if items.is_empty() {
		return None;
	}

	let mid_index = items.len() / 2;

	// Won't panic as `items` ensured not empty.
	let (_, item, _) = items.select_nth_unstable(mid_index);
	Some(item.clone())
}
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L313-324)
```rust
	#[pallet::view_functions]
	impl<T: Config<I>, I: 'static> Pallet<T, I> {
		/// Retrieve the aggregated oracle value for a specific key, including its timestamp.
		pub fn get_value(key: T::OracleKey) -> Option<TimestampedValueOf<T, I>> {
			Self::get(&key)
		}

		/// Retrieve every aggregated oracle value tracked by the pallet.
		pub fn all_values() -> Vec<(T::OracleKey, TimestampedValueOf<T, I>)> {
			<Values<T, I>>::iter().collect()
		}
	}
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L400-403)
```rust
	fn combined(key: &T::OracleKey) -> Option<TimestampedValueOf<T, I>> {
		let values = Self::read_raw_values(key);
		T::CombineData::combine_data(key, values, Self::values(key))
	}
```

**File:** substrate/frame/honzon/oracle/src/lib.rs (L449-460)
```rust
impl<T: Config<I>, I: 'static> DataProvider<T::OracleKey, T::OracleValue> for Pallet<T, I> {
	fn get(key: &T::OracleKey) -> Option<T::OracleValue> {
		Self::get(key).map(|timestamped_value| timestamped_value.value)
	}
}
impl<T: Config<I>, I: 'static> DataProviderExtended<T::OracleKey, TimestampedValueOf<T, I>>
	for Pallet<T, I>
{
	fn get_all_values() -> impl Iterator<Item = (T::OracleKey, Option<TimestampedValueOf<T, I>>)> {
		<Values<T, I>>::iter().map(|(k, v)| (k, Some(v)))
	}
}
```
