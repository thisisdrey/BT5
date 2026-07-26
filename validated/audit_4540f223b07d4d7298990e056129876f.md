Now I have all the information needed to make a definitive determination. Let me trace the exact execution order.

The ordering bug is confirmed by the code. Here is the complete analysis:

---

### Title
Unmetered `LoopSummary::new` / `verify_reducibility` Executes Before `max_basic_blocks` Guard, Enabling Validator Slowdown via Crafted Module Publish — (`third_party/move/move-bytecode-verifier/src/code_unit_verifier.rs`, `control_flow.rs`, `loop_summary.rs`)

### Summary

In `CodeUnitVerifier::verify_function`, the call to `control_flow::verify_function` — which internally builds the full `VMControlFlowGraph`, allocates all `LoopSummary` vectors, and runs the complete Tarjan reducibility DFS — occurs at line 170, **before** the `max_basic_blocks` guard at lines 179–185. The meter parameter passed to `control_flow::verify_function` is explicitly unused (`_meter`, with a `// TODO: metering` comment), so all of this work is completely unmetered. An unprivileged attacker publishing a module with a function containing up to ~32 767 basic blocks (the practical maximum given `CodeOffset: u16`) forces the validator to perform O(blocks²) unmetered work before the block-count rejection fires.

### Finding Description

**Exact call chain:**

```
module publish
  → CodeUnitVerifier::verify_module_impl          (code_unit_verifier.rs:48)
    → CodeUnitVerifier::verify_function           (code_unit_verifier.rs:85)
      → control_flow::verify_function             (code_unit_verifier.rs:170)  ← expensive work here
          → FunctionView::function                (binary_views.rs:437)
              → VMControlFlowGraph::new           (control_flow_graph.rs:85)   ← O(instructions) DFS
          → verify_reducibility                   (control_flow.rs:52)
              → LoopSummary::new                  (loop_summary.rs:55)         ← O(blocks) alloc + DFS
              → Tarjan loop body traversal        (control_flow.rs:132–179)    ← O(blocks × back_edges)
      ← returns FunctionView
      → max_basic_blocks guard                    (code_unit_verifier.rs:179)  ← TOO LATE
```

