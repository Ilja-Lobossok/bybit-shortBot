locals {
  performance_dashboard = jsondecode(replace(replace(replace(
    file("${path.module}/dashboards/trading-performance.json"),
    "__METRICS_DATASOURCE_UID__", var.metrics_datasource_uid),
    "__ENVIRONMENT__", var.environment),
    "__ACCOUNT_ALIAS__", var.performance_account_alias
  ))
}

variable "performance_dashboard_enabled" {
  description = "Metrics-only dashboard using the existing datasource. Does not restart Render."
  type        = bool
  default     = true
}

variable "performance_account_alias" {
  description = "Matches PERFORMANCE_ACCOUNT_ALIAS; primary is the runtime default."
  type        = string
  default     = "primary"
  validation {
    condition     = can(regex("^[A-Za-z0-9_-]{1,32}$", var.performance_account_alias))
    error_message = "Use a bounded safe account alias."
  }
}

resource "grafana_dashboard" "trading_performance" {
  count       = var.performance_dashboard_enabled ? 1 : 0
  folder      = grafana_folder.bot_health.uid
  config_json = jsonencode(local.performance_dashboard)
  overwrite   = false
  message     = "Metrics-only Trading Dashboard managed as code"
  depends_on  = [data.grafana_data_source.metrics]
  lifecycle {
    prevent_destroy = true
  }
}

output "trading_performance_dashboard_url" {
  value       = var.performance_dashboard_enabled ? "${trimsuffix(var.grafana_url, "/")}/d/bybit-trading-performance" : null
  description = "After an approved apply only; no evidence of live data."
}
