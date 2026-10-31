output "dashboard_url" {
  value       = "${trimsuffix(var.grafana_url, "/")}/d/${local.dashboard.uid}"
  description = "URL after an approved apply; not evidence of an existing dashboard."
}

output "alerting_mode" {
  value       = var.alerts_enabled ? "enabled with explicit contact point" : "paused (default)"
  description = "Paused rules do not evaluate or notify."
}

output "rule_count" {
  value = length(local.alert_catalog.rules)
}
