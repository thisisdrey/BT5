# Q2413: validate_ix_first: instruction-hash binding is replayable across contexts [a-mixed-flashloan-and-order] [hash-replay]

## Question
Can an unprivileged attacker replay `lending_account_start_flashloan` with a mixed flashloan and order/liquidation bundle so `validate_ix_first` accepts an instruction-hash binding from the wrong context, violating `flashloan and other guarded multi-phase flows must enforce their exact instruction ordering assumptions` and causing `Critical: unauthorized intra-transaction state changes enabling theft or unbacked debt`? Focus specifically on replay of previously valid instruction hashes, layouts, or discriminator families across new contexts.

## Target
- File/function: `programs/marginfi/src/ix_utils.rs` / `validate_ix_first`
- Entrypoint: `lending_account_start_flashloan`
- Attacker controls: a mixed flashloan and order/liquidation bundle
- Exploit idea: Check that any hash/discriminator used to tie phases together is domain-separated by accounts, signer, and phase-specific state. Focus specifically on replay of previously valid instruction hashes, layouts, or discriminator families across new contexts.
- Invariant to test: flashloan and other guarded multi-phase flows must enforce their exact instruction ordering assumptions
- Expected Immunefi impact: Critical: unauthorized intra-transaction state changes enabling theft or unbacked debt
- Fast validation: Attempt cross-context replay with the same hash material and assert it cannot satisfy the guard for another account or phase. Reuse previously valid instruction layouts against new accounts and assert the guard cannot be satisfied cross-context.
