resource "grafana_rule_group" "bot_health" {
  name               = "bybit-bot-health"
  folder_uid         = grafana_folder.bot_health.uid
  interval_seconds   = local.alert_catalog.evaluation_interval_seconds
  disable_provenance = false
  depends_on         = [data.grafana_data_source.metrics]

  dynamic "rule" {
    for_each = local.alert_catalog.rules
    content {
      uid            = "bybit-health-${rule.value.id}"
      name           = rule.value.name
      condition      = "B"
      for            = rule.value.pending
      is_paused      = !var.alerts_enabled
      no_data_state  = rule.value.no_data
      exec_err_state = "Error"
      annotations = {
        summary          = rule.value.summary
        description      = rule.value.description
        __dashboardUid__ = local.dashboard.uid
        __panelId__      = tostring(rule.value.panel_id)
        runbook_url      = "https://github.com/Ilja-Lobossok/bybit-shortBot/blob/main/docs/monitoring-stage3-runbook.md#${rule.value.id}"
      }
      labels = {
        bot         = "bybit-shortbot"
        component   = "trading-bot"
        environment = var.environment
        severity    = rule.value.severity
        managed_by  = "terraform"
      }

      data {
        ref_id         = "A"
        datasource_uid = var.metrics_datasource_uid
        relative_time_range {
          from = 3600
          to   = 0
        }
        model = jsonencode({
          refId         = "A"
          expr          = replace(rule.value.expr, "__ENVIRONMENT__", var.environment)
          instant       = true
          range         = false
          editorMode    = "code"
          intervalMs    = 1000
          maxDataPoints = 43200
          datasource    = { type = "prometheus", uid = var.metrics_datasource_uid }
        })
      }
      data {
        ref_id         = "B"
        datasource_uid = "__expr__"
        relative_time_range {
          from = 0
          to   = 0
        }
        model = jsonencode({
          refId      = "B"
          type       = "threshold"
          expression = "A"
          datasource = { type = "__expr__", uid = "__expr__" }
          conditions = [{
            evaluator = { type = "gt", params = [local.alert_catalog.condition_threshold] }
            operator  = { type = "and" }
            query     = { params = ["B"] }
            reducer   = { type = "last", params = [] }
            type      = "query"
          }]
        })
      }

      # Direct routing avoids ownership/overwrite of the global policy tree.
      dynamic "notification_settings" {
        for_each = trimspace(var.alert_contact_point_name) == "" ? [] : [var.alert_contact_point_name]
        content {
          contact_point   = notification_settings.value
          group_by        = ["grafana_folder", "alertname"]
          group_wait      = "30s"
          group_interval  = "5m"
          repeat_interval = "4h"
        }
      }
    }
  }
  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = !var.alerts_enabled || trimspace(var.alert_contact_point_name) != ""
      error_message = "Enabling alerts requires an explicitly selected existing notification-only contact point."
    }
  }
}
