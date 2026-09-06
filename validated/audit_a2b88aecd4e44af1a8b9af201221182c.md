[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** stacks-signer/src/monitoring/server.rs (L116-171)
```rust
    pub fn main_loop(&mut self) -> Result<(), MonitoringError> {
        info!("{}: Starting Prometheus metrics server", self);
        loop {
            if let Err(err) = self.refresh_metrics() {
                error!("Monitoring: Error refreshing metrics: {:?}", err);
            }
            let request = match self.http_server.recv() {
                Ok(request) => request,
                Err(err) => {
                    error!("Monitoring: Error receiving request: {:?}", err);
                    return Err(MonitoringError::Terminated);
                }
            };

            debug!("{}: received request {}", self, request.url());

            if request.url() == "/metrics" {
                let response = HttpResponse::from_string(gather_metrics_string());
                request.respond(response).expect("Failed to send response");
                continue;
            }

            if request.url() == "/info" {
                request
                    .respond(HttpResponse::from_string(self.get_info_response()))
                    .expect("Failed to respond to request");
                continue;
            }

            // return 200 OK for "/"
            if request.url() == "/" {
                request
                    .respond(HttpResponse::from_string("OK"))
                    .expect("Failed to respond to request");
                continue;
            }

            // Run heartbeat check to test connection to the node
            if request.url() == "/heartbeat" {
                let (msg, status) = if self.heartbeat() {
                    ("OK", 200)
                } else {
                    ("Failed", 500)
                };
                request
                    .respond(HttpResponse::from_string(msg).with_status_code(status))
                    .expect("Failed to respond to request");
                continue;
            }

            // unknown request, return 404
            request
                .respond(HttpResponse::from_string("Not Found").with_status_code(404))
                .expect("Failed to respond to request");
        }
    }
```

**File:** stacks-signer/src/monitoring/mod.rs (L178-184)
```rust
        let _ = std::thread::Builder::new()
            .name("signer_metrics".to_string())
            .spawn(move || {
                if let Err(monitoring_err) = super::server::MonitoringServer::start(&config) {
                    error!("Monitoring: Error in metrics server: {:?}", monitoring_err);
                }
            });
```
