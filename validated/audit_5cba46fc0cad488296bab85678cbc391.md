No vulnerability found for this question.

The reported issue is a Solidity smart-contract anti-pattern (using `tx.origin` for authentication/authorization inside a DeFi contract, e.g. DODO's `GSPTrader.sol`). It does not map onto anything in the go-ethereum codebase's in-scope packages. The only place `Origin` appears in scope is as the EVM's implementation of the `ORIGIN` opcode itself: `TxContext.Origin` is populated from `msg.From` in `NewEVMTxContext` and exposed to running bytecode via the `ORIGIN` opcode [1](#0-0) [2](#0-1) . This is exactly the correct, spec-conforming behavior for the `ORIGIN` opcode (EIP/Yellow Paper semantics) — geth is not itself using `tx.origin` for any access-control decision, so there is no equality being broken (no consensus divergence, no unauthorized ETH/code change, no gas/receipt divergence, no persistence mismatch). The underlying report is a best-practice recommendation about application-level contract code, which is explicitly out of scope for this analog scan.

### Citations

**File:** core/evm.go (L87-95)
```go
// NewEVMTxContext creates a new transaction context for a single transaction.
func NewEVMTxContext(msg *Message) vm.TxContext {
	ctx := vm.TxContext{
		Origin:     msg.From,
		GasPrice:   msg.GasPrice,
		BlobHashes: msg.BlobHashes,
	}
	return ctx
}
```

**File:** core/vm/evm.go (L74-82)
```go
// TxContext provides the EVM with information about a transaction.
// All fields can change between transactions.
type TxContext struct {
	// Message information
	Origin       common.Address      // Provides information for ORIGIN
	GasPrice     *uint256.Int        // Provides information for GASPRICE (and is used to zero the basefee if NoBaseFee is set)
	BlobHashes   []common.Hash       // Provides information for BLOBHASH
	AccessEvents *state.AccessEvents // Capture all state accesses for this tx
}
```
