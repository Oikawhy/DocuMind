ui = false

storage "file" {
  path = "/openbao/data"
}

listener "tcp" {
  address = "0.0.0.0:8200"
  tls_disable = true
  telemetry {
    unauthenticated_metrics_access = false
  }
}

api_addr = "http://openbao:8200"
