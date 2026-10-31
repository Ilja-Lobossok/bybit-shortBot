mock_provider "grafana" {
  mock_data "grafana_data_source" {
    defaults = { type = "prometheus" }
  }
}

variables {
  grafana_url = "https://offline.grafana.net"
}

run "paused_by_default" {
  command = plan
  assert {
    condition     = length(grafana_rule_group.bot_health.rule) == 8 && alltrue([for r in grafana_rule_group.bot_health.rule : r.is_paused])
    error_message = "All eight rules must be paused by default."
  }
  assert {
    condition     = alltrue([for r in grafana_rule_group.bot_health.rule : length(r.notification_settings) == 0 && r.exec_err_state == "Error"])
    error_message = "Default rules must not select a receiver or hide query errors."
  }
  assert {
    condition     = jsondecode(grafana_dashboard.bot_health.config_json).uid == "bybit-bot-health" && !grafana_dashboard.bot_health.overwrite
    error_message = "Dashboard UID must be stable and existing dashboards must not be overwritten on creation."
  }
  assert {
    condition     = alltrue([for r in grafana_rule_group.bot_health.rule : r.labels.environment == "production" && r.condition == "B"])
    error_message = "Alert scope/condition drifted."
  }
  assert {
    condition     = alltrue([for r in grafana_rule_group.bot_health.rule : r.uid != "bybit-health-heartbeat" || r.no_data_state == "Alerting"])
    error_message = "Heartbeat must not report healthy on NoData."
  }
  assert {
    condition     = alltrue(flatten([for r in grafana_rule_group.bot_health.rule : [for d in r.data : d.ref_id != "B" || (try(jsondecode(d.model).type, "") == "threshold" && try(jsondecode(d.model).conditions[0].evaluator.params[0], 0) == 0.5)]]))
    error_message = "Grafana B must implement the same >0.5 threshold tested by promtool."
  }
}

run "enable_requires_contact_point" {
  command = plan
  variables {
    alerts_enabled = true
  }
  expect_failures = [grafana_rule_group.bot_health]
}

run "explicit_routing_only" {
  command = plan
  variables {
    alerts_enabled           = true
    alert_contact_point_name = "existing-offline-test-receiver"
  }
  assert {
    condition     = alltrue([for r in grafana_rule_group.bot_health.rule : !r.is_paused && r.notification_settings[0].contact_point == "existing-offline-test-receiver"])
    error_message = "Enabled alerts must route only to the explicitly chosen existing receiver."
  }
}

run "label_injection_rejected" {
  command = plan
  variables {
    environment = "production\"} or vector(1)"
  }
  expect_failures = [var.environment]
}

run "performance_opt_in" {
  command = plan
  variables {
    performance_dashboard_enabled = true
    logs_datasource_uid           = "offline-loki"
  }
  override_data {
    target = data.grafana_data_source.performance_logs[0]
    values = { type = "loki" }
  }
  assert {
    condition     = jsondecode(grafana_dashboard.trading_performance[0].config_json).uid == "bybit-trading-performance" && !grafana_dashboard.trading_performance[0].overwrite
    error_message = "Performance dashboard must have its own stable UID."
  }
  assert {
    condition     = alltrue([for r in grafana_rule_group.bot_health.rule : r.is_paused])
    error_message = "Adding performance dashboard must not enable health alerts."
  }
}

run "performance_requires_loki" {
  command = plan
  variables {
    performance_dashboard_enabled = true
  }
  expect_failures = [grafana_dashboard.trading_performance]
}
