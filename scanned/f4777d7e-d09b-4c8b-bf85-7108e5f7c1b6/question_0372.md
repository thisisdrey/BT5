# Q0372: ema_function can deadlock or livelock the node (stream_throttle.rs)

## Question
Can an unprivileged attacker entering through raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port reach `ema_function` in `streamer/src/nonblocking/stream_throttle.rs` with arguments that drive the path into its error branch after side effects were applied, and hold two of the locks `ema_function` touches in an order that stalls forward progress, so that the invariant "Locks in `ema_function` are always acquired in a total order and released on every path, including error paths." breaks and the result is Liveness / Loss of Availability?

## Target
- File/function: `streamer/src/nonblocking/stream_throttle.rs` -> `ema_function()` (around line 87)
- Entrypoint: raw QUIC/UDP packets and transaction batches sent by an unstaked client to the leader's public TPU port
- Attacker controls: arguments that drive the path into its error branch after side effects were applied
- Exploit idea: Create a lock/channel ordering through `ema_function` that two attacker transactions can hold simultaneously, stalling banking or replay indefinitely.
- Invariant to test: Locks in `ema_function` are always acquired in a total order and released on every path, including error paths.
- Expected Immunefi impact: Liveness / Loss of Availability - consensus halts and requires human intervention (1,250-5,000 SOL)
- Fast validation: Loom or stress test the concurrent path; assert forward progress under adversarial interleavings.

## Bounty scope note
In-scope target per anza-xyz/agave SECURITY.md. Assumes no validator, leader,
staked-node, peer, gossip, operator, or leaked-key capability. Folder scope:
Critical. An unprivileged remote client can send TPU/QUIC traffic or transaction packets that panic, deadlock, or unboundedly grow memory in ingress, sigverify, scheduling, or PoH, halting block production across the cluster.
