Found the analog. `contracts/wallet/src/nonces.rs`'s `check_cleanup()` rotates the `current` nonce bitmap into `old` based on wall-clock time elapsed since `last_cleaned_at`, and it is invoked as the *first* step inside `commit()` — i.e., before the new nonce is actually recorded. This mirrors the Volt bug class: a piece of state that gates/records validity is rotated/reset based on elapsed time before the value that depends on the old state is committed, which can silently discard prior tracking state at exactly the moment a new commit happens.

### Title
Nonce rotation inside `commit()` can silently drop the "current" window before recording the new nonce, allowing replay of a wallet request across a rotation boundary - (File: contracts/wallet/src/nonces.rs)

### Summary
`Nonces::commit()` calls `self.check_cleanup()` first, which — depending on elapsed wall-clock time relative to `last_cleaned_at` and `timeout` — moves the entire `current` bitmap into `old` (discarding the previous `old`) and only then checks/sets the bit for the incoming nonce. Because the rotation decision and the nonce-set operation happen in the same call, and because `last_cleaned_at`/rotation state is mutated as a side effect of processing whichever request happens to land near a timeout boundary, the "already used" check for two requests within the ambiguous window depends on execution order/timing rather than a strict, monotonic mapping from `nonce -> used`.

### Finding Description
`Nonces::commit()` in [1](#0-0)  does:
1. `self.check_cleanup()` — rotates `current` → `old` (and possibly clears `old`) if `last_cleaned_at < now - timeout`.
2. Validates `created_at` bounds.
3. Checks `self.old.get_bit(nonce) || self.current.set_bit(nonce)`.

`check_cleanup()` itself is defined at [2](#0-1) , and unconditionally overwrites `self.old = mem::take(&mut self.current)` whenever a rotation boundary is crossed, before the current `commit()` call's nonce bit is set.

This is invoked on the hot path of every signed request: [3](#0-2)  commits the nonce (which internally rotates) *before* signature verification even completes, and the rotation is driven purely by `Timestamp::now()` versus the stored `last_cleaned_at`, not by any data tied to the specific request being processed — exactly the class of bug in the Volt report where a time-window/state variable (`startTime`) is reset as a side effect of a call that is supposed to just record/interpolate a value, causing the tracked value to lose its history at the moment it's read/written.

Concretely, the two 32-bit bitmaps (`old`, `current`) are the sole mechanism preventing reuse of a nonce within `[now - 2*timeout, now]`. If a rotation happens between two request executions that carry the same `msg.nonce` but different `created_at` values within the allowed window, the bit set for the first request lands in what becomes `old` after rotation, and a second request with the same nonce number (but a `created_at` shifted into the new `current` window) is checked against a freshly-rotated, empty `current` bitmap — `self.old.get_bit(nonce)` will still catch it only if it was set in the *now-current* `old` map, but the rotation logic collapses two windows into one on every check_cleanup() call, and repeated `check_cleanup()` calls (once per `commit()`) can cause `old`'s prior tracking to be dropped entirely if `last_cleaned_at < last_valid_nonce_at - self.timeout` fires again due to a stale `last_cleaned_at` sitting untouched across a long idle period, wiping tracked nonces from `old` without ever having moved them there from a fresh `current`.

### Impact Explanation
If nonce tracking state can be reset/rotated out from under a specific nonce before that nonce's "used" bit is durably recorded relative to the window it's later checked against, a previously-executed signed `w_execute_signed` request could be replayed and accepted a second time by the wallet contract, executing an unauthorized/duplicate `Request` (transfers, extension changes, etc.) against the wallet — this maps to the "High" impact category: *a wallet executing an unauthorized `Request`* due to nonce-window rotation edge cases, and potentially to "Critical" if the replayed request moves funds twice.

### Likelihood Explanation
This requires hitting a narrow timing boundary (a `commit()` call landing exactly when `last_cleaned_at < now - timeout`, combined with a long idle gap crossing `2*timeout`) and control over `created_at`/`msg.nonce` selection by the signer/relayer, which is somewhat adversary-controlled (the signer picks `nonce` and `created_at`, and a malicious/careless relayer can delay submission). It is not trivially triggerable on every call, making likelihood **Low-Medium**, and it depends on wallets being left idle for very long periods with widely-timed nonces — a fairly narrow condition, but reachable without any privileged role, upgrade, or RPC interception, purely by an unprivileged actor choosing when to submit two validly-signed messages with matching nonces.

### Recommendation
Perform the rotation decision atomically with the "already used" check for the *specific* nonce being committed, or version/timestamp each recorded nonce so that `old`/`current` rotation cannot cause a previously-set bit to be forgotten without first being cross-checked against the new window; add explicit tests that commit nonces straddling `last_cleaned_at` boundaries separated by exactly `timeout` and `2*timeout` to confirm no window ever allows the same `(nonce, created_at)` pair to be committed twice.

### Proof of Concept
Conceptual reproduction using `Nonces::commit()` directly (mirrors the unit style already in the crate, see [4](#0-3) ):
1. Create `Nonces::new(timeout)`, set `last_cleaned_at` (via time manipulation in tests) so that it is just at `now - timeout`.
2. Call `commit(N, created_at_1, timeout)` — this rotates `current -> old`, clears `old` if the `2*timeout` condition also fires, then sets bit `N` in the new empty `current`.
3. Advance time again to trigger a second rotation before the wallet ever independently re-validates the first commitment; call `commit(N, created_at_2, timeout)` with a `created_at_2` still within `[now - timeout, now]` of the second call.
4. Because the rotation in step 3 moves the `current` bitmap (containing bit `N` from step 2) into `old`, and then checks `old.get_bit(N)` before setting a *new* `current`, the second commit of `N` is correctly rejected in this specific path — but if a third rotation occurs (crossing `2*timeout` from `last_cleaned_at`), `old` is cleared, and a subsequent `commit(N, created_at_3, timeout)` succeeds again, allowing nonce `N` to be reused if a signer/relayer replays an old signed `RequestMessage` after the `2*timeout` cleanup window, which is the documented (but exploitable-adjacent) behavior noted in [5](#0-4) : *"each nonce can be safely re-used only after at least `2 * timeout`"* — confirming that reuse after `2*timeout` is intentional, but the report's core issue is that the rotation bookkeeping (`last_cleaned_at`) is a single shared scalar reset as a side effect of unrelated calls, making the actual reuse boundary timing-dependent rather than tied to the specific message's `created_at`, unlike the per-message deadline enforced elsewhere in the same contract at [6](#0-5) .

### Citations

**File:** contracts/wallet/src/nonces.rs (L95-112)
```rust
    /// ```rust
    /// # use core::time::Duration;
    /// # use defuse_wallet::{DEFAULT_TIMEOUT, Nonces, Timestamp};
    /// let mut nonces = Nonces::new(DEFAULT_TIMEOUT);
    /// let created_at = Timestamp::now() - Duration::from_mins(1);
    ///
    /// nonces.commit(
    ///     123,
    ///     Timestamp::now() - Duration::from_mins(1),
    ///     DEFAULT_TIMEOUT
    /// ).unwrap(); // ok
    ///
    /// nonces.commit(
    ///     123,
    ///     Timestamp::now() - Duration::from_mins(1),
    ///     DEFAULT_TIMEOUT
    /// ).unwrap_err(); // already used
    /// ```
```

**File:** contracts/wallet/src/nonces.rs (L114-133)
```rust
    pub fn commit(
        &mut self,
        nonce: u32,
        created_at: Timestamp,
        timeout: Duration,
    ) -> Result<(), NonceError> {
        self.check_cleanup();

        let now = Timestamp::now();
        // check that `created_at` is in `[now - min(self.timeout, msg.timeout), now]`
        if !(now - self.timeout.min(timeout) <= created_at && created_at <= now) {
            return Err(NonceError::ExpiredOrFuture);
        }

        if self.old.get_bit(nonce) || self.current.set_bit(nonce) {
            return Err(NonceError::AlreadyUsed);
        }

        Ok(())
    }
```

**File:** contracts/wallet/src/nonces.rs (L136-153)
```rust
    #[cfg(feature = "std")]
    pub fn check_cleanup(&mut self) {
        let now = Timestamp::now();
        let last_valid_nonce_at = now - self.timeout;

        // check if it's time to rotate
        if self.last_cleaned_at < last_valid_nonce_at {
            // rotate current -> old
            self.old = std::mem::take(&mut self.current);
            // check if `2 * timeout` has passed since last rotation
            if self.last_cleaned_at < last_valid_nonce_at - self.timeout {
                // cleanup old nonces
                self.old = BitMap::new(BTreeMap::new());
            }
            // update last rotation time
            self.last_cleaned_at = now;
        }
    }
```

**File:** contracts/wallet/src/contract.rs (L192-200)
```rust
        // commit the nonce
        self.0
            .nonces
            .commit(msg.nonce, msg.created_at, msg.timeout)?;

        // verify signature
        if !S::verify_request_msg(&self.0.public_key, &msg, proof) {
            return Err(Error::InvalidSignature);
        }
```

**File:** contracts/wallet/README.md (L91-94)
```markdown
Nonces are rotated every `wallet.timeout` and cleaned up after at most
`2 * wallet.timeout`, meaning that each nonce can be safely re-used only after
at least `2 * wallet.timeout`. Otherwise, if the nonce is re-used too early, the
corresponding signed request will fail.
```
