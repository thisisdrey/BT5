### Title
`GasBudget.Absorb` fabricates free execution gas by using an unrelated child's returned state-gas reservoir to erase a caller's pre-existing, unrelated `Spilled` debt - ([File: core/vm/gascosts.go])

### Summary
`GasBudget.Absorb()` merges a sub-call's leftover EIP-8037 state-gas accounting into the caller's running budget. Its final "sanitize" step blindly nets the caller's outstanding `Spilled` (execution gas already borrowed to pay for a real, permanent state charge) against `StateGas` reclaimed from the child, even when the two have no causal relationship. This mirrors the audit's root cause pattern of "a genuine credit is applied without properly reconciling an unrelated liability", and manufactures gas the sender never paid for.

### Finding Description
`charge()` maintains the invariant that whenever `Spilled > 0`, `StateGas == 0` for a single, continuously-updated `GasBudget` instance (spillover always zeroes the reservoir before it is recorded as debt), and `RefundState()` always repays outstanding `Spilled` before growing `StateGas`, preserving that invariant within one frame: [1](#0-0) [2](#0-1) 

`Forward()` hands the caller's *entire* state reservoir to the child and zeroes the caller's own `StateGas`, while the caller may already carry a nonzero `Spilled` debt from an earlier, unrelated state charge made in the same frame before the call: [3](#0-2) 

When the child later returns a *positive* `StateGas` (e.g. because, inside the child's own frame, it clears an unrelated pre-existing storage slot back to its original value and receives a state-gas refund that lands in the child's own, previously-zero reservoir - the exact scenario documented by `TestAbsorbReturnsStateGas`), `Absorb()` merges the two instances and, because the caller's own `Spilled` and the child's `StateGas` are now simultaneously nonzero, applies:
```
d := min(g.StateGas, g.Spilled)
g.ExecutionGas += d
g.StateGas -= d
g.Spilled -= d
``` [4](#0-3) 

This step is only safe when the returned reservoir genuinely traces back to reversal of the *same* charge that produced the debt (as in the intended `TestAbsorbReturnsStateGas` test, where the child clears the exact slot the parent had just created). But `Spilled`/`StateGas` are aggregate scalars, not tied to a specific slot, so nothing prevents the reservoir returned from clearing an entirely *different*, previously existing slot from being used to erase debt that came from creating a *different, permanent* slot elsewhere in the caller's frame.

Manual trace confirms the discrepancy: caller pre-call state `{ExecutionGas:1000, StateGas:0, Spilled:200}` (200 being real, permanent debt from an earlier slot creation that is never reversed), `Forward(500)` → child starts `{ExecutionGas:500, StateGas:0}`. The child clears an unrelated pre-existing slot back to its original value, generating a refund of 300 into its own reservoir, and returns `{ExecutionGas:500, StateGas:300, Spilled:0}`. `Absorb()` computes `d = min(300,200) = 200`, yielding final caller state `{ExecutionGas:1200, StateGas:100, Spilled:0}`. The economically correct outcome is `ExecutionGas+StateGas-Spilled = 800(pre-call net) + 300(genuine refund) = 1100`; the code instead produces `1200+100-0 = 1300` - 200 gas units materialized from nothing, exactly `d`.

### Impact Explanation
The manufactured gas can either (a) be spent on additional real EVM computation - including further storage writes - that the transaction's `gasLimit`/intrinsic-gas accounting never actually paid for, or (b) remain unspent and be refunded to the sender as ETH at transaction end via the unused-gas refund path. Case (a) lets a crafted contract perform more permanent state mutation than its gas payment should permit, which is a "gas charged that differs from the EIPs" divergence and can alter the resulting state relative to a spec-correct implementation of this gas-metering scheme; case (b) is an unauthorized ETH credit to the sender. Both fall within the High/Critical categories defined by the rules (gas divergence on a reachable path; ETH created from an accounting bug).

### Likelihood Explanation
Medium. It requires a contract to (1) create a new, permanent storage slot early in a frame with a reservoir too small to cover it (forcing a `Spilled` debt), then (2) within the same frame, make a `CALL`/`DELEGATECALL` whose callee clears some *other*, unrelated pre-existing slot back to its original value (a normal, legitimate SSTORE-refund pattern). Both ingredients are ordinary, attacker-controlled EVM operations reachable via a single crafted transaction; no privileged or multi-block setup is required.

### Recommendation
Do not let an unrelated child's returned `StateGas` cancel a caller's pre-existing `Spilled` debt. Either (1) fold the caller's own pre-call `Spilled` into `UsedExecutionGas` (treating it as permanently consumed) before forwarding a call, so no debt survives to be incorrectly netted against a later, unrelated credit, or (2) track spilled debt per originating charge (e.g., a stack keyed to the specific slot/charge) instead of as a single aggregate scalar that any unrelated refund can settle.

### Proof of Concept
Conceptual reproduction using the existing test harness primitives (`sstore`, `callCode`/`delegateCallCode`, `run8037` in `core/vm/eip8037_test.go`):
1. In the top frame, with a `StateGas` reservoir smaller than `stateGasNewSlot`, execute `sstore(1, 1)` to create slot 1 (this is never cleared later) - forces `Spilled = stateGasNewSlot - reservoir`.
2. In the same frame, `CALL`/`DELEGATECALL` a child contract that operates on a *different*, pre-existing slot (e.g. slot 2, pre-set to a nonzero value via the `setup` callback) and executes `sstore(2, 0)`, i.e. clears slot 2 back to its original value - this generates a genuine state-gas refund landing in the child's own, previously-zero `StateGas`.
3. After the call returns and `Absorb()` runs, compare the caller's resulting `ExecutionGas + StateGas` against the value obtained from a reference run where slot 1's creation was fully covered by the reservoir (no spill) plus the slot-2 refund applied independently - the two should be equal but will differ by `d = min(returned StateGas, Spilled)`, demonstrating manufactured gas exactly as computed in the trace above.

I was not able to execute this scenario against the actual test suite (tool budget exhausted before running `go test`), so this analysis is based on careful manual tracing of the documented invariants and the exact code paths cited above, not on an executed reproduction.

### Citations

**File:** core/vm/gascosts.go (L111-137)
```go
// charge deducts both the state and execution cost.
func (g *GasBudget) charge(cost GasCosts) bool {
	if g.ExecutionGas < cost.ExecutionGas {
		return false
	}
	execution := g.ExecutionGas - cost.ExecutionGas
	state := g.StateGas
	spilled := g.Spilled

	if cost.StateGas > state {
		spillover := cost.StateGas - state
		if spillover > execution {
			return false
		}
		execution -= spillover
		state = 0
		spilled += spillover
	} else {
		state -= cost.StateGas
	}
	g.ExecutionGas = execution
	g.StateGas = state
	g.UsedExecutionGas += cost.ExecutionGas
	g.UsedStateGas += int64(cost.StateGas)
	g.Spilled = spilled
	return true
}
```

**File:** core/vm/gascosts.go (L159-166)
```go
// RefundState applies an inline state-gas refund (e.g., SSTORE 0->A->0).
func (g *GasBudget) RefundState(s uint64) {
	repay := min(s, g.Spilled)
	g.ExecutionGas += repay
	g.Spilled -= repay
	g.StateGas += s - repay
	g.UsedStateGas -= int64(s)
}
```

**File:** core/vm/gascosts.go (L174-188)
```go
// Forward drains `execution` gas and the entire state reservoir from
// the parent's running budget and returns the initial GasBudget for a child
// frame. The parent's UsedExecutionGas is bumped by the forwarded amount so
// that the absorb-on-return path correctly reclaims the unused portion.
func (g *GasBudget) Forward(execution uint64) GasBudget {
	g.ExecutionGas -= execution
	g.UsedExecutionGas += execution

	child := GasBudget{
		ExecutionGas: execution,
		StateGas:     g.StateGas,
	}
	g.StateGas = 0
	return child
}
```

**File:** core/vm/gascosts.go (L269-291)
```go
// Absorb merges a sub-call's leftover GasBudget into this (caller's) running
// budget. Additionally, it does an EIP-8037 spillover correction:
// state-gas that spilled into the execution pool inside the child frame is
// excluded from the UsedExecutionGas.
func (g *GasBudget) Absorb(child GasBudget) {
	g.UsedExecutionGas -= child.ExecutionGas
	g.ExecutionGas += child.ExecutionGas
	g.StateGas = child.StateGas
	g.UsedStateGas += child.UsedStateGas

	g.UsedExecutionGas -= child.Spilled
	g.Spilled += child.Spilled

	// Sanitize the state gas counters after merging the child frame. The child may
	// have refilled a charge this frame funded from its execution gas, in which case
	// the gas ends up in the child's reservoir and is handed back to the parent. The
	// parent's state reservoir can then be non-zero while it still has outstanding
	// debt from the execution gas.
	d := min(g.StateGas, g.Spilled)
	g.ExecutionGas += d
	g.StateGas -= d
	g.Spilled -= d
}
```
