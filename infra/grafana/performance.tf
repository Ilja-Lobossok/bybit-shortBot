locals {
  performance_dashboard = jsondecode(replace(replace(replace(replace(
    file("${path.module}/dashboards/trading-performance.json"),
    "__METRICS_DATASOURCE_UID__", var.metrics_datasource_uid),
    "__LOGS_DATASOURCE_UID__", var.logs_datasource_uid),
    "__ENVIRONMENT__", var.environment),
    "__ACCOUNT_ALIAS__", var.performance_account_alias
  ))
}

variable "performance_dashboard_enabled" {
  description = "Explicit opt-in after review. Does not enable collection or restart Render."
  type        = bool
  default     = false
}

variable "logs_datasource_uid" {
  description = "UID (not name) of the existing Cloud Loki datasource; verify in Grafana."
  type        = string
  default     = ""
  validation {
    condition     = var.logs_datasource_uid == "" || can(regex("^[A-Za-z0-9_-]{1,40}$", var.logs_datasource_uid))
    error_message = "Use the existing Loki UID without paths or credentials."
  }
}

variable "performance_account_alias" {
  description = "Matches PERFORMANCE_ACCOUNT_ALIAS, never a real account UID or credential."
  type        = string
  default     = "primary"
  validation {
    condition     = can(regex("^[A-Za-z0-9_-]{1,32}$", var.performance_account_alias))
    error_message = "Use a bounded safe account alias."
  }
}

data "grafana_data_source" "performance_logs" {
  count = var.performance_dashboard_enabled && var.logs_datasource_uid != "" ? 1 : 0
  uid   = var.logs_datasource_uid
  lifecycle {
    postcondition {
      condition     = self.type == "loki"
      error_message = "Performance tables require the existing Loki datasource."
    }
  }
}

resource "grafana_dashboard" "trading_performance" {
  count       = var.performance_dashboard_enabled ? 1 : 0
  folder      = grafana_folder.bot_health.uid
  config_json = jsonencode(local.performance_dashboard)
  overwrite   = false
  message     = "Trading Performance stage 4 managed as code"
  depends_on  = [data.grafana_data_source.metrics, data.grafana_data_source.performance_logs]
  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = var.logs_datasource_uid != ""
      error_message = "Set the verified existing Loki UID before enabling the dashboard."
    }
  }
}

output "trading_performance_dashboard_url" {
  value       = var.performance_dashboard_enabled ? "${trimsuffix(var.grafana_url, "/")}/d/bybit-trading-performance" : null
  description = "After an approved apply only; no evidence of live data."
}
