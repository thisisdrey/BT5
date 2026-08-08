[1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L31-50)
```rust
    /// Check if the connection from the said `ip` is allowed.
    /// Here we assume that only IPs with actual confirmed connections are stored in it,
    /// since we should only modify server state once source IP is verified
    pub fn is_allowed(&self, ip: &IpAddr) -> bool {
        // Check if we have records in the rate limiter for the given IP address
        match self.limiter.current_tokens(ip) {
            Some(r) => r > 0, // we have a record, and rate is not exceeded
            None => true,     // if we have not seen IP, allow connection request
        }
    }

    pub fn register_connection(&self, ip: &IpAddr) -> bool {
        if self.limiter.consume_tokens(*ip, 1).is_ok() {
            debug!("Request from IP {ip:?} allowed");
            true // Request allowed
        } else {
            debug!("Request from IP {ip:?} blocked");
            false // Request blocked
        }
    }
```

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L57-81)
```rust
    #[tokio::test]
    async fn test_connection_rate_limiter() {
        let limiter = ConnectionRateLimiter::new(3, 3, 4);
        let ip1 = IpAddr::V4(Ipv4Addr::new(192, 168, 1, 1));
        assert!(limiter.is_allowed(&ip1));
        assert!(limiter.register_connection(&ip1));
        assert!(limiter.register_connection(&ip1));
        assert!(limiter.is_allowed(&ip1));
        assert!(limiter.register_connection(&ip1));
        assert!(!limiter.is_allowed(&ip1));
        assert!(!limiter.register_connection(&ip1));

        let ip2 = IpAddr::V4(Ipv4Addr::new(192, 168, 1, 2));
        for _ in 0..100 {
            assert!(
                limiter.is_allowed(&ip2),
                "just checking should not mutate state"
            );
        }
        assert!(limiter.register_connection(&ip2));
        assert!(limiter.register_connection(&ip2));
        assert!(limiter.is_allowed(&ip2));
        assert!(limiter.register_connection(&ip2));
        assert!(!limiter.is_allowed(&ip2));
    }
```
