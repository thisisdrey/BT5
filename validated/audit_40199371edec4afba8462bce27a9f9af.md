#No Vulnerability found for this question.

Analysis: The behavior described—`CongestionControl::process_tx_limit()` at [1](#0-0)  reducing gas spent on converting transactions to receipts as a shard's `incoming_congestion` rises—is the intended, documented design of NEP-539 congestion control, not an implementation flaw. The config explicitly documents this mixing behavior: [2](#0-1)  and the fair-throughput throttling is a shared, per-shard resource by design, applying equally to any transaction targeting that shard regardless of sender. `chunk_tx_gas_limit` simply reads the shard's own previous-block congestion info and applies the linear interpolation via `mix_gas`, exactly as specified and unit-tested in `test_incoming_congestion` [3](#0-2) . There is no privilege escalation, fund loss, freezing, double-spend, or state-root divergence—only a documented, self-inflicted-cost throughput reduction that any heavy legitimate workload on a shard would equally cause (the same mechanism that legitimately protects the network from unbounded queue growth). Producing many self-targeted delayed receipts costs the attacker real attached gas/deposit and burns their own funds proportionally to the congestion inflicted, and any account (malicious or not) generating a comparable receipt backlog would have the identical effect. This is a known, accepted trade-off of a shared per-shard gas budget, not a vulnerability, and falls under the "speculative resource-hygiene claims with no reachable mainnet scenario" / no matching real-impact category per the rules.

### Citations

**File:** core/primitives/src/congestion_info.rs (L115-117)
```rust
    pub fn process_tx_limit(&self) -> Gas {
        mix_gas(self.config.max_tx_gas, self.config.min_tx_gas, self.incoming_congestion())
    }
```

**File:** core/primitives/src/congestion_info.rs (L670-721)
```rust
    #[test]
    fn test_incoming_congestion() {
        let config = get_config();
        let mut info = CongestionInfo::default();

        info.add_delayed_receipt_gas(config.max_congestion_incoming_gas).unwrap();
        info.add_delayed_receipt_gas(Gas::from_gas(500)).unwrap();
        info.remove_delayed_receipt_gas(Gas::from_gas(500)).unwrap();

        {
            let control = CongestionControl::new(config, info, 0);
            assert_eq!(1.0, control.congestion_level());
            // fully congested, no more forwarding allowed
            assert_eq!(Gas::ZERO, control.outgoing_gas_limit(ShardId::new(1)));
            assert!(control.shard_accepts_transactions().is_no());
            // processing to other shards is restricted by own incoming congestion
            assert_eq!(config.min_tx_gas, control.process_tx_limit());
        }

        // Assert threshold is 80%. Change this number if the config changes
        assert_eq!(0.8, config.reject_tx_congestion_threshold);

        // reduce congestion to 80%
        info.remove_delayed_receipt_gas(config.max_congestion_incoming_gas.checked_div(5).unwrap())
            .unwrap();
        {
            let control = CongestionControl::new(config, info, 0);
            assert_eq!(0.8, control.congestion_level());
            assert_eq!(
                mix_gas(config.max_outgoing_gas, config.min_outgoing_gas, 0.8),
                control.outgoing_gas_limit(ShardId::new(1))
            );
            // at 80%, still no new transactions are allowed
            assert!(control.shard_accepts_transactions().is_no());
        }

        // reduce congestion to 10%
        info.remove_delayed_receipt_gas(
            config.max_congestion_incoming_gas.checked_mul(7).unwrap().checked_div(10).unwrap(),
        )
        .unwrap();
        {
            let control = CongestionControl::new(config, info, 0);
            assert_eq!(0.1, control.congestion_level());
            assert_eq!(
                mix_gas(config.max_outgoing_gas, config.min_outgoing_gas, 0.1),
                control.outgoing_gas_limit(ShardId::new(1))
            );
            // at 10%, new transactions are allowed (threshold is 80%)
            assert!(control.shard_accepts_transactions().is_yes());
        }
    }
```

**File:** core/parameters/src/config.rs (L189-213)
```rust
    /// The maximum amount of gas in a chunk spent on converting new transactions to
    /// receipts.
    ///
    /// The actual gas forwarding allowance is a linear interpolation between
    /// [MIN_OUTGOING_GAS](CongestionControlConfig::min_outgoing_gas) and
    /// [MAX_OUTGOING_GAS](CongestionControlConfig::max_outgoing_gas),
    /// based on the incoming congestion of the local shard.
    /// Additionally, transactions can be rejected if the receiving
    /// remote shard is congested more than
    /// [REJECT_TX_CONGESTION_THRESHOLD](CongestionControlConfig::reject_tx_congestion_threshold)
    /// based on their general congestion level.
    pub max_tx_gas: Gas,

    /// The minimum amount of gas in a chunk spent on converting new transactions
    /// to receipts, as long as the receiving shard is not congested.
    ///
    /// The actual gas forwarding allowance is a linear interpolation between
    /// [MIN_OUTGOING_GAS](CongestionControlConfig::min_outgoing_gas) and
    /// [MAX_OUTGOING_GAS](CongestionControlConfig::max_outgoing_gas),
    /// based on the incoming congestion of the local shard.
    /// Additionally, transactions can be rejected if the receiving
    /// remote shard is congested more than
    /// [REJECT_TX_CONGESTION_THRESHOLD](CongestionControlConfig::reject_tx_congestion_threshold)
    /// based on their general congestion level.
    pub min_tx_gas: Gas,
```
