` -> `event_receiver.stop_signal.store(true, ...)` -> `Err(EventError::Terminated)` -> `main_loop` breaks. Equality/fault: is the sender of a `/shutdown` request ever checked against
