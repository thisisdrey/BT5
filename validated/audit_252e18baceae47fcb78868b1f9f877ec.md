Now confirmed: in `handle_connection` (`streamer/src/nonblocking/quic.rs:624`), the order is:

```rust
qos.on_new_stream(&context).await;   // throttle: checks stream_count >= limit, may sleep
qos.on_stream_accepted(&context);    // increments stream_count AFTER throttle check
``` [1](#0-0) 

`on_new_stream` (in `SwQos`, `streamer/src/nonblocking/swqos.rs:497-515`) calls `throttle_stream`, which reads `stream_counter.stream_count` and compares it to `max_streams_per_throttling_interval`, sleeping if at/above the limit — but does **not** increment the counter itself. [2](#0-1)  The increment only happens afterward, in `on_stream_accepted`: [3](#0-2)  And the check itself, in `throttle_stream`: [4](#0-3) 

This is the same bug class as the report: a limit/state check is performed and acted upon (or in this case, skipped) using a value that has not yet been updated with the effect of the current operation, and the "increment" (accounting update) is deferred to a call that happens strictly after the check. Since each stream is processed in its own loop iteration on the same connection task (no concurrent streams per connection in this handler), a single client cannot exploit this via one connection to bypass throttling meaningfully in-order — but across the *life* of the connection this ordering means the very first stream(s) in a burst are never counted before the check, and more importantly the accounting window (`reset_throttling_params_if_needed`) plus the check race allows the counter to under-count concurrent open streams. I could not fully verify a cross-stream concurrency path within the same connection (QUIC allows multiple concurrently open uni streams per connection, and `handle_connection`'s outer `'conn: loop` accepts one stream at a time sequentially per task, but note the loop only awaits `connection.accept_uni()` — each accepted stream spawns its own read loop is NOT the case here, it's all in one task), so I present this with the caveat below.

### Title
Stream throttle check reads stale counter before increment, allowing per-connection QUIC stream-rate limit evasion - (File: streamer/src/nonblocking/stream_throttle.rs)

### Summary
`throttle_stream` decides whether to sleep (enforce backpressure) based on `stream_counter.stream_count`, but the counter is only incremented afterward in `on_stream_accepted`. This mirrors the "mint after balance check" bug class: the accounting update needed to make the check accurate happens after the check consumes the (stale) value.

### Finding Description
In `handle_connection`, for every accepted QUIC stream the code calls `qos.on_new_stream(&context).await` first, then `qos.on_stream_accepted(&context)`. [1](#0-0) 

`SwQos::on_new_stream` invokes `throttle_stream`, which loads `stream_counter.stream_count` and compares it against `max_streams_per_throttling_interval`; if below the limit, it returns immediately without touching the counter. [4](#0-3) 

Only after `on_new_stream` returns does `on_stream_accepted` perform `stream_count.fetch_add(1, ...)`. [3](#0-2) 

Because the read-then-decide step in `throttle_stream` uses the pre-increment value, the throttle only "feels" the effect of a stream after the fact — the same ordering flaw as the reported `collectRewardToken` bug (state effect applied after the check/read that should have accounted for it).

### Impact Explanation
This affects the QoS/rate-limiting subsystem in the QUIC streamer, which is in the unprivileged-user-reachable ingress path (any peer opening streams on an accepted connection). If the throttle counter under-counts in-flight streams relative to the enforced limit, a peer can push more streams per throttling interval than intended, evading the per-connection QUIC stream-rate limit (QoS evasion), potentially increasing packet-ingestion load on the validator beyond the intended cap.

### Likelihood Explanation
The ordering bug is deterministic in code structure (check always precedes increment on every single stream), but the practical exploitability depends on whether `handle_connection`'s stream-accept loop is strictly sequential per connection (it appears to be — one `accept_uni()` at a time within the `'conn` loop), which would limit the observable skew to at most one stream's worth of slack per throttling window rather than unbounded evasion. I was not able to fully confirm within the available search whether concurrent streams on the same connection can bypass this check further (e.g., via multiple simultaneous `accept_uni` tasks), so the severity may be limited to an off-by-one undercount per interval rather than a large-scale QoS bypass.

### Recommendation
Increment `stream_count` (or otherwise account for the new stream) before or atomically with the throttle check in `throttle_stream`/`on_new_stream`, so the decision to throttle is based on the post-increment count, consistent with the debit-before-credit pattern already followed elsewhere in the codebase (e.g., cost tracker's `try_add` which validates then updates atomically under a single lock). [5](#0-4) 

### Proof of Concept
Not independently reproduced; based on static code-path analysis: `on_new_stream` (check) at [2](#0-1)  executes before `on_stream_accepted` (increment) at [3](#0-2) , called in that order from [1](#0-0) .

### Citations

**File:** streamer/src/nonblocking/quic.rs (L624-626)
```rust
        qos.on_new_stream(&context).await;
        qos.on_stream_accepted(&context);
        stats.active_streams.fetch_add(1, Ordering::Relaxed);
```

**File:** streamer/src/nonblocking/swqos.rs (L445-454)
```rust
    fn on_stream_accepted(&self, conn_context: &SwQosConnectionContext) {
        self.staked_stream_load_ema
            .increment_load(conn_context.peer_type);
        conn_context
            .stream_counter
            .as_ref()
            .unwrap()
            .stream_count
            .fetch_add(1, Ordering::Relaxed);
    }
```

**File:** streamer/src/nonblocking/swqos.rs (L496-515)
```rust
    #[allow(clippy::manual_async_fn)]
    fn on_new_stream(&self, context: &SwQosConnectionContext) -> impl Future<Output = ()> + Send {
        async move {
            let peer_type = context.peer_type();
            let remote_addr = context.remote_address;
            let stream_counter: &Arc<ConnectionStreamCounter> =
                context.stream_counter.as_ref().unwrap();

            let max_streams_per_throttling_interval =
                self.max_streams_per_throttling_interval(context);

            throttle_stream(
                &self.stats,
                peer_type,
                remote_addr,
                stream_counter,
                max_streams_per_throttling_interval,
            )
            .await;
        }
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L233-246)
```rust
pub(crate) async fn throttle_stream(
    stats: &StreamerStats,
    peer_type: ConnectionPeerType,
    remote_addr: std::net::SocketAddr,
    stream_counter: &Arc<ConnectionStreamCounter>,
    max_streams_per_throttling_interval: u64,
) {
    let throttle_interval_start = stream_counter.reset_throttling_params_if_needed();
    let streams_read_in_throttle_interval = stream_counter.stream_count.load(Ordering::Relaxed);
    if streams_read_in_throttle_interval >= max_streams_per_throttling_interval {
        // The peer is sending faster than we're willing to read. Sleep for what's
        // left of this read interval so the peer backs off.
        let throttle_duration =
            STREAM_THROTTLING_INTERVAL.saturating_sub(throttle_interval_start.elapsed());
```

**File:** cost-model/src/cost_tracker.rs (L167-177)
```rust
    pub fn try_add(
        &mut self,
        tx_cost: &TransactionCost<impl TransactionWithMeta>,
    ) -> Result<UpdatedCosts, CostTrackerError> {
        self.would_fit(tx_cost)?;
        let updated_costliest_account_cost = self.add_transaction_cost(tx_cost);
        Ok(UpdatedCosts {
            updated_block_cost: self.block_cost(),
            updated_costliest_account_cost,
        })
    }
```
