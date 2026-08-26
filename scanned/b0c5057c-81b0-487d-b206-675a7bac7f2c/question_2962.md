# Q2962: flexible-data encoding length fields — encoding.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys, values and extensions whose lengths sit at the exact encoding-width boundaries, with keys producing maximal-length extension nodes, and additionally with a value stored directly at a full 16-way branch node, reach `view_kind` in `core/store/src/trie/mem/node/encoding.rs` and make a length field truncate so a node decodes to different children than it encodes, breaking the invariant that flexible-data length fields cover the full range of representable node contents, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/store/src/trie/mem/node/encoding.rs` :: `view_kind`
- Entrypoint: attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys
- Attacker controls: values and extensions whose lengths sit at the exact encoding-width boundaries; with keys producing maximal-length extension nodes; with a value stored directly at a full 16-way branch node
- Exploit idea: make a length field truncate so a node decodes to different children than it encodes
- Invariant to test: flexible-data length fields cover the full range of representable node contents
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: unit test at each encoding-width boundary
