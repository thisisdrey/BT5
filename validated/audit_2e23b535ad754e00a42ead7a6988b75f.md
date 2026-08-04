[1](#0-0)

### Citations

**File:** substrate/frame/referenda/src/lib.rs (L29-55)
```rust
//! A referendum's lifecycle has three main stages: Preparation, deciding and conclusion.
//! Referenda are considered "ongoing" immediately after submission until their eventual
//! conclusion, and votes may be cast throughout.
//!
//! In order to progress from preparating to being decided, three things must be in place:
//! - There must have been a *Decision Deposit* placed, an amount determined by the track. Anyone
//! may place this deposit.
//! - A period must have elapsed since submission of the referendum. This period is known as the
//! *Preparation Period* and is determined by the track.
//! - The track must not already be at capacity with referendum being decided. The maximum number of
//! referenda which may be being decided simultaneously is determined by the track.
//!
//! In order to become concluded, one of three things must happen:
//! - The referendum should remain in an unbroken _Passing_ state for a period of time. This
//! is known as the _Confirmation Period_ and is determined by the track. A referendum is considered
//! _Passing_ when there is a sufficiently high support and approval, given the amount of time it
//! has been being decided. Generally the threshold for what counts as being "sufficiently high"
//! will reduce over time. The curves setting these thresholds are determined by the track. In this
//! case, the referendum is considered _Approved_ and the proposal is scheduled for dispatch.
//! - The referendum reaches the end of its deciding phase outside not _Passing_. It ends in
//! rejection and the proposal is not dispatched.
//! - The referendum is cancelled.
//!
//! A general time-out is also in place and referenda which exist in preparation for too long may
//! conclude without ever entering into a deciding stage.
//!
//! Once a referendum is concluded, the decision deposit may be refunded.
```