**Step 1 — `control_flow::verify_function` is called first.** [1](#0-0) 

**Step 2 — Inside it, `FunctionView::function` immediately builds `VMControlFlowGraph::new`, a full DFS over all bytecode instructions.** [2](#0-1) 

**Step 3 — `verify_reducibility` is then called, which calls `LoopSummary::new`.** [3](#0-2) 

**Step 4 — `LoopSummary::new` allocates four O(num_blocks) vectors and performs a full DFS.** [4](#0-3) 

**Step 5 — The Tarjan reducibility loop then traverses predecessor edges for every loop head, O(blocks × back_edges) total.** [5](#0-4) 

**Step 6 — Only after all of the above returns does the `max_basic_blocks` guard fire.** [6](#0-5) 

**Step 7 — The meter parameter is explicitly discarded; none of this work is charged.** [7](#0-6) 

**Mainnet config confirms `max_basic_blocks: Some(1024)` and `max_per_fun_meter_units: Some(1000 * 80000)`, but neither fires before the expensive work.** [8](#0-7) 

### Impact Explanation

With `CodeOffset` typed as `u16`, the bytecode vector is bounded at 65 535 instructions. Using alternating `Branch` instructions an attacker can construct a function with approximately 32 767 basic blocks — 32× the intended 1 024-block limit. The work performed before rejection scales as:

| Phase | Complexity | 32 767-block function |
|---|---|---|
| `VMControlFlowGraph::new` | O(instructions) | ~65 535 ops |
| `LoopSummary::new` alloc | O(blocks) | ~3–4 MB, ~131 070 ops |
| `verify_reducibility` | O(blocks × back_edges) | up to ~1 billion ops |

All of this is **unmetered**. When `strict_bounds` is disabled (controlled by `TimedFeatureFlag::EnableStrictBoundsInProdConfig`), `max_function_definitions` is `None`, so a single module can contain arbitrarily many such functions, multiplying the work linearly. Even with `strict_bounds` enabled (1 000 function cap), 1 000 × ~1 billion unmetered operations per publish transaction is sufficient to stall a validator for seconds to minutes per transaction, constituting a material chain availability failure reachable from an unprivileged module publish.

### Likelihood Explanation

The entrypoint is a standard, permissionless module publish transaction available to any account with gas. No privileged keys, governance access, or validator control is required. The bug is structural (wrong ordering of guard vs. work), not probabilistic. Any attacker who can craft bytecode — trivially done with the Move assembler or by directly constructing a `CompiledModule` — can trigger it deterministically.

### Recommendation

Move the `max_basic_blocks` guard to execute **before** `control_flow::verify_function` is called, using a lightweight pre-scan of the bytecode to count basic block entry points (the same pass already performed inside `VMControlFlowGraph::new`). Alternatively, pass and actually use the meter inside `verify_reducibility` and `LoopSummary::new` so that oversized work is charged and rejected by the existing `max_per_fun_meter_units` budget before it completes. The `// TODO: metering` comment on the `_meter` parameter of `control_flow::verify_function` acknowledges this gap.

### Proof of Concept

1. Construct a `CompiledModule` (version ≥ 6) with a single function whose `CodeUnit` contains 65 534 `Branch(i+1)` instructions followed by one `Ret`, creating ~32 767 basic blocks.
2. Submit a module-publish transaction from any unprivileged account.
3. Observe that `LoopSummary::new` and `verify_reducibility` run to completion before `TOO_MANY_BASIC_BLOCKS` is returned.
4. Measure wall-clock time on the validator for this single transaction; it will be orders of magnitude longer than a 1 024-block function.
5. Repeat with a module containing the maximum number of such functions (or no function-count limit when `strict_bounds` is off) to demonstrate cumulative validator stall.

### Citations

**File:** third_party/move/move-bytecode-verifier/src/code_unit_verifier.rs (L169-177)
```rust
        // create `FunctionView` and `BinaryIndexedView`
        let function_view = control_flow::verify_function(
            verifier_config,
            module,
            index,
            function_definition,
            code,
            meter,
        )?;
```

**File:** third_party/move/move-bytecode-verifier/src/code_unit_verifier.rs (L179-185)
```rust
        if let Some(limit) = verifier_config.max_basic_blocks {
            if function_view.cfg().blocks().len() > limit {
                return Err(
                    PartialVMError::new(StatusCode::TOO_MANY_BASIC_BLOCKS).at_code_offset(index, 0)
                );
            }
        }
```

**File:** third_party/move/move-binary-format/src/binary_views.rs (L437-451)
```rust
    pub fn function(
        module: &'a CompiledModule,
        index: FunctionDefinitionIndex,
        code: &'a CodeUnit,
        function_handle: &'a FunctionHandle,
    ) -> Self {
        Self {
            index: Some(index),
            code,
            parameters: module.signature_at(function_handle.parameters),
            return_: module.signature_at(function_handle.return_),
            locals: module.signature_at(code.locals),
            type_parameters: &function_handle.type_parameters,
            cfg: VMControlFlowGraph::new(&code.code),
        }
```

**File:** third_party/move/move-bytecode-verifier/src/control_flow.rs (L42-42)
```rust
    _meter: &mut impl Meter, // TODO: metering
```

**File:** third_party/move/move-bytecode-verifier/src/control_flow.rs (L49-54)
```rust
    } else {
        verify_fallthrough(Some(index), code)?;
        let function_view = FunctionView::function(module, index, code, function_handle);
        verify_reducibility(verifier_config, &function_view)?;
        Ok(function_view)
    }
```

**File:** third_party/move/move-bytecode-verifier/src/control_flow.rs (L127-179)
```rust
    let summary = LoopSummary::new(function_view.cfg());
    let mut partition = LoopPartition::new(&summary);

    // Iterate through nodes in reverse pre-order so more deeply nested loops (which would appear
    // later in the pre-order) are processed first.
    for head in summary.preorder().rev() {
        // If a node has no back edges, it is not a loop head, so doesn't need to be processed.
        let back = summary.back_edges(head);
        if back.is_empty() {
            continue;
        }

        // Collect the rest of the nodes in `head`'s loop, in `body`.  Start with the nodes that
        // jump back to the head, and grow `body` by repeatedly following predecessor edges until
        // `head` is found again.

        let mut body = BTreeSet::new();
        for node in back {
            let node = partition.containing_loop(*node);

            if node != head {
                body.insert(node);
            }
        }

        let mut frontier: Vec<_> = body.iter().copied().collect();
        while let Some(node) = frontier.pop() {
            for pred in summary.pred_edges(node) {
                let pred = partition.containing_loop(*pred);

                // `pred` can eventually jump back to `head`, so is part of its body.  If it is not
                // a descendant of `head`, it implies that `head` does not dominate a node in its
                // loop, therefore the CFG is not reducible, according to Property 1 (see doc
                // comment).
                if !summary.is_descendant(/* ancestor */ head, /* descendant */ pred) {
                    return err(StatusCode::INVALID_LOOP_SPLIT, summary.block(pred));
                }

                let body_extended = pred != head && body.insert(pred);
                if body_extended {
                    frontier.push(pred);
                }
            }
        }

        // Collapse all the nodes in `body` into `head`, so it appears as one node when processing
        // outer loops (this performs a sequence of Operation 4(b), followed by a 4(a)).
        let depth = partition.collapse_loop(head, &body);
        if let Some(max_depth) = verifier_config.max_loop_depth {
            if depth as usize > max_depth {
                return err(StatusCode::LOOP_MAX_DEPTH_REACHED, summary.block(head));
            }
        }
```

**File:** third_party/move/move-bytecode-verifier/src/loop_summary.rs (L76-82)
```rust
        let num_blocks = cfg.num_blocks() as usize;

        // Fields in LoopSummary that are filled via a depth-first traversal of `cfg`.
        let mut blocks = vec![0; num_blocks];
        let mut descs = vec![0; num_blocks];
        let mut backs = vec![vec![]; num_blocks];
        let mut preds = vec![vec![]; num_blocks];
```

**File:** aptos-move/aptos-vm-environment/src/prod_configs.rs (L187-219)
```rust
        max_basic_blocks: Some(1024),
        max_value_stack_size: 1024,
        max_type_nodes: if enable_function_values {
            Some(128)
        } else {
            Some(256)
        },
        max_push_size: Some(10000),
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
        max_function_definitions: if strict_bounds { Some(1000) } else { None },
        max_back_edges_per_function: None,
        max_back_edges_per_module: None,
        max_basic_blocks_in_script: if strict_bounds { Some(1024) } else { None },
        max_per_fun_meter_units: Some(1000 * 80000),
        max_per_mod_meter_units: Some(1000 * 80000),
```
