# Q2992: chunk validation re-execution divergence — event_type.rs

## Question
Can an unprivileged mainnet account, entering through a `FunctionCall` action into an attacker-deployed contract, a contract whose execution depends on data the witness does not fully determine, when transaction conversion cost alone approaches the chunk gas limit, and additionally when the pool is filled exactly to its bound by many attacker keys, reach `from_shard_layout` in `chain/chain/src/resharding/event_type.rs` and make stateless validation of an honestly produced chunk fail or produce a different root, breaking the invariant that re-execution from the witness reproduces the producer's result exactly, leading to Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard?

## Target
- File/function: `chain/chain/src/resharding/event_type.rs` :: `from_shard_layout`
- Entrypoint: a `FunctionCall` action into an attacker-deployed contract
- Attacker controls: a contract whose execution depends on data the witness does not fully determine; when transaction conversion cost alone approaches the chunk gas limit; when the pool is filled exactly to its bound by many attacker keys
- Exploit idea: make stateless validation of an honestly produced chunk fail or produce a different root
- Invariant to test: re-execution from the witness reproduces the producer's result exactly
- Expected Immunefi impact: Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard
- Fast validation: differential test re-executing the chunk from its witness
