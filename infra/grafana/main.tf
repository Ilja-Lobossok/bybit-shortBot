locals {
  dashboard = jsondecode(replace(replace(
    file("${path.module}/dashboards/bot-health.json"),
    "__METRICS_DATASOURCE_UID__", var.metrics_datasource_uid),
    "__ENVIRONMENT__", var.environment
  ))
  alert_catalog = jsondecode(file("${path.module}/alerts/bot-health.rules.json"))
}

data "grafana_data_source" "metrics" {
  uid = var.metrics_datasource_uid
  lifecycle {
    postcondition {
      condition     = self.type == "prometheus"
      error_message = "The existing datasource must be Prometheus-compatible Cloud Metrics."
    }
  }
}

resource "grafana_folder" "bot_health" {
  uid   = "bybit-monitoring"
  title = "Bybit Bot Monitoring"
  lifecycle {
    prevent_destroy = true
  }
}

resource "grafana_dashboard" "bot_health" {
  folder      = grafana_folder.bot_health.uid
  config_json = jsonencode(local.dashboard)
  overwrite   = false
  message     = "Bot Health managed as code (monitoring stage 3)"
  depends_on  = [data.grafana_data_source.metrics]
  lifecycle {
    prevent_destroy = true
  }
}
