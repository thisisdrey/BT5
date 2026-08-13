# Q3715: lending_account_start_flashloan: flashloan close accepts a manipulated pre/post snapshot [a-bundle-that-mixes-flashloan] [tx-shape]

## Question
Can an unprivileged attacker call `lending_account_start_flashloan` with a bundle that mixes flashloan start with order or liquidation setup so `lending_account_start_flashloan` validates the flashloan from a manipulable snapshot rather than the true post-state, breaking `starting a flashloan must prove a safe unique session and forbid any interleaving that could leak value or leave debt unresolved` and causing `Critical: direct theft or unbacked debt through flashloan misuse`? Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs` / `lending_account_start_flashloan`
- Entrypoint: `lending_account_start_flashloan`
- Attacker controls: a bundle that mixes flashloan start with order or liquidation setup
- Exploit idea: Probe whether balance deltas can be hidden behind same-slot transfers, ATA substitutions, or reused accounts between start and end. Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.
- Invariant to test: starting a flashloan must prove a safe unique session and forbid any interleaving that could leak value or leave debt unresolved
- Expected Immunefi impact: Critical: direct theft or unbacked debt through flashloan misuse
- Fast validation: Instrument snapshots before and after the controlled path and assert every accepted close matches the actual economic delta for the right vault. Generate adversarial instruction layouts, duplicate metas, and sibling calls and assert session guards reject every non-canonical shape.
